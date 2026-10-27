"""Tests for value fingerprints: typed, by content, refused for unknown values."""

import contextlib
import enum
import importlib
import os
import subprocess
import sys
from array import array
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from decimal import Decimal
from pathlib import PurePosixPath, PureWindowsPath
from typing import NamedTuple, override
from zoneinfo import ZoneInfo

import pytest

from fnloom import UnfingerprintableError, _core, fingerprint, register_fingerprint

# Imported up front when installed: the shared fixture forgets the modules a test
# imports first, and numpy cannot be imported twice in one process.
for _name in ("numpy", "pandas", "polars"):
    with contextlib.suppress(ImportError):
        importlib.import_module(_name)

DIGEST_SIZE = 32

# A value that exercises sets, dicts, strings and nesting, for the hash-seed check.
_SEEDED = "[{'x', 'y', 'z', 'w'}, {'k': frozenset({1, 'a', (2, 'b')})}, 'text']"


def _fingerprint_in_subprocess(seed: str) -> str:
    """The hex fingerprint of `_SEEDED`, taken by a fresh interpreter with `seed`."""
    code = f"import fnloom; print(fnloom.fingerprint({_SEEDED}).hex())"
    result = subprocess.run(  # noqa: S603  # runs this interpreter on fixed code
        [sys.executable, "-c", code],
        capture_output=True,
        check=True,
        text=True,
        env={**os.environ, "PYTHONHASHSEED": seed},
    )
    return result.stdout.strip()


def test_fingerprint_is_32_bytes() -> None:
    """Fingerprints and framed hashes are 32-byte BLAKE3 digests."""
    assert len(fingerprint(None)) == DIGEST_SIZE
    assert len(_core.hash_parts([b"a", b"bc"])) == DIGEST_SIZE


def test_hash_parts_frames_each_part() -> None:
    """Each part is length-prefixed, so parts never run together."""
    assert _core.hash_parts([b"ab", b"c"]) != _core.hash_parts([b"a", b"bc"])
    assert _core.hash_parts([b"ab", b"c"]) == _core.hash_parts([b"ab", b"c"])
    assert _core.hash_parts([]) != _core.hash_parts([b""])


def test_type_is_part_of_the_fingerprint() -> None:
    """Values that print alike but differ in type have distinct fingerprints."""
    values: list[object] = [
        1,
        1.0,
        True,
        "1",
        b"1",
        bytearray(b"1"),
        (1,),
        [1],
        {1},
        frozenset({1}),
        {1: 1},
        {"1": 1},
    ]
    assert len({fingerprint(value) for value in values}) == len(values)


def test_equal_values_share_a_fingerprint() -> None:
    """Equal values share a fingerprint, whatever the order or hash seed."""
    assert fingerprint([1, "a", (2.5, None)]) == fingerprint([1, "a", (2.5, None)])
    assert fingerprint({2, 1}) == fingerprint({1, 2})
    assert _fingerprint_in_subprocess("1") == _fingerprint_in_subprocess("2")


def test_ints_of_any_size() -> None:
    """Ints beyond 64 bits fingerprint by value, sign included."""
    big = 2**200
    assert fingerprint(big) == fingerprint(2**200)
    assert fingerprint(big) != fingerprint(-big)
    assert fingerprint(big) != fingerprint(big + 1)
    assert fingerprint(-1) != fingerprint(255)
    assert fingerprint(2**63) != fingerprint(-(2**63))


def test_float_bits() -> None:
    """Floats fingerprint by bit pattern, with every NaN the same."""
    assert fingerprint(0.0) != fingerprint(-0.0)
    assert fingerprint(float("nan")) == fingerprint(-float("nan"))


def test_dict_order_counts() -> None:
    """A dict fingerprints its entries in insertion order."""
    assert fingerprint({"a": 1, "b": 2}) != fingerprint({"b": 2, "a": 1})


class _Colour(enum.Enum):
    RED = 1


@dataclass(frozen=True)
class _P:
    x: int


@dataclass(frozen=True)
class _Q:
    x: int


class _Pair(NamedTuple):
    a: int
    b: int


def test_stdlib_values() -> None:
    """Standard-library values fingerprint by type and content."""
    naive = datetime(2026, 1, 1)  # noqa: DTZ001  # the naive value is the point
    assert fingerprint(datetime(2026, 1, 1, tzinfo=UTC)) != fingerprint(naive)
    assert fingerprint(Decimal("1.0")) != fingerprint(Decimal("1.00"))
    assert fingerprint(PurePosixPath("a")) != fingerprint(PureWindowsPath("a"))
    assert fingerprint(_Colour.RED) != fingerprint(1)
    assert fingerprint(_P(1)) != fingerprint(_Q(1))
    assert fingerprint(_P(1)) == fingerprint(_P(1))
    assert fingerprint(_Pair(1, 2)) != fingerprint((1, 2))


class _Offset(tzinfo):
    """A time zone fnloom does not know."""

    @override
    def utcoffset(self, dt: datetime | None) -> timedelta:
        return timedelta(hours=-5)

    @override
    def tzname(self, dt: datetime | None) -> str:
        return "EST"

    @override
    def dst(self, dt: datetime | None) -> timedelta:
        return timedelta(0)


class _Stamp(datetime):
    """A datetime subclass, which may hold state the datetime form leaves out."""


def test_time_zones_and_fold() -> None:
    """A datetime fingerprints its zone's identity and its fold."""
    instant = datetime(2026, 1, 1, tzinfo=ZoneInfo("America/New_York"))
    fixed = datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=-5), "EST"))
    assert fingerprint(instant) != fingerprint(fixed)
    assert fingerprint(instant) == fingerprint(
        datetime(2026, 1, 1, tzinfo=ZoneInfo("America/New_York"))
    )
    naive = datetime(2026, 11, 1, 1, 30)  # noqa: DTZ001  # the naive value is the point
    assert fingerprint(naive) != fingerprint(naive.replace(fold=1))
    with pytest.raises(UnfingerprintableError):
        fingerprint(datetime(2026, 1, 1, tzinfo=_Offset()))
    with pytest.raises(UnfingerprintableError):
        fingerprint(_Stamp(2026, 1, 1))  # zone does not matter here


@dataclass
class _NoFields:
    """A dataclass whose state lives outside its (absent) fields."""

    def __init__(self, x: int) -> None:
        self.x = x


@dataclass(frozen=True)
class _Declared:
    """A dataclass whose own declaration overrides its fields."""

    x: int
    noise: int

    def __fnloom_fingerprint__(self) -> int:
        return self.x


@dataclass(frozen=True)
class _Unset:
    """A dataclass with a field its `__init__` never sets."""

    x: int
    y: int = field(init=False)


def test_a_dataclass_with_an_unset_field_is_refused() -> None:
    """A field never set has no value to fingerprint, so the instance is refused."""
    with pytest.raises(UnfingerprintableError):
        fingerprint(_Unset(1))


def test_declarations_win_and_fieldless_dataclasses_are_refused() -> None:
    """A type's own declaration beats its record form; no fields means refused."""
    with pytest.raises(UnfingerprintableError):
        fingerprint(_NoFields(1))
    assert fingerprint(_Declared(1, 2)) == fingerprint(_Declared(1, 3))


@dataclass
class _Scaled:
    """A dataclass with state its `__post_init__` sets beyond its fields."""

    x: float

    def __post_init__(self) -> None:
        self.scale = 1.0


@dataclass(frozen=True)
class _Slotted:
    """A dataclass with a field."""

    x: int


class _SlottedMore(_Slotted):
    """A subclass with a private slot that is not a field."""

    __slots__ = ("__extra",)


class _Labelled(NamedTuple):
    a: int


class _LabelledMore(_Labelled):
    """A named tuple subclass with a `__dict__`."""


class _Blob(bytes):
    """A bytes subclass, which may hold attributes."""


def test_state_beyond_a_records_fields_counts() -> None:
    """Attributes set beyond the fields are part of the form, or refuse it."""
    a, b = _Scaled(2.0), _Scaled(2.0)
    assert fingerprint(a) == fingerprint(b)
    b.scale = 10.0
    assert fingerprint(a) != fingerprint(b)
    b.scale = 1.0
    assert fingerprint(a) == fingerprint(b)
    vars(b)["scale"] = object()
    with pytest.raises(UnfingerprintableError):
        fingerprint(b)
    # A slot set beyond the fields counts; one never set does not.
    one, two = _SlottedMore(1), _SlottedMore(1)
    assert fingerprint(one) == fingerprint(two)
    object.__setattr__(two, "_SlottedMore__extra", 5)
    assert fingerprint(one) != fingerprint(two)
    plain, labelled = _LabelledMore(1), _LabelledMore(1)
    labelled.note = "x"  # ty: ignore[unresolved-attribute]
    assert fingerprint(plain) != fingerprint(labelled)


def test_a_buffer_subclass_counts_its_attributes() -> None:
    """A bytes subclass's `__dict__` is part of its form, or refuses it."""
    plain, tagged = _Blob(b"ab"), _Blob(b"ab")
    assert fingerprint(plain) == fingerprint(tagged)
    tagged.tag = 1  # ty: ignore[unresolved-attribute]
    assert fingerprint(plain) != fingerprint(tagged)
    tagged.tag = object()  # ty: ignore[unresolved-attribute]
    with pytest.raises(UnfingerprintableError):
        fingerprint(tagged)


def test_other_buffers_fingerprint_by_type_format_and_bytes() -> None:
    """An unknown object with a C-contiguous buffer fingerprints by type and buffer."""
    assert fingerprint(array("i", [1])) == fingerprint(array("i", [1]))
    assert fingerprint(array("i", [1])) != fingerprint(array("i", [2]))
    assert fingerprint(array("b", [1])) != fingerprint(bytes([1]))
    assert fingerprint(array("b", [1])) != fingerprint(memoryview(bytes([1])))


def test_unreadable_buffer_is_refused() -> None:
    """A released memoryview is refused with its path, not a ValueError."""
    view = memoryview(b"ab")
    view.release()
    with pytest.raises(UnfingerprintableError) as caught:
        fingerprint([view])
    assert caught.value.path == "[0]"


def test_unknown_type_is_refused_at_any_depth() -> None:
    """An unknown value is refused wherever it sits, and the error says where."""
    with pytest.raises(UnfingerprintableError) as caught:
        fingerprint([1, {"k": object()}])
    assert caught.value.path == '[1]["k"]'
    assert caught.value.type_name == "object"
    assert isinstance(caught.value, TypeError)
    assert '[1]["k"]' in str(caught.value)


def test_unknown_type_at_the_top_has_an_empty_path() -> None:
    """A refused top-level value has an empty path."""
    with pytest.raises(UnfingerprintableError) as caught:
        fingerprint(object())
    assert caught.value.path == ""


def test_deep_nesting_is_refused_cleanly() -> None:
    """Nesting past the walk's depth limit is refused, not a crash."""
    value: list[object] = []
    for _ in range(100_000):
        value = [value]
    with pytest.raises(UnfingerprintableError, match="nested"):
        fingerprint(value)


def test_protocol_and_registry() -> None:
    """The fingerprint protocol and the registry supply canonical forms."""

    class Own:
        def __init__(self, x: int, noise: int) -> None:
            self.x = x
            self.noise = noise

        def __fnloom_fingerprint__(self) -> tuple[int]:
            return (self.x,)

    assert fingerprint(Own(1, 2)) == fingerprint(Own(1, 3))
    assert fingerprint(Own(1, 2)) != fingerprint(Own(2, 2))

    class Foreign:
        def __init__(self, v: int) -> None:
            self.v = v

    class Sub(Foreign):
        pass

    class Special(Foreign):
        pass

    with pytest.raises(UnfingerprintableError):
        fingerprint(Foreign(1))
    register_fingerprint(Foreign, lambda f: f.v)
    assert fingerprint(Foreign(1)) == fingerprint(Foreign(1))
    assert fingerprint(Foreign(1)) != fingerprint(Foreign(2))
    assert fingerprint(Sub(1)) == fingerprint(Sub(1))
    assert fingerprint(Sub(1)) != fingerprint(Sub(2))

    register_fingerprint(Special, lambda _: 0)
    assert fingerprint(Special(1)) == fingerprint(Special(2))


def test_numpy_arrays() -> None:
    """Arrays fingerprint by dtype, shape and C-order bytes, or element-wise."""
    np = pytest.importorskip("numpy")
    assert fingerprint(np.zeros((3, 2))) != fingerprint(np.zeros((2, 3)))
    assert fingerprint(np.zeros((3, 2))) != fingerprint(
        np.zeros((3, 2), dtype="float32")
    )
    a = np.arange(12).reshape(3, 4)
    assert fingerprint(a[:, ::2]) == fingerprint(np.ascontiguousarray(a[:, ::2]))
    assert fingerprint(a) != fingerprint(a + 1)
    with pytest.raises(UnfingerprintableError):
        fingerprint(np.array([object()], dtype=object))
    masked = np.ma.array([1, 2], mask=[0, 1])
    with pytest.raises(UnfingerprintableError):
        fingerprint(masked)
    structured = np.zeros(2, dtype=[("a", "i4"), ("o", object)])
    with pytest.raises(UnfingerprintableError) as caught:
        fingerprint(structured)
    assert "nested" not in str(caught.value)
    assert fingerprint(np.float64(1.5)) == fingerprint(np.float64(1.5))


def test_pandas_frames() -> None:
    """Frames and series fingerprint by names, dtypes, index and values."""
    pd = pytest.importorskip("pandas")
    frame = pd.DataFrame({"x": [1, 2]})
    assert fingerprint(frame) == fingerprint(pd.DataFrame({"x": [1, 2]}))
    assert fingerprint(frame) != fingerprint(pd.DataFrame({"y": [1, 2]}))
    assert fingerprint(frame) != fingerprint(pd.DataFrame({"x": [1, 2]}, index=[5, 6]))
    series = pd.Series([1, 2])
    assert fingerprint(series) != fingerprint(pd.Series([1, 2], name="s"))
    strings = pd.DataFrame({"s": ["a", "b"]}, dtype=object)
    assert strings["s"].dtype == object
    assert fingerprint(strings) == fingerprint(
        pd.DataFrame({"s": ["a", "b"]}, dtype=object)
    )
    assert fingerprint(strings) != fingerprint(pd.DataFrame({"s": ["a", "b"]}))
    with pytest.raises(UnfingerprintableError):
        fingerprint(pd.DataFrame({"o": [object()]}))


def test_pandas_index_dtype_counts() -> None:
    """An index's dtype is part of its fingerprint, per level for a MultiIndex."""
    pd = pytest.importorskip("pandas")
    assert fingerprint(pd.Index([1, 2], dtype="Int64")) != fingerprint(
        pd.Index([1, 2], dtype="int64")
    )
    assert fingerprint(pd.Index(["a"], dtype=object)) != fingerprint(
        pd.Index(["a"], dtype="str")
    )
    assert fingerprint(pd.Series([1], index=pd.Index([0], dtype="Int64"))) != (
        fingerprint(pd.Series([1], index=pd.Index([0], dtype="int64")))
    )
    level = pd.Index(["a"], dtype=object)
    assert fingerprint(pd.MultiIndex.from_arrays([[1], level])) != fingerprint(
        pd.MultiIndex.from_arrays([[1], pd.Index(["a"], dtype="str")])
    )


def test_pandas_scalars() -> None:
    """Timestamps and timedeltas fingerprint by value, unit and zone."""
    pd = pytest.importorskip("pandas")
    assert fingerprint(pd.Timedelta(1, "ns")) != fingerprint(pd.Timedelta(2, "ns"))
    assert fingerprint(pd.Timedelta(1, "ns")) == fingerprint(pd.Timedelta(1, "ns"))
    stamp = pd.Timestamp("2026-01-01 00:00:00.000000001")
    assert fingerprint(stamp) != fingerprint(pd.Timestamp("2026-01-01"))
    assert fingerprint(pd.Timestamp("2026-01-01", tz="UTC")) != fingerprint(
        pd.Timestamp("2026-01-01", tz="America/New_York").tz_convert("UTC")
        + pd.Timedelta(1, "h")
    )
    assert fingerprint(pd.Timestamp("2026-01-01", tz="UTC")) != fingerprint(
        pd.Timestamp("2026-01-01")
    )


def test_polars_frames() -> None:
    """Polars frames fingerprint their column names."""
    pl = pytest.importorskip("polars")
    assert fingerprint(pl.DataFrame({"x": [1, 2]})) != fingerprint(
        pl.DataFrame({"y": [1, 2]})
    )
    nulls = pl.Series("f", [None], dtype=pl.Float64)
    nans = pl.Series("f", [float("nan")], dtype=pl.Float64)
    assert fingerprint(nulls) != fingerprint(nans)
    assert fingerprint(nulls.to_frame()) != fingerprint(nans.to_frame())


def test_python_never_imports_numpy() -> None:
    """Fingerprinting never imports numpy itself."""
    code = "import sys, fnloom; fnloom.fingerprint([1]); print('numpy' in sys.modules)"
    result = subprocess.run(  # noqa: S603  # runs this interpreter on fixed code
        [sys.executable, "-c", code], capture_output=True, check=True, text=True
    )
    assert result.stdout.strip() == "False"
