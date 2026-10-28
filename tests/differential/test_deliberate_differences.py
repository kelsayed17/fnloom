"""Each deliberate difference from Hamilton, asserted on both sides."""

import asyncio
import collections.abc
import importlib.util
import inspect
import pickle
import sys
import textwrap
import types
import warnings
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, TypedDict, cast

import pytest
from hamilton import async_driver as hamilton_async
from hamilton import driver as hamilton_driver
from hamilton import htypes
from hamilton.function_modifiers.base import InvalidDecoratorException
from hamilton.lifecycle import NoEdgeAndInputTypeChecking
from harness import (
    Nodes,
    Outcome,
    load_fixture,
    nodes_fnloom,
    nodes_hamilton,
    run_fnloom,
    run_hamilton,
)
from test_semantics import DELIBERATE
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
from fnloom._types import type_matches, value_matches
from helpers import make_module

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
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

@extract_fields({{"c": int}}, fill_with=None)
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


_SLEEPER = """
import asyncio
import time

LOG: list[str] = []


def slow(x: int) -> int:
    time.sleep(0.2)
    LOG.append("slept")
    return x


async def ticker(x: int) -> int:
    await asyncio.sleep(0.05)
    LOG.append("tick")
    return x
"""


@pytest.mark.deliberate("Q65")
@pytest.mark.filterwarnings(_HAMILTON_ASYNC_WARNING)
def test_q65_sync_nodes_run_off_the_loop() -> None:
    """Hamilton's sync node stops the loop, so the ticker waits; fnloom's doesn't."""
    request = ["slow", "ticker"]
    hamilton_module = make_module("differential_q65_h", _SLEEPER)

    async def hamilton() -> object:
        built = await _hamilton_async(hamilton_module)
        return await built.execute(request, inputs={"x": 1})

    assert asyncio.run(hamilton()) == {"slow": 1, "ticker": 1}
    assert hamilton_module.LOG == ["slept", "tick"]
    for sync_nodes, order in (
        ("thread", ["tick", "slept"]),
        ("inline", ["slept", "tick"]),
    ):
        module = make_module(f"differential_q65_{sync_nodes}", _SLEEPER)
        driver = (
            fnloom.Builder().with_modules(module).build_async(sync_nodes=sync_nodes)
        )
        assert asyncio.run(driver.execute(request, inputs={"x": 1})) == {
            "slow": 1,
            "ticker": 1,
        }
        assert list(module.LOG) == order, sync_nodes


_FAILING = """
import asyncio

LOG: list[str] = []
FINISHED = asyncio.Event()


async def boom(x: int) -> int:
    await asyncio.sleep(0)
    raise KeyError("boom")


async def sibling(x: int) -> int:
    try:
        await asyncio.sleep(0.2)
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
    """Hamilton leaves the sibling running after it raises; fnloom cancels it."""
    request = ["sibling", "boom"]
    hamilton_module = make_module("differential_q66_h", _FAILING)

    async def hamilton() -> BaseException:
        built = await _hamilton_async(hamilton_module)
        with pytest.raises(KeyError) as info:
            await built.execute(request, inputs={"x": 1})
        assert hamilton_module.LOG == []
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
