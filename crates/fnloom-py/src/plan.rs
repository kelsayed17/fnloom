//! The Python `Plan`, and the evaluator that calls each node's Python function.

use std::cell::{Cell, RefCell};
use std::collections::BTreeMap;
use std::sync::{Arc, OnceLock};

use fnloom_core::node::{Node, NodeId};
use fnloom_core::plan::{Action, Plan as CorePlan, Step};
use fnloom_core::run::{Arg, Blocks, Evaluate, Item, Observer};
use fnloom_core::schedule::Layout;
use pyo3::exceptions::{PyRuntimeError, PyTypeError};
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyDict, PyList, PyString, PyTuple};
use pyo3::{PyTraverseError, PyVisit};

use crate::absent::{self, Absent};
use crate::call::{Keywords, vectorcall};
use crate::errors::{caught, run_error};
use crate::graph::Graph;
use crate::keys::Keys;
use crate::limits::{Caps, Limits};
use crate::observer::{ObserverBase, PyObserver};
use crate::parallel;
use crate::pool::Threads;
use crate::stepper::{self, Stepper};
use crate::task::{Direct, Scope, undeclared_absence};

/// A value flowing along an edge: a strong reference, held under the GIL a run holds
/// throughout, so cloning it for an argument is one reference-count increment.
pub type Value<'py> = Bound<'py, PyAny>;

/// A validated execution order for one request, bound to the graph it was prepared from.
#[pyclass(frozen, module = "fnloom._core")]
pub struct Plan {
    /// Held so that a plan can never be paired with another graph, nor outlive its own.
    /// A Python reference rather than a shared Rust one, so the GC sees the edge.
    graph: Py<Graph>,
    core: CorePlan,
    /// By node id, the keywords each computed step passes its arguments under.
    calls: Vec<Option<Call>>,
    /// What every parallel run of this plan starts from, made by the first one, so a plan
    /// that never runs in parallel never makes it.
    layout: OnceLock<Arc<Layout>>,
    /// By node id, the order key of the node's step outside every block, as a tuple: made
    /// by the first run whose observers want orders.
    orders: OnceLock<Vec<Py<PyTuple>>>,
}

/// The keywords a computed step's function is called with, made when the plan is.
struct Call {
    /// The step's arguments, the nodes whose values are passed, in order.
    args: Box<[NodeId]>,
    /// Their keywords, in the same order.
    names: Keywords,
}

impl Plan {
    /// Binds a core plan to the graph that prepared it, and makes each computed step's
    /// keyword names, so a run passes its arguments without building a dict.
    pub fn new(py: Python<'_>, graph: Py<Graph>, plan: CorePlan) -> PyResult<Self> {
        let bound = graph.get();
        let mut calls: Vec<Option<Call>> = Vec::new();
        calls.resize_with(bound.core().nodes().len(), || None);
        let steps = plan
            .steps()
            .iter()
            .chain(plan.blocks().iter().flat_map(|block| &block.steps));
        for step in steps.filter(|step| step.action == Action::Compute) {
            let names = step
                .args
                .iter()
                .map(|&input| keyword_for(py, bound, step.node, input));
            let call = Call {
                args: step.args.clone().into_boxed_slice(),
                names: Keywords::new(py, names)?,
            };
            if let Some(slot) = calls.get_mut(step.node.index()) {
                *slot = Some(call);
            }
        }
        Ok(Self {
            graph,
            core: plan,
            calls,
            layout: OnceLock::new(),
            orders: OnceLock::new(),
        })
    }

    /// The graph the plan was prepared from.
    pub fn graph(&self) -> &Py<Graph> {
        &self.graph
    }

    /// The core plan.
    pub const fn core(&self) -> &CorePlan {
        &self.core
    }

    /// `id`'s order key outside every block, as a tuple, the table made on first use.
    fn outer_order<'py>(&self, py: Python<'py>, id: NodeId) -> PyResult<Bound<'py, PyTuple>> {
        if self.orders.get().is_none() {
            let count = self.graph.get().core().nodes().len();
            let made = (0..count)
                .map(|index| {
                    let id = NodeId(u32::try_from(index).unwrap_or(u32::MAX));
                    PyTuple::new(py, self.core.order_key(id, &[]).as_slice()).map(Bound::unbind)
                })
                .collect::<PyResult<Vec<_>>>()?;
            drop(self.orders.set(made));
        }
        let found = self.orders.get().and_then(|orders| orders.get(id.index()));
        Ok(found.map_or_else(|| PyTuple::empty(py), |order| order.bind(py).clone()))
    }

    /// The plan's scheduler layout, made on first use.
    pub fn layout(&self) -> Arc<Layout> {
        Arc::clone(
            self.layout
                .get_or_init(|| Arc::new(Layout::new(&self.core, self.graph.get().core()))),
        )
    }

    /// `run` on the calling thread, by `Plan::run`: the default path.
    #[expect(clippy::too_many_arguments, reason = "`run`'s parts, passed on")]
    fn run_here<'py>(
        &self,
        py: Python<'py>,
        inputs: &Bound<'py, PyDict>,
        overrides: &Bound<'py, PyDict>,
        observers: Vec<Bound<'py, ObserverBase>>,
        on_value: Option<Bound<'py, PyAny>>,
        late: Option<&Bound<'py, PyAny>>,
        limits: Option<&Caps>,
    ) -> PyResult<Bound<'py, PyDict>> {
        let graph = self.graph.get();
        let keywords = graph.keywords();
        let kwargs = Kwargs::new(py, self, graph);
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
            context: None,
        });
        let mut late_hook = late.map(|late| LateHook {
            late,
            plan: &self.core,
            graph,
            keywords,
            context: None,
        });
        let mut hooks: Vec<&mut dyn Observer<Value<'py>, PyErr>> = adapters
            .iter_mut()
            .map(|adapter| adapter as &mut dyn Observer<Value<'py>, PyErr>)
            .chain(
                value_hook
                    .as_mut()
                    .map(|hook| hook as &mut dyn Observer<Value<'py>, PyErr>),
            )
            .chain(
                late_hook
                    .as_mut()
                    .map(|hook| hook as &mut dyn Observer<Value<'py>, PyErr>),
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
                Ok(value) => value,
                Err(error) => {
                    failure.get_or_insert(error);
                    None
                }
            }
        };
        let outcome = self.core.run_by_id(
            graph.core(),
            &mut Caller {
                kwargs: &kwargs,
                shared,
                absent: absent::singleton(py)?,
                outputs: self.core.outputs(),
                calls: &self.calls,
                late,
                limits,
                absent_on: Cell::new(false),
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
            result.set_item(keyword(py, keywords, id, name), value)?;
        }
        Ok(result)
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
    ///
    /// With `threads`, the run is on the core scheduler, its tasks on `threads`' pool and
    /// on the calling thread, each call in its own copy of the caller's context, and its
    /// hooks one at a time, each on the thread that ran its node (`parallel`), granted
    /// slots by the pool's limits.
    ///
    /// Without `threads`, `limits` grants each limited node's call its slots, waiting
    /// detached for a release while refused, and releasing them as soon as the call returns.
    ///
    /// `timeout`, in seconds, needs `threads`: the calling thread then runs no task, and
    /// once it passes the run stops, abandons its running tasks and raises `TimeoutError`.
    // PyO3 renders the `Vec::new()` default as `Ellipsis`, so the signature says `()` itself.
    #[pyo3(
        signature = (inputs, overrides, observers = Vec::new(), on_value = None, late = None, *, threads = None, limits = None, timeout = None),
        text_signature = "($self, /, inputs, overrides, observers=(), on_value=None, late=None, *, threads=None, limits=None, timeout=None)"
    )]
    #[expect(
        clippy::too_many_arguments,
        reason = "Python's keyword arguments, one per part of a run"
    )]
    fn run<'py>(
        slf: &Bound<'py, Self>,
        py: Python<'py>,
        inputs: &Bound<'py, PyDict>,
        overrides: &Bound<'py, PyDict>,
        observers: Vec<Bound<'py, ObserverBase>>,
        on_value: Option<Bound<'py, PyAny>>,
        late: Option<&Bound<'py, PyAny>>,
        threads: Option<&Bound<'py, Threads>>,
        limits: Option<&Bound<'py, Limits>>,
        timeout: Option<f64>,
    ) -> PyResult<Bound<'py, PyDict>> {
        if let Some(threads) = threads {
            return parallel::run(
                slf, inputs, overrides, observers, on_value, late, threads, timeout,
            );
        }
        if timeout.is_some() {
            return Err(PyTypeError::new_err(
                "timeout= needs Builder.with_threads(...)",
            ));
        }
        let this = slf.get();
        let limits = limits.map(|limits| &**limits.get().caps());
        this.run_here(py, inputs, overrides, observers, on_value, late, limits)
    }

    /// A run of the plan on the core scheduler, for the async driver to step from its loop
    /// (`Stepper`): seeded from `inputs` and `overrides` as `run` reads them, its events
    /// sent to `observers`, `on_value` and `late` as `run` sends them, but for `pre_graph`
    /// and `post_graph`, which the driver fires itself.
    ///
    /// `limits` grants each task its slots: a node named in `pooled`, which runs on
    /// `threads`' pool, takes a worker slot; every other task none. An expand is pulled
    /// only while fewer than `in_flight` of its branches are unfinished.
    #[pyo3(
        signature = (inputs, overrides, observers = Vec::new(), on_value = None, late = None, *, limits = None, threads = None, pooled = Vec::new(), in_flight = usize::MAX),
        text_signature = "($self, /, inputs, overrides, observers=(), on_value=None, late=None, *, limits=None, threads=None, pooled=(), in_flight=...)"
    )]
    #[expect(
        clippy::too_many_arguments,
        reason = "Python's keyword arguments, one per part of a run"
    )]
    fn start<'py>(
        slf: &Bound<'py, Self>,
        inputs: &Bound<'py, PyDict>,
        overrides: &Bound<'py, PyDict>,
        observers: Vec<Bound<'py, ObserverBase>>,
        on_value: Option<Bound<'py, PyAny>>,
        late: Option<&Bound<'py, PyAny>>,
        limits: Option<&Bound<'py, Limits>>,
        threads: Option<&Bound<'py, Threads>>,
        pooled: Vec<String>,
        in_flight: usize,
    ) -> PyResult<Stepper> {
        stepper::start(
            slf, inputs, overrides, observers, on_value, late, limits, threads, pooled, in_flight,
        )
    }

    /// The steps in execution order, as `(node, action)` pairs.
    ///
    /// Each block's steps follow its expand's step, in the order one branch runs them, so
    /// every node the plan computes is listed once, nested blocks too.
    fn steps(&self) -> Vec<(String, &'static str)> {
        let graph = self.graph.get().core();
        let mut listed = Vec::with_capacity(self.core.steps().len());
        // An explicit stack of the step lists still being read, so nesting never recurses.
        let mut open: Vec<std::slice::Iter<'_, Step>> = vec![self.core.steps().iter()];
        while let Some(steps) = open.last_mut() {
            let Some(step) = steps.next() else {
                open.pop();
                continue;
            };
            listed.push((graph.node(step.node).name.clone(), action_name(step.action)));
            if let Some(block) = self.core.block_of(step.node) {
                open.push(self.core.blocks()[block.index()].steps.iter());
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
            .filter(|(name, _)| graph.id_of(name).is_some_and(|id| self.core.is_late(id)))
            .map(|(name, _)| name)
            .collect()
    }

    /// Each expand whose block the plan runs, with that block's own steps as `(node,
    /// action)` pairs, in the order one branch runs them; a nested block's steps are
    /// listed under its own expand.
    fn blocks(&self) -> BTreeMap<String, Vec<(String, &'static str)>> {
        let graph = self.graph.get().core();
        self.core
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
pub fn keyword<'py>(
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

/// The keyword `consumer` takes `input`'s value under: a `@parameterize` row's own
/// name for it, else `input`'s interned name.
pub fn keyword_for<'py>(
    py: Python<'py>,
    graph: &Graph,
    consumer: NodeId,
    input: NodeId,
) -> Bound<'py, PyString> {
    match graph.renamed(consumer, input) {
        Some(renamed) => renamed.bind(py).clone(),
        None => keyword(py, graph.keywords(), input, &graph.core().node(input).name),
    }
}

/// Builds each node's keyword arguments, by parameter name.
///
/// When a run has observers, the dict built for a node's `pre_node` is kept until the
/// node's call takes it, so observers and function get the very same dict and it is
/// built once.
pub struct Kwargs<'py, 'g> {
    py: Python<'py>,
    /// The plan, for its steps' order keys.
    plan: &'g Plan,
    /// The graph, for the keywords a `@parameterize` row renames.
    graph: &'g Graph,
    /// The dict built for the observers of the node about to run, until its call.
    pending: RefCell<Option<(NodeId, Bound<'py, PyDict>)>>,
    /// The last order key made, by the address and length of the node name it was made
    /// for and the branch: a node's events come one after another, so `post_node` reuses
    /// `pre_node`'s.
    order: RefCell<Option<LastOrder<'py>>>,
}

/// An order key made, with the address and length of its node's name, and its branch.
type LastOrder<'py> = (usize, usize, Vec<usize>, Bound<'py, PyTuple>);

impl<'py, 'g> Kwargs<'py, 'g> {
    /// The keyword arguments of a run of `plan` over `graph`, none built yet.
    pub fn new(py: Python<'py>, plan: &'g Plan, graph: &'g Graph) -> Self {
        Self {
            py,
            plan,
            graph,
            pending: RefCell::new(None),
            order: RefCell::new(None),
        }
    }

    /// The keyword arguments of one parallel task: `kwargs`, already built for `consumer`,
    /// lent to its observers.
    pub fn lending(
        py: Python<'py>,
        plan: &'g Plan,
        graph: &'g Graph,
        consumer: NodeId,
        kwargs: Option<Bound<'py, PyDict>>,
    ) -> Self {
        let this = Self::new(py, plan, graph);
        *this.pending.borrow_mut() = kwargs.map(|kwargs| (consumer, kwargs));
        this
    }
}

impl<'py> Kwargs<'py, '_> {
    /// A new dict of `args`, keyed as `consumer`'s function takes them.
    pub fn build(
        &self,
        consumer: NodeId,
        args: &[Arg<'_, Value<'py>>],
    ) -> PyResult<Bound<'py, PyDict>> {
        let kwargs = PyDict::new(self.py);
        for arg in args {
            // CPython binds a keyword to a parameter by identity with the interned
            // parameter name, else by `==` against every parameter. A fresh key string
            // takes the slow path, which makes an n-parameter call cost O(n²).
            let keyword = keyword_for(self.py, self.graph, consumer, arg.node);
            kwargs.set_item(keyword, &arg.value)?;
        }
        Ok(kwargs)
    }

    /// `node`'s order key in `branch`, as a tuple (`Plan::order_key`).
    pub fn order(&self, node: &str, branch: &[usize]) -> PyResult<Bound<'py, PyTuple>> {
        // The core passes each node's own name, so its address and length name the node;
        // the length too, so a caller passing a slice of a name can't match it.
        let address = node.as_ptr() as usize;
        if let Some((at, len, of, order)) = self.order.borrow().as_ref() {
            if *at == address && *len == node.len() && of.as_slice() == branch {
                return Ok(order.clone());
            }
        }
        let Some(id) = self.graph.core().id_of(node) else {
            return Err(PyRuntimeError::new_err(format!(
                "broken plan: node '{node}' is not in the graph"
            )));
        };
        let order = if branch.is_empty() {
            self.plan.outer_order(self.py, id)?
        } else {
            PyTuple::new(self.py, self.plan.core.order_key(id, branch).as_slice())?
        };
        *self.order.borrow_mut() = Some((address, node.len(), branch.to_vec(), order.clone()));
        Ok(order)
    }

    /// The dict lent for `node`'s observers, if one is: a running node's arguments, which
    /// its `post_node` reports too.
    pub fn lent(&self, node: &str) -> Option<Bound<'py, PyDict>> {
        let consumer = self.graph.core().id_of(node)?;
        let pending = self.pending.borrow();
        let (id, kwargs) = pending.as_ref()?;
        (*id == consumer).then(|| kwargs.clone())
    }

    /// The last order key made, which `post_node` of the same step reuses.
    pub fn last_order(&self) -> Option<Bound<'py, PyTuple>> {
        self.order
            .borrow()
            .as_ref()
            .map(|(_, _, _, order)| order.clone())
    }

    /// Keeps `order` as `node`'s key in `branch`, made by an earlier event of that step:
    /// `node` must be the graph's own name of it, which keys the cache by address.
    pub fn reuse_order(&self, node: &str, branch: &[usize], order: Bound<'py, PyTuple>) {
        *self.order.borrow_mut() =
            Some((node.as_ptr() as usize, node.len(), branch.to_vec(), order));
    }

    /// The dict `node`'s function is about to be called with, built by the first
    /// observer to ask and kept for the call.
    pub fn for_observers(
        &self,
        node: &str,
        args: &[Arg<'_, Value<'py>>],
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
    fn for_call(
        &self,
        consumer: NodeId,
        args: &[Arg<'_, Value<'py>>],
    ) -> PyResult<Bound<'py, PyDict>> {
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
    /// The keywords of each computed step, by node id.
    calls: &'k [Option<Call>],
    /// A cached run's late keys, as `Plan.run`'s `late` describes; `None` uncached.
    late: Option<&'k Bound<'py, PyAny>>,
    /// The driver's limits, granting each limited node's call; `None` for a driver with
    /// none, whose calls test nothing more.
    limits: Option<&'k Caps>,
    /// Whether the last call or pull that failed raised an error its node is absent on
    /// (`Graph::absent_on`): the core asks `absent_on_error` right after, of that node.
    /// Only the error the node's own code raised counts, never one fnloom made.
    absent_on: Cell<bool>,
}

impl<'py> Evaluate<Py<PyAny>> for Caller<'_, 'py, '_> {
    type Value = Value<'py>;
    type Error = PyErr;

    fn evaluate(
        &mut self,
        node: &Node<Py<PyAny>>,
        function: &Py<PyAny>,
        args: &[Arg<'_, Value<'py>>],
    ) -> PyResult<Value<'py>> {
        caught(|| self.call(node, function, args))
    }

    /// Whether `value` is `fnloom.ABSENT`, by identity.
    fn is_absent(&self, value: &Value<'py>) -> bool {
        value.as_ptr() == self.absent.as_ptr()
    }

    /// `fnloom.ABSENT` when the error just raised is one the node is absent on.
    fn absent_on_error(&mut self, _node: &Node<Py<PyAny>>, _error: &PyErr) -> Option<Value<'py>> {
        self.absent_on
            .take()
            .then(|| self.absent.bind(self.kwargs.py).clone().into_any())
    }

    fn blocks(&mut self) -> Option<&mut dyn Blocks<Py<PyAny>, Value<'py>, PyErr>> {
        Some(self)
    }

    /// Calls a block node with `fnloom.current_branch()` set to `branch` for the call,
    /// in the caller's context; a node outside every block is called as `evaluate` does.
    fn evaluate_in(
        &mut self,
        node: &Node<Py<PyAny>>,
        function: &Py<PyAny>,
        args: &[Arg<'_, Value<'py>>],
        branch: &[usize],
    ) -> PyResult<Value<'py>> {
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
    fn lookup(&mut self, node: &Node<Py<PyAny>>, branch: &[usize]) -> PyResult<Option<Value<'py>>> {
        if self.late.is_none() {
            return Ok(None);
        }
        self.scope().lookup(&Direct, node, branch)
    }
}

impl<'k, 'py> Caller<'k, 'py, '_> {
    /// What `task.rs` needs of this run.
    fn scope(&self) -> Scope<'k, 'py> {
        Scope {
            graph: self.kwargs.graph,
            outputs: self.outputs,
            late: self.late,
            absent: self.absent.bind(self.kwargs.py),
        }
    }
}

/// `fnloom._blocks.BRANCH`, the context variable `fnloom.current_branch()` reads.
pub fn branch_variable(py: Python<'_>) -> PyResult<&'static Py<PyAny>> {
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
impl<'py> Blocks<Py<PyAny>, Value<'py>, PyErr> for Caller<'_, 'py, '_> {
    fn iterate(
        &mut self,
        node: &Node<Py<PyAny>>,
        iterable: &Value<'py>,
        branch: &[usize],
    ) -> PyResult<Value<'py>> {
        self.scope().iterate(&Direct, node, iterable, branch)
    }

    fn next_item(
        &mut self,
        node: &Node<Py<PyAny>>,
        iterator: &Value<'py>,
    ) -> PyResult<Item<Value<'py>>> {
        // A generator's body runs as its items are read, so its pulls are its calls.
        let pulled = match self.limits.filter(|limits| limits.limited(node.id)) {
            Some(limits) => limits.around(self.kwargs.py, node.id, || {
                self.scope().next_item(&Direct, node, iterator)
            }),
            None => self.scope().next_item(&Direct, node, iterator),
        }?;
        pulled.inspect_err(|error| self.raised(node, error))
    }

    fn finish_items(
        &mut self,
        _node: &Node<Py<PyAny>>,
        iterator: &Value<'py>,
        exhausted: bool,
    ) -> PyResult<Value<'py>> {
        self.scope().finish_items(&Direct, iterator, exhausted)
    }

    fn gather(
        &mut self,
        _consumer: &Node<Py<PyAny>>,
        _source: NodeId,
        values: Vec<Value<'py>>,
    ) -> PyResult<Value<'py>> {
        let py = self.kwargs.py;
        let list = PyList::new(py, &values)?;
        Ok(list.into_any())
    }
}

/// Where each computed node's value goes.
pub enum Sink<'py> {
    /// A Python callable, called with the node's name and value.
    Call(Bound<'py, PyAny>),
    /// A run's keys, from `KeyPlan.walk`: stored in its memory store with no Python call.
    Store(Bound<'py, Keys>),
}

/// Hands each computed node's name and value to `on_value`, as the last observer.
pub struct ValueHook<'py, 'g> {
    pub sink: Sink<'py>,
    /// The graph, to find a node's id by name.
    pub graph: &'g Graph,
    /// The graph's interned node names, by node id.
    pub keywords: &'g [Py<PyString>],
    /// The `run` method of the context `on_value` is called in; `None` for the current one.
    pub context: Option<&'g Bound<'py, PyAny>>,
}

impl<'py> Observer<Value<'py>, PyErr> for ValueHook<'py, '_> {
    /// A node in a branch is left out: its name alone would not say which value it is.
    fn post_node_in(
        &mut self,
        node: &str,
        branch: &[usize],
        outcome: Result<&Value<'py>, &PyErr>,
    ) -> PyResult<()> {
        if branch.is_empty() {
            self.post_node(node, outcome)
        } else {
            Ok(())
        }
    }

    fn post_node(&mut self, node: &str, outcome: Result<&Value<'py>, &PyErr>) -> PyResult<()> {
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
                caught(|| match self.context {
                    None => callback.call1((name, value)).map(drop),
                    Some(run) => run.call1((callback, name, value)).map(drop),
                })
            }
            Sink::Store(keys) => caught(|| keys.get().store(keys.py(), node, value.as_unbound())),
        }
    }
}

/// Hands each late step's value to `late.store`, as the last observer.
pub struct LateHook<'py, 'g> {
    pub late: &'g Bound<'py, PyAny>,
    pub plan: &'g CorePlan,
    pub graph: &'g Graph,
    pub keywords: &'g [Py<PyString>],
    /// The `run` method of the context `late.store` is called in; `None` for the current one.
    pub context: Option<&'g Bound<'py, PyAny>>,
}

impl<'py> Observer<Value<'py>, PyErr> for LateHook<'py, '_> {
    fn post_node_in(
        &mut self,
        node: &str,
        branch: &[usize],
        outcome: Result<&Value<'py>, &PyErr>,
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
            let branch = PyTuple::new(py, branch)?;
            match self.context {
                None => self
                    .late
                    .call_method1(intern!(py, "store"), (name, branch, value))
                    .map(drop),
                Some(run) => {
                    let store = self.late.getattr(intern!(py, "store"))?;
                    run.call1((store, name, branch, value)).map(drop)
                }
            }
        })
    }
}

impl<'py> Caller<'_, 'py, '_> {
    /// Notes whether `error`, raised by `node`'s own code, is one `node` is absent on.
    #[cold]
    fn raised(&self, node: &Node<Py<PyAny>>, error: &PyErr) {
        if node.absent_on_error {
            let py = self.kwargs.py;
            self.absent_on
                .set(self.kwargs.graph.absent_on(py, node.id, error));
        }
    }

    /// Calls `function`, the payload of `consumer`, with `args` as keyword arguments,
    /// inside its grant when `consumer` is limited.
    fn call(
        &self,
        consumer: &Node<Py<PyAny>>,
        function: &Py<PyAny>,
        args: &[Arg<'_, Value<'py>>],
    ) -> PyResult<Value<'py>> {
        match self.limits.filter(|limits| limits.limited(consumer.id)) {
            Some(limits) => limits.around(self.kwargs.py, consumer.id, || {
                self.call_unlimited(consumer, function, args)
            }),
            None => self.call_unlimited(consumer, function, args),
        }
    }

    /// Calls `function`, the payload of `consumer`, with `args` as keyword arguments.
    ///
    /// A `TypeError` when the function returns `fnloom.ABSENT` but `consumer` does not
    /// decline: its return annotation does not admit `fnloom.Absent`.
    fn call_unlimited(
        &self,
        consumer: &Node<Py<PyAny>>,
        function: &Py<PyAny>,
        args: &[Arg<'_, Value<'py>>],
    ) -> PyResult<Value<'py>> {
        let py = self.kwargs.py;
        let called = if self.shared {
            let kwargs = self.kwargs.for_call(consumer.id, args)?;
            function.bind(py).call((), Some(&kwargs))
        } else {
            match self.calls.get(consumer.id.index()) {
                Some(Some(call)) if call.args.iter().eq(args.iter().map(|arg| &arg.node)) => {
                    vectorcall(function.bind(py), values(args), &call.names)
                }
                // Arguments other than the step's own, as an omitted optional one makes.
                _ => {
                    let names = args
                        .iter()
                        .map(|arg| keyword_for(py, self.kwargs.graph, consumer.id, arg.node));
                    vectorcall(function.bind(py), values(args), &Keywords::new(py, names)?)
                }
            }
        };
        let result = called.inspect_err(|error| self.raised(consumer, error))?;
        if !consumer.declines && result.as_ptr() == self.absent.as_ptr() {
            return Err(undeclared_absence(&consumer.name));
        }
        Ok(result)
    }
}

/// The values of `args`, in order.
fn values<'a, 'py>(
    args: &'a [Arg<'_, Value<'py>>],
) -> impl ExactSizeIterator<Item = &'a Value<'py>> {
    args.iter().map(|arg| &arg.value)
}
