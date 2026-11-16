"""Running one run's nodes in parallel, and limits: a driver's pool, caps and checks."""

import atexit
import contextvars
import math
import os
import threading
import time
import weakref
from collections.abc import Callable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from fnloom import _core
from fnloom._decorators import decorations
from fnloom._extract import extractor

if TYPE_CHECKING:
    from fnloom._scan import NodeDef

__all__ = [
    "CANCELLED",
    "HELD",
    "Caps",
    "Workers",
    "cancelled",
    "caps_of",
    "checked_groups",
    "checked_max_workers",
    "checked_timeout",
    "close_all",
    "make_limits",
    "make_threads",
    "run_sets",
]

# The slots the running calls of this context hold, as `(limits, slot, live)`
# entries the binding makes: a node's index, or a parallel run's worker slots,
# and `[True]` until the slot is given back. A run started inside one sees them
# as its ancestors': it is refused a node or group slot whose every unit they
# hold, and runs its tasks on the worker slot they hold. A worker entry has a
# fourth item, `[True]` while no run started from its context has taken that
# slot: one such run at a time does, with `pop()`, and gives it back at its end.
type _Entry = tuple[int, int, list[bool]] | tuple[int, int, list[bool], list[bool]]
HELD: Final[ContextVar[tuple[_Entry, ...]]] = ContextVar("fnloom_held", default=())

# The stop flag of the parallel run a task belongs to, set in the context every
# task of the run copies: true once the run is stopping.
CANCELLED: Final[ContextVar[_core.Stop | None]] = ContextVar(
    "fnloom_cancelled", default=None
)


def cancelled() -> bool:
    """Whether the run calling this is stopping, so a node can return early.

    `True` once a run of a driver built `with_threads`, or an async run, is
    stopping: its `timeout=` passed, another node failed, it was interrupted,
    or its caller was cancelled. `False` outside a run, and in a run of a
    `Driver` without `with_threads`, unless that run was started by a node of a
    stopping one.
    """
    stop = CANCELLED.get()
    return stop is not None and bool(stop)


def checked_timeout(timeout: object) -> float:
    """`execute`'s `timeout=`, in seconds: a finite `int` or `float` of at least 0.

    A `bool` or another type is a `TypeError`; a negative, NaN or infinite one a
    `ValueError`.
    """
    if isinstance(timeout, bool) or not isinstance(timeout, int | float):
        msg = f"timeout must be a number of seconds, not {timeout!r}"
        raise TypeError(msg)
    if not math.isfinite(timeout) or timeout < 0:
        msg = (
            f"timeout must be a finite number of seconds of at least 0, not {timeout!r}"
        )
        raise ValueError(msg)
    return float(timeout)


# Every pool alive, which interpreter exit closes.
_LIVE: Final[weakref.WeakSet[_core.Threads]] = weakref.WeakSet()
# Pools and limits inherited across `os.fork()`: never dropped, so the child never
# touches them.
_INHERITED: Final[list[object]] = []


def checked_max_workers(max_workers: object) -> int:
    """`with_threads`' `max_workers`: `None` is `os.process_cpu_count() or 1`.

    A `bool` or another non-`int` is a `TypeError`, and a value under 1 a
    `ValueError`, each naming the argument.
    """
    if max_workers is None:
        return os.process_cpu_count() or 1
    if isinstance(max_workers, bool) or not isinstance(max_workers, int):
        msg = f"max_workers must be an int or None, not {max_workers!r}"
        raise TypeError(msg)
    if max_workers < 1:
        msg = f"max_workers must be at least 1, not {max_workers}"
        raise ValueError(msg)
    return max_workers


def checked_groups(groups: object) -> dict[str, int]:
    """`with_limits`' groups: each a non-empty `str` mapped to an `int` of at least 1.

    A non-mapping, a name that is no `str`, or a size that is no `int` (a `bool`
    included) is a `TypeError`; an empty name or a size under 1 a `ValueError`.
    """
    if not isinstance(groups, Mapping):
        msg = f"with_limits takes a mapping of group names to sizes, not {groups!r}"
        raise TypeError(msg)
    checked: dict[str, int] = {}
    for name, size in groups.items():
        if not isinstance(name, str):
            msg = f"a group name must be a str, not {name!r}"
            raise TypeError(msg)
        if not name:
            msg = "a group name must be a non-empty str"
            raise ValueError(msg)
        if isinstance(size, bool) or not isinstance(size, int):
            msg = f"group {name!r} must be sized by an int, not {size!r}"
            raise TypeError(msg)
        if size < 1:
            msg = f"group {name!r} must have a size of at least 1, not {size}"
            raise ValueError(msg)
        checked[name] = size
    return dict(sorted(checked.items()))


@dataclass(frozen=True, slots=True)
class Caps:
    """A graph's limits: each limited node's cap and group, and each group's size."""

    nodes: Mapping[str, int]
    groups_of: Mapping[str, str]
    sizes: Mapping[str, int]


def caps_of(nodes: Sequence[NodeDef], sizes: Mapping[str, int]) -> Caps | None:
    """The caps `@limit` and `with_limits` give `nodes`; `None` for none.

    An extracted field is never limited: its producer's call is. `ValueError`
    for a group a node is in with no size, or a size of a group no node is in.
    """
    own: dict[str, int] = {}
    groups_of: dict[str, str] = {}
    for node in nodes:
        policy = decorations(node.origin).limit
        if policy is None or extractor(node) is not None:
            continue
        if policy.n is not None:
            own[node.name] = policy.n
        if policy.group is not None:
            groups_of[node.name] = policy.group
    problems = [
        f"group {group!r} of {name!r} has no size; give it one with "
        f"Builder.with_limits({{{group!r}: n}})"
        for name, group in sorted(groups_of.items())
        if group not in sizes
    ]
    used = set(groups_of.values())
    problems.extend(
        f"with_limits sizes group {group!r}, but no node is in it; "
        f"put one in with @limit(group={group!r})"
        for group in sorted(sizes)
        if group not in used
    )
    if problems:
        raise ValueError("\n".join(problems))
    if not own and not groups_of:
        return None
    return Caps(own, groups_of, dict(sizes))


def make_limits(
    graph: _core.Graph, caps: Caps | None, max_workers: int | None
) -> _core.Limits:
    """A driver's limits: at most `max_workers` of its tasks at once, and `caps`."""
    if caps is None:
        return _core.Limits(graph, max_workers)
    return _core.Limits(
        graph, max_workers, dict(caps.nodes), dict(caps.groups_of), dict(caps.sizes)
    )


def make_threads(limits: _core.Limits, max_workers: int) -> _core.Threads:
    """A driver's pool of `max_workers - 1` threads, started at its first run."""
    threads = _core.Threads(limits, max_workers)
    _LIVE.add(threads)
    return threads


class Workers:
    """A driver's limits and pool, made again in a process forked from its maker's.

    A driver with `with_threads` has both; a driver with limits and no threads,
    its limits alone. Each driver object has its own: a copy, a rebuilt or an
    unpickled driver makes new ones.
    """

    __slots__ = ("_caps", "_graph", "_limits", "_pid", "_threads", "max_workers")

    def __init__(
        self, graph: _core.Graph, max_workers: int | None, caps: Caps | None
    ) -> None:
        """`max_workers` workers, the run's calling thread among them, and `caps`.

        `None` workers makes no pool: the limits alone.
        """
        self.max_workers = max_workers
        self._graph = graph
        self._caps = caps
        self._pid = os.getpid()
        self._limits, self._threads = self._made()

    def _made(self) -> tuple[_core.Limits, _core.Threads | None]:
        limits = make_limits(self._graph, self._caps, self.max_workers)
        if self.max_workers is None:
            return limits, None
        return limits, make_threads(limits, self.max_workers)

    def _current(self) -> None:
        """Makes new limits and pool when this process is a fork of the maker's."""
        pid = os.getpid()
        if pid != self._pid:
            # Their locks may be held by threads the child doesn't have: never touched.
            _INHERITED.append((self._limits, self._threads))
            self._limits, self._threads = self._made()
            self._pid = pid

    @property
    def caps(self) -> Caps | None:
        """The graph's limits, `None` for none."""
        return self._caps

    def threads(self) -> _core.Threads | None:
        """The pool, or `None` without `with_threads`."""
        self._current()
        return self._threads

    def limits(self) -> _core.Limits:
        """The limits every run of the driver is granted slots by."""
        self._current()
        return self._limits


# How long `run_many`'s caller waits for its other runners before it checks for
# signals again (O22).
_SLICE: Final = 0.05
# An input set's result before it is in.
_MISSING: Final = object()


def run_sets[T](
    one: Callable[[int, float | None], T],
    count: int,
    runners: int,
    timeout: float | None,
) -> list[T]:
    """`run_many`'s input sets 0 to `count - 1`, each run by `one(index, timeout)`.

    `timeout`, in seconds, covers every set together: each set gets what is left
    of it when it opens, and a set not open yet when it passes never opens and
    raises `TimeoutError`. With one runner, the sets run one after another on the
    calling thread, as a loop of `run` would, and the first failure is raised.

    With more, the caller and `runners - 1` threads each open the next set, by
    index, as their last one closes, so at most `runners` sets are open at once
    (O17). Each set runs in its own copy of the caller's context, whose
    `CANCELLED` is the batch's stop flag: once a set fails, no set opens, and
    every open set stops, its running nodes finishing first. Then the failure of
    the lowest-indexed failing set seen is raised (`_Batch.outcome`). A
    `KeyboardInterrupt` in the caller stops the open sets and is raised at once.
    """
    deadline = None if timeout is None else time.monotonic() + timeout
    if runners <= 1:
        results: list[T] = []
        for index in range(count):
            left = _left(deadline, timeout, index)
            try:
                results.append(one(index, left))
            except BaseException as error:
                _noted(error, index)
                raise
        return results
    batch = _Batch(one, count, deadline, timeout)
    helpers: list[threading.Thread] = []
    try:
        for number in range(1, runners):
            thread = threading.Thread(
                target=batch.work, name=f"fnloom-run-many-{number}", daemon=True
            )
            try:
                thread.start()
            except RuntimeError:
                # No thread to be had: the sets run on the runners there are.
                break
            helpers.append(thread)
        batch.work(caller=True)
        for thread in helpers:
            while thread.is_alive():
                thread.join(_SLICE)
    except BaseException:
        batch.stop.set()
        raise
    return batch.outcome()


def _left(deadline: float | None, timeout: float | None, index: int) -> float | None:
    """What's left of `run_many`'s timeout as set `index` opens; else `TimeoutError`."""
    if deadline is None:
        return None
    left = deadline - time.monotonic()
    if left <= 0:
        msg = (
            f"run_many did not finish within its timeout of {timeout} s; input set "
            f"{index} and the sets after it did not start"
        )
        raise TimeoutError(msg)
    return left


def _noted(error: BaseException, index: int) -> None:
    """Notes on `error` which input set of `run_many` raised it."""
    error.add_note(f"while running input set {index} of run_many")


def _derived(error: BaseException) -> bool:
    """Whether `error` only says its set was stopped.

    That is a `StoppedError`, alone or leading the group its run's hooks added
    their errors to.
    """
    if isinstance(error, BaseExceptionGroup):
        return isinstance(error.exceptions[0], _core.StoppedError)
    return isinstance(error, _core.StoppedError)


class _Batch[T]:
    """The input sets of one `run_many` call, opened in index order by its runners."""

    __slots__ = (
        "_context",
        "_count",
        "_deadline",
        "_lock",
        "_next",
        "_one",
        "_pid",
        "_results",
        "_timeout",
        "failures",
        "stop",
    )

    def __init__(
        self,
        one: Callable[[int, float | None], T],
        count: int,
        deadline: float | None,
        timeout: float | None,
    ) -> None:
        """Sets to run through `one`, none opened yet; see `run_sets`."""
        self._one = one
        self._count = count
        self._deadline = deadline
        self._timeout = timeout
        self._lock = threading.Lock()
        self._next = 0
        self._results: list[object] = [_MISSING] * count
        # Each failure: the set's index, its error, and whether the set opened.
        self.failures: list[tuple[int, BaseException, bool]] = []
        # Under the run the caller is in, if any: so the sets stop with it too.
        self.stop = _core.Stop()
        self._context = contextvars.copy_context()
        self._context.run(CANCELLED.set, self.stop)
        self._pid = os.getpid()

    def work(self, *, caller: bool = False) -> None:
        """Opens the next set and runs it, until none is left or the batch stops."""
        while True:
            with self._lock:
                if self.stop or self._next >= self._count:
                    return
                index = self._next
                self._next += 1
            opened = False
            try:
                left = _left(self._deadline, self._timeout, index)
                opened = True
                self._results[index] = self._context.copy().run(self._one, index, left)
            except BaseException as error:
                if os.getpid() != self._pid:
                    # A node forked, and this is the child: its lock may be held by
                    # a thread it doesn't have. The caller raises; a helper ends.
                    if caller:
                        raise
                    return
                with self._lock:
                    self.failures.append((index, error, opened))
                    # Under the lock: no runner opens a set once one has failed.
                    self.stop.set()
                if caller and isinstance(error, KeyboardInterrupt):
                    _noted(error, index)
                    raise

    def outcome(self) -> list[T]:
        """Every set's result, once every runner is done.

        With failures, the lowest-indexed one is raised, noted with its index
        when its set opened. An interrupt or an exit (no `Exception`) comes
        before any error, and a set's `StoppedError` only when no set failed
        otherwise, since it means only that the set was stopped.
        """
        if self.failures:
            index, error, opened = min(
                self.failures,
                key=lambda failure: (
                    isinstance(failure[1], Exception),
                    _derived(failure[1]),
                    failure[0],
                ),
            )
            if opened:
                _noted(error, index)
            raise error
        if any(result is _MISSING for result in self._results):
            msg = "the run that started this run_many has stopped, so it stopped"
            raise _core.StoppedError(msg)
        return self._results  # ty: ignore[invalid-return-type]  # every set's result is in


def close_all() -> None:
    """Stops every pool's threads: idle ones at once, busy ones after their task.

    A pool inherited across a fork is left alone by `close` itself.
    """
    for threads in list(_LIVE):
        threads.close()


atexit.register(close_all)
