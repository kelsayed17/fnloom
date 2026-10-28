"""The async driver: built synchronously, run on a loop, checked as the sync one."""

import asyncio
import copy
import pickle
import threading
from typing import TYPE_CHECKING, Any

import pytest

import fnloom
from fnloom import _async
from helpers import make_module

if TYPE_CHECKING:
    from collections.abc import Coroutine, Sequence
    from types import ModuleType

# How long a test waits on an event before calling the run stuck: generous, so a
# slow CI runner never trips it, and never reached when the code is right.
PATIENCE = 10.0


def _run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    """Runs `coroutine` on a fresh loop, as a caller without a loop would."""
    return asyncio.run(coroutine)


MIXED = """
import asyncio


async def doubled(x: int) -> int:
    await asyncio.sleep(0)
    return x * 2


def plus_one(doubled: int) -> int:
    return doubled + 1


async def total(plus_one: int, doubled: int, offset: int = 0) -> int:
    await asyncio.sleep(0)
    return plus_one + doubled + offset
"""


def _mixed(name: str = "async_mixed") -> ModuleType:
    return make_module(name, MIXED)


def test_build_is_sync_and_execute_returns_a_dict_in_request_order() -> None:
    """`build_async` returns the driver itself; `execute` a dict, in request order."""
    driver = fnloom.Builder().with_modules(_mixed()).build_async()
    assert isinstance(driver, fnloom.AsyncDriver)
    result = _run(
        driver.execute(["total", "doubled", "plus_one", "total"], inputs={"x": 3})
    )
    assert type(result) is dict
    assert list(result.items()) == [("total", 13), ("doubled", 6), ("plus_one", 7)]


def test_async_and_sync_nodes_mix() -> None:
    """Async and sync nodes feed each other, with optional inputs, in both modes."""
    module = _mixed()
    for sync_nodes in ("thread", "inline"):
        driver = (
            fnloom.Builder().with_modules(module).build_async(sync_nodes=sync_nodes)
        )
        assert _run(driver.execute(["total"], inputs={"x": 1})) == {"total": 5}
        assert _run(driver.execute(["total"], inputs={"x": 1, "offset": 10})) == {
            "total": 15
        }


def test_parameterized_rows_get_their_keywords() -> None:
    """A row's renamed and bound keywords reach an async node as the core's would."""
    module = make_module(
        "async_rows",
        """
import asyncio
from fnloom import parameterize, source, value


@parameterize(
    from_a={"left": source("a"), "right": value(10)},
    twice_a={"left": source("a"), "right": source("a")},
)
async def summed(left: int, right: int) -> int:
    await asyncio.sleep(0)
    return left + right
""",
    )
    driver = fnloom.Builder().with_modules(module).build_async()
    assert _run(driver.execute(["from_a", "twice_a"], inputs={"a": 4})) == {
        "from_a": 14,
        "twice_a": 8,
    }


SLEEPER = """
import asyncio
import time

LOG: list[str] = []


def slow(x: int) -> int:
    time.sleep(0.2)
    LOG.append("slept")
    return x


async def ticker(x: int) -> int:
    await asyncio.sleep(0.05)
    LOG.append("tick")
    return x
"""


def test_sync_nodes_do_not_block_the_loop() -> None:
    """A sync node sleeping 0.2 s in its thread; an async ticker ticks within 0.05 s.

    Asserted by order, not by the clock: the tick lands while the sync node sleeps.
    """
    module = make_module("async_sleeper", SLEEPER)
    driver = fnloom.Builder().with_modules(module).build_async()
    assert _run(driver.execute(["slow", "ticker"], inputs={"x": 1})) == {
        "slow": 1,
        "ticker": 1,
    }
    assert module.LOG == ["tick", "slept"]


def test_inline_sync_nodes_run_on_the_loop_thread() -> None:
    """Inline, a sync node runs on the loop's thread; by default, in another."""
    module = make_module(
        "async_where",
        """
import threading


def where(x: int) -> int:
    return threading.get_ident()
""",
    )

    async def ran_on(sync_nodes: _async.SyncNodes) -> tuple[int, int]:
        driver = (
            fnloom.Builder().with_modules(module).build_async(sync_nodes=sync_nodes)
        )
        result = await driver.execute(["where"], inputs={"x": 0})
        return threading.get_ident(), int(result["where"])  # ty: ignore[invalid-argument-type]  # an int, from the node

    loop_thread, node_thread = _run(ran_on("inline"))
    assert node_thread == loop_thread
    loop_thread, node_thread = _run(ran_on("thread"))
    assert node_thread != loop_thread


FAILING = """
import asyncio

LOG: list[str] = []


async def boom(x: int) -> int:
    await asyncio.sleep(0)
    raise KeyError("boom")


async def sibling(x: int) -> int:
    try:
        await asyncio.sleep(60)
    except asyncio.CancelledError:
        LOG.append("cancelled")
        raise
    LOG.append("finished")
    return x
"""

SYNC_FAILING = """
def boom(x: int) -> int:
    raise KeyError("boom")
"""


def _failure(call: Coroutine[Any, Any, object]) -> BaseException:
    """The exception awaiting `call` raises."""
    try:
        _run(call)
    except BaseException as error:  # noqa: BLE001  # returned for the test to inspect
        return error
    pytest.fail("the run did not raise")


def test_a_failure_cancels_siblings_and_raises_the_node_exception() -> None:
    """The sibling sees `CancelledError`; the node's own exception is raised, noted."""
    module = make_module("async_failing", FAILING)
    driver = fnloom.Builder().with_modules(module).build_async()
    error = _failure(driver.execute(["sibling", "boom"], inputs={"x": 1}))
    assert type(error) is KeyError
    assert error.args == ("boom",)
    assert error.__notes__ == ["in node 'boom'"]
    assert module.LOG == ["cancelled"]


def test_a_failing_node_is_raised_as_the_sync_driver_raises_it() -> None:
    """The same failing node: the same class, arguments and notes on both drivers."""
    module = make_module("async_sync_failing", SYNC_FAILING)
    with pytest.raises(KeyError) as sync_info:
        fnloom.build(module).execute(["boom"], inputs={"x": 1})
    for sync_nodes in ("thread", "inline"):
        driver = (
            fnloom.Builder().with_modules(module).build_async(sync_nodes=sync_nodes)
        )
        error = _failure(driver.execute(["boom"], inputs={"x": 1}))
        assert type(error) is type(sync_info.value)
        assert error.args == sync_info.value.args
        assert error.__notes__ == sync_info.value.__notes__ == ["in node 'boom'"]


def test_a_shared_exception_is_noted_once() -> None:
    """An exception object raised on every run keeps one note, as on the sync driver."""
    module = make_module(
        "async_shared_error",
        """
SHARED = KeyError("shared")


async def boom(x: int) -> int:
    raise SHARED
""",
    )
    driver = fnloom.Builder().with_modules(module).build_async()
    for _ in range(3):
        _failure(driver.execute(["boom"], inputs={"x": 1}))
    assert module.SHARED.__notes__ == ["in node 'boom'"]


STUCK = """
import threading
import time

LOG: list[str] = []
STARTED = threading.Event()
RELEASE = threading.Event()
DONE = threading.Event()


def a_sleeper(x: int) -> int:
    STARTED.set()
    RELEASE.wait(10.0)
    LOG.append(f"slept {x}")
    DONE.set()
    return x


async def boom(x: int) -> int:
    if x < 0:
        raise KeyError("boom")
    return x
"""


def test_a_failure_does_not_wait_for_a_sleeping_thread() -> None:
    """Review Focus 4: an immediate failure is raised while a thread still sleeps.

    The sleeping thread's result is discarded, and the next run is not affected.
    """
    module = make_module("async_stuck", STUCK)
    driver = fnloom.Builder().with_modules(module).build_async()

    async def scenario() -> dict[str, object]:
        with pytest.raises(KeyError) as info:
            await driver.execute(["a_sleeper", "boom"], inputs={"x": -1})
        assert info.value.__notes__ == ["in node 'boom'"]
        # Raised while the thread still waits: the run did not wait for it.
        assert module.LOG == []
        module.RELEASE.set()
        if module.STARTED.is_set():
            assert await asyncio.to_thread(module.DONE.wait, PATIENCE)
        return await driver.execute(["a_sleeper", "boom"], inputs={"x": 2})

    assert _run(scenario()) == {"a_sleeper": 2, "boom": 2}
    assert module.LOG in (["slept -1", "slept 2"], ["slept 2"])


def test_a_cancelled_caller_cancels_its_nodes() -> None:
    """Cancelling the caller's task cancels the nodes its run started."""
    module = make_module(
        "async_cancelled_caller",
        """
import asyncio

STARTED = asyncio.Event()
CANCELLED = asyncio.Event()


async def waiting(x: int) -> int:
    STARTED.set()
    try:
        await asyncio.sleep(60)
    except asyncio.CancelledError:
        CANCELLED.set()
        raise
    return x
""",
    )
    driver = fnloom.Builder().with_modules(module).build_async()

    async def scenario() -> None:
        task = asyncio.create_task(driver.execute(["waiting"], inputs={"x": 1}))
        await module.STARTED.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await module.CANCELLED.wait()

    _run(asyncio.wait_for(scenario(), PATIENCE))
    assert module.CANCELLED.is_set()


def test_a_sync_node_returning_an_awaitable_is_refused() -> None:
    """A sync function that returns a coroutine is an error, never a value."""
    module = make_module(
        "async_disguised",
        """
import functools


async def _real(x: int) -> int:
    return x


def disguised(x: int) -> int:
    return _real(x)
""",
    )
    for sync_nodes in ("thread", "inline"):
        driver = (
            fnloom.Builder().with_modules(module).build_async(sync_nodes=sync_nodes)
        )
        error = _failure(driver.execute(["disguised"], inputs={"x": 1}))
        assert type(error) is TypeError
        assert str(error) == (
            "node 'disguised' is a sync function but returned coroutine, an "
            "awaitable; make it an async def, so the async driver awaits it"
        )
        assert error.__notes__ == ["in node 'disguised'"]


CHECKED = """
def total(price: float, quantity: int) -> float:
    return price * quantity


async def label(total: float, currency: str = "EUR") -> str:
    return f"{total:.2f} {currency}"
"""

# Requests the sync driver refuses: (final_vars, inputs, overrides).
REFUSED: list[tuple[Sequence[str], dict[str, object], dict[str, object]]] = [
    (["total"], {"price": "1", "quantity": 2}, {}),
    (["total"], {"price": 1.0}, {}),
    (["total"], {"price": 1.0, "quantity": 2, "typo": 3}, {}),
    (["label"], {}, {"total": "x"}),
    (["label"], {}, {"tota": 1.0}),
    (["nope"], {}, {}),
    (["price"], {}, {}),
    ("total", {}, {}),  # a bare str, refused
]


@pytest.mark.parametrize(
    ("final_vars", "inputs", "overrides"),
    REFUSED,
    ids=[
        "input-type",
        "missing",
        "unknown",
        "override-type",
        "unknown-override",
        "unknown-output",
        "unsupplied-input-output",
        "bare-str",
    ],
)
def test_inputs_are_checked_like_the_sync_driver(
    final_vars: Sequence[str],
    inputs: dict[str, object],
    overrides: dict[str, object],
) -> None:
    """Each refusal is the sync driver's: the same class and the same message."""
    module = make_module("async_checked", CHECKED)
    # The same graph with every node sync, which `build` takes.
    sync = fnloom.build(
        make_module("async_checked_sync", CHECKED.replace("async ", ""))
    )
    with pytest.raises(Exception) as sync_info:  # noqa: PT011  # compared below
        sync.execute(final_vars, inputs=inputs, overrides=overrides)
    driver = fnloom.Builder().with_modules(module).build_async()
    error = _failure(driver.execute(final_vars, inputs=inputs, overrides=overrides))
    assert type(error) is type(sync_info.value)
    assert str(error) == str(sync_info.value)


def test_input_checks_can_be_turned_off() -> None:
    """`without_input_type_checks` turns them off for the async driver too."""
    module = make_module("async_unchecked", CHECKED)
    driver = (
        fnloom.Builder().with_modules(module).without_input_type_checks().build_async()
    )
    result = _run(driver.execute(["total"], inputs={"price": "ab", "quantity": 2}))
    assert result == {"total": "abab"}


def test_overrides_prune_upstream() -> None:
    """An override replaces its node and everything only it needed is not run."""
    module = make_module(
        "async_pruned",
        """
CALLS: list[str] = []


async def doubled(x: int) -> int:
    CALLS.append("doubled")
    return x * 2


def plus_one(doubled: int) -> int:
    CALLS.append("plus_one")
    return doubled + 1
""",
    )
    driver = fnloom.Builder().with_modules(module).build_async()
    assert _run(driver.execute(["plus_one"], overrides={"doubled": 10})) == {
        "plus_one": 11
    }
    assert module.CALLS == ["plus_one"]
    assert _run(driver.execute(["doubled"], overrides={"doubled": 10})) == {
        "doubled": 10
    }
    assert module.CALLS == ["plus_one"]


CONCURRENT = """
import asyncio


async def doubled(x: int) -> int:
    await asyncio.sleep(0)
    return x * 2


def plus(doubled: int, x: int) -> int:
    return doubled * 10 + x


async def tripled(x: int) -> int:
    await asyncio.sleep(0)
    return x * 3
"""

# The request shapes the concurrent runs take turns at.
SHAPES = (["plus", "tripled"], ["tripled"], ["plus"])

# A concurrent run's input, and the result it got.
type _Outcome = tuple[int, dict[str, object]]


async def _many(driver: fnloom.AsyncDriver, offset: int) -> list[_Outcome]:
    """50 runs at once on one loop, each with its own input and a shape by turns."""
    xs = range(offset, offset + 50)
    runs = [driver.execute(SHAPES[x % len(SHAPES)], inputs={"x": x}) for x in xs]
    return list(zip(xs, await asyncio.gather(*runs), strict=True))


def _check(outcomes: list[_Outcome]) -> None:
    """Each run got its own input's values, for its own shape, in its order."""
    for x, result in outcomes:
        values = {"plus": x * 21, "tripled": x * 3}
        assert result == {name: values[name] for name in SHAPES[x % len(SHAPES)]}


def test_concurrent_runs_on_one_driver() -> None:
    """Review Focus 5: runs on one loop, and on loops in two threads, never mix."""
    module = make_module("async_concurrent", CONCURRENT)
    driver = fnloom.Builder().with_modules(module).build_async()
    _check(_run(_many(driver, 0)))
    found: dict[int, list[_Outcome]] = {}
    failures: list[BaseException] = []

    def in_thread(offset: int) -> None:
        try:
            found[offset] = asyncio.run(_many(driver, offset))
        except BaseException as error:  # noqa: BLE001  # reported by the test
            failures.append(error)

    threads = [threading.Thread(target=in_thread, args=(o,)) for o in (1000, 2000)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(PATIENCE)
    assert failures == []
    assert sorted(found) == [1000, 2000]
    for outcomes in found.values():
        _check(outcomes)
    # One plan per request shape, the same one however many runs shared it.
    for shape in SHAPES:
        plan = driver.prepare(shape, inputs={"x": 0})
        assert driver.prepare(list(shape), inputs={"x": 0}) is plan


def test_prepared_plan_runs_again() -> None:
    """A prepared plan runs as often as asked, checked as a `PreparedPlan` is."""
    driver = fnloom.Builder().with_modules(_mixed()).build_async()
    plan = driver.prepare(["total"], inputs={"x": 0})
    assert isinstance(plan, fnloom.AsyncPreparedPlan)
    assert driver.prepare(["total"], inputs={"x": 0}) is plan

    async def twice() -> list[dict[str, object]]:
        return [await plan.run(inputs={"x": 1}), await plan.run(inputs={"x": 2})]

    assert _run(twice()) == [{"total": 5}, {"total": 9}]
    error = _failure(plan.run(inputs={}))
    assert type(error) is ValueError
    assert str(error) == "inputs [] differ from the prepared ['x']"


def test_sync_build_refuses_async_nodes_naming_build_async() -> None:
    """Q32: `build()` refuses an `async def`, and says to use `build_async()`."""
    module = _mixed()
    with pytest.raises(ValueError, match=r"build_async\(\)") as info:
        fnloom.build(module)
    assert str(info.value) == (
        "'doubled': async function is not supported by build(), which calls nodes "
        "synchronously; build it with build_async()"
    )


def test_build_async_still_refuses_generators() -> None:
    """A generator, sync or async, is no node for either build."""
    for index, source in enumerate(
        (
            "def f(a: int) -> int:\n    yield a\n",
            "async def f(a: int) -> int:\n    yield a\n",
        )
    ):
        module = make_module(f"async_generator_{index}", source)
        with pytest.raises(ValueError, match="generator function is not supported"):
            fnloom.Builder().with_modules(module).build_async()


CONFIGURED = """
import asyncio
from fnloom import config, extract_fields


@config.when(source="remote")
@extract_fields({"a": int, "b": int})
async def fetched__remote(x: int) -> dict:
    await asyncio.sleep(0)
    return {"a": x, "b": x + 1}


@config.when(source="local")
@extract_fields({"a": int, "b": int})
def fetched__local(x: int) -> dict:
    return {"a": -x, "b": -x - 1}


async def summed(a: int, b: int) -> int:
    return a + b
"""


def test_config_and_extract_work_async() -> None:
    """An async producer under `extract_fields`, chosen by config; fields run inline."""
    module = make_module("async_configured", CONFIGURED)
    remote = (
        fnloom.Builder()
        .with_modules(module)
        .with_config({"source": "remote"})
        .build_async()
    )
    assert remote.config == {"source": "remote"}
    assert _run(remote.execute(["summed", "a", "fetched"], inputs={"x": 2})) == {
        "summed": 5,
        "a": 2,
        "fetched": {"a": 2, "b": 3},
    }
    local = (
        fnloom.Builder()
        .with_modules(module)
        .with_config({"source": "local"})
        .build_async()
    )
    assert _run(local.execute(["summed"], inputs={"x": 2})) == {"summed": -5}
    calls = _async.calls(remote._nodes, "thread")  # noqa: SLF001  # how each node is called
    assert {name: call.kind for name, call in calls.items()} == {
        "a": "inline",
        "b": "inline",
        "fetched": "await",
        "summed": "await",
    }


def test_a_field_failure_is_noted_with_the_field_node() -> None:
    """A field of the wrong type fails its own node, as on the sync driver."""
    module = make_module(
        "async_bad_field",
        """
from fnloom import extract_fields


@extract_fields({"a": int})
async def fetched(x: int) -> dict:
    return {"a": "not an int"}
""",
    )
    with pytest.raises(ValueError) as sync_info:  # noqa: PT011  # compared below
        fnloom.Builder().with_modules(
            make_module(
                "async_bad_field_sync",
                """
from fnloom import extract_fields


@extract_fields({"a": int})
def fetched(x: int) -> dict:
    return {"a": "not an int"}
""",
            )
        ).build().execute(["a"], inputs={"x": 1})
    error = _failure(
        fnloom.Builder()
        .with_modules(module)
        .build_async()
        .execute(["a"], inputs={"x": 1})
    )
    assert type(error) is ValueError
    assert str(error) == str(sync_info.value)
    assert error.__notes__ == sync_info.value.__notes__ == ["in node 'a'"]


def test_inspection_matches_the_sync_driver() -> None:
    """Names, views and lineage are the sync driver's for the same graph."""
    module = make_module("async_inspected", CHECKED)
    sync = fnloom.build(
        make_module("async_inspected_sync", CHECKED.replace("async ", ""))
    )
    driver = fnloom.Builder().with_modules(module).build_async()
    assert driver.node_names() == sync.node_names()
    assert list(driver.nodes) == list(sync.nodes)
    assert driver.nodes["label"].dependencies == sync.nodes["label"].dependencies
    assert driver.what_is_upstream_of("label") == sync.what_is_upstream_of("label")
    assert driver.what_is_downstream_of("price") == sync.what_is_downstream_of("price")
    assert driver.config == {}


def test_sync_nodes_takes_only_its_two_modes() -> None:
    """Any other `sync_nodes` is a `ValueError` naming both."""
    builder = fnloom.Builder().with_modules(_mixed())
    with pytest.raises(
        ValueError, match="sync_nodes must be 'inline' or 'thread', not 'loop'"
    ):
        builder.build_async(sync_nodes="loop")  # ty: ignore[invalid-argument-type]  # the misuse under test


def test_a_cache_is_refused_for_now() -> None:
    """`with_cache` and `build_async` together are refused until async caching lands."""
    builder = fnloom.Builder().with_modules(_mixed()).with_cache(fnloom.MemoryCache())
    with pytest.raises(NotImplementedError, match="does not support with_cache yet"):
        builder.build_async()
    driver = fnloom.Builder().with_modules(_mixed()).build_async()
    with pytest.raises(ValueError, match="explain_cache needs a driver built with"):
        driver.explain_cache(["total"], inputs={"x": 1})


def test_pickles_as_its_builder() -> None:
    """A built `AsyncDriver` pickles as its builder and mode; a plan as its request."""
    module = make_module("async_pickled", MIXED)
    driver = fnloom.Builder().with_modules(module).build_async(sync_nodes="inline")
    again = pickle.loads(pickle.dumps(driver))  # noqa: S301  # our own pickle
    assert isinstance(again, fnloom.AsyncDriver)
    assert again._sync_nodes == "inline"  # noqa: SLF001  # the mode it was built with
    assert _run(again.execute(["total"], inputs={"x": 1})) == {"total": 5}
    plan = pickle.loads(pickle.dumps(driver.prepare(["total"], inputs={"x": 0})))  # noqa: S301  # our own pickle
    assert _run(plan.run(inputs={"x": 2})) == {"total": 9}
    copied = copy.copy(driver)
    assert _run(copied.execute(["total"], inputs={"x": 1})) == {"total": 5}
    assert copy.copy(plan) is plan


def test_a_deep_async_chain_does_not_recurse() -> None:
    """5,000 chained async nodes run: nothing recurses over the graph's depth."""
    depth = 5_000
    lines = ["async def n0(a: int) -> int:\n    return a\n"]
    lines += [
        f"async def n{i}(n{i - 1}: int) -> int:\n    return n{i - 1} + 1\n"
        for i in range(1, depth)
    ]
    module = make_module("async_deep_chain", "\n".join(lines))
    for sync_nodes in ("thread", "inline"):
        driver = (
            fnloom.Builder().with_modules(module).build_async(sync_nodes=sync_nodes)
        )
        last = f"n{depth - 1}"
        assert _run(driver.execute([last], inputs={"a": 0})) == {last: depth - 1}


def test_walk_runs_a_plan_directly() -> None:
    """`_async.walk` returns every planned node's value, by name."""
    module = _mixed()
    driver = fnloom.Builder().with_modules(module).build_async()
    sync = driver._driver  # noqa: SLF001  # the plans it keeps
    prepared = sync.prepare(["total"], inputs={"x": 1})
    plan = prepared._plan  # noqa: SLF001  # the native plan
    assert isinstance(plan, fnloom._core.Plan)  # noqa: SLF001  # the native plan
    values = _run(
        _async.walk(plan, driver._nodes, {"x": 1}, {}, sync_nodes="inline")  # noqa: SLF001  # the graph's nodes
    )
    assert values == {"x": 1, "doubled": 2, "plus_one": 3, "total": 5}


def test_a_note_that_cannot_be_added_is_raised_as_the_sync_driver_raises_it() -> None:
    """An exception whose `__notes__` is not a list: the sync driver's failure."""
    source = """
class Odd(Exception):
    pass


def make() -> Odd:
    odd = Odd("odd")
    odd.__notes__ = ("not a list",)
    return odd


{prefix}def boom(x: int) -> int:
    raise make()
"""
    sync = fnloom.build(make_module("async_odd_sync", source.format(prefix="")))
    with pytest.raises(TypeError) as sync_info:
        sync.execute(["boom"], inputs={"x": 1})
    module = make_module("async_odd", source.format(prefix="async "))
    driver = fnloom.Builder().with_modules(module).build_async()
    error = _failure(driver.execute(["boom"], inputs={"x": 1}))
    assert type(error) is TypeError
    assert str(error) == str(sync_info.value)
    assert type(error.__cause__).__name__ == "Odd"
    assert type(sync_info.value.__cause__).__name__ == "Odd"
