"""The type-check matrix of design §10, and Hamilton's answers over it.

Hamilton's driver accepts `None` for every input before it calls
`check_input_type`, so its answer for a value is that or-gate. In
`types_match(param_type, required_node_type)` the first argument is the consuming
parameter's annotation and the second the type of the node feeding it: Hamilton's
`add_dependency` calls it as `types_match(param_type, required_node.type)`, where
`required_node` is the upstream node, and `types_match` passes the second
argument on as the candidate subclass. So the producer goes second.
"""

import collections.abc
from typing import (
    Annotated,
    Any,
    Literal,
    NewType,
    Optional,
    Protocol,
    TypeVar,
    runtime_checkable,
)

from hamilton import htypes

from fnloom._types import type_matches, value_matches


class Base:
    """A user class."""


class Child(Base):
    """A subclass of `Base`."""


class Plain(Protocol):
    """A protocol that cannot be checked at runtime."""

    def size(self) -> int:
        """The size."""
        ...


@runtime_checkable
class Checkable(Protocol):
    """A runtime-checkable protocol."""

    def size(self) -> int:
        """The size."""
        ...


T = TypeVar("T")
Count = NewType("Count", int)

TYPES: dict[str, object] = {
    "int": int,
    "float": float,
    "bool": bool,
    "str": str,
    "object": object,
    "Any": Any,
    "Base": Base,
    "Child": Child,
    "list": list,
    "list[int]": list[int],
    "dict[str, int]": dict[str, int],
    "tuple[int, ...]": tuple[int, ...],
    "Sequence": collections.abc.Sequence,
    "Sequence[int]": collections.abc.Sequence[int],
    "int | None": int | None,
    # Equal to `int | None` on 3.14, but a distinct object, so spelled as written.
    "Optional[int]": Optional[int],  # noqa: UP045  # the spelling is the point
    "int | str": int | str,
    "Literal[1, 2]": Literal[1, 2],
    "Annotated[int, 'm']": Annotated[int, "m"],
    "Protocol": Plain,
    "runtime Protocol": Checkable,
    "TypeVar": T,
    "NewType(int)": Count,
    "type[int] | None": type[int] | None,
}

VALUES: dict[str, object] = {
    "1": 1,
    "1.0": 1.0,
    "True": True,
    "'s'": "s",
    "None": None,
    "[1]": [1],
    "[1.0]": [1.0],
    "{'a': 1}": {"a": 1},
    "(1, 2)": (1, 2),
    "object()": object(),
    "Base()": Base(),
    "Child()": Child(),
    # Classes and union objects as values; ids that are no type id, as keys must not be.
    "the class int": int,
    "the union int | str": int | str,
}

type Answer = bool | str
"""A check's result, or the class name of the exception it raised."""


def _answer(check: collections.abc.Callable[[], bool]) -> Answer:
    try:
        return bool(check())
    except Exception as error:  # noqa: BLE001  # the exception class is the thing being compared
        return type(error).__name__


def hamilton_value(declared: str, value: str) -> Answer:
    """Hamilton's answer for `VALUES[value]` supplied as a `TYPES[declared]` input."""
    d, v = TYPES[declared], VALUES[value]
    return _answer(lambda: v is None or htypes.check_input_type(d, v))  # ty: ignore[invalid-argument-type]  # Hamilton's hint is narrower than what it accepts


def fnloom_value(declared: str, value: str) -> Answer:
    """The same pair's answer from fnloom."""
    return _answer(lambda: value_matches(TYPES[declared], VALUES[value]))


def hamilton_type(produced: str, required: str) -> Answer:
    """Hamilton's answer for a node of `TYPES[produced]` feeding a `TYPES[required]`."""
    p, r = TYPES[produced], TYPES[required]
    return _answer(lambda: htypes.types_match(r, p))  # ty: ignore[invalid-argument-type]  # Hamilton's hint is narrower than what it accepts


def fnloom_type(produced: str, required: str) -> Answer:
    """The same pair's answer from fnloom."""
    return _answer(lambda: type_matches(TYPES[produced], TYPES[required]))
