//! One task's calls into Python, shared by `Plan::run`'s evaluator and the parallel
//! workers: an expand's iterator, its items and its end, a late step's lookup, and the
//! return check, so both paths behave the same.

use fnloom_core::node::{Node, NodeId};
use fnloom_core::run::Item;
use pyo3::exceptions::{PyRuntimeError, PyStopIteration, PyTypeError};
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyString, PyTuple};

use crate::absent::Absent;
use crate::errors::caught;
use crate::graph::Graph;
use crate::plan::keyword;

/// How a task calls into Python: directly, in the caller's context, or in a copy of a
/// parallel run's context.
pub trait Calls<'py> {
    /// `function(*args, **kwargs)`.
    ///
    /// # Errors
    ///
    /// Whatever the call raises.
    fn call(
        &self,
        function: &Bound<'py, PyAny>,
        args: Vec<Bound<'py, PyAny>>,
        kwargs: Option<&Bound<'py, PyDict>>,
    ) -> PyResult<Bound<'py, PyAny>>;
}

/// Calls in the current context.
pub struct Direct;

impl<'py> Calls<'py> for Direct {
    fn call(
        &self,
        function: &Bound<'py, PyAny>,
        args: Vec<Bound<'py, PyAny>>,
        kwargs: Option<&Bound<'py, PyDict>>,
    ) -> PyResult<Bound<'py, PyAny>> {
        function.call(PyTuple::new(function.py(), args)?, kwargs)
    }
}

/// What a task needs of its run to call into Python.
pub struct Scope<'a, 'py> {
    /// The graph, for the expands' item factories.
    pub graph: &'a Graph,
    /// The plan's outputs, sorted by id: an expand among them records its items.
    pub outputs: &'a [NodeId],
    /// A cached run's late keys, as `Plan.run`'s `late` describes; `None` uncached.
    pub late: Option<&'a Bound<'py, PyAny>>,
    /// `fnloom.ABSENT`.
    pub absent: &'a Bound<'py, Absent>,
}

impl<'py> Scope<'_, 'py> {
    /// The interned name of `node`.
    fn name(&self, node: &Node<Py<PyAny>>) -> Bound<'py, PyString> {
        keyword(self.absent.py(), self.graph.keywords(), node.id, &node.name)
    }

    /// The iterator over expand `node`'s `iterable`, made by its factory in `branch`.
    pub fn iterate(
        &self,
        calls: &impl Calls<'py>,
        node: &Node<Py<PyAny>>,
        iterable: &Bound<'py, PyAny>,
        branch: &[usize],
    ) -> PyResult<Bound<'py, PyAny>> {
        let py = self.absent.py();
        let Some(factory) = self.graph.items(node.id) else {
            return Err(PyRuntimeError::new_err(format!(
                "broken plan: expand '{}' has no item iterator",
                node.name
            )));
        };
        let branch = PyTuple::new(py, branch)?;
        let mut record = self.outputs.binary_search(&node.id).is_ok();
        if let Some(late) = self.late.filter(|_| !record) {
            let name = self.name(node);
            record = caught(|| late.call_method1(intern!(py, "records"), (name, &branch)))?
                .is_truthy()?;
        }
        // A generator's body runs as its items are read, so in the expand's branch; the
        // iterator tells `late` of each item and of its end, with that branch.
        let late = self
            .late
            .map_or_else(|| py.None().into_bound(py), Clone::clone);
        let args = vec![
            iterable.clone(),
            record.into_pyobject(py)?.to_owned().into_any(),
            branch.into_any(),
            late,
        ];
        caught(|| calls.call(factory.bind(py), args, None))
    }

    /// The next item of expand `node`'s `iterator`.
    pub fn next_item(
        &self,
        calls: &impl Calls<'py>,
        node: &Node<Py<PyAny>>,
        iterator: &Bound<'py, PyAny>,
    ) -> PyResult<Item<Bound<'py, PyAny>>> {
        let py = self.absent.py();
        let next = iterator.getattr(intern!(py, "__next__"))?;
        match caught(|| calls.call(&next, Vec::new(), None)) {
            Ok(item) => Ok(Item::Next(item)),
            Err(error) if error.is_instance_of::<PyStopIteration>(py) => {
                let value = error.value(py).getattr(intern!(py, "value"))?;
                if !value.is(self.absent) {
                    return Ok(Item::Done);
                }
                if !node.declines {
                    return Err(undeclared_absence(&node.name));
                }
                Ok(Item::Declined(value))
            }
            Err(error) => Err(error),
        }
    }

    /// Ends an expand's `iterator`, exhausted or stopped; its value.
    pub fn finish_items(
        &self,
        calls: &impl Calls<'py>,
        iterator: &Bound<'py, PyAny>,
        exhausted: bool,
    ) -> PyResult<Bound<'py, PyAny>> {
        let py = self.absent.py();
        let finish = iterator.getattr(intern!(py, "finish"))?;
        let flag = exhausted.into_pyobject(py)?.to_owned().into_any();
        caught(|| calls.call(&finish, vec![flag], None))
    }

    /// A cache hit from `late.lookup`, which returns a 1-tuple, or `None` for none.
    pub fn lookup(
        &self,
        calls: &impl Calls<'py>,
        node: &Node<Py<PyAny>>,
        branch: &[usize],
    ) -> PyResult<Option<Bound<'py, PyAny>>> {
        let Some(late) = self.late else {
            return Ok(None);
        };
        let py = self.absent.py();
        let lookup = late.getattr(intern!(py, "lookup"))?;
        let args = vec![
            self.name(node).into_any(),
            PyTuple::new(py, branch)?.into_any(),
        ];
        let found = caught(|| calls.call(&lookup, args, None))?;
        if found.is_none() {
            return Ok(None);
        }
        Ok(Some(found.get_item(0)?))
    }

    /// `result` of `node`'s call, once it passes the return check: `fnloom.ABSENT` only
    /// from a node that declines.
    pub fn checked(
        &self,
        node: &Node<Py<PyAny>>,
        result: Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        if !node.declines && result.is(self.absent) {
            return Err(undeclared_absence(&node.name));
        }
        Ok(result)
    }
}

/// The `TypeError` for `node` returning `fnloom.ABSENT` without declaring it may.
#[cold]
pub fn undeclared_absence(node: &str) -> PyErr {
    PyTypeError::new_err(format!(
        "node '{node}' returned fnloom.ABSENT, but its return annotation does not admit \
         fnloom.Absent"
    ))
}
