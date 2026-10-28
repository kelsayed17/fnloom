"""Edge checks at build; input and override checks on each run; the opt-out."""

import re

import pytest

from fnloom import Builder, _types, build
from helpers import make_module

CHAIN = """
def b(a: int) -> int:
    return a + 1

def c(b: int) -> int:
    return b * 2
"""

MISMATCHED = """
def text() -> str:
    return "x"

def number() -> float:
    return 1.5

def uses_text(text: int) -> int:
    return text

def uses_number(number: str, text: int) -> str:
    return number
"""

TWO_INPUTS = """
def total(price: float, quantity: int) -> float:
    return price * quantity

def other(unread: int) -> int:
    return unread
"""


def test_mismatched_edge_fails_build_naming_node_param_and_upstream() -> None:
    """A `str` node feeding an `int` parameter fails the build, naming both ends."""
    module = make_module(
        "checks_edge_one",
        """
def text() -> str:
    return "x"

def uses_text(text: int) -> int:
    return text
""",
    )
    with pytest.raises(
        ValueError, match=r"^uses_text\.text requires int but text returns str$"
    ):
        build(module)


def test_all_edge_mismatches_are_listed_together_sorted() -> None:
    """Every mismatched edge is in the one error, a line each, sorted."""
    module = make_module("checks_edge_all", MISMATCHED)
    with pytest.raises(ValueError, match="requires") as raised:
        build(module)
    assert str(raised.value).splitlines() == [
        "uses_number.number requires str but number returns float",
        "uses_number.text requires int but text returns str",
        "uses_text.text requires int but text returns str",
    ]


def test_edge_check_runs_even_with_input_checks_off() -> None:
    """The opt-out leaves edge checks on."""
    module = make_module("checks_edge_off", MISMATCHED)
    with pytest.raises(ValueError, match=r"uses_text\.text requires int"):
        Builder().with_modules(module).without_input_type_checks().build()


def test_wrong_input_type_is_a_value_error_listing_every_bad_input() -> None:
    """Every input of the wrong type is in the one error, sorted."""
    driver = build(make_module("checks_inputs", TWO_INPUTS))
    with pytest.raises(ValueError, match="expected") as raised:
        driver.execute(["total"], inputs={"price": "cheap", "quantity": 2.5})
    assert str(raised.value).splitlines() == [
        "price: expected float, got str",
        "quantity: expected int, got float",
    ]


def test_none_input_passes_only_where_admitted() -> None:
    """`None` fits `int | None` but not `int`; Hamilton passes it for both (Q79)."""
    module = make_module(
        "checks_none",
        """
def f(a: int | None) -> bool:
    return a is None

def g(b: int) -> bool:
    return b is None
""",
    )
    driver = build(module)
    assert driver.execute(["f"], inputs={"a": None}) == {"f": True}
    with pytest.raises(ValueError, match=r"^b: expected int, got NoneType$"):
        driver.execute(["g"], inputs={"b": None})


def test_unread_input_is_not_checked() -> None:
    """An input no step of the plan reads is not checked."""
    driver = build(make_module("checks_unread", TWO_INPUTS))
    result = driver.execute(
        ["total"], inputs={"price": 2.0, "quantity": 3, "unread": "not an int"}
    )
    assert result == {"total": 6.0}


def test_override_of_the_wrong_type_is_a_value_error() -> None:
    """An override is checked against its node's type (Q22)."""
    driver = build(make_module("checks_override", CHAIN))
    with pytest.raises(ValueError, match=re.escape("b: expected int, got str")):
        driver.execute(["c"], overrides={"b": "x"})


def test_without_input_type_checks_turns_off_input_and_override_checks() -> None:
    """With the opt-out, wrong values reach the nodes."""
    module = make_module("checks_off", CHAIN)
    driver = Builder().with_modules(module).without_input_type_checks().build()
    assert driver.execute(["b"], inputs={"a": 1.5}) == {"b": 2.5}
    assert driver.execute(["c"], overrides={"b": "x"}) == {"c": "xx"}


def test_without_input_type_checks_returns_a_new_builder() -> None:
    """The opt-out leaves the original builder checking."""
    module = make_module("checks_new_builder", CHAIN)
    builder = Builder().with_modules(module)
    unchecked = builder.without_input_type_checks()
    assert unchecked is not builder
    with pytest.raises(ValueError, match="a: expected int, got str"):
        builder.build().execute(["b"], inputs={"a": "x"})
    with pytest.raises(TypeError):
        unchecked.build().execute(["b"], inputs={"a": "x"})


GENERIC_CHAIN = """
def b(a: list[int]) -> list[int]:
    return a

def c(b: list[int]) -> int:
    return len(b)
"""


def test_checks_are_compiled_once_per_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each type's check is prepared with the plan; a run reads no annotation.

    A plain class is one `isinstance`, and a generic takes a value of exactly its
    class without the general walk, which only a value of another class needs.
    """
    prepared: list[object] = []
    walked: list[tuple[object, object]] = []
    real_check_of, real_fits = _types.check_of, _types._fits  # noqa: SLF001  # spying on the walk

    def spy_check_of(declared: object) -> tuple[_types.Classes | None, _types.Matcher]:
        prepared.append(declared)
        return real_check_of(declared)

    def spy_fits(
        declared: object, value: object, asked: tuple[tuple[object, str], ...] = ()
    ) -> bool:
        walked.append((declared, value))
        return real_fits(declared, value, asked)

    monkeypatch.setattr(_types, "check_of", spy_check_of)
    monkeypatch.setattr(_types, "_fits", spy_fits)
    driver = build(make_module("checks_compiled", CHAIN))
    plan = driver.prepare(["c"], inputs={"a": 0})
    assert prepared == [int]
    for value in (1, 2, 3):
        assert plan.run(inputs={"a": value}) == {"c": (value + 1) * 2}
    with pytest.raises(ValueError, match=r"^a: expected int, got str$"):
        plan.run(inputs={"a": "x"})
    overridden = driver.prepare(["c"], overrides={"b": 0})
    with pytest.raises(ValueError, match=r"^b: expected int, got str$"):
        overridden.run(overrides={"b": "x"})
    assert prepared == [int, int]
    assert walked == []
    generic = build(make_module("checks_compiled_generic", GENERIC_CHAIN))
    plan = generic.prepare(["c"], inputs={"a": []})
    assert prepared == [int, int, list[int]]
    for value in ([1], [2]):
        assert plan.run(inputs={"a": value}) == {"c": 1}
    assert walked == []
    assert plan.run(inputs={"a": (1,)}) == {"c": 1}
    assert walked == [(list[int], (1,)), (int, 1)]
    assert prepared == [int, int, list[int]]
    unchecked = (
        Builder()
        .with_modules(make_module("checks_compiled_off", GENERIC_CHAIN))
        .without_input_type_checks()
        .build()
    )
    prepared.clear()
    walked.clear()
    unchecked.execute(["c"], inputs={"a": ("x",)})
    assert (prepared, walked) == ([], [])


def test_bad_input_and_bad_override_fail_together_in_one_sorted_message() -> None:
    """An input and an override that both do not fit are listed in one error, sorted."""
    module = make_module(
        "checks_input_and_override",
        """
def b(z: int) -> int:
    return z

def d(c: int) -> int:
    return c

def e(b: int, d: int) -> int:
    return b + d
""",
    )
    driver = build(module)
    with pytest.raises(ValueError, match="expected") as raised:
        driver.execute(["e"], inputs={"z": "x"}, overrides={"d": "y"})
    # The override `d` sorts before the input `z`, though inputs are checked first.
    assert str(raised.value) == ("d: expected int, got str\nz: expected int, got str")


def test_unreached_override_is_still_checked() -> None:
    """An override of a node the plan never reaches is type checked too."""
    driver = build(make_module("checks_unreached_override", CHAIN))
    with pytest.raises(ValueError, match=r"^c: expected int, got str$"):
        driver.execute(["b"], inputs={"a": 1}, overrides={"c": "x"})


GENERIC = """
def total(prices: list[int], cap: int | None) -> int:
    return sum(prices)

def capped(total: int, cap: int | None) -> int:
    return total if cap is None else min(total, cap)
"""


def test_inputs_and_overrides_of_generic_types_are_checked() -> None:
    """A value failing a generic or a union is refused, not only a plain class."""
    driver = build(make_module("checks_generic", GENERIC))
    with pytest.raises(ValueError, match="expected") as raised:
        driver.execute(["total"], inputs={"prices": ("a",), "cap": "x"})
    assert str(raised.value).splitlines() == [
        "cap: expected int | None, got str",
        "prices: expected list[int], got tuple",
    ]
    with pytest.raises(
        ValueError, match=re.escape("cap: expected int | None, got str")
    ):
        driver.execute(["capped"], overrides={"total": 5, "cap": "x"})


def test_a_recursive_alias_builds_and_checks_its_inputs() -> None:
    """`type Tree = list[Tree]` builds, and a string input is refused cleanly."""
    module = make_module(
        "checks_tree",
        """
type Tree = list[Tree]

def size(tree: Tree) -> int:
    return len(tree)
""",
    )
    driver = build(module)
    assert driver.execute(["size"], inputs={"tree": [[], [[]]]}) == {"size": 2}
    with pytest.raises(ValueError, match=re.escape("tree: expected Tree, got str")):
        driver.execute(["size"], inputs={"tree": "leaf"})
