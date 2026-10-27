# Writing nodes

fnloom wires a graph by name, as Hamilton does: **a function's name is its node, and its parameter names are its
edges.** There is nothing to register, and a plain function needs no decorator.

## Which functions are nodes

`fnloom.build(*modules)` scans each module and makes a node of every function that is

- **public**: its name does not start with `_`;
- **defined in that module**: a function imported from elsewhere is left out, so `from math import floor` adds no node
  called `floor`.

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

A parameter that names no function becomes an **input** node: its value comes from the caller. Above, `quantity` is
an input. Inputs appear in `node_names()` alongside the functions, and you supply them by name:

```python
fnloom.build(orders).execute(["units"], inputs={"quantity": 7.9})
# -> {'units': 7}
```

## Optional inputs

A parameter with a default is **optional**. When the caller does not supply it, the function is called without it
and its default applies:

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

A default makes the edge optional only when it reads an input. A parameter with a default that names another function
still reads that function's value.

## Annotations

Every parameter and the return value need a type annotation, so a graph's edges are typed where they are declared.
The annotations are evaluated when the graph is built, as Hamilton does: string annotations
(`from __future__ import annotations`) and forward references to a class defined further down the module are fine,
but every name must exist at run time. An import moved under `if TYPE_CHECKING:` (as ruff's `TC003` rule suggests)
makes the build raise that name's `NameError`, with a note naming the node and the parameter. fnloom does not check
values against the annotations yet; that comes with the type-checking plan.

## Modules together

`fnloom.build` takes several modules, and so does `Builder.with_modules`. A function in one module can read a node
defined in another, since nodes are matched by name across all of them:

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

`with_modules` returns a new builder rather than changing the one it was called on, so a builder can be shared and
extended. Passing the same module twice adds its nodes once. The same name defined in two different modules is a
`ValueError` naming both.

## One function, several nodes

`@parameterize` makes one node per row from a single function. Each keyword names a node, and its mapping binds some
of the function's parameters: `source("x")` reads the parameter from node or input `x`, and `value(v)` passes the
constant `v`. The function itself is not a node, unless a row takes its name.

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

The decorator returns the function unchanged, so it can still be called directly and keeps its signature for type
checkers.

## What is refused

A node is called with one keyword argument per parameter and must return its value. Functions that cannot be called
that way are refused when you build, each with a `ValueError` naming the function:

| Refused | Why |
| --- | --- |
| A positional-only parameter (`def f(a, /)`) | It cannot be passed by keyword. |
| `*args` or `**kwargs` | Neither binds one named argument as a parameter. |
| A missing parameter or return annotation | Every edge is typed where it is declared. |
| `async def`, or a generator function | Calling one returns a coroutine or generator, not the node's value. |
| A dependency cycle, including a function reading itself | A cycle has no order to run in; the error names the cycle. |

Hamilton accepts the first, second and fourth silently and overflows the stack on a cycle; see
[the differences](quirk-register.md) (Q27, Q29, Q32).
