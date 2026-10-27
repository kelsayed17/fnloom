"""Times fnloom against Hamilton on generated graphs; prints one JSON object per row.

Run it as `uv run python -m benches.run`. It prints to stdout on purpose: the rows
are the output, so redirect them to a file.
"""

import json
import os
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, get_args

# Set before anything imports Hamilton, so its import loads no heavy extensions.
os.environ["HAMILTON_AUTOLOAD_EXTENSIONS"] = "0"

from hamilton import driver as hamilton_driver

import fnloom
from benches.generate import Shape, generated, make_expected, make_request

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from types import ModuleType

SIZES = (10, 100, 1000, 10000)
STEADY_RUNS = 7
NS_PER_MS = 1_000_000


@dataclass(frozen=True)
class Row:
    """One measurement, timings in milliseconds.

    A timing is None when its phase did not finish, and `prepare_ms` is always None
    for Hamilton, which has no prepared plans. `error` is the exception class that
    stopped the run and `error_phase` the phase that raised: `build`, `first_execute`,
    `steady_execute` or `prepare`. Phases that finished keep their timings.
    """

    shape: str
    n: int
    runtime: str
    build_ms: float | None
    first_execute_ms: float | None
    steady_execute_ms: float | None
    prepare_ms: float | None
    error: str | None
    error_phase: str | None


@dataclass(frozen=True)
class Runtime[D]:
    """How to drive one runtime: build, run once, make the steady call, prepare.

    `prepare` is None for a runtime without prepared plans.
    """

    name: str
    build: Callable[[], D]
    first_execute: Callable[[D], Mapping[str, object]]
    steady_call: Callable[[D], Callable[[], object]]
    prepare: Callable[[D], object] | None


def _timed[T](call: Callable[[], T]) -> tuple[T, float]:
    start = time.perf_counter_ns()
    value = call()
    return value, (time.perf_counter_ns() - start) / NS_PER_MS


def _median_ms(call: Callable[[], object]) -> float:
    call()
    return statistics.median(_timed(call)[1] for _ in range(STEADY_RUNS))


def _cold_prepare_ms[D](
    build: Callable[[], D], prepare: Callable[[D], object]
) -> float:
    """Times `prepare` on a newly built driver, so no plan is cached yet."""
    fresh = build()
    return _timed(lambda: prepare(fresh))[1]


def _fnloom(
    module: ModuleType, outputs: Sequence[str], inputs: Mapping[str, object]
) -> Runtime[fnloom.Driver]:

    def steady_call(driver: fnloom.Driver) -> Callable[[], object]:
        prepared = driver.prepare(outputs, inputs=inputs)
        return lambda: prepared.run(inputs=inputs)

    return Runtime(
        "fnloom",
        lambda: fnloom.build(module),
        lambda driver: driver.execute(outputs, inputs=inputs),
        steady_call,
        lambda driver: driver.prepare(outputs, inputs=inputs),
    )


def _hamilton(
    module: ModuleType, outputs: Sequence[str], inputs: Mapping[str, object]
) -> Runtime[hamilton_driver.Driver]:
    # Copied once, outside every timed region, as fnloom's calls need no copy.
    names: list[str | Callable[..., object] | hamilton_driver.Variable] = [*outputs]
    given = dict(inputs)
    return Runtime(
        "hamilton",
        lambda: hamilton_driver.Builder().with_modules(module).build(),
        lambda driver: driver.execute(names, inputs=given),
        lambda driver: lambda: driver.execute(names, inputs=given),
        None,
    )


def _measure[D](
    runtime: Runtime[D], shape: str, n: int
) -> tuple[Row, Mapping[str, object] | None]:
    timings: dict[str, float] = {}
    result: Mapping[str, object] | None = None
    phase = "build"
    try:
        driver, timings["build"] = _timed(runtime.build)
        phase = "first_execute"
        result, timings["first_execute"] = _timed(lambda: runtime.first_execute(driver))
        phase = "steady_execute"
        timings["steady_execute"] = _median_ms(runtime.steady_call(driver))
        phase = "prepare"
        if runtime.prepare is not None:
            timings["prepare"] = _cold_prepare_ms(runtime.build, runtime.prepare)
    except Exception as error:  # noqa: BLE001  # the exception class is the recorded result
        failure = (type(error).__name__, phase)
    else:
        failure = (None, None)
    row = Row(
        shape,
        n,
        runtime.name,
        timings.get("build"),
        timings.get("first_execute"),
        timings.get("steady_execute"),
        timings.get("prepare"),
        *failure,
    )
    return row, result


def _check_answer(
    row: Row, result: Mapping[str, object] | None, expected: object
) -> None:
    if result is not None and dict(result) != expected:
        where = f"{row.shape} n={row.n}: {row.runtime}"
        msg = f"{where} computed {result!r}, not {expected!r}"
        raise SystemExit(msg)


def measure_case(shape: Shape, n: int) -> list[Row]:
    """Times both runtimes on one graph; aborts if an answer is not the closed form."""
    outputs, inputs = make_request(shape, n)
    expected = make_expected(shape, n)
    rows: list[Row] = []
    with generated(shape, n) as module:
        for runtime in (
            _hamilton(module, outputs, inputs),
            _fnloom(module, outputs, inputs),
        ):
            row, result = _measure(runtime, shape, n)
            _check_answer(row, result, expected)
            rows.append(row)
    return rows


def main() -> None:
    """Prints the 24 rows as JSON lines."""
    for shape in get_args(Shape.__value__):
        for n in SIZES:
            for row in measure_case(shape, n):
                sys.stdout.write(json.dumps(asdict(row)) + "\n")
                sys.stdout.flush()


if __name__ == "__main__":
    main()
