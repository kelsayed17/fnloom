"""Tests for `Builder`, `build`, `Driver` and `PreparedPlan`."""

from typing import override

import pytest

import fnloom
from fnloom import Builder, Driver, _core, build
from fnloom.driver import PLAN_CACHE_SIZE
from helpers import make_module

DISTINCT_SHAPES = 2

CHAIN = """
def b(a: int) -> int:
    return a + 1

def c(b: int) -> int:
    return b * 2
"""


def _add_one(a: int) -> int:
    return a + 1


def _double(b: int) -> int:
    return b * 2


def _chain_graph() -> _core.Graph:
    """The native graph of `CHAIN`: `a` (input) -> `b` -> `c`."""
    return _core.Graph(
        [("b", [("a", True)], {}, _add_one), ("c", [("b", True)], {}, _double)]
    )


class SpyGraph:
    """Delegates to a real graph and counts `prepare` calls."""

    def __init__(self, graph: _core.Graph) -> None:
        """Wraps `graph`."""
        self.graph = graph
        self.prepares = 0

    def node_names(self) -> list[str]:
        """Delegates."""
        return self.graph.node_names()

    def upstream_of(self, names: list[str]) -> list[str]:
        """Delegates."""
        return self.graph.upstream_of(names)

    def downstream_of(self, names: list[str]) -> list[str]:
        """Delegates."""
        return self.graph.downstream_of(names)

    def prepare(
        self, outputs: list[str], inputs: list[str], overrides: list[str]
    ) -> _core.Plan:
        """Counts, then delegates."""
        self.prepares += 1
        return self.graph.prepare(outputs, inputs, overrides)


def test_execute_chain() -> None:
    """Parameter names wire the nodes: a -> b -> c."""
    driver = build(make_module("drv_chain", CHAIN))
    assert driver.execute(["c"], inputs={"a": 1}) == {"c": 4}


def test_builder_and_build_shortcut_give_the_same_graph() -> None:
    """`build(m)` and `Builder().with_modules(m).build()` agree."""
    module = make_module("drv_same", CHAIN)
    assert (
        build(module).node_names()
        == Builder().with_modules(module).build().node_names()
    )


def test_with_modules_returns_a_new_builder() -> None:
    """`with_modules` leaves the original builder unchanged and accumulates in order."""
    first = make_module("drv_first", "def x(a: int) -> int:\n    return a\n")
    second = make_module("drv_second", "def y(a: int) -> int:\n    return a\n")
    empty = Builder()
    one = empty.with_modules(first)
    two = one.with_modules(second)
    assert empty is not one
    assert empty.build().node_names() == []
    assert one.build().node_names() == ["a", "x"]
    assert two.build().node_names() == ["a", "x", "y"]


@pytest.mark.parametrize("bad", ["c", ["c", 1]])
def test_bare_string_final_vars_raises_type_error(bad: object) -> None:
    """A bare `str`, or any non-`str` item, is a `TypeError`."""
    driver = build(make_module("drv_type", CHAIN))
    with pytest.raises(TypeError):
        driver.execute(bad, inputs={"a": 1})  # ty: ignore[invalid-argument-type]  # the wrong type is the point


def test_final_vars_may_be_a_one_shot_iterator() -> None:
    """A generator of names is read once, not exhausted by the type check."""
    driver = build(make_module("drv_iter", CHAIN))
    names = (name for name in ["b", "c"])
    assert driver.execute(names, inputs={"a": 1}) == {"b": 2, "c": 4}  # ty: ignore[invalid-argument-type]  # a generator is not a Sequence, which is the point


def test_execute_empty_list_returns_empty_dict() -> None:
    """No requested outputs means no results."""
    assert build(make_module("drv_empty", CHAIN)).execute([]) == {}


def test_build_from_zero_modules_is_empty() -> None:
    """A driver over no modules has no nodes."""
    assert build().node_names() == []


def test_prepare_reuses_plan_across_runs() -> None:
    """The node runs once per run, while planning happens once."""
    calls: list[int] = []

    def counting(a: int) -> int:
        calls.append(a)
        return a + 1

    spy = SpyGraph(_core.Graph([("b", [("a", True)], {}, counting)]))
    plan = Driver(spy).prepare(["b"], inputs={"a": 0})
    assert plan.run(inputs={"a": 1}) == {"b": 2}
    assert plan.run(inputs={"a": 5}) == {"b": 6}
    assert calls == [1, 5]
    assert spy.prepares == 1


def test_execute_caches_plans_by_request_shape() -> None:
    """`execute` prepares once per distinct (outputs, input names, override names)."""
    spy = SpyGraph(_chain_graph())
    driver = Driver(spy)
    driver.execute(["c"], inputs={"a": 1})
    driver.execute(["c"], inputs={"a": 2})
    assert spy.prepares == 1
    driver.execute(["c"], overrides={"b": 7})
    assert spy.prepares == DISTINCT_SHAPES


def test_a_repeated_request_returns_the_same_plan() -> None:
    """A repeat is the same plan; a changed list or other names are not a repeat."""
    spy = SpyGraph(_chain_graph())
    driver = Driver(spy)
    outputs = ["c"]
    first = driver.prepare(outputs, inputs={"a": 0})
    assert driver.prepare(outputs, inputs={"a": 9}) is first
    assert driver.prepare(("c",), inputs={"a": 0}) is first
    outputs.append("b")
    assert driver.prepare(outputs, inputs={"a": 0}) is not first
    assert driver.prepare(["c"], overrides={"b": 0}) is not first
    assert driver.prepare(["c"], inputs={"a": 0}) is first
    assert spy.prepares == DISTINCT_SHAPES + 1


class _LooksLikeC:
    """Equal to the name "c", without being a str."""

    @override
    def __eq__(self, other: object) -> bool:
        return other == "c"

    @override
    def __hash__(self) -> int:
        return hash("c")


def test_a_repeat_with_a_non_str_name_is_refused_as_before() -> None:
    """The repeat path matches names only; an item equal to one is still refused."""
    driver = build(make_module("drv_repeat_names", CHAIN))
    driver.prepare(("c",), inputs={"a": 0})
    with pytest.raises(TypeError, match="final_vars items must be str"):
        driver.prepare((_LooksLikeC(),), inputs={"a": 0})  # ty: ignore[invalid-argument-type]  # the refusal is the point


class _Shouting(dict[str, object]):
    """A dict whose item reads give something else than what it holds."""

    @override
    def __getitem__(self, key: str) -> object:
        return "not an int"


def test_a_dict_subclass_is_read_as_the_plain_values_it_holds() -> None:
    """A dict subclass is copied, so the checks and the run read the same values."""
    driver = build(make_module("drv_dict_subclass", CHAIN))
    assert driver.execute(["c"], inputs=_Shouting(a=1)) == {"c": 4}
    plan = driver.prepare(["c"], overrides={"b": 0})
    assert plan.run(inputs=0, overrides={"b": 1}) == {"c": 2}  # ty: ignore[invalid-argument-type]  # a falsy value is no inputs, as before


def test_plan_cache_evicts_the_least_recently_used_plan() -> None:
    """Past `PLAN_CACHE_SIZE` shapes, the plan unused the longest is compiled again."""
    spy = SpyGraph(_chain_graph())
    driver = Driver(spy)
    shapes = [["c"] * repeat for repeat in range(1, PLAN_CACHE_SIZE + 1)]
    for outputs in shapes:
        driver.prepare(outputs, inputs={"a": 0})
    driver.prepare(shapes[0], inputs={"a": 0})
    assert spy.prepares == PLAN_CACHE_SIZE
    driver.prepare(["b"], inputs={"a": 0})
    driver.prepare(shapes[1], inputs={"a": 0})
    assert spy.prepares == PLAN_CACHE_SIZE + 2
    driver.prepare(shapes[0], inputs={"a": 0})
    assert spy.prepares == PLAN_CACHE_SIZE + 2


def test_prepared_plan_rejects_different_input_names() -> None:
    """A plan is compiled for its names; other names are a `ValueError`."""
    plan = build(make_module("drv_names", CHAIN)).prepare(["c"], inputs={"a": 0})
    with pytest.raises(ValueError, match="inputs"):
        plan.run(inputs={"z": 1})
    with pytest.raises(ValueError, match="overrides"):
        plan.run(inputs={"a": 1}, overrides={"b": 1})


def test_lineage_queries_include_the_named_nodes_sorted() -> None:
    """Upstream and downstream queries include the named nodes, as Hamilton's do."""
    driver = build(make_module("drv_lineage", CHAIN))
    assert driver.what_is_upstream_of("c") == ["a", "b", "c"]
    assert driver.what_is_downstream_of("b") == ["b", "c"]
    assert driver.what_is_upstream_of("c", "a") == ["a", "b", "c"]


def test_results_follow_the_requested_order_without_repeats() -> None:
    """Result keys come in requested order, each once, from `execute` and `run`."""
    driver = build(make_module("drv_order", CHAIN))
    assert list(driver.execute(["c", "a", "b", "c"], inputs={"a": 1})) == [
        "c",
        "a",
        "b",
    ]
    plan = driver.prepare(["b", "c"], inputs={"a": 1})
    assert list(plan.run(inputs={"a": 1})) == ["b", "c"]


def test_inputs_and_overrides_are_keyword_only() -> None:
    """A positional mapping is a `TypeError`, never silently inputs or overrides."""
    driver = build(make_module("drv_keywords", CHAIN))
    positional = {"a": 1}
    with pytest.raises(TypeError):
        driver.execute(["c"], positional)  # ty: ignore[too-many-positional-arguments]  # the positional call is the point
    with pytest.raises(TypeError):
        driver.prepare(["c"], positional)  # ty: ignore[too-many-positional-arguments]  # the positional call is the point
    with pytest.raises(TypeError):
        driver.prepare(["c"], inputs={"a": 1}).run(positional)  # ty: ignore[too-many-positional-arguments]  # the positional call is the point


def test_a_none_input_counts_as_supplied() -> None:
    """`None` is a value: the node receives it instead of its default."""
    source = "def out(a: int | None = 5) -> str:\n    return repr(a)\n"
    driver = build(make_module("drv_none_input", source))
    assert driver.execute(["out"], inputs={"a": None}) == {"out": "None"}


def test_a_none_override_counts_as_supplied() -> None:
    """A `None` override replaces the node, and its upstream is not needed."""
    driver = build(make_module("drv_none_override", CHAIN.replace("b * 2", "b")))
    assert driver.execute(["c"], overrides={"b": None}) == {"c": None}


def test_ten_thousand_node_chain_executes() -> None:
    """A deep chain runs without recursion trouble."""
    n = 10_000
    lines = ["def n0(x: int) -> int:\n    return x + 1\n"]
    lines += [
        f"def n{i}(n{i - 1}: int) -> int:\n    return n{i - 1} + 1\n"
        for i in range(1, n)
    ]
    driver = build(make_module("drv_deep", "\n".join(lines)))
    assert driver.execute([f"n{n - 1}"], inputs={"x": 0}) == {f"n{n - 1}": n}


def test_public_api_is_exported() -> None:
    """The package exports exactly its public names."""
    assert sorted(fnloom.__all__) == [
        "Builder",
        "Driver",
        "NodeView",
        "Param",
        "PreparedPlan",
        "Source",
        "Value",
        "build",
        "parameterize",
        "source",
        "tag",
        "value",
    ]
