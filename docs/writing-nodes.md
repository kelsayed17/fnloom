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

## Keeping a function out

A public function that isn't meant to be a node, such as a helper, can be kept out in two ways (Q73).

**`@fnloom.exclude`** marks one function. It works bare or called, `@fnloom.exclude()`, and only records the mark: the
function is returned unchanged, so you can still call it, and it stacks with the other decorators in either order. An
excluded function is never read, so it may omit its annotations. Hamilton's `@hamilton_exclude` raises when called.

```python
# file: pricing.py
import fnloom


@fnloom.exclude
def round_cents(value: float) -> float:
    """A public helper the graph doesn't need to know about."""
    return round(value, 2)


def price(cost: float) -> float:
    return round_cents(cost * 1.2)
```

```python
import fnloom
import pricing

fnloom.build(pricing).node_names()
# -> ['cost', 'price']
pricing.round_cents(1.239)
# -> 1.24
```

**`__all__`** limits a module to the functions it names. Hamilton ignores `__all__`; fnloom honours it, which is a
breaking change for a module that defines one and relied on the rest becoming nodes. Names it lists that aren't
functions the module defines, such as re-exports and constants, are ignored, and listing a private function doesn't
make it a node. `@fnloom.exclude` wins when a function is both listed and marked.

```python
# file: public_api.py
import fnloom

__all__ = ["revenue", "margin"]


def revenue(units: int) -> int:
    return units * 10


def margin(revenue: int) -> int:
    return revenue // 4


def scratch(units: int) -> int:
    """Not listed: not a node."""
    return units
```

```python
import fnloom
import public_api

fnloom.build(public_api).node_names()
# -> ['margin', 'revenue', 'units']
```

`__all__` is read as the attribute names a module defines, so a `@fnloom.config.when` variant `price__eu` has to be
listed as `price__eu` to be considered at all. An `__all__` that is not a list or tuple of `str` is a `TypeError` at
build, naming every such module.

When something reads the name of a function that was left out, it's a missing input, and the error adds a line saying
why the name isn't a node:

```python
# file: reports.py
import fnloom

__all__ = ["summary"]


def discount(rate: float) -> float:
    return rate / 2


def summary(discount: float) -> float:
    return discount
```

```python
import fnloom
import reports

try:
    fnloom.build(reports).execute(["summary"])
except ValueError as error:
    hint = str(error).splitlines()[-1]
hint
# -> "'discount' is a function in module 'reports' not listed in its __all__"
```

A function `@fnloom.exclude` marked gets `marked @exclude` in place of `not listed in its __all__`. A module whose
functions are all left out has no nodes, and the build says so, giving each function's reason.

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

## Defaults the build refuses

A default only applies to an input nobody supplies. Two patterns make a default surprising, and Hamilton accepts
both. fnloom refuses them when you build (Q76), each with one `ValueError` that lists every case, sorted.

**A node shadows the default.** A parameter with a default that reads a node, by its own name or through
`source("...")`, takes the node's value. The default is never used, and an input of that name is ignored:

```python
# file: surcharge.py
def fee() -> int:
    return 100


def total(fee: int = 3) -> int:
    """The author meant `3` unless the caller supplied a fee."""
    return fee
```

```python
import fnloom
import surcharge

fnloom.build(surcharge)
# raises ValueError: shadowed default: parameter 'fee' of 'total' has a default, but it reads the node 'fee', which replaces it; rename one, or let the node win with @fnloom.defaults(shadowed=('fee',)) on 'total'
```

A `value()` binding has no edge, so a node of that name doesn't shadow it.

**Two consumers disagree.** Each consumer keeps its own default when the input isn't supplied, but one input then reads
as two different values, and the same one for both when it is supplied:

```python
# file: windows.py
def short_window(days: int = 7) -> int:
    return days


def long_window(days: int = 30) -> int:
    return days
```

```python
import fnloom
import windows

fnloom.build(windows)
# raises ValueError: differing defaults: input 'days' has different defaults in 'long_window' (parameter 'days') = 30, 'short_window' (parameter 'days') = 7; make them equal, or write @fnloom.defaults(differing=(<parameter>,)) on each of these consumers but one
```

Defaults are compared with `==`, and two NaNs are equal. An unclear comparison counts as different: a result that
isn't a `bool` (numpy's scalar bool is one), as numpy arrays give, an error, or two objects without `__eq__` that
aren't the same object. A consumer without a default has
none to differ from.

**Opting out.** When you mean it, say so on the consumer with `@fnloom.defaults`, naming the parameters:

```python
# file: windows_ok.py
import fnloom


def fee() -> int:
    return 100


@fnloom.defaults(shadowed=("fee",))
def total(fee: int = 3) -> int:
    return fee


@fnloom.defaults(differing=("days",))
def short_window(days: int = 7) -> int:
    return days


def long_window(days: int = 30) -> int:
    return days
```

```python
import fnloom
import windows_ok

driver = fnloom.build(windows_ok)
driver.execute(["total", "short_window", "long_window"])
# -> {'total': 100, 'short_window': 7, 'long_window': 30}
driver.execute(["short_window", "long_window"], inputs={"days": 14})
# -> {'short_window': 14, 'long_window': 14}
```

- With `shadowed`, the node wins, as it does in Hamilton.
- With `differing`, each consumer keeps its default. A consumer that opts out leaves the comparison, so naming the input
  on every consumer but one is enough: above, `long_window` is alone.
- `@defaults` only records names, so it stacks with the other decorators in any order and returns the function itself.
  Applying it twice merges the names.
- A name that isn't a parameter with a default is a `ValueError` when the decorator is applied, listing every such name.

## Annotations

Every parameter and every return value needs a type annotation, so each edge in the graph is typed where it's
declared. fnloom then checks edges, inputs and overrides against them; see [Type checks](running.md#type-checks).
Annotate what a parameter really takes. A parameter that may be `None` needs `T | None` (or `Optional[T]`), even with
a default of `None`: `None` passed for `x: int` is refused (Q79). A fixed-length tuple, `tuple[int, str]`, is checked
by its length and each item (Q80).

Annotations are evaluated when the graph is built, as in Hamilton. String annotations
(`from __future__ import annotations`) and forward references to a class further down the module are fine.

An annotation naming something that doesn't exist at run time, such as a class imported only under
`if TYPE_CHECKING:` (as ruff's `TC003` rule suggests), still builds. Hamilton raises `NameError` instead (Q81). The
annotation becomes an opaque type, shown as `unresolved 'Decimal'`, with its text as written:

```python
# file: rates.py
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from decimal import Decimal


def rate() -> "Decimal":
    """The exchange rate."""
    raise NotImplementedError


def converted(rate: "Decimal") -> "Decimal":
    """Fed by `rate`: the same unresolved text on both ends."""
    return rate


def checked(amount: "Decimal") -> "Decimal":
    """Reads an input whose type didn't resolve."""
    return amount
```

```python
import fnloom
import rates

driver = fnloom.build(rates)
repr(driver.nodes["rate"].type)
# -> "unresolved 'Decimal'"
driver.execute(["checked"], inputs={"amount": 1})
# raises TypeError: amount: cannot check a value against unresolved 'Decimal': it did not resolve when the graph was built; import what it names at run time, not only under TYPE_CHECKING
```

It surfaces only where a check needs it:

- **Edges:** an edge matches it only from or to an annotation of the same text, or `Any`. Any other edge with it fails
  the build, naming the text.
- **Values:** an input or override checked against it raises a `TypeError` saying to import the name at run time. A
  node never asked for is never checked, and `without_input_type_checks()` skips the check. An input two nodes share
  is checked as its merged type, the narrower of their annotations, so an unresolved one wins over `Any` and is
  checked even for the `Any` consumer.

!!! warning "Keep annotation imports real"

    An unresolved annotation can't check a value. If a node's inputs should be checked, import the names its
    annotations use at run time, not only under `TYPE_CHECKING`.

## Checking results

By default a node's result isn't checked against its return annotation, as in Hamilton: the build checks that each
edge's types agree, and a run checks inputs and overrides, but a node that returns the wrong type isn't caught until
something downstream fails on it. `Builder.with_output_type_checks()` checks every result (Q87):

```python
# file: scores.py
def score(raw: int) -> float:
    """The raw score, as a float."""
    return raw


def best(scores: list[float]) -> float | None:
    """The highest score, or None for no scores."""
    return max(scores, default=None)
```

```python
import scores

fnloom.build(scores).execute(["score"], inputs={"raw": 3})
# -> {'score': 3}
checked = fnloom.Builder().with_modules(scores).with_output_type_checks()
checked.build().execute(["score"], inputs={"raw": 3})
# raises TypeError: score: returned int, expected float
checked.with_numeric_widening().build().execute(["score"], inputs={"raw": 3})
# -> {'score': 3}
checked.build().execute(["best"], inputs={"scores": []})
# -> {'best': None}
```

A result is checked by the same rules as an input: `None` only for a type that admits it (Q79), a fixed-length tuple
by its length and items (Q80), `NewType`s and generics such as `Callable[[int], int]` or `frozenset[str]` by value,
and with [deep checks and numeric widening](running.md#deep-checks-and-numeric-widening) when they're on. A mismatch
is a `TypeError` raised as the node's own failure, so it carries the node's `in node 'score'` note, and a
[hook](hooks.md)'s `after_node` gets `ok=False` with that error. A node that raises keeps its own error: its missing
result is never checked. An `async def` node's awaited result is what's checked. On an `AsyncDriver`, a sync node
that returns a coroutine still gets the driver's own error, saying to make it an `async def`.

The checks are made at build, around only the nodes that need one. A node returning `Any` or `object` gets no check
and costs nothing, and without the setting no node is touched. An extracted field is checked either way
([Extracting fields](#extracting-fields)); with the setting, its producer's result is checked too. A value loaded from
a [cache](caching.md) isn't checked again. Hamilton's `FunctionInputOutputTypeChecker` does the same job, but crashes
on fixed tuples, parameterized generics and `NewType`s, and reports a failing node as returning `None`.

A [custom value check](running.md#custom-value-checks) applies to results too.

## Checking values

`@fnloom.check_output` checks a node's value after its function returns, with built-in checks given as keywords. A
value that fails is the node's own failure, a `fnloom.ValidationError`, which is a `ValueError` (Q99, Q100):

```python
# file: readings.py
from fnloom import check_output


@check_output(range=(0, 100))
def humidity(raw: float) -> float:
    """Relative humidity, in percent."""
    return raw * 100


@check_output(values_in=["ok", "stale"], importance="warn")
def status(age: int) -> str:
    """Whether the reading is fresh."""
    return "ok" if age < 60 else "expired"
```

```python
import readings
from fnloom import ValidationError

sensors = fnloom.build(readings)
sensors.execute(["humidity"], inputs={"raw": 0.4})
# -> {'humidity': 40.0}
sensors.execute(["humidity"], inputs={"raw": 1.2})
# raises ValidationError: humidity: check range failed: 120.0 is not in [0, 100]
```

The node keeps its name and its place in the graph: no node is added, unlike Hamilton's `a_raw` and validator nodes.
`importance="warn"` logs one WARNING on the `fnloom` logger for each failed check in a run, and the run goes on:

```python
sensors.execute(["status"], inputs={"age": 90})
# -> {'status': 'expired'}
```

| Keyword | Passes when |
| --- | --- |
| `range=(lo, hi)` | the value, or every item, is between `lo` and `hi`, inclusive |
| `values_in=[...]` | the value, or every item, is one of these |
| `data_type=T` | `fnloom.value_matches(T, value)`, so `list[int]` and `int \| None` work |
| `allow_none=False` | the value isn't `None` |
| `contains=`, `does_not_contain=` | a `str` value contains, or doesn't contain, each `str` given |
| `allow_nans=False`, `max_fraction_nans=f` | no item, or at most that fraction of items, is NaN |

`range`, `values_in` and the NaN checks read a scalar, or each item of a value that iterates: a list, a numpy array or
a pandas Series or DataFrame, the last ones through their own vectorised methods, with no import from fnloom. A `str`,
`bytes` or mapping is one value. A one-shot iterator is never read, since that would consume it: a node annotated
`Iterator` is refused at build, and one met at run is a `TypeError`. A check that can't read the return annotation,
such as `range` on a `str`, is a `TypeError` at build, naming the node (Q101).

`@fnloom.check_output_custom` takes checks of your own. A function takes the value, and an `inputs` parameter if it
wants the node's arguments, and returns `True` or `None` for a good value, or `False` or a message for a bad one. A
`fnloom.Validator` subclass does the same in `validate(value, inputs)`, and may refuse a return annotation at build
with `applies_to` (Q102):

```python
# file: deliveries.py
from fnloom import check_output_custom


def _no_more_than_ordered(value: int, inputs) -> str | None:
    if value > inputs["ordered"]:
        return f"shipped {value}, more than the {inputs['ordered']} ordered"
    return None


@check_output_custom(_no_more_than_ordered)
def shipped(ordered: int, packed: int) -> int:
    """What leaves the warehouse."""
    return packed
```

```python
import deliveries

fnloom.build(deliveries).execute(["shipped"], inputs={"ordered": 3, "packed": 5})
# raises ValidationError: shipped: check _no_more_than_ordered failed: shipped 5, more than the 3 ordered
```

Checks stack in any number and in any order, and run in the order written, top first. A check is on the function's
own value; `field="p"` puts it on an `@extract_fields` or `@extract_columns` field instead, whatever the decorator
order. Every `@parameterize` row and `@config.when` variant is checked. An overridden node doesn't run, so it isn't
checked, and a [cache](caching.md) hit isn't checked again. The checks are part of the node's cache key, so adding a
stricter check makes the next run compute the value afresh (Q103). A [hook](hooks.md)'s `after_node` gets every
check's result as `NodeEnd.checks`.

Hamilton's pandas statistics checks are a few lines as a custom check:

```python
def _mean_in_range(series) -> bool:
    return 0 <= series.mean() <= 10
```

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

## Namespaces

`Builder.with_modules(*modules, namespace="eu")` names each node of those modules `eu.<name>`, so one module can be
used more than once in a graph (Q74). A namespace is a dotted path of identifiers, such as `"eu"` or `"shop.eu"`:

```python
# file: regional.py
def net(gross: float, tax_rate: float) -> float:
    """The price without tax."""
    return gross / (1 + tax_rate)


def tax_rate() -> float:
    """This region's tax rate, 20%."""
    return 0.2
```

```python
# file: totals.py
def tax_rate() -> float:
    """The default tax rate, 10%."""
    return 0.1


def both(eu_net: float, us_net: float) -> tuple[float, float]:
    """Each region's net price."""
    return (eu_net, us_net)
```

Inside a namespace, a parameter reads the namespace's own node when one of its modules defines that name, and
otherwise the plain name: a node given without a namespace, or an input. Here `eu.net` reads `eu.tax_rate`, which
shadows the plain `tax_rate` for readers inside `eu` only. An input is never namespaced, so both copies of `net` read
the one input `gross`:

```python
import fnloom
import regional
import totals

driver = (
    fnloom.Builder()
    .with_modules(totals)
    .with_modules(regional, namespace="eu")
    .with_modules(regional, namespace="us")
    .build()
)
driver.node_names()
# -> ['both', 'eu.net', 'eu.tax_rate', 'eu_net', 'gross', 'tax_rate', 'us.net', 'us.tax_rate', 'us_net']
driver.nodes["eu.net"].dependencies
# -> ('eu.tax_rate', 'gross')
driver.nodes["eu.net"].namespace
# -> 'eu'
list(driver.inputs())
# -> ['eu_net', 'gross', 'us_net']
driver.execute(["eu.net", "tax_rate"], inputs={"gross": 12.0})
# -> {'eu.net': 10.0, 'tax_rate': 0.1}
```

A node outside reads a namespaced node by its full name, with `source(...)` in a `@parameterize` row or
`fnloom.node(fn, bind=...)`, since a parameter name can't hold a dot:

```python
import fnloom
import regional
import totals

driver = (
    fnloom.Builder()
    .with_modules(regional, namespace="eu")
    .with_modules(regional, namespace="us")
    .with_functions(
        fnloom.node(
            totals.both,
            bind={"eu_net": fnloom.source("eu.net"), "us_net": fnloom.source("us.net")},
        )
    )
    .build()
)
driver.execute(["both"], inputs={"gross": 12.0})
# -> {'both': (10.0, 10.0)}
```

A few details:

- A parameter can't read another namespace's node: a parameter `net` in namespace `us` reads `us.net` or the plain
  `net`, never `eu.net`. Naming the full name in `source("eu.net")` reads that node from another namespace too, unless
  the namespace it's written in has a node of its own named `eu.net` (in full, `us.eu.net`), which it reads instead.
- Every name a namespaced module makes is prefixed: `@config` variants, `@parameterize` rows and extracted fields. Each
  namespace selects its own `@config` variant, and the errors, `validate`, `inputs()`, `outputs()`,
  `what_is_the_path_between` and suggestions all use full names.
- The same module under the same namespace twice counts once. Under two namespaces, the same function makes a node in
  each, so passing the function in `final_vars` is a `TypeError` naming both nodes: pass a name instead.
- Names clash as they do without namespaces, and nothing overrides: a `@parameterize` row named `"eu.net"` in a module
  given without a namespace is a build error naming both definitions. Hamilton's `allow_module_overrides()` silently
  keeps the later module's function instead.
- Cache keys use the full node name, and each function keeps one code version. `with_functions` takes no
  `namespace=`.

## Graphs without modules

`Builder.with_functions` builds from functions directly, with no module to scan (Q72). Each function makes the nodes it
would make in a module, named by its `__name__`, and keeps its decorators, so `@tag`, `@parameterize`, `@config.when*`,
`@extract_fields` and `@cache` all work:

```python
# file: scoring.py
import fnloom


def raw(points: int) -> int:
    return points * 10


@fnloom.tag(owner="games")
def bonus(raw: int) -> int:
    return raw + 5


def _scaled(raw: int, factor: int) -> int:
    return raw * factor
```

```python
import sys

import fnloom
import scoring

modules_before = set(sys.modules)
driver = fnloom.Builder().with_functions(scoring.raw, scoring.bonus).build()
driver.execute(["bonus"], inputs={"points": 2})
# -> {'bonus': 25}
driver.nodes["bonus"].module
# -> 'scoring'
driver.modules
# -> ()
set(sys.modules) == modules_before
# -> True
```

Nothing is copied and no module is made: each node is the function itself, so its module, cache key and code version
are the function's own, and the function in `final_vars` means its node. Hamilton's `create_temporary_module` copies
the functions into a module it registers in `sys.modules`.

**`fnloom.node(fn, *, name=None, bind=None)`** makes one node from a function, named and bound without decorating it.
`bind` takes `source(...)` and `value(...)`, as a `@parameterize` row does, and is checked the same way. One function
can make several nodes, each from its own `node(...)`, and a function given on purpose may have a private name:

```python
import fnloom
import scoring

driver = (
    fnloom.Builder()
    .with_functions(
        scoring.raw,
        fnloom.node(scoring._scaled, name="doubled", bind={"factor": fnloom.value(2)}),
        fnloom.node(scoring._scaled, name="tripled", bind={"factor": fnloom.value(3)}),
    )
    .build()
)
driver.execute(["doubled", "tripled"], inputs={"points": 1})
# -> {'doubled': 20, 'tripled': 30}
```

A node's name must be an identifier, so a dotted name such as `"ns.score"` is refused. A lambda has no name a node
can take, so it needs `name=`:

```python
import fnloom

fnloom.Builder().with_functions(lambda points: points)
# raises ValueError: function '<lambda>' has no name a node can take: give it one with fnloom.node(fn, name=...)
```

A few details:

- `with_functions` is cumulative, like `with_modules`, and mixes with it: nodes are matched by name across both, and a
  name defined twice, in a module and among the functions, is the usual duplicate error. The same function counts
  once, whether it's given twice or also found in a module given to the builder.
- Only a Python function is accepted. For a `functools.partial`, give the function with `bind=`; wrap a callable
  instance in a `def`.
- A function marked `@exclude` is refused rather than dropped, since you gave it on purpose. A module's `__all__` only
  decides what that module's scan finds, so it doesn't apply.
- A driver built this way pickles when each function is importable by its module and qualified name: defined at the
  top of a module, or in a class there. A local function or a lambda is a `TypeError` naming it when pickled.
  Loading finds each function again by that name, as pickle does: if the name now holds a different function, that
  one is used, and if it holds nothing or something else, loading raises `TypeError: cannot load function 'm.f': …`.
  A driver that is already running keeps the functions it was built with.

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

### Docs for each row

A row's node takes the function's docstring as a template, filled in per row with `str.format_map`: `{output_name}` is
the row's name, and each parameter the row binds is a source's name or a value's own value, so a format spec such as
`{limit:.1f}` works. A `group(...)` parameter shows its items joined: `x, 3` for a list, `k=x, j=3` for a dict. Write a
literal brace as `{{` or `}}`. A function without a docstring gives `None` for every row.

A placeholder the row lacks, a positional field such as `{}` or `{0}`, or a stray brace is a `ValueError` naming the row
and the placeholder, when the decorator is applied. Hamilton prints a missing placeholder as the bare key (Q82).

To give a row its own doc, write the row as `(bindings, doc)`. The doc is used as written, never templated, and `None`
means no doc at all, not the function's. A tuple of any other length, or a doc that is not a `str`, is a `TypeError`.

```python
# file: limits.py
from fnloom import parameterize, source, value


@parameterize(
    low={"limit": value(10)},
    high={"limit": source("high_limit")},
    custom=({"limit": value(50)}, "Written as is: {limit} stays braced."),
    quiet=({"limit": value(99)}, None),
)
def above(readings: list[int], limit: int) -> int:
    """{output_name}: readings over {limit}."""
    return sum(reading > limit for reading in readings)
```

```python
import fnloom
import limits

driver = fnloom.build(limits)
{name: driver.nodes[name].doc for name in ["low", "high", "custom", "quiet"]}
# -> {'low': 'low: readings over 10.', 'high': 'high: readings over high_limit.', 'custom': 'Written as is: {limit} stays braced.', 'quiet': None}
```

```python
from fnloom import parameterize, value


def bad(limit: int) -> int:
    """Over {limit}, or {ceiling}."""
    return limit


parameterize(low={"limit": value(10)})(bad)
# raises ValueError: the docstring of 'bad' is a template for each row's doc: row 'low': placeholder {ceiling} is not 'output_name' or a parameter the row binds; write a literal brace as {{ or }}, or give the row its own doc with (bindings, doc)
```

### Lists and dicts: `group`

`group(...)` binds one parameter to a list or a dict built from several sources and values. Positional items make a list,
for a parameter annotated `list[X]`; keywords make a dict, for one annotated `dict[str, X]`. `Optional` of either is
fine.

```python
# file: bundles.py
from fnloom import group, parameterize, source, value


@parameterize(
    scores={"parts": group(source("math"), value(10), source("art"))},
    named={"marks": group(math=source("math"), bonus=value(10))},
)
def bundle(parts: list[int] = [], marks: dict[str, int] = {}) -> object:
    return parts or marks
```

```python
import fnloom
import bundles

driver = fnloom.build(bundles)
driver.execute(["scores", "named"], inputs={"math": 7, "art": 9})
# -> {'scores': [7, 10, 9], 'named': {'math': 7, 'bonus': 10}}
driver.nodes["math"].type is int
# -> True
```

- **Each call builds a fresh list or dict.** A `value(...)` item is the same object every time, as `value()` is
  everywhere.
- **Each `source` is an ordinary input,** or node, typed by the element: `list[int]` makes it an `int`. It is
  required, even when the parameter has a default, since a group is never partly built; Hamilton makes it optional and
  then fails with a `KeyError` when one is missing.
- **`NodeView.params`** lists one `Param` per item, with the item's place in `slot`: its index in a list, or its key
  in a dict.
- **Misuse is refused when decorating,** each offender listed once, in one message:
    - a parameter not annotated as `list[X]` or `dict[str, X]`. A string annotation, as `from __future__ import
      annotations` makes every one, is read then too. One that is only a name defined further down the module, quoted
      or not, is checked at build instead, once it resolves, with the same message. A name that never resolves, such
      as one imported only under `TYPE_CHECKING`, is refused at build, since its shape can't be read (Q81); inside
      `list[...]` or `dict[str, ...]` it is fine, and each item is typed `unresolved`;
    - positional and keyword items mixed, an empty `group()`, a nested `group`, or a bare string.
- `fnloom.node(fn, bind=...)` takes a `group(...)` as a `@parameterize` row does.

### Shorthands

`parameterize_sources` is `@parameterize` with every binding a `source`, and `parameterize_values` is one parameter
bound to a constant per node, each with its own doc:

```python
# file: shorthands.py
from fnloom import parameterize_sources, parameterize_values


@parameterize_sources(west=dict(rate="west_rate"), east=dict(rate="east_rate"))
def price(rate: float, base: float = 100.0) -> float:
    """{output_name} price at {rate}."""
    return base * rate


@parameterize_values("tax", {("low_tax", "A low tax."): 0.1, ("high_tax", None): 0.3})
def taxed(tax: float, base: float = 100.0) -> float:
    return base * (1 + tax)
```

```python
import fnloom
import shorthands

driver = fnloom.build(shorthands)
driver.execute(["west", "high_tax"], inputs={"west_rate": 1.5})
# -> {'west': 150.0, 'high_tax': 130.0}
driver.nodes["west"].doc
# -> 'west price at west_rate.'
driver.nodes["low_tax"].doc
# -> 'A low tax.'
```

- **`parameterize_sources`** takes a `str` for each source, refused otherwise when decorating; Hamilton fails later with
  a raw `TypeError`. No row may be empty.
- **`parameterize_values`** takes a mapping of `(name, doc)` keys to values; a doc may be `None`, and a name may appear
  once. An empty mapping is refused; Hamilton builds no node.
- All three compose with `@config`, `@extract_fields` (one row only), `@tag`, `@cache`, `@defaults` and namespaces, and
  each row is checked as a `@parameterize` row is.

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
- `Driver.config` is a read-only, shallow copy of the config, and a pickled driver keeps it. Shallow means a list
  value you change after `with_config` shows the change there too.
- `NodeView.config` holds the conditions that selected a node, each a `fnloom.Condition`.

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

The same holds for a name only an unselected variant gives: a `@parameterize` row, or a field it extracts. It's never
made an input. Reading it fails the build with `no selected variant gives 'r2': ...`, naming the variants that do.
An `@extract_fields()` that names no fields gives its `TypedDict`'s keys, as far as its return annotation evaluates
at build.

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
