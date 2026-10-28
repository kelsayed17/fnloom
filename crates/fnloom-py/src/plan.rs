//! The Python `Plan`, and the evaluator that calls each node's Python function.

use std::cell::RefCell;
use std::sync::Arc;

use fnloom_core::node::{Node, NodeId};
use fnloom_core::plan::{Action, Plan as CorePlan};
use fnloom_core::run::{Arg, Evaluate, Observer};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyString};
use pyo3::{PyTraverseError, PyVisit};

use crate::errors::{caught, run_error};
use crate::graph::Graph;
use crate::keys::Keys;
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

    /// Runs the plan and returns exactly the requested outputs, keyed by name, sorted.
    ///
    /// Each value is read from `inputs` or `overrides` by its node's interned name, and
    /// each output is keyed by it, so a run makes no name strings. Extra keys are ignored.
    /// Every observer sees every event, in sequence order. The GIL is held throughout.
    ///
    /// `on_value(name, value)` is called with each computed node's name and value right
    /// after that node succeeds and every observer has seen its `post_node`, before any
    /// downstream node runs; never for an input, an override or a skipped optional input.
    /// It is driven as one more observer, so an exception from it is raised like an
    /// observer's: as itself, after the closing `post_graph(ok=False)`.
    // PyO3 renders the `Vec::new()` default as `Ellipsis`, so the signature says `()` itself.
    #[pyo3(
        signature = (inputs, overrides, observers = Vec::new(), on_value = None),
        text_signature = "($self, /, inputs, overrides, observers=(), on_value=None)"
    )]
    fn run<'py>(
        &self,
        py: Python<'py>,
        inputs: &Bound<'py, PyDict>,
        overrides: &Bound<'py, PyDict>,
        observers: Vec<Bound<'py, ObserverBase>>,
        on_value: Option<Bound<'py, PyAny>>,
    ) -> PyResult<Bound<'py, PyDict>> {
        let graph = self.graph.get();
        let keywords = graph.keywords();
        // The value of the node just computed, handed from the evaluator to `on_value`.
        let latest = RefCell::new(None);
        let mut adapters: Vec<PyObserver<'py>> =
            observers.into_iter().map(PyObserver::new).collect();
        let collect_values = on_value.is_some();
        let mut value_hook = on_value.map(|callback| ValueHook {
            sink: match callback.cast_into::<Keys>() {
                Ok(keys) => Sink::Store(keys),
                Err(error) => Sink::Call(error.into_inner()),
            },
            keywords,
            latest: &latest,
        });
        let mut hooks: Vec<&mut dyn Observer<PyErr>> = adapters
            .iter_mut()
            .map(|adapter| adapter as &mut dyn Observer<PyErr>)
            .chain(
                value_hook
                    .as_mut()
                    .map(|hook| hook as &mut dyn Observer<PyErr>),
            )
            .collect();
        // A failed lookup (a key whose `__eq__` raises) is kept and raised after the run.
        let mut failure = None;
        let lookup = |action, id: NodeId| {
            let values = if action == Action::UseOverride {
                overrides
            } else {
                inputs
            };
            let key = keyword(py, keywords, id, &graph.core().node(id).name);
            match values.get_item(key) {
                Ok(value) => value.map(|value| Arc::new(value.unbind())),
                Err(error) => {
                    failure.get_or_insert(error);
                    None
                }
            }
        };
        let outcome = self.plan.run_by_id(
            graph.core(),
            &mut Caller {
                py,
                graph,
                keywords,
                latest: collect_values.then_some(&latest),
            },
            lookup,
            &mut hooks,
        );
        if let Some(error) = failure {
            return Err(error);
        }
        let outputs = outcome.map_err(|error| run_error(py, error))?;
        let result = PyDict::new(py);
        for (id, value) in outputs {
            let name = &graph.core().node(id).name;
            result.set_item(keyword(py, keywords, id, name), value.bind(py))?;
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

/// The interned name of node `id`, called `name`, interned when the graph was built;
/// interned now only if the table lacks it, which a plan run on its own graph rules out.
fn keyword<'py>(
    py: Python<'py>,
    keywords: &[Py<PyString>],
    id: NodeId,
    name: &str,
) -> Bound<'py, PyString> {
    keywords.get(id.index()).map_or_else(
        || PyString::intern(py, name),
        |keyword| keyword.bind(py).clone(),
    )
}

/// Calls each node's function with its arguments as keyword arguments.
struct Caller<'py, 'g> {
    py: Python<'py>,
    /// The graph, for the keywords a `@parameterize` row renames.
    graph: &'g Graph,
    /// The graph's interned node names, by node id.
    keywords: &'g [Py<PyString>],
    /// Where each computed value is left for `on_value`; `None` when there is none.
    latest: Option<&'g RefCell<Option<(NodeId, Value)>>>,
}

impl Evaluate<Py<PyAny>> for Caller<'_, '_> {
    type Value = Value;
    type Error = PyErr;

    fn evaluate(
        &mut self,
        node: &Node<Py<PyAny>>,
        function: &Py<PyAny>,
        args: &[Arg<'_, Value>],
    ) -> PyResult<Value> {
        let value = caught(|| self.call(node.id, function, args))?;
        if let Some(latest) = self.latest {
            *latest.borrow_mut() = Some((node.id, Arc::clone(&value)));
        }
        Ok(value)
    }
}

/// Where each computed node's value goes.
enum Sink<'py> {
    /// A Python callable, called with the node's name and value.
    Call(Bound<'py, PyAny>),
    /// A run's keys, from `KeyPlan.walk`: stored in its memory store with no Python call.
    Store(Bound<'py, Keys>),
}

/// Hands each computed node's name and value to `on_value`, as the last observer.
struct ValueHook<'py, 'g> {
    sink: Sink<'py>,
    /// The graph's interned node names, by node id.
    keywords: &'g [Py<PyString>],
    /// The value the evaluator left for the node just computed.
    latest: &'g RefCell<Option<(NodeId, Value)>>,
}

impl Observer<PyErr> for ValueHook<'_, '_> {
    fn post_node(&mut self, node: &str, ok: bool) -> PyResult<()> {
        let Some((id, value)) = self.latest.borrow_mut().take() else {
            return Ok(());
        };
        if !ok {
            return Ok(());
        }
        match &self.sink {
            Sink::Call(callback) => {
                let py = callback.py();
                let name = keyword(py, self.keywords, id, node);
                caught(|| callback.call1((name, value.bind(py))).map(drop))
            }
            Sink::Store(keys) => caught(|| keys.get().store(keys.py(), node, &value)),
        }
    }
}

impl Caller<'_, '_> {
    /// Calls `function`, the payload of `consumer`, with `args` as keyword arguments.
    fn call(
        &self,
        consumer: NodeId,
        function: &Py<PyAny>,
        args: &[Arg<'_, Value>],
    ) -> PyResult<Value> {
        let kwargs = PyDict::new(self.py);
        for arg in args {
            // CPython binds a keyword to a parameter by identity with the interned
            // parameter name, else by `==` against every parameter. A fresh key string
            // takes the slow path, which makes an n-parameter call cost O(n²).
            let keyword = match self.graph.renamed(consumer, arg.node) {
                Some(renamed) => renamed.bind(self.py).clone(),
                None => keyword(self.py, self.keywords, arg.node, arg.name),
            };
            kwargs.set_item(keyword, arg.value.bind(self.py))?;
        }
        let result = function.bind(self.py).call((), Some(&kwargs))?;
        Ok(Arc::new(result.unbind()))
    }
}
