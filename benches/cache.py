"""Times cached runs of fnloom and Hamilton; prints one JSON object per row.

Run it as `uv run python -m benches.cache`. Every sample is a fresh process, so no
run inherits another's warm caches, interned strings or imported modules; a row is
the median over its processes. It prints to stdout on purpose: the rows are the
output, so redirect them to a file.
"""

import importlib.util
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast, get_args

# Set before anything imports Hamilton, so its import loads no heavy extensions.
os.environ["HAMILTON_AUTOLOAD_EXTENSIONS"] = "0"

from hamilton import driver as hamilton_driver
from hamilton.caching.stores.memory import InMemoryMetadataStore, InMemoryResultStore

import fnloom
from benches.generate import Shape, make_expected, make_request, module_source

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Mapping, Sequence
    from types import ModuleType

type Runtime = Literal["fnloom", "hamilton"]
type Store = Literal["none", "memory", "disk"]

CASES: tuple[tuple[Shape, int], ...] = (("chain", 200), ("layered", 1000))
STORES: tuple[Store, ...] = ("none", "memory", "disk")
# Fresh processes per row; the row is the median of theirs.
PROCESSES = 7
# Fresh drivers, each with an empty store, per process: each gives one cold run.
REPEATS = 3
# Warm runs timed after each cold run.
WARM_RUNS = 7
NS_PER_MS = 1_000_000
ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Sample:
    """One process's medians, in milliseconds, and how many entries a warm run loads.

    `warm_loads` is counted for fnloom with a store only, else None. `error` is the
    exception class that stopped the process, with the timings None.
    """

    cold_ms: float | None
    warm_ms: float | None
    warm_loads: int | None
    error: str | None


@dataclass(frozen=True)
class Row:
    """One measurement: the medians over `processes` samples, in milliseconds.

    `store` is `none` for a run without a cache. `cold_ms` is a first run with an
    empty store (or the first run, uncached); `warm_ms` a run after it, every result
    already stored (or a steady run, uncached).
    """

    shape: str
    n: int
    runtime: str
    store: str
    cold_ms: float | None
    warm_ms: float | None
    warm_loads: int | None
    processes: int
    error: str | None


def _timed[T](call: Callable[[], T]) -> tuple[T, float]:
    start = time.perf_counter_ns()
    value = call()
    return value, (time.perf_counter_ns() - start) / NS_PER_MS


@contextmanager
def _file_module(shape: Shape, n: int, runtime: Runtime) -> Generator[ModuleType]:
    """Yields the generated graph, imported from a file and dropped from `sys.modules`.

    From a file, not `exec`: Hamilton's cache reads each function's source, and
    without it versions no node.
    """
    name = f"bench_cache_{shape}_{n}"
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / f"{name}.py"
        path.write_text(module_source(shape, n, runtime), encoding="utf-8")
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            msg = f"cannot import {path}"
            raise SystemExit(msg)
        module = importlib.util.module_from_spec(spec)
        # Hamilton finds a function's module through `sys.modules`.
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
            yield module
        finally:
            sys.modules.pop(name, None)


class _Counting:
    """A store that counts the `get` calls that find an entry, around another one."""

    def __init__(self, store: fnloom.CacheStore) -> None:
        self.store = store
        self.loads = 0

    def get(self, key: bytes) -> object:
        value = self.store.get(key)
        if value is not fnloom.MISSING:
            self.loads += 1
        return value

    def put(self, key: bytes, value: object) -> None:
        self.store.put(key, value)

    def discard(self, key: bytes) -> None:
        self.store.discard(key)


def _fnloom_store(store: Store, directory: Path) -> fnloom.CacheStore | None:
    if store == "memory":
        return fnloom.MemoryCache()
    if store == "disk":
        return fnloom.DiskCache(directory)
    return None


def _fnloom_runs(
    module: ModuleType,
    outputs: Sequence[str],
    inputs: Mapping[str, object],
    store: Store,
    directory: Path,
) -> Callable[[], Mapping[str, object]]:
    """A prepared plan's run over a new driver with an empty store; nothing timed."""
    builder = fnloom.Builder().with_modules(module)
    cache = _fnloom_store(store, directory)
    if cache is not None:
        builder = builder.with_cache(cache)
    prepared = builder.build().prepare(outputs, inputs=inputs)
    return lambda: prepared.run(inputs=inputs)


def _hamilton_runs(
    module: ModuleType,
    outputs: Sequence[str],
    inputs: Mapping[str, object],
    store: Store,
    directory: Path,
) -> Callable[[], Mapping[str, object]]:
    """`execute` on a new Hamilton driver with an empty cache; nothing timed."""
    builder = hamilton_driver.Builder().with_modules(module)
    if store == "memory":
        builder = builder.with_cache(
            metadata_store=InMemoryMetadataStore(),
            result_store=InMemoryResultStore(),
        )
    elif store == "disk":
        builder = builder.with_cache(path=directory)
    driver = builder.build()
    # Copied once, outside every timed region, as fnloom's calls need no copy.
    names: list[str | Callable[..., object] | hamilton_driver.Variable] = [*outputs]
    given = dict(inputs)
    return lambda: cast("Mapping[str, object]", driver.execute(names, inputs=given))


def _warm_loads(
    module: ModuleType,
    outputs: Sequence[str],
    inputs: Mapping[str, object],
    store: Store,
    directory: Path,
) -> int | None:
    """How many entries a warm fnloom run loads; untimed, on its own driver."""
    cache = _fnloom_store(store, directory)
    if cache is None:
        return None
    counting = _Counting(cache)
    driver = fnloom.Builder().with_modules(module).with_cache(counting).build()
    driver.execute(outputs, inputs=inputs)
    counting.loads = 0
    driver.execute(outputs, inputs=inputs)
    return counting.loads


def _check_answer(
    result: Mapping[str, object], expected: Mapping[str, object], where: str
) -> None:
    """Aborts the run when `result` is not the closed form."""
    if dict(result) != expected:
        msg = f"{where} computed {result!r}, not {expected!r}"
        raise SystemExit(msg)


def sample(shape: Shape, n: int, runtime: Runtime, store: Store) -> Sample:
    """Times `REPEATS` cold runs and the warm runs after each, in this process.

    Aborts if an answer is not the shape's closed form.
    """
    outputs, inputs = make_request(shape, n)
    expected = make_expected(shape, n)
    make_runs = _fnloom_runs if runtime == "fnloom" else _hamilton_runs
    cold: list[float] = []
    warm: list[float] = []
    with _file_module(shape, n, runtime) as module:
        try:
            for _ in range(REPEATS):
                with tempfile.TemporaryDirectory() as directory:
                    run = make_runs(module, outputs, inputs, store, Path(directory))
                    result, elapsed = _timed(run)
                    cold.append(elapsed)
                    for _ in range(WARM_RUNS):
                        result, elapsed = _timed(run)
                        warm.append(elapsed)
                    _check_answer(
                        result, expected, f"{shape} n={n}: {runtime} ({store})"
                    )
            loads = None
            if runtime == "fnloom":
                with tempfile.TemporaryDirectory() as directory:
                    loads = _warm_loads(module, outputs, inputs, store, Path(directory))
        except Exception as error:  # noqa: BLE001  # the exception class is the recorded result
            return Sample(None, None, None, type(error).__name__)
    return Sample(statistics.median(cold), statistics.median(warm), loads, None)


def _sample_in_process(shape: Shape, n: int, runtime: Runtime, store: Store) -> Sample:
    """Runs `sample` in a fresh interpreter and reads back its JSON line."""
    command = [sys.executable, "-m", "benches.cache", shape, str(n), runtime, store]
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
    """Prints one row per case, runtime and store as JSON lines."""
    configs = [
        (shape, n, runtime, store)
        for shape, n in CASES
        for runtime in get_args(Runtime.__value__)
        for store in STORES
    ]
    samples: dict[tuple[Shape, int, Runtime, Store], list[Sample]] = {
        config: [] for config in configs
    }
    # Round-robin, so a slow stretch of the machine touches every row alike.
    for _ in range(PROCESSES):
        for config in configs:
            samples[config].append(_sample_in_process(*config))
    for (shape, n, runtime, store), taken in samples.items():
        errors = {s.error for s in taken if s.error is not None}
        loads = {s.warm_loads for s in taken}
        row = Row(
            shape,
            n,
            runtime,
            store,
            _median([s.cold_ms for s in taken]),
            _median([s.warm_ms for s in taken]),
            loads.pop() if len(loads) == 1 else None,
            len(taken),
            ", ".join(sorted(errors)) or None,
        )
        sys.stdout.write(json.dumps(asdict(row)) + "\n")
        sys.stdout.flush()


def _sample_main(shape: str, n: str, runtime: str, store: str) -> None:
    """Prints `sample` for one case as a JSON line: a sampling process's whole job."""
    for given, allowed in (
        (shape, get_args(Shape.__value__)),
        (runtime, get_args(Runtime.__value__)),
        (store, STORES),
    ):
        if given not in allowed:
            msg = f"{given!r} is not one of {allowed}"
            raise SystemExit(msg)
    taken = sample(
        cast("Shape", shape), int(n), cast("Runtime", runtime), cast("Store", store)
    )
    sys.stdout.write(json.dumps(asdict(taken)) + "\n")


if __name__ == "__main__":
    if len(sys.argv) == 1:
        main()
    else:
        _sample_main(*sys.argv[1:])
