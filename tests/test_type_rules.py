"""Type rules: `None` only where admitted, fixed tuples, and the public matchers."""

import collections.abc
import re
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
