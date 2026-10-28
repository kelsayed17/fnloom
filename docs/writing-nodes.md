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

## Config

`@config.when` and its relatives make one node out of several **variants**, picking one when you build. Name each
variant after its node, followed by `__` and anything you like: `cost__post` and `cost__courier` are both variants of
`cost`. The name is cut at the last `__`, and only on a config-decorated function, so a plain `cost__total` stays
`cost__total`. Pass `name=` to choose the node name yourself.

| Decorator | The variant is used when |
| --- | --- |
| `@config.when(carrier="post")` | every key equals its value |
| `@config.when_not(carrier="post")` | every key differs from its value |
| `@config.when_in(carrier=["courier", "express"])` | every key is one of its values |
| `@config.when_not_in(carrier=["post"])` | every key is none of its values |
| `@config.default` | no other variant of its node matches |

```python
# file: shipping.py
from fnloom import config


@config.when(carrier="post")
def cost__post(weight: float) -> float:
    """A flat fee plus the weight."""
    return 2.0 + weight


@config.when_in(carrier=["courier", "express"])
def cost__courier(weight: float) -> float:
    """A higher fee, and twice the weight."""
    return 5.0 + 2 * weight


@config.default
def cost__free() -> float:
    """Free shipping when no carrier matches."""
    return 0.0


def total(price: float, cost: float) -> float:
    """What the customer pays."""
    return price + cost
```

`Builder.with_config` gives the config. Each call merges into a copy, and a key given again takes its later value:

```python
import fnloom
import shipping

builder = fnloom.Builder().with_modules(shipping)
post = builder.with_config({"carrier": "post"}).build()
post.execute(["total"], inputs={"price": 10.0, "weight": 1.0})
# -> {'total': 13.0}
express = builder.with_config({"carrier": "express"}).build()
express.execute(["total"], inputs={"price": 10.0, "weight": 1.0})
# -> {'total': 17.0}
fnloom.build(shipping).execute(["total"], inputs={"price": 10.0})
# -> {'total': 10.0}
dict(post.config)
# -> {'carrier': 'post'}
post.nodes["cost"].origin is shipping.cost__post
# -> True
[condition.key for condition in post.nodes["cost"].config]
# -> ['carrier']
```

- **Config shapes the graph, and nothing else.** A config key is never a node, an input or an output, and its value
  is never passed to a function. A parameter named like a config key is an ordinary input you supply in `inputs=`.
  Hamilton passes config values to parameters (Q56).
- **A key the config doesn't have never matches,** whichever form tests it. Hamilton reads a missing key as `None`, so
  `when_not(carrier="post")` matches an empty config there (Q57).
- **Values compare with `==`,** so `when(level=1)` matches a config of `{"level": True}`. An `==` that returns
  anything but a `bool`, as a NumPy array's does, is a `TypeError` naming the key; NumPy's boolean scalar
  counts as a `bool`.
- **Stacked decorators all apply:** `@config.when(carrier="post")` over `@config.when_not(region="eu")` needs both.
  The order you stack them in, and their order with `@parameterize` and `@tag`, never matters.
- `@parameterize` under a config decorator gives its rows only when the variant is selected.
- `Driver.config` is a read-only copy of the config, and a pickled driver keeps it.

A node that no variant matches, with no `@config.default`, is left out. That's fine when nothing reads it, and
requesting it says why it's missing. When another node reads it, the build fails, naming what each variant needs:

```python
# file: tiers.py
from fnloom import config


@config.when(tier="gold")
def discount__gold() -> float:
    """Gold customers save a fifth."""
    return 0.2


def price(list_price: float, discount: float) -> float:
    """The list price after the discount."""
    return list_price * (1 - discount)
```

```python
import tiers

fnloom.Builder().with_config({"tier": "basic"}).with_modules(tiers).build()
# raises ValueError: no variant of 'discount' matches the config {'tier': 'basic'}; its variants need tier == 'gold' (discount__gold); add @config.default or a variant for it
```

Mistakes are refused rather than guessed at (Q58, Q59):

- Two variants of one node that both match, or two `@config.default`s for one node, fail the build naming both.
- A config-decorated function defined twice in one module fails the build, naming the lines: Python would keep only
  the last one. A function bound to a second name is still one variant.
- `@config.when` without parentheses, a `str` given to `when_in`, a positional string, a name ending in `__` and a
  `when` with no conditions are refused when the decorator is applied.
- `name=` is always the node name. To test a config key that's literally called `name`, pass a mapping:
  `@config.when({"name": "x"})`.

The decorators never change your function, and add no tags (Q60).

## Extracting fields

`@extract_fields` makes a node of each field of the mapping a function returns. The function stays a node of its own
name, returning the whole mapping, and each field node reads it. Give the fields a type each, in a mapping:

```python
# file: stats.py
from typing import TypedDict

from fnloom import extract_fields


@extract_fields({"low": int, "high": int})
def bounds(readings: list[int]) -> dict:
    """The smallest and largest reading."""
    return {"low": min(readings), "high": max(readings)}


class Summary(TypedDict):
    count: int
    label: str


@extract_fields()
def summary(readings: list[int]) -> Summary:
    """Every field of a TypedDict return is a node."""
    return {"count": len(readings), "label": "readings"}


@extract_fields("mean", fill_with=0.0)
def averages(readings: list[int]) -> dict[str, float]:
    """A named field takes its type from the return: here, float."""
    return {"mean": sum(readings) / len(readings)} if readings else {}


def spread(low: int, high: int) -> int:
    """Field nodes feed other nodes like any node."""
    return high - low
```

```python
import stats

driver = fnloom.build(stats)
driver.node_names()
# -> ['averages', 'bounds', 'count', 'high', 'label', 'low', 'mean', 'readings', 'spread', 'summary']
driver.execute(["spread", "count", "mean"], inputs={"readings": [3, 9, 6]})
# -> {'spread': 6, 'count': 3, 'mean': 6.0}
driver.execute(["mean", "averages"], inputs={"readings": []})
# -> {'mean': 0.0, 'averages': {}}
driver.nodes["mean"].type
# -> float
```

- **Three forms.** A mapping of names to types; names alone, typed from a `dict[str, T]`, `Mapping[str, T]` or
  `TypedDict` return; or no arguments, for every field of a `TypedDict` return. A mapping and names together are
  refused, as is a name the `TypedDict` doesn't have (Q61).
- **Fields are checked on every run** against their types, with the same check as inputs, even when you build
  `without_input_type_checks()`. A wrong type is a `ValueError` naming the producer, the field and both types, and a
  missing field is a `ValueError` naming the fields the producer did return. A declared type the return annotation
  can't feed fails the build (Q61).
- **`fill_with`** is the value of a missing field. Only that field's node sees it: the producer's mapping is never
  changed, and `fill_with=None` fills with `None` (Q62).
- `@tag` tags the producer and every field node (Q63). A field node shares the producer's docstring and config.
- `@parameterize` with one row works, and the producer takes the row's name. Two rows are refused when decorating.

```python
# file: halves.py
from fnloom import extract_fields


@extract_fields({"left": int})
def split(number: int) -> dict:
    """Returns the wrong type for `left`."""
    return {"left": str(number // 2)}
```

```python
import halves

fnloom.build(halves).execute(["left"], inputs={"number": 8})
# raises ValueError: 'split' returned field 'left' as str, but it is declared int
```

## Extracting columns

`@extract_columns` makes a node of each named column of the value a function returns: a pandas or polars
DataFrame, a mapping, or any value that supports `in` and `[]`. No extension needs loading, and fnloom never imports
pandas or polars itself. Name a column, or give `(name, doc)` to set that node's docstring:

```python
# file: table.py
from fnloom import extract_columns


@extract_columns("price", ("volume", "Shares traded."), fill_with=0)
def bars(symbol: str) -> dict[str, list[float]]:
    """Columns of bars, keyed by name."""
    return {"price": [10.0, 10.5], "spread": [0.1, 0.2]}


def notional(price: list[float], volume: object) -> float:
    """Column nodes feed other nodes like any node."""
    return sum(price) * (volume or 1)
```

```python
import table

driver = fnloom.build(table)
driver.node_names()
# -> ['bars', 'notional', 'price', 'symbol', 'volume']
driver.execute(["price", "volume", "notional"], inputs={"symbol": "X"})
# -> {'price': [10.0, 10.5], 'volume': 0, 'notional': 20.5}
driver.nodes["volume"].doc
# -> 'Shares traded.'
driver.nodes["price"].doc
# -> 'Columns of bars, keyed by name.'
```

- **A column's type comes from the return annotation.** A `pandas.DataFrame` return gives `pandas.Series` columns, a
  `polars.DataFrame` gives `polars.Series`, and anything else gives `typing.Any`. Each value is checked against its
  type on every run, so a name the frame holds twice, which reads a DataFrame, is a `ValueError` (Q64).
- **A missing column** is a `ValueError` when run, naming the producer and the columns it returned, sorted.
- **`fill_with`** is the value of a missing column, and never changes the producer's value. For a pandas frame it is
  a full `Series` on the frame's index, for a polars frame a `Series` as long as the frame, and for a mapping the
  value itself. Any other type with a missing column is a `TypeError` naming it. A `(name, doc)` column fills by
  `name` (Q64).
- **Refused when decorating:** no columns, a list, and a column name given more than once, in any form (Q64). Put `*`
  in front of a list: `extract_columns(*names)`.
- `@tag` tags the producer and every column node (Q63). `@config` and one `@parameterize` row work as they do with
  `@extract_fields`.

```python
# file: gaps.py
from fnloom import extract_columns


@extract_columns("close")
def quotes() -> dict[str, list[float]]:
    """Has no `close` column."""
    return {"open": [1.0], "high": [2.0]}
```

```python
import gaps

fnloom.build(gaps).execute(["close"])
# raises ValueError: 'quotes' returned no column 'close'; it returned 'high', 'open'
```

## What is refused

fnloom calls a node with one keyword argument per parameter, and expects it to return its value. Functions that can't
be called that way are refused when you build, each with a `ValueError` naming the function:

| Refused | Why |
| --- | --- |
| A positional-only parameter (`def f(a, /)`) | It cannot be passed by keyword. |
| `*args` or `**kwargs` | Neither binds one named argument as a parameter. |
| A missing parameter or return annotation | Every edge is typed where it is declared. |
| A generator function, sync or async, and an `async def` given to `build()` | Calling one returns a generator or a coroutine, not the node's value. [`build_async()`](running.md#async) awaits an `async def`. |
| A dependency cycle, including a function reading itself | A cycle has no order to run in; the error names the cycle. |

Hamilton silently accepts the first, second and fourth, and overflows the stack on a cycle. See
[the differences](quirk-register.md) (Q27, Q29, Q32).
