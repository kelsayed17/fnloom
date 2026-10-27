"""Fingerprints of values, and canonical forms for the types the Rust walk lacks.

`fingerprint(value)` hashes built-in values in Rust and calls `fallback` for any other
value. `fallback` returns a canonical form, which the walk fingerprints in turn under
the value's type, or `None`, which refuses the value. numpy, pandas and polars are never
imported here: their values are recognised by module name and read through the modules
already loaded.
"""

import dataclasses
import enum
import sys
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
    from collections.abc import Callable

# Registered canonical forms, by the exact type they were registered for.
_REGISTRY: dict[type, Callable[[Any], object]] = {}


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
    """
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

    A dataclass without fields keeps its state, if any, elsewhere, so it is refused.
    """
    if isinstance(value, tuple) and hasattr(type(value), "_fields"):
        return tuple(value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        fields = dataclasses.fields(value)
        if not fields:
            return None
        return tuple(getattr(value, field.name) for field in fields)
    return None


def _buffer(value: object) -> object | None:
    """Any other object's C-contiguous buffer, as a memoryview, or `None`."""
    try:
        view = memoryview(value)  # ty: ignore[invalid-argument-type]
    except TypeError, ValueError:
        return None
    return view if view.c_contiguous else None


def _numpy(value: object) -> object | None:
    """An array's or numpy scalar's form: dtype, shape and bytes, or its elements."""
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
    # the buffer protocol cannot describe.
    data = np.ascontiguousarray(value).reshape(-1).view(np.uint8).data
    return (dtype, value.shape, data)


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
            )
        case pd.Series():
            return (
                value.name,
                _pandas_dtype(value.dtype),
                value.index,
                value.to_numpy(),
            )
        case pd.MultiIndex():
            dtypes = [_pandas_dtype(dtype) for dtype in value.dtypes]
            return (list(value.names), dtypes, value.to_numpy())
        case pd.Index():
            return (value.name, _pandas_dtype(value.dtype), value.to_numpy())
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
