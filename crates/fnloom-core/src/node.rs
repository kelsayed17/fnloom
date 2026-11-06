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

/// A tag's value: one string, or several in the order the author wrote them.
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum TagValue {
    /// A single string.
    One(String),
    /// Several strings, in written order: never empty, and no string twice.
    /// Python's `@tag` enforces both before a value reaches here.
    Many(Vec<String>),
}

/// How a consumer reads one of its inputs: what it needs, and what it does when the
/// input's value is absent.
///
/// An absent value comes from a node that declines (`NodeSpec::declines`). Each consumer
/// decides by its own dependency: `Admits` receives the absent value, `Optional` omits the
/// argument, and `Required` skips the consumer, which is then absent too.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Dependency {
    /// The consumer cannot run without this input; when it is absent, the consumer is skipped.
    Required,
    /// The consumer runs with its default when this input is not supplied or absent.
    Optional,
    /// The consumer receives an absent value as its argument. `optional` says whether it has
    /// a default, which is what counts when the edge reads an input that is not supplied:
    /// an input is never absent.
    Admits {
        /// Whether the consumer has a default for an input that is not supplied.
        optional: bool,
    },
}

impl Dependency {
    /// Whether the consumer has a default for an input that is not supplied: `Optional`, or
    /// `Admits { optional: true }`.
    #[must_use]
    pub const fn has_default(self) -> bool {
        matches!(self, Self::Optional | Self::Admits { optional: true })
    }
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
    pub tags: BTreeMap<String, TagValue>,
    /// Opaque executable payload owned by the embedder.
    pub payload: P,
    /// Whether the node may decline: its payload may return a value the embedder's
    /// `Evaluate::is_absent` recognises as absent.
    pub declines: bool,
}

/// How a node gets its value.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum NodeKind<P> {
    /// Computed by this payload from the node's inputs.
    Function(P),
    /// Supplied by the caller at run time.
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
    /// A function with its payload, or a caller-supplied input.
    pub kind: NodeKind<P>,
    /// Resolved inputs, in declaration order.
    pub inputs: Vec<Edge>,
    /// Free-form key/value tags.
    pub tags: BTreeMap<String, TagValue>,
    /// Whether the node may decline (`NodeSpec::declines`); never for an input.
    pub declines: bool,
    /// Whether the node's value may be absent: it declines, or it is a function with a
    /// `Required` edge from a node whose value may be absent. Known at build.
    pub may_be_absent: bool,
}

impl<P> Node<P> {
    /// Whether the caller supplies this node's value.
    #[must_use]
    pub const fn is_input(&self) -> bool {
        matches!(self.kind, NodeKind::Input)
    }

    /// The payload of a function node; `None` for an input.
    #[must_use]
    pub const fn payload(&self) -> Option<&P> {
        match &self.kind {
            NodeKind::Function(payload) => Some(payload),
            NodeKind::Input => None,
        }
    }
}
