"""Fails when a benchmark's instruction count rises over 2% above the baseline.

Wall-clock time is too noisy on shared CI runners to gate on, so the core's benchmarks
are counted in instructions under callgrind (`crates/fnloom-core/benches/core.rs`).

The input is the harness's machine-readable output: run
`cargo bench -p fnloom-core --bench core -- --output-format=json > bench.jsonl`, which
writes one JSON object per benchmark to stdout. The gate reads a benchmark's
`module_path` and `id`, joined as `module_path.id` (the key in the baseline), and its
`Ir` metric (instructions executed), found at
`profiles[0].summaries.total.summary.Callgrind`. The baseline,
`crates/fnloom-core/benches/baseline.json`, maps each such key to its count.

Exit status: 0 when every baseline id is present and within the threshold, 1 when one
rose too far or is missing, 2 when an input cannot be read: not JSON, no benchmarks,
a count that is not an integer (booleans included), or one benchmark listed twice.
An id with no baseline is reported only. `--write-baseline` replaces the baseline with
the run's counts; do that deliberately, with the reason in the commit message.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, TypeIs

if TYPE_CHECKING:
    from collections.abc import Sequence

type Counts = dict[str, int]

# A rise of more than this many percent over the baseline fails the gate.
THRESHOLD_PERCENT = 2

EXIT_REGRESSED = 1
EXIT_UNREADABLE = 2


class GateError(Exception):
    """An input is not what the gate expects; the run's verdict cannot be trusted."""


def _is_count(value: object) -> TypeIs[int]:
    """Whether `value` is an instruction count: an `int`, and not a `bool`."""
    return isinstance(value, int) and not isinstance(value, bool)


def _step(value: object, step: str | int) -> object:
    """Returns `value[step]` for a dict (str step) or list (int step), or raises."""
    if isinstance(step, str) and isinstance(value, dict) and step in value:
        return value[step]
    if isinstance(step, int) and isinstance(value, list) and step < len(value):
        return value[step]
    msg = f"harness output has no {step!r} where one was expected"
    raise GateError(msg)


def _dig(value: object, *path: str | int) -> object:
    """Follows `path` through nested dicts and lists, or raises `GateError`."""
    for step in path:
        value = _step(value, step)
    return value


def _count(record: object) -> tuple[str, int]:
    """Returns `(module_path.id, instructions)` from one harness record."""
    module_path = _dig(record, "module_path")
    bench_id = _dig(record, "id")
    instructions = _dig(
        record,
        *("profiles", 0, "summaries", "total", "summary", "Callgrind", "Ir"),
        *("metrics", "Left", "Int"),
    )
    if not (isinstance(module_path, str) and isinstance(bench_id, str)):
        msg = "harness output names a benchmark with a non-string module_path or id"
        raise GateError(msg)
    if not _is_count(instructions):
        msg = f"{module_path}.{bench_id}: the Ir count is not an integer"
        raise GateError(msg)
    return f"{module_path}.{bench_id}", instructions


def parse_results(text: str) -> Counts:
    """Returns the instruction count of every benchmark in the harness's JSON lines."""
    counts: Counts = {}
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record: object = json.loads(line)
        except json.JSONDecodeError as error:
            msg = f"results line {number} is not JSON: {error}"
            raise GateError(msg) from error
        key, instructions = _count(record)
        if key in counts:
            msg = f"results line {number} repeats the benchmark {key}"
            raise GateError(msg)
        counts[key] = instructions
    if not counts:
        msg = "the results hold no benchmarks"
        raise GateError(msg)
    return counts


def parse_baseline(text: str) -> Counts:
    """Returns the baseline's `{bench id: instruction count}` map."""
    try:
        data: object = json.loads(text)
    except json.JSONDecodeError as error:
        msg = f"the baseline is not JSON: {error}"
        raise GateError(msg) from error
    if not isinstance(data, dict) or not all(
        isinstance(key, str) and _is_count(value) for key, value in data.items()
    ):
        msg = "the baseline must map bench ids to integer instruction counts"
        raise GateError(msg)
    if not data:
        msg = "the baseline holds no benchmarks"
        raise GateError(msg)
    return {str(key): int(value) for key, value in data.items()}


def _rose_too_far(baseline: int, current: int) -> bool:
    """Whether `current` exceeds `baseline` by more than the threshold, in integers."""
    return current * 100 > baseline * (100 + THRESHOLD_PERCENT)


def _change(baseline: int, current: int) -> str:
    """Formats the change from `baseline` to `current` as a signed percentage."""
    if baseline == 0:
        return "n/a"
    return f"{(current - baseline) * 100 / baseline:+.2f}%"


def compare(baseline: Counts, current: Counts) -> tuple[list[str], bool]:
    """Returns the report lines and whether the gate passes."""
    lines: list[str] = []
    passed = True
    for key in sorted(baseline):
        if key not in current:
            lines.append(f"MISSING  {key}: in the baseline but not in this run")
            passed = False
            continue
        failed = _rose_too_far(baseline[key], current[key])
        passed = passed and not failed
        verdict = "FAIL" if failed else "ok"
        lines.append(
            f"{verdict:8} {key}: {baseline[key]:,} -> {current[key]:,}"
            f" ({_change(baseline[key], current[key])})",
        )
    lines.extend(
        f"NEW      {key}: {current[key]:,} (no baseline yet)"
        for key in sorted(current.keys() - baseline.keys())
    )
    return lines, passed


def _parser() -> argparse.ArgumentParser:
    """Builds the command-line parser."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--baseline", type=Path, required=True, help="baseline.json")
    parser.add_argument(
        "--results",
        type=Path,
        required=True,
        help="the harness's JSON-lines output",
    )
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="replace the baseline with this run's counts instead of comparing",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Runs the gate and returns the process exit status."""
    args = _parser().parse_args(argv)
    try:
        current = parse_results(args.results.read_text(encoding="utf-8"))
        if args.write_baseline:
            body = json.dumps(current, indent=2, sort_keys=True)
            args.baseline.write_text(body + "\n", encoding="utf-8")
            return 0
        baseline = parse_baseline(args.baseline.read_text(encoding="utf-8"))
    except (GateError, OSError) as error:
        sys.stderr.write(f"bench_gate: {error}\n")
        return EXIT_UNREADABLE
    lines, passed = compare(baseline, current)
    sys.stdout.write("\n".join(lines) + "\n")
    return 0 if passed else EXIT_REGRESSED


if __name__ == "__main__":
    raise SystemExit(main())
