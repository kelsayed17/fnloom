"""Each deliberate difference from Hamilton, asserted on both sides."""

import asyncio
import collections.abc
import importlib.util
import inspect
import json
import logging
import os
import pickle
import subprocess
import sys
import textwrap
import types
import warnings
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, TypedDict, cast, override

import pytest
from hamilton import async_driver as hamilton_async
from hamilton import driver as hamilton_driver
from hamilton import graph_types, htypes, lifecycle
from hamilton.function_modifiers import hamilton_exclude
from hamilton.function_modifiers.base import InvalidDecoratorException
from hamilton.lifecycle import NoEdgeAndInputTypeChecking
from harness import (
    FAKE_GRAPHVIZ,
    FIXTURES,
    Nodes,
    Outcome,
    load_fixture,
    nodes_fnloom,
    nodes_hamilton,
    run_fnloom,
    run_hamilton,
)
from test_semantics import (
    DELIBERATE,
    HOOKED_GRAPH,
    FnloomRecorder,
    FnloomSeen,
    HamiltonRecorder,
    HamiltonSeen,
    HookEntry,
)
from type_matrix import (
    TYPES,
    Base,
    Checkable,
    Child,
    fnloom_type,
    fnloom_value,
    hamilton_type,
    hamilton_value,
)

import fnloom
from fnloom import CacheOutcome, CacheWarning, DiskCache
from fnloom import _caching as fnloom_caching
from fnloom import _types as fnloom_types
from fnloom._types import type_matches, value_matches
from helpers import make_module

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Mapping
    from types import ModuleType


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
    """A function means the node it makes, by identity; Hamilton maps its `__name__`.

    They agree on a plain function. A `@parameterize` function makes several nodes:
    Hamilton asks for the node named like the function, which does not exist (H5.3b),
    and fnloom raises `TypeError` listing the rows. Hamilton accepts a function whose
    `__module__` and `__name__` are forged (H5.3e); fnloom matches the object itself.
    """
    module = load_fixture("chain")
    outputs: list[Any] = [module.c]
    assert run_hamilton([module], outputs, {"a": 1}) == Outcome({"c": 4}, None)
    assert run_fnloom([module], outputs, {"a": 1}) == Outcome({"c": 4}, None)

    hamilton_module = make_module(
        "differential_q30_h",
        """
        from hamilton.function_modifiers import parameterize, value


        @parameterize(p1={"y": value(1)}, p2={"y": value(2)})
        def rows(x: int, y: int) -> int:
            return x + y
        """,
    )
    fnloom_module = make_module(
        "differential_q30_f",
        """
        from fnloom import parameterize, value


        @parameterize(p1={"y": value(1)}, p2={"y": value(2)})
        def rows(x: int, y: int) -> int:
            return x + y
        """,
    )
    assert run_hamilton([hamilton_module], [hamilton_module.rows], {"x": 1}) == Outcome(
        None, "ValueError"
    )
    with pytest.raises(TypeError, match=r"makes 2 nodes: pass 'p1', 'p2'"):
        fnloom.build(fnloom_module).execute([fnloom_module.rows], inputs={"x": 1})
    assert run_fnloom([fnloom_module], ["p1", "p2"], {"x": 1}) == Outcome(
        {"p1": 2, "p2": 3}, None
    )

    def forged() -> int:
        return 0

    forged.__module__, forged.__name__ = module.__name__, "c"
    assert run_hamilton([module], [forged], {"a": 1}) == Outcome({"c": 4}, None)
    assert run_fnloom([module], [forged], {"a": 1}) == Outcome(None, "ValueError")


Q30_IMPORTS = {
    "hamilton": "from hamilton.function_modifiers import config, extract_fields",
    "fnloom": "from fnloom import config, extract_fields",
}

Q30_SOURCE = """
{imports}


def base(x: int) -> int:
    return x * 10


@extract_fields({{"f1": int, "f2": str}})
def produced(base: int) -> dict:
    return {{"f1": base, "f2": "s"}}


@config.when(k="a")
def variant__a(base: int) -> int:
    return base


@config.when(k="b")
def variant__b(base: int) -> int:
    return -base
"""


def _q30_pair(name: str) -> tuple[types.ModuleType, types.ModuleType]:
    """The same module written for Hamilton and for fnloom, config `k=a` active."""
    return (
        make_module(
            f"differential_q30_{name}_h",
            Q30_SOURCE.format(imports=Q30_IMPORTS["hamilton"]),
        ),
        make_module(
            f"differential_q30_{name}_f",
            Q30_SOURCE.format(imports=Q30_IMPORTS["fnloom"]),
        ),
    )


def _q30_outcomes(
    hamilton_module: types.ModuleType,
    fnloom_module: types.ModuleType,
    outputs: tuple[str, ...],
    stray: Callable[..., object] | None = None,
) -> tuple[Outcome, Outcome]:
    """Runs `module.<name>` for each name, or `stray`, on both, with `k=a`."""

    def pick(module: types.ModuleType) -> list[Any]:
        return [stray if stray is not None else getattr(module, n) for n in outputs]

    hamilton = hamilton_driver.Builder().with_modules(hamilton_module)
    fnloom_builder = fnloom.Builder().with_modules(fnloom_module)
    results: list[Outcome] = []
    for builder, module in (
        (hamilton.with_config({"k": "a"}), hamilton_module),
        (fnloom_builder.with_config({"k": "a"}), fnloom_module),
    ):
        try:
            built = builder.build()
            result = built.execute(pick(module), inputs={"x": 1})
        except Exception as error:  # noqa: BLE001  # the exception class is compared
            results.append(Outcome(None, type(error).__name__))
        else:
            results.append(Outcome(dict(result), None))
    return results[0], results[1]


@pytest.mark.deliberate("Q30")
def test_q30_an_extract_function_is_its_producer_on_both_sides() -> None:
    """Both map an `@extract_fields` function to its producer (H5.3c)."""
    hamilton, ours = _q30_outcomes(*_q30_pair("extract"), ("produced",))
    assert hamilton == Outcome({"produced": {"f1": 10, "f2": "s"}}, None)
    assert ours == hamilton


@pytest.mark.deliberate("Q30")
def test_q30_functions_no_node_comes_from_raise_value_error_on_both_sides() -> None:
    """A foreign function, a lambda and an unselected variant: `ValueError` each.

    Hamilton's message is wrong ("materializer", H5.3e) or unhelpful, and its
    unselected variant is "Unknown nodes" (H5.3d); fnloom names the function and why.
    """
    hamilton_module, fnloom_module = _q30_pair("absent")
    foreign = make_module(
        "differential_q30_foreign", "def stray(x: int) -> int:\n    return x\n"
    )
    for stray in (foreign.stray, lambda: 1):
        hamilton, ours = _q30_outcomes(hamilton_module, fnloom_module, ("base",), stray)
        assert hamilton == Outcome(None, "ValueError")
        assert ours == hamilton
    hamilton, ours = _q30_outcomes(hamilton_module, fnloom_module, ("variant__b",))
    assert hamilton == Outcome(None, "ValueError")
    assert ours == hamilton


@pytest.mark.deliberate("Q30")
def test_q30_a_submodule_function_is_refused_by_hamilton_and_found_by_identity() -> (
    None
):
    """Hamilton refuses a function of a submodule of a package it was given (H5.3f).

    fnloom has no package-wide discovery: it matches the function against the nodes of
    the module it was given, and accepts it when that module is the submodule.
    """
    sub = make_module("q30pkg.sub", "def from_sub(x: int) -> int:\n    return x + 1\n")
    package = make_module("q30pkg", "from q30pkg.sub import from_sub\n")
    assert run_hamilton([package], [sub.from_sub], {"x": 1}) == Outcome(
        None, "ValueError"
    )
    assert run_hamilton([package], ["from_sub"], {"x": 1}) == Outcome(
        {"from_sub": 2}, None
    )
    assert run_fnloom([sub], [sub.from_sub], {"x": 1}) == Outcome({"from_sub": 2}, None)
    # The same function, through a package that holds only an import of it, is no node.
    with pytest.raises(ValueError, match="has no nodes"):
        fnloom.build(package)


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


@pytest.mark.deliberate("Q34")
def test_tags_match_hamilton_except_module() -> None:
    """Hamilton tags each function node with its module; fnloom does not.

    fnloom's tags are only the author's, and inputs carry no tags in either.
    """
    tags_h, tags_f = load_fixture("tags_h"), load_fixture("tags_f")
    hamilton = {
        node.name: node.tags
        for node in hamilton_driver.Builder()
        .with_modules(tags_h)
        .build()
        .list_available_variables()
    }
    views = fnloom.build(tags_f).nodes
    assert sorted(hamilton) == list(views)
    for name, view in views.items():
        if view.is_input:
            assert hamilton[name] == {}, name
            assert dict(view.tags) == {}, name
            assert view.module is None, name
        else:
            assert hamilton[name] == {**view.tags, "module": tags_h.__name__}, name
            assert view.module == tags_f.__name__, name
    assert dict(views["low"].tags) == {"owner": "limits"}


@pytest.mark.deliberate("Q36")
def test_q36_list_valued_tag() -> None:
    """Hamilton accepts a list of strings as a tag value; fnloom raises `TypeError`."""
    body = """
@tag(kind=["a", "b"])
def f(x: int) -> int:
    return x
"""
    hamilton_module = make_module(
        "differential_list_tag_h",
        "from hamilton.function_modifiers import tag\n" + body,
    )
    built = hamilton_driver.Builder().with_modules(hamilton_module).build()
    (node,) = (n for n in built.list_available_variables() if n.name == "f")
    assert node.tags["kind"] == ["a", "b"]
    with pytest.raises(TypeError, match="'kind'"):
        make_module("differential_list_tag_f", "from fnloom import tag\n" + body)


def _pairs(quirk: str) -> list[tuple[str, str]]:
    return [pair for pair, row in DELIBERATE.items() if row == quirk]


@pytest.mark.deliberate("Q37")
def test_q37_homogeneous_tuple() -> None:
    """Hamilton refuses `(1, 2)` for `tuple[int, ...]` and its `Sequence[int]` edge."""
    assert hamilton_value("tuple[int, ...]", "(1, 2)") is False
    assert fnloom_value("tuple[int, ...]", "(1, 2)") is True
    assert hamilton_type("tuple[int, ...]", "Sequence[int]") is False
    assert fnloom_type("tuple[int, ...]", "Sequence[int]") is True
    assert _pairs("Q37") == [
        ("tuple[int, ...]", "(1, 2)"),
        ("tuple[int, ...]", "Sequence[int]"),
    ]


@pytest.mark.deliberate("Q38")
def test_q38_new_type_answers_as_its_supertype() -> None:
    """Hamilton rejects each value of a `NewType` over `int` and each edge from it."""
    for value in ("1", "True"):
        assert hamilton_value("NewType(int)", value) is False, value
        assert fnloom_value("NewType(int)", value) is True, value
    edges = [pair for pair in _pairs("Q38") if pair[1] in TYPES]
    assert [required for _, required in edges] == [
        "int",
        "object",
        "int | None",
        "Optional[int]",
        "int | str",
        "Annotated[int, 'm']",
    ]
    for produced, required in edges:
        assert hamilton_type(produced, required) is False, required
        assert fnloom_type(produced, required) is True, required


@pytest.mark.deliberate("Q39")
def test_q39_literal_producer_answers_as_its_values_types() -> None:
    """Hamilton refuses a `Literal[1, 2]` node feeding `int` and every type over it."""
    edges = _pairs("Q39")
    assert [required for _, required in edges] == [
        "int",
        "object",
        "int | None",
        "Optional[int]",
        "int | str",
        "Annotated[int, 'm']",
    ]
    for produced, required in edges:
        assert hamilton_type(produced, required) is False, required
        assert fnloom_type(produced, required) is True, required
    assert hamilton_type("Literal[1, 2]", "str") is False
    assert fnloom_type("Literal[1, 2]", "str") is False


@pytest.mark.deliberate("Q40")
def test_q40_union_producer_feeds_object() -> None:
    """Hamilton refuses a union producer feeding `object`; fnloom checks each member."""
    edges = _pairs("Q40")
    assert edges == [
        ("int | None", "object"),
        ("Optional[int]", "object"),
        ("int | str", "object"),
        ("type[int] | None", "object"),
    ]
    for produced, required in edges:
        assert hamilton_type(produced, required) is False, produced
        assert fnloom_type(produced, required) is True, produced
    assert hamilton_type("int | None", "int") is False
    assert fnloom_type("int | None", "int") is False


@pytest.mark.deliberate("Q41")
def test_q41_annotated_is_checked_as_its_type() -> None:
    """Hamilton checks an `Annotated` input by exact type; fnloom as the bare type."""
    assert hamilton_value("int", "True") is True
    assert hamilton_value("Annotated[int, 'm']", "True") is False
    assert fnloom_value("Annotated[int, 'm']", "True") is True
    declared = Annotated[Base, "m"]
    assert htypes.check_input_type(declared, Child()) is False  # ty: ignore[invalid-argument-type]  # Hamilton's hint is narrower than what it accepts
    assert value_matches(declared, Child()) is True
    assert _pairs("Q41") == [("Annotated[int, 'm']", "True")]


class _HasSize:
    """Conforms to `Checkable` without naming it."""

    def size(self) -> int:
        """Always 1."""
        return 1


@pytest.mark.deliberate("Q42")
def test_q42_protocols() -> None:
    """Hamilton raises `TypeError` on a plain-protocol edge; it rejects a conformer."""
    edges = _pairs("Q42")
    assert [produced for produced, _ in edges] == [
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
    for produced, required in edges:
        assert hamilton_type(produced, required) == "TypeError", produced
        assert fnloom_type(produced, required) is False, produced
    assert hamilton_type("Protocol", "Protocol") is True
    assert fnloom_type("Protocol", "Protocol") is True
    assert htypes.check_input_type(Checkable, _HasSize()) is False
    assert value_matches(Checkable, _HasSize()) is True
    assert htypes.types_match(Checkable, _HasSize) is True  # ty: ignore[invalid-argument-type]  # Hamilton's hint is narrower than what it accepts
    assert type_matches(_HasSize, Checkable) is True


@pytest.mark.deliberate("Q43")
def test_q43_an_iterator_is_not_consumed_by_the_input_check() -> None:
    """Hamilton reads an iterator's first item to check it; fnloom does not."""
    declared = collections.abc.Iterable[int]
    hamilton_ok = (n for n in (1, 2, 3))
    assert htypes.check_input_type(declared, hamilton_ok) is True
    assert list(hamilton_ok) == [2, 3]
    hamilton_bad = iter(["s", 2])
    assert htypes.check_input_type(declared, hamilton_bad) is False
    assert list(hamilton_bad) == [2]
    fnloom_ok = (n for n in (1, 2, 3))
    assert value_matches(declared, fnloom_ok) is True
    assert list(fnloom_ok) == [1, 2, 3]
    fnloom_unread = iter(["s", 2])
    assert value_matches(declared, fnloom_unread) is True
    assert list(fnloom_unread) == ["s", 2]


@pytest.mark.deliberate("Q22")
def test_q22_override_type_checked() -> None:
    """Hamilton runs a node on an override of the wrong type; fnloom refuses first."""
    module = load_fixture("chain")
    overrides = {"b": "x"}
    assert run_hamilton([module], ["c"], {}, overrides) == Outcome({"c": "xx"}, None)
    assert run_fnloom([module], ["c"], {}, overrides) == Outcome(None, "ValueError")


@pytest.mark.deliberate("Q10")
def test_q10_opt_out_is_explicit() -> None:
    """Hamilton's opt-out also drops edge checks; fnloom's keeps them."""
    mismatched = make_module(
        "differential_opt_out_edge",
        """
def text() -> str:
    return "x"

def uses_text(text: int) -> str:
    return text
""",
    )
    hamilton = (
        hamilton_driver.Builder()
        .with_modules(mismatched)
        .with_adapters(NoEdgeAndInputTypeChecking())
        .build()
    )
    assert hamilton.execute(["uses_text"]) == {"uses_text": "x"}
    unchecked = fnloom.Builder().with_modules(mismatched).without_input_type_checks()
    with pytest.raises(ValueError, match=r"uses_text\.text requires int"):
        unchecked.build()
    chain = load_fixture("chain")
    hamilton_chain = (
        hamilton_driver.Builder()
        .with_modules(chain)
        .with_adapters(NoEdgeAndInputTypeChecking())
        .build()
    )
    fnloom_chain = (
        fnloom.Builder().with_modules(chain).without_input_type_checks().build()
    )
    assert hamilton_chain.execute(["b"], inputs={"a": 1.5}) == {"b": 2.5}
    assert fnloom_chain.execute(["b"], inputs={"a": 1.5}) == {"b": 2.5}


def _hamilton_fits(declared: object, value: object) -> bool:
    """Hamilton's input check of `value` against `declared`."""
    return htypes.check_input_type(declared, value)  # ty: ignore[invalid-argument-type]  # Hamilton's hint is narrower than what it accepts


def _hamilton_feeds(produced: object, required: object) -> bool:
    """Hamilton's edge check of a `produced` node feeding a `required` parameter."""
    return htypes.types_match(required, produced)  # ty: ignore[invalid-argument-type]  # Hamilton's hint is narrower than what it accepts


ALIASES = """
type Price = float

def base() -> float:
    return 1.5

def priced(base: Price) -> Price:
    return base * 2

def doubled(priced: float) -> float:
    return priced * 2

def total(given: Price) -> float:
    return given + 1
"""


@pytest.mark.deliberate("Q44")
def test_q44_type_alias_is_checked_as_what_it_names() -> None:
    """Hamilton refuses a `float` node feeding `Price` and a `float` for a `Price`."""
    module = make_module("differential_alias", ALIASES)
    edges = run_hamilton([module], ["doubled"], {})
    assert edges == Outcome(None, "ValueError")
    assert run_fnloom([module], ["doubled"], {}) == Outcome({"doubled": 6.0}, None)
    unchecked = (
        hamilton_driver.Builder()
        .with_modules(module)
        .with_adapters(NoEdgeAndInputTypeChecking())
        .build()
    )
    assert unchecked.execute(["doubled"]) == {"doubled": 6.0}
    price = vars(module)["Price"]
    assert _hamilton_fits(price, 1.5) is False
    assert _hamilton_feeds(float, price) is False
    assert _hamilton_feeds(price, float) is False
    assert value_matches(price, 1.5) is True
    assert type_matches(float, price) is True
    assert type_matches(price, float) is True
    assert fnloom.build(module).execute(["total"], inputs={"given": 1.5}) == {
        "total": 2.5
    }


def _square(x: int) -> int:
    return x * x


@pytest.mark.deliberate("Q45")
def test_q45_mapping_and_callable_are_checked_by_kind() -> None:
    """Hamilton refuses a dict for `Mapping[str, int]`, a function for `Callable`."""
    mapping = collections.abc.Mapping[str, int]
    callable_ = collections.abc.Callable[[int], int]
    assert _hamilton_fits(mapping, {"a": 1}) is False
    assert _hamilton_fits(callable_, _square) is False
    assert value_matches(mapping, {"a": 1}) is True
    assert value_matches(callable_, _square) is True
    assert value_matches(mapping, [("a", 1)]) is False
    assert _hamilton_feeds(dict[str, int], mapping) is True
    assert type_matches(dict[str, int], mapping) is True
    module = make_module(
        "differential_mapping_input",
        """
from collections.abc import Callable, Mapping

def applied(table: Mapping[str, int], fn: Callable[[int], int]) -> int:
    return fn(table["a"])
""",
    )
    inputs = {"table": {"a": 3}, "fn": _square}
    assert run_hamilton([module], ["applied"], inputs) == Outcome(None, "ValueError")
    assert run_fnloom([module], ["applied"], inputs) == Outcome({"applied": 9}, None)


def _file_module(directory: Path, name: str, source: str) -> ModuleType:
    """Writes `source` to `<directory>/<name>.py` and imports it as module `name`.

    Hamilton versions a node by `inspect.getsource`, which needs a file: a module
    built by `make_module` has none, so Hamilton would version every node as `None`.
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.py"
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        msg = f"cannot load {path}"
        raise ImportError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _hamilton_cached(
    module: ModuleType,
    path: Path,
    *,
    recompute: list[str] | None = None,
    ignore: list[str] | None = None,
) -> hamilton_driver.Driver:
    """A Hamilton driver over `module` caching under `path`, with per-node behaviors."""
    return (
        hamilton_driver.Builder()
        .with_modules(module)
        .with_cache(path=path, recompute=recompute, ignore=ignore)
        .build()
    )


def _fnloom_cached(module: ModuleType, path: Path) -> fnloom.Driver:
    """A fnloom driver over `module` caching in a `DiskCache` under `path`."""
    return fnloom.Builder().with_modules(module).with_cache(DiskCache(path)).build()


@pytest.mark.deliberate("Q16")
def test_q16_code_versions_are_made_once_at_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hamilton reads every node's source on every run; fnloom reads code at build."""
    module = _file_module(
        tmp_path / "code",
        "differential_cache_source",
        """
        def a(x: int) -> int:
            return x + 1

        def b(a: int) -> int:
            return a * 2
        """,
    )
    read: list[object] = []
    getsource = inspect.getsource

    def counting_getsource(obj: Any) -> str:  # noqa: ANN401  # wraps `inspect.getsource`
        read.append(obj)
        return getsource(obj)

    monkeypatch.setattr(inspect, "getsource", counting_getsource)
    versioned: list[object] = []
    code_version = fnloom_caching.code_version

    def counting_code_version(fn: Callable[..., object]) -> bytes:
        versioned.append(fn)
        return code_version(fn)

    monkeypatch.setattr(fnloom_caching, "code_version", counting_code_version)
    hamilton = _hamilton_cached(module, tmp_path / "hamilton")
    for _ in range(2):
        read.clear()
        assert hamilton.execute(["b"], inputs={"x": 1}) == {"b": 4}
        # Each node's source, `a` and `b`, read again by the run.
        assert sorted(fn.__name__ for fn in read) == ["a", "b"]  # ty: ignore[unresolved-attribute]  # each one is a node's function
    read.clear()
    driver = _fnloom_cached(module, tmp_path / "fnloom")
    assert sorted(fn.__name__ for fn in versioned) == ["a", "b"]  # ty: ignore[unresolved-attribute]  # each one is a node's function
    versioned.clear()
    for _ in range(2):
        assert driver.execute(["b"], inputs={"x": 1}) == {"b": 4}
    assert read == []
    assert versioned == []


@pytest.mark.deliberate("Q46")
def test_q46_equal_hashes_never_share_an_entry(tmp_path: Path) -> None:
    """Hamilton serves `1` for a node returning `"1"`; fnloom keys by node (F1, F2)."""
    module = _file_module(
        tmp_path / "code",
        "differential_cache_equal",
        """
        def a() -> int:
            return 1

        def b() -> str:
            return "1"
        """,
    )
    hamilton = _hamilton_cached(module, tmp_path / "hamilton")
    assert hamilton.execute(["a", "b"]) == {"a": 1, "b": "1"}
    assert hamilton.execute(["a", "b"]) == {"a": 1, "b": 1}
    driver = _fnloom_cached(module, tmp_path / "fnloom")
    assert driver.execute(["a", "b"]) == {"a": 1, "b": "1"}
    second = driver.execute(["a", "b"])
    assert second == {"a": 1, "b": "1"}
    assert type(second["b"]) is str


@pytest.mark.deliberate("Q47")
def test_q47_a_nested_unhashable_input_is_not_a_constant(tmp_path: Path) -> None:
    """Hamilton hits on a list whose `__slots__` item changed; fnloom reruns (F3)."""
    module = _file_module(
        tmp_path / "code",
        "differential_cache_slots",
        """
        class Box:
            __slots__ = ("size",)

            def __init__(self, size: int) -> None:
                self.size = size

        def first_size(boxes: list) -> int:
            return boxes[0].size
        """,
    )
    box_type = vars(module)["Box"]
    driver = _fnloom_cached(module, tmp_path / "fnloom")
    runs = {
        "hamilton": _hamilton_cached(module, tmp_path / "hamilton").execute,
        "fnloom": driver.execute,
    }
    seen: dict[str, list[object]] = {}
    for runtime, execute in runs.items():
        box = box_type(1)
        first = execute(["first_size"], inputs={"boxes": [box]})["first_size"]
        box.size = 2
        second = execute(["first_size"], inputs={"boxes": [box]})["first_size"]
        seen[runtime] = [first, second]
    assert seen == {"hamilton": [1, 1], "fnloom": [1, 2]}
    outcome = driver.explain_cache(["first_size"], inputs={"boxes": [box_type(1)]})
    assert outcome["first_size"].status == "uncacheable"
    assert "boxes" in (outcome["first_size"].reason or "")


HELPER = """
C = {constant}

def _offset(x: int) -> int:
    return x + {offset}

def shifted(x: int) -> int:
    return _offset(x)

def scaled(x: int) -> int:
    return x * C
"""


@pytest.mark.deliberate("Q48")
def test_q48_an_edited_helper_or_constant_changes_the_key(tmp_path: Path) -> None:
    """Hamilton versions only the node's own source, so misses these edits (F4)."""
    seen: dict[str, list[object]] = {"hamilton": [], "fnloom": []}
    edits = ((1, 1), (100, 1), (100, 100))
    for edit, (offset, constant) in enumerate(edits):
        module = _file_module(
            tmp_path / f"edit{edit}",
            "differential_cache_helper",
            HELPER.format(offset=offset, constant=constant),
        )
        hamilton = _hamilton_cached(module, tmp_path / "hamilton")
        driver = _fnloom_cached(module, tmp_path / "fnloom")
        for runtime, execute in (
            ("hamilton", hamilton.execute),
            ("fnloom", driver.execute),
        ):
            result = execute(["shifted", "scaled"], inputs={"x": 2})
            seen[runtime].append((result["shifted"], result["scaled"]))
    assert seen == {
        "hamilton": [(3, 2), (3, 2), (3, 2)],
        "fnloom": [(3, 2), (102, 2), (102, 200)],
    }


@pytest.mark.deliberate("Q50")
def test_q50_a_client_is_never_pickled(tmp_path: Path) -> None:
    """Hamilton's `ignore` still pickles a lock; fnloom's `disable` never stores it."""
    hamilton_module = _file_module(
        tmp_path / "code",
        "differential_cache_lock_h",
        """
        import threading

        def lock() -> threading.Lock:
            return threading.Lock()

        def guarded(lock: threading.Lock, n: int) -> int:
            return n + 1
        """,
    )
    hamilton = _hamilton_cached(hamilton_module, tmp_path / "hamilton", ignore=["lock"])
    with pytest.raises(TypeError, match="pickle"):
        hamilton.execute(["guarded"], inputs={"n": 1})
    # Hamilton's `ignore` on a producer also drops it from every consumer's key.
    chain = _file_module(
        tmp_path / "code",
        "differential_cache_ignored",
        """
        def p(x: int) -> int:
            return x

        def q(p: int) -> int:
            return p * 10
        """,
    )
    ignoring = _hamilton_cached(chain, tmp_path / "hamilton_chain", ignore=["p"])
    assert ignoring.execute(["q"], inputs={"x": 1}) == {"q": 10}
    assert ignoring.execute(["q"], inputs={"x": 2}) == {"q": 10}
    # fnloom's `ignore` is a consumer's, per parameter, so `q` still reads `p`.
    chained = _fnloom_cached(chain, tmp_path / "fnloom_chain")
    assert chained.execute(["q"], inputs={"x": 1}) == {"q": 10}
    assert chained.execute(["q"], inputs={"x": 2}) == {"q": 20}
    # Only fnloom builds this twin, and it versions code objects, not source: no file.
    fnloom_module = make_module(
        "differential_cache_lock_f",
        """
        import threading

        from fnloom import cache

        @cache(behavior="disable")
        def lock() -> threading.Lock:
            return threading.Lock()

        @cache(ignore=("lock",))
        def guarded(lock: threading.Lock, n: int) -> int:
            return n + 1
        """,
    )
    driver = _fnloom_cached(fnloom_module, tmp_path / "fnloom")
    assert driver.execute(["guarded"], inputs={"n": 1}) == {"guarded": 2}
    assert driver.explain_cache(["guarded"], inputs={"n": 1}) == {
        "guarded": CacheOutcome("hit", None),
        "lock": CacheOutcome("uncacheable", "policy disable"),
    }
    assert driver.execute(["guarded"], inputs={"n": 1}) == {"guarded": 2}


def _truncate_entries(directory: Path) -> int:
    """Cuts every stored result under `directory` to half its length; how many."""
    entries = [p for p in directory.rglob("*") if p.is_file() and p.suffix != ".db"]
    for entry in entries:
        data = entry.read_bytes()
        entry.write_bytes(data[: len(data) // 2])
    return len(entries)


@pytest.mark.deliberate("Q51")
def test_q51_a_truncated_entry_is_a_miss(tmp_path: Path) -> None:
    """Hamilton's run raises on a truncated entry; fnloom's recomputes and warns."""
    module = _file_module(
        tmp_path / "code",
        "differential_cache_truncated",
        """
        def numbers(n: int) -> list[int]:
            return list(range(n))
        """,
    )
    inputs = {"n": 50}
    expected = {"numbers": list(range(50))}
    hamilton = _hamilton_cached(module, tmp_path / "hamilton")
    assert hamilton.execute(["numbers"], inputs=inputs) == expected
    assert _truncate_entries(tmp_path / "hamilton") == 1
    with pytest.raises(pickle.UnpicklingError, match="truncated"):
        _hamilton_cached(module, tmp_path / "hamilton").execute(
            ["numbers"], inputs=inputs
        )
    driver = _fnloom_cached(module, tmp_path / "fnloom")
    assert driver.execute(["numbers"], inputs=inputs) == expected
    assert _truncate_entries(tmp_path / "fnloom") == 1
    with pytest.warns(CacheWarning, match="removed unreadable cache entry"):
        assert driver.execute(["numbers"], inputs=inputs) == expected
    assert driver.explain_cache(["numbers"], inputs=inputs) == {
        "numbers": CacheOutcome("hit", None)
    }


def _plain_function(a: int) -> int:
    return a


@pytest.mark.deliberate("Q52")
def test_q52_a_function_passed_as_a_module() -> None:
    """Hamilton builds nothing from a function, failing at execute; fnloom refuses."""
    assert nodes_hamilton([_plain_function]) == Nodes([], None)  # ty: ignore[invalid-argument-type]  # the wrong type is the point
    assert run_hamilton([_plain_function], ["_plain_function"], {"a": 1}) == Outcome(  # ty: ignore[invalid-argument-type]  # the wrong type is the point
        None, "ValueError"
    )
    assert nodes_fnloom([_plain_function]) == Nodes(None, "TypeError")  # ty: ignore[invalid-argument-type]  # the wrong type is the point
    with pytest.raises(TypeError, match=r"^expected a module, got function"):
        fnloom.Builder().with_modules(_plain_function)  # ty: ignore[invalid-argument-type]  # the wrong type is the point


@pytest.mark.deliberate("Q53")
def test_q53_a_module_with_no_node() -> None:
    """Hamilton builds a re-exporting module as empty; fnloom refuses it, saying why."""
    make_module("q53_impl", "def total(a: int) -> int:\n    return a\n")
    reexports = make_module("q53_reexports", "from q53_impl import total\n")
    assert nodes_hamilton([reexports]) == Nodes([], None)
    assert run_hamilton([reexports], ["total"], {"a": 1}) == Outcome(None, "ValueError")
    assert nodes_fnloom([reexports]) == Nodes(None, "ValueError")
    with pytest.raises(
        ValueError,
        match=(
            r"^module 'q53_reexports' has no nodes: "
            r"'total' is defined in 'q53_impl'$"
        ),
    ):
        fnloom.build(reexports)


@pytest.mark.deliberate("Q53")
def test_q53_a_hand_built_module() -> None:
    """A `ModuleType` holding another module's function is empty in Hamilton too."""
    handmade = types.ModuleType("q53_handmade")
    handmade.f = _plain_function  # ty: ignore[unresolved-attribute]  # a hand-built module
    assert nodes_hamilton([handmade]) == Nodes([], None)
    with pytest.raises(
        ValueError, match=r"^module 'q53_handmade' has no nodes: 'f' is defined in "
    ):
        fnloom.build(handmade)


# A node whose dict holds a tuple, stored as JSON (Hamilton's JSON saver takes dicts);
# `{decorator}` imports each library's `cache`.
_PAIR_AS_JSON = """
{decorator}

@cache(format="json")
def pair(x: int) -> dict[str, tuple[int, int]]:
    return {{"xy": (x, x + 1)}}
"""


@pytest.mark.deliberate("Q54")
def test_q54_json_never_serves_a_changed_value(tmp_path: Path) -> None:
    """Hamilton's JSON hit turns a tuple into a list; fnloom never stores the tuple."""
    hamilton_module = _file_module(
        tmp_path / "code",
        "differential_json_pair_h",
        _PAIR_AS_JSON.format(decorator="from hamilton.function_modifiers import cache"),
    )
    hamilton = _hamilton_cached(hamilton_module, tmp_path / "hamilton")
    assert hamilton.execute(["pair"], inputs={"x": 1}) == {"pair": {"xy": (1, 2)}}
    assert hamilton.execute(["pair"], inputs={"x": 1}) == {"pair": {"xy": [1, 2]}}
    fnloom_module = _file_module(
        tmp_path / "code",
        "differential_json_pair_f",
        _PAIR_AS_JSON.format(decorator="from fnloom import cache"),
    )
    driver = _fnloom_cached(fnloom_module, tmp_path / "fnloom")
    for _ in range(2):
        with pytest.warns(CacheWarning, match=r"json cannot store a tuple at \['xy'\]"):
            assert driver.execute(["pair"], inputs={"x": 1}) == {"pair": {"xy": (1, 2)}}


@pytest.mark.deliberate("Q55")
def test_q55_an_unknown_format_is_refused_at_build(tmp_path: Path) -> None:
    """Hamilton fails each run after the node ran; fnloom refuses to build."""
    source = """
        {decorator}

        @cache(format="nosuch")
        def b(x: int) -> int:
            return x
        """
    hamilton_module = _file_module(
        tmp_path / "code",
        "differential_unknown_format_h",
        source.format(decorator="from hamilton.function_modifiers import cache"),
    )
    hamilton = _hamilton_cached(hamilton_module, tmp_path / "hamilton")
    for _ in range(2):
        with pytest.raises(KeyError, match="nosuch"):
            hamilton.execute(["b"], inputs={"x": 1})
    fnloom_module = _file_module(
        tmp_path / "code",
        "differential_unknown_format_f",
        source.format(decorator="from fnloom import cache"),
    )
    with pytest.raises(ValueError, match="'nosuch', which the store does not know"):
        _fnloom_cached(fnloom_module, tmp_path / "fnloom")


# Each library's config decorator, put in a fixture module's source as `{decorator}`.
_HAMILTON_CONFIG = "from hamilton.function_modifiers import config"
_FNLOOM_CONFIG = "from fnloom import config"


def _hamilton_configured(
    settings: dict[str, object], *modules: ModuleType
) -> hamilton_driver.Driver:
    return (
        hamilton_driver.Builder().with_config(settings).with_modules(*modules).build()
    )


def _fnloom_configured(
    settings: dict[str, object], *modules: ModuleType
) -> fnloom.Driver:
    return fnloom.Builder().with_config(settings).with_modules(*modules).build()


def _hamilton_names(built: hamilton_driver.Driver) -> list[str]:
    return sorted(v.name for v in built.list_available_variables())


@pytest.mark.deliberate("Q56")
def test_q56_config_is_never_a_node_or_an_input() -> None:
    """Hamilton passes config to parameters and drops a function a key names."""
    source = """
def usecfg(mode: str) -> str:
    return "mode=" + mode

def a() -> int:
    return 1

def b(a: int) -> int:
    return a * 2
"""
    module = make_module("differential_q56", source)
    settings: dict[str, object] = {"mode": "fast", "a": 50}
    hamilton = _hamilton_configured(settings, module)
    assert _hamilton_names(hamilton) == ["a", "b", "mode", "usecfg"]
    assert hamilton.execute(["usecfg", "b", "mode"]) == {
        "usecfg": "mode=fast",
        "b": 100,
        "mode": "fast",
    }
    with pytest.raises(ValueError, match="present in both config and inputs"):
        hamilton.execute(["usecfg"], inputs={"mode": "slow"})
    driver = _fnloom_configured(settings, module)
    assert driver.node_names() == ["a", "b", "mode", "usecfg"]
    assert driver.nodes["mode"].is_input
    assert not driver.nodes["a"].is_input
    assert driver.execute(["usecfg", "b"], inputs={"mode": "slow"}) == {
        "usecfg": "mode=slow",
        "b": 2,
    }
    with pytest.raises(ValueError, match=r"'mode' is a config key; config is not"):
        driver.execute(["usecfg"])


_VARIANTS = """
{decorator}

@config.when(mode="a")
def foo__a(x: int) -> int:
    return x + 1

@config.when(mode="b")
def foo__b(x: int) -> int:
    return x + 100

@config.when_not(mode="a")
def other__not_a(x: int) -> int:
    return x

def bar(foo: int) -> int:
    return foo + 1
"""


@pytest.mark.deliberate("Q57")
def test_q57_a_missing_key_and_a_missing_variant() -> None:
    """Hamilton matches `when_not` on a missing key and fails late without a variant."""
    hamilton_module = make_module(
        "differential_q57_h", _VARIANTS.format(decorator=_HAMILTON_CONFIG)
    )
    fnloom_module = make_module(
        "differential_q57_f", _VARIANTS.format(decorator=_FNLOOM_CONFIG)
    )
    hamilton_empty = _hamilton_configured({}, hamilton_module)
    assert "other" in _hamilton_names(hamilton_empty)
    unmatched = _hamilton_configured({"mode": "c"}, hamilton_module)
    assert "foo" in _hamilton_names(unmatched)
    with pytest.raises(ValueError, match=r"Required input foo not provided"):
        unmatched.execute(["bar"], inputs={"x": 1})
    with pytest.raises(
        ValueError,
        match=(
            r"^no variant of 'foo' matches the config \{\}; its variants need "
            r"mode == 'a' \(foo__a\), mode == 'b' \(foo__b\); "
            r"add @config\.default or a variant for it$"
        ),
    ):
        _fnloom_configured({}, fnloom_module)
    with pytest.raises(ValueError, match=r"^no variant of 'foo' matches the config"):
        _fnloom_configured({"mode": "c"}, fnloom_module)
    without_bar = make_module(
        "differential_q57_f_alone",
        _VARIANTS.format(decorator=_FNLOOM_CONFIG).replace(
            "def bar(foo: int)", "def bar(x: int)"
        ),
    )
    assert _fnloom_configured({}, without_bar).node_names() == ["bar", "x"]


_ROWS = """
{decorator}
{rows}

@parameterize(r1={{"k": value(1)}}, r2={{"k": value(2)}})
@config.when(mode="a")
def rows__a(k: int) -> int:
    return k

@parameterize(r1={{"k": value(10)}})
@config.when_not(mode="a")
def rows__other(k: int) -> int:
    return k

def total(r1: int, r2: int) -> int:
    return r1 + r2
"""


@pytest.mark.deliberate("Q57")
def test_q57_a_name_only_an_unselected_variant_gives() -> None:
    """Hamilton makes a row only the unselected variant has an input; fnloom refuses."""
    hamilton_module = make_module(
        "differential_q57_rows_h",
        _ROWS.format(
            decorator=_HAMILTON_CONFIG,
            rows="from hamilton.function_modifiers import parameterize, value",
        ),
    )
    hamilton = _hamilton_configured({"mode": "z"}, hamilton_module)
    assert _hamilton_names(hamilton) == ["mode", "r1", "r2", "total"]
    with pytest.raises(ValueError, match=r"Required input r2 not provided"):
        hamilton.execute(["total"])
    assert hamilton.execute(["total"], inputs={"r2": 5}) == {"total": 15}
    fnloom_module = make_module(
        "differential_q57_rows_f",
        _ROWS.format(
            decorator=_FNLOOM_CONFIG, rows="from fnloom import parameterize, value"
        ),
    )
    with pytest.raises(
        ValueError,
        match=(
            r"^no selected variant gives 'r2': the config \{'mode': 'z'\} selects "
            r"rows__other for 'rows', and only rows__a gives 'r2'$"
        ),
    ):
        _fnloom_configured({"mode": "z"}, fnloom_module)
    assert _fnloom_configured({"mode": "a"}, fnloom_module).execute(["total"]) == {
        "total": 3
    }


_MISSING_KEY = """
{decorator}

@config.when(mode=None)
def a__none() -> int:
    return 1

@config.when_not(mode="a")
def b__not_a() -> int:
    return 2

@config.when_in(mode=["a", None])
def c__in() -> int:
    return 3

@config.when_not_in(mode=["a"])
def d__not_in() -> int:
    return 4
"""


@pytest.mark.deliberate("Q57")
def test_q57_a_missing_key_matches_no_form() -> None:
    """Hamilton reads a missing key as `None` in all four forms; fnloom matches none."""
    hamilton_module = make_module(
        "differential_q57_forms_h", _MISSING_KEY.format(decorator=_HAMILTON_CONFIG)
    )
    hamilton = _hamilton_configured({}, hamilton_module)
    assert _hamilton_names(hamilton) == ["a", "b", "c", "d"]
    assert hamilton.execute(["a", "b", "c", "d"]) == {"a": 1, "b": 2, "c": 3, "d": 4}
    fnloom_module = make_module(
        "differential_q57_forms_f", _MISSING_KEY.format(decorator=_FNLOOM_CONFIG)
    )
    assert _fnloom_configured({}, fnloom_module).node_names() == []
    # With the key present, fnloom's forms read it as Hamilton's do.
    both: dict[str, object] = {"mode": "b"}
    assert _fnloom_configured(both, fnloom_module).node_names() == ["b", "d"]
    assert _hamilton_names(_hamilton_configured(both, hamilton_module)) == [
        "b",
        "d",
        "mode",
    ]


_TWO_MATCHES = """
{decorator}

@config.when_not(mode="b")
def foo__nb() -> int:
    return 1

@config.when(mode="a")
def foo__a() -> int:
    return 2
"""

_REDEFINED = """
{decorator}

@config.when(mode="a")
def foo__a() -> int:
    return 1

@config.when(mode="a")
def foo__a() -> int:
    return 2
"""


@pytest.mark.deliberate("Q58")
def test_q58_two_variants_and_a_redefinition(tmp_path: Path) -> None:
    """Hamilton names a wrong culprit and keeps a redefinition; fnloom refuses both."""
    hamilton_two = make_module(
        "differential_q58_two_h", _TWO_MATCHES.format(decorator=_HAMILTON_CONFIG)
    )
    with pytest.raises(ValueError, match="more than once") as raised:
        _hamilton_configured({"mode": "a"}, hamilton_two)
    assert "foo__nb" in str(raised.value)
    assert "foo__a " not in str(raised.value)
    fnloom_two = make_module(
        "differential_q58_two_f", _TWO_MATCHES.format(decorator=_FNLOOM_CONFIG)
    )
    with pytest.raises(ValueError, match=r": 'foo__a', 'foo__nb'$"):
        _fnloom_configured({"mode": "a"}, fnloom_two)
    hamilton_redefined = _file_module(
        tmp_path,
        "differential_q58_redef_h",
        _REDEFINED.format(decorator=_HAMILTON_CONFIG),
    )
    assert _hamilton_configured({"mode": "a"}, hamilton_redefined).execute(["foo"]) == {
        "foo": 2
    }
    fnloom_redefined = _file_module(
        tmp_path,
        "differential_q58_redef_f",
        _REDEFINED.format(decorator=_FNLOOM_CONFIG),
    )
    with pytest.raises(
        ValueError,
        match=r"^function 'foo__a' is defined more than once .* \(lines 5, 9\)",
    ):
        _fnloom_configured({"mode": "a"}, fnloom_redefined)


_BARE_WHEN = """
{decorator}

@config.when
def foo__a() -> int:
    return 1

def keep() -> int:
    return 1
"""

_SUBSTRING = """
{decorator}

@config.when_in(mode="ab")
def foo__a() -> int:
    return 1
"""


@pytest.mark.deliberate("Q59")
def test_q59_misuse_is_refused_when_decorating() -> None:
    """Hamilton drops a bare `@config.when` and reads a `str` as substrings."""
    bare = make_module(
        "differential_q59_bare_h", _BARE_WHEN.format(decorator=_HAMILTON_CONFIG)
    )
    assert _hamilton_names(_hamilton_configured({"mode": "a"}, bare)) == [
        "keep",
        "mode",
    ]
    substring = make_module(
        "differential_q59_sub_h", _SUBSTRING.format(decorator=_HAMILTON_CONFIG)
    )
    for mode in ("a", "b", "ab"):
        assert "foo" in _hamilton_names(_hamilton_configured({"mode": mode}, substring))
    with pytest.raises(InvalidDecoratorException, match=r"whereas$"):
        make_module(
            "differential_q59_trail_h",
            f"{_HAMILTON_CONFIG}\n\n@config.when(mode='a')\n"
            "def foo__() -> int:\n    return 1\n",
        )
    with pytest.raises(TypeError, match=r"with parentheses"):
        make_module(
            "differential_q59_bare_f", _BARE_WHEN.format(decorator=_FNLOOM_CONFIG)
        )
    with pytest.raises(TypeError, match=r"need a collection of values: 'mode' \(str\)"):
        make_module(
            "differential_q59_sub_f", _SUBSTRING.format(decorator=_FNLOOM_CONFIG)
        )
    with pytest.raises(ValueError, match=r"'foo__' ends in '__'"):
        make_module(
            "differential_q59_trail_f",
            f"{_FNLOOM_CONFIG}\n\n@config.when(mode='a')\n"
            "def foo__() -> int:\n    return 1\n",
        )


_POSITIONAL_NAME = """
{decorator}

@config.when("alpha", mode="a")
def thing() -> int:
    return 1
"""

_NOT_ITERABLE = """
{decorator}

@config.when_in(mode=5)
def foo__a() -> int:
    return 1
"""

_NOT_IN_NAME = """
{decorator}

@config.when_not_in(name=["a"])
def foo__x() -> int:
    return 1
"""


@pytest.mark.deliberate("Q59")
def test_q59_a_name_and_a_non_iterable() -> None:
    """Hamilton reads a name or a non-iterable loosely; fnloom refuses when decorating.

    Hamilton takes a positional string as the node name, reads `name` on
    `when_not_in` as a config key, and fails a non-iterable only at build.
    """
    positional = make_module(
        "differential_q59_pos_h", _POSITIONAL_NAME.format(decorator=_HAMILTON_CONFIG)
    )
    assert _hamilton_names(_hamilton_configured({"mode": "a"}, positional)) == [
        "alpha",
        "mode",
    ]
    not_iterable = make_module(
        "differential_q59_nonit_h", _NOT_ITERABLE.format(decorator=_HAMILTON_CONFIG)
    )
    with pytest.raises(TypeError, match="is not a container or iterable"):
        _hamilton_configured({"mode": 5}, not_iterable)
    not_in = make_module(
        "differential_q59_nin_h", _NOT_IN_NAME.format(decorator=_HAMILTON_CONFIG)
    )
    assert "foo" in _hamilton_names(_hamilton_configured({"name": "b"}, not_in))
    assert "foo" not in _hamilton_names(_hamilton_configured({"name": "a"}, not_in))
    with pytest.raises(TypeError, match=r"as a mapping or keywords, got str"):
        make_module(
            "differential_q59_pos_f", _POSITIONAL_NAME.format(decorator=_FNLOOM_CONFIG)
        )
    with pytest.raises(TypeError, match=r"need a collection of values: 'mode' \(int\)"):
        make_module(
            "differential_q59_nonit_f", _NOT_ITERABLE.format(decorator=_FNLOOM_CONFIG)
        )
    # `name=` is the node name on `when_not_in` too, never a key: no condition is left.
    with pytest.raises(ValueError, match=r"when_not_in needs at least one condition"):
        make_module(
            "differential_q59_nin_f", _NOT_IN_NAME.format(decorator=_FNLOOM_CONFIG)
        )
    keyed = make_module(
        "differential_q59_nin_keyed_f",
        _NOT_IN_NAME.format(decorator=_FNLOOM_CONFIG).replace(
            '(name=["a"])', '({"name": ["a"]})'
        ),
    )
    assert _fnloom_configured({"name": "b"}, keyed).node_names() == ["foo"]
    assert _fnloom_configured({"name": "a"}, keyed).node_names() == []


_ONE_VARIANT = """
{decorator}

@config.when(mode="a")
def foo__a() -> int:
    return 1
"""


@pytest.mark.deliberate("Q60")
def test_q60_no_mutation_and_no_tag() -> None:
    """Hamilton renames the function and tags the node; fnloom does neither."""
    hamilton_module = make_module(
        "differential_q60_h", _ONE_VARIANT.format(decorator=_HAMILTON_CONFIG)
    )
    hamilton = _hamilton_configured({"mode": "a"}, hamilton_module)
    assert hamilton_module.foo__a.__name__ == "foo"
    (foo,) = (v for v in hamilton.list_available_variables() if v.name == "foo")
    assert foo.tags["hamilton.config"] == "mode"
    fnloom_module = make_module(
        "differential_q60_f", _ONE_VARIANT.format(decorator=_FNLOOM_CONFIG)
    )
    driver = _fnloom_configured({"mode": "a"}, fnloom_module)
    assert fnloom_module.foo__a.__name__ == "foo__a"
    assert not hasattr(fnloom_module.foo__a, "__original_name__")
    assert driver.nodes["foo"].tags == {}
    assert [c.key for c in driver.nodes["foo"].config] == ["mode"]


_HAMILTON_EXTRACT = (
    "from hamilton.function_modifiers import extract_fields, parameterize, tag, value"
)
_FNLOOM_EXTRACT = "from fnloom import extract_fields, parameterize, tag, value"

_UNCHECKED = """
{decorator}

@extract_fields({{"a": int, "b": int}})
def p() -> dict:
    return {{"a": "not-int"}}

@extract_fields({{"c": str}})
def q() -> dict[str, int]:
    return {{"c": 1}}

def uses_a(a: int) -> str:
    return a
"""


@pytest.mark.deliberate("Q61")
def test_q61_fields_are_checked_at_build_and_run() -> None:
    """Hamilton checks no field's type and fails a missing one with its own class.

    fnloom refuses a declared type the return cannot feed at build, and checks
    each field's value and presence on every run, also with input checks off.
    """
    hamilton_module = make_module(
        "differential_q61_h", _UNCHECKED.format(decorator=_HAMILTON_EXTRACT)
    )
    hamilton = hamilton_driver.Builder().with_modules(hamilton_module).build()
    assert hamilton.execute(["a", "uses_a", "c"]) == {
        "a": "not-int",
        "uses_a": "not-int",
        "c": 1,
    }
    with pytest.raises(InvalidDecoratorException, match="No such field: b"):
        hamilton.execute(["b"])
    fnloom_module = make_module(
        "differential_q61_f", _UNCHECKED.format(decorator=_FNLOOM_EXTRACT)
    )
    with pytest.raises(ValueError, match=r"field 'c' is declared str"):
        fnloom.build(fnloom_module)
    del fnloom_module.q
    for builder in (fnloom.Builder(), fnloom.Builder().without_input_type_checks()):
        driver = builder.with_modules(fnloom_module).build()
        with pytest.raises(ValueError, match=r"returned field 'a' as str"):
            driver.execute(["a"])
        with pytest.raises(ValueError, match=r"no field 'b'; it returned 'a'"):
            driver.execute(["b"])


_MIXED = """
from collections.abc import Mapping
from typing import TypedDict
{decorator}

class Point(TypedDict):
    x: int

@extract_fields({{"a": int}}, "b")
def mixed() -> dict:
    return {{"a": 1, "b": 2}}
"""

_TYPED_DICT_MISSING = """
from typing import TypedDict
{decorator}

class Point(TypedDict):
    x: int

@extract_fields(["zz"])
def p() -> Point:
    return {{"x": 1}}
"""

_MAPPING = """
from collections.abc import Mapping
{decorator}

@extract_fields("a")
def p() -> Mapping[str, int]:
    return {{"a": 1}}
"""

_TWO_ROWS = """
{decorator}

@extract_fields({{"a": int}})
@parameterize(r1={{"n": value(1)}}, r2={{"n": value(2)}})
def p(n: int) -> dict:
    return {{"a": n}}
"""


@pytest.mark.deliberate("Q61")
def test_q61_misuse_is_refused_when_decorating() -> None:
    """Hamilton drops extra names, raises `KeyError` and refuses `Mapping`.

    It refuses two rows only at build. fnloom refuses the first, second and
    fourth with a `ValueError` when decorating, and accepts a `Mapping` return.
    """
    mixed = make_module(
        "differential_q61_mixed_h", _MIXED.format(decorator=_HAMILTON_EXTRACT)
    )
    assert _hamilton_names(hamilton_driver.Builder().with_modules(mixed).build()) == [
        "a",
        "mixed",
    ]
    with pytest.raises(KeyError, match="zz"):
        make_module(
            "differential_q61_td_h",
            _TYPED_DICT_MISSING.format(decorator=_HAMILTON_EXTRACT),
        )
    with pytest.raises(InvalidDecoratorException, match="must be a `dict`"):
        make_module(
            "differential_q61_mapping_h", _MAPPING.format(decorator=_HAMILTON_EXTRACT)
        )
    two_rows = make_module(
        "differential_q61_rows_h", _TWO_ROWS.format(decorator=_HAMILTON_EXTRACT)
    )
    with pytest.raises(InvalidDecoratorException, match="single node"):
        hamilton_driver.Builder().with_modules(two_rows).build()
    with pytest.raises(ValueError, match=r"not both; the extra names: 'b'"):
        make_module(
            "differential_q61_mixed_f", _MIXED.format(decorator=_FNLOOM_EXTRACT)
        )
    with pytest.raises(ValueError, match=r"which has no field 'zz'"):
        make_module(
            "differential_q61_td_f",
            _TYPED_DICT_MISSING.format(decorator=_FNLOOM_EXTRACT),
        )
    mapping = make_module(
        "differential_q61_mapping_f", _MAPPING.format(decorator=_FNLOOM_EXTRACT)
    )
    assert fnloom.build(mapping).execute(["a"]) == {"a": 1}
    with pytest.raises(ValueError, match=r"parameterize makes 2: 'r1', 'r2'"):
        make_module(
            "differential_q61_rows_f", _TWO_ROWS.format(decorator=_FNLOOM_EXTRACT)
        )


class _Coordinates(TypedDict):
    x: int


@pytest.mark.deliberate("Q61")
def test_q61_a_typed_dict_takes_any_dict() -> None:
    """Hamilton's input check raises on a `TypedDict`; fnloom takes any dict."""
    with pytest.raises(TypeError, match="TypedDict does not support instance"):
        htypes.check_input_type(_Coordinates, {"x": 1})
    assert value_matches(_Coordinates, {"x": 1})
    assert not value_matches(_Coordinates, [("x", 1)])
    # And through a driver's real input check.
    module = make_module(
        "differential_q61_td_input",
        """
        def count(point: Coordinates) -> int:
            return len(point)
        """,
    )
    setattr(module, "Coordinates", _Coordinates)  # noqa: B010  # a module attribute ty cannot see
    driver = fnloom.build(module)
    assert driver.execute(["count"], inputs={"point": {"x": 1}}) == {"count": 1}
    with pytest.raises(ValueError, match=r"point: expected .*_Coordinates, got list"):
        driver.execute(["count"], inputs={"point": [("x", 1)]})


_FILLED = """
{decorator}

SHARED: dict[str, int] = {{"a": 1}}

@extract_fields({{"a": int, "b": int}}, fill_with=-1)
def p() -> dict:
    return SHARED

@extract_fields({{"c": int | None}}, fill_with=None)
def q() -> dict:
    return {{}}
"""


@pytest.mark.deliberate("Q62")
def test_q62_fill_never_mutates_and_none_fills() -> None:
    """Hamilton writes the fill into the returned dict and reads `None` as no fill."""
    hamilton_module = make_module(
        "differential_q62_h", _FILLED.format(decorator=_HAMILTON_EXTRACT)
    )
    hamilton = hamilton_driver.Builder().with_modules(hamilton_module).build()
    assert hamilton.execute(["b", "p"]) == {"b": -1, "p": {"a": 1, "b": -1}}
    assert hamilton_module.SHARED == {"a": 1, "b": -1}
    with pytest.raises(InvalidDecoratorException, match="No such field: c"):
        hamilton.execute(["c"])
    fnloom_module = make_module(
        "differential_q62_f", _FILLED.format(decorator=_FNLOOM_EXTRACT)
    )
    driver = fnloom.build(fnloom_module)
    assert driver.execute(["b", "p", "c"]) == {"b": -1, "p": {"a": 1}, "c": None}
    assert fnloom_module.SHARED == {"a": 1}


_TAGGED = """
{decorator}

{first}
{second}
def p() -> dict:
    return {{"a": 1}}
"""
_TAG_ORDERS = [
    ('@tag(owner="z")', '@extract_fields({"a": int})'),
    ('@extract_fields({"a": int})', '@tag(owner="z")'),
]


@pytest.mark.deliberate("Q63")
def test_q63_tags_land_on_the_producer_too() -> None:
    """Hamilton tags only the field nodes, in either order; fnloom the producer too."""
    for index, (first, second) in enumerate(_TAG_ORDERS):
        hamilton_module = make_module(
            f"differential_q63_h{index}",
            _TAGGED.format(decorator=_HAMILTON_EXTRACT, first=first, second=second),
        )
        hamilton = hamilton_driver.Builder().with_modules(hamilton_module).build()
        hamilton_tags = {
            v.name: {k: t for k, t in v.tags.items() if not k.startswith("module")}
            for v in hamilton.list_available_variables()
        }
        assert hamilton_tags == {"a": {"owner": "z"}, "p": {}}, (first, second)
        fnloom_module = make_module(
            f"differential_q63_f{index}",
            _TAGGED.format(decorator=_FNLOOM_EXTRACT, first=first, second=second),
        )
        driver = fnloom.build(fnloom_module)
        assert driver.nodes["a"].tags == {"owner": "z"}
        assert driver.nodes["p"].tags == {"owner": "z"}


_TAGGED_COLUMNS = """
import pandas as pd
{decorator}

{first}
{second}
def p() -> pd.DataFrame:
    return pd.DataFrame({{"a": [1]}})
"""
_COLUMN_TAG_ORDERS = [
    ('@tag(owner="z")', '@extract_columns("a")'),
    ('@extract_columns("a")', '@tag(owner="z")'),
]


@pytest.mark.deliberate("Q63")
def test_q63_column_tags_land_on_the_producer_too() -> None:
    """Hamilton tags only the column nodes, in either order; fnloom the producer too."""
    pytest.importorskip("pandas")
    _hamilton_pandas()
    for index, (first, second) in enumerate(_COLUMN_TAG_ORDERS):
        hamilton_module = make_module(
            f"differential_q63_columns_h{index}",
            _TAGGED_COLUMNS.format(
                decorator=_HAMILTON_COLUMNS_TAG, first=first, second=second
            ),
        )
        hamilton = hamilton_driver.Builder().with_modules(hamilton_module).build()
        hamilton_tags = {
            v.name: {k: t for k, t in v.tags.items() if not k.startswith("module")}
            for v in hamilton.list_available_variables()
        }
        assert hamilton_tags == {"a": {"owner": "z"}, "p": {}}, (first, second)
        fnloom_module = make_module(
            f"differential_q63_columns_f{index}",
            _TAGGED_COLUMNS.format(
                decorator=_FNLOOM_COLUMNS_TAG, first=first, second=second
            ),
        )
        driver = fnloom.build(fnloom_module)
        assert driver.nodes["a"].tags == {"owner": "z"}, (first, second)
        assert driver.nodes["p"].tags == {"owner": "z"}, (first, second)


_HAMILTON_COLUMNS_TAG = "from hamilton.function_modifiers import extract_columns, tag"
_FNLOOM_COLUMNS_TAG = "from fnloom import extract_columns, tag"


_HAMILTON_COLUMNS = "from hamilton.function_modifiers import extract_columns"
_FNLOOM_COLUMNS = "from fnloom import extract_columns"

_FRAME = """
import pandas as pd
{decorator}

@extract_columns("a")
def frame() -> pd.DataFrame:
    return pd.DataFrame({{"a": [1, 2]}})
"""

_DICT = """
{decorator}

@extract_columns("b")
def mapping() -> dict:
    return {{"b": 3}}
"""


@pytest.mark.deliberate("Q64")
def test_q64_extract_columns_needs_no_extension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hamilton without its pandas extension refuses a frame at decoration.

    fnloom splits it into `pandas.Series` columns, read by name.
    """
    pd = pytest.importorskip("pandas")
    from hamilton import registry  # noqa: PLC0415  # only the Q64 tests need it

    # As with autoload off and nothing loaded yet, whatever ran before.
    monkeypatch.setattr(registry, "DF_TYPE_AND_COLUMN_TYPES", {})
    with pytest.raises(InvalidDecoratorException, match="type we know about"):
        make_module("differential_q64_h", _FRAME.format(decorator=_HAMILTON_COLUMNS))
    fnloom_module = make_module(
        "differential_q64_f", _FRAME.format(decorator=_FNLOOM_COLUMNS)
    )
    driver = fnloom.build(fnloom_module)
    assert driver.nodes["a"].type is pd.Series
    result: dict[str, Any] = dict(driver.execute(["a"]))
    assert result["a"].tolist() == [1, 2]


@pytest.mark.deliberate("Q64")
def test_q64_a_dict_return_is_split() -> None:
    """Hamilton refuses a `dict` return even with its pandas extension loaded.

    fnloom splits it into columns typed `Any`.
    """
    pytest.importorskip("pandas")
    _hamilton_pandas()
    with pytest.raises(InvalidDecoratorException, match="type we know about"):
        make_module(
            "differential_q64_dict_h", _DICT.format(decorator=_HAMILTON_COLUMNS)
        )
    fnloom_module = make_module(
        "differential_q64_dict_f", _DICT.format(decorator=_FNLOOM_COLUMNS)
    )
    driver = fnloom.build(fnloom_module)
    assert driver.nodes["b"].type is Any
    assert driver.execute(["b", "mapping"]) == {"b": 3, "mapping": {"b": 3}}


_REPEATED = """
import pandas as pd
{decorator}

@extract_columns({columns})
def frame() -> pd.DataFrame:
    return pd.DataFrame({{"a": [1]}})
"""


@pytest.mark.deliberate("Q64")
def test_q64_a_repeated_column_is_refused_when_decorating() -> None:
    """Hamilton fails the build on a column named twice, in either form.

    fnloom refuses it when decorating, listing every repeat, sorted.
    """
    pytest.importorskip("pandas")
    _hamilton_pandas()
    for index, columns in enumerate(['"a", "a"', '"a", ("a", "doc")']):
        hamilton_module = make_module(
            f"differential_q64_repeat_h{index}",
            _REPEATED.format(decorator=_HAMILTON_COLUMNS, columns=columns),
        )
        with pytest.raises(ValueError, match="Cannot define function a more than"):
            hamilton_driver.Builder().with_modules(hamilton_module).build()
        with pytest.raises(ValueError, match=r"names a column more than once: 'a'$"):
            make_module(
                f"differential_q64_repeat_f{index}",
                _REPEATED.format(decorator=_FNLOOM_COLUMNS, columns=columns),
            )
    with pytest.raises(ValueError, match=r"more than once: 'a', 'b'$"):
        fnloom.extract_columns("b", "a", ("b", "doc"), "a")


_COLUMNS_FILLED = """
import pandas as pd
{decorator}

@extract_columns("a", ("b", "The b column."), fill_with=0)
def frame() -> pd.DataFrame:
    return pd.DataFrame({{"a": [1, 2]}})
"""


def _hamilton_pandas() -> None:
    """Registers Hamilton's pandas extension, which its autoload would load."""
    from hamilton import registry  # noqa: PLC0415  # only the Q64 tests need it

    registry.load_extension("pandas")


@pytest.mark.deliberate("Q64")
def test_q64_fill_never_mutates_and_fills_by_name() -> None:
    """Hamilton fills a column named by the `(name, doc)` tuple into the frame.

    It then fails to find the column by its name. fnloom fills by the name,
    leaving the producer's frame as it was.
    """
    pytest.importorskip("pandas")
    _hamilton_pandas()
    hamilton_module = make_module(
        "differential_q64_fill_h", _COLUMNS_FILLED.format(decorator=_HAMILTON_COLUMNS)
    )
    hamilton = hamilton_driver.Builder().with_modules(hamilton_module).build()
    with pytest.raises(InvalidDecoratorException, match="No such column: b"):
        hamilton.execute(["b"])
    mutated = hamilton.execute(["a", "frame"])["frame"]
    assert list(mutated.columns) == ["a", ("b", "The b column.")]
    fnloom_module = make_module(
        "differential_q64_fill_f", _COLUMNS_FILLED.format(decorator=_FNLOOM_COLUMNS)
    )
    result: dict[str, Any] = dict(fnloom.build(fnloom_module).execute(["b", "frame"]))
    assert result["b"].tolist() == [0, 0]
    assert result["b"].name == "b"
    assert list(result["frame"].columns) == ["a"]


_COLUMNS_CHECKED = """
import pandas as pd
{decorator}

@extract_columns("a")
def twice() -> pd.DataFrame:
    return pd.DataFrame([[1, 2]], columns=["a", "a"])

@extract_columns("zz")
def lacking() -> pd.DataFrame:
    return pd.DataFrame({{"b": [1], "c": [2]}})
"""


@pytest.mark.deliberate("Q64")
def test_q64_columns_are_checked_at_run() -> None:
    """Hamilton passes a duplicated column on as a DataFrame, unchecked.

    It fails a missing column with its own exception class. fnloom checks the
    column is a Series, and names the columns the producer returned.
    """
    pd = pytest.importorskip("pandas")
    _hamilton_pandas()
    hamilton_module = make_module(
        "differential_q64_checked_h",
        _COLUMNS_CHECKED.format(decorator=_HAMILTON_COLUMNS),
    )
    hamilton = hamilton_driver.Builder().with_modules(hamilton_module).build()
    assert isinstance(hamilton.execute(["a"])["a"], pd.DataFrame)
    with pytest.raises(InvalidDecoratorException, match="No such column: zz"):
        hamilton.execute(["zz"])
    fnloom_module = make_module(
        "differential_q64_checked_f",
        _COLUMNS_CHECKED.format(decorator=_FNLOOM_COLUMNS),
    )
    driver = fnloom.build(fnloom_module)
    with pytest.raises(ValueError, match=r"'a' as pandas\.DataFrame, but it is"):
        driver.execute(["a"])
    with pytest.raises(ValueError, match=r"no column 'zz'; it returned 'b', 'c'"):
        driver.execute(["zz"])


_ASYNC = """
import asyncio


async def doubled(x: int) -> int:
    await asyncio.sleep(0)
    return x * 2


def plus_one(doubled: int) -> int:
    return doubled + 1


async def total(plus_one: int, doubled: int) -> int:
    return plus_one + doubled
"""
# Hamilton's AsyncDriver warns on 3.14 from its own code; Q13 asserts it.
_HAMILTON_ASYNC_WARNING = (
    "ignore:'asyncio.iscoroutinefunction' is deprecated:DeprecationWarning"
)


async def _hamilton_async(module: ModuleType) -> hamilton_async.AsyncDriver:
    """Hamilton's async driver through its builder, which gives a dict result."""
    # Typed as the sync builder's `build`, which this one overrides with a coroutine.
    pending: object = hamilton_async.Builder().with_modules(module).build()
    return await cast("Coroutine[Any, Any, hamilton_async.AsyncDriver]", pending)


@pytest.mark.deliberate("Q11")
@pytest.mark.filterwarnings(_HAMILTON_ASYNC_WARNING)
def test_q11_async_results_are_dicts_in_request_order() -> None:
    """Hamilton gives a DataFrame, or a dict sorted by key; fnloom the sync dict."""
    pd = pytest.importorskip("pandas")
    module = make_module("differential_q11", _ASYNC)
    request = ["total", "doubled"]

    async def hamilton() -> tuple[object, object]:
        built = await _hamilton_async(module)
        direct = hamilton_async.AsyncDriver({}, module)
        return (
            await built.execute(request, inputs={"x": 3}),
            await direct.execute(request, inputs={"x": 3}),
        )

    by_builder, by_constructor = asyncio.run(hamilton())
    assert isinstance(by_builder, dict)
    assert list(by_builder.items()) == [("doubled", 6), ("total", 13)]
    assert isinstance(by_constructor, pd.DataFrame)
    driver = fnloom.Builder().with_modules(module).build_async()
    result = asyncio.run(driver.execute(request, inputs={"x": 3}))
    assert type(result) is dict
    assert list(result.items()) == [("total", 13), ("doubled", 6)]


@pytest.mark.deliberate("Q12")
def test_q12_building_is_synchronous() -> None:
    """Hamilton's async `build` is a coroutine; fnloom's `build_async` the driver."""
    module = make_module("differential_q12", _ASYNC)
    pending = hamilton_async.Builder().with_modules(module).build()
    assert inspect.iscoroutine(pending)
    pending.close()
    driver = fnloom.Builder().with_modules(module).build_async()
    assert isinstance(driver, fnloom.AsyncDriver)
    plan = driver.prepare(["total"], inputs={"x": 0})
    assert isinstance(plan, fnloom.AsyncPreparedPlan)
    assert asyncio.run(plan.run(inputs={"x": 1})) == {"total": 5}


@pytest.mark.deliberate("Q13")
def test_q13_the_deprecation_warning_is_hamiltons() -> None:
    """On 3.14, Hamilton's AsyncDriver warns `DeprecationWarning`; fnloom's doesn't."""
    module = make_module("differential_q13", _ASYNC)

    async def hamilton() -> object:
        return await (await _hamilton_async(module)).execute(["total"], inputs={"x": 1})

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert asyncio.run(hamilton()) == {"total": 5}
    hamilton_warnings = {
        (Path(w.filename).parent.name, Path(w.filename).name, str(w.message))
        for w in caught
        if issubclass(w.category, DeprecationWarning)
    }
    assert hamilton_warnings == {
        (
            "hamilton",
            "async_driver.py",
            (
                "'asyncio.iscoroutinefunction' is deprecated and slated for removal "
                "in Python 3.16; use inspect.iscoroutinefunction() instead"
            ),
        )
    }
    driver = fnloom.Builder().with_modules(module).build_async()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert asyncio.run(driver.execute(["total"], inputs={"x": 1})) == {"total": 5}
    assert [w for w in caught if issubclass(w.category, DeprecationWarning)] == []


# A sync node that waits for an async sibling to tick, and says whether it did.
# The ticker ticks only after the sync node has started, so a tick can reach it
# only while it runs, and only if the loop runs the ticker meanwhile.
_TICKING = """
import asyncio
import threading

STARTED = threading.Event()
TICKED = threading.Event()


def waits_for_tick(x: int) -> bool:
    STARTED.set()
    return TICKED.wait({patience})


async def ticker(x: int) -> int:
    await asyncio.to_thread(STARTED.wait, 10.0)
    TICKED.set()
    return x
"""
# How long a sync node blocking the loop waits for a tick that can't come.
_BLOCKED_PATIENCE = 0.1


@pytest.mark.deliberate("Q65")
@pytest.mark.filterwarnings(_HAMILTON_ASYNC_WARNING)
def test_q65_sync_nodes_run_off_the_loop() -> None:
    """Hamilton's sync node stops the loop, so no tick reaches it; fnloom's doesn't.

    By order of events: the node sees the tick only if the loop ran the ticker
    while the node waited, however long it waits.
    """
    request = ["waits_for_tick", "ticker"]
    hamilton_module = make_module(
        "differential_q65_h", _TICKING.format(patience=_BLOCKED_PATIENCE)
    )

    async def hamilton() -> object:
        built = await _hamilton_async(hamilton_module)
        return await built.execute(request, inputs={"x": 1})

    assert asyncio.run(hamilton()) == {"waits_for_tick": False, "ticker": 1}
    for sync_nodes, patience, ticked in (
        ("thread", 10.0, True),
        ("inline", _BLOCKED_PATIENCE, False),
    ):
        module = make_module(
            f"differential_q65_{sync_nodes}", _TICKING.format(patience=patience)
        )
        driver = (
            fnloom.Builder().with_modules(module).build_async(sync_nodes=sync_nodes)
        )
        assert asyncio.run(driver.execute(request, inputs={"x": 1})) == {
            "waits_for_tick": ticked,
            "ticker": 1,
        }, sync_nodes


_FAILING = """
import asyncio

LOG: list[str] = []
SIBLING_STARTED = asyncio.Event()
RELEASE = asyncio.Event()
FINISHED = asyncio.Event()


async def boom(x: int) -> int:
    await SIBLING_STARTED.wait()
    raise KeyError("boom")


async def sibling(x: int) -> int:
    SIBLING_STARTED.set()
    try:
        await RELEASE.wait()
    except asyncio.CancelledError:
        LOG.append("cancelled")
        raise
    LOG.append("finished")
    FINISHED.set()
    return x
"""


@pytest.mark.deliberate("Q66")
@pytest.mark.filterwarnings(_HAMILTON_ASYNC_WARNING)
def test_q66_a_failure_cancels_its_siblings() -> None:
    """Hamilton leaves the sibling running after it raises; fnloom cancels it.

    By order of events: the node fails only once the sibling waits, and the
    sibling finishes only when released, after the run has raised.
    """
    request = ["sibling", "boom"]
    hamilton_module = make_module("differential_q66_h", _FAILING)

    async def hamilton() -> BaseException:
        built = await _hamilton_async(hamilton_module)
        with pytest.raises(KeyError) as info:
            await built.execute(request, inputs={"x": 1})
        assert hamilton_module.LOG == []
        hamilton_module.RELEASE.set()
        await asyncio.wait_for(hamilton_module.FINISHED.wait(), 10.0)
        return info.value

    hamilton_error = asyncio.run(hamilton())
    assert getattr(hamilton_error, "__notes__", None) is None
    assert hamilton_module.LOG == ["finished"]
    module = make_module("differential_q66_f", _FAILING)
    driver = fnloom.Builder().with_modules(module).build_async()

    async def fnloom_run() -> BaseException:
        with pytest.raises(KeyError) as info:
            await driver.execute(request, inputs={"x": 1})
        # Cancelled, and done being cancelled, by the time the run raised.
        assert module.LOG == ["cancelled"]
        return info.value

    error = asyncio.run(fnloom_run())
    assert type(error) is type(hamilton_error)
    assert error.args == hamilton_error.args == ("boom",)
    assert error.__notes__ == ["in node 'boom'"]
    assert module.LOG == ["cancelled"]


_TYPED = """
def typed(x: int) -> int:
    return x
"""


@pytest.mark.deliberate("Q67")
@pytest.mark.filterwarnings(_HAMILTON_ASYNC_WARNING)
def test_q67_requests_are_checked_as_the_sync_driver_checks_them() -> None:
    """Hamilton checks no type and raises other errors; fnloom raises the sync ones."""
    module = make_module("differential_q67", _TYPED)
    requests: list[tuple[list[str], dict[str, object]]] = [
        (["typed"], {"x": "str"}),
        (["typed"], {}),
        (["x"], {}),
    ]

    async def hamilton() -> list[object]:
        built = await _hamilton_async(module)
        outcomes: list[object] = []
        for request, inputs in requests:
            try:
                outcomes.append(await built.execute(request, inputs=inputs))
            except (NotImplementedError, KeyError) as error:
                outcomes.append((type(error).__name__, str(error)))
        return outcomes

    assert asyncio.run(hamilton()) == [
        {"typed": "str"},
        ("NotImplementedError", "x was expected to be passed in but was not."),
        ("KeyError", "'x'"),
    ]
    sync = fnloom.build(module)
    driver = fnloom.Builder().with_modules(module).build_async()
    messages: list[str] = []
    for request, inputs in requests:
        with pytest.raises(ValueError) as sync_info:  # noqa: PT011  # compared below
            sync.execute(request, inputs=inputs)
        with pytest.raises(ValueError) as async_info:  # noqa: PT011  # compared below
            asyncio.run(driver.execute(request, inputs=inputs))
        assert str(async_info.value) == str(sync_info.value), request
        messages.append(str(async_info.value))
    assert messages == [
        "x: expected int, got str",
        "missing inputs: 'x' (needed by 'typed')",
        "missing inputs: 'x' (requested as an output)",
    ]


_CACHED_ASYNC = """
import asyncio


async def doubled(x: int) -> int:
    print("doubled")
    await asyncio.sleep(0)
    return x * 2


def plus_one(doubled: int) -> int:
    print("plus_one")
    return doubled + 1
"""


@pytest.mark.deliberate("Q68")
@pytest.mark.filterwarnings(_HAMILTON_ASYNC_WARNING)
def test_q68_the_async_driver_caches(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Hamilton fails to build an async driver with a cache; fnloom's caches."""
    module = make_module("differential_q68", _CACHED_ASYNC)

    async def hamilton() -> None:
        pending: object = (
            hamilton_async.Builder()
            .with_modules(module)
            .with_cache(path=str(tmp_path / "hamilton"))
            .build()
        )
        await cast("Coroutine[Any, Any, hamilton_async.AsyncDriver]", pending)

    with pytest.raises(ValueError, match="Multiple adapters cannot"):
        asyncio.run(hamilton())
    store = DiskCache(tmp_path / "fnloom")
    driver = fnloom.Builder().with_modules(module).with_cache(store).build_async()
    request = ["plus_one"]
    miss = CacheOutcome("miss", None)
    hit = CacheOutcome("hit", None)
    assert driver.explain_cache(request, inputs={"x": 2}) == {
        "doubled": miss,
        "plus_one": miss,
    }
    assert asyncio.run(driver.execute(request, inputs={"x": 2})) == {"plus_one": 5}
    assert driver.explain_cache(request, inputs={"x": 2}) == {
        "doubled": hit,
        "plus_one": hit,
    }
    assert sorted(capsys.readouterr().out.split()) == ["doubled", "plus_one"]
    assert asyncio.run(driver.execute(request, inputs={"x": 2})) == {"plus_one": 5}
    assert capsys.readouterr().out == "", "the warm run runs nothing"


# The row decorators in each library's spelling.
_HAMILTON_ROWS = "from hamilton.function_modifiers import parameterize, source, value"
_FNLOOM_ROWS = "from fnloom import parameterize, source, value"
_ASYNC_ROWS = """
import asyncio
{decorator}


@parameterize(scaled={{"factor": value(3)}}, shifted={{"factor": source("x")}})
async def times(x: int, factor: int) -> int:
    await asyncio.sleep(0)
    return x * factor


async def plain(x: int) -> int:
    return x
"""


@pytest.mark.deliberate("Q69")
@pytest.mark.filterwarnings(_HAMILTON_ASYNC_WARNING)
def test_q69_an_async_row_is_awaited() -> None:
    """Hamilton returns an async row's coroutine as its value; fnloom awaits it."""
    request = ["scaled", "shifted", "plain"]
    hamilton_module = make_module(
        "differential_q69_h", _ASYNC_ROWS.format(decorator=_HAMILTON_ROWS)
    )

    async def hamilton() -> dict[str, object]:
        built = await _hamilton_async(hamilton_module)
        values = await built.execute(request, inputs={"x": 2})
        found: dict[str, object] = {}
        for name, value in values.items():
            if inspect.iscoroutine(value):
                value.close()
                found[name] = "coroutine"
            else:
                found[name] = value
        return found

    assert asyncio.run(hamilton()) == {
        "plain": 2,
        "scaled": "coroutine",
        "shifted": "coroutine",
    }
    module = make_module(
        "differential_q69_f", _ASYNC_ROWS.format(decorator=_FNLOOM_ROWS)
    )
    driver = fnloom.Builder().with_modules(module).build_async()
    assert asyncio.run(driver.execute(request, inputs={"x": 2})) == {
        "scaled": 6,
        "shifted": 4,
        "plain": 2,
    }


_HAMILTON_PATH_ORDER = """
import json
import sys

from hamilton import driver

sys.path.insert(0, sys.argv[1])
import diamond

built = driver.Builder().with_modules(diamond).build()
path = built.what_is_the_path_between("a", "join")
print(json.dumps([node.name for node in path]))
"""


@pytest.mark.deliberate("Q70")
def test_q70_inspection() -> None:
    """Hamilton's path order follows the hash seed and its errors name one name."""
    module = load_fixture("diamond")
    hamilton = hamilton_driver.Builder().with_modules(module).build()
    built = fnloom.build(module)
    # Hamilton: a set's order, which a hash seed changes; fnloom: one topological order.
    orders = {
        seed: subprocess.run(  # noqa: S603  # our own interpreter and script
            [sys.executable, "-c", _HAMILTON_PATH_ORDER, str(FIXTURES)],
            env={
                **os.environ,
                "PYTHONHASHSEED": seed,
                "HAMILTON_AUTOLOAD_EXTENSIONS": "0",
            },
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        for seed in ("0", "1", "2", "3", "4", "5")
    }
    assert len(set(orders.values())) > 1
    assert built.what_is_the_path_between("a", "join") == [
        "a",
        "root",
        "left",
        "right",
        "join",
    ]
    # Hamilton names the first unknown endpoint only; fnloom names both.
    with pytest.raises(ValueError, match="Upstream node nope not found") as caught:
        hamilton.what_is_the_path_between("nope", "nada")
    assert "nada" not in str(caught.value)
    with pytest.raises(ValueError, match="'nada', 'nope'"):
        built.what_is_the_path_between("nope", "nada")
    # Hamilton lists a module given twice twice; fnloom lists it once.
    # Hamilton builds a module given twice only with `allow_module_overrides`.
    twice = (
        hamilton_driver.Builder()
        .with_modules(module, module)
        .allow_module_overrides()
        .build()
    )
    assert twice.graph_modules == (module, module)
    assert fnloom.Builder().with_modules(module, module).build().modules == (module,)


_Q71_SOURCE = """
def a(x: int) -> int:
    return x + 1


def c(x: int) -> int:
    return x * 2


def profit(revenue: int, cost: int = 3) -> int:
    return revenue - cost
"""


@pytest.mark.deliberate("Q71")
def test_q71_validate() -> None:
    """Hamilton raises one kind of problem at a time; fnloom returns every line."""
    module = make_module("differential_q71", _Q71_SOURCE)
    hamilton = hamilton_driver.Builder().with_modules(module).build()
    built = fnloom.build(module)
    # Hamilton takes `overrides` before `inputs`, positionally; fnloom's are keywords.
    assert list(inspect.signature(hamilton.validate_execution).parameters) == [
        "final_vars",
        "overrides",
        "inputs",
    ]
    keywords = inspect.signature(built.validate).parameters
    assert [keywords[n].kind for n in ("inputs", "overrides")] == [
        inspect.Parameter.KEYWORD_ONLY
    ] * 2
    # A valid request: Hamilton returns `None`, fnloom an empty list.
    assert hamilton.validate_execution(["a"], inputs={"x": 1}) is None
    assert built.validate(["a"], inputs={"x": 1}) == []
    # A missing input: Hamilton names every consumer and raises; fnloom names `a`.
    with pytest.raises(ValueError, match=r"for nodes: \['a', 'c'\]\.$"):
        hamilton.validate_execution(["a"])
    assert built.validate(["a"]) == ["missing inputs: 'x' (needed by 'a')"]
    # A misspelled input key: ignored by Hamilton, with no suggestion anywhere.
    with pytest.raises(ValueError, match="Required input revenue") as caught:
        hamilton.validate_execution(["profit"], inputs={"revenu": 1})
    assert "revenu " not in str(caught.value)
    assert "mean" not in str(caught.value)
    assert built.validate(["profit"], inputs={"revenu": 1}) == [
        "missing inputs: 'revenue' (needed by 'profit') (did you mean 'revenu'?)",
        "unknown inputs: 'revenu' (did you mean 'revenue'?)",
    ]
    # An unknown output hides the input's wrong type in Hamilton; fnloom lists both.
    with pytest.raises(ValueError, match=r"^Unknown nodes \[prfit\]") as unknown:
        hamilton.validate_execution(["a", "prfit"], inputs={"x": "s"})
    assert "mismatch" not in str(unknown.value)
    assert built.validate(["a", "prfit"], inputs={"x": "s"}) == [
        "unknown outputs: 'prfit' (did you mean 'profit'?)",
        "x: expected int, got str",
    ]
    # A function `execute` accepts: Hamilton's validation refuses it, fnloom's reads it.
    assert hamilton.execute([module.a], inputs={"x": 1}) == {"a": 2}
    with pytest.raises(TypeError, match="expected str instance, function found"):
        hamilton.validate_execution([module.a], inputs={"x": 1})
    assert built.validate([module.a], inputs={"x": 1}) == []


_Q75_SOURCE = """
def a(x: int) -> int:
    return x + 1


def profit(revenu: int, cost: int = 3) -> int:
    return revenu - cost


def revenue() -> int:
    return 5
"""


_Q72_SOURCE = """
def fa(x: int) -> int:
    return x + 1


def fb(fa: int) -> int:
    return fa * 2


def _helper(x: int) -> int:
    return x
"""


@pytest.mark.deliberate("Q72")
def test_q72_graphs_from_functions() -> None:
    """Hamilton copies functions into a registered module; fnloom uses them as is."""
    from hamilton import ad_hoc_utils  # noqa: PLC0415  # only this test needs it

    module = make_module("differential_q72_functions", _Q72_SOURCE)
    # Hamilton: a copy of each function, sharing its code and globals, in a new
    # module registered in sys.modules.
    before = set(sys.modules)
    temporary = ad_hoc_utils.create_temporary_module(
        module.fa, module.fb, module_name="differential_q72_temporary"
    )
    assert set(sys.modules) - before == {"differential_q72_temporary"}
    assert sys.modules["differential_q72_temporary"] is temporary
    assert temporary.fa is not module.fa
    assert temporary.fa.__code__ is module.fa.__code__
    assert temporary.fa.__globals__ is module.fa.__globals__
    assert temporary.fb.__module__ == "differential_q72_temporary"
    assert module.fb.__module__ == "differential_q72_functions"
    hamilton = hamilton_driver.Builder().with_modules(temporary).build()
    assert hamilton.execute([temporary.fb], inputs={"x": 1}) == {"fb": 4}
    # The node is the copy, so the original function is refused in `final_vars`.
    with pytest.raises(ValueError, match="not in a module given to the materializer"):
        hamilton.execute([module.fb], inputs={"x": 1})
    # A private name is refused.
    with pytest.raises(ValueError, match="functions that start with '_' are reserved"):
        ad_hoc_utils.create_temporary_module(module._helper)  # noqa: SLF001  # the private function Hamilton refuses
    # A lambda is accepted; unannotated, the build fails on its return annotation.
    bare = ad_hoc_utils.create_temporary_module(
        lambda x: x, module_name="differential_q72_bare_lambda"
    )
    with pytest.raises(ValueError, match="Missing type hint for return value"):
        hamilton_driver.Builder().with_modules(bare).build()
    # Annotated, its node is named `<lambda>`.
    annotated = cast("Any", lambda x: x)
    annotated.__annotations__ = {"x": int, "return": int}
    named_lambda = ad_hoc_utils.create_temporary_module(
        annotated, module_name="differential_q72_lambda"
    )
    lambda_driver = hamilton_driver.Builder().with_modules(named_lambda).build()
    assert {v.name for v in lambda_driver.list_available_variables()} == {
        "<lambda>",
        "x",
    }
    # fnloom: the functions themselves, nothing copied or registered.
    before = set(sys.modules)
    driver = (
        fnloom.Builder()
        .with_functions(module.fa, module.fb, module._helper)  # noqa: SLF001  # given on purpose
        .build()
    )
    assert set(sys.modules) == before
    assert driver.modules == ()
    assert driver.nodes["fb"].origin is module.fb
    assert driver.nodes["fb"].module == "differential_q72_functions"
    # The original in `final_vars` means its node, and a private name is accepted.
    assert driver.execute([module.fb, "_helper"], inputs={"x": 1}) == {
        "fb": 4,
        "_helper": 1,
    }
    # A lambda is refused, annotated or not, unless named.
    for lam in (lambda x: x, annotated):
        with pytest.raises(ValueError, match="has no name a node can take"):
            fnloom.Builder().with_functions(lam)
    named = fnloom.Builder().with_functions(fnloom.node(annotated, name="same"))
    assert named.build().execute(["same"], inputs={"x": 5}) == {"same": 5}


_Q73_SOURCE = """
from typing import TYPE_CHECKING

from {exclude} import {name}
from {other} import imported

if TYPE_CHECKING:
    from decimal import Decimal

__all__ = ["public_one", "reader", "imported", "LIMIT", "_helper", "listed_marked"]

LIMIT = 3


def public_one(x: int) -> int:
    return x


def not_in_all(x: int) -> int:
    return x


def _helper(x: int) -> int:
    return x


@{name}
def legacy(x: "Decimal") -> "Decimal":
    return x


@{name}
def listed_marked(x: int) -> int:
    return x


def reader(legacy: int, not_in_all: int) -> int:
    return legacy + not_in_all
"""

_Q73_VARIANTS = """
{import_}

__all__ = ["price__eu"]


@{when}(region="eu")
def price__eu(x: int) -> int:
    return x


@{when}(region="us")
def price__us(x: int) -> int:
    return x * 2
"""

_Q73_STACKED = """
from {exclude} import {name}
from {tag} import tag


@{name}
@tag(kind="a")
def above(x: int) -> int:
    return x


@tag(kind="b")
@{name}
def below(x: int) -> int:
    return x


def kept(x: int) -> int:
    return x
"""


def _q73_nodes(driver: hamilton_driver.Driver) -> set[str]:
    """The names Hamilton lists for `driver`, inputs included."""
    return {variable.name for variable in driver.list_available_variables()}


@pytest.mark.deliberate("Q73")
def test_q73_all_and_exclude() -> None:
    """Hamilton ignores `__all__`, and its exclude works bare; fnloom honours both."""
    make_module(
        "differential_q73_other", "def imported(x: int) -> int:\n    return x\n"
    )
    hamilton_module = make_module(
        "differential_q73_hamilton",
        _Q73_SOURCE.format(
            exclude="hamilton.function_modifiers",
            name="hamilton_exclude",
            other="differential_q73_other",
        ),
    )
    fnloom_module = make_module(
        "differential_q73_fnloom",
        _Q73_SOURCE.format(
            exclude="fnloom", name="exclude", other="differential_q73_other"
        ),
    )
    # Hamilton: `__all__` is ignored, so `not_in_all` is a node; the excluded
    # function, with an unresolvable annotation, is not, listed or not.
    hamilton = hamilton_driver.Builder().with_modules(hamilton_module).build()
    names = _q73_nodes(hamilton)
    assert {"public_one", "not_in_all", "reader"} <= names
    assert "legacy" in names  # an input of `reader`, not a node
    assert "listed_marked" not in names  # exclude beats a listing
    assert "_helper" not in names  # a listing never makes a private function a node
    assert "imported" not in names  # re-exports and constants are not nodes
    assert "LIMIT" not in names
    assert hamilton.execute(["not_in_all"], inputs={"x": 1}) == {"not_in_all": 1}
    # fnloom: only `public_one` and `reader` are nodes; the other two are inputs.
    built = fnloom.build(fnloom_module)
    assert built.node_names() == ["legacy", "not_in_all", "public_one", "reader", "x"]
    assert not {"public_one", "reader"} - set(built.nodes)
    assert {"legacy", "not_in_all"} <= set(built.inputs())
    assert not {"listed_marked", "_helper", "imported", "LIMIT"} & set(
        built.node_names()
    )
    with pytest.raises(ValueError, match=r"missing inputs: 'legacy'.*") as left_out:
        built.execute(["reader"], inputs={"not_in_all": 1})
    assert (
        "'legacy' is a function in module 'differential_q73_fnloom' "
        "marked @exclude and not listed in its __all__"
    ) in str(left_out.value)
    with pytest.raises(ValueError, match=r"missing inputs: 'not_in_all'") as unlisted:
        built.execute(["reader"], inputs={"legacy": 1})
    assert (
        "'not_in_all' is a function in module 'differential_q73_fnloom' "
        "not listed in its __all__"
    ) in str(unlisted.value)


@pytest.mark.deliberate("Q73")
def test_q73_exclude_forms_and_stacking() -> None:
    """The bare form returns the function, the called form works, and order is free."""

    def fresh_hamilton(x: int) -> int:
        return x

    def fresh_bare(x: int) -> int:
        return x

    def fresh_called(x: int) -> int:
        return x

    # Hamilton's is an instance that only works bare, and returns the function.
    assert not isinstance(hamilton_exclude, type)
    assert hamilton_exclude(fresh_hamilton) is fresh_hamilton
    with pytest.raises(
        TypeError, match=r"missing 1 required positional argument: 'fn'"
    ):
        hamilton_exclude()  # the call under test
    assert fnloom.exclude(fresh_bare) is fresh_bare
    assert fnloom.exclude()(fresh_called) is fresh_called
    # Stacked with `@tag` either way round, both exclude.
    hamilton_stacked = make_module(
        "differential_q73_stack_hamilton",
        _Q73_STACKED.format(
            exclude="hamilton.function_modifiers",
            name="hamilton_exclude",
            tag="hamilton.function_modifiers",
        ),
    )
    fnloom_stacked = make_module(
        "differential_q73_stack_fnloom",
        _Q73_STACKED.format(exclude="fnloom", name="exclude", tag="fnloom"),
    )
    hamilton = hamilton_driver.Builder().with_modules(hamilton_stacked).build()
    assert _q73_nodes(hamilton) == {"kept", "x"}
    assert fnloom.build(fnloom_stacked).node_names() == ["kept", "x"]


@pytest.mark.deliberate("Q73")
def test_q73_config_variants_are_listed_by_attribute_name() -> None:
    """Hamilton picks `price__us` though `__all__` omits it; fnloom does not."""
    hamilton_module = make_module(
        "differential_q73_variant_hamilton",
        _Q73_VARIANTS.format(
            import_="from hamilton.function_modifiers import config",
            when="config.when",
        ),
    )
    fnloom_module = make_module(
        "differential_q73_variant_fnloom",
        _Q73_VARIANTS.format(import_="import fnloom", when="fnloom.config.when"),
    )
    hamilton = (
        hamilton_driver.Builder()
        .with_modules(hamilton_module)
        .with_config({"region": "us"})
        .build()
    )
    assert hamilton.execute(["price"], inputs={"x": 2}) == {"price": 4}
    # Listed as `price__eu`, that variant is the only one considered.
    eu = fnloom.Builder().with_modules(fnloom_module).with_config({"region": "eu"})
    assert eu.build().execute(["price"], inputs={"x": 2}) == {"price": 2}
    us = fnloom.Builder().with_modules(fnloom_module).with_config({"region": "us"})
    with pytest.raises(ValueError, match=r"no variant of 'price'") as unmatched:
        us.build().execute(["price"], inputs={"x": 2})
    assert (
        "'price__us' (node 'price') is a function in module "
        "'differential_q73_variant_fnloom' not listed in its __all__"
    ) in str(unmatched.value)


@pytest.mark.deliberate("Q73")
def test_q73_empty_module_reasons() -> None:
    """Hamilton builds a module with every function excluded; fnloom says why not."""
    hamilton_module = make_module(
        "differential_q73_empty_hamilton",
        "from hamilton.function_modifiers import hamilton_exclude\n\n__all__ = []\n\n\n"
        "@hamilton_exclude\ndef both(x: int) -> int:\n    return x\n\n\n"
        "def unlisted(x: int) -> int:\n    return x\n",
    )
    # `unlisted` is a node for Hamilton, which ignores `__all__`.
    assert "unlisted" in _q73_nodes(
        hamilton_driver.Builder().with_modules(hamilton_module).build()
    )
    fnloom_module = make_module(
        "differential_q73_empty_fnloom",
        "import fnloom\n\n__all__ = []\n\n\n"
        "@fnloom.exclude\ndef both(x: int) -> int:\n    return x\n\n\n"
        "@fnloom.exclude\ndef marked(x: int) -> int:\n    return x\n\n\n"
        "def unlisted(x: int) -> int:\n    return x\n",
    )
    with pytest.raises(ValueError, match=r"has no nodes") as empty:
        fnloom.build(fnloom_module)
    message = str(empty.value)
    assert "'both' is marked @exclude and not listed in its __all__" in message
    assert "'unlisted' is not listed in its __all__" in message


@pytest.mark.deliberate("Q73")
def test_q73_final_vars_identity_refuses_an_excluded_function() -> None:
    """Hamilton maps an excluded function to its name; fnloom decides by identity."""
    source_first = "def total(x: int) -> int:\n    return x * 2\n"
    source_second = (
        "{import_}\n\n\n@{name}\ndef total(x: int) -> int:\n    return x * 3\n"
        "\n\ndef other(x: int) -> int:\n    return x\n"
    )
    h_first = make_module("differential_q73_id_h1", source_first)
    h_second = make_module(
        "differential_q73_id_h2",
        source_second.format(
            import_="from hamilton.function_modifiers import hamilton_exclude",
            name="hamilton_exclude",
        ),
    )
    f_first = make_module("differential_q73_id_f1", source_first)
    f_second = make_module(
        "differential_q73_id_f2",
        source_second.format(import_="import fnloom", name="fnloom.exclude"),
    )
    hamilton = (
        hamilton_driver.Builder()
        .with_modules(h_first, h_second)
        .allow_module_overrides()
        .build()
    )
    assert hamilton.execute([h_second.total], inputs={"x": 2}) == {"total": 4}
    built = fnloom.Builder().with_modules(f_first, f_second).build()
    assert built.execute([f_first.total], inputs={"x": 2}) == {"total": 4}
    with pytest.raises(ValueError, match=r"not the function behind node 'total'"):
        built.execute([f_second.total], inputs={"x": 2})


@pytest.mark.deliberate("Q73")
def test_q73_malformed_all_names_every_module_sorted() -> None:
    """Hamilton builds a malformed `__all__`; fnloom lists each module, sorted."""
    bad = "__all__ = {}\n\n\ndef a(x: int) -> int:\n    return x\n"
    hamilton_modules = [
        make_module("differential_q73_bad_h_zed", bad.format("'a'")),
        make_module("differential_q73_bad_h_alpha", bad.format("[1]")),
    ]
    hamilton_driver.Builder().with_modules(
        *hamilton_modules
    ).allow_module_overrides().build()
    zed = make_module("differential_q73_bad_zed", bad.format("'a'"))
    alpha = make_module("differential_q73_bad_alpha", bad.format("[1]"))
    with pytest.raises(TypeError) as malformed:
        fnloom.Builder().with_modules(zed, alpha).build()
    message = str(malformed.value)
    assert "__all__ must be a list or tuple of str" in message
    assert message.index("'differential_q73_bad_alpha'") < message.index(
        "'differential_q73_bad_zed'"
    )


_Q74_FIRST = """
def dup() -> int:
    return 1


def uses_dup(dup: int) -> int:
    return dup + 100


def only_a(x: int) -> int:
    return x
"""

_Q74_SUBDAG = """
from hamilton.function_modifiers import subdag

import differential_q74_first


@subdag(differential_q74_first, inputs={{}}, config={{}})
def sub(only_a: int) -> int:
    return only_a
"""


def _q74_dependencies(driver: hamilton_driver.Driver) -> dict[str, list[str]]:
    """Each Hamilton node's required dependencies, sorted, by name."""
    return {
        variable.name: sorted(variable.required_dependencies)
        for variable in driver.list_available_variables()
    }


@pytest.mark.deliberate("Q74")
def test_q74_namespaces_and_no_module_overrides() -> None:
    """Hamilton namespaces only by `@subdag` and overrides last-wins; fnloom neither."""
    first = make_module("differential_q74_first", _Q74_FIRST)
    second = make_module("differential_q74_second", "def dup() -> int:\n    return 2\n")
    # Hamilton: `with_modules` takes modules only, and no builder method namespaces.
    assert list(inspect.signature(hamilton_driver.Builder.with_modules).parameters) == [
        "self",
        "modules",
    ]
    assert not [
        name
        for name in dir(hamilton_driver.Builder)
        if "namespace" in name or "prefix" in name
    ]
    # Hamilton: a name two modules define fails the build...
    with pytest.raises(ValueError, match="Cannot define function dup more than once"):
        hamilton_driver.Builder().with_modules(first, second).build()
    # ...unless overrides are on: the later module's function wins, for every reader.
    for order, dup in (((first, second), 2), ((second, first), 1)):
        hamilton = (
            hamilton_driver.Builder()
            .with_modules(*order)
            .allow_module_overrides()
            .build()
        )
        assert hamilton.execute(["dup", "uses_dup"]) == {
            "dup": dup,
            "uses_dup": dup + 100,
        }
    # Hamilton: one module given twice is one set of nodes, overrides on: no reuse.
    twice = (
        hamilton_driver.Builder()
        .with_modules(first, first)
        .allow_module_overrides()
        .build()
    )
    assert {v.name for v in twice.list_available_variables()} == {
        "dup",
        "uses_dup",
        "only_a",
        "x",
    }
    # Hamilton: `@subdag` names its nodes `<function>.<name>`; inside, `uses_dup`
    # reads the subdag's `dup`, and `only_a` the plain input `x`.
    subdag_module = make_module("differential_q74_subdag", _Q74_SUBDAG.format())
    subdag = hamilton_driver.Builder().with_modules(subdag_module).build()
    subdag_dependencies = _q74_dependencies(subdag)
    assert subdag_dependencies["sub.uses_dup"] == ["sub.dup"]
    assert subdag_dependencies["sub.only_a"] == ["x"]
    # fnloom: `namespace=` gives the module's nodes the same names, read the same way.
    driver = fnloom.Builder().with_modules(first, namespace="sub").build()
    assert driver.node_names() == ["sub.dup", "sub.only_a", "sub.uses_dup", "x"]
    assert set(driver.node_names()) < set(subdag_dependencies)
    assert driver.nodes["sub.uses_dup"].dependencies == ("sub.dup",)
    assert driver.nodes["sub.only_a"].dependencies == ("x",)
    assert driver.execute(["sub.uses_dup", "sub.only_a"], inputs={"x": 3}) == (
        subdag.execute(["sub.uses_dup", "sub.only_a"], inputs={"x": 3})
    )
    # fnloom: a name the namespace doesn't define reads the plain node.
    outer = (
        fnloom.Builder()
        .with_modules(second)
        .with_modules(
            make_module(
                "differential_q74_reader",
                "def reads(dup: int) -> int:\n    return dup\n",
            ),
            namespace="ns",
        )
        .build()
    )
    assert outer.nodes["ns.reads"].dependencies == ("dup",)
    assert outer.execute(["ns.reads"]) == {"ns.reads": 2}
    # fnloom: one module under two namespaces is two sets of nodes.
    reused = (
        fnloom.Builder()
        .with_modules(first, namespace="a")
        .with_modules(first, namespace="b")
        .build()
    )
    assert reused.execute(["a.uses_dup", "b.uses_dup"]) == {
        "a.uses_dup": 101,
        "b.uses_dup": 101,
    }
    # fnloom: nothing overrides; a name two modules define fails the build.
    assert not hasattr(fnloom.Builder, "allow_module_overrides")
    with pytest.raises(ValueError, match="node 'dup' is defined in both"):
        fnloom.Builder().with_modules(first, second).build()


@pytest.mark.deliberate("Q75")
def test_q75_strict_overrides_and_declared_inputs() -> None:
    """Hamilton refuses an unsupplied overridden input, and builds a misspelled one."""
    module = make_module("differential_q75", _Q75_SOURCE)
    hamilton = hamilton_driver.Builder().with_modules(module).build()
    plain = fnloom.Builder().with_modules(module)
    # An override of an unsupplied input: Hamilton refuses, fnloom's default does not.
    with pytest.raises(ValueError, match="Required input x not provided"):
        hamilton.execute(["a"], overrides={"x": 10})
    assert plain.build().execute(["a"], overrides={"x": 10}) == {"a": 11}
    # Opt in, and fnloom refuses too; supplied as well, both run.
    strict = plain.with_strict_overrides().build()
    with pytest.raises(ValueError, match="override of input 'x' refused"):
        strict.execute(["a"], overrides={"x": 10})
    assert hamilton.execute(["a"], inputs={"x": 1}, overrides={"x": 10}) == {"a": 11}
    assert strict.execute(["a"], inputs={"x": 1}, overrides={"x": 10}) == {"a": 11}
    # A misspelled parameter builds in Hamilton and is an input nothing supplies.
    assert "revenu" in {v.name for v in hamilton.list_available_variables()}
    declared = plain.with_declared_inputs("cost", "x")
    with pytest.raises(ValueError, match=r"undeclared input 'revenu' \(did you mean"):
        declared.build()
    assert declared.with_declared_inputs("revenu").build().execute(
        ["a"], inputs={"x": 1}
    ) == {"a": 2}


_Q76_SHADOWED = """
def cost() -> int:
    return 100


{opt_out}def uses_cost(cost: int = 3) -> int:
    return cost
"""

_Q76_DIFFERING = """
{opt_out}def opt1(shared: int = 1) -> int:
    return shared


{opt_out}def opt2(shared: int = 2) -> int:
    return shared
"""


@pytest.mark.deliberate("Q76")
def test_q76_shadowed_and_differing_defaults() -> None:
    """Hamilton lets a node replace a default and each consumer keep its own."""
    shadowed = make_module("differential_q76_s", _Q76_SHADOWED.format(opt_out=""))
    hamilton = hamilton_driver.Builder().with_modules(shadowed).build()
    # The node replaces the default, and a supplied input is ignored (H5.6c).
    assert hamilton.execute(["uses_cost"]) == {"uses_cost": 100}
    assert hamilton.execute(["uses_cost"], inputs={"cost": 1}) == {"uses_cost": 100}
    with pytest.raises(ValueError, match=r"parameter 'cost' of 'uses_cost'.*replaces"):
        fnloom.build(shadowed)
    # The opt-out restores Hamilton's behaviour: the node wins.
    out_s = _Q76_SHADOWED.format(opt_out="@fnloom.defaults(shadowed=('cost',))\n")
    allowed = make_module("differential_q76_s2", "import fnloom\n\n" + out_s)
    assert fnloom.build(allowed).execute(["uses_cost"]) == {"uses_cost": 100}
    # Two consumers, two defaults: each keeps its own in Hamilton (H5.6d).
    differing = make_module("differential_q76_d", _Q76_DIFFERING.format(opt_out=""))
    hamilton = hamilton_driver.Builder().with_modules(differing).build()
    assert hamilton.execute(["opt1", "opt2"]) == {"opt1": 1, "opt2": 2}
    assert hamilton.execute(["opt1", "opt2"], inputs={"shared": 9}) == {
        "opt1": 9,
        "opt2": 9,
    }
    with pytest.raises(ValueError, match="differing defaults: input 'shared'"):
        fnloom.build(differing)
    out_d = _Q76_DIFFERING.format(opt_out="@fnloom.defaults(differing=('shared',))\n")
    allowed = make_module("differential_q76_d2", "import fnloom\n\n" + out_d)
    driver = fnloom.build(allowed)
    assert driver.execute(["opt1", "opt2"]) == {"opt1": 1, "opt2": 2}
    assert driver.execute(["opt1", "opt2"], inputs={"shared": 9}) == {
        "opt1": 9,
        "opt2": 9,
    }


_Q77_SOURCE = """
from {library} import parameterize, source, value


@parameterize(r1={{"p": value(1), "s": source("src")}})
def f(p: int, s: int) -> int:
    return p + s
{reader}"""

# Each library, and the module it takes `parameterize`, `source` and `value` from.
_Q77_LIBRARIES = {"hamilton": "hamilton.function_modifiers", "fnloom": "fnloom"}

_Q77_READER = """

def g(p: int) -> int:
    return p * 100
"""


@pytest.mark.deliberate("Q77")
def test_q77_a_value_bound_name_nothing_reads() -> None:
    """Hamilton drops an input for a `value()`-bound name; fnloom refuses it (Q4)."""
    alone = {
        library: make_module(
            f"differential_q77_{library}",
            _Q77_SOURCE.format(library=module, reader=""),
        )
        for library, module in _Q77_LIBRARIES.items()
    }
    inputs = {"src": 1, "p": 99}
    hamilton = hamilton_driver.Builder().with_modules(alone["hamilton"]).build()
    assert hamilton.execute(["r1"], inputs=inputs) == {"r1": 2}
    assert hamilton.execute(["r1"], inputs={"src": 1}, overrides={"p": 99}) == {"r1": 2}
    # Even a wrong-typed one (H5.4e).
    wrong = {"src": 1, "p": "not an int"}
    assert hamilton.execute(["r1"], inputs=wrong) == {"r1": 2}
    assert hamilton.validate_execution(["r1"], inputs=wrong) is None
    built = fnloom.build(alone["fnloom"])
    with pytest.raises(ValueError, match="'p'"):
        built.execute(["r1"], inputs=wrong)
    with pytest.raises(ValueError, match="'p'"):
        built.execute(["r1"], inputs=inputs)
    with pytest.raises(ValueError, match="'p'"):
        built.execute(["r1"], inputs={"src": 1}, overrides={"p": 99})
    # Another node reads the name: a real input, fed to that node alone, in both.
    shared = {
        library: make_module(
            f"differential_q77_read_{library}",
            _Q77_SOURCE.format(library=module, reader=_Q77_READER),
        )
        for library, module in _Q77_LIBRARIES.items()
    }
    expected = {"r1": 2, "g": 9900}
    hamilton = hamilton_driver.Builder().with_modules(shared["hamilton"]).build()
    assert hamilton.execute(["r1", "g"], inputs=inputs) == expected
    assert (
        fnloom.build(shared["fnloom"]).execute(["r1", "g"], inputs=inputs) == expected
    )


@pytest.mark.deliberate("Q78")
@pytest.mark.parametrize("fixture", ["chain", "optional"])
def test_q78_unsupplied_input_as_an_output(fixture: str) -> None:
    """Hamilton returns `None` for an unsupplied input, even one with a default."""
    module = load_fixture(fixture)
    requested = "a" if fixture == "chain" else "factor"
    assert run_hamilton([module], [requested]) == Outcome({requested: None}, None)
    assert run_fnloom([module], [requested]) == Outcome(None, "ValueError")
    with pytest.raises(ValueError, match=rf"missing inputs: '{requested}'"):
        fnloom.build(module).execute([requested])


@pytest.mark.deliberate("Q79")
def test_q79_none_passes_only_types_that_admit_it() -> None:
    """Hamilton passes `None` for every input type; fnloom where the type admits it."""
    refused = [declared for declared, _ in _pairs("Q79")]
    assert refused == [
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
    for declared in refused:
        assert hamilton_value(declared, "None") is True, declared
        assert fnloom_value(declared, "None") is False, declared
    for declared in ("Optional[int]", "int | None", "object", "Any", "TypeVar"):
        assert hamilton_value(declared, "None") is True, declared
        assert fnloom_value(declared, "None") is True, declared
    module = make_module(
        "differential_q79",
        """
from typing import Literal, TypeVar

RAN: list[str] = []
Bound = TypeVar("Bound", bound=int | None)
Either = TypeVar("Either", int, None)

def plain(x: int) -> object:
    RAN.append("plain")
    return x

def optional(y: int | None) -> object:
    return y

def nothing(z: None) -> object:
    return z

def literal(w: Literal[None]) -> object:
    return w

def bound(b: Bound) -> object:
    return b

def constrained(c: Either) -> object:
    return c
""",
    )
    assert run_hamilton([module], ["plain"], {"x": None}) == Outcome(
        {"plain": None}, None
    )
    assert module.RAN == ["plain"]
    module.RAN.clear()
    assert run_fnloom([module], ["plain"], {"x": None}) == Outcome(None, "ValueError")
    assert module.RAN == []
    for output, name in (
        ("optional", "y"),
        ("nothing", "z"),
        ("literal", "w"),
        ("bound", "b"),
        ("constrained", "c"),
    ):
        expected = Outcome({output: None}, None)
        assert run_hamilton([module], [output], {name: None}) == expected, output
        assert run_fnloom([module], [output], {name: None}) == expected, output


@pytest.mark.deliberate("Q80")
def test_q80_fixed_tuples_check_length_and_items() -> None:
    """Hamilton refuses every fixed-length tuple; fnloom checks length and items."""
    module = make_module(
        "differential_q80",
        """
def pair(p: tuple[int, str]) -> object:
    return p

def empty(e: tuple[()]) -> object:
    return e
""",
    )
    refused = Outcome(None, "ValueError")
    for output, name, right in (("pair", "p", (1, "a")), ("empty", "e", ())):
        assert run_hamilton([module], [output], {name: right}) == refused, output
        assert run_fnloom([module], [output], {name: right}) == Outcome(
            {output: right}, None
        )
    for output, name, wrong in (
        ("pair", "p", ("a", 1)),
        ("pair", "p", (1,)),
        ("empty", "e", (1,)),
    ):
        assert run_hamilton([module], [output], {name: wrong}) == refused, wrong
        assert run_fnloom([module], [output], {name: wrong}) == refused, wrong


_Q81_SOURCE = """
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from decimal import Decimal

def rate() -> "Decimal":
    raise AssertionError("never run")

def price(rate: "Decimal") -> object:
    return rate

def loose(rate: Any) -> object:
    return rate

def converted(given: "Decimal") -> object:
    return given

def anything() -> Any:
    return 1

def takes(anything: "Decimal") -> object:
    return anything
"""


@pytest.mark.deliberate("Q81")
def test_q81_unresolved_annotations_build_as_opaque_types() -> None:
    """Hamilton raises `NameError`, naming no node; fnloom builds (H5.10a)."""
    module = make_module("differential_q81", _Q81_SOURCE)
    with pytest.raises(NameError, match=r"^name 'Decimal' is not defined$") as raised:
        hamilton_driver.Builder().with_modules(module).build()
    assert not any(name in str(raised.value) for name in ("rate", "price", "converted"))
    built = fnloom.build(module)
    unresolved = fnloom_types.Unresolved("Decimal")
    assert repr(unresolved) == "unresolved 'Decimal'"
    assert built.nodes["rate"].type == unresolved
    # The same text, and `Any`, are fed by it: both edges built.
    assert built.what_is_the_path_between("rate", "price") == ["rate", "price"]
    assert built.what_is_the_path_between("rate", "loose") == ["rate", "loose"]
    # And from `Any`: a node returning `Any` feeds it, and runs unchecked.
    assert built.what_is_the_path_between("anything", "takes") == ["anything", "takes"]
    assert built.execute(["takes"]) == {"takes": 1}
    for value in (1, None):
        with pytest.raises(
            TypeError, match=r"^given: .*import what it names at run time"
        ):
            built.execute(["converted"], inputs={"given": value})
        with pytest.raises(
            TypeError, match=r"^rate: .*import what it names at run time"
        ):
            built.execute(["price"], overrides={"rate": value})
    other = make_module(
        "differential_q81_other",
        _Q81_SOURCE + "\ndef counted(rate: int) -> object:\n    return rate\n",
    )
    with pytest.raises(NameError):
        hamilton_driver.Builder().with_modules(other).build()
    with pytest.raises(
        ValueError,
        match=r"^counted\.rate requires int but rate returns unresolved 'Decimal'",
    ):
        fnloom.build(other)


_Q82_HEAD = (
    "from {library} import (\n"
    "    group, parameterize, parameterize_sources, parameterize_values,\n"
    "    source, value)\n"
)
_Q82_CASES = {
    "unknown": '''
@parameterize(a={"p": value(1)})
def f(p: int) -> int:
    """has {ghost} here"""
    return p
''',
    "brace": '''
@parameterize(a={"p": value(1)})
def f(p: int) -> int:
    """stray } here"""
    return p
''',
    "group": '''
@parameterize(a={"xs": group(source("q"), value(3))})
def f(xs: list[int]) -> int:
    """has {xs} here"""
    return 1
''',
    "positional": '''
@parameterize(a={"p": value(1)})
def f(p: int) -> int:
    """has {0} here"""
    return p
''',
    "default": """
@parameterize(a={"xs": group(source("p"), source("q"))})
def f(xs: list[int] = [7]) -> int:  # noqa: B006
    return sum(xs)
""",
    "one": """
@parameterize(a=({"p": value(1)},))
def f(p: int) -> int:
    return p
""",
    "three": """
@parameterize(a=({"p": value(1)}, "d", "extra"))
def f(p: int) -> int:
    return p
""",
    "number": """
@parameterize(a=({"p": value(1)}, 5))
def f(p: int) -> int:
    return p
""",
    "source": """
@parameterize_sources(a=dict(p=5))
def f(p: int) -> int:
    return p
""",
    "empty": """
@parameterize_values("p", {})
def f(p: int) -> int:
    return p
""",
    "key": """
@parameterize_values("p", {("a", "b", "c"): 1})
def f(p: int) -> int:
    return p
""",
    "listed": """
@parameterize(a={"xs": group(source("q"))})
def f(xs: int) -> int:
    return 1
""",
}


def _q82_decorate(library: str) -> dict[str, ModuleType | tuple[str, str]]:
    """Each case as `library` takes it: its module, or the error's class and text."""
    head = _Q82_HEAD.format(library=library)
    results: dict[str, ModuleType | tuple[str, str]] = {}
    for label, body in _Q82_CASES.items():
        try:
            results[label] = make_module(f"q82_{library}_{label}", head + body)
        except Exception as error:  # noqa: BLE001  # the class and text are what is compared
            results[label] = (type(error).__name__, str(error))
    return results


def _refused(outcome: ModuleType | tuple[str, str]) -> tuple[str, str]:
    """The exception class and text of a case that raised."""
    assert isinstance(outcome, tuple)
    return outcome


def _decorated(outcome: ModuleType | tuple[str, str]) -> ModuleType:
    """The module of a case that decorated without error."""
    assert isinstance(outcome, types.ModuleType)
    return outcome


@pytest.mark.deliberate("Q82")
def test_q82_parameterize_sugar_is_checked_when_decorating() -> None:
    """Hamilton prints, drops or fails late; fnloom refuses when decorating."""
    hamilton = _q82_decorate("hamilton.function_modifiers")
    ours = _q82_decorate("fnloom")

    def hamilton_graph(label: str) -> hamilton_driver.Driver:
        return (
            hamilton_driver.Builder().with_modules(_decorated(hamilton[label])).build()
        )

    # A placeholder the row lacks: Hamilton prints the bare key.
    assert hamilton_graph("unknown").graph.nodes["a"].documentation == "has ghost here"
    kind, text = _refused(ours["unknown"])
    assert kind == "ValueError"
    assert "row 'a': placeholder {ghost} is not 'output_name'" in text
    # A stray brace is a ValueError in both, but only fnloom names the row.
    kind, text = _refused(hamilton["brace"])
    assert (kind, "row" in text) == ("ValueError", False)
    kind, text = _refused(ours["brace"])
    assert (kind, "row 'a':" in text) == ("ValueError", True)
    kind, text = _refused(hamilton["positional"])
    assert (kind, "positional fields" in text, "row" in text) == (
        "ValueError",
        True,
        False,
    )
    kind, text = _refused(ours["positional"])
    assert (kind, "positional fields" in text, "row 'a':" in text) == (
        "ValueError",
        True,
        True,
    )
    # A group prints as its name in Hamilton, as its items joined in fnloom.
    assert hamilton_graph("group").graph.nodes["a"].documentation == "has xs here"
    built = fnloom.build(_decorated(ours["group"]))
    assert built.nodes["a"].doc == "has q, 3 here"
    # A tuple row of 1 item is a raw IndexError, one of 3 drops the extra, and a
    # non-str doc is kept; fnloom refuses all three with a TypeError.
    assert _refused(hamilton["one"])[0] == "IndexError"
    assert _refused(ours["one"])[0] == "TypeError"
    assert hamilton_graph("three").graph.nodes["a"].documentation == "d"
    assert _refused(ours["three"])[0] == "TypeError"
    assert hamilton_graph("number").graph.nodes["a"].documentation == int("5")
    assert _refused(ours["number"])[0] == "TypeError"
    # A non-str source: Hamilton fails when the graph is used, fnloom when decorating.
    built_source = hamilton_graph("source")
    with pytest.raises(TypeError, match="expected str instance, int found"):
        built_source.execute(["a"], inputs={"5": 1})
    kind, text = _refused(ours["source"])
    assert (kind, "a source is a str, got int" in text) == ("TypeError", True)
    # An empty assigned_output builds no node in Hamilton and is refused by fnloom.
    assert list(hamilton_graph("empty").graph.nodes) == []
    assert _refused(ours["empty"])[0] == "ValueError"
    # A key of 3 items: a raw unpacking error against a clear refusal.
    assert _refused(hamilton["key"]) == (
        "ValueError",
        "too many values to unpack (expected 2, got 3)",
    )
    kind, text = _refused(ours["key"])
    assert (kind, "must be a (name, doc) tuple" in text) == ("TypeError", True)
    # A group source is optional when the parameter has a default, so a partly
    # supplied group is a raw KeyError; fnloom requires it and names the input.
    optional = hamilton_graph("default")
    assert optional.graph.nodes["a"].input_types["q"][1].name == "OPTIONAL"
    with pytest.raises(KeyError, match="q"):
        optional.execute(["a"], inputs={"p": 1})
    required = fnloom.build(_decorated(ours["default"]))
    assert required.inputs()["q"].required
    with pytest.raises(ValueError, match="missing inputs: 'q'"):
        required.execute(["a"], inputs={"p": 1})
    assert required.execute(["a"], inputs={"p": 1, "q": 2}) == {"a": 3}
    # A mis-annotated group parameter is listed twice by Hamilton, once by fnloom.
    assert "xs (<class 'int'>), xs (<class 'int'>)" in _refused(hamilton["listed"])[1]
    once = _refused(ours["listed"])[1]
    assert "'xs' (int) needs list[X] for its group" in once
    assert once.count("'xs'") == 1


_Q83_BEFORE = """
def a(x: int) -> int:
    return x + 1


def b(a: int) -> int:
    return a * 10
"""

_Q83_AFTER = """
def a(x: int) -> int:
    return x + 1000


def b(a: int) -> int:
    return a * 10


def c(b: int) -> int:
    return b + 1
"""


@pytest.mark.deliberate("Q83")
def test_q83_rebuild_after_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both keep a driver's code across a reload; only fnloom builds it again."""
    monkeypatch.syspath_prepend(str(tmp_path))
    # No bytecode, so the rewrite is never read from a stale cache.
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    path = tmp_path / "q83_reloaded.py"
    path.write_text(_Q83_BEFORE, encoding="utf-8")
    module = importlib.import_module("q83_reloaded")
    hamilton = hamilton_driver.Builder().with_modules(module).build()
    store = fnloom.MemoryCache()
    ours = fnloom.Builder().with_modules(module).with_cache(store).build()
    old_a = module.a
    assert hamilton.execute(["b"], inputs={"x": 1}) == {"b": 20}
    assert ours.execute(["b"], inputs={"x": 1}) == {"b": 20}

    def version() -> object:
        found = [n for n in hamilton.list_available_variables() if n.name == "a"]
        return found[0].version

    before = version()
    path.write_text(_Q83_AFTER, encoding="utf-8")
    # Hamilton's version reads the edited file before any reload; fnloom's code
    # version is the built one, so its store still hits.
    assert version() != before
    assert ours.explain_cache(["a"], inputs={"x": 1})["a"].status == "hit"
    importlib.reload(module)
    assert module.a is not old_a

    # Both run the old code, and know no node the edit added.
    assert hamilton.execute(["b"], inputs={"x": 1}) == {"b": 20}
    assert ours.execute(["b"], inputs={"x": 1}) == {"b": 20}
    with pytest.raises(ValueError, match=r"Unknown nodes \[c\] requested"):
        hamilton.execute(["c"], inputs={"x": 1})
    with pytest.raises(ValueError, match="unknown outputs: 'c'"):
        ours.execute(["c"], inputs={"x": 1})
    # The reloaded function: Hamilton maps its name to the old node; fnloom refuses.
    assert hamilton.execute([module.a], inputs={"x": 1}) == {"a": 2}
    with pytest.raises(ValueError, match="is not the function behind node 'a'"):
        ours.execute([module.a], inputs={"x": 1})

    # Only a new Builder builds Hamilton again; `rebuild` does it for fnloom.
    assert not hasattr(hamilton, "rebuild")
    fresh = hamilton_driver.Builder().with_modules(module).build()
    assert fresh.execute(["c"], inputs={"x": 1}) == {"c": 10011}
    rebuilt = ours.rebuild()
    assert rebuilt.explain_cache(["a"], inputs={"x": 1})["a"].status == "miss"
    assert rebuilt.execute(["c"], inputs={"x": 1}) == {"c": 10011}
    assert rebuilt.execute([module.a], inputs={"x": 1}) == {"a": 1001}
    built_async = fnloom.Builder().with_modules(module).build_async()
    assert not hasattr(hamilton_async.AsyncDriver, "rebuild")
    assert asyncio.run(built_async.rebuild().execute(["c"], inputs={"x": 1})) == {
        "c": 10011
    }


@pytest.mark.deliberate("Q84")
def test_q84_typed_events_checked_when_attached() -> None:
    """Hamilton passes keywords and checks nothing; fnloom, events checked early."""
    module = make_module("q84_hooked", HOOKED_GRAPH)
    log: list[HookEntry] = []
    hamilton_hook = HamiltonRecorder("H", log)
    hamilton = (
        hamilton_driver.Builder()
        .with_modules(module)
        .with_adapters(hamilton_hook, object(), hamilton_hook)  # ty: ignore[invalid-argument-type]  # the case under test
        .build()
    )
    assert hamilton.execute(["c"], inputs={"x": 1}) == {"c": 7}
    # A non-adapter is ignored, and the instance given twice is called once.
    assert [stage for _, stage, _ in log].count("before_run") == 1
    before, end = hamilton_hook.calls[0], hamilton_hook.calls[-1]
    assert {"final_vars", "inputs", "overrides", "execution_path"} <= before.keys()
    assert not {"final_vars", "inputs", "overrides"} & end.keys()
    assert sorted(before["execution_path"]) == ["a", "b", "c"]
    # A defaulted parameter left alone is absent from a node's arguments in both.
    assert hamilton_hook.calls[1]["node_kwargs"] == {"x": 1}

    ours = FnloomRecorder("F", [])
    built = fnloom.Builder().with_modules(module).with_hooks(ours).build()
    assert built.execute(["c"], inputs={"x": 1}) == {"c": 7}
    start, finish = ours.events[0], ours.events[-1]
    assert isinstance(start, fnloom.RunStart)
    assert isinstance(finish, fnloom.RunEnd)
    assert (start.nodes, finish.outputs, dict(finish.inputs)) == (
        ("a", "b", "c"),
        ("c",),
        {"x": 1},
    )
    with pytest.raises(TypeError, match=r"object is not a fnloom\.Hook"):
        fnloom.Builder().with_hooks(cast("fnloom.Hook", object()))
    with pytest.raises(ValueError, match="the same FnloomRecorder is given twice"):
        fnloom.Builder().with_hooks(ours, ours)

    # A signature that can't take the call: Hamilton fails at the first call.
    class Narrow(lifecycle.NodeExecutionHook):
        @override
        def run_before_node_execution(self, *, node_name: str) -> None:  # ty: ignore[invalid-method-override]  # the case under test
            """Takes too little."""

        @override
        def run_after_node_execution(self, **kwargs: Any) -> None:
            """Takes everything."""

    narrow = hamilton_driver.Builder().with_modules(module).with_adapters(Narrow())
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        narrow.build().execute(["c"], inputs={"x": 1})

    class NarrowHook(fnloom.Hook):
        @override
        def before_node(self) -> None:  # ty: ignore[invalid-method-override]  # the case under test
            """Takes nothing."""

    with pytest.raises(TypeError, match=r"NarrowHook\.before_node must take the event"):
        fnloom.Builder().with_hooks(NarrowHook())

    # An `async def` hook on the sync driver: Hamilton never awaits it.
    ran: list[str] = []

    class Awaiting(lifecycle.NodeExecutionHook):
        @override
        async def run_before_node_execution(self, **kwargs: Any) -> None:
            ran.append(kwargs["node_name"])

        @override
        def run_after_node_execution(self, **kwargs: Any) -> None:
            """Takes everything."""

    awaiting = hamilton_driver.Builder().with_modules(module).with_adapters(Awaiting())
    with pytest.warns(RuntimeWarning, match="was never awaited"):
        assert awaiting.build().execute(["a"], inputs={"x": 1}) == {"a": 2}
    assert ran == []

    class AwaitingHook(fnloom.Hook):
        @override
        async def before_node(self, event: fnloom.NodeStart) -> None:  # ty: ignore[invalid-method-override]  # the case under test
            ran.append(event.node.name)

    with pytest.raises(TypeError, match=r"AwaitingHook\.before_node is an async def"):
        fnloom.Builder().with_hooks(AwaitingHook())


# Each node records that it ran in another module, found by name, so no code
# version reads a mutable global.
_Q85_GRAPH = """
def a(x: int) -> int:
    __import__("differential_q85_log").RAN.append("a")
    return x + 1


def b(a: int) -> int:
    __import__("differential_q85_log").RAN.append("b")
    return a * 2
"""


@pytest.mark.deliberate("Q85")
def test_q85_cache_outcomes_on_node_events(tmp_path: Path) -> None:
    """Hamilton fires hit nodes' hooks as if they ran; fnloom reports each outcome."""
    ran = cast("list[str]", make_module("differential_q85_log", "RAN = []").RAN)
    module = _file_module(tmp_path / "src", "differential_q85", _Q85_GRAPH)
    log: list[HookEntry] = []
    hook = HamiltonRecorder("H", log)
    hamilton = (
        hamilton_driver.Builder()
        .with_modules(module)
        .with_cache(path=tmp_path / "hamilton")
        .with_adapters(hook)
        .build()
    )
    assert hamilton.execute(["b"], inputs={"x": 1}) == {"b": 4}
    first, first_calls = list(log), list(hook.calls)
    log.clear()
    hook.calls.clear()
    ran.clear()
    assert hamilton.execute(["b"], inputs={"x": 1}) == {"b": 4}
    # No node ran, yet every node, `a` upstream of the hit too, reports as if it had,
    # with the same arguments and result, and nothing saying it was a hit.
    assert ran == []
    assert log == first
    nodes = [i for i, (_, stage, _) in enumerate(log) if stage.endswith("_node")]
    for i in nodes:
        for key in ("node_name", "node_kwargs", "result", "success"):
            assert hook.calls[i].get(key) == first_calls[i].get(key)
        assert not [key for key in hook.calls[i] if "cache" in key]

    ours = FnloomRecorder("F", [])
    built = (
        fnloom.Builder()
        .with_modules(module)
        .with_cache(DiskCache(tmp_path / "fnloom"))
        .with_hooks(ours)
        .build()
    )
    assert built.execute(["b"], inputs={"x": 1}) == {"b": 4}
    miss = CacheOutcome("miss", None)
    assert [(e.node.name, e.cache) for e in ours.events[1:-1]] == [
        ("a", miss),
        ("a", miss),
        ("b", miss),
        ("b", miss),
    ]
    ours.events.clear()
    ran.clear()
    assert built.execute(["b"], inputs={"x": 1}) == {"b": 4}
    # Only the hit reports, paired, with no inputs and its loaded value.
    assert ran == []
    start, hit_start, hit_end, end = ours.events
    assert start.nodes == end.nodes == ("b",)
    hit = CacheOutcome("hit", None)
    assert (hit_start.node.name, hit_start.inputs, hit_start.cache) == ("b", None, hit)
    assert (hit_end.inputs, hit_end.cache, hit_end.ok, hit_end.result) == (
        None,
        hit,
        True,
        4,
    )


class _Raising(HamiltonRecorder):
    """A Hamilton recorder that raises in one stage, after logging it.

    A node stage raises for node `at` only.
    """

    def __init__(
        self, name: str, log: list[HookEntry], stage: str, at: str | None = None
    ) -> None:
        super().__init__(name, log)
        self.stage = stage
        self.at = at

    @override
    def _record(self, stage: str, node: str | None, kwargs: dict[str, Any]) -> None:
        super()._record(stage, node, kwargs)
        if stage == self.stage and self.at in {None, node}:
            msg = f"{self.name} {stage}"
            raise RuntimeError(msg)


class _RaisingHook(FnloomRecorder):
    """The fnloom hook that raises as `_Raising` does."""

    def __init__(
        self, name: str, log: list[HookEntry], stage: str, at: str | None = None
    ) -> None:
        super().__init__(name, log)
        self.stage = stage
        self.at = at

    @override
    def _record(self, stage: str, node: str | None, event: object) -> None:
        super()._record(stage, node, event)
        if stage == self.stage and self.at in {None, node}:
            msg = f"{self.name} {stage}"
            raise RuntimeError(msg)


def _raising_logs(
    stage: str, final_vars: list[str], at: str | None = None
) -> tuple[list[HookEntry], BaseException, list[HookEntry], BaseException]:
    """Runs both with a raising hook `A` and a recorder `B`: their logs and errors."""
    module = make_module("q23_hooked", HOOKED_GRAPH)
    hamilton_log: list[HookEntry] = []
    hamilton = (
        hamilton_driver.Builder()
        .with_modules(module)
        .with_adapters(
            _Raising("A", hamilton_log, stage, at), HamiltonRecorder("B", hamilton_log)
        )
        .build()
    )
    with pytest.raises(BaseException) as hamilton_error:  # noqa: PT011  # compared below
        hamilton.execute(cast("Any", final_vars), inputs={"x": 1})
    log: list[HookEntry] = []
    built = (
        fnloom.Builder()
        .with_modules(module)
        .with_hooks(_RaisingHook("A", log, stage, at), FnloomRecorder("B", log))
        .build()
    )
    with pytest.raises(BaseException) as error:  # noqa: PT011  # compared below
        built.execute(final_vars, inputs={"x": 1})
    return hamilton_log, hamilton_error.value, log, error.value


@pytest.mark.deliberate("Q23")
def test_q23_every_hook_sees_the_event_and_no_error_is_masked() -> None:
    """Hamilton skips a stage's later hooks and masks a node's error; fnloom doesn't."""
    # A raising before-node hook: Hamilton skips B's, then runs after-graph.
    hamilton_log, hamilton_error, log, error = _raising_logs("before_node", ["c"])
    assert ("B", "before_node", "a") not in hamilton_log
    assert hamilton_log[-2:] == [("A", "after_run", None), ("B", "after_run", None)]
    assert ("B", "before_node", "a") in log
    assert log[-2:] == hamilton_log[-2:]
    assert str(hamilton_error) == str(error) == "A before_node"

    # A raising before-graph hook: Hamilton skips every other hook, after-graph too.
    hamilton_log, hamilton_error, log, error = _raising_logs("before_run", ["c"])
    assert hamilton_log == [("A", "before_run", None)]
    assert log == [
        ("A", "before_run", None),
        ("B", "before_run", None),
        ("A", "after_run", None),
        ("B", "after_run", None),
    ]
    assert str(hamilton_error) == str(error) == "A before_run"

    # A node fails, and an after-node hook raises: Hamilton raises the hook's error,
    # with the node's only in `__context__`; fnloom raises both, the node's first.
    hamilton_log, hamilton_error, log, error = _raising_logs(
        "after_node", ["after_boom"], "boom"
    )
    assert str(hamilton_error) == "A after_node"
    assert isinstance(hamilton_error.__context__, ValueError)
    assert str(hamilton_error.__context__) == "bad 2"
    assert isinstance(error, ExceptionGroup)
    node_error, hook_error = error.exceptions
    assert (type(node_error), str(node_error)) == (ValueError, "bad 2")
    assert str(hook_error) == "A after_node"
    assert ("B", "after_node", "boom") not in hamilton_log
    assert ("B", "after_node", "boom") in log


_Q86_GRAPH = """
import asyncio


async def slow(x: int) -> int:
    for _ in range(5):
        await asyncio.sleep(0)
    return x + 1


async def fast(x: int) -> int:
    await asyncio.sleep(0)
    return x + 2


def total(fast: int, slow: int) -> int:
    return fast + slow


async def bad(x: int) -> int:
    await asyncio.sleep(0)
    raise ValueError("async bad")
"""


@pytest.mark.deliberate("Q86")
@pytest.mark.filterwarnings(_HAMILTON_ASYNC_WARNING)
def test_q86_async_hooks_fire_as_nodes_run_with_values() -> None:
    """Hamilton's sync hooks fire at scheduling, with tasks; fnloom's as nodes run."""
    module = make_module("differential_q86", _Q86_GRAPH)
    hamilton_log: list[HookEntry] = []
    hamilton_hook = HamiltonRecorder("H", hamilton_log)

    async def hamilton_runs() -> tuple[dict[str, Any], BaseException]:
        pending: object = (
            hamilton_async.Builder()
            .with_modules(module)
            .with_adapters(hamilton_hook)
            .build()
        )
        built = await cast("Coroutine[Any, Any, hamilton_async.AsyncDriver]", pending)
        result = await built.execute(["total"], inputs={"x": 1})
        try:
            await built.execute(["bad"], inputs={"x": 1})
        except ValueError as error:
            return result, error
        pytest.fail("Hamilton's bad node did not raise")

    result, hamilton_error = asyncio.run(hamilton_runs())
    assert result == {"total": 5}
    # Each node is reported as it is scheduled, in an order that changes with
    # `PYTHONHASHSEED`: its after-node comes before the other node starts, though
    # `slow` and `fast` run at once and `fast` finishes first.
    assert hamilton_log[0] == ("H", "before_run", None)
    pairs = [hamilton_log[i : i + 2] for i in (1, 3, 5)]
    assert all(
        [stage for _, stage, _ in pair] == ["before_node", "after_node"]
        and pair[0][2] == pair[1][2]
        for pair in pairs
    )
    assert [pair[0][2] for pair in pairs][2] == "total"
    assert {pair[0][2] for pair in pairs[:2]} == {"slow", "fast"}
    calls = hamilton_hook.calls
    # `total`'s arguments, and every result, are tasks.
    assert {type(v) for v in calls[5]["node_kwargs"].values()} == {asyncio.Task}
    assert isinstance(calls[2]["result"], asyncio.Task)
    # A failing node is reported a success, with its task as the result.
    failed = calls[-2]
    assert failed["node_name"] == "bad"
    assert (failed["success"], failed["error"]) == (True, None)
    assert isinstance(failed["result"], asyncio.Task)
    assert calls[-1]["success"] is False
    assert calls[-1]["error"] is hamilton_error

    log: list[HookEntry] = []
    ours = FnloomRecorder("F", log)
    driver = fnloom.Builder().with_modules(module).with_hooks(ours).build_async()
    assert asyncio.run(driver.execute(["total"], inputs={"x": 1})) == {"total": 5}
    # Each node reports as it starts and as it finishes.
    assert log == [
        ("F", "before_run", None),
        ("F", "before_node", "fast"),
        ("F", "before_node", "slow"),
        ("F", "after_node", "fast"),
        ("F", "after_node", "slow"),
        ("F", "before_node", "total"),
        ("F", "after_node", "total"),
        ("F", "after_run", None),
    ]
    # `nodes` is the plan's order (R1), and the events carry values.
    assert ours.events[0].nodes == ("fast", "slow", "total")
    assert dict(ours.events[5].inputs) == {"fast": 3, "slow": 2}
    assert [e.result for e in ours.events[3:5]] == [3, 2]
    log.clear()
    with pytest.raises(ValueError, match="async bad") as raised:
        asyncio.run(driver.execute(["bad"], inputs={"x": 1}))
    failed_end = ours.events[-2]
    assert (failed_end.ok, failed_end.error, failed_end.result) == (
        False,
        raised.value,
        None,
    )
    assert (ours.events[-1].ok, ours.events[-1].error) == (False, raised.value)


class _BuildError(Exception):
    """What a raising result builder raises, in Hamilton and in fnloom."""


class _HamiltonRefuse(lifecycle.ResultBuilder):
    """A Hamilton result builder that always raises."""

    @override
    def build_result(self, **outputs: Any) -> Any:
        raise _BuildError(sorted(outputs))


def _refuse(outputs: dict[str, object]) -> object:
    """A fnloom result builder that always raises."""
    raise _BuildError(sorted(outputs))


@pytest.mark.deliberate("Q19")
@pytest.mark.filterwarnings(_HAMILTON_ASYNC_WARNING)
def test_q19_hooks_get_the_raw_results_and_the_async_driver_builds() -> None:
    """Hamilton's after-graph gets the built result, and its async driver drops it."""
    module = make_module("differential_q19", HOOKED_GRAPH)
    hamilton_hook = HamiltonRecorder("H", [])
    hamilton = (
        hamilton_driver.Builder()
        .with_modules(module)
        .with_adapters(hamilton_hook, HamiltonSeen())
        .build()
    )
    assert hamilton.execute(["c"], inputs={"x": 1}) == ("built", 1)
    assert hamilton_hook.calls[-1]["results"] == ("built", 1)
    # A builder that raises: after-graph gets the raw dict, in both (KEEP).
    failing = (
        hamilton_driver.Builder()
        .with_modules(module)
        .with_adapters(hamilton_hook, _HamiltonRefuse())
        .build()
    )
    with pytest.raises(_BuildError) as hamilton_raised:
        failing.execute(["c"], inputs={"x": 1})
    end = hamilton_hook.calls[-1]
    assert (end["success"], end["error"], end["results"]) == (
        False,
        hamilton_raised.value,
        {"c": 7},
    )

    async def hamilton_async_run() -> object:
        pending: object = (
            hamilton_async.Builder()
            .with_modules(module)
            .with_adapters(HamiltonSeen())
            .build()
        )
        built = await cast("Coroutine[Any, Any, hamilton_async.AsyncDriver]", pending)
        return await built.execute(["c"], inputs={"x": 1})

    # The async driver silently drops a `ResultBuilder` subclass.
    assert asyncio.run(hamilton_async_run()) == {"c": 7}

    ours = FnloomRecorder("F", [])
    builder = fnloom.Builder().with_modules(module).with_hooks(ours)
    assert builder.with_result_builder(FnloomSeen()).build().execute(
        ["c"], inputs={"x": 1}
    ) == ("built", 1)
    assert dict(ours.events[-1].results) == {"c": 7}
    with pytest.raises(_BuildError) as raised:
        builder.with_result_builder(_refuse).build().execute(["c"], inputs={"x": 1})
    finish = ours.events[-1]
    assert (finish.ok, finish.error, dict(finish.results)) == (
        False,
        raised.value,
        {"c": 7},
    )
    built_async = builder.with_result_builder(FnloomSeen()).build_async()
    assert asyncio.run(built_async.execute(["c"], inputs={"x": 1})) == ("built", 1)


@pytest.mark.deliberate("Q24")
def test_q24_one_result_builder_refused_at_the_call() -> None:
    """Hamilton refuses two builders at `build()`, naming the method; fnloom, both."""
    module = make_module("differential_q24", HOOKED_GRAPH)
    two = (
        hamilton_driver.Builder()
        .with_modules(module)
        .with_adapters(HamiltonSeen(), HamiltonSeen())
    )
    with pytest.raises(
        ValueError,
        match=r"Multiple adapters cannot \(currently\) implement the same lifecycle "
        r"method\. Sync methods: \['do_build_result'\]",
    ):
        two.build()
    # Hooks layer in both: two of one stage are fine.
    log: list[HookEntry] = []
    hooked = (
        hamilton_driver.Builder()
        .with_modules(module)
        .with_adapters(HamiltonRecorder("A", log), HamiltonRecorder("B", log))
        .build()
    )
    assert hooked.execute(["a"], inputs={"x": 1}) == {"a": 2}

    first = fnloom.Builder().with_modules(module).with_result_builder(FnloomSeen())
    with pytest.raises(
        ValueError,
        match=r"^a builder has one result builder: a FnloomSeen is set, "
        r"so _refuse is refused",
    ):
        first.with_result_builder(_refuse)


@pytest.mark.deliberate("Q89")
def test_q89_no_pandas_builder_and_the_recipe_pads_nothing() -> None:
    """Hamilton's frame flattens and NaN-pads; fnloom's recipe refuses the padding."""
    pandas = pytest.importorskip("pandas")
    from hamilton import base  # noqa: PLC0415  # only this test needs it

    module = make_module(
        "differential_q89",
        """
import pandas as pd


def longer() -> pd.Series:
    return pd.Series([1, 2, 3])


def shorter() -> pd.Series:
    return pd.Series([10, 20])


def table() -> pd.DataFrame:
    return pd.DataFrame({"p": [1, 2, 3], "q": [4, 5, 6]})
""",
    )
    hamilton = (
        hamilton_driver.Builder()
        .with_modules(module)
        .with_adapters(base.PandasDataFrameResult())
        .build()
    )
    padded = hamilton.execute(["longer", "shorter"])
    assert padded["shorter"].isna().tolist() == [False, False, True]
    assert str(padded["shorter"].dtype) == "float64"
    flattened = hamilton.execute(["table", "longer"])
    assert list(flattened.columns) == ["table.p", "table.q", "longer"]

    assert not hasattr(fnloom, "PandasDataFrameResult")
    # The recipe in docs/running.md, "Result builders".

    def frame(outputs: dict[str, object]) -> object:
        indexes = [v.index for v in outputs.values() if isinstance(v, pandas.Series)]
        if any(not index.equals(indexes[0]) for index in indexes[1:]):
            msg = f"outputs on different indexes: {', '.join(outputs)}"
            raise ValueError(msg)
        return pandas.DataFrame(outputs)

    ours = fnloom.Builder().with_modules(module).with_result_builder(frame).build()
    with pytest.raises(
        ValueError, match="outputs on different indexes: longer, shorter"
    ):
        ours.execute(["longer", "shorter"])
    # No column is flattened: pandas refuses a frame as one column.
    with pytest.raises(ValueError, match="Data must be 1-dimensional"):
        ours.execute(["table", "longer"])


Q87_RESULTS = """
from collections.abc import Callable
from typing import NewType

UserId = NewType("UserId", int)


def pair() -> tuple[int, str]:
    return (1, "a")


def short_pair() -> tuple[int, str]:
    return (1,)


def call() -> Callable[[int], int]:
    return abs


def frozen() -> frozenset[str]:
    return frozenset({"a"})


def user() -> UserId:
    return UserId(3)


def int_for_float() -> float:
    return 1


def fails() -> int:
    raise ValueError("original failure")
"""


@pytest.mark.deliberate("Q87")
def test_q87_output_checks_follow_the_input_rules_and_keep_a_nodes_error() -> None:
    """Hamilton's checker crashes, misreads tuples and masks failures; fnloom's not."""
    from hamilton.lifecycle import default  # noqa: PLC0415  # only this test needs it

    module = make_module("differential_q87", Q87_RESULTS)
    hamilton = (
        hamilton_driver.Builder()
        .with_modules(module)
        .with_adapters(default.FunctionInputOutputTypeChecker())
        .build()
    )
    # A correct fixed tuple is refused, and a short one accepted (#1749).
    with pytest.raises(TypeError, match=r"Node pair returned a result of type"):
        hamilton.execute(["pair"])
    assert hamilton.execute(["short_pair"]) == {"short_pair": (1,)}
    # Parameterized generics and NewTypes crash the check (#1738).
    for name in ("call", "frozen"):
        with pytest.raises(TypeError, match=r"parameterized generic"):
            hamilton.execute([name])
    with pytest.raises(TypeError, match=r"isinstance\(\) arg 2 must be a type"):
        hamilton.execute(["user"])
    with pytest.raises(TypeError, match=r"Node int_for_float returned a result"):
        hamilton.execute(["int_for_float"])
    # A failing node is reported as returning `None`; its error only in context.
    with pytest.raises(
        TypeError, match=r"Node fails returned a result of type"
    ) as info:
        hamilton.execute(["fails"])
    assert isinstance(info.value.__context__, ValueError)

    checked = fnloom.Builder().with_modules(module).with_output_type_checks()
    ours = checked.build()
    got = ours.execute(["pair", "call", "frozen", "user"])
    assert got["pair"] == (1, "a")
    assert got["user"] == 3  # noqa: PLR2004  # the NewType's value
    with pytest.raises(
        TypeError,
        match=r"^short_pair: returned tuple, expected tuple\[int, str\] "
        r"\(its length does not fit\)",
    ):
        ours.execute(["short_pair"])
    with pytest.raises(
        TypeError, match=r"^int_for_float: returned int, expected float"
    ):
        ours.execute(["int_for_float"])
    widened = checked.with_numeric_widening().build()
    assert widened.execute(["int_for_float"]) == {"int_for_float": 1}
    with pytest.raises(ValueError, match=r"^original failure") as error:
        ours.execute(["fails"])
    assert error.value.__notes__ == ["in node 'fails'"]


Q21_GRAPH = """
def a(x: int) -> int:
    return x + 1
"""


@pytest.mark.deliberate("Q21")
def test_q21_a_custom_value_check_adds_to_the_default() -> None:
    """Hamilton's `validate_input` replaces the default; fnloom's check only admits."""

    class AlwaysFalse(lifecycle.EdgeConnectionHook):
        @override
        def check_edge_types_match(
            self, type_from: type, type_to: type, **kwargs: Any
        ) -> bool:
            return False

        @override
        def validate_input(
            self, node_type: type, input_value: Any, **kwargs: Any
        ) -> bool:
            return False

    class AlwaysTrue(AlwaysFalse):
        @override
        def validate_input(
            self, node_type: type, input_value: Any, **kwargs: Any
        ) -> bool:
            return True

    module = make_module("differential_q21", Q21_GRAPH)

    def hamilton_with(adapter: lifecycle.EdgeConnectionHook) -> hamilton_driver.Driver:
        return (
            hamilton_driver.Builder()
            .with_modules(module)
            .with_adapters(adapter)
            .build()
        )

    # Refusing replaces the default: a correct value is refused.
    with pytest.raises(ValueError, match=r"Type requirement mismatch"):
        hamilton_with(AlwaysFalse()).execute(["a"], inputs={"x": 1})
    # Admitting replaces it too: a wrong value reaches the node.
    with pytest.raises(TypeError, match=r"can only concatenate str"):
        hamilton_with(AlwaysTrue()).execute(["a"], inputs={"x": "s"})

    def never(declared: object, value: object) -> bool:  # noqa: ARG001  # a check's signature
        return False

    def always(declared: object, value: object) -> bool:  # noqa: ARG001  # a check's signature
        return True

    refusing = fnloom.Builder().with_modules(module).with_value_check(never).build()
    assert refusing.execute(["a"], inputs={"x": 1}) == {"a": 2}
    with pytest.raises(ValueError, match=r"^x: expected int, got str"):
        refusing.execute(["a"], inputs={"x": "s"})
    admitting = fnloom.Builder().with_modules(module).with_value_check(always).build()
    assert admitting.validate(["a"], inputs={"x": "s"}) == []
    with pytest.raises(TypeError, match=r"can only concatenate str"):
        admitting.execute(["a"], inputs={"x": "s"})


_Q88_GRAPH = """
def a(x: int) -> int:
    return x + 1

def boom(a: int, big: list, s: str = "dflt") -> int:
    raise RuntimeError("bad")
"""


@pytest.mark.deliberate("Q88")
def test_q88_failures_are_logged_only_on_request(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Hamilton always logs a failure at ERROR; fnloom does when asked."""
    module = make_module("q88_logged", _Q88_GRAPH)
    big = list(range(100))
    cut = repr(big)[:50] + "..."
    inputs = {"x": 1, "big": big}

    hamilton = hamilton_driver.Builder().with_modules(module).build()
    with caplog.at_level(logging.DEBUG), pytest.raises(RuntimeError, match="bad"):
        hamilton.execute(["boom"], inputs=inputs)
    logged = [
        r for r in caplog.records if r.name == "hamilton.execution.graph_functions"
    ]
    errors = [r for r in logged if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert errors[0].exc_info is not None
    assert "boom" in errors[0].getMessage()
    assert cut in errors[0].getMessage()
    # The defaulted `s` is not among the inputs.
    assert "dflt" not in errors[0].getMessage()

    caplog.clear()
    quiet = fnloom.Builder().with_modules(module).build()
    with caplog.at_level(logging.DEBUG), pytest.raises(RuntimeError, match="bad"):
        quiet.execute(["boom"], inputs=inputs)
    assert not [r for r in caplog.records if r.name.startswith(("fnloom", "hamilton"))]

    caplog.clear()
    loud = (
        fnloom.Builder().with_modules(module).with_hooks(fnloom.LogFailures()).build()
    )
    with caplog.at_level(logging.DEBUG), pytest.raises(RuntimeError, match="bad"):
        loud.execute(["boom"], inputs=inputs)
    (record,) = [r for r in caplog.records if r.name == "fnloom"]
    assert record.levelno == logging.ERROR
    assert record.getMessage() == f"node 'boom' failed; inputs: a=2, big={cut}"
    assert record.exc_info is not None
    assert "dflt" not in record.getMessage()


_HAMILTON_DRAWN_ORDER = """
import json
import sys

from hamilton import driver

sys.path.insert(0, sys.argv[1])
sys.path.insert(0, sys.argv[2])
import diamond

built = driver.Builder().with_modules(diamond).build()
drawn = built.display_all_functions(show_legend=False)
print(json.dumps([name for name, _, _ in drawn.nodes]))
"""


@pytest.mark.deliberate("Q90")
@pytest.mark.usefixtures("fake_graphviz")
def test_q90_drawings(tmp_path: Path) -> None:
    """Hamilton's drawn order follows the hash seed; fnloom's never does."""
    (tmp_path / "graphviz.py").write_text(FAKE_GRAPHVIZ)
    orders = {
        seed: subprocess.run(  # noqa: S603  # our own interpreter and script
            [sys.executable, "-c", _HAMILTON_DRAWN_ORDER, str(FIXTURES), tmp_path],
            env={
                **os.environ,
                "PYTHONHASHSEED": seed,
                "HAMILTON_AUTOLOAD_EXTENSIONS": "0",
            },
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        for seed in ("0", "1", "2", "3", "4", "5")
    }
    assert len(set(orders.values())) > 1
    module = load_fixture("diamond")
    built = fnloom.build(module)
    assert [n.name for n in built.draw().nodes] == [
        "a",
        "root",
        "left",
        "right",
        "join",
    ]
    hamilton = hamilton_driver.Builder().with_modules(module).build()
    # Hamilton names the first unknown endpoint; fnloom names both.
    with pytest.raises(ValueError, match="Upstream node nope not found") as caught:
        hamilton.visualize_path_between("nope", "nada")
    assert "nada" not in str(caught.value)
    with pytest.raises(ValueError, match="'nada', 'nope'"):
        built.draw_path_between("nope", "nada")
    # Hamilton draws nothing for no names; fnloom refuses.
    assert hamilton.display_upstream_of().nodes == []
    with pytest.raises(TypeError, match="at least one node"):
        built.draw_upstream_of()
    # Hamilton draws a downstream node's other parents like any node; fnloom
    # marks them context.
    downstream = hamilton.display_downstream_of("left", show_legend=False)
    styles = {name: attrs for name, _, attrs in downstream.nodes}
    assert set(styles) == {"root", "left", "right", "join"}
    assert styles["root"] == styles["left"]
    marked = {n.name: n.marks for n in built.draw_downstream_of("left").nodes}
    assert marked == {
        "root": {"context"},
        "left": frozenset(),
        "right": {"context"},
        "join": frozenset(),
    }
    # Hamilton writes a bad orient as given; fnloom refuses it.
    attributes = hamilton.display_all_functions(orient="XX").kwargs["graph_attr"]
    assert attributes["rankdir"] == "XX"
    with pytest.raises(ValueError, match="orient must be one of"):
        built.draw().to_dot(orient="XX")  # ty: ignore[invalid-argument-type]


_Q91_GRAPH = """
from typing import Literal

from {package} import config

def nested(x: int) -> dict[str, list[int]]:
    return {{"k": [x]}}

def maybe(nested: dict[str, list[int]]) -> int | None:
    return None

def kind(x: int) -> Literal["a", "b"]:
    return "a"

@config.when(mode="fast")
def chosen__fast(x: int) -> int:
    return x
"""


@pytest.mark.deliberate("Q91")
@pytest.mark.usefixtures("fake_graphviz")
def test_q91_labels() -> None:
    """Hamilton labels a type by its origin's name; fnloom writes it whole."""
    hamilton_module = make_module(
        "q91_h", _Q91_GRAPH.format(package="hamilton.function_modifiers")
    )
    hamilton = (
        hamilton_driver.Builder()
        .with_modules(hamilton_module)
        .with_config({"mode": "fast"})
        .build()
    )
    labels = {
        name: label
        for name, label, _ in hamilton.display_all_functions(show_legend=False).nodes
    }
    assert "<i>dict</i>" in labels["nested"]
    assert "<i>Union</i>" in labels["maybe"]
    assert "<i>Literal</i>" in labels["kind"]
    # A variant shows its key, not the value it matched.
    assert labels["chosen"].startswith("<<b>chosen: mode</b>")
    module = make_module("q91_f", _Q91_GRAPH.format(package="fnloom"))
    built = fnloom.Builder().with_modules(module).with_config({"mode": "fast"}).build()
    drawn = {n.name: n for n in built.draw().nodes}
    assert drawn["nested"].type == "dict[str, list[int]]"
    assert drawn["maybe"].type == "int | None"
    assert drawn["kind"].type == "Literal['a', 'b']"
    assert drawn["chosen"].condition == "mode == 'fast'"


_Q93_HAMILTON = """
from hamilton.function_modifiers import config

def a(x: int) -> int:
    return x

def b(x: int) -> int:
    return x

@config.when(mode="fast")
def c__fast(a: int) -> int:
    return a
"""

_Q93_FNLOOM = _Q93_HAMILTON.replace(
    "from hamilton.function_modifiers import config", "from fnloom import config"
)


@pytest.mark.deliberate("Q93")
@pytest.mark.usefixtures("fake_graphviz")
def test_q93_inputs_and_config() -> None:
    """Hamilton boxes an input per consumer and draws config; fnloom doesn't."""
    hamilton = (
        hamilton_driver.Builder()
        .with_modules(make_module("q93_h", _Q93_HAMILTON))
        .with_config({"mode": "fast", "unused": 1})
        .build()
    )
    drawn = hamilton.display_upstream_of("b", show_legend=False)
    names = [name for name, _, _ in drawn.nodes]
    # Every config key, though `b` reads none; `x` once per consumer, as a box.
    assert {"mode", "unused"} <= set(names)
    every = hamilton.display_all_functions(show_legend=False)
    boxes = [name for name, _, _ in every.nodes if name.endswith("_inputs")]
    assert sorted(boxes) == ["_a_inputs", "_b_inputs"]
    assert "x" not in [name for name, _, _ in every.nodes]
    # `deduplicate_inputs` shares one box between consumers of the same inputs.
    shared = hamilton.display_all_functions(show_legend=False, deduplicate_inputs=True)
    assert len([n for n, _, _ in shared.nodes if n.endswith("_inputs")]) == 1

    built = (
        fnloom.Builder()
        .with_modules(make_module("q93_f", _Q93_FNLOOM))
        .with_config({"mode": "fast", "unused": 1})
        .build()
    )
    assert [n.name for n in built.draw_upstream_of("b").nodes] == ["x", "b"]
    nodes = built.draw().nodes
    assert [n.name for n in nodes if n.kind == "input"] == ["x"]
    assert ("x", "a") in built.draw().edges
    assert ("x", "b") in built.draw().edges
    assert next(n for n in nodes if n.name == "c").condition == "mode == 'fast'"


@pytest.mark.deliberate("Q92")
@pytest.mark.usefixtures("fake_graphviz")
def test_q92_execution_drawings() -> None:
    """Hamilton boxes an input requested as an output, unmarked; fnloom marks it."""
    module = load_fixture("chain")
    hamilton = hamilton_driver.Builder().with_modules(module).build()
    drawn = hamilton.visualize_execution(["a", "b"], inputs={"a": 1}, show_legend=False)
    names = {name: attrs for name, _, attrs in drawn.nodes}
    assert "a" not in names
    assert names["b"]["fillcolor"] == "#FFC857"
    assert "_b_inputs" in names
    built = fnloom.build(module)
    marked = {
        n.name: n.marks for n in built.draw_execution(["a", "b"], inputs={"a": 1}).nodes
    }
    assert marked == {"a": {"output"}, "b": {"output"}}
    # Both validate; Hamilton's error names its bypass, fnloom's is `validate`'s.
    with pytest.raises(ValueError, match="bypass_validation=True"):
        hamilton.visualize_execution(["c"])
    with pytest.raises(ValueError, match="missing inputs") as caught:
        built.draw_execution(["c"])
    assert str(caught.value) == "\n".join(built.validate(["c"]))


@pytest.mark.deliberate("Q94")
def test_q94_drawing_needs_nothing(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """Hamilton without graphviz returns `None` and logs; fnloom's text needs none."""
    if importlib.util.find_spec("graphviz") is not None:
        pytest.skip("the graphviz package is installed")
    module = load_fixture("diamond")
    hamilton = hamilton_driver.Builder().with_modules(module).build()
    target = tmp_path / "graph.svg"
    with caplog.at_level(logging.DEBUG):
        assert hamilton.display_all_functions(str(target)) is None
    assert not target.exists()
    (error,) = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert "sf-hamilton[visualization]" in error.getMessage()
    assert error.exc_info is not None
    dot = fnloom.build(module).draw().to_dot()
    assert dot.startswith("digraph {")


_Q95_HAMILTON = """
from hamilton.function_modifiers import config

def a(x: int) -> int:
    return x + 1

def b(a: int) -> int:
    return a

@config.when(mode="fast")
def c__fast(b: int) -> int:
    return b
"""


def _module_from_file(folder: Path, name: str, source: str) -> types.ModuleType:
    """`source` written to `folder` and imported as `name`, so it has source."""
    path = folder / f"{name}.py"
    path.write_text(source)
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.deliberate("Q95")
def test_q95_exports(tmp_path: Path) -> None:
    """Hamilton gives an input a consumer's source; fnloom exports it as an input."""
    # A function Python has no source for: Hamilton's export raises.
    no_source = make_module("q95_exec", _Q95_HAMILTON)
    with pytest.raises(OSError, match="source code not available"):
        hamilton_driver.Builder().with_modules(no_source).build().export_execution(
            ["a"], {"x": 1}
        )
    assert (
        fnloom.build(no_source).export(["a"], inputs={"x": 1})["nodes"][1]["source"]
        is None
    )
    module = _module_from_file(tmp_path, "q95_h", _Q95_HAMILTON)
    hamilton = (
        hamilton_driver.Builder()
        .with_modules(module)
        .with_config({"mode": "fast"})
        .build()
    )
    exported = json.loads(hamilton.export_execution(["c"], {"x": 1}, {"b": 2}))
    assert set(exported) == {"nodes"}
    by_name = {n["name"]: n for n in exported["nodes"]}
    # The override `b` is unmarked, and no node says which variant `c` is.
    assert set(by_name) == {"b", "c"}
    assert "override" not in json.dumps(by_name["b"])
    plain = json.loads(hamilton.export_execution(["a"], {"x": 1}))
    inputs = {n["name"]: n for n in plain["nodes"]}
    assert inputs["x"]["version"] == inputs["a"]["version"]
    with pytest.raises(TypeError, match="expected str instance, function found"):
        hamilton.export_execution([module.a], {"x": 1})

    fn_module = make_module(
        "q95_f",
        _Q95_HAMILTON.replace(
            "from hamilton.function_modifiers import config",
            "from fnloom import config",
        ),
    )
    built = (
        fnloom.Builder().with_modules(fn_module).with_config({"mode": "fast"}).build()
    )
    document = built.export(["c"], inputs={"x": 1}, overrides={"b": 2})
    assert document["format"] == "fnloom.export/1"
    assert document["config"] == {"mode": "'fast'"}
    nodes = {n["name"]: n for n in document["nodes"]}
    assert nodes["b"]["marks"] == ["override"]
    assert nodes["c"]["config"] == ["mode == 'fast'"]
    x = {n["name"]: n for n in built.export(["a"], inputs={"x": 1})["nodes"]}["x"]
    assert x["version"] is None
    assert x["source"] is None
    assert built.export([fn_module.a], inputs={"x": 1}) == built.export(
        ["a"], inputs={"x": 1}
    )


_Q96_GRAPH = """
K = {k}

def a(x: int) -> int:
    return x + K
"""


@pytest.mark.deliberate("Q96")
def test_q96_versions_see_the_code_a_function_reads() -> None:
    """Hamilton's version is the source text; a changed constant keeps it."""
    hamilton_versions = []
    fnloom_versions = []
    for k in (1, 2):
        module = make_module(f"q96_{k}", _Q96_GRAPH.format(k=k))
        built = hamilton_driver.Builder().with_modules(module).build()
        graph = graph_types.HamiltonGraph.from_graph(built.graph)
        hamilton_versions.append(
            (graph.version, {n.name: n.version for n in graph.nodes})
        )
        document = fnloom.build(module).export()
        fnloom_versions.append(
            (
                document["graph_version"],
                {n["name"]: n["version"] for n in document["nodes"]},
            )
        )
    assert hamilton_versions[0] == hamilton_versions[1]
    # Hamilton gives the input its consumer's version; fnloom gives it none.
    assert hamilton_versions[0][1]["x"] == hamilton_versions[0][1]["a"]
    assert fnloom_versions[0][1]["x"] is None
    assert fnloom_versions[0][0] != fnloom_versions[1][0]
    assert fnloom_versions[0][1]["a"] != fnloom_versions[1][1]["a"]


_Q97_FLOW = """
from {package} import config
from q97_helper import double

def a(x: int) -> int:
    return double(x)

@config.when(mode="fast")
def b__fast(a: int) -> int:
    return a
"""


@pytest.mark.deliberate("Q97")
def test_q97_cli_loading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Hamilton's CLI compiles a file alone and drops `@config.when` nodes."""
    from hamilton.cli import commands as hamilton_commands  # noqa: PLC0415  # its CLI only

    from fnloom._cli import main  # noqa: PLC0415  # the command under test

    monkeypatch.setattr(sys, "path", list(sys.path))
    (tmp_path / "q97_helper.py").write_text(
        "def double(v: int) -> int:\n    return v * 2\n"
    )
    hamilton_file = tmp_path / "q97_hamilton.py"
    hamilton_file.write_text(_Q97_FLOW.format(package="hamilton.function_modifiers"))
    with pytest.raises(ModuleNotFoundError, match="q97_helper"):
        hamilton_commands.build([hamilton_file])
    # Without its sibling import, a context-free build has no `b`.
    alone = tmp_path / "q97_alone.py"
    alone.write_text(
        _Q97_FLOW.format(package="hamilton.function_modifiers")
        .replace("from q97_helper import double\n", "")
        .replace("double(x)", "x")
    )
    built = hamilton_commands.build([alone])
    assert "b" not in {n.name for n in built.list_available_variables()}

    fnloom_file = tmp_path / "q97_fnloom.py"
    fnloom_file.write_text(_Q97_FLOW.format(package="fnloom"))
    assert main(["export", str(fnloom_file), "--config", "mode=fast"]) == 0
    names = [n["name"] for n in json.loads(capsys.readouterr().out)["nodes"]]
    assert names == ["x", "a", "b"]


_Q98_OLD = """
def a(x: int) -> int:
    return x + 1
"""


@pytest.mark.deliberate("Q98")
def test_q98_diff_compares_inputs_as_inputs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Hamilton reports an input as edited with its consumer; fnloom doesn't."""
    from hamilton.cli import logic as hamilton_logic  # noqa: PLC0415  # its CLI only

    from fnloom._cli import main  # noqa: PLC0415  # the command under test

    new_source = _Q98_OLD.replace("x + 1", "x + 2")
    old_module = _module_from_file(tmp_path, "q98_old", _Q98_OLD)
    new_module = _module_from_file(tmp_path, "q98_new", new_source)
    versions = [
        hamilton_logic.hash_hamilton_nodes(
            hamilton_driver.Builder().with_modules(module).build()
        )
        for module in (old_module, new_module)
    ]
    hamilton_diff = hamilton_logic.diff_versions(
        current_map=versions[1], reference_map=versions[0]
    )
    assert sorted(hamilton_diff["edit"]) == ["a", "x"]

    paths = []
    for name, module in (("old", old_module), ("new", new_module)):
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(fnloom.build(module).export()))
        paths.append(str(path))
    assert main(["diff", *paths]) == 1
    found = json.loads(capsys.readouterr().out)
    assert found["changed"] == {"a": ["version"]}


_Q99_CHECKED = """
from {package} import check_output

@check_output(range=(0, 10){importance})
def a(x: int) -> int:
    return x
"""


def _q99(package: str, name: str, importance: str = "") -> types.ModuleType:
    return make_module(
        name, _Q99_CHECKED.format(package=package, importance=importance)
    )


@pytest.mark.deliberate("Q99")
def test_q99_checks_add_no_node() -> None:
    """Hamilton makes `a_raw` and a validator node; fnloom's node keeps its place."""
    hamilton = (
        hamilton_driver.Builder()
        .with_modules(_q99("hamilton.function_modifiers", "q99_h"))
        .build()
    )
    names = {v.name for v in hamilton.list_available_variables()}
    assert {"a", "a_raw", "a_range_validator"} <= names
    result = hamilton.execute(["a_range_validator"], inputs={"x": 1})
    assert result["a_range_validator"].passes
    built = fnloom.build(_q99("fnloom", "q99_f"))
    assert built.node_names() == ["a", "x"]


@pytest.mark.deliberate("Q100")
def test_q100_fail_is_the_default_and_a_value_error() -> None:
    """Hamilton warns by default; its failure is no ValueError, and prints a list."""
    hamilton = (
        hamilton_driver.Builder()
        .with_modules(_q99("hamilton.function_modifiers", "q100_h"))
        .build()
    )
    assert hamilton.execute(["a"], inputs={"x": 12}) == {"a": 12}
    failing = (
        hamilton_driver.Builder()
        .with_modules(
            _q99("hamilton.function_modifiers", "q100_hf", ', importance="fail"')
        )
        .build()
    )
    with pytest.raises(Exception) as caught:  # noqa: PT011  # Hamilton's own class
        failing.execute(["a"], inputs={"x": 12})
    assert type(caught.value).__name__ == "DataValidationError"
    assert not isinstance(caught.value, ValueError)
    assert str(caught.value).startswith("[")
    built = fnloom.build(_q99("fnloom", "q100_f"))
    with pytest.raises(fnloom.ValidationError) as error:
        built.execute(["a"], inputs={"x": 12})
    assert isinstance(error.value, ValueError)
    assert str(error.value) == "a: check range failed: 12 is not in [0, 10]"


_Q101_GENERIC = """
from {package} import check_output

@check_output(range=(0, 10))
def a(x: list[int]) -> list[int]:
    return x
"""


@pytest.mark.deliberate("Q101")
def test_q101_generic_annotations_are_checked() -> None:
    """Hamilton's build crashes on `list[int]`; fnloom checks every item."""
    module = make_module(
        "q101_h", _Q101_GENERIC.format(package="hamilton.function_modifiers")
    )
    with pytest.raises(TypeError, match="issubclass"):
        hamilton_driver.Builder().with_modules(module).build()
    module_f = make_module("q101_f", _Q101_GENERIC.format(package="fnloom"))
    built = fnloom.build(module_f)
    assert built.execute(["a"], inputs={"x": [1, 2]}) == {"a": [1, 2]}
    with pytest.raises(fnloom.ValidationError, match="1 items are not in"):
        built.execute(["a"], inputs={"x": [1, 20]})


@pytest.mark.deliberate("Q102")
def test_q102_custom_checks() -> None:
    """Hamilton never calls `applies_to`, and a `bool` result breaks it."""
    from hamilton.data_quality.base import (  # noqa: PLC0415  # Hamilton's API only here
        DataValidator,
        ValidationResult,
    )
    from hamilton.function_modifiers import (  # noqa: PLC0415  # Hamilton's API only here
        check_output_custom as hamilton_custom,
    )

    asked: list[object] = []

    class Never(DataValidator):
        @override
        def applies_to(self, datatype: type) -> bool:
            asked.append(datatype)
            return False

        @override
        def description(self) -> str:
            return "never applies"

        @classmethod
        @override
        def name(cls) -> str:
            return "never"

        @override
        def validate(self, dataset: object) -> ValidationResult:
            del dataset
            return ValidationResult(passes=True, message="")

    class Boolean(Never):
        @override
        def validate(self, dataset: object) -> bool:  # ty: ignore[invalid-method-override]
            del dataset
            return True

    module = make_module("q102_h", "def a(x: int) -> int:\n    return x\n")
    hamilton_custom(Never("fail"))(module.a)
    hamilton = hamilton_driver.Builder().with_modules(module).build()
    assert hamilton.execute(["a"], inputs={"x": 1}) == {"a": 1}
    assert asked == []
    bool_module = make_module("q102_hb", "def a(x: int) -> int:\n    return x\n")
    hamilton_custom(Boolean("fail"))(bool_module.a)
    with pytest.raises(AttributeError, match="passes"):
        hamilton_driver.Builder().with_modules(bool_module).build().execute(
            ["a"], inputs={"x": 1}
        )

    class OnlyStr(fnloom.Validator):
        @override
        def applies_to(self, declared: object) -> bool:
            return declared is str

        @override
        def validate(self, value: object, inputs: Mapping[str, object]) -> bool:
            del value, inputs
            return True

    refused = make_module("q102_f", "def a(x: int) -> int:\n    return x\n")
    fnloom.check_output_custom(OnlyStr())(refused.a)
    with pytest.raises(TypeError, match="does not apply to int"):
        fnloom.build(refused)
    plain = make_module("q102_fb", "def a(x: int) -> int:\n    return x\n")
    fnloom.check_output_custom(lambda _: True)(plain.a)
    assert fnloom.build(plain).execute(["a"], inputs={"x": 1}) == {"a": 1}


_Q103_STACKED = """
from {package} import check_output

@check_output(range=(0, 10))
@check_output(values_in=[1, 2, 3])
def a(x: int) -> int:
    return x
"""


@pytest.mark.deliberate("Q103")
def test_q103_checks_stack() -> None:
    """Hamilton refuses a second `@check_output`; fnloom runs both, top first."""
    module = make_module(
        "q103_h", _Q103_STACKED.format(package="hamilton.function_modifiers")
    )
    with pytest.raises(ValueError, match="a_raw more than once"):
        hamilton_driver.Builder().with_modules(module).build()
    module_f = make_module("q103_f", _Q103_STACKED.format(package="fnloom"))
    built = fnloom.build(module_f)
    assert built.execute(["a"], inputs={"x": 2}) == {"a": 2}
    with pytest.raises(fnloom.ValidationError, match="check values_in failed"):
        built.execute(["a"], inputs={"x": 5})


_Q104_STEPS = """
from {package} import pipe_output, step, value

def _add(v: int, n: int) -> int:
    return v + n

@pipe_output(step(_add, n=value(1)).when(mode="a"), step(_add, n=value(2)))
def a(x: int) -> int:
    return x
"""


@pytest.mark.deliberate("Q104")
def test_q104_step_names_hold_across_config(tmp_path: Path) -> None:
    """Hamilton renumbers repeats by the steps the config kept; fnloom never does."""
    module = _module_from_file(
        tmp_path, "q104_h", _Q104_STEPS.format(package="hamilton.function_modifiers")
    )
    names = {}
    for mode in ("a", "b"):
        built = (
            hamilton_driver.Builder()
            .with_modules(module)
            .with_config({"mode": mode})
            .build()
        )
        names[mode] = {v.name for v in built.list_available_variables()}
        tags = {v.name: v.tags for v in built.list_available_variables()}
    assert "a.with_add_1" in names["a"]
    assert "a.with_add" in names["b"]
    assert "a.with_add_1" not in names["b"]
    assert tags["a"]["module"] == "hamilton.function_modifiers.macros"
    fn_module = make_module("q104_f", _Q104_STEPS.format(package="fnloom"))
    for mode in ("a", "b"):
        built_f = (
            fnloom.Builder().with_modules(fn_module).with_config({"mode": mode}).build()
        )
        assert "a.with_add_1" in built_f.node_names()
        assert built_f.nodes["a"].module == "q104_f"


_Q105_STEPS = """
from {package} import pipe_output, step

def _mul(v: int, k: int = 10) -> int:
    return v * k

@pipe_output(step(_mul))
def a(x: int) -> int:
    return x
"""


@pytest.mark.deliberate("Q105")
def test_q105_defaulted_step_parameters_stay_their_own(tmp_path: Path) -> None:
    """Hamilton makes `k` a graph input; fnloom keeps the default."""
    module = _module_from_file(
        tmp_path, "q105_h", _Q105_STEPS.format(package="hamilton.function_modifiers")
    )
    hamilton = hamilton_driver.Builder().with_modules(module).build()
    assert "k" in {v.name for v in hamilton.list_available_variables()}
    assert hamilton.execute(["a"], inputs={"x": 1, "k": 3}) == {"a": 3}
    built = fnloom.build(make_module("q105_f", _Q105_STEPS.format(package="fnloom")))
    assert "k" not in built.node_names()
    assert built.execute(["a"], inputs={"x": 1}) == {"a": 10}


_Q106_ON_INPUT = """
from {package} import pipe_input, step

def _add(v: int, n: int) -> int:
    return v + n

@pipe_input(step(_add, n=1), on_input="zz")
def f(x: int) -> int:
    return x
"""


@pytest.mark.deliberate("Q106")
def test_q106_misuse_is_refused(tmp_path: Path) -> None:
    """Hamilton feeds the first parameter for an unknown `on_input`."""
    module = _module_from_file(
        tmp_path, "q106_h", _Q106_ON_INPUT.format(package="hamilton.function_modifiers")
    )
    hamilton = hamilton_driver.Builder().with_modules(module).build()
    assert hamilton.execute(["f"], inputs={"x": 1}) == {"f": 2}
    with pytest.raises(TypeError, match="no parameter named 'zz'"):
        make_module("q106_f", _Q106_ON_INPUT.format(package="fnloom"))


_Q108_ROWS = """
from {package} import parameterize, pipe_input, step, value

def _add(v: int, n: int) -> int:
    return v + n

@pipe_input(step(_add, n=1))
@parameterize(low={{"k": value(1)}}, high={{"k": value(10)}})
def scaled(x: int, k: int) -> int:
    return x * k
"""


@pytest.mark.deliberate("Q108")
def test_q108_rows_get_their_own_chain(tmp_path: Path) -> None:
    """Hamilton refuses `@pipe_input` with `@parameterize`; fnloom chains each row."""
    module = _module_from_file(
        tmp_path, "q108_h", _Q108_ROWS.format(package="hamilton.function_modifiers")
    )
    with pytest.raises(ValueError, match="Cannot call NodeExpander"):
        hamilton_driver.Builder().with_modules(module).build()
    built = fnloom.build(make_module("q108_f", _Q108_ROWS.format(package="fnloom")))
    assert built.execute(["low", "high"], inputs={"x": 1}) == {"low": 2, "high": 20}


_Q107_TARGET = "def a(x: int) -> int:\n    return x\n"
_Q107_MUTATOR = """
from {package} import mutate
import {target}

@mutate({target}.a)
def _plus(v: int) -> int:
    return v + 1
"""


@pytest.mark.deliberate("Q107")
def test_q107_cross_module_mutate(tmp_path: Path) -> None:
    """Hamilton refuses a target in another module; fnloom mutates it."""
    _module_from_file(tmp_path, "q107_target_h", _Q107_TARGET)
    # Hamilton's own `NotSameModuleError`.
    with pytest.raises(Exception, match="have to be in the same module"):
        _module_from_file(
            tmp_path,
            "q107_mutator_h",
            _Q107_MUTATOR.format(
                package="hamilton.function_modifiers", target="q107_target_h"
            ),
        )
    target = make_module("q107_target_f", _Q107_TARGET)
    mutator = make_module(
        "q107_mutator_f",
        _Q107_MUTATOR.format(package="fnloom", target="q107_target_f"),
    )
    assert fnloom.build(target, mutator).execute(["a"], inputs={"x": 1}) == {"a": 2}


_Q109_INJECTED = """
from {package} import inject, pipe_input, source, step

def _inc(v: int) -> int:
    return v + 1

@pipe_input(step(_inc))
@inject(v=source("a"))
def piped(v: int) -> int:
    return v
"""


@pytest.mark.deliberate("Q109")
def test_q109_inject_with_pipe_input(tmp_path: Path) -> None:
    """Hamilton refuses `@inject` with `@pipe_input`; fnloom chains the row."""
    module = _module_from_file(
        tmp_path, "q109_h", _Q109_INJECTED.format(package="hamilton.function_modifiers")
    )
    with pytest.raises(ValueError, match="Cannot call NodeExpander"):
        hamilton_driver.Builder().with_modules(module).build()
    built = fnloom.build(make_module("q109_f", _Q109_INJECTED.format(package="fnloom")))
    assert built.execute(["piped"], inputs={"a": 1}) == {"piped": 2}


_Q110_RESOLVED = """
from {package} import parameterize, source, value{extra}

@resolve({when}decorate_with=lambda pairs: parameterize(
    **{{f"sum_{{a}}_{{b}}": {{"p": source(a), "q": source(b)}} for a, b in pairs}}
))
def summation(p: int, q: int) -> int:
    return p + q
"""


@pytest.mark.deliberate("Q110")
def test_q110_resolve_needs_no_power_user_mode(tmp_path: Path) -> None:
    """Hamilton raises a raw KeyError without its power-user key; fnloom builds."""
    module = _module_from_file(
        tmp_path,
        "q110_h",
        _Q110_RESOLVED.format(
            package="hamilton.function_modifiers",
            extra=", resolve, ResolveAt",
            when="when=ResolveAt.CONFIG_AVAILABLE, ",
        ),
    )
    config = {"pairs": [("u", "v")]}
    with pytest.raises(KeyError, match="enable_power_user_mode"):
        hamilton_driver.Builder().with_modules(module).with_config(config).build()
    powered = (
        hamilton_driver.Builder()
        .with_modules(module)
        .with_config({**config, "hamilton.enable_power_user_mode": True})
        .build()
    )
    assert powered.execute(["sum_u_v"], inputs={"u": 1, "v": 2}) == {"sum_u_v": 3}
    built = (
        fnloom.Builder()
        .with_modules(
            make_module(
                "q110_f",
                _Q110_RESOLVED.format(package="fnloom", extra=", resolve", when=""),
            )
        )
        .with_config(config)
        .build()
    )
    assert built.execute(["sum_u_v"], inputs={"u": 1, "v": 2}) == {"sum_u_v": 3}
    with pytest.raises(ValueError, match="the config has no 'pairs'"):
        fnloom.Builder().with_modules(
            make_module(
                "q110_g",
                _Q110_RESOLVED.format(package="fnloom", extra=", resolve", when=""),
            )
        ).build()


@pytest.mark.deliberate("Q111")
def test_q111_wrappers_chain() -> None:
    """Hamilton refuses a second `NodeExecutionMethod`; fnloom chains any number."""

    class Twice(lifecycle.NodeExecutionMethod):
        @override
        def run_to_execute_node(
            self, *, node_callable: Any, node_kwargs: dict[str, Any], **kwargs: Any
        ) -> Any:
            del kwargs
            return node_callable(**node_kwargs)

    module = load_fixture("chain")
    with pytest.raises(ValueError, match="do_node_execute"):
        hamilton_driver.Builder().with_modules(module).with_adapters(
            Twice(), Twice()
        ).build()
    order: list[str] = []

    def first(node: object, call: Callable[[], object], inputs: object) -> object:
        del node, inputs
        order.append("first")
        return call()

    def second(node: object, call: Callable[[], object], inputs: object) -> object:
        del node, inputs
        order.append("second")
        return call()

    built = fnloom.Builder().with_modules(module).with_node_wrappers(first, second)
    assert built.build().execute(["b"], inputs={"a": 1}) == {"b": 2}
    assert order == ["first", "second"]


_Q113_DOES = """
from hamilton.function_modifiers import does

def _sum_all(a: int, b: int) -> int:
    return a + b

def _as_text(**kwargs: int) -> str:
    return "s"

@does(_sum_all, a="x", b="y")
def total(x: int, y: int) -> int:
    pass

@does(_as_text)
def typed_int(a: int) -> int:
    pass
"""

_Q113_CALLED = """
def _sum_all(a: int, b: int) -> int:
    return a + b

def total(x: int, y: int) -> int:
    return _sum_all(a=x, b=y)
"""


@pytest.mark.deliberate("Q113")
def test_q113_does_is_a_call(tmp_path: Path) -> None:
    """Hamilton's `@does` returns a `str` from an `int` node; a call types it."""
    module = _module_from_file(tmp_path, "q113_h", _Q113_DOES)
    driver = hamilton_driver.Builder().with_modules(module).build()
    assert driver.execute(["total", "typed_int"], inputs={"x": 1, "y": 2, "a": 0}) == {
        "total": 3,
        "typed_int": "s",
    }
    types_ = {v.name: v.type for v in driver.list_available_variables()}
    assert types_["typed_int"] is int
    called = fnloom.build(make_module("q113_f", _Q113_CALLED))
    assert called.execute(["total"], inputs={"x": 1, "y": 2}) == {"total": 3}


_Q114_SCHEMA = """
from hamilton.function_modifiers import schema

@schema.output(("a", "int"), ("b", "float"))
def frame(rows: int) -> dict:
    return {"a": list(range(rows))}
"""

_Q114_CHECKED = """
import fnloom

def _has_columns(value: dict) -> str | None:
    missing = sorted({"a", "b"} - set(value))
    return f"missing columns {missing}" if missing else None

@fnloom.check_output_custom(_has_columns)
@fnloom.tag(schema="a:int,b:float")
def frame(rows: int) -> dict:
    return {"a": list(range(rows))}
"""


@pytest.mark.deliberate("Q114")
def test_q114_schema_is_a_check(tmp_path: Path) -> None:
    """Hamilton refuses the type at build; fnloom checks the columns at run."""
    module = _module_from_file(tmp_path, "q114_h", _Q114_SCHEMA)
    with pytest.raises(InvalidDecoratorException, match="not a registered type"):
        hamilton_driver.Builder().with_modules(module).build()
    driver = fnloom.build(make_module("q114_f", _Q114_CHECKED))
    assert driver.nodes["frame"].tags["schema"] == "a:int,b:float"
    with pytest.raises(fnloom.ValidationError, match=r"missing columns \['b'\]"):
        driver.execute(["frame"], inputs={"rows": 2})


_Q115_LOADER = """
from hamilton.function_modifiers import dataloader

@dataloader()
def raw(path: str = "p.json") -> tuple[list, dict]:
    return [1, 2], {"source": path, "rows": 2}

def total(raw: list) -> int:
    return sum(raw)
"""

_Q115_FIELDS = """
import fnloom

@fnloom.extract_fields({"data": list, "metadata": dict})
@fnloom.tag(kind="loader")
def raw(path: str = "p.json") -> dict:
    return {"data": [1, 2], "metadata": {"source": path, "rows": 2}}

def total(data: list) -> int:
    return sum(data)
"""


@pytest.mark.deliberate("Q115")
def test_q115_a_loader_is_a_function(tmp_path: Path) -> None:
    """The same data and metadata, from `@extract_fields` rather than a tuple split."""
    module = _module_from_file(tmp_path, "q115_h", _Q115_LOADER)
    driver = hamilton_driver.Builder().with_modules(module).build()
    data, metadata = driver.execute(["total", "raw.loader"])["raw.loader"]
    assert driver.execute(["total"]) == {"total": 3}
    fields = fnloom.build(make_module("q115_f", _Q115_FIELDS))
    assert fields.execute(["total", "metadata"]) == {"total": 3, "metadata": metadata}
    assert fields.execute(["data"]) == {"data": data}


_Q116_IO = """
from hamilton.function_modifiers import load_from, save_to, source

@save_to.json(path=source("out_path"), output_name_="saved")
def doubled(total: int) -> dict:
    return {"total": total * 2}

@load_from.json(path=source("in_path"))
def total(data: dict) -> int:
    return sum(data.values())
"""

_Q116_NODES = """
import json
from pathlib import Path

def numbers(in_path: str) -> dict:
    return json.loads(Path(in_path).read_text(encoding="utf-8"))

def total(numbers: dict) -> int:
    return sum(numbers.values())

def doubled(total: int) -> dict:
    return {"total": total * 2}
"""


@pytest.mark.deliberate("Q116")
def test_q116_io_is_a_node_and_a_hook(tmp_path: Path) -> None:
    """A loader node and an `after_run` hook write what `@load_from`/`@save_to` do."""
    given = tmp_path / "in.json"
    given.write_text('{"a": 1, "b": 2}', encoding="utf-8")
    module = _module_from_file(tmp_path, "q116_h", _Q116_IO)
    driver = hamilton_driver.Builder().with_modules(module).build()
    inputs = {"in_path": str(given), "out_path": str(tmp_path / "h.json")}
    driver.execute(["doubled"], inputs=inputs)
    assert not (tmp_path / "h.json").exists()  # only a requested saver writes
    driver.execute(["saved"], inputs=inputs)
    written = json.loads((tmp_path / "h.json").read_text(encoding="utf-8"))

    class SaveJson(fnloom.Hook):
        @override
        def after_run(self, event: fnloom.RunEnd) -> None:
            if event.ok and event.results is not None:
                (tmp_path / "f.json").write_text(
                    json.dumps(event.results["doubled"]), encoding="utf-8"
                )

    built = (
        fnloom.Builder()
        .with_modules(make_module("q116_f", _Q116_NODES))
        .with_hooks(SaveJson())
        .build()
    )
    assert built.execute(["doubled"], inputs={"in_path": str(given)}) == {
        "doubled": {"total": 6}
    }
    assert json.loads((tmp_path / "f.json").read_text(encoding="utf-8")) == written


_Q117_OWNED = """
from hamilton.function_modifiers import tag

@tag(owner="a")
def kept(x: int) -> int:
    return x

def orphan_b(x: int) -> int:
    return x

def orphan_a(x: int) -> int:
    return x
"""


def _owner_problems(driver: fnloom.Driver) -> list[str]:
    return sorted(
        f"{name}: no owner tag"
        for name, node in driver.nodes.items()
        if not node.is_input and "owner" not in node.tags
    )


@pytest.mark.deliberate("Q117")
def test_q117_static_checks_are_a_function(tmp_path: Path) -> None:
    """Hamilton raises `ValidationException`; a function over `nodes` finds the same."""

    class Owned(lifecycle.StaticValidator):
        @override
        def run_to_validate_node(
            self, *, node: graph_types.HamiltonNode, **future_kwargs: Any
        ) -> tuple[bool, str | None]:
            del future_kwargs
            if node.is_external_input or "owner" in node.tags:
                return True, None
            return False, "no owner tag"

    module = _module_from_file(tmp_path, "q117_h", _Q117_OWNED)
    with pytest.raises(Exception, match="2 errors encountered") as caught:
        hamilton_driver.Builder().with_modules(module).with_adapters(Owned()).build()
    assert not isinstance(caught.value, ValueError)
    assert "orphan_a: no owner tag" in str(caught.value)
    # A class rather than an instance is accepted, and never runs.
    hamilton_driver.Builder().with_modules(module).with_adapters(
        Owned  # ty: ignore[invalid-argument-type]  # the class, as Hamilton accepts
    ).build()
    fnloom_module = make_module(
        "q117_f", _Q117_OWNED.replace("hamilton.function_modifiers", "fnloom")
    )
    assert _owner_problems(fnloom.build(fnloom_module)) == [
        "orphan_a: no owner tag",
        "orphan_b: no owner tag",
    ]


_Q110_IMPORTING = """
import importlib
import fnloom

def _late(flag=False):
    if flag:
        importlib.import_module("{mutating}")
    return fnloom.tag(t="1")

@fnloom.resolve(_late)
def host(x: int) -> int:
    return x
"""

_Q110_MUTATING = """
import fnloom

def target(x: int) -> int:
    return x

@fnloom.mutate(target)
def _plus(v: int) -> int:
    return v + 1
"""


@pytest.mark.deliberate("Q110")
def test_q110_a_resolver_importing_a_mutate_is_refused(tmp_path: Path) -> None:
    """The known limit: `@mutate` while a resolver runs is refused, even by import."""
    (tmp_path / "q110_mutating.py").write_text(_Q110_MUTATING, encoding="utf-8")
    sys.path.insert(0, str(tmp_path))
    try:
        module = make_module(
            "q110_importing", _Q110_IMPORTING.format(mutating="q110_mutating")
        )
        with pytest.raises(TypeError, match="can't apply @mutate"):
            fnloom.Builder().with_modules(module).with_config({"flag": True}).build()
    finally:
        sys.path.remove(str(tmp_path))
        sys.modules.pop("q110_mutating", None)


_Q112_TAXES = """
def net(gross: float) -> float:
    return gross * 0.8

def doubled(net: float) -> float:
    return net * 2
"""

_Q112_SUBDAGS = """
from hamilton.function_modifiers import source, subdag, value
import q112_taxes

@subdag(q112_taxes, inputs={"gross": source("eu_gross")})
def eu(doubled: float) -> float:
    return doubled

@subdag(q112_taxes, inputs={"gross": value(5.0)})
def us(doubled: float) -> float:
    return doubled

@subdag(q112_taxes, inputs={"gross": source("eu_gross")})
def typo(doubld: float) -> float:
    return doubld
"""

_Q112_FINALS = """
import fnloom

@fnloom.inject(doubled=fnloom.source("eu.doubled"))
def eu(doubled: float) -> float:
    return doubled

@fnloom.inject(doubled=fnloom.source("us.doubled"))
def us(doubled: float) -> float:
    return doubled

@fnloom.inject(doubld=fnloom.source("eu.doubld"))
def typo(doubld: float) -> float:
    return doubld
"""


@pytest.mark.deliberate("Q112")
def test_q112_bind_feeds_each_namespace(tmp_path: Path) -> None:
    """Two `@subdag`s with `inputs=` and two bound namespaces give the same values.

    Hamilton makes a misspelled final-function parameter a namespaced input;
    fnloom's final node reads the namespace's node by name, and a misspelled
    one is a missing input that `validate` names.
    """
    _module_from_file(tmp_path, "q112_taxes", _Q112_TAXES)
    hamilton_module = _module_from_file(tmp_path, "q112_h", _Q112_SUBDAGS)
    hamilton = hamilton_driver.Builder().with_modules(hamilton_module).build()
    inputs = {"eu_gross": 10.0}
    expected = hamilton.execute(["eu", "us"], inputs=inputs)
    assert expected == {"eu": 16.0, "us": 8.0}
    assert sorted(
        v.name for v in hamilton.list_available_variables() if v.is_external_input
    ) == ["eu_gross", "typo.doubld"]
    taxes = make_module("q112_taxes_f", _Q112_TAXES)
    finals = make_module("q112_f", _Q112_FINALS)
    driver = (
        fnloom.Builder()
        .with_modules(finals)
        .with_modules(taxes, namespace="eu", bind={"gross": fnloom.source("eu_gross")})
        .with_modules(taxes, namespace="us", bind={"gross": fnloom.value(5.0)})
        .build()
    )
    assert driver.execute(["eu", "us"], inputs=inputs) == expected
    assert driver.validate(["typo"], inputs=inputs) == [
        "missing inputs: 'eu.doubld' (needed by 'typo')"
    ]
    # An unused binding is refused, where Hamilton drops a source() silently.
    with pytest.raises(ValueError, match="bind names 'rate' under namespace 'eu'"):
        fnloom.Builder().with_modules(
            taxes,
            namespace="eu",
            bind={"gross": fnloom.value(1.0), "rate": fnloom.source("r")},
        ).build()
