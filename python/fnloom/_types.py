"""Whether a value fits a declared type, and whether one type may feed another.

The answers agree with Hamilton 1.90.0 over the matrix of design §10, except
where the quirk register says fnloom differs (Q37 to Q45). Forms outside the
matrix are unsupported: they get an answer, but no promise of Hamilton's.
`Rules` holds the opt-ins, deep checks and numeric widening, which Hamilton has
not; an annotation that did not resolve at build is `Unresolved` (Q81).
"""

import abc
import annotationlib
import collections.abc
import dataclasses
import functools
import re
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


@dataclasses.dataclass(frozen=True, slots=True)
class Unresolved:
    """An annotation that did not evaluate at build: a named, opaque type (Q81).

    `name` is the annotation as written, such as `"decimal.Decimal"` for a name
    imported only under `TYPE_CHECKING`. It feeds and is fed by an annotation of
    the same text, and by `Any`, and no other type; no value fits it, and a check
    of a value against it raises `UnresolvedError`.
    """

    name: str

    @typing.override
    def __repr__(self) -> str:
        return f"unresolved {self.name!r}"


class UnresolvedError(TypeError):
    """A value was checked against an `Unresolved` annotation, which none can fit."""

    def __init__(self, unresolved: Unresolved) -> None:
        """Says that `unresolved` cannot check a value, and how to make it resolve."""
        self.unresolved = unresolved
        super().__init__(
            f"cannot check a value against {unresolved!r}: it did not resolve when "
            "the graph was built; import what it names at run time, not only under "
            "TYPE_CHECKING"
        )


@dataclasses.dataclass(frozen=True, slots=True)
class Rules:
    """The opt-in rules a value check follows on top of the default, shallow ones.

    `deep` checks every item of a standard collection, nested ones included,
    where the default reads at most the first. `widen` lets an `int` (not a
    `bool`) and a numpy floating scalar fit `float`, and a numpy integer scalar
    fit `int`.
    """

    deep: bool = False
    widen: bool = False


SHALLOW = Rules()
"""The default rules: Hamilton's shallow checks, no widening."""


# A `ForwardRef` as `repr` shows it inside a generic, for `_written`.
_FORWARD_REF = re.compile(r"ForwardRef\('((?:[^'\\]|\\.)*)'[^)]*\)")


def _written(annotation: object) -> str:
    """`annotation` as text, each unresolved name in it as written."""
    if isinstance(annotation, annotationlib.ForwardRef):
        return annotation.__forward_arg__
    return _FORWARD_REF.sub(r"\1", annotationlib.type_repr(annotation))


def _parts(current: object, seen: set[int]) -> list[object] | None:
    """The annotations `current` is made of, or `None` when a name in it is unresolved.

    A PEP 695 alias is made of its value, read once (`seen` holds those read); a
    `TypeVar` has its bound and constraints read here, once (see `_limits`).
    `Annotated` metadata is no type: only the annotated type is a part.
    """
    if isinstance(current, annotationlib.ForwardRef):
        return None
    if isinstance(current, list | tuple):
        return list(typing.cast("collections.abc.Sequence[object]", current))
    parts: list[object] = []
    if isinstance(current, typing.TypeAliasType):
        if id(current) in seen:
            return parts
        seen.add(id(current))
        try:
            parts.append(current.__value__)
        except NameError, AttributeError:
            return None
    elif isinstance(current, TypeVar):
        _limits(current)
    origin = typing.get_origin(current)
    args = typing.get_args(current)
    parts.extend(args[:1] if origin is Annotated else args)
    if origin is not None:
        parts.append(origin)
    return parts


def _unresolvable(annotation: object) -> bool:
    """Whether some name in `annotation` did not resolve, nested or through an alias.

    A `ForwardRef` left by evaluating it, or a PEP 695 alias whose value raises
    `NameError` or `AttributeError` when read, is such a name (Q44, Q81). Each
    `TypeVar`'s bound and constraints are read too, once, so a run never
    evaluates one. `Annotated` metadata is never read.
    """
    seen: set[int] = set()
    pending = [annotation]
    while pending:
        current = pending.pop()
        if isinstance(current, type):
            # A class, the common case, holds no name.
            continue
        if (parts := _parts(current, seen)) is None:
            return True
        pending.extend(parts)
    return False


def resolved_hints(owner: object, *, extras: bool = True) -> dict[str, Any]:
    """`typing.get_type_hints(owner)`, an annotation that does not resolve `Unresolved`.

    An annotation naming something that does not exist at run time, such as a
    name imported only under `TYPE_CHECKING`, or an alias over one, becomes
    `Unresolved` with its text as written, and the others are evaluated as usual
    (Q81). `extras` keeps `Annotated` metadata.
    """
    try:
        hints = typing.get_type_hints(owner, include_extras=extras)
    except NameError, AttributeError:
        hints = typing.get_type_hints(
            owner, include_extras=extras, format=annotationlib.Format.FORWARDREF
        )
    for key, hint in hints.items():
        if _unresolvable(hint):
            hints[key] = Unresolved(_written(hint))
    return hints


def _evaluated(
    evaluate: collections.abc.Callable[[annotationlib.Format], object], *, many: bool
) -> object:
    """A lazily evaluated `TypeVar` part; one naming what does not exist `Unresolved`.

    With `many`, the part is a tuple of constraints, each made `Unresolved` alone.
    """
    value = annotationlib.call_evaluate_function(
        evaluate, annotationlib.Format.FORWARDREF
    )
    if many:
        parts = typing.cast("tuple[object, ...]", value)
        return tuple(
            Unresolved(_written(part)) if _unresolvable(part) else part
            for part in parts
        )
    return Unresolved(_written(value)) if _unresolvable(value) else value


@functools.lru_cache(maxsize=256)
def _limits(typevar: TypeVar) -> tuple[object, tuple[object, ...]]:
    """`typevar`'s bound and constraints, each read once.

    A PEP 695 `TypeVar` (`def f[U: Decimal](x: U)`) evaluates them only when first
    read, so a name in them that does not resolve would raise then, at run. Read
    here, at build, such a part is `Unresolved` (Q81) instead.
    """
    try:
        return typevar.__bound__, typevar.__constraints__
    except NameError, AttributeError:
        pass
    if (evaluate := typevar.evaluate_constraints) is not None:
        constraints = typing.cast("tuple[object, ...]", _evaluated(evaluate, many=True))
        return None, constraints
    if (evaluate := typevar.evaluate_bound) is not None:
        return _evaluated(evaluate, many=False), ()
    return None, ()


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
        hash((aliased, args))
    except TypeError:
        # An unhashable part, such as `Annotated` metadata: substituted afresh on
        # each reading, so only the walk's budget ends a cycle through it.
        return aliased[args]
    return _substituted(aliased, args)


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
    """The questions one question depends on: any of them (`any_`), or all of them.

    `descends` when the questions are about the value's items, not the value.
    """

    any_: bool
    questions: list[tuple[object, object]]
    descends: bool = False


@dataclasses.dataclass(slots=True)
class _Frame:
    """A group being evaluated: its next question, and its own pair while it is open.

    `descents` counts the steps into an item between the first question and this
    one, so a repeated question tells whether the value changed on the way.
    """

    group: _Group
    pair: _Pair
    descents: int
    next: int = 0


type _Pair = tuple[int, int | str]
"""A question's key: its type by identity, its value by identity, a string by value.

A one-character string above U+00FF is a new object each time it is read from a
string, so a string is compared by equality, or the walk would never see it again.
"""


def _pair(declared: object, value: object) -> _Pair:
    return (id(declared), value if type(value) is str else id(value))


WALK_BUDGET = 1_000_000
"""How many groups one check may open before it gives up with a `TypeError`.

A group is opened for a value whose items need questions of their own: a union,
a fixed tuple, or under deep checks a collection whose item type is no plain
class. A legitimate value opens a few per level it is read to, so this is far
beyond any real depth. Deep checks open one per container of containers, so a
list of 100,000 `int`s or of 100,000 `tuple[int, str]`s opens one; a value holding
over a million collections whose own items are collections ends here, as does a
cycle the walk cannot see.
"""


type _Expanded = bool | _Group


def _first_item(element: object, value: object) -> _Expanded:
    """The question about the first item of the collection `value`; `True` if empty."""
    for item in typing.cast("collections.abc.Iterable[object]", value):
        return _Group(any_=False, questions=[(element, item)], descends=True)
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
            return _Group(
                any_=False,
                questions=list(zip(items, value, strict=True)),
                descends=True,
            )
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
    bound, constraints = _limits(typevar)
    if constraints:
        return _Group(any_=True, questions=[(c, None) for c in constraints])
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


type _Expand = collections.abc.Callable[[object, object], _Expanded]
"""One rule set's answer to a question, or the group of questions it depends on."""


def _walk(  # noqa: C901  # one loop, its branches the walk's states
    declared: object,
    value: object,
    expand: _Expand = _expand,
    budget: int = WALK_BUDGET,
) -> bool:
    """Whether `value` fits `declared`, by the rules of `expand`, with no recursion.

    `expand` answers one (type, value) question or names the group it depends on;
    this evaluates the groups depth first with an explicit stack, stopping a group
    as soon as its answer is known.

    A question already open further up the stack (the same type object, about the
    same value object or an equal string) is a cycle:

    - **Through the value's items** (a list holding itself): it fits, so a cyclic
      value ends.
    - **Through types alone** (`type X = X | int`), with no step into an item on
      the way: it does not fit, so a union falls to its other members.
    - **A string**, whose first item is a string again: it does not fit, as a
      recursive alias never ends in one.

    Past `budget` groups, a cycle no key can see (a type made afresh on each
    reading), or a value with more containers of containers than that, it raises
    `TypeError` naming the type. A value that does not fit because an
    `Unresolved` type was asked about raises `UnresolvedError` (Q81).
    """
    answer = expand(declared, value)
    if isinstance(answer, bool):
        if not answer and type(declared) is Unresolved:
            raise UnresolvedError(declared)
        return answer
    stack = [_Frame(answer, _pair(declared, value), 0)]
    open_pairs = {stack[0].pair: 0}
    opened = 1
    finished: bool | None = None
    unresolved: Unresolved | None = None
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
            pair = _pair(question, item)
            descents = frame.descents + group.descends
            if (before := open_pairs.get(pair)) is not None:
                finished = descents > before and type(item) is not str
                continue
            expanded = expand(question, item)
            if isinstance(expanded, bool):
                if not expanded and type(question) is Unresolved:
                    unresolved = question
                finished = expanded
                continue
            opened += 1
            if opened > budget:
                msg = (
                    f"value too large or too cyclic to check against {declared!r}: "
                    f"more than {budget:,} steps"
                )
                raise TypeError(msg)
            stack.append(_Frame(expanded, pair, descents))
            open_pairs[pair] = descents
            finished = None
            continue
        stack.pop()
        del open_pairs[frame.pair]
        if not stack:
            if not answer and unresolved is not None:
                raise UnresolvedError(unresolved)
            return answer
        finished = answer


def _fits(declared: object, value: object) -> bool:
    """Whether `value` fits `declared`: the shallow walk every check uses."""
    return _walk(declared, value)


# --- Opt-in rules: numeric widening and deep checks ----------------------------


@functools.lru_cache(maxsize=256)
def _numpy_kind(cls: type) -> str | None:
    """`"integer"` or `"floating"` for a numpy scalar class of that kind, else `None`.

    Read from the class's bases by module and name, so numpy is never imported. A
    `timedelta64`, which numpy counts as an integer, is neither.
    """
    kind: str | None = None
    for base in cls.__mro__:
        if getattr(base, "__module__", None) != "numpy":
            continue
        if base.__name__ == "timedelta64":
            return None
        if base.__name__ in {"integer", "floating"}:
            kind = kind or base.__name__
    return kind


def _widened(classes: tuple[type, ...], value: object) -> bool:
    """Whether numeric widening lets `value` fit one of `classes`, which it does not.

    An `int`, not a `bool`, and a numpy floating scalar fit `float`; a numpy
    integer scalar fits `int`.
    """
    cls = type(value)
    if float in classes and (
        (isinstance(value, int) and not isinstance(value, bool))
        or _numpy_kind(cls) == "floating"
    ):
        return True
    return int in classes and _numpy_kind(cls) == "integer"


def _widens(classes: Classes) -> tuple[type, ...] | None:
    """`classes` as a tuple when widening changes their answer, else `None`."""
    members = classes if isinstance(classes, tuple) else (classes,)
    return members if int in members or float in members else None


def _plain_matcher(classes: Classes, *, widen: bool) -> Matcher:
    """`isinstance` against `classes`, and with `widen` the numeric widening too."""
    if widen and (members := _widens(classes)) is not None:
        return lambda value: isinstance(value, classes) or _widened(members, value)
    return _isinstance_of(classes)


def _widening(expand: _Expand) -> _Expand:
    """`expand`, with numeric widening of a declared `int` or `float` (decision 12f)."""

    def widened(declared: object, value: object) -> _Expanded:
        answer = expand(declared, value)
        if answer is not False:
            return answer
        declared = _strip(declared)
        return (declared is int or declared is float) and _widened((declared,), value)

    return widened


# The origins whose items a deep check reads: the standard collections, never an
# iterator or a generator, and never `type[T]`, whose value is a class.
_DEEP_MODULES = frozenset({"builtins", "collections", "collections.abc"})


def _reads_items(origin: type) -> bool:
    return (
        origin.__module__ in _DEEP_MODULES
        and origin is not type
        and issubclass(origin, collections.abc.Iterable)
        and not issubclass(origin, collections.abc.Iterator)
    )


def _kind_fits(origin: type, value: object) -> bool:
    """Whether `value` is of the kind a generic of `origin` takes, items aside.

    The shallow check's kinds: exactly the class, else any of a sequence or an
    iterable family for one of it, else an instance of another abstract
    collection (Q45).
    """
    if type(value) is origin:
        return True
    for group in (_SEQUENCES, _ITERABLES):
        if origin in group:
            return isinstance(value, group)
    return origin in _BY_KIND and isinstance(value, origin)


@functools.lru_cache(maxsize=256)
def _cached_plain(declared: object) -> Classes | None:
    return plain_class(declared)


def _item_classes(declared: object) -> Classes | None:
    """`plain_class(declared)`, made once per hashable type."""
    try:
        return _cached_plain(declared)
    except TypeError:
        # An unhashable annotation, such as `Annotated[int, []]`.
        return plain_class(declared)


class _Deep:
    """The deep rules' expansion (decision 12d), with numeric widening or without.

    A standard collection asks about every item: each item of a `list`, `set`,
    `tuple[T, ...]` or other iterable, each key and value of a mapping, and a
    nested collection's items in turn. An item type that is flat, a plain class
    or a fixed tuple of them, is checked in a loop, with no question per item.
    Everything else, an iterator included, which is never read (Q43), answers as
    the shallow rules.
    """

    __slots__ = ("_matchers", "_shallow", "_widen")

    def __init__(self, *, widen: bool) -> None:
        """The deep rules, widening numbers too with `widen`."""
        self._widen = widen
        self._shallow = _widening(_expand) if widen else _expand
        # Each flat item type's check, by type, made on first use.
        self._matchers: dict[object, Matcher | None] = {}

    def _plain(self, declared: object) -> Matcher | None:
        """The check of a flat item type, made once, or `None` for another type."""
        try:
            return self._matchers[declared]
        except KeyError:
            pass
        except TypeError:
            # An unhashable annotation, such as `Annotated[int, []]`.
            return self._made(declared)
        made = self._matchers[declared] = self._made(declared)
        return made

    def _made(self, declared: object) -> Matcher | None:
        """`isinstance` for a plain class; each item's, for a fixed tuple of them."""
        if (classes := plain_class(declared)) is not None:
            return _plain_matcher(classes, widen=self._widen)
        declared = _strip(declared)
        if typing.get_origin(declared) is not tuple:
            return None
        items = _fixed_items(declared)
        if items is None or any(plain_class(item) is None for item in items):
            return None
        return _tuple_matcher(items, Rules(deep=True, widen=self._widen))

    def _all_of(
        self, element: object, items: collections.abc.Iterable[object]
    ) -> _Expanded:
        if (matches := self._plain(element)) is not None:
            return all(map(matches, items))
        return _Group(
            any_=False, questions=[(element, item) for item in items], descends=True
        )

    def _mapping(
        self, key: object, item: object, value: collections.abc.Mapping[object, object]
    ) -> _Expanded:
        keys, items = self._plain(key), self._plain(item)
        if keys is not None and items is not None:
            return all(keys(k) and items(v) for k, v in value.items())
        questions: list[tuple[object, object]] = []
        for k, v in value.items():
            questions.extend(((key, k), (item, v)))
        return _Group(any_=False, questions=questions, descends=True)

    def _fixed(self, items: tuple[object, ...], value: tuple[object, ...]) -> _Expanded:
        if len(value) != len(items):
            return False
        checks = [self._plain(item) for item in items]
        if all(check is not None for check in checks):
            return all(
                typing.cast("Matcher", check)(item)
                for check, item in zip(checks, value, strict=True)
            )
        return _Group(
            any_=False, questions=list(zip(items, value, strict=True)), descends=True
        )

    def _items(self, declared: object, origin: type, value: object) -> _Expanded:
        """A collection of a kind `origin` takes: the questions about its items."""
        args = _element_args(declared)
        if not args:
            return True
        if (
            len(args) == 2  # noqa: PLR2004  # a key and a value type
            and issubclass(origin, collections.abc.Mapping)
            and isinstance(value, collections.abc.Mapping)
        ):
            pairs = typing.cast("collections.abc.Mapping[object, object]", value)
            return self._mapping(args[0], args[1], pairs)
        if len(args) == 1:
            items = typing.cast("collections.abc.Iterable[object]", value)
            return self._all_of(args[0], items)
        return True

    def __call__(self, declared: object, value: object) -> _Expanded:
        """Whether `value` fits `declared`, or the questions it depends on."""
        declared = _strip(declared)
        origin = typing.get_origin(declared)
        if (
            not isinstance(origin, type)
            or _is_union(declared)
            or isinstance(value, collections.abc.Iterator)
            or not _reads_items(origin)
        ):
            return self._shallow(declared, value)
        if origin is tuple:
            if not isinstance(value, tuple):
                return False
            if (items := _fixed_items(declared)) is not None:
                return self._fixed(items, typing.cast("tuple[object, ...]", value))
            if typing.get_args(declared)[1:] != (...,):
                # A bare `Tuple`, or one with an unpacked part: shallow.
                return self._shallow(declared, value)
        if not _kind_fits(origin, value):
            return False
        return self._items(declared, origin, value)


@functools.cache
def _expander(rules: Rules) -> _Expand:
    """The expansion `rules` check by: `_expand` itself for the default rules."""
    if rules.deep:
        return _Deep(widen=rules.widen)
    return _widening(_expand) if rules.widen else _expand


def value_matches(
    declared: object, value: object, *, deep: bool = False, widen: bool = False
) -> bool:
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
    - **An annotation that did not resolve at build** (`Unresolved`, Q81) fits no
      value: checking one raises `UnresolvedError`, a `TypeError`.

    `deep` and `widen` apply the opt-in rules of `Builder.with_deep_type_checks()`
    and `Builder.with_numeric_widening()`: every item of a standard collection is
    checked, nested ones too, and an `int` or numpy floating scalar fits `float`,
    and a numpy integer scalar `int`. A value with more than `WALK_BUDGET`
    containers of containers, or a cycle no check can see, raises `TypeError`.
    """
    if not deep and not widen:
        return _fits(declared, value)
    return _walk(declared, value, _expander(Rules(deep=deep, widen=widen)))


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


def _item_matcher(declared: object, rules: Rules = SHALLOW) -> Matcher:
    """`isinstance` against a plain class, else the general walk, for one item type.

    A nested fixed tuple takes the general walk, which reads nested tuples with a
    stack, so preparing a check never recurses over an annotation's depth.
    """
    if (classes := plain_class(declared)) is not None:
        return _plain_matcher(classes, widen=rules.widen)
    if rules == SHALLOW:
        return functools.partial(_fits, declared)
    return functools.partial(_walk, declared, expand=_expander(rules))


def _tuple_matcher(items: tuple[object, ...], rules: Rules = SHALLOW) -> Matcher:
    """The check of a fixed-length tuple of `items`, each item's check made once."""
    checks = tuple(_item_matcher(item, rules) for item in items)
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


def check_of(
    declared: object, rules: Rules = SHALLOW
) -> tuple[Classes | None, Matcher]:
    """`declared`'s plain classes (see `plain_class`), and its check, made once.

    The check answers as `value_matches(declared, value)`, under `rules`, for
    every value. A plan's checks are made when it is prepared, so a run reads no
    annotation: a plain class or union of them is one `isinstance`, a fixed tuple
    checks its length and each item's own check, and, under the default rules, a
    generic takes a value of exactly its class at once. Any other type, or value,
    takes the general walk.

    The classes are `None` when `rules` widen them (an `int` for `float`), so a
    caller checking by `isinstance` alone uses the check instead.
    """
    if (classes := plain_class(declared)) is not None:
        if rules.widen and _widens(classes) is not None:
            return None, _plain_matcher(classes, widen=True)
        return classes, _isinstance_of(classes)
    declared = _strip(declared)
    origin = typing.get_origin(declared)
    if origin is tuple and (items := _fixed_items(declared)) is not None:
        return None, _tuple_matcher(items, rules)
    if rules != SHALLOW:
        return None, _ruled_matcher(declared, origin, rules)
    if isinstance(origin, type) and not _is_union(declared):
        # The general walk's first answer for a generic: its exact class fits. Not
        # for a union, whose origin `types.UnionType` is a class too: a value of
        # it, such as `int | str`, is no member of the union.
        exact = origin

        def matches(value: object) -> bool:
            return type(value) is exact or _fits(declared, value)

        return None, matches
    return None, functools.partial(_fits, declared)


def _ruled_matcher(declared: object, origin: object, rules: Rules) -> Matcher:
    """`check_of`'s check of a type that is no plain class or fixed tuple, by `rules`.

    Deep checks never take a value by its class alone: they read a `list`'s
    items too. Widening alone keeps the shortcut, as it changes no collection.
    """
    walk = functools.partial(_walk, declared, expand=_expander(rules))
    if not isinstance(origin, type) or _is_union(declared):
        return walk
    exact = origin
    if not rules.deep:
        return lambda value: type(value) is exact or walk(value)
    if (items := _deep_items(declared, origin, rules)) is None:
        return walk
    # A value of exactly the class: its items checked in a loop, with no walk.
    return lambda value: items(value) if type(value) is exact else walk(value)


def _deep_items(declared: object, origin: type, rules: Rules) -> Matcher | None:
    """For a collection of plain-class items, the check of a value of its class.

    `list[int]`, `tuple[int, ...]` and `dict[str, int]` check each item, or key
    and value, by `isinstance`; `None` for another type, which the walk checks.
    """
    args = _element_args(declared)
    if not _reads_items(origin) or (origin is tuple and len(args) != 1):
        return None
    plain = [_item_classes(arg) for arg in args]
    if not args or any(classes is None for classes in plain):
        return None
    checks = [
        _plain_matcher(typing.cast("Classes", classes), widen=rules.widen)
        for classes in plain
    ]
    if len(checks) == 1:
        item = checks[0]
        return lambda value: all(
            map(item, typing.cast("collections.abc.Iterable[object]", value))
        )
    if len(checks) == 2 and issubclass(origin, collections.abc.Mapping):  # noqa: PLR2004  # a key and a value type
        key, item = checks

        def mapping(value: object) -> bool:
            pairs = typing.cast("collections.abc.Mapping[object, object]", value)
            return all(key(k) and item(v) for k, v in pairs.items())

        return mapping
    return None


def checks_of(
    declared_types: collections.abc.Iterable[object], rules: Rules = SHALLOW
) -> list[tuple[Classes | None, Matcher]]:
    """`check_of` under `rules` for each type, made once for each distinct type.

    The checks made are kept for this call only, so checks under other rules are
    never mixed up with them.
    """
    make = check_of if rules == SHALLOW else functools.partial(check_of, rules=rules)
    made: dict[object, tuple[Classes | None, Matcher]] = {}
    checks: list[tuple[Classes | None, Matcher]] = []
    for declared in declared_types:
        try:
            check = made[declared]
        except KeyError:
            check = made[declared] = make(declared)
        except TypeError:
            # An unhashable annotation, such as `Annotated[int, []]`: made each time.
            check = make(declared)
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
