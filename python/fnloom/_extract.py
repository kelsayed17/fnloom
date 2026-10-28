"""`@extract_fields`: a producer node, and one checked node per field it returns.

The decorator records an `Extraction`; the scan lowers it (`lower`) into the
producer, unchanged, and one generated node per field. A generated node's payload
is an `_Extractor`: it reads the field from the producer's value by key, fills a
missing one when asked, and checks the value against the field's declared type
with the input check's matcher, on every run (Q61).
"""

import annotationlib
import collections.abc
import inspect
import typing
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Final, Literal, final, override

from fnloom import _types
from fnloom._decorators import _ATTRIBUTE, Source, decorations, too_many_rows
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
        kind: `"fields"`, read from a mapping by key.
        items: Each item to extract; empty to extract every field of a
            `TypedDict` return.
        fill: The value a missing item takes, or `MISSING_FILL` for none.
    """

    kind: Literal["fields", "columns"]
    items: tuple[Item, ...]
    fill: object


@final
class _Extractor:
    """A generated field node's payload: the field, read from the producer's value.

    Called with the producer's value under the producer's name. A missing field
    takes the fill, when there is one, without changing the producer's value
    (Q62). The value is checked as an input is: `None` passes, a plain class by
    `isinstance`, anything else by `value_matches` (Q61).
    """

    __slots__ = ("_cls", "_declared", "_field", "_fill", "_producer")

    def __init__(
        self, producer: str, field: str, declared: object, fill: object
    ) -> None:
        """Reads `field` from node `producer`'s value, checked against `declared`."""
        self._producer = producer
        self._field = field
        self._declared = declared
        self._cls = _types.plain_class(declared)
        self._fill = fill

    @property
    def field(self) -> str:
        """The field this node reads."""
        return self._field

    @property
    def declared(self) -> object:
        """The field's declared type, which its value is checked against."""
        return self._declared

    @property
    def fill(self) -> object:
        """The value of a missing field, or `MISSING_FILL` for none."""
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
        if found is None:
            return found
        cls = self._cls
        if cls is not None:
            if isinstance(found, cls):
                return found
        elif _types.value_matches(self._declared, found):
            return found
        msg = (
            f"{self._producer!r} returned field {field!r} as "
            f"{inspect.formatannotation(type(found))}, but it is declared "
            f"{inspect.formatannotation(self._declared)}"
        )
        raise ValueError(msg)


def _returned(mapping: object) -> str:
    """The fields `mapping` holds, sorted, for a missing-field error."""
    keys = getattr(mapping, "keys", None)
    if not callable(keys):
        return f"it returned a {type(mapping).__name__}"
    names = sorted(repr(key) for key in typing.cast("Iterable[object]", keys()))
    if not names:
        return "it returned no fields"
    return f"it returned {', '.join(names)}"


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
        if current.extract is not None:
            msg = f"{label} already extracts fields; apply extract_fields once"
            raise ValueError(msg)
        if current.rows is not None and len(current.rows) > 1:
            raise ValueError(too_many_rows(current.rows))
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


def _field_types(producer: NodeDef, extraction: Extraction) -> list[tuple[str, object]]:
    """Each field with its type, from the resolved return; `ValueError` on misfits."""
    label = repr(producer.name)
    if (problem := _shape(label, extraction.items, producer.returns)) is not None:
        raise ValueError(problem)
    returns = _types.stripped(producer.returns)
    if typing.is_typeddict(returns):
        # A field type that does not resolve raises; `_scan` names every producer.
        given = typing.get_type_hints(returns)
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
    producer: NodeDef, extraction: Extraction, *, resolved: bool
) -> list[NodeDef]:
    """`producer`, unchanged, then one generated node per extracted field.

    Each generated node reads the producer under its name, has the producer's
    origin, module, tags and config, and an `_Extractor` payload. With `resolved`,
    `producer`'s annotations are resolved: each field's type is read from the
    return, and a declared type it cannot feed is a `ValueError` naming every
    such field; a `TypedDict` field type that does not resolve raises its
    `NameError` or `AttributeError`. Without, as when scanning, the field nodes
    are placeholders, used only to find duplicate names: a field the decorator
    gave no type is typed `Any`, and nothing is checked yet.
    """
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
                name=name,
                params=(param,),
                payload=_Extractor(producer.name, name, declared, extraction.fill),
                returns=declared,
                renames={},
            )
            for name, declared in typed
        ),
    ]


def is_generated(node: NodeDef) -> bool:
    """Whether `node` is a field node `lower` made, not a function's own."""
    return isinstance(node.payload, _Extractor)


def extractor(node: NodeDef) -> _Extractor | None:
    """The payload of a field node `lower` made; `None` for any other node."""
    payload = node.payload
    return payload if isinstance(payload, _Extractor) else None


def returned_type(node: NodeDef) -> str:
    """`node`'s return type as an error shows it."""
    return inspect.formatannotation(_types.stripped(node.returns))
