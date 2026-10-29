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

## Mermaid and images

`to_mermaid()` writes the drawing as a Mermaid flowchart, which GitHub and many Markdown tools show as a diagram:

```python
driver.draw_upstream_of("subtotal").to_mermaid().splitlines()[:3]
# -> ['flowchart LR', '  n0["price<br>float"]', '  n1["quantity<br>int"]']
```

`render(path)` writes a file in the format its suffix names. A `.dot` or `.mmd` path gets the text, with nothing to
install; any other format, such as `.svg`, `.png` or `.pdf`, is made by Graphviz's `dot` program, which must be on
`PATH`. fnloom depends on no Python package for it. Without Graphviz, `render` raises a `FileNotFoundError` saying how
to install it. In Jupyter, a drawing shows as an image when Graphviz is installed, and as its DOT text otherwise.

```python
import tempfile
from pathlib import Path

with tempfile.TemporaryDirectory() as folder:
    written = driver.draw().render(Path(folder) / "prices.dot")
    text = written.read_text()
text == driver.draw().to_dot()
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
function, config conditions, source and marks. An input says whether it is required and which nodes read it:

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
version, such as one reading a mutable global, has `version` `None` and an `unversioned` entry saying why.
`graph_version` hashes every node's name, kind, type, wiring, conditions and version, so it changes when any of them
does, and it is `None` when some node is unversioned.

A value's text is the same in every process: a set's members are sorted, and an address in a default `repr` is
dropped. A `value()` holding an object known only by its address has no fingerprint, so `graph_version` is `None`.
The version has the result cache's limits: a global `frozenset` of strings a function reads is versioned in the order
the hash seed gives, so pass a sorted tuple, or fix `PYTHONHASHSEED`, for a version that holds across processes. Tags
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

A `@config.when` node is built as `Builder` builds it: without its key in the config it is left out when nothing reads
it, and is otherwise a `ValueError` naming the key (Q57). Give the key with `--config`.

| Command | Does |
| --- | --- |
| `draw` | the whole graph, or `--upstream NODE...`, `--downstream NODE...`, `--path FROM TO` (`--no-context` for the path alone) or `--execution`; `--format dot` (the default) or `mermaid` to stdout, or any format Graphviz makes to `-o FILE`; `--orient`, `--no-legend`, `--no-validate` |
| `export` | the graph, or the run `--outputs` makes, as JSON, to stdout or `-o FILE` |
| `validate` | every problem a run of `--outputs` would raise, one per line; status 1 if there is any |
| `diff OLD NEW` | what changed between two exports, as JSON; status 0 for no change, 1 for a change or a node it can't see into, 2 for a file that is no export. When an export has no `graph_version`, `warnings` says which unversioned nodes hide their changes, and each warning is printed on stderr too |

stdout carries only the result. An error goes to stderr as `fnloom: TypeName: message`, with status 1; a command line
that can't be run gets argparse's message and status 2.

Every `KEY=VALUE` is read as JSON first, and as a plain string when it isn't JSON: `--config n=7` is the number 7,
`--config n=007` the string `'007'` (a leading zero isn't JSON), and `--input 'x="1"'` the string `'1'`.
