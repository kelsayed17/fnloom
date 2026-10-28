"""The async driver's walk: a prepared plan's steps, run on an event loop.

The native core plans; this module runs the plan's computed nodes as their inputs
become ready. A coroutine function is awaited as a task. A sync function runs in a
worker thread of the loop's default executor, through `loop.run_in_executor`, in
the caller's context, so an awaiting sibling keeps running (Q65); or inline, on the
loop, with `sync_nodes="inline"`. A generated `extract_*` node always runs inline:
it only reads a field.

A cached plan runs through `CachedSchedule`: `CacheRun.begin` finds the hits, the
walk runs the core plan it returns, and `CacheRun.finish` stores each value as its
node succeeds, so a node that fails, or that a failure cancels, stores nothing. The
`recompute` nodes something reads run first, on the loop too, so an `async def` one
is awaited. Every store call runs on the loop's thread.

Nothing about a run is kept on the schedule, which is shared by every run of its
plan: each run has its own `_Run`, so runs on one loop, or on loops in several
threads, never share a value (Review Focus 5). Nothing recurses over the graph's
depth: each node counts the computed inputs it still waits for.
"""

import asyncio
import contextvars
import functools
import inspect
import threading
from collections import deque
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, cast

from fnloom import _extract
from fnloom._caching import HIT_PLAN_CACHE_SIZE
from fnloom._scan import core_inputs

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Mapping

    from fnloom import _core
    from fnloom._caching import CacheRun
    from fnloom._scan import NodeDef

# What the walk calls with each node's name and value as the node succeeds.
type OnValue = Callable[[str, object], None]

type SyncNodes = Literal["thread", "inline"]


def checked_sync_nodes(sync_nodes: str) -> SyncNodes:
    """`sync_nodes`, when it is `"inline"` or `"thread"`; `ValueError` if not."""
    if sync_nodes == "inline":
        return "inline"
    if sync_nodes == "thread":
        return "thread"
    msg = f"sync_nodes must be 'inline' or 'thread', not {sync_nodes!r}"
    raise ValueError(msg)


# How a computed node is called: awaited as a task, run in a worker thread, or run
# on the loop.
type _Kind = Literal["await", "thread", "inline"]


class Call(NamedTuple):
    """How the walk calls one computed node.

    `args` are the node's core inputs, each with the keyword it is passed under: its
    name, or the parameter a `@parameterize` row renames it to.
    """

    payload: Callable[..., object]
    kind: _Kind
    args: tuple[tuple[str, str], ...]


def _awaits(node: NodeDef) -> bool:
    """Whether calling `node`'s payload returns a coroutine to await.

    Read from the function as written, so a `functools.partial` over it, or a row's
    call that maps keywords onto it, counts as what it calls.
    """
    return inspect.iscoroutinefunction(node.origin) or inspect.iscoroutinefunction(
        node.payload
    )


def calls(nodes: Mapping[str, NodeDef], sync_nodes: SyncNodes) -> dict[str, Call]:
    """How the walk calls each node of a graph, by name."""
    found: dict[str, Call] = {}
    for name, node in nodes.items():
        kind: _Kind
        if _extract.is_generated(node):
            kind = "inline"
        elif _awaits(node):
            kind = "await"
        else:
            kind = sync_nodes
        args = tuple(
            (source, node.renames.get(source, source))
            for source, _ in core_inputs(node.params)
        )
        found[name] = Call(node.payload, kind, args)
    return found


class Schedule:
    """A prepared plan's steps, read once, as the walk runs them; never changed.

    `start` holds the computed nodes no computed node feeds, in plan order. Each
    computed node has its call, with the inputs a skipped optional input leaves out
    dropped, the computed nodes it waits for, counted, and the computed nodes
    waiting for it.
    """

    __slots__ = ("calls", "dependents", "given", "overridden", "pending", "start")

    def __init__(self, plan: _core.Plan, calls: Mapping[str, Call]) -> None:
        """Reads `plan`'s steps, with `calls` for every node of its graph."""
        steps = plan.steps()
        skipped = {name for name, action in steps if action == "skip_optional_input"}
        computed = [name for name, action in steps if action == "compute"]
        computed_names = set(computed)
        self.given = tuple(name for name, action in steps if action == "use_input")
        self.overridden = tuple(
            name for name, action in steps if action == "use_override"
        )
        self.calls: dict[str, Call] = {}
        self.pending: dict[str, int] = {}
        dependents: dict[str, list[str]] = {}
        for name in computed:
            call = calls[name]
            args = tuple(arg for arg in call.args if arg[0] not in skipped)
            self.calls[name] = call._replace(args=args)
            waits = {source for source, _ in args if source in computed_names}
            self.pending[name] = len(waits)
            for source in sorted(waits):
                dependents.setdefault(source, []).append(name)
        self.dependents = {name: tuple(after) for name, after in dependents.items()}
        self.start = tuple(name for name in computed if not self.pending[name])

    async def run(
        self,
        inputs: Mapping[str, object],
        overrides: Mapping[str, object],
        on_value: OnValue | None = None,
    ) -> dict[str, object]:
        """Every planned node's value, by name; see `walk`.

        `on_value`, when given, is called with each computed node's name and value
        as the node succeeds, on the loop's thread.
        """
        values: dict[str, object] = {name: inputs[name] for name in self.given}
        for name in self.overridden:
            values[name] = overrides[name]
        if not self.calls:
            return values
        return await _Run(self, values, on_value).wait()


class CachedSchedule:
    """A cached plan's runs on the loop: its `CacheRun`, and a `Schedule` per plan.

    A run's hits make it a core plan of their own, so a schedule is read once per
    core plan, on first use, and kept, as `CacheRun` keeps the plans: the least
    recently used goes first. Safe to share between runs and threads.
    """

    __slots__ = ("_cache_run", "_calls", "_lock", "_schedules")

    # How many schedules are kept: a cached run's plans, and its nested run's.
    SIZE = 2 * HIT_PLAN_CACHE_SIZE

    def __init__(self, cache_run: CacheRun, calls: Mapping[str, Call]) -> None:
        """Runs `cache_run`'s plans, with `calls` for every node of its graph."""
        self._cache_run = cache_run
        self._calls = calls
        # By the plan's id; the plan is kept with its schedule, so the id is not
        # reused while the entry lives. Insertion order is recency order.
        self._schedules: dict[int, tuple[_core.Plan, Schedule]] = {}
        self._lock = threading.Lock()

    def _schedule(self, plan: _core.Plan) -> Schedule:
        """`plan`'s schedule, read on first use."""
        with self._lock:
            entry = self._schedules.pop(id(plan), None)
            if entry is None:
                entry = (plan, Schedule(plan, self._calls))
            self._schedules[id(plan)] = entry
            if len(self._schedules) > self.SIZE:
                del self._schedules[next(iter(self._schedules))]
        return entry[1]

    async def run(
        self, inputs: dict[str, object], overrides: dict[str, object]
    ) -> dict[str, object]:
        """Every planned node's value, by name, the hits loaded, the rest stored.

        The nested runs of `recompute` nodes go first, innermost first, each
        walked on the loop: a loop, not a recursion, however deep they nest.
        """
        levels = [self._cache_run]
        while (inner := levels[-1].nested) is not None:
            levels.append(inner)
        warned: set[str] = set()
        versions = self._cache_run.versions(inputs, overrides, warned)
        known: dict[str, object] = {}
        for level in reversed(levels):
            begun = level.begin(
                inputs,
                overrides,
                known=known,
                versions=versions if level is self._cache_run else dict(versions),
                warned=warned,
                native=False,
            )
            known = await self._schedule(begun.plan).run(
                inputs, begun.overrides, functools.partial(level.finish, begun)
            )
        return known


async def walk(
    plan: _core.Plan,
    nodes: Mapping[str, NodeDef],
    inputs: Mapping[str, object],
    overrides: Mapping[str, object],
    *,
    sync_nodes: SyncNodes,
) -> dict[str, object]:
    """Runs `plan` on the running loop; every planned node's value, by name.

    `nodes` are the graph's nodes; `inputs` and `overrides` the checked values.
    `AsyncPreparedPlan` keeps the `Schedule` this makes, to read the steps once.
    """
    return await Schedule(plan, calls(nodes, sync_nodes)).run(inputs, overrides)


def noted(error: BaseException, node: str) -> BaseException:
    """`error` with the sync driver's note naming `node`, added once.

    As the sync driver: when adding the note fails, that failure, caused by `error`.
    """
    note = f"in node '{node}'"
    try:
        if note not in getattr(error, "__notes__", ()):
            error.add_note(note)
    except Exception as failure:  # noqa: BLE001  # raised in the error's place
        failure.__cause__ = error
        return failure
    return error


def _awaitable_refusal(node: str, value: object) -> TypeError:
    """The error for a sync node that returned an awaitable, which is closed."""
    close = getattr(value, "close", None)
    if callable(close):
        # A coroutine never awaited warns when collected; closing it does not.
        close()
    msg = (
        f"node {node!r} is a sync function but returned "
        f"{type(value).__name__}, an awaitable; make it an async def, "
        "so the async driver awaits it"
    )
    return TypeError(msg)


def _cancellation(name: str, future: asyncio.Future[object]) -> BaseException:
    """What a node's future, cancelled other than by its run, is raised as.

    A task nobody called `cancel` on ended with the node's own `CancelledError`,
    which is raised as it is. A task something else cancelled is raised as a new
    `CancelledError` naming the node: the run cancels only once it has stopped.
    """
    if isinstance(future, asyncio.Task) and not future.cancelling():
        try:
            future.result()
        except asyncio.CancelledError as own:
            return own
    return asyncio.CancelledError(f"node {name!r} was cancelled")


# What a node may raise that the walk does not catch: they stop the loop itself.
_STOPPING = (KeyboardInterrupt, SystemExit)


class _Run:
    """One run of a schedule: its values and what is still to do.

    Driven by the loop's callbacks, so it is only ever touched from the loop's
    thread. A worker thread only computes its node's value.
    """

    __slots__ = (
        "_cancelled",
        "_failure",
        "_finished",
        "_loop",
        "_on_value",
        "_pending",
        "_ready",
        "_remaining",
        "_running",
        "_schedule",
        "_values",
    )

    def __init__(
        self,
        schedule: Schedule,
        values: dict[str, object],
        on_value: OnValue | None = None,
    ) -> None:
        """A run over `schedule`, starting from the given and overridden `values`.

        `on_value`, when given, is called as each computed node succeeds.
        """
        self._schedule = schedule
        self._values = values
        self._on_value = on_value
        self._loop = asyncio.get_running_loop()
        self._pending = dict(schedule.pending)
        self._remaining = len(schedule.calls)
        self._ready = deque(schedule.start)
        self._running: dict[asyncio.Future[object], str] = {}
        self._finished: asyncio.Future[None] = self._loop.create_future()
        self._failure: BaseException | None = None
        # The node tasks running when the run failed: cancelled, then awaited.
        self._cancelled: list[asyncio.Future[object]] = []

    async def wait(self) -> dict[str, object]:
        """Runs every node; the values, or the first failure, noted, raised.

        Whether the run fails or its caller is cancelled, every running node task
        is cancelled and awaited before the exception goes on, as `TaskGroup` does.
        A sync node in a thread can't be interrupted: its future is cancelled, its
        result dropped when it comes, and the thread is not waited for.
        """
        try:
            self._drain()
            if not self._finished.done():
                await self._finished
            if self._cancelled:
                await asyncio.wait(self._cancelled)
        except asyncio.CancelledError:
            # The caller was cancelled: its nodes stop before the caller hears of it.
            tasks = self._tasks()
            self._stop()
            if tasks:
                await asyncio.wait(tasks)
            raise
        except BaseException:
            # The loop is stopping (`KeyboardInterrupt`, `SystemExit`): no waiting.
            self._stop()
            raise
        if self._failure is not None:
            raise self._failure
        return self._values

    def _tasks(self) -> list[asyncio.Future[object]]:
        """The running node tasks: everything running but a thread's future."""
        calls = self._schedule.calls
        return [f for f, name in self._running.items() if calls[name].kind == "await"]

    def _drain(self) -> None:
        """Starts every ready node, running inline ones to their dependents."""
        ready = self._ready
        values = self._values
        calls = self._schedule.calls
        while ready and self._failure is None:
            name = ready.popleft()
            payload, kind, args = calls[name]
            kwargs = {keyword: values[source] for source, keyword in args}
            if kind == "inline":
                if not self._inline(name, payload, kwargs):
                    return
                continue
            try:
                future = self._started(payload, kind, kwargs)
            except _STOPPING:
                raise
            except BaseException as error:  # noqa: BLE001  # raised once the run stops
                self._fail(name, error)
                return
            if future.done():
                if not self._settle(name, kind, future):
                    return
                continue
            self._running[future] = name
            future.add_done_callback(self._done)

    def _inline(
        self, name: str, payload: Callable[..., object], kwargs: dict[str, object]
    ) -> bool:
        """Runs a node on the loop and stores its value; `False` if the run failed."""
        try:
            value = payload(**kwargs)
        except _STOPPING:
            raise
        except BaseException as error:  # noqa: BLE001  # raised once the run stops
            self._fail(name, error)
            return False
        if inspect.isawaitable(value):
            self._fail(name, _awaitable_refusal(name, value))
            return False
        return self._store(name, value)

    def _started(
        self, payload: Callable[..., object], kind: _Kind, kwargs: dict[str, object]
    ) -> asyncio.Future[object]:
        """The task awaiting an async node, or the future of a sync node's thread."""
        if kind == "await":
            coroutine = cast("Coroutine[Any, Any, object]", payload(**kwargs))
            # Eager: a node that never suspends is done here, with no loop turn. A
            # custom task factory gets `eager_start` too.
            return self._loop.create_task(coroutine, eager_start=True)
        # In the default executor and the caller's context, as `asyncio.to_thread`
        # would, but as a future, not a task: cancelling it never waits for the
        # thread.
        context = contextvars.copy_context()
        return self._loop.run_in_executor(
            None, functools.partial(context.run, payload, **kwargs)
        )

    def _done(self, future: asyncio.Future[object]) -> None:
        """A task or a thread finished: its value goes on, or the run fails."""
        name = self._running.pop(future, None)
        if name is None or self._finished.done():
            # Cancelled by a failure or a stop: its outcome is discarded, retrieved
            # so asyncio does not report it as never retrieved.
            if not future.cancelled():
                future.exception()
            return
        kind = self._schedule.calls[name].kind
        if self._settle(name, kind, future):
            self._drain()

    def _settle(self, name: str, kind: _Kind, future: asyncio.Future[object]) -> bool:
        """Stores a finished node's value; `False`, the run failed, when it raised."""
        if future.cancelled():
            self._fail(name, _cancellation(name, future))
            return False
        error = future.exception()
        if error is not None:
            self._fail(name, error)
            return False
        value = future.result()
        if kind == "thread" and inspect.isawaitable(value):
            self._fail(name, _awaitable_refusal(name, value))
            return False
        return self._store(name, value)

    def _store(self, name: str, value: object) -> bool:
        """Keeps `name`'s value; readies each node it was the last wait of.

        `on_value` hears of it first. `False`, the run failed, when that raises:
        a `CacheWarning` made an error by a warnings filter, say. That is raised
        without the node's note, as the sync driver raises it.
        """
        if self._on_value is not None:
            try:
                self._on_value(name, value)
            except _STOPPING:
                raise
            except BaseException as error:  # noqa: BLE001  # raised once the run stops
                self._fail(name, error, note=False)
                return False
        self._values[name] = value
        self._remaining -= 1
        pending = self._pending
        for after in self._schedule.dependents.get(name, ()):
            pending[after] -= 1
            if not pending[after]:
                self._ready.append(after)
        if not self._remaining and not self._finished.done():
            self._finished.set_result(None)
        return True

    def _fail(self, name: str, error: BaseException, *, note: bool = True) -> None:
        """Keeps the first failure, noted; cancels everything still running.

        Without `note`, the failure is not the node's, and is raised as it is.
        """
        if self._failure is not None:
            return
        self._failure = noted(error, name) if note else error
        self._cancelled = self._tasks()
        self._stop()

    def _stop(self) -> None:
        """Ends the run and cancels every running node.

        A task takes its cancellation; a thread cannot be interrupted, so its
        future is cancelled and its result, when it comes, dropped.
        """
        self._ready.clear()
        if not self._finished.done():
            self._finished.set_result(None)
        for future in self._running:
            future.cancel()
