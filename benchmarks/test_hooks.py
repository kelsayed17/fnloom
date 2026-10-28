"""Hooked runs: a hook overriding all four methods, on both drivers.

Each case runs a 1,000-node chain, so next to ``test_run.py``'s unhooked chain the
difference over 1,000 is what a hook costs per node. Output checks add one check per
node on top. A hooked ``MemoryCache`` run walks the keys in Python, not natively, so
its warm and cold runs are measured too.
"""

from typing import TYPE_CHECKING

import pytest
from benches.generate import make_expected, make_request
from benches.hooks import Counting

import fnloom

if TYPE_CHECKING:
    from collections.abc import Callable

    from pytest_codspeed import BenchmarkFixture

    from benchmarks.conftest import LoadGraph, Operation

N = 1000


def _builder(load_graph: LoadGraph, *, output_checks: bool) -> fnloom.Builder:
    """The 1,000-node chain with the counting hook, output checks on if asked."""
    builder = (
        fnloom.Builder().with_modules(load_graph("chain", N)).with_hooks(Counting())
    )
    return builder.with_output_type_checks() if output_checks else builder


@pytest.mark.parametrize("output_checks", [False, True])
def test_hooked_run(
    benchmark: BenchmarkFixture, load_graph: LoadGraph, *, output_checks: bool
) -> None:
    """Measure one hooked run of a prepared 1,000-node chain."""
    outputs, inputs = make_request("chain", N)
    prepared = (
        _builder(load_graph, output_checks=output_checks)
        .build()
        .prepare(outputs, inputs=inputs)
    )
    assert prepared.run(inputs=inputs) == make_expected("chain", N)
    benchmark(prepared.run, inputs=inputs)


@pytest.mark.parametrize("output_checks", [False, True])
def test_hooked_async_run(
    run_batch: Callable[[Operation], None],
    load_graph: LoadGraph,
    *,
    output_checks: bool,
) -> None:
    """Measure one hooked async run of a 1,000-node chain, its nodes inline."""
    outputs, inputs = make_request("chain", N)
    plan = (
        _builder(load_graph, output_checks=output_checks)
        .build_async(sync_nodes="inline")
        .prepare(outputs, inputs=inputs)
    )
    run_batch(lambda: plan.run(inputs=inputs))


def test_hooked_warm_cached_run(
    benchmark: BenchmarkFixture, load_graph: LoadGraph
) -> None:
    """Measure a hooked run in which every node is already in a ``MemoryCache``."""
    outputs, inputs = make_request("chain", N)
    builder = _builder(load_graph, output_checks=False).with_cache(fnloom.MemoryCache())
    prepared = builder.build().prepare(outputs, inputs=inputs)
    assert prepared.run(inputs=inputs) == make_expected("chain", N)
    benchmark(prepared.run, inputs=inputs)


def test_hooked_cold_cached_run(
    benchmark: BenchmarkFixture, load_graph: LoadGraph
) -> None:
    """Measure a hooked run against an empty ``MemoryCache``; each round a new one."""
    outputs, inputs = make_request("chain", N)
    module = load_graph("chain", N)

    def fresh() -> tuple[tuple[fnloom.PreparedPlan], dict[str, object]]:
        builder = (
            fnloom.Builder()
            .with_modules(module)
            .with_hooks(Counting())
            .with_cache(fnloom.MemoryCache())
        )
        return (builder.build().prepare(outputs, inputs=inputs),), {}

    def cold(prepared: fnloom.PreparedPlan) -> None:
        prepared.run(inputs=inputs)

    benchmark.pedantic(cold, setup=fresh, rounds=20)
