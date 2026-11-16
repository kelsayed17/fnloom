"""The scheduler from Python: runs of a driver built `with_threads(1)`.

CodSpeed counts instructions on one core, so only one worker is measured here: the
scheduler's cost per node and per branch, on the calling thread. The parallel rows,
with eight workers against Hamilton's thread executor, are `benches/parallel.py`'s.
"""

from typing import TYPE_CHECKING

import pytest
from benches.generate import make_expected, make_request

import fnloom

if TYPE_CHECKING:
    from types import ModuleType

    from pytest_codspeed import BenchmarkFixture

    from benchmarks.conftest import LoadGraph

CACHED_BRANCHES = 1000
LAYERED_NODES = 1000


def _one_worker(module: ModuleType, *, cached: bool = False) -> fnloom.Driver:
    builder = fnloom.Builder().with_modules(module).with_threads(1)
    return (builder.with_cache(fnloom.MemoryCache()) if cached else builder).build()


@pytest.mark.parametrize("branches", [1, 100, 10000])
def test_fan_out_on_one_worker(
    benchmark: BenchmarkFixture, load_graph: LoadGraph, branches: int
) -> None:
    """Measure a fan-out of `branches` one-node branches on the scheduler."""
    outputs, inputs = make_request("fan_out", branches)
    prepared = _one_worker(load_graph("fan_out", branches)).prepare(
        outputs, inputs=inputs
    )
    assert prepared.run(inputs=inputs) == make_expected("fan_out", branches)
    benchmark(prepared.run, inputs=inputs)


def test_layered_on_one_worker(
    benchmark: BenchmarkFixture, load_graph: LoadGraph
) -> None:
    """Measure the 1,000-node layered graph, which has no blocks, on the scheduler."""
    outputs, inputs = make_request("layered", LAYERED_NODES)
    prepared = _one_worker(load_graph("layered", LAYERED_NODES)).prepare(
        outputs, inputs=inputs
    )
    assert prepared.run(inputs=inputs) == make_expected("layered", LAYERED_NODES)
    benchmark(prepared.run, inputs=inputs)


def test_cached_fan_out_warm_on_one_worker(
    benchmark: BenchmarkFixture, load_graph: LoadGraph
) -> None:
    """Measure a cached fan-out of 1,000 stored branches on the scheduler."""
    module = load_graph("fan_out", CACHED_BRANCHES)
    prepared = _one_worker(module, cached=True).prepare(["total"], inputs={"count": 0})
    inputs = {"count": CACHED_BRANCHES}
    assert prepared.run(inputs=inputs) == make_expected("fan_out", CACHED_BRANCHES)
    benchmark(prepared.run, inputs=inputs)
