"""Drawing styles and display options: `Style`, `Drawing.styled`, `show_*` (Q139)."""

import dataclasses
import os
import pickle
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import fnloom
from fnloom import Drawing, DrawnNode, Style
from helpers import make_module

import drawing_golden

if TYPE_CHECKING:
    from fnloom import NodeView

GOLDEN = Path(__file__).parent / "golden" / "styled_drawing.txt"

SALES = """
import collections
import dataclasses
from typing import NamedTuple, TypedDict
from fnloom import tag

class Totals(TypedDict):
    net: float
    tax: float

@dataclasses.dataclass
class Region:
    name: str
    rate: "float"

class Pair(NamedTuple):
    low: int
    high: int

Loose = collections.namedtuple("Loose", ["a", "b"])

@tag(owner=["ann", "bo"], stage="raw", display_name="Sales")
def sales(region: str, units: int) -> float:
    return 1.0

def totals(sales: float, rate: float = 0.2) -> Totals:
    return {"net": sales, "tax": sales * rate}

def regional(region: str) -> Region:
    return Region(region, 0.2)

def pair(units: int) -> Pair:
    return Pair(units, units)

def loose(units: int) -> Loose:
    return Loose(1, 2)

@tag(stage="final")
def report(totals: Totals, regional: Region) -> str:
    return ""
"""


def _driver() -> fnloom.Driver:
    return fnloom.build(make_module("styled_sales", SALES))


def _nodes(drawing: Drawing) -> dict[str, DrawnNode]:
    return {n.name: n for n in drawing.nodes}


def _dot_line(text: str, name: str) -> str:
    return next(line for line in text.splitlines() if line.startswith(f'  "{name}" ['))


# ---- Style ------------------------------------------------------------------------


def test_a_style_checks_its_colours() -> None:
    """`#rrggbb` or a CSS name in any case; anything else names the attribute."""
    Style(fill="#A0b1C2", stroke="Red", font_color="lightblue")
    with pytest.raises(ValueError, match="Style fill 'redd' is not a colour"):
        Style(fill="redd")
    with pytest.raises(ValueError, match="Style stroke '#12345' is not a colour"):
        Style(stroke="#12345")
    with pytest.raises(
        TypeError, match="Style font_color must be a str colour, got int"
    ):
        Style(font_color=0x123456)  # ty: ignore[invalid-argument-type]
    assert "rebeccapurple" not in Style.COLOURS  # a fixed list
    assert "red" in Style.COLOURS


def test_a_style_checks_every_other_field() -> None:
    """Width, dashes, shape and the passthroughs, each named when wrong."""
    with pytest.raises(
        ValueError, match="stroke_width must be a finite number above 0"
    ):
        Style(stroke_width=0)
    with pytest.raises(ValueError, match="stroke_width"):
        Style(stroke_width=float("inf"))
    with pytest.raises(TypeError, match="stroke_width must be a number, got bool"):
        Style(stroke_width=True)
    with pytest.raises(TypeError, match="dashed must be a bool, got int"):
        Style(dashed=1)  # ty: ignore[invalid-argument-type]
    with pytest.raises(
        ValueError, match="shape 'star' is not a shape: give one of box"
    ):
        Style(shape="star")  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="Style dot must be a mapping"):
        Style(dot=[("a", "b")])  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match=r"Style dot\['width'\] must be a str, got int"):
        Style(dot={"width": 2})  # ty: ignore[invalid-argument-type]
    for key in ("a b", "1x", "", "x-y", 'a"'):
        with pytest.raises(
            ValueError, match="Style dot key .* is not an attribute name"
        ):
            Style(dot={key: "v"})
    Style(mermaid={"font-weight": "bold"})  # a CSS name may hold `-`
    assert Style.SHAPES[0] == "box"


def test_style_values_are_escaped() -> None:
    """A `"` or a line break never breaks the text (Review Focus 1).

    DOT quotes and escapes a passthrough value; Mermaid's `style` statement can't
    quote, so a value holding one is refused, naming the attribute. A colour is
    checked, so it can hold neither.
    """
    tricky = 'a "b"\nc\\'
    drawing = (
        _driver()
        .draw()
        .styled(
            graph=Style(dot={"label": tricky}),
            node=Style(dot={"tooltip": tricky}),
            edge=Style(dot={"label": tricky}),
        )
    )
    dot = drawing.to_dot()
    escaped = '"a \\"b\\"\\nc\\\\"'
    assert f"label={escaped}" in dot.splitlines()[1]
    assert f"tooltip={escaped}" in _dot_line(dot, "sales")
    # One line per statement still: the unstyled text and the `edge` line.
    plain = _driver().draw().to_dot()
    assert len(dot.splitlines()) == len(plain.splitlines()) + 1
    for bad in ('a"b', "a\nb", "a,b", "a;b", "a{b", "a}b", "a%b", "a\x00"):
        with pytest.raises(ValueError, match=r"Style mermaid\['color'\] .* can't take"):
            Style(mermaid={"color": bad})
    with pytest.raises(ValueError, match=r"Style dot\['x'\] .* can't take"):
        Style(dot={"x": "a\rb"})
    for colour in ('red"', "red\n"):
        with pytest.raises(ValueError, match="is not a colour"):
            Style(fill=colour)
    dot_program = shutil.which("dot")
    if dot_program is not None:
        done = subprocess.run(  # noqa: S603  # Graphviz from PATH, our own text
            [dot_program, "-Tsvg"],
            input=dot,
            capture_output=True,
            text=True,
            check=False,
        )
        assert done.returncode == 0, done.stderr
        assert "<title>sales</title>" in done.stdout


def test_styles_merge_field_by_field() -> None:
    """A later style's fields win; passthroughs merge key by key."""
    drawing = (
        _driver()
        .draw()
        .styled(node=Style(fill="red", stroke="blue", dot={"a": "1", "b": "2"}))
        .styled(node=Style(fill="green", dot={"b": "3"}))
    )
    style = _nodes(drawing)["sales"].style
    assert style == Style(fill="green", stroke="blue", dot={"a": "1", "b": "3"})
    assert style is not None
    assert dict(style.dot or {}) == {"a": "1", "b": "3"}
    assert hash(style) == hash(
        Style(fill="green", stroke="blue", dot={"b": "3", "a": "1"})
    )


# ---- styled ----------------------------------------------------------------------


def test_the_style_function_sees_each_node_once_in_order() -> None:
    """`style(view, drawn)`: the full `NodeView` and its `DrawnNode`, in drawing order."""
    seen: list[tuple[str, str, object]] = []

    def style(view: NodeView, drawn: DrawnNode) -> Style | None:
        seen.append((view.name, drawn.name, view.tags.get("owner")))
        return None

    drawing = _driver().draw()
    assert drawing.styled(style=style) == drawing
    assert seen == [
        (n.name, n.name, ("ann", "bo") if n.name == "sales" else None)
        for n in drawing.nodes
    ]


def test_precedence_later_wins() -> None:
    """Kind and marks, then `node`, then `style(...)`; edges: `edge`, then the path's red."""
    drawing = (
        _driver()
        .draw_path_between("sales", "totals")
        .styled(
            node=Style(fill="red", stroke="blue"),
            edge=Style(stroke="green"),
            style=lambda view, _: Style(fill="gold") if view.name == "sales" else None,
        )
    )
    dot = drawing.to_dot()
    assert 'fillcolor="#ffd700" color="#0000ff"' in _dot_line(dot, "sales")
    # A context input: its own fill and font colour gone under `node`'s fill.
    assert _dot_line(dot, "rate").endswith(
        'fontcolor="#777777" fillcolor="#ff0000" color="#0000ff"]'
    )
    assert '  edge [color="#008000"]' in dot
    assert '  "sales" -> "totals" [color=red]' in dot
    mermaid = drawing.to_mermaid()
    assert "  linkStyle default stroke:#008000\n  linkStyle 3 stroke:red\n" in mermaid


def test_a_bad_style_names_the_node_and_the_attribute() -> None:
    """A bad colour from the style function, and a wrong return, name the node."""
    drawing = _driver().draw()
    with pytest.raises(
        ValueError, match="style for node 'rate': Style fill 'blu' is not a colour"
    ):
        drawing.styled(style=lambda view, _: Style(fill="blu"))
    with pytest.raises(
        TypeError, match="style for node 'rate': Style dashed must be a bool"
    ):
        drawing.styled(style=lambda view, _: Style(dashed="yes"))  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="returned dict for node 'rate'"):
        drawing.styled(style=lambda view, _: {"fill": "red"})  # ty: ignore[invalid-argument-type]
    with pytest.raises(KeyError, match="mine"):  # the function's own error, untouched
        drawing.styled(style=lambda view, _: {}["mine"])
    with pytest.raises(TypeError, match="node must be a fnloom.Style, got dict"):
        drawing.styled(node={"fill": "red"})  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="style must be callable, got Style"):
        drawing.styled(style=Style())  # ty: ignore[invalid-argument-type]
    hand_made = Drawing(
        (DrawnNode(name="n", kind="function", type="int", title="n"),), ()
    )
    with pytest.raises(ValueError, match="node 'n' has no view for the style function"):
        hand_made.styled(style=lambda view, _: None)


def test_a_graph_and_an_edge_style_take_their_own_fields() -> None:
    """A graph has a fill; an edge a stroke, width and dashes; both passthroughs."""
    drawing = _driver().draw()
    with pytest.raises(
        ValueError,
        match="a graph style can't set stroke: a graph takes fill, dot, mermaid",
    ):
        drawing.styled(graph=Style(stroke="red"))
    with pytest.raises(
        ValueError,
        match="an edge style can't set fill: an edge takes stroke, stroke_width",
    ):
        drawing.styled(edge=Style(fill="red"))
    with pytest.raises(ValueError, match="can't set shape"):
        drawing.styled(edge=Style(shape="box"))


def test_every_writer_draws_the_vocabulary() -> None:
    """Fill, stroke, font colour, width, dashes and shape, in DOT and Mermaid."""
    style = Style(
        fill="#112233",
        stroke="navy",
        font_color="white",
        stroke_width=1.5,
        dashed=False,
        shape="hexagon",
        dot={"z": "1", "a": "2"},
        mermaid={"opacity": "0.5"},
    )
    drawing = _driver().draw().styled(node=style)
    line = _dot_line(drawing.to_dot(), "region")
    assert line == (
        '  "region" [label="region\\nstr" style="filled" shape=hexagon '
        'fillcolor="#112233" color="#000080" fontcolor="#ffffff" penwidth=1.5 a="2" z="1"]'
    )
    mermaid = drawing.to_mermaid()
    assert '  n1{{"region<br>str"}}' in mermaid
    assert (
        "  style n1 fill:#112233,stroke:#000080,color:#ffffff,stroke-width:1.5px,"
        "stroke-dasharray:0,opacity:0.5"
    ) in mermaid


@pytest.mark.parametrize(
    ("shape", "dot", "mermaid"),
    [
        ("box", 'style="filled" shape=box', '["sales'),
        ("rounded", 'style="rounded,filled" shape=box', '("sales'),
        ("ellipse", 'style="filled" shape=ellipse', '(["sales'),
        ("circle", 'style="filled" shape=circle', '(("sales'),
        ("diamond", 'style="filled" shape=diamond', '{"sales'),
        ("cylinder", 'style="filled" shape=cylinder', '[("sales'),
        ("parallelogram", 'style="filled" shape=parallelogram', '[/"sales'),
    ],
)
def test_each_shape(shape: str, dot: str, mermaid: str) -> None:
    """Each shape in DOT's words and Mermaid's brackets."""
    drawing = _driver().draw_upstream_of("sales").styled(node=Style(shape=shape))  # ty: ignore[invalid-argument-type]
    assert dot in _dot_line(drawing.to_dot(), "sales")
    assert mermaid.replace("sales", "Sales") in drawing.to_mermaid()


def test_passthrough_keys_are_written_sorted() -> None:
    """The same text whatever order the passthrough was written in."""
    one = (
        _driver()
        .draw()
        .styled(node=Style(dot={"b": "1", "a": "2"}, mermaid={"y": "1", "x": "2"}))
    )
    two = (
        _driver()
        .draw()
        .styled(node=Style(dot={"a": "2", "b": "1"}, mermaid={"x": "2", "y": "1"}))
    )
    assert one.to_dot() == two.to_dot()
    assert one.to_mermaid() == two.to_mermaid()
    assert 'a="2" b="1"]' in _dot_line(one.to_dot(), "sales")


def test_a_passthrough_key_replaces_the_vocabularys() -> None:
    """`dot={"fillcolor": ...}` is written once, in the passthrough's place."""
    drawing = _driver().draw().styled(node=Style(fill="red", dot={"fillcolor": "blue"}))
    line = _dot_line(drawing.to_dot(), "sales")
    assert line.count("fillcolor=") == 1
    assert line.endswith('fillcolor="blue"]')


def test_a_graph_style_in_each_writer() -> None:
    """DOT's `bgcolor`, Mermaid's theme variables, SVG's background."""
    drawing = (
        _driver()
        .draw()
        .styled(graph=Style(fill="ivory", mermaid={"lineColor": "#333333"}))
    )
    assert drawing.to_dot().splitlines()[1] == '  graph [rankdir=LR bgcolor="#fffff0"]'
    assert drawing.to_mermaid().splitlines()[0] == (
        '%%{init: {"themeVariables": {"background": "#fffff0", "lineColor": "#333333"}}}%%'
    )
    assert '<rect width="100%" height="100%" fill="#fffff0"/>' in drawing.to_svg()


def test_an_unstyled_drawing_is_written_as_before() -> None:
    """No style, no change: an unstyled drawing's text has no new lines."""
    drawing = _driver().draw()
    assert drawing.styled() == drawing
    assert "edge [" not in drawing.to_dot()
    assert "style n" not in drawing.to_mermaid()
    assert "%%{init" not in drawing.to_mermaid()


def test_styled_drawings_are_plain_data() -> None:
    """Equal by value, pickled whole, the view left out of equality, `repr` and pickling."""
    drawing = (
        _driver()
        .draw(show_tags=("owner",))
        .styled(
            graph=Style(fill="red"), node=Style(dot={"a": "b"}), edge=Style(dashed=True)
        )
    )
    again = pickle.loads(pickle.dumps(drawing))  # noqa: S301  # our own bytes
    assert again == drawing
    assert again.to_dot() == drawing.to_dot()
    node = drawing.nodes[0]
    assert node.view is not None
    assert "view" not in repr(node)
    assert pickle.loads(pickle.dumps(node)).view is None  # noqa: S301  # our own bytes
    assert dataclasses.replace(node, view=None) == node


# ---- display options ---------------------------------------------------------------


def test_show_types() -> None:
    """`show_types=False` drops the type line from every label."""
    drawing = _driver().draw(show_types=False)
    assert '"sales" [label="Sales"]' in drawing.to_dot()
    index = [n.name for n in drawing.nodes].index("sales")
    assert f'  n{index}["Sales"]\n' in drawing.to_mermaid()
    assert "<tspan" in drawing.to_svg()
    assert ">float</tspan>" not in drawing.to_svg()
    assert _nodes(drawing)["sales"].type == "float"  # still known


def test_show_tags() -> None:
    """Values under the type, `name=value`, a tuple joined, in the order named."""
    drawing = _driver().draw(show_tags=("stage", "owner", "missing"))
    nodes = _nodes(drawing)
    assert nodes["sales"].tags == (("stage", "raw"), ("owner", "ann, bo"))
    assert nodes["report"].tags == (("stage", "final"),)
    assert nodes["totals"].tags == ()
    assert '[label="Sales\\nfloat\\nstage=raw\\nowner=ann, bo"]' in drawing.to_dot()


def test_show_schema() -> None:
    """A `TypedDict`, dataclass or `NamedTuple` return type's fields, in order."""
    nodes = _nodes(_driver().draw(show_schema=True))
    assert nodes["totals"].schema == (("net", "float"), ("tax", "float"))
    assert nodes["regional"].schema == (("name", "str"), ("rate", "float"))
    assert nodes["pair"].schema == (("low", "int"), ("high", "int"))
    assert nodes["loose"].schema == (("a", ""), ("b", ""))
    assert nodes["sales"].schema == ()
    assert nodes["region"].schema == ()  # an input's type is never expanded
    label = _dot_line(_driver().draw(show_schema=True).to_dot(), "loose")
    assert "\\na\\nb" in label
    assert _nodes(_driver().draw())["totals"].schema == ()


def test_hide_inputs() -> None:
    """Inputs and their edges left out, but not one requested as an output."""
    driver = _driver()
    drawing = driver.draw(hide_inputs=True)
    assert {n.kind for n in drawing.nodes} == {"function"}
    assert all(a in _nodes(drawing) and b in _nodes(drawing) for a, b in drawing.edges)
    run = driver.draw_execution(
        ["totals", "units"], inputs={"region": "eu", "units": 2}, hide_inputs=True
    )
    assert [n.name for n in run.nodes] == ["units", "sales", "totals"]


def test_every_draw_method_takes_the_display_options() -> None:
    """The same options on each `draw*` method of both drivers."""
    driver = _driver()
    asynchronous = (
        fnloom.Builder()
        .with_modules(make_module("styled_sales_async", SALES))
        .build_async()
    )
    options = {
        "show_types": False,
        "show_tags": ("stage",),
        "show_schema": True,
        "hide_inputs": True,
    }
    for each in (driver, asynchronous):
        drawings = [
            each.draw(**options),
            each.draw_upstream_of("report", **options),
            each.draw_downstream_of("sales", **options),
            each.draw_path_between("sales", "report", **options),
            each.draw_execution(
                ["report"], inputs={"region": "eu", "units": 1}, **options
            ),
        ]
        for drawing in drawings:
            assert not drawing.show_types
            assert all(n.kind == "function" for n in drawing.nodes)
            assert _nodes(drawing)["report"].tags == (("stage", "final"),)
    assert asynchronous.draw(**options).to_dot() == driver.draw(**options).to_dot()


def test_display_options_are_checked() -> None:
    """A wrong type names the option; a tag named twice is a `ValueError`."""
    driver = _driver()
    with pytest.raises(TypeError, match="show_tags must be a tuple of tag names"):
        driver.draw(show_tags="owner")
    with pytest.raises(TypeError, match="show_tags must hold str tag names, got int"):
        driver.draw_upstream_of("sales", show_tags=(1,))  # ty: ignore[invalid-argument-type]
    with pytest.raises(ValueError, match="show_tags names a tag twice: 'owner'"):
        driver.draw(show_tags=("owner", "owner"))
    with pytest.raises(TypeError, match="show_schema must be a bool, got str"):
        driver.draw_path_between("sales", "report", show_schema="yes")  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="hide_inputs must be a bool"):
        driver.draw_execution(["report"], validate=False, hide_inputs=None)  # ty: ignore[invalid-argument-type]
    assert driver.draw(show_tags=["owner"]) == driver.draw(show_tags=("owner",))


# ---- golden text -------------------------------------------------------------------


def test_the_golden_text() -> None:
    """DOT, Mermaid and SVG text of a drawing using every option and style."""
    assert drawing_golden.texts() == GOLDEN.read_text(encoding="utf-8")


def test_the_golden_text_does_not_depend_on_the_hash_seed() -> None:
    """The same text under three `PYTHONHASHSEED`s, and the pinned one."""
    tests = Path(__file__).parent
    texts = {
        subprocess.run(  # noqa: S603  # our own interpreter and script
            [sys.executable, str(tests / "drawing_golden.py")],
            env={**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": str(tests)},
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        for seed in ("0", "1", "2")
    }
    assert texts == {GOLDEN.read_text(encoding="utf-8")}
