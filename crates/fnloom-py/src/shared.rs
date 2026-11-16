//! A parallel run's observers, kept as `Py` so any worker can fire them.
//!
//! `PyObserver` borrows a thread's attachment, so it is not `Send`: the run keeps each
//! observer as a `Py` with its flags, and binds one per event, attached, under the run's
//! busy flag. `on_value` and `late` are kept the same way, and called after the flag is
//! cleared.

use fnloom_core::plan::Plan as CorePlan;
use fnloom_core::run::Observer;
use pyo3::prelude::*;

use crate::graph::Graph;
use crate::keys::Keys;
use crate::observer::{Flags, ObserverBase, PyObserver};
use crate::plan::{Kwargs, LateHook, Sink, Value, ValueHook};

/// A parallel run's observers, `on_value` and `late`.
pub struct SharedObservers {
    observers: Vec<(Py<ObserverBase>, Flags)>,
    on_value: Option<Py<PyAny>>,
    late: Option<Py<PyAny>>,
}

impl SharedObservers {
    /// Keeps a run's observers, each with the hooks its class overrides.
    pub fn new(
        observers: Vec<Bound<'_, ObserverBase>>,
        on_value: Option<Bound<'_, PyAny>>,
        late: Option<&Bound<'_, PyAny>>,
    ) -> Self {
        Self {
            observers: observers
                .into_iter()
                .map(|observer| {
                    let flags = Flags::of(&observer);
                    (observer.unbind(), flags)
                })
                .collect(),
            on_value: on_value.map(Bound::unbind),
            late: late.map(|late| late.clone().unbind()),
        }
    }

    /// Whether the run has observers, so its events need the busy flag.
    pub fn any(&self) -> bool {
        !self.observers.is_empty()
    }

    /// The run's late keys, if it is cached.
    pub fn late<'py>(&self, py: Python<'py>) -> Option<Bound<'py, PyAny>> {
        self.late.as_ref().map(|late| late.bind(py).clone())
    }

    /// Sends `event` to every observer, in order, each bound for this event, with
    /// `kwargs` lending a node's dict to `pre_node`; the errors they raised, in order.
    pub fn fire<'py>(
        &self,
        py: Python<'py>,
        kwargs: &Kwargs<'py, '_>,
        mut event: impl FnMut(&mut dyn Observer<Value<'py>, PyErr>) -> PyResult<()>,
    ) -> Vec<PyErr> {
        self.observers
            .iter()
            .filter_map(|(observer, flags)| {
                let mut adapter = PyObserver::flagged(observer.bind(py).clone(), kwargs, *flags);
                event(&mut adapter).err()
            })
            .collect()
    }

    /// Hands a computed node's outcome to `on_value` and `late.store`, as `Plan::run`'s
    /// last observers; the errors they raised.
    pub fn stored<'py>(
        &self,
        py: Python<'py>,
        plan: &CorePlan,
        graph: &Graph,
        node: &str,
        branch: &[usize],
        outcome: Result<&Value<'py>, &PyErr>,
    ) -> Vec<PyErr> {
        let mut errors = Vec::new();
        if let Some(on_value) = &self.on_value {
            let callback = on_value.bind(py).clone();
            let mut hook = ValueHook {
                sink: match callback.cast_into::<Keys>() {
                    Ok(keys) => Sink::Store(keys),
                    Err(error) => Sink::Call(error.into_inner()),
                },
                graph,
                keywords: graph.keywords(),
            };
            errors.extend(hook.post_node_in(node, branch, outcome).err());
        }
        if let Some(late) = &self.late {
            let late = late.bind(py);
            let mut hook = LateHook {
                late,
                plan,
                graph,
                keywords: graph.keywords(),
            };
            errors.extend(hook.post_node_in(node, branch, outcome).err());
        }
        errors
    }
}
