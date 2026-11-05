"""`frame(...)`: a result builder making one pandas or polars frame (Q144).

fnloom never imports either library itself: `frame` imports the one it is asked for
when the builder is made, and the builder reads it back from `sys.modules` (or
imports it again in a process that unpickled the builder).
"""

import importlib
from collections.abc import Hashable, Mapping, Sized
from typing import Any, Final, Literal, override

type Library = Literal["pandas", "polars"]
type Join = Literal["strict", "outer"]

_LIBRARIES: Final = ("pandas", "polars")
_JOINS: Final = ("strict", "outer")


def frame(
    library: Library = "pandas",
    *,
    flatten: bool = True,
    separator: str = ".",
    join: Join = "strict",
) -> FrameBuilder:
    """A result builder making one `library` frame of a run's outputs.

    A frame output becomes one column per column, named `<output><separator><column>`
    (its own column names with `flatten=False`), in request order and then column
    order; a series or a list is one column; a scalar is broadcast; outputs that are
    all scalars make one row. `join="strict"` refuses pandas outputs whose indexes
    differ, or polars outputs whose lengths differ, with a `ValueError` naming them.
    `join="outer"` aligns pandas outputs on the union of their indexes, as Hamilton's
    `PandasDataFrameResult` does, without logging; polars, which has no index,
    refuses it. Two columns of one name are a `ValueError` naming both.

    The library is imported here, never by fnloom itself: one that can't be imported
    is an `ImportError`. A bad argument is a `ValueError` or `TypeError` naming it.
    """
    return FrameBuilder(library, flatten=flatten, separator=separator, join=join)


class FrameBuilder:
    """What `frame(...)` returns: call it with the outputs to get the frame.

    It compares, hashes, pickles and copies by its settings; unpickling imports the
    library again.
    """

    __slots__ = ("_flatten", "_join", "_library", "_separator")

    def __init__(
        self,
        library: Library = "pandas",
        *,
        flatten: bool = True,
        separator: str = ".",
        join: Join = "strict",
    ) -> None:
        """Checks the settings and imports `library`; see `frame`."""
        if library not in _LIBRARIES:
            msg = f"frame() library must be 'pandas' or 'polars', not {library!r}"
            raise ValueError(msg)
        if not isinstance(flatten, bool):
            msg = f"frame() flatten must be a bool, not {flatten!r}"
            raise TypeError(msg)
        if not isinstance(separator, str):
            msg = f"frame() separator must be a str, not {separator!r}"
            raise TypeError(msg)
        if not separator:
            msg = f"frame() separator must be a non-empty str, not {separator!r}"
            raise ValueError(msg)
        if join not in _JOINS:
            msg = f"frame() join must be 'strict' or 'outer', not {join!r}"
            raise ValueError(msg)
        if library == "polars" and join == "outer":
            msg = (
                "frame('polars') takes join='strict' only: "
                "polars has no index to align on"
            )
            raise ValueError(msg)
        _imported(library)
        self._library: Library = library
        self._flatten = flatten
        self._separator = separator
        self._join: Join = join

    def _key(self) -> tuple[Library, bool, str, Join]:
        """The settings, which equality, hashing and pickling use."""
        return (self._library, self._flatten, self._separator, self._join)

    @override
    def __eq__(self, other: object) -> bool:
        if not isinstance(other, FrameBuilder):
            return NotImplemented
        return self._key() == other._key()

    @override
    def __hash__(self) -> int:
        return hash(self._key())

    @override
    def __repr__(self) -> str:
        return (
            f"fnloom.frame({self._library!r}, flatten={self._flatten}, "
            f"separator={self._separator!r}, join={self._join!r})"
        )

    @override
    def __reduce__(self) -> tuple[object, ...]:
        return (_rebuilt, self._key())

    def __copy__(self) -> FrameBuilder:
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> FrameBuilder:
        return self

    def __call__(self, outputs: dict[str, object]) -> Any:  # noqa: ANN401  # a pandas or polars frame
        """The frame of `outputs`, which `execute` gives in request order."""
        module = _imported(self._library)
        if self._library == "pandas":
            return self._pandas(module, outputs)
        return self._polars(module, outputs)

    def _columns(
        self, outputs: dict[str, object], frame_class: type
    ) -> dict[Hashable, object]:
        """Each column's name and value, in order; two of one name are refused."""
        columns: dict[Hashable, object] = {}
        whence: dict[Hashable, tuple[str, str]] = {}
        for name, value in outputs.items():
            if isinstance(value, frame_class):
                parts: list[tuple[Hashable, object, str]] = [
                    (
                        f"{name}{self._separator}{column}" if self._flatten else column,
                        series,
                        f"column {column!r} of output {name!r}",
                    )
                    for column, series in _frame_columns(value)
                ]
            else:
                parts = [(name, value, f"output {name!r}")]
            for label, column_value, source in parts:
                if label in columns:
                    owner, first = whence[label]
                    msg = (
                        f"frame() can't make a column of output {name!r}: "
                        f"it has the column {label!r} twice"
                        if owner == name
                        else f"frame() makes the column {label!r} twice: "
                        f"from {first} and from {source}"
                    )
                    raise ValueError(msg)
                columns[label] = column_value
                whence[label] = (name, source)
        return columns

    def _pandas(self, pd: Any, outputs: dict[str, object]) -> Any:  # noqa: ANN401  # the pandas module and a frame
        is_list_like = pd.api.types.is_list_like
        for name, value in outputs.items():
            if not isinstance(value, pd.DataFrame) and _two_dimensional(value):
                msg = (
                    f"frame('pandas') can't make columns of output {name!r}, "
                    f"a {type(value).__qualname__}"
                )
                raise TypeError(msg)
        columns = self._columns(outputs, pd.DataFrame)
        # As Hamilton does: one row only when no output has a length. A frame has
        # one even with no columns, so this asks the outputs, not the columns.
        if not any(is_list_like(value) for value in outputs.values()):
            return pd.DataFrame([columns])
        if self._join == "strict":
            _strict_pandas(pd, {n: v for n, v in outputs.items() if is_list_like(v)})
        return pd.DataFrame(columns)

    def _polars(self, pl: Any, outputs: dict[str, object]) -> Any:  # noqa: ANN401  # the polars module and a frame
        for name, value in outputs.items():
            lazy = isinstance(value, pl.LazyFrame)
            if lazy or (
                not isinstance(value, pl.DataFrame) and _two_dimensional(value)
            ):
                hint = ": call .collect() on it first" if lazy else ""
                msg = (
                    f"frame('polars') can't make columns of output {name!r}, "
                    f"a {type(value).__qualname__}{hint}"
                )
                raise TypeError(msg)
        columns = self._columns(outputs, pl.DataFrame)
        lengths = {
            name: len(value)  # ty: ignore[invalid-argument-type]  # a frame, series, list or array
            for name, value in outputs.items()
            if _polars_list_like(pl, value)
        }
        if not lengths:
            return pl.DataFrame(
                [
                    _polars_column(pl, str(label), value, 1)
                    for label, value in columns.items()
                ]
            )
        if len(set(lengths.values())) > 1:
            msg = (
                f"frame('polars') needs every output of one length: {_listed(lengths)}"
            )
            raise ValueError(msg)
        (height,) = set(lengths.values())
        series = [
            _polars_column(pl, str(label), value, height)
            for label, value in columns.items()
        ]
        return pl.DataFrame(series)


def _polars_column(pl: Any, label: str, value: object, height: int) -> Any:  # noqa: ANN401  # the polars module and a Series
    """`value` as the polars column `label`: a scalar repeated `height` times.

    A scalar takes the type a one-row frame gives it, whatever its column's height.
    A value polars refuses is a `TypeError` naming the column.
    """
    try:
        if isinstance(value, pl.Series):
            return value.alias(label)
        if _polars_list_like(pl, value):
            return pl.Series(label, value)
        dtype = pl.Series([value]).dtype
        return pl.repeat(value, height, dtype=dtype, eager=True).alias(label)
    except (TypeError, ValueError, pl.exceptions.PolarsError) as error:
        msg = (
            f"frame('polars') can't make the column {label!r} "
            f"of a {type(value).__qualname__}: {error}"
        )
        raise TypeError(msg) from error


def _rebuilt(
    library: Library,
    flatten: bool,  # noqa: FBT001  # pickle calls it positionally
    separator: str,
    join: Join,
) -> FrameBuilder:
    """The builder unpickled: checked and imported again."""
    return FrameBuilder(library, flatten=flatten, separator=separator, join=join)


def _imported(library: str) -> Any:  # noqa: ANN401  # pandas or polars
    """`library`, imported; an `ImportError` naming it otherwise."""
    try:
        return importlib.import_module(library)
    except ImportError as error:
        msg = f"frame({library!r}) needs {library}, which can't be imported: {error}"
        raise ImportError(msg, name=library) from error


def _frame_columns(value: Any) -> list[tuple[Hashable, object]]:  # noqa: ANN401  # a pandas or polars frame
    """A frame's columns as (name, series) pairs, in order."""
    if hasattr(value, "get_columns"):  # polars
        return [(column.name, column) for column in value.get_columns()]
    return list(value.items())


def _polars_list_like(pl: Any, value: object) -> bool:  # noqa: ANN401  # the polars module
    """Whether `value` is a column (or frame) to polars rather than a scalar."""
    return isinstance(value, (pl.DataFrame, pl.Series, list, tuple, range)) or (
        bool(getattr(value, "ndim", 0) == 1)
    )


def _strict_pandas(pd: Any, outputs: dict[str, object]) -> None:  # noqa: ANN401  # the pandas module
    """A `ValueError` unless every list-like output has one index, or one length.

    A list-like output with no length, such as a generator, is a `TypeError`.
    """
    lengths: dict[str, int] = {}
    for name, value in outputs.items():
        if not isinstance(value, Sized):
            msg = (
                f"frame(join='strict') can't compare output {name!r}, a "
                f"{type(value).__qualname__}: it has no length; "
                "return a list or a series"
            )
            raise TypeError(msg)
        lengths[name] = len(value)
    if len(set(lengths.values())) > 1:
        _refuse(_listed(lengths))
    # pandas aligns a mapping on its keys, as it does a series on its index.
    indexed: list[tuple[str, Any]] = [
        (name, value.index if hasattr(value, "index") else pd.Index(list(value)))
        for name, value in outputs.items()
        if isinstance(value, (pd.Series, pd.DataFrame, Mapping))
    ]
    if not indexed:
        return
    first, index = indexed[0]
    for name, other in indexed[1:]:
        if index.equals(other):
            continue
        rows = len(index)
        where = next(
            (
                f"row {row}: {a!r} against {b!r}"
                for row, (a, b) in enumerate(zip(index, other, strict=True))
                if not _same_label(a, b)
            ),
            None,
        )
        detail = (
            "their indexes differ"
            if where is None
            else f"their labels differ at {where}"
        )
        _refuse(f"{first} and {name} both have {rows} rows, but {detail}")


def _same_label(a: object, b: object) -> bool:
    """Whether two index labels match, NaN matching NaN as `Index.equals` counts it."""
    try:
        return bool(a == b) or (a != a and b != b)  # noqa: PLR0124  # NaN test
    except TypeError, ValueError:
        return False


def _listed(lengths: dict[str, int]) -> str:
    """Each output with its rows, in request order."""
    return ", ".join(f"{name} has {rows} rows" for name, rows in lengths.items())


def _refuse(detail: str) -> None:
    """The strict join's `ValueError`, saying `detail`."""
    msg = (
        "frame(join='strict') needs every output on one index: "
        f"{detail}; pass join='outer' to align them"
    )
    raise ValueError(msg)


def _two_dimensional(value: object) -> bool:
    """Whether `value` has two or more axes: a frame of any library, or an array."""
    shape = getattr(value, "shape", ())
    return isinstance(shape, tuple) and len(shape) > 1
