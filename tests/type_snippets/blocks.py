"""`Parallelizable` and `Collect` as a checker reads them (Q127).

`ty check` checks this module: a generator is an expand's body, and what a collect
receives is a `list`, so `len()` and indexing type-check.
"""

# The build reads the annotations at run time.
from fnloom import Collect, Parallelizable  # noqa: TC001


def picked(listing: str) -> Parallelizable[str]:
    """One item per comma-separated name."""
    yield from listing.split(",")


def details(picked: str) -> float:
    """A detail per item: its length."""
    return float(len(picked))


def total(details: Collect[float]) -> float:
    """The first detail plus how many there are."""
    return details[0] + len(details)
