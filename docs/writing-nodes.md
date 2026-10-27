# Writing nodes

fnloom wires a graph by name, just as Apache Hamilton does: **a function's name is its node, and its parameter names
are its edges.** There's nothing to register, and a plain function needs no decorator. This page covers the few rules
that decide what becomes a node.

## Which functions are nodes

`fnloom.build(*modules)` scans each module and makes a node of every function that is:

- **public**, meaning its name doesn't start with `_`;
- **defined in that module**. Imported functions are left out, so `from math import floor` doesn't add a node called
  `floor`.

```python
# file: orders.py
from math import floor


def _round_down(value: float) -> int:
    """A private helper: not a node."""
    return floor(value)


def units(quantity: float) -> int:
    """Whole units ordered."""
    return _round_down(quantity)
```

```python
import fnloom
import orders

fnloom.build(orders).node_names()
# -> ['quantity', 'units']
```

## Inputs

A parameter that doesn't name any function becomes an **input** node, whose value comes from the caller. Above,
`quantity` is an input. Inputs show up in `node_names()` next to the functions, and you supply them by name:

```python
fnloom.build(orders).execute(["units"], inputs={"quantity": 7.9})
# -> {'units': 7}
```

## Optional inputs

A parameter with a default is **optional**. If the caller doesn't supply it, the function is called without it, so
its default applies:

```python
# file: greeting.py
def greeting(name: str, punctuation: str = "!") -> str:
    """Hello, with the punctuation the caller chose."""
    return f"Hello, {name}{punctuation}"
```

```python
import fnloom
import greeting

driver = fnloom.build(greeting)
driver.execute(["greeting"], inputs={"name": "Ada"})
# -> {'greeting': 'Hello, Ada!'}
driver.execute(["greeting"], inputs={"name": "Ada", "punctuation": "."})
# -> {'greeting': 'Hello, Ada.'}
```

!!! note

    A default only makes an edge optional when the parameter reads an input. If a parameter with a default names
    another function, it still reads that function's value.

## Annotations

Every parameter and every return value needs a type annotation, so each edge in the graph is typed where it's
declared. fnloom then checks edges, inputs and overrides against them; see [Type checks](running.md#type-checks).

Annotations are evaluated when the graph is built, as in Hamilton. String annotations
(`from __future__ import annotations`) and forward references to a class further down the module are fine.

!!! warning "Keep annotation imports real"

    Every name in an annotation must exist at run time. If you move an import under `if TYPE_CHECKING:` (as ruff's
    `TC003` rule suggests), the build raises that name's `NameError`, with a note naming the node and the parameter.

## Modules together

`fnloom.build` takes several modules, and so does `Builder.with_modules`. Nodes are matched by name across all of
them, so a function in one module can read a node defined in another:

```python
# file: sizes.py
def doubled(base: int) -> int:
    """Twice the base size."""
    return base * 2
```

```python
# file: report.py
def message(doubled: int) -> str:
    """The doubled size, as text."""
    return f"size {doubled}"
```

```python
import fnloom
import report
import sizes

driver = fnloom.Builder().with_modules(sizes).with_modules(report).build()
driver.execute(["message"], inputs={"base": 21})
# -> {'message': 'size 42'}
```

A few details:

- `with_modules` returns a new builder instead of changing the one you called it on, so you can share and extend a
  builder safely.
- Passing the same module twice adds its nodes once.
- Defining the same name in two different modules is a `ValueError` naming both.

## One function, several nodes

`@parameterize` turns one function into several nodes, one per row. Each keyword names a node, and its mapping binds
some of the function's parameters:

- `source("x")` reads the parameter from the node or input `x`;
- `value(v)` passes the constant `v`.

The function itself isn't a node, unless a row takes its name.

```python
# file: thresholds.py
from fnloom import parameterize, source, value


@parameterize(
    above_low={"limit": value(10)},
    above_high={"limit": source("high_limit")},
)
def above(readings: list[int], limit: int) -> int:
    """How many readings exceed the limit."""
    return sum(reading > limit for reading in readings)
```

```python
import fnloom
import thresholds

driver = fnloom.build(thresholds)
driver.node_names()
# -> ['above_high', 'above_low', 'high_limit', 'readings']
driver.execute(
    ["above_low", "above_high"],
    inputs={"readings": [5, 12, 30], "high_limit": 20},
)
# -> {'above_low': 2, 'above_high': 1}
```

- A parameter a row does not mention reads the node of its own name, as in a plain function: `readings` above.
- Rows are ordinary nodes, so one row can `source()` another, including a row of the same function.
- A parameter with a default that a row binds to `source("x")` makes `x` optional: when `x` is not supplied, the
  function is called without that parameter and its default applies. Hamilton raises `KeyError` there (Q35).
- A `value()` is passed as the object itself on every call, never a copy, so a node that changes a bound list
  changes it for the next run too.
- A row naming a parameter the function does not have is a `ValueError` when the decorator is applied (Q33).

The decorator returns the function unchanged, so you can still call it directly, and type checkers still see its
signature.

## What is refused

fnloom calls a node with one keyword argument per parameter, and expects it to return its value. Functions that can't
be called that way are refused when you build, each with a `ValueError` naming the function:

| Refused | Why |
| --- | --- |
| A positional-only parameter (`def f(a, /)`) | It cannot be passed by keyword. |
| `*args` or `**kwargs` | Neither binds one named argument as a parameter. |
| A missing parameter or return annotation | Every edge is typed where it is declared. |
| `async def`, or a generator function | Calling one returns a coroutine or generator, not the node's value. |
| A dependency cycle, including a function reading itself | A cycle has no order to run in; the error names the cycle. |

Hamilton silently accepts the first, second and fourth, and overflows the stack on a cycle. See
[the differences](quirk-register.md) (Q27, Q29, Q32).
