"""Result builders (Q19, Q24) and the raw results that skip them, on both drivers."""

import asyncio
import copy
import pickle
from typing import TYPE_CHECKING, Any, Literal, assert_type, cast

import pytest

import fnloom
import picklable_nodes
from fnloom import RunEnd
from helpers import make_module
from test_hooks import HookError, Recorder

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from types import ModuleType

GRAPH = """
def a(x: int) -> int:
    return x + 1

def b(a: int) -> int:
    return a * 2

def c(b: int) -> int:
    return b + 3
"""

type SyncNodes = Literal["thread", "inline"]


def pairs(outputs: dict[str, object]) -> list[tuple[str, object]]:
    """A builder: the outputs as `(name, value)` pairs, in the order given."""
    return list(outputs.items())


class Seen:
    """A builder that keeps each dict it gets, and returns how many it has seen."""

    def __init__(self) -> None:
        """No dict seen yet."""
        self.got: list[dict[str, object]] = []

    def __call__(self, outputs: dict[str, object]) -> int:
        """Keeps a copy of `outputs`, in order."""
        self.got.append(dict(outputs))
        return len(self.got)


class BuildError(Exception):
    """What a raising builder raises."""


def refuse(outputs: dict[str, object]) -> object:
    """A builder that always raises."""
    msg = f"cannot build {sorted(outputs)}"
    raise BuildError(msg)


# What a type checker sees: the raw dict with no builder, the builder's type with one.
_PLAIN = fnloom.Builder().with_modules(picklable_nodes).build()
_BUILT = fnloom.Builder().with_modules(picklable_nodes).with_result_builder(pairs)
_TOTAL_INPUTS = {"a": 1, "b": 2}
assert_type(fnloom.Builder(), fnloom.Builder)
assert_type(fnloom.Builder(), fnloom.Builder[dict[str, object]])
assert_type(_BUILT, fnloom.Builder[list[tuple[str, object]]])
assert_type(_PLAIN.execute(["total"], inputs=_TOTAL_INPUTS), dict[str, object])
assert_type(
    _BUILT.build().execute(["total"], inputs=_TOTAL_INPUTS), list[tuple[str, object]]
)
assert_type(
    _BUILT.build().raw_execute(["total"], inputs=_TOTAL_INPUTS), dict[str, object]
)
assert_type(
    _PLAIN.prepare(["total"], inputs=_TOTAL_INPUTS).run(inputs=_TOTAL_INPUTS),
    dict[str, object],
)
assert_type(
    _BUILT.build().prepare(["total"], inputs=_TOTAL_INPUTS).run(inputs=_TOTAL_INPUTS),
    list[tuple[str, object]],
)
assert_type(
    _BUILT.build()
    .prepare(["total"], inputs=_TOTAL_INPUTS)
    .run_raw(inputs=_TOTAL_INPUTS),
    dict[str, object],
)
assert_type(_BUILT.with_hooks().build(), fnloom.Driver[list[tuple[str, object]]])
assert_type(
    fnloom.Builder().with_result_builder(len).build_async(), fnloom.AsyncDriver[int]
)


async def _typed_async() -> None:
    """Never run: ty checks what the async driver's coroutines return."""
    plain = fnloom.Builder().with_modules(picklable_nodes).build_async()
    built = _BUILT.build_async()
    assert_type(await plain.execute(["total"], inputs=_TOTAL_INPUTS), dict[str, object])
    assert_type(
        await built.execute(["total"], inputs=_TOTAL_INPUTS), list[tuple[str, object]]
    )
    assert_type(
        await built.raw_execute(["total"], inputs=_TOTAL_INPUTS), dict[str, object]
    )
    plan = built.prepare(["total"], inputs=_TOTAL_INPUTS)
    assert_type(plan, fnloom.AsyncPreparedPlan[list[tuple[str, object]]])
    assert_type(await plan.run(inputs=_TOTAL_INPUTS), list[tuple[str, object]])
    assert_type(await plan.run_raw(inputs=_TOTAL_INPUTS), dict[str, object])


def _module() -> ModuleType:
    return make_module("results_graph", GRAPH)


def _run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coroutine)


# Each way a sync driver runs: plain, hooked, cached, and both.
KINDS = ["plain", "hooked", "cached", "hooked-cached"]


def _builder(kind: str, *hooks: fnloom.Hook) -> fnloom.Builder:
    builder = fnloom.Builder().with_modules(_module())
    if "hooked" in kind:
        builder = builder.with_hooks(*(hooks or (Recorder(),)))
    if "cached" in kind:
        builder = builder.with_cache(fnloom.MemoryCache())
    return builder


@pytest.mark.parametrize("kind", KINDS)
def test_execute_returns_what_the_builder_returns(kind: str) -> None:
    """`execute` and `PreparedPlan.run` return the built value, on every path."""
    driver = _builder(kind).with_result_builder(pairs).build()
    for _ in range(2):  # a cached driver hits on the second run
        assert driver.execute(["c", "a"], inputs={"x": 1}) == [("c", 7), ("a", 2)]
    plan = driver.prepare(["a", "c"], inputs={"x": 0})
    assert plan.run(inputs={"x": 2}) == [("a", 3), ("c", 9)]


def test_without_a_builder_the_plan_is_exactly_a_prepared_plan() -> None:
    """The builder-free, hook-free path is today's plan, which returns the dict."""
    driver = fnloom.Builder().with_modules(_module()).build()
    plan = driver.prepare(["c"], inputs={"x": 0})
    assert type(plan) is fnloom.PreparedPlan
    assert plan.run(inputs={"x": 1}) == plan.run_raw(inputs={"x": 1}) == {"c": 7}
    assert driver.raw_execute(["c"], inputs={"x": 1}) == {"c": 7}


@pytest.mark.parametrize("kind", KINDS)
def test_the_builder_gets_request_order_with_requested_inputs_and_overrides(
    kind: str,
) -> None:
    """H6.12: request order, each output once, inputs and overrides included."""
    seen = Seen()
    driver = _builder(kind).with_result_builder(seen).build()
    assert driver.execute(["c", "x", "a"], inputs={"x": 1}) == 1
    assert driver.execute(["a"], overrides={"a": 99}) == 2  # noqa: PLR2004  # the second build
    assert driver.execute(["c", "c", "b"], inputs={"x": 1}, overrides={"a": 5}) == 3  # noqa: PLR2004  # the third build
    assert [list(got.items()) for got in seen.got] == [
        [("c", 7), ("x", 1), ("a", 2)],
        [("a", 99)],
        [("c", 13), ("b", 10)],
    ]


@pytest.mark.parametrize("hooked", [False, True], ids=["plain", "hooked"])
def test_raw_execute_and_run_raw_skip_the_builder_and_run_the_hooks(
    hooked: bool,  # noqa: FBT001  # a parameter
) -> None:
    """`raw_execute` and `run_raw` fire every hook and return the raw dict (H6.13)."""
    hook = Recorder()
    seen = Seen()
    builder = fnloom.Builder().with_modules(_module()).with_result_builder(seen)
    driver = (builder.with_hooks(hook) if hooked else builder).build()
    assert driver.raw_execute(["c", "a"], inputs={"x": 1}) == {"c": 7, "a": 2}
    plan = driver.prepare(["c"], inputs={"x": 0})
    assert plan.run_raw(inputs={"x": 2}) == {"c": 9}
    assert seen.got == []
    expected_ends = [{"c": 7, "a": 2}, {"c": 9}] if hooked else []
    assert [dict(cast("RunEnd", e).results or {}) for e in hook.of(RunEnd)] == (
        expected_ends
    )
    if hooked:
        before_nodes = [stage for _, stage, _ in hook.log].count("before_node")
        assert before_nodes == 2 * len("abc")  # a, b and c, in both runs
    # The builder still applies to `execute` and `run` on the same driver and plan.
    assert driver.execute(["c"], inputs={"x": 1}) == 1
    assert plan.run(inputs={"x": 1}) == 2  # noqa: PLR2004  # the second build


def test_raw_results_take_keywords_only() -> None:
    """`inputs` and `overrides` are keyword-only, as on `execute` (Q31)."""
    driver = fnloom.Builder().with_modules(_module()).build()
    with pytest.raises(TypeError):
        driver.raw_execute(["c"], {"x": 1})  # ty: ignore[too-many-positional-arguments]  # the case under test
    plan = driver.prepare(["c"], inputs={"x": 0})
    with pytest.raises(TypeError):
        plan.run_raw({"x": 1})  # ty: ignore[too-many-positional-arguments]  # the case under test


def test_raw_execute_fires_the_hooks_for_a_missing_input() -> None:
    """Decision 4 holds for `raw_execute`: `before_run`, the error, `after_run`."""
    hook = Recorder()
    driver = (
        fnloom.Builder()
        .with_modules(_module())
        .with_hooks(hook)
        .with_result_builder(pairs)
        .build()
    )
    with pytest.raises(ValueError, match="missing inputs: 'x'"):
        driver.raw_execute(["c"])
    assert [stage for _, stage, _ in hook.log] == ["before_run", "after_run"]


def test_a_second_builder_is_refused() -> None:
    """Q24: one builder per driver; the second call names both."""
    builder = fnloom.Builder().with_result_builder(pairs)
    with pytest.raises(
        ValueError,
        match=r"^a builder has one result builder: pairs is set, so len is refused",
    ):
        builder.with_result_builder(len)
    # The first builder stays, and other settings keep it.
    kept = builder.with_modules(_module()).with_hooks(Recorder())
    assert kept.build().execute(["a"], inputs={"x": 1}) == [("a", 2)]
    with pytest.raises(ValueError, match="pairs is set, so pairs is refused"):
        kept.with_result_builder(pairs)


def test_a_builder_must_be_callable() -> None:
    """A builder that can't be called is a `TypeError` when it is set."""
    with pytest.raises(TypeError, match="a result builder must be callable, not 3"):
        fnloom.Builder().with_result_builder(3)  # ty: ignore[invalid-argument-type]  # the case under test


@pytest.mark.parametrize("kind", KINDS)
def test_a_raising_builder_fails_the_run_with_raw_results(kind: str) -> None:
    """Review Focus 4: the builder's error, `after_run` with the raw `results`.

    On the same driver, `raw_execute` still returns the dict.
    """
    hook = Recorder()
    driver = _builder(kind, hook).with_result_builder(refuse).build()
    with pytest.raises(BuildError, match=r"cannot build \['a', 'c'\]") as raised:
        driver.execute(["c", "a"], inputs={"x": 1})
    assert getattr(raised.value, "__notes__", None) is None
    assert raised.value.__context__ is None
    if "hooked" in kind:
        end = hook.of(RunEnd)[-1]
        assert (end.ok, end.error) == (False, raised.value)
        assert end.results is not None
        assert list(end.results.items()) == [("c", 7), ("a", 2)]
        assert [stage for _, stage, _ in hook.log][-1] == "after_run"
    assert driver.raw_execute(["c", "a"], inputs={"x": 1}) == {"c": 7, "a": 2}


def test_a_raising_builder_and_a_raising_after_run_are_one_group() -> None:
    """Decision 5: the builder's error leads, then the `after_run` hook's."""
    hook = Recorder(raise_on=(("after_run", None),))
    driver = _builder("hooked", hook).with_result_builder(refuse).build()
    with pytest.raises(
        ExceptionGroup, match="the run failed and hooks raised"
    ) as group:
        driver.execute(["c"], inputs={"x": 1})
    assert [type(error) for error in group.value.exceptions] == [BuildError, HookError]
    end = hook.of(RunEnd)[-1]
    assert end.results is not None
    assert dict(end.results) == {"c": 7}


@pytest.mark.parametrize("sync_nodes", ["thread", "inline"])
@pytest.mark.parametrize("kind", KINDS)
def test_the_async_driver_uses_the_builder(kind: str, sync_nodes: SyncNodes) -> None:
    """The async driver builds as the sync one does; its raw forms are coroutines."""
    seen = Seen()
    driver = _builder(kind).with_result_builder(seen).build_async(sync_nodes=sync_nodes)
    assert _run(driver.execute(["c", "x", "a"], inputs={"x": 1})) == 1
    plan = driver.prepare(["a"], overrides={"a": 0})
    assert _run(plan.run(overrides={"a": 99})) == 2  # noqa: PLR2004  # the second build
    assert [list(got.items()) for got in seen.got] == [
        [("c", 7), ("x", 1), ("a", 2)],
        [("a", 99)],
    ]
    raw = driver.raw_execute(["c"], inputs={"x": 1})
    assert asyncio.iscoroutine(raw)
    assert _run(raw) == {"c": 7}
    assert _run(plan.run_raw(overrides={"a": 5})) == {"a": 5}
    assert len(seen.got) == 2  # noqa: PLR2004  # the raw runs built nothing


@pytest.mark.parametrize("kind", KINDS)
def test_a_raising_builder_fails_an_async_run_with_raw_results(kind: str) -> None:
    """Review Focus 4 on the async driver."""
    hook = Recorder()
    driver = _builder(kind, hook).with_result_builder(refuse).build_async()
    with pytest.raises(BuildError):
        _run(driver.execute(["c"], inputs={"x": 1}))
    if "hooked" in kind:
        end = hook.of(RunEnd)[-1]
        assert (end.ok, type(end.error)) == (False, BuildError)
        assert end.results is not None
        assert dict(end.results) == {"c": 7}
    assert _run(driver.raw_execute(["c"], inputs={"x": 1})) == {"c": 7}


def _round_trip[T](value: T) -> T:
    return cast("T", pickle.loads(pickle.dumps(value)))  # noqa: S301  # our own pickle


def test_the_builder_survives_pickling_copy_rebuild_and_build_async() -> None:
    """Every way of carrying a driver keeps its builder, by Python's own rules."""
    builder = fnloom.Builder().with_modules(picklable_nodes).with_result_builder(len)
    driver = builder.build()
    inputs = {"a": 3, "b": 1}
    request = (["doubled", "total"],)
    carried: list[fnloom.Driver[int]] = [
        driver,
        copy.copy(driver),
        copy.deepcopy(driver),
        driver.rebuild(),
        _round_trip(driver),
        _round_trip(builder).build(),
    ]
    plan = _round_trip(driver.prepare(*request, inputs=inputs))
    assert plan.run(inputs=inputs) == 2  # noqa: PLR2004  # two outputs
    for each in carried:
        assert each.execute(*request, inputs=inputs) == 2  # noqa: PLR2004  # two outputs
        assert each.raw_execute(*request, inputs=inputs) == {"doubled": 6, "total": 7}
    async_drivers = [
        builder.build_async(),
        builder.build_async().rebuild(),
        copy.copy(builder.build_async()),
        _round_trip(builder.build_async()),
    ]
    for each_async in async_drivers:
        assert _run(each_async.execute(*request, inputs=inputs)) == 2  # noqa: PLR2004  # two outputs
    async_plan = _round_trip(builder.build_async().prepare(*request, inputs=inputs))
    assert _run(async_plan.run(inputs=inputs)) == 2  # noqa: PLR2004  # two outputs

    local = fnloom.Builder().with_modules(picklable_nodes)
    unpicklable = local.with_result_builder(lambda outputs: outputs)
    assert copy.copy(unpicklable.build()).execute(*request, inputs=inputs) == {
        "doubled": 6,
        "total": 7,
    }
    with pytest.raises((pickle.PicklingError, AttributeError)):
        pickle.dumps(unpicklable.build())


# The recipe docs/running.md documents, under "Result builders".
def _frame_builder(pandas: Any) -> Callable[[dict[str, object]], object]:  # noqa: ANN401  # the pandas module
    def frame(outputs: dict[str, object]) -> object:
        """One column per output; Series on different indexes are refused."""
        indexes = [v.index for v in outputs.values() if isinstance(v, pandas.Series)]
        if any(not index.equals(indexes[0]) for index in indexes[1:]):
            msg = f"outputs on different indexes: {', '.join(outputs)}"
            raise ValueError(msg)
        return pandas.DataFrame(outputs)

    return frame


SERIES = """
import pandas as pd


def prices() -> pd.Series:
    return pd.Series([1.0, 2.0, 3.0])


def quantities() -> pd.Series:
    return pd.Series([1, 2, 3])


def shorter() -> pd.Series:
    return pd.Series([10, 20])


def revenue(prices: pd.Series, quantities: pd.Series) -> pd.Series:
    return prices * quantities
"""


def test_the_documented_pandas_recipe_builds_a_frame() -> None:
    """`pandas.DataFrame` builds a frame; the recipe refuses Series it would pad."""
    pandas = pytest.importorskip("pandas")
    module = make_module("results_series", SERIES)
    plain = fnloom.Builder().with_modules(module).with_result_builder(pandas.DataFrame)
    frame = plain.build().execute(["revenue", "quantities"])
    assert isinstance(frame, pandas.DataFrame)
    assert list(frame.columns) == ["revenue", "quantities"]
    assert frame["revenue"].tolist() == [1.0, 4.0, 9.0]
    strict = (
        fnloom.Builder()
        .with_modules(module)
        .with_result_builder(_frame_builder(pandas))
    )
    built = strict.build().execute(["revenue", "quantities"])
    assert isinstance(built, pandas.DataFrame)
    assert built.equals(frame)
    with pytest.raises(
        ValueError, match="outputs on different indexes: prices, shorter"
    ):
        strict.build().execute(["prices", "shorter"])
