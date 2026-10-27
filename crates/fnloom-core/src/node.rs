//! The node IR: what a graph is made of.

use std::collections::BTreeMap;

/// Dense identifier of a node; ids follow the sorted order of node names.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct NodeId(pub u32);

impl NodeId {
    /// Position of this node in `Graph::nodes`.
    #[must_use]
    pub const fn index(self) -> usize {
        self.0 as usize
    }
}

/// Whether a dependency must be satisfied for its consumer to run.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Dependency {
    /// The consumer cannot run without this input.
    Required,
    /// The consumer runs with its default when this input is absent.
    Optional,
}

/// A named input (parameter) of a node, as declared by its author.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct InputSpec {
    /// Name of the node this parameter reads from.
    pub name: String,
    /// Whether the parameter is required.
    pub dependency: Dependency,
}

/// A node as declared, before ids are assigned and edges resolved.
#[derive(Clone, Debug)]
pub struct NodeSpec<P> {
    /// Unique node name.
    pub name: String,
    /// Parameters, in declaration order.
    pub inputs: Vec<InputSpec>,
    /// Free-form key/value tags.
    pub tags: BTreeMap<String, String>,
    /// Opaque executable payload owned by the embedder.
    pub payload: P,
}

/// How a node gets its value.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum NodeKind {
    /// Computed by its payload from its inputs.
    Function,
    /// Supplied by the caller at run time; has no payload.
    Input,
}

/// A resolved dependency edge from a node to one of its inputs.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Edge {
    /// The node this edge reads from.
    pub node: NodeId,
    /// Whether the edge is required.
    pub dependency: Dependency,
}

/// A node of a built graph.
#[derive(Clone, Debug)]
pub struct Node<P> {
    /// Identifier, equal to the node's position in the graph.
    pub id: NodeId,
    /// Unique node name.
    pub name: String,
    /// Function or caller-supplied input.
    pub kind: NodeKind,
    /// Resolved inputs, in declaration order.
    pub inputs: Vec<Edge>,
    /// Free-form key/value tags.
    pub tags: BTreeMap<String, String>,
    /// The executable payload; `None` for `NodeKind::Input`.
    pub payload: Option<P>,
}
