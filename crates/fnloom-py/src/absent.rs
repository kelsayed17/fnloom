//! `fnloom.Absent` and its one instance, `fnloom.ABSENT`: the value of a node that declined.

use pyo3::exceptions::PyTypeError;
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::PyString;

/// The type of `fnloom.ABSENT`, a node's declined value. It has exactly one instance,
/// and no other can be made: Python can neither call nor subclass it.
///
/// A node whose return annotation admits `Absent` may return `ABSENT`; each consumer then
/// receives it, uses its default, or is skipped, by its own signature. `ABSENT` has no
/// truth value, so it is never read as a falsy one: compare with `is ABSENT`.
#[pyclass(frozen, module = "fnloom", name = "Absent")]
pub struct Absent;

/// The one instance, made when the extension module is.
static SINGLETON: PyOnceLock<Py<Absent>> = PyOnceLock::new();

/// `fnloom.ABSENT`, made on first use.
pub fn singleton(py: Python<'_>) -> PyResult<&'static Py<Absent>> {
    SINGLETON.get_or_try_init(py, || Py::new(py, Absent))
}

#[pymethods]
#[expect(
    clippy::unused_self,
    reason = "Python calls these as methods of the one instance, which has no state"
)]
impl Absent {
    /// `fnloom.ABSENT`.
    fn __repr__(&self) -> &'static str {
        "fnloom.ABSENT"
    }

    /// Refuses a truth value, so an absence is never read as a falsy value.
    fn __bool__(&self) -> PyResult<bool> {
        Err(PyTypeError::new_err(
            "fnloom.ABSENT has no truth value: compare with `is ABSENT`, or test \
             `isinstance(value, fnloom.Absent)`",
        ))
    }

    /// Pickles as the name `fnloom.ABSENT`, so it loads as the one instance.
    fn __reduce__<'py>(&self, py: Python<'py>) -> Bound<'py, PyString> {
        PyString::intern(py, "ABSENT")
    }

    /// The one instance itself.
    fn __copy__(slf: Py<Self>) -> Py<Self> {
        slf
    }

    /// The one instance itself, whatever the memo.
    fn __deepcopy__(slf: Py<Self>, _memo: &Bound<'_, PyAny>) -> Py<Self> {
        slf
    }
}
