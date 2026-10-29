"""Drawings of a built graph: which nodes, how each is marked, and their text.

A `Drawing` is plain data: its nodes in topological order and its edges, each node
with the text its label shows. It writes DOT itself, so drawing needs nothing
installed (Q90, Q94).
"""

import inspect
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, override

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

# A label's type line longer than this is cut, as Hamilton cuts at 80 (Q91).
_TYPE_WIDTH = 80

# Every function node's look; inputs and marks change parts of it (`_STYLES`).
_NODE_DEFAULTS = (
    '  node [shape=box style="rounded,filled" fillcolor="#b4d8e4" fontname=Helvetica]'
)

# The tag whose value replaces a node's name in its label, as Hamilton's does.
DISPLAY_NAME = "display_name"


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
                title=tags.get(DISPLAY_NAME) or name,
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


def _quoted(text: str) -> str:
    """`text` as a DOT quoted string: backslashes and quotes escaped."""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def _label(node: DrawnNode) -> str:
    """The label's lines: the title, the type, and the condition if any."""
    lines = [node.title, node.type]
    if node.condition is not None:
        lines.append(node.condition)
    # Each line escaped alone, so the `\n` between them stays a line break.
    return "\\n".join(_quoted(line)[1:-1] for line in lines)


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
    lines = ["  subgraph cluster_legend {", '    label="Legend"']
    for key in ("function", "input", "output", "override", "path", "context"):
        if key in used:
            kind: Kind = "input" if key == "input" else "function"
            marks = () if key in {"function", "input"} else (key,)
            attributes = {"label": _quoted(key), **_styles(kind, marks)}
            listed = " ".join(f"{k}={v}" for k, v in attributes.items())
            lines.append(f"    {_quoted('legend: ' + key)} [{listed}]")
    lines.append("  }")
    return lines
