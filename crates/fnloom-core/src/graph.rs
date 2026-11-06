//! Graph construction: id assignment, input synthesis and cycle detection.

use std::collections::{BTreeMap, BTreeSet};

use crate::error::{BuildError, PlanError};
use crate::node::{Dependency, Edge, Node, NodeId, NodeKind, NodeSpec};

/// An immutable, acyclic dependency graph whose node ids follow sorted name order.
#[derive(Debug)]
pub struct Graph<P> {
    nodes: Vec<Node<P>>,
    ids: BTreeMap<String, NodeId>,
    /// For each node, the nodes that read from it, in id order.
    dependents: Vec<Vec<NodeId>>,
    /// Whether any node may be absent, so a plan of a graph without one skips the question.
    absence: bool,
}

impl<P> Graph<P> {
    /// Builds a graph from specs.
    ///
    /// Names that are depended on but not declared become `NodeKind::Input` nodes.
    ///
    /// # Errors
    ///
    /// `BuildError::DuplicateName` when two specs share a name, `BuildError::ConflictingInput`
    /// when a spec lists one input twice with different dependencies, `BuildError::Cycle` when
    /// the dependencies are cyclic (a self-dependency counts).
    pub fn build(specs: Vec<NodeSpec<P>>) -> Result<Self, BuildError> {
        let mut declared = index_by_name(specs)?;
        let ids = assign_ids(&declared);
        // A loop rather than a `Result` collect, which cannot reserve the exact length.
        let mut nodes = Vec::with_capacity(ids.len());
        for (name, &id) in &ids {
            nodes.push(make_node(id, name, declared.remove(name), &ids)?);
        }
        if let Some(path) = find_cycle(&nodes) {
            return Err(BuildError::Cycle {
                path: path
                    .into_iter()
                    .map(|id| nodes[id.index()].name.clone())
                    .collect(),
            });
        }
        let absence = mark_may_be_absent(&mut nodes);
        let dependents = forward_adjacency(&nodes);
        Ok(Self {
            nodes,
            ids,
            dependents,
            absence,
        })
    }

    /// All nodes, indexed by `NodeId`.
    #[must_use]
    pub fn nodes(&self) -> &[Node<P>] {
        &self.nodes
    }

    /// The node with this id.
    ///
    /// # Panics
    ///
    /// Panics when `id` did not come from this graph.
    #[must_use]
    pub fn node(&self, id: NodeId) -> &Node<P> {
        &self.nodes[id.index()]
    }

    /// The id of the node called `name`, if any.
    #[must_use]
    pub fn id_of(&self, name: &str) -> Option<NodeId> {
        self.ids.get(name).copied()
    }

    /// Nodes the named nodes depend on, directly or transitively, excluding the named nodes.
    ///
    /// # Errors
    ///
    /// `PlanError::UnknownNodes` listing every unknown name.
    pub fn upstream_of(&self, names: &[&str]) -> Result<Vec<NodeId>, PlanError> {
        let starts = self.lookup(names.iter().copied(), PlanError::UnknownNodes, |_| true)?;
        Ok(self.reach_upstream(&starts))
    }

    /// Nodes that depend on the named nodes, directly or transitively, excluding the named nodes.
    ///
    /// # Errors
    ///
    /// `PlanError::UnknownNodes` listing every unknown name.
    pub fn downstream_of(&self, names: &[&str]) -> Result<Vec<NodeId>, PlanError> {
        let starts = self.lookup(names.iter().copied(), PlanError::UnknownNodes, |_| true)?;
        Ok(self.reach_downstream(&starts))
    }

    /// Nodes strictly between `from` and `to`: on some path from `from` to `to`, endpoints excluded.
    ///
    /// # Errors
    ///
    /// `PlanError::UnknownNodes` listing every unknown name among `from` and `to`.
    pub fn between(&self, from: &str, to: &str) -> Result<Vec<NodeId>, PlanError> {
        self.lookup([from, to], PlanError::UnknownNodes, |_| true)?;
        let (Some(from), Some(to)) = (self.id_of(from), self.id_of(to)) else {
            return Ok(Vec::new());
        };
        let above = self.reach_upstream(&[to]);
        Ok(self
            .reach_downstream(&[from])
            .into_iter()
            .filter(|id| above.binary_search(id).is_ok())
            .collect())
    }

    /// Whether any node of the graph may be absent (`Node::may_be_absent`).
    pub(crate) const fn has_absence(&self) -> bool {
        self.absence
    }

    /// The nodes that read from `id`, in id order.
    pub(crate) fn dependents(&self, id: NodeId) -> &[NodeId] {
        self.dependents.get(id.index()).map_or(&[], Vec::as_slice)
    }

    /// Ids of the names whose node passes `accept`, sorted and de-duplicated; otherwise every
    /// other name, sorted and de-duplicated, wrapped by `error`.
    pub(crate) fn lookup<'a>(
        &self,
        names: impl IntoIterator<Item = &'a str>,
        error: fn(Vec<String>) -> PlanError,
        accept: fn(&Node<P>) -> bool,
    ) -> Result<Vec<NodeId>, PlanError> {
        let mut found = BTreeSet::new();
        let mut rejected = BTreeSet::new();
        for name in names {
            match self.id_of(name).filter(|&id| accept(self.node(id))) {
                Some(id) => found.insert(id),
                None => rejected.insert(name),
            };
        }
        if rejected.is_empty() {
            Ok(found.into_iter().collect())
        } else {
            Err(error(rejected.into_iter().map(str::to_owned).collect()))
        }
    }

    fn reach_upstream(&self, starts: &[NodeId]) -> Vec<NodeId> {
        self.reach(starts, |id| {
            self.node(id).inputs.iter().map(|edge| edge.node)
        })
    }

    fn reach_downstream(&self, starts: &[NodeId]) -> Vec<NodeId> {
        self.reach(starts, |id| self.dependents(id).iter().copied())
    }

    /// Iterative worklist reachability; the result is sorted and excludes `starts` themselves.
    fn reach<I>(&self, starts: &[NodeId], neighbours: impl Fn(NodeId) -> I) -> Vec<NodeId>
    where
        I: Iterator<Item = NodeId>,
    {
        let mut seen = vec![false; self.nodes.len()];
        for start in starts {
            seen[start.index()] = true;
        }
        let mut found = Vec::new();
        let mut work = starts.to_vec();
        while let Some(current) = work.pop() {
            for next in neighbours(current) {
                if !std::mem::replace(&mut seen[next.index()], true) {
                    found.push(next);
                    work.push(next);
                }
            }
        }
        found.sort_unstable();
        found
    }
}

/// Inverts the input edges: for each node, who reads from it.
fn forward_adjacency<P>(nodes: &[Node<P>]) -> Vec<Vec<NodeId>> {
    let mut dependents = vec![Vec::new(); nodes.len()];
    for node in nodes {
        for edge in &node.inputs {
            dependents[edge.node.index()].push(node.id);
        }
    }
    dependents
}

/// Sets `may_be_absent` on every node that declines or reads, by a `Required` edge, a node
/// that may be absent.
///
/// The flag is a reachability from the declining nodes along `Required` edges, so an
/// iterative worklist finds it in any order, in time linear in the graph. A graph where no
/// node declines costs one scan. Returns whether any node may be absent.
fn mark_may_be_absent<P>(nodes: &mut [Node<P>]) -> bool {
    let mut work: Vec<NodeId> = Vec::new();
    for node in nodes.iter_mut().filter(|node| node.declines) {
        node.may_be_absent = true;
        work.push(node.id);
    }
    if work.is_empty() {
        return false;
    }
    // For each node, the consumers reading it by a `Required` edge.
    let mut required_by = vec![Vec::new(); nodes.len()];
    for node in nodes.iter() {
        for edge in &node.inputs {
            if edge.dependency == Dependency::Required {
                required_by[edge.node.index()].push(node.id);
            }
        }
    }
    while let Some(id) = work.pop() {
        for &consumer in &required_by[id.index()] {
            let node = &mut nodes[consumer.index()];
            if !node.may_be_absent {
                node.may_be_absent = true;
                work.push(consumer);
            }
        }
    }
    true
}

fn index_by_name<P>(specs: Vec<NodeSpec<P>>) -> Result<BTreeMap<String, NodeSpec<P>>, BuildError> {
    let mut declared = BTreeMap::new();
    for spec in specs {
        let name = spec.name.clone();
        if declared.insert(name.clone(), spec).is_some() {
            return Err(BuildError::DuplicateName { name });
        }
    }
    Ok(declared)
}

/// Ids over declared and depended-on names together, in sorted order.
fn assign_ids<P>(declared: &BTreeMap<String, NodeSpec<P>>) -> BTreeMap<String, NodeId> {
    let names: BTreeSet<&String> = declared
        .iter()
        .flat_map(|(name, spec)| std::iter::once(name).chain(spec.inputs.iter().map(|i| &i.name)))
        .collect();
    names.into_iter().cloned().zip((0..).map(NodeId)).collect()
}

/// The node `name` with `id`: a function for a declared spec, an input otherwise.
///
/// `BuildError::ConflictingInput` when the spec lists one input twice, differently.
fn make_node<P>(
    id: NodeId,
    name: &str,
    spec: Option<NodeSpec<P>>,
    ids: &BTreeMap<String, NodeId>,
) -> Result<Node<P>, BuildError> {
    Ok(match spec {
        Some(spec) => Node {
            id,
            inputs: resolve_edges(&spec, ids)?,
            name: spec.name,
            kind: NodeKind::Function(spec.payload),
            tags: spec.tags,
            declines: spec.declines,
            may_be_absent: false,
        },
        None => Node {
            id,
            name: name.to_owned(),
            kind: NodeKind::Input,
            inputs: Vec::new(),
            tags: BTreeMap::new(),
            declines: false,
            may_be_absent: false,
        },
    })
}

/// Resolves `spec`'s inputs to edges, keeping one of an input listed twice alike.
///
/// `BuildError::ConflictingInput` for an input listed twice with different dependencies: no
/// one of them says how the node reads it, so neither is kept.
fn resolve_edges<P>(
    spec: &NodeSpec<P>,
    ids: &BTreeMap<String, NodeId>,
) -> Result<Vec<Edge>, BuildError> {
    // At least four, the capacity a first push gives, so a node with few inputs allocates
    // as it always has.
    let mut edges = Vec::with_capacity(spec.inputs.len().max(4));
    for input in &spec.inputs {
        if let Some(&node) = ids.get(&input.name) {
            edges.push(Edge {
                node,
                dependency: input.dependency,
            });
        }
    }
    let Some(repeats) = repeated_edges(&edges) else {
        return Ok(edges);
    };
    // The first conflicting repeat in input order is the one reported.
    if let Some(&(_, at)) = repeats
        .iter()
        .find(|&&(first, at)| edges[first].dependency != edges[at].dependency)
    {
        let node = edges[at].node;
        let input = spec
            .inputs
            .iter()
            .find(|input| ids.get(&input.name) == Some(&node))
            .map(|input| input.name.clone())
            .unwrap_or_default();
        return Err(BuildError::ConflictingInput {
            node: spec.name.clone(),
            input,
        });
    }
    let mut keep = vec![true; edges.len()];
    for &(_, at) in &repeats {
        keep[at] = false;
    }
    let mut keep = keep.into_iter();
    edges.retain(|_| keep.next().unwrap_or(true));
    Ok(edges)
}

/// Below this many edges, repeats are found by comparing every pair, which needs no
/// allocation; from it on, by sorting.
const PAIRWISE_LIMIT: usize = 16;

/// Each edge that repeats an earlier edge's node, as `(first, repeat)`: the index of the
/// node's first edge and of the repeat, ordered by the repeat. `None` when no node repeats,
/// the common case, which is found without sorting a short list or allocating.
fn repeated_edges(edges: &[Edge]) -> Option<Vec<(usize, usize)>> {
    if edges.len() < 2 {
        return None;
    }
    if edges.len() < PAIRWISE_LIMIT {
        let repeats: Vec<(usize, usize)> = (1..edges.len())
            .filter_map(|at| {
                let first = edges[..at].iter().position(|e| e.node == edges[at].node)?;
                Some((first, at))
            })
            .collect();
        return (!repeats.is_empty()).then_some(repeats);
    }
    let mut sorted: Vec<(NodeId, usize)> = edges
        .iter()
        .enumerate()
        .map(|(index, edge)| (edge.node, index))
        .collect();
    sorted.sort_unstable();
    if sorted.windows(2).all(|pair| pair[0].0 != pair[1].0) {
        return None;
    }
    let mut repeats = Vec::new();
    for group in sorted.chunk_by(|a, b| a.0 == b.0) {
        let first = group[0].1;
        repeats.extend(group[1..].iter().map(|&(_, at)| (first, at)));
    }
    repeats.sort_unstable_by_key(|&(_, at)| at);
    Some(repeats)
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Colour {
    White,
    Grey,
    Black,
}

/// Iterative three-colour DFS; returns the ids along a cycle, first id repeated last.
fn find_cycle<P>(nodes: &[Node<P>]) -> Option<Vec<NodeId>> {
    let mut colours = vec![Colour::White; nodes.len()];
    nodes
        .iter()
        .find_map(|node| cycle_from(node.id, nodes, &mut colours))
}

fn cycle_from<P>(start: NodeId, nodes: &[Node<P>], colours: &mut [Colour]) -> Option<Vec<NodeId>> {
    if colours[start.index()] != Colour::White {
        return None;
    }
    colours[start.index()] = Colour::Grey;
    // Each frame is a node and the index of its next unvisited input.
    let mut stack = vec![(start, 0_usize)];
    while let Some(frame) = stack.last_mut() {
        let (current, next_input) = *frame;
        let Some(edge) = nodes[current.index()].inputs.get(next_input) else {
            colours[current.index()] = Colour::Black;
            stack.pop();
            continue;
        };
        frame.1 += 1;
        match colours[edge.node.index()] {
            Colour::White => {
                colours[edge.node.index()] = Colour::Grey;
                stack.push((edge.node, 0));
            }
            Colour::Grey => return Some(cycle_path(&stack, edge.node)),
            Colour::Black => {}
        }
    }
    None
}

fn cycle_path(stack: &[(NodeId, usize)], back_to: NodeId) -> Vec<NodeId> {
    let from = stack.iter().position(|&(id, _)| id == back_to).unwrap_or(0);
    stack[from..]
        .iter()
        .map(|&(id, _)| id)
        .chain(std::iter::once(back_to))
        .collect()
}

#[cfg(test)]
#[expect(clippy::unwrap_used, reason = "tests assert by unwrapping")]
mod tests {
    use super::*;

    use crate::node::InputSpec;
    use crate::testing::{declining, names, node, spec};

    #[test]
    fn build_synthesises_input_nodes_for_unresolved_parameters() {
        let graph = Graph::build(vec![node("b", &["a"])]).unwrap();
        let a = graph.node(graph.id_of("a").unwrap());
        assert!(a.is_input());
        assert!(a.payload().is_none());
        let b = graph.node(graph.id_of("b").unwrap());
        assert_eq!(b.kind, NodeKind::Function(()));
        assert_eq!(
            b.inputs.iter().map(|e| e.node).collect::<Vec<_>>(),
            vec![a.id]
        );
    }

    #[test]
    fn build_rejects_duplicate_names() {
        let err = Graph::build(vec![node("x", &[]), node("x", &[])]).unwrap_err();
        assert_eq!(
            err,
            BuildError::DuplicateName {
                name: "x".to_owned()
            }
        );
    }

    #[test]
    fn build_rejects_an_input_listed_twice_with_different_dependencies() {
        let spec = |second| NodeSpec {
            name: "b".to_owned(),
            inputs: vec![
                InputSpec {
                    name: "a".to_owned(),
                    dependency: Dependency::Required,
                },
                InputSpec {
                    name: "a".to_owned(),
                    dependency: second,
                },
            ],
            tags: BTreeMap::new(),
            payload: (),
            declines: false,
        };
        let err = Graph::build(vec![spec(Dependency::Optional)]).unwrap_err();
        assert_eq!(
            err,
            BuildError::ConflictingInput {
                node: "b".to_owned(),
                input: "a".to_owned()
            }
        );
        assert_eq!(
            err.to_string(),
            "node 'b' reads 'a' twice, with different dependencies; list it once"
        );
        // Listed twice alike, it is one edge.
        let graph = Graph::build(vec![spec(Dependency::Required)]).unwrap();
        let b = graph.node(graph.id_of("b").unwrap());
        assert_eq!(b.inputs.len(), 1);
        assert_eq!(b.inputs[0].dependency, Dependency::Required);
    }

    #[test]
    fn build_finds_repeated_inputs_among_many() {
        // More inputs than `PAIRWISE_LIMIT`, so repeats are found by sorting.
        let spec = |repeat: Dependency| {
            let mut inputs: Vec<InputSpec> = (0..20)
                .map(|i| InputSpec {
                    name: format!("x{i:02}"),
                    dependency: Dependency::Required,
                })
                .collect();
            inputs.insert(
                5,
                InputSpec {
                    name: "x12".to_owned(),
                    dependency: Dependency::Required,
                },
            );
            inputs.push(InputSpec {
                name: "x03".to_owned(),
                dependency: repeat,
            });
            inputs.push(InputSpec {
                name: "x07".to_owned(),
                dependency: Dependency::Optional,
            });
            NodeSpec {
                name: "total".to_owned(),
                inputs,
                tags: BTreeMap::new(),
                payload: (),
                declines: false,
            }
        };
        // `x03` conflicts before `x07` does, in input order.
        let err = Graph::build(vec![spec(Dependency::Optional)]).unwrap_err();
        assert_eq!(
            err,
            BuildError::ConflictingInput {
                node: "total".to_owned(),
                input: "x03".to_owned()
            }
        );
        let err = Graph::build(vec![spec(Dependency::Required)]).unwrap_err();
        assert_eq!(
            err,
            BuildError::ConflictingInput {
                node: "total".to_owned(),
                input: "x07".to_owned()
            }
        );
        // Repeated alike, each input is one edge, in the order first listed.
        let mut alike = spec(Dependency::Required);
        alike.inputs.pop();
        let graph = Graph::build(vec![alike]).unwrap();
        let total = graph.node(graph.id_of("total").unwrap());
        let read: Vec<&str> = total
            .inputs
            .iter()
            .map(|edge| graph.node(edge.node).name.as_str())
            .collect();
        let mut expected: Vec<String> = (0..20).map(|i| format!("x{i:02}")).collect();
        expected.insert(5, "x12".to_owned());
        expected.remove(13);
        assert_eq!(read, expected);
    }

    #[test]
    fn build_reports_a_cycle_with_its_path() {
        let err = Graph::build(vec![node("a", &["b"]), node("b", &["a"])]).unwrap_err();
        let BuildError::Cycle { path } = err else {
            unreachable!("expected a cycle, got {err}");
        };
        assert!(path.contains(&"a".to_owned()) && path.contains(&"b".to_owned()));
    }

    #[test]
    fn build_treats_a_self_dependency_as_a_cycle() {
        let err = Graph::build(vec![node("a", &["a"])]).unwrap_err();
        assert!(matches!(err, BuildError::Cycle { .. }));
    }

    #[test]
    fn build_of_zero_specs_is_an_empty_graph() {
        let graph = Graph::<()>::build(Vec::new()).unwrap();
        assert!(graph.nodes().is_empty());
        assert_eq!(graph.id_of("a"), None);
    }

    #[test]
    fn node_ids_follow_sorted_name_order() {
        let graph =
            Graph::build(vec![node("m", &["z"]), node("c", &[]), node("a", &["m"])]).unwrap();
        let names: Vec<&str> = graph.nodes().iter().map(|n| n.name.as_str()).collect();
        assert_eq!(names, ["a", "c", "m", "z"]);
        for (position, node) in graph.nodes().iter().enumerate() {
            assert_eq!(node.id.index(), position);
        }
    }

    #[test]
    fn build_accepts_a_deep_chain_without_recursion() {
        let specs: Vec<_> = (0..50_000)
            .map(|i| node(&format!("n{i:06}"), &[&format!("n{:06}", i + 1)]))
            .collect();
        assert!(Graph::build(specs).is_ok());
    }

    #[test]
    fn may_be_absent_follows_required_edges_only() {
        let graph = Graph::build(vec![
            declining("a", &[], ()),
            spec("b", &[("a", Dependency::Required)]),
            spec("c", &[("b", Dependency::Optional)]),
            spec("d", &[("b", Dependency::Admits { optional: false })]),
        ])
        .unwrap();
        let flags: Vec<(&str, bool, bool)> = graph
            .nodes()
            .iter()
            .map(|n| (n.name.as_str(), n.declines, n.may_be_absent))
            .collect();
        assert_eq!(
            flags,
            [
                ("a", true, true),
                ("b", false, true),
                ("c", false, false),
                ("d", false, false),
            ]
        );
    }

    #[test]
    fn may_be_absent_reaches_down_a_deep_chain_without_recursion() {
        let mut specs: Vec<_> = (0..50_000)
            .map(|i| node(&format!("n{i:06}"), &[&format!("n{:06}", i + 1)]))
            .collect();
        specs.push(declining("n050000", &[], ()));
        let graph = Graph::build(specs).unwrap();
        assert!(graph.nodes().iter().all(|n| n.may_be_absent));
    }

    mod queries {
        use super::*;

        fn diamond() -> Graph<()> {
            Graph::build(vec![
                node("a", &[]),
                node("b", &["a"]),
                node("c", &["a"]),
                node("d", &["b", "c"]),
            ])
            .unwrap()
        }

        #[test]
        fn upstream_of_a_sink_is_every_ancestor() {
            let g = diamond();
            assert_eq!(names(&g, &g.upstream_of(&["d"]).unwrap()), ["a", "b", "c"]);
        }

        #[test]
        fn downstream_of_a_source_is_every_descendant() {
            let g = diamond();
            assert_eq!(
                names(&g, &g.downstream_of(&["a"]).unwrap()),
                ["b", "c", "d"]
            );
        }

        #[test]
        fn between_keeps_only_nodes_on_a_path() {
            let g = diamond();
            assert_eq!(names(&g, &g.between("a", "d").unwrap()), ["b", "c"]);
        }

        #[test]
        fn between_excludes_branches_off_the_path() {
            let g = Graph::build(vec![
                node("b", &["a"]),
                node("c", &["b"]),
                node("side", &["a"]),
                node("tail", &["c"]),
            ])
            .unwrap();
            assert_eq!(names(&g, &g.between("a", "c").unwrap()), ["b"]);
        }

        #[test]
        fn between_with_no_path_or_equal_endpoints_is_empty() {
            let g = diamond();
            assert_eq!(names(&g, &g.between("b", "c").unwrap()), [] as [&str; 0]);
            assert_eq!(names(&g, &g.between("d", "a").unwrap()), [] as [&str; 0]);
            assert_eq!(names(&g, &g.between("b", "b").unwrap()), [] as [&str; 0]);
        }

        #[test]
        fn every_named_start_is_excluded_even_when_one_is_upstream_of_another() {
            let g = diamond();
            assert_eq!(names(&g, &g.upstream_of(&["b", "d"]).unwrap()), ["a", "c"]);
            assert_eq!(
                names(&g, &g.downstream_of(&["a", "b"]).unwrap()),
                ["c", "d"]
            );
        }

        #[test]
        fn an_empty_name_list_gives_an_empty_result() {
            let g = diamond();
            assert_eq!(names(&g, &g.upstream_of(&[]).unwrap()), [] as [&str; 0]);
            assert_eq!(names(&g, &g.downstream_of(&[]).unwrap()), [] as [&str; 0]);
        }

        #[test]
        fn unknown_names_are_reported_sorted_and_deduplicated() {
            let g = diamond();
            let expected = PlanError::UnknownNodes(vec!["nope".to_owned(), "zzz".to_owned()]);
            assert_eq!(
                g.upstream_of(&["zzz", "a", "nope", "zzz"]).unwrap_err(),
                expected
            );
            assert_eq!(g.downstream_of(&["nope", "zzz"]).unwrap_err(), expected);
            assert_eq!(g.between("zzz", "nope").unwrap_err(), expected);
            assert_eq!(
                g.between("a", "nope").unwrap_err(),
                PlanError::UnknownNodes(vec!["nope".to_owned()])
            );
        }

        #[test]
        fn queries_walk_a_deep_chain_without_recursion() {
            let specs: Vec<_> = (0..50_000)
                .map(|i| node(&format!("n{i:06}"), &[&format!("n{:06}", i + 1)]))
                .collect();
            let g = Graph::build(specs).unwrap();
            assert_eq!(g.upstream_of(&["n000000"]).unwrap().len(), 50_000);
            assert_eq!(g.downstream_of(&["n050000"]).unwrap().len(), 50_000);
        }
    }
}
