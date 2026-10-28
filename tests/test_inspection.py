"""Inspection: `inputs`, `outputs`, `modules` and `what_is_the_path_between`."""

import asyncio
import copy
import importlib
import subprocess
import sys
import textwrap
from typing import TYPE_CHECKING

import pytest

import fnloom
from fnloom import InputView
from helpers import make_module

if TYPE_CHECKING:
    from pathlib import Path
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


RELOADED = """
def a(x: int) -> int:
    return x + 1


def b(a: int) -> int:
    return a * 10
"""

EDITED = """
def a(x: int) -> int:
    return x + 1000


def b(a: int) -> int:
    return a * 10


def c(b: int) -> int:
    return b + 1
"""


def _importable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, source: str
) -> ModuleType:
    """Writes `source` as module `name` on a fresh path entry and imports it."""
    monkeypatch.syspath_prepend(str(tmp_path))
    # No bytecode, so a rewrite within the same second is never read from a stale cache.
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    (tmp_path / f"{name}.py").write_text(textwrap.dedent(source), encoding="utf-8")
    return importlib.import_module(name)


def test_rebuild_sees_a_reloaded_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a reload, `rebuild` runs the edit; the old driver keeps its code."""
    module = _importable(tmp_path, monkeypatch, "rebuild_reloaded", RELOADED)
    store = fnloom.MemoryCache()
    builder = fnloom.Builder().with_modules(module).with_cache(store)
    driver = builder.build()
    built_async = builder.build_async(sync_nodes="inline")
    assert driver.execute(["b"], inputs={"x": 1}) == {"b": 20}
    (tmp_path / "rebuild_reloaded.py").write_text(EDITED, encoding="utf-8")
    importlib.reload(module)

    rebuilt = driver.rebuild()
    assert rebuilt is not driver
    assert rebuilt.modules == (module,)
    # The store is shared, and code versions are read at each build (Q48): the
    # edited node misses on the rebuilt driver, and the old one still hits.
    assert rebuilt.explain_cache(["a"], inputs={"x": 1})["a"].status == "miss"
    assert driver.explain_cache(["a"], inputs={"x": 1})["a"].status == "hit"
    assert rebuilt.execute(["c"], inputs={"x": 1}) == {"c": 10011}
    assert asyncio.run(built_async.rebuild().execute(["c"], inputs={"x": 1})) == {
        "c": 10011
    }
    # The existing drivers keep the functions they were built with (H5.11b).
    assert driver.execute(["b"], inputs={"x": 1}) == {"b": 20}
    assert asyncio.run(built_async.execute(["b"], inputs={"x": 1})) == {"b": 20}
    assert "c" not in driver.node_names()
    assert rebuilt.execute(["b"], inputs={"x": 1}) == {"b": 10010}


def test_rebuild_needs_a_builder() -> None:
    """A driver made from a native graph has no builder to build again from."""
    native = fnloom.Driver(fnloom._core.Graph([], {}))  # noqa: SLF001  # a native graph on purpose
    with pytest.raises(TypeError, match="cannot rebuild a Driver made from a native"):
        native.rebuild()
    made = fnloom.AsyncDriver(native, {})
    with pytest.raises(TypeError, match="cannot rebuild an AsyncDriver made from"):
        made.rebuild()
