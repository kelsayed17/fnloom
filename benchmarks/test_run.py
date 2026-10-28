"""A prepared plan's steady ``run`` on each shape of graph, and the driver around it.

``run`` is what a service calls on every request, so it is the number the library is
judged by. The graph is built and the plan prepared outside the measured call; what is
measured is the walk, the node calls and the result mapping.
"""

from typing import TYPE_CHECKING

import pytest
from benches.generate import Shape, make_expected, make_request

import fnloom

if TYPE_CHECKING:
    from pytest_codspeed import BenchmarkFixture

    from benchmarks.conftest import LoadGraph

SHAPES: tuple[Shape, ...] = ("chain", "layered", "fan_in", "rows", "extract")
SIZES = (100, 1000)


@pytest.mark.parametrize("n", SIZES)
@pytest.mark.parametrize("shape", SHAPES)
def test_prepared_run(
    benchmark: BenchmarkFixture, load_graph: LoadGraph, shape: Shape, n: int
) -> None:
    """Measure one run of a prepared plan over ``n`` nodes, input checks on."""
    outputs, inputs = make_request(shape, n)
    prepared = (
        fnloom.Builder()
        .with_modules(load_graph(shape, n))
        .build()
        .prepare(outputs, inputs=inputs)
    )
    assert prepared.run(inputs=inputs) == make_expected(shape, n)
    benchmark(prepared.run, inputs=inputs)


@pytest.mark.parametrize("shape", ["chain", "layered"])
def test_prepared_run_without_input_checks(
    benchmark: BenchmarkFixture, load_graph: LoadGraph, shape: Shape
) -> None:
    """Measure one run of a 1,000-node plan with input type checks off.

    Next to the checked runs, it keeps the cost of the checks visible.
    """
    outputs, inputs = make_request(shape, 1000)
    driver = (
        fnloom.Builder()
        .with_modules(load_graph(shape, 1000))
        .without_input_type_checks()
        .build()
    )
    prepared = driver.prepare(outputs, inputs=inputs)
    benchmark(prepared.run, inputs=inputs)


@pytest.mark.parametrize("shape", ["chain", "layered"])
def test_driver_execute_repeated(
    benchmark: BenchmarkFixture, load_graph: LoadGraph, shape: Shape
) -> None:
    """Measure ``Driver.execute`` on a request it has already seen.

    The plan cache's fast path finds the plan, so this is ``run`` plus the lookup.
    """
    outputs, inputs = make_request(shape, 100)
    driver = fnloom.Builder().with_modules(load_graph(shape, 100)).build()
    driver.execute(outputs, inputs=inputs)
    benchmark(driver.execute, outputs, inputs=inputs)


def test_build_1000_nodes(benchmark: BenchmarkFixture, load_graph: LoadGraph) -> None:
    """Measure ``Builder.build`` over a 1,000-node layered graph.

    That is the scan, the lowering and the Rust graph.
    """
    builder = fnloom.Builder().with_modules(load_graph("layered", 1000))
    benchmark(builder.build)
