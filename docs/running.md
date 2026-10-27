# Running a graph

The examples on this page use one module:

```python
# file: pricing.py
def total(price: float, quantity: int) -> float:
    """What the line costs before any discount."""
    return price * quantity


def discounted(total: float, discount: float = 0.0) -> float:
    """The total after the discount, if one is given."""
    return total * (1 - discount)


def label(discounted: float, currency: str = "EUR") -> str:
    """The price as shown to a customer."""
    return f"{discounted:.2f} {currency}"
```

```python
import fnloom
import pricing

driver = fnloom.build(pricing)
```

## Execute

`execute(final_vars, *, inputs=None, overrides=None)` computes the nodes named in `final_vars` and returns a dict with
exactly those, in the order you asked for them, each once:

```python
driver.execute(["label", "total"], inputs={"price": 2.5, "quantity": 4})
# -> {'label': '10.00 EUR', 'total': 10.0}
```

Only the nodes the outputs need run. Every required input on the way must be supplied, and every name you pass must
be a node of the graph: a misspelled input is an error, not a value that is silently ignored.

## Overrides

`overrides` supplies a node's value in place of computing it, and cuts off everything behind it. Overriding `total`
means `price` and `quantity` are no longer needed:

```python
driver.execute(["label"], overrides={"total": 100.0})
# -> {'label': '100.00 EUR'}
```

An override may name any node, a function or an input. It beats a supplied input of the same name, and it satisfies
a required input that was not supplied.

## Prepared plans

Each `execute` call compiles a plan for its request: which nodes run, in what order, and when each value can be
released. `prepare` does that once and returns the plan, which runs as often as you like:

```python
plan = driver.prepare(["label"], inputs={"price": 0.0, "quantity": 0})
plan.run(inputs={"price": 1.5, "quantity": 2})
# -> {'label': '3.00 EUR'}
plan.run(inputs={"price": 4.0, "quantity": 3})
# -> {'label': '12.00 EUR'}
```

A plan is compiled for its outputs and for the **names** of its inputs and overrides, not their values, so the values
passed to `prepare` are placeholders. `run` must be given the same names, or it raises a `ValueError` saying how they
differ.

`execute` keeps the plans it compiles, keyed by the outputs and the input and override names, so repeated calls of the
same shape skip compiling. It keeps the 128 most recently used.

## Type checks

Types are checked at two points, against the annotations the build evaluated.

**Edges, at build.** Every parameter fed by another function's node must accept that node's return type. A mismatch
fails the build, with one `ValueError` listing every such parameter, sorted:

```python
# file: mismatched.py
def count() -> str:
    """Returns text by mistake."""
    return "3"


def doubled(count: int) -> int:
    """Twice the count."""
    return count * 2
```

```python
import mismatched

fnloom.build(mismatched)
# raises ValueError: doubled.count requires int but count returns str
```

A node's return type feeds a parameter when it is the same type or a subclass, when either side is `Any`, when every
member of a returned union is accepted, or when one member of a parameter's union accepts it. A generic matches by
its kind, and by its arguments when both sides name them.

**Inputs and overrides, on each run.** Each input that the plan reads is checked against the input's type, and each
override against the overridden node's return type, before any node runs. The values that do not fit are listed in
one `ValueError`, sorted, each as `<name>: expected <type>, got <type of the value>`:

```python
driver.execute(["total"], inputs={"price": "2.5", "quantity": 4})
# raises ValueError: price: expected float, got str
```

`None` fits every type. An input supplied but not read by the plan is not checked. Which checks a plan needs is
worked out once, when it is prepared, so a run only checks the values.

**What the checks understand.** Classes, including your own and their subclasses; `Any`; `object`; unions and
`Optional`; `Literal`; `Annotated[T, ...]`, checked as `T`; `NewType`, checked as its supertype; `TypeVar`, which
takes any value and, on an edge, matches only itself; runtime-checkable protocols, checked with `isinstance`; and the
collections `list`, `list[T]`, `dict[K, V]`, `tuple[T, ...]`, `Sequence` and `Sequence[T]`. A value of exactly the
collection's own class is checked by its class alone, so `["a"]` fits `list[int]`; another value of an abstract
collection such as `Sequence[int]` is checked by its kind and its first element only. An iterator is checked by its
kind alone and is never read.

Other forms are unsupported and, as written, refuse every value but `None`: for example `Mapping[str, int]` given a
`dict`, `Callable[..., T]` given a function, a protocol that is not runtime-checkable, and a PEP 695 alias
(`type Price = float`, which takes no `float` and feeds only a parameter annotated with the same alias). Annotate with
a supported form, or build with `Builder.without_input_type_checks()`:

```python
lenient = fnloom.Builder().with_modules(pricing).without_input_type_checks().build()
lenient.execute(["total"], inputs={"price": 2, "quantity": 4})
# -> {'total': 8}
```

The opt-out turns off the input and override checks only. Edge checks stay on: they need no values, so they cost
nothing per run, and a graph whose edges disagree is a bug whatever is supplied.

## Execution order

Nodes run in topological order, and nodes that could run in either order run in order of their names. The order does
not depend on the order of `final_vars` or of a function's parameters, so side effects and logs come out the same way
on every run.

## Lineage

```python
driver.node_names()
# -> ['currency', 'discount', 'discounted', 'label', 'price', 'quantity', 'total']
driver.what_is_upstream_of("discounted")
# -> ['discount', 'discounted', 'price', 'quantity', 'total']
driver.what_is_downstream_of("total")
# -> ['discounted', 'label', 'total']
```

Each query returns sorted names, and includes the nodes you named. `node_names()` lists every node, inputs included.

## Inspecting a graph

`driver.nodes` maps every node name, inputs included, to a read-only `NodeView`, in name order:

```python
list(driver.nodes)
# -> ['currency', 'discount', 'discounted', 'label', 'price', 'quantity', 'total']
view = driver.nodes["discounted"]
view.type
# -> float
view.dependencies
# -> ('discount', 'total')
view.is_input
# -> False
view.doc
# -> 'The total after the discount, if one is given.'
[(p.name, p.required) for p in view.params]
# -> [('total', True), ('discount', False)]
```

`origin` is the function the node came from and `module` its module's name; `tags` are the ones written with `@tag`.
An input has no function: its `origin`, `module` and `doc` are `None`, its `params` are empty, and its `type` is the
annotation of the parameters that read it:

```python
driver.nodes["quantity"].type
# -> int
driver.nodes["quantity"].origin is None
# -> True
```

The view is built once, when the graph is built. Annotations are evaluated then too, so a module written with
`from __future__ import annotations` gives the same types as one without it, and an annotation may name a class
defined further down the module. A name that does not resolve, such as one imported only under `TYPE_CHECKING`, fails
the build with its `NameError`, with a note naming the node and the parameter. When the functions reading one input
annotate it with different types, the build raises a `ValueError` naming the input and each function.

## Exceptions from your functions

When a node's function raises, the run stops and the exception propagates as itself, with a note naming the node:

```python
# file: ratios.py
def ratio(numerator: int, denominator: int) -> float:
    """The numerator over the denominator."""
    return numerator / denominator
```

```python
import ratios

broken = fnloom.build(ratios)
broken.execute(["ratio"], inputs={"numerator": 1, "denominator": 0})
# raises ZeroDivisionError: division by zero
```

Python prints the note, `in node 'ratio'`, under the traceback; it is also in the exception's `__notes__`.
