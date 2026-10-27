"""The `typed` module under `from __future__ import annotations`: string annotations."""

from __future__ import annotations

from collections.abc import Sequence


def counts(words: list[str]) -> dict[str, int]:
    """How often each word occurs."""
    return {w: words.count(w) for w in words}


def total(counts: dict[str, int], floor: int | None = None) -> int:
    """The number of words, at least `floor` when one is given."""
    n = sum(counts.values())
    return n if floor is None else max(n, floor)


def ratio(total: int, scale: float) -> float:
    """The total, scaled."""
    return total * scale


def pairs(words: list[str]) -> tuple[str, ...]:
    """The words, as a tuple."""
    return tuple(words)


def window(pairs: tuple[str, ...]) -> Sequence[str]:
    """The first two words."""
    return pairs[:2]


def report(ratio: float, window: Sequence[str]) -> Report:
    """A report, of a class this module defines further down."""
    return Report(ratio, tuple(window))


def history(report: Report) -> list["Report"]:
    """The report in a list, its element type a nested string forward reference."""
    return [report]


def log(history: list[Report]) -> None:
    """Returns nothing: Hamilton's type for it is `NoneType`."""


class Report:
    """What `report` returns, defined after it: a forward reference."""

    def __init__(self, ratio: float, window: tuple[str, ...]) -> None:
        """Keeps the two values."""
        self.ratio = ratio
        self.window = window
