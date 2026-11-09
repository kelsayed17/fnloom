"""The graph `export_golden` exports: no node in it may be absent (Q95, Q135).

Only nodes here: the helpers live in `export_golden`, so they are not scanned.
"""

from typing import TypedDict

from fnloom import tag


class Totals(TypedDict):
    """Net and tax."""

    net: float
    tax: float


@tag(stage="raw")
def sales(region: str, units: int) -> float:
    """Sales of a region."""
    return float(units) if region else 0.0


def totals(sales: float, rate: float = 0.2) -> Totals:
    """The totals of `sales`."""
    return {"net": sales, "tax": sales * rate}


def report(totals: Totals, region: str) -> str:
    """A line of the report."""
    return f"{region}: {totals['net']}"
