"""Tests for the instruction-count gate: synthetic JSON in, exit codes out."""

import json
from typing import TYPE_CHECKING

import pytest

import bench_gate

if TYPE_CHECKING:
    from pathlib import Path

BUILD = "core::engine::build"
RUN = "core::engine::run"


def harness_line(module_path: str, bench_id: str, instructions: object) -> str:
    """Returns one line of harness output, reduced to the fields the gate reads."""
    ir = {"metrics": {"Left": {"Int": instructions}}}
    summary = {"Callgrind": {"Ir": ir}}
    total = {"summary": summary}
    profile = {"summaries": {"total": total}}
    return json.dumps(
        {"module_path": module_path, "id": bench_id, "profiles": [profile]},
    )


def write_results(path: Path, counts: dict[str, int]) -> Path:
    """Writes harness output with one benchmark per `module_path.id` key of `counts`."""
    lines = [
        harness_line(*key.rsplit(".", 1), instructions)
        for key, instructions in counts.items()
    ]
    path.write_text("\n".join(lines) + "\n")
    return path


def write_baseline(path: Path, counts: dict[str, int]) -> Path:
    """Writes a baseline file mapping bench ids to instruction counts."""
    path.write_text(json.dumps(counts))
    return path


def gate(tmp_path: Path, baseline: dict[str, int], current: dict[str, int]) -> int:
    """Runs the gate on a baseline and a run, and returns its exit code."""
    baseline_file = write_baseline(tmp_path / "baseline.json", baseline)
    results_file = write_results(tmp_path / "results.jsonl", current)
    return bench_gate.main(
        ["--baseline", str(baseline_file), "--results", str(results_file)],
    )


def test_gate_fails_on_a_three_percent_rise_and_passes_on_one(tmp_path: Path) -> None:
    """The threshold sits between 1% and 3%."""
    key = f"{BUILD}.chain_1000"
    assert gate(tmp_path, {key: 1_000_000}, {key: 1_030_000}) == 1
    assert gate(tmp_path, {key: 1_000_000}, {key: 1_010_000}) == 0


def test_a_rise_of_exactly_two_percent_passes(tmp_path: Path) -> None:
    """Only a rise of more than 2% fails."""
    key = f"{BUILD}.chain_1000"
    assert gate(tmp_path, {key: 1_000_000}, {key: 1_020_000}) == 0
    assert gate(tmp_path, {key: 1_000_000}, {key: 1_020_001}) == 1


def test_a_drop_passes(tmp_path: Path) -> None:
    """Getting faster is never a failure."""
    key = f"{RUN}.fan_in_10000"
    assert gate(tmp_path, {key: 1_000_000}, {key: 500_000}) == 0


def test_one_regression_among_passing_benches_fails(tmp_path: Path) -> None:
    """Each id is judged on its own."""
    steady, slow = f"{BUILD}.chain_1000", f"{RUN}.chain_1000"
    baseline = {steady: 1_000, slow: 1_000}
    assert gate(tmp_path, baseline, {steady: 1_000, slow: 1_100}) == 1


def test_a_baseline_id_missing_from_the_run_fails(tmp_path: Path) -> None:
    """A bench that vanished would otherwise slip past the gate."""
    kept, lost = f"{BUILD}.chain_1000", f"{RUN}.chain_1000"
    assert gate(tmp_path, {kept: 10, lost: 10}, {kept: 10}) == 1


def test_a_new_id_is_reported_and_does_not_fail(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A bench with no baseline yet is news, not a regression."""
    kept, new = f"{BUILD}.chain_1000", f"{RUN}.layered_1000"
    assert gate(tmp_path, {kept: 10}, {kept: 10, new: 99}) == 0
    assert new in capsys.readouterr().out


def test_a_failure_names_the_regressed_id(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The report says which bench rose, so the failure can be acted on."""
    key = f"{BUILD}.fan_in_1000"
    assert gate(tmp_path, {key: 1_000}, {key: 2_000}) == 1
    assert key in capsys.readouterr().out


def test_output_that_is_not_harness_output_is_an_error(tmp_path: Path) -> None:
    """Garbage in must not look like a pass."""
    baseline = write_baseline(tmp_path / "baseline.json", {"a.b": 1})
    results = tmp_path / "results.jsonl"
    results.write_text('{"unrelated": true}\n')
    code = bench_gate.main(["--baseline", str(baseline), "--results", str(results)])
    assert code == bench_gate.EXIT_UNREADABLE


def test_write_baseline_round_trips_through_the_gate(tmp_path: Path) -> None:
    """A baseline written from a run passes against that same run."""
    counts = {f"{BUILD}.chain_1000": 123, f"{RUN}.chain_1000": 45}
    results = write_results(tmp_path / "results.jsonl", counts)
    baseline = tmp_path / "baseline.json"
    argv = ["--baseline", str(baseline), "--results", str(results)]
    assert bench_gate.main([*argv, "--write-baseline"]) == 0
    assert json.loads(baseline.read_text()) == counts
    assert bench_gate.main(argv) == 0


def run_files(tmp_path: Path, baseline_text: str, results_text: str) -> int:
    """Runs the gate on raw file contents, and returns its exit code."""
    baseline = tmp_path / "baseline.json"
    baseline.write_text(baseline_text)
    results = tmp_path / "results.jsonl"
    results.write_text(results_text)
    return bench_gate.main(["--baseline", str(baseline), "--results", str(results)])


KEY = f"{BUILD}.chain_1000"
ONE_RESULT = harness_line(BUILD, "chain_1000", 10) + "\n"


def test_an_empty_baseline_is_an_error(tmp_path: Path) -> None:
    """A baseline with no ids would pass every run."""
    assert run_files(tmp_path, "{}", ONE_RESULT) == bench_gate.EXIT_UNREADABLE


@pytest.mark.parametrize("count", [True, 1.5, "10"])
def test_a_baseline_count_that_is_not_an_integer_is_an_error(
    tmp_path: Path, count: object
) -> None:
    """Booleans are integers to Python, but not instruction counts."""
    baseline = json.dumps({KEY: count})
    assert run_files(tmp_path, baseline, ONE_RESULT) == bench_gate.EXIT_UNREADABLE


@pytest.mark.parametrize("count", [True, 1.5, "10"])
def test_a_result_count_that_is_not_an_integer_is_an_error(
    tmp_path: Path, count: object
) -> None:
    """The run's counts are held to the same rule as the baseline's."""
    line = harness_line(BUILD, "chain_1000", count)
    baseline = json.dumps({KEY: 10})
    assert run_files(tmp_path, baseline, line + "\n") == bench_gate.EXIT_UNREADABLE


def test_a_repeated_result_id_is_an_error(tmp_path: Path) -> None:
    """Two counts for one id leave the verdict ambiguous."""
    results = ONE_RESULT + harness_line(BUILD, "chain_1000", 20) + "\n"
    baseline = json.dumps({KEY: 10})
    assert run_files(tmp_path, baseline, results) == bench_gate.EXIT_UNREADABLE


@pytest.mark.parametrize("results", ["", "\n\n", "not json\n"])
def test_results_that_are_empty_or_not_json_are_an_error(
    tmp_path: Path, results: str
) -> None:
    """A run that produced nothing readable must not pass."""
    baseline = json.dumps({KEY: 10})
    assert run_files(tmp_path, baseline, results) == bench_gate.EXIT_UNREADABLE
