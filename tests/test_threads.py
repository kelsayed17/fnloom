"""Concurrent use of one driver and one plan, and free-threaded imports."""

import sys
import sysconfig
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import override

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
