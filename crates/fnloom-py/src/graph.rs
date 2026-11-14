//! The Python `Graph`: a built fnloom-core graph whose payloads are Python callables.

use std::collections::{BTreeMap, BTreeSet};

use fnloom_core::graph::Graph as CoreGraph;
use fnloom_core::node::{Collect, Dependency, InputSpec, NodeId, NodeSpec, TagValue};
use fnloom_core::plan::Request;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyString};
use pyo3::{PyTraverseError, PyVisit};

use crate::errors::value_error;
use crate::plan::Plan;

/// The core graph as the binding instantiates it: every payload is a Python callable.
pub type PyCoreGraph = CoreGraph<Py<PyAny>>;

/// One node as Python declares it: `(name, [(source, kind, collect)], tags, fn, declines,
/// items)`.
///
/// `kind` is `"required"`, `"optional"`, `"admits"` or `"admits_optional"`; `collect` is
/// `"present"`, `"all"` or `None`. `items` is `None` for a node that does not expand, and
/// for an expand the callable making its item iterator, `items(iterable, record)`.
type NodeTuple = (
    String,
    Vec<(String, String, Option<String>)>,
    BTreeMap<String, PyTagValue>,
    Py<PyAny>,
    bool,
    Option<Py<PyAny>>,
);

/// A tag value as Python gives it: a `str`, or a `tuple` of them.
#[derive(FromPyObject)]
enum PyTagValue {
    One(String),
    Many(Vec<String>),
}

impl From<PyTagValue> for TagValue {
    fn from(value: PyTagValue) -> Self {
        match value {
            PyTagValue::One(text) => Self::One(text),
            PyTagValue::Many(items) => Self::Many(items),
        }
    }
}

/// An immutable dependency graph of Python functions, wired by parameter name.
///
/// It is the only owner of the node functions: a `Plan` reaches them through its
/// reference to this object, so the cyclic GC counts each function reference once.
#[pyclass(frozen, module = "fnloom._core")]
pub struct Graph {
    core: PyCoreGraph,
    /// Each node's name as an interned string, by node id: the keyword its consumers
    /// receive it under, interned once here instead of on every call.
    keywords: Vec<Py<PyString>>,
    /// By consumer id, the inputs it receives under another keyword, sorted by input id:
    /// a `@parameterize` row binding a parameter to a source of another name.
    renames: RenamesById,
    /// By node id, an expand's item iterator factory; `None` for any other node.
    items: Vec<Option<Py<PyAny>>>,
}

/// Renamed inputs by consumer id, each consumer's sorted by input id.
type RenamesById = Vec<Vec<(NodeId, Py<PyString>)>>;

#[pymethods]
impl Graph {
    /// Builds a graph from `(name, [(source, kind, collect)], tags, fn, declines, items)`
    /// tuples.
    ///
    /// Each `kind` is how the node reads that source: `"required"`, `"optional"` (it has
    /// a default), `"admits"` (it receives `fnloom.ABSENT`) or `"admits_optional"` (that,
    /// with a default). `collect` is `"present"` or `"all"` for a parameter collecting a
    /// block, else `None`. `declines` says whether the node may return `fnloom.ABSENT`;
    /// `items`, when not `None`, makes the node an expand whose value it iterates.
    ///
    /// `renames` maps a node to the inputs it is passed under another keyword: input
    /// name to keyword. Raises `ValueError` naming the culprits for a duplicate name or a
    /// cycle, a rename of a node or input the graph does not have, or an unknown kind.
    #[new]
    #[pyo3(signature = (nodes, renames = None))]
    fn new(
        py: Python<'_>,
        nodes: Vec<NodeTuple>,
        renames: Option<&Bound<'_, PyDict>>,
    ) -> PyResult<Self> {
        let mut factories = Vec::new();
        let mut specs = Vec::with_capacity(nodes.len());
        for (name, params, tags, function, declines, items) in nodes {
            let expands = items.is_some();
            if let Some(items) = items {
                factories.push((name.clone(), items));
            }
            specs.push(node_spec(name, params, tags, function, declines, expands)?);
        }
        let graph = CoreGraph::build(specs).map_err(value_error)?;
        let mut items: Vec<Option<Py<PyAny>>> = Vec::new();
        items.resize_with(graph.nodes().len(), || None);
        for (name, factory) in factories {
            if let Some(slot) = graph.id_of(&name).and_then(|id| items.get_mut(id.index())) {
                *slot = Some(factory);
            }
        }
        let keywords = graph
            .nodes()
            .iter()
            .map(|node| PyString::intern(py, &node.name).unbind())
            .collect();
        let renames = renamed(&graph, renames)?;
        Ok(Self {
            core: graph,
            keywords,
            renames,
            items,
        })
    }

    /// Lets the cyclic GC see the node functions, which may refer back to this graph
    /// through their module's globals.
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        for function in self.core.nodes().iter().filter_map(|node| node.payload()) {
            visit.call(function)?;
        }
        for factory in self.items.iter().flatten() {
            visit.call(factory)?;
        }
        Ok(())
    }

    /// Every node name, declared and synthesized inputs alike, sorted.
    fn node_names(&self) -> Vec<String> {
        self.core
            .nodes()
            .iter()
            .map(|node| node.name.clone())
            .collect()
    }

    /// The nodes whose value may be absent: each that declines, and each reading one of
    /// them by a required edge, transitively; sorted.
    fn may_be_absent(&self) -> Vec<String> {
        self.core
            .nodes()
            .iter()
            .filter(|node| node.may_be_absent)
            .map(|node| node.name.clone())
            .collect()
    }

    /// Each node inside a block, with its block path: the expands enclosing it, outermost
    /// first.
    fn blocks(&self) -> BTreeMap<String, Vec<String>> {
        self.core
            .nodes()
            .iter()
            .filter(|node| node.block.is_some())
            .map(|node| {
                (
                    node.name.clone(),
                    self.names_of(self.core.block_path(node.id)),
                )
            })
            .collect()
    }

    /// Nodes the named nodes depend on, transitively, excluding them; sorted.
    #[expect(
        clippy::needless_pass_by_value,
        reason = "PyO3 extracts a Python list into an owned Vec"
    )]
    fn upstream_of(&self, names: Vec<String>) -> PyResult<Vec<String>> {
        let ids = self
            .core
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
            .core
            .downstream_of(&as_strs(&names))
            .map_err(value_error)?;
        Ok(self.names_of(&ids))
    }

    /// Nodes on some path from `a` to `b`, endpoints excluded; sorted.
    fn between(&self, a: &str, b: &str) -> PyResult<Vec<String>> {
        let ids = self.core.between(a, b).map_err(value_error)?;
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
        let plan = slf.get().core.prepare(&request).map_err(value_error)?;
        Plan::new(slf.py(), slf.clone().unbind(), plan)
    }
}

impl Graph {
    /// The core graph.
    pub const fn core(&self) -> &PyCoreGraph {
        &self.core
    }

    /// Each node's interned name, by node id.
    pub fn keywords(&self) -> &[Py<PyString>] {
        &self.keywords
    }

    /// The keyword `consumer` receives `input` under, when it is not `input`'s own name.
    pub fn renamed(&self, consumer: NodeId, input: NodeId) -> Option<&Py<PyString>> {
        let renames = self.renames.get(consumer.index())?;
        let found = renames.binary_search_by_key(&input, |&(id, _)| id).ok()?;
        renames.get(found).map(|(_, keyword)| keyword)
    }

    /// The item iterator factory of expand `id`; `None` for any other node.
    pub fn items(&self, id: NodeId) -> Option<&Py<PyAny>> {
        self.items.get(id.index()).and_then(Option::as_ref)
    }

    fn names_of(&self, ids: &[NodeId]) -> Vec<String> {
        ids.iter()
            .map(|&id| self.core.node(id).name.clone())
            .collect()
    }
}

fn node_spec(
    name: String,
    params: Vec<(String, String, Option<String>)>,
    tags: BTreeMap<String, PyTagValue>,
    function: Py<PyAny>,
    declines: bool,
    expands: bool,
) -> PyResult<NodeSpec<Py<PyAny>>> {
    let inputs = params
        .into_iter()
        .map(|(source, kind, collect)| {
            let collect = match collect.as_deref() {
                None => None,
                Some("present") => Some(Collect::Present),
                Some("all") => Some(Collect::All),
                Some(mode) => {
                    return Err(PyValueError::new_err(format!(
                        "node '{name}' collects '{source}' as {mode:?}, which is no collect \
                         mode: 'present' or 'all'"
                    )));
                }
            };
            Ok(InputSpec {
                dependency: dependency(&name, &source, &kind)?,
                collect,
                name: source,
            })
        })
        .collect::<PyResult<Vec<_>>>()?;
    Ok(NodeSpec {
        name,
        inputs,
        tags: tags
            .into_iter()
            .map(|(key, value)| (key, value.into()))
            .collect(),
        payload: function,
        declines,
        expands,
    })
}

/// The dependency a kind names; `ValueError` naming the node and source for another kind.
fn dependency(node: &str, source: &str, kind: &str) -> PyResult<Dependency> {
    match kind {
        "required" => Ok(Dependency::Required),
        "optional" => Ok(Dependency::Optional),
        "admits" => Ok(Dependency::Admits { optional: false }),
        "admits_optional" => Ok(Dependency::Admits { optional: true }),
        _ => Err(PyValueError::new_err(format!(
            "node '{node}' reads '{source}' as {kind:?}, which is no kind: one of \
             'required', 'optional', 'admits' or 'admits_optional'"
        ))),
    }
}

fn as_strs(names: &[String]) -> Vec<&str> {
    names.iter().map(String::as_str).collect()
}

/// The renames by consumer id, each sorted by input id.
///
/// `renames` maps each renaming node's name to a dict of input name to the keyword
/// it is passed as. A keyword is kept as the `str` given: a parameter name read off a
/// code object, which Python has already interned. Names are read without copying.
/// `TypeError` for a name or keyword that is not a `str`, or a value that is not a
/// dict; `ValueError` naming every unknown node, sorted.
fn renamed(graph: &PyCoreGraph, renames: Option<&Bound<'_, PyDict>>) -> PyResult<RenamesById> {
    let mut by_id: RenamesById = Vec::new();
    by_id.resize_with(graph.nodes().len(), Vec::new);
    let Some(renames) = renames else {
        return Ok(by_id);
    };
    let mut unknown = BTreeSet::new();
    for (consumer, inputs) in renames.iter() {
        let consumer = consumer.cast::<PyString>()?.to_str()?;
        let inputs = inputs.cast::<PyDict>()?;
        let mut entries = Vec::with_capacity(inputs.len());
        for (input, keyword) in inputs.iter() {
            let input = input.cast::<PyString>()?.to_str()?;
            let keyword = keyword.cast_into::<PyString>()?.unbind();
            match graph.id_of(input) {
                Some(id) => entries.push((id, keyword)),
                None => {
                    unknown.insert(input.to_owned());
                }
            }
        }
        entries.sort_by_key(|&(id, _)| id);
        match graph.id_of(consumer) {
            Some(id) => {
                if let Some(slot) = by_id.get_mut(id.index()) {
                    *slot = entries;
                }
            }
            None => {
                unknown.insert(consumer.to_owned());
            }
        }
    }
    if !unknown.is_empty() {
        let listed: Vec<String> = unknown.iter().map(|name| format!("'{name}'")).collect();
        return Err(PyValueError::new_err(format!(
            "rename of unknown nodes: {}",
            listed.join(", ")
        )));
    }
    Ok(by_id)
}
