"""Registers the shared helpers' fixtures for every test, and the threads watchdog."""

import faulthandler
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

pytest_plugins = ["helpers"]

# How long a test marked `threads` may run before the watchdog ends the session.
WATCHDOG_SECONDS = 300


@pytest.fixture(autouse=True)
def _watchdog(request: pytest.FixtureRequest) -> Iterator[None]:
    """Ends the session, printing every Python thread's stack, if a threads test hangs.

    The last resort: a thread test's own waits are bounded, so a deadlock usually
    fails that one test first. Rust frames are not shown.
    """
    if request.node.get_closest_marker("threads") is None:
        yield
        return
    faulthandler.dump_traceback_later(WATCHDOG_SECONDS, exit=True)
    try:
        yield
    finally:
        faulthandler.cancel_dump_traceback_later()
