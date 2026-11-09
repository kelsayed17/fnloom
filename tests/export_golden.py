"""A graph with no node that may be absent, whose export the tests pin (Q95).

`fnloom.export/1` stays byte for byte what it was before `fnloom.export/2` (Q135):
`text()` is compared with `golden/export_1.json`, written by the release before it.
"""

import json
from typing import TypedDict

import fnloom
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


def text() -> str:
    """The whole graph's export and one run's, as JSON, fnloom's version dropped."""
    import export_golden  # noqa: PLC0415  # this module, by its importable name

    built = fnloom.build(export_golden)
    documents = [
        built.export(),
        built.export(["report"], inputs={"region": "n", "units": 1}),
    ]
    for document in documents:
        document["fnloom"] = "<version>"
    return json.dumps(documents, indent=2) + "\n"
