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

use std::cell::{Cell, RefCell};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Condvar, Mutex, PoisonError, TryLockError};
use std::time::Duration;

use fnloom_core::limits::{Grant, Limits as CoreLimits};
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
use crate::limits::{Caps, held_variable};
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
    /// Nothing to take now, nothing of the run is in flight, and the ready task with the
    /// lowest key waits for a slot of this node (`Scheduler::blocked`).
    Blocked(NodeId),
    /// Nothing is left to hand out.
    Done,
    /// The scheduler's lock is poisoned: the run is broken.
    Broken,
    /// A node forked, and this is the child: the run's other threads are gone, so nothing
    /// it inherited is locked again.
    Forked,
}

/// Holds the run's busy flag, and clears it when dropped, a panic included.
struct Busy<'a, 'py> {
    run: &'a Run,
    py: Python<'py>,
}

impl Drop for Busy<'_, '_> {
    fn drop(&mut self) {
        self.run.release(self.py);
    }
}

/// Counts a task of the run as running, until dropped, a panic included: then the run's
/// waiters are woken, since it may have ended.
struct Active<'a, 'py> {
    run: &'a Run,
    pool: &'a Pool,
    py: Python<'py>,
}

impl Drop for Active<'_, '_> {
    fn drop(&mut self) {
        self.run.active.fetch_sub(1, Ordering::AcqRel);
        self.pool.notify(self.py);
    }
}

/// The node and group slots a limited compute or pull holds: given back, with a wake, as
/// soon as its call returns, before its hooks and store writes, or when dropped, a panic
/// included. So `complete` gives back only the worker slot (`WorkerOnly`).
struct Slots<'a, 'py> {
    pool: &'a Pool,
    /// The node, while its slots are held.
    node: Option<NodeId>,
    /// The node, if it is limited, held or not.
    limited: Option<NodeId>,
    py: Python<'py>,
}

impl<'a, 'py> Slots<'a, 'py> {
    /// The slots a task of `node` doing `work` holds, if its node is limited.
    fn of(py: Python<'py>, pool: &'a Pool, node: NodeId, work: &Work<Shared>) -> Self {
        let takes = matches!(work, Work::Compute { .. } | Work::Pull { .. });
        let limited = (takes && pool.limits.limited(node)).then_some(node);
        Self {
            pool,
            node: limited,
            limited,
            py,
        }
    }

    /// Takes the slots back once given back, waiting detached while refused.
    fn retake(&mut self) -> PyResult<()> {
        if let (Some(node), None) = (self.limited, self.node) {
            self.pool.limits.retake(self.py, node)?;
            self.node = Some(node);
        }
        Ok(())
    }

    /// The caps the slots are held of, while they are.
    fn caps(&self) -> Option<&'a Caps> {
        self.node.map(|_| &*self.pool.limits)
    }

    fn release(&mut self) {
        // In a forked child nothing inherited is locked again.
        if let Some(node) = self
            .node
            .take()
            .filter(|_| std::process::id() == self.pool.pid)
        {
            self.pool.release(self.py, node);
        }
    }
}

impl Drop for Slots<'_, '_> {
    fn drop(&mut self) {
        self.release();
    }
}

/// The grant a completion gives slots back to: the worker slot only, since a task's node
/// and group slots went back as its call returned (`Slots`).
struct WorkerOnly<'a>(&'a CoreLimits);

impl Grant for WorkerOnly<'_> {
    fn try_start(&self, node: Option<NodeId>, seat: Seat) -> bool {
        self.0.try_start(node, seat)
    }

    fn release(&self, _node: Option<NodeId>, seat: Seat) {
        self.0.release(None, seat);
    }
}

thread_local! {
    /// The caps this thread holds a worker slot of, innermost last: a run started inside a
    /// task takes its caller's tasks on the slot already held.
    static WORKING: RefCell<Vec<usize>> = const { RefCell::new(Vec::new()) };
}

/// Marks a worker slot of `limits` held by this thread while it lives.
struct Holding;

impl Holding {
    fn enter(limits: &Arc<Caps>) -> Self {
        WORKING.with(|held| held.borrow_mut().push(Arc::as_ptr(limits) as usize));
        Self
    }
}

impl Drop for Holding {
    fn drop(&mut self) {
        WORKING.with(|held| held.borrow_mut().pop());
    }
}

/// Whether this thread holds a worker slot of `limits`.
fn holds(limits: &Arc<Caps>) -> bool {
    let id = Arc::as_ptr(limits) as usize;
    WORKING.with(|held| held.borrow().contains(&id))
}

/// The calls of one task, in its own copy of the run's context, with
/// `fnloom.current_branch()` set to its branch. Its hooks, `on_value` and `late.store`
/// run in it too.
///
/// An expand's iterator keeps a copy of the context it was made in: the scheduler holds
/// it as the pair `(iterator, context)`, so each pull and the finish run where the
/// generator's body started, and a context variable it sets lasts from one item to the
/// next. One iterator has at most one pull or finish in flight, and their hooks run in a
/// fresh copy of the run's, so its context is never entered twice at once.
struct Copied<'py> {
    context: Bound<'py, PyAny>,
    /// The context's `run` method.
    run: Bound<'py, PyAny>,
}

impl<'py> Copied<'py> {
    fn new(py: Python<'py>, snapshot: &Py<PyAny>, branch: &[usize]) -> PyResult<Self> {
        let copied = Self::of(snapshot.bind(py).call_method0(intern!(py, "copy"))?)?;
        if !branch.is_empty() {
            let set = branch_variable(py)?.bind(py).getattr(intern!(py, "set"))?;
            copied.run.call1((set, PyTuple::new(py, branch)?))?;
        }
        Ok(copied)
    }

    /// The calls in `context` itself.
    fn of(context: Bound<'py, PyAny>) -> PyResult<Self> {
        let run = context.getattr(intern!(context.py(), "run"))?;
        Ok(Self { context, run })
    }

    /// The calls of a task: an iterator's own context for a pull or a finish, else a new
    /// copy of the run's.
    fn of_task(
        py: Python<'py>,
        snapshot: &Py<PyAny>,
        branch: &[usize],
        work: &Work<Shared>,
    ) -> PyResult<Self> {
        match work {
            Work::Pull { iterator } | Work::Finish { iterator, .. } => {
                Self::of(iterator.bind(py).get_item(1)?)
            }
            _ => Self::new(py, snapshot, branch),
        }
    }

    /// `call`, in this context with `fnloom._threads.HELD` recording `node`'s slot of
    /// `caps` while it runs, when the task holds one: so a run the call starts sees it.
    fn holding<T>(
        &self,
        caps: Option<&Caps>,
        node: NodeId,
        call: impl FnOnce() -> PyResult<T>,
    ) -> PyResult<T> {
        let Some(caps) = caps else {
            return call();
        };
        let py = self.context.py();
        let variable = held_variable(py)?.bind(py);
        let held = self.run.call1((variable.getattr(intern!(py, "get"))?,))?;
        let set = variable.getattr(intern!(py, "set"))?;
        let (holding, live) = caps.holding(&held, node)?;
        let token = self.run.call1((set, holding))?;
        let result = call();
        let cleared = live.set_item(0, false);
        let reset = variable.getattr(intern!(py, "reset"))?;
        let reset = self.run.call1((reset, token));
        cleared?;
        let value = result?;
        reset.map(|_| value)
    }

    /// `iterator`, paired with a copy of this context, as the scheduler keeps it: a copy,
    /// since the task that made it still fires its hooks here while the first pull may
    /// already run.
    fn paired(&self, iterator: Bound<'py, PyAny>) -> PyResult<Shared> {
        let py = self.context.py();
        let own = self.context.call_method0(intern!(py, "copy"))?;
        let pair = PyTuple::new(py, [iterator, own])?;
        Ok(shared(pair.into_any()))
    }
}

/// Whether a report settled anything the hooks hear of.
trait Events {
    fn has_events(&self) -> bool;
}

impl Events for Report<Shared> {
    fn has_events(&self) -> bool {
        !(self.hits.is_empty() && self.expanded.is_empty() && self.skipped.is_empty())
    }
}

/// The iterator of a pair `Copied::paired` made.
fn unpaired<'py>(pair: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    pair.get_item(0)
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

    /// Sets the busy flag, waiting detached while another thread holds it; cleared when
    /// the guard drops.
    fn acquire<'a, 'py>(&'a self, py: Python<'py>) -> Busy<'a, 'py> {
        self.set_busy(py);
        let busy = Busy { run: self, py };
        #[cfg(feature = "test-hooks")]
        assert!(
            !HOOK_ARMED.swap(false, Ordering::AcqRel),
            "deliberate panic while holding a run's busy flag"
        );
        busy
    }

    fn set_busy(&self, py: Python<'_>) {
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

    /// Fires one event to the observers under the busy flag, in the task's context; the
    /// errors raised.
    fn hooked<'py>(
        &self,
        py: Python<'py>,
        calls: &Copied<'py>,
        kwargs: &Kwargs<'py, '_>,
        event: impl FnMut(&mut dyn Observer<Value<'py>, PyErr>) -> PyResult<()>,
    ) -> Vec<PyErr> {
        if !self.observers.any() {
            return Vec::new();
        }
        let busy = self.acquire(py);
        let errors = self.observers.fire(py, kwargs, Some(&calls.run), event);
        drop(busy);
        errors
    }

    /// Fires what a report settled, hits, then expands, then skips, as `Report` says, under
    /// one hold of the busy flag, in `calls`' context or else the current one. The first
    /// event an observer raised in stops the run.
    fn report(&self, py: Python<'_>, calls: Option<&Copied<'_>>, report: &Report<Shared>) {
        if !report.has_events() || !self.observers.any() {
            return;
        }
        let plan = self.plan.get();
        let graph = plan.graph().get();
        let core = graph.core();
        let kwargs = Kwargs::new(py, plan, graph);
        let context = calls.map(|calls| &calls.run);
        let busy = self.acquire(py);
        #[cfg(feature = "test-hooks")]
        assert!(
            !REPORT_ARMED.swap(false, Ordering::AcqRel),
            "deliberate panic while reporting a completed task"
        );
        let mut failed = None;
        for (node, branch, value) in &report.hits {
            let name = &core.node(*node).name;
            let errors = self.observers.fire(py, &kwargs, context, |hook| {
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
                let errors = self.observers.fire(py, &kwargs, context, |hook| {
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
                    .fire(py, &kwargs, context, |hook| hook.skipped(name, branch));
                if !errors.is_empty() {
                    failed = Some((key.clone(), "skipped", errors));
                    break;
                }
            }
        }
        drop(busy);
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
    let limits: &CoreLimits = &pool.limits.core;
    let next = run.with_state(py, |state| {
        let Some(scheduler) = state.scheduler.as_mut() else {
            return (Next::Done, None);
        };
        let next = scheduler.next(core, graph, limits, seat);
        if matches!(next, Next::Task(_)) {
            run.active.fetch_add(1, Ordering::AcqRel);
        }
        let blocked = matches!(next, Next::Wait)
            .then(|| scheduler.blocked())
            .flatten();
        (next, blocked)
    });
    match next {
        None => Step::Broken,
        Some((Next::Wait, Some(node))) => Step::Blocked(node),
        Some((Next::Wait, None)) => Step::Wait,
        Some((Next::Done, _)) => Step::Done,
        Some((Next::Task(task), _)) => {
            let active = Active { run, pool, py };
            let id = task.id;
            let completed = Cell::new(false);
            // A panic outside the node call is the run's failure, recorded before `active`
            // drops: else the run could end as a success between the count reaching zero
            // and the failure going in. A task it hit before its completion is completed
            // as failed, else the scheduler would wait for it for good.
            let forked = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                perform(py, run, pool, seat, task, report, &completed)
            }))
            .unwrap_or_else(|payload| {
                report.clear();
                let error = crate::errors::panic_error(&*payload);
                if !completed.get() {
                    let failed = Outcome::Failed(error.clone_ref(py));
                    let limits = WorkerOnly(&pool.limits.core);
                    let report_in = &mut *report;
                    run.with_state(py, move |state| {
                        if let Some(scheduler) = state.scheduler.as_mut() {
                            scheduler.complete(core, graph, &limits, id, failed, report_in);
                        }
                    });
                    report.clear();
                }
                run.fail_now(py, error);
                false
            });
            if forked {
                // The child of a fork: no lock it inherited is touched again.
                std::mem::forget(active);
                return Step::Forked;
            }
            drop(active);
            Step::Worked
        }
    }
}

/// Runs `task`, completes it, and sends what its completion settled to the hooks.
/// `true` when the task's node forked and this is the child, which then does none of that.
fn perform(
    py: Python<'_>,
    run: &Run,
    pool: &Pool,
    seat: Seat,
    task: Task<Shared>,
    report: &mut Report<Shared>,
    completed: &Cell<bool>,
) -> bool {
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
    let iterating = matches!(work, Work::Pull { .. } | Work::Finish { .. });
    let mut slots = Slots::of(py, pool, node, &work);
    let calls = Copied::of_task(py, &run.snapshot, &branch, &work);
    let landed = match &calls {
        Ok(calls) => caught(|| Ok(land(py, run, graph, node, &branch, work, calls, &mut slots)))
            .unwrap_or_else(Landed::failed),
        Err(error) => Landed::failed(error.clone_ref(py)),
    };
    drop(slots);
    drop(held);
    if std::process::id() != run.pid {
        return true;
    }
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
    #[cfg(feature = "test-hooks")]
    assert!(
        !PERFORM_ARMED.swap(false, Ordering::AcqRel),
        "deliberate panic in a task before its completion"
    );
    let limits = WorkerOnly(&pool.limits.core);
    let report_in = &mut *report;
    completed.set(true);
    run.with_state(py, move |state| {
        state.failures.extend(failure);
        if let Some(scheduler) = state.scheduler.as_mut() {
            if stop {
                scheduler.stop();
            }
            scheduler.complete(core, graph.core(), &limits, id, outcome, report_in);
        }
    });
    if iterating && report.has_events() && run.observers.any() {
        // Once completed, the iterator's next pull may be in its context already: its
        // hooks run in a fresh copy of the run's.
        let fresh = Copied::new(py, &run.snapshot, &branch).ok();
        run.report(py, fresh.as_ref(), report);
    } else {
        run.report(py, calls.as_ref().ok(), report);
    }
    // The values no step reads any more are dropped here, attached.
    report.clear();
    false
}

/// Runs a task's work: its node's call, or a pull, a finish, an iterate or a probe.
#[expect(
    clippy::too_many_arguments,
    reason = "the task's parts, as the scheduler gives them, and its slots"
)]
fn land(
    py: Python<'_>,
    run: &Run,
    graph: &Graph,
    id: NodeId,
    branch: &[usize],
    work: Work<Shared>,
    calls: &Copied<'_>,
    slots: &mut Slots<'_, '_>,
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
    match work {
        Work::Compute {
            args,
            expands,
            late,
        } => compute(
            py, run, &scope, calls, node, branch, args, expands, late, slots,
        ),
        Work::Iterate { iterable } => match scope
            .iterate(calls, node, iterable.bind(py), branch)
            .and_then(|iterator| calls.paired(iterator))
        {
            Ok(iterator) => Landed::done(Outcome::Iterator(iterator)),
            Err(error) => Landed::failed(error),
        },
        Work::Pull { iterator } => match unpaired(iterator.bind(py)).and_then(|iterator| {
            let item = calls.holding(slots.caps(), id, || scope.next_item(calls, node, &iterator));
            slots.release();
            item
        }) {
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
        } => match unpaired(iterator.bind(py))
            .and_then(|iterator| scope.finish_items(calls, &iterator, exhausted))
        {
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
                // A step that runs shares its value with the steps of this run whose key
                // equals its own (single-flight); a hit runs nothing, so has no key.
                let (hit, key) = scope.probe(calls, node, branch)?;
                let hit = hit.map(|value| {
                    let absent = node.may_be_absent && value.is(scope.absent);
                    (shared(value), absent)
                });
                Ok((hit, key))
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

/// A compute's arguments, bound: a gathered collect argument as a `list`, an omitted
/// one left out.
fn bound_args<'g, 'py>(
    py: Python<'py>,
    graph: &'g Graph,
    args: Vec<(NodeId, ArgValue<Shared>)>,
) -> PyResult<Vec<Arg<'g, Value<'py>>>> {
    let mut built = Vec::with_capacity(args.len());
    for (input, arg) in args {
        let value = match arg {
            ArgValue::One(value) => value.bind(py).clone(),
            ArgValue::Gather(values) => {
                PyList::new(py, values.iter().map(|value| value.bind(py)))?.into_any()
            }
            ArgValue::Omitted => continue,
        };
        built.push(Arg {
            node: input,
            name: graph.core().node(input).name.as_str(),
            value,
        });
    }
    Ok(built)
}

/// Runs a compute: a late step's lookup, its hooks, its node's call in the task's context,
/// `on_value` and `late.store`, and an expand's iterator. A limited node's slots, taken
/// at hand-out, are given back while its `pre_node` hooks wait for the run's busy flag and
/// run, then taken back for the call: the flag's holder may be a hook whose own run needs
/// them, which would otherwise wait for good on a task that waits for it.
#[expect(
    clippy::too_many_arguments,
    reason = "the task's parts, as the scheduler gives them, and its slots"
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
    slots: &mut Slots<'_, '_>,
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
    let built = match bound_args(py, graph, args) {
        Ok(built) => built,
        Err(error) => return Landed::failed(error),
    };
    let Some(payload) = node.payload() else {
        return Landed::failed(PyRuntimeError::new_err(format!(
            "broken plan: input node '{}' planned to compute",
            node.name
        )));
    };
    let blank = Kwargs::new(py, plan, graph);
    let kwargs = match blank.build(node.id, &built) {
        Ok(kwargs) => kwargs,
        Err(error) => return Landed::failed(error),
    };
    let lent = Kwargs::lending(py, plan, graph, node.id, Some(kwargs.clone()));
    let hooked = run.observers.any();
    if hooked {
        slots.release();
    }
    let errors = run.hooked(py, calls, &lent, |hook| {
        hook.pre_node_in(&node.name, branch, &built)
    });
    if !errors.is_empty() {
        return Landed::hooks(py, "pre_node", errors);
    }
    if let Err(error) = slots.retake() {
        return Landed::failed(error);
    }
    let result = calls
        .holding(slots.caps(), node.id, || {
            caught(|| calls.call(payload.bind(py), Vec::new(), Some(&kwargs)))
        })
        .and_then(|value| scope.checked(node, value));
    if std::process::id() != run.pid {
        // The node forked, and this is the child: `perform` returns at once.
        return Landed::hooks(py, "fork", Vec::new());
    }
    // A limit counts concurrent calls, and a hook may start a run needing this slot.
    slots.release();
    let mut errors = run.hooked(py, calls, &lent, |hook| {
        hook.post_node_in(&node.name, branch, result.as_ref())
    });
    errors.extend(run.observers.stored(
        py,
        &calls.run,
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
                match scope
                    .iterate(calls, node, &value, branch)
                    .and_then(|iterator| calls.paired(iterator))
                {
                    Ok(iterator) => Landed::done(Outcome::Iterator(iterator)),
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

/// Clears a `HELD` entry's live flag when dropped: the slot it names is given back.
struct Ended<'py>(Bound<'py, PyList>);

impl Drop for Ended<'_> {
    fn drop(&mut self) {
        drop(self.0.set_item(0, false));
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
        .call_method0(intern!(py, "copy_context"))?;
    let variable = held_variable(py)?.bind(py);
    let held = snapshot.call_method1(intern!(py, "get"), (variable, PyTuple::empty(py)))?;
    // A run started on the thread of one of its tasks, or one its context reaches, runs
    // inside a task holding a worker slot: it takes its caller's tasks on that slot.
    let seat = if holds(&pool.limits) || pool.limits.works(&held) {
        Seat::Held
    } else {
        Seat::Take
    };
    // Every task of this run holds a worker slot or runs inside one that does, so the
    // context they all copy records it, live until the run ends.
    let (working, live) = pool.limits.holding_worker(&held)?;
    snapshot.call_method1(
        intern!(py, "run"),
        (variable.getattr(intern!(py, "set"))?, working),
    )?;
    let _live = Ended(live);
    let snapshot = snapshot.unbind();
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
    let kwargs = Kwargs::new(py, plan, graph);
    let errors = run
        .observers
        .fire(py, &kwargs, None, |hook| hook.pre_graph());
    if !errors.is_empty() {
        let closing = run
            .observers
            .fire(py, &kwargs, None, |hook| hook.post_graph(false));
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
    run.report(py, None, &report);
    report.clear();
    pool.register(py, &run);
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
    let kwargs = Kwargs::new(py, plan, graph);
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
            .fire(py, &kwargs, None, |hook| hook.post_graph(false));
        drop(closing);
        return Err(broken());
    };
    let outcome = outcome_of(scheduler.finish(core, graph.core()), failures);
    match outcome {
        Ok(outputs) => {
            let errors = run
                .observers
                .fire(py, &kwargs, None, |hook| hook.post_graph(true));
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
                .fire(py, &kwargs, None, |hook| hook.post_graph(false));
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
            Step::Forked => return step,
            Step::Broken => {
                // The tasks still running end on their own, their counts dropped by guards.
                while run.active.load(Ordering::Acquire) > 0 {
                    let generation = pool.generation(py);
                    pool.wait_slice(py, generation, SLICE);
                }
                return step;
            }
            Step::Done if run.active.load(Ordering::Acquire) == 0 => return step,
            Step::Done | Step::Wait | Step::Blocked(_) => {
                if let Step::Blocked(node) = step {
                    if let Some(error) = reentered(py, run, pool, node) {
                        // Waiting would be for good: the run fails, once nothing runs.
                        run.fail_now(py, error);
                        continue;
                    }
                }
                pool.wait_slice(py, generation, SLICE);
                if let Err(error) = py.check_signals() {
                    run.fail_now(py, error);
                }
            }
        }
    }
}

/// The `RuntimeError` for a run blocked on a slot of `node` that the runs that called it
/// hold every unit of, by the `HELD` its context started with; `None` when it may wait.
fn reentered(py: Python<'_>, run: &Run, pool: &Pool, node: NodeId) -> Option<PyErr> {
    if !pool.limits.limited(node) {
        return None;
    }
    let variable = held_variable(py).ok()?.bind(py);
    let held = run
        .snapshot
        .bind(py)
        .call_method1(intern!(py, "get"), (variable,))
        .ok()?;
    pool.limits.reentered(node, Some(&held))
}

/// Armed by `_core.arm_worker_panic()`: the next worker-loop iteration panics.
#[cfg(feature = "test-hooks")]
static ARMED: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);

/// Armed by `_core.arm_hook_panic()`: the next hold of a busy flag panics.
#[cfg(feature = "test-hooks")]
static HOOK_ARMED: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);

/// Armed by `_core.arm_perform_panic()`: the next task panics after its work, outside
/// every node call, before its completion.
#[cfg(feature = "test-hooks")]
static PERFORM_ARMED: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);

/// Makes the next task of any run panic, in Rust, between its work and its completion.
#[cfg(feature = "test-hooks")]
#[pyfunction]
pub fn arm_perform_panic() {
    PERFORM_ARMED.store(true, Ordering::Release);
}

/// Armed by `_core.arm_report_panic()`: the next report of a completed task's events
/// panics, outside any node call, after the task completed.
#[cfg(feature = "test-hooks")]
static REPORT_ARMED: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);

/// Makes the next report of any run's completed task panic, in Rust, holding the busy
/// flag: outside `caught`, so the panic unwinds through the task's guards.
#[cfg(feature = "test-hooks")]
#[pyfunction]
pub fn arm_report_panic() {
    REPORT_ARMED.store(true, Ordering::Release);
}

/// Makes the next hold of any run's busy flag panic, in Rust, while it holds it.
#[cfg(feature = "test-hooks")]
#[pyfunction]
pub fn arm_hook_panic() {
    HOOK_ARMED.store(true, Ordering::Release);
}

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
