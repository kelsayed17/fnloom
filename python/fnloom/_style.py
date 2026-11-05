"""`Style`: how a drawing's graph, nodes and edges look, in DOT, Mermaid and SVG (Q139).

A `Style` is a small neutral vocabulary each writer maps to its own attributes,
plus a passthrough for DOT and one for Mermaid. Everything is checked when the
style is made, so a typo fails loudly rather than drawing black, and no value
can break the text it is written into.
"""

import math
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, fields
from typing import ClassVar, Literal, override

type Shape = Literal[
    "box",
    "rounded",
    "ellipse",
    "circle",
    "diamond",
    "hexagon",
    "cylinder",
    "parallelogram",
]

SHAPES: tuple[Shape, ...] = (
    "box",
    "rounded",
    "ellipse",
    "circle",
    "diamond",
    "hexagon",
    "cylinder",
    "parallelogram",
)

# The colour names a `Style` takes: CSS's, each written as its `#rrggbb` so
# Graphviz (whose X11 `green` and `gray` differ from CSS's), Mermaid and SVG
# draw the same colour.
COLOURS: Mapping[str, str] = {
    "aliceblue": "#f0f8ff",
    "aqua": "#00ffff",
    "beige": "#f5f5dc",
    "black": "#000000",
    "blue": "#0000ff",
    "brown": "#a52a2a",
    "chocolate": "#d2691e",
    "coral": "#ff7f50",
    "crimson": "#dc143c",
    "cyan": "#00ffff",
    "darkblue": "#00008b",
    "darkgray": "#a9a9a9",
    "darkgreen": "#006400",
    "darkgrey": "#a9a9a9",
    "darkorange": "#ff8c00",
    "darkred": "#8b0000",
    "dimgray": "#696969",
    "dimgrey": "#696969",
    "firebrick": "#b22222",
    "forestgreen": "#228b22",
    "fuchsia": "#ff00ff",
    "gainsboro": "#dcdcdc",
    "gold": "#ffd700",
    "goldenrod": "#daa520",
    "gray": "#808080",
    "green": "#008000",
    "grey": "#808080",
    "honeydew": "#f0fff0",
    "indigo": "#4b0082",
    "ivory": "#fffff0",
    "khaki": "#f0e68c",
    "lavender": "#e6e6fa",
    "lightblue": "#add8e6",
    "lightgray": "#d3d3d3",
    "lightgreen": "#90ee90",
    "lightgrey": "#d3d3d3",
    "lightpink": "#ffb6c1",
    "lightyellow": "#ffffe0",
    "lime": "#00ff00",
    "magenta": "#ff00ff",
    "maroon": "#800000",
    "mintcream": "#f5fffa",
    "navy": "#000080",
    "olive": "#808000",
    "orange": "#ffa500",
    "orangered": "#ff4500",
    "orchid": "#da70d6",
    "pink": "#ffc0cb",
    "plum": "#dda0dd",
    "purple": "#800080",
    "red": "#ff0000",
    "royalblue": "#4169e1",
    "salmon": "#fa8072",
    "seagreen": "#2e8b57",
    "silver": "#c0c0c0",
    "skyblue": "#87ceeb",
    "slategray": "#708090",
    "slategrey": "#708090",
    "steelblue": "#4682b4",
    "tan": "#d2b48c",
    "teal": "#008080",
    "tomato": "#ff6347",
    "turquoise": "#40e0d0",
    "violet": "#ee82ee",
    "wheat": "#f5deb3",
    "white": "#ffffff",
    "whitesmoke": "#f5f5f5",
    "yellow": "#ffff00",
}

_HEX = re.compile(r"#[0-9a-fA-F]{6}")
# A DOT attribute name: an identifier.
_DOT_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# DOT's keywords, in any case, which an attribute name can't be unquoted.
_DOT_WORDS = frozenset({"node", "edge", "graph", "digraph", "subgraph", "strict"})
# The widest stroke a style may ask for, in points.
_WIDEST = 1000
# A Mermaid style property: a CSS name, which may hold `-` (`stroke-dasharray`).
_MERMAID_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
# What a Mermaid `style` statement can't hold: its separators, quotes, braces,
# the `%%` of a directive, and any control character (a line break among them).
_MERMAID_REFUSED = re.compile(r'[,;"{}%\x00-\x1f\x7f]')
# A DOT value is quoted and escaped, all but control characters other than `\n`.
_DOT_REFUSED = re.compile(r"[\x00-\x09\x0b-\x1f\x7f]")


class StyleValueError(ValueError):
    """A `Style` argument that is the right type but not a value it takes."""


class StyleTypeError(TypeError):
    """A `Style` argument of the wrong type."""


class Pairs(Mapping[str, str]):
    """A passthrough's entries: read-only, sorted by key, hashable and picklable."""

    __slots__ = ("_items",)

    def __init__(self, items: Mapping[str, str] = {}) -> None:  # never mutated
        self._items: tuple[tuple[str, str], ...] = tuple(sorted(items.items()))

    @override
    def __getitem__(self, key: str) -> str:
        for name, value in self._items:
            if name == key:
                return value
        raise KeyError(key)

    @override
    def __iter__(self) -> Iterator[str]:
        return (name for name, _ in self._items)

    @override
    def __len__(self) -> int:
        return len(self._items)

    @override
    def __hash__(self) -> int:
        return hash(self._items)

    @override
    def __eq__(self, other: object) -> bool:
        if isinstance(other, Pairs):
            return self._items == other._items
        return super().__eq__(other)

    @override
    def __repr__(self) -> str:
        return repr(dict(self._items))

    @override
    def __reduce__(self) -> tuple[type[Pairs], tuple[dict[str, str]]]:
        return (Pairs, (dict(self._items),))


@dataclass(frozen=True, slots=True, kw_only=True)
class Style:
    """How a drawing's graph, nodes or edges look, in DOT, Mermaid and SVG alike.

    `fill`, `stroke` and `font_color` are colours: `"#rrggbb"`, or a CSS name in
    `Style.COLOURS` (`"red"`, `"lightblue"`), in any case. `stroke_width` is a
    number above 0; `dashed` a `bool` (`False` takes a dashed outline away);
    `shape` one of `Style.SHAPES`. `dot` and `mermaid` are passthroughs only
    their writer reads: identifier keys (Mermaid's may hold `-`) and `str`
    values. A DOT value is quoted and escaped; a Mermaid value can't hold
    `, ; " { } %` or a line break. Anything else is a `ValueError` (a value of
    the wrong type a `TypeError`) naming the attribute. Every field left `None`
    leaves that part as it was.
    """

    COLOURS: ClassVar[tuple[str, ...]] = tuple(COLOURS)
    SHAPES: ClassVar[tuple[Shape, ...]] = SHAPES

    fill: str | None = None
    stroke: str | None = None
    font_color: str | None = None
    stroke_width: float | None = None
    dashed: bool | None = None
    shape: Shape | None = None
    dot: Mapping[str, str] | None = None
    mermaid: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        """Checks every field, naming the one that is wrong."""
        for name in ("fill", "stroke", "font_color"):
            _check_colour(name, getattr(self, name))
        _check_width(self.stroke_width)
        if self.dashed is not None and not isinstance(self.dashed, bool):
            msg = f"Style dashed must be a bool, got {type(self.dashed).__name__}"
            raise StyleTypeError(msg)
        if self.shape is not None and self.shape not in SHAPES:
            msg = (
                f"Style shape {self.shape!r} is not a shape: "
                f"give one of {', '.join(SHAPES)}"
            )
            raise StyleValueError(msg)
        for name, key, refused in (
            ("dot", _DOT_KEY, _DOT_REFUSED),
            ("mermaid", _MERMAID_KEY, _MERMAID_REFUSED),
        ):
            given = getattr(self, name)
            if given is not None:
                object.__setattr__(self, name, _passthrough(name, given, key, refused))


def _check_colour(name: str, value: object) -> None:
    if value is None:
        return
    if not isinstance(value, str):
        msg = f"Style {name} must be a str colour, got {type(value).__name__}"
        raise StyleTypeError(msg)
    if _HEX.fullmatch(value) is None and value.lower() not in COLOURS:
        msg = (
            f"Style {name} {value!r} is not a colour: give '#rrggbb' "
            "or a CSS name in Style.COLOURS, such as 'red'"
        )
        raise StyleValueError(msg)


def _check_width(value: object) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int | float):
        msg = f"Style stroke_width must be a number, got {type(value).__name__}"
        raise StyleTypeError(msg)
    if (
        isinstance(value, float) and not math.isfinite(value)
    ) or not 0 < value <= _WIDEST:
        msg = (
            f"Style stroke_width must be a number above 0 and at most {_WIDEST}, "
            f"got {value!r:.40}"
        )
        raise StyleValueError(msg)


def _passthrough(
    name: str, given: object, key: re.Pattern[str], refused: re.Pattern[str]
) -> Pairs:
    if not isinstance(given, Mapping):
        msg = (
            f"Style {name} must be a mapping of str to str, got {type(given).__name__}"
        )
        raise StyleTypeError(msg)
    entries: dict[str, str] = {}
    for k, v in given.items():
        if (
            not isinstance(k, str)
            or key.fullmatch(k) is None
            or (name == "dot" and k.lower() in _DOT_WORDS)
        ):
            msg = f"Style {name} key {k!r} is not an attribute name"
            raise StyleValueError(msg)
        if not isinstance(v, str):
            msg = f"Style {name}[{k!r}] must be a str, got {type(v).__name__}"
            raise StyleTypeError(msg)
        if refused.search(v) is not None:
            msg = f"Style {name}[{k!r}] {v!r} holds a character {name} can't take"
            raise StyleValueError(msg)
        entries[k] = v
    return Pairs(entries)


def hex_colour(value: str) -> str:
    """A checked colour as `#rrggbb`, in lower case."""
    return COLOURS.get(value.lower(), value.lower())


def merged(*styles: Style | None) -> Style | None:
    """The styles applied in order, each field given by the last that sets it.

    Passthroughs merge key by key, the later value winning.
    """
    given = [s for s in styles if s is not None]
    if not given:
        return None
    if len(given) == 1:
        return given[0]
    values: dict[str, object] = {}
    for style in given:
        for f in fields(style):
            value = getattr(style, f.name)
            if value is None:
                continue
            if f.name in {"dot", "mermaid"} and isinstance(value, Mapping):
                earlier = values.get(f.name)
                base = dict(earlier) if isinstance(earlier, Mapping) else {}
                values[f.name] = {**base, **value}
            else:
                values[f.name] = value
    return Style(**values)  # ty: ignore[invalid-argument-type]  # each value checked once already


# What a graph's and an edge's style may set; a node's may set everything.
_SCOPES: Mapping[str, tuple[str, ...]] = {
    "graph": ("fill", "dot", "mermaid"),
    "edge": ("stroke", "stroke_width", "dashed", "dot", "mermaid"),
}


def check_scope(scope: str, style: Style | None) -> None:
    """`ValueError` for a field a `graph` or `edge` style can't set."""
    if style is None:
        return
    allowed = _SCOPES[scope]
    article = "an" if scope == "edge" else "a"
    for f in fields(style):
        if f.name not in allowed and getattr(style, f.name) is not None:
            msg = (
                f"{article} {scope} style can't set {f.name}: "
                f"{article} {scope} takes {', '.join(allowed)}"
            )
            raise ValueError(msg)


def number(value: float) -> str:
    """A width as text: `2`, `1.5`."""
    return f"{value:g}"
