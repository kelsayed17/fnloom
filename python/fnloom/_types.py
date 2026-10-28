"""Whether a value fits a declared type, and whether one type may feed another.

The answers agree with Hamilton 1.90.0 over the matrix of design §10, except
where the quirk register says fnloom differs (Q37 to Q45). Forms outside the
matrix are unsupported: they get an answer, but no promise of Hamilton's.
"""

import abc
import collections.abc
import dataclasses
import functools
import types
import typing
from typing import Annotated, Any, Literal, NewType, TypeVar, Union, Unpack

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


def _alias_value(annotation: object) -> object | None:  # noqa: PLR0911  # one return per form
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
    try:
        return _substituted(aliased, args)
    except TypeError:
        # An unhashable argument: substituted afresh on each reading.
        return aliased[args]


@functools.lru_cache(maxsize=256)
def _substituted(aliased: object, args: tuple[object, ...]) -> object:
    """`aliased[args]`, the same object on every reading of one generic alias.

    The value walk tells a cycle by the identity of the type it asks about, so a
    recursive generic alias (`type S[T] = Sequence[S[T]]`) must read as one object.
    """
    return typing.cast("typing.Any", aliased)[args]


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


def stripped(annotation: object) -> object:
    """`annotation` without `Annotated` metadata, and read through PEP 695 aliases."""
    return _strip(annotation)


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


def _fixed_items(declared: object) -> tuple[object, ...] | None:
    """The item types of a fixed-length tuple type, or `None` for any other tuple type.

    `tuple[()]` has no items. `tuple[T, ...]`, a bare `typing.Tuple` and a tuple with
    an unpacked part (`tuple[int, *tuple[str, ...]]`, `tuple[*Ts]`) have no fixed
    length, so they are no fixed tuple.
    """
    if declared is typing.Tuple:  # noqa: UP006  # the bare typing spelling has no items
        return None
    items = typing.get_args(declared)
    if items[1:] == (...,):
        return None
    for item in items:
        if getattr(item, "__unpacked__", False) or typing.get_origin(item) is Unpack:
            return None
    return items


# --- The value walk -------------------------------------------------------------
#
# Whether a value fits a type is a tree of questions: a union asks whether any
# member fits the value (an OR-group), a fixed tuple whether every item fits its
# type (an AND-group), a sequence whether its first item fits the element type (an
# AND-group of one). `_expand` answers one question, or turns it into the group of
# questions it depends on; `_walk` evaluates the tree with a stack of its own,
# never by recursion, so no value is too deep and no annotation too nested.


@dataclasses.dataclass(slots=True)
class _Group:
    """The questions one question depends on: any of them (`any_`), or all of them."""

    any_: bool
    questions: list[tuple[object, object]]


@dataclasses.dataclass(slots=True)
class _Frame:
    """A group being evaluated: its next question, and its own pair while it is open."""

    group: _Group
    pair: tuple[int, int]
    next: int = 0


type _Expanded = bool | _Group


def _first_item(element: object, value: object) -> _Expanded:
    """The question about the first item of the collection `value`; `True` if empty."""
    for item in typing.cast("collections.abc.Iterable[object]", value):
        return _Group(any_=False, questions=[(element, item)])
    return True


def _expand_generic(  # noqa: PLR0911  # one return per form
    declared: object, origin: object, value: object
) -> _Expanded:
    """A parameterized collection: its kind, then at most its items' questions.

    A fixed-length tuple asks about each item (Q80); otherwise only a sequence or
    an iterable asks about its first item, and another abstract collection or a
    callable is checked by its kind alone (Q45).
    """
    if origin is tuple:
        if not isinstance(value, tuple):
            return False
        if (items := _fixed_items(declared)) is not None:
            if len(value) != len(items):
                return False
            return _Group(any_=False, questions=list(zip(items, value, strict=True)))
    if type(value) is origin:
        return True
    if isinstance(value, collections.abc.Iterator):
        # Never look inside an iterator: reading its first item would consume it,
        # so it is checked by its kind alone (Q43).
        return isinstance(origin, type) and isinstance(value, origin)
    for group in (_SEQUENCES, _ITERABLES):
        if origin in group:
            if not isinstance(value, group):
                return False
            args = _element_args(declared)
            return not args or _first_item(args[0], value)
    return origin in _BY_KIND and isinstance(value, typing.cast("type", origin))


def _expand_typevar(typevar: TypeVar, value: object) -> _Expanded:
    """Whether `value` fits `typevar`, or the questions that decide it for `None`.

    Any value but `None` fits, as in Hamilton; `None` fits a free `TypeVar`, or one
    whose bound, or one of whose constraints, admits it (Q79).
    """
    if value is not None:
        return True
    if constraints := typevar.__constraints__:
        return _Group(any_=True, questions=[(c, None) for c in constraints])
    bound = typevar.__bound__
    return bound is None or _Group(any_=False, questions=[(bound, None)])


def _expand(declared: object, value: object) -> _Expanded:  # noqa: PLR0911  # one return per form
    """The answer to whether `value` fits `declared`, or the questions it depends on.

    This is the one place the shallow rules live: `_walk` only evaluates them.
    """
    declared = _strip(declared)
    if declared is Any:
        return True
    if isinstance(declared, TypeVar):
        return _expand_typevar(declared, value)
    if declared is None:
        # `None` written as a type, as `get_type_hints` would read it: `NoneType`.
        return value is None
    if isinstance(declared, NewType):
        return _Group(any_=False, questions=[(declared.__supertype__, value)])
    if _is_union(declared):
        return _Group(
            any_=True, questions=[(m, value) for m in typing.get_args(declared)]
        )
    origin = typing.get_origin(declared)
    if origin is Literal:
        return value in typing.get_args(declared)
    if origin is not None:
        return _expand_generic(declared, origin, value)
    if not isinstance(declared, type):
        return False
    try:
        return isinstance(value, declared)
    except TypeError:
        # A `TypedDict` is checked by its kind, any `dict`, its keys unread (Q61);
        # a protocol that is not runtime-checkable is no match (Q42).
        return typing.is_typeddict(declared) and isinstance(value, dict)


def _walk(
    declared: object,
    value: object,
    expand: collections.abc.Callable[[object, object], _Expanded] = _expand,
) -> bool:
    """Whether `value` fits `declared`, by the rules of `expand`, with no recursion.

    `expand` answers one (type, value) question or names the group it depends on;
    this evaluates the groups depth first with an explicit stack, stopping a group
    as soon as its answer is known. A question already open further up the stack,
    the same type object about the same value object, is a cycle in the value or a
    recursive alias: it fits, so a cyclic value ends, except that a string, whose
    first item is a string again, does not fit (a recursive alias never ends in one).
    """
    answer = expand(declared, value)
    if isinstance(answer, bool):
        return answer
    stack = [_Frame(answer, (id(declared), id(value)))]
    open_pairs = {stack[0].pair}
    finished: bool | None = None
    while True:
        frame = stack[-1]
        group = frame.group
        if finished is not None and finished is group.any_:
            # An OR-group with a fitting question, or an AND-group with a failing one.
            answer = finished
        elif frame.next == len(group.questions):
            # Every question asked: an OR-group found none fit, an AND-group all.
            answer = not group.any_
        else:
            question, item = group.questions[frame.next]
            frame.next += 1
            pair = (id(question), id(item))
            if pair in open_pairs:
                finished = not isinstance(item, str)
                continue
            expanded = expand(question, item)
            if isinstance(expanded, bool):
                finished = expanded
                continue
            stack.append(_Frame(expanded, pair))
            open_pairs.add(pair)
            finished = None
            continue
        stack.pop()
        open_pairs.discard(frame.pair)
        if not stack:
            return answer
        finished = answer


def _fits(declared: object, value: object) -> bool:
    """Whether `value` fits `declared`: the shallow walk every check uses."""
    return _walk(declared, value)


def value_matches(declared: object, value: object) -> bool:
    """Whether `value`, supplied as an input or override, fits the `declared` type.

    This is the check `execute` runs on every input, override and extracted field.

    - **`None`** fits only a type that admits it (Q79): `Optional[T]`, `T | None`,
      `object`, `Any`, `None` itself, and a `TypeVar` that is free or whose bound
      or one of whose constraints admits it. Hamilton passes `None` for every type.
    - **A class** fits by `isinstance`, so a subclass fits; `bool` fits `int`, and
      `int` does not fit `float`.
    - **Unions, `Literal`, `Annotated`, `NewType` and PEP 695 aliases** answer per
      member, by value, as the bare type, as the supertype and as the aliased type.
    - **A `TypeVar`** takes every value but `None`, which it takes as above.
    - **A fixed-length tuple** (`tuple[int, str]`) takes a tuple of that length whose
      items each fit, nested tuples included (Q80); `tuple[()]` takes only `()`.
    - **A parameterized collection** is otherwise checked by its kind and, for a
      sequence or set, by its first element only; a value whose class is exactly
      the declared one (a `list` for `list[int]`, a `tuple` for `tuple[int, ...]`)
      is checked by its class alone, its items unread. Other `collections.abc`
      generics are checked by kind, and an iterator is never read.
    """
    return _fits(declared, value)


type Classes = type | tuple[type, ...]
"""What `isinstance` checks against: one class, or a tuple of them for a union."""

type Matcher = collections.abc.Callable[[object], bool]
"""One type's check, worked out beforehand: `value_matches`'s answer for a value."""


def _one_class(declared: object) -> type | None:
    """The class `isinstance` checks `declared` against, or `None` for no class."""
    if type(declared) is type:
        # A class of the plain metaclass: no alias, no generic, no protocol.
        return declared
    declared = _strip(declared)
    if declared is None:
        return types.NoneType
    if declared is Any or not isinstance(declared, type):
        return None
    if typing.get_origin(declared) is not None:
        return None
    # A metaclass of its own may refuse `isinstance` for some values only, which the
    # general check turns into a mismatch; only these two never refuse a class.
    if type(declared) not in {type, abc.ABCMeta}:
        return None
    return declared


def plain_class(declared: object) -> Classes | None:
    """What `isinstance` checks `declared` against, or `None` when that is no check.

    For a returned class or tuple of classes, `value_matches(declared, value)` is
    `isinstance(value, cls)` for every value, `None` included, so a check compiled
    once per plan can skip the general walk. A union of plain classes, `int | None`
    among them, is the tuple of its members. `Any`, a generic, and a class of any
    metaclass but `type` and `abc.ABCMeta`, protocols included (Q42), are not plain
    classes, and nor is a union with such a member.
    """
    if type(declared) is type:
        return declared
    declared = _strip(declared)
    if not _is_union(declared):
        return _one_class(declared)
    members: list[type] = []
    for member in typing.get_args(declared):
        if (cls := _one_class(member)) is None:
            return None
        members.append(cls)
    return tuple(members)


def _isinstance_of(classes: Classes) -> Matcher:
    return lambda value: isinstance(value, classes)


def _item_matcher(declared: object) -> Matcher:
    """`isinstance` against a plain class, else the general walk, for one item type.

    A nested fixed tuple takes the general walk, which reads nested tuples with a
    stack, so preparing a check never recurses over an annotation's depth.
    """
    if (classes := plain_class(declared)) is not None:
        return _isinstance_of(classes)
    return functools.partial(_fits, declared)


def _tuple_matcher(items: tuple[object, ...]) -> Matcher:
    """The check of a fixed-length tuple of `items`, each item's check made once."""
    checks = tuple(_item_matcher(item) for item in items)
    size = len(checks)

    def matches(value: object) -> bool:
        if not isinstance(value, tuple) or len(value) != size:
            return False
        for check, item in zip(
            checks, typing.cast("tuple[object, ...]", value), strict=True
        ):
            if not check(item):
                return False
        return True

    return matches


def check_of(declared: object) -> tuple[Classes | None, Matcher]:
    """`declared`'s plain classes (see `plain_class`), and its check, made once.

    The check answers as `value_matches(declared, value)` for every value. A plan's
    checks are made when it is prepared, so a run reads no annotation: a plain
    class or union of them is one `isinstance`, a fixed tuple checks its length
    and each item's own check, and a generic takes a value of exactly its class at
    once. Any other type, or value, takes the general walk.
    """
    if (classes := plain_class(declared)) is not None:
        return classes, _isinstance_of(classes)
    declared = _strip(declared)
    origin = typing.get_origin(declared)
    if origin is tuple and (items := _fixed_items(declared)) is not None:
        return None, _tuple_matcher(items)
    if isinstance(origin, type) and not _is_union(declared):
        # The general walk's first answer for a generic: its exact class fits. Not
        # for a union, whose origin `types.UnionType` is a class too: a value of
        # it, such as `int | str`, is no member of the union.
        exact = origin

        def matches(value: object) -> bool:
            return type(value) is exact or _fits(declared, value)

        return None, matches
    return None, functools.partial(_fits, declared)


def checks_of(
    declared_types: collections.abc.Iterable[object],
) -> list[tuple[Classes | None, Matcher]]:
    """`check_of` for each type, made once for each distinct type among them."""
    made: dict[object, tuple[Classes | None, Matcher]] = {}
    checks: list[tuple[Classes | None, Matcher]] = []
    for declared in declared_types:
        try:
            check = made[declared]
        except KeyError:
            check = made[declared] = check_of(declared)
        except TypeError:
            # An unhashable annotation, such as `Annotated[int, []]`: made each time.
            check = check_of(declared)
        checks.append(check)
    return checks


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


def type_matches(produced: object, expected: object) -> bool:  # noqa: PLR0911  # one return per form
    """Whether a node returning `produced` may feed a parameter annotated `expected`.

    This is the edge check `build` runs on every parameter another node feeds.

    - **Same type or a subclass** matches; `bool` feeds `int`, and `int` does not
      feed `float`.
    - **`Any`** on either side matches anything; a **`TypeVar`** matches only itself.
    - **A union producer** matches when every member does, so `int | None` feeds
      `object`; **a union parameter** when one member does, so `int` feeds
      `int | None`. `None` is a type like any other here: `int | None` does not feed
      `int`.
    - **A `Literal` producer** answers as its values' types, **a `NewType`** as its
      supertype, and `Annotated` and PEP 695 aliases as the type they name.
    - **Generics** match by kind, and by their arguments when both sides name them;
      `tuple[T, ...]` feeds `Sequence[T]`, and `tuple[int, str]` feeds
      `tuple[int, str]` and bare `tuple`, not `tuple[int, ...]`.
    """
    if produced == expected:
        return True
    # Stripped first, so `Annotated[Any, ...]` and an alias of `Any` answer as `Any`.
    produced, expected = _strip(produced), _strip(expected)
    if produced is Any:
        return True
    if isinstance(produced, TypeVar) or isinstance(expected, TypeVar):
        return produced == expected
    if expected is Any or produced == expected:
        return True
    if isinstance(produced, NewType):
        return type_matches(produced.__supertype__, expected)
    if typing.get_origin(produced) is Literal:
        return all(type_matches(type(v), expected) for v in typing.get_args(produced))
    if _is_union(produced):
        return all(type_matches(m, expected) for m in typing.get_args(produced))
    if _is_union(expected):
        return any(type_matches(produced, m) for m in typing.get_args(expected))
    if isinstance(expected, NewType) or typing.get_origin(expected) is Literal:
        return False
    return _generic_matches(produced, expected)
