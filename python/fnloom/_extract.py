"""`@extract_fields` and `@extract_columns`: a producer, and a checked node per item.

The decorator records an `Extraction`; the scan lowers it (`lower`) into the
producer, unchanged, and one generated node per field or column. A generated
node's payload is an `_Extractor`: it reads the item from the producer's value by
key, fills a missing one when asked, and checks the value against the item's
declared type with the input check's matcher, on every run (Q61, Q64). A column's
type comes from the producer's frame type (`column_type`); pandas and polars are
recognised by name and never imported.
"""

import annotationlib
import collections.abc
import inspect
import sys
import typing
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, ClassVar, Final, Literal, final, override

from fnloom import _types
from fnloom._decorators import (
    _ATTRIBUTE,
    Decorations,
    Source,
    decorations,
    too_many_rows,
)
from fnloom._namespaces import qualified
from fnloom._view import Param

if TYPE_CHECKING:
    from fnloom._scan import NodeDef


@final
class _MissingFill:
    """The type of `MISSING_FILL`: no fill was given."""

    __slots__ = ()

    @override
    def __repr__(self) -> str:
        return "MISSING_FILL"


# No fill: a missing field is an error. `None` is a fill like any other (Q62).
MISSING_FILL: Final = _MissingFill()

# One extracted item: its name, its declared type (`None` to take it from the
# producer's return type) and its doc (`None` for the producer's).
type Item = tuple[str, object | None, str | None]


@dataclass(frozen=True, slots=True)
class Extraction:
    """What an `extract_*` decorator recorded on a function.

    Attributes:
        kind: `"fields"`, read from a mapping by key, or `"columns"`, read from
            any value with `in` and `[]`, typed by `column_type`.
        items: Each item to extract; empty to extract every field of a
            `TypedDict` return. A column's doc is its own, or `None` for the
            producer's.
        fill: The value a missing item takes, or `MISSING_FILL` for none.
    """

    kind: Literal["fields", "columns"]
    items: tuple[Item, ...]
    fill: object


class _Extractor:
    """A generated field node's payload: the field, read from the producer's value.

    Called with the producer's value under the producer's name. A missing field
    takes the fill, when there is one, without changing the producer's value
    (Q62). The value is checked as an input is: a plain class by `isinstance`,
    anything else by `value_matches`, so `None` passes only a type admitting it
    (Q61, Q79).
    """

    __slots__ = ("_cls", "_declared", "_field", "_fill", "_matches", "_producer")

    # What an error calls the item this node reads.
    noun: ClassVar[str] = "field"

    def __init__(  # noqa: PLR0913  # the item, its fill, and the driver's check settings
        self,
        producer: str,
        field: str,
        declared: object,
        fill: object,
        rules: _types.Rules = _types.SHALLOW,
        *,
        value_checks: tuple[_types.ValueCheck, ...] = (),
    ) -> None:
        """Reads `field` from node `producer`'s value, checked against `declared`.

        The check follows `rules`, the driver's deep checks and widening, and
        admits what any of `value_checks` admits (Q21).
        """
        self._producer = producer
        self._field = field
        self._declared = declared
        self._cls, self._matches = _types.admitting(
            declared, _types.check_of(declared, rules), value_checks
        )
        self._fill = fill

    @property
    def field(self) -> str:
        """The field or column this node reads."""
        return self._field

    @property
    def declared(self) -> object:
        """The item's declared type, which its value is checked against."""
        return self._declared

    @property
    def fill(self) -> object:
        """The value of a missing item, or `MISSING_FILL` for none."""
        return self._fill

    def __call__(self, /, **values: object) -> object:
        """The field's value; `ValueError` when it is missing or of the wrong type."""
        # Any value with `in` and `[]` will do, as in Hamilton; another is named.
        mapping = typing.cast("Mapping[object, object]", values[self._producer])
        field = self._field
        try:
            present = field in mapping
        except TypeError:
            msg = (
                f"{self._producer!r} returned {type(mapping).__name__}, "
                f"which has no field {field!r}"
            )
            raise TypeError(msg) from None
        if present:
            found = mapping[field]
        elif self._fill is not MISSING_FILL:
            found = self._fill
        else:
            msg = (
                f"{self._producer!r} returned no field {field!r}; {_returned(mapping)}"
            )
            raise ValueError(msg)
        return self._checked(found)

    def _checked(self, found: object) -> object:
        """`found`, when it matches the declared type; `ValueError` otherwise."""
        cls = self._cls
        if cls is not None and isinstance(found, cls):
            return found
        # Refused by class: the check answers the same, or a custom check admits.
        try:
            if self._matches(found):
                return found
        except _types.UnresolvedError as error:
            msg = f"{self._producer!r} returned {self.noun} {self._field!r}: {error}"
            raise TypeError(msg) from None
        msg = (
            f"{self._producer!r} returned {self.noun} {self._field!r} as "
            f"{inspect.formatannotation(type(found))}, but it is declared "
            f"{inspect.formatannotation(self._declared)}"
        )
        raise ValueError(msg)


@final
class _ColumnExtractor(_Extractor):
    """A generated column node's payload: the column, read from the producer's value.

    Reads any value with `in` and `[]`: a mapping, a pandas or a polars frame. A
    missing column takes the fill: the value itself for a mapping, a full column
    for a pandas or polars frame, and a `TypeError` for any other value; the
    producer's value is never changed (Q64). The value is checked against the
    column's declared type as a field's is.
    """

    __slots__ = ()

    noun: ClassVar[str] = "column"

    @override
    def __call__(self, /, **values: object) -> object:
        """The column; `ValueError` when it is missing or of the wrong type."""
        frame = typing.cast("Mapping[object, object]", values[self._producer])
        column = self._field
        try:
            present = column in frame
        except TypeError:
            msg = (
                f"{self._producer!r} returned {type(frame).__name__}, "
                f"which has no column {column!r}"
            )
            raise TypeError(msg) from None
        if present:
            return self._checked(frame[column])
        if self._fill is MISSING_FILL:
            msg = (
                f"{self._producer!r} returned no column {column!r}; "
                f"{_returned_columns(frame)}"
            )
            raise ValueError(msg)
        return self._checked(self._filled(frame))

    def _filled(self, frame: object) -> object:
        """The fill as `frame`'s missing column; `TypeError` for an unknown frame."""
        if isinstance(frame, Mapping):
            return self._fill
        library = _frame_library(type(frame))
        column = self._field
        if library == "pandas":
            sized = typing.cast("Any", frame)
            series = sys.modules["pandas"].Series
            if self._fill is None:
                # `None` stays `None`, not NaN: it is a fill like any other (Q62).
                # A scalar `None` is read as missing data, so it is given per row.
                nones = [None] * len(sized.index)
                return series(nones, index=sized.index, name=column, dtype=object)
            return series(self._fill, index=sized.index, name=column)
        if library == "polars":
            sized = typing.cast("collections.abc.Sized", frame)
            return sys.modules["polars"].Series(column, [self._fill] * len(sized))
        msg = (
            f"{self._producer!r} returned no column {column!r}, and fill_with fills "
            "only a mapping, a pandas DataFrame or a polars DataFrame, "
            f"not {_qualified(type(frame))}"
        )
        raise TypeError(msg)


# The frame classes `column_type` knows, by (`__module__`, `__qualname__`), with
# the library whose `Series` is their column type. pandas 3 reports `pandas`.
_FRAMES: Final = {
    ("pandas", "DataFrame"): "pandas",
    ("pandas.core.frame", "DataFrame"): "pandas",
    ("polars", "DataFrame"): "polars",
    ("polars.dataframe.frame", "DataFrame"): "polars",
}


def _qualified(cls: type) -> str:
    """`cls`'s module and qualified name, as an error names a type."""
    return f"{cls.__module__}.{cls.__qualname__}"


def _frame_library(cls: type) -> str | None:
    """`"pandas"` or `"polars"` when `cls` is, or derives from, its DataFrame."""
    for base in cls.__mro__:
        module = getattr(base, "__module__", None)
        if (library := _FRAMES.get((module, base.__qualname__))) is not None:
            return library
    return None


def column_type(frame_type: object) -> object:
    """The type of a column of a `frame_type` value.

    `pandas.Series` for a `pandas.DataFrame`, `polars.Series` for a
    `polars.DataFrame`, each recognised by module and name and read from
    `sys.modules`, never imported; `typing.Any` for anything else.
    """
    frame_type = _types.stripped(frame_type)
    if not isinstance(frame_type, type):
        return Any
    library = _frame_library(frame_type)
    if library is None:
        return Any
    series = getattr(sys.modules.get(library), "Series", None)
    return series if isinstance(series, type) else Any


def _returned(mapping: object) -> str:
    """The fields `mapping` holds, sorted, for a missing-field error."""
    keys = getattr(mapping, "keys", None)
    if not callable(keys):
        return f"it returned a {type(mapping).__name__}"
    return _listed(typing.cast("Iterable[object]", keys()), "fields")


def _returned_columns(frame: object) -> str:
    """The columns `frame` holds, sorted: its `columns`, else its `keys()`."""
    columns = getattr(frame, "columns", None)
    if isinstance(columns, Iterable) and not isinstance(columns, str | bytes):
        return _listed(typing.cast("Iterable[object]", columns), "columns")
    keys = getattr(frame, "keys", None)
    if not callable(keys):
        return f"it returned a {type(frame).__name__}"
    return _listed(typing.cast("Iterable[object]", keys()), "columns")


def _listed(names: Iterable[object], noun: str) -> str:
    """`names`, sorted by `repr`, as a missing-item error ends."""
    shown = sorted(repr(name) for name in names)
    if not shown:
        return f"it returned no {noun}"
    return f"it returned {', '.join(shown)}"


def _is_type(declared: object) -> bool:
    """Whether `declared` is a type: a class, `Any`, a generic, a union or an alias."""
    return (
        isinstance(declared, type | typing.TypeAliasType | typing.NewType)
        or declared is Any
        or typing.get_origin(declared) is not None
    )


def _from_mapping(
    fields: Mapping[object, object], names: tuple[object, ...]
) -> list[Item]:
    """The items of the mapping form; every culprit listed, sorted."""
    if names:
        listed = ", ".join(sorted(repr(n) for n in names))
        msg = (
            "extract_fields takes a mapping of fields to types or field names, "
            f"not both; the extra names: {listed}"
        )
        raise ValueError(msg)
    if not fields:
        msg = "extract_fields got an empty mapping; it needs at least one field"
        raise ValueError(msg)
    _check_names(list(fields))
    untyped = sorted(
        f"{name!r} ({declared!r})"
        for name, declared in fields.items()
        if not _is_type(declared)
    )
    if untyped:
        msg = f"extract_fields needs a type for each field: {', '.join(untyped)}"
        raise TypeError(msg)
    return [(str(name), declared, None) for name, declared in fields.items()]


def _check_names(names: list[object]) -> None:
    """`TypeError` listing every name that is not a `str`, sorted."""
    not_str = sorted(
        f"{name!r} ({type(name).__name__})"
        for name in names
        if not isinstance(name, str)
    )
    if not_str:
        msg = f"extract_fields field names must be str, got: {', '.join(not_str)}"
        raise TypeError(msg)


def _items(fields: object, names: tuple[object, ...]) -> tuple[Item, ...]:
    """The items `extract_fields(fields, *names)` asks for, validated."""
    if isinstance(fields, Mapping):
        return tuple(
            _from_mapping(typing.cast("Mapping[object, object]", fields), names)
        )
    if fields is None:
        given: list[object] = list(names)
    elif isinstance(fields, str):
        given = [fields, *names]
    elif isinstance(fields, Iterable):
        given = [*typing.cast("Iterable[object]", fields), *names]
        if not given:
            msg = "extract_fields got no names; it needs at least one field name"
            raise ValueError(msg)
    else:
        msg = (
            "extract_fields takes a mapping, field names or nothing, "
            f"got {type(fields).__name__}"
        )
        raise TypeError(msg)
    _check_names(given)
    # A name given twice is one field, as in Hamilton.
    return tuple((str(name), None, None) for name in dict.fromkeys(given))


def _typed_dict_keys(returns: object) -> frozenset[str]:
    """The keys of the `TypedDict` `returns`, read without evaluating its types."""
    keys: set[str] = set()
    for attribute in ("__required_keys__", "__optional_keys__"):
        keys.update(typing.cast("Iterable[str]", getattr(returns, attribute, ())))
    return frozenset(keys)


def field_names(fn: Callable[..., object], extraction: Extraction) -> tuple[str, ...]:
    """The names `extraction` gives on `fn`, as far as they are known unresolved.

    The named items; for an `extract_fields` naming none, the keys of the
    `TypedDict` that `fn` returns, its annotation evaluated if it is written as
    a string. A return annotation that does not evaluate gives no names.
    """
    if extraction.items:
        return tuple(name for name, _, _ in extraction.items)
    returns = annotationlib.get_annotations(
        fn, format=annotationlib.Format.FORWARDREF
    ).get("return")
    if isinstance(returns, str):
        returns = annotationlib.ForwardRef(returns, owner=fn)
    if isinstance(returns, annotationlib.ForwardRef):
        try:
            returns = typing.evaluate_forward_ref(returns)
        except Exception:  # noqa: BLE001  # an annotation that fails to evaluate gives no names
            return ()
    returns = _types.stripped(returns)
    if not typing.is_typeddict(returns):
        return ()
    return tuple(sorted(_typed_dict_keys(returns)))


def _value_type(returns: object) -> tuple[bool, object | None]:
    """Whether `returns` is a mapping type, and its value type when it names one."""
    origin = typing.get_origin(returns) or returns
    try:
        mapping = isinstance(origin, type) and issubclass(
            origin, collections.abc.Mapping
        )
    except TypeError:
        return False, None
    if not mapping:
        return False, None
    args = typing.get_args(returns)
    return True, args[1] if len(args) == 2 else None  # noqa: PLR2004  # key and value


def _shape(label: str, items: tuple[Item, ...], returns: object) -> str | None:
    """What is wrong with extracting `items` from a `returns` value, or `None`.

    Reads only the return's form and a `TypedDict`'s keys, so it holds for an
    annotation not yet evaluated, as long as its outer type is.
    """
    returns = _types.stripped(returns)
    shown = inspect.formatannotation(returns)
    if typing.is_typeddict(returns):
        keys = _typed_dict_keys(returns)
        absent = sorted(name for name, _, _ in items if name not in keys)
        if absent:
            plural = "s" if len(absent) > 1 else ""
            listed = ", ".join(repr(name) for name in absent)
            return f"{label} returns {shown}, which has no field{plural} {listed}"
        return None
    mapping, value = _value_type(returns)
    if not mapping:
        return (
            f"{label}: extract_fields needs a dict, Mapping or TypedDict return, "
            f"not {shown}"
        )
    if not items:
        return (
            f"{label}: extract_fields with no fields extracts a TypedDict's fields, "
            f"but the return is {shown}; name the fields"
        )
    if value is None and any(declared is None for _, declared, _ in items):
        return (
            f"{label}: field names take their types from the return annotation, but "
            f"{shown} gives none; annotate it dict[str, T], Mapping[str, T] or a "
            "TypedDict, or pass a mapping of fields to types"
        )
    return None


def _evaluated(returns: object) -> bool:
    """Whether `returns`, read without evaluating, has an outer type to check."""
    return not isinstance(returns, str | annotationlib.ForwardRef)


def extract_fields[F: Callable[..., object]](
    fields: Mapping[str, object] | Iterable[str] | None = None,
    /,
    *names: str,
    fill_with: object = MISSING_FILL,
) -> Callable[[F], F]:
    """Makes a node of each named field of the function's returned mapping.

    Pass a mapping of field names to types, or field names whose types come from
    a `dict[str, T]`, `Mapping[str, T]` or `TypedDict` return, or nothing to take
    every field of a `TypedDict` return. The function stays a node of its own
    name, returning the whole mapping. Each field's value is checked against its
    type on every run (Q61). `fill_with` is the value of a field the mapping
    lacks, `None` included; the producer's value is never changed (Q62).
    """
    given: object = fields
    items = _items(given, names)
    extraction = Extraction("fields", items, fill_with)

    def apply(fn: F) -> F:
        current = decorations(fn)
        label = repr(getattr(fn, "__name__", "function"))
        _refuse_stacking(label, current, "fields")
        annotations = annotationlib.get_annotations(
            fn, format=annotationlib.Format.FORWARDREF
        )
        if "return" not in annotations:
            msg = f"{label}: extract_fields needs a return annotation"
            raise ValueError(msg)
        returns = annotations["return"]
        if (
            _evaluated(returns)
            and (problem := _shape(label, items, returns)) is not None
        ):
            raise ValueError(problem)
        setattr(fn, _ATTRIBUTE, replace(current, extract=extraction))
        return fn

    return apply


def _refuse_stacking(
    label: str, current: Decorations, kind: Literal["fields", "columns"]
) -> None:
    """`ValueError` when `extract_{kind}` cannot go on a function so decorated.

    A function extracts once, and lowers to one producer: two `@parameterize`
    rows are refused (Q61).
    """
    if current.extract is not None:
        if current.extract.kind == kind:
            msg = f"{label} already extracts {kind}; apply extract_{kind} once"
        else:
            msg = (
                f"{label} already extracts {current.extract.kind}; "
                "extract_fields and extract_columns don't combine"
            )
        raise ValueError(msg)
    if current.rows is not None and len(current.rows) > 1:
        raise ValueError(too_many_rows(current.rows, f"extract_{kind}"))


def _columns(columns: tuple[object, ...]) -> tuple[Item, ...]:
    """The items `extract_columns(*columns)` asks for, validated.

    A list is refused with the advice Hamilton gives (H2.3c), and every
    argument that is neither a name nor a `(name, doc)` pair is listed, sorted.
    A name given more than once, in any form, is refused, every repeat listed,
    sorted (Q64): Hamilton fails such a build.
    """
    if not columns:
        msg = "extract_columns got no columns; it needs at least one column name"
        raise ValueError(msg)
    items: list[Item] = []
    bad: list[str] = []
    for column in columns:
        if isinstance(column, str):
            items.append((column, None, None))
        elif (
            isinstance(column, tuple)
            and len(typing.cast("tuple[object, ...]", column)) == 2  # noqa: PLR2004  # a name and a doc
            and all(isinstance(part, str) for part in column)
        ):
            name, doc = typing.cast("tuple[str, str]", column)
            items.append((name, None, doc))
        else:
            bad.append(repr(column))
    if bad:
        listed = ", ".join(sorted(bad))
        msg = f"extract_columns takes column names or (name, doc) pairs, got: {listed}"
        if any(isinstance(column, list) for column in columns):
            msg += "; it got a list: put `*` in front of it to expand it"
        raise TypeError(msg)
    counts = collections.Counter(name for name, _, _ in items)
    if repeated := sorted(repr(name) for name, count in counts.items() if count > 1):
        msg = f"extract_columns names a column more than once: {', '.join(repeated)}"
        raise ValueError(msg)
    return tuple(items)


def extract_columns[F: Callable[..., object]](
    *columns: str | tuple[str, str],
    fill_with: object = MISSING_FILL,
) -> Callable[[F], F]:
    """Makes a node of each named column of the value the function returns.

    Works on any value with `in` and `[]`: a mapping, a pandas or a polars
    DataFrame, or another frame; no extension is needed (Q64). A column is
    named, or given as `(name, doc)` to set its node's doc. Its type comes from
    the return annotation: `pandas.Series` for a `pandas.DataFrame`,
    `polars.Series` for a `polars.DataFrame`, `typing.Any` for anything else,
    and its value is checked against it on every run. The function stays a
    node of its own name. `fill_with` is the value of a missing column: a full
    column of it for a pandas or polars frame, the value itself for a mapping,
    and a `TypeError` at run for another type; the producer's value is never
    changed.
    """
    given: tuple[object, ...] = columns
    extraction = Extraction("columns", _columns(given), fill_with)

    def apply(fn: F) -> F:
        current = decorations(fn)
        _refuse_stacking(repr(getattr(fn, "__name__", "function")), current, "columns")
        setattr(fn, _ATTRIBUTE, replace(current, extract=extraction))
        return fn

    return apply


def _field_types(producer: NodeDef, extraction: Extraction) -> list[tuple[str, object]]:
    """Each field with its type, from the resolved return; `ValueError` on misfits."""
    label = repr(producer.name)
    if (problem := _shape(label, extraction.items, producer.returns)) is not None:
        raise ValueError(problem)
    returns = _types.stripped(producer.returns)
    if typing.is_typeddict(returns):
        # A field type that does not resolve is `Unresolved` (Q81).
        given = _types.resolved_hints(returns, extras=False)
        items = extraction.items or tuple((name, None, None) for name in sorted(given))
    else:
        given = None
        items = extraction.items
    _, value = _value_type(returns)
    typed: list[tuple[str, object]] = []
    misfits: list[str] = []
    for name, declared, _ in items:
        derived = value if given is None else given[name]
        if declared is None:
            typed.append((name, derived if derived is not None else Any))
            continue
        if derived is not None and not _types.type_matches(derived, declared):
            misfits.append(
                f"{label}: field {name!r} is declared "
                f"{inspect.formatannotation(declared)}, but the return type "
                f"{inspect.formatannotation(producer.returns)} gives "
                f"{inspect.formatannotation(derived)}"
            )
        typed.append((name, declared))
    if misfits:
        raise ValueError("; ".join(sorted(misfits)))
    return typed


def lower(
    producer: NodeDef,
    extraction: Extraction,
    *,
    resolved: bool,
    rules: _types.Rules = _types.SHALLOW,
    value_checks: tuple[_types.ValueCheck, ...] = (),
) -> list[NodeDef]:
    """`producer`, unchanged, then one generated node per extracted field.

    Each generated node reads the producer under its name, has the producer's
    origin, module, tags, config and namespace, under which it is named, and an
    `_Extractor` payload, which reads the field by its own name. With `resolved`,
    `producer`'s annotations are resolved: each field's type is read from the
    return, and a declared type it cannot feed is a `ValueError` naming every
    such field; a `TypedDict` field type that does not resolve is `Unresolved`
    (Q81). Each field's check follows `rules`, and admits what any of
    `value_checks` admits. Without, as when scanning, the
    field nodes are placeholders, used only to find duplicate names: a field the
    decorator gave no type is typed `Any`, and nothing is checked yet. Column nodes are
    typed by `column_type` of the resolved return, and `Any` before it resolves;
    each carries its own doc.
    """
    if extraction.kind == "columns":
        declared = column_type(producer.returns) if resolved else Any
        return _lowered_columns(producer, extraction, declared, rules, value_checks)
    if resolved:
        typed = _field_types(producer, extraction)
    else:
        items = extraction.items
        if not items and typing.is_typeddict(
            returns := _types.stripped(producer.returns)
        ):
            # Every field of a `TypedDict` already defined: its keys, not yet typed.
            items = tuple(
                (key, None, None) for key in sorted(_typed_dict_keys(returns))
            )
        typed = [
            (name, Any if declared is None else declared) for name, declared, _ in items
        ]
    param = Param(producer.name, Source(producer.name), True, producer.returns)  # noqa: FBT003  # `required`, in field order
    return [
        producer,
        *(
            replace(
                producer,
                name=qualified(producer.namespace, name),
                params=(param,),
                payload=_Extractor(
                    producer.name,
                    name,
                    declared,
                    extraction.fill,
                    rules,
                    value_checks=value_checks,
                ),
                returns=declared,
                renames={},
            )
            for name, declared in typed
        ),
    ]


def _lowered_columns(
    producer: NodeDef,
    extraction: Extraction,
    declared: object,
    rules: _types.Rules = _types.SHALLOW,
    value_checks: tuple[_types.ValueCheck, ...] = (),
) -> list[NodeDef]:
    """`producer`, then a node per column, each typed `declared`, with its doc."""
    param = Param(producer.name, Source(producer.name), True, producer.returns)  # noqa: FBT003  # `required`, in field order
    return [
        producer,
        *(
            replace(
                producer,
                name=qualified(producer.namespace, name),
                params=(param,),
                payload=_ColumnExtractor(
                    producer.name,
                    name,
                    declared,
                    extraction.fill,
                    rules,
                    value_checks=value_checks,
                ),
                returns=declared,
                renames={},
                doc=doc,
            )
            for name, _, doc in extraction.items
        ),
    ]


def is_generated(node: NodeDef) -> bool:
    """Whether `node` is a field or column node `lower` made, not a function's own."""
    return isinstance(node.payload, _Extractor)


def extractor(node: NodeDef) -> _Extractor | None:
    """The payload of a node `lower` made; `None` for any other node."""
    payload = node.payload
    return payload if isinstance(payload, _Extractor) else None
