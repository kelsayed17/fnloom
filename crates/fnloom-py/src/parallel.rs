//! One parallel run: a plan on the core scheduler, its tasks run by the calling thread
//! and the driver's pool threads.
//!
//! The rules (Plan 11, "Free-threading safety rules"):
//!
//! - No thread waits for a Rust lock while attached: it tries the lock attached, else does
//!   the whole critical section inside `detach`, returning only `Send` data.
//! - No Rust lock is held while calling into Python, and no `Py` is dropped while one is:
//!   the scheduler hands released values back in its `Report`, dropped attached after.
//! - The run's busy flag is the one thing held across Python: its hooks, one at a time,
//!   each on the thread that ran the node. `on_value` and `late.store` run after it.
//! - Each task's calls run in their own copy of the context the run started in.
//! - On a failure no new task starts; the run waits for the running ones, then raises the
//!   failure with the lowest order key, as `Plan::run` would have raised it.

use std::cell::RefCell;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Condvar, Mutex, PoisonError, TryLockError};
use std::time::Duration;

use fnloom_core::limits::Limits as CoreLimits;
use fnloom_core::node::{Node, NodeId};
use fnloom_core::plan::{Action, Plan as CorePlan};
use fnloom_core::run::{Arg, Item, Observer, RunError, Slot};
use fnloom_core::schedule::{
    self, ArgValue, Next, Options, OrderKey, Outcome, Report, Scheduler, Seat, Task, Work,
};
use pyo3::exceptions::PyRuntimeError;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyTuple};

use crate::absent::{self, Absent};
use crate::errors::{caught, run_error};
use crate::graph::Graph;
use crate::observer::ObserverBase;
use crate::plan::{Kwargs, Plan, Value, branch_variable, keyword};
use crate::pool::{Pool, Threads};
use crate::shared::SharedObservers;
use crate::task::{Calls, Scope};

/// A value of a parallel run: a strong reference any thread may hold, and clone without
/// attaching. Only ever dropped attached.
type Shared = Arc<Py<PyAny>>;
type Sched = Scheduler<Shared, PyErr, [u8; 32]>;
type Out = Outcome<Shared, PyErr, [u8; 32]>;

/// How long a calling thread waits before it checks for signals again.
const SLICE: Duration = Duration::from_millis(50);

/// What one parallel run shares between its threads.
pub struct Run {
    plan: Py<Plan>,
    state: Mutex<State>,
    /// Tasks handed out whose events are not all sent yet: the run ends only at zero.
    active: AtomicUsize,
    /// The busy flag: set while a thread fires this run's hooks.
    busy: Mutex<bool>,
    clear: Condvar,
    /// The context the run started in, copied for each task.
    snapshot: Py<PyAny>,
    observers: SharedObservers,
    /// The process the run started in.
    pid: u32,
}

/// What the scheduler's lock guards.
struct State {
    scheduler: Option<Sched>,
    /// Every failure the run saw, with its order key.
    failures: Vec<(OrderKey, Failure)>,
}

/// A failure the run saw.
enum Failure {
    /// A task failed; the scheduler holds its error. The observer errors its `post_node`
    /// raised go with it.
    Task(Vec<PyErr>),
    /// The run's own failure: its hooks, a signal, or a panic outside a node call.
    Run(RunError<PyErr>),
}

/// What one attempt to take a task did.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Step {
    /// A task ran.
    Worked,
    /// Nothing to take now.
    Wait,
    /// Nothing is left to hand out.
    Done,
    /// The scheduler's lock is poisoned: the run is broken.
    Broken,
}

thread_local! {
    /// The caps this thread holds a worker slot of, innermost last: a run started inside a
    /// task takes its caller's tasks on the slot already held.
    static HELD: RefCell<Vec<usize>> = const { RefCell::new(Vec::new()) };
}

/// Marks a worker slot of `limits` held by this thread while it lives.
struct Holding;

impl Holding {
    fn enter(limits: &Arc<CoreLimits>) -> Self {
        HELD.with(|held| held.borrow_mut().push(Arc::as_ptr(limits) as usize));
        Self
    }
}

impl Drop for Holding {
    fn drop(&mut self) {
        HELD.with(|held| held.borrow_mut().pop());
    }
}

/// Whether this thread holds a worker slot of `limits`.
fn holds(limits: &Arc<CoreLimits>) -> bool {
    let id = Arc::as_ptr(limits) as usize;
    HELD.with(|held| held.borrow().contains(&id))
}

/// The calls of one task, in its own copy of the run's context, with
/// `fnloom.current_branch()` set to its branch.
struct Copied<'py> {
    /// The copy's `run` method.
    run: Bound<'py, PyAny>,
}

impl<'py> Copied<'py> {
    fn new(py: Python<'py>, snapshot: &Py<PyAny>, branch: &[usize]) -> PyResult<Self> {
        let copy = snapshot.bind(py).call_method0(intern!(py, "copy"))?;
        let run = copy.getattr(intern!(py, "run"))?;
        if !branch.is_empty() {
            let set = branch_variable(py)?.bind(py).getattr(intern!(py, "set"))?;
            run.call1((set, PyTuple::new(py, branch)?))?;
        }
        Ok(Self { run })
    }
}

impl<'py> Calls<'py> for Copied<'py> {
    fn call(
        &self,
        function: &Bound<'py, PyAny>,
        args: Vec<Bound<'py, PyAny>>,
        kwargs: Option<&Bound<'py, PyDict>>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let mut all = Vec::with_capacity(args.len() + 1);
        all.push(function.clone());
        all.extend(args);
        self.run.call(PyTuple::new(function.py(), all)?, kwargs)
    }
}

/// What a task gave, and what the run records of it.
struct Landed {
    outcome: Out,
    /// The failure to record with the task's key.
    failure: Option<Failure>,
    /// Whether the run stops before the outcome is taken in.
    stop: bool,
}

impl Landed {
    const fn done(outcome: Out) -> Self {
        Self {
            outcome,
            failure: None,
            stop: false,
        }
    }

    const fn failed(error: PyErr) -> Self {
        Self {
            outcome: Outcome::Failed(error),
            failure: Some(Failure::Task(Vec::new())),
            stop: false,
        }
    }

    /// A failure of the run's hooks: the run stops, and the outcome is dropped.
    fn hooks(py: Python<'_>, event: &'static str, errors: Vec<PyErr>) -> Self {
        Self {
            outcome: Outcome::Value {
                value: Arc::new(py.None()),
                absent: false,
            },
            failure: Some(Failure::Run(RunError::Observers { event, errors })),
            stop: true,
        }
    }
}

fn shared(value: Bound<'_, PyAny>) -> Shared {
    Arc::new(value.unbind())
}

impl Run {
    /// Runs `with` on the scheduler's state by rule 1; `None` when its lock is poisoned,
    /// and then `with` is dropped attached, here.
    fn with_state<R: Send, F: FnOnce(&mut State) -> R + Send>(
        &self,
        py: Python<'_>,
        with: F,
    ) -> Option<R> {
        match self.state.try_lock() {
            Ok(mut state) => return Some(with(&mut state)),
            Err(TryLockError::Poisoned(_)) => return None,
            Err(TryLockError::WouldBlock) => {}
        }
        let locked = py.detach(|| match self.state.lock() {
            Ok(mut state) => Ok(with(&mut state)),
            Err(_) => Err(with),
        });
        locked.ok()
    }

    /// Records `failure` at `key` and stops the run.
    fn fail_at(&self, py: Python<'_>, key: OrderKey, failure: RunError<PyErr>) {
        self.with_state(py, move |state| {
            state.failures.push((key, Failure::Run(failure)));
            if let Some(scheduler) = state.scheduler.as_mut() {
                scheduler.stop();
            }
        });
    }

    /// Fails the run with `error`, ahead of any node's failure: an interrupt, or a panic
    /// in the worker loop outside a node call.
    pub fn fail_now(&self, py: Python<'_>, error: PyErr) {
        self.fail_at(
            py,
            OrderKey::default(),
            RunError::Observers {
                event: "the worker loop",
                errors: vec![error],
            },
        );
    }

    /// Sets the busy flag, waiting detached while another thread holds it.
    fn acquire(&self, py: Python<'_>) {
        if let Ok(mut busy) = self.busy.try_lock() {
            if !*busy {
                *busy = true;
                return;
            }
        }
        py.detach(|| {
            let mut busy = self.busy.lock().unwrap_or_else(PoisonError::into_inner);
            while *busy {
                busy = self
                    .clear
                    .wait(busy)
                    .unwrap_or_else(PoisonError::into_inner);
            }
            *busy = true;
        });
    }

    /// Clears the busy flag and wakes one waiter.
    fn release(&self, py: Python<'_>) {
        match self.busy.try_lock() {
            Ok(mut busy) => *busy = false,
            Err(TryLockError::Poisoned(poisoned)) => *poisoned.into_inner() = false,
            Err(TryLockError::WouldBlock) => py.detach(|| {
                *self.busy.lock().unwrap_or_else(PoisonError::into_inner) = false;
            }),
        }
        self.clear.notify_one();
    }

    /// Fires one event to the observers under the busy flag; the errors raised.
    fn hooked<'py>(
        &self,
        py: Python<'py>,
        kwargs: &Kwargs<'py, '_>,
        event: impl FnMut(&mut dyn Observer<Value<'py>, PyErr>) -> PyResult<()>,
    ) -> Vec<PyErr> {
        if !self.observers.any() {
            return Vec::new();
        }
        self.acquire(py);
        let errors = self.observers.fire(py, kwargs, event);
        self.release(py);
        errors
    }

    /// Fires what a report settled, hits, then expands, then skips, as `Report` says, under
    /// one hold of the busy flag. The first event an observer raised in stops the run.
    fn report(&self, py: Python<'_>, report: &Report<Shared>) {
        let nothing =
            report.hits.is_empty() && report.expanded.is_empty() && report.skipped.is_empty();
        if nothing || !self.observers.any() {
            return;
        }
        let plan = self.plan.get();
        let graph = plan.graph().get();
        let core = graph.core();
        let kwargs = Kwargs::new(py, plan.core(), graph);
        self.acquire(py);
        let mut failed = None;
        for (node, branch, value) in &report.hits {
            let name = &core.node(*node).name;
            let errors = self.observers.fire(py, &kwargs, |hook| {
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
                let errors = self
                    .observers
                    .fire(py, &kwargs, |hook| hook.post_expand(name, branch, *items));
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
                    .fire(py, &kwargs, |hook| hook.skipped(name, branch));
                if !errors.is_empty() {
                    failed = Some((key.clone(), "skipped", errors));
                    break;
                }
            }
        }
        self.release(py);
        if let Some((key, event, errors)) = failed {
            self.fail_at(py, key, RunError::Observers { event, errors });
        }
    }
}

/// Takes one task of `run` for `seat`, if one is ready, runs it, completes it and sends
/// its events.
pub fn step(
    py: Python<'_>,
    run: &Arc<Run>,
    pool: &Pool,
    seat: Seat,
    report: &mut Report<Shared>,
) -> Step {
    #[cfg(feature = "test-hooks")]
    if armed_panic(py, run, pool) {
        return Step::Worked;
    }
    let plan = run.plan.get();
    let core = plan.core();
    let graph = plan.graph().get().core();
    let limits: &CoreLimits = &pool.limits;
    let next = run.with_state(py, |state| {
        let Some(scheduler) = state.scheduler.as_mut() else {
            return Next::Done;
        };
        let next = scheduler.next(core, graph, limits, seat);
        if matches!(next, Next::Task(_)) {
            run.active.fetch_add(1, Ordering::AcqRel);
        }
        next
    });
    match next {
        None => Step::Broken,
        Some(Next::Wait) => Step::Wait,
        Some(Next::Done) => Step::Done,
        Some(Next::Task(task)) => {
            perform(py, run, pool, seat, task, report);
            run.active.fetch_sub(1, Ordering::AcqRel);
            pool.notify(py);
            Step::Worked
        }
    }
}

/// Runs `task`, completes it, and sends what its completion settled to the hooks.
fn perform(
    py: Python<'_>,
    run: &Run,
    pool: &Pool,
    seat: Seat,
    task: Task<Shared>,
    report: &mut Report<Shared>,
) {
    let plan = run.plan.get();
    let core = plan.core();
    let graph = plan.graph().get();
    let held = (seat == Seat::Take).then(|| Holding::enter(&pool.limits));
    let Task {
        id,
        node,
        branch,
        key,
        work,
    } = task;
    let landed =
        caught(|| Ok(land(py, run, graph, node, &branch, work))).unwrap_or_else(Landed::failed);
    drop(held);
    let Landed {
        outcome,
        failure,
        stop,
    } = landed;
    let failure = failure.map(|failure| {
        let at = match &outcome {
            // A probe's failure is its node's, keyed by that node's step.
            Outcome::LookupFailed(failed, _) => core.order_key(*failed, &branch),
            _ => key,
        };
        (at, failure)
    });
    let limits: &CoreLimits = &pool.limits;
    let report_in = &mut *report;
    run.with_state(py, move |state| {
        state.failures.extend(failure);
        if let Some(scheduler) = state.scheduler.as_mut() {
            if stop {
                scheduler.stop();
            }
            scheduler.complete(core, graph.core(), limits, id, outcome, report_in);
        }
    });
    run.report(py, report);
    // The values no step reads any more are dropped here, attached.
    report.clear();
}

/// Runs a task's work: its node's call, or a pull, a finish, an iterate or a probe.
fn land(
    py: Python<'_>,
    run: &Run,
    graph: &Graph,
    id: NodeId,
    branch: &[usize],
    work: Work<Shared>,
) -> Landed {
    let plan = run.plan.get();
    let node = graph.core().node(id);
    let late = run.observers.late(py);
    let Ok(absent) = absent::singleton(py) else {
        return Landed::failed(PyRuntimeError::new_err("fnloom.ABSENT is missing"));
    };
    let scope = Scope {
        graph,
        outputs: plan.core().outputs(),
        late: late.as_ref(),
        absent: absent.bind(py),
    };
    let calls = match Copied::new(py, &run.snapshot, branch) {
        Ok(calls) => calls,
        Err(error) => return Landed::failed(error),
    };
    match work {
        Work::Compute {
            args,
            expands,
            late,
        } => compute(py, run, &scope, &calls, node, branch, args, expands, late),
        Work::Iterate { iterable } => {
            match scope.iterate(&calls, node, iterable.bind(py), branch) {
                Ok(iterator) => Landed::done(Outcome::Iterator(shared(iterator))),
                Err(error) => Landed::failed(error),
            }
        }
        Work::Pull { iterator } => match scope.next_item(&calls, node, iterator.bind(py)) {
            Ok(item) => Landed::done(Outcome::Item(match item {
                Item::Next(value) => Item::Next(shared(value)),
                Item::Declined(value) => Item::Declined(shared(value)),
                Item::Done => Item::Done,
            })),
            Err(error) => Landed::failed(error),
        },
        Work::Finish {
            iterator,
            exhausted,
        } => match scope.finish_items(&calls, iterator.bind(py), exhausted) {
            Ok(value) => Landed::done(Outcome::Finished(shared(value))),
            // What closing a stopped run's iterator raises is dropped, as `Plan::run` does.
            Err(error) if !exhausted => Landed::done(Outcome::Failed(error)),
            Err(error) => Landed::failed(error),
        },
        Work::Probe { block } => {
            let core = graph.core();
            let mut wanted = Vec::new();
            let outcome = schedule::probe(plan.core(), core, block, &mut wanted, |step| {
                let node = core.node(step);
                scope.lookup(&calls, node, branch).map(|hit| {
                    let hit = hit.map(|value| {
                        let absent = node.may_be_absent && value.is(scope.absent);
                        (shared(value), absent)
                    });
                    (hit, None)
                })
            });
            match outcome {
                Outcome::LookupFailed(..) => Landed {
                    outcome,
                    failure: Some(Failure::Task(Vec::new())),
                    stop: false,
                },
                outcome => Landed::done(outcome),
            }
        }
    }
}

/// Runs a compute: a late step's lookup, its hooks, its node's call in the task's context,
/// `on_value` and `late.store`, and an expand's iterator.
#[expect(
    clippy::too_many_arguments,
    reason = "the task's parts, as the scheduler gives them"
)]
fn compute<'py>(
    py: Python<'py>,
    run: &Run,
    scope: &Scope<'_, 'py>,
    calls: &Copied<'py>,
    node: &Node<Py<PyAny>>,
    branch: &[usize],
    args: Vec<(NodeId, ArgValue<Shared>)>,
    expands: bool,
    late: bool,
) -> Landed {
    let plan = run.plan.get();
    let graph = scope.graph;
    if late {
        match scope.lookup(calls, node, branch) {
            Err(error) => return Landed::failed(error),
            Ok(Some(value)) => {
                let absent = node.may_be_absent && value.is(scope.absent);
                return Landed::done(Outcome::Hit {
                    value: shared(value),
                    absent,
                });
            }
            Ok(None) => {}
        }
    }
    let mut built: Vec<Arg<'_, Value<'py>>> = Vec::with_capacity(args.len());
    for (input, arg) in args {
        let value = match arg {
            ArgValue::One(value) => value.bind(py).clone(),
            ArgValue::Gather(values) => {
                match PyList::new(py, values.iter().map(|value| value.bind(py))) {
                    Ok(list) => list.into_any(),
                    Err(error) => return Landed::failed(error),
                }
            }
            ArgValue::Omitted => continue,
        };
        built.push(Arg {
            node: input,
            name: graph.core().node(input).name.as_str(),
            value,
        });
    }
    let Some(payload) = node.payload() else {
        return Landed::failed(PyRuntimeError::new_err(format!(
            "broken plan: input node '{}' planned to compute",
            node.name
        )));
    };
    let blank = Kwargs::new(py, plan.core(), graph);
    let kwargs = match blank.build(node.id, &built) {
        Ok(kwargs) => kwargs,
        Err(error) => return Landed::failed(error),
    };
    let lent = Kwargs::lending(py, plan.core(), graph, node.id, Some(kwargs.clone()));
    let errors = run.hooked(py, &lent, |hook| {
        hook.pre_node_in(&node.name, branch, &built)
    });
    if !errors.is_empty() {
        return Landed::hooks(py, "pre_node", errors);
    }
    let result = caught(|| calls.call(payload.bind(py), Vec::new(), Some(&kwargs)))
        .and_then(|value| scope.checked(node, value));
    let mut errors = run.hooked(py, &lent, |hook| {
        hook.post_node_in(&node.name, branch, result.as_ref())
    });
    errors.extend(run.observers.stored(
        py,
        plan.core(),
        graph,
        &node.name,
        branch,
        result.as_ref(),
    ));
    match result {
        Err(error) => Landed {
            outcome: Outcome::Failed(error),
            failure: Some(Failure::Task(errors)),
            stop: false,
        },
        Ok(_) if !errors.is_empty() => Landed::hooks(py, "post_node", errors),
        Ok(value) => {
            let absent = node.declines && value.is(scope.absent);
            if expands && !absent {
                match scope.iterate(calls, node, &value, branch) {
                    Ok(iterator) => Landed::done(Outcome::Iterator(shared(iterator))),
                    Err(error) => Landed::failed(error),
                }
            } else {
                Landed::done(Outcome::Value {
                    value: shared(value),
                    absent,
                })
            }
        }
    }
}

/// The value of each input and override the plan reads, by node id; `MissingValues`
/// before any hook when one is missing.
fn seed(
    py: Python<'_>,
    core: &CorePlan,
    graph: &Graph,
    inputs: &Bound<'_, PyDict>,
    overrides: &Bound<'_, PyDict>,
    absent: &Bound<'_, Absent>,
) -> PyResult<Vec<Option<Slot<Shared>>>> {
    let nodes = graph.core().nodes();
    let mut seeded: Vec<Option<Slot<Shared>>> = (0..nodes.len()).map(|_| None).collect();
    let mut missing = Vec::new();
    for step in core.steps() {
        let values = match step.action {
            Action::UseInput => inputs,
            Action::UseOverride => overrides,
            _ => continue,
        };
        let node = graph.core().node(step.node);
        let name = keyword(py, graph.keywords(), node.id, &node.name);
        match (values.get_item(name)?, seeded.get_mut(step.node.index())) {
            (Some(value), Some(slot)) => {
                let absent =
                    step.action == Action::UseOverride && node.may_be_absent && value.is(absent);
                *slot = Some(Slot {
                    value: shared(value),
                    absent,
                });
            }
            _ => missing.push(node.name.clone()),
        }
    }
    if missing.is_empty() {
        Ok(seeded)
    } else {
        missing.sort();
        Err(run_error(py, RunError::MissingValues(missing)))
    }
}

/// `error` with the errors of the closing `post_graph(false)` added, as `Plan::run` adds
/// them.
fn closed(mut error: RunError<PyErr>, closing: Vec<PyErr>) -> RunError<PyErr> {
    match &mut error {
        RunError::Node {
            observer_errors, ..
        }
        | RunError::BrokenPlan {
            observer_errors, ..
        } => observer_errors.extend(closing),
        RunError::Observers { errors, .. } => errors.extend(closing),
        RunError::MissingValues(_) => {}
    }
    error
}

/// The run's failure: the one with the lowest order key, a task's as the scheduler made
/// it, with any observer errors its `post_node` raised; or its outputs.
#[expect(
    clippy::result_large_err,
    reason = "`RunError` is the core's, and is made once per run"
)]
fn outcome_of(
    finished: Result<Vec<(NodeId, Shared)>, RunError<PyErr>>,
    mut failures: Vec<(OrderKey, Failure)>,
) -> Result<Vec<(NodeId, Shared)>, RunError<PyErr>> {
    // Stable, so of two failures at one key the first seen leads.
    failures.sort_by(|a, b| a.0.cmp(&b.0));
    match failures.into_iter().next().map(|(_, failure)| failure) {
        // A stopped run's scheduler may lack outputs: the run's own failure is the one.
        Some(Failure::Run(failure)) => Err(failure),
        Some(Failure::Task(extra)) => match finished {
            Err(RunError::Node {
                node,
                source,
                mut observer_errors,
                branch,
                block,
            }) => {
                observer_errors.extend(extra);
                Err(RunError::Node {
                    node,
                    source,
                    observer_errors,
                    branch,
                    block,
                })
            }
            other => other,
        },
        None => finished,
    }
}

/// The `RuntimeError` of a run whose scheduler's lock a panic poisoned.
fn broken() -> PyErr {
    PyRuntimeError::new_err("fnloom's scheduler panicked: a run's scheduler lock is poisoned")
}

/// `Plan.run(..., threads=)`: the run on the scheduler, its tasks taken by the calling
/// thread and `threads`' pool.
pub fn run<'py>(
    slf: &Bound<'py, Plan>,
    inputs: &Bound<'py, PyDict>,
    overrides: &Bound<'py, PyDict>,
    observers: Vec<Bound<'py, ObserverBase>>,
    on_value: Option<Bound<'py, PyAny>>,
    late: Option<&Bound<'py, PyAny>>,
    threads: &Bound<'py, Threads>,
) -> PyResult<Bound<'py, PyDict>> {
    let py = slf.py();
    let pool = threads.get().pool();
    let pid = std::process::id();
    if pool.pid != pid {
        return Err(PyRuntimeError::new_err(format!(
            "this pool was made in process {}; a pool doesn't survive os.fork()",
            pool.pid
        )));
    }
    let plan = slf.get();
    let graph = plan.graph().get();
    let core = plan.core();
    let absent = absent::singleton(py)?.bind(py);
    let seeded = seed(py, core, graph, inputs, overrides, absent)?;
    let snapshot = py
        .import(intern!(py, "contextvars"))?
        .call_method0(intern!(py, "copy_context"))?
        .unbind();
    let run = Arc::new(Run {
        plan: slf.clone().unbind(),
        state: Mutex::new(State {
            scheduler: None,
            failures: Vec::new(),
        }),
        active: AtomicUsize::new(0),
        busy: Mutex::new(false),
        clear: Condvar::new(),
        snapshot,
        observers: SharedObservers::new(observers, on_value, late),
        pid,
    });
    let kwargs = Kwargs::new(py, core, graph);
    let errors = run.observers.fire(py, &kwargs, |hook| hook.pre_graph());
    if !errors.is_empty() {
        let closing = run
            .observers
            .fire(py, &kwargs, |hook| hook.post_graph(false));
        let failure = RunError::Observers {
            event: "pre_graph",
            errors,
        };
        return Err(run_error(py, closed(failure, closing)));
    }
    let mut report = Report::default();
    let options = Options {
        in_flight: pool.max_workers.saturating_mul(2),
    };
    let scheduler = Scheduler::new(
        core,
        graph.core(),
        plan.layout(),
        seeded,
        options,
        &mut report,
    );
    run.with_state(py, move |state| state.scheduler = Some(scheduler));
    run.report(py, &report);
    report.clear();
    pool.register(py, &run);
    let seat = if holds(&pool.limits) {
        Seat::Held
    } else {
        Seat::Take
    };
    let drove = drive(py, &run, pool, seat, &mut report);
    if std::process::id() != run.pid {
        // A node forked: this is the child, whose run's other threads are gone. Nothing
        // it inherited is locked again.
        return Err(PyRuntimeError::new_err(format!(
            "this run was started in process {}; a run doesn't survive os.fork()",
            run.pid
        )));
    }
    pool.unregister(py, &run);
    ended(slf, &run, drove == Step::Broken)
}

/// The end of a run whose tasks are all done: its outputs, or its failure, once
/// `post_graph` has fired.
fn ended<'py>(slf: &Bound<'py, Plan>, run: &Run, broke: bool) -> PyResult<Bound<'py, PyDict>> {
    let py = slf.py();
    let plan = slf.get();
    let graph = plan.graph().get();
    let core = plan.core();
    let kwargs = Kwargs::new(py, core, graph);
    let taken = if broke {
        None
    } else {
        run.with_state(py, |state| {
            (state.scheduler.take(), std::mem::take(&mut state.failures))
        })
    };
    let Some((Some(scheduler), failures)) = taken else {
        let closing = run
            .observers
            .fire(py, &kwargs, |hook| hook.post_graph(false));
        drop(closing);
        return Err(broken());
    };
    let outcome = outcome_of(scheduler.finish(core, graph.core()), failures);
    match outcome {
        Ok(outputs) => {
            let errors = run
                .observers
                .fire(py, &kwargs, |hook| hook.post_graph(true));
            if !errors.is_empty() {
                let failure = RunError::Observers {
                    event: "post_graph",
                    errors,
                };
                return Err(run_error(py, failure));
            }
            let result = PyDict::new(py);
            for (id, value) in outputs {
                let name = &graph.core().node(id).name;
                result.set_item(keyword(py, graph.keywords(), id, name), value.bind(py))?;
            }
            Ok(result)
        }
        Err(failure) => {
            let closing = run
                .observers
                .fire(py, &kwargs, |hook| hook.post_graph(false));
            Err(run_error(py, closed(failure, closing)))
        }
    }
}

/// The calling thread's part of a run: it takes tasks too, and waits in slices, checking
/// for signals between them, until nothing is left and every task's events are sent.
fn drive(
    py: Python<'_>,
    run: &Arc<Run>,
    pool: &Pool,
    seat: Seat,
    report: &mut Report<Shared>,
) -> Step {
    loop {
        let generation = pool.generation(py);
        let step = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            step(py, run, pool, seat, report)
        }))
        .unwrap_or_else(|payload| {
            run.fail_now(py, crate::errors::panic_error(&*payload));
            Step::Worked
        });
        if std::process::id() != run.pid {
            return step;
        }
        match step {
            Step::Worked => {}
            Step::Broken => return step,
            Step::Done if run.active.load(Ordering::Acquire) == 0 => return step,
            Step::Done | Step::Wait => {
                pool.wait_slice(py, generation, SLICE);
                if let Err(error) = py.check_signals() {
                    run.fail_now(py, error);
                }
            }
        }
    }
}

/// Armed by `_core.arm_worker_panic()`: the next worker-loop iteration panics.
#[cfg(feature = "test-hooks")]
static ARMED: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);

/// Makes the next iteration of any worker loop panic outside a node call.
#[cfg(feature = "test-hooks")]
#[pyfunction]
pub fn arm_worker_panic() {
    ARMED.store(true, Ordering::Release);
}

/// The armed panic, in a run still going: it panics, and the run fails with it as a
/// worker loop's panic does. Counted as a task while it runs, so the run can't end
/// before its failure is in; a pool thread may also still hold an ended run's handle.
#[cfg(feature = "test-hooks")]
fn armed_panic(py: Python<'_>, run: &Run, pool: &Pool) -> bool {
    if !ARMED.load(Ordering::Acquire) {
        return false;
    }
    let live = run.with_state(py, |state| {
        let live = state.scheduler.is_some() && ARMED.swap(false, Ordering::AcqRel);
        if live {
            run.active.fetch_add(1, Ordering::AcqRel);
        }
        live
    });
    if live != Some(true) {
        return false;
    }
    #[expect(
        clippy::panic,
        reason = "the deliberate panic is this hook's whole purpose"
    )]
    let payload = std::panic::catch_unwind(|| panic!("deliberate panic in fnloom's worker loop"))
        .err()
        .unwrap_or_else(|| Box::new("no panic"));
    run.fail_now(py, crate::errors::panic_error(&*payload));
    run.active.fetch_sub(1, Ordering::AcqRel);
    pool.notify(py);
    true
}
