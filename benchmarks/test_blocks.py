"""Fan-out and absence: a prepared plan's run over blocks, cached and declining.

A fan-out runs one trivial branch per item, so its run is the per-branch overhead. A
cached fan-out is the key walk over every branch, warm or with one item appended. A
200-node chain that may be absent is the absence-aware loop, beside the plain chain
that never takes it.
"""

from typing import TYPE_CHECKING

import pytest
from benches.generate import (
    absent_chain_source,
    make_expected,
    make_request,
    module_source,
)

import fnloom

if TYPE_CHECKING:
    from pytest_codspeed import BenchmarkFixture

    from benchmarks.conftest import LoadGraph, LoadSource

CACHED_BRANCHES = 1000
CHAIN_NODES = 200


@pytest.mark.parametrize("branches", [1, 100, 10000])
def test_fan_out_run(
    benchmark: BenchmarkFixture, load_graph: LoadGraph, branches: int
) -> None:
    """Measure a run of `branches` one-node branches, collected into their sum."""
    outputs, inputs = make_request("fan_out", branches)
    driver = fnloom.Builder().with_modules(load_graph("fan_out", branches)).build()
    prepared = driver.prepare(outputs, inputs=inputs)
    assert prepared.run(inputs=inputs) == make_expected("fan_out", branches)
    benchmark(prepared.run, inputs=inputs)


def _cached_fan_out(load_graph: LoadGraph) -> fnloom.PreparedPlan:
    """A plan over the fan-out with an empty `MemoryCache`."""
    module = load_graph("fan_out", CACHED_BRANCHES)
    builder = fnloom.Builder().with_modules(module).with_cache(fnloom.MemoryCache())
    return builder.build().prepare(["total"], inputs={"count": 0})


def test_cached_fan_out_warm(
    benchmark: BenchmarkFixture, load_graph: LoadGraph
) -> None:
    """Measure a fan-out of 1,000 branches whose items and branches are all stored."""
    prepared = _cached_fan_out(load_graph)
    inputs = {"count": CACHED_BRANCHES}
    assert prepared.run(inputs=inputs) == make_expected("fan_out", CACHED_BRANCHES)
    benchmark(prepared.run, inputs=inputs)


def test_cached_fan_out_appended(
    benchmark: BenchmarkFixture, load_graph: LoadGraph
) -> None:
    """Measure 1,001 items after 1,000 were stored: one branch runs, 1,000 hit.

    Each round gets a fresh store, filled with the 1,000 branches before it.
    """
    stored = {"count": CACHED_BRANCHES}
    appended = {"count": CACHED_BRANCHES + 1}

    def fresh() -> tuple[tuple[fnloom.PreparedPlan], dict[str, object]]:
        prepared = _cached_fan_out(load_graph)
        prepared.run(inputs=stored)
        return (prepared,), {}

    def run(prepared: fnloom.PreparedPlan) -> None:
        prepared.run(inputs=appended)

    ((checked,), _) = fresh()
    expected = make_expected("fan_out", CACHED_BRANCHES + 1)
    assert checked.run(inputs=appended) == expected
    benchmark.pedantic(run, setup=fresh, rounds=20)


@pytest.mark.parametrize("chain", ["plain", "present", "declines"])
def test_absent_chain_run(
    benchmark: BenchmarkFixture, load_source: LoadSource, chain: str
) -> None:
    """Measure a 200-node chain: plain, through nodes that may be absent, or skipped.

    `plain` has no node that may be absent, so it runs the plain loop; `present` and
    `declines` have a first node that may decline, and every other node reads it.
    """
    outputs, inputs = make_request("chain", CHAIN_NODES)
    if chain == "plain":
        source = module_source("chain", CHAIN_NODES)
    else:
        source = absent_chain_source(CHAIN_NODES)
        inputs = {**inputs, "skip": chain == "declines"}
    module = load_source(f"bench_absent_chain_{chain}", source)
    prepared = fnloom.build(module).prepare(outputs, inputs=inputs)
    expected = make_expected("chain", CHAIN_NODES)
    if chain == "declines":
        expected = {outputs[0]: fnloom.ABSENT}
    assert prepared.run(inputs=inputs) == expected
    benchmark(prepared.run, inputs=inputs)
