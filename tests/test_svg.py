"""The builtin SVG engine: its layout's guarantees, its text, and `render` (Q146)."""

import itertools
import random
import shutil
import xml.etree.ElementTree as ET
from typing import TYPE_CHECKING

import pytest
from benches.generate import generated

import fnloom
import test_drawing
from fnloom import Drawing, DrawnNode, Style
from fnloom._svg import Layout, layout, text_width
from helpers import make_module

if TYPE_CHECKING:
    from pathlib import Path

    from fnloom._drawing import Orient

ORIENTS: tuple[Orient, ...] = ("LR", "TB", "RL", "BT")
_SVG = "{http://www.w3.org/2000/svg}"
_EPSILON = 1e-9


def _probe() -> Drawing:
    module = make_module("svg_probe", test_drawing.PROBE)
    driver = fnloom.Builder().with_modules(module).with_config({"mode": "fast"}).build()
    return driver.draw()


def _random_dag(n: int, seed: int) -> str:
    """`n` functions, each reading up to three of the 25 before it."""
    rng = random.Random(seed)  # noqa: S311  # a seeded shape, not cryptography
    lines = []
    for i in range(n):
        k = rng.choice([0, 1, 1, 2, 2, 3]) if i else 0
        reads = (
            sorted(set(rng.sample(range(max(0, i - 25), i), min(k, i)))) if i else []
        )
        params = ", ".join(f"n{d}: int" for d in reads) or "seed: int"
        lines.append(f"def n{i}({params}) -> int:\n    return 1\n")
    return "\n".join(lines)


def _rects(placed: Layout) -> list[tuple[str, float, float, float, float]]:
    boxes = [*placed.boxes, *([placed.legend] if placed.legend else [])]
    return [
        (
            b.name,
            b.x - b.width / 2,
            b.y - b.height / 2,
            b.x + b.width / 2,
            b.y + b.height / 2,
        )
        for b in boxes
    ]


def _check_layout(drawing: Drawing, orient: Orient) -> Layout:
    """The quality bar's first two parts: no overlaps, every edge along `orient`.

    And no edge runs through a node it doesn't join.
    """
    placed = layout(drawing, orient)
    rects = _rects(placed)
    for (a, *r), (b, *s) in itertools.combinations(rects, 2):
        apart = r[2] <= s[0] or s[2] <= r[0] or r[3] <= s[1] or s[3] <= r[1]
        assert apart, f"{a!r} and {b!r} overlap"
    assert all(r[2] >= 0 and r[4] <= placed.height for r in rects)
    assert all(r[1] >= 0 and r[3] <= placed.width for r in rects)
    axis, sign = {"LR": (0, 1), "RL": (0, -1), "TB": (1, 1), "BT": (1, -1)}[orient]
    named = {r[0]: r[1:] for r in rects}
    for (dependency, node), route in zip(drawing.edges, placed.edges, strict=True):
        for p, q in itertools.pairwise(route):
            assert (q[axis] - p[axis]) * sign >= 0, (dependency, node, route)
            for name, (x0, y0, x1, y1) in named.items():
                if name in {dependency, node}:
                    continue
                for step in range(11):
                    x = p[0] + (q[0] - p[0]) * step / 10
                    y = p[1] + (q[1] - p[1]) * step / 10
                    assert not (x0 < x < x1 and y0 < y < y1), (dependency, node, name)
        assert (route[-1][axis] - route[0][axis]) * sign > 0
    return placed


@pytest.mark.parametrize("orient", ORIENTS)
def test_the_layout_meets_the_bar_on_the_probe(orient: Orient) -> None:
    """No overlapping nodes or labels, every edge along `orient` (Q146)."""
    _check_layout(_probe(), orient)


@pytest.mark.parametrize(
    ("shape", "orient"), [("layered", "LR"), ("layered", "TB"), ("extract", "LR")]
)
def test_the_layout_meets_the_bar_on_200_generated_nodes(
    shape: str, orient: Orient
) -> None:
    """The benchmark generator's 200-node graphs (Q146)."""
    with generated(shape, 200) as module:  # ty: ignore[invalid-argument-type]
        _check_layout(fnloom.build(module).draw(), orient)


@pytest.mark.parametrize("orient", ["LR", "BT"])
def test_the_layout_meets_the_bar_on_a_random_graph(orient: Orient) -> None:
    """A 200-node graph of long and short edges, styled shapes among them."""
    drawing = fnloom.build(make_module("svg_random", _random_dag(200, seed=3))).draw()
    shapes = Style.SHAPES
    drawing = drawing.styled(
        style=lambda _, drawn: (
            Style(shape=shapes[int(drawn.name[1:]) % len(shapes)])
            if drawn.name.startswith("n")
            else None
        )
    )
    _check_layout(drawing, orient)


def test_a_layering_without_crossings_is_found() -> None:
    """Sources sit next to the one node they feed, so nothing crosses."""
    module = make_module(
        "svg_shorthands",
        """
        def high(base: int) -> int:
            return base
        def low(base: int) -> int:
            return base
        def east(base: int, east_rate: int) -> int:
            return base
        def west(base: int, west_rate: int) -> int:
            return base
        """,
    )
    placed = layout(fnloom.build(module).draw(), "LR")
    assert _crossings(placed) == 0


def _crossings(placed: Layout) -> int:
    def crosses(
        p: tuple[float, float],
        q: tuple[float, float],
        r: tuple[float, float],
        s: tuple[float, float],
    ) -> bool:
        def side(
            a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]
        ) -> int:
            v = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
            return (v > _EPSILON) - (v < -_EPSILON)

        return side(p, q, r) * side(p, q, s) < 0 and side(r, s, p) * side(r, s, q) < 0

    total = 0
    for one, two in itertools.combinations(placed.edges, 2):
        for p, q in itertools.pairwise(one):
            for r, s in itertools.pairwise(two):
                total += crosses(p, q, r, s)
    return total


def test_edges_are_as_short_as_a_layering_allows() -> None:
    """Every edge spans a layer at least; an input sits just before its reader.

    `x` is read only by `late`, three layers down: network simplex ranks it
    beside `late`, where a longest-path layering would leave it at the top.
    """
    module = make_module(
        "svg_ranks",
        """
        def a(seed: int) -> int:
            return seed
        def b(a: int) -> int:
            return a
        def c(b: int) -> int:
            return b
        def late(c: int, x: int) -> int:
            return c + x
        """,
    )
    drawing = fnloom.build(module).draw()
    placed = layout(drawing, "LR")
    x = {b.name: b.x for b in placed.boxes}
    assert x["seed"] < x["a"] < x["b"] < x["c"] < x["late"]
    assert x["c"] == x["x"]  # the same layer
    assert len(placed.edges[-1]) <= 4  # x -> late: through no other layer  # noqa: PLR2004  # out, two stubs and the tip


def test_the_svg_is_well_formed() -> None:
    """XML that parses, a group per node and edge, every name and label escaped."""
    module = make_module(
        "svg_tricky",
        r"""
        from fnloom import tag

        @tag(display_name='a "b" <c> & d\'s')
        def a(x: int) -> int:
            return x
        """,
    )
    driver = fnloom.Builder().with_modules(module, namespace="ns").build()
    text = driver.draw().to_svg()
    root = ET.fromstring(text)  # noqa: S314  # our own text
    groups = root.findall(f"{_SVG}g")
    assert [g.get("class") for g in groups] == ["edge", "node", "node", "legend"]
    titles = [g.find(f"{_SVG}title") for g in groups[:3]]
    assert [t.text for t in titles if t is not None] == ["x -> ns.a", "x", "ns.a"]
    labels = [
        "".join(s.text or "" for s in g.iter(f"{_SVG}tspan")) for g in groups[1:3]
    ]
    assert labels == ["xint", 'a "b" <c> & d\'s' + "int"]


def test_control_characters_are_replaced() -> None:
    """XML can't hold them, so each becomes U+FFFD."""
    node = DrawnNode(name="n\x01", kind="function", type="int", title="t\x02")
    root = ET.fromstring(Drawing((node,), ()).to_svg())  # noqa: S314  # our own text
    assert "t�" in "".join(s.text or "" for s in root.iter(f"{_SVG}tspan"))


def test_an_empty_drawing() -> None:
    """A blank page, no legend."""
    text = Drawing((), ()).to_svg()
    root = ET.fromstring(text)  # noqa: S314  # our own text
    assert root.findall(f"{_SVG}g") == []


def test_styles_and_marks_are_drawn() -> None:
    """Kinds, marks and a `Style` reach the SVG as `to_dot` draws them."""
    module = make_module("svg_probe_styled", test_drawing.PROBE)
    driver = fnloom.Builder().with_modules(module).with_config({"mode": "fast"}).build()
    drawing = driver.draw_path_between("a", "c").styled(
        edge=Style(stroke="blue", dashed=True, stroke_width=2),
        style=lambda view, _: (
            Style(fill="gold", shape="ellipse") if view.name == "b" else None
        ),
    )
    text = drawing.to_svg(legend=False)
    assert "<ellipse" in text
    assert 'fill="#ffd700"' in text
    assert 'stroke="#0000ff" stroke-width="2" stroke-dasharray="5,3"' in text  # x -> a
    assert (
        text.count('stroke="#ff0000" stroke-width="2" stroke-dasharray="5,3"') == 2  # noqa: PLR2004  # the path's two edges
    )  # path edges
    assert 'class="legend"' not in text
    assert 'class="legend"' in drawing.to_svg()


def test_text_widths_come_from_the_table() -> None:
    """Helvetica's widths for ASCII, a full em past it."""
    assert text_width("i") < text_width("m")
    assert text_width("…") == 14  # noqa: PLR2004  # an em at 14px


def test_render_with_the_builtin_engine_needs_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`engine="builtin"` writes `to_svg()`; only `engine` chooses (Q146)."""
    monkeypatch.setattr(shutil, "which", lambda _: None)
    drawing = _probe()
    out = drawing.render(tmp_path / "graph.svg", engine="builtin", orient="TB")
    assert out.read_text() == drawing.to_svg(orient="TB")
    assert (
        drawing.render(tmp_path / "g.dot", engine="builtin").read_text()
        == drawing.to_dot()
    )
    with pytest.raises(FileNotFoundError, match="needs Graphviz"):
        drawing.render(tmp_path / "graph2.svg")  # the default is still dot
    with pytest.raises(ValueError, match="the builtin engine writes svg only, not png"):
        drawing.render(tmp_path / "graph.png", engine="builtin")
    with pytest.raises(
        ValueError, match="engine must be one of dot, builtin, got 'cairo'"
    ):
        drawing.render(tmp_path / "graph.svg", engine="cairo")  # ty: ignore[invalid-argument-type]
    with pytest.raises(ValueError, match="orient must be one of"):
        drawing.to_svg(orient="up")  # ty: ignore[invalid-argument-type]


def test_the_svg_is_the_same_on_every_run() -> None:
    """Built twice, the same text: nothing depends on order of discovery."""
    assert _probe().to_svg() == _probe().to_svg()


def test_a_hand_made_drawing_in_any_order() -> None:
    """Nodes not in topological order are sorted; a cycle is a `ValueError`."""
    nodes = tuple(
        DrawnNode(name=name, kind="function", type="int", title=name)
        for name in ("c", "b", "a")
    )
    _check_layout(Drawing(nodes, (("a", "b"), ("b", "c"))), "LR")
    with pytest.raises(ValueError, match="edges form a cycle through 'a', 'b', 'c'"):
        Drawing(nodes, (("a", "b"), ("b", "c"), ("c", "a"))).to_svg()
    with pytest.raises(ValueError, match="joins a node the drawing doesn't have"):
        Drawing(nodes, (("a", "z"),)).to_svg()
