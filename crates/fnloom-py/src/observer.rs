//! Run observers: the subclassable Python base class and its adapter to the core trait.

use fnloom_core::run::Observer;
use pyo3::call::PyCallArgs;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyString, PyTuple};

use crate::errors::caught;

/// Base class for run observers; subclass it and override any of the four hooks.
///
/// Every hook is a no-op here. An exception raised by a hook aborts the run once every
/// observer has seen the event, and is raised as itself.
#[pyclass(subclass, frozen, module = "fnloom._core")]
pub struct ObserverBase;

#[pymethods]
impl ObserverBase {
    /// Creates an observer with no native state.
    ///
    /// Arguments are accepted and ignored, as `object.__new__` does, so a subclass's
    /// `__init__` can take its own.
    #[new]
    #[pyo3(signature = (*_args, **_kwargs))]
    const fn new(_args: &Bound<'_, PyTuple>, _kwargs: Option<&Bound<'_, PyDict>>) -> Self {
        Self
    }

    /// Called once before anything runs.
    #[expect(
        clippy::unused_self,
        reason = "Python calls the hooks as instance methods"
    )]
    const fn pre_graph(&self) {}

    /// Called before a computed node is evaluated.
    #[expect(unused_variables, reason = "the base hook ignores its arguments")]
    #[expect(
        clippy::unused_self,
        reason = "Python calls the hooks as instance methods"
    )]
    const fn pre_node(&self, node: &str) {}

    /// Called after a computed node was evaluated; `ok` says whether it succeeded.
    #[pyo3(signature = (node, *, ok))]
    #[expect(unused_variables, reason = "the base hook ignores its arguments")]
    #[expect(
        clippy::unused_self,
        reason = "Python calls the hooks as instance methods"
    )]
    const fn post_node(&self, node: &str, ok: bool) {}

    /// Called once after the run ended; `ok` says whether it succeeded.
    #[pyo3(signature = (*, ok))]
    #[expect(unused_variables, reason = "the base hook ignores its arguments")]
    #[expect(
        clippy::unused_self,
        reason = "Python calls the hooks as instance methods"
    )]
    const fn post_graph(&self, ok: bool) {}
}

/// Drives one Python observer through its methods, so subclass overrides take effect.
pub struct PyObserver<'py> {
    observer: Bound<'py, ObserverBase>,
}

impl<'py> PyObserver<'py> {
    /// Wraps a Python observer for one run.
    pub const fn new(observer: Bound<'py, ObserverBase>) -> Self {
        Self { observer }
    }

    /// Calls the hook `name` with `args` and the keyword `ok` when given.
    fn call<A>(&self, name: &Bound<'py, PyString>, args: A, ok: Option<bool>) -> PyResult<()>
    where
        A: PyCallArgs<'py>,
    {
        let py = self.observer.py();
        let kwargs = ok
            .map(|ok| -> PyResult<_> {
                let kwargs = PyDict::new(py);
                kwargs.set_item(intern!(py, "ok"), ok)?;
                Ok(kwargs)
            })
            .transpose()?;
        caught(|| {
            self.observer
                .as_any()
                .call_method(name, args, kwargs.as_ref())
                .map(drop)
        })
    }
}

impl Observer<PyErr> for PyObserver<'_> {
    fn pre_graph(&mut self) -> PyResult<()> {
        let py = self.observer.py();
        self.call(intern!(py, "pre_graph"), (), None)
    }

    fn pre_node(&mut self, node: &str) -> PyResult<()> {
        let py = self.observer.py();
        self.call(intern!(py, "pre_node"), (node,), None)
    }

    fn post_node(&mut self, node: &str, ok: bool) -> PyResult<()> {
        let py = self.observer.py();
        self.call(intern!(py, "post_node"), (node,), Some(ok))
    }

    fn post_graph(&mut self, ok: bool) -> PyResult<()> {
        let py = self.observer.py();
        self.call(intern!(py, "post_graph"), (), Some(ok))
    }
}
