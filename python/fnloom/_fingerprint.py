"""Fingerprints of values, and canonical forms for the types the Rust walk lacks.

`fingerprint(value)` hashes built-in values in Rust and calls `fallback` for any other
value. `fallback` returns a canonical form, which the walk fingerprints in turn under
the value's type, or `None`, which refuses the value. numpy, pandas and polars are never
imported here: their values are recognised by module name and read through the modules
already loaded.
"""

import contextlib
import dataclasses
import enum
import sys
import threading
import weakref
from collections.abc import Callable  # noqa: TC003  # `inspect.signature` evaluates it
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from fractions import Fraction
from pathlib import (
    Path,
    PosixPath,
    PurePath,
    PurePosixPath,
    PureWindowsPath,
    WindowsPath,
)
from typing import TYPE_CHECKING, Any
from uuid import UUID
from zoneinfo import ZoneInfo

from fnloom import _core

if TYPE_CHECKING:
    from collections.abc import Generator

# Registered canonical forms, by the exact type they were registered for.
_REGISTRY: dict[type, Callable[[Any], object]] = {}

# What `getattr` gives for a dataclass field never set.
_UNSET = object()

# The code version of each class whose values were fingerprinted: `b""` for a library
# class, `None` for a user class refused. Cleared when a cached driver is built, so a
# rebuild after an edit reads the class again.
_CLASS_VERSIONS: weakref.WeakKeyDictionary[type, bytes | None] = (
    weakref.WeakKeyDictionary()
)


# Whether this thread is in a code-version walk. The walk versions the classes inside
# every value it fingerprints itself, so their fingerprints leave them out: else an
# enum's version, which fingerprints its members, would ask for itself again.
class _Walking(threading.local):
    """How many code-version walks this thread is inside."""

    depth: int = 0


_WALKING = _Walking()


@contextlib.contextmanager
def classes_versioned_by_caller() -> Generator[None]:
    """Leaves class versions out of fingerprints taken inside the block."""
    depth = _WALKING.depth
    _WALKING.depth = depth + 1
    try:
        yield
    finally:
        _WALKING.depth = depth


def forget_class_versions() -> None:
    """Drops every remembered class version, for a driver built after an edit."""
    _CLASS_VERSIONS.clear()


def fingerprint(value: object) -> bytes:
    """The 32-byte fingerprint of `value`'s type and content.

    Raises:
        UnfingerprintableError: `value`, or a value inside it, cannot be fingerprinted;
            its `path` and `type_name` say which.
    """
    return _core.fingerprint(value, fallback)


def register_fingerprint[T](cls: type[T], fn: Callable[[T], object]) -> None:
    """Fingerprints instances of `cls` and its subclasses by what `fn` returns.

    `fn` returns a value that can itself be fingerprinted. Of several registrations, the
    one for the most specific class in an instance's MRO applies.
    """
    _REGISTRY[cls] = fn


def fallback(value: object) -> object | None:
    """A canonical form of `value`, a type the Rust walk does not know, or `None`.

    The form is fingerprinted under `value`'s type, so it holds only the content. A
    type's own declaration, `__fnloom_fingerprint__` or a registration, comes first.
    A value of a user class carries the class's code version too: a node may call its
    methods, so the same content under edited code is another value.
    """
    form = _form(value)
    if form is None:
        return None
    version = _class_version(type(value))
    if version is None:
        return None
    return form if version == b"" else (version, form)


def _class_version(cls: type) -> bytes | None:
    """A user class's code version, `b""` for a library class, `None` if refused."""
    from fnloom import _codeversion  # noqa: PLC0415  # it imports this module

    if _WALKING.depth:
        return b""
    try:
        return _CLASS_VERSIONS[cls]
    except KeyError:
        pass
    module = sys.modules.get(cls.__module__)
    version: bytes | None = b""
    if module is not None and _codeversion.is_user_module(module):
        try:
            version = _codeversion.class_version(cls)
        except _codeversion.UncacheableError:
            version = None
    _CLASS_VERSIONS[cls] = version
    return version


def _form(value: object) -> object | None:
    """`value`'s canonical form by its declaration, a registration or its kind."""
    own = getattr(type(value), "__fnloom_fingerprint__", None)
    if own is not None:
        return own(value)
    for cls in type(value).__mro__:
        registered = _REGISTRY.get(cls)
        if registered is not None:
            return registered(value)
    library = _LIBRARIES.get(type(value).__module__.partition(".")[0])
    if library is not None:
        # A library value is known to its branch or refused: never read as a bare
        # buffer, which would drop a subclass's state (a masked array's mask, say).
        return library(value)
    for known in (_stdlib, _record, _buffer):
        form = known(value)
        if form is not None:
            return form
    return None


# Path types whose state is all in `parts`; a subclass may hold more, so it is refused.
_PATHS = frozenset(
    {PurePath, PurePosixPath, PureWindowsPath, Path, PosixPath, WindowsPath}
)


def _stdlib(value: object) -> object | None:  # noqa: PLR0911  # one return per type
    """The canonical form of a standard-library value, or `None` for anything else.

    Types match exactly, since a subclass may hold state the form leaves out; enum
    members are the exception, their state being their value.
    """
    exact = type(value)
    match value:
        case enum.Enum():
            return (f"{exact.__qualname__}.{value.name}", value.value)
        case datetime() | time() if exact in {datetime, time}:
            zone = _tz_form(value.tzinfo)
            return None if zone is None else (value.isoformat(), value.fold, zone)
        case date() if exact is date:
            return value.isoformat()
        case timedelta() if exact is timedelta:
            return (value.days, value.seconds, value.microseconds)
        case Decimal() if exact is Decimal:
            return value.as_tuple()
        case Fraction() if exact is Fraction:
            return (value.numerator, value.denominator)
        case UUID() if exact is UUID:
            return value.bytes
        case PurePath() if exact in _PATHS:
            return value.parts
        case _:
            return None


def _tz_form(zone: object) -> object | None:
    """A time zone's canonical form: `("naive",)` without one, `None` if unknown.

    A `ZoneInfo` by its key and a fixed `timezone` by offset and name: two zones with
    the same offset today may differ at another date, so the offset alone is not enough.
    """
    if zone is None:
        return ("naive",)
    if type(zone) is ZoneInfo:
        # A zone read from a file has no key (typeshed says `str`), so nothing names it.
        key = getattr(zone, "key", None)
        return None if key is None else ("zoneinfo", key)
    if type(zone) is timezone:
        return ("fixed", zone.utcoffset(None), zone.tzname(None))
    return None


def _record(value: object) -> object | None:
    """A named tuple's or dataclass instance's fields, or `None` for anything else.

    State set beyond the fields (by `__post_init__`, say, or later) follows them, by
    name. A dataclass without fields keeps its state, if any, elsewhere, so it is
    refused; so is one with a field never set (`init=False` without a default).
    """
    if isinstance(value, tuple) and hasattr(type(value), "_fields"):
        items = tuple(value)
        names = frozenset(type(value)._fields)  # ty: ignore[unresolved-attribute]
    elif dataclasses.is_dataclass(value) and not isinstance(value, type):
        fields = dataclasses.fields(value)
        if not fields:
            return None
        items = tuple(getattr(value, field.name, _UNSET) for field in fields)
        if any(item is _UNSET for item in items):
            return None
        names = frozenset(field.name for field in fields)
    else:
        return None
    extra = _extra_state(value, names)
    return items if not extra else (*items, extra)


def _extra_state(value: object, known: frozenset[str]) -> tuple[object, ...]:
    """`value`'s instance attributes outside `known`, as sorted `(name, value)` pairs.

    Both its `__dict__` and any slots set, from every class in its MRO.
    """
    state: dict[str, object] = {
        name: item
        for name, item in getattr(value, "__dict__", {}).items()
        if name not in known
    }
    for cls in type(value).__mro__:
        slots = cls.__dict__.get("__slots__", ())
        for slot in (slots,) if isinstance(slots, str) else slots:
            if slot in {"__dict__", "__weakref__"}:
                continue
            # A private slot is stored under its mangled name.
            name = (
                f"_{cls.__name__.lstrip('_')}{slot}"
                if slot.startswith("__") and not slot.endswith("__")
                else slot
            )
            if name in known or name in state:
                continue
            item = getattr(value, name, _UNSET)
            if item is not _UNSET:
                state[name] = item
    return tuple(sorted(state.items()))


def _buffer(value: object) -> object | None:
    """Any other object's C-contiguous buffer, as a memoryview, or `None`.

    A subclass instance's attributes follow the buffer, by name.
    """
    try:
        view = memoryview(value)  # ty: ignore[invalid-argument-type]
    except TypeError, ValueError:
        return None
    if not view.c_contiguous:
        return None
    extra = _extra_state(value, frozenset())
    return view if not extra else (view, extra)


def _numpy(value: object) -> object | None:
    """An array's or numpy scalar's form: dtype, shape, strides and bytes, or items."""
    np = sys.modules["numpy"]
    if isinstance(value, np.generic):
        value = np.asarray(value)
    # Exactly an ndarray: a subclass (a masked array, say) holds state beyond its data.
    if type(value) is not np.ndarray:
        return None
    if value.dtype.hasobject:
        if value.dtype.names is not None:
            # Its elements would be structured scalars of the same dtype, without end.
            return None
        return (value.shape, list(value.flat))
    # A structured dtype's `str` is only its size; `descr` names its fields.
    dtype = value.dtype.str if value.dtype.names is None else value.dtype.descr
    # Flat bytes in C order: every dtype exports them, including those (datetimes)
    # the buffer protocol cannot describe. The strides keep the layout, which a node
    # can read (`ravel(order="K")`, `.flags`): a Fortran-ordered array or a strided
    # view is not its C-ordered copy.
    data = np.ascontiguousarray(value).reshape(-1).view(np.uint8).data
    return (dtype, value.shape, value.strides, data)


def _pandas(value: object) -> object | None:  # noqa: PLR0911  # one return per type
    """A pandas frame's, series', index's or scalar's canonical form, or `None`."""
    pd = sys.modules["pandas"]
    match value:
        case pd.DataFrame():
            columns = [value.iloc[:, i] for i in range(value.shape[1])]
            # The columns as an index, so labels of any type keep their dtype.
            return (
                value.columns,
                [_pandas_dtype(column.dtype) for column in columns],
                value.index,
                [column.to_numpy() for column in columns],
                value.attrs,
            )
        case pd.Series():
            return (
                value.name,
                _pandas_dtype(value.dtype),
                value.index,
                value.to_numpy(),
                value.attrs,
            )
        case pd.MultiIndex():
            dtypes = [_pandas_dtype(dtype) for dtype in value.dtypes]
            return (list(value.names), dtypes, value.to_numpy())
        case pd.Index():
            # A datetime or period index's frequency is state its values do not show.
            frequency = getattr(value, "freqstr", None)
            return (value.name, _pandas_dtype(value.dtype), value.to_numpy(), frequency)
        case pd.Timestamp():
            zone = _tz_form(value.tzinfo)
            if zone is None:
                return None
            return (value.value, value.unit, value.fold, zone)
        case pd.Timedelta():
            return (value.value, value.unit)
        case _:
            return None


def _pandas_dtype(dtype: Any) -> object:  # noqa: ANN401  # numpy or pandas dtype
    """A column dtype's canonical form.

    A numpy dtype by its `str`; an extension dtype by its type and name, and a
    categorical's by its categories too, which its values alone do not show.
    """
    np = sys.modules["numpy"]
    if isinstance(dtype, np.dtype):
        return dtype.str
    pd = sys.modules["pandas"]
    if isinstance(dtype, pd.CategoricalDtype):
        return (str(dtype), dtype.categories, dtype.ordered)
    return (type(dtype).__qualname__, str(dtype))


def _polars(value: object) -> object | None:
    """A polars frame's or series' canonical form, or `None`."""
    pl = sys.modules["polars"]
    match value:
        case pl.DataFrame():
            return (
                value.columns,
                [str(dtype) for dtype in value.dtypes],
                [_polars_series(column) for column in value.get_columns()],
            )
        case pl.Series():
            return (value.name, str(value.dtype), *_polars_series(value))
        case _:
            return None


def _polars_series(series: Any) -> tuple[object, object]:  # noqa: ANN401  # a polars Series
    """A polars series' values and null mask: `to_numpy` turns a null into a NaN."""
    return (series.to_numpy(), series.is_null().to_numpy())


# The libraries recognised by the root of a value's module, and their branches.
_LIBRARIES: dict[str, Callable[[object], object | None]] = {
    "numpy": _numpy,
    "pandas": _pandas,
    "polars": _polars,
}
