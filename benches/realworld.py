"""Times the cold start, first call and memory of fnloom and Hamilton; prints JSON rows.

Run it as `uv run python -m benches.realworld`. Every sample is a fresh interpreter,
so nothing is warm: a sample imports the library, imports the workload's module,
builds a driver, makes a first `execute`, then times warm ones, and reports the peak
and final resident set size. A row is the median over its processes. It prints to
stdout on purpose: the rows are the output, so redirect them to a file.

The workloads are written to a temporary directory in each runtime's spelling and
imported by the sampling process, as an application would:

- `mixed`: about forty nodes of numpy arrays: a chain of transforms, a
  `parameterize` fan-out of ten scales, an `extract_fields` of five statistics, and a
  fan-in report.
- `mixed_async`: the same, the numpy nodes `async def`. The `parameterize` rows stay
  plain functions: Hamilton's async driver hands a parameterized `async def` node's
  dependents a coroutine, not its value.
- `rows_np`: one function with 200 `parameterize` rows, each doing a numpy dot.

`numpy` is `lazy` (each node imports it when it first runs, as an application with
optional heavy dependencies does) or `loaded` (the sampling process imports and
uses it before the timed part, so the first call carries no import). The runtimes
are `fnloom`, `hamilton` (extensions autoloaded, as it ships) and `hamilton-noext`
(`HAMILTON_AUTOLOAD_EXTENSIONS=0`, as the other scripts run it); `none` is a bare
interpreter, for the memory baseline.
"""

import asyncio
import hashlib
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal, cast, get_args

type Workload = Literal["mixed", "mixed_async", "rows_np"]
type Numpy = Literal["lazy", "loaded"]
type Runtime = Literal["none", "fnloom", "hamilton", "hamilton-noext"]

WORKLOADS = cast("tuple[Workload, ...]", get_args(Workload.__value__))
RUNTIMES = cast("tuple[Runtime, ...]", get_args(Runtime.__value__))
PROCESSES = 7
RUNS = 15
SCALES = 10
ROWS = 200
SIZE = 20_000
NS_PER_MS = 1_000_000
DIGEST_LIMIT = 80
ROOT = Path(__file__).resolve().parents[1]

_MODIFIERS = {
    "hamilton": "hamilton.function_modifiers",
    "fnloom": "fnloom",
}


@dataclass(frozen=True)
class Sample:
    """One process's times (milliseconds) and sizes (megabytes)."""

    import_ms: float
    module_ms: float
    build_ms: float
    first_ms: float
    steady_ms: float
    peak_mb: float
    rss_mb: float
    digest: str


@dataclass(frozen=True)
class Row:
    """One measurement: the medians over `processes` samples."""

    workload: str
    numpy: str
    runtime: str
    processes: int
    import_ms: float
    module_ms: float
    build_ms: float
    first_ms: float
    steady_ms: float
    peak_mb: float
    rss_mb: float


def _def(*, asynchronous: bool) -> str:
    return "async def" if asynchronous else "def"


def _mixed_source(spelling: str, numpy: Numpy, *, asynchronous: bool) -> str:
    """The `mixed` module: numpy arrays through about forty nodes."""
    lazy = numpy == "lazy"
    arr = "object" if lazy else "np.ndarray"
    imp = "    import numpy as np\n" if lazy else ""
    top = "" if lazy else "import numpy as np\n"
    kw = _def(asynchronous=asynchronous)
    yield_ = "    await asyncio.sleep(0)\n" if asynchronous else ""
    head = "import asyncio\n" if asynchronous else ""
    scales = ",\n".join(
        f"    scaled_{i}={{'k': value({0.5 + i / 4})}}" for i in range(SCALES)
    )
    energies = ",\n".join(
        f"    energy_{i}={{'x': source('scaled_{i}')}}" for i in range(SCALES)
    )
    params = ", ".join(f"energy_{i}: float" for i in range(SCALES))
    terms = " + ".join(f"energy_{i}" for i in range(SCALES))
    return f"""{head}{top}from {_MODIFIERS[spelling]} import (
    extract_fields,
    parameterize,
    source,
    value,
)


{kw} raw(seed: int) -> {arr}:
{imp}{yield_}    return np.random.default_rng(seed).standard_normal({SIZE})


{kw} clean(raw: {arr}) -> {arr}:
{imp}    return np.clip(raw, -3.0, 3.0)


{kw} centered(clean: {arr}) -> {arr}:
{imp}    return clean - clean.mean()


{kw} smooth(centered: {arr}) -> {arr}:
{imp}    return np.convolve(centered, np.ones(5) / 5.0, mode="same")


{kw} diffs(smooth: {arr}) -> {arr}:
{imp}    return np.diff(smooth)


{kw} squared(smooth: {arr}) -> {arr}:
{imp}    return smooth * smooth


{kw} ranked(smooth: {arr}) -> {arr}:
{imp}    return np.argsort(smooth)


{kw} cumulative(squared: {arr}) -> {arr}:
{imp}    return np.cumsum(squared)


{kw} spectrum(smooth: {arr}) -> {arr}:
{imp}    return np.abs(np.fft.rfft(smooth))


@parameterize(
{scales},
)
def scaled(smooth: {arr}, k: float) -> {arr}:
    return smooth * k


@extract_fields({{"mean": float, "std": float, "lo": float, "hi": float, "q50": float}})
{kw} stats(centered: {arr}) -> dict:
{imp}    return {{
        "mean": float(centered.mean()),
        "std": float(centered.std()),
        "lo": float(centered.min()),
        "hi": float(centered.max()),
        "q50": float(np.quantile(centered, 0.5)),
    }}


@parameterize(
{energies},
)
def energy(x: {arr}) -> float:
{imp}    return float(np.dot(x, x))


{kw} span(lo: float, hi: float) -> float:
    return hi - lo


{kw} zscore(hi: float, mean: float, std: float) -> float:
    return (hi - mean) / std


{kw} peak(spectrum: {arr}) -> float:
{imp}    return float(np.argmax(spectrum))


{kw} order(ranked: {arr}, diffs: {arr}, cumulative: {arr}) -> float:
{imp}    return float(ranked[0] + diffs.sum() + cumulative[-1])


{kw} report(
    {params},
    span: float,
    zscore: float,
    peak: float,
    order: float,
    q50: float,
) -> float:
{yield_}    return {terms} + span + zscore + peak + order + q50
"""


def _rows_source(spelling: str, numpy: Numpy) -> str:
    """The `rows_np` module: one function with 200 `parameterize` rows."""
    lazy = numpy == "lazy"
    arr = "object" if lazy else "np.ndarray"
    imp = "    import numpy as np\n" if lazy else ""
    top = "" if lazy else "import numpy as np\n"
    rows = ",\n".join(
        f"    row_{i}={{'x': source('base'), 'k': value({i})}}" for i in range(ROWS)
    )
    return f"""{top}from {_MODIFIERS[spelling]} import parameterize, source, value


def base(seed: int) -> {arr}:
{imp}    return np.random.default_rng(seed).standard_normal({SIZE})


@parameterize(
{rows},
)
def row(x: {arr}, k: int) -> float:
{imp}    return float(np.dot(x, x)) * k
"""


def source_of(workload: Workload, spelling: str, numpy: Numpy) -> str:
    """The module text of `workload` in `spelling` (`fnloom` or `hamilton`)."""
    if workload == "rows_np":
        return _rows_source(spelling, numpy)
    return _mixed_source(spelling, numpy, asynchronous=workload == "mixed_async")


def request(workload: Workload) -> list[str]:
    """The outputs the workload is asked for."""
    if workload == "rows_np":
        return [f"row_{i}" for i in range(ROWS)]
    return ["report"]


def _rss_mb() -> float:
    out = subprocess.run(  # noqa: S603  # ps, asked about this process
        ["ps", "-o", "rss=", "-p", str(os.getpid())],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    )
    return int(out.stdout) / 1024


def _peak_mb() -> float:
    import resource  # noqa: PLC0415  # not on every platform

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Bytes on macOS, kilobytes on Linux.
    return peak / (1024 * 1024 if sys.platform == "darwin" else 1024)


def _ms(start: int) -> float:
    return (time.perf_counter_ns() - start) / NS_PER_MS


def _digest(values: dict[str, object]) -> str:
    text = ",".join(f"{k}={float(v):.9g}" for k, v in sorted(values.items()))  # ty: ignore[invalid-argument-type]
    return (
        hashlib.sha1(text.encode(), usedforsecurity=False).hexdigest()[:12]
        if len(text) > DIGEST_LIMIT
        else text
    )


def sample(  # noqa: C901
    workload: Workload, numpy: Numpy, runtime: Runtime, directory: str
) -> Sample:
    """Runs one sample in this process; the parent starts a fresh one for each."""
    spelling = "fnloom" if runtime == "fnloom" else "hamilton"
    if runtime == "hamilton-noext":
        os.environ["HAMILTON_AUTOLOAD_EXTENSIONS"] = "0"
    if numpy == "loaded":
        import numpy as np  # noqa: PLC0415

        # The submodules the nodes use load on first use, so use them.
        np.fft.rfft(np.random.default_rng(1).standard_normal(8))
        np.quantile(np.ones(3), 0.5)
    sys.path.insert(0, str(Path(directory) / spelling))
    outputs = request(workload)
    inputs = {"seed": 7}
    asynchronous = workload == "mixed_async"
    steady: list[float] = []
    start = time.perf_counter_ns()
    if runtime == "fnloom":
        import fnloom  # noqa: PLC0415
    elif runtime != "none":
        import hamilton  # noqa: F401, PLC0415
        from hamilton import async_driver, base, driver  # noqa: PLC0415
    import_ms = _ms(start)
    if runtime == "none":
        return Sample(0, 0, 0, 0, 0, _peak_mb(), _rss_mb(), "")
    start = time.perf_counter_ns()
    import importlib  # noqa: PLC0415

    module = importlib.import_module(f"{workload}_{numpy}")
    module_ms = _ms(start)

    async def run() -> tuple[float, float, dict[str, object]]:
        drv: Any
        begin = time.perf_counter_ns()
        if runtime == "fnloom":
            built = fnloom.Builder().with_modules(module)
            drv = built.build_async() if asynchronous else built.build()
        elif asynchronous:
            drv = await async_driver.Builder().with_modules(module).build()  # ty: ignore[invalid-await]
        else:
            drv = (
                driver.Builder()
                .with_modules(module)
                .with_adapters(base.DictResult())
                .build()
            )
        built_ms = _ms(begin)

        async def call() -> dict[str, object]:
            result: Any = drv.execute(outputs, inputs=inputs)  # ty: ignore[invalid-argument-type]
            return dict(await result if asynchronous else result)

        begin = time.perf_counter_ns()
        result = await call()
        first = _ms(begin)
        for _ in range(RUNS):
            begin = time.perf_counter_ns()
            await call()
            steady.append(_ms(begin))
        return built_ms, first, result

    import warnings  # noqa: PLC0415

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        build_ms, first_ms, result = asyncio.run(run())
    return Sample(
        import_ms,
        module_ms,
        build_ms,
        first_ms,
        statistics.median(steady),
        _peak_mb(),
        _rss_mb(),
        _digest(result),
    )


def _write(directory: str) -> None:
    """Writes every workload module, in each spelling, under `directory`."""
    for spelling in ("fnloom", "hamilton"):
        folder = Path(directory) / spelling
        folder.mkdir(parents=True)
        for workload in WORKLOADS:
            for numpy in get_args(Numpy.__value__):
                text = source_of(workload, spelling, numpy)
                (folder / f"{workload}_{numpy}.py").write_text(text)


def _sample_in_process(
    workload: Workload, numpy: Numpy, runtime: Runtime, directory: str
) -> Sample:
    command = [
        sys.executable,
        "-X",
        "frozen_modules=on",
        "-m",
        "benches.realworld",
        workload,
        numpy,
        runtime,
        directory,
    ]
    done = subprocess.run(  # noqa: S603  # this interpreter, running this module
        command, cwd=ROOT, capture_output=True, text=True, check=False
    )
    if done.returncode != 0:
        sys.stderr.write(done.stderr)
        raise SystemExit(done.returncode)
    return Sample(**json.loads(done.stdout.splitlines()[-1]))


def _median(taken: list[Sample], field: str) -> float:
    return round(float(statistics.median(getattr(s, field) for s in taken)), 3)


def main() -> None:
    """Prints a row per workload, numpy mode and runtime as JSON lines."""
    with tempfile.TemporaryDirectory() as directory:
        _write(directory)
        configs: list[tuple[Workload, Numpy, Runtime]] = [
            (w, n, r)
            for w in WORKLOADS
            for n in get_args(Numpy.__value__)
            for r in RUNTIMES
            if r != "none"
        ]
        configs += [("mixed", n, "none") for n in get_args(Numpy.__value__)]
        samples: dict[tuple[Workload, Numpy, Runtime], list[Sample]] = {
            c: [] for c in configs
        }
        # Round-robin, so a slow stretch of the machine touches every row alike.
        for _ in range(PROCESSES):
            for config in configs:
                samples[config].append(_sample_in_process(*config, directory))
    digests: dict[tuple[str, str], set[str]] = {}
    for (workload, numpy, runtime), taken in samples.items():
        if runtime != "none":
            digests.setdefault((workload, numpy), set()).update(s.digest for s in taken)
    for key, seen in digests.items():
        if len(seen) != 1:
            msg = f"{key}: the runtimes computed different answers: {sorted(seen)}"
            raise SystemExit(msg)
    for (workload, numpy, runtime), taken in samples.items():
        row = Row(
            workload,
            numpy,
            runtime,
            len(taken),
            *(
                _median(taken, f)
                for f in (
                    "import_ms",
                    "module_ms",
                    "build_ms",
                    "first_ms",
                    "steady_ms",
                    "peak_mb",
                    "rss_mb",
                )
            ),
        )
        sys.stdout.write(json.dumps(asdict(row)) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    if len(sys.argv) == 1:
        main()
    else:
        workload, numpy, runtime, directory = sys.argv[1:]
        sys.stdout.write(
            json.dumps(asdict(sample(workload, numpy, runtime, directory))) + "\n"  # ty: ignore[invalid-argument-type]
        )
