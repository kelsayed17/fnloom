"""`PreparedPlan.run_many`: one request over many input sets (task 10, Q171).

Every wait here is bounded: barriers, events and joins take a timeout, and the
`threads` watchdog in `conftest.py` is the last resort. A node that hangs waits on
an event the test sets at its end.
"""

import threading
import time
from typing import TYPE_CHECKING, override

import pytest

import fnloom
from fnloom import _core, _threads
from helpers import make_module

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import ModuleType

pytestmark = pytest.mark.threads

# How long a stop may take to reach the caller: well above the 50 ms slices.
PROMPT = 1.0
WAIT = 5.0
# How many sets the counting tests run, and on how many workers.
SETS = 20
WORKERS = 2

# A small graph each test drives with its own gates. `TOOLS` is a dict the test
# fills: `meet` a barrier, `gate` an event, `calls` a list, `lock` a lock.
NODES = """
import threading
import time

import fnloom
from fnloom import Collect, Parallelizable

TOOLS = {}


def doubled(x: int, pause: float) -> int:
    time.sleep(pause)
    TOOLS.setdefault("calls", []).append((x, threading.get_ident()))
    return 2 * x


def met(x: int) -> int:
    TOOLS["meet"].wait()
    return x


def stepped(x: int) -> int:
    if x == TOOLS.get("late"):
        TOOLS["before"].wait(5)
        time.sleep(0.05)
        raise ValueError(x)
    if x in TOOLS.get("fails", ()):
        if "started" in TOOLS:
            TOOLS["started"].wait(5)
        if "before" in TOOLS:
            TOOLS["before"].set()
        raise ValueError(x)
    if "started" in TOOLS:
        TOOLS["started"].set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if fnloom.cancelled():
            TOOLS.setdefault("seen", []).append(x)
            return -1
        if "gate" in TOOLS and TOOLS["gate"].is_set():
            return x
        time.sleep(0.005)
    return x


def first(x: int) -> int:
    if x == 0:
        TOOLS["failed"].wait(5)
    return x


def second(first: int) -> int:
    if first == 1:
        TOOLS["failed"].set()
        raise ValueError(first)
    return first


def hung(x: int) -> int:
    TOOLS["gate"].wait(10)
    return x


def interrupting(x: int) -> int:
    if x == 1:
        import _thread

        _thread.interrupt_main()
        time.sleep(1)
        return x
    # Polled, not waited on: a sleep takes the interrupt on every system.
    deadline = time.monotonic() + 10
    while not TOOLS["gate"].is_set() and time.monotonic() < deadline:
        time.sleep(0.01)
    return x


def counted(x: int) -> int:
    with TOOLS["lock"]:
        TOOLS["now"] = TOOLS.get("now", 0) + 1
        TOOLS["most"] = max(TOOLS.get("most", 0), TOOLS["now"])
    time.sleep(0.01)
    with TOOLS["lock"]:
        TOOLS["now"] -= 1
    return x


def threads_seen(x: int) -> int:
    from fnloom import _core

    with TOOLS["lock"]:
        TOOLS["pool"] = max(TOOLS.get("pool", 0), _core.live_workers())
        TOOLS["all"] = max(TOOLS.get("all", 0), threading.active_count())
    return x


def picked(x: int) -> Parallelizable[int]:
    yield from range(x)


def squared(picked: int) -> int:
    return picked * picked


def summed(squared: Collect[int]) -> int:
    return sum(squared)
"""

LIMITED = """
import threading
import time

from fnloom import limit

TOOLS = {}


def _alone(lock: threading.Lock) -> None:
    assert lock.acquire(blocking=False), "two calls at once"
    try:
        time.sleep(0.005)
    finally:
        lock.release()


@limit(1)
def capped(x: int) -> int:
    _alone(TOOLS["capped"])
    return x


@limit(group="shared")
def grouped(x: int) -> int:
    _alone(TOOLS["grouped"])
    return x


def both(capped: int, grouped: int) -> int:
    return capped + grouped
"""


# A cached node: its version is given, since it reads the module's `TOOLS`.
CACHED = """
import threading

import fnloom

TOOLS = {"calls": [], "meet": None}


@fnloom.cache(version="1")
def doubled(x: int) -> int:
    TOOLS["calls"].append(x)
    if TOOLS["meet"] is not None:
        TOOLS["meet"].wait()
    return 2 * x
"""


def module(name: str, source: str = NODES) -> ModuleType:
    """The test's nodes, as a fresh module."""
    return make_module(name, source)


def driver(
    nodes: ModuleType,
    threads: int | None,
    *,
    hooks: tuple[fnloom.Hook, ...] = (),
) -> fnloom.Driver:
    """`nodes`' driver, `with_threads(threads)` when given."""
    builder = fnloom.Builder().with_modules(nodes).with_hooks(*hooks)
    if threads is not None:
        builder = builder.with_threads(threads)
    return builder.build()


def sets(*xs: int) -> list[dict[str, object]]:
    """One input set per `x`."""
    return [{"x": x} for x in xs]


class Runs(fnloom.Hook):
    """Records each run's start and end, and how many were open at once."""

    def __init__(self) -> None:
        """No run seen yet."""
        self.lock = threading.Lock()
        self.starts: dict[str, dict[str, object]] = {}
        self.ends: dict[str, fnloom.RunEnd] = {}
        self.nodes: list[tuple[str, str]] = []
        self.open = 0
        self.most = 0

    @override
    def before_run(self, event: fnloom.RunStart) -> None:
        with self.lock:
            self.starts[event.run_id] = dict(event.inputs)
            self.open += 1
            self.most = max(self.most, self.open)

    @override
    def after_node(self, event: fnloom.NodeEnd) -> None:
        with self.lock:
            self.nodes.append((event.run_id, event.node.name))

    @override
    def after_run(self, event: fnloom.RunEnd) -> None:
        with self.lock:
            self.ends[event.run_id] = event
            self.open -= 1

    def opened(self) -> list[int]:
        """The `x` of every set that fired `before_run`, sorted."""
        return sorted(int(inputs["x"]) for inputs in self.starts.values())  # ty: ignore[invalid-argument-type]  # every `x` is an int


@pytest.mark.parametrize("threads", [None, 1, 4])
def test_run_many_returns_one_result_per_set_in_order(threads: int | None) -> None:
    """Sets finishing in reverse still come back in input order."""
    nodes = module("rm_order")
    plan = driver(nodes, threads).prepare(["doubled"], inputs={"x": 0, "pause": 0.0})
    given = [{"x": x, "pause": (8 - x) * 0.01} for x in range(8)]
    assert plan.run_many(given) == [{"doubled": 2 * x} for x in range(8)]


@pytest.mark.parametrize("threads", [None, 1, 2, 8])
def test_run_many_equals_a_loop_of_run(threads: int | None) -> None:
    """Over a fan-out, each set's result is what `run` gives it, at any worker count."""
    plan = driver(module("rm_loop"), threads).prepare(["summed"], inputs={"x": 0})
    given = sets(*range(30))
    assert plan.run_many(given) == [plan.run(inputs=inputs) for inputs in given]


def test_run_many_overlaps_sets_on_the_pool() -> None:
    """Four sets meet at a barrier of four, so they run at once."""
    nodes = module("rm_overlap")
    nodes.TOOLS["meet"] = threading.Barrier(4, timeout=WAIT)
    plan = driver(nodes, 4).prepare(["met"], inputs={"x": 0})
    assert plan.run_many(sets(0, 1, 2, 3)) == [{"met": x} for x in range(4)]


def test_run_many_opens_at_most_max_workers_sets() -> None:
    """Under `with_threads(2)`, two of twenty sets are open at once, never more."""
    nodes = module("rm_most")
    nodes.TOOLS["meet"] = threading.Barrier(WORKERS, timeout=WAIT)
    runs = Runs()
    plan = driver(nodes, WORKERS, hooks=(runs,)).prepare(["met"], inputs={"x": 0})
    assert plan.run_many(sets(*range(SETS))) == [{"met": x} for x in range(SETS)]
    assert runs.most == WORKERS
    assert len(runs.starts) == len(runs.ends) == SETS


def test_run_many_without_threads_runs_sets_in_order() -> None:
    """Without `with_threads`, the sets run one after another on the caller."""
    nodes = module("rm_sequential")
    plan = driver(nodes, None).prepare(["doubled"], inputs={"x": 0, "pause": 0.0})
    given = [{"x": x, "pause": 0.0} for x in range(5)]
    assert plan.run_many(given) == [{"doubled": 2 * x} for x in range(5)]
    assert nodes.TOOLS["calls"] == [(x, threading.get_ident()) for x in range(5)]


def test_run_many_checks_every_set_before_running_any() -> None:
    """A set that fails its checks raises, noted with its index, and nothing runs."""
    nodes = module("rm_checks")
    runs = Runs()
    plan = driver(nodes, 2, hooks=(runs,)).prepare(
        ["doubled"], inputs={"x": 0, "pause": 0.0}
    )
    good = {"x": 1, "pause": 0.0}
    with pytest.raises(ValueError, match="pause: expected float") as raised:
        plan.run_many([good, good, {"x": 1, "pause": "slow"}, good])
    assert raised.value.__notes__ == ["in input set 2 of run_many"]
    with pytest.raises(ValueError, match="differ from the prepared") as raised:
        plan.run_many([good, {"x": 1, "pause": 0.0, "extra": 1}])
    assert raised.value.__notes__ == ["in input set 1 of run_many"]
    assert "calls" not in nodes.TOOLS
    assert runs.starts == {}


def test_run_many_refuses_a_mapping_and_takes_an_empty_batch() -> None:
    """One mapping is a mistake for a list of them; no set gives no result."""
    plan = driver(module("rm_shapes"), 2).prepare(["met"], inputs={"x": 0})
    with pytest.raises(TypeError, match="iterable of input mappings"):
        plan.run_many({"x": 1})  # ty: ignore[invalid-argument-type]  # the mistake tested
    assert plan.run_many([]) == []
    assert plan.run_many(iter(sets())) == []


def test_a_failing_set_raises_with_its_index_and_stops_the_others() -> None:
    """Set 1 fails: set 0 sees `cancelled()`, and no later set ever opens."""
    nodes = module("rm_fails")
    nodes.TOOLS.update(fails={1}, started=threading.Event())
    runs = Runs()
    plan = driver(nodes, 2, hooks=(runs,)).prepare(["stepped"], inputs={"x": 0})
    with pytest.raises(ValueError, match="1") as raised:
        plan.run_many(sets(*range(10)))
    assert raised.value.__notes__[-1] == "while running input set 1 of run_many"
    assert nodes.TOOLS["seen"] == [0]
    # Sets that never opened fire no run events; every set that opened ended.
    assert runs.opened() == [0, 1]
    assert runs.ends.keys() == runs.starts.keys()
    # Set 0's node returned as it was stopped. Its run either kept the value, or had
    # stopped first and dropped it, raising `StoppedError`, which never takes the
    # place of set 1's error.
    errors = sorted(type(end.error).__name__ for end in runs.ends.values())
    assert errors in (["NoneType", "ValueError"], ["StoppedError", "ValueError"])


def test_sets_without_threads_stop_at_the_first_failure() -> None:
    """Without `with_threads`, the failing set is the last to run."""
    nodes = module("rm_fails_sequential")
    nodes.TOOLS["fails"] = {2}
    nodes.TOOLS["gate"] = threading.Event()
    nodes.TOOLS["gate"].set()
    runs = Runs()
    plan = driver(nodes, None, hooks=(runs,)).prepare(["stepped"], inputs={"x": 0})
    with pytest.raises(ValueError, match="2") as raised:
        plan.run_many(sets(*range(6)))
    assert raised.value.__notes__[-1] == "while running input set 2 of run_many"
    assert runs.opened() == [0, 1, 2]


def test_the_lowest_indexed_failure_leads_though_a_later_one_came_first() -> None:
    """Set 3 fails first, then set 1, which was open: set 1's error is raised."""
    nodes = module("rm_lowest")
    nodes.TOOLS.update(fails={3}, late=1, before=threading.Event())
    plan = driver(nodes, 4).prepare(["stepped"], inputs={"x": 0})
    with pytest.raises(ValueError, match="1") as raised:
        plan.run_many(sets(0, 1, 2, 3))
    assert raised.value.args == (1,)
    assert raised.value.__notes__[-1] == "while running input set 1 of run_many"


def test_a_stopped_set_never_hides_the_failure_that_stopped_it() -> None:
    """Set 0, stopped before its last node, raises `StoppedError`; set 1's leads."""
    nodes = module("rm_stopped")
    nodes.TOOLS["failed"] = threading.Event()
    plan = driver(nodes, 2).prepare(["second"], inputs={"x": 0})
    with pytest.raises(ValueError, match="1") as raised:
        plan.run_many(sets(0, 1))
    assert not isinstance(raised.value, _core.StoppedError)
    assert raised.value.__notes__[-1] == "while running input set 1 of run_many"


def test_a_stopped_sets_hook_errors_never_hide_the_failure() -> None:
    """A hook raising on every failed run groups set 0's stop with its error.

    That group still only says set 0 was stopped: set 1's failure leads.
    """

    class OnFailure(fnloom.Hook):
        @override
        def after_run(self, event: fnloom.RunEnd) -> None:
            if not event.ok:
                raise LookupError(event.inputs["x"])

    nodes = module("rm_stopped_hooked")
    nodes.TOOLS["failed"] = threading.Event()
    plan = driver(nodes, 2, hooks=(OnFailure(),)).prepare(["second"], inputs={"x": 0})
    with pytest.raises(BaseExceptionGroup) as raised:
        plan.run_many(sets(0, 1))
    lead = raised.value.exceptions[0]
    assert isinstance(lead, ValueError)
    assert lead.args == (1,)
    assert raised.value.__notes__[-1] == "while running input set 1 of run_many"


def test_each_set_is_copied_before_it_is_checked() -> None:
    """An iterable that hands out one dict, changed for each set, runs every set."""
    nodes = module("rm_reused")
    plan = driver(nodes, 2).prepare(["doubled"], inputs={"x": 0, "pause": 0.0})

    def reused() -> Iterator[dict[str, object]]:
        given: dict[str, object] = {"pause": 0.0}
        for x in range(4):
            given["x"] = x
            yield given

    assert plan.run_many(reused()) == [{"doubled": 2 * x} for x in range(4)]


def test_each_set_fires_its_own_run_events() -> None:
    """Each set is its own run: one `run_id`, one start, one end, its own nodes."""
    nodes = module("rm_events")
    runs = Runs()
    plan = driver(nodes, 2, hooks=(runs,)).prepare(["summed"], inputs={"x": 0})
    assert plan.run_many(sets(1, 2, 3, 4)) == [{"summed": n} for n in (0, 1, 5, 14)]
    assert runs.opened() == [1, 2, 3, 4]
    assert runs.ends.keys() == runs.starts.keys()
    assert all(end.ok for end in runs.ends.values())
    for run_id, inputs in runs.starts.items():
        squares = [n for r, n in runs.nodes if r == run_id and n == "squared"]
        assert len(squares) == inputs["x"]


def test_two_sets_hooks_may_overlap() -> None:
    """Each set has its own busy flag: two sets' hooks run at once (O18)."""

    class Meeting(fnloom.Hook):
        def __init__(self) -> None:
            self.meet = threading.Barrier(2, timeout=WAIT)

        @override
        def before_node(self, event: fnloom.NodeStart) -> None:
            self.meet.wait()

    nodes = module("rm_hooks")
    nodes.TOOLS["lock"] = threading.Lock()
    plan = driver(nodes, 2, hooks=(Meeting(),)).prepare(["counted"], inputs={"x": 0})
    assert plan.run_many(sets(0, 1)) == [{"counted": 0}, {"counted": 1}]


def test_run_many_through_a_cache_and_a_result_builder() -> None:
    """Each set is cached as `run` caches it, and built by the result builder."""
    nodes = module("rm_cached", CACHED)
    built = (
        fnloom.Builder()
        .with_modules(nodes)
        .with_threads(2)
        .with_cache(fnloom.MemoryCache())
        .with_result_builder(lambda outputs: outputs["doubled"])
        .build()
    )
    plan = built.prepare(["doubled"], inputs={"x": 0})
    assert plan.run_many(sets(*range(6))) == [2 * x for x in range(6)]
    assert plan.run_many(sets(*range(6))) == [2 * x for x in range(6)]
    assert sorted(nodes.TOOLS["calls"]) == list(range(6))


def test_single_flight_is_per_set() -> None:
    """Two open sets that need one key both compute it."""
    nodes = module("rm_flight", CACHED)
    nodes.TOOLS["meet"] = threading.Barrier(2, timeout=WAIT)
    built = (
        fnloom.Builder()
        .with_modules(nodes)
        .with_threads(2)
        .with_cache(fnloom.MemoryCache())
        .build()
    )
    plan = built.prepare(["doubled"], inputs={"x": 0})
    assert plan.run_many(sets(5, 5)) == [{"doubled": 10}, {"doubled": 10}]
    assert nodes.TOOLS["calls"] == [5, 5]
    # Once stored, a later set hits it.
    nodes.TOOLS["meet"] = None
    assert plan.run_many(sets(5)) == [{"doubled": 10}]
    assert nodes.TOOLS["calls"] == [5, 5]


def test_run_many_with_timeout() -> None:
    """The timeout covers every set: open sets are abandoned, later ones never open."""
    nodes = module("rm_timeout")
    nodes.TOOLS["gate"] = threading.Event()
    runs = Runs()
    plan = driver(nodes, 2, hooks=(runs,)).prepare(["hung"], inputs={"x": 0})
    start = time.monotonic()
    try:
        with pytest.raises(TimeoutError) as raised:
            plan.run_many(sets(*range(6)), timeout=0.2)
        assert time.monotonic() - start < 0.2 + PROMPT
    finally:
        nodes.TOOLS["gate"].set()
    assert raised.value.__notes__[-1] == "while running input set 0 of run_many"
    assert runs.opened() == [0, 1]
    assert all(not end.ok for end in runs.ends.values())


def test_a_set_not_open_at_the_deadline_never_opens() -> None:
    """Set 0's `after_run` outlasts the deadline, so set 1 raises without opening."""

    class Slow(Runs):
        @override
        def after_run(self, event: fnloom.RunEnd) -> None:
            super().after_run(event)
            time.sleep(0.3)

    runs = Slow()
    for threads in (1, 2):
        plan = driver(module(f"rm_unopened_{threads}"), threads, hooks=(runs,)).prepare(
            ["doubled"], inputs={"x": 0, "pause": 0.0}
        )
        runs.starts.clear()
        given = [{"x": x, "pause": 0.0} for x in range(threads + 3)]
        with pytest.raises(
            TimeoutError, match=f"input set {threads} and the sets"
        ) as raised:
            plan.run_many(given, timeout=0.2)
        assert runs.opened() == list(range(threads))
        # It never ran, so no note says it did.
        assert not getattr(raised.value, "__notes__", [])


def test_timeout_needs_threads_and_a_good_value() -> None:
    """`timeout=` without `with_threads` is a `TypeError`; a bad one a `ValueError`."""
    plan = driver(module("rm_bad_timeout"), None).prepare(["met"], inputs={"x": 0})
    with pytest.raises(TypeError, match=r"needs Builder\.with_threads"):
        plan.run_many(sets(1), timeout=1)
    threaded = driver(module("rm_bad_timeout_t"), 2).prepare(["met"], inputs={"x": 0})
    with pytest.raises(ValueError, match="at least 0"):
        threaded.run_many(sets(1), timeout=-1)


def test_limits_hold_across_the_sets() -> None:
    """A `limit(1)` node and a group of one never run twice at once over the batch."""
    nodes = make_module("rm_limits", LIMITED)
    nodes.TOOLS.update(capped=threading.Lock(), grouped=threading.Lock())
    built = (
        fnloom.Builder()
        .with_modules(nodes)
        .with_threads(4)
        .with_limits({"shared": 1})
        .build()
    )
    plan = built.prepare(["both"], inputs={"x": 0})
    assert plan.run_many(sets(*range(24))) == [{"both": 2 * x} for x in range(24)]


def test_max_workers_caps_the_nodes_of_every_set_together() -> None:
    """At most `max_workers` nodes of the batch run at once."""
    nodes = module("rm_cap")
    nodes.TOOLS["lock"] = threading.Lock()
    plan = driver(nodes, WORKERS).prepare(["counted"], inputs={"x": 0})
    assert plan.run_many(sets(*range(SETS))) == [{"counted": x} for x in range(SETS)]
    assert nodes.TOOLS["most"] <= WORKERS


def test_threads_stay_bounded_over_a_large_batch() -> None:
    """400 sets on `with_threads(4)`: the pool stays at four, the helpers at three."""
    nodes = module("rm_bounded")
    nodes.TOOLS["lock"] = threading.Lock()
    plan = driver(nodes, 4).prepare(["threads_seen"], inputs={"x": 0})
    # Other tests' pools may still be closing: they only ever shrink these.
    pools, before = _core.live_workers(), threading.active_count()
    assert plan.run_many(sets(*range(400))) == [{"threads_seen": x} for x in range(400)]
    # The pool's three threads, and this call's three helpers.
    assert nodes.TOOLS["pool"] <= pools + 3
    assert nodes.TOOLS["all"] <= before + 3 + 3
    helpers = [t for t in threading.enumerate() if t.name.startswith("fnloom-run-many")]
    for thread in helpers:
        thread.join(WAIT)
    assert not any(thread.is_alive() for thread in helpers)


def test_interrupt_stops_run_many_at_once() -> None:
    """`Ctrl-C` in the caller stops every open set and is raised at once."""
    nodes = module("rm_interrupt")
    nodes.TOOLS["gate"] = threading.Event()
    built = driver(nodes, 2)
    plan = built.prepare(["interrupting"], inputs={"x": 0})
    start = time.monotonic()
    try:
        with pytest.raises(KeyboardInterrupt):
            plan.run_many(sets(0, 1, 2, 3))
        assert time.monotonic() - start < 1 + PROMPT
    finally:
        nodes.TOOLS["gate"].set()
    # Once the other set's node returns, its helper leaves and no slot stays held.
    for thread in threading.enumerate():
        if thread.name.startswith("fnloom-run-many"):
            thread.join(WAIT)
            assert not thread.is_alive()
    workers = built._workers  # noqa: SLF001  # the driver's own limits
    assert workers is not None
    deadline = time.monotonic() + WAIT
    while _core.held_slots(workers.limits())[0] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert _core.held_slots(workers.limits())[0] == 0
    assert plan.run_many(sets(0)) == [{"interrupting": 0}]


def test_run_many_inside_a_node_of_its_own_driver_runs_its_sets_in_turn() -> None:
    """Inside a task of its own driver, the sets share its slot, one at a time."""
    source = """
import threading
import time

TOOLS = {}


def inner(x: int) -> int:
    with TOOLS["lock"]:
        TOOLS["now"] = TOOLS.get("now", 0) + 1
        TOOLS["most"] = max(TOOLS.get("most", 0), TOOLS["now"])
    time.sleep(0.005)
    with TOOLS["lock"]:
        TOOLS["now"] -= 1
    return x


def outer(n: int) -> list:
    plan = TOOLS["plan"]
    return [r["inner"] for r in plan.run_many([{"x": x} for x in range(n)])]
"""
    for threads, timeout in ((1, None), (2, None), (1, WAIT), (2, WAIT)):
        nodes = make_module(f"rm_nested_{threads}_{timeout}", source)
        nodes.TOOLS["lock"] = threading.Lock()
        built = driver(nodes, threads)
        nodes.TOOLS["plan"] = built.prepare(["inner"], inputs={"x": 0})
        result = built.execute(["outer"], inputs={"n": 6}, timeout=timeout)
        assert result == {"outer": list(range(6))}
        assert nodes.TOOLS["most"] == 1


def test_run_many_inside_a_stopping_run_stops() -> None:
    """A node's `run_many` stops with its run, and the run raises its own failure."""
    source = """
import time

TOOLS = {}


def slow(x: int) -> int:
    time.sleep(0.02)
    return x


def batch(n: int) -> int:
    return len(TOOLS["plan"].run_many([{"x": x} for x in range(n)]))


def boom(n: int) -> int:
    time.sleep(0.05)
    raise KeyError(n)
"""
    nodes = make_module("rm_inside_stopping", source)
    built = driver(nodes, 4)
    other = driver(make_module("rm_inside_stopping_inner", source), 2)
    nodes.TOOLS["plan"] = other.prepare(["slow"], inputs={"x": 0})
    with pytest.raises(KeyError):
        built.execute(["batch", "boom"], inputs={"n": 200}, timeout=WAIT)
    # Called where a stopped run's context reaches, it opens no set.
    stop = _core.Stop()
    stop.set()
    token = _threads.CANCELLED.set(stop)
    try:
        with pytest.raises(_core.StoppedError, match="this run_many has stopped"):
            nodes.TOOLS["plan"].run_many(sets(*range(4)))
    finally:
        _threads.CANCELLED.reset(token)


def test_a_panic_in_one_set_fails_the_batch_and_frees_its_slots() -> None:
    """A Rust panic in a set's task fails the batch once, leaking no slot."""
    nodes = module("rm_panic")
    nodes.TOOLS["lock"] = threading.Lock()
    built = driver(nodes, 2)
    plan = built.prepare(["counted"], inputs={"x": 0})
    _core.arm_perform_panic()
    with pytest.raises(BaseException, match="before its completion") as info:
        plan.run_many(sets(*range(6)))
    assert type(info.value).__name__ == "PanicException"
    workers = built._workers  # noqa: SLF001  # the driver's own limits
    assert workers is not None
    assert _core.held_slots(workers.limits())[0] == 0
    assert plan.run_many(sets(1, 2)) == [{"counted": 1}, {"counted": 2}]


def test_a_keyboard_interrupt_is_noted_with_its_set() -> None:
    """An interrupt raised by the caller's own set names that set."""
    nodes = module("rm_interrupt_note")
    plan = driver(nodes, 1).prepare(["interrupting"], inputs={"x": 0})
    with pytest.raises(KeyboardInterrupt) as raised:
        plan.run_many(sets(1))
    assert raised.value.__notes__[-1] == "while running input set 0 of run_many"
