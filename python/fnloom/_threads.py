"""Running one run's nodes in parallel: a driver's pool, and its setting's check."""

import atexit
import os
import weakref
from typing import Final

from fnloom import _core

__all__ = ["Workers", "checked_max_workers", "close_all", "make_limits", "make_threads"]

# Every pool alive, which interpreter exit closes.
_LIVE: Final[weakref.WeakSet[_core.Threads]] = weakref.WeakSet()
# Pools inherited across `os.fork()`: never dropped, so the child never touches them.
_INHERITED: Final[list[_core.Threads]] = []


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


def make_limits(max_workers: int) -> _core.Limits:
    """A driver's limits: at most `max_workers` of its tasks at once."""
    return _core.Limits(max_workers)


def make_threads(limits: _core.Limits, max_workers: int) -> _core.Threads:
    """A driver's pool of `max_workers - 1` threads, started at its first run."""
    threads = _core.Threads(limits, max_workers)
    _LIVE.add(threads)
    return threads


class Workers:
    """A driver's pool and limits, made again in a process forked from its maker's.

    Each driver object has its own: a copy, a rebuilt or an unpickled driver
    makes a new one.
    """

    __slots__ = ("_pid", "_threads", "max_workers")

    def __init__(self, max_workers: int) -> None:
        """A pool of `max_workers` workers, the run's calling thread among them."""
        self.max_workers = max_workers
        self._pid = os.getpid()
        self._threads = make_threads(make_limits(max_workers), max_workers)

    def threads(self) -> _core.Threads:
        """The pool, a new one when this process is a fork of the one that made it."""
        pid = os.getpid()
        if pid != self._pid:
            # Its locks may be held by threads the child doesn't have: never touched.
            _INHERITED.append(self._threads)
            self._threads = make_threads(
                make_limits(self.max_workers), self.max_workers
            )
            self._pid = pid
        return self._threads


def close_all() -> None:
    """Stops every pool's threads: idle ones at once, busy ones after their task.

    A pool inherited across a fork is left alone by `close` itself.
    """
    for threads in list(_LIVE):
        threads.close()


atexit.register(close_all)
