//! Run observers: the subclassable Python base class and its adapter to the core trait.

use fnloom_core::run::{Arg, Observer};
use pyo3::BoundObject;
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
    /// The hooks its class overrides.
    flags: Flags,
    /// The `run` method of the context the hooks are called in; `None` for the current one.
    context: Option<Bound<'py, PyAny>>,
}

/// Which hooks an observer's class overrides, found once per observer.
#[derive(Clone, Copy, Debug)]
#[expect(
    clippy::struct_excessive_bools,
    reason = "one flag per kind of event, each sent only to an observer that overrides it"
)]
pub struct Flags {
    /// Whether the class overrides `pre_node` or `post_node`: when it overrides neither,
    /// node events are not sent, so its keyword arguments are never made.
    nodes: bool,
    /// Whether the class overrides `post_expand`, which is sent only then.
    expands: bool,
    /// Whether the class overrides `hit_in`, which is sent only then.
    hits: bool,
    /// Whether the class overrides `skipped`, which is sent only then.
    skips: bool,
    /// Whether the class sets `orders = True`: its node, expand, hit and skip events are then
    /// called by position, `branch` and `order`, the step's order key, as tuples, last.
    orders: bool,
}

impl Flags {
    /// The hooks `observer`'s class overrides.
    pub fn of(observer: &Bound<'_, ObserverBase>) -> Self {
        Self {
            nodes: overrides(observer, "pre_node") || overrides(observer, "post_node"),
            expands: overrides(observer, "post_expand"),
            hits: overrides(observer, "hit_in"),
            skips: overrides(observer, "skipped"),
            orders: observer
                .get_type()
                .getattr(intern!(observer.py(), "orders"))
                .and_then(|orders| orders.is_truthy())
                .unwrap_or(false),
        }
    }
}

impl<'py, 's> PyObserver<'py, 's> {
    /// Wraps a Python observer for one run.
    pub fn new(observer: Bound<'py, ObserverBase>, kwargs: &'s Kwargs<'py, 's>) -> Self {
        let flags = Flags::of(&observer);
        Self::flagged(observer, kwargs, flags, None)
    }

    /// Wraps a Python observer whose `flags` are already known, for one event, its hooks
    /// called through `context`, a `Context.run`, when given.
    pub const fn flagged(
        observer: Bound<'py, ObserverBase>,
        kwargs: &'s Kwargs<'py, 's>,
        flags: Flags,
        context: Option<Bound<'py, PyAny>>,
    ) -> Self {
        Self {
            observer,
            kwargs,
            flags,
            context,
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

    /// Calls the hook `name` with `args` and the keyword arguments `kwargs`, in the
    /// observer's context, if it has one.
    fn call<A>(
        &self,
        name: &Bound<'py, PyString>,
        args: A,
        kwargs: Option<&Bound<'py, PyDict>>,
    ) -> PyResult<()>
    where
        A: PyCallArgs<'py> + IntoPyObject<'py, Target = PyTuple>,
    {
        let hook = self.observer.as_any();
        let Some(run) = &self.context else {
            // Called in place, so a Rust tuple goes by vectorcall, as before contexts.
            return caught(|| hook.call_method(name, args, kwargs).map(drop));
        };
        let py = self.observer.py();
        let args = args.into_pyobject(py).map_err(Into::into)?.into_bound();
        caught(|| {
            let mut all = vec![hook.getattr(name)?];
            all.extend(args.iter());
            run.call(PyTuple::new(py, all)?, kwargs).map(drop)
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

impl<'py> PyObserver<'py, '_> {
    /// `branch` as a tuple: the shared empty one outside blocks.
    fn branch(&self, branch: &[usize]) -> PyResult<Bound<'py, PyTuple>> {
        let py = self.observer.py();
        if branch.is_empty() {
            return Ok(PyTuple::empty(py));
        }
        PyTuple::new(py, branch)
    }

    /// `pre_node`, called by position for an observer that wants orders: `(node, kwargs,
    /// branch, order)`, so no keyword dict is made per event.
    fn pre_node_ordered(
        &self,
        node: &str,
        branch: &[usize],
        args: &[Arg<'_, Value<'py>>],
    ) -> PyResult<()> {
        let py = self.observer.py();
        let kwargs = self.kwargs.for_observers(node, args)?;
        let order = self.kwargs.order(node, branch)?;
        let args = (node, kwargs, self.branch(branch)?, order);
        self.call(intern!(py, "pre_node"), args, None)
    }
}

impl<'py> Observer<Value<'py>, PyErr> for PyObserver<'py, '_> {
    fn pre_graph(&mut self) -> PyResult<()> {
        let py = self.observer.py();
        self.call(intern!(py, "pre_graph"), (), None)
    }

    fn pre_node(&mut self, node: &str, args: &[Arg<'_, Value<'py>>]) -> PyResult<()> {
        if !self.flags.nodes {
            return Ok(());
        }
        if self.flags.orders {
            return self.pre_node_ordered(node, &[], args);
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
        if !self.flags.nodes {
            return Ok(());
        }
        if self.flags.orders {
            return self.pre_node_ordered(node, branch, args);
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
        if !self.flags.nodes {
            return Ok(());
        }
        let py = self.observer.py();
        if self.flags.orders {
            let missing = py.None().into_bound(py);
            let (value, error) = match outcome {
                Ok(value) => (value.clone(), missing),
                Err(error) => (missing, error.value(py).clone().into_any()),
            };
            let order = self.kwargs.order(node, branch)?;
            let args = (
                node,
                outcome.is_ok(),
                value,
                error,
                self.branch(branch)?,
                order,
            );
            return self.call(intern!(py, "post_node"), args, None);
        }
        let kwargs = self.outcome(branch, outcome)?;
        self.call(intern!(py, "post_node"), (node,), Some(&kwargs))
    }

    fn post_expand(&mut self, node: &str, branch: &[usize], items: usize) -> PyResult<()> {
        if !self.flags.expands {
            return Ok(());
        }
        let py = self.observer.py();
        if self.flags.orders {
            let order = self.kwargs.order(node, branch)?;
            let args = (node, self.branch(branch)?, items, order);
            return self.call(intern!(py, "post_expand"), args, None);
        }
        let kwargs = PyDict::new(py);
        kwargs.set_item(intern!(py, "branch"), PyTuple::new(py, branch)?)?;
        kwargs.set_item(intern!(py, "items"), items)?;
        self.call(intern!(py, "post_expand"), (node,), Some(&kwargs))
    }

    fn hit_in(&mut self, node: &str, branch: &[usize], value: &Value<'py>) -> PyResult<()> {
        if !self.flags.hits {
            return Ok(());
        }
        let py = self.observer.py();
        if self.flags.orders {
            let order = self.kwargs.order(node, branch)?;
            let args = (node, self.branch(branch)?, value, order);
            return self.call(intern!(py, "hit_in"), args, None);
        }
        let kwargs = PyDict::new(py);
        kwargs.set_item(intern!(py, "branch"), PyTuple::new(py, branch)?)?;
        kwargs.set_item(intern!(py, "value"), value)?;
        self.call(intern!(py, "hit_in"), (node,), Some(&kwargs))
    }

    fn skipped(&mut self, node: &str, branch: &[usize]) -> PyResult<()> {
        if !self.flags.skips {
            return Ok(());
        }
        let py = self.observer.py();
        if self.flags.orders {
            let order = self.kwargs.order(node, branch)?;
            let args = (node, self.branch(branch)?, order);
            return self.call(intern!(py, "skipped"), args, None);
        }
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
