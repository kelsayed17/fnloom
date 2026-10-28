"""An async plan in both ``sync_nodes`` modes, batched on one event loop.

``thread`` hands each sync node to the loop's executor and ``inline`` calls it on the
loop, so the pair shows what the hand-off costs. A graph whose nodes are all
``async def`` is measured inline, which is the loop's own scheduling.
"""

from typing import TYPE_CHECKING, Literal

import pytest
from benches.generate import Shape, make_request

import fnloom

if TYPE_CHECKING:
    from collections.abc import Callable

    from benchmarks.conftest import LoadGraph, Operation

type Mode = Literal["thread", "inline"]


@pytest.mark.parametrize("mode", ["thread", "inline"])
@pytest.mark.parametrize("shape", ["chain", "layered"])
def test_sync_nodes(
    run_batch: Callable[[Operation], None],
    load_graph: LoadGraph,
    shape: Shape,
    mode: Mode,
) -> None:
    """Measure an async run of 100 plain functions in one ``sync_nodes`` mode."""
    outputs, inputs = make_request(shape, 100)
    driver = (
        fnloom.Builder()
        .with_modules(load_graph(shape, 100))
        .build_async(sync_nodes=mode)
    )
    plan = driver.prepare(outputs, inputs=inputs)
    run_batch(lambda: plan.run(inputs=inputs))


@pytest.mark.parametrize("shape", ["chain", "layered"])
def test_async_nodes(
    run_batch: Callable[[Operation], None], load_graph: LoadGraph, shape: Shape
) -> None:
    """Measure an async run of 100 nodes that are all ``async def``."""
    outputs, inputs = make_request(shape, 100)
    module = load_graph(shape, 100, asynchronous=True)
    plan = (
        fnloom.Builder()
        .with_modules(module)
        .build_async(sync_nodes="inline")
        .prepare(outputs, inputs=inputs)
    )
    run_batch(lambda: plan.run(inputs=inputs))
