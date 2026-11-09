//! Run observers: the subclassable Python base class and its adapter to the core trait.

use fnloom_core::run::{Arg, Observer};
use pyo3::call::PyCallArgs;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyString, PyTuple};

use crate::errors::caught;
use crate::plan::{Kwargs, Value};

/// Base class for run observers; subclass it and override any of the five hooks.
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

    /// Called before a computed node is evaluated, with the keyword arguments its
    /// function is called with, by parameter name.
    #[expect(unused_variables, reason = "the base hook ignores its arguments")]
    #[expect(
        clippy::unused_self,
        reason = "Python calls the hooks as instance methods"
    )]
    const fn pre_node(&self, node: &str, kwargs: &Bound<'_, PyDict>) {}

    /// Called after a computed node was evaluated: `ok` says whether it succeeded, `value`
    /// is what it returned (`None` on failure) and `error` the exception it raised (`None`
    /// on success).
    #[pyo3(signature = (node, *, ok, value, error))]
    #[expect(unused_variables, reason = "the base hook ignores its arguments")]
    #[expect(
        clippy::unused_self,
        reason = "Python calls the hooks as instance methods"
    )]
    const fn post_node(
        &self,
        node: &str,
        ok: bool,
        value: &Bound<'_, PyAny>,
        error: &Bound<'_, PyAny>,
    ) {
    }

    /// Called instead of `pre_node` and `post_node` for a computed node that is skipped,
    /// because an argument it needs is absent: the node is then absent too. `branch` is
    /// the block branch it was skipped in, `()` outside blocks.
    #[pyo3(signature = (node, *, branch))]
    #[expect(unused_variables, reason = "the base hook ignores its arguments")]
    #[expect(
        clippy::unused_self,
        reason = "Python calls the hooks as instance methods"
    )]
    const fn skipped(&self, node: &str, branch: &Bound<'_, PyTuple>) {}

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
///
/// Values become Python objects here and only here, so a run with no observers converts
/// nothing for them.
pub struct PyObserver<'py, 's> {
    observer: Bound<'py, ObserverBase>,
    /// Where `pre_node` gets the node's keyword arguments, shared with the call.
    kwargs: &'s Kwargs<'py, 's>,
    /// Whether the observer's class overrides `pre_node` or `post_node`: when it overrides
    /// neither, node events are not sent, so its keyword arguments are never made.
    nodes: bool,
}

impl<'py, 's> PyObserver<'py, 's> {
    /// Wraps a Python observer for one run.
    pub fn new(observer: Bound<'py, ObserverBase>, kwargs: &'s Kwargs<'py, 's>) -> Self {
        let nodes = overrides(&observer, "pre_node") || overrides(&observer, "post_node");
        Self {
            observer,
            kwargs,
            nodes,
        }
    }

    /// Calls the hook `name` with `args` and the keyword arguments `kwargs`.
    fn call<A>(
        &self,
        name: &Bound<'py, PyString>,
        args: A,
        kwargs: Option<&Bound<'py, PyDict>>,
    ) -> PyResult<()>
    where
        A: PyCallArgs<'py>,
    {
        caught(|| {
            self.observer
                .as_any()
                .call_method(name, args, kwargs)
                .map(drop)
        })
    }

    /// The keyword argument `ok`, plus any `extra` keyword arguments.
    fn keywords<const N: usize>(
        &self,
        ok: bool,
        extra: [(&Bound<'py, PyString>, &Bound<'py, PyAny>); N],
    ) -> PyResult<Bound<'py, PyDict>> {
        let py = self.observer.py();
        let kwargs = PyDict::new(py);
        kwargs.set_item(intern!(py, "ok"), ok)?;
        for (key, value) in extra {
            kwargs.set_item(key, value)?;
        }
        Ok(kwargs)
    }
}

/// Whether `observer`'s class defines its own `name`, not `ObserverBase`'s no-op. When it
/// can't be told, it counts as overridden, so the event is sent.
fn overrides(observer: &Bound<'_, ObserverBase>, name: &str) -> bool {
    let py = observer.py();
    let own = observer.get_type().getattr(name);
    let base = py.get_type::<ObserverBase>().getattr(name);
    match (own, base) {
        (Ok(own), Ok(base)) => !own.is(&base),
        _ => true,
    }
}

impl Observer<Value, PyErr> for PyObserver<'_, '_> {
    fn pre_graph(&mut self) -> PyResult<()> {
        let py = self.observer.py();
        self.call(intern!(py, "pre_graph"), (), None)
    }

    fn pre_node(&mut self, node: &str, args: &[Arg<'_, Value>]) -> PyResult<()> {
        if !self.nodes {
            return Ok(());
        }
        let py = self.observer.py();
        let kwargs = self.kwargs.for_observers(node, args)?;
        self.call(intern!(py, "pre_node"), (node, kwargs), None)
    }

    fn post_node(&mut self, node: &str, outcome: Result<&Value, &PyErr>) -> PyResult<()> {
        if !self.nodes {
            return Ok(());
        }
        let py = self.observer.py();
        let absent = py.None().into_bound(py);
        let (value, error) = match outcome {
            Ok(value) => (value.bind(py).clone(), absent),
            Err(error) => (absent, error.value(py).clone().into_any()),
        };
        let kwargs = self.keywords(
            outcome.is_ok(),
            [
                (intern!(py, "value"), &value),
                (intern!(py, "error"), &error),
            ],
        )?;
        self.call(intern!(py, "post_node"), (node,), Some(&kwargs))
    }

    fn skipped(&mut self, node: &str, branch: &[usize]) -> PyResult<()> {
        let py = self.observer.py();
        let kwargs = PyDict::new(py);
        kwargs.set_item(intern!(py, "branch"), PyTuple::new(py, branch)?)?;
        self.call(intern!(py, "skipped"), (node,), Some(&kwargs))
    }

    fn post_graph(&mut self, ok: bool) -> PyResult<()> {
        let py = self.observer.py();
        let kwargs = self.keywords(ok, [])?;
        self.call(intern!(py, "post_graph"), (), Some(&kwargs))
    }
}
