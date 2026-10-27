//! Graph construction: id assignment, input synthesis and cycle detection.

use std::collections::{BTreeMap, BTreeSet};

use crate::error::{BuildError, PlanError};
use crate::node::{Edge, Node, NodeId, NodeKind, NodeSpec};

/// An immutable, acyclic dependency graph whose node ids follow sorted name order.
#[derive(Debug)]
pub struct Graph<P> {
    nodes: Vec<Node<P>>,
    ids: BTreeMap<String, NodeId>,
    /// For each node, the nodes that read from it, in id order.
    dependents: Vec<Vec<NodeId>>,
}

impl<P> Graph<P> {
    /// Builds a graph from specs.
    ///
    /// Names that are depended on but not declared become `NodeKind::Input` nodes.
    ///
    /// # Errors
    ///
    /// `BuildError::DuplicateName` when two specs share a name, `BuildError::Cycle` when the
    /// dependencies are cyclic (a self-dependency counts).
    pub fn build(specs: Vec<NodeSpec<P>>) -> Result<Self, BuildError> {
        let mut declared = index_by_name(specs)?;
        let ids = assign_ids(&declared);
        let nodes = ids
            .iter()
            .map(|(name, &id)| make_node(id, name, declared.remove(name), &ids))
            .collect::<Vec<_>>();
        if let Some(path) = find_cycle(&nodes) {
            return Err(BuildError::Cycle {
                path: path
                    .into_iter()
                    .map(|id| nodes[id.index()].name.clone())
                    .collect(),
            });
        }
        let dependents = forward_adjacency(&nodes);
        Ok(Self {
            nodes,
            ids,
            dependents,
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

    /// Nodes the named nodes depend on, directly or transitively, excluding the named nodes.
    ///
    /// # Errors
    ///
    /// `PlanError::UnknownNodes` listing every unknown name.
    pub fn upstream_of(&self, names: &[&str]) -> Result<Vec<NodeId>, PlanError> {
        let starts = self.resolve(names)?;
        Ok(self.reach_upstream(&starts))
    }

    /// Nodes that depend on the named nodes, directly or transitively, excluding the named nodes.
    ///
    /// # Errors
    ///
    /// `PlanError::UnknownNodes` listing every unknown name.
    pub fn downstream_of(&self, names: &[&str]) -> Result<Vec<NodeId>, PlanError> {
        let starts = self.resolve(names)?;
        Ok(self.reach_downstream(&starts))
    }

    /// Nodes strictly between `from` and `to`: on some path from `from` to `to`, endpoints excluded.
    ///
    /// # Errors
    ///
    /// `PlanError::UnknownNodes` listing every unknown name among `from` and `to`.
    pub fn between(&self, from: &str, to: &str) -> Result<Vec<NodeId>, PlanError> {
        let ends = self.resolve(&[from, to])?;
        let (Some(&from), Some(&to)) = (ends.first(), ends.last()) else {
            return Ok(Vec::new());
        };
        let below = self.reach_downstream(&[from]);
        let above: BTreeSet<NodeId> = self.reach_upstream(&[to]).into_iter().collect();
        Ok(below.into_iter().filter(|id| above.contains(id)).collect())
    }

    /// Ids of `names`, or every unknown name sorted and de-duplicated.
    fn resolve(&self, names: &[&str]) -> Result<Vec<NodeId>, PlanError> {
        let (known, unknown): (Vec<_>, Vec<_>) = names
            .iter()
            .map(|&name| self.id_of(name).ok_or(name))
            .partition(Result::is_ok);
        if unknown.is_empty() {
            return Ok(known.into_iter().flatten().collect());
        }
        let unknown: BTreeSet<String> = unknown
            .into_iter()
            .filter_map(Result::err)
            .map(str::to_owned)
            .collect();
        Err(PlanError::UnknownNodes(unknown.into_iter().collect()))
    }

    fn reach_upstream(&self, starts: &[NodeId]) -> Vec<NodeId> {
        reach(starts, |id| self.node(id).inputs.iter().map(|e| e.node))
    }

    fn reach_downstream(&self, starts: &[NodeId]) -> Vec<NodeId> {
        reach(starts, |id| {
            self.dependents
                .get(id.index())
                .into_iter()
                .flatten()
                .copied()
        })
    }

    /// The id of the node called `name`, if any.
    #[must_use]
    pub fn id_of(&self, name: &str) -> Option<NodeId> {
        self.ids.get(name).copied()
    }
}

/// Inverts the input edges: for each node, who reads from it.
fn forward_adjacency<P>(nodes: &[Node<P>]) -> Vec<Vec<NodeId>> {
    let mut dependents = vec![Vec::new(); nodes.len()];
    for node in nodes {
        for edge in &node.inputs {
            if let Some(list) = dependents.get_mut(edge.node.index()) {
                list.push(node.id);
            }
        }
    }
    dependents
}

/// Iterative worklist reachability; the result is sorted and excludes `starts` themselves.
fn reach<I>(starts: &[NodeId], neighbours: impl Fn(NodeId) -> I) -> Vec<NodeId>
where
    I: Iterator<Item = NodeId>,
{
    let mut seen: BTreeSet<NodeId> = starts.iter().copied().collect();
    let mut found = BTreeSet::new();
    let mut work: Vec<NodeId> = starts.to_vec();
    while let Some(current) = work.pop() {
        for next in neighbours(current) {
            if seen.insert(next) {
                found.insert(next);
                work.push(next);
            }
        }
    }
    found.into_iter().collect()
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

fn make_node<P>(
    id: NodeId,
    name: &str,
    spec: Option<NodeSpec<P>>,
    ids: &BTreeMap<String, NodeId>,
) -> Node<P> {
    let Some(spec) = spec else {
        return Node {
            id,
            name: name.to_owned(),
            kind: NodeKind::Input,
            inputs: Vec::new(),
            tags: BTreeMap::new(),
            payload: None,
        };
    };
    Node {
        id,
        name: spec.name,
        kind: NodeKind::Function,
        inputs: resolve_edges(&spec.inputs, ids),
        tags: spec.tags,
        payload: Some(spec.payload),
    }
}

/// Resolves inputs to edges, keeping the first of any repeated dependency.
fn resolve_edges(inputs: &[crate::node::InputSpec], ids: &BTreeMap<String, NodeId>) -> Vec<Edge> {
    let mut seen = BTreeSet::new();
    inputs
        .iter()
        .filter_map(|input| {
            let node = *ids.get(&input.name)?;
            seen.insert(node).then_some(Edge {
                node,
                dependency: input.dependency,
            })
        })
        .collect()
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
    use crate::node::{Dependency, InputSpec};

    fn spec(name: &str, deps: &[&str]) -> NodeSpec<()> {
        NodeSpec {
            name: name.to_owned(),
            inputs: deps
                .iter()
                .map(|d| InputSpec {
                    name: (*d).to_owned(),
                    dependency: Dependency::Required,
                })
                .collect(),
            tags: BTreeMap::new(),
            payload: (),
        }
    }

    #[test]
    fn build_synthesises_input_nodes_for_unresolved_parameters() {
        let graph = Graph::build(vec![spec("b", &["a"])]).unwrap();
        let a = graph.node(graph.id_of("a").unwrap());
        assert_eq!(a.kind, NodeKind::Input);
        assert!(a.payload.is_none());
        let b = graph.node(graph.id_of("b").unwrap());
        assert_eq!(b.kind, NodeKind::Function);
        assert_eq!(
            b.inputs.iter().map(|e| e.node).collect::<Vec<_>>(),
            vec![a.id]
        );
    }

    #[test]
    fn build_rejects_duplicate_names() {
        let err = Graph::build(vec![spec("x", &[]), spec("x", &[])]).unwrap_err();
        assert_eq!(
            err,
            BuildError::DuplicateName {
                name: "x".to_owned()
            }
        );
    }

    #[test]
    fn build_reports_a_cycle_with_its_path() {
        let err = Graph::build(vec![spec("a", &["b"]), spec("b", &["a"])]).unwrap_err();
        let BuildError::Cycle { path } = err else {
            unreachable!("expected a cycle, got {err}");
        };
        assert!(path.contains(&"a".to_owned()) && path.contains(&"b".to_owned()));
    }

    #[test]
    fn build_treats_a_self_dependency_as_a_cycle() {
        let err = Graph::build(vec![spec("a", &["a"])]).unwrap_err();
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
            Graph::build(vec![spec("m", &["z"]), spec("c", &[]), spec("a", &["m"])]).unwrap();
        let names: Vec<&str> = graph.nodes().iter().map(|n| n.name.as_str()).collect();
        assert_eq!(names, ["a", "c", "m", "z"]);
        for (position, node) in graph.nodes().iter().enumerate() {
            assert_eq!(node.id.index(), position);
        }
    }

    #[test]
    fn build_accepts_a_deep_chain_without_recursion() {
        let specs: Vec<_> = (0..50_000)
            .map(|i| spec(&format!("n{i:06}"), &[&format!("n{:06}", i + 1)]))
            .collect();
        assert!(Graph::build(specs).is_ok());
    }

    mod queries {
        use super::*;

        fn diamond() -> Graph<()> {
            Graph::build(vec![
                spec("a", &[]),
                spec("b", &["a"]),
                spec("c", &["a"]),
                spec("d", &["b", "c"]),
            ])
            .unwrap()
        }

        fn names(graph: &Graph<()>, ids: &[NodeId]) -> Vec<String> {
            ids.iter().map(|&id| graph.node(id).name.clone()).collect()
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
                spec("b", &["a"]),
                spec("c", &["b"]),
                spec("side", &["a"]),
                spec("tail", &["c"]),
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
                .map(|i| spec(&format!("n{i:06}"), &[&format!("n{:06}", i + 1)]))
                .collect();
            let g = Graph::build(specs).unwrap();
            assert_eq!(g.upstream_of(&["n000000"]).unwrap().len(), 50_000);
            assert_eq!(g.downstream_of(&["n050000"]).unwrap().len(), 50_000);
        }
    }
}
