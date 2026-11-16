//! A driver's pool of worker threads, shared by its parallel runs.
//!
//! Threads start at the first parallel run, `max_workers - 1` of them, since a run's
//! calling thread works too. Each is a daemon `threading.Thread` named
//! `fnloom-worker-<n>`, whose target is the worker loop: so it is attached once, for its
//! whole life, by `threading`, does every wait inside `detach`, and leaves the way any
//! Python thread does. (A thread of our own, attached through `PyGILState_Ensure`, gets a
//! `_DummyThread` once Python code asks for `threading.current_thread()`; on 3.14t, many
//! of those ending at once deadlocked in the dummy's cleanup at interpreter exit.)
//! Threads leave when the pool is stopped: by `Threads.close`, when the `Threads` is
//! collected, or at interpreter exit.
//!
//! One `Mutex` and `Condvar` guard the pool's state and wake every thread waiting on any
//! of the driver's runs: a task's completion, a new run and the close each bump its
//! generation, so a waiter that read the generation before it looked for work never
//! misses the change.

use std::panic::{self, AssertUnwindSafe};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Condvar, Mutex, MutexGuard, PoisonError, TryLockError};
use std::time::{Duration, Instant};

use fnloom_core::limits::Limits as CoreLimits;
use fnloom_core::schedule::{Report, Seat};
use pyo3::exceptions::PyValueError;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyCFunction, PyDict};

use crate::errors::panic_error;
use crate::limits::Limits;
use crate::parallel::{self, Run, Step};

/// Pool threads alive in this process, started and not yet ended.
static LIVE: AtomicUsize = AtomicUsize::new(0);
/// The number the next pool thread is named with.
static NEXT: AtomicUsize = AtomicUsize::new(1);

/// How long closing a pool waits for its idle threads, in all.
const CLOSING: Duration = Duration::from_secs(1);
/// How long an idle thread sleeps before it looks at the stop flag again, should a stop
/// that could not take the lock have woken no one.
const IDLE: Duration = Duration::from_secs(1);

/// A driver's pool: its caps, its threads and the runs that want work.
pub struct Pool {
    /// The driver's caps, which every run's scheduler is granted slots by.
    pub limits: Arc<CoreLimits>,
    /// The threads it starts: `max_workers - 1`.
    size: usize,
    /// `max_workers`.
    pub max_workers: usize,
    state: Mutex<State>,
    wake: Condvar,
    /// Set once, by `stop`: no thread starts, and every thread leaves.
    stopping: AtomicBool,
    /// The process that made it: a forked child never locks what it inherited.
    pub pid: u32,
}

/// What the pool's lock guards.
struct State {
    /// Bumped on every change a waiter may wait for.
    generation: u64,
    /// The runs that want work, oldest first.
    runs: Vec<Arc<Run>>,
    /// Threads started, and threads ended.
    started: usize,
    exited: usize,
}

impl Pool {
    /// Runs `with` on the state, by the safety rules' rule 1: `try_lock` while attached,
    /// else the whole critical section inside `detach`. A poisoned lock is recovered, since
    /// the state is consistent at every unlock.
    fn with_state<R: Send>(&self, py: Python<'_>, with: impl FnOnce(&mut State) -> R + Send) -> R {
        match self.state.try_lock() {
            Ok(mut state) => return with(&mut state),
            Err(TryLockError::Poisoned(poisoned)) => return with(&mut poisoned.into_inner()),
            Err(TryLockError::WouldBlock) => {}
        }
        py.detach(|| with(&mut self.locked()))
    }

    fn locked(&self) -> MutexGuard<'_, State> {
        self.state.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// The current generation, which `wait` and `wait_slice` wait to change.
    pub fn generation(&self, py: Python<'_>) -> u64 {
        self.with_state(py, |state| state.generation)
    }

    /// Wakes every waiter: something they may wait for changed.
    pub fn notify(&self, py: Python<'_>) {
        self.with_state(py, |state| state.generation += 1);
        self.wake.notify_all();
    }

    /// Waits, detached, until the generation is no longer `generation`.
    fn wait(&self, py: Python<'_>, generation: u64) {
        py.detach(|| {
            let mut state = self.locked();
            while state.generation == generation && !self.stopping.load(Ordering::Acquire) {
                state = self
                    .wake
                    .wait_timeout(state, IDLE)
                    .unwrap_or_else(PoisonError::into_inner)
                    .0;
            }
        });
    }

    /// Waits, detached, at most `slice`, until the generation is no longer `generation`:
    /// a calling thread's wait, which checks for signals between slices.
    pub fn wait_slice(&self, py: Python<'_>, generation: u64, slice: Duration) {
        py.detach(|| {
            let state = self.locked();
            if state.generation == generation {
                drop(
                    self.wake
                        .wait_timeout(state, slice)
                        .unwrap_or_else(PoisonError::into_inner),
                );
            }
        });
    }

    /// Adds `run` to the runs that want work, and starts the threads if none has started.
    pub fn register(self: &Arc<Self>, py: Python<'_>, run: &Arc<Run>) {
        let run = Arc::clone(run);
        let (size, stopping) = (self.size, self.stopping.load(Ordering::Acquire));
        let wanted = self.with_state(py, move |state| {
            state.runs.push(run);
            state.generation += 1;
            let wanted = if stopping { 0 } else { size - state.started };
            state.started += wanted;
            wanted
        });
        self.wake.notify_all();
        for _ in 0..wanted {
            if self.spawn(py).is_err() {
                // The run still runs, on the threads there are and its caller.
                self.with_state(py, |state| state.started -= 1);
            }
        }
    }

    /// Starts one worker: a daemon `threading.Thread` running `work`.
    fn spawn(self: &Arc<Self>, py: Python<'_>) -> PyResult<()> {
        let number = NEXT.fetch_add(1, Ordering::Relaxed);
        let pool = Arc::clone(self);
        let target = PyCFunction::new_closure(py, None, None, move |args, _| {
            work(args.py(), &pool);
        })?;
        let kwargs = PyDict::new(py);
        kwargs.set_item(intern!(py, "target"), target)?;
        kwargs.set_item(intern!(py, "name"), format!("fnloom-worker-{number}"))?;
        kwargs.set_item(intern!(py, "daemon"), true)?;
        let thread = py
            .import(intern!(py, "threading"))?
            .getattr(intern!(py, "Thread"))?
            .call((), Some(&kwargs))?;
        LIVE.fetch_add(1, Ordering::AcqRel);
        if let Err(error) = thread.call_method0(intern!(py, "start")) {
            LIVE.fetch_sub(1, Ordering::AcqRel);
            return Err(error);
        }
        Ok(())
    }

    /// Removes `run` from the runs that want work. The removed handle is dropped here,
    /// attached, never inside the lock.
    pub fn unregister(&self, py: Python<'_>, run: &Arc<Run>) {
        let run = Arc::clone(run);
        let removed: Vec<Arc<Run>> = self.with_state(py, move |state| {
            let (gone, kept) = std::mem::take(&mut state.runs)
                .into_iter()
                .partition(|other| Arc::ptr_eq(other, &run));
            state.runs = kept;
            let mut gone: Vec<Arc<Run>> = gone;
            gone.push(run);
            gone
        });
        drop(removed);
    }

    /// Tells the threads to stop, waiting for nothing: idle ones leave at once, busy ones
    /// once their task is done. Never blocks, so a collected pool may call it during a
    /// garbage collection.
    fn stop(&self) {
        self.stopping.store(true, Ordering::Release);
        if let Ok(mut state) = self.state.try_lock() {
            state.generation += 1;
        }
        self.wake.notify_all();
    }

    /// Stops the threads, then waits, detached, at most a second for the idle ones to
    /// leave. A busy thread is never waited for past that. Idempotent.
    pub fn close(&self, py: Python<'_>) {
        self.stop();
        self.with_state(py, |state| state.generation += 1);
        self.wake.notify_all();
        py.detach(|| {
            let deadline = Instant::now() + CLOSING;
            let mut state = self.locked();
            while state.exited < state.started {
                let now = Instant::now();
                if now >= deadline {
                    break;
                }
                state = self
                    .wake
                    .wait_timeout(state, deadline - now)
                    .unwrap_or_else(PoisonError::into_inner)
                    .0;
            }
        });
    }

    /// The runs that want work, the generation, and whether the pool is stopping.
    fn snapshot(&self, py: Python<'_>) -> (u64, bool, Vec<Arc<Run>>) {
        let (generation, runs) =
            self.with_state(py, |state| (state.generation, state.runs.clone()));
        (generation, self.stopping.load(Ordering::Acquire), runs)
    }

    /// A thread is leaving.
    fn exited(&self, py: Python<'_>) {
        self.with_state(py, |state| state.exited += 1);
        self.wake.notify_all();
    }
}

/// A pool thread's body, attached by `threading` for the thread's whole life: it takes
/// tasks from any run that wants work, and waits detached when none does.
fn work(py: Python<'_>, pool: &Arc<Pool>) {
    let forked = (|| {
        let mut report = Report::default();
        loop {
            let (generation, stopping, runs) = pool.snapshot(py);
            if stopping {
                return false;
            }
            let mut worked = false;
            for run in &runs {
                let step = panic::catch_unwind(AssertUnwindSafe(|| {
                    parallel::step(py, run, pool, Seat::Take, &mut report)
                }))
                .unwrap_or_else(|payload| {
                    run.fail_now(py, panic_error(&*payload));
                    Step::Worked
                });
                // A node forked: this child's one thread is this one, with no caller to
                // return to. It ends without touching any lock it inherited.
                if std::process::id() != pool.pid {
                    return true;
                }
                if step == Step::Worked {
                    worked = true;
                    break;
                }
            }
            drop(runs);
            if !worked {
                pool.wait(py, generation);
            }
        }
    })();
    if !forked {
        pool.exited(py);
    }
    LIVE.fetch_sub(1, Ordering::AcqRel);
}

/// A driver's pool of worker threads, for `Plan.run(..., threads=)`.
///
/// Holds `max_workers - 1` threads, started at the first parallel run, since the run's
/// calling thread works too. `close()` stops them, waiting a second at most for the idle
/// ones to leave; a collected pool stops them without waiting.
#[pyclass(frozen, weakref, module = "fnloom._core")]
pub struct Threads {
    pool: Arc<Pool>,
}

#[pymethods]
impl Threads {
    /// A pool of `max_workers - 1` threads, granted slots by `limits`.
    #[new]
    fn new(limits: &Bound<'_, Limits>, max_workers: usize) -> PyResult<Self> {
        if max_workers == 0 {
            return Err(PyValueError::new_err(
                "max_workers must be at least 1, not 0",
            ));
        }
        Ok(Self {
            pool: Arc::new(Pool {
                limits: Arc::clone(limits.get().core()),
                size: max_workers - 1,
                max_workers,
                state: Mutex::new(State {
                    generation: 0,
                    runs: Vec::new(),
                    started: 0,
                    exited: 0,
                }),
                wake: Condvar::new(),
                stopping: AtomicBool::new(false),
                pid: std::process::id(),
            }),
        })
    }

    /// Stops the pool's threads; idempotent. A run still going finishes on its caller.
    fn close(&self, py: Python<'_>) {
        if self.pool.pid == std::process::id() {
            self.pool.close(py);
        }
    }

    /// `max_workers`.
    #[getter]
    fn max_workers(&self) -> usize {
        self.pool.max_workers
    }
}

impl Threads {
    /// The pool.
    pub const fn pool(&self) -> &Arc<Pool> {
        &self.pool
    }
}

impl Drop for Threads {
    /// Tells the threads to stop, and waits for none: a collector may run this inside a
    /// garbage collection, which another thread's `gc.collect()` would then skip.
    fn drop(&mut self) {
        // Inherited across a fork: its locks may be held by threads the child doesn't
        // have, so nothing of it is touched.
        if self.pool.pid == std::process::id() {
            self.pool.stop();
        }
    }
}

/// How many pool threads are alive in this process, started and not yet ended.
#[cfg(feature = "test-hooks")]
#[pyfunction]
pub fn live_workers() -> usize {
    LIVE.load(Ordering::Acquire)
}
