"""Whether a value fits a declared type, and whether one type may feed another.

The answers agree with Hamilton 1.90.0 over the matrix of design §10, except
where the quirk register says fnloom differs (Q37 to Q45). Forms outside the
matrix are unsupported: they get an answer, but no promise of Hamilton's.
"""

import abc
import collections.abc
import types
import typing
from typing import Annotated, Any, Literal, NewType, TypeVar, Union

# Each group: a declared kind in it takes any value of the group, and then the
# value's first element is checked against the declaration's first argument.
_SEQUENCES: tuple[type, ...] = (list, tuple, collections.abc.Sequence)
_ITERABLES: tuple[type, ...] = (set, collections.abc.Iterable)
# Every other abstract collection or callable (`Mapping`, `Callable`, `Set`, ...)
# takes any value of its kind, its items or signature unchecked (Q45).
_BY_KIND: frozenset[type] = frozenset(
    abstract
    for abstract in (getattr(collections.abc, name) for name in collections.abc.__all__)
    if isinstance(abstract, type) and abstract not in {*_SEQUENCES, *_ITERABLES}
)


def _alias_value(annotation: object) -> object | None:
    """What a PEP 695 alias names (Q44), or `None` when `annotation` is no alias.

    `type Price = float` names `float`. A subscripted generic alias, `Pair[int]`
    for `type Pair[T] = tuple[T, T]`, names its value with the arguments put in:
    when the value is one of the alias's own type parameters, or a generic whose
    free parameters are exactly the alias's, in order. Any other generic alias
    (one that reorders, repeats or drops its parameters, or takes a `*Ts` or a
    `**P`) is not read, and answers as before.
    """
    if isinstance(annotation, typing.TypeAliasType):
        return annotation.__value__
    alias = typing.get_origin(annotation)
    if not isinstance(alias, typing.TypeAliasType):
        return None
    params, args = alias.__type_params__, typing.get_args(annotation)
    if len(params) != len(args) or not all(isinstance(p, TypeVar) for p in params):
        return None
    aliased = alias.__value__
    if aliased in params:
        return args[params.index(aliased)]
    if getattr(aliased, "__parameters__", None) != params:
        return None
    return aliased[args]


def _strip(annotation: object) -> object:
    """The type an answer is about.

    `Annotated[T, ...]` is `T`: metadata never changes an answer (Q41). A PEP 695
    alias is what it names, read through as many aliases as there are (Q44); an
    alias reached a second time, in aliases naming each other, stops the reading.
    """
    # A list, compared by equality: `Pair[int]` is a new object on each reading.
    seen: list[object] = []
    while True:
        if typing.get_origin(annotation) is Annotated:
            annotation = typing.get_args(annotation)[0]
            continue
        if annotation in seen:
            return seen[0]
        if (aliased := _alias_value(annotation)) is None:
            return annotation
        seen.append(annotation)
        annotation = aliased


def read_aliases(annotation: object) -> None:
    """Evaluates the value of every PEP 695 alias in `annotation`, nested or not.

    An alias's value is evaluated only when first read, so a name in it that does
    not resolve raises then: reading them all at build raises that `NameError` or
    `AttributeError` once, where the build can name the annotation (Q44).
    """
    if isinstance(annotation, type):
        # A class, the common case, holds no alias.
        return
    seen: set[int] = set()
    pending = [annotation]
    while pending:
        current = pending.pop()
        if isinstance(current, type):
            continue
        if isinstance(current, list | tuple):
            pending.extend(typing.cast("collections.abc.Sequence[object]", current))
            continue
        if isinstance(current, typing.TypeAliasType):
            if id(current) in seen:
                continue
            seen.add(id(current))
            pending.append(current.__value__)
        pending.extend(typing.get_args(current))
        origin = typing.get_origin(current)
        if origin is not None:
            pending.append(origin)


def _is_union(annotation: object) -> bool:
    return typing.get_origin(annotation) in {Union, types.UnionType}


def _element_args(annotation: object) -> tuple[object, ...]:
    """The arguments of a generic, with `tuple[T, ...]` read as one element type `T`."""
    args = typing.get_args(annotation)
    if typing.get_origin(annotation) is tuple and args[1:] == (...,):
        return args[:1]
    return args


# The (element type, string) questions already being asked in one check. A
# string's first item is a string, so under a recursive alias (`type Tree =
# list[Tree]`) the same question comes round again; it is answered `False`.
type _Asked = tuple[tuple[object, str], ...]


def _first_fits(element: object, value: object, asked: _Asked) -> bool:
    """Whether the first item of the collection `value` fits `element`.

    An empty collection always fits.
    """
    for item in typing.cast("collections.abc.Iterable[object]", value):
        if isinstance(item, str):
            if (element, item) in asked:
                return False
            asked = (*asked, (element, item))
        return _fits(element, item, asked)
    return True


def _fits_generic(
    declared: object, origin: object, value: object, asked: _Asked
) -> bool:
    """A parameterized collection: its kind, then at most its first element.

    Only a sequence or an iterable has its first element checked; another
    abstract collection, or a callable, is checked by its kind alone (Q45).
    """
    if type(value) is origin:
        return True
    if origin is tuple and not isinstance(value, tuple):
        return False
    if isinstance(value, collections.abc.Iterator):
        # Never look inside an iterator: reading its first item would consume it,
        # so it is checked by its kind alone (Q43).
        return isinstance(origin, type) and isinstance(value, origin)
    for group in (_SEQUENCES, _ITERABLES):
        if origin in group:
            if not isinstance(value, group):
                return False
            args = _element_args(declared)
            return not args or _first_fits(args[0], value, asked)
    return origin in _BY_KIND and isinstance(value, typing.cast("type", origin))


def _fits(declared: object, value: object, asked: _Asked = ()) -> bool:  # noqa: PLR0911  # one return per form
    declared = _strip(declared)
    if declared is Any or isinstance(declared, TypeVar):
        return True
    if isinstance(declared, NewType):
        return _fits(declared.__supertype__, value, asked)
    if _is_union(declared):
        return any(_fits(member, value, asked) for member in typing.get_args(declared))
    origin = typing.get_origin(declared)
    if origin is Literal:
        return value in typing.get_args(declared)
    if origin is not None:
        return _fits_generic(declared, origin, value, asked)
    if not isinstance(declared, type):
        return False
    try:
        return isinstance(value, declared)
    except TypeError:  # a protocol that is not runtime-checkable (Q42)
        return False


def value_matches(declared: object, value: object) -> bool:
    """Whether `value`, supplied as an input or override, fits the `declared` type.

    `None` fits every declaration, as in Hamilton. A parameterized collection is
    checked by its kind and, for a sequence or set, by its first element only;
    a value whose class is exactly the declared one (a `list` for `list[int]`)
    is checked by its class alone, its items unread.
    """
    return value is None or _fits(declared, value)


def plain_class(declared: object) -> type | None:
    """The class `isinstance` checks `declared` against, or `None` when it is no class.

    For a returned class, `value_matches(declared, value)` is `value is None or
    isinstance(value, cls)` for every value, so a check compiled once per plan can
    skip the general walk. `Any`, a generic, and a class of any metaclass but
    `type` and `abc.ABCMeta`, protocols included (Q42), are not plain classes.
    """
    if type(declared) is type:
        # A class of the plain metaclass: no alias, no generic, no protocol.
        return declared
    declared = _strip(declared)
    if declared is Any or not isinstance(declared, type):
        return None
    if typing.get_origin(declared) is not None:
        return None
    # A metaclass of its own may refuse `isinstance` for some values only, which the
    # general check turns into a mismatch; only these two never refuse a class.
    if type(declared) not in {type, abc.ABCMeta}:
        return None
    return declared


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
    if produced == required:
        return True
    # Stripped first, so `Annotated[Any, ...]` and an alias of `Any` answer as `Any`.
    produced, required = _strip(produced), _strip(required)
    if produced is Any:
        return True
    if isinstance(produced, TypeVar) or isinstance(required, TypeVar):
        return produced == required
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
