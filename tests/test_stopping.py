"""Stopping a parallel run: `timeout=`, `cancelled()`, closing and abandonment (task 9).

Every wait here is bounded: events and joins take a timeout, so a deadlock fails the
test that hit it, and the `threads` watchdog in `conftest.py` is the last resort. A
node that hangs waits on an event the test sets at its end.
"""

import _thread
import contextvars
import gc
import os
import subprocess
import sys
import textwrap
import threading
import time
from typing import TYPE_CHECKING, NoReturn, override

import pytest

import fnloom
from fnloom import _core
from helpers import make_module

if TYPE_CHECKING:
    from collections.abc import Callable

pytestmark = pytest.mark.threads

# How long a stop may take to reach the caller: well above the 50 ms slices.
PROMPT = 1.0
# What the nodes module's driver holds with nothing running: no worker slot, and no
# slot of its one limited node.
FREE = (0, {"guarded": 0}, {})
# How long a run waiting on a slow node, or a process exiting, may take at most.
SLOW = 5.0
EXIT = 10.0

# Nodes that hang, poll, fail or return at once. A gate is three events: set as the
# call starts, waited for (at most ten seconds), and set as the call returns.
NODES = """
import threading
import time

import fnloom
from fnloom import limit


def hung(x: int, gate: object) -> int:
    started, release, returned = gate
    started.set()
    try:
        release.wait(10)
        return x
    finally:
        returned.set()


@limit(1)
def guarded(x: int, gate: object) -> int:
    started, release, returned = gate
    started.set()
    try:
        release.wait(10)
        return x
    finally:
        returned.set()


def polled(x: int, gate: object, seen: list) -> int:
    started, _, returned = gate
    started.set()
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if fnloom.cancelled():
                seen.append("cancelled")
                return -1
            time.sleep(0.01)
        return x
    finally:
        returned.set()


def failing(x: int, gate: object) -> int:
    gate[0].wait(5)
    raise ValueError(x)


def quick(x: int) -> int:
    return x + 1


def asked(x: int) -> bool:
    return fnloom.cancelled()
"""


def gate() -> tuple[threading.Event, threading.Event, threading.Event]:
    """A fresh gate: started, release and returned."""
    return threading.Event(), threading.Event(), threading.Event()


def nodes(
    name: str,
    threads: int | None = 2,
    *,
    hooks: tuple[fnloom.Hook, ...] = (),
    cache: bool = False,
) -> fnloom.Driver:
    """The nodes module's driver, `with_threads(threads)` when given."""
    builder = fnloom.Builder().with_modules(make_module(name, NODES)).with_hooks(*hooks)
    if threads is not None:
        builder = builder.with_threads(threads)
    if cache:
        builder = builder.with_cache(fnloom.MemoryCache())
    return builder.build()


def held(driver: fnloom.Driver) -> tuple[int, dict[str, int], dict[str, int]]:
    """The units of the driver's caps held now: workers, nodes, groups."""
    workers = driver._workers  # noqa: SLF001  # the driver's own limits
    assert workers is not None
    return _core.held_slots(workers.limits())


def settled(probe: Callable[[], object], expected: object) -> object:
    """`probe()`, once it equals `expected` or two seconds have passed."""
    deadline = time.monotonic() + 2
    seen = probe()
    while seen != expected and time.monotonic() < deadline:
        time.sleep(0.02)
        seen = probe()
    return seen


def started(target: Callable[[], object]) -> threading.Thread:
    """A started thread running `target`."""
    thread = threading.Thread(target=target)
    thread.start()
    return thread


def joined(thread: threading.Thread) -> None:
    """Joins `thread`, failing the test if it is still running."""
    thread.join(10)
    assert not thread.is_alive()


class Recorded(fnloom.Hook):
    """Records each node's start and end, and the run's end."""

    def __init__(self) -> None:
        """No events yet."""
        self.events: list[tuple[object, ...]] = []

    @override
    def before_node(self, event: fnloom.NodeStart) -> None:
        self.events.append(("start", event.node.name))

    @override
    def after_node(self, event: fnloom.NodeEnd) -> None:
        self.events.append(("end", event.node.name, event.ok))

    @override
    def after_run(self, event: fnloom.RunEnd) -> None:
        self.events.append(("run", event.ok, type(event.error).__name__))


def test_timeout_raises_at_once_and_abandons_running_tasks() -> None:
    """A hung node: `TimeoutError` within the prompt, and no hook of it afterwards."""
    hook = Recorded()
    driver = nodes("stop_timeout", hooks=(hook,))
    hang = gate()
    start = time.monotonic()
    with pytest.raises(TimeoutError, match=r"within its timeout of 0\.2 s"):
        driver.execute(["hung"], inputs={"x": 1, "gate": hang}, timeout=0.2)
    assert time.monotonic() - start < PROMPT
    assert hang[0].is_set()
    assert not hang[2].is_set()
    assert hook.events == [("start", "hung"), ("run", False, "TimeoutError")]
    hang[1].set()
    assert hang[2].wait(5)
    # The abandoned call returned: it fires nothing, and holds nothing.
    assert settled(lambda: held(driver), FREE) == FREE
    assert hook.events == [("start", "hung"), ("run", False, "TimeoutError")]
    assert driver.execute(["quick"], inputs={"x": 1}, timeout=5) == {"quick": 2}


def test_a_run_within_its_timeout_returns_its_values() -> None:
    """A timeout that does not pass changes nothing, on every kind of plan."""
    for driver in (
        nodes("stop_within"),
        nodes("stop_within_cached", cache=True),
        nodes("stop_within_hooked", hooks=(Recorded(),)),
        nodes("stop_within_one", 1),
    ):
        assert driver.execute(["quick"], inputs={"x": 1}, timeout=5) == {"quick": 2}
        plan = driver.prepare(["quick"], inputs={"x": 1})
        assert plan.run_raw(inputs={"x": 3}, timeout=5) == {"quick": 4}
        assert driver.raw_execute(["quick"], inputs={"x": 0}, timeout=0.5) == {
            "quick": 1
        }


def test_a_cached_run_times_out_too() -> None:
    """A timeout bounds a cached run's core run, and stores nothing it abandoned."""
    driver = nodes("stop_cached", cache=True)
    hang = gate()
    with pytest.raises(TimeoutError):
        driver.execute(["hung"], inputs={"x": 1, "gate": hang}, timeout=0.2)
    hang[1].set()
    assert hang[2].wait(5)
    again = gate()
    again[1].set()
    assert driver.execute(["hung"], inputs={"x": 1, "gate": again}, timeout=5) == {
        "hung": 1
    }
    assert again[0].is_set()


def test_a_deadline_while_waiting_for_a_slot_abandons_nothing() -> None:
    """A run whose task waits for a slot another run holds times out, holding none."""
    driver = nodes("stop_slot")
    holder_gate = gate()
    holder = started(
        lambda: driver.execute(["guarded"], inputs={"x": 1, "gate": holder_gate})
    )
    try:
        assert holder_gate[0].wait(5)
        waiting = gate()
        waiting[1].set()
        start = time.monotonic()
        with pytest.raises(TimeoutError):
            driver.execute(["guarded"], inputs={"x": 2, "gate": waiting}, timeout=0.3)
        assert time.monotonic() - start < PROMPT
        assert not waiting[0].is_set()
        assert held(driver)[1] == {"guarded": 1}
    finally:
        holder_gate[1].set()
        joined(holder)
    assert settled(lambda: held(driver), (0, {"guarded": 0}, {})) == (
        0,
        {"guarded": 0},
        {},
    )


def test_a_deadline_releases_limits_at_abandonment() -> None:
    """The abandoned call's node slot is free at once: another run takes it."""
    driver = nodes("stop_releases")
    hang = gate()
    with pytest.raises(TimeoutError):
        driver.execute(["guarded"], inputs={"x": 1, "gate": hang}, timeout=0.2)
    assert held(driver) == (0, {"guarded": 0}, {})
    free = gate()
    free[1].set()
    assert driver.execute(["guarded"], inputs={"x": 2, "gate": free}) == {"guarded": 2}
    hang[1].set()
    assert hang[2].wait(5)


def test_an_abandoned_call_releases_nothing_when_it_returns() -> None:
    """Once it returns, the counters are the other holder's, not wrapped or freed."""
    driver = nodes("stop_no_second_release")
    hang = gate()
    with pytest.raises(TimeoutError):
        driver.execute(["guarded"], inputs={"x": 1, "gate": hang}, timeout=0.2)
    other = gate()
    holder = started(
        lambda: driver.execute(["guarded"], inputs={"x": 2, "gate": other})
    )
    try:
        assert other[0].wait(5)
        assert held(driver) == (1, {"guarded": 1}, {})
        hang[1].set()
        assert hang[2].wait(5)
        time.sleep(0.1)
        assert held(driver) == (1, {"guarded": 1}, {})
    finally:
        other[1].set()
        joined(holder)
    assert settled(lambda: held(driver), (0, {"guarded": 0}, {})) == (
        0,
        {"guarded": 0},
        {},
    )


def test_hung_abandoned_tasks_do_not_starve_later_runs() -> None:
    """Two runs abandon hung nodes on two workers; a third run still completes."""
    driver = nodes("stop_starve")
    gates = [gate(), gate()]
    try:
        for hang in gates:
            with pytest.raises(TimeoutError):
                driver.execute(["hung"], inputs={"x": 1, "gate": hang}, timeout=0.2)
            assert hang[0].is_set()
        start = time.monotonic()
        assert driver.execute(["quick"], inputs={"x": 1}) == {"quick": 2}
        assert driver.execute(["quick"], inputs={"x": 2}, timeout=2) == {"quick": 3}
        assert time.monotonic() - start < SLOW
    finally:
        for hang in gates:
            hang[1].set()


def test_timeout_without_with_threads_is_a_type_error() -> None:
    """Without a pool the caller can't abandon a node it runs; bad values refused."""
    hook = Recorded()
    for driver in (
        nodes("stop_plain", None),
        nodes("stop_plain_hooked", None, hooks=(hook,)),
        fnloom.Builder()
        .with_modules(make_module("stop_plain_limited", NODES))
        .with_limits({})
        .build(),
    ):
        with pytest.raises(TypeError, match=r"timeout= needs Builder\.with_threads"):
            driver.execute(["quick"], inputs={"x": 1}, timeout=1)
    assert hook.events == []
    threaded = nodes("stop_bad_timeouts")
    for bad, error in (
        (True, TypeError),
        ("1", TypeError),
        (-1, ValueError),
        (float("nan"), ValueError),
        (float("inf"), ValueError),
    ):
        with pytest.raises(error, match="timeout must be"):
            threaded.execute(["quick"], inputs={"x": 1}, timeout=bad)  # ty: ignore[invalid-argument-type]  # refused on purpose


def test_cancelled_is_false_outside_a_stopping_run() -> None:
    """Outside a run, and in a run going on, with or without threads."""
    assert fnloom.cancelled() is False
    for driver in (nodes("stop_asked"), nodes("stop_asked_plain", None)):
        assert driver.execute(["asked"], inputs={"x": 1}) == {"asked": False}


def test_cancelled_turns_true_on_a_deadline() -> None:
    """A node polling `cancelled()` returns early once its run's deadline passed."""
    driver = nodes("stop_cancel_deadline")
    poll, seen = gate(), []
    with pytest.raises(TimeoutError):
        driver.execute(
            ["polled"], inputs={"x": 1, "gate": poll, "seen": seen}, timeout=0.2
        )
    assert poll[2].wait(PROMPT)
    assert seen == ["cancelled"]


def test_cancelled_turns_true_on_a_siblings_failure() -> None:
    """The run waits for the running node, which sees the failure and returns."""
    driver = nodes("stop_cancel_failure")
    poll, seen = gate(), []
    start = time.monotonic()
    with pytest.raises(ValueError, match="1"):
        driver.execute(
            ["polled", "failing"], inputs={"x": 1, "gate": poll, "seen": seen}
        )
    assert time.monotonic() - start < SLOW
    assert poll[2].is_set()
    assert seen == ["cancelled"]


def test_cancelled_turns_true_on_an_interrupt() -> None:
    """`Ctrl-C` while the caller waits: the node on the pool sees it, and returns."""
    driver = nodes("stop_cancel_interrupt")
    poll, seen = gate(), []
    poke = started(lambda: poll[0].wait(5) and _thread.interrupt_main())
    # A deadline keeps the node off the calling thread, so the interrupt reaches the
    # caller's wait.
    with pytest.raises(KeyboardInterrupt):
        driver.execute(
            ["polled"], inputs={"x": 1, "gate": poll, "seen": seen}, timeout=30
        )
    joined(poke)
    assert poll[2].wait(PROMPT)
    assert seen == ["cancelled"]


# A fan-out whose branches hang on a pool thread and return at once on the caller's.
FAN_HUNG = """
import threading
import time

from fnloom import Collect, Parallelizable

STARTED = threading.Event()
RELEASE = threading.Event()


def picked(n: int) -> Parallelizable[int]:
    yield from range(n)


def details(picked: int) -> int:
    if threading.current_thread() is threading.main_thread():
        time.sleep(0.05)
        return picked
    STARTED.set()
    RELEASE.wait(10)
    return picked


def summary(details: Collect[int]) -> list[int]:
    return list(details)
"""


def test_interrupt_main_stops_a_parallel_run_with_a_node_hung_on_a_worker() -> None:
    """`Ctrl-C` ends the run within the prompt, abandoning the node a worker runs."""
    module = make_module("stop_interrupt", FAN_HUNG)
    driver = fnloom.Builder().with_modules(module).with_threads(2).build()
    poke = started(lambda: module.STARTED.wait(5) and _thread.interrupt_main())
    try:
        with pytest.raises(KeyboardInterrupt):
            driver.execute(["summary"], inputs={"n": 8})
        joined(poke)
        assert module.STARTED.is_set()
        # The abandoned branch's worker slot is free: a new run takes it at once.
        start = time.monotonic()
        assert driver.execute(["summary"], inputs={"n": 0}) == {"summary": []}
        assert time.monotonic() - start < PROMPT
    finally:
        module.RELEASE.set()


def test_interrupt_ends_a_broken_run_waiting_for_a_hung_node() -> None:
    """A run whose scheduler lock a panic poisoned still ends on `Ctrl-C`, hooked."""
    module = make_module("stop_broken_interrupt", FAN_HUNG)
    hook = Recorded()
    driver = (
        fnloom.Builder().with_modules(module).with_hooks(hook).with_threads(2).build()
    )

    def poke() -> None:
        assert module.STARTED.wait(5)
        _core.arm_state_panic()
        # Long enough for the caller to step into the poisoned lock, then wait.
        time.sleep(0.5)
        _thread.interrupt_main()

    thread = started(poke)
    try:
        start = time.monotonic()
        with pytest.raises(KeyboardInterrupt):
            driver.execute(["summary"], inputs={"n": 8})
        assert time.monotonic() - start < SLOW
        joined(thread)
        assert hook.events[-1] == ("run", False, "KeyboardInterrupt")
    finally:
        module.RELEASE.set()


# A run inside a node of a run with a deadline: `poll` waits for `cancelled()`, and
# `after` runs only if the inner run goes on.
CHAINED = """
import time

import fnloom

INNER = []
SEEN = []
AFTER = []


def poll(x: int) -> int:
    start = time.monotonic()
    while time.monotonic() - start < 5:
        if fnloom.cancelled():
            SEEN.append(time.monotonic() - start)
            return -1
        time.sleep(0.01)
    return x


def after(poll: int) -> int:
    AFTER.append(poll)
    return poll


def outer(x: int) -> int:
    try:
        return INNER[0].execute(["after"], inputs={"x": x})["after"]
    except Exception as error:
        RAISED.append(type(error))
        raise


RAISED = []
"""


@pytest.mark.parametrize("inner", ["same driver", "other driver", "sequential"])
def test_a_run_inside_a_stopping_run_sees_it_cancelled(inner: str) -> None:
    """Its nodes see `cancelled()` at the outer deadline; a parallel one stops too."""
    module = make_module(f"stop_chained_{inner.replace(' ', '_')}", CHAINED)
    driver = fnloom.Builder().with_modules(module).with_threads(2).build()
    builder = fnloom.Builder().with_modules(module)
    module.INNER.append(
        {
            "same driver": driver,
            "other driver": builder.with_threads(2).build(),
            "sequential": builder.build(),
        }[inner]
    )
    with pytest.raises(TimeoutError):
        driver.execute(["outer"], inputs={"x": 1}, timeout=0.3)
    seen = settled(lambda: len(module.SEEN), 1)
    assert seen == 1
    assert module.SEEN[0] < PROMPT
    time.sleep(0.2)
    # A sequential inner run goes on to `after`; a parallel one stopped.
    went_on = module.AFTER
    assert went_on == ([-1] if inner == "sequential" else [])
    # A parallel one, missing `after`, raised `StoppedError`, a `RuntimeError`.
    raised = module.RAISED
    assert raised == ([] if inner == "sequential" else [_core.StoppedError])
    assert issubclass(_core.StoppedError, RuntimeError)


# Branch 0, first in key order, runs a run of its own; branch 1 fails meanwhile.
# `slow` sleeps through the stop; `finishing` returns its value as it sees it.
MASKED = """
import time

import fnloom
from fnloom import Collect, Parallelizable

INNER = []
OUTPUT = ["slow"]
RETURNED = []


def slow(x: int) -> int:
    time.sleep(0.4)
    return x


def finishing(x: int) -> int:
    start = time.monotonic()
    while not fnloom.cancelled() and time.monotonic() - start < 5:
        time.sleep(0.001)
    return x


def picked(n: int) -> Parallelizable[int]:
    yield from range(n)


def detail(picked: int) -> int:
    if picked == 0:
        value = INNER[0].execute(OUTPUT, inputs={"x": 7})[OUTPUT[0]]
        RETURNED.append(value)
        return value
    time.sleep(0.05)
    raise ValueError(f"the real failure, in branch {picked}")


def summary(detail: Collect[int]) -> list[int]:
    return list(detail)
"""


@pytest.mark.parametrize("inner", ["threaded", "sequential"])
def test_a_stopped_inner_run_never_hides_the_failure_that_stopped_it(
    inner: str,
) -> None:
    """The later branch's `ValueError` is raised, not the inner run's `StoppedError`."""
    module = make_module(f"stop_masked_{inner}", MASKED)
    builder = fnloom.Builder().with_modules(module)
    module.INNER.append(
        builder.with_threads(2).build() if inner == "threaded" else builder.build()
    )
    driver = builder.with_threads(4).build()
    for _ in range(3):
        with pytest.raises(ValueError, match="the real failure, in branch 1") as raised:
            driver.execute(["summary"], inputs={"n": 2})
        assert raised.value.__notes__ == [
            "while computing 'detail' for branch (1,) of 'picked'"
        ]


def test_an_inner_run_that_finished_before_its_stop_returns_its_values() -> None:
    """Every node done when its next step sees the stop: it returns, failing nothing."""
    module = make_module("stop_finished", MASKED)
    module.OUTPUT[:] = ["finishing"]
    builder = fnloom.Builder().with_modules(module)
    # One worker: the inner caller runs the node, so no other thread steps meanwhile.
    module.INNER.append(builder.with_threads(1).build())
    driver = builder.with_threads(4).build()
    with pytest.raises(ValueError, match="the real failure, in branch 1"):
        driver.execute(["summary"], inputs={"n": 2})
    returned = module.RETURNED
    assert returned == [7]


# Three levels: `top` runs the middle driver, whose `mid` runs the inner one; `boom`
# fails the top run, which stops the two below it.
LAYERED = """
import time

import fnloom

DRIVERS = {}
SEEN = []


def poll(x: int) -> int:
    start = time.monotonic()
    while not fnloom.cancelled() and time.monotonic() - start < 5:
        time.sleep(0.01)
    return -1


def after(poll: int) -> int:
    return poll


def mid(x: int) -> int:
    return DRIVERS["inner"].execute(["after"], inputs={"x": x})["after"]


def top(x: int) -> int:
    try:
        return DRIVERS["middle"](x)
    except BaseException as error:
        SEEN.append(error)
        raise


def boom(x: int) -> int:
    time.sleep(0.1)
    raise ValueError("boom")
"""


class _RaisingAfterMid(_core.ObserverBase):
    """A core observer whose `post_node` of `mid` raises."""

    @override
    def post_node(
        self,
        node: str,
        *,
        ok: bool,
        value: object,
        error: BaseException | None,
        branch: tuple[int, ...] = (),
    ) -> None:
        if node == "mid":
            msg = "the observer's own error"
            raise LookupError(msg)


@pytest.mark.parametrize("observer", ["hook", "core observer"])
def test_a_stopped_nodes_observer_errors_are_raised_with_its_stop(
    observer: str,
) -> None:
    """The middle run raises its node's `StoppedError` with what its observer raised."""

    class Raising(fnloom.Hook):
        @override
        def after_node(self, event: fnloom.NodeEnd) -> None:
            if event.node.name == "mid":
                msg = "the hook's own error"
                raise LookupError(msg)

    module = make_module(f"stop_layered_{observer.replace(' ', '_')}", LAYERED)
    builder = fnloom.Builder().with_modules(module).with_threads(2)
    module.DRIVERS["inner"] = builder.build()
    if observer == "hook":
        hooked = builder.with_hooks(Raising()).build()
        module.DRIVERS["middle"] = lambda x: hooked.execute(["mid"], inputs={"x": x})[
            "mid"
        ]
    else:
        # The middle plan run directly, so its observer's errors go through the core.
        runnable = builder.build().prepare(["mid"], inputs={"x": 1})._plan  # noqa: SLF001  # the native plan and its pool
        native, workers = runnable._plan, runnable._workers  # noqa: SLF001  # ty: ignore[unresolved-attribute]
        module.DRIVERS["middle"] = lambda x: native.run(
            {"x": x},
            {},
            [_RaisingAfterMid()],
            threads=workers.threads(),
            limits=workers.limits(),
        )["mid"]
    with pytest.raises(ValueError, match="boom"):
        builder.build().execute(["top", "boom"], inputs={"x": 1})
    (raised,) = module.SEEN
    assert isinstance(raised, BaseExceptionGroup)
    kinds = sorted(type(error).__name__ for error in raised.exceptions)
    assert kinds == ["LookupError", "StoppedError"]


def test_a_store_after_a_hook_the_deadline_outlasted_is_dropped() -> None:
    """A node whose `after_node` hook runs past the deadline stores nothing."""

    class Slow(fnloom.Hook):
        @override
        def after_node(self, event: fnloom.NodeEnd) -> None:
            time.sleep(0.5)

    class Puts(fnloom.MemoryCache):
        def __init__(self) -> None:
            super().__init__()
            self.puts: list[bytes] = []

        @override
        def put(self, key: bytes, value: object) -> None:
            self.puts.append(key)
            super().put(key, value)

    store = Puts()
    driver = (
        fnloom.Builder()
        .with_modules(make_module("stop_late_store", NODES))
        .with_hooks(Slow())
        .with_cache(store)
        .with_threads(2)
        .build()
    )
    with pytest.raises(TimeoutError):
        driver.execute(["quick"], inputs={"x": 1}, timeout=0.2)
    time.sleep(0.2)
    assert store.puts == []


# A generator whose second pull runs while the first branch fails.
PULLING = """
import threading
import time

from fnloom import Collect, Parallelizable

IN_PULL = threading.Event()
EVENTS = []


def picked(n: int) -> Parallelizable[int]:
    try:
        yield 0
        IN_PULL.set()
        time.sleep(0.3)
        EVENTS.append("pulled")
        yield 1
        yield 2
    except GeneratorExit:
        EVENTS.append("closed")
        raise


def details(picked: int) -> int:
    if picked == 0:
        IN_PULL.wait(5)
        raise ValueError("item 0")
    return picked


def summary(details: Collect[int]) -> list[int]:
    return list(details)
"""


def test_a_failure_during_a_pull_closes_the_generator_after_it() -> None:
    """Review Focus 1: closed once, after its pull returns; the node's error raised."""
    module = make_module("stop_pulling", PULLING)
    driver = fnloom.Builder().with_modules(module).with_threads(2).build()
    with pytest.raises(ValueError, match="item 0"):
        driver.execute(["summary"], inputs={"n": 3})
    assert module.EVENTS == ["pulled", "closed"]


# A generator over many items whose branches hang, recording the thread that closed it.
CLOSING = """
import os
import threading

from fnloom import Collect, Parallelizable

CLOSED = []
RELEASE = threading.Event()
FORKS = []
FORKED = []


def picked(n: int) -> Parallelizable[int]:
    try:
        yield from range(n)
    finally:
        CLOSED.append(threading.get_ident())
        if FORKS and not FORKED:
            FORKED.append(os.fork())


def details(picked: int) -> int:
    RELEASE.wait(10)
    return picked


def summary(details: Collect[int]) -> list[int]:
    return list(details)
"""


def test_a_generator_is_closed_on_timeout_by_the_caller_when_no_pull_is_in_flight() -> (
    None
):
    """With no pull in flight at the deadline, the caller closes it before raising."""
    module = make_module("stop_closing", CLOSING)
    driver = fnloom.Builder().with_modules(module).with_threads(1).build()
    try:
        with pytest.raises(TimeoutError):
            driver.execute(["summary"], inputs={"n": 100}, timeout=0.3)
        closers = module.CLOSED
        assert closers == [threading.get_ident()]
    finally:
        module.RELEASE.set()


def test_a_nested_run_with_a_deadline_uses_its_callers_lent_slot() -> None:
    """Under `with_threads(1)` a node's deadline run of ten nodes completes."""
    reads = ["x"] + [f"c{i}" for i in range(9)]
    chain = "\n".join(
        f"def c{i}({read}: int) -> int:\n    return {read} + 1\n"
        for i, read in enumerate(reads)
    )
    source = f"""
DRIVER = []


{chain}

def outer(x: int, inner: float) -> int:
    return DRIVER[0].execute(["c9"], inputs={{"x": x}}, timeout=inner)["c9"]
"""
    module = make_module("stop_lent", source)
    driver = fnloom.Builder().with_modules(module).with_threads(1).build()
    module.DRIVER.append(driver)
    inputs = {"x": 1, "inner": 5.0}
    assert driver.execute(["outer"], inputs=inputs) == {"outer": 11}
    # The outer run's deadline puts its node on a pool thread, which lends its slot too.
    assert driver.execute(["outer"], inputs=inputs, timeout=10) == {"outer": 11}
    assert settled(lambda: held(driver), (0, {}, {})) == (0, {}, {})


# A node that runs its own driver from three threads carrying its context.
LENDING = """
import contextvars
import threading
import time

DRIVER = []
LOCK = threading.Lock()
NOW = [0]
MOST = [0]


def inner(x: int) -> int:
    with LOCK:
        NOW[0] += 1
        MOST[0] = max(MOST[0], NOW[0])
    time.sleep(0.05)
    with LOCK:
        NOW[0] -= 1
    return x


def outer(n: int) -> list[int]:
    results = []

    def run(i):
        results.append(DRIVER[0].execute(["inner"], inputs={"x": i})["inner"])

    threads = [
        threading.Thread(target=run, args=(i,), context=contextvars.copy_context())
        for i in range(n)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    return sorted(results)
"""


def test_a_task_lends_its_worker_slot_to_one_inner_run_at_a_time() -> None:
    """Three inner runs on threads a node starts take its slot in turn, not at once."""
    module = make_module("stop_lending", LENDING)
    driver = fnloom.Builder().with_modules(module).with_threads(1).build()
    module.DRIVER.append(driver)
    assert driver.execute(["outer"], inputs={"n": 3}) == {"outer": [0, 1, 2]}
    assert module.MOST == [1]


def test_the_interpreter_exits_cleanly_with_an_abandoned_task() -> None:
    """Review Focus 4: a process whose run abandoned a hung node exits with status 0."""
    script = textwrap.dedent(
        """
        import sys, types
        import fnloom
        source = '''
        import threading
        FOREVER = threading.Event()
        def hung(x: int) -> int:
            FOREVER.wait()
            return x
        '''
        module = types.ModuleType("hung_at_exit")
        sys.modules["hung_at_exit"] = module
        exec(source.replace("\\n        ", "\\n"), module.__dict__)
        driver = fnloom.Builder().with_modules(module).with_threads(2).build()
        try:
            driver.execute(["hung"], inputs={"x": 1}, timeout=0.2)
        except TimeoutError:
            print("timed out")
        """
    )
    start = time.monotonic()
    done = subprocess.run(  # noqa: S603  # this interpreter, on a script of ours
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=EXIT,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "timed out"
    assert time.monotonic() - start < EXIT


def test_a_collected_driver_stops_its_threads() -> None:
    """Every pool thread ends once the driver is collected, the abandoned one too."""
    before = settled_workers()
    driver = nodes("stop_collected", 3)
    hang = gate()
    with pytest.raises(TimeoutError):
        driver.execute(["hung"], inputs={"x": 1, "gate": hang}, timeout=0.2)
    assert _core.live_workers() > before
    del driver
    gc.collect()
    hang[1].set()
    assert hang[2].wait(5)
    assert settled(_core.live_workers, before) == before


def settled_workers() -> int:
    """`live_workers()`, once pools that are closing have let their threads go."""
    gc.collect()
    count = _core.live_workers()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        time.sleep(0.05)
        now = _core.live_workers()
        if now == count:
            return now
        count = now
    return count


def _child(work: Callable[[], object]) -> NoReturn:
    """Runs `work` in a forked child and exits it: 0 when `work` returns `True`."""
    try:
        ok = bool(work())
    except BaseException:  # noqa: BLE001  # the child reports through its status
        ok = False
    os._exit(0 if ok else 1)


def _waited(pid: int) -> int:
    """The forked child `pid`'s exit status, waiting at most ten seconds."""
    assert sys.platform != "win32", "only where os.fork is"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        done, status = os.waitpid(pid, os.WNOHANG)
        if done:
            return os.waitstatus_to_exitcode(status)
        time.sleep(0.02)
    os.kill(pid, 9)
    pytest.fail("the forked child did not exit")


forks = pytest.mark.skipif(not hasattr(os, "fork"), reason="no os.fork on this system")
multithreaded_fork = pytest.mark.filterwarnings(
    "ignore:This process .* is multi-threaded:DeprecationWarning"
)


@forks
@multithreaded_fork
def test_a_fork_while_the_caller_closes_a_stopped_run_raises_in_the_child() -> None:
    """A generator forking as the caller closes it at the deadline: the child raises."""
    module = make_module("stop_fork_closing", CLOSING)
    module.FORKS.append(True)
    driver = fnloom.Builder().with_modules(module).with_threads(1).build()
    parent = os.getpid()
    try:
        driver.execute(["summary"], inputs={"n": 100}, timeout=0.3)
    except RuntimeError as error:
        if os.getpid() == parent:
            raise
        message = str(error)
        _child(lambda: "a run doesn't survive os.fork()" in message)
    except TimeoutError:
        pass
    else:
        pytest.fail("the run did not time out")
    module.RELEASE.set()
    (child,) = module.FORKED
    assert _waited(child) == 0


class Forks(fnloom.Hook):
    """Forks once, in `before_node` of the node named."""

    def __init__(self, node: str) -> None:
        """Forks before `node` runs."""
        self.node = node
        self.forked: list[int] = []

    @override
    def before_node(self, event: fnloom.NodeStart) -> None:
        assert sys.platform != "win32", "only where os.fork is"
        if event.node.name == self.node and not self.forked:
            self.forked.append(os.fork())


@forks
@multithreaded_fork
def test_a_fork_inside_a_before_node_hook_raises_in_the_child() -> None:
    """The limited node's slots are not retaken in the child; its run raises there."""
    hook = Forks("guarded")
    driver = nodes("stop_fork_hook", 1, hooks=(hook,))
    free = gate()
    free[1].set()
    parent = os.getpid()
    try:
        result = driver.execute(["guarded"], inputs={"x": 7, "gate": free})
    except RuntimeError as error:
        if os.getpid() == parent:
            raise
        message = str(error)
        _child(lambda: "a run doesn't survive os.fork()" in message)
    assert result == {"guarded": 7}
    (child,) = hook.forked
    assert _waited(child) == 0


def test_cancelled_is_public() -> None:
    """`cancelled` is a public name, and reads a context variable of the run."""
    assert "cancelled" in fnloom.__all__
    assert contextvars.copy_context().run(fnloom.cancelled) is False
