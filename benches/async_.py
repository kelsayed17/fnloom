"""Times async runs of fnloom and Hamilton; prints one JSON object per row.

Run it as `uv run python -m benches.async_`. Every sample is a fresh process, so no
run inherits another's warm caches, interned strings or imported modules; a row is
the median over its processes. It prints to stdout on purpose: the rows are the
output, so redirect them to a file.

Each graph is timed with every node an `async def`, and with every node a plain
function. fnloom runs it in both `sync_nodes` modes; Hamilton through
`async_driver.Builder`, whose result is a dict.
"""

import asyncio
import json
import os
import re
import statistics
import subprocess
import sys
import time
import warnings
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast, get_args

# Set before anything imports Hamilton, so its import loads no heavy extensions.
os.environ["HAMILTON_AUTOLOAD_EXTENSIONS"] = "0"

from hamilton import async_driver

import fnloom
from benches.generate import (
    Shape,
    discard,
    make_expected,
    make_request,
    module_source,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
    from types import ModuleType

type Runtime = Literal["hamilton", "fnloom-thread", "fnloom-inline"]
type Nodes = Literal["async", "sync"]

CASES: tuple[tuple[Shape, int], ...] = (
    ("chain", 100),
    ("chain", 1000),
    ("layered", 100),
    ("layered", 1000),
)
# Fresh processes per row; the row is the median of theirs.
PROCESSES = 5
# Runs timed after the first in each process; the process gives their median.
RUNS = 21
NS_PER_MS = 1_000_000
ROOT = Path(__file__).resolve().parents[1]
# A generated node's definition, at the start of its line.
_DEF = re.compile(r"^def ", re.MULTILINE)


@dataclass(frozen=True)
class Sample:
    """One process's times, in milliseconds; `error` names what stopped it."""

    first_ms: float | None
    run_ms: float | None
    error: str | None


@dataclass(frozen=True)
class Row:
    """One measurement: the medians over `processes` samples, in milliseconds.

    `first_ms` is the first run on a new driver; `run_ms` the median of the
    `RUNS` after it.
    """

    shape: str
    n: int
    nodes: str
    runtime: str
    first_ms: float | None
    run_ms: float | None
    processes: int
    error: str | None


def _source(shape: Shape, n: int, nodes: Nodes) -> str:
    """The generated graph, every node made an `async def` when `nodes` says so."""
    source = module_source(shape, n)
    return _DEF.sub("async def ", source) if nodes == "async" else source


def _module(shape: Shape, n: int, nodes: Nodes) -> ModuleType:
    """The generated graph as a module registered in `sys.modules`."""
    name = f"bench_async_{shape}_{n}_{nodes}"
    module = type(sys)(name)
    sys.modules[name] = module
    exec(compile(_source(shape, n, nodes), name, "exec"), module.__dict__)  # noqa: S102  # a generated benchmark graph
    return module


async def _fnloom_runs(
    module: ModuleType,
    outputs: Sequence[str],
    inputs: Mapping[str, object],
    runtime: Runtime,
) -> Callable[[], Awaitable[Mapping[str, object]]]:
    """A prepared plan's `run` on a new driver; nothing timed."""
    mode = "thread" if runtime == "fnloom-thread" else "inline"
    driver = fnloom.Builder().with_modules(module).build_async(sync_nodes=mode)
    plan = driver.prepare(outputs, inputs=inputs)
    return lambda: plan.run(inputs=inputs)


async def _hamilton_runs(
    module: ModuleType,
    outputs: Sequence[str],
    inputs: Mapping[str, object],
    _runtime: Runtime,
) -> Callable[[], Awaitable[Mapping[str, object]]]:
    """`execute` on a new Hamilton async driver; nothing timed."""
    # Typed as the sync builder's `build`, which this one makes a coroutine.
    pending: object = async_driver.Builder().with_modules(module).build()
    driver = await cast("Coroutine[Any, Any, async_driver.AsyncDriver]", pending)
    # Copied once, outside every timed region, as fnloom's calls need no copy.
    names = [*outputs]
    given = dict(inputs)
    return lambda: cast(
        "Awaitable[Mapping[str, object]]", driver.execute(names, inputs=given)
    )


async def _timed(
    run: Callable[[], Awaitable[Mapping[str, object]]],
) -> tuple[Mapping[str, object], float]:
    start = time.perf_counter_ns()
    value = await run()
    return value, (time.perf_counter_ns() - start) / NS_PER_MS


async def _sample(shape: Shape, n: int, nodes: Nodes, runtime: Runtime) -> Sample:
    """Times a first run and `RUNS` after it, on this process's loop.

    Aborts if an answer is not the shape's closed form.
    """
    outputs, inputs = make_request(shape, n)
    expected = make_expected(shape, n)
    module = _module(shape, n, nodes)
    make_runs = _hamilton_runs if runtime == "hamilton" else _fnloom_runs
    try:
        run = await make_runs(module, outputs, inputs, runtime)
        result, first = await _timed(run)
        times: list[float] = []
        for _ in range(RUNS):
            result, elapsed = await _timed(run)
            times.append(elapsed)
    except Exception as error:  # noqa: BLE001  # the exception class is the recorded result
        return Sample(None, None, type(error).__name__)
    finally:
        discard(module)
    if dict(result) != expected:
        msg = f"{shape} n={n} {nodes}: {runtime} computed {result!r}, not {expected!r}"
        raise SystemExit(msg)
    return Sample(first, statistics.median(times), None)


def sample(shape: Shape, n: int, nodes: Nodes, runtime: Runtime) -> Sample:
    """`_sample` on a new loop, Hamilton's 3.14 deprecation warning silenced."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return asyncio.run(_sample(shape, n, nodes, runtime))


def _sample_in_process(shape: Shape, n: int, nodes: Nodes, runtime: Runtime) -> Sample:
    """Runs `sample` in a fresh interpreter and reads back its JSON line."""
    command = [sys.executable, "-m", "benches.async_", shape, str(n), nodes, runtime]
    done = subprocess.run(  # noqa: S603  # this interpreter, running this module
        command, cwd=ROOT, capture_output=True, text=True, check=False
    )
    if done.returncode != 0:
        sys.stderr.write(done.stderr)
        raise SystemExit(done.returncode)
    return Sample(**json.loads(done.stdout.splitlines()[-1]))


def _median(values: list[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return statistics.median(present) if present else None


def main() -> None:
    """Prints one row per case, kind of node and runtime as JSON lines."""
    configs = [
        (shape, n, nodes, runtime)
        for shape, n in CASES
        for nodes in get_args(Nodes.__value__)
        for runtime in get_args(Runtime.__value__)
    ]
    samples: dict[tuple[Shape, int, Nodes, Runtime], list[Sample]] = {
        config: [] for config in configs
    }
    # Round-robin, so a slow stretch of the machine touches every row alike.
    for _ in range(PROCESSES):
        for config in configs:
            samples[config].append(_sample_in_process(*config))
    for (shape, n, nodes, runtime), taken in samples.items():
        errors = {s.error for s in taken if s.error is not None}
        row = Row(
            shape,
            n,
            nodes,
            runtime,
            _median([s.first_ms for s in taken]),
            _median([s.run_ms for s in taken]),
            len(taken),
            ", ".join(sorted(errors)) or None,
        )
        sys.stdout.write(json.dumps(asdict(row)) + "\n")
        sys.stdout.flush()


def _sample_main(shape: str, n: str, nodes: str, runtime: str) -> None:
    """Prints `sample` for one case as a JSON line: a sampling process's whole job."""
    for given, allowed in (
        (shape, get_args(Shape.__value__)),
        (nodes, get_args(Nodes.__value__)),
        (runtime, get_args(Runtime.__value__)),
    ):
        if given not in allowed:
            msg = f"{given!r} is not one of {allowed}"
            raise SystemExit(msg)
    taken = sample(
        cast("Shape", shape),
        int(n),
        cast("Nodes", nodes),
        cast("Runtime", runtime),
    )
    sys.stdout.write(json.dumps(asdict(taken)) + "\n")


if __name__ == "__main__":
    if len(sys.argv) == 1:
        main()
    else:
        _sample_main(*sys.argv[1:])
