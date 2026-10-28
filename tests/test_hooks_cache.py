"""Hooks on a cached driver: each node event carries its cache outcome (Q85).

Every test runs on both drivers, the async one with inline sync nodes so its
events come in a fixed order, and on both stores: a plain `MemoryCache`, which a
run walks in the binding, and a `DiskCache`, which it walks in Python.
"""

import asyncio
from typing import TYPE_CHECKING, Literal, cast

import pytest

import fnloom
from fnloom import CacheOutcome, DiskCache, Hook, NodeEnd, NodeStart, RunEnd, RunStart
from helpers import make_module
from test_hooks import HookError, Recorder

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

type Kind = Literal["sync", "async"]
type StoreKind = Literal["memory", "disk"]

# `late` reads `b` through `base`; `early` reads `a`; `both` reads them both. Plan
# order is `base`, `early`, `late`, `both`, and an inline async run's order too.
GRAPH = """
def base(b: int) -> int:
    return b + 1


def early(a: int) -> int:
    return a * 3


def late(base: int) -> int:
    return base * 2


def both(early: int, late: int) -> int:
    return early + late
"""

MISS = CacheOutcome("miss", None)
HIT = CacheOutcome("hit", None)


class Cached:
    """A cached graph with hooks, run by one kind of driver over one kind of store.

    `explain` is the sync driver's `explain_cache`, over the same store.
    """

    def __init__(
        self, source: str, kind: Kind, store: StoreKind, path: Path, *hooks: Hook
    ) -> None:
        """Builds `source` with `hooks` over a new store of kind `store`."""
        self.module = make_module("hooked_cache_graph", source)
        cache = fnloom.MemoryCache() if store == "memory" else DiskCache(path)
        builder = fnloom.Builder().with_modules(self.module).with_cache(cache)
        self.kind = kind
        self.explaining = builder.build()
        self.driver = builder.with_hooks(*hooks).build()
        self.async_driver = builder.with_hooks(*hooks).build_async(sync_nodes="inline")

    def execute(
        self, outputs: list[str], *, inputs: Mapping[str, object]
    ) -> dict[str, object]:
        """`execute` on this kind of driver."""
        if self.kind == "sync":
            return self.driver.execute(outputs, inputs=inputs)
        return asyncio.run(self.async_driver.execute(outputs, inputs=inputs))

    def explain(
        self, outputs: list[str], *, inputs: Mapping[str, object]
    ) -> dict[str, CacheOutcome]:
        """What `explain_cache` says now about the request."""
        return self.explaining.explain_cache(outputs, inputs=inputs)


@pytest.fixture(params=["sync", "async"])
def kind(request: pytest.FixtureRequest) -> Kind:
    """Which driver runs."""
    return cast("Kind", request.param)


@pytest.fixture(params=["memory", "disk"])
def store(request: pytest.FixtureRequest) -> StoreKind:
    """Which store the cache keeps results in."""
    return cast("StoreKind", request.param)


def _stages(hook: Recorder) -> list[tuple[str, str | None]]:
    return [(stage, node) for _, stage, node in hook.log]


def _outcomes(hook: Recorder) -> list[tuple[str, str, CacheOutcome | None]]:
    """Each node event's stage, node and outcome."""
    return [
        (stage, event.node.name, event.cache)
        for (_, stage, _), event in zip(hook.log, hook.events, strict=True)
        if isinstance(event, (NodeStart, NodeEnd))
    ]


def test_a_miss_then_a_hit_carry_their_outcomes(
    kind: Kind, store: StoreKind, tmp_path: Path
) -> None:
    """A first run's nodes miss; the second run's output hits, and says so."""
    hook = Recorder()
    cached = Cached(GRAPH, kind, store, tmp_path, hook)
    assert cached.execute(["both"], inputs={"b": 1, "a": 1}) == {"both": 7}
    assert _outcomes(hook) == [
        (stage, name, MISS)
        for name in ("base", "early", "late", "both")
        for stage in ("before_node", "after_node")
    ]
    hook.log.clear()
    hook.events.clear()
    assert cached.execute(["both"], inputs={"b": 1, "a": 1}) == {"both": 7}
    assert _outcomes(hook) == [
        ("before_node", "both", HIT),
        ("after_node", "both", HIT),
    ]


def test_a_hit_is_paired_with_no_inputs_and_the_loaded_result(
    kind: Kind, store: StoreKind, tmp_path: Path
) -> None:
    """A hit gets `before_node` and `after_node`, with no inputs and its value."""
    hook = Recorder()
    cached = Cached(GRAPH, kind, store, tmp_path, hook)
    cached.execute(["late"], inputs={"b": 1})
    hook.events.clear()
    assert cached.execute(["late"], inputs={"b": 1}) == {"late": 4}
    start, end = hook.of(NodeStart), hook.of(NodeEnd)
    assert [(s.node.name, s.inputs, s.cache) for s in start] == [("late", None, HIT)]
    assert [(e.node.name, e.inputs, e.ok, e.result, e.error) for e in end] == [
        ("late", None, True, 4, None)
    ]
    assert start[0].run_id == end[0].run_id == hook.of(RunStart)[0].run_id


def test_hits_come_first_in_plan_order(
    kind: Kind, store: StoreKind, tmp_path: Path
) -> None:
    """Hits report before any computed node, and `RunStart.nodes` lists that order."""
    hook = Recorder()
    cached = Cached(GRAPH, kind, store, tmp_path, hook)
    cached.execute(["both"], inputs={"b": 1, "a": 1})
    hook.log.clear()
    hook.events.clear()
    # A new `y`: `early` and `both` miss, while `late`, later in plan order, hits.
    assert cached.execute(["both"], inputs={"b": 1, "a": 2}) == {"both": 10}
    assert _stages(hook) == [
        ("before_run", None),
        ("before_node", "late"),
        ("after_node", "late"),
        ("before_node", "early"),
        ("after_node", "early"),
        ("before_node", "both"),
        ("after_node", "both"),
        ("after_run", None),
    ]
    assert [e.cache for e in hook.of(NodeEnd)] == [HIT, MISS, MISS]
    expected = ("late", "early", "both")
    assert hook.of(RunStart)[0].nodes == hook.of(RunEnd)[0].nodes == expected


def test_nodes_needed_only_by_hits_get_no_events(
    kind: Kind, store: StoreKind, tmp_path: Path
) -> None:
    """`base` feeds only `late`, a hit, so it neither runs nor reports."""
    hook = Recorder()
    cached = Cached(GRAPH, kind, store, tmp_path, hook)
    cached.execute(["late"], inputs={"b": 1})
    hook.log.clear()
    hook.events.clear()
    cached.execute(["both"], inputs={"b": 1, "a": 1})
    reported = {node for _, node in _stages(hook) if node is not None}
    assert reported == {"late", "early", "both"}
    assert hook.of(RunStart)[0].nodes == ("late", "early", "both")


def test_all_hits_fire_no_computed_node(
    kind: Kind, store: StoreKind, tmp_path: Path
) -> None:
    """When every output hits, only the hits report, and nothing runs."""
    hook = Recorder()
    cached = Cached(GRAPH, kind, store, tmp_path, hook)
    cached.execute(["early", "late"], inputs={"b": 1, "a": 1})
    hook.log.clear()
    hook.events.clear()
    assert cached.execute(["late", "early"], inputs={"b": 1, "a": 1}) == {
        "late": 4,
        "early": 3,
    }
    assert _outcomes(hook) == [
        ("before_node", "early", HIT),
        ("after_node", "early", HIT),
        ("before_node", "late", HIT),
        ("after_node", "late", HIT),
    ]
    assert hook.of(RunStart)[0].nodes == ("early", "late")
    end = hook.of(RunEnd)[0]
    assert end.ok
    assert dict(end.results) == {"late": 4, "early": 3}


UNCACHEABLE = """
from fnloom import cache


class Opaque:
    pass


@cache(behavior="disable")
def clock() -> int:
    return 7


def stamped(clock: int, x: int) -> int:
    return clock + x


def held(thing: Opaque) -> int:
    return 1


@cache(behavior="recompute")
def fresh(x: int) -> int:
    return x * 2
"""


def test_uncacheable_nodes_say_why(
    kind: Kind, store: StoreKind, tmp_path: Path
) -> None:
    """Every outcome, with its reason, is the one `explain_cache` gives."""
    hook = Recorder()
    cached = Cached(UNCACHEABLE, kind, store, tmp_path, hook)
    opaque = cached.module.Opaque()
    inputs: dict[str, object] = {"x": 1, "thing": opaque}
    outputs = ["stamped", "held", "fresh"]
    explained = cached.explain(outputs, inputs=inputs)
    cached.execute(outputs, inputs=inputs)
    reported = {name: outcome for _, name, outcome in _outcomes(hook)}
    assert reported == explained
    assert reported == {
        "clock": CacheOutcome("uncacheable", "policy disable"),
        "stamped": CacheOutcome("uncacheable", "reads uncacheable 'clock'"),
        "held": CacheOutcome("uncacheable", "thing: Opaque"),
        "fresh": CacheOutcome("miss", "policy recompute"),
    }


RECOMPUTED = """
from fnloom import cache


def symbol(code: str) -> str:
    return code.upper()


@cache(behavior="recompute")
def price(symbol: str) -> float:
    return 100.0


def value(price: float, lots: int) -> float:
    return price * lots
"""


def test_a_recompute_run_reports_through_the_same_run(
    kind: Kind, store: StoreKind, tmp_path: Path
) -> None:
    """The `recompute` nodes a key reads run first, and report in the same run."""
    hook = Recorder()
    cached = Cached(RECOMPUTED, kind, store, tmp_path, hook)
    inputs: dict[str, object] = {"code": "abc", "lots": 2}
    assert cached.execute(["value"], inputs=inputs) == {"value": 200.0}
    assert _outcomes(hook) == [
        (stage, name, outcome)
        for name, outcome in (
            ("symbol", MISS),
            ("price", CacheOutcome("miss", "policy recompute")),
            ("value", MISS),
        )
        for stage in ("before_node", "after_node")
    ]
    assert hook.of(RunStart)[0].nodes == ("symbol", "price", "value")
    hook.log.clear()
    hook.events.clear()
    assert cached.execute(["value"], inputs=inputs) == {"value": 200.0}
    # `symbol` hits in the run of `price`, which runs, and then `value` hits.
    assert _stages(hook) == [
        ("before_run", None),
        ("before_node", "symbol"),
        ("after_node", "symbol"),
        ("before_node", "price"),
        ("after_node", "price"),
        ("before_node", "value"),
        ("after_node", "value"),
        ("after_run", None),
    ]
    assert [e.cache.status for e in hook.of(NodeEnd)] == ["hit", "miss", "hit"]
    # Which nodes after `price` hit is known only once it ran.
    assert hook.of(RunStart)[0].nodes == ("symbol", "price", "value")


def test_a_value_check_that_fails_lists_no_nodes(
    kind: Kind, store: StoreKind, tmp_path: Path
) -> None:
    """R5 holds on a cached driver: a mistyped input runs nothing, and lists nothing."""
    hook = Recorder()
    cached = Cached(GRAPH, kind, store, tmp_path, hook)
    with pytest.raises(ValueError, match="b: expected int, got str"):
        cached.execute(["late"], inputs={"b": "1"})
    assert _stages(hook) == [("before_run", None), ("after_run", None)]
    assert hook.of(RunStart)[0].nodes == ()


def test_a_value_an_after_node_hook_refused_is_not_stored(
    kind: Kind, store: StoreKind, tmp_path: Path
) -> None:
    """R8: a node whose `after_node(ok=True)` raised is discarded, not stored."""
    hook = Recorder(raise_on=(("after_node", "late"),))
    cached = Cached(GRAPH, kind, store, tmp_path, hook)
    with pytest.raises(HookError, match="r after_node late"):
        cached.execute(["late"], inputs={"b": 1})
    explained = cached.explain(["late"], inputs={"b": 1})
    assert explained == {"base": HIT, "late": MISS}


def test_a_raising_hook_on_a_hit_stops_the_run(
    kind: Kind, store: StoreKind, tmp_path: Path
) -> None:
    """A hit's `before_node` raising stops the run before anything runs."""
    hook = Recorder()
    cached = Cached(GRAPH, kind, store, tmp_path, hook)
    cached.execute(["late"], inputs={"b": 1})
    hook.log.clear()
    hook.events.clear()
    hook.raise_on = (("before_node", "late"),)
    with pytest.raises(HookError, match="r before_node late"):
        cached.execute(["late", "early"], inputs={"b": 1, "a": 1})
    assert _stages(hook) == [
        ("before_run", None),
        ("before_node", "late"),
        ("after_run", None),
    ]
    assert not hook.of(RunEnd)[0].ok
    # `early` never ran, so it was never stored.
    assert cached.explain(["early"], inputs={"a": 1}) == {"early": MISS}


def test_the_async_driver_fires_the_sync_drivers_events(
    store: StoreKind, tmp_path: Path
) -> None:
    """Decision 7: the same cached requests give the same events on both drivers."""
    logs: dict[str, list[tuple[str, str | None, object]]] = {}
    for kind in ("sync", "async"):
        hook = Recorder()
        cached = Cached(GRAPH, kind, store, tmp_path / kind, hook)
        for request in ({"b": 1, "a": 1}, {"b": 1, "a": 2}, {"b": 1, "a": 2}):
            cached.execute(["both"], inputs=request)
        logs[kind] = [
            (stage, node, getattr(event, "cache", None))
            for (_, stage, node), event in zip(hook.log, hook.events, strict=True)
        ]
    assert logs["sync"] == logs["async"]
