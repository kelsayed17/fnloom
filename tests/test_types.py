"""Tests for `value_matches` and `type_matches`, fnloom's own answers."""

import collections.abc
from typing import Annotated, Any, Literal, NewType, Protocol, runtime_checkable

import pytest

from fnloom._types import type_matches, value_matches


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
