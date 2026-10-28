"""Behaviour fnloom keeps from Hamilton: same values or same exception class."""

import asyncio
import copy
import inspect
import pickle
from typing import TYPE_CHECKING, Any, cast

import pytest
from hamilton import driver as hamilton_driver
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
    from collections.abc import Callable, Coroutine


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
    ("Annotated[int, 'm']", "True"): "Q41",
    **dict.fromkeys(((p, "Protocol") for p in _PROTOCOL_PRODUCERS), "Q42"),
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
            compiled = value is None or isinstance(value, cls)
            assert compiled is _types.value_matches(annotation, value), (
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


# A graph mixing async and sync nodes, with an optional input.
_ASYNC_GRAPH = """
import asyncio


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
"""
# Hamilton's AsyncDriver warns on 3.14 from its own code (Q13).
_HAMILTON_ASYNC_WARNING = (
    "ignore:'asyncio.iscoroutinefunction' is deprecated:DeprecationWarning"
)


@pytest.mark.filterwarnings(_HAMILTON_ASYNC_WARNING)
def test_async_driver_computes_the_same_values() -> None:
    """Hamilton's `AsyncDriver`, through its builder, and fnloom's agree on values."""
    from hamilton import async_driver  # noqa: PLC0415  # only this test needs it

    module = make_module("semantics_async", _ASYNC_GRAPH)
    outputs = ["scaled", "shifted", "plus_one", "doubled"]
    requests: list[tuple[dict[str, object], dict[str, object]]] = [
        ({"x": 2}, {}),
        ({"x": 2, "offset": 5}, {}),
        ({}, {"doubled": 10}),
        ({"x": 1}, {"plus_one": 0}),
    ]

    async def hamilton() -> list[dict[str, object]]:
        # Typed as the sync builder's `build`, which this one makes a coroutine.
        pending: object = async_driver.Builder().with_modules(module).build()
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
    assert expected[0] == {"doubled": 4, "plus_one": 5, "scaled": 16, "shifted": 20}
    for sync_nodes in ("thread", "inline"):
        builder = fnloom.Builder().with_modules(module)
        driver = builder.build_async(sync_nodes=sync_nodes)
        assert asyncio.run(runs(driver)) == expected
