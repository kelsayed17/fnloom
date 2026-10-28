"""Each deliberate difference from Hamilton, asserted on both sides."""

import collections.abc
import importlib.util
import inspect
import pickle
import sys
import textwrap
import types
from typing import TYPE_CHECKING, Annotated, Any

import pytest
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
    from collections.abc import Callable
    from pathlib import Path
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
