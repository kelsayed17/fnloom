"""The hook the benchmarks time: it overrides all four methods and counts the events.

Counting is about the least a hook can do with an event, so a run with it measures
what the hooks themselves cost a run: making each event and calling the method.
"""

from typing import override

import fnloom


class Counting(fnloom.Hook):
    """Counts every event, of all four kinds."""

    __slots__ = ("events",)

    def __init__(self) -> None:
        """Starts at no events."""
        self.events = 0

    @override
    def before_run(self, event: fnloom.RunStart) -> None:
        """Counts the event."""
        del event
        self.events += 1

    @override
    def before_node(self, event: fnloom.NodeStart) -> None:
        """Counts the event."""
        del event
        self.events += 1

    @override
    def after_node(self, event: fnloom.NodeEnd) -> None:
        """Counts the event."""
        del event
        self.events += 1

    @override
    def after_run(self, event: fnloom.RunEnd) -> None:
        """Counts the event."""
        del event
        self.events += 1
