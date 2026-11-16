//! The async driver's run: a plan on the core scheduler, stepped from an event loop.
//!
//! `fnloom._async` asks `next` for each task, runs it where it belongs (an `async def`
//! node as a task on the loop, a sync node in a thread or on the loop, an async
//! generator's pull awaited) and hands its outcome to `complete`. The rest happens here,
//! on the loop's thread, as the parallel workers do it: late lookups and probes, an
//! expand's iterator, the hooks, `on_value` and `late.store`, and the run's failure, which
//! `outputs` raises.
//!
//! Only the loop's thread steps a run, so the stepper holds its scheduler with no lock of
//! its own: `PyO3`'s borrow flag refuses a second caller, from any thread, rather than
//! letting two in. No Rust lock is held while it calls Python.
//!
//! Slots. The scheduler grants a task's slots as it hands the task out. A sync node or a
//! sync generator's pull on the driver's pool takes a worker slot (`Seat::Take`); every
//! other task takes none (`Seat::Threadless`), so `async def` nodes never wait for a
//! worker. A limited node's own and group slots are held around its call only: given back
//! before its `before_node` hooks and taken back after them, waiting parked here while
//! refused; and given back as its outcome comes in, before its `after_node` hooks.

use std::collections::VecDeque;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};

use fnloom_core::limits::{Grant, Limits as CoreLimits};
use fnloom_core::node::NodeId;
use fnloom_core::run::{Item, RunError};
use fnloom_core::schedule::{
    self, ArgValue, Next, Options, OrderKey, Outcome, Report, Scheduler, Seat, TaskId, Work,
};
use pyo3::exceptions::PyRuntimeError;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyDict, PyList, PyString, PyTuple};

use crate::absent;
use crate::errors::run_error;
use crate::graph::Graph;
use crate::limits::{Caps, Limits, held_variable};
use crate::observer::ObserverBase;
use crate::parallel::{Failure, Out, Sched, Shared, outcome_of, seed, shared, unpaired};
use crate::plan::{Kwargs, Plan, branch_variable, keyword, keyword_for};
use crate::pool::{Pool, Threads};
use crate::shared::SharedObservers;
use crate::task::{Direct, Scope, undeclared_absence};

/// `next`'s status when it hands out no task: nothing is left.
const DONE: u8 = 1;
/// The status bit for a run refused a slot, which a release may let go on.
const WAITING: u8 = 2;
/// The status bit for a stopping run: it failed, or was stopped.
const STOPPING: u8 = 4;

/// One run of a plan, stepped by the async driver: `Plan.start` makes it.
#[pyclass(module = "fnloom._core")]
pub struct Stepper {
    plan: Py<Plan>,
    /// `None` once `outputs` took it.
    scheduler: Option<Sched>,
    /// Every failure the run saw, with its order key.
    failures: Vec<(OrderKey, Failure)>,
    /// The tasks handed to Python and not yet completed, by the index Python names them by.
    running: Vec<Option<Running>>,
    free: Vec<usize>,
    /// Computes whose slots were given back for their hooks and are not taken back yet.
    parked: VecDeque<usize>,
    /// What the last completion settled, kept so its buffers are reused.
    report: Report<Shared>,
    /// Whether the last `next` was refused a slot.
    refused: bool,
    /// The driver's limits; `None` for a driver with none, whose tasks are all granted.
    caps: Option<Arc<Caps>>,
    /// The driver's pool, which a worker slot given back wakes.
    pool: Option<Arc<Pool>>,
    /// By node id, whether the node's computes and pulls run on the pool, holding a
    /// worker slot.
    pooled: Vec<bool>,
    /// `fnloom._threads.HELD` as the run started: its ancestors' slots.
    held: Py<PyAny>,
    observers: SharedObservers,
    /// The process the run started in.
    pid: u32,
}

/// A task handed to Python.
struct Running {
    id: TaskId,
    node: NodeId,
    branch: Vec<usize>,
    key: OrderKey,
    doing: Doing,
    /// Whether it holds a worker slot.
    worker: bool,
    /// Whether it holds its node's own and group slots.
    slots: bool,
    /// The live flags of the `HELD` entries it set, cleared as its slots are given back.
    live: Vec<Py<PyList>>,
    /// A parked compute's keyword arguments, until it is handed out.
    kwargs: Option<Py<PyDict>>,
    /// The context a compute runs in, and its hooks and stores with it; `None` for the
    /// caller's own.
    context: Option<Py<PyAny>>,
    /// A hooked compute's keyword arguments and order key, as its `pre_node` lent and
    /// made them, for its `post_node`.
    lent: Option<Py<PyDict>>,
    order: Option<Py<PyTuple>>,
}

/// What a running task does.
#[derive(Clone, Copy, PartialEq, Eq)]
enum Doing {
    Compute,
    Pull,
    Finish,
}

/// The grant of an async run: a pooled node's computes and pulls take a worker slot, and
/// every other task none; whether any was refused is kept for `next`'s status.
struct Grants<'a> {
    core: Option<&'a CoreLimits>,
    pooled: &'a [bool],
    refused: AtomicBool,
}

impl<'a> Grants<'a> {
    fn new(caps: Option<&'a Arc<Caps>>, pooled: &'a [bool]) -> Self {
        Self {
            core: caps.map(|caps| &caps.core),
            pooled,
            refused: AtomicBool::new(false),
        }
    }

    fn seat(&self, node: Option<NodeId>) -> Seat {
        match node {
            Some(node) if self.pooled.get(node.index()) == Some(&true) => Seat::Take,
            _ => Seat::Threadless,
        }
    }
}

impl Grant for Grants<'_> {
    fn try_start(&self, node: Option<NodeId>, _seat: Seat) -> bool {
        let Some(core) = self.core else {
            return true;
        };
        let granted = core.try_start(node, self.seat(node));
        if !granted {
            self.refused.store(true, Ordering::Relaxed);
        }
        granted
    }

    fn release(&self, node: Option<NodeId>, _seat: Seat) {
        if let Some(core) = self.core {
            core.release(node, self.seat(node));
        }
    }
}

/// The grant a completion gives slots back to: the worker slot only, when the task still
/// holds it, since the stepper gives a task's node and group slots back itself.
struct WorkersOnly<'a>(Grants<'a>, bool);

impl Grant for WorkersOnly<'_> {
    fn try_start(&self, node: Option<NodeId>, seat: Seat) -> bool {
        self.0.try_start(node, seat)
    }

    fn release(&self, node: Option<NodeId>, _seat: Seat) {
        if let Some(core) = self.0.core.filter(|_| self.1) {
            core.release(None, self.0.seat(node));
        }
    }
}

/// `contextvars.copy_context`.
fn copy_context(py: Python<'_>) -> PyResult<Bound<'_, PyAny>> {
    static COPY: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
    let copy = COPY.get_or_try_init(py, || {
        Ok::<_, PyErr>(
            py.import(intern!(py, "contextvars"))?
                .getattr(intern!(py, "copy_context"))?
                .unbind(),
        )
    })?;
    copy.bind(py).call0()
}

/// The `run` method of `context`, which calls into it.
fn context_run<'py>(context: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    context.getattr(intern!(context.py(), "run"))
}

/// A copy of the caller's context, with `fnloom.current_branch()` set to `branch`.
fn in_branch<'py>(py: Python<'py>, branch: &[usize]) -> PyResult<Bound<'py, PyAny>> {
    let context = copy_context(py)?;
    if !branch.is_empty() {
        let set = branch_variable(py)?.bind(py).getattr(intern!(py, "set"))?;
        context.call_method1(intern!(py, "run"), (set, PyTuple::new(py, branch)?))?;
    }
    Ok(context)
}

/// `iterator`, paired with the context its every pull and its finish run in: a copy of the
/// caller's, in the expand's branch, so a context variable its body sets lasts from one
/// item to the next.
fn paired(py: Python<'_>, iterator: Bound<'_, PyAny>, branch: &[usize]) -> PyResult<Shared> {
    let pair = PyTuple::new(py, [iterator, in_branch(py, branch)?])?;
    Ok(shared(pair.into_any()))
}

/// The keyword arguments of `consumer`'s call from `args`, emptied: a gathered collect
/// argument as a `list`, an omitted one left out.
fn kwargs_of<'py>(
    py: Python<'py>,
    graph: &Graph,
    consumer: NodeId,
    args: &mut Vec<(NodeId, ArgValue<Shared>)>,
) -> PyResult<Bound<'py, PyDict>> {
    let kwargs = PyDict::new(py);
    for (input, arg) in args.drain(..) {
        let value = match arg {
            ArgValue::One(value) => value.bind(py).clone(),
            ArgValue::Gather(values) => {
                PyList::new(py, values.iter().map(|value| value.bind(py)))?.into_any()
            }
            ArgValue::Omitted => continue,
        };
        kwargs.set_item(keyword_for(py, graph, consumer, input), value)?;
    }
    Ok(kwargs)
}

/// The outcome of a task whose result is dropped: the run stopped.
fn dropped(py: Python<'_>) -> Out {
    Outcome::Value {
        value: Arc::new(py.None()),
        absent: false,
    }
}

/// The interned name of a task's work, as `next` hands it out.
fn work_name(py: Python<'_>, doing: Doing, exhausted: bool) -> &Bound<'_, PyString> {
    match (doing, exhausted) {
        (Doing::Compute, _) => intern!(py, "compute"),
        (Doing::Pull, _) => intern!(py, "pull"),
        (Doing::Finish, true) => intern!(py, "finish"),
        (Doing::Finish, false) => intern!(py, "close"),
    }
}

impl Stepper {
    /// `RuntimeError` in a forked child, which touches nothing it inherited.
    fn alive(&self) -> PyResult<()> {
        if std::process::id() == self.pid {
            return Ok(());
        }
        Err(PyRuntimeError::new_err(format!(
            "this run was started in process {}; a run doesn't survive os.fork()",
            self.pid
        )))
    }

    fn stopping(&self) -> bool {
        self.scheduler.as_ref().is_none_or(Sched::is_stopping)
    }

    /// The status bits of a run that hands out no task now.
    fn status(&self, done: bool) -> u8 {
        let mut status = u8::from(done);
        if self.refused {
            status |= WAITING;
        }
        if self.stopping() {
            status |= STOPPING;
        }
        status
    }

    fn put(&mut self, running: Running) -> usize {
        if let Some(index) = self.free.pop() {
            if let Some(slot) = self.running.get_mut(index) {
                *slot = Some(running);
                return index;
            }
        }
        self.running.push(Some(running));
        self.running.len() - 1
    }

    fn take(&mut self, index: usize) -> Option<Running> {
        let taken = self.running.get_mut(index).and_then(Option::take);
        if taken.is_some() {
            self.free.push(index);
        }
        taken
    }

    /// Whether `node` has a cap of its own or a group.
    fn limited(&self, node: NodeId) -> bool {
        self.caps.as_ref().is_some_and(|caps| caps.limited(node))
    }

    /// Takes `node`'s own slot and its group's back, if they are free now.
    fn retake(&self, node: NodeId) -> bool {
        self.caps
            .as_ref()
            .is_none_or(|caps| caps.core.try_start(Some(node), Seat::Threadless))
    }

    /// Gives a task's node and group slots back, if it holds them, waking every waiter,
    /// and clears the `HELD` entries it set.
    fn give_back(&self, py: Python<'_>, running: &mut Running) {
        if std::mem::take(&mut running.slots) {
            match (&self.pool, &self.caps) {
                (Some(pool), _) => pool.release(py, running.node),
                (None, Some(caps)) => caps.release(py, running.node),
                (None, None) => {}
            }
        }
        for flag in running.live.drain(..) {
            drop(flag.bind(py).set_item(0, false));
        }
    }

    /// Records `failure` at `key` and stops the run.
    fn fail_at(&mut self, key: OrderKey, failure: RunError<PyErr>) {
        self.failures.push((key, Failure::Run(failure)));
        if let Some(scheduler) = self.scheduler.as_mut() {
            scheduler.stop();
        }
    }

    /// The `HELD` value with entries for a worker slot and `node`'s slots, as a task holds
    /// them, set in `context`; their live flags go to `live`.
    fn hold(
        &self,
        context: &Bound<'_, PyAny>,
        worker: bool,
        node: Option<NodeId>,
        live: &mut Vec<Py<PyList>>,
    ) -> PyResult<()> {
        let Some(caps) = self.caps.as_ref().filter(|_| worker || node.is_some()) else {
            return Ok(());
        };
        let py = context.py();
        let mut held = self.held.bind(py).clone();
        if worker {
            let (holding, flag) = caps.holding_worker(&held)?;
            held = holding;
            live.push(flag.unbind());
        }
        if let Some(node) = node {
            let (holding, flag) = caps.holding(&held, node)?;
            held = holding;
            live.push(flag.unbind());
        }
        let set = held_variable(py)?.bind(py).getattr(intern!(py, "set"))?;
        context.call_method1(intern!(py, "run"), (set, held))?;
        Ok(())
    }

    /// The run's calls into Python for a task's work, as `task.rs` makes them.
    fn scope<'a, 'py>(
        &'a self,
        py: Python<'py>,
        late: Option<&'a Bound<'py, PyAny>>,
    ) -> PyResult<Scope<'a, 'py>> {
        let plan = self.plan.get();
        Ok(Scope {
            graph: plan.graph().get(),
            outputs: plan.core().outputs(),
            late,
            absent: absent::singleton(py)?.bind(py),
        })
    }

    /// Takes in what a task gave: gives its worker slot back, records its failure, fires
    /// what its completion settled, and drops the values no step reads any more.
    fn settle(
        &mut self,
        py: Python<'_>,
        running: &Running,
        outcome: Out,
        failure: Option<Failure>,
        stop: bool,
    ) {
        let plan = self.plan.get();
        let core = plan.core();
        let graph = plan.graph().get().core();
        if let Some(failure) = failure {
            let at = match &outcome {
                // A probe's failure is its node's, keyed by that node's step.
                Outcome::LookupFailed(failed, _) => core.order_key(*failed, &running.branch),
                _ => running.key.clone(),
            };
            self.failures.push((at, failure));
        }
        let grants = WorkersOnly(
            Grants::new(self.caps.as_ref(), &self.pooled),
            running.worker,
        );
        let mut report = std::mem::take(&mut self.report);
        if let Some(scheduler) = self.scheduler.as_mut() {
            if stop {
                scheduler.stop();
            }
            scheduler.complete(core, graph, &grants, running.id, outcome, &mut report);
        }
        if running.worker {
            if let Some(pool) = &self.pool {
                pool.notify(py);
            }
        }
        self.fire(py, &report, running.context.as_ref(), &running.branch);
        // The values no step reads any more are dropped here, attached.
        report.clear();
        self.report = report;
    }

    /// Fires what a report settled, hits, then expands, then skips, as `Report` says, in
    /// `context`, the completed task's, or else a fresh copy in its `branch`, as the
    /// parallel path does. The first event an observer raised in stops the run.
    fn fire(
        &mut self,
        py: Python<'_>,
        report: &Report<Shared>,
        context: Option<&Py<PyAny>>,
        branch: &[usize],
    ) {
        let quiet =
            report.hits.is_empty() && report.expanded.is_empty() && report.skipped.is_empty();
        if quiet || !self.observers.any() {
            return;
        }
        let made = match context {
            Some(context) => Some(context.bind(py).clone()),
            None if !branch.is_empty() => in_branch(py, branch).ok(),
            None => None,
        };
        let run = made.as_ref().and_then(|context| context_run(context).ok());
        let run = run.as_ref();
        let plan = self.plan.get();
        let graph = plan.graph().get();
        let core = graph.core();
        let kwargs = Kwargs::new(py, plan, graph);
        // shortcut: `parallel::Run::report` without its busy flag, kept apart so the stopping
        // PR's edits to `parallel.rs` merge cleanly; fold the two together after it lands.
        let mut failed = None;
        for (node, branch, value) in &report.hits {
            let name = &core.node(*node).name;
            let errors = self.observers.fire(py, &kwargs, run, |hook| {
                hook.hit_in(name, branch, value.bind(py))
            });
            if !errors.is_empty() {
                failed = Some((plan.core().order_key(*node, branch), "hit", errors));
                break;
            }
        }
        if failed.is_none() {
            for (node, branch, items) in &report.expanded {
                let name = &core.node(*node).name;
                let errors = self.observers.fire(py, &kwargs, run, |hook| {
                    hook.post_expand(name, branch, *items)
                });
                if !errors.is_empty() {
                    failed = Some((plan.core().order_key(*node, branch), "post_expand", errors));
                    break;
                }
            }
        }
        if failed.is_none() {
            for (node, branch, key) in &report.skipped {
                let name = &core.node(*node).name;
                let errors = self
                    .observers
                    .fire(py, &kwargs, run, |hook| hook.skipped(name, branch));
                if !errors.is_empty() {
                    failed = Some((key.clone(), "skipped", errors));
                    break;
                }
            }
        }
        if let Some((key, event, errors)) = failed {
            self.fail_at(key, RunError::Observers { event, errors });
        }
    }

    /// Completes a task that failed before it was handed out.
    fn failed(&mut self, py: Python<'_>, mut running: Running, error: PyErr) {
        self.give_back(py, &mut running);
        let failure = Some(Failure::Task(Vec::new()));
        self.settle(py, &running, Outcome::Failed(error), failure, false);
    }

    /// Hands `running` to Python, doing its work on `payload`: in `context`, an iterator's
    /// own, or for a compute in the context its branch and slots need, `None` for the
    /// caller's. A task that can't be handed out fails.
    fn hand_out<'py>(
        &mut self,
        py: Python<'py>,
        mut running: Running,
        work: &Bound<'py, PyString>,
        payload: Bound<'py, PyAny>,
        context: Option<Bound<'py, PyAny>>,
    ) -> Option<Bound<'py, PyTuple>> {
        let held = running.slots.then_some(running.node);
        let mut live = Vec::new();
        let context = match context {
            Some(context) if running.doing != Doing::Finish => self
                .hold(&context, running.worker, held, &mut live)
                .map(|()| Some(context)),
            Some(context) => Ok(Some(context)),
            None if running.branch.is_empty()
                && (self.caps.is_none() || (!running.worker && held.is_none())) =>
            {
                Ok(None)
            }
            None => in_branch(py, &running.branch).and_then(|context| {
                self.hold(&context, running.worker, held, &mut live)
                    .map(|()| Some(context))
            }),
        };
        running.live = live;
        let context = match context {
            Ok(context) => context,
            Err(error) => {
                self.failed(py, running, error);
                return None;
            }
        };
        if running.doing == Doing::Compute {
            // Its `after_node` hooks and stores run where it ran.
            running.context = context.as_ref().map(|context| context.clone().unbind());
        }
        let graph = self.plan.get().graph().get();
        let name = keyword(
            py,
            graph.keywords(),
            running.node,
            &graph.core().node(running.node).name,
        );
        let branch = if running.branch.is_empty() {
            Ok(PyTuple::empty(py))
        } else {
            PyTuple::new(py, &running.branch)
        };
        let index = self.put(running);
        let handed = branch.and_then(|branch| {
            PyTuple::new(
                py,
                [
                    index.into_pyobject(py)?.into_any(),
                    name.into_any(),
                    branch.into_any(),
                    work.clone().into_any(),
                    payload,
                    context.unwrap_or_else(|| py.None().into_bound(py)),
                ],
            )
        });
        match handed {
            Ok(handed) => Some(handed),
            Err(error) => {
                if let Some(running) = self.take(index) {
                    self.failed(py, running, error);
                }
                None
            }
        }
    }

    /// The first parked compute that can take its slots back, handed out; parked computes
    /// are dropped once the run stops.
    fn unpark<'py>(&mut self, py: Python<'py>) -> Option<Bound<'py, PyTuple>> {
        let stopping = self.stopping();
        let parked: Vec<usize> = self.parked.iter().copied().collect();
        for index in parked {
            let Some(node) = self
                .running
                .get(index)
                .and_then(Option::as_ref)
                .map(|running| running.node)
            else {
                self.parked.retain(|&at| at != index);
                continue;
            };
            if !stopping && !self.retake(node) {
                continue;
            }
            self.parked.retain(|&at| at != index);
            let Some(mut running) = self.take(index) else {
                continue;
            };
            if stopping {
                self.settle(py, &running, dropped(py), None, false);
                continue;
            }
            running.slots = true;
            let kwargs = running
                .kwargs
                .take()
                .map_or_else(|| PyDict::new(py), |kwargs| kwargs.into_bound(py));
            let work = work_name(py, Doing::Compute, false);
            let context = running.context.take().map(|context| context.into_bound(py));
            if let Some(handed) = self.hand_out(py, running, work, kwargs.into_any(), context) {
                return Some(handed);
            }
        }
        None
    }

    /// Begins a task the scheduler handed out: a probe, an iterate or a late hit is done
    /// here, on the loop, and `None` returned; anything else is handed to Python.
    #[expect(
        clippy::too_many_lines,
        reason = "one arm per kind of task, as `parallel::land`"
    )]
    fn begin<'py>(
        &mut self,
        py: Python<'py>,
        task: schedule::Task<Shared>,
    ) -> Option<Bound<'py, PyTuple>> {
        let schedule::Task {
            id,
            node,
            branch,
            key,
            work,
        } = task;
        let plan = self.plan.get();
        let graph = plan.graph().get();
        let target = graph.core().node(node);
        let worker = Grants::new(self.caps.as_ref(), &self.pooled).seat(Some(node)) == Seat::Take;
        let limited = self.limited(node);
        let mut running = Running {
            id,
            node,
            branch,
            key,
            doing: Doing::Compute,
            worker: false,
            slots: false,
            live: Vec::new(),
            kwargs: None,
            context: None,
            lent: None,
            order: None,
        };
        match work {
            Work::Iterate { iterable } => {
                let late = self.observers.late(py);
                let outcome = self.scope(py, late.as_ref()).and_then(|scope| {
                    scope
                        .iterate(&Direct, target, iterable.bind(py), &running.branch)
                        .and_then(|iterator| paired(py, iterator, &running.branch))
                });
                let (outcome, failure) = match outcome {
                    Ok(iterator) => (Outcome::Iterator(iterator), None),
                    Err(error) => (Outcome::Failed(error), Some(Failure::Task(Vec::new()))),
                };
                self.settle(py, &running, outcome, failure, false);
                None
            }
            Work::Probe { block } => {
                let late = self.observers.late(py);
                let core = graph.core();
                let mut wanted = Vec::new();
                let outcome = match self.scope(py, late.as_ref()) {
                    Ok(scope) => schedule::probe(plan.core(), core, block, &mut wanted, |step| {
                        let node = core.node(step);
                        let (hit, key) = scope.probe(&Direct, node, &running.branch)?;
                        let hit = hit.map(|value| {
                            let absent = node.may_be_absent && value.is(scope.absent);
                            (shared(value), absent)
                        });
                        Ok((hit, key))
                    }),
                    Err(error) => Outcome::LookupFailed(node, error),
                };
                let failure =
                    matches!(outcome, Outcome::LookupFailed(..)).then(|| Failure::Task(Vec::new()));
                self.settle(py, &running, outcome, failure, false);
                None
            }
            Work::Compute { mut args, late, .. } => {
                running.worker = worker;
                running.slots = limited;
                if late {
                    let store = self.observers.late(py);
                    let found = self.scope(py, store.as_ref()).and_then(|scope| {
                        Ok((
                            scope.lookup(&Direct, target, &running.branch)?,
                            scope.absent.clone(),
                        ))
                    });
                    match found {
                        Err(error) => {
                            self.failed(py, running, error);
                            return None;
                        }
                        Ok((Some(value), absent)) => {
                            let absent = target.may_be_absent && value.is(&absent);
                            self.give_back(py, &mut running);
                            let hit = Outcome::Hit {
                                value: shared(value),
                                absent,
                            };
                            self.settle(py, &running, hit, None, false);
                            return None;
                        }
                        Ok((None, _)) => {}
                    }
                }
                let kwargs = kwargs_of(py, graph, node, &mut args);
                if let Some(scheduler) = self.scheduler.as_mut() {
                    // Emptied, so the next compute reuses its buffer.
                    scheduler.recycle(args);
                }
                let kwargs = match kwargs {
                    Ok(kwargs) => kwargs,
                    Err(error) => {
                        self.failed(py, running, error);
                        return None;
                    }
                };
                let mut context = None;
                if self.observers.any() {
                    // Never held across a hook, which may start a run needing them.
                    self.give_back(py, &mut running);
                    // The hooks run in the context the node will run in, when it has one.
                    let own = !running.branch.is_empty()
                        || (self.caps.is_some() && (running.worker || limited));
                    if own {
                        match in_branch(py, &running.branch) {
                            Ok(made) => context = Some(made),
                            Err(error) => {
                                self.failed(py, running, error);
                                return None;
                            }
                        }
                    }
                    let run = match context.as_ref().map(context_run).transpose() {
                        Ok(run) => run,
                        Err(error) => {
                            self.failed(py, running, error);
                            return None;
                        }
                    };
                    let lent = Kwargs::lending(py, plan, graph, node, Some(kwargs.clone()));
                    let errors = self.observers.fire(py, &lent, run.as_ref(), |hook| {
                        hook.pre_node_in(&target.name, &running.branch, &[])
                    });
                    if !errors.is_empty() {
                        let failure = Failure::Run(RunError::Observers {
                            event: "pre_node",
                            errors,
                        });
                        self.settle(py, &running, dropped(py), Some(failure), true);
                        return None;
                    }
                    running.lent = Some(kwargs.clone().unbind());
                    running.order = lent.last_order().map(Bound::unbind);
                    if limited && !self.retake(node) {
                        let held = self.held.bind(py);
                        let reentered = self
                            .caps
                            .as_ref()
                            .and_then(|caps| caps.reentered(node, Some(held)));
                        if let Some(error) = reentered {
                            self.failed(py, running, error);
                            return None;
                        }
                        running.kwargs = Some(kwargs.unbind());
                        running.context = context.map(Bound::unbind);
                        let index = self.put(running);
                        self.parked.push_back(index);
                        return None;
                    }
                    running.slots = limited;
                }
                let work = work_name(py, Doing::Compute, false);
                self.hand_out(py, running, work, kwargs.into_any(), context)
            }
            Work::Pull { iterator } => {
                running.doing = Doing::Pull;
                running.worker = worker;
                running.slots = limited;
                let pair = iterator.bind(py);
                match unpaired(pair).and_then(|items| Ok((items, pair.get_item(1)?))) {
                    Ok((items, context)) => {
                        let work = work_name(py, Doing::Pull, false);
                        self.hand_out(py, running, work, items, Some(context))
                    }
                    Err(error) => {
                        self.failed(py, running, error);
                        None
                    }
                }
            }
            Work::Finish {
                iterator,
                exhausted,
            } => {
                running.doing = Doing::Finish;
                let pair = iterator.bind(py);
                match unpaired(pair).and_then(|items| Ok((items, pair.get_item(1)?))) {
                    Ok((items, context)) => {
                        let work = work_name(py, Doing::Finish, exhausted);
                        self.hand_out(py, running, work, items, Some(context))
                    }
                    Err(error) => {
                        self.failed(py, running, error);
                        None
                    }
                }
            }
        }
    }

    /// What a compute's outcome becomes, once its `after_node` hooks and stores heard of it.
    fn computed(
        &self,
        py: Python<'_>,
        running: &Running,
        result: PyResult<Bound<'_, PyAny>>,
    ) -> (Out, Option<Failure>, bool) {
        let plan = self.plan.get();
        let graph = plan.graph().get();
        let node = graph.core().node(running.node);
        let Ok(absent) = absent::singleton(py) else {
            let error = PyRuntimeError::new_err("fnloom.ABSENT is missing");
            return (
                Outcome::Failed(error),
                Some(Failure::Task(Vec::new())),
                false,
            );
        };
        let result = result.and_then(|value| {
            if !node.declines && value.is(absent) {
                return Err(undeclared_absence(&node.name));
            }
            Ok(value)
        });
        let mut errors = Vec::new();
        if !self.observers.quiet() {
            // In the context the node ran in, when it had one of its own.
            let run = running
                .context
                .as_ref()
                .and_then(|context| context_run(context.bind(py)).ok());
            // The arguments and order key its `pre_node` made, lent again.
            let lent = running.lent.as_ref().map(|lent| lent.bind(py).clone());
            let kwargs = Kwargs::lending(py, plan, graph, node.id, lent);
            if let Some(order) = &running.order {
                kwargs.reuse_order(&node.name, &running.branch, order.bind(py).clone());
            }
            errors = self.observers.fire(py, &kwargs, run.as_ref(), |hook| {
                hook.post_node_in(&node.name, &running.branch, result.as_ref())
            });
            errors.extend(self.observers.stored(
                py,
                run.as_ref(),
                plan.core(),
                graph,
                &node.name,
                &running.branch,
                result.as_ref(),
            ));
        }
        match result {
            Err(error) => (Outcome::Failed(error), Some(Failure::Task(errors)), false),
            Ok(_) if !errors.is_empty() => {
                let failure = Failure::Run(RunError::Observers {
                    event: "post_node",
                    errors,
                });
                (dropped(py), Some(failure), true)
            }
            Ok(value) => {
                let absent = node.declines && value.is(absent);
                (
                    Outcome::Value {
                        value: shared(value),
                        absent,
                    },
                    None,
                    false,
                )
            }
        }
    }

    /// What a pull's outcome becomes: its item, its iterator's end, or its failure.
    fn pulled(
        &self,
        py: Python<'_>,
        running: &Running,
        kind: &str,
        value: Bound<'_, PyAny>,
    ) -> (Out, Option<Failure>) {
        let failed = |error: PyErr| (Outcome::Failed(error), Some(Failure::Task(Vec::new())));
        match kind {
            "value" => (Outcome::Item(Item::Next(shared(value))), None),
            "done" => {
                let Ok(absent) = absent::singleton(py) else {
                    return failed(PyRuntimeError::new_err("fnloom.ABSENT is missing"));
                };
                if !value.is(absent) {
                    return (Outcome::Item(Item::Done), None);
                }
                let node = self.plan.get().graph().get().core().node(running.node);
                if !node.declines {
                    return failed(undeclared_absence(&node.name));
                }
                (Outcome::Item(Item::Declined(shared(value))), None)
            }
            _ => failed(PyErr::from_value(value)),
        }
    }
}

#[pymethods]
impl Stepper {
    /// The next task to run, as `(task, node, branch, work, payload, context)`; or, when
    /// none is ready, the run's status: `1` set when nothing is left, `2` when it was
    /// refused a slot, which a release may give it, and `4` when it is stopping.
    ///
    /// `work` is `"compute"`, its payload the node's keyword arguments; `"pull"`, the
    /// expand's item iterator; or `"finish"` or `"close"`, that iterator, ended exhausted
    /// or stopped. `context` is where the task runs, `None` for the caller's own.
    fn next<'py>(&mut self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        self.alive()?;
        #[cfg(feature = "test-hooks")]
        assert!(
            !STEPPER_ARMED.swap(false, Ordering::AcqRel),
            "deliberate panic stepping an async run"
        );
        let handle = self.plan.clone_ref(py);
        let plan = handle.get();
        let core = plan.core();
        let graph = plan.graph().get().core();
        loop {
            if !self.parked.is_empty() {
                if let Some(found) = self.unpark(py) {
                    return Ok(found.into_any());
                }
            }
            let grants = Grants::new(self.caps.as_ref(), &self.pooled);
            let Some(scheduler) = self.scheduler.as_mut() else {
                return Ok(DONE.into_pyobject(py)?.into_any());
            };
            let next = scheduler.next(core, graph, &grants, Seat::Threadless);
            let blocked = matches!(next, Next::Wait)
                .then(|| scheduler.blocked())
                .flatten();
            self.refused = grants.refused.load(Ordering::Relaxed) || !self.parked.is_empty();
            match next {
                Next::Done => return Ok(self.status(true).into_pyobject(py)?.into_any()),
                Next::Wait => {
                    let held = self.held.bind(py);
                    let reentered = blocked.and_then(|node| {
                        self.caps
                            .as_ref()
                            .and_then(|caps| caps.reentered(node, Some(held)))
                    });
                    let Some(error) = reentered else {
                        return Ok(self.status(false).into_pyobject(py)?.into_any());
                    };
                    // Waiting would be for good: the run fails.
                    self.fail_at(
                        OrderKey::default(),
                        RunError::Observers {
                            event: "the async run",
                            errors: vec![error],
                        },
                    );
                }
                Next::Task(task) => {
                    if let Some(found) = self.begin(py, task) {
                        return Ok(found.into_any());
                    }
                }
            }
        }
    }

    /// Takes back task `task`'s outcome: `kind` `"value"` with its value, `"error"` with
    /// its exception, `"done"` for a pull that found the iterator exhausted, with the
    /// value it returned, or `"dropped"` for a task whose outcome no longer counts, which
    /// stops the run. A task already completed is ignored. Whether the run is stopping.
    fn complete(
        &mut self,
        py: Python<'_>,
        task: usize,
        kind: &str,
        value: Bound<'_, PyAny>,
    ) -> PyResult<bool> {
        self.alive()?;
        let Some(mut running) = self.take(task) else {
            return Ok(self.stopping());
        };
        // As soon as the call returns, before any hook or store write.
        self.give_back(py, &mut running);
        if kind == "dropped" {
            if let Some(scheduler) = self.scheduler.as_mut() {
                scheduler.stop();
            }
            let outcome = match running.doing {
                Doing::Compute | Doing::Finish => dropped(py),
                Doing::Pull => Outcome::Item(Item::Done),
            };
            self.settle(py, &running, outcome, None, false);
            return Ok(true);
        }
        let (outcome, failure, stop) = match running.doing {
            Doing::Compute => {
                let result = if kind == "error" {
                    Err(PyErr::from_value(value))
                } else {
                    Ok(value)
                };
                self.computed(py, &running, result)
            }
            Doing::Pull => {
                let (outcome, failure) = self.pulled(py, &running, kind, value);
                (outcome, failure, false)
            }
            Doing::Finish if kind == "error" => (
                Outcome::Failed(PyErr::from_value(value)),
                Some(Failure::Task(Vec::new())),
                false,
            ),
            Doing::Finish => (Outcome::Finished(shared(value)), None, false),
        };
        self.settle(py, &running, outcome, failure, stop);
        Ok(self.stopping())
    }

    /// Completes task `task` as dropped, a sync node's call its stopping run abandons while
    /// its thread still runs, but keeps the task's slots: the `Held` it returns gives them
    /// back once the call returns, so a limit holds while it runs. `None` when it holds no
    /// slot, or the task was already completed.
    fn detach(&mut self, py: Python<'_>, task: usize) -> PyResult<Option<Held>> {
        self.alive()?;
        let Some(mut running) = self.take(task) else {
            return Ok(None);
        };
        // Its `HELD` entries stay live as long as `held` keeps their slots: a run the call
        // starts still sees it holds them.
        let live = std::mem::take(&mut running.live);
        let held = self
            .caps
            .as_ref()
            .filter(|_| running.slots || running.worker)
            .map(|caps| Held {
                caps: Arc::clone(caps),
                pool: self.pool.clone(),
                node: running.node,
                slots: running.slots,
                worker: running.worker,
                live,
                released: AtomicBool::new(false),
                pid: self.pid,
            });
        // The scheduler takes the task back; its slots stay with `held`.
        (running.slots, running.worker) = (false, false);
        if let Some(scheduler) = self.scheduler.as_mut() {
            scheduler.stop();
        }
        self.settle(py, &running, dropped(py), None, false);
        Ok(held)
    }

    /// Whether the run's sync nodes run on the driver's pool, holding worker slots: not for
    /// a run started inside a task holding one, whose sync nodes go to its own loop's
    /// executor instead, so it never waits for, nor grows, the pool its caller is on.
    #[getter]
    fn pooling(&self) -> bool {
        self.pool.is_some() && self.pooled.contains(&true)
    }

    /// Stops the run: no compute, pull or probe is handed out any more, and each open
    /// iterator is finished as stopped.
    fn stop(&mut self) {
        if let Some(scheduler) = self.scheduler.as_mut() {
            scheduler.stop();
        }
    }

    /// The requested outputs, keyed by name, once the run is done; or its failure, the one
    /// with the lowest order key, raised as `Plan.run` raises it.
    fn outputs<'py>(&mut self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        self.alive()?;
        let Some(scheduler) = self.scheduler.take() else {
            return Err(PyRuntimeError::new_err(
                "this run's outputs were already taken",
            ));
        };
        let failures = std::mem::take(&mut self.failures);
        let plan = self.plan.get();
        let graph = plan.graph().get();
        let outputs = outcome_of(scheduler.finish(plan.core(), graph.core()), failures)
            .map_err(|error| run_error(py, error))?;
        let result = PyDict::new(py);
        for (id, value) in outputs {
            let name = &graph.core().node(id).name;
            result.set_item(keyword(py, graph.keywords(), id, name), value.bind(py))?;
        }
        Ok(result)
    }
}

/// `Plan.start`: a run of `slf` for the async driver to step.
#[expect(
    clippy::too_many_arguments,
    reason = "`Plan.start`'s arguments, passed on"
)]
pub fn start(
    slf: &Bound<'_, Plan>,
    inputs: &Bound<'_, PyDict>,
    overrides: &Bound<'_, PyDict>,
    observers: Vec<Bound<'_, ObserverBase>>,
    on_value: Option<Bound<'_, PyAny>>,
    late: Option<&Bound<'_, PyAny>>,
    limits: Option<&Bound<'_, Limits>>,
    threads: Option<&Bound<'_, Threads>>,
    pooled: Vec<String>,
    in_flight: usize,
) -> PyResult<Stepper> {
    let py = slf.py();
    let plan = slf.get();
    let graph = plan.graph().get();
    let core = plan.core();
    let caps = limits.map(|limits| Arc::clone(limits.get().caps()));
    let pid = std::process::id();
    if caps.as_ref().is_some_and(|caps| caps.pid() != pid) {
        return Err(PyRuntimeError::new_err(
            "these limits were made in another process; limits don't survive os.fork()",
        ));
    }
    let absent = absent::singleton(py)?.bind(py);
    let seeded = seed(py, core, graph, inputs, overrides, absent)?;
    let held = held_variable(py)?
        .bind(py)
        .call_method0(intern!(py, "get"))?;
    // A run started inside a task holding one of these worker slots pools nothing: its sync
    // nodes go to its own loop's executor, so it never waits for the slot its caller holds.
    let nested = caps.as_ref().is_some_and(|caps| caps.works(&held));
    let mut flags = vec![false; graph.core().nodes().len()];
    if caps.is_some() && !nested {
        for name in pooled {
            if let Some(flag) = graph
                .core()
                .id_of(&name)
                .and_then(|id| flags.get_mut(id.index()))
            {
                *flag = true;
            }
        }
    }
    let mut report = Report::default();
    let options = Options {
        in_flight: in_flight.max(1),
    };
    let scheduler = Scheduler::new(
        core,
        graph.core(),
        plan.layout(),
        seeded,
        options,
        &mut report,
    );
    let mut stepper = Stepper {
        plan: slf.clone().unbind(),
        scheduler: Some(scheduler),
        failures: Vec::new(),
        running: Vec::new(),
        free: Vec::new(),
        parked: VecDeque::new(),
        report: Report::default(),
        refused: false,
        caps,
        pool: threads.map(|threads| Arc::clone(threads.get().pool())),
        pooled: flags,
        held: held.unbind(),
        observers: SharedObservers::new(observers, on_value, late),
        pid,
    };
    stepper.fire(py, &report, None, &[]);
    report.clear();
    stepper.report = report;
    Ok(stepper)
}

/// Gives back a task's node and group slots and its worker slot, as it holds them, then
/// wakes whoever waits for one, when attached.
fn release_all(caps: &Caps, pool: Option<&Arc<Pool>>, node: NodeId, slots: bool, worker: bool) {
    if slots {
        caps.core.release(Some(node), Seat::Threadless);
    }
    if worker {
        caps.core.release(None, Seat::Take);
    }
    if slots || worker {
        let _ = Python::try_attach(|py| {
            caps.notify(py);
            if let Some(pool) = pool {
                pool.notify(py);
            }
        });
    }
}

impl Drop for Stepper {
    /// Gives back the slots of tasks never completed, a run dropped mid-way, unless this
    /// is a forked child, which touches nothing it inherited.
    fn drop(&mut self) {
        if std::process::id() != self.pid {
            return;
        }
        let Some(caps) = &self.caps else {
            return;
        };
        for running in self.running.iter().flatten() {
            release_all(
                caps,
                self.pool.as_ref(),
                running.node,
                running.slots,
                running.worker,
            );
        }
    }
}

/// The slots of a sync node's call that its run abandoned while the call still runs in
/// a thread (`Stepper.detach`): given back once, by `release` as the call returns, or
/// when collected.
#[pyclass(frozen, module = "fnloom._core")]
pub struct Held {
    caps: Arc<Caps>,
    pool: Option<Arc<Pool>>,
    node: NodeId,
    slots: bool,
    worker: bool,
    /// The live flags of the call's `HELD` entries, cleared as the slots go back.
    live: Vec<Py<PyList>>,
    released: AtomicBool,
    pid: u32,
}

impl Held {
    fn give_back(&self) {
        // In a forked child nothing inherited is touched.
        if std::process::id() != self.pid || self.released.swap(true, Ordering::AcqRel) {
            return;
        }
        let _ = Python::try_attach(|py| {
            for flag in &self.live {
                drop(flag.bind(py).set_item(0, false));
            }
        });
        release_all(
            &self.caps,
            self.pool.as_ref(),
            self.node,
            self.slots,
            self.worker,
        );
    }
}

#[pymethods]
impl Held {
    /// Gives the slots back, waking whoever waits for one; only the first call does.
    fn release(&self) {
        self.give_back();
    }
}

impl Drop for Held {
    fn drop(&mut self) {
        self.give_back();
    }
}

/// Armed by `_core.arm_stepper_panic()`: the next `Stepper.next` panics.
#[cfg(feature = "test-hooks")]
static STEPPER_ARMED: AtomicBool = AtomicBool::new(false);

/// Makes the next `Stepper.next` of any run panic, in Rust, before it hands out a task.
#[cfg(feature = "test-hooks")]
#[pyfunction]
pub fn arm_stepper_panic() {
    STEPPER_ARMED.store(true, Ordering::Release);
}
