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
`"RL"` or `"BT"`), and `legend=False` leaves out the box naming each kind of node drawn:

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
