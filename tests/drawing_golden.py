"""A styled drawing whose DOT, Mermaid and SVG text the drawing tests pin (Q139, Q146).

Imported by `test_drawing_style.py` and run in a child process under several
`PYTHONHASHSEED`s, so it builds everything from this module alone.
"""

from typing import TYPE_CHECKING

import fnloom
from fnloom import Style
from helpers import make_module

if TYPE_CHECKING:
    from fnloom import Drawing, DrawnNode, NodeView

SOURCE = """
from typing import TypedDict
from fnloom import tag

class Totals(TypedDict):
    net: float
    tax: float

@tag(owner=["ann", "bo"], stage="raw")
def sales(region: str, units: int) -> float:
    return 1.0

def totals(sales: float, rate: float = 0.2) -> Totals:
    return {"net": sales, "tax": sales * rate}

@tag(stage="final")
def report(totals: dict, region: str) -> str:
    return ""
"""


def _owned(view: NodeView, drawn: DrawnNode) -> Style | None:
    """Gold ellipses for nodes with an owner; `report` dashed."""
    if "owner" in view.tags:
        return Style(
            fill="gold",
            shape="ellipse",
            dot={"peripheries": "2"},
            mermaid={"font-weight": "bold"},
        )
    if drawn.name == "report":
        return Style(dashed=True, stroke_width=2)
    return None


def golden() -> Drawing:
    """The styled drawing: every display option and every kind of style."""
    driver = fnloom.build(make_module("drawing_golden_sales", SOURCE))
    return driver.draw_path_between(
        "sales", "report", show_tags=("owner", "stage"), show_schema=True
    ).styled(
        graph=Style(fill="whitesmoke", dot={"splines": "ortho"}),
        node=Style(stroke="navy"),
        edge=Style(stroke="gray", dashed=True),
        style=_owned,
    )


def texts() -> str:
    """Every writer's text for the golden drawing, one after another."""
    drawing = golden()
    return "\n".join(
        [
            drawing.to_dot(),
            drawing.to_mermaid(),
            drawing.to_svg(orient="TB"),
        ]
    )


if __name__ == "__main__":
    import sys

    sys.stdout.write(texts())
