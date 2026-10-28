"""Type rules: `None` only where admitted, fixed tuples, and the public matchers."""

import asyncio
import collections.abc
import copy
import dataclasses
import pickle
import random
import re
import time
import types
import typing
from pathlib import Path
from typing import (
    Annotated,
    Any,
    Literal,
    NewType,
    Optional,
    Protocol,
    TypedDict,
    TypeVar,
    runtime_checkable,
)

import pytest

import fnloom
from fnloom import _types
from helpers import make_module

CHANGELOG = Path(__file__).parents[1] / "CHANGELOG.md"


@runtime_checkable
class _Sized(Protocol):
    def size(self) -> int: ...


class _Point(TypedDict):
    x: int


_Count = NewType("_Count", int)
_Free = TypeVar("_Free")
_BoundOptional = TypeVar("_BoundOptional", bound=int | None)
_BoundInt = TypeVar("_BoundInt", bound=int)
_ConstrainedOptional = TypeVar("_ConstrainedOptional", int, None)
_ConstrainedInt = TypeVar("_ConstrainedInt", int, str)

type _MaybeInt = int | None
type _Int = int

ADMITS_NONE: dict[str, object] = {
    "Optional[int]": Optional[int],  # noqa: UP045  # the spelling is the point
    "int | None": int | None,
    "Annotated[int | None, 'm']": Annotated[int | None, "m"],
    "alias of int | None": _MaybeInt,
    "object": object,
    "Any": Any,
    "None": None,
    "NoneType": types.NoneType,
    "Literal[None]": Literal[None],  # noqa: PYI061  # the spelling is the point
    "TypeVar": _Free,
    "TypeVar bound to int | None": _BoundOptional,
    "TypeVar constrained to int, None": _ConstrainedOptional,
}
REFUSES_NONE: dict[str, object] = {
    "int": int,
    "str": str,
    "alias of int": _Int,
    "Annotated[int, 'm']": Annotated[int, "m"],
    "list[int]": list[int],
    "dict[str, int]": dict[str, int],
    "tuple[int, str]": tuple[int, str],
    "tuple[int, ...]": tuple[int, ...],
    "Sequence[int]": collections.abc.Sequence[int],
    "Mapping[str, int]": collections.abc.Mapping[str, int],
    "Literal[1]": Literal[1],
    "NewType(int)": _Count,
    "int | str": int | str,
    "runtime protocol": _Sized,
    "TypedDict": _Point,
    "TypeVar bound to int": _BoundInt,
    "TypeVar constrained to int, str": _ConstrainedInt,
}


@pytest.mark.parametrize(
    ("declared", "admits"),
    [*((d, True) for d in ADMITS_NONE), *((d, False) for d in REFUSES_NONE)],
)
def test_none_passes_only_types_that_admit_it(declared: str, admits: bool) -> None:  # noqa: FBT001  # a parametrized expectation
    """`None` fits `Optional`, `object`, `Any`, `None` and a free `TypeVar` (Q79).

    A bounded or constrained `TypeVar` admits `None` only when its bound, or one of
    its constraints, does. Every other type refuses it.
    """
    annotation = {**ADMITS_NONE, **REFUSES_NONE}[declared]
    assert fnloom.value_matches(annotation, None) is admits


def test_a_typevar_still_takes_every_other_value() -> None:
    """A bound or constraint is read for `None` only: others pass, as in Hamilton."""
    for typevar in (_BoundInt, _ConstrainedInt, _Free):
        assert fnloom.value_matches(typevar, "s") is True
        assert fnloom.value_matches(typevar, 1.5) is True


def test_none_for_a_non_optional_input_is_a_mismatch() -> None:
    """`None` for `x: int` is listed with the other mismatches, before anything runs."""
    module = make_module(
        "rules_none_input",
        """
from typing import Any

def plain(x: int) -> object:
    return x

def loose(y: object, z: Any) -> object:
    return y, z

def generic(g: list[int]) -> object:
    return g
""",
    )
    driver = fnloom.build(module)
    with pytest.raises(
        ValueError,
        match=re.escape(
            "g: expected list[int], got NoneType\nx: expected int, got NoneType"
        ),
    ):
        driver.execute(["plain", "generic"], inputs={"x": None, "g": None})
    assert driver.execute(["loose"], inputs={"y": None, "z": None}) == {
        "loose": (None, None)
    }
    lenient = fnloom.Builder().with_modules(module).without_input_type_checks().build()
    assert lenient.execute(["plain"], inputs={"x": None}) == {"plain": None}


def test_none_override_of_a_non_optional_node_is_a_mismatch() -> None:
    """An override is checked by the same rule: `None` for an `int` node is refused."""
    module = make_module(
        "rules_none_override",
        """
from typing import Optional

def count() -> int:
    return 1

def maybe() -> Optional[int]:
    return 1

def both(count: int, maybe: Optional[int]) -> object:
    return count, maybe
""",
    )
    driver = fnloom.build(module)
    with pytest.raises(ValueError, match=r"^count: expected int, got NoneType$"):
        driver.execute(["both"], overrides={"count": None})
    assert driver.execute(["both"], overrides={"maybe": None}) == {"both": (1, None)}


def test_none_for_an_extracted_field_follows_the_same_rule() -> None:
    """Field checks use the input check's matcher (Q61), so the `None` rule applies."""
    module = make_module(
        "rules_none_field",
        """
from typing import Optional
from fnloom import extract_fields

@extract_fields({"a": int, "b": Optional[int]})
def parts(given: dict) -> dict:
    return given
""",
    )
    driver = fnloom.build(module)
    assert driver.execute(["b"], inputs={"given": {"a": 1, "b": None}}) == {"b": None}
    with pytest.raises(ValueError, match="returned field 'a' as NoneType"):
        driver.execute(["a"], inputs={"given": {"a": None, "b": 1}})


def test_optional_default_still_receives_none() -> None:
    """`None` for an `Optional[int]` input with a default reaches the node (RF 3)."""
    module = make_module(
        "rules_optional_default",
        """
from typing import Optional

def scaled(factor: Optional[int] = 5) -> object:
    return factor

def piped(rate: int | None = 2) -> object:
    return rate
""",
    )
    driver = fnloom.build(module)
    inputs = {"factor": None, "rate": None}
    assert driver.execute(["scaled", "piped"], inputs=inputs) == {
        "scaled": None,
        "piped": None,
    }
    assert driver.prepare(["scaled"], inputs={"factor": None}).run(
        inputs={"factor": None}
    ) == {"scaled": None}
    assert driver.execute(["scaled", "piped"]) == {"scaled": 5, "piped": 2}


type _Nested = tuple[tuple[int, str], float]


def test_fixed_tuples_check_length_and_items() -> None:
    """A fixed tuple checks its length and each item with the same matcher (Q80)."""
    fixed = tuple[int, str]
    assert fnloom.value_matches(fixed, (1, "a")) is True
    assert fnloom.value_matches(fixed, ("a", 1)) is False
    assert fnloom.value_matches(fixed, (1,)) is False
    assert fnloom.value_matches(fixed, (1, "a", 2)) is False
    assert fnloom.value_matches(fixed, [1, "a"]) is False
    assert fnloom.value_matches(typing.Tuple[int, str], (1, "a")) is True  # noqa: UP006  # the typing spelling
    assert fnloom.value_matches(typing.Tuple[int, str], (1, 2)) is False  # noqa: UP006  # the typing spelling
    assert fnloom.value_matches(tuple[int, int | None], (1, None)) is True
    assert fnloom.value_matches(tuple[int, list[int]], (1, [2])) is True
    assert fnloom.value_matches(tuple[int, list[int]], (1, ("s",))) is False
    # Nested, through an alias and `Annotated`.
    assert fnloom.value_matches(_Nested, ((1, "a"), 1.5)) is True
    assert fnloom.value_matches(_Nested, ((1, 2), 1.5)) is False
    assert fnloom.value_matches(_Nested, ((1, "a", 3), 1.5)) is False
    assert fnloom.value_matches(_Nested, (1, 1.5)) is False
    nested = tuple[Annotated[tuple[int], "m"], int]
    assert fnloom.value_matches(nested, ((1,), 2)) is True
    assert fnloom.value_matches(nested, (("s",), 2)) is False
    # The empty tuple type takes only the empty tuple.
    assert fnloom.value_matches(tuple[()], ()) is True
    assert fnloom.value_matches(tuple[()], (1,)) is False
    assert fnloom.value_matches(tuple[()], []) is False
    assert fnloom.value_matches(typing.Tuple[()], ()) is True  # noqa: UP006  # the typing spelling
    assert fnloom.value_matches(typing.Tuple[()], (1,)) is False  # noqa: UP006  # the typing spelling
    # Unchanged: a homogeneous tuple by class, bare `tuple` and `Tuple` by kind.
    assert fnloom.value_matches(tuple[int, ...], ("s", "t")) is True
    assert fnloom.value_matches(tuple[int, ...], ["s"]) is False
    assert fnloom.value_matches(tuple, (1, "a", 2)) is True
    assert fnloom.value_matches(typing.Tuple, (1, "a", 2)) is True  # noqa: UP006  # the typing spelling
    # A variadic tuple has no fixed length, so it is checked by kind.
    assert fnloom.value_matches(tuple[int, *tuple[str, ...]], (1, "a", "b")) is True
    assert fnloom.value_matches(tuple[int, *tuple[str, ...]], [1]) is False


def test_nested_tuples_are_checked_without_recursion() -> None:
    """Tuples nested deeper than the recursion limit are checked without recursing."""
    depth = 5000
    annotation: object = int
    value: object = 1
    wrong: object = "s"
    for _ in range(depth):
        annotation = tuple[annotation, str]
        value = (value, "a")
        wrong = (wrong, "a")
    assert fnloom.value_matches(annotation, value) is True
    assert fnloom.value_matches(annotation, wrong) is False


type _Rec = tuple[int, _Rec | None]
type _RecSeq = tuple[int, collections.abc.Sequence[_RecSeq]]
type _Seq = collections.abc.Sequence[_Seq]
type _GenericSeq[T] = collections.abc.Sequence[_GenericSeq[T]]
type _SeqOrInt = collections.abc.Sequence[_SeqOrInt] | int
type _Self = _Self | int  # ty: ignore[cyclic-type-alias-definition]  # the cycle is the point
type _ListOrSelf = list[_ListOrSelf] | _ListOrSelf  # ty: ignore[cyclic-type-alias-definition]  # the cycle is the point


@dataclasses.dataclass
class _Meta:
    """Metadata that is not hashable: a dataclass with `eq` and no `frozen`."""

    n: int


def _chain(depth: int, leaf: object) -> object:
    value: object = (1, leaf)
    for _ in range(depth):
        value = (1, value)
    return value


def test_a_value_deeper_than_the_recursion_limit_is_walked() -> None:
    """A recursive alias's value 10,000 levels deep fits, and a bad leaf fails."""
    good, bad = _chain(10_000, None), _chain(10_000, "s")
    _, prepared = _types.check_of(_Rec)
    assert fnloom.value_matches(_Rec, good) is True
    assert prepared(good) is True
    assert fnloom.value_matches(_Rec, bad) is False
    assert prepared(bad) is False
    driver = _with_input(_Rec)
    assert driver.validate(["reads"], inputs={"x": good}) == []
    assert driver.execute(["reads"], inputs={"x": good}) == {"reads": good}
    with pytest.raises(ValueError, match=r"^x: expected "):
        driver.execute(["reads"], inputs={"x": bad})


def test_a_cyclic_value_ends() -> None:
    """A value that contains itself fits a recursive alias, without recursing."""
    items: list[object] = []
    looped = (1, items)
    items.append(looped)
    assert fnloom.value_matches(_RecSeq, looped) is True
    assert _types.check_of(_RecSeq)[1](looped) is True
    itself: list[object] = []
    itself.append(itself)
    assert fnloom.value_matches(_Seq, itself) is True
    assert fnloom.value_matches(_Seq, ("s",)) is False
    assert fnloom.value_matches(_Seq, "ab") is False
    # A generic alias reads as one object each time, so its cycle is seen too.
    assert fnloom.value_matches(_GenericSeq[int], itself) is True
    # A one-character string above U+00FF is a new object on each reading, so a
    # string is compared by value: these end, and a string still does not fit.
    assert fnloom.value_matches(_Seq, "中") is False
    assert fnloom.value_matches(_Seq, "中文") is False
    assert fnloom.value_matches(_SeqOrInt, "中文") is False
    assert fnloom.value_matches(_SeqOrInt, [1, "中"]) is True
    assert fnloom.value_matches(_SeqOrInt, ["中"]) is False


def test_a_cycle_through_types_alone_does_not_fit() -> None:
    """With no step into an item, a repeated question fails; a union tries the rest."""
    assert fnloom.value_matches(_Self, 1.5) is False
    assert fnloom.value_matches(_Self, 1) is True
    assert fnloom.value_matches(_ListOrSelf, 1.5) is False
    assert fnloom.value_matches(_ListOrSelf, []) is True


def test_a_cycle_no_key_can_see_runs_out_of_budget() -> None:
    """A type made afresh on each reading never repeats: the walk raises, not hangs."""
    itself: list[object] = []
    itself.append(itself)
    fresh = _GenericSeq[Annotated[int, _Meta(1)]]
    with pytest.raises(TypeError, match=r"^value too cyclic to check "):
        _types._walk(fresh, itself, budget=1_000)  # noqa: SLF001  # a small budget, for speed
    # The default budget is far beyond a legitimate value's depth.
    assert fnloom.value_matches(_Rec, _chain(50_000, None)) is True


def test_fixed_tuple_inputs_are_checked_by_the_driver() -> None:
    """A wrong-length or wrong-item tuple input is a mismatch; a right one runs."""
    module = make_module(
        "rules_tuple_input",
        """
def first(pair: tuple[int, str]) -> int:
    return pair[0]
""",
    )
    driver = fnloom.build(module)
    assert driver.execute(["first"], inputs={"pair": (1, "a")}) == {"first": 1}
    for wrong in (("a", 1), (1,), (1, "a", 2)):
        with pytest.raises(
            ValueError, match=r"^pair: expected tuple\[int, str\], got tuple$"
        ):
            driver.execute(["first"], inputs={"pair": wrong})


class _Base:
    pass


class _Child(_Base):
    pass


VALUE_CASES: list[tuple[object, object]] = [
    *((declared, None) for declared in (*ADMITS_NONE.values(), *REFUSES_NONE.values())),
    (int, 1),
    (int, "s"),
    (int, True),
    (float, 1),
    (_Base, _Child()),
    (_Child, _Base()),
    (list[int], [1]),
    (list[int], (1,)),
    (tuple[int, str], (1, "a")),
    (tuple[int, str], ("a", 1)),
    (tuple[()], ()),
    (tuple[()], (1,)),
    (int | str, 1.5),
    (Literal["a"], "a"),
]


def _case_id(case: tuple[object, object]) -> str:
    """A readable, stable test id: an instance of a test class by its class name."""
    left, right = case
    shown = f"{type(right).__name__}()" if isinstance(right, _Base) else repr(right)
    return f"{left!r}-{shown}"


PREPARED_CASES: list[tuple[object, object]] = [
    *VALUE_CASES,
    (tuple[int, str], (1, "a", 2)),
    (tuple[int, str], [1, "a"]),
    (tuple[int, list[int]], (1, [2])),
    (tuple[int, list[int]], (1, ("s",))),
    (tuple[tuple[int, str], float], ((1, "a"), 1.5)),
    (tuple[tuple[int, str], float], ((1, 2), 1.5)),
    (tuple[int, ...], ("s",)),
    (tuple[int, *tuple[str, ...]], (1, "a")),
    (list[int], ["s"]),
    (list[int] | None, None),
    (list[int] | None, ("s",)),
    (dict[str, int], {"a": 1}),
    (collections.abc.Sequence[int], ("s",)),
    (int | None, 1),
    (int | None, "s"),
    # A union object as a value: no union takes it as one of its own class.
    (type[int] | None, int | str),
    (type[int] | None, int),
    (type[int], int | str),
    (list[int] | None, int | str),
    (int | list[int], int | str),
]


@pytest.mark.parametrize(
    ("declared", "value"), PREPARED_CASES, ids=[_case_id(c) for c in PREPARED_CASES]
)
def test_a_prepared_check_answers_as_value_matches(
    declared: object, value: object
) -> None:
    """The check a plan prepares once per type gives `value_matches`'s answer."""
    assert _types.check_of(declared)[1](value) is fnloom.value_matches(declared, value)


def test_checks_are_made_once_for_each_distinct_type() -> None:
    """Equal types share one check; an unhashable annotation still gets its own."""
    unhashable = Annotated[int, []]
    checks = _types.checks_of([int | None, list[int], unhashable, int | None])
    assert checks[0] is checks[3]
    assert checks[0][0] == (int, types.NoneType)
    assert checks[1][0] is None
    assert checks[1][1]([1]) is True
    assert checks[1][1]((1,)) is True
    assert checks[1][1](("s",)) is False
    assert checks[2][0] is int
    assert checks[2][1](None) is False


def _with_input(declared: object) -> fnloom.Driver:
    """A driver whose one node reads input `x`, annotated `declared`."""

    def reads(x):  # noqa: ANN001, ANN202  # annotated below, at run time
        return x

    reads.__annotations__ = {"x": declared, "return": object}
    return fnloom.Builder().with_functions(reads).build()


@pytest.mark.parametrize(
    ("declared", "value"), VALUE_CASES, ids=[_case_id(c) for c in VALUE_CASES]
)
def test_public_matchers_equal_the_driver_checks(
    declared: object, value: object
) -> None:
    """`fnloom.value_matches` answers as the driver's input check, `None` included."""
    driver = _with_input(declared)
    expected = fnloom.value_matches(declared, value)
    assert (driver.validate(["reads"], inputs={"x": value}) == []) is expected
    if expected:
        assert driver.execute(["reads"], inputs={"x": value}) == {"reads": value}
    else:
        with pytest.raises(ValueError, match=r"^x: expected "):
            driver.execute(["reads"], inputs={"x": value})


EDGE_CASES: list[tuple[object, object]] = [
    (int, int),
    (int, float),
    (bool, int),
    (_Child, _Base),
    (_Base, _Child),
    (int, int | None),
    (int | None, int),
    (Any, int),
    (int, Any),
    (tuple[int, str], tuple[int, str]),
    (tuple[int, str], tuple),
    (tuple[int, str], tuple[int, ...]),
    (list[int], collections.abc.Sequence[int]),
]


@pytest.mark.parametrize(
    ("produced", "expected"), EDGE_CASES, ids=[_case_id(c) for c in EDGE_CASES]
)
def test_public_type_matcher_equals_the_build_check(
    produced: object, expected: object
) -> None:
    """`fnloom.type_matches` answers as the build's edge check."""

    def made():  # noqa: ANN202  # annotated below, at run time
        return None

    def reads(made):  # noqa: ANN001, ANN202  # annotated below, at run time
        return made

    made.__annotations__ = {"return": produced}
    reads.__annotations__ = {"made": expected, "return": object}
    builder = fnloom.Builder().with_functions(made, reads)
    if fnloom.type_matches(produced, expected):
        builder.build()
    else:
        with pytest.raises(ValueError, match=r"^reads\.made requires "):
            builder.build()


def test_public_matchers_are_the_internal_ones() -> None:
    """The exported names are the checks the driver itself calls."""
    assert fnloom.value_matches is _types.value_matches
    assert fnloom.type_matches is _types.type_matches
    assert "value_matches" in fnloom.__all__
    assert "type_matches" in fnloom.__all__
    for matcher in (fnloom.value_matches, fnloom.type_matches):
        assert "None" in (matcher.__doc__ or "")


def test_changelog_calls_out_the_none_change() -> None:
    """The breaking `None` rule is under **Changed** in the unreleased section."""
    text = CHANGELOG.read_text(encoding="utf-8")
    unreleased = text.split("## [Unreleased]", 1)[1].split("\n## [", 1)[0]
    changed = unreleased.split("### Changed", 1)[1].split("\n### ", 1)[0]
    bullets = re.split(r"^- ", changed, flags=re.MULTILINE)
    assert any(
        b.startswith("**Breaking:**") and "`None`" in b and "Q79" in b for b in bullets
    )


# --- Unresolved annotations (Q81) ---------------------------------------------

_TYPE_CHECKING_ONLY = """
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from decimal import Decimal

def rate() -> "Decimal":
    raise AssertionError("never run")

def price(rate: "Decimal") -> "Decimal":
    return rate

def converted(given: "Decimal") -> object:
    return given

def label(amount: int) -> str:
    return str(amount)
"""


def test_a_type_checking_import_builds() -> None:
    """A name imported only for type checkers builds, as an opaque type (Q81)."""
    module = make_module("rules_unresolved_builds", _TYPE_CHECKING_ONLY)
    driver = fnloom.build(module)
    unresolved = _types.Unresolved("Decimal")
    assert driver.nodes["rate"].type == unresolved
    assert driver.nodes["price"].params[0].annotation == unresolved
    assert repr(unresolved) == "unresolved 'Decimal'"
    assert repr(_types.Unresolved("decimal.Decimal")) == "unresolved 'decimal.Decimal'"
    # A node that never asks for the unresolved type never checks it.
    assert driver.execute(["label"], inputs={"amount": 2}) == {"label": "2"}


def test_unresolved_text_is_kept_as_written() -> None:
    """Quoted, `from __future__`, nested and alias forms each keep their own text."""
    module = make_module(
        "rules_unresolved_forms",
        """
from __future__ import annotations

import decimal

type Money = Missing

def quoted(a: "decimal.Nope", b: list[Absent], c: Money, d: int) -> int:
    return d
""",
    )
    params = {p.name: p.annotation for p in fnloom.build(module).nodes["quoted"].params}
    assert params == {
        "a": _types.Unresolved("decimal.Nope"),
        "b": _types.Unresolved("list[Absent]"),
        "c": _types.Unresolved("Money"),
        "d": int,
    }


def test_same_unresolved_name_matches_itself() -> None:
    """An edge between two annotations of the same unresolved text, or `Any`, builds."""
    unresolved = _types.Unresolved("decimal.Decimal")
    assert fnloom.type_matches(unresolved, _types.Unresolved("decimal.Decimal"))
    assert fnloom.type_matches(unresolved, Any)
    assert fnloom.type_matches(Any, unresolved)
    for other in (_types.Unresolved("Decimal"), object, int, int | None, list[int]):
        assert not fnloom.type_matches(unresolved, other), other
        assert not fnloom.type_matches(other, unresolved), other
    module = make_module("rules_unresolved_same", _TYPE_CHECKING_ONLY)
    driver = fnloom.build(module)
    assert driver.what_is_the_path_between("rate", "price") == ["rate", "price"]


def test_an_unresolved_edge_names_the_annotation() -> None:
    """Unresolved feeding or fed by another type fails the build, naming the text."""
    module = make_module(
        "rules_unresolved_edge",
        """
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from decimal import Decimal

def amount() -> int:
    return 1

def rate() -> "Decimal":
    raise AssertionError("never run")

def price(amount: "Decimal") -> float:
    return 1.0

def total(rate: float) -> float:
    return rate
""",
    )
    hint = "; import what the unresolved annotation names at run time"
    message = (
        f"price.amount requires unresolved 'Decimal' but amount returns int{hint}\n"
        f"total.rate requires float but rate returns unresolved 'Decimal'{hint}"
    )
    with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
        fnloom.build(module)


def test_an_unresolved_input_check_says_to_import_it() -> None:
    """A value checked against an unresolved type is a `TypeError` naming it (Q81)."""
    module = make_module("rules_unresolved_input", _TYPE_CHECKING_ONLY)
    driver = fnloom.build(module)
    because = (
        ": cannot check a value against unresolved 'Decimal': it did not resolve "
        "when the graph was built; import what it names at run time, not only "
        "under TYPE_CHECKING"
    )
    for value in (1, None):
        with pytest.raises(TypeError, match=f"^{re.escape('given' + because)}$"):
            driver.execute(["converted"], inputs={"given": value})
    # An override is checked against the node's type, so it is refused alike, and
    # one message lists every culprit, sorted, a plain mismatch with them.
    message = f"amount: expected int, got str\ngiven{because}\nrate{because}"
    with pytest.raises(TypeError, match=f"^{re.escape(message)}$"):
        driver.execute(
            ["price", "converted", "label"],
            overrides={"rate": 1},
            inputs={"amount": "x", "given": 1},
        )
    lines = driver.validate(["converted"], inputs={"given": 1})
    assert lines == ["given" + because]
    assert type(lines[0]) is str
    # Without input checks nothing is checked, so the value goes through.
    lenient = fnloom.Builder().with_modules(module).without_input_type_checks().build()
    assert lenient.execute(["converted"], inputs={"given": 1}) == {"converted": 1}
    with pytest.raises(TypeError, match=r"^cannot check a value against unresolved "):
        fnloom.value_matches(_types.Unresolved("Decimal"), 1)


def test_an_unresolved_typevar_bound_is_read_at_build() -> None:
    """A lazy PEP 695 bound that does not resolve is unresolved, checked for `None`.

    It is read once, when building, so a run never raises its `NameError`.
    """
    module = make_module(
        "rules_unresolved_bound",
        """
def bounded[U: Undefined](x: U) -> object:
    return x

def constrained[V: (int, Gone)](y: V) -> object:
    return y
""",
    )
    driver = fnloom.build(module)
    assert driver.execute(["bounded"], inputs={"x": 1}) == {"bounded": 1}
    with pytest.raises(
        TypeError, match=r"^x: cannot check a value against unresolved 'Undefined'"
    ):
        driver.execute(["bounded"], inputs={"x": None})
    assert driver.execute(["constrained"], inputs={"y": "s"}) == {"constrained": "s"}
    with pytest.raises(
        TypeError, match=r"^y: cannot check a value against unresolved 'Gone'"
    ):
        driver.execute(["constrained"], inputs={"y": None})


def test_an_unresolved_typed_dict_field_checks_only_when_run() -> None:
    """An unresolved `TypedDict` field type builds; its check raises when run."""
    module = make_module(
        "rules_unresolved_field",
        """
from typing import TypedDict
from fnloom import extract_fields

class Broken(TypedDict):
    x: Missing
    y: int

@extract_fields()
def p() -> Broken:
    return {"x": 1, "y": 2}
""",
    )
    driver = fnloom.build(module)
    assert driver.nodes["x"].type == _types.Unresolved("Missing")
    assert driver.execute(["y"]) == {"y": 2}
    because = "cannot check a value against unresolved 'Missing'"
    with pytest.raises(TypeError, match=f"^'p' returned field 'x': {because}"):
        driver.execute(["x"])


def test_an_unresolved_annotation_caches_like_any_other() -> None:
    """Code versions never evaluate annotations: an unresolved one changes nothing."""
    module = make_module("rules_unresolved_cache", _TYPE_CHECKING_ONLY)
    driver = (
        fnloom.Builder().with_modules(module).with_cache(fnloom.MemoryCache()).build()
    )
    assert driver.execute(["label"], inputs={"amount": 3}) == {"label": "3"}
    assert driver.execute(["label"], inputs={"amount": 3}) == {"label": "3"}


# --- Deep checks (decision 12d) ------------------------------------------------

type _Nest = list[_Nest | int]


def _nested(depth: int, leaf: object) -> list[object]:
    value: list[object] = [1, leaf]
    for _ in range(depth):
        value = [2, value]
    return value


def _deep(module: types.ModuleType, *, widen: bool = False) -> fnloom.Driver:
    builder = fnloom.Builder().with_modules(module).with_deep_type_checks()
    return (builder.with_numeric_widening() if widen else builder).build()


def test_deep_checks_find_a_bad_item_in_nested_containers_iteratively() -> None:
    """Every item, 10,000 levels down, is read; a shallow check reads none of them."""
    good, bad = _nested(10_000, 3), _nested(10_000, 1.5)
    assert fnloom.value_matches(_Nest, bad) is True
    assert fnloom.value_matches(_Nest, good, deep=True) is True
    assert fnloom.value_matches(_Nest, bad, deep=True) is False
    module = make_module(
        "rules_deep_nested",
        """
from collections.abc import Sequence

def reads(
    x: list[dict[str, set[int]]], y: tuple[int, ...], z: Sequence[list[int]]
) -> object:
    return x
""",
    )
    _, prepared = _types.check_of(_Nest, _types.Rules(deep=True))
    assert prepared(good) is True
    assert prepared(bad) is False
    shallow, deep = fnloom.build(module), _deep(module)
    wrong = {"x": [{"a": {1}}, {"b": {2, "s"}}], "y": (1, "s"), "z": ([1], ["s"])}
    assert shallow.validate(["reads"], inputs=wrong) == []
    assert deep.validate(["reads"], inputs=wrong) == [
        "x: expected list[dict[str, set[int]]], got list",
        "y: expected tuple[int, ...], got tuple",
        "z: expected collections.abc.Sequence[list[int]], got tuple",
    ]
    with pytest.raises(
        ValueError, match=r"^x: expected list\[dict\[str, set\[int\]\]\], got list\n"
    ):
        deep.execute(["reads"], inputs=wrong)
    right = {"x": [{"a": {1}}, {}], "y": (1, 2), "z": ([1], [])}
    assert deep.execute(["reads"], inputs=right) == {"reads": right["x"]}
    # Keys are read too, and a list of the exact class no longer passes on its class.
    assert fnloom.value_matches(dict[str, int], {1: 1}, deep=True) is False
    assert _types.check_of(list[int], _types.Rules(deep=True))[1](["a"]) is False
    assert _types.check_of(list[int])[1](["a"]) is True


def test_deep_checks_never_read_an_iterator() -> None:
    """An iterator is checked by kind alone, so no item is lost (Q43)."""
    items = iter([1, "s"])
    assert fnloom.value_matches(collections.abc.Iterator[int], items, deep=True)
    assert list(items) == [1, "s"]
    generated = (n for n in [1, 2])
    assert fnloom.value_matches(collections.abc.Iterable[str], generated, deep=True)
    assert list(generated) == [1, 2]


type _Tree = list[_Tree] | int


def test_a_large_value_stays_under_the_open_group_cap() -> None:
    """Groups open about one value reach only the type's nesting, whatever the size."""
    size = 1_000
    values: list[tuple[object, object]] = [
        (list[int], list(range(size))),
        (list[tuple[int, str]], [(n, "s") for n in range(size)]),
        (list[list[list[int]]], [[[n]] for n in range(size)]),
        (list[list[int] | None], [[n] for n in range(size)]),
        (list[list[list[int]] | None], [[[n]] for n in range(size)]),
        (_Tree, [[n] for n in range(size)]),
        (list[tuple[int, list[int]]], [(n, [n]) for n in range(size)]),
        (list[dict[str, list[float]]], [{"a": [1.5]} for _ in range(size)]),
    ]
    expand = _types._expander(_types.Rules(deep=True))  # noqa: SLF001  # the walk's own expansion
    walk = _types._walk  # noqa: SLF001  # with a cap of its own
    for declared, value in values:
        # At most 3 groups open about one value, for 1,000 items of nested types.
        assert walk(declared, value, expand, 3) is True, declared
    assert fnloom.value_matches(list[int], [*range(size), "s"], deep=True) is False


type _Y = list[_T]
type _T = list[_Y]
type _L = list[_L]
type _Pairs = tuple[_Pairs, _Pairs] | None


def test_an_answer_resting_on_an_open_question_is_not_reused() -> None:
    """Only an answer that consulted nothing still open is kept (its lowlink rule).

    `(_T, w)` is first met while `(_Y, v)` is open, and fits by assuming it does;
    reused later, outside that cycle, it would wrongly let the second member pass.
    """
    v: list[object] = []
    w = [v]
    v.extend([w, "s"])
    declared = tuple[_Y, object] | tuple[object, _T]
    assert fnloom.value_matches(_T, w, deep=True) is False
    assert fnloom.value_matches(declared, (v, w), deep=True) is False
    cyclic: list[object] = []
    cyclic.append(cyclic)
    assert fnloom.value_matches(_L, cyclic, deep=True) is True


def _subscripted(generic: Any, *args: object) -> object:  # noqa: ANN401  # any generic
    """`generic[args]`, built at run time, typed only as an object."""
    return generic[args if len(args) > 1 else args[0]]


def _dag_list(depth: int, leaf: object) -> object:
    value: object = leaf
    for _ in range(depth):
        value = [value, value]
    return value


def _dag_tuple(depth: int, leaf: object) -> object:
    value: object = leaf
    for _ in range(depth):
        value = (value, value)
    return value


def test_a_value_sharing_its_parts_is_read_once_per_part() -> None:
    """A DAG of depth 60 has 2**60 paths; each shared part is answered once."""
    start = time.perf_counter()
    assert fnloom.value_matches(_L, _dag_list(60, []), deep=True) is True
    assert fnloom.value_matches(_Pairs, _dag_tuple(60, None)) is True
    assert fnloom.value_matches(_Pairs, _dag_tuple(60, None), deep=True) is True
    nested_type: object = int
    for _ in range(60):
        nested_type = _subscripted(tuple, nested_type, nested_type)
    assert fnloom.value_matches(nested_type, _dag_tuple(60, 1)) is True
    listed: object = int
    for _ in range(60):
        listed = _subscripted(list, listed)
    assert fnloom.value_matches(listed, _dag_list(60, 1), deep=True) is True
    assert time.perf_counter() - start < 1
    assert fnloom.value_matches(_L, _dag_list(1_000, []), deep=True) is True
    # A bad leaf deep inside is still found, through every shared part.
    assert fnloom.value_matches(listed, _dag_list(60, "s"), deep=True) is False
    assert fnloom.value_matches(_Pairs, _dag_tuple(60, 1)) is False
    bad = [_dag_list(30, []), _dag_list(29, [[1]])]
    assert fnloom.value_matches(_L, bad, deep=True) is False


def test_an_error_shows_a_long_type_cut_short() -> None:
    """The over-budget message shows at most 200 characters of the type."""
    itself: list[object] = []
    itself.append(itself)
    fresh = _GenericSeq[Annotated[int, _Meta(1)]]
    long_type: object = fresh
    for _ in range(30):
        long_type = _subscripted(
            typing.Union, _subscripted(tuple, long_type, long_type), long_type
        )
    with pytest.raises(TypeError) as raised:
        _types._walk(long_type, itself, budget=100)  # noqa: SLF001  # a small budget, for speed
    shown = str(raised.value).split(": more than", 1)[0]
    assert shown.endswith("...")
    assert len(shown) == len("value too cyclic to check against ") + 200


def test_cut_type_text_names_classes_and_writes_lists_piece_by_piece() -> None:
    """Classes show as `int`, and a parameter list of a shared type ends quickly."""
    shared: object = int
    for _ in range(40):
        shared = _subscripted(tuple, int, _subscripted(tuple, shared, shared))
    shown = _types._shown(shared)  # noqa: SLF001  # the error's text
    assert shown.startswith("tuple[int, tuple[tuple[int, ")
    assert "<class" not in shown
    assert shown.endswith("...")
    assert len(shown) == 200  # noqa: PLR2004  # the cap
    start = time.perf_counter()
    called = _types._shown(_subscripted(collections.abc.Callable, [shared], int))  # noqa: SLF001  # the error's text
    assert called.startswith("collections.abc.Callable[[tuple[int, ")
    assert time.perf_counter() - start < 1
    # A short type is its own `repr`.
    short = collections.abc.Callable[[int, str], int]
    assert _types._shown(short) == repr(short)  # noqa: SLF001  # the error's text


def _back_linked(depth: int) -> list[object]:
    """A root list holding a 2-way DAG whose leaves point back to the root."""
    root: list[object] = []
    part: object = root
    for _ in range(depth):
        part = [part, part]
    root.append(part)
    return root


type _Graph = dict[str, list[_Graph]]


def _graph(size: int, *, bad: bool = False) -> dict[str, object]:
    """`size` nodes, each linking to node 0 and two others, by a fixed seed."""
    rng = random.Random(size)  # noqa: S311  # a seeded shape, not cryptography
    nodes: list[dict[str, object]] = [{"next": []} for _ in range(size)]
    for node in nodes:
        links = typing.cast("list[object]", node["next"])
        links.extend([nodes[rng.randrange(size)], nodes[rng.randrange(size)], nodes[0]])
    if bad:
        typing.cast("list[object]", nodes[size // 2]["next"]).append("leaf")
    return nodes[0]


def test_shared_parts_on_a_cycle_are_read_once_per_part() -> None:
    """Inside a cycle, a fitting answer is reused provisionally, so no path repeats."""
    start = time.perf_counter()
    assert fnloom.value_matches(_L, _back_linked(20), deep=True) is True
    assert fnloom.value_matches(_Graph, _graph(80), deep=True) is True
    assert time.perf_counter() - start < 1
    assert fnloom.value_matches(_Graph, _graph(80, bad=True), deep=True) is False
    bad_root = _back_linked(20)
    bad_root.append("leaf")
    assert fnloom.value_matches(_L, bad_root, deep=True) is False


type _Either = list[_Either] | collections.abc.Sequence[_Either]


def _list_graph(size: int, *, bad: bool) -> list[object]:
    """`_graph`'s links as plain lists, with a string leaf in the middle node."""
    rng = random.Random(size)  # noqa: S311  # a seeded shape, not cryptography
    nodes: list[list[object]] = [[] for _ in range(size)]
    for node in nodes:
        node.extend([nodes[rng.randrange(size)], nodes[rng.randrange(size)], nodes[0]])
    if bad:
        nodes[size // 2].append("leaf")
    return nodes[0]


_FUZZED_TYPES = """
from collections.abc import Sequence
type A2 = int
type A3 = A3 | list[A4] | Sequence[A4] | tuple[None | A3, tuple[A2, A3]]
type A4 = A3
type A0 = list[A3 | None | A4]
"""
"""Aliases a fuzz found, in its order; `A3` and `A4` cycle through types alone."""


def _fuzzed_type() -> object:
    namespace: dict[str, object] = {}
    exec(_FUZZED_TYPES, namespace)  # noqa: S102  # fixed source
    return namespace["A0"]


def _fuzzed_value() -> list[object]:
    """A cyclic value a fuzz found: 11 nodes, lists and tuples linked back."""
    n: list[Any] = [[] for _ in range(11)]  # lists, then tuples over them
    n[8], n[9], n[10] = (1, n[1]), (1, "s"), ("s", "t")
    n[2] = (n[3], n[1])
    n[0].extend([n[1], n[2]])
    n[1].append(n[0])
    n[3].extend([n[0], n[4], n[10]])
    n[4].extend([n[5], n[7], 0])
    n[5].extend([n[5], n[6], n[3]])
    n[6].extend([n[7], n[7], n[7], n[9]])
    n[7].extend([n[5], n[4], n[0], n[8]])
    return typing.cast("list[object]", n[0])


def test_a_failing_check_on_a_cycle_reads_shared_parts_once() -> None:
    """A `False` that assumed only that open questions fit is kept, inside a cycle too.

    Both members of `_Either` read the same items, so each path into a shared
    part would read it again if a failing answer were dropped with the cycle.
    """
    start = time.perf_counter()
    assert fnloom.value_matches(_Either, _list_graph(80, bad=True), deep=True) is False
    assert fnloom.value_matches(_Either, _list_graph(80, bad=False), deep=True) is True
    fuzzed = _fuzzed_type()
    assert fnloom.value_matches(fuzzed, _fuzzed_value(), deep=True) is False
    assert fnloom.value_matches(fuzzed, _fuzzed_value()) is True
    assert time.perf_counter() - start < 0.5  # noqa: PLR2004  # well under a second


_FUZZ_TYPES: list[object] = [
    _L,
    _Pairs,
    _Tree,
    _Graph,
    _Y,
    _T,
    tuple[_Y, object] | tuple[object, _T],
    list[_L | int],
    list[_Tree] | str,
]


def _fuzz_value(rng: random.Random) -> object:
    """A small value of lists, dicts and tuples that may share parts and cycle."""
    leaves: list[object] = [1, "s", None, 1.5]
    built: list[object] = []
    for _ in range(rng.randint(1, 10)):
        kind = rng.random()
        if kind < 0.5:  # noqa: PLR2004  # half lists
            built.append([])
        elif kind < 0.75:  # noqa: PLR2004  # a quarter dicts
            built.append({})
        else:
            parts = [rng.choice([*leaves, *built]) for _ in range(rng.randint(1, 2))]
            built.append(tuple(parts))
    for node in built:
        if isinstance(node, list):
            node.extend(rng.choice([*leaves, *built]) for _ in range(rng.randint(0, 3)))
        elif isinstance(node, dict):
            node.update({f"k{k}": rng.choice([*leaves, *built]) for k in range(2)})
    return rng.choice(built)


def test_reused_answers_agree_with_a_walk_that_reuses_none() -> None:
    """On random cyclic and shared values, reuse never changes an answer."""
    rng = random.Random(10)  # noqa: S311  # a seeded fuzz, not cryptography
    walk = _types._walk  # noqa: SLF001  # with and without reused answers
    for _ in range(2_000):
        value, declared = _fuzz_value(rng), rng.choice(_FUZZ_TYPES)
        for rules in (_types.Rules(), _types.Rules(deep=True)):
            expand = _types._expander(rules)  # noqa: SLF001  # the walk's own expansion
            reused = walk(declared, value, expand)
            assert reused is walk(declared, value, expand, reuse=False), declared


type _FreshUnion[T] = _FreshUnion[Annotated[T, _Meta(1)]] | str  # ty: ignore[cyclic-type-alias-definition]  # the cycle is the point


def test_a_runaway_through_types_alone_still_trips_the_budget() -> None:
    """A union made afresh on each reading, about one value, counts every step."""
    with pytest.raises(TypeError, match=r"^value too cyclic to check "):
        _types._walk(_FreshUnion[int], 1.5, budget=1_000)  # noqa: SLF001  # a small budget, for speed
    expand = _types._expander(_types.Rules(deep=True))  # noqa: SLF001  # the walk's own expansion
    with pytest.raises(TypeError, match=r"^value too cyclic to check "):
        _types._walk(_FreshUnion[int], 1.5, expand, 1_000)  # noqa: SLF001  # a small budget, for speed


_ENDLESS_LIMIT = 1_000


class _Endless:
    """An iterable of its own class, endless: it fails the test past 1,000 items."""

    def __init__(self, item: object) -> None:
        self.item = item
        self.read = 0

    def __iter__(self) -> collections.abc.Iterator[object]:
        while True:
            self.read += 1
            if self.read > _ENDLESS_LIMIT:
                pytest.fail("an endless iterable was read past its first item")
            yield self.item


def test_deep_checks_read_only_standard_collections() -> None:
    """A user iterable keeps the shallow rules: endless ones end, read as far."""
    ints = _Endless(1)
    assert fnloom.value_matches(collections.abc.Iterable[int], ints, deep=True)
    shallow = _Endless(1)
    assert fnloom.value_matches(collections.abc.Iterable[int], shallow)
    assert ints.read == shallow.read == 1
    nested = _Endless(_Endless(1))
    declared = collections.abc.Iterable[collections.abc.Iterable[int]]
    assert fnloom.value_matches(declared, nested, deep=True)
    assert nested.read == 1
    assert fnloom.value_matches(declared, _Endless(_Endless("s")), deep=True) is False
    # A standard collection holding one is read in full, and the user one shallowly.
    assert fnloom.value_matches(list[collections.abc.Iterable[int]], [ints], deep=True)
    assert ints.read == 2  # noqa: PLR2004  # one more first item read


def test_deep_checks_apply_to_overrides_fields_and_validate() -> None:
    """Overrides, extracted fields and `validate` follow the builder's deep setting."""
    module = make_module(
        "rules_deep_everywhere",
        """
from fnloom import extract_fields

def counts() -> list[int]:
    return [1, 2]

@extract_fields({"items": list[int]})
def parts(given: dict) -> dict:
    return given

def total(counts: list[int], items: list[int]) -> int:
    return len(counts) + len(items)
""",
    )
    shallow, deep = fnloom.build(module), _deep(module)
    given = {"given": {"items": [1, "s"]}}
    assert shallow.execute(["items"], inputs=given) == {"items": [1, "s"]}
    with pytest.raises(ValueError, match=r"^'parts' returned field 'items' as list"):
        deep.execute(["items"], inputs=given)
    assert shallow.execute(["counts"], overrides={"counts": ["s"]}) == {"counts": ["s"]}
    with pytest.raises(ValueError, match=r"^counts: expected list\[int\], got list$"):
        deep.execute(["counts"], overrides={"counts": ["s"]})
    assert deep.validate(["counts"], overrides={"counts": ["s"]}) == [
        "counts: expected list[int], got list"
    ]


# --- Numeric widening (decision 12f) ------------------------------------------

_NUMBERS = """
def scaled(rate: float, count: int, rates: list[float]) -> float:
    return rate * count + sum(rates)
"""


def test_widening_is_off_by_default() -> None:
    """An `int` for `float` is refused, as Hamilton refuses it (H5.10f)."""
    driver = fnloom.build(make_module("rules_widen_off", _NUMBERS))
    with pytest.raises(ValueError, match=r"^rate: expected float, got int$"):
        driver.execute(["scaled"], inputs={"rate": 1, "count": 2, "rates": []})
    assert fnloom.value_matches(float, 1) is False
    # The default check stays one `isinstance`; widening needs the general check.
    assert _types.check_of(float)[0] is float
    assert _types.check_of(float, _types.Rules(widen=True))[0] is None


def test_widening_accepts_int_for_float_and_numpy_scalars() -> None:
    """With widening an `int` fits `float`, never a `bool`; at the top and inside."""
    module = make_module("rules_widen_on", _NUMBERS)
    driver = fnloom.Builder().with_modules(module).with_numeric_widening().build()
    inputs = {"rate": 1, "count": 2, "rates": [0.5]}
    assert driver.execute(["scaled"], inputs=inputs) == {"scaled": 2.5}
    with pytest.raises(ValueError, match=r"^rate: expected float, got bool$"):
        driver.execute(["scaled"], inputs={**inputs, "rate": True})
    with pytest.raises(ValueError, match=r"^count: expected int, got float$"):
        driver.execute(["scaled"], inputs={**inputs, "count": 2.0})
    assert driver.validate(["scaled"], inputs={**inputs, "rate": 1}) == []
    # Every item of a collection is read only with deep checks; the first item a
    # shallow check reads, and a fixed tuple's items, widen without them.
    assert driver.validate(["scaled"], inputs={**inputs, "rates": (1, "s")}) == []
    deep = _deep(module, widen=True)
    assert deep.execute(["scaled"], inputs={**inputs, "rates": [1, 0.5]}) == {
        "scaled": 3.5
    }
    with pytest.raises(ValueError, match=r"^rates: expected list\[float\], got list$"):
        deep.execute(["scaled"], inputs={**inputs, "rates": [True]})
    assert fnloom.value_matches(float | str, 1, widen=True) is True
    assert fnloom.value_matches(tuple[float, int], (1, 2), widen=True) is True
    assert fnloom.value_matches(float, value=True, widen=True) is False
    # Edge checks compare types, not values: widening never applies to them.
    edge = make_module(
        "rules_widen_edge",
        "def n() -> int:\n    return 1\n\ndef m(n: float) -> float:\n    return n\n",
    )
    with pytest.raises(ValueError, match=r"^m\.n requires float but n returns int$"):
        fnloom.Builder().with_modules(edge).with_numeric_widening().build()
    np = pytest.importorskip("numpy")
    numbers = {"rate": np.float32(1.5), "count": np.int64(2), "rates": []}
    assert driver.execute(["scaled"], inputs=numbers) == {"scaled": 3.0}
    with pytest.raises(ValueError, match=r"^count: expected int, got numpy\.float64$"):
        driver.execute(["scaled"], inputs={**numbers, "count": np.float64(2)})
    with pytest.raises(ValueError, match=r"^count: expected int, got numpy\.bool$"):
        driver.execute(["scaled"], inputs={**numbers, "count": np.bool_(True)})  # noqa: FBT003  # a numpy bool is the point
    strict = fnloom.build(module)
    with pytest.raises(ValueError, match=r"count: expected int, got numpy\.int64"):
        strict.execute(["scaled"], inputs=numbers)
    assert deep.execute(["scaled"], inputs={**inputs, "rates": [np.float32(0.5)]}) == {
        "scaled": 2.5
    }
    assert fnloom.value_matches(int, np.timedelta64(1, "s"), widen=True) is False
    # Widening composes: what fits `int` fits `float`.
    assert driver.execute(["scaled"], inputs={**numbers, "rate": np.int64(2)}) == {
        "scaled": 4
    }
    assert fnloom.value_matches(float, np.int32(1), widen=True) is True
    assert fnloom.value_matches(float, np.int32(1)) is False


def test_type_rule_settings_survive_pickling_copying_and_async() -> None:
    """Deep checks and widening are builder settings, carried wherever it goes."""
    module = make_module("rules_settings_carried", _NUMBERS)
    builder = (
        fnloom.Builder()
        .with_modules(module)
        .with_deep_type_checks()
        .with_numeric_widening()
    )
    inputs = {"rate": 1, "count": 2, "rates": [1]}
    for driver in (
        builder.build(),
        pickle.loads(pickle.dumps(builder.build())),  # noqa: S301  # our own pickle
        pickle.loads(pickle.dumps(builder)).build(),  # noqa: S301  # our own pickle
        copy.copy(builder.build()),
        copy.deepcopy(builder.build()),
        builder.with_config({}).without_override_type_checks().build(),
    ):
        assert driver.execute(["scaled"], inputs=inputs) == {"scaled": 3.0}
        with pytest.raises(ValueError, match=r"^rates: expected list\[float\]"):
            driver.execute(["scaled"], inputs={**inputs, "rates": [1, "s"]})
    pickled = pickle.loads(pickle.dumps(builder.build_async()))  # noqa: S301  # our own pickle
    for built in (builder.build_async(), pickled):
        assert asyncio.run(built.execute(["scaled"], inputs=inputs)) == {"scaled": 3.0}
        assert built.validate(["scaled"], inputs={**inputs, "rates": ["s"]}) == [
            "rates: expected list[float], got list"
        ]
    settings = pickle.loads(pickle.dumps(builder))._settings()  # noqa: S301, SLF001  # our own pickle, its settings
    assert settings["deep_type_checks"] is True
    assert settings["numeric_widening"] is True
    default = fnloom.Builder()._settings()  # noqa: SLF001  # the default settings
    assert default["deep_type_checks"] is False
    assert default["numeric_widening"] is False


_RULED_TYPES: list[object] = [
    float,
    int | None,
    list[int],
    list[float],
    dict[str, int],
    dict[str, list[float]],
    tuple[int, ...],
    tuple[float, str],
    set[float],
    collections.abc.Sequence[int],
    collections.abc.Mapping[str, float],
    list[list[int]],
    _Nest,
]
_RULED_VALUES: list[object] = [
    1,
    1.5,
    True,
    None,
    [1, 2],
    [1, "s"],
    [1.5, 2],
    [[1], [2]],
    [[1], ["s"]],
    {"a": 1},
    {"a": 1.5},
    {1: 1},
    {"a": [1, 2.5]},
    (1, 2),
    (1, "s"),
    (1.5, "s"),
    {1.5, 2},
    "abc",
    iter([1]),
]


@pytest.mark.parametrize(
    "rules",
    [
        _types.Rules(deep=True),
        _types.Rules(widen=True),
        _types.Rules(deep=True, widen=True),
    ],
    ids=["deep", "widen", "both"],
)
def test_a_prepared_ruled_check_answers_as_value_matches(rules: _types.Rules) -> None:
    """With deep checks or widening, a prepared check answers as `value_matches`."""
    for declared in _RULED_TYPES:
        classes, prepared = _types.check_of(declared, rules)
        for value in _RULED_VALUES:
            expected = fnloom.value_matches(
                declared, value, deep=rules.deep, widen=rules.widen
            )
            got = isinstance(value, classes) if classes is not None else prepared(value)
            assert got is expected, (declared, value)
