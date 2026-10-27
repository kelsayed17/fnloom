//! Mapping fnloom-core errors onto Python exceptions.

use std::any::Any;
use std::fmt::Display;
use std::panic::{self, AssertUnwindSafe};

use fnloom_core::run::RunError;
use pyo3::exceptions::{PyBaseExceptionGroup, PyRuntimeError, PyValueError};
use pyo3::intern;
use pyo3::panic::PanicException;
use pyo3::prelude::*;

/// A `ValueError` carrying `error`'s message, which names the culprits.
pub fn value_error(error: impl Display) -> PyErr {
    PyValueError::new_err(error.to_string())
}

/// Runs `call`, turning a Rust panic that unwinds out of it into a `PanicException`.
///
/// When a call into Python fails with a `PanicException` (a panic in nested Rust code,
/// which the binding library turns into that exception), fetching the error resumes the
/// panic in Rust. Caught here, at each call into Python, the panic becomes an ordinary
/// error again, so the executor's error path still runs the closing hooks.
pub fn caught<T>(call: impl FnOnce() -> PyResult<T>) -> PyResult<T> {
    // Unwind safety: a caught panic only discards `call`'s own Python objects; no state
    // the caller keeps afterwards is left half-updated.
    panic::catch_unwind(AssertUnwindSafe(call))
        .unwrap_or_else(|payload| Err(panic_error(&*payload)))
}

/// A `PanicException` carrying the panic's message, extracted the way the binding library
/// extracts it.
fn panic_error(payload: &(dyn Any + Send)) -> PyErr {
    let message = payload
        .downcast_ref::<String>()
        .cloned()
        .or_else(|| {
            payload
                .downcast_ref::<&str>()
                .map(|text| (*text).to_owned())
        })
        .unwrap_or_else(|| "panic from Rust code".to_owned());
    PanicException::new_err((message,))
}

/// The Python exception for a failed run.
///
/// A node failure raises the node's own exception, noted with the node's name; observer
/// errors raise the original exception objects, grouped when there are several. A broken
/// plan raises `RuntimeError` naming what was found, grouped with any observer errors.
pub fn run_error(py: Python<'_>, error: RunError<PyErr>) -> PyErr {
    match error {
        RunError::Node {
            node,
            source,
            observer_errors,
        } => {
            let source = noted(py, source, &format!("in node '{node}'"));
            if observer_errors.is_empty() {
                source
            } else {
                let mut errors = vec![source];
                errors.extend(observer_errors);
                group(
                    format!("node '{node}' failed and observer hooks raised"),
                    errors,
                )
            }
        }
        RunError::Observers { event, mut errors } => {
            if errors.len() == 1 {
                if let Some(only) = errors.pop() {
                    return only;
                }
            }
            group(format!("observer hooks raised during {event}"), errors)
        }
        missing @ RunError::MissingValues(_) => value_error(missing),
        RunError::BrokenPlan {
            detail,
            observer_errors,
        } => {
            let error = PyRuntimeError::new_err(format!("broken plan: {detail}"));
            if observer_errors.is_empty() {
                error
            } else {
                let mut errors = vec![error];
                errors.extend(observer_errors);
                group(
                    format!("broken plan: {detail}; observer hooks raised"),
                    errors,
                )
            }
        }
    }
}

/// `error` with `note` added; if adding the note itself fails, that failure, caused by `error`.
///
/// A note the exception already carries is not added again, so an exception object a node
/// raises on every run (a module-level instance, say) keeps one note rather than one per run.
fn noted(py: Python<'_>, error: PyErr, note: &str) -> PyErr {
    let carried = error
        .value(py)
        .getattr(intern!(py, "__notes__"))
        .and_then(|notes| notes.contains(note))
        .unwrap_or(false);
    if carried {
        return error;
    }
    match error.add_note(py, note) {
        Ok(()) => error,
        Err(failure) => {
            failure.set_cause(py, Some(error));
            failure
        }
    }
}

/// An exception group of `errors`.
///
/// Built through `BaseExceptionGroup`, which yields an `ExceptionGroup` whenever every
/// member is an `Exception`, and stays a `BaseExceptionGroup` (rather than failing to
/// construct) when a hook raised a bare `BaseException`.
fn group(message: String, errors: Vec<PyErr>) -> PyErr {
    PyBaseExceptionGroup::new_err((message, errors))
}
