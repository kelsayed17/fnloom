"""Each deliberate difference from Hamilton, asserted on both sides."""

from typing import Any

import pytest
from hamilton import driver as hamilton_driver
from hamilton.function_modifiers.base import InvalidDecoratorException
from harness import (
    Nodes,
    Outcome,
    load_fixture,
    nodes_fnloom,
    nodes_hamilton,
    run_fnloom,
    run_hamilton,
)

import fnloom
from helpers import make_module


@pytest.mark.deliberate("Q4")
def test_q4_unknown_input_name() -> None:
    """Hamilton ignores an input no node reads; fnloom refuses it."""
    module = load_fixture("chain")
    inputs = {"a": 1, "typo": 2}
    assert run_hamilton([module], ["c"], inputs) == Outcome({"c": 4}, None)
    assert run_fnloom([module], ["c"], inputs) == Outcome(None, "ValueError")


@pytest.mark.deliberate("Q5")
def test_q5_misspelled_override() -> None:
    """Hamilton ignores an override naming no node; fnloom refuses it."""
    module = load_fixture("chain")
    overrides = {"typo": 2}
    assert run_hamilton([module], ["c"], {"a": 1}, overrides) == Outcome({"c": 4}, None)
    assert run_fnloom([module], ["c"], {"a": 1}, overrides) == Outcome(
        None, "ValueError"
    )


@pytest.mark.deliberate("Q6")
def test_q6_bare_string_final_vars() -> None:
    """Hamilton splits "bc" into outputs "b" and "c"; fnloom raises `TypeError`."""
    module = load_fixture("chain")
    assert run_hamilton([module], "bc", {"a": 1}) == Outcome({"b": 2, "c": 4}, None)
    assert run_fnloom([module], "bc", {"a": 1}) == Outcome(None, "TypeError")


@pytest.mark.deliberate("Q7")
def test_q7_override_naming_an_input() -> None:
    """Hamilton refuses an input override unless the input is supplied too."""
    module = load_fixture("chain")
    overrides = {"a": 5}
    supplied = {"a": 1}
    assert run_hamilton([module], ["c"], supplied, overrides) == Outcome(
        {"c": 12}, None
    )
    assert run_fnloom([module], ["c"], supplied, overrides) == Outcome({"c": 12}, None)
    assert run_hamilton([module], ["c"], {}, overrides) == Outcome(None, "ValueError")
    assert run_fnloom([module], ["c"], {}, overrides) == Outcome({"c": 12}, None)


@pytest.mark.deliberate("Q25")
def test_q25_deep_chain_hamilton_recursion_error() -> None:
    """Hamilton's recursive walk overflows at 5,000 chained nodes; fnloom's does not."""
    depth = 5_000
    lines = ["def n0(a: int) -> int:\n    return a\n"]
    lines += [
        f"def n{i}(n{i - 1}: int) -> int:\n    return n{i - 1} + 1\n"
        for i in range(1, depth)
    ]
    module = make_module("differential_deep_chain", "\n".join(lines))
    last = f"n{depth - 1}"
    assert run_hamilton([module], [last], {"a": 0}) == Outcome(None, "RecursionError")
    assert run_fnloom([module], [last], {"a": 0}) == Outcome({last: depth - 1}, None)


@pytest.mark.deliberate("Q26")
def test_q26_execution_order() -> None:
    """Hamilton's order follows the requested outputs; fnloom's breaks ties by name."""
    module = load_fixture("branches")
    orders: dict[tuple[str, tuple[str, ...]], list[str]] = {}
    for runtime, run in (("hamilton", run_hamilton), ("fnloom", run_fnloom)):
        for request in (("y", "c"), ("c", "y")):
            module.CALLS.clear()
            assert run([module], request, {"x": 1}).error is None
            orders[runtime, request] = list(module.CALLS)
    by_name = ["a", "b", "c", "z", "y"]
    assert orders == {
        ("hamilton", ("y", "c")): ["z", "y", "a", "b", "c"],
        ("hamilton", ("c", "y")): by_name,
        ("fnloom", ("y", "c")): by_name,
        ("fnloom", ("c", "y")): by_name,
    }


@pytest.mark.deliberate("Q27")
def test_q27_cycle() -> None:
    """Hamilton overflows the stack on a cycle; fnloom raises a `ValueError`."""
    module = load_fixture("cycle")
    assert run_hamilton([module], ["p"], {}) == Outcome(None, "RecursionError")
    assert run_fnloom([module], ["p"], {}) == Outcome(None, "ValueError")


@pytest.mark.deliberate("Q27")
def test_q27_cycle_is_refused_at_build_time() -> None:
    """Hamilton builds a cyclic graph; fnloom's build raises, before any request."""
    module = load_fixture("cycle")
    assert nodes_hamilton([module]).error is None
    with pytest.raises(ValueError, match="dependency cycle"):
        fnloom.build(module)


@pytest.mark.deliberate("Q28")
def test_q28_lineage_queries_return_names() -> None:
    """Hamilton answers with its node objects; fnloom answers with sorted names."""
    module = load_fixture("chain")
    hamilton = hamilton_driver.Builder().with_modules(module).build()
    upstream = hamilton.what_is_upstream_of("c")
    assert {type(node).__name__ for node in upstream} == {"HamiltonNode"}
    assert sorted(node.name for node in upstream) == ["a", "b", "c"]
    assert fnloom.build(module).what_is_upstream_of("c") == ["a", "b", "c"]


@pytest.mark.deliberate("Q29")
@pytest.mark.parametrize(
    "source",
    [
        "def f(a: int, /) -> int:\n    return a\n",
        "def f(*a: int) -> int:\n    return 1\n",
        "def f(**a: int) -> int:\n    return 1\n",
    ],
    ids=["positional_only", "var_positional", "var_keyword"],
)
def test_q29_parameters_that_cannot_be_passed_by_keyword(source: str) -> None:
    """Hamilton treats such a parameter as an input; fnloom refuses the function."""
    module = make_module("differential_kinds", source)
    assert nodes_hamilton([module]) == Nodes(["a", "f"], None)
    assert nodes_fnloom([module]) == Nodes(None, "ValueError")


@pytest.mark.deliberate("Q32")
@pytest.mark.parametrize(
    ("source", "made"),
    [
        ("async def total(x: int) -> int:\n    return x\n", "coroutine"),
        ("def total(x: int) -> int:\n    yield x\n", "generator"),
    ],
    ids=["async", "generator"],
)
def test_q32_functions_that_do_not_return_their_value(source: str, made: str) -> None:
    """Hamilton returns the coroutine or generator as the value; fnloom refuses."""
    module = make_module("differential_lazy", source)
    hamilton = run_hamilton([module], ["total"], {"x": 1})
    assert hamilton.error is None
    assert hamilton.values is not None
    value = hamilton.values["total"]
    assert type(value).__name__ == made
    if made == "coroutine":
        value.close()  # ty: ignore[unresolved-attribute]  # silences "never awaited"
    assert nodes_fnloom([module]) == Nodes(None, "ValueError")


@pytest.mark.deliberate("Q30")
def test_q30_final_vars_given_as_functions() -> None:
    """Hamilton maps a function to its node; fnloom accepts only names for now."""
    module = load_fixture("chain")
    outputs: list[Any] = [module.c]
    assert run_hamilton([module], outputs, {"a": 1}) == Outcome({"c": 4}, None)
    assert run_fnloom([module], outputs, {"a": 1}) == Outcome(None, "TypeError")


@pytest.mark.deliberate("Q31")
def test_q31_inputs_and_overrides_are_keyword_only() -> None:
    """Hamilton reads a positional mapping as overrides; fnloom raises `TypeError`."""
    module = load_fixture("chain")
    hamilton = hamilton_driver.Builder().with_modules(module).build()
    assert hamilton.execute(["c"], {"b": 10}) == {"c": 20}
    with pytest.raises(TypeError):
        fnloom.build(module).execute(["c"], {"b": 10})  # ty: ignore[too-many-positional-arguments]  # the positional call is the point


@pytest.mark.deliberate("Q33")
def test_q33_row_naming_an_unknown_parameter() -> None:
    """Both refuse the row when decorating; only fnloom's error is a `ValueError`."""
    body = """
@parameterize(out={"missing": source("a")})
def f(x: int) -> int:
    return x
"""
    hamilton_header = "from hamilton.function_modifiers import parameterize, source\n"
    with pytest.raises(InvalidDecoratorException, match="missing") as hamilton:
        make_module("differential_unknown_h", hamilton_header + body)
    assert not isinstance(hamilton.value, ValueError)
    with pytest.raises(ValueError, match="'missing'"):
        make_module(
            "differential_unknown_f", "from fnloom import parameterize, source\n" + body
        )


@pytest.mark.deliberate("Q35")
def test_q35_defaulted_parameter_bound_to_an_unsupplied_source() -> None:
    """Hamilton raises `KeyError`; fnloom omits the keyword, so the default applies."""
    inputs = {"numbers": [1, 2, 3]}
    assert run_hamilton([load_fixture("rows_h")], ["shifted_total"], inputs) == (
        Outcome(None, "KeyError")
    )
    assert run_fnloom([load_fixture("rows_f")], ["shifted_total"], inputs) == (
        Outcome({"shifted_total": 106}, None)
    )
