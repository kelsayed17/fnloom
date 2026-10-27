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
