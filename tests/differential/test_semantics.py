"""Behaviour fnloom keeps from Hamilton: same values or same exception class."""

import pytest
from harness import (
    Direction,
    Outcome,
    lineage_fnloom,
    lineage_hamilton,
    load_fixture,
    nodes_fnloom,
    nodes_hamilton,
    run_fnloom,
    run_hamilton,
)

from helpers import make_module


def test_chain() -> None:
    """A linear chain computes the same value."""
    module = load_fixture("chain")
    hamilton = run_hamilton([module], ["c"], {"a": 1})
    assert hamilton == Outcome({"c": 4}, None)
    assert run_fnloom([module], ["c"], {"a": 1}) == hamilton


def test_diamond() -> None:
    """A diamond joins both branches the same way."""
    module = load_fixture("diamond")
    hamilton = run_hamilton([module], ["join"], {"a": 1})
    assert hamilton == Outcome({"join": 2}, None)
    assert run_fnloom([module], ["join"], {"a": 1}) == hamilton


def test_several_outputs() -> None:
    """Intermediate nodes can be requested alongside the final one."""
    module = load_fixture("chain")
    hamilton = run_hamilton([module], ["b", "c"], {"a": 1})
    assert hamilton == Outcome({"b": 2, "c": 4}, None)
    assert run_fnloom([module], ["b", "c"], {"a": 1}) == hamilton


def test_optional_input_used_when_supplied() -> None:
    """A supplied value beats a parameter's default."""
    module = load_fixture("optional")
    inputs = {"a": 2, "factor": 5}
    hamilton = run_hamilton([module], ["scaled"], inputs)
    assert hamilton == Outcome({"scaled": 10}, None)
    assert run_fnloom([module], ["scaled"], inputs) == hamilton


def test_optional_input_default_when_not_supplied() -> None:
    """An unsupplied optional input falls back to the default."""
    module = load_fixture("optional")
    hamilton = run_hamilton([module], ["scaled"], {"a": 2})
    assert hamilton == Outcome({"scaled": 6}, None)
    assert run_fnloom([module], ["scaled"], {"a": 2}) == hamilton


def test_required_input_missing_same_exception_class() -> None:
    """A missing required input is a `ValueError` in both."""
    module = load_fixture("missing")
    hamilton = run_hamilton([module], ["out"], {})
    assert hamilton == Outcome(None, "ValueError")
    assert run_fnloom([module], ["out"], {}) == hamilton


def test_unknown_output_same_exception_class() -> None:
    """Requesting a node that does not exist is a `ValueError` in both."""
    module = load_fixture("chain")
    hamilton = run_hamilton([module], ["nope"], {"a": 1})
    assert hamilton == Outcome(None, "ValueError")
    assert run_fnloom([module], ["nope"], {"a": 1}) == hamilton


def test_override_cuts_upstream() -> None:
    """Overriding a node means its upstream, which here always fails, never runs."""
    module = load_fixture("override")
    hamilton = run_hamilton([module], ["top"], {"a": 1}, {"mid": 10})
    assert hamilton == Outcome({"top": 20}, None)
    assert run_fnloom([module], ["top"], {"a": 1}, {"mid": 10}) == hamilton


def test_duplicate_across_modules_same_exception_class() -> None:
    """The same node name in two modules is a `ValueError` in both."""
    modules = [load_fixture("duplicate_a"), load_fixture("duplicate_b")]
    hamilton = run_hamilton(modules, ["shared"], {"a": 1})
    assert hamilton == Outcome(None, "ValueError")
    assert run_fnloom(modules, ["shared"], {"a": 1}) == hamilton


def test_result_keys_follow_the_requested_order() -> None:
    """Result keys come in requested order, each once, in both."""
    module = load_fixture("chain")
    hamilton = run_hamilton([module], ["c", "b", "c"], {"a": 1})
    assert hamilton.values is not None
    assert list(hamilton.values) == ["c", "b"]
    fnloom = run_fnloom([module], ["c", "b", "c"], {"a": 1})
    assert fnloom.values is not None
    assert list(fnloom.values) == list(hamilton.values)


@pytest.mark.parametrize(
    ("direction", "names"),
    [
        ("upstream", ["join"]),
        ("upstream", ["left", "right"]),
        ("downstream", ["root"]),
        ("downstream", ["a"]),
    ],
)
def test_lineage_queries_name_the_same_nodes(
    direction: Direction, names: list[str]
) -> None:
    """Upstream and downstream queries include the named nodes in both."""
    module = load_fixture("diamond")
    hamilton = lineage_hamilton([module], direction, names)
    assert set(names) <= set(hamilton)
    assert lineage_fnloom([module], direction, names) == hamilton


SCANNED = {
    "underscore": "def _hidden(a: int) -> int:\n    return a\n"
    "def f(a: int) -> int:\n    return a\n",
    "imported": "from os.path import join\ndef f(a: int) -> int:\n    return a\n",
    "unannotated_parameter": "def f(a) -> int:\n    return a\n",
    "unannotated_return": "def f(a: int):\n    return a\n",
}


@pytest.mark.parametrize("case", sorted(SCANNED))
def test_scanning_finds_the_same_nodes(case: str) -> None:
    """Underscore and imported functions are skipped; missing annotations refused."""
    module = make_module(f"differential_scan_{case}", SCANNED[case])
    hamilton = nodes_hamilton([module])
    expected = "ValueError" if case.startswith("unannotated") else None
    assert hamilton.error == expected
    assert nodes_fnloom([module]) == hamilton
