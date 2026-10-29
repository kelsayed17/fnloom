"""Drawings of a built graph: which nodes, how each is marked, and their text.

A `Drawing` is plain data: its nodes in topological order and its edges, each node
with the text its label shows. It writes DOT and Mermaid itself, so drawing needs
nothing installed; only an image needs Graphviz's `dot` (Q90, Q94).
"""

import inspect

# `os` is imported for real: `render`'s signature annotates with `os.PathLike`,
# and `inspect.signature` resolves that at run time.
import os  # noqa: TC003  # resolved at run time, see above
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, Protocol, override

from fnloom._types import Unresolved
from fnloom._view import topological

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from fnloom._config import Condition
    from fnloom._view import NodeViews

type Mark = Literal["output", "override", "path", "context"]
type Orient = Literal["LR", "TB", "RL", "BT"]
type Kind = Literal["function", "input"]

_ORIENTS = ("LR", "TB", "RL", "BT")

# The formats `render` writes as `to_dot()`'s text: Graphviz's own suffixes.
_DOT_SUFFIXES = frozenset({"dot", "gv"})


class _Printer(Protocol):
    """The part of IPython's pretty printer `_repr_pretty_` uses."""

    def text(self, obj: str) -> None: ...


# A label's type line longer than this is cut, as Hamilton cuts at 80 (Q91).
_TYPE_WIDTH = 80

# Every function node's look; inputs and marks change parts of it (`_STYLES`).
_NODE_DEFAULTS = (
    '  node [shape=box style="rounded,filled" fillcolor="#b4d8e4" fontname=Helvetica]'
)

# The tag whose value replaces a node's name in its label, as Hamilton's does.
_DISPLAY_NAME = "display_name"


@dataclass(frozen=True, slots=True)
class DrawnNode:
    """One node of a `Drawing`.

    `name` is the node's name, its id in the text. `title` is what its label
    shows for it: the `display_name` tag when the node has one, else the name.
    `type` is its annotation as text, cut to 80 characters; `condition` the
    `@config.when*` conditions it was chosen by, or `None`. `marks` say what the
    drawing shows it as: a requested `output`, an `override`, on the `path`, or
    `context` (drawn only because a drawn node reads it).
    """

    name: str
    kind: Kind
    type: str
    title: str
    condition: str | None = None
    marks: frozenset[Mark] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class Drawing:
    """A drawing of some of a graph's nodes: plain data that writes DOT text.

    `nodes` are in topological order, ties broken by name; `edges` are
    `(dependency, node)` pairs in the order of their nodes, then of their
    dependencies' names. Neither depends on `PYTHONHASHSEED` (Q90). `str()` of a
    drawing is its DOT text.
    """

    nodes: tuple[DrawnNode, ...]
    edges: tuple[tuple[str, str], ...]

    def to_dot(self, *, orient: Orient = "LR", legend: bool = True) -> str:
        """The drawing as Graphviz DOT text.

        `orient` is the direction edges point: `"LR"` (left to right), `"TB"`,
        `"RL"` or `"BT"`; any other value is a `ValueError`. `legend` adds a box
        naming each kind and mark the drawing uses.
        """
        _checked_orient(orient)
        lines = [
            "digraph {",
            f"  graph [rankdir={orient}]",
            _NODE_DEFAULTS,
        ]
        lines.extend(f"  {_quoted(n.name)} [{_dot_attributes(n)}]" for n in self.nodes)
        on_path = {n.name for n in self.nodes if "path" in n.marks}
        for dependency, node in self.edges:
            styled = " [color=red]" if {dependency, node} <= on_path else ""
            lines.append(f"  {_quoted(dependency)} -> {_quoted(node)}{styled}")
        if legend and self.nodes:
            lines.extend(_dot_legend(self.nodes))
        lines.append("}")
        return "\n".join(lines) + "\n"

    def to_mermaid(self, *, orient: Orient = "LR") -> str:
        """The drawing as a Mermaid flowchart, styled as `to_dot` styles it.

        Nodes are `n0`, `n1` and so on, in the drawing's order, each labelled
        with its title, type and condition. `orient` is checked as `to_dot`
        checks it.
        """
        _checked_orient(orient)
        ids = {n.name: f"n{i}" for i, n in enumerate(self.nodes)}
        lines = [f"flowchart {orient}"]
        lines.extend(f'  {ids[n.name]}["{_mermaid_label(n)}"]' for n in self.nodes)
        on_path = {n.name for n in self.nodes if "path" in n.marks}
        red: list[str] = []
        for i, (dependency, node) in enumerate(self.edges):
            lines.append(f"  {ids[dependency]} --> {ids[node]}")
            if {dependency, node} <= on_path:
                red.append(str(i))
        if red:
            lines.append(f"  linkStyle {','.join(red)} stroke:red")
        lines.extend(_mermaid_classes(self.nodes, ids))
        return "\n".join(lines) + "\n"

    def render(
        self,
        path: str | os.PathLike[str],
        *,
        format: str | None = None,  # noqa: A002  # Graphviz's own word for it
        orient: Orient = "LR",
        legend: bool = True,
    ) -> Path:
        """Writes the drawing to `path` and returns it (Q94).

        `format` defaults to the path's suffix, in lower case: `"svg"` for
        `graph.svg`. `"dot"` and `"gv"` write `to_dot()`'s text, never Graphviz's
        laid-out DOT, and `"mmd"` writes `to_mermaid()`'s (with no legend),
        needing nothing.
        Any other format is made by Graphviz's `dot` executable, which must be on
        `PATH`: `FileNotFoundError` when it isn't, saying how to get it, and
        `RuntimeError` with `dot`'s own message when it fails, as for a format it
        doesn't know. `ValueError` for no format and a path with no suffix, and
        `FileNotFoundError` naming the folder when `path`'s folder doesn't exist.
        """
        target = Path(path)
        given = format if format is not None else target.suffix.lower()
        chosen = given.removeprefix(".")
        if not chosen:
            msg = f"give a format, or a path with a suffix such as .svg: {target}"
            raise ValueError(msg)
        if not target.parent.is_dir():
            msg = f"no folder to write {target.name} in: {target.parent}"
            raise FileNotFoundError(msg)
        if chosen == "mmd":
            target.write_text(self.to_mermaid(orient=orient), encoding="utf-8")
            return target
        text = self.to_dot(orient=orient, legend=legend)
        if chosen in _DOT_SUFFIXES:
            target.write_text(text, encoding="utf-8")
            return target
        target.write_bytes(_graphviz(text, chosen))
        return target

    def _repr_pretty_(self, printer: _Printer, cycle: bool) -> None:  # noqa: FBT001  # IPython's signature
        """The DOT text, which IPython shows when there is no SVG."""
        del cycle  # a drawing holds no drawing, so never a cycle
        printer.text(self.to_dot())

    def _repr_svg_(self) -> str | None:
        """An SVG for Jupyter, or `None` without Graphviz, so it shows the text."""
        if shutil.which("dot") is None:
            return None
        try:
            return _graphviz(self.to_dot(), "svg").decode()
        except RuntimeError:
            return None

    @override
    def __str__(self) -> str:
        """The DOT text, as `to_dot()` writes it."""
        return self.to_dot()


def drawing(
    views: NodeViews,
    names: Iterable[str],
    marks: Mapping[str, Iterable[Mark]] = MappingProxyType({}),
) -> Drawing:
    """The drawing of `names`, each marked as `marks` says.

    Only edges between drawn nodes are drawn.
    """
    drawn = set(names)
    order = topological(drawn, views)
    nodes: list[DrawnNode] = []
    edges: list[tuple[str, str]] = []
    for name in order:
        view = views[name]
        tags = view.tags
        nodes.append(
            DrawnNode(
                name=name,
                kind="input" if view.is_input else "function",
                type=type_text(view.type),
                title=tags.get(_DISPLAY_NAME) or name,
                condition=condition_text(view.config) if view.config else None,
                marks=frozenset(marks.get(name, ())),
            )
        )
        edges.extend((dep, name) for dep in view.dependencies if dep in drawn)
    return Drawing(tuple(nodes), tuple(edges))


def with_context(views: NodeViews, names: Sequence[str]) -> dict[str, list[Mark]]:
    """Each node outside `names` that a node in `names` reads, marked `context`."""
    inside = set(names)
    return {
        dep: ["context"]
        for name in names
        for dep in views[name].dependencies
        if dep not in inside
    }


def type_text(annotation: object) -> str:
    """`annotation` as a label shows it: whole, cut to 80 characters (Q91)."""
    if isinstance(annotation, Unresolved):
        text = annotation.name
    else:
        text = inspect.formatannotation(annotation)
    if len(text) > _TYPE_WIDTH:
        return text[: _TYPE_WIDTH - 1] + "…"
    return text


_OPERATORS = {"when": "==", "when_not": "!=", "when_in": "in", "when_not_in": "not in"}


def condition_text(conditions: Sequence[Condition]) -> str:
    """The `@config.when*` conditions as text: `mode == 'fast'`, joined by `, `."""
    parts: list[str] = []
    for c in conditions:
        operator = _OPERATORS[c.kind]
        if c.kind in {"when", "when_not"}:
            parts.append(f"{c.key} {operator} {c.values[0]!r}")
        else:
            listed = ", ".join(map(repr, c.values))
            parts.append(f"{c.key} {operator} [{listed}]")
    return ", ".join(parts)


def _checked_orient(orient: object) -> None:
    if orient not in _ORIENTS:
        msg = f"orient must be one of {', '.join(_ORIENTS)}, got {orient!r}"
        raise ValueError(msg)


def _escaped(text: str) -> str:
    """`text` for inside a DOT quoted string: backslashes, quotes, line breaks."""
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _quoted(text: str) -> str:
    """`text` as a DOT quoted string."""
    return f'"{_escaped(text)}"'


def _label(node: DrawnNode) -> str:
    """The label's lines: the title, the type, and the condition if any."""
    lines = [node.title, node.type]
    if node.condition is not None:
        lines.append(node.condition)
    # Each line escaped alone, so the `\n` between them stays a line break.
    return "\\n".join(_escaped(line) for line in lines)


# Each kind's and mark's DOT attributes, applied in this order, later ones winning.
_STYLES: dict[str, dict[str, str]] = {
    "input": {"style": '"dashed,filled"', "fillcolor": '"#ffffff"'},
    "output": {"fillcolor": '"#FFC857"'},
    "override": {"style": '"filled,diagonals"'},
    "path": {"color": "red"},
    "context": {
        "color": '"#999999"',
        "fontcolor": '"#777777"',
        "fillcolor": '"#eeeeee"',
    },
}


def _styles(kind: Kind, marks: Iterable[str]) -> dict[str, str]:
    styles: dict[str, str] = {}
    for key in (kind, *sorted(marks, key=list(_STYLES).index)):
        styles.update(_STYLES.get(key, {}))
    return styles


def _dot_attributes(node: DrawnNode) -> str:
    attributes = {"label": f'"{_label(node)}"', **_styles(node.kind, node.marks)}
    return " ".join(f"{key}={value}" for key, value in attributes.items())


def _dot_legend(nodes: Sequence[DrawnNode]) -> list[str]:
    """A legend box naming each kind and mark drawn, in a fixed order."""
    used = {n.kind for n in nodes} | {m for n in nodes for m in n.marks}
    # A node may be named like a legend entry: such an entry's id gets
    # underscores until no drawn node has it.
    taken = {n.name for n in nodes}
    lines = ["  subgraph cluster_legend {", '    label="Legend"']
    for key in ("function", "input", "output", "override", "path", "context"):
        if key in used:
            kind: Kind = "input" if key == "input" else "function"
            marks = () if key in {"function", "input"} else (key,)
            attributes = {"label": _quoted(key), **_styles(kind, marks)}
            listed = " ".join(f"{k}={v}" for k, v in attributes.items())
            entry = f"legend: {key}"
            while entry in taken:
                entry = f"_{entry}"
            lines.append(f"    {_quoted(entry)} [{listed}]")
    lines.append("  }")
    return lines


# Mermaid's entity codes for the characters a quoted label can't hold as they are.
_MERMAID_ESCAPES = {
    "#": "#35;",
    '"': "#quot;",
    "<": "#lt;",
    ">": "#gt;",
    "&": "#38;",
    "`": "#96;",
}


def _mermaid_label(node: DrawnNode) -> str:
    lines = [node.title, node.type]
    if node.condition is not None:
        lines.append(node.condition)
    escaped = ("".join(_MERMAID_ESCAPES.get(c, c) for c in line) for line in lines)
    return "<br>".join(line.replace("\n", " ") for line in escaped)


# Each kind's and mark's Mermaid style, as `_STYLES` gives DOT's.
_MERMAID_STYLES = {
    "function": "fill:#b4d8e4,stroke:#333333",
    "input": "fill:#ffffff,stroke:#333333,stroke-dasharray:5 5",
    "output": "fill:#FFC857",
    "override": "stroke-dasharray:2 2,stroke-width:3px",
    "path": "stroke:red,stroke-width:2px",
    "context": "fill:#eeeeee,stroke:#999999,color:#777777",
}


def _mermaid_classes(nodes: Sequence[DrawnNode], ids: Mapping[str, str]) -> list[str]:
    """A `classDef` per kind and mark drawn, and the nodes of each, in a fixed order."""
    members: dict[str, list[str]] = {}
    for n in nodes:
        for key in (n.kind, *sorted(n.marks, key=list(_STYLES).index)):
            members.setdefault(key, []).append(ids[n.name])
    lines: list[str] = []
    for key, style in _MERMAID_STYLES.items():
        if key in members:
            lines.append(f"  classDef {key} {style}")
            lines.append(f"  class {','.join(members[key])} {key}")
    return lines


_NO_GRAPHVIZ = (
    "rendering needs Graphviz's 'dot' on PATH; install Graphviz "
    "(https://graphviz.org/download/), or write the DOT text with "
    "drawing.to_dot() or render(path) to a .dot path"
)


def _graphviz(text: str, format: str) -> bytes:  # noqa: A002  # Graphviz's word
    """`text` laid out by Graphviz's `dot` in `format`."""
    dot = shutil.which("dot")
    if dot is None:
        raise FileNotFoundError(_NO_GRAPHVIZ)
    done = subprocess.run(  # noqa: S603  # `dot` from PATH, its arguments ours
        [dot, f"-T{format}"],
        input=text.encode(),
        capture_output=True,
        check=False,
    )
    if done.returncode != 0:
        msg = f"dot failed: {done.stderr.decode(errors='replace').strip()}"
        raise RuntimeError(msg)
    return done.stdout
