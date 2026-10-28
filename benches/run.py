"""Times fnloom against Hamilton on generated graphs; prints one JSON object per row.

Run it as `uv run python -m benches.run`, or `uv run python -m benches.run extract`
to time only the shapes named. It prints to stdout on purpose: the rows are the
output, so redirect them to a file.
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
from benches.hooks import Counting

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
    input_checks: bool
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
    input_checks: bool


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
    module: ModuleType,
    outputs: Sequence[str],
    inputs: Mapping[str, object],
    *,
    input_checks: bool,
    variant: str = "",
) -> Runtime[fnloom.Driver]:
    """fnloom, its checks on or off, as runtime `fnloom (<variant>)` if given.

    `variant` `"deep"` or `"widen"` turns that rule on; `"hooked"` adds a hook
    overriding all four methods.
    """

    def build() -> fnloom.Driver:
        builder = fnloom.Builder().with_modules(module)
        if variant == "hooked":
            builder = builder.with_hooks(Counting())
        if not input_checks:
            builder = builder.without_input_type_checks()
        if variant == "deep":
            builder = builder.with_deep_type_checks()
        elif variant == "widen":
            builder = builder.with_numeric_widening()
        return builder.build()

    def steady_call(driver: fnloom.Driver) -> Callable[[], object]:
        prepared = driver.prepare(outputs, inputs=inputs)
        return lambda: prepared.run(inputs=inputs)

    return Runtime(
        f"fnloom ({variant})" if variant else "fnloom",
        build,
        lambda driver: driver.execute(outputs, inputs=inputs),
        steady_call,
        lambda driver: driver.prepare(outputs, inputs=inputs),
        input_checks=input_checks,
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
        input_checks=True,
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
        runtime.input_checks,
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
        checks = "on" if row.input_checks else "off"
        where = f"{row.shape} n={row.n}: {row.runtime} (checks {checks})"
        msg = f"{where} computed {result!r}, not {expected!r}"
        raise SystemExit(msg)


def measure_case(shape: Shape, n: int) -> list[Row]:
    """Times Hamilton and fnloom, checks on and off, on one graph.

    fnloom is timed too with a hook overriding all four methods, checks on, as
    runtime `fnloom (hooked)`.
    The `types` shape is timed too with deep checks and with numeric widening on,
    as runtimes `fnloom (deep)` and `fnloom (widen)`. Aborts if an answer is not
    the closed form.
    """
    outputs, inputs = make_request(shape, n)
    expected = make_expected(shape, n)
    rows: list[Row] = []
    with (
        generated(shape, n, "hamilton") as hamilton_module,
        generated(shape, n, "fnloom") as fnloom_module,
    ):
        runtimes = [
            _hamilton(hamilton_module, outputs, inputs),
            _fnloom(fnloom_module, outputs, inputs, input_checks=True),
            _fnloom(fnloom_module, outputs, inputs, input_checks=False),
            _fnloom(
                fnloom_module, outputs, inputs, input_checks=True, variant="hooked"
            ),
        ]
        if shape == "types":
            runtimes += [
                _fnloom(fnloom_module, outputs, inputs, input_checks=True, variant=v)
                for v in ("deep", "widen")
            ]
        for runtime in runtimes:
            row, result = _measure(runtime, shape, n)
            _check_answer(row, result, expected)
            rows.append(row)
    return rows


def main() -> None:
    """Prints the 104 rows as JSON lines, or those of the shapes named as arguments."""
    every = get_args(Shape.__value__)
    named = sys.argv[1:]
    if unknown := sorted(set(named) - set(every)):
        msg = f"unknown shapes: {', '.join(unknown)}; the shapes: {', '.join(every)}"
        raise SystemExit(msg)
    shapes = [shape for shape in every if not named or shape in named]
    for shape in shapes:
        for n in SIZES:
            for row in measure_case(shape, n):
                sys.stdout.write(json.dumps(asdict(row)) + "\n")
                sys.stdout.flush()


if __name__ == "__main__":
    main()
