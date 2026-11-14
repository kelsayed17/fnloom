"""Drawings of a built graph: which nodes, how each is marked, and their text.

A `Drawing` is plain data: its nodes in topological order and its edges, each node
with the text its label shows. It writes DOT, Mermaid and SVG itself, so drawing
needs nothing installed; only another image format needs Graphviz's `dot` (Q90,
Q94, Q146). `Drawing.styled` adds `Style`s that every writer draws (Q139).
"""

import annotationlib
import dataclasses
import inspect
import itertools
import json

# `os` is imported for real: `render`'s signature annotates with `os.PathLike`,
# and `inspect.signature` resolves that at run time.
import os  # noqa: TC003  # resolved at run time, see above
import shutil
import subprocess
import sys
import typing
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, Protocol, override

from fnloom._style import (
    Style,
    StyleTypeError,
    StyleValueError,
    check_scope,
    hex_colour,
    merged,
    number,
)
from fnloom._types import Unresolved
from fnloom._view import NodeView, topological

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence
    from collections.abc import Set as AbstractSet

    from fnloom._config import Condition
    from fnloom._view import NodeViews

type Mark = Literal["output", "override", "path", "context", "may be absent"]
type Orient = Literal["LR", "TB", "RL", "BT"]
type Kind = Literal["function", "input"]
type Engine = Literal["dot", "builtin"]

_ORIENTS = ("LR", "TB", "RL", "BT")
_ENGINES = ("dot", "builtin")

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
# The `style` tokens a node has when its kind and marks set none.
_DEFAULT_TOKENS = ("rounded", "filled")

# The tag whose value replaces a node's name in its label, as Hamilton's does.
_DISPLAY_NAME = "display_name"


@dataclass(frozen=True, slots=True)
class DrawnNode:
    """One node of a `Drawing`.

    `name` is the node's name, its id in the text. `title` is what its label
    shows for it: the `display_name` tag when the node has one, else the name.
    `type` is its annotation as text, cut to 80 characters; `condition` the
    `@config.when*` conditions it was chosen by, or `None`. `marks` say what the
    drawing shows it as: a requested `output`, an `override`, on the `path`,
    `context` (drawn only because a drawn node reads it), or `may be absent`
    (its value may be `fnloom.ABSENT`, Q135), drawn dashed.

    `tags` are the `(name, value)` pairs of the tags the drawing was asked to
    show (`show_tags`), in the order asked, a tuple value joined by `, `;
    `schema` the `(field, type)` pairs of a `TypedDict`, dataclass or
    `NamedTuple` return type (`show_schema`). `style` is the node's own `Style`,
    from `Drawing.styled`. `view` is the node's `NodeView`, for a style
    function; it is left out of equality, `repr` and pickling, and is `None`
    for a node made by hand or unpickled. `block` names the expand of the
    innermost block the node is in (`NodeView.block`), or `None`: DOT and
    Mermaid draw the node inside that block's cluster (Q131). It is left out
    of equality, like `view`.
    """

    name: str
    kind: Kind
    type: str
    title: str
    condition: str | None = None
    marks: frozenset[Mark] = field(default_factory=frozenset)
    tags: tuple[tuple[str, str], ...] = ()
    schema: tuple[tuple[str, str], ...] = ()
    style: Style | None = None
    view: NodeView | None = field(default=None, compare=False, repr=False)
    block: str | None = field(default=None, compare=False)

    def __copy__(self) -> DrawnNode:
        """A copy keeping `view`: only pickling leaves it out."""
        return dataclasses.replace(self)

    def __deepcopy__(self, memo: dict[int, object]) -> DrawnNode:
        """A deep copy sharing `view`, a read-only record of the graph."""
        del memo  # every other field is immutable
        return dataclasses.replace(self)

    @override
    def __reduce__(self) -> tuple[object, ...]:
        """Pickled without `view`, which holds the node's function."""
        return (
            DrawnNode,
            (
                self.name,
                self.kind,
                self.type,
                self.title,
                self.condition,
                self.marks,
                self.tags,
                self.schema,
                self.style,
                None,
                self.block,
            ),
        )


type StyleFunction = Callable[[NodeView, DrawnNode], Style | None]


@dataclass(frozen=True, slots=True)
class Drawing:
    """A drawing of some of a graph's nodes: plain data that writes DOT text.

    `nodes` are in topological order, ties broken by name; `edges` are
    `(dependency, node)` pairs in the order of their nodes, then of their
    dependencies' names. Neither depends on `PYTHONHASHSEED` (Q90). `str()` of a
    drawing is its DOT text. `graph_style` and `edge_style` are the `Style`s
    `styled` gave the whole graph and every edge; `show_types` says whether a
    label shows its node's type. `blocks` pairs each drawn block's expand with
    the expand of the block it is in, or `None`, sorted by expand: it says how
    the clusters nest (Q131). A node's `block` missing from it is a cluster at
    the top.
    """

    nodes: tuple[DrawnNode, ...]
    edges: tuple[tuple[str, str], ...]
    graph_style: Style | None = None
    edge_style: Style | None = None
    show_types: bool = True
    blocks: tuple[tuple[str, str | None], ...] = ()

    def styled(
        self,
        *,
        graph: Style | None = None,
        node: Style | None = None,
        edge: Style | None = None,
        style: StyleFunction | None = None,
    ) -> Drawing:
        """A new drawing with these styles added, for DOT, Mermaid and SVG alike (Q139).

        `graph` styles the whole drawing (`fill`, `dot`, `mermaid`); `edge`
        every edge (`stroke`, `stroke_width`, `dashed`, `dot`, `mermaid`);
        `node` every node. `style(view, drawn)` is called once per node, in
        drawing order, with its `NodeView` and `DrawnNode`, and returns a
        `Style` or `None`. Later wins: a node's kind and marks, then what an
        earlier `styled` gave it, then `node`, then `style(...)`; for an edge,
        `edge`, then a path's red. A bad `Style` made inside `style` is raised
        naming the node and the attribute. A `graph` or `edge` style setting
        what it can't is a `ValueError`; anything but a `Style`, and a `style`
        returning anything but a `Style` or `None`, a `TypeError`.
        """
        for name, given in (("graph", graph), ("node", node), ("edge", edge)):
            if given is not None and not isinstance(given, Style):
                msg = f"{name} must be a fnloom.Style, got {type(given).__name__}"
                raise TypeError(msg)
        if style is not None and not callable(style):
            msg = f"style must be callable, got {type(style).__name__}"
            raise TypeError(msg)
        check_scope("graph", graph)
        check_scope("edge", edge)
        nodes = tuple(
            dataclasses.replace(
                n,
                style=merged(
                    n.style, node, None if style is None else _called(style, n)
                ),
            )
            for n in self.nodes
        )
        return dataclasses.replace(
            self,
            nodes=nodes,
            graph_style=merged(self.graph_style, graph),
            edge_style=merged(self.edge_style, edge),
        )

    def to_dot(self, *, orient: Orient = "LR", legend: bool = True) -> str:
        """The drawing as Graphviz DOT text.

        `orient` is the direction edges point: `"LR"` (left to right), `"TB"`,
        `"RL"` or `"BT"`; any other value is a `ValueError`. `legend` adds a box
        naming each kind and mark the drawing uses. A drawing made by hand with
        two nodes of one name, or an edge to a node it doesn't have, is a
        `ValueError` here and in every writer.
        """
        _checked_orient(orient)
        checked_parts(self)
        lines = [
            "digraph {",
            f"  graph [{_listed(_dot_graph(orient, self.graph_style))}]",
            _NODE_DEFAULTS,
        ]
        if self.edge_style is not None:
            lines.append(f"  edge [{_listed(_dot_edge(self.edge_style))}]")
        for step, depth in clustered(self):
            indent = "  " * depth
            if isinstance(step, DrawnNode):
                attributes = _listed(_dot_attributes(step, self.show_types))
                lines.append(f"{indent}{_quoted(step.name)} [{attributes}]")
            elif step is None:
                lines.append(f"{indent}}}")
            else:
                lines.append(f"{indent}subgraph {_quoted(f'cluster_{step}')} {{")
                lines.append(f"{indent}  label={_quoted(block_title(step))}")
        on_path = {n.name for n in self.nodes if "path" in n.marks}
        labels = edge_labels(self)
        for (dependency, node), label in zip(self.edges, labels, strict=True):
            attributes = {} if label is None else {"label": _quoted(label)}
            if {dependency, node} <= on_path:
                attributes["color"] = "red"
            styled = f" [{_listed(attributes)}]" if attributes else ""
            lines.append(f"  {_quoted(dependency)} -> {_quoted(node)}{styled}")
        if legend and self.nodes:
            lines.extend(_dot_legend(self.nodes, {b for b, _ in self.blocks}))
        lines.append("}")
        return "\n".join(lines) + "\n"

    def to_mermaid(self, *, orient: Orient = "LR") -> str:
        """The drawing as a Mermaid flowchart, styled as `to_dot` styles it.

        Nodes are `n0`, `n1` and so on, in the drawing's order, each labelled
        with its title, type and condition. `orient` is checked as `to_dot`
        checks it, and the nodes and edges as `to_dot` checks them.
        """
        _checked_orient(orient)
        checked_parts(self)
        ids = {n.name: f"n{i}" for i, n in enumerate(self.nodes)}
        lines = _mermaid_init(self.graph_style)
        lines.append(f"flowchart {orient}")
        lines.extend(_mermaid_nodes(self, ids))
        on_path = {n.name for n in self.nodes if "path" in n.marks}
        red: list[str] = []
        labels = edge_labels(self)
        for i, (dependency, node) in enumerate(self.edges):
            arrow = "-->" if labels[i] is None else f"-- {labels[i]} -->"
            lines.append(f"  {ids[dependency]} {arrow} {ids[node]}")
            if {dependency, node} <= on_path:
                red.append(str(i))
        if self.edge_style is not None and self.edges:
            edge = _mermaid_style(self.edge_style)
            if edge:
                lines.append(f"  linkStyle default {edge}")
        if red:
            # A numbered `linkStyle` replaces the default one, so the edge
            # style is written again, its stroke red.
            own = (
                ""
                if self.edge_style is None
                else _mermaid_style(dataclasses.replace(self.edge_style, stroke=None))
            )
            path = f"{own},stroke:red" if own else "stroke:red"
            lines.append(f"  linkStyle {','.join(red)} {path}")
        lines.extend(_mermaid_classes(self.nodes, ids))
        for n in self.nodes:
            own = "" if n.style is None else _mermaid_style(n.style)
            if own:
                lines.append(f"  style {ids[n.name]} {own}")
        return "\n".join(lines) + "\n"

    def to_svg(self, *, orient: Orient = "LR", legend: bool = True) -> str:
        """The drawing as an SVG image, laid out in pure Python (Q146).

        Nothing needs installing. Each node sits one layer past every node it
        reads, nodes are ordered within a layer to cut crossings, and each is
        sized from its text, so no two overlap and every edge points along
        `orient`. Styles are drawn as `to_dot` draws them; the `dot` and
        `mermaid` passthroughs are not read. The text is the same on every run.
        `orient` is checked as `to_dot` checks it.
        """
        _checked_orient(orient)
        from fnloom._svg import svg  # noqa: PLC0415  # only an SVG needs the layout

        return svg(self, orient, legend=legend)

    def render(
        self,
        path: str | os.PathLike[str],
        *,
        format: str | None = None,  # noqa: A002  # Graphviz's own word for it
        orient: Orient = "LR",
        legend: bool = True,
        engine: Engine = "dot",
    ) -> Path:
        """Writes the drawing to `path` and returns it (Q94, Q146).

        `format`, or else the path's suffix, is read in lower case: `"svg"` for
        `graph.svg`. `"dot"` and `"gv"` write `to_dot()`'s text, never Graphviz's
        laid-out DOT, and `"mmd"` writes `to_mermaid()`'s (with no legend),
        needing nothing.
        Any other format is made by `engine`. `"dot"`, the default, is Graphviz's
        `dot` executable, which must be on `PATH`: `FileNotFoundError` when it
        isn't, giving the command that installs it, and `RuntimeError` with
        `dot`'s own message when it fails, as for a format it doesn't know.
        `"builtin"` is `to_svg()`, which needs nothing and writes `svg` only. A
        file never changes engine with the machine: only `engine` chooses.
        `ValueError` for an unknown engine, a format the builtin engine doesn't
        write, and no format with a path with no suffix; `FileNotFoundError`
        naming the folder when `path`'s folder doesn't exist.
        """
        if engine not in _ENGINES:
            msg = f"engine must be one of {', '.join(_ENGINES)}, got {engine!r}"
            raise ValueError(msg)
        target = Path(path)
        given = format if format is not None else target.suffix
        chosen = given.lower().removeprefix(".")
        if not chosen:
            msg = f"give a format, or a path with a suffix such as .svg: {target}"
            raise ValueError(msg)
        if not target.parent.is_dir():
            msg = f"no folder to write {target.name} in: {target.parent}"
            raise FileNotFoundError(msg)
        if chosen == "mmd":
            target.write_text(self.to_mermaid(orient=orient), encoding="utf-8")
            return target
        if chosen in _DOT_SUFFIXES:
            text = self.to_dot(orient=orient, legend=legend)
            target.write_text(text, encoding="utf-8")
            return target
        if engine == "builtin":
            if chosen != "svg":
                msg = (
                    f"the builtin engine writes svg only, not {chosen}: "
                    "use engine='dot' for other formats"
                )
                raise ValueError(msg)
            text = self.to_svg(orient=orient, legend=legend)
            target.write_text(text, encoding="utf-8")
            return target
        text = self.to_dot(orient=orient, legend=legend)
        target.write_bytes(_graphviz(text, chosen))
        return target

    def _repr_pretty_(self, printer: _Printer, cycle: bool) -> None:  # noqa: FBT001  # IPython's signature
        """The DOT text, which IPython shows when there is no SVG."""
        del cycle  # a drawing holds no drawing, so never a cycle
        printer.text(self.to_dot())

    def _repr_svg_(self) -> str | None:
        """An SVG for Jupyter: Graphviz's when `dot` runs, else `to_svg()`'s.

        A preview is not a file, so it may follow the machine (Q146). Past
        3000 nodes and edges, or 20000 nodes and edge slots once layered, the
        builtin preview is `None` and Jupyter shows the DOT text, so a preview
        never hangs; `to_svg()` still draws any size.
        """
        if shutil.which("dot") is not None:
            try:
                return _graphviz(self.to_dot(), "svg").decode(errors="replace")
            except RuntimeError, FileNotFoundError:
                pass
        from fnloom._svg import TooLargeError, svg  # noqa: PLC0415  # only an SVG needs it

        try:
            return svg(self, "LR", preview=True)
        except TooLargeError:
            return None

    @override
    def __str__(self) -> str:
        """The DOT text, as `to_dot()` writes it."""
        return self.to_dot()


def block_title(expand: str) -> str:
    """The title of the cluster of `expand`'s block."""
    return f"for each item of {expand}"


def clustered(drawing: Drawing) -> list[tuple[DrawnNode | str | None, int]]:
    """The drawing's nodes as DOT and Mermaid write them, each block a cluster.

    Each step is a node, a block's expand opening its cluster, or `None` closing
    the last one opened, with its depth. Nodes outside every block come first,
    in the drawing's order; then each block, by its expand's name, its nodes in
    the drawing's order, then the blocks nested in it. Walked on a stack, so
    any depth of nesting draws (Q131).
    """
    members: dict[str | None, list[DrawnNode]] = {}
    for n in drawing.nodes:
        members.setdefault(n.block, []).append(n)
    parents = _parents(drawing)
    children: dict[str | None, list[str]] = {}
    for block in sorted(parents, reverse=True):
        children.setdefault(parents[block], []).append(block)
    steps: list[tuple[DrawnNode | str | None, int]] = [
        (n, 1) for n in members.get(None, ())
    ]
    stack: list[tuple[str | None, int]] = [(b, 1) for b in children.get(None, ())]
    while stack:
        block, depth = stack.pop()
        if block is None:
            steps.append((None, depth))
            continue
        steps.append((block, depth))
        steps.extend((n, depth + 1) for n in members.get(block, ()))
        stack.append((None, depth))
        stack.extend((b, depth + 1) for b in children.get(block, ()))
    return steps


def _parents(drawing: Drawing) -> dict[str, str | None]:
    """Each block a drawn node is in, and each block that is in, with its parent.

    One `blocks` doesn't name is at the top. A block of `blocks` with no drawn
    node in it or in a block inside it, as after filtering nodes, is left out.
    """
    given = dict(drawing.blocks)
    parents: dict[str, str | None] = {}
    for n in drawing.nodes:
        block = n.block
        while block is not None and block not in parents:
            parents[block] = given.get(block)
            block = parents[block]
    return parents


def edge_labels(drawing: Drawing) -> list[str | None]:
    """Each edge's label, in the drawing's order: `expand`, `collect` or `None`.

    An edge from an expand into its block is `expand`; one from a block node to
    a node outside that block, which collects it, is `collect` (Q131).
    """
    parents = _parents(drawing)
    block_of = {n.name: n.block for n in drawing.nodes}
    labels: list[str | None] = []
    for dependency, node in drawing.edges:
        if dependency in parents:
            labels.append("expand")
            continue
        inner = block_of[dependency]
        outer = block_of[node]
        while outer is not None and outer != inner:
            outer = parents.get(outer)
        labels.append(None if inner is None or outer == inner else "collect")
    return labels


def checked_parts(drawing: Drawing) -> None:
    """`ValueError` for two nodes of one name, or an edge to a node not drawn."""
    seen: set[str] = set()
    for n in drawing.nodes:
        if n.name in seen:
            msg = f"the drawing has two nodes named {n.name!r}"
            raise ValueError(msg)
        seen.add(n.name)
    for a, b in drawing.edges:
        if a not in seen or b not in seen:
            msg = f"edge ({a!r}, {b!r}) joins a node the drawing doesn't have"
            raise ValueError(msg)


def _called(style: StyleFunction, node: DrawnNode) -> Style | None:
    """`style(view, node)`, its refusals and its result named by the node."""
    if node.view is None:
        msg = (
            f"node {node.name!r} has no view for the style function: "
            "draw it from a driver"
        )
        raise ValueError(msg)
    try:
        chosen = style(node.view, node)
    except StyleValueError as error:
        msg = f"style for node {node.name!r}: {error}"
        raise ValueError(msg) from error
    except StyleTypeError as error:
        msg = f"style for node {node.name!r}: {error}"
        raise TypeError(msg) from error
    if chosen is not None and not isinstance(chosen, Style):
        msg = (
            f"the style function returned {type(chosen).__name__} for node "
            f"{node.name!r}: return a fnloom.Style or None"
        )
        raise TypeError(msg)
    return chosen


@dataclass(frozen=True, slots=True)
class Display:
    """What a drawing's labels show, and whether it draws inputs."""

    show_types: bool = True
    show_tags: tuple[str, ...] = ()
    show_schema: bool = False
    hide_inputs: bool = False


def display(
    *,
    show_types: object = True,
    show_tags: object = (),
    show_schema: object = False,
    hide_inputs: object = False,
) -> Display:
    """The display options of a `draw*` method, checked, naming the one that's wrong."""
    for name, value in (
        ("show_types", show_types),
        ("show_schema", show_schema),
        ("hide_inputs", hide_inputs),
    ):
        if not isinstance(value, bool):
            msg = f"{name} must be a bool, got {type(value).__name__}"
            raise TypeError(msg)
    if isinstance(show_tags, str) or not isinstance(show_tags, tuple | list):
        msg = (
            "show_tags must be a tuple of tag names, such as ('owner',), "
            f"got {type(show_tags).__name__}"
        )
        raise TypeError(msg)
    tags = tuple(typing.cast("Sequence[object]", show_tags))
    if wrong := [type(t).__name__ for t in tags if not isinstance(t, str)]:
        msg = f"show_tags must hold str tag names, got {', '.join(wrong)}"
        raise TypeError(msg)
    names = typing.cast("tuple[str, ...]", tags)
    if repeated := sorted({t for t in names if names.count(t) > 1}):
        msg = f"show_tags names a tag twice: {', '.join(map(repr, repeated))}"
        raise ValueError(msg)
    return Display(
        show_types=bool(show_types),
        show_tags=names,
        show_schema=bool(show_schema),
        hide_inputs=bool(hide_inputs),
    )


def drawing(
    views: NodeViews,
    names: Iterable[str],
    marks: Mapping[str, Iterable[Mark]] = MappingProxyType({}),
    shown: Display = Display(),  # noqa: B008  # frozen, never mutated
    named: Iterable[str] = (),
) -> Drawing:
    """The drawing of `names`, each marked as `marks` says, shown as `shown` says.

    Only edges between drawn nodes are drawn, and a node that may be absent is
    marked `may be absent` too (Q135). `hide_inputs` leaves out every
    input the call didn't name: `named` are the names it did (the outputs
    requested, the nodes drawn from, a path's endpoints).
    """
    drawn = set(names)
    if shown.hide_inputs:
        kept = set(named)
        drawn = {n for n in drawn if not views[n].is_input or n in kept}
    order = topological(drawn, views)
    nodes: list[DrawnNode] = []
    edges: list[tuple[str, str]] = []
    for name in order:
        view = views[name]
        title = view.tags.get(_DISPLAY_NAME)
        if isinstance(title, tuple):
            title = ", ".join(title)
        nodes.append(
            DrawnNode(
                name=name,
                kind="input" if view.is_input else "function",
                type=type_text(view.type),
                title=title or name,
                condition=condition_text(view.config) if view.config else None,
                marks=frozenset(marks.get(name, ()))
                | (_MAY_BE_ABSENT if view.may_be_absent else _NO_MARKS),
                tags=_shown_tags(view, shown.show_tags),
                schema=schema_fields(view.type) if shown.show_schema else (),
                view=view,
                block=view.block,
            )
        )
        edges.extend((dep, name) for dep in view.dependencies if dep in drawn)
    return Drawing(
        tuple(nodes),
        tuple(edges),
        show_types=shown.show_types,
        blocks=_nesting(views, (n.block for n in nodes)),
    )


def _nesting(
    views: NodeViews, blocks: Iterable[str | None]
) -> tuple[tuple[str, str | None], ...]:
    """Each of `blocks`, and each block they are in, with the block it is in."""
    parents: dict[str, str | None] = {}
    for start in blocks:
        block = start
        while block is not None and block not in parents:
            parents[block] = views[block].block
            block = parents[block]
    return tuple(sorted(parents.items()))


def _shown_tags(view: NodeView, names: Sequence[str]) -> tuple[tuple[str, str], ...]:
    """The `(name, value)` of each tag in `names` the node has, in `names`' order."""
    shown: list[tuple[str, str]] = []
    for name in names:
        value = view.tags.get(name)
        if value is not None:
            shown.append((name, value if isinstance(value, str) else ", ".join(value)))
    return tuple(shown)


def schema_fields(annotation: object) -> tuple[tuple[str, str], ...]:
    """The `(field, type)` pairs of a `TypedDict`, dataclass or `NamedTuple` class.

    In declaration order, each type as a label shows it; `()` for any other type.
    A field with no annotation, as in `collections.namedtuple`, has type `""`.
    """
    if not isinstance(annotation, type):
        return ()
    if typing.is_typeddict(annotation):
        names = list(annotationlib.get_annotations(annotation))
    elif dataclasses.is_dataclass(annotation):
        names = [f.name for f in dataclasses.fields(annotation)]
    elif issubclass(annotation, tuple) and hasattr(annotation, "_fields"):
        names = list(typing.cast("tuple[str, ...]", annotation._fields))
    else:
        return ()
    hints = _hints(annotation)
    return tuple(
        (name, type_text(hints[name]) if name in hints else "") for name in names
    )


def _hints(cls: type) -> dict[str, object]:
    """`cls`'s field annotations, resolved where they can be, else as written."""
    try:
        hints: dict[str, object] = dict(typing.get_type_hints(cls, include_extras=True))
    except Exception:  # noqa: BLE001  # any unresolvable name: fall back to the text
        written: dict[str, object] = {}
        for base in reversed(cls.__mro__):  # inherited fields too, a base's first
            written.update(
                annotationlib.get_annotations(base, format=annotationlib.Format.STRING)
            )
        return written
    return hints


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
    elif isinstance(annotation, str):
        text = annotation
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
        if c.kind == "matches":
            parts.append(f"matches({c.key})")
            continue
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


# The control characters other than `\n`, which Graphviz copies into an SVG it
# writes, making it XML no reader takes, and the lone surrogates, which no file
# can hold as UTF-8: each becomes U+FFFD, as in `to_svg()`.
_DOT_CONTROLS = dict.fromkeys(
    (*range(10), *range(11, 32), 127, *range(0xD800, 0xE000)), "�"
)


def _escaped(text: str) -> str:
    """`text` for inside a DOT quoted string: backslashes, quotes, line breaks.

    Other control characters become U+FFFD.
    """
    text = text.translate(_DOT_CONTROLS)
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _quoted(text: str) -> str:
    """`text` as a DOT quoted string."""
    return f'"{_escaped(text)}"'


def label_lines(node: DrawnNode, *, show_types: bool = True) -> list[str]:
    """A label's lines: title, type, condition, shown tags, then schema fields."""
    lines = [node.title]
    if show_types:
        lines.append(node.type)
    if node.condition is not None:
        lines.append(node.condition)
    lines.extend(f"{name}={value}" for name, value in node.tags)
    lines.extend(f"{name}: {kind}" if kind else name for name, kind in node.schema)
    return lines


def _label(node: DrawnNode, show_types: bool) -> str:  # noqa: FBT001  # private
    # Each line escaped alone, so the `\n` between them stays a line break.
    lines = label_lines(node, show_types=show_types)
    return "\\n".join(_escaped(line) for line in lines)


# The mark of a node that may be absent, and no mark, made once.
_MAY_BE_ABSENT: frozenset[Mark] = frozenset({"may be absent"})
_NO_MARKS = frozenset[Mark]()

# Each kind's and mark's DOT attributes, applied in this order, later ones winning.
_STYLES: dict[str, dict[str, str]] = {
    "input": {"style": '"dashed,filled"', "fillcolor": '"#ffffff"'},
    "may be absent": {"style": '"dashed,rounded,filled"'},
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
    marks = sorted(marks, key=list(_STYLES).index)
    for key in (kind, *marks):
        styles.update(_STYLES.get(key, {}))
    if "may be absent" in marks and "dashed" not in styles["style"]:
        # A later mark set its own style, an override's diagonals: keep the dashes.
        styles["style"] = '"dashed,' + styles["style"].strip('"') + '"'
    return styles


def _listed(attributes: Mapping[str, str]) -> str:
    return " ".join(f"{key}={value}" for key, value in attributes.items())


def _with(base: dict[str, str], added: Mapping[str, str]) -> dict[str, str]:
    """`base` without the keys `added` sets, then `added` in its own order."""
    kept = {k: v for k, v in base.items() if k not in added}
    return {**kept, **added}


def _dot_style(style: Style, base: Mapping[str, str]) -> dict[str, str]:
    """A node's `Style` as DOT attributes, in vocabulary order, then passthrough."""
    out: dict[str, str] = {}
    if style.dashed is not None or style.shape is not None:
        given = base.get("style")
        tokens = list(_DEFAULT_TOKENS) if given is None else given.strip('"').split(",")
        if style.shape is not None:
            tokens = [t for t in tokens if t != "rounded"]
            if style.shape == "rounded":
                tokens.insert(0, "rounded")
        if style.dashed is True and "dashed" not in tokens:
            tokens.insert(0, "dashed")
        if style.dashed is False:
            tokens = [t for t in tokens if t != "dashed"]
        out["style"] = _quoted(",".join(tokens))
    if style.shape is not None:
        out["shape"] = "box" if style.shape in {"box", "rounded"} else style.shape
    out |= _dot_vocabulary(style, fill="fillcolor")
    return out


def _dot_vocabulary(style: Style, *, fill: str) -> dict[str, str]:
    """The colours, width and passthrough of `style`; `fill` names its attribute."""
    out: dict[str, str] = {}
    if style.fill is not None:
        out[fill] = _quoted(hex_colour(style.fill))
    if style.stroke is not None:
        out["color"] = _quoted(hex_colour(style.stroke))
    if style.font_color is not None:
        out["fontcolor"] = _quoted(hex_colour(style.font_color))
    if style.stroke_width is not None:
        out["penwidth"] = _quoted(number(style.stroke_width))
    for key, value in (style.dot or {}).items():
        out.pop(key, None)
        out[key] = _quoted(value)
    return out


def _dot_attributes(node: DrawnNode, show_types: bool = True) -> dict[str, str]:  # noqa: FBT001, FBT002  # private
    base = {"label": f'"{_label(node, show_types)}"', **_styles(node.kind, node.marks)}
    if node.style is None:
        return base
    return _with(base, _dot_style(node.style, base))


def _dot_graph(orient: str, style: Style | None) -> dict[str, str]:
    base = {"rankdir": orient}
    if style is None:
        return base
    return _with(base, _dot_vocabulary(style, fill="bgcolor"))


def _dot_edge(style: Style) -> dict[str, str]:
    out: dict[str, str] = {}
    if style.dashed is not None:
        out["style"] = "dashed" if style.dashed else "solid"
    return _with(out, _dot_vocabulary(style, fill="fillcolor"))


def _dot_legend(nodes: Sequence[DrawnNode], blocks: AbstractSet[str]) -> list[str]:
    """A legend box naming each kind and mark drawn, in a fixed order."""
    used = {n.kind for n in nodes} | {m for n in nodes for m in n.marks}
    # A node may be named like a legend entry: such an entry's id gets
    # underscores until no drawn node has it. An expand named `legend` would
    # share the legend's cluster: the legend's gets underscores too.
    taken = {n.name for n in nodes}
    legend = "legend"
    while legend in blocks:
        legend = f"_{legend}"
    lines = [f"  subgraph cluster_{legend} {{", '    label="Legend"']
    for key in LEGEND_KEYS:
        if key in used:
            kind: Kind = "input" if key == "input" else "function"
            marks = () if key in {"function", "input"} else (key,)
            attributes = {"label": _quoted(key), **_styles(kind, marks)}
            entry = f"legend: {key}"
            while entry in taken:
                entry = f"_{entry}"
            lines.append(f"    {_quoted(entry)} [{_listed(attributes)}]")
    lines.append("  }")
    return lines


# The legend's entries, in the order it lists them.
LEGEND_KEYS = (
    "function",
    "input",
    "may be absent",
    "output",
    "override",
    "path",
    "context",
)


# Mermaid's entity codes for the characters a quoted label can't hold as they are.
_MERMAID_ESCAPES = {
    "#": "#35;",
    '"': "#quot;",
    "<": "#lt;",
    ">": "#gt;",
    "&": "#38;",
    "`": "#96;",
}

# Each `Style` shape's Mermaid brackets around the quoted label.
_MERMAID_SHAPES = {
    "box": ("[", "]"),
    "rounded": ("(", ")"),
    "ellipse": ("([", "])"),
    "circle": ("((", "))"),
    "diamond": ("{", "}"),
    "hexagon": ("{{", "}}"),
    "cylinder": ("[(", ")]"),
    "parallelogram": ("[/", "/]"),
}


def _mermaid_text(line: str) -> str:
    """`line` for inside a quoted Mermaid label."""
    # A control character, a line break among them, can't stand in a label, and a
    # lone surrogate can't be written as UTF-8: it becomes U+FFFD, as in `to_svg()`.
    return "".join(
        " "
        if ord(c) < 32  # noqa: PLR2004  # the C0 controls
        else "�"
        if 0xD800 <= ord(c) < 0xE000  # noqa: PLR2004  # the surrogates
        else _MERMAID_ESCAPES.get(c, c)
        for c in line
    )


def _mermaid_label(node: DrawnNode, show_types: bool = True) -> str:  # noqa: FBT001, FBT002  # private
    lines = label_lines(node, show_types=show_types)
    return "<br>".join(_mermaid_text(line) for line in lines)


def _mermaid_nodes(drawing: Drawing, ids: Mapping[str, str]) -> list[str]:
    """The drawing's nodes as Mermaid lines, each block a `subgraph` (Q131)."""
    lines: list[str] = []
    clusters = itertools.count()
    for step, depth in clustered(drawing):
        indent = "  " * depth
        if isinstance(step, DrawnNode):
            shown = _mermaid_node(step, drawing.show_types)
            lines.append(f"{indent}{ids[step.name]}{shown}")
        elif step is None:
            lines.append(f"{indent}end")
        else:
            title = _mermaid_text(block_title(step))
            lines.append(f'{indent}subgraph b{next(clusters)} ["{title}"]')
    return lines


def _mermaid_node(node: DrawnNode, show_types: bool) -> str:  # noqa: FBT001  # private
    """The node's shape and label, as written after its id."""
    shape = (
        "box" if node.style is None or node.style.shape is None else node.style.shape
    )
    opening, closing = _MERMAID_SHAPES[shape]
    return f'{opening}"{_mermaid_label(node, show_types)}"{closing}'


def _mermaid_style(style: Style) -> str:
    """`style` as a Mermaid style list, in vocabulary order, then passthrough."""
    out: dict[str, str] = {}
    if style.fill is not None:
        out["fill"] = hex_colour(style.fill)
    if style.stroke is not None:
        out["stroke"] = hex_colour(style.stroke)
    if style.font_color is not None:
        out["color"] = hex_colour(style.font_color)
    if style.stroke_width is not None:
        out["stroke-width"] = f"{number(style.stroke_width)}px"
    if style.dashed is not None:
        out["stroke-dasharray"] = "5 5" if style.dashed else "0"
    for key, value in (style.mermaid or {}).items():
        out.pop(key, None)
        out[key] = value
    return ",".join(f"{key}:{value}" for key, value in out.items())


def _mermaid_init(style: Style | None) -> list[str]:
    """A graph style as Mermaid theme variables, in an `init` directive."""
    if style is None:
        return []
    variables: dict[str, str] = {}
    if style.fill is not None:
        variables["background"] = hex_colour(style.fill)
    for key, value in (style.mermaid or {}).items():
        variables.pop(key, None)
        variables[key] = value
    if not variables:
        return []
    return [f"%%{{init: {json.dumps({'themeVariables': variables})}}}%%"]


# Each kind's and mark's Mermaid style, as `_STYLES` gives DOT's.
_MERMAID_STYLES = {
    "function": "fill:#b4d8e4,stroke:#333333",
    "input": "fill:#ffffff,stroke:#333333,stroke-dasharray:5 5",
    "may be absent": "stroke-dasharray:5 5",
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
            # A class name is one word: `may be absent` is `may_be_absent`.
            name = key.replace(" ", "_")
            lines.append(f"  classDef {name} {style}")
            lines.append(f"  class {','.join(members[key])} {name}")
    return lines


# The command that installs Graphviz, by `sys.platform`'s prefix.
_INSTALL = (
    ("darwin", "brew install graphviz"),
    ("win32", "winget install graphviz"),
    (
        "linux",
        "apt-get install graphviz (Debian, Ubuntu; else your package manager)",
    ),
)


class GraphvizMissingError(FileNotFoundError):
    """Graphviz's `dot` isn't on `PATH`: the message says how to get it."""


def no_graphviz_message(format: str = "svg", *, cli: bool = False) -> str:  # noqa: A002  # Graphviz's word
    """Why `render` can't make `format`, and every way on from there.

    `cli` words the ways on as `fnloom draw`'s flags rather than Python calls.
    """
    command = next(
        (c for prefix, c in _INSTALL if sys.platform.startswith(prefix)),
        "see https://graphviz.org/download/",
    )
    if cli:
        text = "write a .mmd (Mermaid) or .dot file, or --format mermaid or dot"
        builtin = ", or --engine builtin for an SVG drawn by fnloom itself"
    else:
        text = "render a .mmd (Mermaid) or .dot path for the text"
        builtin = ", or render(path, engine='builtin') for an SVG drawn in pure Python"
    return (
        f"rendering {format} needs Graphviz's 'dot' on PATH. Install it with: "
        f"{command} (all platforms: https://graphviz.org/download/). Needing "
        f"nothing: {text}{builtin if format == 'svg' else ''}"
    )


def _graphviz(text: str, format: str) -> bytes:  # noqa: A002  # Graphviz's word
    """`text` laid out by Graphviz's `dot` in `format`."""
    dot = shutil.which("dot")
    if dot is None:
        raise GraphvizMissingError(no_graphviz_message(format))
    done = subprocess.run(  # noqa: S603  # `dot` from PATH, its arguments ours
        [dot, f"-T{format}"],
        # A lone surrogate can't be UTF-8: it is replaced, not raised.
        input=text.encode(errors="replace"),
        capture_output=True,
        check=False,
    )
    if done.returncode != 0:
        msg = f"dot failed: {done.stderr.decode(errors='replace').strip()}"
        raise RuntimeError(msg)
    return done.stdout
