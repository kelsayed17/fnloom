"""Times building a graph from functions; prints one JSON object per row.

Each row times one way of building the same generated graph, at 1,000 nodes:

- `fnloom modules`: `Builder().with_modules(module).build()`, the baseline.
- `fnloom functions`: `Builder().with_functions(*functions).build()` (Q72).
- `hamilton temporary module`: `ad_hoc_utils.create_temporary_module(*functions)`
  and a build of it, Hamilton's nearest way to build from functions.

And `layered` at 1,000 nodes two ways (Q74):

- `fnloom modules`: one module of 1,000 nodes, the baseline.
- `fnloom namespaces`: ten modules of 100 nodes each, each under its own
  namespace, `ns0` to `ns9`, all reading the one input `seed`.

Run it as `uv run python -m benches.builds`. Each row is the median of `SAMPLES`
builds, in one process, the three ways interleaved so drift affects each alike. It
prints to stdout on purpose: the rows are the output, so redirect them to a file.
"""

import inspect
import itertools
import json
import os
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

# Set before anything imports Hamilton, so its import loads no heavy extensions.
os.environ["HAMILTON_AUTOLOAD_EXTENSIONS"] = "0"

from hamilton import ad_hoc_utils
from hamilton import driver as hamilton_driver

import fnloom
from benches.generate import (
    Shape,
    discard,
    generated,
    make_expected,
    make_module,
    make_request,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import ModuleType

SHAPES: tuple[Shape, ...] = ("chain", "rows")
N = 1000
NAMESPACES = 10
SAMPLES = 9
NS_PER_MS = 1_000_000
_names = itertools.count()


@dataclass(frozen=True)
class Row:
    """One way of building one graph: the median build time, in milliseconds."""

    shape: str
    n: int
    build: str
    build_ms: float


def _functions(module: ModuleType) -> list[Callable[..., object]]:
    """The functions `module` defines, in definition order."""
    return [
        value
        for value in vars(module).values()
        if inspect.isfunction(value) and value.__module__ == module.__name__
    ]


def _hamilton_build(functions: list[Callable[..., object]]) -> object:
    """Hamilton's build from functions: a temporary module, dropped afterwards."""
    name = f"bench_temporary_{next(_names)}"
    try:
        module = ad_hoc_utils.create_temporary_module(*functions, module_name=name)
        return hamilton_driver.Builder().with_modules(module).build()
    finally:
        sys.modules.pop(name, None)


def _timed_ms(call: Callable[[], object]) -> float:
    start = time.perf_counter_ns()
    call()
    return (time.perf_counter_ns() - start) / NS_PER_MS


def measure(shape: Shape, n: int = N) -> list[Row]:
    """Times the three builds of one graph; checks fnloom's answer from functions."""
    outputs, inputs = make_request(shape, n)
    with (
        generated(shape, n, "fnloom") as fnloom_module,
        generated(shape, n, "hamilton") as hamilton_module,
    ):
        fnloom_functions = _functions(fnloom_module)
        hamilton_functions = _functions(hamilton_module)
        builds: dict[str, Callable[[], object]] = {
            "fnloom modules": lambda: (
                fnloom.Builder().with_modules(fnloom_module).build()
            ),
            "fnloom functions": lambda: (
                fnloom.Builder().with_functions(*fnloom_functions).build()
            ),
            "hamilton temporary module": lambda: _hamilton_build(hamilton_functions),
        }
        driver = fnloom.Builder().with_functions(*fnloom_functions).build()
        if driver.execute(outputs, inputs=inputs) != make_expected(shape, n):
            msg = f"{shape} n={n}: with_functions computed a wrong answer"
            raise SystemExit(msg)
        medians = _sampled(builds)
    return [Row(shape, n, name, median) for name, median in medians.items()]


def _sampled(builds: dict[str, Callable[[], object]]) -> dict[str, float]:
    """The median time of each build, warmed once, the builds interleaved."""
    samples: dict[str, list[float]] = {name: [] for name in builds}
    for build in builds.values():
        build()
    for _ in range(SAMPLES):
        for name, build in builds.items():
            samples[name].append(_timed_ms(build))
    return {name: statistics.median(times) for name, times in samples.items()}


def measure_namespaces(n: int = N, count: int = NAMESPACES) -> list[Row]:
    """Times `layered` at `n` as one module, and split over `count` namespaces."""
    part = n // count
    _, inputs = make_request("layered", part)
    parts = [make_module("layered", part) for _ in range(count)]
    try:
        with generated("layered", n) as whole:
            namespaced = fnloom.Builder()
            for index, module in enumerate(parts):
                namespaced = namespaced.with_modules(module, namespace=f"ns{index}")
            outputs, _ = make_request("layered", part)
            expected = make_expected("layered", part)
            driver = namespaced.build()
            wanted = [f"ns{i}.{name}" for i in range(count) for name in outputs]
            if driver.execute(wanted, inputs=inputs) != {
                f"ns{i}.{name}": value
                for i in range(count)
                for name, value in expected.items()
            }:
                msg = f"layered n={n}: namespaces computed a wrong answer"
                raise SystemExit(msg)
            medians = _sampled(
                {
                    "fnloom modules": lambda: (
                        fnloom.Builder().with_modules(whole).build()
                    ),
                    "fnloom namespaces": namespaced.build,
                }
            )
    finally:
        for module in parts:
            discard(module)
    return [Row("layered", n, name, median) for name, median in medians.items()]


def main() -> None:
    """Prints one row per shape and way of building."""
    for shape in SHAPES:
        for row in measure(shape):
            sys.stdout.write(json.dumps(asdict(row)) + "\n")
            sys.stdout.flush()
    for row in measure_namespaces():
        sys.stdout.write(json.dumps(asdict(row)) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
