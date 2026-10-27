"""Whether a value fits a declared type, and whether one type may feed another.

The answers agree with Hamilton 1.90.0 over the matrix of design §10, except
where the quirk register says fnloom differs (Q37 to Q43). Forms outside the
matrix are unsupported: they get an answer, but no promise of Hamilton's.
"""

import collections.abc
import types
import typing
from typing import Annotated, Any, Literal, NewType, TypeVar, Union

# Each group: a declared kind in it takes any value of the group, and then the
# value's first element is checked against the declaration's first argument.
_SEQUENCES: tuple[type, ...] = (list, tuple, collections.abc.Sequence)
_ITERABLES: tuple[type, ...] = (set, collections.abc.Iterable)


def _strip(annotation: object) -> object:
    """`Annotated[T, ...]` is `T`: metadata never changes an answer (Q41)."""
    while typing.get_origin(annotation) is Annotated:
        annotation = typing.get_args(annotation)[0]
    return annotation


def _is_union(annotation: object) -> bool:
    return typing.get_origin(annotation) in {Union, types.UnionType}


def _element_args(annotation: object) -> tuple[object, ...]:
    """The arguments of a generic, with `tuple[T, ...]` read as one element type `T`."""
    args = typing.get_args(annotation)
    if typing.get_origin(annotation) is tuple and args[1:] == (...,):
        return args[:1]
    return args


def _first_fits(element: object, value: object) -> bool:
    """Whether the first item of the collection `value` fits `element`.

    An empty collection always fits.
    """
    for item in typing.cast("collections.abc.Iterable[object]", value):
        return _fits(element, item)
    return True


def _fits_generic(declared: object, origin: object, value: object) -> bool:
    """A parameterized collection: its kind, then at most its first element."""
    if type(value) is origin:
        return True
    if origin is tuple and not isinstance(value, tuple):
        return False
    if isinstance(value, collections.abc.Iterator):
        # Never look inside an iterator: reading its first item would consume it,
        # so it is checked by its kind alone (Q43).
        return isinstance(origin, type) and isinstance(value, origin)
    groups = (_SEQUENCES, _ITERABLES)
    if not any(origin in group and isinstance(value, group) for group in groups):
        return False
    args = _element_args(declared)
    return not args or _first_fits(args[0], value)


def _fits(declared: object, value: object) -> bool:  # noqa: PLR0911  # one return per form
    declared = _strip(declared)
    if declared is Any or isinstance(declared, TypeVar):
        return True
    if isinstance(declared, NewType):
        return _fits(declared.__supertype__, value)
    if _is_union(declared):
        return any(_fits(member, value) for member in typing.get_args(declared))
    origin = typing.get_origin(declared)
    if origin is Literal:
        return value in typing.get_args(declared)
    if origin is not None:
        return _fits_generic(declared, origin, value)
    if not isinstance(declared, type):
        return False
    try:
        return isinstance(value, declared)
    except TypeError:  # a protocol that is not runtime-checkable (Q42)
        return False


def value_matches(declared: object, value: object) -> bool:
    """Whether `value`, supplied as an input or override, fits the `declared` type.

    `None` fits every declaration, as in Hamilton. A parameterized collection is
    checked by its kind and, for a sequence or set, by its first element only.
    """
    return value is None or _fits(declared, value)


def _subclass(produced: object, required: object) -> bool:
    """`issubclass` for two classes; else, or for an uncheckable protocol, False."""
    if not (isinstance(produced, type) and isinstance(required, type)):
        return False
    try:
        return issubclass(produced, required)
    except TypeError:  # a protocol that is not runtime-checkable (Q42)
        return False


def _generic_matches(produced: object, required: object) -> bool:
    """Classes and generics: related kinds, and equal arguments if both have some."""
    produced_kind = typing.get_origin(produced) or produced
    required_kind = typing.get_origin(required) or required
    if produced_kind != required_kind and not _subclass(produced_kind, required_kind):
        return False
    produced_args, required_args = _element_args(produced), _element_args(required)
    if produced_kind is not required_kind:
        # Across kinds, a homogeneous tuple's one element type is what counts (Q37).
        return not produced_args or not required_args or produced_args == required_args
    return (
        not produced_args
        or not required_args
        or typing.get_args(produced) == typing.get_args(required)
    )


def type_matches(produced: object, required: object) -> bool:  # noqa: PLR0911  # one return per form
    """Whether a node returning `produced` may feed a parameter annotated `required`.

    An `Any` producer or parameter matches anything; a `TypeVar` matches only
    itself; a union producer matches when every member does, and a union parameter
    when one member does. A `Literal` producer answers as its values' types and a
    `NewType` as its supertype. Generics match by kind, and by their arguments
    when both sides name them.
    """
    if produced is Any:
        return True
    if isinstance(produced, TypeVar) or isinstance(required, TypeVar):
        return produced == required
    if produced == required:
        return True
    produced, required = _strip(produced), _strip(required)
    if required is Any or produced == required:
        return True
    if isinstance(produced, NewType):
        return type_matches(produced.__supertype__, required)
    if typing.get_origin(produced) is Literal:
        return all(type_matches(type(v), required) for v in typing.get_args(produced))
    if _is_union(produced):
        return all(type_matches(m, required) for m in typing.get_args(produced))
    if _is_union(required):
        return any(type_matches(produced, m) for m in typing.get_args(required))
    if isinstance(required, NewType) or typing.get_origin(required) is Literal:
        return False
    return _generic_matches(produced, required)
