"""Blocks on the async driver: async generators as expands (Plan 11, task 8, Q132).

The async driver runs a fan-out as the sync driver does, on the core scheduler, with
an `async def` generator's items awaited on the loop. Every wait is bounded.
"""

import asyncio
import threading
from typing import TYPE_CHECKING, Any, Literal, override

import pytest

import fnloom
from fnloom import MemoryCache
from helpers import make_module
from test_hooks_async import Events

if TYPE_CHECKING:
    from collections.abc import Coroutine

# How long a test waits before calling the run stuck.
PATIENCE = 10.0


def _run(coroutine: Coroutine[Any, Any, Any]) -> Any:  # noqa: ANN401  # what the run returns
    """`coroutine` on a fresh loop, bounded, so a hang fails the test."""
    return asyncio.run(asyncio.wait_for(coroutine, PATIENCE))


FANNED = """
import asyncio

from fnloom import Collect, Parallelizable, current_branch

SEEN = []


async def picked(n: int) -> Parallelizable[int]:
    for item in range(n):
        await asyncio.sleep(0)
        yield item


async def doubled(picked: int) -> int:
    await asyncio.sleep(0.01 * (5 - picked))
    SEEN.append(current_branch())
    return picked * 2


def plus(doubled: int) -> int:
    return doubled + 1


def total(plus: Collect[int]) -> list[int]:
    return plus
"""


@pytest.mark.parametrize("sync_nodes", ["thread", "inline"])
def test_an_async_generator_expand_fans_out(
    sync_nodes: Literal["thread", "inline"],
) -> None:
    """Each item runs its branch, each node in its own branch, collected in order."""
    module = make_module(f"async_fanned_{sync_nodes}", FANNED)
    driver = fnloom.Builder().with_modules(module).build_async(sync_nodes=sync_nodes)
    assert _run(driver.execute(["total"], inputs={"n": 5})) == {
        "total": [1, 3, 5, 7, 9]
    }
    assert sorted(module.SEEN) == [(i,) for i in range(5)]


def test_async_collect_is_in_branch_order() -> None:
    """Branches finish in reverse, and the collect is still in item order."""
    module = make_module("async_reversed", FANNED)
    driver = fnloom.Builder().with_modules(module).with_threads(4).build_async()
    assert _run(driver.execute(["total"], inputs={"n": 5})) == {
        "total": [1, 3, 5, 7, 9]
    }
    # The last item's branch, which slept least, finished first.
    assert module.SEEN[0] == (4,)


MEETING = """
import asyncio

from fnloom import Collect, Parallelizable

MEET = []


async def picked(n: int) -> Parallelizable[int]:
    for item in range(n):
        yield item


async def met(picked: int) -> int:
    await asyncio.wait_for(MEET[0].wait(), 5)
    return picked


def gathered(met: Collect[int]) -> list[int]:
    return met
"""


def test_async_branches_overlap() -> None:
    """Four `async def` branch nodes meet at an `asyncio.Barrier(4)` within 5 s."""
    module = make_module("async_meeting", MEETING)
    driver = fnloom.Builder().with_modules(module).with_threads(4).build_async()

    async def scenario() -> object:
        module.MEET.append(asyncio.Barrier(4))
        return await driver.execute(["gathered"], inputs={"n": 4})

    assert _run(scenario()) == {"gathered": [0, 1, 2, 3]}


FAILING = """
import asyncio

from fnloom import Collect, Parallelizable

CLOSED = []
CANCELLED = []
STARTED = []


async def picked(n: int) -> Parallelizable[int]:
    try:
        for item in range(n):
            yield item
        await asyncio.Event().wait()
    finally:
        CLOSED.append(True)


async def details(picked: int) -> int:
    STARTED.append(picked)
    if picked == 2:
        while len(STARTED) < 4:
            await asyncio.sleep(0)
        raise KeyError(picked)
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        CANCELLED.append(picked)
        raise
    return picked


def summary(details: Collect[int]) -> list[int]:
    return details
"""


def test_a_failing_async_branch_cancels_the_others_and_closes_the_generator_once() -> (
    None
):
    """Branch 2 fails: the waiting branches are cancelled, the generator closed once."""
    module = make_module("async_failing_branch", FAILING)
    driver = fnloom.Builder().with_modules(module).with_threads(4).build_async()
    with pytest.raises(KeyError) as info:
        _run(driver.execute(["summary"], inputs={"n": 4}))
    assert info.value.args == (2,)
    assert info.value.__notes__ == [
        "while computing 'details' for branch (2,) of 'picked'"
    ]
    assert sorted(module.CANCELLED) == [0, 1, 3]
    assert module.CLOSED == [True]


PULLING = """
import asyncio

from fnloom import Collect, Parallelizable

CLOSED = []
PULLING = asyncio.Event()


async def picked(n: int) -> Parallelizable[int]:
    try:
        yield 0
        PULLING.set()
        await asyncio.Event().wait()
        yield 1
    finally:
        CLOSED.append(True)


async def details(picked: int) -> int:
    return picked


def summary(details: Collect[int]) -> list[int]:
    return details
"""


def test_a_cancelled_caller_closes_a_generator_mid_pull_once() -> None:
    """The caller is cancelled while a pull waits: the generator is closed, once."""
    module = make_module("async_pulling", PULLING)
    driver = fnloom.Builder().with_modules(module).build_async()

    async def scenario() -> None:
        run = asyncio.ensure_future(driver.execute(["summary"], inputs={"n": 2}))
        await asyncio.wait_for(module.PULLING.wait(), 5)
        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run
        assert module.CLOSED == [True]

    _run(scenario())


SYNC_FANNED = """
import threading

from fnloom import Collect, Parallelizable, current_branch

NAMES = []


def picked(n: int) -> Parallelizable[int]:
    for item in range(n):
        NAMES.append(threading.current_thread().name)
        yield item


def branch_of(picked: int) -> tuple[int, ...]:
    return current_branch()


async def tripled(picked: int, branch_of: tuple[int, ...]) -> int:
    assert branch_of == (picked,)
    return picked * 3


def everything(tripled: Collect[int]) -> list[int]:
    return tripled
"""


@pytest.mark.parametrize(
    ("sync_nodes", "threads"),
    [("thread", None), ("inline", None), ("thread", 2)],
    ids=["executor", "inline", "pool"],
)
def test_a_sync_generator_expand_runs_where_its_node_would(
    sync_nodes: Literal["thread", "inline"], threads: int | None
) -> None:
    """A sync generator's items are read in a thread, on the loop, or on the pool."""
    module = make_module(f"async_sync_fanned_{sync_nodes}_{threads}", SYNC_FANNED)
    builder = fnloom.Builder().with_modules(module)
    if threads is not None:
        builder = builder.with_threads(threads)
    driver = builder.build_async(sync_nodes=sync_nodes)
    assert _run(driver.execute(["everything"], inputs={"n": 4})) == {
        "everything": [0, 3, 6, 9]
    }
    names = set(module.NAMES)
    if threads is not None:
        assert all(name.startswith("fnloom-worker-") for name in names)
    elif sync_nodes == "inline":
        assert names == {threading.current_thread().name}
    else:
        assert threading.current_thread().name not in names


CONTEXTUAL = """
import contextvars
import decimal

from fnloom import Collect, Parallelizable

SCALE = contextvars.ContextVar("scale", default=1)


async def picked(n: int) -> Parallelizable[str]:
    SCALE.set(10)
    with decimal.localcontext() as context:
        context.prec = 3
        for item in range(n):
            yield str(decimal.Decimal(1) / decimal.Decimal(3 + item) * SCALE.get())


def unscaled(picked: str) -> str:
    return f"{picked}:{SCALE.get()}"


def listed(unscaled: Collect[str]) -> list[str]:
    return unscaled
"""


def test_an_async_generators_context_lasts_across_its_items() -> None:
    """A variable an async generator sets, and its decimal context, last per item.

    Its branches run in the caller's context, not the generator's.
    """
    module = make_module("async_contextual", CONTEXTUAL)
    driver = fnloom.Builder().with_modules(module).with_threads(4).build_async()
    assert _run(driver.execute(["listed"], inputs={"n": 3})) == {
        "listed": ["3.33:1", "2.50:1", "2.0:1"]
    }


NESTED = """
from fnloom import Collect, Parallelizable


async def outer(sizes: list[int]) -> Parallelizable[int]:
    for size in sizes:
        yield size


def inner(outer: int) -> Parallelizable[int]:
    return range(outer)


async def leaf(inner: int, outer: int) -> int:
    return inner * 10 + outer


def per_outer(leaf: Collect[int]) -> list[int]:
    return leaf


def everything(per_outer: Collect[list[int]]) -> list[list[int]]:
    return per_outer
"""


def test_nested_async_blocks_collect_per_branch() -> None:
    """An async expand around a sync one: each outer branch collects its own."""
    module = make_module("async_nested_blocks", NESTED)
    sync = fnloom.Builder().with_modules(
        make_module("async_nested_blocks_sync", NESTED.replace("async def", "def"))
    )
    expected = {"everything": [[1], [], [3, 13, 23]]}
    assert sync.build().execute(["everything"], inputs={"sizes": [1, 0, 3]}) == expected
    driver = fnloom.Builder().with_modules(module).with_threads(4).build_async()
    assert _run(driver.execute(["everything"], inputs={"sizes": [1, 0, 3]})) == expected


ITEMS = """
from fnloom import Collect, Parallelizable


async def picked(n: int) -> Parallelizable[int]:
    yield 1
    yield "two"


def kept(picked: int) -> int:
    return picked


def every(kept: Collect[int]) -> list[int]:
    return kept
"""


def test_an_async_generators_items_are_checked() -> None:
    """An item of the wrong type fails the expand, as on the sync driver (Q127)."""
    driver = fnloom.Builder().with_modules(make_module("async_items", ITEMS))
    with pytest.raises(TypeError) as info:
        _run(driver.build_async().execute(["every"], inputs={"n": 2}))
    assert str(info.value) == "item 1 of 'picked': expected int, got str"
    assert info.value.__notes__ == ["in node 'picked'"]


def test_an_async_generator_on_build_is_refused() -> None:
    """`build()` refuses an async generator expand, naming `build_async()` (Q32)."""
    with pytest.raises(ValueError, match="build_async") as info:
        fnloom.build(make_module("async_gen_sync_build", FANNED))
    assert str(info.value) == (
        "'picked': async generator function is not supported by build(), which "
        "calls nodes synchronously; build it with build_async()"
    )


def test_an_async_generator_that_is_no_expand_stays_refused() -> None:
    """An async generator not annotated `-> Parallelizable[...]` is refused (Q32)."""
    module = make_module(
        "async_gen_plain",
        """
async def numbers(n: int) -> list[int]:
    yield n
""",
    )
    with pytest.raises(ValueError, match="async generator function") as info:
        fnloom.Builder().with_modules(module).build_async()
    assert str(info.value) == (
        "'numbers': async generator function is not supported; calling it returns "
        "a generator, not the node's value"
    )


CACHED = """
import asyncio

from fnloom import Collect, Parallelizable, cache

CALLS = []


async def picked(items: list[int]) -> Parallelizable[int]:
    for item in items:
        yield item


@cache(version="1")
async def details(picked: int) -> int:
    CALLS.append(picked)
    await asyncio.sleep(0)
    return picked * 10


def summary(details: Collect[int]) -> list[int]:
    return details
"""


class _Keys(MemoryCache):
    """A memory store that records each key it is given."""

    def __init__(self) -> None:
        """No key yet."""
        super().__init__()
        self.keys: set[bytes] = set()

    @override
    def put(self, key: bytes, value: object) -> None:
        self.keys.add(key)
        super().put(key, value)


def test_async_blocks_cache_as_sync_blocks_do() -> None:
    """The same keys as `Driver`, and a second run, one item longer, calls one node.

    The keys are compared over one module of sync nodes, since a key holds its code.
    """
    sync_module = make_module(
        "async_blocks_cached_sync",
        CACHED.replace("async def", "def").replace("await asyncio.sleep(0)", ""),
    )
    sync_store, async_store = _Keys(), _Keys()
    inputs = {"items": [1, 2, 3]}
    builder = fnloom.Builder().with_modules(sync_module)
    sync = builder.with_cache(sync_store).build()
    assert sync.execute(["summary"], inputs=inputs) == {"summary": [10, 20, 30]}
    on_loop = builder.with_cache(async_store).build_async(sync_nodes="inline")
    assert _run(on_loop.execute(["summary"], inputs=inputs)) == {
        "summary": [10, 20, 30]
    }
    assert async_store.keys == sync_store.keys
    async_module = make_module("async_blocks_cached", CACHED)
    driver = (
        fnloom.Builder()
        .with_modules(async_module)
        .with_cache(MemoryCache())
        .build_async()
    )
    assert _run(driver.execute(["summary"], inputs=inputs)) == {"summary": [10, 20, 30]}
    async_module.CALLS.clear()
    longer = {"items": [1, 2, 3, 4]}
    assert _run(driver.execute(["summary"], inputs=longer)) == {
        "summary": [10, 20, 30, 40]
    }
    assert async_module.CALLS == [4]


SEQUENCED = """
from fnloom import ABSENT, Absent, Collect, Parallelizable


def picked(n: int) -> Parallelizable[int]:
    yield from range(n)


def odd(picked: int) -> int | Absent:
    return picked if picked % 2 else ABSENT


def tenfold(odd: int) -> int:
    return odd * 10


def gathered(tenfold: Collect[int]) -> list[int]:
    return tenfold
"""


def test_async_block_events_equal_the_sync_drivers_with_one_worker() -> None:
    """With `sync_nodes="inline"`, a block's events and skips are `Driver`'s."""
    module = make_module("async_block_events", SEQUENCED)
    sync_events, async_events = Events(), Events()
    fnloom.Builder().with_modules(module).with_hooks(sync_events).build().execute(
        ["gathered"], inputs={"n": 4}
    )
    _run(
        fnloom.Builder()
        .with_modules(module)
        .with_hooks(async_events)
        .build_async(sync_nodes="inline")
        .execute(["gathered"], inputs={"n": 4})
    )
    assert async_events.seen == sync_events.seen
    assert ("expand", "picked", (), (1,)) in sync_events.seen
    assert sync_events.seen[-1] == ("run", True, (("tenfold", (0,)), ("tenfold", (2,))))
