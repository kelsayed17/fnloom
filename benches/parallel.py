"""Times fan-outs run in parallel by fnloom and by Hamilton; prints one JSON row each.

Run it as `uv run python -m benches.parallel`, once on each build (3.14 and 3.14t).
Every sample is a fresh process, which builds its driver, runs once to warm it, then
times its steady runs; a row is the median over its processes. It prints to stdout on
purpose: the rows are the output, so redirect them to a file.

Each row names its `shape`, `branches`, `runtime`, `workers`, `build` and `cores`
(`os.process_cpu_count()`), with `ms`, a steady run's median, and `per_branch_us`:

- `trivial`, `sleeping` and `cpu`: the `fan_out` graph, its `branch` doing nothing
  more, sleeping 10 ms, or about 1 ms of pure Python (`generate.fan_out_source`).
- `cached_warm`: a cached fan-out over the items a run before it stored;
  `cached_appended`: over one item more, on a new driver and store each run.
- `layered`: the 1,000-node `layered` graph, which has no blocks; `per_branch_us`
  is then per node.

The runtimes are `fnloom` (no `with_threads`), `fnloom-threads`
(`with_threads(workers)`), `hamilton-sync` (`SynchronousLocalTaskExecutor` as both
executors) and `hamilton-threads` (`MultiThreadingExecutor(workers)` as the remote
one). Every answer is checked against the shape's closed form.
"""

import json
import os
import statistics
import subprocess
import sys
import sysconfig
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast, get_args

# Set before anything imports Hamilton, so its import loads no heavy extensions.
os.environ["HAMILTON_AUTOLOAD_EXTENSIONS"] = "0"

from hamilton import driver as hamilton_driver
from hamilton.execution import executors

import fnloom
from benches.generate import (
    Body,
    fan_out_source,
    file_module,
    make_expected,
    make_request,
    module_source,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from types import ModuleType

type Shape = Literal[
    "trivial", "sleeping", "cpu", "cached_warm", "cached_appended", "layered"
]
type Runtime = Literal["fnloom", "fnloom-threads", "hamilton-sync", "hamilton-threads"]
type Case = tuple[Shape, int, Runtime, int | None]
# One steady run: its answer, and the nanoseconds to count.
type Run = Callable[[], tuple[Mapping[str, object], int]]

WORKERS = 8
_TRIVIAL: tuple[tuple[Runtime, int | None], ...] = (
    ("fnloom", None),
    ("fnloom-threads", WORKERS),
    ("hamilton-sync", None),
    ("hamilton-threads", WORKERS),
)
_SPEEDUP: tuple[tuple[Runtime, int | None], ...] = (
    ("fnloom", None),
    ("fnloom-threads", 1),
    ("fnloom-threads", WORKERS),
    ("hamilton-threads", WORKERS),
)
CASES: tuple[Case, ...] = (
    *(
        ("trivial", n, *runtime)
        for n in (1, 100, 1_000, 10_000)
        for runtime in _TRIVIAL
    ),
    *(("sleeping", 100, *runtime) for runtime in _SPEEDUP),
    *(("cpu", 100, *runtime) for runtime in _SPEEDUP),
    *(("cached_warm", 1_000, "fnloom-threads", w) for w in (1, WORKERS)),
    *(("cached_appended", 1_000, "fnloom-threads", w) for w in (1, WORKERS)),
    ("layered", 1_000, "fnloom", None),
    ("layered", 1_000, "fnloom-threads", 1),
)
# Fresh processes per row; a row whose first sample took over `SLOW_MS` keeps it alone.
PROCESSES = 3
SLOW_MS = 5_000.0
# A process times steady runs for about this long: at least one, at most `MOST_RUNS`.
BUDGET_MS = 1_500.0
MOST_RUNS = 21
NS_PER_MS = 1_000_000
ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Sample:
    """One process's steady run, in milliseconds; `error` names what stopped it."""

    ms: float | None
    error: str | None


@dataclass(frozen=True)
class Row:
    """One measurement: the median over `processes` samples."""

    shape: str
    branches: int
    runtime: str
    workers: int | None
    build: str
    cores: int | None
    ms: float | None
    per_branch_us: float | None
    processes: int
    error: str | None


def _source(shape: Shape, runtime: Runtime, n: int) -> str:
    """The graph's source, in the runtime's spelling."""
    if shape == "layered":
        return module_source("layered", n)
    spelling = "hamilton" if runtime.startswith("hamilton") else "fnloom"
    body: Body = shape if shape in {"sleeping", "cpu"} else "trivial"
    return fan_out_source(spelling, body)


def _fnloom(
    module: ModuleType, workers: int | None, *, cached: bool = False
) -> fnloom.Driver:
    builder = fnloom.Builder().with_modules(module)
    if workers is not None:
        builder = builder.with_threads(workers)
    if cached:
        builder = builder.with_cache(fnloom.MemoryCache())
    return builder.build()


def _timing(call: Callable[[], Mapping[str, object]]) -> Run:
    """`call`, timed whole."""

    def run() -> tuple[Mapping[str, object], int]:
        start = time.perf_counter_ns()
        result = call()
        return result, time.perf_counter_ns() - start

    return run


def _hamilton(module: ModuleType, runtime: Runtime, workers: int | None, n: int) -> Run:
    remote = (
        executors.SynchronousLocalTaskExecutor()
        if runtime == "hamilton-sync"
        else executors.MultiThreadingExecutor(max_tasks=workers or WORKERS)
    )
    built = (
        hamilton_driver.Builder()
        .with_modules(module)
        .enable_dynamic_execution(allow_experimental_mode=True)
        .with_local_executor(executors.SynchronousLocalTaskExecutor())
        .with_remote_executor(remote)
        .build()
    )
    names: list[str | Callable[..., object] | hamilton_driver.Variable] = ["total"]
    inputs = {"count": n}
    return _timing(
        lambda: cast("Mapping[str, object]", built.execute(names, inputs=inputs))
    )


def _appended(module: ModuleType, workers: int | None, n: int) -> Run:
    """A run over `n + 1` items on a new driver and store holding `n`: only it timed."""

    def run() -> tuple[Mapping[str, object], int]:
        plan = _fnloom(module, workers, cached=True).prepare(
            ["total"], inputs={"count": 0}
        )
        plan.run(inputs={"count": n})
        start = time.perf_counter_ns()
        result = plan.run(inputs={"count": n + 1})
        return result, time.perf_counter_ns() - start

    return run


def _runs(module: ModuleType, case: Case) -> Run:
    """The row's steady run: nothing timed, nothing run yet."""
    shape, n, runtime, workers = case
    if runtime.startswith("hamilton"):
        return _hamilton(module, runtime, workers, n)
    if shape == "cached_appended":
        return _appended(module, workers, n)
    if shape == "layered":
        outputs, inputs = make_request("layered", n)
    else:
        outputs, inputs = ["total"], {"count": n}
    plan = _fnloom(module, workers, cached=shape == "cached_warm").prepare(
        outputs, inputs=inputs
    )
    return _timing(lambda: plan.run(inputs=inputs))


def _expected(shape: Shape, n: int) -> dict[str, object]:
    if shape == "layered":
        return make_expected("layered", n)
    return make_expected("fan_out", n + 1 if shape == "cached_appended" else n)


def sample(case: Case) -> Sample:
    """A warm run, then the median of the steady runs after it.

    Aborts on a wrong answer.
    """
    shape, n, runtime, _ = case
    expected = _expected(shape, n)
    with file_module(_source(shape, runtime, n)) as module:
        try:
            run = _runs(module, case)
            result, first = run()
            # A slow first run is the sample: Hamilton plans each run anew anyway.
            if first / NS_PER_MS > SLOW_MS:
                times = [first]
            else:
                count = max(1, min(MOST_RUNS, int(BUDGET_MS * NS_PER_MS / first)))
                times = []
                for _ in range(count):
                    result, elapsed = run()
                    times.append(elapsed)
        except Exception as error:  # noqa: BLE001  # the exception class is the recorded result
            return Sample(None, type(error).__name__)
    if dict(result) != expected:
        msg = f"{case}: computed {result!r}, not {expected!r}"
        raise SystemExit(msg)
    return Sample(statistics.median(times) / NS_PER_MS, None)


def _sample_in_process(case: Case) -> Sample:
    """Runs `sample` in a fresh interpreter and reads back its JSON line."""
    shape, n, runtime, workers = case
    command = [sys.executable, "-m", "benches.parallel", shape, str(n), runtime]
    command += [] if workers is None else [str(workers)]
    done = subprocess.run(  # noqa: S603  # this interpreter, running this module
        command, cwd=ROOT, capture_output=True, text=True, check=False
    )
    if done.returncode != 0:
        sys.stderr.write(done.stderr)
        raise SystemExit(done.returncode)
    return Sample(**json.loads(done.stdout.splitlines()[-1]))


def build() -> str:
    """`3.14t` on a free-threaded build, else `3.14`."""
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    return f"{version}t" if sysconfig.get_config_var("Py_GIL_DISABLED") else version


def main() -> None:
    """Prints one row per case as JSON lines."""
    samples: dict[Case, list[Sample]] = {case: [] for case in CASES}
    # Round-robin, so a slow stretch of the machine touches every row alike.
    for round_ in range(PROCESSES):
        for case in CASES:
            taken = samples[case]
            slow = any(s.ms is not None and s.ms > SLOW_MS for s in taken)
            if round_ == 0 or not slow:
                taken.append(_sample_in_process(case))
    for (shape, n, runtime, workers), taken in samples.items():
        times = [s.ms for s in taken if s.ms is not None]
        ms = statistics.median(times) if times else None
        errors = sorted({s.error for s in taken if s.error is not None})
        row = Row(
            shape,
            n,
            runtime,
            workers,
            build(),
            os.process_cpu_count(),
            ms,
            None if ms is None else ms * 1000 / n,
            len(taken),
            ", ".join(errors) or None,
        )
        sys.stdout.write(json.dumps(asdict(row)) + "\n")
        sys.stdout.flush()


def _sample_main(shape: str, n: str, runtime: str, workers: str | None = None) -> None:
    """Prints `sample` for one case as a JSON line: a sampling process's whole job."""
    for given, allowed in (
        (shape, get_args(Shape.__value__)),
        (runtime, get_args(Runtime.__value__)),
    ):
        if given not in allowed:
            msg = f"{given!r} is not one of {allowed}"
            raise SystemExit(msg)
    case: Case = (
        cast("Shape", shape),
        int(n),
        cast("Runtime", runtime),
        None if workers is None else int(workers),
    )
    sys.stdout.write(json.dumps(asdict(sample(case))) + "\n")


if __name__ == "__main__":
    if len(sys.argv) == 1:
        main()
    else:
        _sample_main(*sys.argv[1:])
