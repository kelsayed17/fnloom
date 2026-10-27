"""Behaviour fnloom keeps from Hamilton: same values or same exception class."""

import pytest
from hamilton import driver as hamilton_driver
from harness import (
    Direction,
    Nodes,
    Outcome,
    lineage_fnloom,
    lineage_hamilton,
    load_fixture,
    nodes_fnloom,
    nodes_hamilton,
    run_fnloom,
    run_hamilton,
)

import fnloom
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


ROWS = [
    "bound_seen",
    "first",
    "second",
    "shifted_total",
    "squared",
    "sum_once",
    "sum_twice",
]


@pytest.mark.parametrize("bump", [None, 7], ids=["without_optional", "with_optional"])
def test_rows_compute_the_same_values(bump: int | None) -> None:
    """Every row node gives the same value, with and without the optional input.

    Without `bump`, `shifted_total` is left out: Hamilton fails it (Q35).
    """
    inputs: dict[str, object] = {"numbers": [1, 2, 3], "start": 3}
    outputs = list(ROWS)
    if bump is None:
        outputs.remove("shifted_total")
    else:
        inputs["bump"] = bump
    hamilton = run_hamilton([load_fixture("rows_h")], outputs, inputs)
    assert hamilton.error is None
    assert run_fnloom([load_fixture("rows_f")], outputs, inputs) == hamilton


def test_rows_list_the_same_nodes() -> None:
    """Rows are nodes, their function is not, and their sources are inputs, in both."""
    hamilton = nodes_hamilton([load_fixture("rows_h")])
    assert hamilton.error is None
    assert nodes_fnloom([load_fixture("rows_f")]) == hamilton


def test_value_object_is_passed_by_identity() -> None:
    """A bound list is the same object on every call, so its mutations persist."""
    seen: dict[str, list[object]] = {}
    for runtime, run, fixture in (
        ("hamilton", run_hamilton, "rows_h"),
        ("fnloom", run_fnloom, "rows_f"),
    ):
        module = load_fixture(fixture)
        results = [
            run([module], ["bound_seen"], {"start": start}).values for start in (1, 2)
        ]
        seen[runtime] = [results, list(module.BOUND)]
    assert seen["hamilton"] == [[{"bound_seen": True}] * 2, [1, 2]]
    assert seen["fnloom"] == seen["hamilton"]


@pytest.mark.parametrize("fixture", ["typed", "typed_future"])
def test_node_types_match_hamilton(fixture: str) -> None:
    """Every node, inputs included, has the type Hamilton gives it.

    The fixtures include `-> None` (`NoneType` in both) and the nested string
    forward reference `list["Report"]` (resolved to the class in both).
    """
    module = load_fixture(fixture)
    hamilton = {
        node.name: node.type
        for node in hamilton_driver.Builder()
        .with_modules(module)
        .build()
        .list_available_variables()
    }
    views = fnloom.build(module).nodes
    assert sorted(hamilton) == list(views)
    for name, view in views.items():
        assert hamilton[name] == view.type, name
    assert views["report"].type is module.Report
    assert views["history"].type == list[module.Report]
    assert views["log"].type is type(None)


def test_unresolvable_annotation_same_class() -> None:
    """A name imported only under `TYPE_CHECKING` fails the build the same way."""
    module = make_module(
        "differential_unresolvable",
        """
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from decimal import Decimal

def price(rate: Decimal) -> int:
    return 1
""",
    )
    hamilton = nodes_hamilton([module])
    assert hamilton == Nodes(None, "NameError")
    assert nodes_fnloom([module]) == hamilton
