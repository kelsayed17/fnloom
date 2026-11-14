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
    /// function is called with, by parameter name. A block node's call also gets
    /// `branch`, its branch path; a node outside every block is called without it.
    #[pyo3(signature = (node, kwargs, *, branch = None))]
    #[expect(unused_variables, reason = "the base hook ignores its arguments")]
    #[expect(
        clippy::unused_self,
        reason = "Python calls the hooks as instance methods"
    )]
    const fn pre_node(
        &self,
        node: &str,
        kwargs: &Bound<'_, PyDict>,
        branch: Option<&Bound<'_, PyTuple>>,
    ) {
    }

    /// Called after a computed node was evaluated: `ok` says whether it succeeded, `value`
    /// is what it returned (`None` on failure) and `error` the exception it raised (`None`
    /// on success). A block node's call also gets `branch`, as `pre_node` does.
    #[pyo3(signature = (node, *, ok, value, error, branch = None))]
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
        branch: Option<&Bound<'_, PyTuple>>,
    ) {
    }

    /// Called when an expand's items are exhausted, with how many there were; `branch`
    /// is the branch the expand ran in, `()` outside blocks. Sent only to an observer
    /// whose class overrides it.
    #[pyo3(signature = (node, *, branch, items))]
    #[expect(unused_variables, reason = "the base hook ignores its arguments")]
    #[expect(
        clippy::unused_self,
        reason = "Python calls the hooks as instance methods"
    )]
    const fn post_expand(&self, node: &str, branch: &Bound<'_, PyTuple>, items: usize) {}

    /// Called instead of `pre_node` and `post_node` for a node downstream of an expand
    /// whose value the cache held, with that value; `branch` is the branch it hit in,
    /// `()` outside blocks. Sent only to an observer whose class overrides it.
    #[pyo3(signature = (node, *, branch, value))]
    #[expect(unused_variables, reason = "the base hook ignores its arguments")]
    #[expect(
        clippy::unused_self,
        reason = "Python calls the hooks as instance methods"
    )]
    const fn hit_in(&self, node: &str, branch: &Bound<'_, PyTuple>, value: &Bound<'_, PyAny>) {}

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
    /// Whether the observer's class overrides `post_expand`, which is sent only then.
    expands: bool,
    /// Whether the observer's class overrides `hit_in`, which is sent only then.
    hits: bool,
}

impl<'py, 's> PyObserver<'py, 's> {
    /// Wraps a Python observer for one run.
    pub fn new(observer: Bound<'py, ObserverBase>, kwargs: &'s Kwargs<'py, 's>) -> Self {
        let nodes = overrides(&observer, "pre_node") || overrides(&observer, "post_node");
        let expands = overrides(&observer, "post_expand");
        let hits = overrides(&observer, "hit_in");
        Self {
            observer,
            kwargs,
            nodes,
            expands,
            hits,
        }
    }

    /// `post_node`'s keyword arguments, with `branch` when it is not empty.
    fn outcome(
        &self,
        branch: &[usize],
        outcome: Result<&Value<'py>, &PyErr>,
    ) -> PyResult<Bound<'py, PyDict>> {
        let py = self.observer.py();
        let absent = py.None().into_bound(py);
        let (value, error) = match outcome {
            Ok(value) => (value.clone(), absent),
            Err(error) => (absent, error.value(py).clone().into_any()),
        };
        let kwargs = self.keywords(
            outcome.is_ok(),
            [
                (intern!(py, "value"), &value),
                (intern!(py, "error"), &error),
            ],
        )?;
        if !branch.is_empty() {
            kwargs.set_item(intern!(py, "branch"), PyTuple::new(py, branch)?)?;
        }
        Ok(kwargs)
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

impl<'py> Observer<Value<'py>, PyErr> for PyObserver<'py, '_> {
    fn pre_graph(&mut self) -> PyResult<()> {
        let py = self.observer.py();
        self.call(intern!(py, "pre_graph"), (), None)
    }

    fn pre_node(&mut self, node: &str, args: &[Arg<'_, Value<'py>>]) -> PyResult<()> {
        if !self.nodes {
            return Ok(());
        }
        let py = self.observer.py();
        let kwargs = self.kwargs.for_observers(node, args)?;
        self.call(intern!(py, "pre_node"), (node, kwargs), None)
    }

    fn post_node(&mut self, node: &str, outcome: Result<&Value<'py>, &PyErr>) -> PyResult<()> {
        self.post_node_in(node, &[], outcome)
    }

    fn pre_node_in(
        &mut self,
        node: &str,
        branch: &[usize],
        args: &[Arg<'_, Value<'py>>],
    ) -> PyResult<()> {
        if branch.is_empty() {
            return self.pre_node(node, args);
        }
        if !self.nodes {
            return Ok(());
        }
        let py = self.observer.py();
        let kwargs = self.kwargs.for_observers(node, args)?;
        let keywords = PyDict::new(py);
        keywords.set_item(intern!(py, "branch"), PyTuple::new(py, branch)?)?;
        self.call(intern!(py, "pre_node"), (node, kwargs), Some(&keywords))
    }

    fn post_node_in(
        &mut self,
        node: &str,
        branch: &[usize],
        outcome: Result<&Value<'py>, &PyErr>,
    ) -> PyResult<()> {
        if !self.nodes {
            return Ok(());
        }
        let py = self.observer.py();
        let kwargs = self.outcome(branch, outcome)?;
        self.call(intern!(py, "post_node"), (node,), Some(&kwargs))
    }

    fn post_expand(&mut self, node: &str, branch: &[usize], items: usize) -> PyResult<()> {
        if !self.expands {
            return Ok(());
        }
        let py = self.observer.py();
        let kwargs = PyDict::new(py);
        kwargs.set_item(intern!(py, "branch"), PyTuple::new(py, branch)?)?;
        kwargs.set_item(intern!(py, "items"), items)?;
        self.call(intern!(py, "post_expand"), (node,), Some(&kwargs))
    }

    fn hit_in(&mut self, node: &str, branch: &[usize], value: &Value<'py>) -> PyResult<()> {
        if !self.hits {
            return Ok(());
        }
        let py = self.observer.py();
        let kwargs = PyDict::new(py);
        kwargs.set_item(intern!(py, "branch"), PyTuple::new(py, branch)?)?;
        kwargs.set_item(intern!(py, "value"), value)?;
        self.call(intern!(py, "hit_in"), (node,), Some(&kwargs))
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
