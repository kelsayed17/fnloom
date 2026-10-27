"""Tests for `value_matches` and `type_matches`, fnloom's own answers."""

import collections.abc
from typing import (
    Annotated,
    Any,
    Literal,
    NewType,
    Protocol,
    TypeVar,
    override,
    runtime_checkable,
)

import pytest

from fnloom._types import plain_class, type_matches, value_matches


class Base:
    """A user class."""


class Child(Base):
    """A subclass of `Base`."""


@runtime_checkable
class Sized(Protocol):
    """A runtime-checkable protocol."""

    def size(self) -> int:
        """The size."""
        ...


class HasSize:
    """Conforms to `Sized` without naming it."""

    def size(self) -> int:
        """Always 1."""
        return 1


Count = NewType("Count", int)

DECLARATIONS: list[object] = [
    int,
    str,
    Base,
    list[int],
    int | str,
    Literal[1, 2],
    Annotated[int, "m"],
    Sized,
    Count,
]


@pytest.mark.parametrize("declared", DECLARATIONS)
def test_none_fits_every_declaration(declared: object) -> None:
    """`None` passes the check whatever the declaration (Hamilton KEEP)."""
    assert value_matches(declared, None) is True


def test_subclass_fits_its_base_and_not_the_reverse() -> None:
    """A `Child` value or producer fits `Base`; a `Base` does not fit `Child`."""
    assert value_matches(Base, Child()) is True
    assert value_matches(Child, Base()) is False
    assert type_matches(Child, Base) is True
    assert type_matches(Base, Child) is False


@pytest.mark.parametrize("other", [int, Base, list[int], int | None, Literal[1]])
def test_any_fits_everything(other: object) -> None:
    """`Any` accepts every value and producer, and an `Any` producer feeds anything."""
    assert value_matches(Any, object()) is True
    assert type_matches(other, Any) is True
    assert type_matches(Any, other) is True


def test_bool_for_int_answers_as_the_oracle() -> None:
    """`True` fits `int` and `bool` feeds `int`, as in Hamilton (Review Focus 4)."""
    assert value_matches(int, True) is True  # noqa: FBT003  # the bool value is the point
    assert type_matches(bool, int) is True


def test_int_for_float_answers_as_the_oracle() -> None:
    """`1` does not fit `float` and `int` does not feed `float`, as in Hamilton."""
    assert value_matches(float, 1) is False
    assert type_matches(int, float) is False


def test_annotated_answers_as_its_type() -> None:
    """Metadata changes nothing: a `Child()` fits `Annotated[Base, ...]` (Q41)."""
    assert value_matches(Annotated[Base, "m"], Child()) is True
    assert type_matches(Annotated[Child, "m"], Annotated[Base, "n"]) is True


def test_annotated_any_and_typevar_answer_as_themselves() -> None:
    """Metadata changes nothing for `Any` and a `TypeVar` either (Q41)."""
    t = TypeVar("t")
    assert type_matches(Annotated[Any, "m"], int) is True
    assert type_matches(Annotated[t, "m"], t) is True
    assert type_matches(t, Annotated[t, "m"]) is True
    assert type_matches(Annotated[t, "m"], int) is False


def test_runtime_checkable_protocol_takes_a_conforming_value() -> None:
    """A value with the protocol's methods fits it; one without does not (Q42)."""
    assert value_matches(Sized, HasSize()) is True
    assert value_matches(Sized, Base()) is False


def test_plain_protocol_is_no_match_rather_than_an_error() -> None:
    """A protocol not checkable at runtime answers False, never raises (Q42)."""

    class Plain(Protocol):
        def size(self) -> int: ...

    assert value_matches(Plain, HasSize()) is False
    assert type_matches(HasSize, Plain) is False
    assert type_matches(Plain, Plain) is True


def test_homogeneous_tuple_is_a_sequence() -> None:
    """`tuple[int, ...]` takes a tuple of ints and feeds `Sequence[int]` (Q37)."""
    assert value_matches(tuple[int, ...], (1, 2)) is True
    assert type_matches(tuple[int, ...], collections.abc.Sequence[int]) is True
    assert type_matches(tuple[int, ...], collections.abc.Sequence[str]) is False


def test_union_producer_matches_when_every_member_does() -> None:
    """`int | None` feeds `object` and `int | str | None`, not `int` (Q40)."""
    assert type_matches(int | None, object) is True
    assert type_matches(int | None, int | str | None) is True
    assert type_matches(int | None, int) is False


def test_literal_and_new_type_producers_answer_as_their_values_types() -> None:
    """`Literal[1, 2]` and a `NewType` over `int` both feed `int` (Q38, Q39)."""
    count = NewType("count", int)
    assert type_matches(Literal[1, 2], int) is True
    assert type_matches(Literal["a", 1], int) is False
    assert type_matches(count, int) is True
    assert type_matches(int, count) is False
    assert value_matches(count, 3) is True


def test_an_iterator_is_checked_without_being_consumed() -> None:
    """A generator fits `Iterable[int]` and `Iterator[int]`, keeping its items (Q43)."""
    items = iter([1, 2, 3])
    assert value_matches(collections.abc.Iterable[int], items) is True
    assert value_matches(collections.abc.Iterator[int], items) is True
    assert value_matches(list[int], items) is False
    assert list(items) == [1, 2, 3]
    generator = (n for n in (1, 2, 3))
    assert value_matches(collections.abc.Iterable[str], generator) is True
    assert list(generator) == [1, 2, 3]


type Price = float
type Prices = collections.abc.Sequence[Price]
type Again = Price
type Pair[T] = tuple[T, T]
type Swapped[K, V] = dict[V, K]
type Same[T] = T


def test_type_alias_answers_as_its_value() -> None:
    """A PEP 695 alias is checked as what it names, also through another alias (Q44)."""
    assert value_matches(Price, 1.5) is True
    assert value_matches(Price, "s") is False
    assert value_matches(Again, 1.5) is True
    assert value_matches(Prices, [1.5]) is True
    assert value_matches(Prices, ["s"]) is False
    assert value_matches(Price | None, 1.5) is True
    assert type_matches(float, Price) is True
    assert type_matches(Price, float) is True
    assert type_matches(Again, float) is True
    assert type_matches(float, Again) is True
    assert type_matches(str, Price) is False
    assert type_matches(Price, str) is False


type Anything = Any
type Tree = list[Tree]


def test_recursive_alias_refuses_a_string_without_recursing() -> None:
    """A string's first item is a string, so `Tree` would ask about it forever (Q44)."""
    assert value_matches(Tree, [[], [[]]]) is True
    assert value_matches(Tree, "x") is False
    assert value_matches(Tree, "ab") is False
    assert value_matches(Tree, ("a",)) is False


def test_alias_of_any_answers_as_any() -> None:
    """An alias naming `Any` feeds anything, as `Any` does (Q44)."""
    assert type_matches(Anything, int) is True
    assert type_matches(int, Anything) is True


def test_generic_type_alias_is_substituted_when_its_parameters_are_in_order() -> None:
    """`Pair[int]` is `tuple[int, int]`; an alias reordering its parameters is not."""
    assert value_matches(Pair[int], (1, 2)) is True
    assert value_matches(Pair[int], [1, 2]) is False
    assert type_matches(tuple[int, int], Pair[int]) is True
    assert type_matches(Pair[int], tuple[int, int]) is True
    assert type_matches(Pair[int], tuple[str, str]) is False
    assert value_matches(Same[Same[int]], 1) is True
    assert type_matches(tuple[int, int], Same[Same[Pair[int]]]) is True
    assert value_matches(Swapped[str, int], {1: "a"}) is False
    assert type_matches(dict[int, str], Swapped[str, int]) is False


def _function(x: int) -> int:
    return x


def test_abstract_collection_without_an_element_check_is_checked_by_its_kind() -> None:
    """`Mapping[str, int]` takes a dict, `Callable[..., T]` a function (Q45)."""
    assert value_matches(collections.abc.Mapping[str, int], {"a": 1}) is True
    assert value_matches(collections.abc.Mapping[str, int], {"a": "s"}) is True
    assert value_matches(collections.abc.Mapping[str, int], [1]) is False
    assert value_matches(collections.abc.MutableMapping[str, int], {"a": 1}) is True
    assert value_matches(collections.abc.Callable[[int], int], _function) is True
    assert value_matches(collections.abc.Callable[..., int], _function) is True
    assert value_matches(collections.abc.Callable[..., int], 1) is False
    assert value_matches(collections.abc.Sequence[int], ["s"]) is False
    assert type_matches(dict[str, int], collections.abc.Mapping[str, int]) is True
    assert type_matches(dict[str, int], collections.abc.Mapping[str, str]) is False


def test_cyclic_type_alias_answers_as_written() -> None:
    """Aliases naming each other are compared as written, and the answer ends (Q44)."""
    # Built at run time: a type checker refuses a circular alias in source.
    namespace: dict[str, object] = {}
    exec("type Ping = Pong\ntype Pong = Ping", namespace)  # noqa: S102  # fixed source
    ping = namespace["Ping"]
    assert value_matches(ping, 1) is False
    assert type_matches(ping, ping) is True
    assert type_matches(int, ping) is False


def test_plain_class_is_only_a_class_isinstance_accepts() -> None:
    """Only a class `isinstance` can check, through aliases and `Annotated`."""

    class Plain(Protocol):
        def run(self) -> None: ...

    assert plain_class(int) is int
    assert plain_class(Price) is float
    assert plain_class(Annotated[int, "m"]) is int
    assert plain_class(list) is list
    assert plain_class(list[int]) is None
    assert plain_class(int | None) is None
    assert plain_class(Any) is None
    assert plain_class(Plain) is None

    class Meta(type):
        @override
        def __instancecheck__(cls, value: object) -> bool:
            # Refuses every value but `None`, as some metaclasses refuse some values.
            if value is None:
                return False
            raise TypeError(value)

    class Odd(metaclass=Meta):
        pass

    assert plain_class(Odd) is None
    assert plain_class(collections.abc.Sized) is collections.abc.Sized


type Variadic[*Ts] = tuple[*Ts]


def test_a_variadic_generic_alias_is_compared_as_written() -> None:
    """An alias over `*Ts` is not read through, so it answers as before (Q44)."""
    assert type_matches(Variadic[int], Variadic[int]) is True
    assert type_matches(tuple[int], Variadic[int]) is False


def test_an_empty_collection_fits_any_element_type() -> None:
    """With no first element there is nothing to check."""
    assert value_matches(collections.abc.Sequence[int], ()) is True
    assert value_matches(collections.abc.Iterable[str], set()) is True
