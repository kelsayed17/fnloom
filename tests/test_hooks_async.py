"""`fnloom.Hook` on the async driver: the sync driver's events, on the loop's thread."""

import ast
import asyncio
import inspect
import logging
import textwrap
import threading
from typing import TYPE_CHECKING, Any, Literal, cast, override

import pytest

import fnloom
from fnloom import Hook, NodeEnd, NodeStart, RunEnd, RunStart, _async
from helpers import make_module
from test_hooks import GRAPH, SHAPES, HookError, Recorder

if TYPE_CHECKING:
    from collections.abc import Coroutine

type SyncNodes = Literal["thread", "inline"]


def _run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coroutine)


def _async_driver(
    source: str, *hooks: Hook, sync_nodes: SyncNodes = "thread"
) -> fnloom.AsyncDriver:
    module = make_module("hooked_async_graph", source)
    builder = fnloom.Builder().with_modules(module).with_hooks(*hooks)
    return builder.build_async(sync_nodes=sync_nodes)


def _stages(hook: Recorder) -> list[tuple[str, str | None]]:
    return [(stage, node) for _, stage, node in hook.log]


AWAITED = """
import asyncio


async def a(x: int) -> int:
    await asyncio.sleep(0)
    return x + 1


def b(a: int) -> int:
    return a * 10


async def c(a: int, b: int) -> int:
    await asyncio.sleep(0)
    return a + b
"""


def test_async_events_carry_values_not_tasks() -> None:
    """Each node's inputs and result are values, never the tasks computing them."""
    hook = Recorder()
    driver = _async_driver(AWAITED, hook)
    assert _run(driver.execute(["c", "a"], inputs={"x": 1})) == {"c": 22, "a": 2}
    assert _stages(hook) == [
        ("before_run", None),
        ("before_node", "a"),
        ("after_node", "a"),
        ("before_node", "b"),
        ("after_node", "b"),
        ("before_node", "c"),
        ("after_node", "c"),
        ("after_run", None),
    ]
    starts, ends = hook.of(NodeStart), hook.of(NodeEnd)
    assert [dict(s.inputs) for s in starts] == [{"x": 1}, {"a": 2}, {"a": 2, "b": 20}]
    assert [(e.ok, e.result, e.error) for e in ends] == [
        (True, 2, None),
        (True, 20, None),
        (True, 22, None),
    ]
    assert [s.node for s in starts] == [driver.nodes[n] for n in ("a", "b", "c")]
    start, end = hook.of(RunStart)[0], hook.of(RunEnd)[0]
    assert (start.outputs, start.nodes, dict(start.inputs)) == (
        ("c", "a"),
        ("a", "b", "c"),
        {"x": 1},
    )
    assert {e.run_id for e in hook.events if hasattr(e, "run_id")} == {start.run_id}
    assert (end.ok, end.error) == (True, None)
    assert end.results is not None
    assert list(end.results.items()) == [("c", 22), ("a", 2)]
    assert all(e.cache is None for e in (*starts, *ends))


FAILING = """
import asyncio


async def a(x: int) -> int:
    await asyncio.sleep(0)
    return x + 1


async def boom(a: int) -> int:
    await asyncio.sleep(0)
    raise ValueError(f"bad {a}")


def thread_boom(a: int) -> int:
    raise KeyError(a)


def after(boom: int) -> int:
    return boom
"""


@pytest.mark.parametrize(
    ("output", "error", "failing"),
    [("after", ValueError, "boom"), ("thread_boom", KeyError, "thread_boom")],
    ids=["async", "thread"],
)
def test_a_failing_async_node_reports_ok_false(
    output: str, error: type[Exception], failing: str
) -> None:
    """`after_node(ok=False)` gets the very error, as `after_run` does."""
    hook = Recorder()
    driver = _async_driver(FAILING, hook)
    with pytest.raises(error) as raised:
        _run(driver.execute([output], inputs={"x": 1}))
    assert _stages(hook) == [
        ("before_run", None),
        ("before_node", "a"),
        ("after_node", "a"),
        ("before_node", failing),
        ("after_node", failing),
        ("after_run", None),
    ]
    failed = hook.of(NodeEnd)[1]
    assert (failed.ok, failed.result) == (False, None)
    assert failed.error is raised.value
    end = hook.of(RunEnd)[0]
    assert (end.ok, end.error, end.results) == (False, raised.value, None)
    assert raised.value.__notes__ == [f"in node '{failing}'"]


THREADS = """
import asyncio
import threading

ran: dict[str, int] = {}


def a(x: int) -> int:
    ran["a"] = threading.get_ident()
    return x + 1


async def b(a: int) -> int:
    ran["b"] = threading.get_ident()
    await asyncio.sleep(0)
    return a * 2


def c(a: int, b: int) -> int:
    ran["c"] = threading.get_ident()
    return a + b
"""


class Threads(Hook):
    """Records the thread each event is delivered on."""

    def __init__(self) -> None:
        """Records nothing yet."""
        self.threads: list[tuple[str, int]] = []

    @override
    def before_run(self, event: RunStart) -> None:
        self.threads.append(("before_run", threading.get_ident()))

    @override
    def before_node(self, event: NodeStart) -> None:
        self.threads.append((f"before {event.node.name}", threading.get_ident()))

    @override
    def after_node(self, event: NodeEnd) -> None:
        self.threads.append((f"after {event.node.name}", threading.get_ident()))

    @override
    def after_run(self, event: RunEnd) -> None:
        self.threads.append(("after_run", threading.get_ident()))


@pytest.mark.parametrize("sync_nodes", ["thread", "inline"])
def test_hooks_run_on_the_loop_thread_for_thread_and_inline_sync_nodes(
    sync_nodes: SyncNodes,
) -> None:
    """Every event is delivered on the loop's thread, wherever its node ran."""
    hook = Threads()
    module = make_module(f"hooked_threads_{sync_nodes}", THREADS)
    driver = (
        fnloom.Builder()
        .with_modules(module)
        .with_hooks(hook)
        .build_async(sync_nodes=sync_nodes)
    )
    loop_thread: list[int] = []

    async def main() -> dict[str, object]:
        loop_thread.append(threading.get_ident())
        return await driver.execute(["c"], inputs={"x": 1})

    assert _run(main()) == {"c": 6}
    (loop,) = loop_thread
    assert len(hook.threads) == 8  # noqa: PLR2004  # a run, and three nodes
    assert {thread for _, thread in hook.threads} == {loop}
    ran = cast("dict[str, int]", module.ran)
    assert ran["b"] == loop
    if sync_nodes == "thread":
        assert ran["a"] != loop
        assert ran["c"] != loop
    else:
        assert ran["a"] == ran["c"] == loop


CONCURRENT = """
import asyncio


async def a_slow(x: int) -> int:
    for _ in range(5):
        await asyncio.sleep(0)
    return x + 1


async def b_fast(x: int) -> int:
    await asyncio.sleep(0)
    return x + 2


def total(a_slow: int, b_fast: int) -> int:
    return a_slow + b_fast
"""


def test_concurrent_nodes_report_in_start_and_finish_order() -> None:
    """Nodes report as each starts and as each finishes; `nodes` is plan order."""
    hook = Recorder()
    driver = _async_driver(CONCURRENT, hook)
    assert _run(driver.execute(["total"], inputs={"x": 1})) == {"total": 5}
    assert _stages(hook) == [
        ("before_run", None),
        ("before_node", "a_slow"),
        ("before_node", "b_fast"),
        ("after_node", "b_fast"),
        ("after_node", "a_slow"),
        ("before_node", "total"),
        ("after_node", "total"),
        ("after_run", None),
    ]
    # The plan's order, not the order of the events (R1).
    assert hook.of(RunStart)[0].nodes == ("a_slow", "b_fast", "total")
    assert hook.of(RunEnd)[0].nodes == ("a_slow", "b_fast", "total")


RAISING = """
import asyncio

events: list[str] = []


async def a_slow(x: int) -> int:
    events.append("a_slow started")
    try:
        await asyncio.sleep(60)
    except asyncio.CancelledError:
        events.append("a_slow cancelled")
        raise
    return x


async def b_quick(x: int) -> int:
    events.append("b_quick ran")
    await asyncio.sleep(0)
    return x


def total(a_slow: int, b_quick: int) -> int:
    events.append("total ran")
    return a_slow + b_quick
"""


@pytest.mark.parametrize("stage", ["before_node", "after_node"])
def test_a_raising_hook_cancels_the_rest(stage: str) -> None:
    """A hook's error fails the run as a node's would: every running node stops."""
    hook = Recorder(raise_on=((stage, "b_quick"),))
    other = Recorder("o", hook.log)
    module = make_module(f"hooked_raising_{stage}", RAISING)
    driver = fnloom.Builder().with_modules(module).with_hooks(hook, other).build_async()
    with pytest.raises(HookError, match=f"r {stage} b_quick") as raised:
        _run(driver.execute(["total"], inputs={"x": 1}))
    # Raised as itself, with no node's note: the node didn't fail.
    assert not hasattr(raised.value, "__notes__")
    # The running node was cancelled and awaited before `execute` raised.
    assert module.events[0] == "a_slow started"
    assert module.events[-1] == "a_slow cancelled"
    assert "total ran" not in module.events
    if stage == "before_node":
        # The node is never called, and gets no `after_node`.
        assert "b_quick ran" not in module.events
        failed = [("before_node", "b_quick")]
    else:
        assert "b_quick ran" in module.events
        failed = [("before_node", "b_quick"), ("after_node", "b_quick")]
    # Every hook saw each event; the cancelled node gets no `after_node`.
    events = [
        ("before_run", None),
        ("before_node", "a_slow"),
        *failed,
        ("after_run", None),
    ]
    assert hook.log == [(name, *event) for event in events for name in ("r", "o")]
    end = hook.of(RunEnd)[0]
    assert (end.ok, end.error, end.results) == (False, raised.value, None)


CHAIN = """
def a(x: int) -> int:
    return x + 1


def b(a: int, y: int = 2) -> int:
    return a + y


def c(b: int) -> int:
    return b * 10
"""

DIAMOND = """
def a(x: int) -> int:
    return x + 1


def b(a: int) -> int:
    return a * 2


def c(a: int) -> int:
    return a * 3


def d(b: int, c: int, y: int = 0) -> int:
    return b + c + y
"""


def _seen(hook: Recorder) -> list[tuple[object, ...]]:
    """Each event a hook got, without its `run_id` or node view."""
    seen: list[tuple[object, ...]] = []
    for event in hook.events:
        match event:
            case RunStart():
                seen.append(("start", event.outputs, dict(event.inputs), event.nodes))
            case NodeStart():
                seen.append(("node", event.node.name, dict(event.inputs or {})))
            case NodeEnd():
                inputs = dict(event.inputs or {})
                seen.append(("ended", event.node.name, inputs, event.ok, event.result))
            case RunEnd():
                results = None if event.results is None else dict(event.results)
                seen.append(("end", event.ok, event.nodes, results))
            case _:
                pytest.fail(f"unexpected event {event!r}")
    return seen


@pytest.mark.parametrize(
    ("source", "outputs"),
    [(CHAIN, ["c", "a"]), (DIAMOND, ["d"])],
    ids=["chain", "diamond"],
)
@pytest.mark.parametrize("sync_nodes", ["inline", "thread"])
def test_the_same_hook_sees_the_same_events_on_both_drivers(
    source: str, outputs: list[str], sync_nodes: SyncNodes
) -> None:
    """The same events, with the same values; in the same order when run inline."""
    module = make_module("hooked_both", source)
    sync_hook, async_hook = Recorder(), Recorder()
    sync_driver = fnloom.Builder().with_modules(module).with_hooks(sync_hook).build()
    async_driver = (
        fnloom.Builder()
        .with_modules(module)
        .with_hooks(async_hook)
        .build_async(sync_nodes=sync_nodes)
    )
    expected = sync_driver.execute(outputs, inputs={"x": 1})
    assert _run(async_driver.execute(outputs, inputs={"x": 1})) == expected
    sync_seen, async_seen = _seen(sync_hook), _seen(async_hook)
    if sync_nodes == "inline":
        assert async_seen == sync_seen
    else:
        # Threads finish in any order: the same events, each node's in order.
        assert sorted(map(repr, async_seen)) == sorted(map(repr, sync_seen))
        assert (async_seen[0], async_seen[-1]) == (sync_seen[0], sync_seen[-1])


def test_node_inputs_are_by_parameter_name_on_the_async_driver() -> None:
    """Renames, groups, shared sources and namespaces key as on the sync driver (R4)."""
    shapes = make_module("hooked_async_shapes", SHAPES)
    spaced = make_module("hooked_async_spaced", GRAPH)
    hooks = Recorder(), Recorder()
    drivers = [
        fnloom.Builder()
        .with_modules(shapes)
        .with_modules(spaced, namespace="eu")
        .with_hooks(hook)
        for hook in hooks
    ]
    request = ["scaled", "twice", "listed", "keyed", "first", "eu.c"]
    drivers[0].build().execute(request, inputs={"x": 1})
    _run(drivers[1].build_async(sync_nodes="inline").execute(request, inputs={"x": 1}))
    sync_inputs, async_inputs = (
        {s.node.name: dict(s.inputs) for s in hook.of(NodeStart)} for hook in hooks
    )
    assert async_inputs == sync_inputs
    assert async_inputs["twice"] == {"p": 2, "q": 2}


def test_value_checks_fail_after_before_run_on_the_async_driver() -> None:
    """A missing or mistyped input fails after `before_run`, with no nodes (R5)."""
    hook = Recorder()
    driver = _async_driver(GRAPH, hook)
    with pytest.raises(ValueError, match="missing inputs: 'x'"):
        _run(driver.execute(["c"]))
    with pytest.raises(ValueError, match="x: expected int, got str"):
        _run(driver.execute(["c"], inputs={"x": "1"}))
    assert _stages(hook) == [("before_run", None), ("after_run", None)] * 2
    assert [e.nodes for e in hook.of(RunStart)] == [(), ()]
    assert [(e.ok, e.nodes) for e in hook.of(RunEnd)] == [(False, ()), (False, ())]
    hook.log.clear()
    # A request of the wrong shape fires nothing.
    with pytest.raises(ValueError, match="unknown outputs"):
        _run(driver.execute(["nope"], inputs={"x": 1}))
    assert hook.log == []


def test_async_prepared_plan_runs_fire_the_hooks() -> None:
    """`AsyncPreparedPlan.run` fires as `execute` does; no hooks, a plain plan."""
    hook = Recorder()
    plan = _async_driver(GRAPH, hook).prepare(["c"], inputs={"x": 0})
    assert _run(plan.run(inputs={"x": 1})) == {"c": 40}
    assert _run(plan.run(inputs={"x": 2})) == {"c": 50}
    assert [dict(s.inputs) for s in hook.of(RunStart)] == [{"x": 1}, {"x": 2}]
    hook.log.clear()
    with pytest.raises(ValueError, match="differ from the prepared"):
        _run(plan.run(inputs={"y": 1}))
    assert hook.log == []
    unhooked = _async_driver(GRAPH).prepare(["c"], inputs={"x": 0})
    assert type(unhooked) is fnloom.AsyncPreparedPlan


def test_a_cached_async_driver_fires_the_events_of_the_nodes_it_runs() -> None:
    """With a cache, the nodes the run computes report; then the hit reports alone."""
    hook = Recorder()
    module = make_module("hooked_async_cached", GRAPH)
    driver = (
        fnloom.Builder()
        .with_modules(module)
        .with_cache(fnloom.MemoryCache())
        .with_hooks(hook)
        .build_async()
    )
    assert _run(driver.execute(["c"], inputs={"x": 1})) == {"c": 40}
    assert [n for stage, n in _stages(hook) if stage == "after_node"] == ["a", "b", "c"]
    hook.log.clear()
    assert _run(driver.execute(["c"], inputs={"x": 1})) == {"c": 40}
    # Only the hit reports, as one (Q85); `a` and `b` only fed it.
    assert _stages(hook) == [
        ("before_run", None),
        ("before_node", "c"),
        ("after_node", "c"),
        ("after_run", None),
    ]


GROUPED = """
import asyncio


async def a(x: int) -> int:
    await asyncio.sleep(0)
    return x + 1


async def boom(a: int) -> int:
    await asyncio.sleep(0)
    raise ValueError(f"bad {a}")


def thread_boom(a: int) -> int:
    raise KeyError(a)
"""


@pytest.mark.parametrize(
    ("failing", "error"),
    [("boom", ValueError), ("thread_boom", KeyError)],
    ids=["async", "thread"],
)
def test_a_failing_node_and_a_raising_hook_are_one_group_led_by_the_node(
    failing: str, error: type[Exception]
) -> None:
    """Decision 5 on async: the node's own error, noted, then the hook's."""
    hook = Recorder(raise_on=(("after_node", failing),))
    driver = _async_driver(GROUPED, hook)
    with pytest.raises(ExceptionGroup) as raised:
        _run(driver.execute([failing], inputs={"x": 1}))
    group = raised.value
    assert str(group).startswith(f"node '{failing}' failed and hooks raised")
    node_error, hook_error = group.exceptions
    assert type(node_error) is error
    assert node_error.__notes__ == [f"in node '{failing}'"]
    assert type(hook_error) is HookError
    assert str(hook_error) == f"r after_node {failing}"
    assert hook.of(NodeEnd)[-1].error is node_error
    end = hook.of(RunEnd)[0]
    assert (end.ok, end.error) == (False, group)


class _NoBegin(ast.NodeTransformer):
    """Drops the docstring and every `if not self._begin(...): return`."""

    @override
    def visit_If(self, node: ast.If) -> ast.AST | None:
        test = node.test
        if (
            isinstance(test, ast.UnaryOp)
            and isinstance(test.op, ast.Not)
            and isinstance(test.operand, ast.Call)
            and isinstance(test.operand.func, ast.Attribute)
            and test.operand.func.attr == "_begin"
        ):
            return None
        return self.generic_visit(node)


def _drain_body(method: object, *, hooked: bool) -> str:
    """The method's body as an AST dump, without its docstring or `_begin` lines."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(cast("Any", method))))
    function = tree.body[0]
    assert isinstance(function, ast.FunctionDef)
    body = function.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
    ):
        body = body[1:]
    module = ast.Module(body=body, type_ignores=[])
    if hooked:
        module = _NoBegin().visit(module)
    return ast.dump(module)


def test_the_hooked_drain_mirrors_the_plain_drain() -> None:
    """`_HookedRun._drain` is `_Run._drain` plus `_begin`, kept apart for speed (R6)."""
    plain = _drain_body(_async._Run._drain, hooked=False)  # noqa: SLF001  # the guarded copy
    hooked = _drain_body(_async._HookedRun._drain, hooked=True)  # noqa: SLF001  # the guarded copy
    assert hooked == plain, (
        "_HookedRun._drain has drifted from _Run._drain: mirror the change in both, "
        "keeping only the `if not self._begin(name, kwargs): return` line extra"
    )
    # The guard does strip something: the hooked copy does call `_begin`.
    assert hooked != _drain_body(_async._HookedRun._drain, hooked=False)  # noqa: SLF001  # the guarded copy


def test_log_failures_logs_on_the_async_driver(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The async driver logs the same record, once the failure is in."""
    from test_hooks import BIG, LOGGED  # noqa: PLC0415  # only this test needs them

    driver = _async_driver(LOGGED, fnloom.LogFailures())
    with (
        caplog.at_level(logging.DEBUG),
        pytest.raises(ValueError, match="bad") as raised,
    ):
        _run(driver.execute(["fine", "boom"], inputs={"x": 1, "big": BIG}))
    (record,) = [r for r in caplog.records if r.name == "fnloom"]
    cut = repr(BIG)[:50] + "..."
    assert record.getMessage() == f"node 'boom' failed; inputs: a=2, big={cut}"
    assert record.levelno == logging.ERROR
    assert isinstance(record.exc_info, tuple)
    assert record.exc_info[1] is raised.value
