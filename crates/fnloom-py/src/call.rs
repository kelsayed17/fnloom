//! The binding's one `unsafe` call: a vectorcall, which passes its arguments in place
//! and names its keywords by a tuple.

use std::ptr;

use pyo3::exceptions::PyRuntimeError;
use pyo3::ffi;
use pyo3::prelude::*;
use pyo3::types::{PyString, PyTuple};

/// Keyword names for a vectorcall: a tuple whose every item is a `str`, as `kwnames`
/// must be. Only made from strings, so `vectorcall` can rely on it.
pub struct Keywords(Py<PyTuple>);

impl Keywords {
    /// The tuple of `names`, in order.
    pub fn new<'py>(
        py: Python<'py>,
        names: impl ExactSizeIterator<Item = Bound<'py, PyString>>,
    ) -> PyResult<Self> {
        Ok(Self(PyTuple::new(py, names)?.unbind()))
    }

    /// How many names there are.
    fn len(&self, py: Python<'_>) -> usize {
        self.0.bind(py).len()
    }
}

/// The most arguments `vectorcall` passes from the stack rather than a heap vector.
const INLINE_ARGS: usize = 8;

/// Calls `function` with each of `args` under its keyword in `names`, in order.
///
/// The same call as with a dict of them, without making the dict, nor `CPython`
/// unpacking it again into exactly these names. `RuntimeError` unless there are as many
/// arguments as names, a check that holds in release builds too.
#[expect(
    unsafe_code,
    reason = "PyO3 has no safe call taking keyword names, and the dict it takes instead is \
              most of a node call's cost; the one block is checked below"
)]
pub fn vectorcall<'a, 'py: 'a>(
    function: &Bound<'py, PyAny>,
    args: impl ExactSizeIterator<Item = &'a Bound<'py, PyAny>>,
    names: &Keywords,
) -> PyResult<Bound<'py, PyAny>> {
    let py = function.py();
    let count = args.len();
    let keywords = names.len(py);
    // Slot 0 is spare, so a callee may borrow it to prepend an argument
    // (`PY_VECTORCALL_ARGUMENTS_OFFSET`), as a bound method or a partial does. A call
    // with few arguments, the usual one, needs no allocation for them.
    let mut inline = [ptr::null_mut(); INLINE_ARGS + 1];
    let mut spilled;
    let vector: &mut [*mut ffi::PyObject] = if count <= INLINE_ARGS {
        &mut inline
    } else {
        spilled = vec![ptr::null_mut(); count + 1];
        &mut spilled
    };
    // Counted as filled, since an iterator's stated length is not a promise.
    let mut filled: usize = 0;
    for (slot, arg) in vector.iter_mut().skip(1).zip(args) {
        *slot = arg.as_ptr();
        filled += 1;
    }
    if filled != count || filled != keywords {
        return Err(PyRuntimeError::new_err(format!(
            "broken call: {filled} arguments for {keywords} keywords"
        )));
    }
    // SAFETY: the thread is attached to the interpreter (`function` is bound).
    // References: `vector` holds one spare slot, then `filled` borrowed references, each
    // owned by a `Bound` the caller keeps alive for the whole call; the callee borrows
    // them and takes no ownership, so no count changes hands. It may write only the
    // spare slot, which the offset flag allows and `vector` is mutable for.
    // Keywords: the kwnames tuple is borrowed from `names`, which outlives the call; it
    // holds only `str`s (the only kind `Keywords` holds), exactly one per argument, as
    // checked above, so no argument is positional.
    // Result: a new reference, owned by the `Bound` made of it, or null with the
    // callee's exception set, which `from_owned_ptr_or_err` takes as the error.
    unsafe {
        let result = ffi::PyObject_Vectorcall(
            function.as_ptr(),
            vector.as_mut_ptr().add(1),
            ffi::PY_VECTORCALL_ARGUMENTS_OFFSET,
            names.0.as_ptr(),
        );
        Bound::from_owned_ptr_or_err(py, result)
    }
}
