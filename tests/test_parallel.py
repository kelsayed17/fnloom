"""`Builder.with_threads`: one run's nodes on the driver's pool (Plan 11, task 3).

Every wait here is bounded: barriers and joins take a timeout, so a deadlock fails the
test that hit it, and the `threads` watchdog in `conftest.py` is the last resort.
"""

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
from typing import TYPE_CHECKING, Any, NoReturn, override

import pytest

import fnloom
import picklable_nodes
from fnloom import _core
from helpers import make_module

if TYPE_CHECKING:
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
