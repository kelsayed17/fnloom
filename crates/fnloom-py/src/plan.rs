//! The Python `Plan`, and the evaluator that calls each node's Python function.

use std::cell::RefCell;
use std::collections::BTreeMap;
use std::sync::Arc;

use fnloom_core::node::{Node, NodeId};
use fnloom_core::plan::{Action, Plan as CorePlan, Step};
use fnloom_core::run::{Arg, Blocks, Evaluate, Item, Observer};
use pyo3::exceptions::{PyRuntimeError, PyStopIteration, PyTypeError};
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyDict, PyList, PyString, PyTuple};
use pyo3::{PyTraverseError, PyVisit};

use crate::absent::{self, Absent};
use crate::errors::{caught, run_error};
use crate::graph::Graph;
use crate::keys::Keys;
use crate::observer::{ObserverBase, PyObserver};

/// A value flowing along an edge. `Arc` makes the core's per-argument clones free of
/// reference-count traffic on the Python object, and needs no GIL to clone.
pub type Value = Arc<Py<PyAny>>;

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
    /// downstream node runs; never for an input, an override, a skipped optional input or
    /// a node in a branch, whose value is one of many and goes to `late.store` instead.
    /// It is driven as one more observer, so an exception from it is raised like an
    /// observer's: as itself, after the closing `post_graph(ok=False)`.
    ///
    /// `late`, for a plan with blocks, keys the steps downstream of an expand during the
    /// run: `late.lookup(node, branch)` gives a hit as a 1-tuple, or `None`;
    /// `late.store(node, branch, value)` takes each such step's value once it succeeds;
    /// `late.pulled(expand, item)` hears of each item, `late.finished(expand, value)` of
    /// each exhausted expand, and `late.records(expand)` says whether its items are kept.
    // PyO3 renders the `Vec::new()` default as `Ellipsis`, so the signature says `()` itself.
    #[pyo3(
        signature = (inputs, overrides, observers = Vec::new(), on_value = None, late = None),
        text_signature = "($self, /, inputs, overrides, observers=(), on_value=None, late=None)"
    )]
    fn run<'py>(
        &self,
        py: Python<'py>,
        inputs: &Bound<'py, PyDict>,
        overrides: &Bound<'py, PyDict>,
        observers: Vec<Bound<'py, ObserverBase>>,
        on_value: Option<Bound<'py, PyAny>>,
        late: Option<&Bound<'py, PyAny>>,
    ) -> PyResult<Bound<'py, PyDict>> {
        let graph = self.graph.get();
        let keywords = graph.keywords();
        let kwargs = Kwargs {
            py,
            graph,
            keywords,
            pending: RefCell::new(None),
        };
        let mut adapters: Vec<PyObserver<'py, '_>> = observers
            .into_iter()
            .map(|observer| PyObserver::new(observer, &kwargs))
            .collect();
        let shared = !adapters.is_empty();
        let mut value_hook = on_value.map(|callback| ValueHook {
            sink: match callback.cast_into::<Keys>() {
                Ok(keys) => Sink::Store(keys),
                Err(error) => Sink::Call(error.into_inner()),
            },
            graph,
            keywords,
        });
        let mut late_hook = late.map(|late| LateHook {
            late,
            plan: &self.plan,
            graph,
            keywords,
        });
        let mut hooks: Vec<&mut dyn Observer<Value, PyErr>> = adapters
            .iter_mut()
            .map(|adapter| adapter as &mut dyn Observer<Value, PyErr>)
            .chain(
                value_hook
                    .as_mut()
                    .map(|hook| hook as &mut dyn Observer<Value, PyErr>),
            )
            .chain(
                late_hook
                    .as_mut()
                    .map(|hook| hook as &mut dyn Observer<Value, PyErr>),
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
                kwargs: &kwargs,
                shared,
                absent: absent::singleton(py)?,
                outputs: self.plan.outputs(),
                late,
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
    ///
    /// Each block's steps follow its expand's step, in the order one branch runs them, so
    /// every node the plan computes is listed once, nested blocks too.
    fn steps(&self) -> Vec<(String, &'static str)> {
        let graph = self.graph.get().core();
        let mut listed = Vec::with_capacity(self.plan.steps().len());
        // An explicit stack of the step lists still being read, so nesting never recurses.
        let mut open: Vec<std::slice::Iter<'_, Step>> = vec![self.plan.steps().iter()];
        while let Some(steps) = open.last_mut() {
            let Some(step) = steps.next() else {
                open.pop();
                continue;
            };
            listed.push((graph.node(step.node).name.clone(), action_name(step.action)));
            if let Some(block) = self.plan.block_of(step.node) {
                open.push(self.plan.blocks()[block.index()].steps.iter());
            }
        }
        listed
    }

    /// The computed steps downstream of an expand, whose cache keys are known only
    /// during the run, in plan order: every block's steps, and each outer step reading
    /// a collect or another such step.
    fn late(&self) -> Vec<String> {
        let graph = self.graph.get().core();
        self.steps()
            .into_iter()
            .filter(|(name, _)| graph.id_of(name).is_some_and(|id| self.plan.is_late(id)))
            .map(|(name, _)| name)
            .collect()
    }

    /// Each expand whose block the plan runs, with that block's own steps as `(node,
    /// action)` pairs, in the order one branch runs them; a nested block's steps are
    /// listed under its own expand.
    fn blocks(&self) -> BTreeMap<String, Vec<(String, &'static str)>> {
        let graph = self.graph.get().core();
        self.plan
            .blocks()
            .iter()
            .map(|sub| {
                (
                    graph.node(sub.expand).name.clone(),
                    sub.steps
                        .iter()
                        .map(|step| (graph.node(step.node).name.clone(), action_name(step.action)))
                        .collect(),
                )
            })
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

/// Builds each node's keyword arguments, by parameter name.
///
/// When a run has observers, the dict built for a node's `pre_node` is kept until the
/// node's call takes it, so observers and function get the very same dict and it is
/// built once.
pub struct Kwargs<'py, 'g> {
    py: Python<'py>,
    /// The graph, for the keywords a `@parameterize` row renames.
    graph: &'g Graph,
    /// The graph's interned node names, by node id.
    keywords: &'g [Py<PyString>],
    /// The dict built for the observers of the node about to run, until its call.
    pending: RefCell<Option<(NodeId, Bound<'py, PyDict>)>>,
}

impl<'py> Kwargs<'py, '_> {
    /// A new dict of `args`, keyed as `consumer`'s function takes them.
    fn build(&self, consumer: NodeId, args: &[Arg<'_, Value>]) -> PyResult<Bound<'py, PyDict>> {
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
        Ok(kwargs)
    }

    /// The dict `node`'s function is about to be called with, built by the first
    /// observer to ask and kept for the call.
    pub fn for_observers(
        &self,
        node: &str,
        args: &[Arg<'_, Value>],
    ) -> PyResult<Bound<'py, PyDict>> {
        let Some(consumer) = self.graph.core().id_of(node) else {
            return Err(PyRuntimeError::new_err(format!(
                "broken plan: node '{node}' is not in the graph"
            )));
        };
        let mut pending = self.pending.borrow_mut();
        if let Some((id, kwargs)) = pending.as_ref() {
            if *id == consumer {
                return Ok(kwargs.clone());
            }
        }
        let kwargs = self.build(consumer, args)?;
        *pending = Some((consumer, kwargs.clone()));
        Ok(kwargs)
    }

    /// The dict to call `consumer`'s function with: the observers' one, else a new one.
    fn for_call(&self, consumer: NodeId, args: &[Arg<'_, Value>]) -> PyResult<Bound<'py, PyDict>> {
        match self.pending.borrow_mut().take() {
            Some((id, kwargs)) if id == consumer => Ok(kwargs),
            _ => self.build(consumer, args),
        }
    }
}

/// Calls each node's function with its arguments as keyword arguments.
struct Caller<'k, 'py, 'g> {
    kwargs: &'k Kwargs<'py, 'g>,
    /// Whether observers may have left a node's dict in `kwargs`; `false` for a run with
    /// none, which then only ever builds one.
    shared: bool,
    /// `fnloom.ABSENT`, which results are compared with by identity.
    absent: &'static Py<Absent>,
    /// The plan's outputs, sorted by id: an expand among them records its items.
    outputs: &'k [NodeId],
    /// A cached run's late keys, as `Plan.run`'s `late` describes; `None` uncached.
    late: Option<&'k Bound<'py, PyAny>>,
}

impl Evaluate<Py<PyAny>> for Caller<'_, '_, '_> {
    type Value = Value;
    type Error = PyErr;

    fn evaluate(
        &mut self,
        node: &Node<Py<PyAny>>,
        function: &Py<PyAny>,
        args: &[Arg<'_, Value>],
    ) -> PyResult<Value> {
        caught(|| self.call(node, function, args))
    }

    /// Whether `value` is `fnloom.ABSENT`, by identity.
    fn is_absent(&self, value: &Value) -> bool {
        value.as_ptr() == self.absent.as_ptr()
    }

    fn blocks(&mut self) -> Option<&mut dyn Blocks<Py<PyAny>, Value, PyErr>> {
        Some(self)
    }

    /// Calls a block node with `fnloom.current_branch()` set to `branch` for the call,
    /// in the caller's context; a node outside every block is called as `evaluate` does.
    fn evaluate_in(
        &mut self,
        node: &Node<Py<PyAny>>,
        function: &Py<PyAny>,
        args: &[Arg<'_, Value>],
        branch: &[usize],
    ) -> PyResult<Value> {
        if branch.is_empty() {
            return self.evaluate(node, function, args);
        }
        let py = self.kwargs.py;
        let current = branch_variable(py)?.bind(py);
        let token = current.call_method1(intern!(py, "set"), (PyTuple::new(py, branch)?,))?;
        let outcome = caught(|| self.call(node, function, args));
        let reset = current.call_method1(intern!(py, "reset"), (token,));
        let value = outcome?;
        reset.map(|_| value)
    }

    /// A cache hit from `late.lookup`, which returns a 1-tuple, or `None` for none.
    fn lookup(&mut self, node: &Node<Py<PyAny>>, branch: &[usize]) -> PyResult<Option<Value>> {
        let Some(late) = self.late else {
            return Ok(None);
        };
        let py = self.kwargs.py;
        let name = keyword(py, self.kwargs.keywords, node.id, &node.name);
        let found =
            caught(|| late.call_method1(intern!(py, "lookup"), (name, PyTuple::new(py, branch)?)))?;
        if found.is_none() {
            return Ok(None);
        }
        Ok(Some(Arc::new(found.get_item(0)?.unbind())))
    }
}

/// `fnloom._blocks.BRANCH`, the context variable `fnloom.current_branch()` reads.
fn branch_variable(py: Python<'_>) -> PyResult<&'static Py<PyAny>> {
    static BRANCH: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
    BRANCH.get_or_try_init(py, || {
        Ok(py
            .import(intern!(py, "fnloom._blocks"))?
            .getattr(intern!(py, "BRANCH"))?
            .unbind())
    })
}

/// Blocks through Python: an expand's items come from the iterator its node's factory
/// makes (`fnloom._blocks.Items`), and a collect argument is a `list`.
impl Blocks<Py<PyAny>, Value, PyErr> for Caller<'_, '_, '_> {
    fn iterate(
        &mut self,
        node: &Node<Py<PyAny>>,
        iterable: &Value,
        branch: &[usize],
    ) -> PyResult<Value> {
        let py = self.kwargs.py;
        let Some(factory) = self.kwargs.graph.items(node.id) else {
            return Err(PyRuntimeError::new_err(format!(
                "broken plan: expand '{}' has no item iterator",
                node.name
            )));
        };
        let mut record = self.outputs.binary_search(&node.id).is_ok();
        if let Some(late) = self.late.filter(|_| !record) {
            let name = keyword(py, self.kwargs.keywords, node.id, &node.name);
            record = caught(|| late.call_method1(intern!(py, "records"), (name,)))?.is_truthy()?;
        }
        // A generator's body runs as its items are read, so in the expand's branch.
        let branch = PyTuple::new(py, branch)?;
        caught(|| {
            factory
                .bind(py)
                .call1((iterable.bind(py), record, branch))
                .map(|items| Arc::new(items.unbind()))
        })
    }

    fn next_item(&mut self, node: &Node<Py<PyAny>>, iterator: &Value) -> PyResult<Item<Value>> {
        let py = self.kwargs.py;
        match caught(|| iterator.bind(py).call_method0(intern!(py, "__next__"))) {
            Ok(item) => {
                if let Some(late) = self.late {
                    let name = keyword(py, self.kwargs.keywords, node.id, &node.name);
                    caught(|| {
                        late.call_method1(intern!(py, "pulled"), (name, &item))
                            .map(drop)
                    })?;
                }
                Ok(Item::Next(Arc::new(item.unbind())))
            }
            Err(error) if error.is_instance_of::<PyStopIteration>(py) => {
                let value = error.value(py).getattr(intern!(py, "value"))?;
                if !value.is(self.absent.bind(py)) {
                    return Ok(Item::Done);
                }
                if !node.declines {
                    return Err(undeclared_absence(&node.name));
                }
                Ok(Item::Declined(Arc::new(value.unbind())))
            }
            Err(error) => Err(error),
        }
    }

    fn finish_items(
        &mut self,
        node: &Node<Py<PyAny>>,
        iterator: &Value,
        exhausted: bool,
    ) -> PyResult<Value> {
        let py = self.kwargs.py;
        let value = caught(|| {
            iterator
                .bind(py)
                .call_method1(intern!(py, "finish"), (exhausted,))
        })?;
        if let Some(late) = self.late.filter(|_| exhausted) {
            let name = keyword(py, self.kwargs.keywords, node.id, &node.name);
            caught(|| {
                late.call_method1(intern!(py, "finished"), (name, &value))
                    .map(drop)
            })?;
        }
        Ok(Arc::new(value.unbind()))
    }

    fn gather(
        &mut self,
        _consumer: &Node<Py<PyAny>>,
        _source: NodeId,
        values: Vec<Value>,
    ) -> PyResult<Value> {
        let py = self.kwargs.py;
        let list = PyList::new(py, values.iter().map(|value| value.bind(py)))?;
        Ok(Arc::new(list.into_any().unbind()))
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
    /// The graph, to find a node's id by name.
    graph: &'g Graph,
    /// The graph's interned node names, by node id.
    keywords: &'g [Py<PyString>],
}

impl Observer<Value, PyErr> for ValueHook<'_, '_> {
    /// A node in a branch is left out: its name alone would not say which value it is.
    fn post_node_in(
        &mut self,
        node: &str,
        branch: &[usize],
        outcome: Result<&Value, &PyErr>,
    ) -> PyResult<()> {
        if branch.is_empty() {
            self.post_node(node, outcome)
        } else {
            Ok(())
        }
    }

    fn post_node(&mut self, node: &str, outcome: Result<&Value, &PyErr>) -> PyResult<()> {
        let Ok(value) = outcome else {
            return Ok(());
        };
        match &self.sink {
            Sink::Call(callback) => {
                let py = callback.py();
                let name = self.graph.core().id_of(node).map_or_else(
                    || PyString::intern(py, node),
                    |id| keyword(py, self.keywords, id, node),
                );
                caught(|| callback.call1((name, value.bind(py))).map(drop))
            }
            Sink::Store(keys) => caught(|| keys.get().store(keys.py(), node, value)),
        }
    }
}

/// Hands each late step's value to `late.store`, as the last observer.
struct LateHook<'py, 'g> {
    late: &'g Bound<'py, PyAny>,
    plan: &'g CorePlan,
    graph: &'g Graph,
    keywords: &'g [Py<PyString>],
}

impl Observer<Value, PyErr> for LateHook<'_, '_> {
    fn post_node_in(
        &mut self,
        node: &str,
        branch: &[usize],
        outcome: Result<&Value, &PyErr>,
    ) -> PyResult<()> {
        let Ok(value) = outcome else {
            return Ok(());
        };
        let Some(id) = self
            .graph
            .core()
            .id_of(node)
            .filter(|&id| self.plan.is_late(id))
        else {
            return Ok(());
        };
        let py = self.late.py();
        let name = keyword(py, self.keywords, id, node);
        caught(|| {
            self.late
                .call_method1(
                    intern!(py, "store"),
                    (name, PyTuple::new(py, branch)?, value.bind(py)),
                )
                .map(drop)
        })
    }
}

impl Caller<'_, '_, '_> {
    /// Calls `function`, the payload of `consumer`, with `args` as keyword arguments.
    ///
    /// A `TypeError` when the function returns `fnloom.ABSENT` but `consumer` does not
    /// decline: its return annotation does not admit `fnloom.Absent`.
    fn call(
        &self,
        consumer: &Node<Py<PyAny>>,
        function: &Py<PyAny>,
        args: &[Arg<'_, Value>],
    ) -> PyResult<Value> {
        let kwargs = if self.shared {
            self.kwargs.for_call(consumer.id, args)?
        } else {
            self.kwargs.build(consumer.id, args)?
        };
        let result = function.bind(self.kwargs.py).call((), Some(&kwargs))?;
        if !consumer.declines && result.as_ptr() == self.absent.as_ptr() {
            return Err(undeclared_absence(&consumer.name));
        }
        Ok(Arc::new(result.unbind()))
    }
}

/// The `TypeError` for `node` returning `fnloom.ABSENT` without declaring it may.
#[cold]
fn undeclared_absence(node: &str) -> PyErr {
    PyTypeError::new_err(format!(
        "node '{node}' returned fnloom.ABSENT, but its return annotation does not admit \
         fnloom.Absent"
    ))
}
