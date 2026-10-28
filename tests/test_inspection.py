"""Inspection: `inputs`, `outputs`, `modules` and `what_is_the_path_between`."""

import copy
import subprocess
import sys
import textwrap
from typing import TYPE_CHECKING

import pytest

import fnloom
from fnloom import InputView
from helpers import make_module

if TYPE_CHECKING:
    from types import ModuleType

PRICING = """
def total(price: float, quantity: int) -> float:
    return price * quantity


def discounted(total: float, discount: float = 0.0) -> float:
    return total * (1 - discount)


def label(discounted: float, currency: str = "EUR") -> str:
    return f"{discounted:.2f} {currency}"


def audit(total: float, discount: float = 0.0, note: str = "") -> str:
    return note
"""

DIAMOND = """
def root(a: int) -> int:
    return a


def right(root: int) -> int:
    return root


def left(root: int) -> int:
    return root


def join(left: int, right: int) -> int:
    return left + right
"""


def _pricing() -> ModuleType:
    return make_module("inspect_pricing", PRICING)


def test_inputs_lists_every_input_with_type_requiredness_and_consumers() -> None:
    """Each input has its type, whether some consumer requires it, and its readers."""
    inputs = fnloom.build(_pricing()).inputs()
    assert list(inputs) == ["currency", "discount", "note", "price", "quantity"]
    assert inputs["price"] == InputView(
        "price", float, required=True, consumers=("total",)
    )
    assert inputs["quantity"] == InputView(
        "quantity", int, required=True, consumers=("total",)
    )
    # Optional for every consumer, so not required.
    assert inputs["currency"] == InputView(
        "currency", str, required=False, consumers=("label",)
    )
    assert inputs["discount"] == InputView(
        "discount", float, required=False, consumers=("audit", "discounted")
    )
    assert inputs["note"] == InputView(
        "note", str, required=False, consumers=("audit",)
    )


def test_an_input_one_consumer_requires_is_required() -> None:
    """Required means some consumer has no default for it."""
    module = make_module(
        "inspect_mixed",
        """
        def a(x: int = 1) -> int:
            return x


        def b(x: int) -> int:
            return x
        """,
    )
    view = fnloom.build(module).inputs()["x"]
    assert view.required is True
    assert view.consumers == ("a", "b")


def test_inputs_is_read_only_and_made_once() -> None:
    """The mapping can't be changed, and the same one comes back."""
    driver = fnloom.build(_pricing())
    inputs = driver.inputs()
    assert driver.inputs() is inputs
    with pytest.raises(TypeError):
        inputs["price"] = inputs["quantity"]  # ty: ignore[invalid-assignment]


def test_outputs_are_the_unconsumed_nodes() -> None:
    """Nodes nothing reads, sorted; inputs and read nodes are not outputs."""
    driver = fnloom.build(_pricing())
    assert driver.outputs() == ["audit", "label"]
    driver.outputs().append("junk")
    assert driver.outputs() == ["audit", "label"]


def test_modules_are_unique_in_build_order() -> None:
    """A module given twice is listed once, in the order first given."""
    first = make_module("inspect_first", "def a(x: int) -> int:\n    return x\n")
    second = make_module("inspect_second", "def b(a: int) -> int:\n    return a\n")
    built = fnloom.Builder().with_modules(second, first).with_modules(second).build()
    assert built.modules == (second, first)
    assert copy.copy(built).modules == (second, first)
    assert fnloom.build(first).modules == (first,)


def test_path_between_is_inclusive_and_topological() -> None:
    """Both endpoints, in dependency order with ties broken by name."""
    driver = fnloom.build(make_module("inspect_diamond", DIAMOND))
    assert driver.what_is_the_path_between("root", "join") == [
        "root",
        "left",
        "right",
        "join",
    ]
    # An input is an endpoint too, and a path may start at one.
    assert driver.what_is_the_path_between("a", "join") == [
        "a",
        "root",
        "left",
        "right",
        "join",
    ]
    assert driver.what_is_the_path_between("root", "left") == ["root", "left"]
    assert driver.what_is_the_path_between("a", "root") == ["a", "root"]


def test_path_between_is_independent_of_the_hash_seed() -> None:
    """The same query in subprocesses with different seeds gives equal results."""
    script = textwrap.dedent(
        f"""
        import json
        import sys
        import types

        import fnloom

        module = types.ModuleType("seeded")
        sys.modules["seeded"] = module
        exec({DIAMOND!r}, module.__dict__)
        print(json.dumps(fnloom.build(module).what_is_the_path_between("a", "join")))
        """
    )
    answers = {
        seed: subprocess.run(  # noqa: S603  # our own interpreter and script
            [sys.executable, "-c", script],
            env={"PYTHONHASHSEED": seed},
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        for seed in ("0", "1", "2", "3")
    }
    assert set(answers.values()) == {'["a", "root", "left", "right", "join"]'}, answers


def test_path_between_unknown_names_are_all_named() -> None:
    """Every unknown endpoint is listed, sorted, in one `ValueError`."""
    driver = fnloom.build(make_module("inspect_unknown", DIAMOND))
    with pytest.raises(ValueError, match=r"unknown nodes: 'nope', 'zip'$"):
        driver.what_is_the_path_between("zip", "nope")
    with pytest.raises(ValueError, match=r"unknown nodes: 'nope'$"):
        driver.what_is_the_path_between("root", "nope")
    with pytest.raises(TypeError, match="node names must be str, got int"):
        driver.what_is_the_path_between("root", 5)  # ty: ignore[invalid-argument-type]


def test_path_between_no_path_is_empty() -> None:
    """Reversed, unrelated and same-node queries."""
    driver = fnloom.build(make_module("inspect_none", DIAMOND))
    assert driver.what_is_the_path_between("join", "root") == []
    assert driver.what_is_the_path_between("left", "right") == []
    assert driver.what_is_the_path_between("a", "a") == ["a"]
    assert driver.what_is_the_path_between("join", "join") == ["join"]


def test_async_driver_has_the_same_inspection() -> None:
    """Every inspection method answers the same on both drivers."""
    module = make_module("inspect_async", DIAMOND)
    sync = fnloom.build(module)
    built = fnloom.Builder().with_modules(module).build_async()
    assert built.inputs() == sync.inputs()
    assert built.outputs() == sync.outputs() == ["join"]
    assert built.modules == sync.modules == (module,)
    assert built.what_is_the_path_between("a", "join") == (
        sync.what_is_the_path_between("a", "join")
    )
    with pytest.raises(ValueError, match="unknown nodes: 'nope'"):
        built.what_is_the_path_between("a", "nope")
