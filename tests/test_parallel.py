"""`Builder.with_threads`: one run's nodes on the driver's pool (Plan 11, task 3).

Every wait here is bounded: barriers and joins take a timeout, so a deadlock fails the
test that hit it, and the `threads` watchdog in `conftest.py` is the last resort.
"""

import asyncio
import copy
import gc
import os
import pickle
import subprocess
import sys
import sysconfig
import textwrap
import threading
import time
import weakref
from typing import TYPE_CHECKING, Any, NoReturn, override

import pytest

import fnloom
import picklable_nodes
from fnloom import _async, _core
from helpers import make_module

if TYPE_CHECKING:
    import types
    from collections.abc import Callable, Sequence

pytestmark = pytest.mark.threads

# Four independent nodes that pass a barrier only when all four wait on it together.
MEETING = """
import threading

MEET = threading.Barrier(4, timeout=5)


def a(x: int) -> int:
    MEET.wait()
    return x


def b(x: int) -> int:
    MEET.wait()
    return x + 1


def c(x: int) -> int:
    MEET.wait()
    return x + 2


def d(x: int) -> int:
    MEET.wait()
    return x + 3
"""

# A fan-out whose branch node records where it ran, and fails for the items asked.
FAN = """
import threading
import time

from fnloom import Collect, Parallelizable

SEEN = []
LOCK = threading.Lock()
RUNNING = []


def picked(n: int) -> Parallelizable[int]:
    yield from range(n)


def details(picked: int, fail: tuple[int, ...], pause: float) -> int:
    current = threading.current_thread()
    with LOCK:
        SEEN.append((picked, threading.get_ident(), id(current), current.name))
        RUNNING.append(picked)
    try:
        if picked in fail:
            # The later item fails first, so key order, not time, picks the error.
            time.sleep(pause * (len(fail) - fail.index(picked)))
            raise ValueError(picked)
        time.sleep(pause)
        return picked * 10
    finally:
        with LOCK:
            RUNNING.remove(picked)


def summary(details: Collect[int]) -> list[int]:
    return list(details)
"""

NO_FAIL: dict[str, object] = {"fail": (), "pause": 0.0}


def fan(
    name: str, threads: int | None = None, hooks: tuple[fnloom.Hook, ...] = ()
) -> fnloom.Driver:
    """The fan-out module's driver, `with_threads(threads)` when given."""
    builder = fnloom.Builder().with_modules(make_module(name, FAN)).with_hooks(*hooks)
    if threads is not None:
        builder = builder.with_threads(threads)
    return builder.build()


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


def test_with_threads_overlaps_independent_nodes() -> None:
    """Four independent nodes run at once: the barrier passes only then."""
    driver = (
        fnloom.Builder()
        .with_modules(make_module("meeting", MEETING))
        .with_threads(4)
        .build()
    )
    assert driver.execute(["a", "b", "c", "d"], inputs={"x": 1}) == {
        "a": 1,
        "b": 2,
        "c": 3,
        "d": 4,
    }


def test_with_threads_one_runs_on_the_callers_thread() -> None:
    """With one worker, the caller is the only worker."""
    driver = fan("one_worker", 1)
    module = sys.modules["one_worker"]
    assert driver.execute(["summary"], inputs={"n": 5, **NO_FAIL}) == {
        "summary": [0, 10, 20, 30, 40]
    }
    assert {ident for _, ident, _, _ in module.SEEN} == {threading.get_ident()}


def test_pool_threads_are_named_fnloom_worker() -> None:
    """A node on another thread than the caller's sees its worker's name."""
    driver = fan("named", 4)
    module = sys.modules["named"]
    driver.execute(["summary"], inputs={"n": 16, "fail": (), "pause": 0.02})
    others = {
        name for _, ident, _, name in module.SEEN if ident != threading.get_ident()
    }
    assert others
    assert all(name.startswith("fnloom-worker-") for name in others)


def test_a_worker_keeps_one_thread_state_across_tasks() -> None:
    """Every node one worker runs sees the same `current_thread()`, over 100 tasks."""
    driver = fan("one_state", 2)
    module = sys.modules["one_state"]
    driver.execute(["summary"], inputs={"n": 100, "fail": (), "pause": 0.001})
    seen: dict[int, set[tuple[int, str]]] = {}
    for _, ident, current, name in module.SEEN:
        seen.setdefault(ident, set()).add((current, name))
    assert all(len(states) == 1 for states in seen.values())


def test_no_pool_thread_starts_before_the_first_run() -> None:
    """A pool starts at its driver's first run, not at build."""
    before = settled_workers()
    driver = fan("lazy", 4)
    assert _core.live_workers() == before
    driver.execute(["summary"], inputs={"n": 2, **NO_FAIL})
    assert _core.live_workers() == before + 3


def test_the_pool_holds_max_workers_minus_one_threads() -> None:
    """The caller works too, so the pool holds one thread fewer than `max_workers`."""
    before = settled_workers()
    driver = fan("sized", 3)
    for _ in range(3):
        driver.execute(["summary"], inputs={"n": 8, **NO_FAIL})
    assert _core.live_workers() == before + 2
    del driver
    assert settled_workers() == before


def test_a_failure_waits_for_running_branches() -> None:
    """No branch is still running when `execute` raises."""
    driver = fan("waits", 4)
    module = sys.modules["waits"]
    with pytest.raises(ValueError, match="3"):
        driver.execute(["summary"], inputs={"n": 4, "fail": (3,), "pause": 0.1})
    assert module.RUNNING == []
    assert {item for item, _, _, _ in module.SEEN} == {0, 1, 2, 3}


def test_the_lowest_order_key_failure_is_raised() -> None:
    """Two branches fail, the later one first: the earlier branch's error is raised."""
    driver = fan("lowest", 4)
    with pytest.raises(ValueError, match="1") as info:
        driver.execute(["summary"], inputs={"n": 4, "fail": (1, 2), "pause": 0.05})
    assert info.value.__notes__ == [
        "while computing 'details' for branch (1,) of 'picked'"
    ]


class Ends(fnloom.Hook):
    """Records each `after_node`'s node, branch and outcome."""

    def __init__(self) -> None:
        """No `after_node` yet."""
        self.ends: list[tuple[str, tuple[int, ...], bool]] = []
        self.lock = threading.Lock()

    @override
    def after_node(self, event: fnloom.NodeEnd) -> None:
        with self.lock:
            self.ends.append((event.node.name, event.branch, event.ok))


def test_every_failure_seen_reaches_after_node() -> None:
    """Both failures get `ok=False`, and one exception is raised, never a group."""
    hook = Ends()
    driver = fan("every", 4, hooks=(hook,))
    with pytest.raises(ValueError, match="1") as info:
        driver.execute(["summary"], inputs={"n": 4, "fail": (1, 2), "pause": 0.05})
    assert not isinstance(info.value, BaseExceptionGroup)
    failed = sorted(branch for name, branch, ok in hook.ends if not ok)
    assert failed == [(1,), (2,)]


def test_with_threads_survives_pickle_copy_rebuild_and_build_async() -> None:
    """Every copy of a driver keeps the setting, with a pool of its own."""
    builder = fnloom.Builder().with_modules(picklable_nodes).with_threads(3)
    original = builder.build()
    assert original._workers is not None  # noqa: SLF001  # the driver's own pool
    for other in (
        pickle.loads(pickle.dumps(original)),  # noqa: S301  # our own pickle
        copy.copy(original),
        copy.deepcopy(original),
        original.rebuild(),
        pickle.loads(pickle.dumps(builder)).build(),  # noqa: S301  # our own pickle
    ):
        workers = other._workers  # noqa: SLF001  # the driver's own pool
        assert workers is not None
        assert workers.max_workers == original._workers.max_workers  # noqa: SLF001  # the driver's own pool
        assert workers.threads() is not original._workers.threads()  # noqa: SLF001  # the driver's own pool
    assert builder.build_async()._builder is builder  # noqa: SLF001  # the builder kept


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "2"])
def test_bad_max_workers_are_refused(bad: object) -> None:
    """A non-`int` or a `bool` is a `TypeError`; a value under 1 a `ValueError`."""
    error = (
        ValueError if isinstance(bad, int) and not isinstance(bad, bool) else TypeError
    )
    with pytest.raises(error, match="max_workers"):
        fnloom.Builder().with_threads(bad)  # ty: ignore[invalid-argument-type]  # the refusal is the point


def test_max_workers_defaults_to_the_cpu_count() -> None:
    """`with_threads()` sizes the pool by `os.process_cpu_count()`."""
    module = make_module("sized_default", FAN)
    built = fnloom.Builder().with_modules(module).with_threads().build()
    assert built._workers is not None  # noqa: SLF001  # the driver's own pool
    assert built._workers.max_workers == (os.process_cpu_count() or 1)  # noqa: SLF001  # the driver's own pool


# A node that forks once, on the thread running it: the child goes on from the node.
FORKING = """
import os

FORKED = []


def forked(x: int) -> int:
    if not FORKED:
        FORKED.append(os.getpid())
        return os.fork()
    return x
"""


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


@forks
@pytest.mark.filterwarnings(
    "ignore:This process .* is multi-threaded:DeprecationWarning"
)
def test_a_fork_inside_a_node_raises_in_the_child() -> None:
    """The child's run raises, its next run gets a new pool; the parent completes."""
    module = make_module("forking_threads", FORKING)
    driver = fnloom.Builder().with_modules(module).with_threads(1).build()
    parent = os.getpid()
    try:
        result = driver.execute(["forked"], inputs={"x": 7})
    except RuntimeError as error:
        if os.getpid() == parent:
            raise
        survived = "a run doesn't survive os.fork()" in str(error)
        _child(
            lambda: (
                survived
                and driver.execute(["forked"], inputs={"x": 7}) == {"forked": 7}
            )
        )
    child = result["forked"]
    assert isinstance(child, int)
    assert child > 0
    assert _waited(child) == 0


@forks
@pytest.mark.filterwarnings(
    "ignore:This process .* is multi-threaded:DeprecationWarning"
)
def test_a_sequential_run_continues_in_a_forked_child() -> None:
    """Without `with_threads` a run has no shared state, so the child finishes it."""
    module = make_module("forking_plain", FORKING)
    driver = fnloom.build(module)
    parent = os.getpid()
    result = driver.execute(["forked"], inputs={"x": 7})
    if os.getpid() != parent:
        _child(lambda: result == {"forked": 0})
    child = result["forked"]
    assert isinstance(child, int)
    assert _waited(child) == 0


@forks
@pytest.mark.filterwarnings(
    "ignore:This process .* is multi-threaded:DeprecationWarning"
)
def test_a_forked_child_gets_a_new_pool() -> None:
    """A driver whose pool ran in the parent runs in the child, on a pool of its own."""
    driver = fan("forked_pool", 3)
    inputs = {"n": 6, **NO_FAIL}
    expected = {"summary": [0, 10, 20, 30, 40, 50]}
    assert driver.execute(["summary"], inputs=inputs) == expected
    assert sys.platform != "win32", "only where os.fork is"
    pid = os.fork()
    if pid == 0:
        _child(lambda: driver.execute(["summary"], inputs=inputs) == expected)
    assert _waited(pid) == 0
    assert driver.execute(["summary"], inputs=inputs) == expected


def test_a_panic_in_the_worker_loop_fails_the_run_once() -> None:
    """A panic outside a node call fails the run as one `PanicException`."""
    driver = fan("panicked", 2)
    _core.arm_worker_panic()
    with pytest.raises(BaseException, match="deliberate panic") as info:
        driver.execute(["summary"], inputs={"n": 4, **NO_FAIL})
    assert type(info.value).__name__ == "PanicException"
    assert driver.execute(["summary"], inputs={"n": 2, **NO_FAIL}) == {
        "summary": [0, 10]
    }


@pytest.mark.skipif(
    not sysconfig.get_config_var("Py_GIL_DISABLED"),
    reason="only a free-threaded build can run without the GIL",
)
def test_a_parallel_run_keeps_the_gil_off() -> None:
    """A fresh interpreter runs a parallel fan-out with the GIL still off."""
    script = textwrap.dedent(
        """
        import sys, types
        import fnloom
        source = '''
        from fnloom import Collect, Parallelizable
        def picked(n: int) -> Parallelizable[int]:
            yield from range(n)
        def details(picked: int) -> int:
            return picked * 2
        def summary(details: Collect[int]) -> int:
            return sum(details)
        '''
        module = types.ModuleType("gil_off")
        exec(textwrap.dedent(source), module.__dict__)
        driver = fnloom.Builder().with_modules(module).with_threads(4).build()
        assert driver.execute(["summary"], inputs={"n": 100}) == {"summary": 9900}
        print(sys._is_gil_enabled())
        """
    )
    done = subprocess.run(  # noqa: S603  # our own interpreter and script
        [sys.executable, "-c", "import textwrap\n" + script],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    assert done.stdout.strip() == "False"


class _Spy:
    """Stands in for a prepared plan's native plan, recording each run's keywords."""

    def __init__(self, plan: _core.Plan) -> None:
        """Forwards to `plan`."""
        self.plan = plan
        self.keywords: list[dict[str, object]] = []

    def run(
        self,
        inputs: dict[str, Any],
        overrides: dict[str, Any],
        observers: Sequence[_core.ObserverBase] = (),
        /,
        **keywords: Any,  # noqa: ANN401  # whatever the plan's keywords are
    ) -> dict[str, object]:
        """Records `keywords`, then runs the plan."""
        self.keywords.append(keywords)
        return self.plan.run(inputs, overrides, observers, **keywords)


def test_a_driver_without_with_threads_calls_plan_run() -> None:
    """Without `with_threads` a run is the native plan's own, with no pool given."""
    driver = fan("plain_spy")
    plan = driver.prepare(["summary"], inputs={"n": 0, **NO_FAIL})
    assert type(plan._plan) is _core.Plan  # noqa: SLF001  # the prepared plan's own runnable
    native = plan._plan  # noqa: SLF001  # the prepared plan's own runnable
    assert isinstance(native, _core.Plan)
    spy = _Spy(native)
    plan._plan = spy  # noqa: SLF001  # the prepared plan's own runnable
    assert plan.run(inputs={"n": 3, **NO_FAIL}) == {"summary": [0, 10, 20]}
    assert spy.keywords == [{}]
    threaded = fan("threaded_spy", 2).prepare(["summary"], inputs={"n": 0, **NO_FAIL})
    assert type(threaded._plan) is not _core.Plan  # noqa: SLF001  # the prepared plan's own runnable


# A fan-out whose branch node is cacheable, counting its calls.
CACHED = """
import threading

from fnloom import Collect, Parallelizable, cache

CALLS = []
LOCK = threading.Lock()


def picked(n: int) -> Parallelizable[int]:
    yield from range(n)


@cache(version="1")
def details(picked: int) -> int:
    with LOCK:
        CALLS.append(picked)
    return picked * 10


def summary(details: Collect[int]) -> list[int]:
    return list(details)
"""


def test_a_cached_parallel_run_hits_as_a_sequential_one() -> None:
    """A cached driver with threads stores each branch, and its second run hits."""
    module = make_module("cached_threads", CACHED)
    store = fnloom.MemoryCache()
    driver = (
        fnloom.Builder().with_modules(module).with_cache(store).with_threads(4).build()
    )
    expected = {"summary": [0, 10, 20, 30, 40, 50]}
    assert driver.execute(["summary"], inputs={"n": 6}) == expected
    assert sorted(module.CALLS) == [0, 1, 2, 3, 4, 5]
    assert driver.execute(["summary"], inputs={"n": 6}) == expected
    assert driver.execute(["summary"], inputs={"n": 7}) == {
        "summary": [*expected["summary"], 60]
    }
    assert sorted(module.CALLS) == [0, 1, 2, 3, 4, 5, 6]


# Hooks per run, contexts, order keys, branch-safe keys and checks (Plan 11, task 4).


class Alone(fnloom.Hook):
    """Takes a lock without blocking in each node event, noting any overlap."""

    def __init__(self, pause: float = 0.005) -> None:
        """No overlap yet; each event holds the lock for `pause` seconds."""
        self.lock = threading.Lock()
        self.pause = pause
        self.overlapped = 0
        self.threads: dict[tuple[str, tuple[int, ...]], int] = {}

    def _alone(self, event: fnloom.NodeStart | fnloom.NodeEnd) -> None:
        if not self.lock.acquire(blocking=False):
            self.overlapped += 1
            return
        try:
            self.threads[event.node.name, event.branch] = threading.get_ident()
            time.sleep(self.pause)
        finally:
            self.lock.release()

    @override
    def before_node(self, event: fnloom.NodeStart) -> None:
        self._alone(event)

    @override
    def after_node(self, event: fnloom.NodeEnd) -> None:
        self._alone(event)


def test_hook_calls_of_one_run_never_overlap() -> None:
    """Over 200 branches on eight workers, no two hook calls of the run overlap."""
    hook = Alone()
    driver = fan("alone", 8, (hook,))
    result = driver.execute(["summary"], inputs={"n": 200, "fail": (), "pause": 0.0})
    assert result == {"summary": [i * 10 for i in range(200)]}
    assert hook.overlapped == 0


def test_a_hook_runs_on_the_thread_that_ran_the_node() -> None:
    """`after_node` of each branch runs on the thread its node ran on."""
    hook = Alone(pause=0.0)
    driver = fan("hook_thread", 4, (hook,))
    module = sys.modules["hook_thread"]
    driver.execute(["summary"], inputs={"n": 32, "fail": (), "pause": 0.002})
    ran = {(item,): ident for item, ident, _, _ in module.SEEN}
    assert {branch: hook.threads["details", branch] for branch in ran} == ran
    assert len(set(ran.values())) > 1


class Events(fnloom.Hook):
    """Records every event as a comparable tuple, under a lock."""

    def __init__(self) -> None:
        """No event yet."""
        self.events: list[tuple[object, ...]] = []
        self.skipped: tuple[tuple[str, tuple[int, ...]], ...] = ()
        self.lock = threading.Lock()

    def _add(self, *event: object) -> None:
        with self.lock:
            self.events.append(event)

    @override
    def before_node(self, event: fnloom.NodeStart) -> None:
        self._add("start", event.node.name, event.branch, event.order)

    @override
    def after_node(self, event: fnloom.NodeEnd) -> None:
        self._add("end", event.node.name, event.branch, event.order, event.ok)

    @override
    def after_expand(self, event: fnloom.ExpandEnd) -> None:
        self._add("expand", event.node.name, event.branch, event.order, event.items)

    @override
    def after_run(self, event: fnloom.RunEnd) -> None:
        self.skipped = event.skipped


# A block of two nodes, a skip in some branches, and a second block after it.
ORDERED = """
from fnloom import ABSENT, Absent, Collect, Parallelizable


def picked(n: int) -> Parallelizable[int]:
    yield from range(n)


def first(picked: int) -> int | Absent:
    return ABSENT if picked % 3 == 1 else picked


def second(first: int) -> int:
    return first * 2


def gathered(second: Collect[int]) -> list[int]:
    return list(second)


def again(gathered: list[int]) -> Parallelizable[int]:
    yield from gathered


def last(again: int) -> int:
    return again + 1


def ends(last: Collect[int]) -> list[int]:
    return list(last)
"""


def ordered_events(
    threads: int | None, n: int = 6
) -> tuple[list[tuple[object, ...]], Events]:
    """The `ORDERED` program's events, without threads or on `threads` workers."""
    hook = Events()
    builder = (
        fnloom.Builder().with_modules(make_module("ordered", ORDERED)).with_hooks(hook)
    )
    if threads is not None:
        builder = builder.with_threads(threads)
    builder.build().execute(["ends"], inputs={"n": n})
    return hook.events, hook


def test_with_one_worker_events_equal_the_sequential_run() -> None:
    """With one worker, every event and its order come as without threads."""
    sequential, plain = ordered_events(None)
    one, threaded = ordered_events(1)
    assert one == sequential
    assert threaded.skipped == plain.skipped
    assert all(event[3] for event in sequential)


def test_order_sorts_parallel_events_into_reference_order() -> None:
    """Sorted by order, eight workers' events are the sequential run's, a0 b0 a1 b1."""
    sequential, _ = ordered_events(None, 12)
    parallel, _ = ordered_events(8, 12)
    ends = [e for e in sequential if e[0] == "end"]
    assert sorted((e for e in parallel if e[0] == "end"), key=lambda e: e[3]) == ends
    names = [(e[1], e[2]) for e in ends if e[1] in {"first", "second"}][:4]
    assert names == [
        ("first", (0,)),
        ("second", (0,)),
        ("first", (1,)),
        ("first", (2,)),
    ]


def test_skipped_is_sorted_by_order_on_both_paths() -> None:
    """`RunEnd.skipped` is in order key order, with or without threads."""
    _, plain = ordered_events(None, 12)
    _, threaded = ordered_events(8, 12)
    assert plain.skipped == tuple(("second", (i,)) for i in range(12) if i % 3 == 1)
    assert threaded.skipped == plain.skipped


CONTEXTS = """
import contextvars

VAR = contextvars.ContextVar("parallel_var", default="unset")


def setter(x: int) -> str:
    VAR.set("setter")
    return VAR.get()


def reader(setter: str) -> str:
    return VAR.get()
"""


@pytest.mark.parametrize("workers", [1, 2, 8])
def test_a_contextvar_set_in_one_node_is_not_seen_by_another(workers: int) -> None:
    """A node sees the caller's variables, and none another node set."""
    module = make_module(f"contexts_{workers}", CONTEXTS)
    driver = fnloom.Builder().with_modules(module).with_threads(workers).build()
    token = module.VAR.set("caller")
    try:
        result = driver.execute(["setter", "reader"], inputs={"x": 1})
    finally:
        module.VAR.reset(token)
    assert result == {"setter": "setter", "reader": "caller"}
    assert module.VAR.get() == "unset"


BRANCHES = """
import time

import fnloom
from fnloom import Collect, Parallelizable


def picked(n: int) -> Parallelizable[int]:
    yield from range(n)


def where(picked: int) -> tuple[int, ...]:
    time.sleep(0.002)
    return fnloom.current_branch()


def all_where(where: Collect[tuple[int, ...]]) -> list[tuple[int, ...]]:
    return list(where)
"""


def test_current_branch_on_workers_is_each_nodes_own() -> None:
    """Each branch's node sees its own branch, on whatever worker ran it."""
    module = make_module("branches", BRANCHES)
    driver = fnloom.Builder().with_modules(module).with_threads(8).build()
    result = driver.execute(["all_where"], inputs={"n": 40})
    assert result == {"all_where": [(i,) for i in range(40)]}


class Raising(fnloom.Hook):
    """Raises in `after_node` for one branch; counts `after_run`."""

    def __init__(self) -> None:
        """No run yet."""
        self.runs: list[bool] = []
        self.lock = threading.Lock()

    @override
    def after_node(self, event: fnloom.NodeEnd) -> None:
        if event.branch == (3,):
            msg = "hook says no"
            raise LookupError(msg)

    @override
    def after_run(self, event: fnloom.RunEnd) -> None:
        with self.lock:
            self.runs.append(event.ok)


def test_a_raising_hook_stops_a_parallel_run_once() -> None:
    """The run stops once, `after_run` sees `ok=False`, the hook's error is raised."""
    hook = Raising()
    driver = fan("raising_hook", 8, (hook,))
    inputs = {"n": 32, "fail": (), "pause": 0.002}
    with pytest.raises(LookupError, match="hook says no") as info:
        driver.execute(["summary"], inputs=inputs)
    assert not isinstance(info.value, BaseExceptionGroup)
    assert hook.runs == [False]
    module = sys.modules["raising_hook"]
    assert module.RUNNING == []
    # The busy flag was cleared: the next run is not held up by it.
    assert fan("after_raising", 8).execute(["summary"], inputs=inputs) == {
        "summary": [i * 10 for i in range(32)]
    }


class SlowStore(fnloom.MemoryCache):
    """A store whose first write waits until a hook runs while it writes."""

    def __init__(self) -> None:
        """Not writing yet."""
        super().__init__()
        self.first = True
        self.writing = threading.Event()
        self.hooked = threading.Event()
        self.overlapped = False

    @override
    def put(self, key: bytes, value: object) -> None:
        if self.first:
            self.first = False
            self.writing.set()
            # A store's own error is only warned about, so the outcome is kept.
            self.overlapped = self.hooked.wait(5)
        super().put(key, value)


class WhileWriting(fnloom.Hook):
    """Tells the store a `before_node` ran while its first write was waiting."""

    def __init__(self, store: SlowStore) -> None:
        """Watches `store`."""
        self.store = store

    @override
    def before_node(self, event: fnloom.NodeStart) -> None:
        if self.store.writing.is_set():
            self.store.hooked.set()


def test_a_slow_store_does_not_hold_the_busy_flag() -> None:
    """While a store writes, another branch's hook runs."""
    store = SlowStore()
    driver = (
        fnloom.Builder()
        .with_modules(make_module("slow_store", CACHED))
        .with_cache(store)
        .with_hooks(WhileWriting(store))
        .with_threads(2)
        .build()
    )
    assert driver.execute(["summary"], inputs={"n": 8}) == {
        "summary": [i * 10 for i in range(8)]
    }
    assert store.overlapped


def test_print_nodes_and_log_failures_under_threads(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The bundled hooks print and log each branch once, from any worker."""
    lines: list[str] = []
    printed = threading.Lock()

    def show(line: str) -> None:
        with printed:
            lines.append(line)

    driver = fan("bundled", 8, (fnloom.PrintNodes(print_fn=show), fnloom.LogFailures()))
    with pytest.raises(ValueError, match="5"):
        driver.execute(["summary"], inputs={"n": 16, "fail": (5,), "pause": 0.002})
    ran = [line for line in lines if line.startswith("node 'details' (branch")]
    assert len(ran) == len({line.split(")")[0] for line in ran})
    assert any("(branch (5,)) failed" in line for line in ran)
    assert [r for r in caplog.records if "details" in r.getMessage()]


NESTED = """
from fnloom import Collect, Parallelizable


def outer(n: int) -> Parallelizable[int]:
    yield from range(n)


def inner(outer: int) -> Parallelizable[int]:
    yield from range(outer, outer + 3)


def leaf(inner: int) -> int:
    return inner * 2


def per_outer(leaf: Collect[int]) -> list[int]:
    return list(leaf)


def everything(per_outer: Collect[list[int]]) -> list[list[int]]:
    return list(per_outer)
"""


class Written(fnloom.MemoryCache):
    """A memory store that records the key of each write."""

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


def nested_keys(threads: int | None) -> tuple[object, list[bytes]]:
    """`NESTED`'s result and the keys written, without threads or on `threads`."""
    store = Written()
    builder = (
        fnloom.Builder()
        .with_modules(make_module("nested_keys", NESTED))
        .with_cache(store)
    )
    if threads is not None:
        builder = builder.with_threads(threads)
    result = builder.build().execute(["everything"], inputs={"n": 8})
    # A set: two branches with one key may both miss and write, until single-flight.
    return result, sorted(set(store.keys))


def test_nested_expands_pulled_concurrently_key_each_branch() -> None:
    """Inner expands of different outer branches, pulled at once, key as in sequence."""
    reference = nested_keys(None)
    assert reference[1]
    for _ in range(5):
        assert nested_keys(8) == reference


CHECKED = """
import time

from fnloom import Collect, Parallelizable, check_output


def picked(n: int) -> Parallelizable[int]:
    yield from range(n)


@check_output(range=(0, 4), importance="warn")
def details(picked: int) -> int:
    time.sleep(0.005 * (8 - picked))
    return picked


def summary(details: Collect[int]) -> list[int]:
    return list(details)
"""


# The top of `CHECKED`'s range check.
CHECKED_TOP = 4


class Checks(fnloom.Hook):
    """Keeps each branch's `NodeEnd.checks`."""

    def __init__(self) -> None:
        """No check yet."""
        self.checks: dict[tuple[int, ...], tuple[fnloom.CheckResult, ...]] = {}
        self.lock = threading.Lock()

    @override
    def after_node(self, event: fnloom.NodeEnd) -> None:
        if event.node.name == "details":
            with self.lock:
                self.checks[event.branch] = event.checks


def test_check_results_are_per_branch() -> None:
    """Two branches of a checked node at once each report their own checks."""
    hook = Checks()
    driver = (
        fnloom.Builder()
        .with_modules(make_module("checked_branches", CHECKED))
        .with_hooks(hook)
        .with_threads(8)
        .build()
    )
    assert driver.execute(["summary"], inputs={"n": 8}) == {"summary": list(range(8))}
    assert {
        branch: [c.passes for c in found] for branch, found in hook.checks.items()
    } == {(i,): [i <= CHECKED_TOP] for i in range(8)}


# Review fixes: iterator contexts, forks under hooks, hooks' context, guards, leads.

GENERATOR_CONTEXT = """
import contextvars
import decimal

from fnloom import Collect, Parallelizable

VAR = contextvars.ContextVar("generator_var", default="unset")


def picked(n: int) -> Parallelizable[str]:
    token = VAR.set("inside")
    try:
        for i in range(n):
            yield VAR.get()
    finally:
        VAR.reset(token)


def seen(picked: str) -> str:
    return picked


def all_seen(seen: Collect[str]) -> list[str]:
    return list(seen)


def ratios(n: int) -> Parallelizable[str]:
    with decimal.localcontext(prec=3):
        for i in range(1, n + 1):
            yield str(decimal.Decimal(1) / decimal.Decimal(7 * i))


def ratio(ratios: str) -> str:
    return ratios


def all_ratios(ratio: Collect[str]) -> list[str]:
    return list(ratio)
"""


@pytest.mark.parametrize("workers", [1, 4])
def test_a_generators_context_lasts_across_its_items(workers: int) -> None:
    """Every pull of a generator runs in the one context its body started in."""
    module = make_module(f"generator_context_{workers}", GENERATOR_CONTEXT)
    driver = fnloom.Builder().with_modules(module).with_threads(workers).build()
    assert driver.execute(["all_seen", "all_ratios"], inputs={"n": 3}) == {
        "all_seen": ["inside"] * 3,
        "all_ratios": ["0.143", "0.0714", "0.0476"],
    }


# `a` forks while a worker holds the run's busy flag in `b`'s `before_node`.
FORK_UNDER_HOOK = """
import os
import threading

IN_HOOK = threading.Event()
FORKED = []


def a(x: int) -> int:
    assert IN_HOOK.wait(5)
    if not FORKED:
        FORKED.append(os.getpid())
        return os.fork()
    return x


def b(x: int) -> int:
    return x
"""


class HoldsTheFlag(fnloom.Hook):
    """Holds the busy flag in `b`'s `before_node` while `a` forks."""

    def __init__(self, module: types.ModuleType) -> None:
        """Signals `module.IN_HOOK`."""
        self.module = module

    @override
    def before_node(self, event: fnloom.NodeStart) -> None:
        if event.node.name == "b" and not self.module.FORKED:
            self.module.IN_HOOK.set()
            time.sleep(0.5)


@forks
@pytest.mark.filterwarnings(
    "ignore:This process .* is multi-threaded:DeprecationWarning"
)
def test_a_fork_while_a_worker_holds_the_busy_flag_does_not_hang_the_child() -> None:
    """The child skips the forking node's hooks, so it never waits for that flag."""
    module = make_module("fork_under_hook", FORK_UNDER_HOOK)
    driver = (
        fnloom.Builder()
        .with_modules(module)
        .with_hooks(HoldsTheFlag(module))
        .with_threads(2)
        .build()
    )
    parent = os.getpid()
    try:
        result = driver.execute(["a", "b"], inputs={"x": 7})
    except RuntimeError as error:
        if os.getpid() == parent:
            raise
        survived = "a run doesn't survive os.fork()" in str(error)
        _child(lambda: survived)
    child = result["a"]
    assert isinstance(child, int)
    assert child > 0
    assert _waited(child) == 0


HOOK_CONTEXT = """
import contextvars
import time

from fnloom import Collect, Parallelizable

REQUEST = contextvars.ContextVar("request", default="unset")


def picked(n: int) -> Parallelizable[int]:
    yield from range(n)


def details(picked: int) -> int:
    time.sleep(0.005)
    return picked


def summary(details: Collect[int]) -> list[int]:
    return list(details)
"""


class Requests(fnloom.Hook):
    """Records what a worker's `before_node` reads of the caller's `REQUEST`."""

    def __init__(self, module: types.ModuleType) -> None:
        """Reads `module.REQUEST`."""
        self.module = module
        self.seen: set[tuple[bool, str]] = set()
        self.lock = threading.Lock()

    @override
    def before_node(self, event: fnloom.NodeStart) -> None:
        if event.node.name == "details":
            worker = threading.current_thread().name.startswith("fnloom-worker-")
            with self.lock:
                self.seen.add((worker, self.module.REQUEST.get()))


def test_hooks_see_the_callers_context_on_every_worker() -> None:
    """Each run's hooks read its caller's variables, on the pool's threads too."""
    module = make_module("hook_context", HOOK_CONTEXT)
    hook = Requests(module)
    driver = (
        fnloom.Builder().with_modules(module).with_hooks(hook).with_threads(4).build()
    )
    for request in ("first", "second"):
        hook.seen = set()
        token = module.REQUEST.set(request)
        try:
            driver.execute(["summary"], inputs={"n": 32})
        finally:
            module.REQUEST.reset(token)
        assert (True, request) in hook.seen
        assert {seen for _, seen in hook.seen} == {request}


def test_a_panic_holding_the_busy_flag_fails_the_run_and_frees_the_flag() -> None:
    """A Rust panic under the busy flag fails the run once; no hook waits for good."""
    hook = Alone(pause=0.0)
    driver = fan("busy_panic", 4, (hook,))
    inputs = {"n": 16, "fail": (), "pause": 0.002}
    _core.arm_hook_panic()
    with pytest.raises(BaseException, match="deliberate panic") as info:
        driver.execute(["summary"], inputs=inputs)
    assert type(info.value).__name__ == "PanicException"
    assert driver.execute(["summary"], inputs=inputs) == {
        "summary": [i * 10 for i in range(16)]
    }


def test_a_panic_reporting_a_completed_task_fails_the_run_and_ends_it() -> None:
    """A Rust panic reporting a completed task unwinds through its guards.

    It is outside every node call: the run fails once, and still ends, the flag free.
    """
    hook = Alone(pause=0.0)
    driver = fan("report_panic", 4, (hook,))
    inputs = {"n": 16, "fail": (), "pause": 0.002}
    _core.arm_report_panic()
    with pytest.raises(BaseException, match="deliberate panic while reporting") as info:
        driver.execute(["summary"], inputs=inputs)
    assert type(info.value).__name__ == "PanicException"
    assert driver.execute(["summary"], inputs=inputs) == {
        "summary": [i * 10 for i in range(16)]
    }


class RaisesForOne(fnloom.Hook):
    """Raises in `after_node` for branch (1,) of `details`."""

    @override
    def after_node(self, event: fnloom.NodeEnd) -> None:
        if event.node.name == "details" and event.branch == (1,):
            msg = "hook after branch 1"
            raise LookupError(msg)


def test_the_lowest_failure_leads_when_its_hooks_raised_too() -> None:
    """Branch 2 fails first; branch 1's, grouped with its hook's error, still leads."""
    driver = fan("grouped_lead", 4, (RaisesForOne(),))
    with pytest.raises(BaseExceptionGroup) as info:
        driver.execute(["summary"], inputs={"n": 4, "fail": (1, 2), "pause": 0.05})
    first, *rest = info.value.exceptions
    assert isinstance(first, ValueError)
    assert first.args == (1,)
    assert [type(error) for error in rest] == [LookupError]


class Inputs(fnloom.Hook):
    """Keeps each `details` branch's `NodeEnd.inputs`."""

    def __init__(self) -> None:
        """No node yet."""
        self.inputs: dict[tuple[int, ...], dict[str, object]] = {}
        self.lock = threading.Lock()

    @override
    def after_node(self, event: fnloom.NodeEnd) -> None:
        if event.node.name == "details" and event.inputs is not None:
            with self.lock:
                self.inputs[event.branch] = dict(event.inputs)


def test_each_running_branch_reports_its_own_inputs() -> None:
    """Branches between their start and end at once each end with their own inputs."""
    hook = Inputs()
    driver = fan("own_inputs", 8, (hook,))
    driver.execute(["summary"], inputs={"n": 32, "fail": (), "pause": 0.002})
    assert {branch: found["picked"] for branch, found in hook.inputs.items()} == {
        (i,): i for i in range(32)
    }


class Expands(fnloom.Hook):
    """Counts `after_expand`, which fires as a pull or finish completes."""

    def __init__(self) -> None:
        """None yet."""
        self.count = 0
        self.lock = threading.Lock()

    @override
    def after_expand(self, event: fnloom.ExpandEnd) -> None:
        with self.lock:
            self.count += 1


def test_an_iterators_hooks_never_enter_its_context_while_it_pulls() -> None:
    """Expand events fire while the next pull runs, in another context than its own."""
    hook = Expands()
    module = make_module("expand_events", NESTED)
    driver = (
        fnloom.Builder().with_modules(module).with_hooks(hook).with_threads(8).build()
    )
    expected = driver.execute(["everything"], inputs={"n": 8})
    for _ in range(19):
        assert driver.execute(["everything"], inputs={"n": 8}) == expected
    assert hook.count == 20 * 9


# The async driver on `with_threads`: its sync nodes on the pool (task 7, Q65).

ON_POOL = """
import asyncio
import contextvars
import threading

SEEN = contextvars.ContextVar("seen", default="unset")
MEET = threading.Barrier(2, timeout=5)


def left(x: int) -> tuple[str, str]:
    MEET.wait()
    SEEN.set("left")
    return threading.current_thread().name, SEEN.get()


def right(x: int) -> tuple[str, str]:
    MEET.wait()
    return threading.current_thread().name, SEEN.get()


async def both(left: tuple[str, str], right: tuple[str, str]) -> list[object]:
    return [left, right, SEEN.get()]
"""


def _async_run(coroutine: Any) -> Any:  # noqa: ANN401  # what the run returns
    """`coroutine` on a fresh loop, bounded, so a hang fails the test."""
    return asyncio.run(asyncio.wait_for(coroutine, 10))


def test_async_with_threads_runs_sync_nodes_on_the_pool() -> None:
    """Two sync nodes overlap on the pool's threads, each in the caller's context."""
    module = make_module("async_on_pool", ON_POOL)
    driver = fnloom.Builder().with_modules(module).with_threads(2).build_async()
    token = module.SEEN.set("caller")
    try:
        result = _async_run(driver.execute(["both"], inputs={"x": 1}))
    finally:
        module.SEEN.reset(token)
    (left, left_seen), (right, right_seen), after = result["both"]
    assert left.startswith("fnloom-worker-")
    assert right.startswith("fnloom-worker-")
    assert left != right
    # Each node runs in its own copy of the caller's context.
    assert (left_seen, right_seen, after) == ("left", "caller", "caller")


ASYNC_MEETING = """
import asyncio

MEET = []


async def a(x: int) -> int:
    await asyncio.wait_for(MEET[0].wait(), 5)
    return x


async def b(x: int) -> int:
    await asyncio.wait_for(MEET[0].wait(), 5)
    return x + 1


def c(a: int, b: int) -> int:
    return a + b
"""


def test_async_nodes_hold_no_worker_slot() -> None:
    """Under `with_threads(1)`, two `async def` nodes overlap, and a sync one runs."""
    module = make_module("async_slotless", ASYNC_MEETING)
    driver = fnloom.Builder().with_modules(module).with_threads(1).build_async()

    async def scenario() -> object:
        module.MEET.append(asyncio.Barrier(2))
        return await driver.execute(["c"], inputs={"x": 1})

    assert _async_run(scenario()) == {"c": 3}


def test_with_threads_and_inline_are_refused() -> None:
    """`with_threads` runs sync nodes on the pool, so `sync_nodes="inline"` can't."""
    builder = fnloom.Builder().with_modules(make_module("inline_pool", ON_POOL))
    with pytest.raises(ValueError, match="sync_nodes='inline' can't apply") as info:
        builder.with_threads(2).build_async(sync_nodes="inline")
    assert str(info.value) == (
        "with_threads runs sync nodes on the driver's pool, so sync_nodes='inline' "
        "can't apply; drop one"
    )
    plain = builder.build_async()._driver  # noqa: SLF001  # the sync driver it wraps
    threaded = builder.with_threads(2).build_async()._driver  # noqa: SLF001  # as above
    assert fnloom.AsyncDriver(plain, {}, sync_nodes="inline") is not None
    with pytest.raises(ValueError, match="drop one"):
        fnloom.AsyncDriver(threaded, {}, sync_nodes="inline")


def test_a_sync_call_detached_before_its_thread_began_it_never_runs() -> None:
    """A stopping run that detaches a call not yet begun never has it run after.

    Its thread had taken the job, but not yet begun the call: the run, seeing it
    not started, replaces no thread, so the call must not run on that thread.
    """
    guard = _async._Guard()  # noqa: SLF001  # the guard under test
    with guard.lock:  # as `_Run._cancel` detaches it
        guard.detached = True
    calls: list[int] = []
    assert guard.call(lambda: calls.append(1)) is None
    assert calls == []
    assert guard.started
    assert guard.returned


def test_async_plans_of_one_prepared_plan_from_many_threads() -> None:
    """16 threads make async plans of one prepared plan at once, and none fails."""
    module = make_module("async_plans_threads", ON_POOL)
    first = fnloom.Builder().with_modules(module).build_async()
    base = first._driver  # noqa: SLF001  # the driver the async ones share
    nodes = first._nodes  # noqa: SLF001  # its nodes
    base.prepare(["left"], inputs={"x": 1})
    start = threading.Barrier(16, timeout=5)
    errors: list[BaseException] = []

    def make() -> None:
        try:
            start.wait()
            for _ in range(50):
                fnloom.AsyncDriver(base, nodes).prepare(["left"], inputs={"x": 1})
        except BaseException as error:  # noqa: BLE001  # collected for the assert
            errors.append(error)

    threads = [threading.Thread(target=make) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert errors == []


NESTED_ASYNC = """
import asyncio

DRIVER = []


def outer(x: int) -> int:
    # A run of this very driver, on this pool thread's own loop.
    return asyncio.run(DRIVER[0].execute(["inner"], inputs={"x": x}))["inner"]


def inner(x: int) -> int:
    return x * 10
"""


def test_an_async_run_inside_a_pooled_node_takes_no_worker_slot() -> None:
    """A sync node on `with_threads(1)` runs its driver: it never waits for itself."""
    module = make_module("async_nested_pool", NESTED_ASYNC)
    driver = fnloom.Builder().with_modules(module).with_threads(1).build_async()
    module.DRIVER.append(driver)
    assert _async_run(driver.execute(["outer"], inputs={"x": 2})) == {"outer": 20}


def test_a_panic_in_the_stepper_fails_the_run_and_frees_its_slots() -> None:
    """A Rust panic stepping a run raises once; the next run finds every slot free."""
    module = make_module("async_stepper_panic", ON_POOL)
    driver = fnloom.Builder().with_modules(module).with_threads(2).build_async()
    _core.arm_stepper_panic()
    with pytest.raises(BaseException, match="deliberate panic") as info:
        _async_run(driver.execute(["both"], inputs={"x": 1}))
    assert type(info.value).__name__ == "PanicException"
    gc.collect()
    module.MEET.reset()
    result = _async_run(driver.execute(["both"], inputs={"x": 1}))
    assert result["both"][2] == "unset"


def _wide(name: str, count: int, body: str = "return x") -> types.ModuleType:
    """`count` independent sync nodes `n0`... reading `x`."""
    lines = [f"def n{i}(x: int) -> int:\n    {body}\n" for i in range(count)]
    return make_module(name, "import time\n\n" + "\n".join(lines))


def test_async_runs_never_grow_the_pool_past_max_workers() -> None:
    """Twenty runs of fifty sync nodes on `with_threads(2)`: two threads at most."""
    before = _core.live_workers()
    module = _wide("async_pool_growth", 50)
    driver = fnloom.Builder().with_modules(module).with_threads(2).build_async()
    outputs = [f"n{i}" for i in range(50)]
    for _ in range(20):
        assert _async_run(driver.execute(outputs, inputs={"x": 1})) == dict.fromkeys(
            outputs, 1
        )
    assert _core.live_workers() - before <= 2  # noqa: PLR2004  # max_workers


CLOSE_STARVED = """
import asyncio
import threading

from fnloom import Collect, Parallelizable

GATE = threading.Event()
CLOSED = []


def items(n: int) -> Parallelizable[int]:
    try:
        yield from range(n)
    finally:
        CLOSED.append(n)


def slow(items: int) -> int:
    GATE.wait(10)
    return items


def total(slow: Collect[int]) -> int:
    return sum(slow)


async def boom(n: int) -> int:
    await asyncio.sleep(0.2)
    raise ValueError("boom")
"""


@pytest.mark.parametrize("threads", [1, 4])
def test_an_iterator_closes_while_its_items_hang_on_every_pool_thread(
    threads: int,
) -> None:
    """A sibling fails while each pool thread is in a hung item call: the run ends now.

    The generator's close holds no slot, so it never waits for a pool thread.
    """
    module = make_module(f"async_close_starved_{threads}", CLOSE_STARVED)
    driver = fnloom.Builder().with_modules(module).with_threads(threads).build_async()
    started = time.monotonic()
    try:
        with pytest.raises(ValueError, match="boom"):
            _async_run(
                asyncio.wait_for(
                    driver.execute(["total", "boom"], inputs={"n": 100}), 5
                )
            )
        assert time.monotonic() - started < 3  # noqa: PLR2004  # not the hung calls' 10
        assert module.CLOSED == [100]
    finally:
        module.GATE.set()


ABANDONED = """
import asyncio
import threading

GATE = threading.Event()


def h0(x: int) -> int:
    GATE.wait(10)
    return x


def h1(x: int) -> int:
    GATE.wait(10)
    return x


async def fail(x: int) -> int:
    await asyncio.sleep(0.05)
    raise ValueError("sibling fails")


{nodes}
"""


def test_abandoned_pool_calls_are_replaced_and_their_threads_leave() -> None:
    """Runs that abandon hung calls keep the pool bounded, by the slots they hold.

    Each abandoned call keeps its worker slot and its thread, which the pool replaces
    at once and lets go once the call returns: never a thread per call.
    """
    before = settled_workers()
    nodes = "\n".join(f"def n{i}(x: int) -> int:\n    return x\n" for i in range(50))
    module = make_module("async_abandoned", ABANDONED.format(nodes=nodes))
    driver = fnloom.Builder().with_modules(module).with_threads(2).build_async()
    try:
        for _ in range(5):
            with pytest.raises(ValueError, match="sibling fails"):
                _async_run(driver.execute(["fail", "h0", "h1"], inputs={"x": 1}))
            # The pool's two, and at most two in abandoned calls, by their slots.
            assert _core.live_workers() - before <= 4  # noqa: PLR2004
    finally:
        module.GATE.set()
    assert settled_workers() - before == 2  # noqa: PLR2004  # max_workers
    outputs = [f"n{i}" for i in range(50)]
    for _ in range(10):
        assert _async_run(driver.execute(outputs, inputs={"x": 1})) == dict.fromkeys(
            outputs, 1
        )
    assert settled_workers() - before == 2  # noqa: PLR2004  # max_workers


COLLECTED = """
import asyncio

DRIVER = []


def ok(x: int) -> int:
    return x


async def fail(x: int) -> int:
    await asyncio.sleep(0.01)
    raise ValueError("fails")
"""


@pytest.mark.parametrize("threads", [2, None], ids=["with_threads", "executor"])
def test_a_failed_async_runs_driver_is_collected_and_its_pool_stops(
    threads: int | None,
) -> None:
    """The failure's traceback keeps the run's stepper, which the collector now follows.

    The driver, kept in its module, is collected in one pass, and its threads leave.
    """
    before = settled_workers()
    module = make_module(f"async_collected_{threads}", COLLECTED)
    builder = fnloom.Builder().with_modules(module)
    if threads is not None:
        builder = builder.with_threads(threads)
    driver = builder.build_async()
    module.DRIVER.append(driver)
    with pytest.raises(ValueError, match="fails") as info:
        _async_run(driver.execute(["ok", "fail"], inputs={"x": 1}))
    collected = weakref.ref(driver)
    del sys.modules[module.__name__]
    del driver, builder, module, info
    gc.collect()
    assert collected() is None
    assert settled_workers() == before


NESTED_WIDE = """
import asyncio
import time

DRIVER = []
WIDE = [f"n{{i}}" for i in range({count})]


def outer(x: int) -> int:
    return sum(asyncio.run(DRIVER[0].execute(WIDE, inputs={{"x": x}})).values())


{nodes}
"""


def test_a_nested_async_run_inside_a_pooled_node_starts_no_pool_threads() -> None:
    """Its sync nodes go to its own loop's executor: the pool stays at `max_workers`."""
    before = _core.live_workers()
    nodes = "\n".join(
        f"def n{i}(x: int) -> int:\n    time.sleep(0.005)\n    return x\n"
        for i in range(60)
    )
    module = make_module("async_nested_wide", NESTED_WIDE.format(count=60, nodes=nodes))
    driver = fnloom.Builder().with_modules(module).with_threads(2).build_async()
    module.DRIVER.append(driver)
    assert _async_run(driver.execute(["outer"], inputs={"x": 1})) == {"outer": 60}
    assert _core.live_workers() - before <= 2  # noqa: PLR2004  # max_workers


DETACHED = """
import asyncio
import threading
import time

from fnloom import limit
from fnloom._threads import CANCELLED

DRIVER = []
INNER = []
DONE = threading.Event()


@limit(group="db")
def held(n: int) -> int:
    time.sleep(0.3)
    # Not stopped with the run that abandoned it, so the inner run reaches the slots.
    CANCELLED.set(None)
    try:
        run = DRIVER[0].execute(["inner"], inputs={{"n": n}})
        return asyncio.run(asyncio.wait_for(run, 5))["inner"]
    except BaseException as error:
        INNER.append(type(error).__name__)
        raise
    finally:
        DONE.set()


async def fail(n: int) -> int:
    await asyncio.sleep(0.05)
    raise ValueError("sibling fails")


{inner}
def inner(n: int) -> int:
    return n
"""


@pytest.mark.parametrize(
    ("threads", "inner", "seen"),
    [(1, "", []), (None, '@limit(group="db")', ["RuntimeError"])],
    ids=["worker-slot", "group-slot"],
)
def test_an_abandoned_call_running_its_driver_sees_the_slot_it_still_holds(
    threads: int | None, inner: str, seen: list[str]
) -> None:
    """The abandoned call's slots stay its ancestry: its inner run never waits on them.

    On the worker slot, the inner run takes no worker; on its group, it is refused.
    Later runs then get every slot back.
    """
    module = make_module(f"async_detached_{threads}", DETACHED.format(inner=inner))
    builder = fnloom.Builder().with_modules(module).with_limits({"db": 1})
    if threads is not None:
        builder = builder.with_threads(threads)
    driver = builder.build_async()
    module.DRIVER.append(driver)
    with pytest.raises(ValueError, match="sibling fails"):
        _async_run(driver.execute(["fail", "held"], inputs={"n": 1}))
    assert module.DONE.wait(5)
    assert seen == module.INNER
    assert _async_run(driver.execute(["inner"], inputs={"n": 2})) == {"inner": 2}


POOLED_FORK = """
import os


def forked(x: int) -> int:
    pid = os.fork()
    # The child returns into the pool's worker, which ends it, setting nothing.
    return pid
"""


@forks
@pytest.mark.filterwarnings(
    "ignore:This process .* is multi-threaded:DeprecationWarning"
)
def test_a_fork_inside_a_pooled_async_node_ends_the_child_quietly() -> None:
    """A pooled sync node's fork child sets no result and ends; the parent goes on."""
    assert sys.platform != "win32"
    module = make_module("async_pooled_fork", POOLED_FORK)
    driver = fnloom.Builder().with_modules(module).with_threads(2).build_async()
    child = _async_run(driver.execute(["forked"], inputs={"x": 1}))["forked"]
    assert child > 0
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        done, _ = os.waitpid(child, os.WNOHANG)
        if done:
            break
        time.sleep(0.05)
    else:
        os.kill(child, 9)
        pytest.fail("the forked child did not end")
