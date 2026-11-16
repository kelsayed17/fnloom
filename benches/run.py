"""Times fnloom against Hamilton on generated graphs; prints one JSON object per row.

Run it as `uv run python -m benches.run`, or `uv run python -m benches.run extract`
to time only the shapes named. Beside the shapes are two block cases, also named
this way: `fan_out_cached` and `absent_chain`, which print `CaseRow`s. It prints to
stdout on purpose: the rows are the output, so redirect them to a file.
"""

import json
import os
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, cast, get_args

# Set before anything imports Hamilton, so its import loads no heavy extensions.
os.environ["HAMILTON_AUTOLOAD_EXTENSIONS"] = "0"

from hamilton import driver as hamilton_driver
from hamilton.caching.stores.memory import InMemoryMetadataStore, InMemoryResultStore
from hamilton.execution import executors

import fnloom
from benches.generate import (
    Shape,
    absent_chain_source,
    file_module,
    generated,
    make_expected,
    make_request,
    module_source,
)
from benches.hooks import Counting

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from types import ModuleType

SIZES = (10, 100, 1000, 10000)
# Branches, not nodes: the `fan_out` graph is three nodes at every size.
FAN_OUT_SIZES = (1, 100, 1000, 10000)
STEADY_RUNS = 7
NS_PER_MS = 1_000_000
#: The block cases, named like shapes.
CASES = ("fan_out_cached", "absent_chain")
CACHED_BRANCHES = 1000
CHAIN_NODES = 200
# A cached Hamilton fan-out of 1,000 branches takes seconds a run, so fewer samples.
HAMILTON_CACHED_RUNS = 3
# Runs per timed batch of `absent_chain`, whose runs take microseconds.
BATCH = 100


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
class CaseRow:
    """One block case's run: the median of its samples, in milliseconds per run.

    `case` is `fan_out_warm` (every branch already stored), `fan_out_appended` (one
    item appended to `n` stored ones) or `absent_chain` (a chain of `n` nodes run
    plain, through nodes that may be absent, or with its first node declining).
    """

    case: str
    n: int
    runtime: str
    ms: float


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


def _hamilton_builder(module: ModuleType, *, blocks: bool) -> hamilton_driver.Builder:
    """Hamilton's builder; with `blocks`, its dynamic execution, synchronously."""
    builder = hamilton_driver.Builder().with_modules(module)
    if blocks:
        builder = builder.enable_dynamic_execution(
            allow_experimental_mode=True
        ).with_local_executor(executors.SynchronousLocalTaskExecutor())
    return builder


def _hamilton(
    module: ModuleType,
    outputs: Sequence[str],
    inputs: Mapping[str, object],
    *,
    blocks: bool = False,
) -> Runtime[hamilton_driver.Driver]:
    # Copied once, outside every timed region, as fnloom's calls need no copy.
    names: list[str | Callable[..., object] | hamilton_driver.Variable] = [*outputs]
    given = dict(inputs)
    return Runtime(
        "hamilton",
        lambda: _hamilton_builder(module, blocks=blocks).build(),
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
            _hamilton(hamilton_module, outputs, inputs, blocks=shape == "fan_out"),
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


type _FanOut = Callable[[int], Mapping[str, object]]


def _fnloom_cached(module: ModuleType) -> _FanOut:
    """A fnloom plan over `module` with an empty `MemoryCache`, run by item count."""
    builder = fnloom.Builder().with_modules(module).with_cache(fnloom.MemoryCache())
    prepared = builder.build().prepare(["total"], inputs={"count": 0})
    return lambda count: prepared.run(inputs={"count": count})


def _hamilton_cached(module: ModuleType) -> _FanOut:
    """A Hamilton driver over `module` with empty in-memory stores, by item count."""
    driver = (
        _hamilton_builder(module, blocks=True)
        .with_cache(
            metadata_store=InMemoryMetadataStore(), result_store=InMemoryResultStore()
        )
        .build()
    )
    names: list[str | Callable[..., object] | hamilton_driver.Variable] = ["total"]
    return lambda count: cast(
        "Mapping[str, object]", driver.execute(names, inputs={"count": count})
    )


def _timed_fan_out(run: _FanOut, count: int, where: str) -> float:
    """Times one run over `count` items, aborting unless it gives the closed form."""
    result, elapsed = _timed(lambda: run(count))
    if dict(result) != (expected := make_expected("fan_out", count)):
        msg = f"{where} computed {result!r}, not {expected!r}"
        raise SystemExit(msg)
    return elapsed


def cached_fan_out() -> list[CaseRow]:
    """Times a cached fan-out of `CACHED_BRANCHES` branches, warm and one appended.

    A warm run follows a run over the same items. An appended run follows a run over
    one item fewer, on a new driver and store each sample, so only the new branch,
    the expand and the collect miss. Every answer is checked against the closed form.
    """
    n = CACHED_BRANCHES
    rows: list[CaseRow] = []
    for runtime, new_run, samples in (
        ("fnloom", _fnloom_cached, STEADY_RUNS),
        ("hamilton", _hamilton_cached, HAMILTON_CACHED_RUNS),
    ):
        where = f"fan_out_cached n={n}: {runtime}"
        spelling = "hamilton" if runtime == "hamilton" else "fnloom"
        with file_module(module_source("fan_out", n, spelling)) as module:
            run = new_run(module)
            _timed_fan_out(run, n, where)
            warm = [_timed_fan_out(run, n, where) for _ in range(samples)]
            appended: list[float] = []
            for _ in range(samples):
                fresh = new_run(module)
                _timed_fan_out(fresh, n, where)
                appended.append(_timed_fan_out(fresh, n + 1, where))
        rows += [
            CaseRow("fan_out_warm", n, runtime, statistics.median(warm)),
            CaseRow("fan_out_appended", n, runtime, statistics.median(appended)),
        ]
    return rows


def _batch(prepared: fnloom.PreparedPlan, inputs: Mapping[str, object]) -> None:
    for _ in range(BATCH):
        prepared.run(inputs=inputs)


def absent_chain() -> list[CaseRow]:
    """Times a prepared run of a `CHAIN_NODES`-node chain three ways, fnloom only.

    `fnloom (plain)` is the `chain` shape, with no node that may be absent; `fnloom
    (present)` the same chain after a first node that may decline and doesn't; and
    `fnloom (declines)` that chain when it does, skipping every other node. Each is
    the median of `STEADY_RUNS` batches of `BATCH` runs, per run.
    """
    n = CHAIN_NODES
    outputs, inputs = make_request("chain", n)
    present = make_expected("chain", n)
    declining = absent_chain_source(n)
    cases: tuple[tuple[str, str, dict[str, object], dict[str, object]], ...] = (
        ("fnloom (plain)", module_source("chain", n), inputs, present),
        ("fnloom (present)", declining, {**inputs, "skip": False}, present),
        (
            "fnloom (declines)",
            declining,
            {**inputs, "skip": True},
            {outputs[0]: fnloom.ABSENT},
        ),
    )
    rows: list[CaseRow] = []
    for runtime, source, given, answer in cases:
        with file_module(source) as module:
            prepared = fnloom.build(module).prepare(outputs, inputs=given)
            if dict(result := prepared.run(inputs=given)) != answer:
                msg = f"absent_chain n={n}: {runtime} computed {result!r}"
                raise SystemExit(msg)
            ms = _median_ms(lambda: _batch(prepared, given)) / BATCH  # noqa: B023  # called before the loop moves on
            rows.append(CaseRow("absent_chain", n, runtime, ms))
    return rows


def main() -> None:
    """Prints the 120 rows and the 7 case rows as JSON lines, or those named."""
    every = get_args(Shape.__value__)
    named = sys.argv[1:]
    if unknown := sorted(set(named) - set(every) - set(CASES)):
        known = ", ".join([*every, *CASES])
        msg = f"unknown names: {', '.join(unknown)}; the shapes and cases: {known}"
        raise SystemExit(msg)
    shapes = [shape for shape in every if not named or shape in named]
    for shape in shapes:
        for n in FAN_OUT_SIZES if shape == "fan_out" else SIZES:
            for row in measure_case(shape, n):
                sys.stdout.write(json.dumps(asdict(row)) + "\n")
                sys.stdout.flush()
    cases = {"fan_out_cached": cached_fan_out, "absent_chain": absent_chain}
    for name, measure in cases.items():
        if not named or name in named:
            for case_row in measure():
                sys.stdout.write(json.dumps(asdict(case_row)) + "\n")
                sys.stdout.flush()


if __name__ == "__main__":
    main()
