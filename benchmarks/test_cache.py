"""Cached runs against a ``MemoryCache``, warm and cold.

A warm run hits for every node, which is the cost of the key walk plus the lookups; a
cold run misses and stores every node, which is that cost plus the writes. The two
bound what a cache adds to a run.
"""

from typing import TYPE_CHECKING

import pytest
from benches.generate import Shape, make_expected, make_request

import fnloom

if TYPE_CHECKING:
    from pytest_codspeed import BenchmarkFixture

    from benchmarks.conftest import LoadGraph

CASES: tuple[tuple[Shape, int], ...] = (("chain", 200), ("layered", 1000))


@pytest.mark.parametrize(("shape", "n"), CASES)
def test_warm_run(
    benchmark: BenchmarkFixture, load_graph: LoadGraph, shape: Shape, n: int
) -> None:
    """Measure a run in which every node is already in the store."""
    outputs, inputs = make_request(shape, n)
    builder = (
        fnloom.Builder()
        .with_modules(load_graph(shape, n))
        .with_cache(fnloom.MemoryCache())
    )
    prepared = builder.build().prepare(outputs, inputs=inputs)
    assert prepared.run(inputs=inputs) == make_expected(shape, n)
    benchmark(prepared.run, inputs=inputs)


@pytest.mark.parametrize(("shape", "n"), CASES)
def test_cold_run(
    benchmark: BenchmarkFixture, load_graph: LoadGraph, shape: Shape, n: int
) -> None:
    """Measure a run against an empty store, which runs and stores every node.

    Each round gets a fresh store.
    """
    outputs, inputs = make_request(shape, n)
    module = load_graph(shape, n)

    def fresh() -> tuple[tuple[fnloom.PreparedPlan], dict[str, object]]:
        builder = fnloom.Builder().with_modules(module).with_cache(fnloom.MemoryCache())
        return (builder.build().prepare(outputs, inputs=inputs),), {}

    def cold(prepared: fnloom.PreparedPlan) -> None:
        prepared.run(inputs=inputs)

    benchmark.pedantic(cold, setup=fresh, rounds=20)
