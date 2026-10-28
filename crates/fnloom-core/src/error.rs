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
    /// The dependencies form a cycle.
    Cycle {
        /// Node names along the cycle in traversal order; the first name is repeated at the end.
        path: Vec<String>,
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
            Self::Cycle { path } => write!(f, "dependency cycle: {}", quoted(path, " -> ")),
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
