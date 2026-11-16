"""Executor equivalence: results never depend on the worker count (Plan 11, task 3).

A program with a nested fan-out, a node that declines, a node an error makes absent
and a node that may fail runs without `with_threads` and with 1, 2 and 8 workers,
over drawn inputs. Its values, `Collect` orders, exception class, skipped nodes, error
absences and cache keys are the same on all.
"""

import gc
import os
import threading
import time
from typing import Any, override

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import fnloom
from fnloom import _core
from helpers import make_module

pytestmark = pytest.mark.threads

PROGRAM = """
from fnloom import ABSENT, Absent, Collect, Parallelizable, absent_on


def picked(n: int) -> Parallelizable[int]:
    yield from range(n)


def inner(picked: int) -> Parallelizable[int]:
    yield from range(picked % 3)


def leaf(inner: int, picked: int, fail_at: int) -> int:
    if picked * 10 + inner == fail_at:
        raise KeyError(fail_at)
    return picked * 10 + inner


def per_outer(leaf: Collect[int]) -> list[int]:
    return list(leaf)


def maybe(picked: int, absent_at: int, fail_at: int) -> int | Absent:
    if picked == absent_at:
        return ABSENT
    if picked == -fail_at:
        raise ValueError(picked)
    return picked


def shown(maybe: int) -> str:
    return f"#{maybe}"


def everything(per_outer: Collect[list[int]]) -> list[list[int]]:
    return list(per_outer)


def present(shown: Collect[str]) -> list[str]:
    return list(shown)


def total(everything: list[list[int]], present: list[str]) -> int:
    return sum(map(sum, everything)) + len(present)


@absent_on(ArithmeticError)
def ratio(picked: int, absent_at: int) -> int | Absent:
    return 100 // (picked - absent_at)


def ratios(ratio: Collect[int | Absent]) -> list[object]:
    return list(ratio)
"""

WORKERS: tuple[int | None, ...] = (None, 1, 2, 8)
# The CI `stress` job sets HYPOTHESIS_PROFILE=stress for its profile's examples.
EXAMPLES = (
    settings.get_profile("stress").max_examples
    if os.environ.get("HYPOTHESIS_PROFILE") == "stress"
    else 50
)
OUTPUTS = ("everything", "present", "total", "ratios")


class Keys(fnloom.MemoryCache):
    """A memory store that records every key written to it."""

    def __init__(self) -> None:
        """An empty store."""
        super().__init__()
        self.keys: list[bytes] = []
        self.lock = threading.Lock()

    @override
    def put(self, key: bytes, value: object) -> None:
        with self.lock:
            self.keys.append(key)
        super().put(key, value)


class Skips(fnloom.Hook):
    """Keeps each run's `RunEnd.skipped` and `RunEnd.absent_errors`."""

    def __init__(self) -> None:
        """No run yet."""
        self.skipped: tuple[tuple[str, tuple[int, ...]], ...] = ()
        self.absent: list[tuple[str, tuple[int, ...], str]] = []

    @override
    def after_run(self, event: fnloom.RunEnd) -> None:
        self.skipped = event.skipped
        self.absent = [(n, b, repr(e)) for n, b, e in event.absent_errors]


def outcome(workers: int | None, inputs: dict[str, int], outputs: list[str]) -> Any:  # noqa: ANN401  # one comparable record
    """One run's values, skipped nodes, error absences, the keys it wrote; or its error.

    A failed run is only its exception class: which other branches ran before the
    run stopped depends on timing with several workers, as Hamilton's does. The keys
    are compared as a set: two branches with one key both write it when the first
    finishes before the second is handed out, which single-flight doesn't cover.
    """
    store, hook = Keys(), Skips()
    builder = (
        fnloom.Builder()
        .with_modules(make_module("equivalence", PROGRAM))
        .with_cache(store)
        .with_hooks(hook)
    )
    if workers is not None:
        builder = builder.with_threads(workers)
    driver = builder.build()
    try:
        result = driver.execute(outputs, inputs=inputs)
    except Exception as error:  # noqa: BLE001  # the exception class is compared
        return type(error).__name__
    finally:
        # Ended now, not when the cyclic collector frees the driver: hundreds of
        # examples would otherwise keep hundreds of pool threads alive.
        pool = None if driver._workers is None else driver._workers.threads()  # noqa: SLF001  # the driver's own pool
        if pool is not None:
            pool.close()
    # In order, unsorted: every driver lists them in reference order already.
    return result, sorted(hook.skipped), hook.absent, sorted(set(store.keys))


@settings(max_examples=EXAMPLES, deadline=None)
@given(
    n=st.integers(0, 6),
    absent_at=st.integers(-1, 6),
    fail_at=st.sampled_from([-99, 1, 21, 40, 51, -3]),
    outputs=st.lists(st.sampled_from(OUTPUTS), min_size=1, max_size=3, unique=True),
)
def test_results_are_equal_over_worker_counts(
    n: int, absent_at: int, fail_at: int, outputs: list[str]
) -> None:
    """The same inputs give the same record without threads and with 1, 2 and 8."""
    inputs = {"n": n, "absent_at": absent_at, "fail_at": fail_at}
    reference = outcome(None, inputs, outputs)
    for workers in WORKERS[1:]:
        assert outcome(workers, inputs, outputs) == reference


def test_an_outcome_leaves_no_pool_thread_running() -> None:
    """Each run's pool is closed once it ends, so examples don't pile up threads."""
    gc.collect()
    before = _core.live_workers()
    inputs = {"n": 4, "absent_at": -1, "fail_at": -99}
    for _ in range(5):
        outcome(8, inputs, ["total"])
    deadline = time.monotonic() + 2
    while _core.live_workers() > before and time.monotonic() < deadline:
        time.sleep(0.02)
    assert _core.live_workers() <= before
