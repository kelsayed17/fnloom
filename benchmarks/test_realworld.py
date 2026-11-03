"""The mixed numpy workload of `benches/realworld.py`: builds and steady runs.

Forty-odd nodes of numpy arrays with a `parameterize` fan-out and an
`extract_fields`, and a function of 200 `parameterize` rows. Numpy is imported at the
top of the generated module, so what is measured is fnloom, not numpy's import.
"""

from typing import TYPE_CHECKING

import numpy as np
import pytest
from benches.realworld import request, source_of

import fnloom

if TYPE_CHECKING:
    from collections.abc import Callable

    from pytest_codspeed import BenchmarkFixture

    from benchmarks.conftest import LoadSource, Operation

assert np.ones(1)  # the generated modules import numpy themselves


def _inputs() -> dict[str, object]:
    return {"seed": 7}


def test_mixed_run(benchmark: BenchmarkFixture, load_source: LoadSource) -> None:
    """Measure a steady run of the mixed graph."""
    module = load_source(
        "bench_realworld_mixed", source_of("mixed", "fnloom", "loaded")
    )
    plan = (
        fnloom.Builder()
        .with_modules(module)
        .build()
        .prepare(request("mixed"), inputs=_inputs())
    )
    plan.run(inputs=_inputs())
    benchmark(lambda: plan.run(inputs=_inputs()))


def test_mixed_build(benchmark: BenchmarkFixture, load_source: LoadSource) -> None:
    """Measure building the mixed graph."""
    module = load_source(
        "bench_realworld_mixed_build", source_of("mixed", "fnloom", "loaded")
    )
    benchmark(lambda: fnloom.Builder().with_modules(module).build())


def test_rows_np_build(benchmark: BenchmarkFixture, load_source: LoadSource) -> None:
    """Measure building 200 `parameterize` rows."""
    module = load_source(
        "bench_realworld_rows", source_of("rows_np", "fnloom", "loaded")
    )
    benchmark(lambda: fnloom.Builder().with_modules(module).build())


@pytest.mark.parametrize("mode", ["thread", "inline"])
def test_mixed_async_run(
    run_batch: Callable[[Operation], None], load_source: LoadSource, mode: str
) -> None:
    """Measure an async run of the mixed graph in one `sync_nodes` mode."""
    module = load_source(
        "bench_realworld_mixed_async", source_of("mixed_async", "fnloom", "loaded")
    )
    driver = fnloom.Builder().with_modules(module).build_async(sync_nodes=mode)  # ty: ignore[invalid-argument-type]
    run_batch(lambda: driver.execute(request("mixed_async"), inputs=_inputs()))
