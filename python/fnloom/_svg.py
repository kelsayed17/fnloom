"""A `Drawing` laid out and written as SVG in pure Python: the builtin engine (Q146).

The layout is a small layered one. Each node sits one layer past every node it
reads (a node read by nothing earlier is pulled up to just before its first
reader), an edge spanning several layers passes through a reserved slot in each,
nodes are ordered within a layer by a fixed number of barycentre sweeps and
adjacent swaps (ties by position, so by name), and each layer's nodes are placed
as near their neighbours as the gaps between them allow. Nodes are sized from
their text with a fixed table of character widths. An edge's slanted part only
ever crosses the gap between two layers, so no edge runs through a node, and
every edge moves along `orient`. Nothing depends on the hash seed or the machine.
"""

import heapq
import itertools
import math
from bisect import bisect_left, bisect_right, insort
from dataclasses import dataclass
from typing import TYPE_CHECKING

from fnloom._drawing import (
    LEGEND_KEYS,
    block_title,
    checked_parts,
    edge_labels,
    label_lines,
)
from fnloom._style import Style, hex_colour, number

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from fnloom._drawing import Drawing, DrawnNode

_FONT = "Helvetica,Arial,sans-serif"
_FONT_SIZE = 14
_LINE = 18
_PAD_X = 12
_PAD_Y = 8
# Space between layers, along `orient`, and between nodes within one.
_RANK_GAP = 64
_NODE_GAP = 18
# Space around an edge's slot in a layer it passes through.
_SLOT_GAP = 8
_MARGIN = 16
_ARROW = 9
# A cylinder's end caps, how far each bulges.
_CAP = 6
_SWEEPS = 24
_TRANSPOSES = 16
_PLACEMENTS = 12
# Network simplex's work bound, in swaps times the size of the graph part.
_RANK_WORK = 2_000_000
# The ordering sweeps' work bound, in sweeps times items and links.
_ORDER_WORK = 3_000_000
# Sifting's work bounds, in the sum of the squares of the layers' widths.
_SIFT_EVERY = 40_000
_SIFT_ONCE = 400_000

# Helvetica's advance widths in thousandths of an em, for ' ' to '~'.
_WIDTHS = (
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
    1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
    667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
    333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
    556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584,
)  # fmt: skip


def text_width(text: str) -> float:
    """`text`'s width in pixels at the label's size, by the fixed table.

    A character past ASCII counts as a full em, so a label is never drawn
    narrower than its text.
    """
    total = 0
    for c in text:
        code = ord(c)
        total += _WIDTHS[code - 32] if 32 <= code < 127 else 1000  # noqa: PLR2004  # ASCII's printable range
    return total * _FONT_SIZE / 1000


@dataclass(frozen=True, slots=True)
class Box:
    """A laid-out node or the legend: its centre and size, in pixels."""

    name: str
    x: float
    y: float
    width: float
    height: float


@dataclass(frozen=True, slots=True)
class Layout:
    """Where everything is: each node's box, each edge's points, the legend's box."""

    width: float
    height: float
    boxes: tuple[Box, ...]
    edges: tuple[tuple[tuple[float, float], ...], ...]
    legend: Box | None


@dataclass(slots=True)
class _Item:
    """A node, or an edge's slot in a layer it crosses, in layout space.

    Layout space has `rank` along `orient` and `order` across it; `depth` is the
    item's size along `orient` and `breadth` across it.
    """

    rank: int
    depth: float
    breadth: float
    real: bool
    up: list[int]
    down: list[int]
    order: float = 0.0


def _shape(node: DrawnNode) -> str:
    return (
        "rounded"
        if node.style is None or node.style.shape is None
        else node.style.shape
    )


def _size(lines: Sequence[str], shape: str) -> tuple[float, float]:
    """A node's width and height: its text and padding, grown to fit its shape."""
    width = max(text_width(line) for line in lines) + 2 * _PAD_X
    height = len(lines) * _LINE + 2 * _PAD_Y
    root = math.sqrt(2)
    side = max(width, height) * root
    grown = {
        "ellipse": (width * root, height * root),
        "circle": (side, side),
        "diamond": (width * 2, height * 2),
        "hexagon": (width + height, height),
        "cylinder": (width, height + 2 * _CAP),
        "parallelogram": (width + height / 2, height),
    }
    return grown.get(shape, (width, height))


def _ranks(drawing: Drawing) -> dict[str, int]:
    """Each node's layer: past every node it reads, edges as short as can be.

    The total length of the edges is the least any layering gives, found by
    network simplex as Graphviz's `dot` finds it, from a longest-path start.
    """
    order = _topological(drawing)
    index = {name: i for i, name in enumerate(order)}
    edges = [(index[a], index[b]) for a, b in drawing.edges]
    rank = _Ranking(len(index), edges).ranks()
    return {name: rank[i] for i, name in enumerate(order)}


def _topological(drawing: Drawing) -> list[str]:
    """The nodes in an order where every edge points forward.

    A driver's drawing is in one already and keeps it; a hand-made one is
    sorted, ties in its own order. `ValueError` for two nodes of one name, an
    edge to an undrawn node, or edges in a cycle.
    """
    names = [n.name for n in drawing.nodes]
    waiting = dict.fromkeys(names, 0)
    readers: dict[str, list[str]] = {name: [] for name in names}
    checked_parts(drawing)
    for a, b in drawing.edges:
        waiting[b] += 1
        readers[a].append(b)
    position = {name: i for i, name in enumerate(names)}
    ready = [(position[name], name) for name in names if not waiting[name]]
    heapq.heapify(ready)
    order: list[str] = []
    while ready:
        _, name = heapq.heappop(ready)
        order.append(name)
        for reader in readers[name]:
            waiting[reader] -= 1
            if not waiting[reader]:
                heapq.heappush(ready, (position[reader], reader))
    if len(order) < len(names):
        stuck = sorted(name for name in names if waiting[name])
        msg = f"the drawing's edges form a cycle through {', '.join(map(repr, stuck))}"
        raise ValueError(msg)
    return order


# A boundary edge as the feasible tree's heaps hold it: its key, part node, place
# among that node's edges, and the edge.
type _Boundary = tuple[int, int, int, int]


class _Ranking:
    """Network simplex over a graph whose nodes are `0..count-1` in topological order.

    Every edge must span at least one layer; the ranks minimise the sum of the
    spans. Ties go to the lower node or edge index, so the result is fixed.
    """

    def __init__(self, count: int, edges: Sequence[tuple[int, int]]) -> None:
        self.count = count
        self.edges = list(edges)
        self.incident: list[list[int]] = [[] for _ in range(count)]
        for e, (a, b) in enumerate(self.edges):
            self.incident[a].append(e)
            self.incident[b].append(e)
        self.rank = [0] * count
        self._longest()
        self.tree: set[int] = set()
        self.cut: dict[int, int] = {}
        self.parent: list[int | None] = [None] * count
        self.low = [0] * count
        self.lim = [0] * count

    def _longest(self) -> None:
        """Longest-path ranks: each node one past the latest node it reads."""
        reads: list[list[int]] = [[] for _ in range(self.count)]
        for a, b in self.edges:
            reads[b].append(a)
        for v in range(self.count):  # topological order
            self.rank[v] = 1 + max((self.rank[a] for a in reads[v]), default=-1)

    def _slack(self, e: int) -> int:
        a, b = self.edges[e]
        return self.rank[b] - self.rank[a] - 1

    def ranks(self) -> list[int]:
        """The ranks, each weakly connected part starting at 0."""
        placed = [False] * self.count
        for root in range(self.count):
            if not placed[root]:
                part = self._feasible_tree(root)
                for v in part:
                    placed[v] = True
                self._improve(root, part)
                lowest = min(self.rank[v] for v in part)
                for v in part:
                    self.rank[v] -= lowest
        return self.rank

    def _feasible_tree(self, root: int) -> set[int]:
        """A tight spanning tree of `root`'s part, its ranks shifted to make one.

        Each round adds the boundary edge of least slack, the first of them by
        part node and then by its place in that node's edges, shifts the part to
        make it tight, and grows the tree by every tight edge it can reach, from
        the part's nodes in descending order, depth first. Each boundary edge
        waits in a heap and the part's shift is one offset, so a round costs
        what changes in it, not the size of the part.
        """
        self._part: set[int] = set()
        # A part node's rank is `rank[v] + shift`: the part moves as one.
        self._shift = 0
        # Boundary edges by slack, part node and place: `outward` from the part,
        # whose slack falls as the part rises, then `inward`, whose slack grows.
        # An entry's key is its slack less, or plus, the shift when it was made.
        self._outward: list[_Boundary] = []
        self._inward: list[_Boundary] = []
        self._join(root)
        self._tight([root])
        while True:
            tops = (self._first(self._outward, -1), self._first(self._inward, 1))
            best = min((top for top in tops if top is not None), default=None)
            if best is None:
                break
            delta, _, _, e = best
            self._shift += -delta if self.edges[e][1] in self._part else delta
            self._tight(self._tight_now())
        part = self._part
        for v in part:
            self.rank[v] += self._shift
        return part

    def _at(self, v: int) -> int:
        """`v`'s rank while the feasible tree grows: a part node's shifted."""
        return self.rank[v] + self._shift if v in self._part else self.rank[v]

    def _growing_slack(self, e: int) -> int:
        a, b = self.edges[e]
        return self._at(b) - self._at(a) - 1

    def _join(self, v: int) -> None:
        """Adds `v` to the part, its edges out of the part to the heaps."""
        part = self._part
        self.rank[v] -= self._shift
        part.add(v)
        for place, e in enumerate(self.incident[v]):
            a, b = self.edges[e]
            if (a in part) == (b in part):
                continue
            slack = self._growing_slack(e)
            if a == v:
                heapq.heappush(self._outward, (slack + self._shift, v, place, e))
            else:
                heapq.heappush(self._inward, (slack - self._shift, v, place, e))

    def _first(self, heap: list[_Boundary], sign: int) -> _Boundary | None:
        """The heap's least boundary entry, with its slack now; spent ones dropped."""
        part, edges = self._part, self.edges
        while heap and (edges[heap[0][3]][0] in part) == (edges[heap[0][3]][1] in part):
            heapq.heappop(heap)
        if not heap:
            return None
        key, v, place, e = heap[0]
        return (key + sign * self._shift, v, place, e)

    def _tight_now(self) -> list[int]:
        """The part nodes with a boundary edge of no slack, taken off the heaps."""
        found: set[int] = set()
        for heap, sign in ((self._outward, -1), (self._inward, 1)):
            while (top := self._first(heap, sign)) is not None and top[0] == 0:
                found.add(top[1])
                heapq.heappop(heap)
        return list(found)

    def _tight(self, starts: list[int]) -> None:
        """Grows the tree by every tight edge reachable from `starts`, depth first."""
        stack = sorted(starts)
        while stack:
            v = stack.pop()
            for e in self.incident[v]:
                a, b = self.edges[e]
                other = b if a == v else a
                if other not in self._part and self._growing_slack(e) == 0:
                    self._join(other)
                    self.tree.add(e)
                    stack.append(other)

    def _ranges(self, root: int) -> None:
        """Each tree node's parent, and `low`/`lim`: its subtree's postorder span."""
        adjacent: dict[int, list[tuple[int, int]]] = {}
        for e in sorted(self.tree):
            a, b = self.edges[e]
            adjacent.setdefault(a, []).append((b, e))
            adjacent.setdefault(b, []).append((a, e))
        self.parent[root] = None
        counter = 0
        stack: list[tuple[int, int | None, int]] = [(root, None, 0)]
        lows: dict[int, int] = {}
        while stack:
            v, via, step = stack.pop()
            near = adjacent.get(v, [])
            if step == 0:
                self.parent[v] = via
                lows[v] = counter
            if step < len(near):
                stack.append((v, via, step + 1))
                other, e = near[step]
                if e != via:
                    stack.append((other, e, 0))
                continue
            self.lim[v] = counter
            self.low[v] = lows[v]
            counter += 1

    def _inside(self, v: int, w: int) -> bool:
        """Whether `w` is in `v`'s subtree."""
        return self.low[v] <= self.lim[w] <= self.lim[v]

    def _cut_values(self, root: int, part: set[int]) -> None:
        """Every tree edge's cut value, children first, as `dot` computes it."""
        self.cut = {}
        for v in sorted(part, key=lambda n: self.lim[n]):
            f = self.parent[v]
            if v == root or f is None:
                continue
            direction = 1 if self.edges[f][0] == v else -1
            total = 0
            for e in self.incident[v]:
                a, b = self.edges[e]
                other = b if a == v else a
                outside = not self._inside(v, other)
                value = 1 if outside else (self.cut[e] if e in self.tree else 0) - 1
                # +1 when `e` points into `v` as the parent edge points (or
                # away from it, for an edge leaving the subtree).
                d = 1 if (b if direction > 0 else a) == v else -1
                if outside:
                    d = -d
                total += value if d > 0 else -value
            self.cut[f] = total

    def _improve(self, root: int, part: set[int]) -> None:
        """Swaps a tree edge of negative cut value for a better one, while any is.

        Every edge already tight is the least length there is, so nothing runs.
        Each swap costs about the part's size, so the swaps are bounded by
        `_RANK_WORK` over it: work fixed by the graph, never by time, so a large
        graph ends with feasible ranks, if not always the shortest.
        """
        inside = [e for v in part for e in self.incident[v] if self.edges[e][0] == v]
        if all(self._slack(e) == 0 for e in inside):
            return
        pivots = max(1, _RANK_WORK // (len(part) + len(inside)))
        for _ in range(pivots):
            self._ranges(root)
            self._cut_values(root, part)
            leaving = next((e for e in sorted(self.cut) if self.cut[e] < 0), None)
            if leaving is None:
                return
            a, b = self.edges[leaving]
            down, outward = (a, False) if self.lim[a] < self.lim[b] else (b, True)
            entering: int | None = None
            for e, (x, y) in enumerate(self.edges):
                if e in self.tree or x not in part:
                    continue
                crosses = (
                    self._inside(down, x) and not self._inside(down, y)
                    if outward
                    else self._inside(down, y) and not self._inside(down, x)
                )
                if crosses and (
                    entering is None or self._slack(e) < self._slack(entering)
                ):
                    entering = e
            if entering is None:
                return
            self.tree.discard(leaving)
            self.tree.add(entering)
            self._rerank(root)

    def _rerank(self, root: int) -> None:
        """Ranks that make every tree edge tight, `root` keeping its own."""
        adjacent: dict[int, list[tuple[int, int]]] = {}
        for e in sorted(self.tree):
            a, b = self.edges[e]
            adjacent.setdefault(a, []).append((b, e))
            adjacent.setdefault(b, []).append((a, e))
        seen = {root}
        stack = [root]
        while stack:
            v = stack.pop()
            for other, e in adjacent.get(v, []):
                if other in seen:
                    continue
                seen.add(other)
                a, _ = self.edges[e]
                self.rank[other] = self.rank[v] + (1 if a == v else -1)
                stack.append(other)


def _items(
    drawing: Drawing, sizes: Mapping[str, tuple[float, float]], *, across: bool
) -> tuple[list[_Item], list[list[int]]]:
    """The layout's items, real nodes first in drawing order, and each edge's chain.

    `across` is whether `orient` runs across the page (`LR`, `RL`), so a node's
    depth is its width.
    """
    rank = _ranks(drawing)
    index = {n.name: i for i, n in enumerate(drawing.nodes)}
    items: list[_Item] = []
    for n in drawing.nodes:
        width, height = sizes[n.name]
        depth, breadth = (width, height) if across else (height, width)
        items.append(_Item(rank[n.name], depth, breadth, real=True, up=[], down=[]))
    chains: list[list[int]] = []
    for dependency, node in drawing.edges:
        chain = [index[dependency]]
        for r in range(rank[dependency] + 1, rank[node]):
            items.append(_Item(r, 0.0, 0.0, real=False, up=[], down=[]))
            chain.append(len(items) - 1)
        chain.append(index[node])
        for a, b in itertools.pairwise(chain):
            items[a].down.append(b)
            items[b].up.append(a)
        chains.append(chain)
    return items, chains


def _crossings(layers: Sequence[Sequence[int]], items: Sequence[_Item]) -> int:
    """The edge crossings between each pair of adjacent layers."""
    position = {i: p for layer in layers for p, i in enumerate(layer)}
    total = 0
    for layer in layers:
        pairs = sorted((position[u], position[v]) for u in layer for v in items[u].down)
        seen: list[int] = []  # sorted
        for _, b in pairs:
            total += len(seen) - bisect_right(seen, b)
            insort(seen, b)
    return total


def _swept(
    layers: list[list[int]],
    items: Sequence[_Item],
    *,
    downward: bool,
    median: bool = False,
) -> list[list[int]]:
    """Each layer reordered by its neighbours' positions in the layer before.

    By their mean (the barycentre), or with `median` their median.
    """
    position = {i: p for layer in layers for p, i in enumerate(layer)}
    out = [list(layer) for layer in layers]
    steps = range(1, len(out)) if downward else range(len(out) - 2, -1, -1)
    for r in steps:

        def key(i: int) -> tuple[float, int]:
            near = items[i].up if downward else items[i].down
            here = position[i]
            if not near:
                return (float(here), here)
            places = sorted(position[j] for j in near)
            if median:
                middle = len(places) // 2
                value = (places[middle] + places[(len(places) - 1) // 2]) / 2
            else:
                value = sum(places) / len(places)
            return (value, here)

        out[r].sort(key=key)
        position.update({i: p for p, i in enumerate(out[r])})
    return out


def _sifted(layers: list[list[int]], items: Sequence[_Item]) -> list[list[int]]:
    """Each item moved to the place in its layer where its edges cross least.

    Items are taken in layer order; a move happens only when it cuts crossings,
    the earliest best place winning, and passes repeat while any move does.
    """
    out = [list(layer) for layer in layers]
    position = {i: p for layer in out for p, i in enumerate(layer)}
    for _ in range(_TRANSPOSES):
        improved = False
        for layer in out:
            if len(layer) > 1 and _sift_layer(layer, items, position):
                improved = True
        if not improved:
            break
    return out


def _sift_layer(
    layer: list[int], items: Sequence[_Item], position: dict[int, int]
) -> bool:
    """Sifts each item of `layer` in turn; whether any moved.

    The layers either side hold still meanwhile, so the crossings of each pair
    of items, `before[u][w]` with `u` before `w`, are counted once.
    """
    ups = {u: sorted(position[a] for a in items[u].up) for u in layer}
    downs = {u: sorted(position[a] for a in items[u].down) for u in layer}
    before = _pair_crossings(layer, ups, downs)
    moved = False
    for u in list(layer):
        if not ups[u] and not downs[u]:
            continue
        row = before[u]
        others = [w for w in layer if w != u]
        cost = sum(row.values())
        best_cost, best_place = cost, 0
        current = layer.index(u)
        current_cost = cost
        for p, w in enumerate(others, start=1):
            cost += before[w][u] - row[w]
            if cost < best_cost:
                best_cost, best_place = cost, p
            if p == current:
                current_cost = cost
        if best_cost < current_cost:
            others.insert(best_place, u)
            layer[:] = others
            position.update({w: p for p, w in enumerate(layer)})
            moved = True
    return moved


def _pair_crossings(
    layer: Sequence[int],
    ups: Mapping[int, Sequence[int]],
    downs: Mapping[int, Sequence[int]],
) -> dict[int, dict[int, int]]:
    """`[u][w]`: the crossings of `u`'s and `w`'s edges with `u` placed before `w`.

    `ups` and `downs` are each item's neighbours' sorted positions either side.
    """
    return {
        u: {
            w: sum(bisect_left(ups[w], a) for a in ups[u])
            + sum(bisect_left(downs[w], a) for a in downs[u])
            for w in layer
            if w != u
        }
        for u in layer
    }


def _starts(items: Sequence[_Item]) -> list[list[list[int]]]:
    """The orders the sweeps start from: by index, depth first, and reversed.

    Depth first walks from each item read by nothing, in index order, to the
    items reading it, so what one item feeds starts out together.
    """
    depth = max((item.rank for item in items), default=-1) + 1
    by_index: list[list[int]] = [[] for _ in range(depth)]
    for i, item in enumerate(items):
        by_index[item.rank].append(i)
    walked: list[list[int]] = [[] for _ in range(depth)]
    seen: set[int] = set()
    for root in range(len(items)):
        if items[root].up or root in seen:
            continue
        stack = [root]
        while stack:
            i = stack.pop()
            if i in seen:
                continue
            seen.add(i)
            walked[items[i].rank].append(i)
            stack.extend(reversed(items[i].down))
    for i, item in enumerate(items):  # anything a cycle-free walk missed
        if i not in seen:
            walked[item.rank].append(i)
    return [by_index, walked, [list(reversed(layer)) for layer in by_index]]


def _ordered(items: Sequence[_Item]) -> list[list[int]]:
    """The items of each layer in the order with the fewest crossings found."""
    starts = _starts(items)
    # Sifting costs the square of a layer's width, so how much of it a layout
    # gets is fixed by the graph alone: after every run, once, or not at all.
    width = sum(len(layer) ** 2 for layer in starts[0])
    every = width <= _SIFT_EVERY
    # A sweep costs about the items and their links: the sweeps of each run
    # are bounded so all six runs fit `_ORDER_WORK`.
    size = len(items) + sum(len(item.down) for item in items)
    sweeps = max(2, min(_SWEEPS, _ORDER_WORK // (6 * max(1, size))))
    best: list[list[int]] = []
    fewest = -1
    for start in starts:
        for first in (0, 1):
            median = bool(first)
            found, least = _improved(start, items, first, median=median, sweeps=sweeps)
            if every:
                found, least = _polished(found, least, items, first, median=median)
            if fewest < 0 or least < fewest:
                best, fewest = found, least
            if not fewest:
                return best
    if not every and width <= _SIFT_ONCE:
        best, fewest = _polished(best, fewest, items, 0, median=False)
    return best


def _improved(
    start: list[list[int]],
    items: Sequence[_Item],
    first: int,
    *,
    median: bool,
    sweeps: int = _SWEEPS,
) -> tuple[list[list[int]], int]:
    """The best order sweeps reach from `start`, and its crossings.

    `first` is 0 to sweep downward first, 1 upward; `median` sweeps by the
    neighbours' median rather than their barycentre.
    """
    best, fewest = start, _crossings(start, items)
    current = start
    for sweep in range(sweeps):
        if not fewest:
            break
        current = _swept(
            current, items, downward=(sweep + first) % 2 == 0, median=median
        )
        count = _crossings(current, items)
        if count < fewest:
            best, fewest = current, count
    return best, fewest


def _polished(
    best: list[list[int]],
    fewest: int,
    items: Sequence[_Item],
    first: int,
    *,
    median: bool,
) -> tuple[list[list[int]], int]:
    """`best` sifted, with a sweep between, while that cuts its crossings."""
    current = best
    for sweep in range(_SWEEPS):
        if not fewest:
            break
        current = _sifted(current, items)
        count = _crossings(current, items)
        if count < fewest:
            best, fewest = current, count
        elif sweep:
            break
        current = _swept(
            current, items, downward=(sweep + first) % 2 == 0, median=median
        )
    return best, fewest


def _separation(a: _Item, b: _Item) -> float:
    gap = _NODE_GAP if a.real and b.real else _SLOT_GAP
    return (a.breadth + b.breadth) / 2 + gap


def _fitted(
    targets: Sequence[float], weights: Sequence[float], gaps: Sequence[float]
) -> list[float]:
    """Positions in order, each `gaps[i]` past the one before, nearest `targets`.

    Least squares under the gaps: shifted by the running gaps, it is an
    isotonic regression, solved by pooling adjacent violators.
    """
    offsets = [0.0]
    for gap in gaps:
        offsets.append(offsets[-1] + gap)
    blocks: list[tuple[float, float, int]] = []
    for target, weight, offset in zip(targets, weights, offsets, strict=True):
        blocks.append((target - offset, weight, 1))
        while len(blocks) > 1 and blocks[-2][0] > blocks[-1][0]:
            v2, w2, c2 = blocks.pop()
            v1, w1, c1 = blocks.pop()
            blocks.append(((v1 * w1 + v2 * w2) / (w1 + w2), w1 + w2, c1 + c2))
    shifted = [value for value, _, count in blocks for _ in range(count)]
    return [s + o for s, o in zip(shifted, offsets, strict=True)]


def _place(layers: Sequence[Sequence[int]], items: list[_Item]) -> None:
    """Sets each item's `order`: near its neighbours, a gap from the next item."""
    for layer in layers:
        position = 0.0
        for p, i in enumerate(layer):
            if p:
                position += _separation(items[layer[p - 1]], items[i])
            items[i].order = position
        shift = position / 2
        for i in layer:
            items[i].order -= shift
    for round_ in range(_PLACEMENTS):
        downward = round_ % 2 == 0
        steps = range(1, len(layers)) if downward else range(len(layers) - 2, -1, -1)
        for r in steps:
            layer = layers[r]
            targets: list[float] = []
            weights: list[float] = []
            for i in layer:
                near = items[i].up if downward else items[i].down
                if near:
                    targets.append(sum(items[j].order for j in near) / len(near))
                    # A slot pulls hard, so a long edge runs straight.
                    weights.append(len(near) * (1.0 if items[i].real else 4.0))
                else:
                    targets.append(items[i].order)
                    weights.append(0.5)
            gaps = [
                _separation(items[a], items[b]) for a, b in itertools.pairwise(layer)
            ]
            for i, order in zip(layer, _fitted(targets, weights, gaps), strict=True):
                items[i].order = order


class TooLargeError(ValueError):
    """A drawing past a preview's size bound (`PREVIEW_PARTS`, `PREVIEW_ITEMS`)."""


# A notebook preview's bounds: nodes and edges, then nodes and edge slots once
# layered. Past either, the preview shows text, so it can never hang.
PREVIEW_PARTS = 3_000
PREVIEW_ITEMS = 20_000


def layout(
    drawing: Drawing, orient: str, *, legend: bool = True, preview: bool = False
) -> Layout:
    """Lays `drawing` out along `orient`; `legend` adds the legend's box below."""
    sizes = {
        n.name: _size(_lines(n, drawing.show_types), _shape(n)) for n in drawing.nodes
    }
    across = orient in {"LR", "RL"}
    if preview and len(drawing.nodes) + len(drawing.edges) > PREVIEW_PARTS:
        raise TooLargeError(len(drawing.nodes) + len(drawing.edges))
    items, chains = _items(drawing, sizes, across=across)
    if preview and len(items) > PREVIEW_ITEMS:
        raise TooLargeError(len(items))
    layers = _ordered(items)
    _place(layers, items)
    # Each layer's span along `orient`: as deep as its deepest node.
    depths = [max((items[i].depth for i in layer), default=0.0) for layer in layers]
    starts: list[float] = []
    position = 0.0
    for depth in depths:
        starts.append(position)
        position += depth + _RANK_GAP
    span = position - _RANK_GAP if layers else 0.0
    lowest = min((item.order - item.breadth / 2 for item in items), default=0.0)
    highest = max((item.order + item.breadth / 2 for item in items), default=0.0)

    def placed(rank_at: float, order: float) -> tuple[float, float]:
        """A layout-space point on the page, `_MARGIN` inside its edges."""
        along = rank_at if orient in {"LR", "TB"} else span - rank_at
        aside = order - lowest
        if across:
            return (_MARGIN + along, _MARGIN + aside)
        return (_MARGIN + aside, _MARGIN + along)

    boxes: list[Box] = []
    for n, item in zip(drawing.nodes, items, strict=False):
        centre = starts[item.rank] + depths[item.rank] / 2
        x, y = placed(centre, item.order)
        width, height = sizes[n.name]
        boxes.append(Box(n.name, x, y, width, height))
    edges = tuple(
        tuple(placed(a, o) for a, o in _route(chain, items, starts, depths))
        for chain in chains
    )
    breadth = highest - lowest
    width, height = (span, breadth) if across else (breadth, span)
    legend_box = _legend_box(drawing, _MARGIN + height) if legend else None
    page_width = width + 2 * _MARGIN
    page_height = height + 2 * _MARGIN
    if legend_box is not None:
        page_width = max(page_width, legend_box.x + legend_box.width / 2 + _MARGIN)
        page_height = legend_box.y + legend_box.height / 2 + _MARGIN
    return Layout(page_width, page_height, tuple(boxes), edges, legend_box)


def _route(
    chain: Sequence[int],
    items: Sequence[_Item],
    starts: Sequence[float],
    depths: Sequence[float],
) -> list[tuple[float, float]]:
    """An edge's points in layout space, `(along, across)`.

    It runs straight through each layer it is in, along `orient`, and slants
    only in the gaps between layers. The last point is the arrow's tip.
    """
    first, last = items[chain[0]], items[chain[-1]]
    points: list[tuple[float, float]] = []
    centre = starts[first.rank] + depths[first.rank] / 2
    points.append((centre + first.depth / 2, first.order))
    points.append((starts[first.rank] + depths[first.rank], first.order))
    for i in chain[1:-1]:
        item = items[i]
        points.append((starts[item.rank], item.order))
        points.append((starts[item.rank] + depths[item.rank], item.order))
    tip = starts[last.rank] + depths[last.rank] / 2 - last.depth / 2
    points.append((min(starts[last.rank], tip - _ARROW), last.order))
    points.append((tip, last.order))
    route: list[tuple[float, float]] = []
    for along, across in points:
        if route:
            # Equal sums can differ in the last bit: never step back.
            along = max(along, route[-1][0])  # noqa: PLW2901  # clamped in place
            if math.isclose(along, route[-1][0], abs_tol=1e-6) and math.isclose(
                across, route[-1][1], abs_tol=1e-6
            ):
                continue
        route.append((along, across))
    return route


def _lines(node: DrawnNode, show_types: bool) -> list[str]:  # noqa: FBT001  # private
    """A node's label lines, and its block's title: the builtin engine draws no cluster.

    Its layers don't keep a block's nodes together, so each says its block (Q131).
    """
    lines = label_lines(node, show_types=show_types)
    if node.block is not None:
        lines.append(block_title(node.block))
    return [line.replace("\n", " ") for line in lines]


def _legend_keys(drawing: Drawing) -> list[str]:
    used = {n.kind for n in drawing.nodes} | {m for n in drawing.nodes for m in n.marks}
    return [key for key in LEGEND_KEYS if key in used]


_LEGEND_TITLE = 22


def _legend_entry_width(key: str) -> float:
    return text_width(key) + 2 * _PAD_X


def _legend_box(drawing: Drawing, top: float) -> Box | None:
    """The legend below the drawing: a row of one entry per kind and mark drawn."""
    keys = _legend_keys(drawing)
    if not keys:
        return None
    entries = sum(_legend_entry_width(k) for k in keys) + _NODE_GAP * (len(keys) - 1)
    width = max(entries, text_width("Legend")) + 2 * _PAD_X
    height = _LEGEND_TITLE + _LINE + 2 * _PAD_Y + _PAD_Y
    y = top + _NODE_GAP + height / 2
    return Box("Legend", _MARGIN + width / 2, y, width, height)


@dataclass(frozen=True, slots=True)
class _Look:
    fill: str
    stroke: str
    font: str
    width: float
    dashes: str | None
    shape: str


_BLACK = "#000000"


def _look(
    kind: str, marks: frozenset[str] | tuple[str, ...], style: Style | None
) -> _Look:
    """A node's look: its kind's, its marks' in legend order, then its style's."""
    fill, stroke, font, width = "#b4d8e4", _BLACK, _BLACK, 1.0
    dashes: str | None = None
    if kind == "input":
        fill, dashes = "#ffffff", "5,3"
    for mark in LEGEND_KEYS:
        if mark not in marks:
            continue
        if mark == "may be absent":
            dashes = "5,3"
        elif mark == "output":
            fill = "#ffc857"
        elif mark == "override":
            width, dashes = 3.0, "2,2"
        elif mark == "path":
            stroke, width = "#ff0000", max(width, 2.0)
        elif mark == "context":
            fill, stroke, font = "#eeeeee", "#999999", "#777777"
    look = _Look(fill, stroke, font, width, dashes, "rounded")
    return look if style is None else _styled(look, style)


def _styled(look: _Look, style: Style) -> _Look:
    """`look`, with what `style` sets taking over."""
    dashes = look.dashes
    if style.dashed is not None:
        dashes = "5,3" if style.dashed else None
    return _Look(
        hex_colour(style.fill) if style.fill is not None else look.fill,
        hex_colour(style.stroke) if style.stroke is not None else look.stroke,
        hex_colour(style.font_color) if style.font_color is not None else look.font,
        style.stroke_width if style.stroke_width is not None else look.width,
        dashes,
        style.shape if style.shape is not None else look.shape,
    )


def _n(value: float) -> str:
    """A coordinate as text, to a tenth of a pixel."""
    text = f"{value:.1f}".rstrip("0").rstrip(".")
    return "0" if text == "-0" else text


def _escaped(text: str) -> str:
    """`text` for XML: its markup characters as entities, control characters gone."""
    out: list[str] = []
    for c in text:
        if c in "&<>\"'":
            out.append(
                {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]
            )
        elif ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF or ord(c) in {0xFFFE, 0xFFFF}:  # noqa: PLR2004  # what XML 1.0 can't hold
            out.append("�")
        else:
            out.append(c)
    return "".join(out)


def _outline(look: _Look, box: Box) -> str:
    """The element drawing a node's shape."""
    paint = (
        f'fill="{look.fill}" stroke="{look.stroke}" stroke-width="{number(look.width)}"'
        + (f' stroke-dasharray="{look.dashes}"' if look.dashes is not None else "")
    )
    left, top = box.x - box.width / 2, box.y - box.height / 2
    right, bottom = left + box.width, top + box.height
    if look.shape in {"rounded", "box"}:
        corner = ' rx="6"' if look.shape == "rounded" else ""
        return (
            f'<rect x="{_n(left)}" y="{_n(top)}" width="{_n(box.width)}" '
            f'height="{_n(box.height)}"{corner} {paint}/>'
        )
    if look.shape in {"ellipse", "circle"}:
        return (
            f'<ellipse cx="{_n(box.x)}" cy="{_n(box.y)}" rx="{_n(box.width / 2)}" '
            f'ry="{_n(box.height / 2)}" {paint}/>'
        )
    if look.shape == "cylinder":
        cap = _CAP
        return (
            f'<path d="M{_n(left)},{_n(top + cap)} '
            f"A{_n(box.width / 2)},{cap} 0 0 1 {_n(right)},{_n(top + cap)} "
            f"L{_n(right)},{_n(bottom - cap)} "
            f"A{_n(box.width / 2)},{cap} 0 0 1 {_n(left)},{_n(bottom - cap)} Z "
            f"M{_n(left)},{_n(top + cap)} "
            f'A{_n(box.width / 2)},{cap} 0 0 0 {_n(right)},{_n(top + cap)}" {paint}/>'
        )
    corners = {
        "diamond": [(box.x, top), (right, box.y), (box.x, bottom), (left, box.y)],
        "hexagon": [
            (left + box.height / 2, top),
            (right - box.height / 2, top),
            (right, box.y),
            (right - box.height / 2, bottom),
            (left + box.height / 2, bottom),
            (left, box.y),
        ],
        "parallelogram": [
            (left + box.height / 2, top),
            (right, top),
            (right - box.height / 2, bottom),
            (left, bottom),
        ],
    }[look.shape]
    points = " ".join(f"{_n(x)},{_n(y)}" for x, y in corners)
    return f'<polygon points="{points}" {paint}/>'


def _text(lines: Sequence[str], x: float, y: float, colour: str) -> str:
    top = y - (len(lines) - 1) * _LINE / 2
    spans = "".join(
        f'<tspan x="{_n(x)}" y="{_n(top + i * _LINE + 5)}">{_escaped(line)}</tspan>'
        for i, line in enumerate(lines)
    )
    return f'<text text-anchor="middle" fill="{colour}">{spans}</text>'


def _edge_label(
    label: str | None, route: Sequence[tuple[float, float]], colour: str
) -> str:
    """An edge's `expand` or `collect`, at the middle of its route, or nothing."""
    if label is None:
        return ""
    middle = len(route) // 2
    (x0, y0), (x1, y1) = route[middle - 1], route[middle]
    return _text([label], (x0 + x1) / 2, (y0 + y1) / 2 - _LINE / 2, colour)


def _arrow(route: Sequence[tuple[float, float]], colour: str) -> str:
    """The arrowhead at the route's last point, pointing along its last segment."""
    (x0, y0), (x1, y1) = route[-2], route[-1]
    length = math.hypot(x1 - x0, y1 - y0) or 1.0
    ux, uy = (x1 - x0) / length, (y1 - y0) / length
    bx, by = x1 - ux * _ARROW, y1 - uy * _ARROW
    half = _ARROW / 2.2
    corners = [
        (x1, y1),
        (bx - uy * half, by + ux * half),
        (bx + uy * half, by - ux * half),
    ]
    points = " ".join(f"{_n(x)},{_n(y)}" for x, y in corners)
    return f'<polygon points="{points}" fill="{colour}" stroke="{colour}"/>'


def svg(
    drawing: Drawing, orient: str, *, legend: bool = True, preview: bool = False
) -> str:
    """`drawing` as SVG text, laid out along `orient`."""
    placed = layout(drawing, orient, legend=legend, preview=preview)
    background = "#ffffff"
    if drawing.graph_style is not None and drawing.graph_style.fill is not None:
        background = hex_colour(drawing.graph_style.fill)
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{_n(placed.width)}" '
            f'height="{_n(placed.height)}" viewBox="0 0 {_n(placed.width)} '
            f'{_n(placed.height)}" font-family="{_FONT}" font-size="{_FONT_SIZE}">'
        ),
        f'<rect width="100%" height="100%" fill="{background}"/>',
    ]
    on_path = {n.name for n in drawing.nodes if "path" in n.marks}
    edge = drawing.edge_style
    labels = edge_labels(drawing)
    for (dependency, node), route, label in zip(
        drawing.edges, placed.edges, labels, strict=True
    ):
        colour, width, dashes = _BLACK, 1.0, None
        if edge is not None:
            colour = hex_colour(edge.stroke) if edge.stroke is not None else colour
            width = edge.stroke_width if edge.stroke_width is not None else width
            dashes = "5,3" if edge.dashed is True else None
        if {dependency, node} <= on_path:
            colour = "#ff0000"
        x0, y0 = route[-2]
        x1, y1 = route[-1]
        length = math.hypot(x1 - x0, y1 - y0) or 1.0
        end = (x1 - (x1 - x0) / length * _ARROW, y1 - (y1 - y0) / length * _ARROW)
        written = [f"{_n(x)},{_n(y)}" for x, y in [*route[:-1], end]]
        points = " ".join(
            p for i, p in enumerate(written) if not i or p != written[i - 1]
        )
        dash = f' stroke-dasharray="{dashes}"' if dashes else ""
        title = f"{_escaped(dependency)} -&gt; {_escaped(node)}"
        out.append(
            f'<g class="edge"><title>{title}</title>'
            f'<polyline points="{points}" fill="none" stroke="{colour}" '
            f'stroke-width="{number(width)}"{dash}/>{_arrow(route, colour)}'
            f"{_edge_label(label, route, colour)}</g>"
        )
    for n, box in zip(drawing.nodes, placed.boxes, strict=True):
        look = _look(n.kind, n.marks, n.style)
        out.append(
            f'<g class="node"><title>{_escaped(n.name)}</title>{_outline(look, box)}'
            f"{_text(_lines(n, drawing.show_types), box.x, box.y, look.font)}</g>"
        )
    if placed.legend is not None:
        out.extend(_legend(drawing, placed.legend))
    out.append("</svg>")
    return "\n".join(out) + "\n"


def _legend(drawing: Drawing, box: Box) -> list[str]:
    left, top = box.x - box.width / 2, box.y - box.height / 2
    out = [
        '<g class="legend">',
        (
            f'<rect x="{_n(left)}" y="{_n(top)}" width="{_n(box.width)}" '
            f'height="{_n(box.height)}" fill="none" stroke="{_BLACK}"/>'
        ),
        _text(["Legend"], box.x, top + _LEGEND_TITLE / 2 + 2, _BLACK),
    ]
    x = left + _PAD_X
    entry_y = top + _LEGEND_TITLE + _PAD_Y + (_LINE + _PAD_Y) / 2
    for key in _legend_keys(drawing):
        width = _legend_entry_width(key)
        marks = () if key in {"function", "input"} else (key,)
        look = _look("input" if key == "input" else "function", marks, None)
        entry = Box(key, x + width / 2, entry_y, width, _LINE + _PAD_Y)
        out.append(_outline(look, entry) + _text([key], entry.x, entry.y, look.font))
        x += width + _NODE_GAP
    out.append("</g>")
    return out
