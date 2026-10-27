//! The Python `Plan`, and the evaluator that calls each node's Python function.

use std::collections::BTreeMap;
use std::sync::Arc;

use fnloom_core::node::Node;
use fnloom_core::plan::{Action, Plan as CorePlan};
use fnloom_core::run::{Arg, Evaluate, Observer};
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyString};
use pyo3::{PyTraverseError, PyVisit};

use crate::errors::{caught, run_error};
use crate::graph::Graph;
use crate::observer::{ObserverBase, PyObserver};

/// A value flowing along an edge. `Arc` makes the core's per-argument clones free of
/// reference-count traffic on the Python object, and needs no GIL to clone.
type Value = Arc<Py<PyAny>>;

/// A validated execution order for one request, bound to the graph it was prepared from.
#[pyclass(frozen, module = "fnloom._core")]
pub struct Plan {
    /// Held so that a plan can never be paired with another graph, nor outlive its own.
    /// A Python reference rather than a shared Rust one, so the GC sees the edge.
    graph: Py<Graph>,
    plan: CorePlan,
}

impl Plan {
    /// Binds a core plan to the graph that prepared it.
    pub const fn new(graph: Py<Graph>, plan: CorePlan) -> Self {
        Self { graph, plan }
    }
}

#[pymethods]
impl Plan {
    /// Lets the cyclic GC follow this plan's reference to its graph.
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.graph)
    }

    /// Runs the plan and returns exactly the requested outputs, keyed by name.
    ///
    /// Every observer sees every event, in sequence order. The GIL is held throughout.
    #[pyo3(signature = (inputs, overrides, observers = Vec::new()))]
    fn run<'py>(
        &self,
        py: Python<'py>,
        inputs: BTreeMap<String, Py<PyAny>>,
        overrides: BTreeMap<String, Py<PyAny>>,
        observers: Vec<Bound<'py, ObserverBase>>,
    ) -> PyResult<Bound<'py, PyDict>> {
        let mut adapters: Vec<PyObserver<'py>> =
            observers.into_iter().map(PyObserver::new).collect();
        let mut hooks: Vec<&mut dyn Observer<PyErr>> = adapters
            .iter_mut()
            .map(|adapter| adapter as &mut dyn Observer<PyErr>)
            .collect();
        let graph = self.graph.get();
        let outputs = self
            .plan
            .run(
                graph.core(),
                &mut Caller {
                    py,
                    keywords: graph.keywords(),
                },
                &wrap(inputs),
                &wrap(overrides),
                &mut hooks,
            )
            .map_err(|error| run_error(py, error))?;
        let result = PyDict::new(py);
        for (name, value) in outputs {
            result.set_item(name, value.bind(py))?;
        }
        Ok(result)
    }

    /// The steps in execution order, as `(node, action)` pairs.
    fn steps(&self) -> Vec<(String, &'static str)> {
        let graph = self.graph.get().core();
        self.plan
            .steps()
            .iter()
            .map(|step| (graph.node(step.node).name.clone(), action_name(step.action)))
            .collect()
    }
}

/// The Python-facing name of an action.
const fn action_name(action: Action) -> &'static str {
    match action {
        Action::Compute => "compute",
        Action::UseInput => "use_input",
        Action::UseOverride => "use_override",
        Action::SkipOptionalInput => "skip_optional_input",
    }
}

fn wrap(values: BTreeMap<String, Py<PyAny>>) -> BTreeMap<String, Value> {
    values
        .into_iter()
        .map(|(name, value)| (name, Arc::new(value)))
        .collect()
}

/// Calls each node's function with its arguments as keyword arguments.
struct Caller<'py, 'g> {
    py: Python<'py>,
    /// The graph's interned node names, by node id.
    keywords: &'g [Py<PyString>],
}

impl Evaluate<Py<PyAny>> for Caller<'_, '_> {
    type Value = Value;
    type Error = PyErr;

    fn evaluate(&mut self, node: &Node<Py<PyAny>>, args: &[Arg<'_, Value>]) -> PyResult<Value> {
        caught(|| self.call(node, args))
    }
}

impl<'py> Caller<'py, '_> {
    /// Calls `node`'s function with `args` as keyword arguments.
    fn call(&self, node: &Node<Py<PyAny>>, args: &[Arg<'_, Value>]) -> PyResult<Value> {
        let Some(function) = &node.payload else {
            // The core plans `Compute` only for function nodes, which always carry a payload.
            return Err(PyRuntimeError::new_err(format!(
                "node '{}' has no function to call",
                node.name
            )));
        };
        let kwargs = PyDict::new(self.py);
        for arg in args {
            // CPython binds a keyword to a parameter by identity with the interned
            // parameter name, else by `==` against every parameter. A fresh key string
            // takes the slow path, which makes an n-parameter call cost O(n²).
            kwargs.set_item(self.keyword(arg), arg.value.bind(self.py))?;
        }
        let result = function.bind(self.py).call((), Some(&kwargs))?;
        Ok(Arc::new(result.unbind()))
    }

    /// The interned keyword for `arg`, interned when built; interned now only if the
    /// table lacks it, which a plan run on its own graph rules out.
    fn keyword(&self, arg: &Arg<'_, Value>) -> Bound<'py, PyString> {
        self.keywords.get(arg.node.index()).map_or_else(
            || PyString::intern(self.py, arg.name),
            |keyword| keyword.bind(self.py).clone(),
        )
    }
}
