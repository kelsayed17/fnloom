//! Block discovery: which expand's block each node is in, and the build errors of §4.2.
//!
//! A block belongs to one expand and holds every node that reads it along a path that does
//! not pass through a collect of that block. Blocks nest, so each node has a block path: the
//! expands whose blocks hold it, outermost first. The paths form a tree, so a node's path is
//! kept as its innermost block, and each expand's `block` is its parent.

use std::collections::VecDeque;

use crate::error::BuildError;
use crate::node::{Node, NodeId};

/// Where an edge places its consumer: inside the block of `innermost` (`None`: in none), at
/// this many levels of nesting.
#[derive(Clone, Copy, PartialEq, Eq)]
struct Place {
    innermost: Option<NodeId>,
    depth: usize,
}

/// The innermost block of each node, by id: the expand whose block holds it.
///
/// `order` is a topological order of `nodes`, each node after every node it reads. Each
/// node's path is the longest of its in-edges' contributions, which must be prefixes of one
/// another; then every block, at every level of nesting, must not wait for itself once it
/// is contracted into its expand. Iterative throughout.
///
/// # Errors
///
/// The first refusal in `order`: `CollectOfExpand`, `CollectOutsideBlock`,
/// `UnrelatedBlocks` or `CollectsOwnBlock`; then a `CollectsOwnBlock` for a block that reads
/// its own collect through nodes outside it.
pub(crate) fn discover<P>(
    nodes: &[Node<P>],
    order: &[NodeId],
) -> Result<Vec<Option<NodeId>>, BuildError> {
    let mut tree = Tree {
        block: vec![None; nodes.len()],
        depth: vec![0; nodes.len()],
    };
    for &id in order {
        let node = &nodes[id.index()];
        let mut place = Place {
            innermost: None,
            depth: 0,
        };
        for edge in &node.inputs {
            let source = edge.node;
            let contribution = if edge.collect.is_none() {
                if nodes[source.index()].is_expand() {
                    Place {
                        innermost: Some(source),
                        depth: tree.depth[source.index()] + 1,
                    }
                } else {
                    tree.place(source)
                }
            } else if nodes[source.index()].is_expand() {
                return Err(BuildError::CollectOfExpand {
                    consumer: node.name.clone(),
                    expand: nodes[source.index()].name.clone(),
                });
            } else if let Some(collected) = tree.block[source.index()] {
                // The path of the source without its last element.
                Place {
                    innermost: tree.block[collected.index()],
                    depth: tree.depth[source.index()] - 1,
                }
            } else {
                return Err(BuildError::CollectOutsideBlock {
                    consumer: node.name.clone(),
                    source: nodes[source.index()].name.clone(),
                });
            };
            let (shallow, deep) = if contribution.depth > place.depth {
                (place, contribution)
            } else {
                (contribution, place)
            };
            if tree.ancestor(deep, shallow.depth) != shallow.innermost {
                // The two blocks where the paths diverge, at the shallower one's depth.
                let mut pair = [shallow.innermost, tree.ancestor(deep, shallow.depth)].map(|id| {
                    id.map(|id| nodes[id.index()].name.clone())
                        .unwrap_or_default()
                });
                pair.sort();
                let [first, second] = pair;
                return Err(BuildError::UnrelatedBlocks {
                    consumer: node.name.clone(),
                    first,
                    second,
                });
            }
            place = deep;
        }
        tree.block[id.index()] = place.innermost;
        tree.depth[id.index()] = place.depth;
        for edge in node.inputs.iter().filter(|edge| edge.collect.is_some()) {
            let collected = tree.block[edge.node.index()];
            let level = tree.depth[edge.node.index()];
            if place.depth >= level && tree.ancestor(place, level) == collected {
                return Err(collects_own_block(nodes, &tree, id, edge.node));
            }
        }
    }
    if let Some(cycle) = contracted_cycle(nodes, &tree) {
        return Err(cycle);
    }
    Ok(tree.block)
}

/// The block tree: each node's innermost block, and the length of its block path.
struct Tree {
    block: Vec<Option<NodeId>>,
    depth: Vec<usize>,
}

impl Tree {
    fn place(&self, id: NodeId) -> Place {
        Place {
            innermost: self.block[id.index()],
            depth: self.depth[id.index()],
        }
    }

    /// The innermost block of `place`'s path cut to `depth` levels, which must not exceed
    /// `place.depth`.
    fn ancestor(&self, place: Place, depth: usize) -> Option<NodeId> {
        let mut innermost = place.innermost;
        for _ in depth..place.depth {
            innermost = innermost.and_then(|expand| self.block[expand.index()]);
        }
        innermost
    }

    /// Whether `id` is inside the block of `expand`, at any depth.
    fn inside(&self, id: NodeId, expand: NodeId) -> bool {
        let level = self.depth[expand.index()] + 1;
        self.depth[id.index()] >= level && self.ancestor(self.place(id), level) == Some(expand)
    }

    /// The unit that stands for `id` in the scope `scope` (the path of that many levels that
    /// `id`'s path starts with): `id` itself when it is in that scope, else the expand of the
    /// nested block that holds it.
    fn unit(&self, id: NodeId, scope: usize) -> NodeId {
        if self.depth[id.index()] == scope {
            return id;
        }
        // The block one level below the scope; its expand sits in the scope.
        self.ancestor(self.place(id), scope + 1).unwrap_or(id)
    }

    /// The depth of the deepest scope whose path both `a` and `b` start with.
    fn common_scope(&self, a: NodeId, b: NodeId) -> usize {
        let (mut a, mut b) = (self.place(a), self.place(b));
        while a.depth > b.depth {
            a = self.up(a);
        }
        while b.depth > a.depth {
            b = self.up(b);
        }
        while a.innermost != b.innermost {
            a = self.up(a);
            b = self.up(b);
        }
        a.depth
    }

    fn up(&self, place: Place) -> Place {
        Place {
            innermost: place
                .innermost
                .and_then(|expand| self.block[expand.index()]),
            depth: place.depth.saturating_sub(1),
        }
    }
}

/// The refusal of `consumer`, which collects `source` from a block it is itself inside,
/// with a shortest path from that block's expand to the consumer inside the block.
#[cold]
fn collects_own_block<P>(
    nodes: &[Node<P>],
    tree: &Tree,
    consumer: NodeId,
    source: NodeId,
) -> BuildError {
    let expand = tree.block[source.index()].unwrap_or(source);
    // Breadth first, upstream from the consumer, through nodes of the block only.
    let mut reached_from: Vec<Option<NodeId>> = vec![None; nodes.len()];
    let mut queue = VecDeque::from([consumer]);
    let mut found = false;
    while let Some(current) = queue.pop_front() {
        for edge in &nodes[current.index()].inputs {
            let next = edge.node;
            if next == consumer || reached_from[next.index()].is_some() {
                continue;
            }
            if next == expand || tree.inside(next, expand) {
                reached_from[next.index()] = Some(current);
                if next == expand {
                    found = true;
                    break;
                }
                queue.push_back(next);
            }
        }
        if found {
            break;
        }
    }
    let mut path = vec![nodes[expand.index()].name.clone()];
    let mut at = expand;
    while let Some(next) = reached_from[at.index()] {
        path.push(nodes[next.index()].name.clone());
        at = next;
    }
    BuildError::CollectsOwnBlock {
        consumer: nodes[consumer.index()].name.clone(),
        source: nodes[source.index()].name.clone(),
        expand: nodes[expand.index()].name.clone(),
        path,
    }
}

/// An edge of the contracted graph: the consumer's unit reads `unit`, because `via` reads
/// `source`.
#[derive(Clone, Copy)]
struct Contracted {
    unit: NodeId,
    via: NodeId,
    source: NodeId,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Colour {
    White,
    Grey,
    Black,
}

/// A `CollectsOwnBlock` for a block that would wait for itself once contracted.
///
/// Each edge is rewritten to join the units of the deepest scope holding both its ends: a
/// node in that scope stands for itself, a node of a nested block for that block's expand.
/// An edge inside one nested block is left to that block's own scope. A cycle among the
/// rewritten edges passes through an expand, since the graph itself is acyclic.
fn contracted_cycle<P>(nodes: &[Node<P>], tree: &Tree) -> Option<BuildError> {
    let mut reads: Vec<Vec<Contracted>> = vec![Vec::new(); nodes.len()];
    for node in nodes {
        for edge in &node.inputs {
            let scope = tree.common_scope(node.id, edge.node);
            let (from, to) = (tree.unit(node.id, scope), tree.unit(edge.node, scope));
            if from != to {
                reads[from.index()].push(Contracted {
                    unit: to,
                    via: node.id,
                    source: edge.node,
                });
            }
        }
    }
    let mut colours = vec![Colour::White; nodes.len()];
    for start in 0..nodes.len() {
        if colours[start] != Colour::White {
            continue;
        }
        colours[start] = Colour::Grey;
        // Each frame is a unit, the index of its next unvisited edge, and the edge it was
        // entered by.
        let mut stack: Vec<(NodeId, usize, Option<Contracted>)> =
            vec![(NodeId(as_u32(start)), 0, None)];
        while let Some(frame) = stack.last_mut() {
            let (current, next) = (frame.0, frame.1);
            let Some(&edge) = reads[current.index()].get(next) else {
                colours[current.index()] = Colour::Black;
                stack.pop();
                continue;
            };
            frame.1 += 1;
            match colours[edge.unit.index()] {
                Colour::White => {
                    colours[edge.unit.index()] = Colour::Grey;
                    stack.push((edge.unit, 0, Some(edge)));
                }
                Colour::Grey => return Some(cycle_refusal(nodes, &stack, edge)),
                Colour::Black => {}
            }
        }
    }
    None
}

fn as_u32(index: usize) -> u32 {
    u32::try_from(index).unwrap_or(u32::MAX)
}

/// The refusal for the cycle closed by `closing`, back to a unit on `stack`.
///
/// The cycle is turned to start at its first expand by id, which the edge before it enters
/// through a collect: that collect is the consumer.
#[cold]
fn cycle_refusal<P>(
    nodes: &[Node<P>],
    stack: &[(NodeId, usize, Option<Contracted>)],
    closing: Contracted,
) -> BuildError {
    let from = stack
        .iter()
        .position(|&(id, _, _)| id == closing.unit)
        .unwrap_or(0);
    // The cycle's edges in reads order: each frame after the first was entered by one.
    let mut edges: Vec<Contracted> = stack[from + 1..]
        .iter()
        .filter_map(|&(_, _, entered)| entered)
        .collect();
    edges.push(closing);
    let start = edges
        .iter()
        .enumerate()
        .filter(|(_, edge)| nodes[edge.unit.index()].is_expand())
        .min_by_key(|(_, edge)| edge.unit)
        .map_or(0, |(at, _)| (at + 1) % edges.len());
    edges.rotate_left(start);
    let name = |id: NodeId| nodes[id.index()].name.clone();
    let entering = edges[edges.len() - 1];
    let expand = entering.unit;
    let mut path = vec![name(expand)];
    let mut unit = expand;
    for edge in &edges {
        if edge.via != unit {
            path.push(name(edge.via));
        }
        path.push(name(edge.unit));
        unit = edge.unit;
    }
    BuildError::CollectsOwnBlock {
        consumer: name(entering.via),
        source: name(entering.source),
        expand: name(expand),
        path,
    }
}

#[cfg(test)]
#[expect(clippy::unwrap_used, reason = "tests assert by unwrapping")]
mod tests {
    use crate::error::BuildError;
    use crate::graph::Graph;
    use crate::node::{Collect, Dependency, NodeSpec};
    use crate::testing::{collect, expanding, names, plain, reading, strings};

    fn build(specs: Vec<NodeSpec<()>>) -> Graph<()> {
        Graph::build(specs).unwrap()
    }

    fn refusal(specs: Vec<NodeSpec<()>>) -> BuildError {
        Graph::build(specs).unwrap_err()
    }

    /// The block path of `name`: its enclosing expands, outermost first.
    fn path(graph: &Graph<()>, name: &str) -> Vec<String> {
        names(graph, graph.block_path(graph.id_of(name).unwrap()))
    }

    /// The innermost block of `name`, by its expand's name.
    fn block(graph: &Graph<()>, name: &str) -> Option<String> {
        let node = graph.node(graph.id_of(name).unwrap());
        node.block.map(|id| graph.node(id).name.clone())
    }

    /// `tickers` expands `universe`; `prices` reads each ticker and the shared `day`;
    /// `scaled` reads `prices`; `total` collects `scaled`.
    fn tickers() -> Vec<NodeSpec<()>> {
        vec![
            expanding("tickers", &[plain("universe")], ()),
            reading("day", &[], ()),
            reading("prices", &[plain("tickers"), plain("day")], ()),
            reading("scaled", &[plain("prices")], ()),
            reading("total", &[collect("scaled")], ()),
        ]
    }

    #[test]
    fn a_block_holds_every_node_reachable_without_a_collect() {
        let g = build(tickers());
        assert_eq!(block(&g, "prices").as_deref(), Some("tickers"));
        assert_eq!(block(&g, "scaled").as_deref(), Some("tickers"));
        assert_eq!(path(&g, "scaled"), ["tickers"]);
        for outside in ["tickers", "universe", "day", "total"] {
            assert_eq!(block(&g, outside), None, "{outside}");
            assert_eq!(path(&g, outside), [] as [String; 0], "{outside}");
        }
        assert!(g.node(g.id_of("tickers").unwrap()).is_expand());
    }

    #[test]
    fn shared_outer_nodes_stay_outside() {
        let mut specs = tickers();
        specs.push(reading("rate", &[plain("day")], ()));
        specs.push(reading("converted", &[plain("scaled"), plain("rate")], ()));
        specs.push(reading("sum", &[collect("converted")], ()));
        let g = build(specs);
        assert_eq!(block(&g, "rate"), None);
        assert_eq!(block(&g, "day"), None);
        assert_eq!(block(&g, "converted").as_deref(), Some("tickers"));
    }

    #[test]
    fn two_collects_of_one_block_build() {
        // Q123.
        let mut specs = tickers();
        specs.push(reading("count", &[collect("scaled")], ()));
        specs.push(reading("first", &[collect("prices")], ()));
        let g = build(specs);
        for collector in ["total", "count", "first"] {
            assert_eq!(block(&g, collector), None, "{collector}");
        }
    }

    #[test]
    fn reading_a_block_node_and_its_collect_is_refused_with_the_path() {
        // Q124: reading `prices` puts `total` in the block it collects.
        let err = refusal(vec![
            expanding("tickers", &[], ()),
            reading("prices", &[plain("tickers")], ()),
            reading("total", &[collect("prices"), plain("prices")], ()),
        ]);
        assert_eq!(
            err,
            BuildError::CollectsOwnBlock {
                consumer: "total".to_owned(),
                source: "prices".to_owned(),
                expand: "tickers".to_owned(),
                path: strings(&["tickers", "prices", "total"]),
            }
        );
        assert_eq!(
            err.to_string(),
            "'total' reads 'prices' both through Collect and directly, so it would be inside \
             the block of 'tickers' it collects: 'tickers' -> 'prices' -> 'total'"
        );
        // Reading another node of the block is the same refusal, through that node.
        let mut specs = tickers();
        specs.push(reading("both", &[collect("scaled"), plain("prices")], ()));
        let err = refusal(specs);
        assert_eq!(
            err.to_string(),
            "'both' collects 'scaled' but also reads the block of 'tickers' directly, so it \
             would be inside the block it collects: 'tickers' -> 'prices' -> 'both'"
        );
    }

    #[test]
    fn collecting_one_node_two_ways_is_a_conflicting_input() {
        let mut specs = tickers();
        specs.push(reading(
            "both",
            &[
                collect("scaled"),
                ("scaled", Dependency::Required, Some(Collect::All)),
            ],
            (),
        ));
        assert_eq!(
            refusal(specs),
            BuildError::ConflictingInput {
                node: "both".to_owned(),
                input: "scaled".to_owned(),
            }
        );
    }

    #[test]
    fn collect_outside_a_block_is_refused() {
        let mut specs = tickers();
        specs.push(reading("days", &[collect("day")], ()));
        let err = refusal(specs);
        assert_eq!(
            err,
            BuildError::CollectOutsideBlock {
                consumer: "days".to_owned(),
                source: "day".to_owned(),
            }
        );
        assert_eq!(
            err.to_string(),
            "'days' collects 'day', which is in no block; collect a node that reads an expand"
        );
        // So is a collect in a graph with no expand at all.
        let err = refusal(vec![reading("days", &[collect("day")], ())]);
        assert!(matches!(err, BuildError::CollectOutsideBlock { .. }));
    }

    #[test]
    fn collect_of_an_expand_is_refused() {
        let mut specs = tickers();
        specs.push(reading("all", &[collect("tickers")], ()));
        let err = refusal(specs);
        assert_eq!(
            err,
            BuildError::CollectOfExpand {
                consumer: "all".to_owned(),
                expand: "tickers".to_owned(),
            }
        );
        assert_eq!(
            err.to_string(),
            "'all' collects the expand 'tickers', which is not in its own block; collect a \
             node of its block instead"
        );
    }

    #[test]
    fn two_unrelated_blocks_are_refused() {
        let err = refusal(vec![
            expanding("right", &[], ()),
            expanding("left", &[], ()),
            reading("r", &[plain("right")], ()),
            reading("l", &[plain("left")], ()),
            reading("pair", &[plain("r"), plain("l")], ()),
        ]);
        assert_eq!(
            err,
            BuildError::UnrelatedBlocks {
                consumer: "pair".to_owned(),
                first: "left".to_owned(),
                second: "right".to_owned(),
            }
        );
        assert_eq!(
            err.to_string(),
            "'pair' reads blocks of 'left' and 'right', which are unrelated; collect both in \
             one node to pair them"
        );
    }

    #[test]
    fn unrelated_blocks_name_where_the_paths_diverge() {
        // `b` is nested in `a`; `pair` reads the block of `b` and that of `c`.
        let err = refusal(vec![
            expanding("a", &[], ()),
            expanding("b", &[plain("a")], ()),
            expanding("c", &[], ()),
            reading("in_b", &[plain("b")], ()),
            reading("in_c", &[plain("c")], ()),
            reading("pair", &[plain("in_b"), plain("in_c")], ()),
        ]);
        assert_eq!(
            err,
            BuildError::UnrelatedBlocks {
                consumer: "pair".to_owned(),
                first: "a".to_owned(),
                second: "c".to_owned(),
            }
        );
    }

    #[test]
    fn plain_reads_of_one_node_that_disagree_conflict_beside_a_collect() {
        let mut specs = tickers();
        specs.push(reading(
            "odd",
            &[
                collect("scaled"),
                plain("scaled"),
                ("scaled", Dependency::Optional, None),
            ],
            (),
        ));
        assert_eq!(
            refusal(specs),
            BuildError::ConflictingInput {
                node: "odd".to_owned(),
                input: "scaled".to_owned(),
            }
        );
    }

    #[test]
    fn a_node_may_collect_one_block_and_expand_another() {
        // Q136: `mid` collects the block of `a` and expands a block of its own.
        let g = build(vec![
            expanding("a", &[], ()),
            reading("b", &[plain("a")], ()),
            expanding("mid", &[collect("b")], ()),
            reading("c", &[plain("mid")], ()),
            reading("out", &[collect("c")], ()),
        ]);
        assert_eq!(block(&g, "mid"), None);
        assert_eq!(path(&g, "mid"), [] as [String; 0]);
        assert_eq!(path(&g, "c"), ["mid"]);
        assert_eq!(path(&g, "b"), ["a"]);
        assert_eq!(block(&g, "out"), None);
    }

    #[test]
    fn nested_blocks_record_their_path() {
        let g = build(vec![
            expanding("outer", &[], ()),
            expanding("inner", &[plain("outer")], ()),
            reading("leaf", &[plain("inner"), plain("outer")], ()),
            reading("inner_sum", &[collect("leaf")], ()),
            reading("outer_sum", &[collect("inner_sum")], ()),
        ]);
        assert_eq!(path(&g, "leaf"), ["outer", "inner"]);
        assert_eq!(block(&g, "leaf").as_deref(), Some("inner"));
        assert_eq!(path(&g, "inner"), ["outer"]);
        assert_eq!(path(&g, "inner_sum"), ["outer"]);
        assert_eq!(path(&g, "outer_sum"), [] as [String; 0]);
    }

    /// `picked` expands; `price` is in its block and `summary` collects it; `y` reads
    /// `summary`, and `details`, in the block, reads `y`: the block would wait for itself.
    fn reads_its_own_collect() -> Vec<NodeSpec<()>> {
        vec![
            expanding("picked", &[], ()),
            reading("price", &[plain("picked")], ()),
            reading("summary", &[collect("price")], ()),
            reading("y", &[plain("summary")], ()),
            reading("details", &[plain("picked"), plain("y")], ()),
            reading("report", &[collect("details")], ()),
        ]
    }

    #[test]
    fn a_block_reading_its_own_collect_through_an_outer_node_is_refused() {
        let err = refusal(reads_its_own_collect());
        assert_eq!(
            err,
            BuildError::CollectsOwnBlock {
                consumer: "summary".to_owned(),
                source: "price".to_owned(),
                expand: "picked".to_owned(),
                path: strings(&["picked", "details", "y", "summary", "picked"]),
            }
        );
        assert_eq!(
            err.to_string(),
            "the block of 'picked' reads its own collect 'summary' through nodes outside it: \
             'picked' -> 'details' -> 'y' -> 'summary' -> 'picked'"
        );
    }

    #[test]
    fn a_nested_block_reading_its_own_collect_through_an_outer_node_is_refused() {
        // The same shape, inside the block of `outer`: `y` is an outer node of `picked`'s
        // block but a node of `outer`'s.
        let mut specs: Vec<NodeSpec<()>> = reads_its_own_collect()
            .into_iter()
            .map(|mut spec| {
                if spec.name == "picked" {
                    spec = expanding("picked", &[plain("outer")], ());
                }
                spec
            })
            .collect();
        specs.push(expanding("outer", &[], ()));
        specs.push(reading("everything", &[collect("report")], ()));
        let err = refusal(specs);
        let BuildError::CollectsOwnBlock { path, .. } = err else {
            unreachable!("expected CollectsOwnBlock, got {err}");
        };
        assert_eq!(path, ["picked", "details", "y", "summary", "picked"]);
    }

    #[test]
    fn may_be_absent_crosses_a_collect_only_from_an_absent_expand() {
        let declining_expand = NodeSpec {
            declines: true,
            ..expanding("items", &[], ())
        };
        let specs = |expand: NodeSpec<()>, price_declines: bool| {
            vec![
                expand,
                NodeSpec {
                    declines: price_declines,
                    ..reading("price", &[plain("items")], ())
                },
                reading("total", &[collect("price")], ()),
                reading(
                    "or_default",
                    &[("price", Dependency::Optional, Some(Collect::Present))],
                    (),
                ),
                reading(
                    "every",
                    &[("price", Dependency::Required, Some(Collect::All))],
                    (),
                ),
            ]
        };
        let absent = |g: &Graph<()>, name: &str| g.node(g.id_of(name).unwrap()).may_be_absent;
        // An expand that may be absent: a required collect of its block may be absent too.
        let g = build(specs(declining_expand, false));
        assert!(absent(&g, "items") && absent(&g, "total") && absent(&g, "every"));
        assert!(!absent(&g, "or_default"));
        // A block node that may be absent: its collects drop it, or keep its place.
        let g = build(specs(expanding("items", &[], ()), true));
        assert!(absent(&g, "price"));
        for collector in ["total", "or_default", "every"] {
            assert!(!absent(&g, collector), "{collector}");
        }
    }

    #[test]
    fn an_expand_that_may_be_absent_does_not_make_its_block_nodes_absent() {
        let g = build(vec![
            NodeSpec {
                declines: true,
                ..expanding("items", &[], ())
            },
            reading("price", &[plain("items")], ()),
            reading("total", &[collect("price")], ()),
        ]);
        assert!(!g.node(g.id_of("price").unwrap()).may_be_absent);
    }

    #[test]
    fn discovery_has_no_recursion() {
        // A 50,000-node chain inside one block.
        let mut specs = vec![
            expanding("e", &[], ()),
            reading("n00000", &[plain("e")], ()),
        ];
        specs.extend(
            (1..50_000)
                .map(|i| reading(&format!("n{i:05}"), &[plain(&format!("n{:05}", i - 1))], ())),
        );
        specs.push(reading("total", &[collect("n49999")], ()));
        let g = build(specs);
        assert_eq!(path(&g, "n49999"), ["e"]);
        assert_eq!(block(&g, "total"), None);

        // 1,000 nested blocks: `e{i}` expands inside `e{i-1}`, and `c{i}` collects `c{i+1}`.
        let depth = 1_000;
        let mut specs = vec![expanding("e0000", &[], ())];
        specs.extend(
            (1..depth)
                .map(|i| expanding(&format!("e{i:04}"), &[plain(&format!("e{:04}", i - 1))], ())),
        );
        specs.push(reading("leaf", &[plain(&format!("e{:04}", depth - 1))], ()));
        specs.push(reading(
            &format!("c{:04}", depth - 1),
            &[collect("leaf")],
            (),
        ));
        specs.extend((0..depth - 1).map(|i| {
            reading(
                &format!("c{i:04}"),
                &[collect(&format!("c{:04}", i + 1))],
                (),
            )
        }));
        let g = build(specs);
        assert_eq!(g.block_path(g.id_of("leaf").unwrap()).len(), depth);
        assert_eq!(path(&g, "c0001"), ["e0000"]);
        assert_eq!(block(&g, "c0000"), None);
    }
}
