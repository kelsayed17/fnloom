"""Concurrent use of one driver and one plan, and free-threaded imports."""

import sys
import sysconfig
from concurrent.futures import ThreadPoolExecutor

import pytest

import fnloom
from helpers import make_module

THREADS = 8
RUNS = 500

SOURCE = """
def total(price: float, quantity: int) -> float:
    return price * quantity

def label(total: float) -> str:
    return f"{total:.2f}"
"""


@pytest.mark.skipif(
    not sysconfig.get_config_var("Py_GIL_DISABLED"),
    reason="only a free-threaded build can run without the GIL",
)
def test_importing_fnloom_keeps_the_gil_off() -> None:
    """The extension declares itself free-threading safe, so the GIL stays off."""
    assert fnloom.__version__
    assert not sys._is_gil_enabled()  # noqa: SLF001  # the only way to ask


def test_threads_share_a_driver_and_a_plan() -> None:
    """Threads sharing one plan and one driver each get their own answers."""
    driver = fnloom.build(make_module("threaded", SOURCE))
    plan = driver.prepare(["label"], inputs={"price": 0.0, "quantity": 0})

    def work(thread: int) -> None:
        for run in range(RUNS):
            quantity = thread * RUNS + run
            expected = f"{quantity * 2.0:.2f}"
            assert plan.run(inputs={"price": 2.0, "quantity": quantity}) == {
                "label": expected
            }
            # Alternate two request shapes, so the plan cache and its fast path churn.
            outputs = ["label"] if run % 2 else ["total", "label"]
            result = driver.execute(
                outputs, inputs={"price": 2.0, "quantity": quantity}
            )
            assert result["label"] == expected

    with ThreadPoolExecutor(THREADS) as pool:
        for future in [pool.submit(work, thread) for thread in range(THREADS)]:
            future.result()


MIXED_THREADS = 16
MIXED_RUNS = 200
# Each thread repeats a value every `REPEAT` runs, so the cache both stores and hits.
REPEAT = 50

MIXED = """
import fnloom


@fnloom.extract_fields({"low": int, "high": int})
def parts(n: int) -> dict:
    return {"low": n % 7, "high": n // 7}


@fnloom.config.when(region="eu")
def rate__eu() -> int:
    return 3


@fnloom.config.when(region="us")
def rate__us() -> int:
    return 5


@fnloom.parameterize(
    scaled_low={"x": fnloom.source("low"), "k": fnloom.value(2)},
    scaled_high={"x": fnloom.source("high"), "k": fnloom.source("rate")},
)
def scaled(x: int, k: int) -> int:
    return x * k


@fnloom.parameterize(
    gathered={
        "items": fnloom.group(
            fnloom.source("scaled_low"),
            fnloom.source("scaled_high"),
            fnloom.source("shifted.offset"),
        )
    }
)
def gather(items: list[int]) -> int:
    return sum(items)
"""

SHIFTED = """
def offset(n: int) -> int:
    return n + 1
"""


def _mixed_driver() -> fnloom.Driver:
    """Config, extracted fields, a cache, a namespace, a row and a group."""
    return (
        fnloom.Builder()
        .with_modules(make_module("mixed", MIXED))
        .with_modules(make_module("shifted_nodes", SHIFTED), namespace="shifted")
        .with_config({"region": "eu"})
        .with_cache(fnloom.MemoryCache())
        .build()
    )


def _gathered(n: int) -> int:
    """`gathered` in closed form: `2 (n mod 7) + 3 (n div 7) + n + 1`."""
    return 2 * (n % 7) + 3 * (n // 7) + n + 1


def test_one_driver_across_threads() -> None:
    """16 threads share one driver: every run, check, inspection and rebuild."""
    driver = _mixed_driver()
    inputs_before = dict(driver.inputs())
    path = driver.what_is_the_path_between("high", "gathered")

    def work(thread: int) -> int:
        checked = 0
        for run in range(MIXED_RUNS):
            n = thread * 1_000 + run % REPEAT
            given = {"n": n}
            outputs = ["gathered", "high"] if run % 3 else ["gathered"]
            result = driver.execute(outputs, inputs=given)
            assert result["gathered"] == _gathered(n), (thread, run, result)
            if "high" in result:
                assert result["high"] == n // 7
            # The caller's mapping is never written to.
            assert given == {"n": n}
            if run % 100 == 0:
                # Builds run alongside the other threads' runs, on the same store.
                rebuilt = driver.rebuild()
                assert rebuilt.execute(["gathered"], inputs=given) == {
                    "gathered": _gathered(n)
                }
            if run % 10 == 0:
                assert driver.validate(["gathered"], inputs=given) == []
                assert driver.validate(["gathered"], inputs={"n": "x"}) == [
                    "n: expected int, got str"
                ]
                assert dict(driver.inputs()) == inputs_before
                assert driver.what_is_the_path_between("high", "gathered") == path
            checked += 1
        return checked

    with ThreadPoolExecutor(MIXED_THREADS) as pool:
        done = [pool.submit(work, thread) for thread in range(MIXED_THREADS)]
        assert sum(future.result() for future in done) == MIXED_THREADS * MIXED_RUNS
    assert path == ["high", "scaled_high", "gathered"]
    # Every thread's values were stored, and are hits now, on the same driver.
    explained = driver.explain_cache(["gathered"], inputs={"n": 15 * 1_000 + 3})
    assert explained["gathered"].status == "hit"
    assert driver.execute(["gathered"], inputs={"n": 3}) == {"gathered": _gathered(3)}
