"""Concurrent use of one driver and one plan, free-threaded imports, and stress."""

import os
import sys
import sysconfig
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any, cast, override

import pytest

import fnloom
from fnloom import _core
from helpers import make_module
from helpers import run as run_driver

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

THREADS = 8
RUNS = 500

SOURCE = """
def total(step: float, steps: int) -> float:
    return step * steps

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
    plan = driver.prepare(["label"], inputs={"step": 0.0, "steps": 0})

    def work(thread: int) -> None:
        for run in range(RUNS):
            steps = thread * RUNS + run
            expected = f"{steps * 2.0:.2f}"
            assert plan.run(inputs={"step": 2.0, "steps": steps}) == {"label": expected}
            # Alternate two request shapes, so the plan cache and its fast path churn.
            outputs = ["label"] if run % 2 else ["total", "label"]
            result = driver.execute(outputs, inputs={"step": 2.0, "steps": steps})
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
    upstream = driver.what_is_upstream_of("gathered")
    # Every thread starts at once, so all 16 overlap rather than a few at a time.
    start = threading.Barrier(MIXED_THREADS)

    def work(thread: int) -> int:
        checked = 0
        start.wait()
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
                assert driver.what_is_upstream_of("gathered") == upstream
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


class _Recording(fnloom.Hook):
    """Records every event as a tuple whose second item is its run's id."""

    def __init__(self) -> None:
        self.events: list[tuple[object, ...]] = []
        self._lock = threading.Lock()

    def _add(self, *event: object) -> None:
        with self._lock:
            self.events.append(event)

    @override
    def before_run(self, event: fnloom.RunStart) -> None:
        self._add("before_run", event.run_id, event.nodes)

    @override
    def before_node(self, event: fnloom.NodeStart) -> None:
        status = None if event.cache is None else event.cache.status
        self._add("before_node", event.run_id, event.node.name, status)

    @override
    def after_node(self, event: fnloom.NodeEnd) -> None:
        status = None if event.cache is None else event.cache.status
        self._add("after_node", event.run_id, event.node.name, status, event.ok)

    @override
    def after_run(self, event: fnloom.RunEnd) -> None:
        self._add("after_run", event.run_id, event.ok)


def _check_whole(run: list[tuple[object, ...]]) -> list[object]:
    """Asserts one run's events are whole and in order; returns its nodes' statuses."""
    first, *middle, last = run
    assert first[0] == "before_run", run
    assert last == ("after_run", first[1], True), run
    assert len(middle) % 2 == 0, run
    befores, afters = middle[::2], middle[1::2]
    assert all(event[0] == "before_node" for event in befores), run
    assert all(event[0] == "after_node" for event in afters), run
    # Each node's after_node comes right after its before_node, with its outcome.
    for before, after in zip(befores, afters, strict=True):
        assert after[2:4] == before[2:4], run
        assert after[4] is True, run
    # The nodes reported are exactly those `before_run` announced, in its order.
    assert tuple(event[2] for event in befores) == first[2], run
    return [event[3] for event in befores]


def test_one_hook_shared_by_threads_sees_each_run_whole() -> None:
    """16 threads over one hooked, cached driver: each run's events are whole.

    Grouped by `run_id`, a run's events are `before_run`, each node's pair in
    the order `before_run` named them, then `after_run`: nothing missing, and
    nothing from another run in between.
    """
    hook = _Recording()
    driver = (
        fnloom.Builder()
        .with_modules(make_module("hooked_mixed", MIXED))
        .with_modules(make_module("hooked_shifted", SHIFTED), namespace="shifted")
        .with_config({"region": "eu"})
        .with_cache(fnloom.MemoryCache())
        .with_hooks(hook)
        .build()
    )
    plan = driver.prepare(["gathered"], inputs={"n": 0})
    start = threading.Barrier(MIXED_THREADS)

    def work(thread: int) -> None:
        start.wait()
        for run in range(MIXED_RUNS):
            n = thread * 1_000 + run % REPEAT
            if run % 2:
                result = driver.execute(["gathered", "high"], inputs={"n": n})
            else:
                result = plan.run(inputs={"n": n})
            assert result["gathered"] == _gathered(n), (thread, run, result)

    with ThreadPoolExecutor(MIXED_THREADS) as pool:
        for future in [pool.submit(work, thread) for thread in range(MIXED_THREADS)]:
            future.result()

    runs: dict[object, list[tuple[object, ...]]] = {}
    for event in hook.events:
        runs.setdefault(event[1], []).append(event)
    assert len(runs) == MIXED_THREADS * MIXED_RUNS
    statuses = [status for run in runs.values() for status in _check_whole(run)]
    # The cache both stored and hit while the threads ran.
    assert {"hit", "miss"} <= set(statuses)


BLOCKED = """
from fnloom import Collect, Parallelizable, current_branch


def outer(sizes: list[int]) -> Parallelizable[int]:
    yield from sizes


def inner(outer: int) -> Parallelizable[int]:
    yield from range(outer)


def leaf(inner: int, outer: int, sizes: list[int]) -> int:
    branch = current_branch()
    if (sizes[branch[0]], branch[1]) != (outer, inner):
        raise AssertionError(f"read branch {branch} for item ({outer}, {inner})")
    return outer * 10 + inner


def per_outer(leaf: Collect[int]) -> int:
    return sum(leaf)


def everything(per_outer: Collect[int]) -> list[int]:
    return list(per_outer)
"""

BLOCK_RUNS = 60


def _everything(sizes: list[int]) -> list[int]:
    """`everything` in closed form."""
    return [sum(size * 10 + inner for inner in range(size)) for size in sizes]


def test_a_block_graph_shared_by_threads() -> None:
    """16 threads share one cached driver over a nested fan-out, hits and misses.

    Each result is the single-threaded one, and `current_branch()` inside a node is
    its own branch: a run's late keys and item iterators, and the branch variable,
    are its own.
    """
    module = make_module("threaded_blocks", BLOCKED)
    store = fnloom.MemoryCache()
    driver = fnloom.Builder().with_modules(module).with_cache(store).build()
    start = threading.Barrier(MIXED_THREADS)

    def work(thread: int) -> int:
        start.wait()
        for run in range(BLOCK_RUNS):
            # Repeats every 12 runs per thread, and overlaps other threads' items.
            sizes = [(thread + run) % 4 + 1, run % 3 + 1, (thread * run) % 2 + 1]
            result = driver.execute(["everything"], inputs={"sizes": sizes})
            assert result == {"everything": _everything(sizes)}, (thread, run)
        return BLOCK_RUNS

    with ThreadPoolExecutor(MIXED_THREADS) as pool:
        done = [pool.submit(work, thread) for thread in range(MIXED_THREADS)]
        assert sum(future.result() for future in done) == MIXED_THREADS * BLOCK_RUNS
    assert len(store) > 0


def _mutated(kind: str) -> list[int] | dict[int, int] | set[int]:
    """A container of `kind` large enough that reading it takes a while."""
    if kind == "dict":
        return {i: i for i in range(2000)}
    if kind == "set":
        return set(range(2000))
    return list(range(2000))


@pytest.mark.parametrize("kind", ["dict", "set", "list"])
def test_a_container_changed_by_another_thread_is_fingerprinted_whole(
    kind: str,
) -> None:
    """A container another thread is changing is read whole, never mid-change.

    Without the GIL, reading a dict or a set while another thread resized it panicked
    inside the binding. Each read now holds the container's lock, so it sees one state.
    With the GIL there is no race, and the test passes either way.
    """
    value = _mutated(kind)
    stop = threading.Event()

    def mutate() -> None:
        step = 0
        while not stop.is_set():
            step += 1
            added, dropped = 10_000 + step % 50, 10_000 + (step + 25) % 50
            if isinstance(value, dict):
                value[added] = step
                value.pop(dropped, None)
            elif isinstance(value, set):
                value.add(added)
                value.discard(dropped)
            else:
                value.append(added)
                value.pop(0)

    thread = threading.Thread(target=mutate)
    thread.start()
    try:
        for _ in range(RUNS):
            assert len(fnloom.fingerprint(value)) == 32  # noqa: PLR2004  # a digest's size
    finally:
        stop.set()
        thread.join()


# The stress tests: each marked `threads`, and repeated `ROUNDS` times; the CI
# `stress` job runs 200. Every counter they keep is changed under a lock, since
# `+=` on a shared value is no atomic step on 3.14t.
ROUNDS = int(os.environ.get("FNLOOM_STRESS_ROUNDS", "10"))
CALLERS = 16
WORKERS = 8
# How long a caller thread may take for all its rounds, at most.
CALLER_TIMEOUT = 120.0


def _sizes(thread: int, run: int) -> list[int]:
    """A nested fan-out's sizes, repeating every 12 runs, overlapping other threads'."""
    return [(thread + run) % 4 + 1, run % 3 + 1, (thread * run) % 2 + 1]


def _callers[T](work: Callable[[int], T], count: int = CALLERS) -> list[T]:
    """`work(thread)` on `count` threads started together; their results, in order.

    Every thread meets at a barrier first, so all of them overlap, and each is
    given `CALLER_TIMEOUT` seconds: a hang fails the test, not the session.
    """
    start = threading.Barrier(count, timeout=CALLER_TIMEOUT)

    def started(thread: int) -> T:
        start.wait()
        return work(thread)

    with ThreadPoolExecutor(count) as pool:
        done = [pool.submit(started, thread) for thread in range(count)]
        return [future.result(timeout=CALLER_TIMEOUT) for future in done]


def _held(driver: fnloom.Driver[Any] | fnloom.AsyncDriver[Any]) -> object:
    """The units of the driver's caps held now: workers, nodes, groups."""
    inner = driver._driver if isinstance(driver, fnloom.AsyncDriver) else driver  # noqa: SLF001  # its sync driver
    workers = inner._workers  # noqa: SLF001  # the driver's own limits
    assert workers is not None
    return _core.held_slots(workers.limits())


@pytest.mark.threads
@pytest.mark.parametrize("store", ["memory", "disk"])
def test_parallel_runs_share_one_driver_pool_and_cache(
    store: str, tmp_path: Path
) -> None:
    """16 callers each run a nested, cached fan-out on one `with_threads(8)` driver.

    Every result is the single-threaded one, `current_branch()` is each node's
    own, and once they end no worker slot is held.
    """
    cache: fnloom.MemoryCache | fnloom.DiskCache = (
        fnloom.MemoryCache() if store == "memory" else fnloom.DiskCache(tmp_path)
    )
    driver = (
        fnloom.Builder()
        .with_modules(make_module(f"stress_shared_{store}", BLOCKED))
        .with_cache(cache)
        .with_threads(WORKERS)
        .build()
    )

    def work(thread: int) -> int:
        for run in range(ROUNDS):
            sizes = _sizes(thread, run)
            result = driver.execute(["everything"], inputs={"sizes": sizes})
            assert result == {"everything": _everything(sizes)}, (thread, run)
        return ROUNDS

    assert sum(_callers(work)) == CALLERS * ROUNDS
    assert _held(driver) == (0, {}, {})
    # What the callers stored is there for a later run.
    explained = driver.explain_cache(["everything"], inputs={"sizes": _sizes(0, 0)})
    assert explained["everything"].status == "hit"


# Each node of `BLOCKED` with the length of its branch, and the nodes it reads.
_DEPTHS = {"outer": 0, "inner": 1, "leaf": 2, "per_outer": 1, "everything": 0}
_READS = {
    "outer": (),
    "inner": ("outer",),
    "leaf": ("inner", "outer"),
    "per_outer": ("leaf",),
    "everything": ("per_outer",),
}

type _Call = tuple[str, tuple[int, ...]]


class _Ordered(fnloom.Hook):
    """Every run's sizes, and its node events in the order the hook heard them."""

    def __init__(self) -> None:
        self.sizes: dict[str, list[int]] = {}
        self.events: dict[str, list[tuple[str, _Call]]] = {}
        self._lock = threading.Lock()

    @override
    def before_run(self, event: fnloom.RunStart) -> None:
        with self._lock:
            self.sizes[event.run_id] = cast("list[int]", event.inputs["sizes"])

    @override
    def before_node(self, event: fnloom.NodeStart) -> None:
        with self._lock:
            self.events.setdefault(event.run_id, []).append(
                ("before", (event.node.name, event.branch))
            )

    @override
    def after_node(self, event: fnloom.NodeEnd) -> None:
        with self._lock:
            self.events.setdefault(event.run_id, []).append(
                ("after", (event.node.name, event.branch))
            )


def _calls(sizes: list[int]) -> set[_Call]:
    """Every `(node, branch)` a run of `everything` over `sizes` calls."""
    calls: set[_Call] = {("outer", ()), ("everything", ())}
    for i, size in enumerate(sizes):
        calls |= {("inner", (i,)), ("per_outer", (i,))}
        calls |= {("leaf", (i, j)) for j in range(size)}
    return calls


def _read_ended(call: _Call, ended: set[_Call], sizes: list[int]) -> bool:
    """Whether every input `call` reads has ended: each branch of a collected one."""
    name, branch = call
    for read in _READS[name]:
        depth = _DEPTHS[read]
        if depth <= len(branch):
            if (read, branch[:depth]) not in ended:
                return False
            continue
        # A collect, one branch deeper: every branch of its block.
        width = sizes[branch[0]] if branch else len(sizes)
        if any((read, (*branch, i)) not in ended for i in range(width)):
            return False
    return True


def _check_ordered(events: list[tuple[str, _Call]], sizes: list[int]) -> None:
    """One run's events: each node once, started after its inputs ended, then ended."""
    started: set[_Call] = set()
    ended: set[_Call] = set()
    for kind, call in events:
        if kind == "before":
            assert call not in started, (call, events)
            assert _read_ended(call, ended, sizes), (call, events)
            started.add(call)
        else:
            assert call in started, (call, events)
            assert call not in ended, (call, events)
            ended.add(call)
    assert started == ended == _calls(sizes), events


@pytest.mark.threads
def test_a_shared_hook_sees_each_parallel_run_whole() -> None:
    """16 callers share one hooked `with_threads(8)` driver over a nested fan-out.

    Grouped by `run_id`, each run calls every node once, each `before_node`
    precedes its `after_node`, and a node starts only after each input it reads
    has ended: a collect after every branch it gathers.
    """
    hook = _Ordered()
    driver = (
        fnloom.Builder()
        .with_modules(make_module("stress_ordered", BLOCKED))
        .with_hooks(hook)
        .with_threads(WORKERS)
        .build()
    )

    def work(thread: int) -> None:
        for run in range(ROUNDS):
            sizes = _sizes(thread, run)
            assert driver.execute(["everything"], inputs={"sizes": sizes}) == {
                "everything": _everything(sizes)
            }

    _callers(work)
    assert len(hook.sizes) == len(hook.events) == CALLERS * ROUNDS
    for run_id, events in hook.events.items():
        _check_ordered(events, hook.sizes[run_id])


@pytest.mark.threads
def test_run_many_and_execute_mixed_on_one_driver() -> None:
    """Callers mix `run_many`, `PreparedPlan.run` and `execute` on one driver."""
    driver = (
        fnloom.Builder()
        .with_modules(make_module("stress_mixed_calls", BLOCKED))
        .with_threads(WORKERS)
        .build()
    )
    plan = driver.prepare(["everything"], inputs={"sizes": []})

    def work(thread: int) -> int:
        checked = 0
        for run in range(ROUNDS):
            sets = [_sizes(thread, run + k) for k in range(4)]
            if thread % 3 == 0:
                results = plan.run_many([{"sizes": sizes} for sizes in sets])
            elif thread % 3 == 1:
                results = [plan.run(inputs={"sizes": sizes}) for sizes in sets]
            else:
                results = [
                    driver.execute(["everything"], inputs={"sizes": sizes})
                    for sizes in sets
                ]
            assert results == [{"everything": _everything(s)} for s in sets]
            checked += len(sets)
        return checked

    assert sum(_callers(work)) == CALLERS * ROUNDS * 4
    assert _held(driver) == (0, {}, {})


@pytest.mark.threads
def test_async_and_sync_drivers_of_one_builder_under_load() -> None:
    """A builder's `Driver` and `AsyncDriver`, each with its own pool, 16 callers."""
    builder = (
        fnloom.Builder()
        .with_modules(make_module("stress_sync_async", BLOCKED))
        .with_cache(fnloom.MemoryCache())
        .with_threads(4)
    )
    sync, async_ = builder.build(), builder.build_async()

    def work(thread: int) -> int:
        for run in range(ROUNDS):
            sizes = _sizes(thread, run)
            driver = async_ if (thread + run) % 2 else sync
            got = run_driver(driver, ["everything"], inputs={"sizes": sizes})
            assert got == {"everything": _everything(sizes)}, (thread, run)
        return ROUNDS

    assert sum(_callers(work)) == CALLERS * ROUNDS
    assert _held(sync) == _held(async_) == (0, {}, {})


# A limited node that records how many of its calls overlap, and the most seen.
CONTENDED = """
import threading
import time

from fnloom import Collect, Parallelizable, limit

LOCK = threading.Lock()
RUNNING = [0]
PEAK = [0]


def item(n: int) -> Parallelizable[int]:
    yield from range(n)


def _enter() -> None:
    with LOCK:
        RUNNING[0] += 1
        PEAK[0] = max(PEAK[0], RUNNING[0])


def _leave() -> None:
    with LOCK:
        RUNNING[0] -= 1


@limit(group="store")
def read(item: int) -> int:
    _enter()
    try:
        time.sleep(0)
        return item
    finally:
        _leave()


@limit(2, group="store")
def write(read: int) -> int:
    _enter()
    try:
        time.sleep(0)
        return read * 2
    finally:
        _leave()


def total(write: Collect[int]) -> int:
    return sum(write)


@limit(2)
def single(x: int) -> int:
    _enter()
    try:
        time.sleep(0.001)
        return x + 1
    finally:
        _leave()
"""
BRANCHES = 500


@pytest.mark.threads
def test_limits_never_overshoot_under_contention() -> None:
    """A group of 3 over 500 branches on eight workers: never four calls at once."""
    module = make_module("stress_contended", CONTENDED)
    driver = (
        fnloom.Builder()
        .with_modules(module)
        .with_threads(WORKERS)
        .with_limits({"store": 3})
        .build()
    )
    expected = sum(range(BRANCHES)) * 2
    for _ in range(ROUNDS):
        assert driver.execute(["total"], inputs={"n": BRANCHES}) == {"total": expected}
    assert 1 <= module.PEAK[0] <= 3  # noqa: PLR2004  # the group's size
    assert module.RUNNING == [0]
    assert _held(driver) == (0, {"single": 0, "write": 0}, {"store": 0})


@pytest.mark.threads
def test_a_sequential_driver_under_16_threads_honours_its_limits() -> None:
    """A `@limit(2)` node of a driver without threads, called by 16 threads at once."""
    module = make_module("stress_sequential_limits", CONTENDED)
    driver = fnloom.Builder().with_modules(module).with_limits({"store": 3}).build()

    def work(thread: int) -> int:
        for run in range(ROUNDS):
            x = thread * 1_000 + run
            assert driver.execute(["single"], inputs={"x": x}) == {"single": x + 1}
        return ROUNDS

    assert sum(_callers(work)) == CALLERS * ROUNDS
    assert 1 <= module.PEAK[0] <= 2  # noqa: PLR2004  # the node's cap
    assert module.RUNNING == [0]
    assert _held(driver) == (0, {"single": 0, "write": 0}, {"store": 0})
