"""Behaviour fnloom keeps from Hamilton: same values or same exception class."""

import asyncio
import copy
import importlib.util
import inspect
import json
import pickle
import re
import sys
from typing import TYPE_CHECKING, Any, cast, override

import pytest
from hamilton import driver as hamilton_driver
from hamilton import lifecycle
from hamilton.function_modifiers.base import InvalidDecoratorException
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
from type_matrix import (
    TYPES,
    VALUES,
    fnloom_type,
    fnloom_value,
    hamilton_type,
    hamilton_value,
)

import fnloom
from fnloom import _types
from helpers import make_module

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Sequence
    from pathlib import Path
    from types import ModuleType


def pickle_round_trip(value: object) -> object:
    """`value` pickled and loaded again."""
    return pickle.loads(pickle.dumps(value))  # noqa: S301  # our own pickle


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


# An input's row in one of Hamilton's input boxes: `<tr><td>name</td>...`.
_INPUT_ROW = re.compile(r"<tr><td>([^<]+)</td>")


def _hamilton_drawn(module: ModuleType, scope: str, names: Sequence[str]) -> set[str]:
    """The nodes and inputs Hamilton's drawing of `scope` shows.

    Run with the `fake_graphviz` fixture. Hamilton draws inputs as rows of a box
    per consumer, so they are read from the boxes; config nodes are left out.
    """
    built = hamilton_driver.Builder().with_modules(module).build()
    if scope == "upstream":
        drawn = built.display_upstream_of(*names, show_legend=False)
    elif scope == "downstream":
        drawn = built.display_downstream_of(*names, show_legend=False)
    else:
        drawn = built.visualize_path_between(
            names[0],
            names[1],
            strict_path_visualization=scope == "strict",
            show_legend=False,
        )
    return _boxed(drawn.nodes)


def _boxed(nodes: Sequence[tuple[str, str, object]]) -> set[str]:
    """The names Hamilton drew: its nodes, and each input in an input box."""
    found: set[str] = set()
    for name, label, _ in nodes:
        if name.startswith("_") and name.endswith("_inputs"):
            found.update(_INPUT_ROW.findall(label))
        else:
            found.add(name)
    return found


_STEPPED = """
from {package} import pipe_input, pipe_output, source, step, value

def _add(v: int, n: int) -> int:
    return v + n

def _sum(v: int, other: int) -> int:
    return v + other

@pipe_input(step(_add, n=value(1)), step(_add, n=2), step(_sum, other=source("y")))
def fed(x: int) -> int:
    return x * 10

@pipe_output(step(_add, n=1), step(_add, n=value(5)).named("last"))
def piped(x: int) -> int:
    return x
"""


def test_steps_make_the_same_nodes_and_values(tmp_path: Path) -> None:
    """Names, wiring and values of a shared step graph (KEEP, Q104)."""
    hamilton_module = _file_module(
        tmp_path, "stepped_h", _STEPPED.format(package="hamilton.function_modifiers")
    )
    hamilton = hamilton_driver.Builder().with_modules(hamilton_module).build()
    built = fnloom.build(make_module("stepped_f", _STEPPED.format(package="fnloom")))
    hamilton_deps = {
        v.name: sorted(v.required_dependencies | v.optional_dependencies)
        for v in hamilton.list_available_variables()
        if not v.is_external_input
    }
    fnloom_deps = {
        name: sorted(built.nodes[name].dependencies)
        for name in built.node_names()
        if not built.nodes[name].is_input
    }
    assert fnloom_deps == hamilton_deps
    outputs: list[str | Callable[..., object]] = [
        "fed",
        "piped",
        "piped.raw",
        "fed.with_add_1",
    ]
    inputs = {"x": 2, "y": 100}
    assert built.execute(outputs, inputs=inputs) == hamilton.execute(
        cast("Any", outputs), inputs=inputs
    )


_MUTATED = """
from {package} import mutate, pipe_output, step, value

def _first(v: int) -> int:
    return v + 1000

@pipe_output(step(_first))
def a(x: int) -> int:
    return x

@mutate(a, n=value(1))
def _add(v: int, n: int) -> int:
    return v + n

@mutate(a)
def _double(v: int) -> int:
    return v * 2
"""


def test_mutate_makes_the_same_nodes_and_values(tmp_path: Path) -> None:
    """Steps after the target's own, in definition order (KEEP, Q107)."""
    hamilton_module = _file_module(
        tmp_path, "mutated_h", _MUTATED.format(package="hamilton.function_modifiers")
    )
    hamilton = hamilton_driver.Builder().with_modules(hamilton_module).build()
    built = fnloom.build(make_module("mutated_f", _MUTATED.format(package="fnloom")))
    hamilton_names = {
        v.name for v in hamilton.list_available_variables() if not v.is_external_input
    }
    fnloom_names = {n for n in built.node_names() if not built.nodes[n].is_input}
    assert fnloom_names == hamilton_names
    assert built.execute(["a"], inputs={"x": 1}) == hamilton.execute(
        ["a"], inputs={"x": 1}
    )


def _file_module(folder: Path, name: str, source: str) -> ModuleType:
    """`source` as a module loaded from a file, so Hamilton can read its source."""
    path = folder / f"{name}.py"
    path.write_text(source)
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_CHECKED = """
from {package} import check_output

@check_output({checks}, importance="fail")
def a(x: {kind}) -> {kind}:
    return x
"""


@pytest.mark.parametrize(
    ("checks", "kind", "values"),
    [
        ("range=(0, 10)", "int", [0, 10, -1, 11, 5]),
        ("range=(0.5, 1.5)", "float", [0.5, 1.5, 0.4, 2.0]),
        ("values_in=[1, 2]", "int", [1, 2, 3]),
        ("values_in=['p', 'q']", "str", ["p", "r"]),
        ("data_type=int", "int", [1]),
        ("allow_none=False", "int", [1, None]),
        ("contains='lo'", "str", ["hello", "world"]),
    ],
)
def test_builtin_checks_pass_and_fail_the_same_values(
    checks: str, kind: str, values: list[object]
) -> None:
    """Which values a shared built-in check passes, as Hamilton's (KEEP, Q101)."""
    hamilton = (
        hamilton_driver.Builder()
        .with_modules(
            make_module(
                "checked_h",
                _CHECKED.format(
                    package="hamilton.function_modifiers", checks=checks, kind=kind
                ),
            )
        )
        .build()
    )
    built = (
        fnloom.Builder()
        .with_modules(
            make_module(
                "checked_f", _CHECKED.format(package="fnloom", checks=checks, kind=kind)
            )
        )
        .without_input_type_checks()
        .build()
    )
    for value in values:
        try:
            hamilton.execute(["a"], inputs={"x": value})
            hamilton_passes = True
        except Exception:  # noqa: BLE001  # Hamilton's error is no ValueError
            hamilton_passes = False
        try:
            built.execute(["a"], inputs={"x": value})
            fnloom_passes = True
        except fnloom.ValidationError:
            fnloom_passes = False
        assert fnloom_passes == hamilton_passes, value


@pytest.mark.parametrize(
    ("fixture", "outputs", "inputs", "overrides"),
    [
        ("diamond", ["join"], {"a": 1}, {}),
        ("diamond", ["join"], {}, {"root": 2}),
        ("override", ["top"], {}, {"mid": 3}),
        ("optional", ["scaled"], {"a": 1}, {}),
        ("optional", ["scaled"], {"a": 1, "factor": 2}, {}),
    ],
)
def test_exports_of_a_run_list_the_same_nodes(
    fixture: str,
    outputs: list[str],
    inputs: dict[str, object],
    overrides: dict[str, object],
) -> None:
    """The run's nodes and the inputs it reads, as `export_execution`'s (Q95)."""
    module = load_fixture(fixture)
    hamilton = hamilton_driver.Builder().with_modules(module).build()
    exported = json.loads(hamilton.export_execution(outputs, inputs, overrides))
    document = fnloom.build(module).export(outputs, inputs=inputs, overrides=overrides)
    assert {n["name"] for n in document["nodes"]} == {
        n["name"] for n in exported["nodes"]
    }


@pytest.mark.parametrize(
    ("fixture", "scope", "names"),
    [
        ("diamond", "upstream", ["join"]),
        ("diamond", "upstream", ["left", "right"]),
        ("diamond", "downstream", ["root"]),
        ("diamond", "downstream", ["left"]),
        ("diamond", "path", ["a", "join"]),
        ("diamond", "path", ["root", "left"]),
        ("diamond", "strict", ["root", "join"]),
        ("chain", "upstream", ["c"]),
        ("chain", "path", ["a", "c"]),
        ("chain", "strict", ["b", "c"]),
        ("optional", "upstream", ["scaled"]),
        ("optional", "downstream", ["a"]),
        ("optional", "path", ["factor", "scaled"]),
    ],
)
@pytest.mark.usefixtures("fake_graphviz")
def test_drawings_show_the_same_nodes(
    fixture: str, scope: str, names: list[str]
) -> None:
    """Upstream, downstream with parents, and paths draw the same nodes (Q90)."""
    module = load_fixture(fixture)
    built = fnloom.build(module)
    if scope == "upstream":
        drawing = built.draw_upstream_of(*names)
    elif scope == "downstream":
        drawing = built.draw_downstream_of(*names)
    else:
        drawing = built.draw_path_between(names[0], names[1], context=scope != "strict")
    drawn = {node.name for node in drawing.nodes}
    assert drawn == _hamilton_drawn(module, scope, names)


@pytest.mark.parametrize(
    ("fixture", "outputs", "inputs", "overrides"),
    [
        ("diamond", ["join"], {"a": 1}, {}),
        ("diamond", ["left", "right"], {"a": 1}, {}),
        ("diamond", ["join"], {}, {"root": 2}),
        ("chain", ["c"], {"a": 1}, {}),
        ("override", ["top"], {}, {"mid": 3}),
        ("optional", ["scaled"], {"a": 1}, {}),
        ("optional", ["scaled"], {"a": 1, "factor": 2}, {}),
    ],
)
@pytest.mark.usefixtures("fake_graphviz")
def test_execution_drawings_show_the_same_nodes(
    fixture: str,
    outputs: list[str],
    inputs: dict[str, object],
    overrides: dict[str, object],
) -> None:
    """The computed nodes, the inputs read and the overrides, as Hamilton's (Q92)."""
    module = load_fixture(fixture)
    hamilton = hamilton_driver.Builder().with_modules(module).build()
    drawn = hamilton.visualize_execution(
        outputs, inputs=inputs, overrides=overrides, show_legend=False
    )
    drawing = fnloom.build(module).draw_execution(
        outputs, inputs=inputs, overrides=overrides
    )
    assert {n.name for n in drawing.nodes} == _boxed(drawn.nodes)
    marked = {n.name for n in drawing.nodes if "output" in n.marks}
    assert marked == {n for n in outputs if n not in inputs}


@pytest.mark.parametrize(
    ("upstream", "downstream", "expected"),
    [
        ("root", "join", {"root", "left", "right", "join"}),
        ("a", "join", {"a", "root", "left", "right", "join"}),
        ("root", "left", {"root", "left"}),
        ("join", "root", set()),
        ("left", "right", set()),
        ("a", "a", {"a"}),
        ("join", "join", {"join"}),
    ],
)
def test_path_between_has_the_same_nodes(
    upstream: str, downstream: str, expected: set[str]
) -> None:
    """Endpoints are included, an input is an endpoint, and no path is empty."""
    module = load_fixture("diamond")
    built = hamilton_driver.Builder().with_modules(module).build()
    hamilton = {n.name for n in built.what_is_the_path_between(upstream, downstream)}
    assert hamilton == expected
    path = fnloom.build(module).what_is_the_path_between(upstream, downstream)
    assert set(path) == expected
    assert len(path) == len(expected)


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


_PROTOCOL_PRODUCERS = [
    "int",
    "float",
    "bool",
    "str",
    "object",
    "Base",
    "Child",
    "list",
    "Sequence",
    "runtime Protocol",
]
_INT_SUPERTYPES = [
    "int",
    "object",
    "int | None",
    "Optional[int]",
    "int | str",
    "Annotated[int, 'm']",
]
# Every matrix type that does not admit `None`, which Hamilton passes for all (Q79).
_REFUSING_NONE = [
    "int",
    "float",
    "bool",
    "str",
    "Base",
    "Child",
    "list",
    "list[int]",
    "dict[str, int]",
    "tuple[int, ...]",
    "Sequence",
    "Sequence[int]",
    "int | str",
    "Literal[1, 2]",
    "Annotated[int, 'm']",
    "Protocol",
    "runtime Protocol",
    "NewType(int)",
]
DELIBERATE: dict[tuple[str, str], str] = {
    ("tuple[int, ...]", "(1, 2)"): "Q37",
    ("tuple[int, ...]", "Sequence[int]"): "Q37",
    ("NewType(int)", "1"): "Q38",
    ("NewType(int)", "True"): "Q38",
    **dict.fromkeys((("NewType(int)", r) for r in _INT_SUPERTYPES), "Q38"),
    **dict.fromkeys((("Literal[1, 2]", r) for r in _INT_SUPERTYPES), "Q39"),
    ("int | None", "object"): "Q40",
    ("Optional[int]", "object"): "Q40",
    ("int | str", "object"): "Q40",
    ("type[int] | None", "object"): "Q40",
    ("Annotated[int, 'm']", "True"): "Q41",
    **dict.fromkeys(((p, "Protocol") for p in _PROTOCOL_PRODUCERS), "Q42"),
    **dict.fromkeys(((d, "None") for d in _REFUSING_NONE), "Q79"),
}
"""Matrix pairs where fnloom differs on purpose, each mapped to its register row.

Keys are `(declared, value)` for the value check and `(produced, required)` for the
type check; the two never share a key, as no value id is a type id.
"""

VALUE_PAIRS = [
    (declared, value)
    for declared in TYPES
    for value in VALUES
    if (declared, value) not in DELIBERATE
]
TYPE_PAIRS = [
    (produced, required)
    for produced in TYPES
    for required in TYPES
    if (produced, required) not in DELIBERATE
]


@pytest.mark.parametrize(
    ("declared", "value"), VALUE_PAIRS, ids=[f"{d}-{v}" for d, v in VALUE_PAIRS]
)
def test_value_matches_agrees_with_hamilton(declared: str, value: str) -> None:
    """A value fits a declaration exactly when Hamilton's input check says so."""
    assert fnloom_value(declared, value) == hamilton_value(declared, value)


@pytest.mark.parametrize(
    ("produced", "required"), TYPE_PAIRS, ids=[f"{p}-{r}" for p, r in TYPE_PAIRS]
)
def test_type_matches_agrees_with_hamilton(produced: str, required: str) -> None:
    """A producer feeds a parameter exactly when Hamilton's edge check says so."""
    assert fnloom_type(produced, required) == hamilton_type(produced, required)


def test_every_deliberate_pair_is_a_matrix_pair_that_differs() -> None:
    """No stale entry: each listed pair is in the matrix, and the answers differ."""
    for (left, right), quirk in DELIBERATE.items():
        if right in VALUES:
            assert fnloom_value(left, right) != hamilton_value(left, right), quirk
        else:
            assert right in TYPES, (left, right)
            assert fnloom_type(left, right) != hamilton_type(left, right), quirk


def test_input_takes_the_narrower_consumer_type_as_hamilton() -> None:
    """`Base` and `Child` consumers of one input give it `Child` in both."""
    module = make_module(
        "differential_narrow",
        """
class Base:
    pass

class Child(Base):
    pass

def f(a: Base) -> int:
    return 1

def g(a: Child) -> int:
    return 1
""",
    )
    (hamilton,) = (
        node.type
        for node in hamilton_driver.Builder()
        .with_modules(module)
        .build()
        .list_available_variables()
        if node.name == "a"
    )
    assert hamilton is module.Child
    assert fnloom.build(module).nodes["a"].type is hamilton


_ANY_AND_INT = {
    "int first": "def a(x: int) -> int:\n    return x\n\n"
    "def b(x: Any) -> int:\n    return 1\n",
    "Any first": "def b(x: Any) -> int:\n    return 1\n\n"
    "def a(x: int) -> int:\n    return x\n",
}


@pytest.mark.parametrize("order", list(_ANY_AND_INT))
def test_any_and_int_consumers_check_the_input_as_int(order: str) -> None:
    """`int` and `Any` consumers of one input check it as `int`, in either order.

    Hamilton 1.90.0 does too (H5.10b), so `Any` never widens a typed input: a `str`
    is refused even when only the `Any` consumer is requested.
    """
    module = make_module(
        f"differential_any_int_{order.split(maxsplit=1)[0].lower()}",
        "from typing import Any\n\n" + _ANY_AND_INT[order],
    )
    (hamilton,) = (
        node.type
        for node in hamilton_driver.Builder()
        .with_modules(module)
        .build()
        .list_available_variables()
        if node.name == "x"
    )
    assert hamilton is int
    assert fnloom.build(module).nodes["x"].type is int
    for outputs in (["b"], ["a", "b"]):
        wrong = run_hamilton([module], outputs, {"x": "s"})
        assert wrong == Outcome(None, "ValueError"), outputs
        assert run_fnloom([module], outputs, {"x": "s"}) == wrong, outputs
    right = run_hamilton([module], ["b"], {"x": 1})
    assert right == Outcome({"b": 1}, None)
    assert run_fnloom([module], ["b"], {"x": 1}) == right


def test_edge_mismatch_same_exception_class() -> None:
    """A node returning `str` feeding an `int` parameter fails both builds."""
    module = make_module(
        "differential_edge_mismatch",
        """
def text() -> str:
    return "x"

def uses_text(text: int) -> int:
    return 1
""",
    )
    assert nodes_hamilton([module]) == Nodes(None, "ValueError")
    assert nodes_fnloom([module]) == Nodes(None, "ValueError")


def test_wrong_input_type_same_exception_class() -> None:
    """A `str` supplied for an `int` input is refused by both, before any node runs."""
    module = load_fixture("chain")
    assert run_hamilton([module], ["c"], {"a": "x"}) == Outcome(None, "ValueError")
    assert run_fnloom([module], ["c"], {"a": "x"}) == Outcome(None, "ValueError")


def test_a_compiled_plain_class_check_agrees_with_value_matches() -> None:
    """Over the matrix, `isinstance` on a plain class answers as `value_matches`."""
    for declared, annotation in TYPES.items():
        cls = _types.plain_class(annotation)
        if cls is None:
            continue
        for value_id, value in VALUES.items():
            compiled = isinstance(value, cls)
            assert compiled is _types.value_matches(annotation, value), (
                declared,
                value_id,
            )


def test_a_prepared_check_agrees_with_value_matches() -> None:
    """Over the matrix, the check prepared once per type answers as `value_matches`."""
    for declared, annotation in TYPES.items():
        _, matches = _types.check_of(annotation)
        for value_id, value in VALUES.items():
            assert matches(value) is _types.value_matches(annotation, value), (
                declared,
                value_id,
            )


def test_public_signatures_resolve() -> None:
    """`inspect.signature` resolves the driver API's annotations in both runtimes."""
    for hamilton_callable, fnloom_callable in (
        (hamilton_driver.Builder.with_modules, fnloom.Builder.with_modules),
        (hamilton_driver.Builder.build, fnloom.Builder.build),
        (hamilton_driver.Driver.execute, fnloom.Driver.execute),
    ):
        inspect.signature(hamilton_callable, eval_str=True)
        inspect.signature(fnloom_callable, eval_str=True)


@pytest.mark.parametrize("copier", [pickle_round_trip, copy.deepcopy])
def test_a_driver_pickles_and_copies(copier: Callable[[object], object]) -> None:
    """A pickled or deep-copied driver runs the same in both runtimes."""
    module = load_fixture("chain")
    hamilton = copier(hamilton_driver.Builder().with_modules(module).build())
    again = copier(fnloom.build(module))
    assert isinstance(hamilton, hamilton_driver.Driver)
    assert isinstance(again, fnloom.Driver)
    expected = {"c": 4}
    assert hamilton.execute(["c"], inputs={"a": 1}) == expected
    assert again.execute(["c"], inputs={"a": 1}) == expected


def test_a_missing_input_names_the_nodes_needing_it() -> None:
    """Both raise `ValueError` for a missing input, naming the node that reads it."""
    module = load_fixture("chain")
    hamilton = hamilton_driver.Builder().with_modules(module).build()
    with pytest.raises(
        ValueError, match=r"Required input a not provided for nodes: \['b'\]"
    ):
        hamilton.execute(["c"])
    with pytest.raises(ValueError, match=r"^missing inputs: 'a' \(needed by 'b'\)$"):
        fnloom.build(module).execute(["c"])


# Variants for the config tests; `{decorator}` imports each library's `config`.
_CONFIG_VARIANTS = """
{decorator}

@config.when(mode="a")
def foo__a__b(x: int) -> int:
    return x + 1

def plain__x(x: int) -> int:
    return x

@config.when(a=1, b=2)
def both__eq(x: int) -> int:
    return x

@config.when_not(a=1, b=2)
def neither__ne(x: int) -> int:
    return x

@config.when(flag=1)
def truthy__one(x: int) -> int:
    return x

@config.when(flag="1")
def text__one(x: int) -> int:
    return x

@config.when(a=1)
@config.when(b=2)
def stacked__ab(x: int) -> int:
    return x
"""

_CONFIGS: list[dict[str, object]] = [
    {"mode": "a", "a": 1, "b": 2, "flag": True},
    {"mode": "b", "a": 0, "b": 2, "flag": 1},
    {"mode": "a", "a": 0, "b": 0, "flag": "1"},
    {"mode": "a", "a": 1, "b": 0, "flag": 0},
]


@pytest.mark.parametrize("settings", _CONFIGS, ids=repr)
def test_config_selects_the_same_nodes(settings: dict[str, object]) -> None:
    """Names cut at the last `__`, AND of keys, `==` matching and stacking agree."""
    hamilton_module = make_module(
        "semantics_config_h",
        _CONFIG_VARIANTS.format(
            decorator="from hamilton.function_modifiers import config"
        ),
    )
    fnloom_module = make_module(
        "semantics_config_f",
        _CONFIG_VARIANTS.format(decorator="from fnloom import config"),
    )
    hamilton = (
        hamilton_driver.Builder()
        .with_config(settings)
        .with_modules(hamilton_module)
        .build()
    )
    # Hamilton adds every config key as a node; fnloom never does (Q56).
    hamilton_names = sorted(
        v.name for v in hamilton.list_available_variables() if v.name not in settings
    )
    driver = fnloom.Builder().with_config(settings).with_modules(fnloom_module).build()
    assert driver.node_names() == hamilton_names
    outputs = [name for name in hamilton_names if name != "x"]
    assert driver.execute(outputs, inputs={"x": 1}) == hamilton.execute(
        outputs, inputs={"x": 1}
    )


# A config decorator stacked with `{other}`, in the order `{stack}` gives;
# `{imports}` imports each library's decorators.
_STACKED = """
{imports}

{stack}
def rows__a(k: int) -> int:
    return k

@config.when(mode="b")
def rows__b(x: int) -> int:
    return x
"""

_PARAMETERIZE = '@parameterize(r1={"k": value(1)}, r2={"k": value(2)})'
_TAG = '@tag(owner="desk")'
_WHEN = '@config.when(mode="a")'


@pytest.mark.parametrize("other", [_PARAMETERIZE, _TAG], ids=["parameterize", "tag"])
@pytest.mark.parametrize("config_first", [True, False], ids=["outer", "inner"])
@pytest.mark.parametrize("mode", ["a", "b"])
def test_decorator_order_never_matters(
    other: str, *, config_first: bool, mode: str
) -> None:
    """A config decorator over or under `@parameterize` or `@tag`: the same graph."""
    stack = f"{_WHEN}\n{other}" if config_first else f"{other}\n{_WHEN}"
    library = "hamilton.function_modifiers"
    names = "config, parameterize, tag, value"
    hamilton_module = make_module(
        "semantics_stacked_h",
        _STACKED.format(imports=f"from {library} import {names}", stack=stack),
    )
    fnloom_module = make_module(
        "semantics_stacked_f",
        _STACKED.format(imports=f"from fnloom import {names}", stack=stack),
    )
    settings: dict[str, object] = {"mode": mode}
    hamilton = (
        hamilton_driver.Builder()
        .with_config(settings)
        .with_modules(hamilton_module)
        .build()
    )
    variables = {
        v.name: v for v in hamilton.list_available_variables() if v.name != "mode"
    }
    driver = fnloom.Builder().with_config(settings).with_modules(fnloom_module).build()
    assert driver.node_names() == sorted(variables)
    outputs = [
        name for name in sorted(variables) if not variables[name].is_external_input
    ]
    inputs = {name: 9 for name, v in variables.items() if v.is_external_input}
    assert driver.execute(outputs, inputs=inputs) == hamilton.execute(
        outputs, inputs=inputs
    )
    for name in outputs:
        assert driver.nodes[name].tags.get("owner") == variables[name].tags.get("owner")


# Producers for the extract tests; `{decorator}` imports each library's
# `extract_fields`.
_EXTRACTED = """
from typing import TypedDict
{decorator}

class Point(TypedDict):
    x: int
    y: str

@extract_fields({{"a": int, "b": str}})
def mapped(n: int) -> dict:
    return {{"a": n, "b": str(n), "extra": 0}}

@extract_fields("c", "d")
def named(n: int) -> dict[str, int]:
    return {{"c": n, "d": n + 1}}

@extract_fields()
def point(n: int) -> Point:
    return {{"x": n, "y": "y"}}

def total(a: int, c: int, x: int) -> int:
    return a + c + x
"""
_HAMILTON_EXTRACT = "from hamilton.function_modifiers import extract_fields"
_FNLOOM_EXTRACT = "from fnloom import extract_fields"


def test_extract_fields_makes_the_same_nodes_types_and_values() -> None:
    """Each producer is kept under its own name; the fields and their types agree."""
    hamilton_module = make_module(
        "semantics_extract_h", _EXTRACTED.format(decorator=_HAMILTON_EXTRACT)
    )
    fnloom_module = make_module(
        "semantics_extract_f", _EXTRACTED.format(decorator=_FNLOOM_EXTRACT)
    )
    hamilton = hamilton_driver.Builder().with_modules(hamilton_module).build()
    driver = fnloom.build(fnloom_module)
    hamilton_types = {
        v.name: v.type for v in hamilton.list_available_variables() if v.name != "n"
    }
    assert driver.node_names() == sorted([*hamilton_types, "n"])
    for name, produced in hamilton_types.items():
        if name == "point":
            # Each module defines its own `Point`.
            assert driver.nodes[name].type is fnloom_module.Point
            assert produced is hamilton_module.Point
        else:
            assert driver.nodes[name].type == produced, name
    outputs = sorted(hamilton_types)
    assert driver.execute(outputs, inputs={"n": 2}) == hamilton.execute(
        outputs, inputs={"n": 2}
    )


_EMPTY_FIELDS = """
{decorator}

@extract_fields({{}})
def p() -> dict:
    return {{}}
"""

_NOT_A_TYPE = """
{decorator}

@extract_fields({{"a": 5}})
def p() -> dict:
    return {{"a": 5}}
"""


def test_extract_fields_refuses_the_same_arguments_when_decorating() -> None:
    """An empty mapping and a non-type value fail at decoration on both sides."""
    for source_text, fnloom_error in (
        (_EMPTY_FIELDS, ValueError),
        (_NOT_A_TYPE, TypeError),
    ):
        with pytest.raises(InvalidDecoratorException):
            make_module(
                "semantics_extract_bad_h",
                source_text.format(decorator=_HAMILTON_EXTRACT),
            )
        with pytest.raises(fnloom_error):
            make_module(
                "semantics_extract_bad_f",
                source_text.format(decorator=_FNLOOM_EXTRACT),
            )


# The producers for the extract_columns tests; `{decorator}` imports each
# library's `extract_columns`.
_COLUMNS = """
import pandas as pd
{decorator}

@extract_columns("a", ("b", "The b column."))
def frame(n: int) -> pd.DataFrame:
    '''The producer.'''
    return pd.DataFrame({{"a": [n, n + 1], "b": ["x", "y"], "c": [0, 0]}})
"""
_HAMILTON_COLUMNS = "from hamilton.function_modifiers import extract_columns"
_FNLOOM_COLUMNS = "from fnloom import extract_columns"


def _hamilton_pandas() -> None:
    """Registers Hamilton's pandas extension, which its autoload would load."""
    from hamilton import registry  # noqa: PLC0415  # only these tests need it

    registry.load_extension("pandas")


def test_extract_columns_makes_the_same_nodes_types_docs_and_values() -> None:
    """The column nodes, their Series type, docs and values, and the kept producer."""
    pd = pytest.importorskip("pandas")
    _hamilton_pandas()
    hamilton_module = make_module(
        "semantics_columns_h", _COLUMNS.format(decorator=_HAMILTON_COLUMNS)
    )
    fnloom_module = make_module(
        "semantics_columns_f", _COLUMNS.format(decorator=_FNLOOM_COLUMNS)
    )
    hamilton = hamilton_driver.Builder().with_modules(hamilton_module).build()
    driver = fnloom.build(fnloom_module)
    hamilton_nodes = {
        v.name: (v.type, v.documentation)
        for v in hamilton.list_available_variables()
        if v.name != "n"
    }
    assert hamilton_nodes == {
        "a": (pd.Series, "The producer."),
        "b": (pd.Series, "The b column."),
        "frame": (pd.DataFrame, "The producer."),
    }
    assert driver.node_names() == sorted([*hamilton_nodes, "n"])
    for name, (produced, doc) in hamilton_nodes.items():
        assert driver.nodes[name].type is produced, name
        assert driver.nodes[name].doc == doc, name
        assert driver.nodes[name].dependencies == (
            ("n",) if name == "frame" else ("frame",)
        )
    outputs = sorted(hamilton_nodes)
    expected = hamilton.execute(outputs, inputs={"n": 2})
    result: dict[str, Any] = dict(driver.execute(outputs, inputs={"n": 2}))
    assert list(result) == outputs
    for name in outputs:
        assert result[name].equals(expected[name]), name
    assert list(result["frame"].columns) == ["a", "b", "c"]


def test_extract_columns_refuses_a_list_and_no_columns() -> None:
    """A list and no columns at all are refused when decorating, on both sides."""
    from hamilton.function_modifiers import extract_columns  # noqa: PLC0415  # beside fnloom's

    with pytest.raises(InvalidDecoratorException, match=r"Please `\*` in front"):
        extract_columns(["a", "b"])  # ty: ignore[invalid-argument-type]  # the misuse under test
    with pytest.raises(TypeError, match=r"put `\*` in front of it to expand it"):
        fnloom.extract_columns(["a", "b"])  # ty: ignore[invalid-argument-type]  # the misuse under test
    with pytest.raises(InvalidDecoratorException, match="empty arguments"):
        extract_columns()
    with pytest.raises(ValueError, match="got no columns"):
        fnloom.extract_columns()


# A column named like its producer (H2.3l).
_SELF_COLUMN = """
import pandas as pd
{decorator}

@extract_columns("frame", "a")
def frame(n: int) -> pd.DataFrame:
    return pd.DataFrame({{"frame": [n], "a": [n]}})
"""


def test_a_column_named_like_its_producer_fails_the_build() -> None:
    """Both sides refuse the clash with `ValueError` at build, not when run."""
    pytest.importorskip("pandas")
    _hamilton_pandas()
    hamilton_module = make_module(
        "semantics_self_column_h", _SELF_COLUMN.format(decorator=_HAMILTON_COLUMNS)
    )
    with pytest.raises(ValueError, match="more than once"):
        hamilton_driver.Builder().with_modules(hamilton_module).build()
    fnloom_module = make_module(
        "semantics_self_column_f", _SELF_COLUMN.format(decorator=_FNLOOM_COLUMNS)
    )
    with pytest.raises(ValueError, match=r"^node 'frame' is defined twice in "):
        fnloom.build(fnloom_module)


# A graph mixing async and sync nodes, with an optional input and a sync row. An
# async row is left out: Hamilton doesn't await one (Q69).
_ASYNC_GRAPH = """
import asyncio
{decorator}


async def doubled(x: int) -> int:
    await asyncio.sleep(0)
    return x * 2


def plus_one(doubled: int) -> int:
    return doubled + 1


async def shifted(doubled: int, plus_one: int, offset: int = 0) -> int:
    await asyncio.sleep(0)
    return doubled * plus_one + offset


def scaled(shifted: int, doubled: int) -> int:
    return shifted - doubled


@parameterize(bumped={{"step": value(7)}}, rebased={{"step": source("plus_one")}})
def moved(scaled: int, step: int) -> int:
    return scaled + step
"""
# Hamilton's AsyncDriver warns on 3.14 from its own code (Q13).
_HAMILTON_ASYNC_WARNING = (
    "ignore:'asyncio.iscoroutinefunction' is deprecated:DeprecationWarning"
)


@pytest.mark.filterwarnings(_HAMILTON_ASYNC_WARNING)
def test_async_driver_computes_the_same_values() -> None:
    """Hamilton's `AsyncDriver`, through its builder, and fnloom's agree on values."""
    from hamilton import async_driver  # noqa: PLC0415  # only this test needs it

    hamilton_module = make_module(
        "semantics_async_h",
        _ASYNC_GRAPH.format(
            decorator="from hamilton.function_modifiers import parameterize, "
            "source, value"
        ),
    )
    module = make_module(
        "semantics_async_f",
        _ASYNC_GRAPH.format(decorator="from fnloom import parameterize, source, value"),
    )
    outputs = ["rebased", "bumped", "scaled", "shifted", "plus_one", "doubled"]
    requests: list[tuple[dict[str, object], dict[str, object]]] = [
        ({"x": 2}, {}),
        ({"x": 2, "offset": 5}, {}),
        ({}, {"doubled": 10}),
        ({"x": 1}, {"plus_one": 0}),
    ]

    async def hamilton() -> list[dict[str, object]]:
        # Typed as the sync builder's `build`, which this one makes a coroutine.
        pending: object = async_driver.Builder().with_modules(hamilton_module).build()
        built = await cast("Coroutine[Any, Any, async_driver.AsyncDriver]", pending)
        return [
            await built.execute(outputs, inputs=inputs, overrides=overrides)
            for inputs, overrides in requests
        ]

    async def runs(driver: fnloom.AsyncDriver) -> list[dict[str, object]]:
        return [
            await driver.execute(outputs, inputs=inputs, overrides=overrides)
            for inputs, overrides in requests
        ]

    expected = asyncio.run(hamilton())
    assert expected[0] == {
        "bumped": 23,
        "doubled": 4,
        "plus_one": 5,
        "rebased": 21,
        "scaled": 16,
        "shifted": 20,
    }
    for sync_nodes in ("thread", "inline"):
        builder = fnloom.Builder().with_modules(module)
        driver = builder.build_async(sync_nodes=sync_nodes)
        assert asyncio.run(runs(driver)) == expected


@pytest.mark.parametrize(
    ("outputs", "inputs", "overrides"),
    [
        (["c"], {"a": 1}, {"a": 5}),
        (["c"], {}, {"a": 5}),
        (["a"], {}, {"a": 5}),
        (["c"], {}, {"b": 10}),
        (["c"], {}, {"c": 7}),
    ],
)
def test_strict_overrides_behave_as_hamilton(
    outputs: list[str], inputs: dict[str, object], overrides: dict[str, object]
) -> None:
    """With strict overrides, an unsupplied overridden input is refused as in Hamilton.

    Hamilton's rule (H5.5): an override only prunes below the overridden node, so
    an input is refused when a node the plan computes needs it and it is not supplied.
    """
    module = load_fixture("chain")
    hamilton = run_hamilton([module], outputs, inputs, overrides)
    strict = fnloom.Builder().with_modules(module).with_strict_overrides().build()
    try:
        values = strict.execute(outputs, inputs=inputs, overrides=overrides)
        outcome = Outcome(dict(values), None)
    except Exception as error:  # noqa: BLE001  # the exception class is the thing being compared
        outcome = Outcome(None, type(error).__name__)
    assert outcome == hamilton


_SUGAR_GRAPH = """
{imports}

@parameterize(
    literal=({{"p": value(1)}}, "kept {{as}} written {{p}}"),
    silent=({{"p": value(2)}}, None),
    templated={{"p": source("up")}},
    constant={{"p": value(4.5)}},
)
def rows(p: int) -> int:
    \"\"\"{{output_name}} reads {{p}}.\"\"\"
    return int(p) + 100


@parameterize_sources(low=dict(p="a"), high=dict(p="b"))
def picked(p: int, q: int = 5) -> int:
    \"\"\"Row {{output_name}} takes {{p}}.\"\"\"
    return p * q


@parameterize_values("p", {{("one", "the first"): 1, ("two", None): 2}})
def fixed(p: int) -> int:
    \"\"\"Never shown.\"\"\"
    return p * 10


@parameterize(
    listed={{"xs": group(source("a"), value(3), source("b"))}},
    single={{"xs": group(source("b"))}},
)
def collected(xs: list[int]) -> list[int]:
    return [*xs, len(xs)]


@parameterize(
    keyed={{"ys": group(first=source("a"), fixed=value(3), second=source("b"))}}
)
def mapped(ys: dict[str, int]) -> dict[str, int]:
    return {{**ys, "n": len(ys)}}


def up(a: int) -> int:
    return a + 1
"""
_SUGAR_IMPORTS = (
    "from {library}.function_modifiers import (\n"
    "    group, parameterize, parameterize_sources, parameterize_values, source, value)"
)


def test_parameterize_sugar_values_docs_and_groups_match_hamilton() -> None:
    """Row tuples, templated docs, shorthands and `group` agree with Hamilton (H5.4)."""
    hamilton_module = make_module(
        "semantics_sugar_h",
        _SUGAR_GRAPH.format(imports=_SUGAR_IMPORTS.format(library="hamilton")),
    )
    module = make_module(
        "semantics_sugar_f",
        _SUGAR_GRAPH.format(
            imports=_SUGAR_IMPORTS.format(library="fnloom").replace(
                ".function_modifiers", ""
            )
        ),
    )
    hamilton = hamilton_driver.Builder().with_modules(hamilton_module).build()
    built = fnloom.build(module)
    names = sorted(
        {
            "literal",
            "silent",
            "templated",
            "constant",
            "low",
            "high",
            "one",
            "two",
            "listed",
            "single",
            "keyed",
        }
    )
    assert sorted(set(names) - set(built.node_names())) == []
    inputs = {"a": 2, "b": 3}
    expected = hamilton.execute(cast("Any", names), inputs=inputs)
    assert expected["listed"] == [2, 3, 3, 3]
    assert expected["keyed"] == {"first": 2, "fixed": 3, "second": 3, "n": 3}
    assert dict(built.execute(names, inputs=inputs)) == dict(expected)
    # Docs: a row's own doc verbatim, the function's templated otherwise.
    docs = {n: hamilton.graph.nodes[n].documentation for n in names}
    assert docs["literal"] == "kept {as} written {p}"
    assert docs["silent"] is None
    assert docs["templated"] == "templated reads up."
    assert docs["constant"] == "constant reads 4.5."
    assert docs["low"] == "Row low takes a."
    assert docs["two"] is None
    assert {n: built.nodes[n].doc for n in names} == docs
    # A group's sources are inputs of the element type, `int`, not `list[int]`.
    hamilton_types = {n: v.input_types for n, v in hamilton.graph.nodes.items()}
    assert hamilton_types["listed"]["a"][0] is int
    assert built.nodes["a"].type is int
    assert built.nodes["b"].type is int
    assert built.nodes["listed"].dependencies == ("a", "b")


# Hooks (Plan 6, H6.2 to H6.4): which nodes report, when the run starts, and the order
# across hooks. Each recorder logs `(hook, stage, node)`, with fnloom's stage names.
type HookEntry = tuple[str, str, str | None]

HOOKED_GRAPH = """
def a(x: int) -> int:
    return x + 1


def b(a: int) -> int:
    return a * 2


def c(b: int) -> int:
    return b + 3


def unused(x: int) -> int:
    return x


def boom(a: int) -> int:
    raise ValueError("bad " + str(a))


def after_boom(boom: int) -> int:
    return boom
"""


class HamiltonRecorder(lifecycle.NodeExecutionHook, lifecycle.GraphExecutionHook):
    """A Hamilton adapter that logs each hook it gets, and keeps its keywords."""

    def __init__(self, name: str, log: list[HookEntry]) -> None:
        """Logs to `log` as `name`."""
        self.name = name
        self.log = log
        self.calls: list[dict[str, Any]] = []

    def _record(self, stage: str, node: str | None, kwargs: dict[str, Any]) -> None:
        self.log.append((self.name, stage, node))
        self.calls.append(kwargs)

    @override
    def run_before_graph_execution(self, **kwargs: Any) -> None:
        self._record("before_run", None, kwargs)

    @override
    def run_before_node_execution(self, **kwargs: Any) -> None:
        self._record("before_node", kwargs["node_name"], kwargs)

    @override
    def run_after_node_execution(self, **kwargs: Any) -> None:
        self._record("after_node", kwargs["node_name"], kwargs)

    @override
    def run_after_graph_execution(self, **kwargs: Any) -> None:
        self._record("after_run", None, kwargs)


class FnloomRecorder(fnloom.Hook):
    """The fnloom hook that logs as `HamiltonRecorder` does, and keeps its events."""

    def __init__(self, name: str, log: list[HookEntry]) -> None:
        """Logs to `log` as `name`."""
        self.name = name
        self.log = log
        self.events: list[Any] = []

    def _record(self, stage: str, node: str | None, event: object) -> None:
        self.log.append((self.name, stage, node))
        self.events.append(event)

    @override
    def before_run(self, event: fnloom.RunStart) -> None:
        self._record("before_run", None, event)

    @override
    def before_node(self, event: fnloom.NodeStart) -> None:
        self._record("before_node", event.node.name, event)

    @override
    def after_node(self, event: fnloom.NodeEnd) -> None:
        self._record("after_node", event.node.name, event)

    @override
    def after_run(self, event: fnloom.RunEnd) -> None:
        self._record("after_run", None, event)


def hooked_logs(
    request: dict[str, Any], names: tuple[str, ...] = ("A",)
) -> tuple[
    tuple[list[HookEntry], list[HamiltonRecorder], Outcome],
    tuple[list[HookEntry], list[FnloomRecorder], Outcome],
]:
    """Runs `request` on both, each with one recorder per name, in that order."""
    module = make_module("semantics_hooked", HOOKED_GRAPH)
    hamilton_log: list[HookEntry] = []
    hamiltons = [HamiltonRecorder(n, hamilton_log) for n in names]
    hamilton = (
        hamilton_driver.Builder().with_modules(module).with_adapters(*hamiltons).build()
    )
    log: list[HookEntry] = []
    ours = [FnloomRecorder(n, log) for n in names]
    built = fnloom.Builder().with_modules(module).with_hooks(*ours).build()
    outcomes: list[Outcome] = []
    for run in (hamilton.execute, built.execute):
        try:
            outcomes.append(Outcome(dict(run(**request)), None))
        except Exception as error:  # noqa: BLE001  # the exception class is compared
            outcomes.append(Outcome(None, type(error).__name__))
    return (hamilton_log, hamiltons, outcomes[0]), (log, ours, outcomes[1])


@pytest.mark.parametrize(
    "request_",
    [
        {"final_vars": ["c"], "inputs": {"x": 1}, "overrides": {"b": 100}},
        {"final_vars": ["c"], "overrides": {"a": 5}},
        {"final_vars": ["x", "a"], "inputs": {"x": 1}},
        {"final_vars": ["c", "a"], "inputs": {"x": 1}},
    ],
    ids=["override", "override-replaces-input", "input-as-output", "request"],
)
def test_hooks_skip_inputs_overrides_and_unneeded_nodes(
    request_: dict[str, Any],
) -> None:
    """Decision 3 (H6.2): only the nodes the run computes get node events."""
    (hamilton_log, _, hamilton), (log, _, ours) = hooked_logs(request_)
    assert hamilton.error is None
    assert ours == hamilton
    assert log == hamilton_log


def test_a_failing_node_stops_the_run_for_hooks() -> None:
    """Decision 3 (H6.3): no later node starts; the very error, `ok=False` after."""
    # One output: with two, the order nodes run in may differ (Q26).
    request = {"final_vars": ["after_boom"], "inputs": {"x": 1}}
    (hamilton_log, (hamilton,), outcome), (log, (ours,), mine) = hooked_logs(request)
    assert outcome == mine == Outcome(None, "ValueError")
    assert log == hamilton_log
    assert [stage for _, stage, _ in log][-2:] == ["after_node", "after_run"]
    failed, end = hamilton.calls[-2], hamilton.calls[-1]
    assert (failed["success"], failed["result"]) == (False, None)
    assert (end["success"], end["error"], end["results"]) == (
        False,
        failed["error"],
        None,
    )
    node_end, run_end = ours.events[-2], ours.events[-1]
    assert (node_end.ok, node_end.result) == (False, None)
    assert (run_end.ok, run_end.error, run_end.results) == (False, node_end.error, None)
    assert type(node_end.error) is type(failed["error"])
    assert str(node_end.error) == str(failed["error"])


@pytest.mark.parametrize(
    ("request_", "expected"),
    [
        ({"final_vars": ["c"]}, ["before_run", "after_run"]),
        ({"final_vars": ["c"], "inputs": {"x": "1"}}, ["before_run", "after_run"]),
        ({"final_vars": ["nope"], "inputs": {"x": 1}}, []),
    ],
    ids=["missing-input", "mistyped-input", "unknown-output"],
)
def test_before_run_fires_before_the_value_checks(
    request_: dict[str, Any], expected: list[str]
) -> None:
    """Decision 4 (H6.3): a value check fails inside the run, a shape check before."""
    (hamilton_log, (hamilton,), outcome), (log, (ours,), mine) = hooked_logs(request_)
    assert outcome == mine == Outcome(None, "ValueError")
    assert [stage for _, stage, _ in hamilton_log] == expected
    assert log == hamilton_log
    if expected:
        assert hamilton.calls[-1]["success"] is False
        assert ours.events[-1].ok is False
        assert type(ours.events[-1].error) is type(hamilton.calls[-1]["error"])


@pytest.mark.parametrize("names", [("A", "B"), ("B", "A")])
def test_hooks_run_in_the_order_given(names: tuple[str, ...]) -> None:
    """H6.4: every event calls the hooks in the order given, after-events too."""
    request = {"final_vars": ["c"], "inputs": {"x": 1}}
    (hamilton_log, _, outcome), (log, _, mine) = hooked_logs(request, names)
    assert outcome == mine == Outcome({"c": 7}, None)
    assert log == hamilton_log
    assert [name for name, _, _ in log[:2]] == list(names)
    assert [name for name, _, _ in log[-2:]] == list(names)


# Result builders (Plan 6, H6.12 and H6.13): what a builder gets, and `raw_execute`.
class HamiltonSeen(lifecycle.ResultBuilder):
    """A Hamilton result builder that keeps the outputs it gets, in order."""

    def __init__(self) -> None:
        """Nothing seen yet."""
        self.got: list[list[tuple[str, Any]]] = []

    @override
    def build_result(self, **outputs: Any) -> Any:
        self.got.append(list(outputs.items()))
        return ("built", len(self.got))


class FnloomSeen:
    """The fnloom builder that keeps what it gets as `HamiltonSeen` does."""

    def __init__(self) -> None:
        """Nothing seen yet."""
        self.got: list[list[tuple[str, Any]]] = []

    def __call__(self, outputs: dict[str, object]) -> tuple[str, int]:
        """Keeps the outputs as pairs, in order."""
        self.got.append(list(outputs.items()))
        return ("built", len(self.got))


@pytest.mark.parametrize(
    "request_",
    [
        {"final_vars": ["c", "x", "a"], "inputs": {"x": 1}},
        {"final_vars": ["a"], "overrides": {"a": 99}},
        {"final_vars": ["c", "b"], "inputs": {"x": 1}, "overrides": {"a": 5}},
        {"final_vars": ["c", "c"], "inputs": {"x": 1}},
    ],
    ids=["input-as-output", "override-as-output", "mixed", "repeated"],
)
def test_a_result_builder_gets_the_outputs_in_request_order(
    request_: dict[str, Any],
) -> None:
    """H6.12: request order, each once, inputs and overrides requested included."""
    module = make_module("semantics_built", HOOKED_GRAPH)
    hamilton_seen = HamiltonSeen()
    hamilton = (
        hamilton_driver.Builder()
        .with_modules(module)
        .with_adapters(hamilton_seen)
        .build()
    )
    ours = FnloomSeen()
    built = fnloom.Builder().with_modules(module).with_result_builder(ours).build()
    assert hamilton.execute(**request_) == built.execute(**request_) == ("built", 1)
    assert ours.got == hamilton_seen.got


def test_raw_execute_skips_the_builder_and_runs_the_hooks() -> None:
    """H6.13: the raw dict, no build, and every hook of the run."""
    module = make_module("semantics_raw", HOOKED_GRAPH)
    hamilton_log: list[HookEntry] = []
    hamilton_seen = HamiltonSeen()
    hamilton = (
        hamilton_driver.Builder()
        .with_modules(module)
        .with_adapters(HamiltonRecorder("A", hamilton_log), hamilton_seen)
        .build()
    )
    log: list[HookEntry] = []
    ours = FnloomSeen()
    built = (
        fnloom.Builder()
        .with_modules(module)
        .with_hooks(FnloomRecorder("A", log))
        .with_result_builder(ours)
        .build()
    )
    request: dict[str, Any] = {"final_vars": ["c", "x"], "inputs": {"x": 1}}
    raw = hamilton.raw_execute(**request)
    assert raw == built.raw_execute(**request) == {"c": 7, "x": 1}
    assert list(raw) == ["c", "x"]
    assert hamilton_seen.got == ours.got == []
    assert log == hamilton_log
    assert [stage for _, stage, _ in log] == [
        "before_run",
        "before_node",
        "after_node",
        "before_node",
        "after_node",
        "before_node",
        "after_node",
        "after_run",
    ]


# Output checks (Plan 6, H6.14): neither side checks a node's result by default.
WRONG_RESULT = """
def wrong() -> int:
    return "s"


def reads(wrong: int) -> str:
    return wrong * 2
"""


def test_no_result_is_checked_by_default() -> None:
    """H6.14: a node returning the wrong type is passed on, unchecked, by both."""
    module = make_module("semantics_wrong_result", WRONG_RESULT)
    hamilton = hamilton_driver.Builder().with_modules(module).build()
    ours = fnloom.Builder().with_modules(module).build()
    request: dict[str, Any] = {"final_vars": ["wrong", "reads"]}
    assert hamilton.execute(**request) == ours.execute(**request)
    assert ours.execute(**request) == {"wrong": "s", "reads": "ss"}
