//! Value fingerprints: BLAKE3 digests of a value's type and content, for cache keys.
//!
//! Every digest is one *node* in the shared framing ([`Framer`]): a one-byte [`Kind`],
//! then each part as its length (`u64`, little-endian) and its bytes. A container's parts
//! are its children's digests, so a fingerprint is a Merkle hash of the value's tree.
//! `hash_parts` and cache keys use the same framing under their own kinds.

use std::fmt::Write as _;
use std::mem;

use pyo3::buffer::PyBuffer;
use pyo3::create_exception;
use pyo3::exceptions::{PyBufferError, PyNotImplementedError, PyTypeError, PyValueError};
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{
    PyBool, PyByteArray, PyBytes, PyComplex, PyDict, PyFloat, PyFrozenSet, PyInt, PyList,
    PyMemoryView, PySet, PySlice, PyString, PyTuple,
};

create_exception!(
    fnloom,
    UnfingerprintableError,
    PyTypeError,
    "A value, or a value inside it, that cannot be fingerprinted.\n\n\
     `path` locates it in the value passed (`[1][\"k\"]`, empty at the top) and \
     `type_name` names its type."
);

/// A 32-byte BLAKE3 digest.
pub(crate) type Digest = [u8; 32];

/// The containers a fingerprint walks through at most, nested: deeper values (or cyclic
/// ones, which never end) are refused rather than exhausting memory.
const MAX_DEPTH: usize = 1000;

/// The bytes of a buffer are copied out and hashed this many at a time.
const CHUNK: usize = 1 << 20;

/// What a framed digest is of: its first byte.
///
/// Fingerprints may outlive the process (they key cached results), so a kind's number
/// never changes once released; new kinds take new numbers.
#[derive(Clone, Copy, PartialEq, Eq)]
#[repr(u8)]
pub(crate) enum Kind {
    /// `hash_parts`: arbitrary parts, framed.
    Parts = 0,
    /// `None`.
    None = 1,
    /// `bool`: one byte.
    Bool = 2,
    /// `int`: minimal two's-complement bytes, little-endian.
    Int = 3,
    /// `float`: its bit pattern, every NaN the same.
    Float = 4,
    /// `complex`: the real and imaginary parts' bit patterns.
    Complex = 5,
    /// `str`: UTF-8.
    Str = 6,
    /// `bytes`.
    Bytes = 7,
    /// `bytearray`.
    ByteArray = 8,
    /// `tuple`: its items' digests, in order.
    Tuple = 9,
    /// `list`: its items' digests, in order.
    List = 10,
    /// `dict`: each key's then its value's digest, in insertion order.
    Dict = 11,
    /// `set`: its members' digests, sorted.
    Set = 12,
    /// `frozenset`: its members' digests, sorted.
    FrozenSet = 13,
    /// `memoryview`: format, shape and bytes in C order.
    MemoryView = 14,
    /// A canonical form from the fallback: the value's type's qualified name, then the
    /// form's digest.
    Canonical = 15,
}

/// One framed digest under construction: a kind byte, then length-prefixed parts.
///
/// This is the only framing: fingerprints, `hash_parts` and cache keys all use it, so a
/// digest of one kind never equals a digest of another, and parts never run together.
pub(crate) struct Framer(blake3::Hasher);

impl Framer {
    /// Starts a digest of `kind`.
    pub(crate) fn new(kind: Kind) -> Self {
        let mut hasher = blake3::Hasher::new();
        hasher.update(&[kind as u8]);
        Self(hasher)
    }

    /// Adds `bytes` as one part.
    pub(crate) fn part(&mut self, bytes: &[u8]) {
        self.begin_part(bytes.len());
        self.0.update(bytes);
    }

    /// Starts a part of `len` bytes, which [`Self::extend`] then adds.
    fn begin_part(&mut self, len: usize) {
        self.0.update(&(len as u64).to_le_bytes());
    }

    /// Adds bytes to the part [`Self::begin_part`] started.
    fn extend(&mut self, bytes: &[u8]) {
        self.0.update(bytes);
    }

    /// The finished digest.
    pub(crate) fn finish(&self) -> Digest {
        *self.0.finalize().as_bytes()
    }
}

/// The framed digest of `kind` and `parts`.
pub(crate) fn digest(kind: Kind, parts: &[&[u8]]) -> Digest {
    let mut framer = Framer::new(kind);
    for part in parts {
        framer.part(part);
    }
    framer.finish()
}

/// The 32-byte BLAKE3 digest of `parts`, each framed by its length.
#[pyfunction]
pub fn hash_parts<'py>(py: Python<'py>, parts: Vec<Bound<'py, PyBytes>>) -> Bound<'py, PyBytes> {
    let mut framer = Framer::new(Kind::Parts);
    for part in parts {
        framer.part(part.as_bytes());
    }
    PyBytes::new(py, &framer.finish())
}

/// The 32-byte fingerprint of `value`'s type and content.
///
/// Built-in values are hashed here. For any other value `fallback(value)` returns a
/// canonical form, fingerprinted in turn under the value's type, or `None`, which
/// refuses it. Raises `UnfingerprintableError` naming where the first refused value sits.
#[pyfunction]
pub fn fingerprint<'py>(
    value: &Bound<'py, PyAny>,
    fallback: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyBytes>> {
    let digest = walk(value, fallback)?;
    Ok(PyBytes::new(value.py(), &digest))
}

/// A container being fingerprinted: its children, walked one at a time.
struct Frame<'py> {
    kind: Kind,
    /// A part hashed before the children's digests: a canonical form's type name.
    head: Option<String>,
    /// The values to fingerprint: items, members, keys and values alternately, or a
    /// canonical form.
    children: Vec<Bound<'py, PyAny>>,
    /// How many children have been visited.
    visited: usize,
    digests: Vec<Digest>,
}

impl<'py> Frame<'py> {
    fn new(kind: Kind, children: Vec<Bound<'py, PyAny>>) -> Self {
        let digests = Vec::with_capacity(children.len());
        Self {
            kind,
            head: None,
            children,
            visited: 0,
            digests,
        }
    }

    /// The next child to visit, if any is left.
    fn next_child(&mut self) -> Option<Bound<'py, PyAny>> {
        let child = self.children.get(self.visited)?.clone();
        self.visited += 1;
        Some(child)
    }

    /// The digest of the container, once every child's digest is in.
    fn finish(mut self) -> Digest {
        if matches!(self.kind, Kind::Set | Kind::FrozenSet) {
            self.digests.sort_unstable();
        }
        let mut framer = Framer::new(self.kind);
        if let Some(head) = &self.head {
            framer.part(head.as_bytes());
        }
        for child in &self.digests {
            framer.part(child);
        }
        framer.finish()
    }

    /// Where the child being visited sits, appended to `path`.
    fn segment(&self, path: &mut String) {
        let Some(index) = self.visited.checked_sub(1) else {
            return;
        };
        match self.kind {
            Kind::Tuple | Kind::List => {
                let _ = write!(path, "[{index}]");
            }
            Kind::Dict => {
                // Keys and values alternate; both report the entry's key.
                if let Some(key) = self.children.get(index & !1) {
                    let _ = write!(path, "[{}]", shown(key));
                }
            }
            Kind::Set | Kind::FrozenSet => {
                if let Some(member) = self.children.get(index) {
                    let _ = write!(path, "{{{}}}", shown(member));
                }
            }
            _ => {}
        }
    }
}

/// A key or member as a path shows it: a string in double quotes, anything else by `repr`.
fn shown(value: &Bound<'_, PyAny>) -> String {
    if let Ok(text) = value.cast::<PyString>() {
        if let Ok(text) = text.to_str() {
            return format!("{text:?}");
        }
    }
    value
        .repr()
        .map_or_else(|_| "<unrepresentable>".to_owned(), |repr| repr.to_string())
}

/// What visiting a value gives: its digest, or a container to walk.
enum Step<'py> {
    Done(Digest),
    Open(Frame<'py>),
}

/// The fingerprint of `value`, walked iteratively so deep values cannot overflow the stack.
fn walk<'py>(value: &Bound<'py, PyAny>, fallback: &Bound<'py, PyAny>) -> PyResult<Digest> {
    // The container being walked, and the ones enclosing it, outermost first.
    let mut ancestors: Vec<Frame<'py>> = Vec::new();
    let mut current = match visit(value, fallback, &ancestors, None)? {
        Step::Done(digest) => return Ok(digest),
        Step::Open(frame) => frame,
    };
    loop {
        if let Some(child) = current.next_child() {
            match visit(&child, fallback, &ancestors, Some(&current))? {
                Step::Done(digest) => current.digests.push(digest),
                Step::Open(frame) => {
                    if ancestors.len() + 1 >= MAX_DEPTH {
                        let path = path(&ancestors, Some(&current));
                        return Err(refused(
                            &child,
                            &path,
                            &format!(": nested more than {MAX_DEPTH} levels deep, or cyclic"),
                        ));
                    }
                    ancestors.push(mem::replace(&mut current, frame));
                }
            }
        } else {
            let digest = current.finish();
            match ancestors.pop() {
                Some(parent) => {
                    current = parent;
                    current.digests.push(digest);
                }
                None => return Ok(digest),
            }
        }
    }
}

/// The path to the value being visited, through `ancestors` then `current`.
///
/// Inside a canonical form the path stops at the value the form stands for: the form's
/// own structure is not where the user put anything.
fn path(ancestors: &[Frame<'_>], current: Option<&Frame<'_>>) -> String {
    let mut path = String::new();
    for frame in ancestors.iter().chain(current) {
        if frame.kind == Kind::Canonical {
            break;
        }
        frame.segment(&mut path);
    }
    path
}

/// An `UnfingerprintableError` for `value` at `path`, its message ending with `detail`.
fn refused(value: &Bound<'_, PyAny>, path: &str, detail: &str) -> PyErr {
    let py = value.py();
    let type_name = value
        .get_type()
        .qualname()
        .map_or_else(|_| "<unknown>".to_owned(), |name| name.to_string());
    let location = if path.is_empty() {
        String::new()
    } else {
        format!(" at {path}")
    };
    let error = UnfingerprintableError::new_err(format!(
        "cannot fingerprint {type_name}{location}{detail}"
    ));
    let instance = error.value(py);
    if let Err(failure) = instance
        .setattr(intern!(py, "path"), path)
        .and_then(|()| instance.setattr(intern!(py, "type_name"), &type_name))
    {
        return failure;
    }
    error
}

/// Fingerprints a built-in leaf, opens a built-in container, or asks `fallback`.
fn visit<'py>(
    value: &Bound<'py, PyAny>,
    fallback: &Bound<'py, PyAny>,
    ancestors: &[Frame<'py>],
    current: Option<&Frame<'py>>,
) -> PyResult<Step<'py>> {
    if value.is_none() {
        return Ok(Step::Done(digest(Kind::None, &[])));
    }
    if value.is_exact_instance_of::<PyBool>() {
        return Ok(Step::Done(digest(
            Kind::Bool,
            &[&[u8::from(value.is_truthy()?)]],
        )));
    }
    if value.is_exact_instance_of::<PyInt>() {
        return Ok(Step::Done(digest(Kind::Int, &[&int_bytes(value)?])));
    }
    if value.is_exact_instance_of::<PyFloat>() {
        let bits = float_bits(value.extract()?);
        return Ok(Step::Done(digest(Kind::Float, &[&bits])));
    }
    if let Ok(complex) = value.cast_exact::<PyComplex>() {
        let real = float_bits(complex.real());
        let imag = float_bits(complex.imag());
        return Ok(Step::Done(digest(Kind::Complex, &[&real, &imag])));
    }
    if let Ok(text) = value.cast_exact::<PyString>() {
        return Ok(Step::Done(str_digest(text)?));
    }
    if let Ok(bytes) = value.cast_exact::<PyBytes>() {
        return Ok(Step::Done(digest(Kind::Bytes, &[bytes.as_bytes()])));
    }
    if let Ok(array) = value.cast_exact::<PyByteArray>() {
        return Ok(Step::Done(digest(Kind::ByteArray, &[&array.to_vec()])));
    }
    if let Ok(tuple) = value.cast_exact::<PyTuple>() {
        return Ok(Step::Open(Frame::new(Kind::Tuple, tuple.iter().collect())));
    }
    if let Ok(list) = value.cast_exact::<PyList>() {
        return Ok(Step::Open(Frame::new(Kind::List, list.iter().collect())));
    }
    if let Ok(dict) = value.cast_exact::<PyDict>() {
        // Copied out first: the walk calls back into Python, which may change the dict.
        let mut children = Vec::with_capacity(dict.len() * 2);
        for (key, item) in dict.iter() {
            children.push(key);
            children.push(item);
        }
        return Ok(Step::Open(Frame::new(Kind::Dict, children)));
    }
    if let Ok(set) = value.cast_exact::<PySet>() {
        return Ok(Step::Open(Frame::new(Kind::Set, set.iter().collect())));
    }
    if let Ok(set) = value.cast_exact::<PyFrozenSet>() {
        return Ok(Step::Open(Frame::new(
            Kind::FrozenSet,
            set.iter().collect(),
        )));
    }
    if let Ok(view) = value.cast_exact::<PyMemoryView>() {
        let digest = buffer_digest(view);
        return unreadable_refused(digest, value, ancestors, current).map(Step::Done);
    }
    let form = fallback.call1((value,))?;
    if form.is_none() {
        return Err(refused(value, &path(ancestors, current), ""));
    }
    let mut frame = Frame::new(Kind::Canonical, vec![form]);
    frame.head = Some(value.get_type().fully_qualified_name()?.to_string());
    Ok(Step::Open(frame))
}

/// `digest`, or, when `value`'s buffer could not be read (a released view, a format
/// Python cannot copy), an `UnfingerprintableError` saying why.
fn unreadable_refused(
    digest: PyResult<Digest>,
    value: &Bound<'_, PyAny>,
    ancestors: &[Frame<'_>],
    current: Option<&Frame<'_>>,
) -> PyResult<Digest> {
    let py = value.py();
    digest.map_err(|error| {
        let unreadable = [
            py.get_type::<PyValueError>(),
            py.get_type::<PyNotImplementedError>(),
            py.get_type::<PyBufferError>(),
            py.get_type::<PyTypeError>(),
        ]
        .iter()
        .any(|kind| error.is_instance(py, kind));
        if unreadable {
            refused(value, &path(ancestors, current), &format!(": {error}"))
        } else {
            error
        }
    })
}

/// `value`'s bit pattern, little-endian, with every NaN made the same.
fn float_bits(value: f64) -> [u8; 8] {
    let value = if value.is_nan() { f64::NAN } else { value };
    value.to_bits().to_le_bytes()
}

/// An exact `int`'s minimal two's-complement bytes, little-endian, whatever its size.
fn int_bytes(value: &Bound<'_, PyAny>) -> PyResult<Vec<u8>> {
    let py = value.py();
    let bytes = if let Ok(small) = value.extract::<i64>() {
        small.to_le_bytes().to_vec()
    } else {
        let bits: usize = value.call_method0(intern!(py, "bit_length"))?.extract()?;
        let kwargs = PyDict::new(py);
        kwargs.set_item(intern!(py, "signed"), true)?;
        value
            .call_method(
                intern!(py, "to_bytes"),
                (bits / 8 + 1, intern!(py, "little")),
                Some(&kwargs),
            )?
            .cast_into::<PyBytes>()?
            .as_bytes()
            .to_vec()
    };
    Ok(minimal_signed(bytes))
}

/// Two's-complement little-endian `bytes` without the redundant sign bytes at the top.
fn minimal_signed(mut bytes: Vec<u8>) -> Vec<u8> {
    while let [.., below, top] = bytes[..] {
        let redundant = (top == 0x00 && below & 0x80 == 0) || (top == 0xff && below & 0x80 != 0);
        if !redundant {
            break;
        }
        bytes.pop();
    }
    bytes
}

/// A `str`'s digest: its UTF-8, or, for a string holding lone surrogates, the bytes
/// `surrogatepass` encodes, which no other string's UTF-8 equals.
fn str_digest(text: &Bound<'_, PyString>) -> PyResult<Digest> {
    if let Ok(utf8) = text.to_str() {
        return Ok(digest(Kind::Str, &[utf8.as_bytes()]));
    }
    let py = text.py();
    let encoded = text
        .call_method1(
            intern!(py, "encode"),
            (intern!(py, "utf-8"), intern!(py, "surrogatepass")),
        )?
        .cast_into::<PyBytes>()?;
    Ok(digest(Kind::Str, &[encoded.as_bytes()]))
}

/// A memoryview's digest: its format, shape and bytes in C order.
fn buffer_digest(view: &Bound<'_, PyMemoryView>) -> PyResult<Digest> {
    let py = view.py();
    let format: String = view.getattr(intern!(py, "format"))?.extract()?;
    let shape: Vec<u64> = view.getattr(intern!(py, "shape"))?.extract()?;
    let nbytes: usize = view.getattr(intern!(py, "nbytes"))?.extract()?;
    let mut framer = Framer::new(Kind::MemoryView);
    framer.part(format.as_bytes());
    let shape: Vec<u8> = shape.iter().flat_map(|dim| dim.to_le_bytes()).collect();
    framer.part(&shape);
    framer.begin_part(nbytes);
    let contiguous = view.getattr(intern!(py, "c_contiguous"))?.is_truthy()?;
    // A C-contiguous view casts to flat bytes, copied out a chunk at a time; anything
    // else (a strided view, or a format `cast` refuses) is copied whole, in C order.
    let flat = if contiguous {
        view.call_method1(intern!(py, "cast"), (intern!(py, "B"),))
            .ok()
    } else {
        None
    };
    if let Some(flat) = flat {
        let mut chunk = vec![0; nbytes.min(CHUNK)];
        let mut start = 0;
        while start < nbytes {
            let end = nbytes.min(start + CHUNK);
            let slice = PySlice::new(py, isize::try_from(start)?, isize::try_from(end)?, 1);
            let part = PyBuffer::<u8>::get(&flat.get_item(slice)?)?;
            let target = &mut chunk[..end - start];
            part.copy_to_slice(py, target)?;
            framer.extend(target);
            start = end;
        }
    } else {
        let bytes = view
            .call_method0(intern!(py, "tobytes"))?
            .cast_into::<PyBytes>()?;
        framer.extend(bytes.as_bytes());
    }
    Ok(framer.finish())
}
