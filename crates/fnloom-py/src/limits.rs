//! A driver's limits: the core's atomic caps, shared by every run of the driver, and a
//! wake for whoever waits on a slot.
//!
//! A node's own slot and its group's are released as soon as its call returns, before
//! its hooks or store writes, and the release wakes every waiter: a sequential run's
//! grant, waiting on this wake's `Condvar`, and each async waker registered, called once,
//! attached and after the lock. A parallel run waits on its pool instead, which the
//! release notifies too (`parallel`).
//!
//! The slots held are recorded in `fnloom._threads.HELD`, a context variable of
//! `(limits, slot, live)` entries, so a run started inside a task sees them as its
//! ancestors', on any thread its context reaches and at any depth:
//!
//! - a limited call's node slot, around the call: a run that needs a slot whose every unit
//!   its ancestors hold is refused at once instead of waiting for good;
//! - a parallel run's worker slots, in the context its tasks copy: a run started from one
//!   takes its caller's tasks on the slot its ancestor holds (`Seat::Held`), one such run
//!   at a time. The entry's fourth item, `lend`, is `[True]` while no run has taken it: a
//!   run takes it with `lend.pop()`, atomic on either build, and gives it back at its end.
//!
//! `live` is `[True]` until the slot is given back, so a thread or task that inherited the
//! context and outlived the slot counts it no more.

use std::collections::{BTreeMap, HashMap};
use std::num::NonZeroUsize;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Condvar, Mutex, MutexGuard, PoisonError, TryLockError};
use std::time::Duration;

use fnloom_core::graph::Graph as CoreGraph;
use fnloom_core::limits::{Grant, Limits as CoreLimits};
use fnloom_core::node::NodeId;
use fnloom_core::schedule::Seat;
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyList, PyTuple};

use crate::graph::Graph;

/// How long a sequential grant waits before it checks for signals again.
const SLICE: Duration = Duration::from_millis(50);

/// The slot a `HELD` entry names for a parallel run's worker slots: no node's index.
const WORKER: usize = usize::MAX;

/// A node's own cap and its group, for the re-entrancy check and its messages.
struct Capped {
    name: String,
    cap: Option<usize>,
    group: Option<u32>,
}

/// What a driver's runs share of its limits: the caps, the node and group tables, and
/// the wake.
pub struct Caps {
    /// The atomic caps every grant takes and gives back.
    pub core: CoreLimits,
    /// By node id, the node's cap and group; `None` for a node with neither.
    nodes: Vec<Option<Capped>>,
    /// Each group's name and size, by group id.
    groups: Vec<(String, usize)>,
    /// The process that made it: a forked child never touches what it inherited.
    pid: u32,
    /// Bumped by every release, which a waiter waits to change.
    wake: Mutex<u64>,
    released: Condvar,
    /// Async wakers, each called once at the next release.
    wakers: Mutex<Vec<Py<PyAny>>>,
    /// How many wakers are registered, so a release with none takes no lock.
    waiting: AtomicUsize,
}

/// Releases a sequential grant's node and group slots when dropped, a panic included.
struct Granted<'a, 'py> {
    caps: &'a Caps,
    node: NodeId,
    py: Python<'py>,
}

impl Drop for Granted<'_, '_> {
    fn drop(&mut self) {
        // A call that forked returns here in the child too, which locks nothing it inherited.
        if self.caps.pid == std::process::id() {
            self.caps.release(self.py, self.node);
        }
    }
}

/// The `RuntimeError` of a run inherited across a fork, started in process `pid`.
pub fn inherited(pid: u32) -> PyErr {
    PyRuntimeError::new_err(format!(
        "this run was started in process {pid}; a run doesn't survive os.fork()"
    ))
}

/// The error of a task its run abandoned, which no one sees: its outcome is dropped.
pub fn abandoned() -> PyErr {
    PyRuntimeError::new_err("the run abandoned this task")
}

/// `fnloom._threads.HELD`, the context variable of the node and group slots the running
/// tasks hold.
pub fn held_variable(py: Python<'_>) -> PyResult<&'static Py<PyAny>> {
    static HELD: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
    HELD.get_or_try_init(py, || {
        Ok(py
            .import(intern!(py, "fnloom._threads"))?
            .getattr(intern!(py, "HELD"))?
            .unbind())
    })
}

impl Caps {
    /// Whether `node` has a cap of its own or a group.
    pub fn limited(&self, node: NodeId) -> bool {
        self.nodes.get(node.index()).is_some_and(Option::is_some)
    }

    /// The process the caps were made in.
    pub const fn pid(&self) -> u32 {
        self.pid
    }

    /// `held`, the `HELD` value of the calling context, with an entry for `node`'s slot
    /// added, and its live flag, which the caller clears as the slot is given back.
    pub fn holding<'py>(
        &self,
        held: &Bound<'py, PyAny>,
        node: NodeId,
    ) -> PyResult<(Bound<'py, PyAny>, Bound<'py, PyList>)> {
        self.entered(held, node.index())
    }

    /// `held` with an entry for the worker slots a parallel run's tasks hold, and its live
    /// flag, which the run clears as it ends. The entry carries `lend`, `[True]`, which
    /// one run started from the run's context at a time takes to run on that slot.
    pub fn holding_worker<'py>(
        &self,
        held: &Bound<'py, PyAny>,
    ) -> PyResult<(Bound<'py, PyAny>, Bound<'py, PyList>)> {
        let py = held.py();
        let live = PyList::new(py, [true])?;
        let lend = PyList::new(py, [true])?;
        let entry = (self.id(), WORKER, live.clone(), lend).into_pyobject(py)?;
        Ok((held.add(PyTuple::new(py, [entry])?)?, live))
    }

    fn entered<'py>(
        &self,
        held: &Bound<'py, PyAny>,
        slot: usize,
    ) -> PyResult<(Bound<'py, PyAny>, Bound<'py, PyList>)> {
        let py = held.py();
        let live = PyList::new(py, [true])?;
        let entry = (self.id(), slot, live.clone()).into_pyobject(py)?;
        Ok((held.add(PyTuple::new(py, [entry])?)?, live))
    }

    /// The identity entries name these caps by.
    fn id(&self) -> usize {
        std::ptr::from_ref(self) as usize
    }

    /// The live entries of these caps in `held`, a `HELD` value, as their slots, each with
    /// its entry.
    fn live<'py>(
        &self,
        held: &Bound<'py, PyAny>,
    ) -> impl Iterator<Item = (usize, Bound<'py, PyAny>)> + use<'py> {
        let me = self.id();
        held.try_iter()
            .into_iter()
            .flatten()
            .flatten()
            .filter_map(move |entry| {
                let caps = entry.get_item(0).ok()?.extract::<usize>().ok()?;
                let slot = entry.get_item(1).ok()?.extract::<usize>().ok()?;
                let live = entry.get_item(2).ok()?.get_item(0).ok()?.is_truthy().ok()?;
                (caps == me && live).then_some((slot, entry))
            })
    }

    /// The `lend` list of the innermost live worker entry of these caps in `held`, a `HELD`
    /// value: a run started there runs inside a task that holds a worker slot of them, and
    /// may take that slot for its own tasks. `None` when there is no such entry.
    pub fn lend<'py>(&self, held: &Bound<'py, PyAny>) -> Option<Bound<'py, PyList>> {
        self.live(held)
            .filter(|(slot, _)| *slot == WORKER)
            .last()
            .and_then(|(_, entry)| entry.get_item(3).ok()?.cast_into::<PyList>().ok())
    }

    /// Takes `node`'s slots back for a task that gave them back while it waited for its
    /// run's hooks: waits, detached, for a release while refused. Its ancestors can't hold
    /// every unit, since the task was granted them once in this run. Gives up once `gone`
    /// is set, as its run abandons it, and in a forked child, which locks nothing it
    /// inherited.
    pub fn retake(&self, py: Python<'_>, node: NodeId, gone: &AtomicBool) -> PyResult<()> {
        if self.pid != std::process::id() {
            return Err(inherited(self.pid));
        }
        loop {
            if gone.load(Ordering::SeqCst) {
                return Err(abandoned());
            }
            let seen = self.generation(py);
            if self.core.try_start(Some(node), Seat::Threadless) {
                return Ok(());
            }
            self.wait_slice(py, seen);
            py.check_signals()?;
        }
    }

    fn wake(&self) -> MutexGuard<'_, u64> {
        self.wake.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// The release count, which `wait_slice` waits to change.
    fn generation(&self, py: Python<'_>) -> u64 {
        match self.wake.try_lock() {
            Ok(count) => *count,
            Err(TryLockError::Poisoned(poisoned)) => *poisoned.into_inner(),
            Err(TryLockError::WouldBlock) => py.detach(|| *self.wake()),
        }
    }

    /// Waits, detached, at most `slice`, until the release count is no longer `seen`.
    fn wait_slice(&self, py: Python<'_>, seen: u64) {
        py.detach(|| {
            let count = self.wake();
            if *count == seen {
                drop(
                    self.released
                        .wait_timeout(count, SLICE)
                        .unwrap_or_else(PoisonError::into_inner),
                );
            }
        });
    }

    /// Gives back `node`'s own slot and its group's, and wakes every waiter: sequential
    /// grants, then each async waker, once, attached and after the lock. A waker's
    /// exception is dropped, so it never fails the run whose release called it.
    pub fn release(&self, py: Python<'_>, node: NodeId) {
        self.core.release(Some(node), Seat::Threadless);
        match self.wake.try_lock() {
            Ok(mut count) => *count = count.wrapping_add(1),
            Err(TryLockError::Poisoned(poisoned)) => {
                let mut count = poisoned.into_inner();
                *count = count.wrapping_add(1);
            }
            Err(TryLockError::WouldBlock) => py.detach(|| {
                let mut count = self.wake();
                *count = count.wrapping_add(1);
            }),
        }
        self.released.notify_all();
        if self.waiting.load(Ordering::Acquire) == 0 {
            return;
        }
        let wakers = self.with_wakers(py, |wakers| {
            self.waiting.store(0, Ordering::Release);
            std::mem::take(wakers)
        });
        for waker in wakers {
            drop(waker.bind(py).call0());
        }
    }

    /// Runs `with` on the wakers by rule 1: tried attached, else locked detached.
    fn with_wakers<R: Send>(
        &self,
        py: Python<'_>,
        with: impl FnOnce(&mut Vec<Py<PyAny>>) -> R + Send,
    ) -> R {
        match self.wakers.try_lock() {
            Ok(mut wakers) => return with(&mut wakers),
            Err(TryLockError::Poisoned(poisoned)) => return with(&mut poisoned.into_inner()),
            Err(TryLockError::WouldBlock) => {}
        }
        py.detach(|| with(&mut self.wakers.lock().unwrap_or_else(PoisonError::into_inner)))
    }

    /// The `RuntimeError` for a run that needs a slot of `node` whose every unit its
    /// ancestors hold, by `held`, the `HELD` value the run started with; `None` when they
    /// leave one, so waiting may end.
    pub fn reentered(&self, node: NodeId, held: Option<&Bound<'_, PyAny>>) -> Option<PyErr> {
        let wanted = self.nodes.get(node.index())?.as_ref()?;
        let (mut own, mut grouped) = (0, 0);
        for (index, _) in self.live(held?) {
            if index == node.index() {
                own += 1;
            }
            let group = self
                .nodes
                .get(index)
                .and_then(Option::as_ref)
                .and_then(|held| held.group);
            if group.is_some() && group == wanted.group {
                grouped += 1;
            }
        }
        if let Some((group, size)) = wanted
            .group
            .and_then(|group| self.groups.get(group as usize))
        {
            if grouped >= *size {
                return Some(PyRuntimeError::new_err(format!(
                    "'{}' needs group '{group}', but every slot of it is held by the runs \
                     that called this one",
                    wanted.name
                )));
            }
        }
        match wanted.cap {
            Some(cap) if own >= cap => Some(PyRuntimeError::new_err(format!(
                "'{}' is limited to {cap} call{} at a time, and the runs that called this \
                 one hold {}",
                wanted.name,
                if cap == 1 { "" } else { "s" },
                if cap == 1 { "it" } else { "them" },
            ))),
            _ => None,
        }
    }

    /// Takes `node`'s slots for a call on the calling thread, outside any pool: waits,
    /// detached, for a release while refused, checking for signals every 50 ms, unless the
    /// runs that called this one hold every unit, which is a `RuntimeError` at once.
    fn grant(&self, py: Python<'_>, node: NodeId, held: &Bound<'_, PyAny>) -> PyResult<()> {
        if self.pid != std::process::id() {
            return Err(inherited(self.pid));
        }
        let mut checked = false;
        loop {
            let seen = self.generation(py);
            if self.core.try_start(Some(node), Seat::Threadless) {
                return Ok(());
            }
            if !checked {
                if let Some(error) = self.reentered(node, Some(held)) {
                    return Err(error);
                }
                checked = true;
            }
            self.wait_slice(py, seen);
            py.check_signals()?;
        }
    }

    /// Runs `call`, a limited node's call on the calling thread, inside its grant: with
    /// `HELD` recording its slot, which is released, waking every waiter, as soon as it
    /// returns.
    pub fn around<T>(
        &self,
        py: Python<'_>,
        node: NodeId,
        call: impl FnOnce() -> PyResult<T>,
    ) -> PyResult<T> {
        let variable = held_variable(py)?.bind(py);
        let held = variable.call_method0(intern!(py, "get"))?;
        self.grant(py, node, &held)?;
        let granted = Granted {
            caps: self,
            node,
            py,
        };
        let (holding, live) = self.holding(&held, node)?;
        let token = variable.call_method1(intern!(py, "set"), (holding,))?;
        let result = call();
        drop(granted);
        let cleared = live.set_item(0, false);
        let reset = variable.call_method1(intern!(py, "reset"), (token,));
        cleared?;
        let value = result?;
        reset.map(|_| value)
    }
}

/// A driver's caps: workers, each node's own and each group's, shared by every run of
/// the driver, with the wake a release sends.
///
/// `max_workers` caps the tasks that hold a worker slot, across every run of the driver;
/// `None` caps none. `node_caps` caps a node's concurrent calls, `node_groups` puts a node
/// in a group, and `group_caps` sizes each group. Every cap is at least 1, and every group
/// a node is in has a size.
#[pyclass(frozen, module = "fnloom._core")]
pub struct Limits {
    caps: Arc<Caps>,
}

/// The entry of node `name` in `nodes`, made on first use; `ValueError` for no node.
fn capped<'a, P>(
    nodes: &'a mut [Option<Capped>],
    graph: &CoreGraph<P>,
    name: &str,
) -> PyResult<&'a mut Capped> {
    let slot = graph
        .id_of(name)
        .and_then(|id| nodes.get_mut(id.index()))
        .ok_or_else(|| PyValueError::new_err(format!("limits name '{name}', which is no node")))?;
    Ok(slot.get_or_insert_with(|| Capped {
        name: name.to_owned(),
        cap: None,
        group: None,
    }))
}

fn at_least_one(count: usize, what: &str) -> PyResult<NonZeroUsize> {
    NonZeroUsize::new(count)
        .ok_or_else(|| PyValueError::new_err(format!("{what} must be at least 1, not 0")))
}

#[pymethods]
impl Limits {
    /// The caps of a driver over `graph`.
    #[new]
    #[pyo3(signature = (graph, max_workers = None, node_caps = None, node_groups = None, group_caps = None))]
    fn new(
        graph: &Bound<'_, Graph>,
        max_workers: Option<usize>,
        node_caps: Option<HashMap<String, usize>>,
        node_groups: Option<HashMap<String, String>>,
        group_caps: Option<BTreeMap<String, usize>>,
    ) -> PyResult<Self> {
        let core = graph.get().core();
        let workers = max_workers
            .map(|count| at_least_one(count, "max_workers"))
            .transpose()?;
        let groups: Vec<(String, usize)> = group_caps.unwrap_or_default().into_iter().collect();
        let sizes = groups
            .iter()
            .map(|(name, size)| at_least_one(*size, &format!("group '{name}'")))
            .collect::<PyResult<Vec<_>>>()?;
        let mut nodes: Vec<Option<Capped>> = (0..core.nodes().len()).map(|_| None).collect();
        for (name, cap) in node_caps.unwrap_or_default() {
            at_least_one(cap, &format!("the limit of '{name}'"))?;
            capped(&mut nodes, core, &name)?.cap = Some(cap);
        }
        for (name, group) in node_groups.unwrap_or_default() {
            let id = groups
                .iter()
                .position(|(known, _)| *known == group)
                .ok_or_else(|| {
                    PyValueError::new_err(format!("group '{group}' of '{name}' has no size"))
                })?;
            capped(&mut nodes, core, &name)?.group = Some(u32::try_from(id).unwrap_or(u32::MAX));
        }
        let caps = nodes
            .iter()
            .map(|node| {
                node.as_ref()
                    .and_then(|node| node.cap)
                    .and_then(NonZeroUsize::new)
            })
            .collect();
        let groups_of = nodes
            .iter()
            .map(|node| node.as_ref().and_then(|node| node.group))
            .collect();
        Ok(Self {
            caps: Arc::new(Caps {
                core: CoreLimits::new(workers, caps, groups_of, sizes),
                nodes,
                groups,
                pid: std::process::id(),
                wake: Mutex::new(0),
                released: Condvar::new(),
                wakers: Mutex::new(Vec::new()),
                waiting: AtomicUsize::new(0),
            }),
        })
    }

    /// Registers `waker`, called once, with no arguments, at the next release of a node or
    /// group slot. An exception it raises is dropped.
    fn add_waker(&self, py: Python<'_>, waker: Bound<'_, PyAny>) {
        let waker = waker.unbind();
        self.caps.with_wakers(py, move |wakers| {
            wakers.push(waker);
            self.caps.waiting.store(wakers.len(), Ordering::Release);
        });
    }
}

impl Limits {
    /// The caps, shared with the pool.
    pub const fn caps(&self) -> &Arc<Caps> {
        &self.caps
    }
}

/// How many units of `limits`' caps are held now: worker slots, then each node's own cap
/// and each group's, by name, for the tests of abandoned calls.
#[cfg(feature = "test-hooks")]
#[pyfunction]
pub fn held_slots(
    limits: &Bound<'_, Limits>,
) -> (usize, BTreeMap<String, usize>, BTreeMap<String, usize>) {
    use fnloom_core::limits::Resource;
    let caps = limits.get().caps();
    let nodes = caps
        .nodes
        .iter()
        .zip(0..)
        .filter_map(|(node, id)| {
            let node = node.as_ref().filter(|node| node.cap.is_some())?;
            Some((
                node.name.clone(),
                caps.core.held(Resource::Node(NodeId(id))),
            ))
        })
        .collect();
    let groups = caps
        .groups
        .iter()
        .zip(0..)
        .map(|((name, _), id)| (name.clone(), caps.core.held(Resource::Group(id))))
        .collect();
    (caps.core.held(Resource::Worker), nodes, groups)
}
