//! The Python `Graph`: a built fnloom-core graph whose payloads are Python callables.

use std::collections::{BTreeMap, BTreeSet};

use fnloom_core::graph::Graph as CoreGraph;
use fnloom_core::node::{Dependency, InputSpec, NodeId, NodeSpec};
use fnloom_core::plan::Request;
use pyo3::prelude::*;
use pyo3::types::PyString;
use pyo3::{PyTraverseError, PyVisit};

use crate::errors::value_error;
use crate::plan::Plan;

/// The core graph as the binding instantiates it: every payload is a Python callable.
pub type PyCoreGraph = CoreGraph<Py<PyAny>>;

/// One node as Python declares it: `(name, [(param, required)], tags, fn)`.
type NodeTuple = (
    String,
    Vec<(String, bool)>,
    BTreeMap<String, String>,
    Py<PyAny>,
);

/// An immutable dependency graph of Python functions, wired by parameter name.
///
/// It is the only owner of the node functions: a `Plan` reaches them through its
/// reference to this object, so the cyclic GC counts each function reference once.
#[pyclass(frozen, module = "fnloom._core")]
pub struct Graph {
    graph: PyCoreGraph,
    /// Each node's name as an interned string, by node id: the keyword its consumers
    /// receive it under, interned once here instead of on every call.
    keywords: Vec<Py<PyString>>,
}

#[pymethods]
impl Graph {
    /// Builds a graph from `(name, [(param, required)], tags, fn)` tuples.
    ///
    /// Raises `ValueError` naming the culprits for a duplicate name or a cycle.
    #[new]
    fn new(py: Python<'_>, nodes: Vec<NodeTuple>) -> PyResult<Self> {
        let specs = nodes.into_iter().map(node_spec).collect();
        let graph = CoreGraph::build(specs).map_err(value_error)?;
        let keywords = graph
            .nodes()
            .iter()
            .map(|node| PyString::intern(py, &node.name).unbind())
            .collect();
        Ok(Self { graph, keywords })
    }

    /// Lets the cyclic GC see the node functions, which may refer back to this graph
    /// through their module's globals.
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        for node in self.graph.nodes() {
            visit.call(&node.payload)?;
        }
        Ok(())
    }

    /// Every node name, declared and synthesized inputs alike, sorted.
    fn node_names(&self) -> Vec<String> {
        self.graph
            .nodes()
            .iter()
            .map(|node| node.name.clone())
            .collect()
    }

    /// Nodes the named nodes depend on, transitively, excluding them; sorted.
    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 extracts a Python list into an owned Vec"
    )]
    fn upstream_of(&self, names: Vec<String>) -> PyResult<Vec<String>> {
        let ids = self
            .graph
            .upstream_of(&as_strs(&names))
            .map_err(value_error)?;
        Ok(self.names_of(&ids))
    }

    /// Nodes that depend on the named nodes, transitively, excluding them; sorted.
    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 extracts a Python list into an owned Vec"
    )]
    fn downstream_of(&self, names: Vec<String>) -> PyResult<Vec<String>> {
        let ids = self
            .graph
            .downstream_of(&as_strs(&names))
            .map_err(value_error)?;
        Ok(self.names_of(&ids))
    }

    /// Nodes on some path from `a` to `b`, endpoints excluded; sorted.
    fn between(&self, a: &str, b: &str) -> PyResult<Vec<String>> {
        let ids = self.graph.between(a, b).map_err(value_error)?;
        Ok(self.names_of(&ids))
    }

    /// Validates a request and returns the plan that computes `outputs`.
    ///
    /// Raises `ValueError` naming unknown outputs, inputs or overrides, or missing inputs.
    fn prepare(
        slf: &Bound<'_, Self>,
        outputs: Vec<String>,
        inputs: Vec<String>,
        overrides: Vec<String>,
    ) -> PyResult<Plan> {
        let request = Request {
            outputs,
            inputs: inputs.into_iter().collect::<BTreeSet<_>>(),
            overrides: overrides.into_iter().collect::<BTreeSet<_>>(),
        };
        let plan = slf.get().graph.prepare(&request).map_err(value_error)?;
        Ok(Plan::new(slf.clone().unbind(), plan))
    }
}

impl Graph {
    /// The core graph.
    pub const fn core(&self) -> &PyCoreGraph {
        &self.graph
    }

    /// Each node's interned name, by node id.
    pub fn keywords(&self) -> &[Py<PyString>] {
        &self.keywords
    }

    fn names_of(&self, ids: &[NodeId]) -> Vec<String> {
        ids.iter()
            .map(|&id| self.graph.node(id).name.clone())
            .collect()
    }
}

fn node_spec((name, params, tags, function): NodeTuple) -> NodeSpec<Py<PyAny>> {
    let inputs = params
        .into_iter()
        .map(|(name, required)| InputSpec {
            name,
            dependency: if required {
                Dependency::Required
            } else {
                Dependency::Optional
            },
        })
        .collect();
    NodeSpec {
        name,
        inputs,
        tags,
        payload: function,
    }
}

fn as_strs(names: &[String]) -> Vec<&str> {
    names.iter().map(String::as_str).collect()
}
