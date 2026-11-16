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
//! - At a deadline, or on an interrupt, the run stops and abandons its running tasks
//!   (O27): their slots are given back at once, the pool starts a thread for each, and
//!   each drops what its call gives when it returns. The caller then closes the iterators
//!   no pull holds, and raises.

use std::cell::{Cell, RefCell};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Condvar, Mutex, PoisonError, TryLockError};
use std::time::{Duration, Instant};

use fnloom_core::limits::{Grant, Limits as CoreLimits, Resource};
use fnloom_core::node::{Node, NodeId};
use fnloom_core::plan::{Action, Plan as CorePlan};
use fnloom_core::run::{Arg, Item, Observer, RunError, Slot};
use fnloom_core::schedule::{
    self, ArgValue, Next, Options, OrderKey, Outcome, Report, Scheduler, Seat, Task, Work,
};
use pyo3::exceptions::{PyKeyboardInterrupt, PyRuntimeError, PyTimeoutError, PyValueError};
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyDict, PyList, PyTuple};

use crate::absent::{self, Absent};
use crate::errors::{caught, run_error};
use crate::graph::Graph;
use crate::limits::{Caps, abandoned, held_variable, inherited};
use crate::observer::ObserverBase;
use crate::plan::{Kwargs, Plan, Value, branch_variable, keyword};
use crate::pool::{Pool, Threads};
use crate::shared::SharedObservers;
use crate::task::{Calls, Scope};

/// A value of a parallel run: a strong reference any thread may hold, and clone without
/// attaching. Only ever dropped attached.
pub(crate) type Shared = Arc<Py<PyAny>>;
pub(crate) type Sched = Scheduler<Shared, PyErr, [u8; 32]>;
pub(crate) type Out = Outcome<Shared, PyErr, [u8; 32]>;

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
    /// Set once the run is stopping: what `fnloom.cancelled()` reads, through `Stop`.
    stop: Arc<AtomicBool>,
    /// Set once the run abandoned its running tasks: they fire no hook and store nothing.
    abandoned: AtomicBool,
    /// Set when a task failed with `KeyboardInterrupt`: the caller abandons the others.
    interrupted: AtomicBool,
    /// Set once a step found nothing left to hand out: what the caller of a run with a
    /// deadline, which takes no task, waits for.
    done: AtomicBool,
    /// Whether the run has a lent seat (`Scheduler::lend`), which pool threads take tasks
    /// on once no worker slot is free.
    lends: AtomicBool,
    /// The stop flags of the runs that started this one, innermost first: once one is
    /// set, this run stops too.
    ancestors: Vec<Arc<AtomicBool>>,
    /// Set once the run stopped because one of its ancestors did (`follow`).
    followed: AtomicBool,
}

/// What the scheduler's lock guards.
struct State {
    scheduler: Option<Sched>,
    /// Every failure the run saw, with its order key.
    failures: Vec<(OrderKey, Failure)>,
    /// Tasks handed out and not completed, and those of them holding a worker slot.
    running: usize,
    taking: usize,
    /// The node and group slots of the limited tasks handed out and not completed.
    limited: Vec<Arc<Held>>,
    /// Bumped as the run abandons its running tasks: a task handed out before that
    /// completes as abandoned.
    abandons: u32,
}

/// A limited task's node and group slots, and whether it holds them: whoever swaps
/// `held` from `true` gives them back, the task itself or the run abandoning it.
struct Held {
    node: NodeId,
    held: AtomicBool,
}

/// A task as `step` handed it out: the task, its slots if its node is limited, and the
/// run's abandonment count then.
struct Handed {
    task: Task<Shared>,
    held: Option<Arc<Held>>,
    epoch: u32,
}

/// How `perform` left a task.
enum Performed {
    /// Completed, and its events sent.
    Done,
    /// A node forked, and this is the child, which does nothing more.
    Forked,
    /// The run abandoned it while it ran: its outcome is dropped.
    Abandoned,
}

/// A run's stop flag, as `fnloom._threads.CANCELLED` holds it: true once the run is
/// stopping, by a deadline, a failure or an interrupt, or once a run that started it,
/// at any depth, is.
#[pyclass(frozen, module = "fnloom._core")]
pub struct Stop {
    own: Arc<AtomicBool>,
    /// The stop flags of the runs that started this one, innermost first.
    ancestors: Vec<Arc<AtomicBool>>,
}

#[pymethods]
impl Stop {
    fn __bool__(&self) -> bool {
        self.own.load(Ordering::Acquire) || stopping(&self.ancestors)
    }
}

impl Stop {
    /// A new run's flag, unset, under the run whose task, at any depth, the current context
    /// is in, if any.
    pub(crate) fn current(py: Python<'_>) -> PyResult<Self> {
        let parent = cancelled_variable(py)?
            .bind(py)
            .call_method0(intern!(py, "get"))?;
        let ancestors = parent
            .cast::<Self>()
            .map(|parent| parent.get().chain())
            .unwrap_or_default();
        Ok(Self {
            own: Arc::new(AtomicBool::new(false)),
            ancestors,
        })
    }

    /// The run is stopping.
    pub(crate) fn set(&self) {
        self.own.store(true, Ordering::Release);
    }

    /// Whether a run that started this one has stopped.
    pub(crate) fn followed(&self) -> bool {
        stopping(&self.ancestors)
    }

    /// This flag and its ancestors': the ancestors of a run this one's tasks start.
    fn chain(&self) -> Vec<Arc<AtomicBool>> {
        let mut chain = Vec::with_capacity(self.ancestors.len() + 1);
        chain.push(Arc::clone(&self.own));
        chain.extend(self.ancestors.iter().cloned());
        chain
    }
}

/// Whether any of `flags` is set.
fn stopping(flags: &[Arc<AtomicBool>]) -> bool {
    flags.iter().any(|flag| flag.load(Ordering::Acquire))
}

/// `fnloom._threads.CANCELLED`, the context variable of the run a task belongs to.
fn cancelled_variable(py: Python<'_>) -> PyResult<&'static Py<PyAny>> {
    static CANCELLED: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
    CANCELLED.get_or_try_init(py, || {
        Ok(py
            .import(intern!(py, "fnloom._threads"))?
            .getattr(intern!(py, "CANCELLED"))?
            .unbind())
    })
}

pyo3::create_exception!(
    fnloom,
    StoppedError,
    PyRuntimeError,
    "A parallel run stopped because the run that started it stopped or ended.\n\n\
     The run that started it, should a node of it raise this, raises the failure that \
     stopped it instead, when it has one."
);

/// The `StoppedError` a run raises when it stopped for the run that started it.
pub(crate) fn stopped() -> PyErr {
    StoppedError::new_err("the run that started this one has stopped or ended, so this one stopped")
}

/// Stops `scheduler` once a run that started this one has stopped: it hands out no new
/// task. Checked under the scheduler's lock before every hand-out, so no task starts
/// after it. It records no failure: a run whose outputs were all computed returns them,
/// and one missing some raises `StoppedError` (`ended`).
fn follow(run: &Run, scheduler: &mut Sched) {
    if run.ancestors.is_empty() || scheduler.is_stopping() || !stopping(&run.ancestors) {
        return;
    }
    scheduler.stop();
    run.followed.store(true, Ordering::Release);
    run.stop.store(true, Ordering::Release);
}

/// A task whose node raised `StoppedError`, as a run it started does once this one stops: its
/// failure is keyed after every step, so the failure that stopped this run is raised, and
/// the scheduler, stopped, drops the task's outcome instead of keeping it as a failure.
fn demoted(
    py: Python<'_>,
    outcome: Out,
    failure: Option<(OrderKey, Failure)>,
    stop: bool,
) -> (Out, Option<(OrderKey, Failure)>, bool) {
    match outcome {
        Outcome::Failed(error) if error.is_instance_of::<StoppedError>(py) => {
            // What its `post_node` hooks raised goes with it.
            let mut errors = vec![error];
            if let Some((_, Failure::Task(extra))) = failure {
                errors.extend(extra);
            }
            let failure = RunError::Observers {
                event: "the run",
                errors,
            };
            let dropped = Outcome::Value {
                value: Arc::new(py.None()),
                absent: false,
            };
            let last = OrderKey::new(&[u32::MAX]);
            (dropped, Some((last, Failure::Run(failure))), true)
        }
        outcome => (outcome, failure, stop),
    }
}

/// The slots of a limited task of `node` just handed out, held, and kept in `limited` for
/// the run to give back should it abandon the task.
fn held_by(node: NodeId, limited: &mut Vec<Arc<Held>>) -> Arc<Held> {
    let slots = Arc::new(Held {
        node,
        held: AtomicBool::new(true),
    });
    limited.push(Arc::clone(&slots));
    slots
}

/// Counts a completed task out of the running ones.
fn settle(state: &mut State, seat: Seat, held: Option<&Arc<Held>>) {
    state.running = state.running.saturating_sub(1);
    if seat == Seat::Take {
        state.taking = state.taking.saturating_sub(1);
    }
    if let Some(held) = held {
        state.limited.retain(|other| !Arc::ptr_eq(other, held));
    }
}

/// A failure the run saw.
pub(crate) enum Failure {
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
    /// The run abandoned the task this thread ran: the pool has a thread in its place.
    Abandoned,
}

/// Holds the run's busy flag, and clears it when dropped, a panic included.
struct Busy<'a, 'py> {
    run: &'a Run,
    py: Python<'py>,
}

impl Drop for Busy<'_, '_> {
    fn drop(&mut self) {
        // A hook that forked returns here in the child, which locks nothing it inherited.
        if std::process::id() == self.run.pid {
            self.run.release(self.py);
        }
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
/// included. So `complete` gives back only the worker slot (`WorkerOnly`). A run that
/// abandons the task gives them back instead, whichever swaps `held` first.
struct Slots<'a, 'py> {
    pool: &'a Pool,
    run: &'a Run,
    /// The slots, if the task's node is limited; taken at hand-out.
    held: Option<Arc<Held>>,
    py: Python<'py>,
}

impl<'a> Slots<'a, '_> {
    /// Takes the slots back once given back, waiting detached while refused; an error once
    /// the run abandoned the task.
    fn retake(&mut self) -> PyResult<()> {
        let Some(held) = self
            .held
            .as_ref()
            .filter(|held| !held.held.load(Ordering::SeqCst))
        else {
            return Ok(());
        };
        self.pool
            .limits
            .retake(self.py, held.node, &self.run.abandoned)?;
        held.held.store(true, Ordering::SeqCst);
        // Set before `held` was, the abandoning run gave nothing back: this task does.
        if self.run.abandoned.load(Ordering::SeqCst) {
            self.release();
            return Err(abandoned());
        }
        Ok(())
    }

    /// The caps the slots are held of, while they are.
    fn caps(&self) -> Option<&'a Caps> {
        self.held
            .as_ref()
            .filter(|held| held.held.load(Ordering::SeqCst))
            .map(|_| &*self.pool.limits)
    }

    fn release(&mut self) {
        if let Some(held) = self.held.as_ref() {
            // In a forked child nothing inherited is locked again.
            if std::process::id() == self.pool.pid && held.held.swap(false, Ordering::SeqCst) {
                self.pool.release(self.py, held.node);
            }
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
pub(crate) fn unpaired<'py>(pair: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
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

pub(crate) fn shared(value: Bound<'_, PyAny>) -> Shared {
    Arc::new(value.unbind())
}

impl Run {
    /// A run of `plan` in this process, its tasks copying `snapshot`, its scheduler not
    /// yet made; `stop` is its own stop flag and its ancestors'.
    fn new(
        plan: &Bound<'_, Plan>,
        snapshot: Py<PyAny>,
        observers: SharedObservers,
        (stop, ancestors): Flags,
        lends: bool,
    ) -> Self {
        Self {
            plan: plan.clone().unbind(),
            state: Mutex::new(State {
                scheduler: None,
                failures: Vec::new(),
                running: 0,
                taking: 0,
                limited: Vec::new(),
                abandons: 0,
            }),
            active: AtomicUsize::new(0),
            busy: Mutex::new(false),
            clear: Condvar::new(),
            snapshot,
            observers,
            pid: std::process::id(),
            stop,
            abandoned: AtomicBool::new(false),
            interrupted: AtomicBool::new(false),
            done: AtomicBool::new(false),
            lends: AtomicBool::new(lends),
            ancestors,
            followed: AtomicBool::new(false),
        }
    }

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
        self.stop.store(true, Ordering::Release);
    }

    /// Whether the run has a lent seat its pool threads may take tasks on.
    pub fn lends(&self) -> bool {
        self.lends.load(Ordering::Acquire)
    }

    /// Sets the busy flag for the caller of a run that abandoned its tasks, which may
    /// still be firing a hook: waits detached in slices, checking for signals between.
    fn hold<'a, 'py>(&'a self, py: Python<'py>) -> PyResult<Busy<'a, 'py>> {
        loop {
            let set = py.detach(|| {
                let mut busy = self.busy.lock().unwrap_or_else(PoisonError::into_inner);
                if *busy {
                    busy = self
                        .clear
                        .wait_timeout(busy, SLICE)
                        .unwrap_or_else(PoisonError::into_inner)
                        .0;
                }
                let free = !*busy;
                if free {
                    *busy = true;
                }
                free
            });
            if set {
                return Ok(Busy { run: self, py });
            }
            py.check_signals()?;
        }
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
        // The run ended without this task: its hooks heard the end already.
        if self.abandoned.load(Ordering::SeqCst) {
            return Vec::new();
        }
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
        let kwargs = Kwargs::new(py, plan, plan.graph().get());
        let context = calls.map(|calls| &calls.run);
        let busy = self.acquire(py);
        #[cfg(feature = "test-hooks")]
        assert!(
            !REPORT_ARMED.swap(false, Ordering::AcqRel),
            "deliberate panic while reporting a completed task"
        );
        if self.abandoned.load(Ordering::SeqCst) {
            return;
        }
        let failed = fire_report(py, &self.observers, &kwargs, context, plan, report);
        drop(busy);
        // A hook that forked: the child locks nothing it inherited.
        if std::process::id() != self.pid {
            return;
        }
        if let Some((key, event, errors)) = failed {
            self.fail_at(py, key, RunError::Observers { event, errors });
        }
    }
}

/// Fires what `report` settled to `observers`: its hits, then its expands, then its skips,
/// in `context`, or else the current one. The first event an observer raised in, with its
/// order key and its errors, after which no event fires.
pub(crate) fn fire_report<'py>(
    py: Python<'py>,
    observers: &SharedObservers,
    kwargs: &Kwargs<'py, '_>,
    context: Option<&Bound<'py, PyAny>>,
    plan: &Plan,
    report: &Report<Shared>,
) -> Option<(OrderKey, &'static str, Vec<PyErr>)> {
    let core = plan.graph().get().core();
    for (node, branch, value) in &report.hits {
        let name = &core.node(*node).name;
        let errors = observers.fire(py, kwargs, context, |hook| {
            hook.hit_in(name, branch, value.bind(py))
        });
        if !errors.is_empty() {
            return Some((plan.core().order_key(*node, branch), "hit", errors));
        }
    }
    for (node, branch, items) in &report.expanded {
        let name = &core.node(*node).name;
        let errors = observers.fire(py, kwargs, context, |hook| {
            hook.post_expand(name, branch, *items)
        });
        if !errors.is_empty() {
            return Some((plan.core().order_key(*node, branch), "post_expand", errors));
        }
    }
    for (node, branch, key) in &report.skipped {
        let name = &core.node(*node).name;
        let errors = observers.fire(py, kwargs, context, |hook| hook.skipped(name, branch));
        if !errors.is_empty() {
            return Some((key.clone(), "skipped", errors));
        }
    }
    None
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
        #[cfg(feature = "test-hooks")]
        state_armed();
        let State {
            scheduler,
            running,
            taking,
            limited,
            abandons,
            ..
        } = state;
        // Once the run abandoned its tasks, only its caller takes any: the finishes it runs.
        let Some(scheduler) = scheduler
            .as_mut()
            .filter(|_| *abandons == 0 || seat == Seat::Threadless)
        else {
            return (Next::Done, None, None, 0);
        };
        follow(run, scheduler);
        let next = scheduler.next(core, graph, limits, seat);
        let mut held = None;
        if let Next::Task(task) = &next {
            run.active.fetch_add(1, Ordering::AcqRel);
            *running += 1;
            *taking += usize::from(seat == Seat::Take);
            let calls = matches!(task.work, Work::Compute { .. } | Work::Pull { .. });
            held = (calls && pool.limits.limited(task.node)).then(|| held_by(task.node, limited));
        }
        let blocked = matches!(next, Next::Wait)
            .then(|| scheduler.blocked())
            .flatten();
        (next, blocked, held, *abandons)
    });
    match next {
        None => Step::Broken,
        Some((Next::Wait, Some(node), ..)) => Step::Blocked(node),
        Some((Next::Wait, None, ..)) => Step::Wait,
        Some((Next::Done, ..)) => {
            if !run.done.swap(true, Ordering::AcqRel) {
                pool.notify(py);
            }
            Step::Done
        }
        Some((Next::Task(task), _, held, epoch)) => {
            let active = Active { run, pool, py };
            let id = task.id;
            let slots = held.clone();
            let completed = Cell::new(false);
            let handed = Handed { task, held, epoch };
            // A panic outside the node call is the run's failure, recorded before `active`
            // drops: else the run could end as a success between the count reaching zero
            // and the failure going in. A task it hit before its completion is completed
            // as failed, else the scheduler would wait for it for good.
            let performed = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                perform(py, run, pool, seat, handed, report, &completed)
            }))
            .unwrap_or_else(|payload| {
                report.clear();
                let error = crate::errors::panic_error(&*payload);
                if !completed.get() {
                    let failed = Outcome::Failed(error.clone_ref(py));
                    let limits = WorkerOnly(&pool.limits.core);
                    let report_in = &mut *report;
                    let unused = run.with_state(py, move |state| {
                        if state.abandons != epoch {
                            return Some(failed);
                        }
                        settle(state, seat, slots.as_ref());
                        if let Some(scheduler) = state.scheduler.as_mut() {
                            scheduler.complete(core, graph, &limits, id, failed, report_in);
                        }
                        None
                    });
                    drop(unused);
                    report.clear();
                }
                run.fail_now(py, error);
                Performed::Done
            });
            match performed {
                Performed::Forked => {
                    // The child of a fork: no lock it inherited is touched again.
                    std::mem::forget(active);
                    Step::Forked
                }
                Performed::Abandoned => Step::Abandoned,
                Performed::Done => Step::Worked,
            }
        }
    }
}

/// Runs a handed-out task, completes it, and sends what its completion settled to the
/// hooks. A node that forked leaves the child doing none of that, and a task the run
/// abandoned meanwhile drops its outcome, closing an iterator it pulled.
fn perform(
    py: Python<'_>,
    run: &Run,
    pool: &Pool,
    seat: Seat,
    handed: Handed,
    report: &mut Report<Shared>,
    completed: &Cell<bool>,
) -> Performed {
    let plan = run.plan.get();
    let core = plan.core();
    let graph = plan.graph().get();
    let working = matches!(seat, Seat::Take | Seat::Lent).then(|| Holding::enter(&pool.limits));
    let Handed { task, held, epoch } = handed;
    let Task {
        id,
        node,
        branch,
        key,
        work,
    } = task;
    let iterating = matches!(work, Work::Pull { .. } | Work::Finish { .. });
    let pulled = match &work {
        Work::Pull { iterator } => Some(Arc::clone(iterator)),
        _ => None,
    };
    let mut slots = Slots {
        pool,
        run,
        held: held.clone(),
        py,
    };
    let calls = Copied::of_task(py, &run.snapshot, &branch, &work);
    let landed = match &calls {
        Ok(calls) => caught(|| Ok(land(py, run, graph, node, &branch, work, calls, &mut slots)))
            .unwrap_or_else(Landed::failed),
        Err(error) => Landed::failed(error.clone_ref(py)),
    };
    drop(slots);
    drop(working);
    if std::process::id() != run.pid {
        return Performed::Forked;
    }
    let Landed {
        outcome,
        failure,
        stop,
    } = landed;
    if let Outcome::Failed(error) = &outcome {
        if error.is_instance_of::<PyKeyboardInterrupt>(py) {
            run.interrupted.store(true, Ordering::Release);
        }
    }
    let failure = failure.map(|failure| {
        let at = match &outcome {
            // A probe's failure is its node's, keyed by that node's step.
            Outcome::LookupFailed(failed, _) => core.order_key(*failed, &branch),
            _ => key,
        };
        (at, failure)
    });
    let (outcome, failure, stop) = demoted(py, outcome, failure, stop);
    #[cfg(feature = "test-hooks")]
    assert!(
        !PERFORM_ARMED.swap(false, Ordering::AcqRel),
        "deliberate panic in a task before its completion"
    );
    let limits = WorkerOnly(&pool.limits.core);
    let report_in = &mut *report;
    completed.set(true);
    let stopped = &run.stop;
    let abandoned = run.with_state(py, move |state| {
        // Handed out before the run abandoned its tasks: its slots went back then.
        if state.abandons != epoch {
            return Some(outcome);
        }
        settle(state, seat, held.as_ref());
        state.failures.extend(failure);
        if let Some(scheduler) = state.scheduler.as_mut() {
            if stop {
                scheduler.stop();
            }
            scheduler.complete(core, graph.core(), &limits, id, outcome, report_in);
            if scheduler.is_stopping() {
                stopped.store(true, Ordering::Release);
            }
        }
        None
    });
    if let Some(Some(outcome)) = abandoned {
        drop(outcome);
        if let (Some(pair), Ok(calls)) = (pulled, &calls) {
            closed_after(calls, &pair);
        }
        return Performed::Abandoned;
    }
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
    // Only a hook can have forked since `land`; `getpid` is a system call, so it is asked
    // only then.
    if run.observers.any() && std::process::id() != run.pid {
        Performed::Forked
    } else {
        Performed::Done
    }
}

/// Closes the iterator of `pair`, as `Copied::paired` made it, after an abandoned pull
/// of it returned: once its run is gone, no one else will. What closing raises is dropped.
fn closed_after(calls: &Copied<'_>, pair: &Shared) {
    let py = calls.context.py();
    let flag = false
        .into_pyobject(py)
        .map(|flag| flag.to_owned().into_any());
    if let (Ok(iterator), Ok(flag)) = (unpaired(pair.bind(py)), flag) {
        drop(caught(|| {
            calls.call_method(&iterator, intern!(py, "finish"), vec![flag])
        }));
    }
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
        // A pull its run abandoned before it started reads nothing; `perform` closes it.
        Work::Pull { .. } if run.abandoned.load(Ordering::SeqCst) => Landed::failed(abandoned()),
        Work::Pull { iterator } => match unpaired(iterator.bind(py)).and_then(|iterator| {
            let item = calls.holding(slots.caps(), id, || scope.next_item(calls, node, &iterator));
            slots.release();
            item
        }) {
            Ok(Ok(item)) => Landed::done(Outcome::Item(match item {
                Item::Next(value) => Item::Next(shared(value)),
                Item::Declined(value) => Item::Declined(shared(value)),
                Item::Done => Item::Done,
            })),
            Ok(Err(error)) if graph.absent_on(py, id, &error) => {
                // The expand is absent, its branches so far dropped, as if it declined.
                let lent = Kwargs::new(py, plan, graph);
                let errors = run.hooked(py, calls, &lent, |hook| {
                    hook.absent_error(&node.name, branch, &error)
                });
                if errors.is_empty() {
                    Landed::done(Outcome::Item(Item::Declined(shared(
                        scope.absent.clone().into_any(),
                    ))))
                } else {
                    Landed::hooks(py, "absent_error", errors)
                }
            }
            Ok(Err(error)) | Err(error) => Landed::failed(error),
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
pub(crate) fn bound_args<'g, 'py>(
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

/// A late step's cache hit, or its lookup's failure; `None` when it missed.
fn looked_up<'py>(
    scope: &Scope<'_, 'py>,
    calls: &Copied<'py>,
    node: &Node<Py<PyAny>>,
    branch: &[usize],
) -> Option<Landed> {
    match scope.lookup(calls, node, branch) {
        Err(error) => Some(Landed::failed(error)),
        Ok(Some(value)) => {
            let absent = node.may_be_absent && value.is(scope.absent);
            Some(Landed::done(Outcome::Hit {
                value: shared(value),
                absent,
            }))
        }
        Ok(None) => None,
    }
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
    if let Some(hit) = late
        .then(|| looked_up(scope, calls, node, branch))
        .flatten()
    {
        return hit;
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
    if hooked && std::process::id() != run.pid {
        // A hook forked, and this is the child: `perform` returns at once.
        return Landed::hooks(py, "fork", Vec::new());
    }
    if !errors.is_empty() {
        return Landed::hooks(py, "pre_node", errors);
    }
    if let Err(error) = slots.retake() {
        return Landed::failed(error);
    }
    // Handed out just before its run abandoned it: the call doesn't start.
    if run.abandoned.load(Ordering::SeqCst) {
        return Landed::failed(abandoned());
    }
    // Only what the node's own call raises may make it absent, never a check of fnloom's.
    let mut absent_on = false;
    let result = calls
        .holding(slots.caps(), node.id, || {
            let called = caught(|| calls.call(payload.bind(py), Vec::new(), Some(&kwargs)));
            if let Err(error) = &called {
                absent_on = graph.absent_on(py, node.id, error);
            }
            called
        })
        .and_then(|value| scope.checked(node, value));
    if std::process::id() != run.pid {
        // The node forked, and this is the child: `perform` returns at once.
        return Landed::hooks(py, "fork", Vec::new());
    }
    if run.abandoned.load(Ordering::SeqCst) {
        // The run ended without this call: it fires no hook and stores nothing.
        return Landed::failed(abandoned());
    }
    // A limit counts concurrent calls, and a hook may start a run needing this slot.
    slots.release();
    // An error absence's two events fire together, so its `post_node` is never seen alone.
    let mut errors = run.hooked(py, calls, &lent, |hook| {
        let ended = hook.post_node_in(&node.name, branch, result.as_ref());
        match &result {
            Err(error) if absent_on => ended.and(hook.absent_error(&node.name, branch, error)),
            _ => ended,
        }
    });
    // Abandoned while its hooks ran: nothing is stored.
    if run.abandoned.load(Ordering::SeqCst) {
        return Landed::failed(abandoned());
    }
    errors.extend(run.observers.stored(
        py,
        Some(&calls.run),
        plan.core(),
        graph,
        &node.name,
        branch,
        result.as_ref(),
    ));
    landed(
        py, scope, calls, node, branch, expands, result, absent_on, errors,
    )
}

/// What a compute's call gave becomes, once its hooks and stores heard of it: an error
/// absence when `absent_on`, its node's own error being one it is absent on.
#[expect(
    clippy::too_many_arguments,
    reason = "the task's parts, and what its call and hooks gave"
)]
fn landed<'py>(
    py: Python<'py>,
    scope: &Scope<'_, 'py>,
    calls: &Copied<'py>,
    node: &Node<Py<PyAny>>,
    branch: &[usize],
    expands: bool,
    result: PyResult<Bound<'py, PyAny>>,
    absent_on: bool,
    errors: Vec<PyErr>,
) -> Landed {
    match result {
        Err(_) if absent_on && !errors.is_empty() => Landed::hooks(py, "post_node", errors),
        // Never stored, and never shared with a step of an equal key.
        Err(_) if absent_on => Landed::done(Outcome::ErrorAbsent(shared(
            scope.absent.clone().into_any(),
        ))),
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
pub(crate) fn seed(
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
pub(crate) fn outcome_of(
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

/// What the calling thread brings to its run: the seat its own steps take, its deadline,
/// and the worker slot an ancestor's context lends it.
struct Caller<'py> {
    /// `Held` when the caller runs inside a task that holds a worker slot of the driver,
    /// and waits for this run; else `Take`. A run with a deadline takes no task on it.
    seat: Seat,
    /// When the run must end, and the `timeout=` it came from.
    deadline: Option<(Instant, f64)>,
    /// The `lend` list of the live worker entry the run's context carries, and whether
    /// this run took it: one run at a time runs on that entry's slot.
    lend: Option<Bound<'py, PyList>>,
    took: bool,
}

impl Caller<'_> {
    /// Takes the slot the run's context lends, unless another run has it.
    fn take(&mut self) -> bool {
        if !self.took {
            self.took = self
                .lend
                .as_ref()
                .is_some_and(|lend| lend.call_method0(intern!(lend.py(), "pop")).is_ok());
        }
        self.took
    }
}

impl Drop for Caller<'_> {
    /// Gives the slot back for the next run its lender starts.
    fn drop(&mut self) {
        if let (true, Some(lend)) = (self.took, &self.lend) {
            drop(lend.append(true));
        }
    }
}

/// A run's own stop flag, and its ancestors', innermost first.
type Flags = (Arc<AtomicBool>, Vec<Arc<AtomicBool>>);

/// Sets up `snapshot`, the context every task of a run copies. Every task holds a worker
/// slot or runs inside one that does, so its `HELD` records one, live until the run ends,
/// whose live flag is returned. Its `CANCELLED` is the run's stop flag, chained to the
/// one it held, so a run started inside a task of another stops when that one does.
fn prepared<'py>(
    pool: &Pool,
    snapshot: &Bound<'py, PyAny>,
    held: &Bound<'py, PyAny>,
) -> PyResult<(Bound<'py, PyList>, Flags)> {
    let py = snapshot.py();
    let variable = held_variable(py)?.bind(py);
    let (working, live) = pool.limits.holding_worker(held)?;
    let inside = snapshot.getattr(intern!(py, "run"))?;
    inside.call1((variable.getattr(intern!(py, "set"))?, working))?;
    let cancelled = cancelled_variable(py)?.bind(py);
    let parent = snapshot.call_method1(intern!(py, "get"), (cancelled, py.None()))?;
    let ancestors = parent
        .cast::<Stop>()
        .map(|parent| parent.get().chain())
        .unwrap_or_default();
    let own = Arc::new(AtomicBool::new(false));
    let flag = Stop {
        own: Arc::clone(&own),
        ancestors: ancestors.clone(),
    };
    inside.call1((
        cancelled.getattr(intern!(py, "set"))?,
        Bound::new(py, flag)?,
    ))?;
    Ok((live, (own, ancestors)))
}

/// The time `timeout` seconds from now; `ValueError` for a negative, NaN or too large one.
fn deadline_of(timeout: f64) -> PyResult<(Instant, f64)> {
    Duration::try_from_secs_f64(timeout)
        .ok()
        .and_then(|after| Instant::now().checked_add(after))
        .map(|deadline| (deadline, timeout))
        .ok_or_else(|| {
            PyValueError::new_err(format!(
                "timeout must be a finite number of seconds of at least 0, not {timeout}"
            ))
        })
}

/// `Plan.run(..., threads=)`: the run on the scheduler, its tasks taken by the calling
/// thread and `threads`' pool. With `timeout`, only the pool takes them, and the run is
/// abandoned once `timeout` seconds pass.
#[expect(
    clippy::too_many_arguments,
    reason = "`Plan.run`'s arguments, as Python passes them"
)]
pub fn run<'py>(
    slf: &Bound<'py, Plan>,
    inputs: &Bound<'py, PyDict>,
    overrides: &Bound<'py, PyDict>,
    observers: Vec<Bound<'py, ObserverBase>>,
    on_value: Option<Bound<'py, PyAny>>,
    late: Option<&Bound<'py, PyAny>>,
    threads: &Bound<'py, Threads>,
    timeout: Option<f64>,
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
    let deadline = timeout.map(deadline_of).transpose()?;
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
    // A run started on the thread of one of its driver's tasks runs inside that task,
    // which waits for it, so it runs on that task's worker slot. So does one started where
    // the context of a run's tasks reaches, on another thread, while no other such run
    // does. With a deadline, the caller runs no task: it lends the slot to the run.
    let mut caller = Caller {
        seat: Seat::Take,
        deadline,
        lend: pool.limits.lend(&held),
        took: false,
    };
    let nested = holds(&pool.limits) || caller.take();
    let lends = nested && deadline.is_some();
    if nested && !lends {
        caller.seat = Seat::Held;
    }
    let (live, stop) = prepared(pool, &snapshot, &held)?;
    let _live = Ended(live);
    let observers = SharedObservers::new(observers, on_value, late);
    let run = Arc::new(Run::new(slf, snapshot.unbind(), observers, stop, lends));
    let kwargs = Kwargs::new(py, plan, graph);
    let errors = run
        .observers
        .fire(py, &kwargs, None, |hook| hook.pre_graph());
    if std::process::id() != run.pid {
        // A `pre_graph` hook forked: the child locks nothing the parent's threads may hold.
        return Err(inherited(run.pid));
    }
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
    let mut scheduler = Scheduler::new(
        core,
        graph.core(),
        plan.layout(),
        seeded,
        options,
        &mut report,
    );
    if lends {
        scheduler.lend();
    }
    run.with_state(py, move |state| state.scheduler = Some(scheduler));
    run.report(py, None, &report);
    report.clear();
    pool.register(py, &run, deadline.is_some(), lends);
    let drove = drive(py, &run, pool, &mut caller, &mut report);
    if std::process::id() != run.pid {
        // A node forked: this is the child, whose run's other threads are gone. Nothing
        // it inherited is locked again.
        return Err(inherited(run.pid));
    }
    pool.unregister(py, &run, run.lends());
    match drove {
        Ok(last) => ended(slf, &run, last == Step::Broken),
        Err(interrupt) => {
            // A task still running may be firing a hook: `post_graph` waits for it, as
            // `ended` does. A second interrupt while it waits skips `post_graph`.
            if let Ok(busy) = run.hold(py) {
                let closing = run
                    .observers
                    .fire(py, &kwargs, None, |hook| hook.post_graph(false));
                drop((closing, busy));
            }
            Err(interrupt)
        }
    }
}

/// The end of a run whose tasks are all done, or abandoned: its outputs, or its failure,
/// once `post_graph` has fired.
fn ended<'py>(slf: &Bound<'py, Plan>, run: &Run, broke: bool) -> PyResult<Bound<'py, PyDict>> {
    let py = slf.py();
    let plan = slf.get();
    let graph = plan.graph().get();
    let core = plan.core();
    let kwargs = Kwargs::new(py, plan, graph);
    // A task the run abandoned may be firing a hook still: `post_graph` waits for it, so
    // the run's hooks never overlap, and the task fires none after it.
    let _busy = if run.abandoned.load(Ordering::SeqCst) {
        Some(run.hold(py)?)
    } else {
        None
    };
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
    let outcome = match outcome_of(scheduler.finish(core, graph.core()), failures) {
        // Stopped for its ancestor before every output was in: no failure of its own.
        Err(RunError::BrokenPlan { .. }) if run.followed.load(Ordering::Acquire) => {
            Err(RunError::Observers {
                event: "the run",
                errors: vec![stopped()],
            })
        }
        outcome => outcome,
    };
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

/// The calling thread's part of a run. It takes tasks too, unless the run has a deadline,
/// and waits in slices, checking for signals between them, until nothing is left and
/// every task's events are sent. At the deadline, on an interrupt, or once a task failed
/// with `KeyboardInterrupt`, the run abandons its running tasks instead (`abandon`), and
/// the caller closes its iterators (`close`). An error only for an interrupt while a
/// broken run waits for its tasks.
fn drive(
    py: Python<'_>,
    run: &Arc<Run>,
    pool: &Arc<Pool>,
    caller: &mut Caller<'_>,
    report: &mut Report<Shared>,
) -> PyResult<Step> {
    loop {
        let generation = pool.generation(py);
        let step = if caller.deadline.is_some() {
            observed(py, run)
        } else {
            std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                step(py, run, pool, caller.seat, report)
            }))
            .unwrap_or_else(|payload| {
                run.fail_now(py, crate::errors::panic_error(&*payload));
                Step::Worked
            })
        };
        if std::process::id() != run.pid {
            return Ok(Step::Forked);
        }
        if run.interrupted.load(Ordering::Acquire) && run.interrupted.swap(false, Ordering::AcqRel)
        {
            abandon(py, run, pool, None);
            return Ok(close(py, run, pool, report));
        }
        match step {
            Step::Worked | Step::Abandoned => {}
            Step::Forked => return Ok(step),
            Step::Broken => {
                // The tasks still running end on their own, their counts dropped by guards.
                while run.active.load(Ordering::Acquire) > 0 {
                    let generation = pool.generation(py);
                    pool.wait_slice(py, generation, SLICE);
                    py.check_signals()?;
                }
                return Ok(step);
            }
            Step::Done if run.active.load(Ordering::Acquire) == 0 => return Ok(step),
            Step::Done | Step::Wait | Step::Blocked(_) => {
                if let Step::Blocked(node) = step {
                    if let Some(error) = reentered(py, run, pool, node) {
                        // Waiting would be for good: the run fails, once nothing runs.
                        run.fail_now(py, error);
                        continue;
                    }
                    if borrowed(py, run, pool, caller) {
                        continue;
                    }
                }
                let slice = match caller.deadline {
                    None => SLICE,
                    Some((deadline, timeout)) => {
                        let left = deadline.saturating_duration_since(Instant::now());
                        if left.is_zero() {
                            let error = PyTimeoutError::new_err(format!(
                                "the run did not finish within its timeout of {timeout} s"
                            ));
                            // Keyed after every step, so a failure seen first is raised.
                            let key = OrderKey::new(&[u32::MAX]);
                            abandon(py, run, pool, Some((key, error)));
                            return Ok(close(py, run, pool, report));
                        }
                        left.min(SLICE)
                    }
                };
                pool.wait_slice(py, generation, slice);
                if let Err(error) = py.check_signals() {
                    abandon(py, run, pool, Some((OrderKey::default(), error)));
                    return Ok(close(py, run, pool, report));
                }
            }
        }
    }
}

/// What the caller of a run with a deadline, which takes no task, sees of it.
fn observed(py: Python<'_>, run: &Run) -> Step {
    if run.done.load(Ordering::Acquire) {
        return Step::Done;
    }
    let blocked = run.with_state(py, |state| {
        state.scheduler.as_ref().and_then(Scheduler::blocked)
    });
    match blocked {
        None => Step::Broken,
        Some(Some(node)) => Step::Blocked(node),
        Some(None) => Step::Wait,
    }
}

/// Takes the worker slot the run's context lends, for a run blocked with nothing in
/// flight while every worker slot is held: one of them is its ancestor's, which waits for
/// it. `true` when it took it, for its caller's steps or, with a deadline, its lent seat.
fn borrowed(py: Python<'_>, run: &Run, pool: &Arc<Pool>, caller: &mut Caller<'_>) -> bool {
    let full = pool.limits.core.held(Resource::Worker) >= pool.max_workers;
    if caller.seat != Seat::Take || run.lends() || !full || !caller.take() {
        return false;
    }
    if caller.deadline.is_some() {
        run.with_state(py, |state| {
            if let Some(scheduler) = state.scheduler.as_mut() {
                scheduler.lend();
            }
        });
        run.lends.store(true, Ordering::Release);
        pool.lend(py);
    } else {
        caller.seat = Seat::Held;
    }
    true
}

/// Stops the run and abandons its running tasks (O27), recording `failure` first. Their
/// worker, node and group slots are given back now, with a wake, and the pool starts a
/// thread for each, since each keeps its own until its call returns. A task completing
/// after this drops its outcome (`perform`).
fn abandon(py: Python<'_>, run: &Run, pool: &Arc<Pool>, failure: Option<(OrderKey, PyErr)>) {
    let abandoned = &run.abandoned;
    let taken = run.with_state(py, move |state| {
        if let Some((key, error)) = failure {
            let failure = RunError::Observers {
                event: "the run",
                errors: vec![error],
            };
            state.failures.push((key, Failure::Run(failure)));
        }
        if let Some(scheduler) = state.scheduler.as_mut() {
            scheduler.stop();
        }
        abandoned.store(true, Ordering::SeqCst);
        state.abandons = state.abandons.wrapping_add(1);
        let taken = (
            state.running,
            state.taking,
            std::mem::take(&mut state.limited),
        );
        state.running = 0;
        state.taking = 0;
        taken
    });
    run.stop.store(true, Ordering::Release);
    let Some((running, taking, limited)) = taken else {
        return;
    };
    for _ in 0..taking {
        pool.limits.core.release(None, Seat::Take);
    }
    for held in limited {
        if held.held.swap(false, Ordering::SeqCst) {
            pool.limits.release(py, held.node);
        }
    }
    pool.notify(py);
    pool.replace(py, running);
}

/// The caller's finishes of a stopped run's iterators that no pull holds, run attached
/// before it raises. `Forked` in the child of a generator's close that forked.
fn close(py: Python<'_>, run: &Arc<Run>, pool: &Pool, report: &mut Report<Shared>) -> Step {
    loop {
        let step = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            step(py, run, pool, Seat::Threadless, report)
        }))
        .unwrap_or_else(|payload| {
            run.fail_now(py, crate::errors::panic_error(&*payload));
            Step::Worked
        });
        if std::process::id() != run.pid {
            return Step::Forked;
        }
        match step {
            Step::Worked => {}
            Step::Broken => return step,
            _ => return Step::Done,
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

/// Armed by `_core.arm_state_panic()`: the next step of any run panics holding its
/// scheduler's lock, which poisons it and breaks the run.
#[cfg(feature = "test-hooks")]
static STATE_ARMED: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);

/// Makes the next step of any run panic while it holds the run's scheduler lock.
#[cfg(feature = "test-hooks")]
#[pyfunction]
pub fn arm_state_panic() {
    STATE_ARMED.store(true, Ordering::Release);
}

/// The armed panic of `arm_state_panic`, called holding a run's scheduler lock.
#[cfg(feature = "test-hooks")]
fn state_armed() {
    assert!(
        !STATE_ARMED.swap(false, Ordering::AcqRel),
        "deliberate panic holding a run's scheduler lock"
    );
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
