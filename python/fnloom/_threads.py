"""Running one run's nodes in parallel, and limits: a driver's pool, caps and checks."""

import atexit
import os
import weakref
from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from fnloom import _core
from fnloom._decorators import decorations
from fnloom._extract import extractor

if TYPE_CHECKING:
    from fnloom._scan import NodeDef

__all__ = [
    "HELD",
    "Caps",
    "Workers",
    "caps_of",
    "checked_groups",
    "checked_max_workers",
    "close_all",
    "make_limits",
    "make_threads",
]

# The node and group slots the running calls of this context hold, as `(limits,
# node)` pairs from the binding: a run started inside one sees them as its
# ancestors', and is refused a slot whose every unit they hold.
HELD: Final[ContextVar[tuple[tuple[int, int], ...]]] = ContextVar(
    "fnloom_held", default=()
)

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


def close_all() -> None:
    """Stops every pool's threads: idle ones at once, busy ones after their task.

    A pool inherited across a fork is left alone by `close` itself.
    """
    for threads in list(_LIVE):
        threads.close()


atexit.register(close_all)
