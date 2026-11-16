"""The async driver's run: a prepared plan on the core scheduler, stepped on a loop.

Each run is a `_core.Stepper` over its plan, the core scheduler the parallel sync
driver runs on (Plan 11, Q65). The stepper hands out ready tasks in order and takes
their outcomes back; it does a late lookup, a probe and an expand's iterator itself,
and fires the hooks, `on_value` and `late.store`, as the sync driver's binding does.
This module runs what it hands out where each belongs:

- an `async def` node, or an async generator's pull, is awaited as a task;
- a sync node, or a sync generator's pull, runs in the loop's default executor, in
  the caller's context, so the loop keeps running (Q65); on a driver with
  `with_threads`, on the driver's pool; or on the loop, with `sync_nodes="inline"`;
- a generated `extract_*` node, the call making an async generator, and ending an
  expand's iterator run on the loop.

Every store call runs on the loop's thread, but a sync generator's `late.pulled`,
which runs as its item is read. A run's values live in its stepper, so runs on one
loop, or on loops in several threads, never share one (Review Focus 5). Nothing
recurses over the graph's depth.

The first failure stops the run: no task starts after it, every running task is
cancelled once, and the run ends once they have; a sync node in a thread can't be
interrupted, so its future is cancelled and its result dropped. A generator is
never closed while a pull of it runs: a sync one's pull in a thread is waited for.
"""

import asyncio
import contextvars
import functools
import inspect
import os
import threading
import weakref
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, cast

from fnloom import _core, _extract
from fnloom._blocks import AsyncItems
from fnloom._caching import Errors
from fnloom._threads import CANCELLED

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Iterator, Mapping, Sequence

    from fnloom._caching import CacheRun, LateKeys
    from fnloom._hooks import Dispatch
    from fnloom._scan import NodeDef
    from fnloom._threads import Workers

# What the run calls with each node's name and value as the node succeeds.
type OnValue = Callable[[str, object], None]

type SyncNodes = Literal["thread", "inline"]


def checked_sync_nodes(sync_nodes: str, *, threaded: bool = False) -> SyncNodes:
    """`sync_nodes`, when it is `"inline"` or `"thread"`; `ValueError` if not.

    `"inline"` is refused too on a driver with `with_threads`, whose pool runs
    every sync node.
    """
    if sync_nodes == "inline":
        if threaded:
            msg = (
                "with_threads runs sync nodes on the driver's pool, so "
                "sync_nodes='inline' can't apply; drop one"
            )
            raise ValueError(msg)
        return "inline"
    if sync_nodes == "thread":
        return "thread"
    msg = f"sync_nodes must be 'inline' or 'thread', not {sync_nodes!r}"
    raise ValueError(msg)


# How a computed node is called: awaited as a task, run in a worker thread, or run
# on the loop.
type _Kind = Literal["await", "thread", "inline"]


class Call(NamedTuple):
    """How the run calls one computed node: its payload, and where it runs."""

    payload: Callable[..., Any]
    kind: _Kind


def awaits(node: NodeDef) -> bool:
    """Whether calling `node`'s payload returns a coroutine to await.

    Read from the function as written, so a `functools.partial` over it, or a row's
    call that maps keywords onto it, counts as what it calls.
    """
    return inspect.iscoroutinefunction(node.origin) or inspect.iscoroutinefunction(
        node.payload
    )


def calls(nodes: Mapping[str, NodeDef], sync_nodes: SyncNodes) -> dict[str, Call]:
    """How the run calls each node of a graph, by name.

    Calling an async generator function runs none of its body, so it runs on the
    loop; its items are awaited.
    """
    found: dict[str, Call] = {}
    for name, node in nodes.items():
        kind: _Kind
        if _extract.is_generated(node) or inspect.isasyncgenfunction(node.origin):
            kind = "inline"
        elif awaits(node):
            kind = "await"
        else:
            kind = sync_nodes
        found[name] = Call(node.payload, kind)
    return found


class Runner:
    """How a driver's runs step: its calls, its pool and limits, its in-flight bound."""

    __slots__ = ("_in_flight", "_pooled", "_workers", "calls", "sequential")

    def __init__(self, calls: Mapping[str, Call], workers: Workers | None) -> None:
        """Runs each node by `calls`, granted slots by `workers`, if given.

        With a pool, every sync node runs on it, holding a worker slot. An expand is
        pulled while fewer than twice the workers, or the CPUs, of its branches are
        unfinished.
        """
        self.calls = calls
        self._workers = workers
        threaded = workers is not None and workers.max_workers is not None
        self._pooled = (
            tuple(name for name, call in calls.items() if call.kind == "thread")
            if threaded
            else ()
        )
        count = workers.max_workers if workers is not None else None
        self._in_flight = 2 * (count or os.process_cpu_count() or 1)
        # Every node is called on the loop, start to end, and none waits parked for a
        # slot, so no two nodes overlap between their start and their end.
        self.sequential = workers is None and all(
            call.kind == "inline" for call in calls.values()
        )

    async def run(  # noqa: PLR0913, PLR0917  # a run's parts, as `_core.Plan.run` takes them
        self,
        plan: _core.Plan,
        inputs: Mapping[str, object],
        overrides: Mapping[str, object],
        observers: Sequence[_core.ObserverBase] = (),
        on_value: OnValue | None = None,
        late: LateKeys | None = None,
    ) -> dict[str, object]:
        """The requested outputs of a run of `plan`, keyed by name; see the module.

        `observers`, `on_value` and `late` are `_core.Plan.run`'s.
        """
        workers = self._workers
        limits = None if workers is None else workers.limits()
        threads = None if workers is None else workers.threads()
        stepper = plan.start(
            dict(inputs),
            dict(overrides),
            observers,
            on_value,
            late,
            limits=limits,
            threads=threads,
            pooled=self._pooled,
            in_flight=self._in_flight,
        )
        # A run inside a pooled task pools nothing: its own loop's executor runs them.
        pool = threads if stepper.pooling else None
        # The stop flag of the run that started this one, if any, which it follows.
        parent = CANCELLED.get()
        # Its stop flag, for `fnloom.cancelled()` and the runs its nodes start.
        token = CANCELLED.set(stepper.cancelled)
        try:
            return await _Run(stepper, self.calls, pool, limits, parent).wait()
        finally:
            CANCELLED.reset(token)


class Schedule:
    """A prepared plan's runs on the loop."""

    __slots__ = ("_plan", "_runner")

    def __init__(self, plan: _core.Plan, runner: Runner) -> None:
        """Runs `plan` by `runner`."""
        self._plan = plan
        self._runner = runner

    async def run(
        self,
        inputs: Mapping[str, object],
        overrides: Mapping[str, object],
    ) -> dict[str, object]:
        """The requested outputs, by name."""
        return await self._runner.run(self._plan, inputs, overrides)

    async def hooked(
        self,
        inputs: Mapping[str, object],
        overrides: Mapping[str, object],
        dispatch: Dispatch,
    ) -> dict[str, object]:
        """`run`, its node events sent to `dispatch`, a run's own dispatcher."""
        return await self._runner.run(
            self._plan, inputs, overrides, dispatch.observers()
        )


class CachedSchedule:
    """A cached plan's runs on the loop, through its `CacheRun`; safe to share."""

    __slots__ = ("_cache_run", "_runner")

    def __init__(self, cache_run: CacheRun, runner: Runner) -> None:
        """Runs `cache_run`'s plans by `runner`."""
        self._cache_run = cache_run
        self._runner = runner

    async def run(
        self,
        inputs: dict[str, object],
        overrides: dict[str, object],
    ) -> dict[str, object]:
        """The requested outputs, by name, the hits loaded, the rest stored.

        The nested runs of `recompute` nodes go first, innermost first, each run on
        the loop: a loop, not a recursion, however deep they nest. Each gives the
        next every value it computed or loaded, as the sync driver's do.
        """
        levels = [self._cache_run]
        while (inner := levels[-1].nested) is not None:
            levels.append(inner)
        warned: set[str] = set()
        versions = self._cache_run.versions(inputs, overrides, warned)
        known: dict[str, object] = {}
        errors = Errors()
        for level in reversed(levels):
            outer = level is self._cache_run
            begun = level.begin(
                inputs,
                overrides,
                known=known,
                versions=versions if outer else dict(versions),
                warned=warned,
                values=None if outer else {},
                native=False,
                errors=errors,
            )
            on_value = functools.partial(level.finish, begun)
            result = await self._runner.run(
                begun.plan,
                inputs,
                begun.overrides,
                begun.observers(),
                on_value,
                begun.late,
            )
            values = begun.values
            if values is None:
                known = result
            else:
                values.update(begun.hits)
                values.update(result)
                # Cast: ty can't infer a value fed back into the next `begin`.
                known = cast("dict[str, object]", values)
        return known

    async def hooked(
        self,
        inputs: dict[str, object],
        overrides: dict[str, object],
        dispatch: Dispatch,
        start: Callable[[tuple[str, ...]], None],
    ) -> dict[str, object]:
        """`run`, reporting every node to `dispatch`, as `CacheRun.hooked` does.

        Runs `CacheRun.reported_levels`, the sync driver's levels and events. A node
        whose `after_node` raised is never stored (`CacheRun.kept`).
        """
        levels = self._cache_run.reported_levels(inputs, overrides, dispatch, start)
        level, begun = next(levels)
        while True:
            if begun.late is not None:
                begun.late.dispatch = dispatch
                dispatch.late = begun.late
            result = await self._runner.run(
                begun.plan,
                inputs,
                begun.overrides,
                begun.observers(dispatch.observers()),
                functools.partial(level.kept, begun, dispatch),
                begun.late,
            )
            if begun.values is not None:
                begun.values.update(begun.hits)
                begun.values.update(result)
            try:
                level, begun = levels.send(
                    result if begun.values is None else begun.values
                )
            except StopIteration:
                return result


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


async def _held(finished: asyncio.Future[None]) -> None:
    """Waits until `finished` is done, whatever cancels the waiter.

    A cancellation while waiting is held back: the caller hears of it, as it
    hears of its first, only once every node task has stopped.
    """
    while not finished.done():
        try:
            await asyncio.shield(finished)
        except asyncio.CancelledError:
            continue


def _next(items: Iterator[object]) -> tuple[bool, object]:
    """A sync iterator's next item as `(True, item)`, or `(False, value)` at its end.

    A future can't carry `StopIteration`, so its end is a value.
    """
    try:
        return True, next(items)
    except StopIteration as stop:
        return False, stop.value


async def _pull(items: AsyncItems) -> tuple[bool, object]:
    """The next async item as `(True, item)`, or `(False, None)` at the end."""
    try:
        return True, await items.__anext__()
    except StopAsyncIteration:
        return False, None


class _Guard:
    """A sync node's call in a thread, which its stopping run may abandon.

    The run then hands the guard the call's slots (`Stepper.detach`), which it gives
    back once the call returns, so a limit holds while an abandoned call runs. A call
    abandoned on the driver's pool has its thread replaced at once (`Threads.abandon`),
    and the thread leaves the pool once the call returns (`Threads.leave`). A call
    the run detached before its thread began it never runs.
    """

    __slots__ = ("abandoned", "detached", "held", "lock", "returned", "started")

    def __init__(self) -> None:
        """A call not yet started, holding its own slots."""
        self.lock = threading.Lock()
        self.started = False
        self.returned = False
        self.detached = False
        self.abandoned = False
        self.held: _core.Held | None = None

    def call(self, function: Callable[[], object]) -> object:
        """`function()`, then the abandoned call's slots given back, if it was.

        `None`, without calling it, once its run detached it: so a stopping run
        sees either a call it must replace the thread of, or one that never runs.
        """
        with self.lock:
            self.started = True
            detached = self.detached
        try:
            return None if detached else function()
        finally:
            with self.lock:
                self.returned = True
                held = self.held
                abandoned = self.abandoned
            if abandoned:
                _core.Threads.leave()
            if held is not None:
                held.release()


def _retrieved(future: asyncio.Future[object]) -> None:
    """Marks a discarded future's outcome retrieved, so asyncio never reports it."""
    if not future.cancelled():
        future.exception()


# What a node may raise that the run does not catch: they stop the loop itself.
_STOPPING = (KeyboardInterrupt, SystemExit)

# The bits of the status `Stepper.next` gives when it hands out no task.
_DONE = 1
_WAITING = 2
_HALTED = 4
# How often a run started inside another run looks whether that one stopped (O22).
_FOLLOW = 0.05


class _Run:
    """One run: its stepper, and the tasks and threads it has running.

    Driven by the loop's callbacks, so it is only ever touched from the loop's
    thread. A worker thread only computes its node's value or pulls its item.
    """

    __slots__ = (
        "__weakref__",
        "_calls",
        "_factory",
        "_finished",
        "_guards",
        "_halted",
        "_limits",
        "_loop",
        "_over",
        "_parent",
        "_running",
        "_stepper",
        "_stopped",
        "_threads",
        "_waking",
    )

    def __init__(
        self,
        stepper: _core.Stepper,
        calls: Mapping[str, Call],
        threads: _core.Threads | None,
        limits: _core.Limits | None,
        parent: _core.Stop | None = None,
    ) -> None:
        """A run of `stepper`, each node called by `calls`, on `threads` if given.

        It waits for `limits`' slots when refused one, and stops once `parent`,
        the stop flag of the run that started it, is set.
        """
        self._stepper = stepper
        self._parent = parent
        self._calls = calls
        self._threads = threads
        self._limits = limits
        self._loop = asyncio.get_running_loop()
        # Read once per run: without a custom factory, tasks are made directly.
        self._factory = self._loop.get_task_factory() is not None
        # Each running task's future: its stepper task, node and work.
        self._running: dict[asyncio.Future[Any], tuple[int, str, str]] = {}
        self._finished: asyncio.Future[None] = self._loop.create_future()
        # The guard of each running sync compute, on a driver with limits.
        self._guards: dict[asyncio.Future[Any], _Guard] = {}
        # Whether the stepper is stopping, as its calls said.
        self._halted = False
        # Whether the running tasks were cancelled, once the run stopped.
        self._stopped = False
        # Whether `wait` has returned or raised: an outcome after it is dropped. Not
        # `_finished.done()`, which a cancelled caller makes true before it stops.
        self._over = False
        # Whether a waker is registered for a slot this run waits on.
        self._waking = False

    async def wait(self) -> dict[str, object]:
        """Runs every task; the outputs, or the run's failure, raised.

        Whether the run fails or its caller is cancelled, every running node task
        is cancelled and awaited, and every open iterator ended, before the
        exception goes on, as `TaskGroup` does.
        """
        try:
            self._drain()
            if self._parent is not None and not self._finished.done():
                self._loop.call_later(_FOLLOW, self._follow)
            await self._finished
        except asyncio.CancelledError:
            # The caller was cancelled: its nodes stop before the caller hears of it.
            if self._finished.cancelled():
                # Cancelled with the caller, which was awaiting it.
                self._finished = self._loop.create_future()
            self._stop()
            self._drain()
            await _held(self._finished)
            raise
        except BaseException:
            # The loop is stopping (`KeyboardInterrupt`, `SystemExit`): no waiting.
            self._stop()
            raise
        finally:
            self._over = True
        return self._stepper.outputs()

    def _drain(self) -> None:
        """Starts every task the stepper hands out; ends the run once none is left."""
        next_task = self._stepper.next
        start = self._start
        while type(task := next_task()) is tuple:
            start(task)
        status = cast("int", task)
        if status & _HALTED or self._halted:
            self._halted = True
            if self._cancel():
                # A task detached as it was cancelled is complete: look again.
                self._drain()
                return
        if status & _DONE:
            if not self._finished.done():
                self._finished.set_result(None)
        elif status & _WAITING and not self._waking:
            self._wait_for_slot()

    def _complete(self, task: int, kind: _core.Completion, value: object) -> None:
        """Hands the stepper task `task`'s outcome."""
        if self._stepper.complete(task, kind, value):
            self._halted = True

    def _start(self, handed: _core.Handed) -> None:  # noqa: C901, PLR0912, PLR0915  # per work
        """Starts one task, where its work runs; a call on the loop completes here.

        A compute on the loop is run here, its frame saved: the hot path.
        """
        task, name, _, work, payload, context = handed
        function, kind = self._calls[name]
        guard = None
        try:
            if work == "compute":
                if kind == "inline":
                    if context is None:
                        value = function(**payload)
                    else:
                        value = context.run(function, **payload)
                    if inspect.isawaitable(value):
                        value = _awaitable_refusal(name, value)
                        self._complete(task, "refused", value)
                    elif self._stepper.complete(task, "value", value):
                        self._halted = True
                    return
                if kind == "await":
                    future = self._task(function(**payload), context)
                    # Not a cancelled one: `result()` would use up its own error.
                    if future.done() and not self._halted and not future.cancelled():
                        # Eagerly done, a node that never suspended: the hot path.
                        try:
                            value = future.result()
                        except BaseException:  # noqa: BLE001, S110  # `_settle` reads it below
                            pass
                        else:
                            if self._stepper.complete(task, "value", value):
                                self._halted = True
                            return
                else:
                    call = functools.partial(function, **payload)
                    if self._limits is not None or self._threads is not None:
                        # Its slots, and its pool thread, outlive the run that abandons
                        # it, until it returns.
                        guard = _Guard()
                        call = functools.partial(guard.call, call)
                    future = self._threaded(call, context)
            elif isinstance(payload, AsyncItems):
                awaited = (
                    _pull(payload)
                    if work == "pull"
                    else payload.afinish(work == "finish")
                )
                future = self._task(awaited, context)
            elif kind != "inline":
                # A sync iterator's reads run where its node would, and its end, which
                # holds no slot, in the loop's executor: so a `finally` that blocks
                # never stalls the loop, nor waits for a pool thread a hung call holds.
                if work == "pull":
                    future = self._threaded(functools.partial(_next, payload), context)
                else:
                    future = self._loop.run_in_executor(
                        None,
                        (context or contextvars.copy_context()).run,
                        functools.partial(payload.finish, work == "finish"),
                    )
            else:
                # An inline expand's reads and its end run on the loop.
                self._on_loop(task, work, payload, cast("contextvars.Context", context))
                return
        except _STOPPING:
            raise
        except BaseException as error:  # noqa: BLE001  # the task's own failure
            self._complete(task, "error", error)
            return
        if future.done():
            self._settle(future, task, name, work)
            return
        self._running[future] = (task, name, work)
        if guard is not None:
            self._guards[future] = guard
        future.add_done_callback(self._done)

    def _on_loop(
        self,
        task: int,
        work: str,
        items: Any,  # noqa: ANN401  # a sync item iterator, `_blocks.Items`
        context: contextvars.Context,
    ) -> None:
        """Pulls or ends a sync item iterator on the loop, in its own context."""
        try:
            if work == "pull":
                more, value = context.run(_next, items)
            else:
                more, value = True, context.run(items.finish, work == "finish")
        except _STOPPING:
            raise
        except BaseException as error:  # noqa: BLE001  # the expand's own failure
            self._complete(task, "error", error)
            return
        self._complete(task, "value" if more else "done", value)

    def _task(
        self,
        coroutine: Coroutine[Any, Any, object],
        context: contextvars.Context | None,
    ) -> asyncio.Future[object]:
        """A task awaiting `coroutine`, in `context` when given.

        Eager: a node that never suspends is done here, with no loop turn. A
        custom task factory sees the task, `eager_start` passed on; without one,
        the task is made directly, skipping `create_task`'s dispatch.
        """
        loop = self._loop
        if self._factory:
            if context is None:
                return loop.create_task(coroutine, eager_start=True)
            return loop.create_task(coroutine, eager_start=True, context=context)
        return asyncio.Task(coroutine, loop=loop, eager_start=True, context=context)

    def _threaded(
        self, function: Callable[[], object], context: contextvars.Context | None
    ) -> asyncio.Future[object]:
        """`function()` in a thread, in `context` or a copy of the caller's.

        On the driver's pool when it has one, else in the loop's default executor,
        as `asyncio.to_thread` would, but as a future, not a task: cancelling it
        never waits for the thread.
        """
        if context is None:
            context = contextvars.copy_context()
        threads = self._threads
        if threads is None:
            return self._loop.run_in_executor(None, context.run, function)
        return asyncio.wrap_future(threads.submit(function, context), loop=self._loop)

    def _done(self, future: asyncio.Future[object]) -> None:
        """A task or a thread finished: its outcome goes to the stepper."""
        found = self._running.pop(future, None)
        if self._guards:
            self._guards.pop(future, None)
        if found is None or self._over:
            _retrieved(future)
            return
        try:
            self._settle(future, *found)
            self._drain()
        except _STOPPING:
            raise
        except BaseException as error:  # noqa: BLE001  # raised by the run's `wait`
            self._broke(error)

    def _settle(
        self, future: asyncio.Future[Any], task: int, name: str, work: str
    ) -> None:
        """Completes a finished task with its outcome; dropped once the run stopped."""
        if self._halted:
            _retrieved(future)
            self._complete(task, "dropped", None)
            return
        if future.cancelled():
            self._complete(task, "error", _cancellation(name, future))
            return
        error = future.exception()
        if error is not None:
            self._complete(task, "error", error)
            return
        value = future.result()
        if work == "pull":
            more, value = value
            self._complete(task, "value" if more else "done", value)
            return
        threaded = not isinstance(future, asyncio.Task)
        if threaded and work == "compute" and inspect.isawaitable(value):
            self._complete(task, "refused", _awaitable_refusal(name, value))
            return
        if self._stepper.complete(task, "value", value):
            self._halted = True

    def _cancel(self) -> bool:
        """Cancels every running node and pull, once, as the run stops.

        A task takes its cancellation; a thread cannot be interrupted, so a node's
        future is cancelled and its result, when it comes, dropped, while its slots
        stay taken until its call returns. A pull in a thread is waited for, so its
        generator is ended only after it returns, and an iterator's end is never
        cancelled. Whether it completed a task, detaching it.
        """
        if self._stopped:
            return False
        self._stopped = True
        detached = False
        replaced = 0
        for future, (task, _, work) in list(self._running.items()):
            guard = self._guards.pop(future, None)
            if guard is not None:
                with guard.lock:
                    if not guard.returned:
                        guard.detached = True
                        guard.held = self._stepper.detach(task)
                        del self._running[future]
                        detached = True
                        # On the pool, and in a thread already: that thread is lost
                        # until the call returns.
                        if self._threads is not None and guard.started:
                            guard.abandoned = True
                            replaced += 1
            if work == "compute" or (
                work == "pull" and isinstance(future, asyncio.Task)
            ):
                future.cancel()
        for _ in range(replaced):
            cast("_core.Threads", self._threads).abandon()
        return detached

    def _stop(self) -> None:
        """Stops the run and cancels what runs; a second stop does nothing more.

        A node still cleaning up from the first is not interrupted, as with
        `TaskGroup`.
        """
        self._stepper.stop()
        self._halted = True
        self._cancel()

    def _broke(self, error: BaseException) -> None:
        """Ends the run with `error`, an error of the run itself, not of a node."""
        self._stop()
        if not self._finished.done():
            self._finished.set_exception(error)

    def _follow(self) -> None:
        """Stops the run once the run that started it stopped, else looks again later.

        The stepper notices too, but only as a task of this run completes: one
        `async def` node awaiting for long would keep the run going meanwhile.
        """
        if self._over or self._finished.done():
            return
        parent = self._parent
        if parent is not None and not bool(parent):
            self._loop.call_later(_FOLLOW, self._follow)
            return
        try:
            # The stepper stops as it sees the stopped parent, and the drain cancels
            # what runs.
            self._drain()
        except _STOPPING:
            raise
        except BaseException as error:  # noqa: BLE001  # raised by the run's `wait`
            self._broke(error)

    def _wait_for_slot(self) -> None:
        """Wakes the run at the next release of a slot of the driver's limits.

        A release between the refusal and the registration woke no one, so the
        run looks again once registered. The wake runs in the run's own context,
        not the releasing thread's, and holds the run only weakly: a run that ends
        first leaves nothing behind for its waker to keep alive.
        """
        limits = self._limits
        if limits is None:
            return
        self._waking = True
        loop = self._loop
        context = contextvars.copy_context()
        woken = weakref.WeakMethod(self._woken)

        def wake() -> None:
            method = woken()
            if method is not None:
                loop.call_soon_threadsafe(method, context=context)

        limits.add_waker(wake)
        self._drain()

    def _woken(self) -> None:
        """A slot was released: the run looks for tasks again."""
        self._waking = False
        if self._over:
            return
        try:
            self._drain()
        except _STOPPING:
            raise
        except BaseException as error:  # noqa: BLE001  # raised by the run's `wait`
            self._broke(error)
