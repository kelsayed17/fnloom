"""`Parallelizable` and `Collect` as a checker reads them (Q127).

`ty check` checks this module: a generator is an expand's body, and what a collect
receives is a `list`, so `len()` and indexing type-check.
"""

# The build reads the annotations at run time.
from fnloom import Collect, Parallelizable  # noqa: TC001


def tickers(universe: str) -> Parallelizable[str]:
    """One item per comma-separated name."""
    yield from universe.split(",")


def price(tickers: str) -> float:
    """A price per ticker: its length."""
    return float(len(tickers))


def total(price: Collect[float]) -> float:
    """The first price plus how many there are."""
    return price[0] + len(price)
