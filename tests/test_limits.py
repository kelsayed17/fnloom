"""`fnloom.limit` and `Builder.with_limits`: caps on concurrent calls (Plan 11, task 6).

Every driver honours them, with or without `with_threads`. Every wait here is bounded:
barriers, events and joins take a timeout, and the `threads` watchdog in `conftest.py`
is the last resort. Every counter is updated under a lock.
"""

import _thread
import asyncio
import copy
import pickle
import sys
import threading
import time
from typing import TYPE_CHECKING, override

import pytest

import fnloom
import picklable_limited
from fnloom import _core
from helpers import make_module

if TYPE_CHECKING:
    import types

pytestmark = pytest.mark.threads

# Branch nodes that count how many of their calls run at once, by name.
COUNTED = """
import threading
import time

from fnloom import Collect, Parallelizable, limit

LOCK = threading.Lock()
NOW = {}
MOST = {}
# A lock a `limit(1)` node takes without blocking: it must never find it held.
ALONE = threading.Lock()


def _counted(name, pause):
    with LOCK:
        NOW[name] = NOW.get(name, 0) + 1
        MOST[name] = max(MOST.get(name, 0), NOW[name])
    try:
        time.sleep(pause)
    finally:
        with LOCK:
            NOW[name] -= 1


def picked(n: int) -> Parallelizable[int]:
    yield from range(n)


@limit(2)
def load(picked: int, pause: float) -> int:
    _counted("load", pause)
    return picked


@limit(group="db")
def fetch(picked: int, pause: float) -> int:
    _counted("db", pause)
    return picked


@limit(group="db")
def store(picked: int, pause: float) -> int:
    _counted("db", pause)
    return -picked


@limit(1)
def alone(x: int, pause: float) -> int:
    if not ALONE.acquire(blocking=False):
        raise RuntimeError("two calls of alone at once")
    try:
        time.sleep(pause)
        return x
    finally:
        ALONE.release()


@limit(1)
def hold(x: int, gate: object) -> int:
    started, release = gate
    started.set()
    if not release.wait(5):
        raise TimeoutError("never released")
    return x


def loads(load: Collect[int]) -> list[int]:
    return list(load)


def both(fetch: Collect[int], store: Collect[int]) -> list[int]:
    return [*fetch, *store]
"""

# The cap of `load`, and the size of group `db`, in `COUNTED`.
CAP = 2
# How many runs, or branches, a test starts at once.
RUNS = 4
# How long a refusal or a wake may take, in seconds.
PROMPT = 2.0

THREADS = pytest.mark.parametrize("threads", [None, 2], ids=["sequential", "threads"])


def counted(
    name: str, threads: int | None = None, *, hooks: tuple[fnloom.Hook, ...] = ()
) -> tuple[fnloom.Driver, types.ModuleType]:
    """The counting module's driver, group `db` of size 2, `with_threads` when given."""
    module = make_module(name, COUNTED)
    builder = (
        fnloom.Builder().with_modules(module).with_limits({"db": 2}).with_hooks(*hooks)
    )
    if threads is not None:
        builder = builder.with_threads(threads)
    return builder.build(), module


def started(*targets: object) -> list[threading.Thread]:
    """A started thread per target."""
    threads = [threading.Thread(target=target) for target in targets]  # ty: ignore[invalid-argument-type]  # each target is callable
    for thread in threads:
        thread.start()
    return threads


def joined(threads: list[threading.Thread]) -> None:
    """Joins each thread, failing the test if one is still running."""
    for thread in threads:
        thread.join(10)
        assert not thread.is_alive()


def test_limit_caps_a_nodes_concurrent_calls() -> None:
    """32 branches on eight workers: never more than two calls of `load` at once."""
    driver, module = counted("limit_node", 8)
    result = driver.execute(["loads"], inputs={"n": 32, "pause": 0.01})
    assert result == {"loads": list(range(32))}
    assert module.MOST["load"] == CAP


def test_a_group_caps_its_members_together() -> None:
    """`fetch` and `store` share group `db`: two calls of either at once, at most."""
    driver, module = counted("limit_group", 8)
    result = driver.execute(["both"], inputs={"n": 16, "pause": 0.01})
    assert result == {"both": [*range(16), *(-i for i in range(16))]}
    assert module.MOST["db"] == CAP


@THREADS
def test_limits_hold_across_concurrent_runs_of_one_driver(threads: int | None) -> None:
    """Four threads' runs at once share the driver's group cap."""
    driver, module = counted(f"limit_runs_{threads}", threads)
    results: list[object] = []

    def run() -> None:
        results.append(driver.execute(["both"], inputs={"n": 4, "pause": 0.02}))

    joined(started(*[run] * RUNS))
    assert len(results) == RUNS
    assert module.MOST["db"] == CAP


def test_a_sequential_driver_shared_by_threads_honours_limit_one() -> None:
    """Four threads `execute` a `limit(1)` node at once: never two calls together."""
    driver, _ = counted("limit_shared")
    results: list[object] = []

    def run() -> None:
        results.append(driver.execute(["alone"], inputs={"x": 1, "pause": 0.02}))

    joined(started(*[run] * 4))
    assert results == [{"alone": 1}] * 4


@THREADS
def test_a_released_slot_wakes_a_waiting_run(threads: int | None) -> None:
    """B's run waits for the slot A's run holds, and finishes once A lets it go."""
    driver, _ = counted(f"limit_wake_{threads}", threads)
    gate_a = (threading.Event(), threading.Event())
    gate_b = (threading.Event(), threading.Event())
    gate_b[1].set()
    done: list[float] = []

    def run(gate: tuple[threading.Event, threading.Event]) -> None:
        driver.execute(["hold"], inputs={"x": 1, "gate": gate})
        done.append(time.monotonic())

    first = started(lambda: run(gate_a))
    assert gate_a[0].wait(5)
    second = started(lambda: run(gate_b))
    time.sleep(0.2)
    assert not gate_b[0].is_set()
    released = time.monotonic()
    gate_a[1].set()
    joined(first + second)
    assert gate_b[0].is_set()
    assert max(done) - released < 1


UNSIZED = """
from fnloom import limit


@limit(group="db")
def load(x: int) -> int:
    return x
"""


@pytest.mark.parametrize("kind", ["driver", "threads", "async"])
def test_an_unsized_group_is_refused_at_build(kind: str) -> None:
    """A group with no size fails every build, naming it and its node."""
    builder = fnloom.Builder().with_modules(make_module(f"unsized_{kind}", UNSIZED))
    match = r"group 'db' of 'load' has no size; give it one with Builder.with_limits"
    build = {
        "async": builder.build_async,
        "threads": builder.with_threads(2).build,
        "driver": builder.build,
    }[kind]
    with pytest.raises(ValueError, match=match):
        build()


def test_a_size_for_a_group_no_node_is_in_is_refused() -> None:
    """Sizing a group no node is in fails the build, naming the group."""
    builder = (
        fnloom.Builder()
        .with_modules(make_module("unused_group", UNSIZED))
        .with_limits({"db": 1, "api": 2})
    )
    with pytest.raises(ValueError, match="with_limits sizes group 'api'"):
        builder.build()


@pytest.mark.parametrize(
    ("groups", "error"),
    [
        ([("db", 1)], TypeError),
        ({1: 1}, TypeError),
        ({"": 1}, ValueError),
        ({"db": True}, TypeError),
        ({"db": 1.5}, TypeError),
        ({"db": 0}, ValueError),
    ],
)
def test_bad_group_sizes_are_refused(groups: object, error: type[Exception]) -> None:
    """A size is an `int` of at least 1, named by a non-empty `str`."""
    with pytest.raises(error):
        fnloom.Builder().with_limits(groups)  # ty: ignore[invalid-argument-type]  # the refusal is the point


@pytest.mark.parametrize(
    ("args", "kwargs", "error"),
    [
        ((), {}, ValueError),
        ((0,), {}, ValueError),
        ((True,), {}, TypeError),
        ((1.5,), {}, TypeError),
        ((), {"group": ""}, ValueError),
        ((), {"group": 1}, TypeError),
    ],
)
def test_bad_limits_are_refused(
    args: tuple[object, ...], kwargs: dict[str, object], error: type[Exception]
) -> None:
    """`limit` takes an `n` of at least 1, a non-empty group, or both."""
    with pytest.raises(error):
        fnloom.limit(*args, **kwargs)  # ty: ignore[invalid-argument-type]  # the refusal is the point


def test_a_second_limit_is_refused() -> None:
    """One `@limit` per function."""
    with pytest.raises(ValueError, match="apply limit once"):

        @fnloom.limit(1)
        @fnloom.limit(2)
        def twice(x: int) -> int:
            return x


def test_with_limits_survives_pickle_copy_rebuild_and_build_async() -> None:
    """Every copy of a driver keeps its caps, with counters of its own."""
    builder = fnloom.Builder().with_modules(picklable_limited).with_limits({"db": 1})
    original = builder.build()
    workers = original._workers  # noqa: SLF001  # the driver's own limits
    assert workers is not None
    assert workers.caps is not None
    assert dict(workers.caps.nodes) == {"loaded": 2}
    assert dict(workers.caps.groups_of) == {"loaded": "db", "saved": "db"}
    for other in (
        pickle.loads(pickle.dumps(original)),  # noqa: S301  # our own pickle
        copy.copy(original),
        copy.deepcopy(original),
        original.rebuild(),
        pickle.loads(pickle.dumps(builder)).build(),  # noqa: S301  # our own pickle
        builder.with_threads(2).build(),
    ):
        copied = other._workers  # noqa: SLF001  # the driver's own limits
        assert copied is not None
        assert copied.caps == workers.caps
        assert copied.limits() is not workers.limits()
        assert other.execute(["saved"], inputs={"a": 1}) == {"saved": 4}
    assert builder.build_async()._builder is builder  # noqa: SLF001  # the builder kept


LAZY = """
import threading

from fnloom import Collect, Parallelizable

LOCK = threading.Lock()
PULLED = [0]
EXITED = [0]
MOST = [0]


def picked(n: int) -> Parallelizable[int]:
    for item in range(n):
        with LOCK:
            PULLED[0] += 1
            MOST[0] = max(MOST[0], PULLED[0] - EXITED[0])
        yield item


def details(picked: int) -> int:
    with LOCK:
        EXITED[0] += 1
    return picked


def summary(details: Collect[int]) -> int:
    return len(details)
"""


def test_the_in_flight_bound_keeps_a_large_generator_lazy() -> None:
    """Two workers: at most four of 10,000 branches are open at once."""
    module = make_module("limit_lazy", LAZY)
    driver = fnloom.Builder().with_modules(module).with_threads(2).build()
    assert driver.execute(["summary"], inputs={"n": 10_000}) == {"summary": 10_000}
    assert 1 <= module.MOST[0] <= 2 * 2  # twice the two workers


NESTED = """
from fnloom import Collect, Parallelizable, limit

# The driver under test, set once built: its nodes run it again.
DRIVER = []


def outer(x: int) -> int:
    return DRIVER[0].execute(["inner"], inputs={"x": x})["inner"] + 1


def inner(x: int) -> int:
    return x * 10


@limit(group="db")
def held(x: int) -> int:
    return DRIVER[0].execute(["needs"], inputs={"x": x})["needs"]


def middle(x: int) -> int:
    return DRIVER[0].execute(["needs"], inputs={"x": x})["needs"]


@limit(group="db")
def twice(x: int) -> int:
    return DRIVER[0].execute(["middle"], inputs={"x": x})["middle"]


@limit(group="db")
def needs(x: int) -> int:
    return x


@limit(1)
def recursive(n: int) -> int:
    if n == 0:
        return 0
    return DRIVER[0].execute(["recursive"], inputs={"n": n - 1})["recursive"] + 1
"""


def nested(name: str, threads: int | None) -> fnloom.Driver:
    """The nesting module's driver, group `db` of size 1, which its nodes run again."""
    module = make_module(name, NESTED)
    builder = fnloom.Builder().with_modules(module).with_limits({"db": 1})
    if threads is not None:
        builder = builder.with_threads(threads)
    driver = builder.build()
    module.DRIVER.append(driver)
    return driver


def test_a_node_may_run_its_own_driver_inside_its_slot() -> None:
    """One worker: a node's own run takes its tasks on the slot it holds."""
    driver = nested("limit_nested_slot", 1)
    assert driver.execute(["outer"], inputs={"x": 2}) == {"outer": 21}


@THREADS
@pytest.mark.parametrize("request_", ["held", "twice"], ids=["one_level", "two_levels"])
def test_an_inner_run_needing_a_group_its_callers_hold_is_refused(
    threads: int | None, request_: str
) -> None:
    """`needs` waits for group `db`, which the run that called it holds: refused."""
    driver = nested(f"limit_refused_{threads}_{request_}", threads)
    start = time.monotonic()
    with pytest.raises(RuntimeError, match="'needs' needs group 'db', but every slot"):
        driver.execute([request_], inputs={"x": 1})
    assert time.monotonic() - start < PROMPT
    # The slots were all given back: the driver still runs what needs the group.
    assert driver.execute(["needs"], inputs={"x": 3}) == {"needs": 3}


FROM_POOL = """
import threading

from fnloom import Collect, Parallelizable

LIMITED = []
LOCK = threading.Lock()
WHERE = []


def picked(n: int) -> Parallelizable[int]:
    yield from range(n)


def tried(picked: int) -> str:
    with LOCK:
        WHERE.append(threading.current_thread().name)
    try:
        LIMITED[0].execute(["held"], inputs={"x": picked})
    except RuntimeError as error:
        return str(error).splitlines()[0]
    return "ran"


def outcomes(tried: Collect[str]) -> list[str]:
    return list(tried)
"""


@THREADS
def test_an_inner_run_is_refused_when_its_caller_started_on_a_pool_thread(
    threads: int | None,
) -> None:
    """Runs started by another driver's pool threads are refused the same way."""
    limited = nested(f"limit_from_pool_{threads}", threads)
    module = make_module(f"limit_pool_caller_{threads}", FROM_POOL)
    module.LIMITED.append(limited)
    caller = fnloom.Builder().with_modules(module).with_threads(4).build()
    outcomes = caller.execute(["outcomes"], inputs={"n": RUNS})["outcomes"]
    assert isinstance(outcomes, list)
    assert len(outcomes) == RUNS
    assert all("'needs' needs group 'db'" in outcome for outcome in outcomes)
    assert any(name.startswith("fnloom-worker-") for name in module.WHERE)


@THREADS
def test_a_limit_one_node_running_its_own_driver_on_itself_is_refused(
    threads: int | None,
) -> None:
    """A `limit(1)` node whose own run needs it gets the node-slot error at once."""
    driver = nested(f"limit_recursive_{threads}", threads)
    match = "'recursive' is limited to 1 call at a time, and the runs that called"
    with pytest.raises(RuntimeError, match=match):
        driver.execute(["recursive"], inputs={"n": 2})
    assert driver.execute(["recursive"], inputs={"n": 0}) == {"recursive": 0}


class RunsAgain(fnloom.Hook):
    """In `after_node` of `alone`, runs `alone` once more, in another run."""

    def __init__(self) -> None:
        """Not run yet."""
        self.driver: fnloom.Driver | None = None
        self.ran = False
        self.results: list[object] = []

    @override
    def after_node(self, event: fnloom.NodeEnd) -> None:
        if event.node.name == "alone" and self.driver is not None and not self.ran:
            # Set first: the inner run's own `after_node` comes here too.
            self.ran = True
            self.results.append(
                self.driver.execute(["alone"], inputs={"x": 7, "pause": 0.0})
            )


@THREADS
def test_node_and_group_slots_are_released_before_hooks(threads: int | None) -> None:
    """A hook of a `limit(1)` node runs that node in another run, which completes."""
    hook = RunsAgain()
    driver, _ = counted(f"limit_before_hooks_{threads}", threads, hooks=(hook,))
    hook.driver = driver
    assert driver.execute(["alone"], inputs={"x": 1, "pause": 0.0}) == {"alone": 1}
    assert hook.results == [{"alone": 7}]


def test_a_sequential_grant_wait_stops_on_interrupt() -> None:
    """`Ctrl-C` ends a sequential run waiting for a held slot within a second."""
    driver, _ = counted("limit_interrupt")
    gate = (threading.Event(), threading.Event())
    holder = started(lambda: driver.execute(["hold"], inputs={"x": 1, "gate": gate}))
    try:
        assert gate[0].wait(5)
        timer = threading.Timer(0.2, _thread.interrupt_main)
        timer.start()
        free = (threading.Event(), threading.Event())
        free[1].set()
        start = time.monotonic()
        with pytest.raises(KeyboardInterrupt):
            driver.execute(["hold"], inputs={"x": 2, "gate": free})
        assert time.monotonic() - start < PROMPT
        timer.join(5)
    finally:
        gate[1].set()
        joined(holder)


def test_a_waker_on_a_closed_loop_does_not_fail_the_releasing_run() -> None:
    """A waker that raises is dropped; each is called once, at the next release."""
    driver, _ = counted("limit_waker")
    workers = driver._workers  # noqa: SLF001  # the driver's own limits
    assert workers is not None
    limits = workers.limits()
    calls: list[str] = []
    loop = asyncio.new_event_loop()
    loop.close()

    def closed() -> None:
        calls.append("closed")
        loop.call_soon_threadsafe(calls.append, "never")

    limits.add_waker(closed)
    limits.add_waker(lambda: calls.append("woken"))
    assert driver.execute(["alone"], inputs={"x": 1, "pause": 0.0}) == {"alone": 1}
    assert calls == ["closed", "woken"]
    assert driver.execute(["alone"], inputs={"x": 2, "pause": 0.0}) == {"alone": 2}
    assert calls == ["closed", "woken"]


GENERATOR = """
import threading
import time

from fnloom import Collect, Parallelizable, limit

LOCK = threading.Lock()
NOW = [0]
MOST = [0]


@limit(1)
def picked(n: int) -> Parallelizable[int]:
    for item in range(n):
        with LOCK:
            NOW[0] += 1
            MOST[0] = max(MOST[0], NOW[0])
        time.sleep(0.0005)
        with LOCK:
            NOW[0] -= 1
        yield item


def details(picked: int) -> int:
    return picked


def summary(details: Collect[int]) -> int:
    return sum(details)
"""


@THREADS
def test_a_limited_generator_takes_its_slot_for_each_item(threads: int | None) -> None:
    """Two runs at once read one `limit(1)` generator's items one call at a time."""
    module = make_module(f"limit_generator_{threads}", GENERATOR)
    builder = fnloom.Builder().with_modules(module)
    if threads is not None:
        builder = builder.with_threads(threads)
    driver = builder.build()
    results: list[object] = []

    def run() -> None:
        results.append(driver.execute(["summary"], inputs={"n": 200}))

    joined(started(run, run, run))
    assert results == [{"summary": sum(range(200))}] * 3
    assert module.MOST[0] == 1


STALE = """
import contextvars

from fnloom import limit

# A copy of each call's context, kept after the call returned: as a thread or a task
# started inside the call would keep it.
CONTEXTS = []


@limit(1)
def gate(x: int, events: object) -> int:
    CONTEXTS.append(contextvars.copy_context())
    started, release = events
    started.set()
    if not release.wait(5):
        raise TimeoutError("never released")
    return x
"""


@THREADS
def test_a_context_kept_past_its_call_holds_no_slot(threads: int | None) -> None:
    """A run started from a context that outlived a limited call waits, not raises."""
    module = make_module(f"limit_stale_{threads}", STALE)
    builder = fnloom.Builder().with_modules(module)
    if threads is not None:
        builder = builder.with_threads(threads)
    driver = builder.build()
    free = (threading.Event(), threading.Event())
    free[1].set()
    assert driver.execute(["gate"], inputs={"x": 1, "events": free}) == {"gate": 1}
    held = (threading.Event(), threading.Event())
    holder = started(lambda: driver.execute(["gate"], inputs={"x": 2, "events": held}))
    try:
        assert held[0].wait(5)
        timer = threading.Timer(0.2, held[1].set)
        timer.start()
        again = (threading.Event(), threading.Event())
        again[1].set()
        stale = module.CONTEXTS[0]
        result = stale.run(driver.execute, ["gate"], inputs={"x": 3, "events": again})
        assert result == {"gate": 3}
        timer.join(5)
    finally:
        held[1].set()
        joined(holder)


class RunsBefore(fnloom.Hook):
    """In `before_node` of `alone`, runs `alone` in another run, once."""

    def __init__(self) -> None:
        """Not run yet."""
        self.driver: fnloom.Driver | None = None
        self.ran = False
        self.results: list[object] = []

    @override
    def before_node(self, event: fnloom.NodeStart) -> None:
        if event.node.name == "alone" and self.driver is not None and not self.ran:
            self.ran = True
            self.results.append(
                self.driver.execute(["alone"], inputs={"x": 7, "pause": 0.0})
            )


@THREADS
def test_a_before_node_hook_may_run_its_own_node(threads: int | None) -> None:
    """No slot is held while `before_node` runs: the hook's own run of the node runs."""
    hook = RunsBefore()
    driver, _ = counted(f"limit_before_node_{threads}", threads, hooks=(hook,))
    hook.driver = driver
    assert driver.execute(["alone"], inputs={"x": 1, "pause": 0.0}) == {"alone": 1}
    assert hook.results == [{"alone": 7}]


# Finding: a hook holding the run's busy flag runs the driver on a group a sibling
# task holds; the sibling waits for the flag. It must give its slot back meanwhile.
SIBLINGS = """
import contextvars
import time

from fnloom import limit

DRIVER = []
NESTED = contextvars.ContextVar("NESTED", default=False)
""" + "".join(
    f"""

@limit(group="db")
def x{i}(v: int) -> int:
    time.sleep(0.005)
    return v


def h{i}(v: int) -> int:
    return v
"""
    for i in range(8)
)


class RunsInBefore(fnloom.Hook):
    """In `before_node` of each `h*` node, runs `x0` of the same driver, unnested."""

    def __init__(self, module: types.ModuleType) -> None:
        """Runs `module.DRIVER[0]`."""
        self.module = module

    @override
    def before_node(self, event: fnloom.NodeStart) -> None:
        nested = self.module.NESTED
        if event.node.name.startswith("h") and not nested.get():
            nested.set(True)
            self.module.DRIVER[0].execute(["x0"], inputs={"v": 1})


def test_a_hook_running_the_driver_never_waits_on_a_sibling_holding_its_slot() -> None:
    """A task waiting for the run's hooks holds no slot, so a hook's own run gets it."""
    module = make_module("limit_siblings", SIBLINGS)
    driver = (
        fnloom.Builder()
        .with_modules(module)
        .with_limits({"db": 1})
        .with_hooks(RunsInBefore(module))
        .with_threads(4)
        .build()
    )
    module.DRIVER.append(driver)
    names = [f"x{i}" for i in range(8)] + [f"h{i}" for i in range(8)]
    results: list[object] = []

    def run() -> None:
        results.extend(driver.execute(names, inputs={"v": v}) for v in range(10))

    joined(started(run))
    assert len(results) == 10  # noqa: PLR2004  # one per run


# A node runs its own driver on a thread it starts, then joins it.
FROM_A_THREAD = """
import asyncio
import contextvars
import threading

DRIVER = []
OUT = []


def b(x: int) -> int:
    return x + 1


def a(x: int, how: str) -> int:
    def inner() -> None:
        OUT.append(DRIVER[0].execute(["b"], inputs={"x": x})["b"])

    if how == "to_thread":
        asyncio.run(asyncio.to_thread(inner))
        return OUT[-1]
    context = contextvars.copy_context() if how == "context" else None
    thread = threading.Thread(target=inner, context=context)
    thread.start()
    thread.join(5)
    return -1 if thread.is_alive() else OUT[-1]
"""

HOWS = [
    "context",
    "to_thread",
    pytest.param(
        "inherited",
        marks=pytest.mark.skipif(
            not sys.flags.thread_inherit_context,
            reason="only where threads inherit their starter's context (3.14t)",
        ),
    ),
]


@pytest.mark.parametrize("how", HOWS)
def test_a_run_started_on_a_thread_a_node_starts_uses_its_worker_slot(how: str) -> None:
    """One worker, held by `a`: a run on a thread `a` waits for takes no other slot."""
    module = make_module(f"limit_from_thread_{how}", FROM_A_THREAD)
    driver = fnloom.Builder().with_modules(module).with_threads(1).build()
    module.DRIVER.append(driver)
    assert driver.execute(["a"], inputs={"x": 1, "how": how}) == {"a": 2}


def test_a_panic_before_a_tasks_completion_fails_the_run_and_ends_it() -> None:
    """The task is completed as failed: the run raises at once instead of hanging."""
    driver, _ = counted("limit_perform_panic", 2)
    results: list[object] = []

    def run() -> None:
        _core.arm_perform_panic()
        try:
            driver.execute(["loads"], inputs={"n": 8, "pause": 0.0})
        except BaseException as error:  # noqa: BLE001  # a PanicException is the point
            results.append(type(error).__name__)

    joined(started(run))
    assert results == ["PanicException"]
    assert driver.execute(["loads"], inputs={"n": 4, "pause": 0.0}) == {
        "loads": [0, 1, 2, 3]
    }
