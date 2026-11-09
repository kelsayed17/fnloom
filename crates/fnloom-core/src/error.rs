//! Errors raised while building a graph or querying it.

use std::error::Error;
use std::fmt;

/// Why a set of node specs does not form a valid graph.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum BuildError {
    /// Two specs share one name.
    DuplicateName {
        /// The repeated name.
        name: String,
    },
    /// One spec lists an input twice, with different dependencies.
    ConflictingInput {
        /// The node whose spec lists it.
        node: String,
        /// The input it lists twice.
        input: String,
    },
    /// The dependencies form a cycle.
    Cycle {
        /// Node names along the cycle in traversal order; the first name is repeated at the end.
        path: Vec<String>,
    },
    /// A collect reads a node that is in no block.
    CollectOutsideBlock {
        /// The node whose parameter collects.
        consumer: String,
        /// The node it collects, which no expand reaches.
        source: String,
    },
    /// A collect reads an expand itself, which is not in its own block.
    CollectOfExpand {
        /// The node whose parameter collects.
        consumer: String,
        /// The expand it collects.
        expand: String,
    },
    /// A node collects a block it is itself inside, so a branch would collect its own block.
    ///
    /// Either the node also reads the block without a collect, or the block reads the
    /// collect back through nodes outside it, which would make the block wait for itself.
    CollectsOwnBlock {
        /// The node whose parameter collects.
        consumer: String,
        /// The block node it collects.
        source: String,
        /// The expand of the block it collects.
        expand: String,
        /// From the expand to the consumer without crossing a collect; when the block reads
        /// its collect back through outer nodes, from the expand round to the expand again.
        path: Vec<String>,
    },
    /// A node reads blocks of two unrelated expands, whose branches do not pair up.
    UnrelatedBlocks {
        /// The node reading both.
        consumer: String,
        /// One expand, the first by name.
        first: String,
        /// The other expand.
        second: String,
    },
}

/// `names` quoted the way Python quotes them, `'name'`, joined by `separator`.
///
/// Error messages reach Python users, so they quote names as the rest of fnloom does.
pub(crate) fn quoted(names: &[String], separator: &str) -> String {
    names
        .iter()
        .map(|name| format!("'{name}'"))
        .collect::<Vec<_>>()
        .join(separator)
}

impl fmt::Display for BuildError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::DuplicateName { name } => write!(f, "duplicate node name '{name}'"),
            Self::ConflictingInput { node, input } => write!(
                f,
                "node '{node}' reads '{input}' twice, with different dependencies; \
                 list it once"
            ),
            Self::Cycle { path } => write!(f, "dependency cycle: {}", quoted(path, " -> ")),
            Self::CollectOutsideBlock { consumer, source } => write!(
                f,
                "'{consumer}' collects '{source}', which is in no block; collect a node that \
                 reads an expand"
            ),
            Self::CollectOfExpand { consumer, expand } => write!(
                f,
                "'{consumer}' collects the expand '{expand}', which is not in its own block; \
                 collect a node of its block instead"
            ),
            Self::CollectsOwnBlock {
                consumer,
                source,
                expand,
                path,
            } => {
                let shown = quoted(path, " -> ");
                if path.last() == Some(expand) {
                    write!(
                        f,
                        "the block of '{expand}' reads its own collect '{consumer}' through \
                         nodes outside it: {shown}"
                    )
                } else if path.len() >= 2 && path[path.len() - 2] == *source {
                    write!(
                        f,
                        "'{consumer}' reads '{source}' both through Collect and directly, so it \
                         would be inside the block of '{expand}' it collects: {shown}"
                    )
                } else {
                    write!(
                        f,
                        "'{consumer}' collects '{source}' but also reads the block of \
                         '{expand}' directly, so it would be inside the block it collects: \
                         {shown}"
                    )
                }
            }
            Self::UnrelatedBlocks {
                consumer,
                first,
                second,
            } => write!(
                f,
                "'{consumer}' reads blocks of '{first}' and '{second}', which are unrelated; \
                 collect both in one node to pair them"
            ),
        }
    }
}

impl Error for BuildError {}

/// Why a query or plan request cannot be answered.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum PlanError {
    /// Names that do not exist in the graph.
    UnknownNodes(Vec<String>),
    /// Requested outputs that do not exist in the graph.
    UnknownOutputs(Vec<String>),
    /// Supplied inputs that are not `Input` nodes of the graph.
    UnknownInputs(Vec<String>),
    /// Overrides that do not exist in the graph.
    UnknownOverrides(Vec<String>),
    /// Required inputs that are neither supplied nor overridden, each with what needs it.
    MissingInputs(Vec<MissingInput>),
}

/// A required input that is neither supplied nor overridden, and what needs it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct MissingInput {
    /// The input's name.
    pub name: String,
    /// Whether the input was itself requested as an output.
    pub requested: bool,
    /// The planned nodes reading it by a required edge, sorted.
    pub consumers: Vec<String>,
}

impl fmt::Display for MissingInput {
    /// `'x' (needed by 'a', 'b')`, `'x' (requested as an output)`, or both reasons.
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let mut reasons = Vec::new();
        if self.requested {
            reasons.push("requested as an output".to_owned());
        }
        if !self.consumers.is_empty() {
            reasons.push(format!("needed by {}", quoted(&self.consumers, ", ")));
        }
        write!(f, "'{}' ({})", self.name, reasons.join(", "))
    }
}

impl fmt::Display for PlanError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::UnknownNodes(names) => write!(f, "unknown nodes: {}", quoted(names, ", ")),
            Self::UnknownOutputs(names) => write!(f, "unknown outputs: {}", quoted(names, ", ")),
            Self::UnknownInputs(names) => write!(f, "unknown inputs: {}", quoted(names, ", ")),
            Self::UnknownOverrides(names) => {
                write!(f, "unknown overrides: {}", quoted(names, ", "))
            }
            Self::MissingInputs(missing) => {
                let listed: Vec<String> = missing.iter().map(ToString::to_string).collect();
                write!(f, "missing inputs: {}", listed.join("; "))
            }
        }
    }
}

impl Error for PlanError {}
