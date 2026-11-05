# Drawing

A driver draws its graph as text: DOT, which Graphviz lays out, with nothing to install. The examples on this page use
one module:

```python
# file: prices.py
from fnloom import config


def subtotal(price: float, quantity: int) -> float:
    """What the items cost."""
    return price * quantity


def shipping(subtotal: float, free_above: float = 50.0) -> float:
    """Free above a threshold, a flat rate below it."""
    return 0.0 if subtotal > free_above else 4.5


@config.when(region="eu")
def tax__eu(subtotal: float) -> float:
    """VAT."""
    return subtotal * 0.2


def total(subtotal: float, shipping: float, tax: float) -> float:
    """What the customer pays."""
    return subtotal + shipping + tax
```

## A drawing

`draw()` draws every node and input. It returns a `fnloom.Drawing`, plain data: its `nodes` in dependency order, ties
broken by name, and its `edges`, each a `(dependency, node)` pair:

```python
import fnloom
import prices

driver = fnloom.Builder().with_modules(prices).with_config({"region": "eu"}).build()
drawing = driver.draw()
[node.name for node in drawing.nodes]
# -> ['free_above', 'price', 'quantity', 'subtotal', 'shipping', 'tax', 'total']
drawing.edges[:2]
# -> (('price', 'subtotal'), ('quantity', 'subtotal'))
```

Each input is a node of its own, drawn with a dashed outline, with an edge to every node that reads it. Config is not
drawn, since it is never a node; a `@config.when` node's label shows the conditions it was chosen by. A label shows the
node's name, or its `display_name` tag, over its whole type:

```python
tax = drawing.nodes[5]
(tax.title, tax.type, tax.condition)
# -> ('tax', 'float', "region == 'eu'")
```

`to_dot()` writes the text, and `str(drawing)` is the same. `orient` sets the direction edges point (`"LR"`, `"TB"`,
`"RL"` or `"BT"`), and `legend=False` leaves out the box naming each kind and mark drawn:

```python
dot = drawing.to_dot(orient="TB", legend=False)
dot.splitlines()[1]
# -> '  graph [rankdir=TB]'
```

The text is the same on every run and under every `PYTHONHASHSEED`, so it can be committed, diffed or compared in a
test.

## Parts of a graph

| Method | Draws |
| --- | --- |
| `draw()` | every node and input |
| `draw_upstream_of(*names)` | the named nodes and all they depend on |
| `draw_downstream_of(*names)` | the named nodes and all that depends on them, plus what else those read, marked `context` |
| `draw_path_between(upstream, downstream)` | the nodes on a path, marked `path`, plus what else they read, marked `context` |

Each node's `marks` say how it is drawn:

```python
path = driver.draw_path_between("subtotal", "total")
{node.name: sorted(node.marks) for node in path.nodes}
# -> {'free_above': ['context'], 'price': ['context'], 'quantity': ['context'], 'subtotal': ['path'], 'shipping': ['path'], 'tax': ['path'], 'total': ['path']}
```

`context=False` draws the path alone. A name the graph doesn't have is a `ValueError` naming every such name, and two
nodes with no path between them are a `ValueError` too:

```python
driver.draw_path_between("total", "subtotal")
# raises ValueError: no path from 'total' to 'subtotal'
```

## A run

`draw_execution` takes what `execute` takes and draws what it would compute: the nodes it runs and the inputs it
reads, the requested outputs marked `output` and each override marked `override`. Nothing only an override needed is
drawn:

```python
run = driver.draw_execution(
    ["total"], inputs={"price": 9.5, "quantity": 2}, overrides={"shipping": 0.0}
)
{node.name: sorted(node.marks) for node in run.nodes}
# -> {'price': [], 'quantity': [], 'subtotal': [], 'shipping': ['override'], 'tax': [], 'total': ['output']}
```

It refuses what `validate` refuses, every problem in one `ValueError`, so a drawing never shows a run that can't
happen. `validate=False` draws as if every required input were supplied:

```python
driver.draw_execution(["total"])
# raises ValueError: missing inputs: 'price' (needed by 'subtotal'); 'quantity' (needed by 'subtotal')
[node.name for node in driver.draw_execution(["total"], validate=False).nodes][:3]
# -> ['price', 'quantity', 'subtotal']
```

The async driver draws the same drawings.

## What a label shows

Every `draw*` method, on both drivers, takes four display options. `show_types=False` leaves each type out of its
label. `show_tags` names tags whose values are printed under the type, as `name=value`, in the order named, a list
value joined by commas. `show_schema=True` adds a `field: type` line for each field of a return type that is a
`TypedDict`, a dataclass or a `NamedTuple`, in declaration order, read from its annotations. `hide_inputs=True` leaves
the inputs out, all but one requested as an output. This module returns a `TypedDict`:

```python
# file: orders.py
from typing import TypedDict

from fnloom import tag


class Totals(TypedDict):
    net: float
    tax: float


@tag(owner=["sales", "finance"], stage="raw")
def order_total(price: float, quantity: int) -> Totals:
    """The order's net and tax."""
    net = price * quantity
    return {"net": net, "tax": net * 0.2}
```

```python
import orders

orders_driver = fnloom.build(orders)
shown = orders_driver.draw(show_tags=("owner",), show_schema=True, hide_inputs=True)
[node.name for node in shown.nodes]
# -> ['order_total']
shown.to_mermaid().splitlines()[1]
# -> '  n0["order_total<br>orders.Totals<br>owner=sales, finance<br>net: float<br>tax: float"]'
orders_driver.draw(show_types=False, hide_inputs=True).to_mermaid().splitlines()[1]
# -> '  n0["order_total"]'
```

`fnloom draw` takes them as `--no-types`, `--show-tag NAME` (repeated for more), `--show-schema` and `--hide-inputs`.

## Styling

`drawing.styled(graph=, node=, edge=, style=)` returns a new drawing with styles added, which `to_dot()`,
`to_mermaid()` and `to_svg()` all draw. Each style is a `fnloom.Style`: `fill`, `stroke` and `font_color` are colours,
`stroke_width` a number, `dashed` a `bool`, and `shape` one of `Style.SHAPES` (`"box"`, `"rounded"`, `"ellipse"`,
`"circle"`, `"diamond"`, `"hexagon"`, `"cylinder"`, `"parallelogram"`). `graph` styles the whole drawing, `node` every
node and `edge` every edge. `style` is a function called once per node, in drawing order, with its
[`NodeView`](running.md#inspecting-a-graph) (its tags, config conditions, namespace and parameters) and its
`DrawnNode`, returning a `Style` or `None`:

```python
from fnloom import Style


def variants(view, drawn):
    """Gold for a node a config condition chose."""
    return Style(fill="gold") if view.config else None


styled = driver.draw().styled(
    node=Style(stroke="navy"), edge=Style(stroke="gray", dashed=True), style=variants
)
[line for line in styled.to_dot().splitlines() if line.startswith('  "tax" [')]
# -> ['  "tax" [label="tax\\nfloat\\nregion == \'eu\'" fillcolor="#ffd700" color="#000080"]']
styled.to_mermaid().splitlines()[-2:]
# -> ['  style n5 fill:#ffd700,stroke:#000080', '  style n6 stroke:#000080']
```

Later wins: a node's kind and marks, then `node`, then `style(...)`; an edge's `edge`, then a path's red. Calling
`styled` again adds to what an earlier call gave, field by field.

A colour is `"#rrggbb"` or a CSS name from the fixed list `Style.COLOURS`, in any case, and every writer draws it as
the same `#rrggbb`. Anything else is a `ValueError` naming the attribute, and the node when a style function made it,
so a typo never draws black:

```python
driver.draw().styled(style=lambda view, drawn: Style(fill="gren"))
# raises ValueError: style for node 'free_above': Style fill 'gren' is not a colour: give '#rrggbb' or a CSS name in Style.COLOURS, such as 'red'
```

`dot={...}` and `mermaid={...}` pass attributes only their writer reads: keys are identifiers (a Mermaid key may hold
`-`, as CSS names do) and values are `str`. A DOT value is quoted and escaped. Mermaid's `style` statement can't quote,
so a Mermaid value holding `,`, `;`, `"`, `{`, `}`, `%` or a line break is a `ValueError`. A graph's style takes
`fill`, `dot` and `mermaid`, and an edge's takes `stroke`, `stroke_width`, `dashed`, `dot` and `mermaid`; any other
field is a `ValueError`. Attributes are written in a fixed order, passthrough keys sorted, so a styled drawing's
text is the same under every `PYTHONHASHSEED`.

```python
boxed = driver.draw_upstream_of("subtotal").styled(
    graph=Style(fill="whitesmoke"), node=Style(shape="box", dot={"peripheries": "2"})
)
boxed.to_dot().splitlines()[1]
# -> '  graph [rankdir=LR bgcolor="#f5f5f5"]'
```

Each `DrawnNode` keeps its `NodeView` as `view`, so a style function can be run again on a drawing; `view` is left out
of equality, `repr` and pickling.

## Mermaid and images

`to_mermaid()` writes the drawing as a Mermaid flowchart, which GitHub and many Markdown tools show as a diagram:

```python
driver.draw_upstream_of("subtotal").to_mermaid().splitlines()[:3]
# -> ['flowchart LR', '  n0["price<br>float"]', '  n1["quantity<br>int"]']
```

`to_svg()` draws it as an SVG image in pure Python, with nothing to install. Each node sits one layer past every node
it reads, the layers placed to keep edges short, and nodes are ordered within a layer to cut crossings and sized from
their text, so no two overlap and every edge points along `orient`. The text is the same on every run.

`render(path)` writes a file in the format its suffix names. A `.dot` or `.mmd` path gets the text, with nothing to
install; any other format, such as `.svg`, `.png` or `.pdf`, is made by Graphviz's `dot` program, which must be on
`PATH`. fnloom depends on no Python package for it. Without Graphviz, `render` raises a `FileNotFoundError` giving
the command that installs it on this platform (`brew install graphviz`, `apt-get install graphviz` or `winget install
graphviz`) and the formats that need nothing. `render(path, engine="builtin")` writes `to_svg()` instead, needing
nothing; it makes `.svg` only. A file never changes engine by itself: only `engine` chooses, and `"dot"` is the
default. In Jupyter, a drawing shows as Graphviz's image when `dot` is installed, and as the builtin SVG otherwise.

```python
import tempfile
from pathlib import Path

with tempfile.TemporaryDirectory() as folder:
    written = driver.draw().render(Path(folder) / "prices.dot")
    text = written.read_text()
    image = driver.draw().render(Path(folder) / "prices.svg", engine="builtin")
    svg = image.read_text()
text == driver.draw().to_dot()
# -> True
svg == driver.draw().to_svg()
# -> True
```

## Exporting

`export()` describes the graph as a JSON-ready document, for a tool to read or for a record of what ran. With
`final_vars`, and `inputs` and `overrides` as `execute` takes them, it describes the run `draw_execution` would draw,
checked the same way. It runs nothing and holds no value: the request lists names, and config values are their `repr`.

```python
import json

document = driver.export(["total"], inputs={"price": 9.5, "quantity": 2})
document["format"], document["config"], document["request"]["outputs"]
# -> ('fnloom.export/1', {'region': "'eu'"}, ['total'])
[node["name"] for node in document["nodes"]]
# -> ['price', 'quantity', 'subtotal', 'shipping', 'tax', 'total']
text = json.dumps(document, sort_keys=True)
```

Each node gives its kind (`"function"` or `"input"`), its whole type, each parameter and what it reads, its tags, doc,
function, config conditions, source, marks and output checks. An input says whether it is required and which nodes read
it:

```python
tax = document["nodes"][4]
tax["kind"], tax["type"], tax["config"], tax["marks"]
# -> ('function', 'float', ["region == 'eu'"], [])
price = document["nodes"][0]
price["kind"], price["required"], price["consumers"], price["version"]
# -> ('input', True, ['subtotal'], None)
```

A function node's `version` is its code version, the one the [result cache](caching.md) keys on. It covers the
function's code, defaults, closures and the globals it reads, helpers and constants included. A function with no code
version, such as one reading a mutable global, has `version` `None` and an `unversioned` entry saying why. A node with
output checks has them in its `version` too, as the result cache keys them, so `fnloom diff` sees a check change, and
so does a variant a [`config.matches`](writing-nodes.md#a-predicate-of-config-keys) predicate selects, its condition
shown as `matches(module.qualname)`. A node that [replaced](writing-nodes.md#replacing-a-node) another module's names
that module as `replaced`.
`graph_version` hashes every node's name, kind, type, wiring, conditions and version, so it changes when any of them
does, and it is `None` when some node is unversioned. The document's `namespace_config` lists each namespace's own
`config=`, and `edge_checks` is `"on"`, `"off"` or the nodes whose edges the build skipped; both are in
`graph_version` too.

A value's text is the same in every process: a set's members are sorted, and an address in a default `repr` is
dropped. A `value()` holding an object known only by its address has no fingerprint, so `graph_version` is `None`.
A code version is the same on every hash seed too: a `set` or `frozenset` a function reads, as a global or a
default, is versioned by its members sorted. Tags
and docs don't change what a node computes, so `graph_version` leaves them out; `fnloom diff` still reports them.

## The CLI

The `fnloom` command, also `python -m fnloom`, draws, exports, validates and compares graphs. It needs nothing but
fnloom. Each module is a dotted name, imported from the current folder as `python -m` would, or a `.py` path, imported
with its own folder on `sys.path` so it can import its siblings. A module inside a package needs its dotted name, for
its relative imports. `MODULE@NS` gives a module's nodes the namespace `NS`. Without `--format`, `-o` writes the format
its suffix names.

`run.toml` below holds a run's request, and `after.json` is an export made later, after the code changed:

```toml
outputs = ["total"]

[config]
region = "eu"

[inputs]
price = 9.5
quantity = 2
```

```bash
fnloom draw prices.py --config region=eu > prices.dot
fnloom draw prices.py --config region=eu --upstream subtotal --format mermaid
fnloom draw prices.py --config region=eu --execution --outputs total \
    --input price=9.5 --input quantity=2 -o run.svg
fnloom validate prices.py --config region=eu --outputs total --input price=9.5
fnloom export prices.py --context run.toml -o before.json
fnloom diff before.json after.json
```

| Option | What it gives |
| --- | --- |
| `--config KEY=VALUE` | a config entry; `VALUE` is JSON, or else a plain string |
| `--input KEY=VALUE`, `--override KEY=VALUE` | an input or an override of a run, read the same way |
| `--outputs a,b` | the outputs of a run |
| `--context FILE` | a `.json` or `.toml` file with the tables `config`, `inputs` and `overrides` and the list `outputs`; a flag wins over its entry |

`--outputs`, `--input` and `--override` describe a run, so `draw` takes them only with `--execution`, and `export`
takes `--input` and `--override` only with `--outputs`; given elsewhere, they are a usage error.

A `@config.when` node is built as `Builder` builds it: without its key in the config it is left out when nothing reads
it, and is otherwise a `ValueError` naming the key (Q57). Give the key with `--config`.

| Command | Does |
| --- | --- |
| `draw` | the whole graph, or `--upstream NODE...`, `--downstream NODE...`, `--path FROM TO` (`--no-context` for the path alone) or `--execution`; `--format dot` (the default) or `mermaid` to stdout, or any format Graphviz makes to `-o FILE` (`--engine builtin` for an SVG needing nothing); `--orient`, `--no-legend`, `--no-validate`, and the display options `--no-types`, `--show-tag NAME`, `--show-schema`, `--hide-inputs` |
| `export` | the graph, or the run `--outputs` makes, as JSON, to stdout or `-o FILE` |
| `validate` | every problem a run of `--outputs` would raise, one per line; status 1 if there is any |
| `diff OLD NEW` | what changed between two exports, as JSON; status 0 for no change, 1 for a change or a node it can't see into, 2 for a file that is no export. When an export has no `graph_version`, `warnings` says which unversioned nodes hide their changes, and each warning is printed on stderr too |

stdout carries only the result. An error goes to stderr as `fnloom: TypeName: message`, with status 1; a command line
that can't be run gets argparse's message and status 2.

Every `KEY=VALUE` is read as JSON first, and as a plain string when it isn't JSON: `--config n=7` is the number 7,
`--config n=007` the string `'007'` (a leading zero isn't JSON), and `--input 'x="1"'` the string `'1'`.
