# Running a graph

Everything about calling a graph: inputs, overrides, prepared plans, type checks, lineage and inspection. The
examples on this page all use one module:

```python
# file: shipping.py
def total(weight: float, quantity: int) -> float:
    """What the parcel weighs before any trim."""
    return weight * quantity


def trimmed(total: float, trim: float = 0.0) -> float:
    """The total after the trim, if one is given."""
    return total * (1 - trim)


def label(trimmed: float, unit: str = "kg") -> str:
    """The weight as printed on the parcel."""
    return f"{trimmed:.2f} {unit}"
```

```python
import fnloom
import shipping

driver = fnloom.build(shipping)
```

## Execute

`execute(final_vars, *, inputs=None, overrides=None)` computes the nodes named in `final_vars`. It returns a dict with
exactly those, each once, in the order you asked for them:

```python
driver.execute(["label", "total"], inputs={"weight": 2.5, "quantity": 4})
# -> {'label': '10.00 kg', 'total': 10.0}
```

You can also pass a function instead of its name. It means the node the function makes in this driver, and the
result is still keyed by the node's name:

```python
driver.execute([shipping.label], inputs={"weight": 2.5, "quantity": 4})
# -> {'label': '10.00 kg'}
```

fnloom matches the function itself, never its name. An `@extract_fields` function means its producer, not its fields.
A function that makes several nodes raises a `TypeError` listing them, so pass their names, unless one of them is
named like the function, as an alias or a `@parameterize` row can be: that one is its node. A function that makes
none, such as one from another module, an unselected `@config` variant or a lambda, raises a `ValueError`. A request
lists every such problem at once.

Only the nodes your outputs need will run. You must supply every required input on the way, and every name you pass
must be a node in the graph. A misspelled input is an error, never a value that's quietly ignored.

## Overrides

`overrides` supplies a node's value instead of computing it, and cuts off everything upstream of it. Override
`total`, and `weight` and `quantity` are no longer needed:

```python
driver.execute(["label"], overrides={"total": 100.0})
# -> {'label': '100.00 kg'}
```

An override can name any node, whether a function or an input. It wins over a supplied input of the same name, and
it fills in for a required input you didn't supply.

An override of a node that [may be absent](writing-nodes.md#declining) may be `fnloom.ABSENT`, which is how a test forces
its consumers' skip. For any other node, and for every input, `ABSENT` is a `TypeError` naming it, with or without type
checks: leave an input out to say it has no value.

### Strict overrides

Hamilton refuses an override of an input that you haven't also supplied, because it only cuts off what is upstream of
an overridden node. To get that rule, build with `with_strict_overrides()`. The refusal names every such input,
sorted, with the nodes that need it. It is settled when the request is prepared, never on a run of a prepared plan:

```python
strict = fnloom.Builder().with_modules(shipping).with_strict_overrides().build()
strict.execute(
    ["total"], inputs={"weight": 2.0, "quantity": 3}, overrides={"weight": 5.0}
)
# -> {'total': 15.0}
strict.execute(["total"], inputs={"quantity": 3}, overrides={"weight": 5.0})
# raises ValueError: override of input 'weight' refused: not supplied, though needed by 'total' (supply it in inputs= too; strict overrides are on)
```

An input that no node the request computes needs, such as an output asked for itself, is never refused. `validate`
reports it as a line too.

### Declared inputs

`with_declared_inputs(*names)` makes the build fail for any input of the graph that you haven't declared, and for any
declared name that isn't an input. A misspelled parameter, or a misspelled `source("...")`, is otherwise a new input
that nothing supplies. Each culprit gets the closest node or declared name, and every one is listed in one sorted
message. Calling it again adds names:

```python
declared = (
    fnloom.Builder().with_modules(shipping).with_declared_inputs("weight", "quantty")
)
try:
    declared.build()
except ValueError as error:
    lines = str(error).splitlines()
lines
# -> ["declared name 'quantty' is not an input of the graph (did you mean 'quantity'?)", "undeclared input 'quantity' (did you mean 'quantty'?)", "undeclared input 'trim'", "undeclared input 'unit'"]
```

## Prepared plans

Each `execute` call compiles a plan for its request: which nodes run, in what order, and when each value can be
freed. `prepare` does that work once and hands you the plan, which you can run as often as you like. This is the
fastest way to run the same graph in a loop:

```python
plan = driver.prepare(["label"], inputs={"weight": 0.0, "quantity": 0})
plan.run(inputs={"weight": 1.5, "quantity": 2})
# -> {'label': '3.00 kg'}
plan.run(inputs={"weight": 4.0, "quantity": 3})
# -> {'label': '12.00 kg'}
```

A plan is compiled for its outputs and for the **names** of its inputs and overrides, not their values. The values
you pass to `prepare` are just placeholders. `run` must get the same names, or it raises a `ValueError` saying how
they differ.

!!! tip "`execute` caches plans too"

    `execute` keeps the 128 most recently used plans, keyed by the outputs and the input and override names. Repeated
    calls of the same shape skip compiling, so `prepare` mostly saves you the cache lookup.

## Result builders

`execute` returns a dict. `Builder.with_result_builder(build)` sets a function, or any callable, that gets that dict
and makes what `execute` returns instead:

```python
def pairs(outputs: dict[str, object]) -> list[str]:
    return [f"{name}={value}" for name, value in outputs.items()]


listed = fnloom.Builder().with_modules(shipping).with_result_builder(pairs).build()
listed.execute(["total", "weight"], inputs={"weight": 2.5, "quantity": 4})
# -> ['total=10.0', 'weight=2.5']
```

- **What it gets.** Each requested output once, in the order you asked for it, with the inputs and overrides you
  requested as outputs, as Hamilton's result builder gets them (Q19). An output that is
  [absent](writing-nodes.md#declining) is `fnloom.ABSENT`, as in the dict; `frame()` refuses it, naming it, since a
  column can't be absent.
- **Where it applies.** `PreparedPlan.run` builds too, and so do the async driver's `execute` and `run`. Hamilton's
  async driver silently drops a `ResultBuilder` subclass.
- **Typed.** `Builder`, `Driver`, `AsyncDriver` and their plans take the built type as a parameter, so a type checker
  sees `execute` return `list[str]` here, and `dict[str, object]` with no builder.
- **One per builder.** A second `with_result_builder` is a `ValueError` naming both (Q24). Hamilton raises at `build()`.
- **A builder that raises** fails the run with its own error. [Hooks](hooks.md) still get the raw dict, as
  `RunEnd.results`, with `ok=False` and that error.
- **It travels with the driver**, as hooks do: through `copy`, `rebuild()`, `build_async` and pickling. It pickles by
  Python's own rules, so a lambda makes the driver fail to pickle.

`raw_execute(final_vars, *, inputs=None, overrides=None)` and `PreparedPlan.run_raw(*, inputs=None, overrides=None)`
skip the builder and return the dict. Every hook fires, as for `execute`. On an `AsyncDriver` both are coroutines.

```python
listed.raw_execute(["total"], inputs={"weight": 2.5, "quantity": 4})
# -> {'total': 10.0}
```

### pandas and polars frames

`fnloom.frame()` is a result builder that makes one pandas frame of the outputs, as Hamilton's
`PandasDataFrameResult` does, but refuses to pad (Q144). fnloom needs neither library: `frame` imports the one you ask
for when you call it, and `import fnloom` imports neither.

```python
# file: harvest.py
import pandas as pd


def yields() -> pd.Series:
    return pd.Series([1.0, 2.0, 3.0])


def plots() -> pd.Series:
    return pd.Series([1, 2, 3])


def losses() -> pd.Series:
    return pd.Series([1, 0])


def regions() -> pd.DataFrame:
    return pd.DataFrame({"north": [1, 2, 3], "south": [4, 5, 6]})


def moisture() -> float:
    return 0.2


def crop(yields: pd.Series, plots: pd.Series) -> pd.Series:
    return yields * plots
```

```python
import harvest

builder = fnloom.Builder().with_modules(harvest)
frames = builder.with_result_builder(fnloom.frame()).build()
frames.execute(["crop", "regions", "moisture"]).to_dict("list")
# -> {'crop': [1.0, 4.0, 9.0], 'regions.north': [1, 2, 3], 'regions.south': [4, 5, 6], 'moisture': [0.2, 0.2, 0.2]}
```

`frame(library="pandas", *, flatten=True, separator=".", join="strict")`:

- **Columns.** A frame output gives one column per column, named `<output><separator><column>`, in the order you
  asked for the outputs and then the frame's column order. A lone frame is flattened too, where Hamilton returns it as
  it is. `flatten=False` keeps a frame's own column names. A series or a list is one column, and a scalar fills its
  column. Outputs that are all scalars make one row.
- **`join="strict"`**, the default, refuses outputs on different indexes, where pandas would pad the gaps with NaN
  and turn integers into floats. The `ValueError` names each output and its rows, or the first row whose labels
  differ. Nothing is logged, where Hamilton only logs a warning.
- **`join="outer"`** aligns the outputs on the union of their indexes: Hamilton's frame, without the log.
- **Two columns of one name**, such as an output `regions.north` beside `regions`, are a `ValueError` naming both.
  A dict output is aligned on its keys, so strict compares them as an index.
- **`frame("polars")`** makes a polars frame by the same rules. polars has no index, so outputs of different lengths
  are refused, and so is `join="outer"`. A pandas series given to it is taken by position, its index dropped.
- **It travels with the driver**, as any result builder does, and compares and pickles by its settings. It is a
  `fnloom.FrameBuilder`, for annotations.

```python
frames.execute(["plots", "losses"])
# raises ValueError: frame(join='strict') needs every output on one index: plots has 3 rows, losses has 2 rows; pass join='outer' to align them
```

The frame class itself still works, and builds by the library's own rules (Q89):

```python
import pandas

plain = builder.with_result_builder(pandas.DataFrame).build()
plain.execute(["plots", "losses"])["losses"].isna().tolist()  # NaN-padded
# -> [False, False, True]
```

`with_result_builder(polars.DataFrame)` is the polars form. Neither flattens a frame output, and pandas needs an index
for outputs that are all scalars.

## Validate

`validate` checks a request the way `execute` would, but runs nothing. It returns every problem as one line, sorted,
and an empty list when the request would run:

```python
driver.validate(["label"], inputs={"weight": 1.5, "quantity": 2})
# -> []
driver.validate(
    ["labl", "total"], inputs={"weight": "heavy", "quantity": 2, "quantty": 3}
)
# -> ["unknown inputs: 'quantty' (did you mean 'quantity'?)", "unknown outputs: 'labl' (did you mean 'label'?)", 'weight: expected float, got str']
driver.validate(["total"], inputs={"weight": 1.5, "quantty": 3})
# -> ["missing inputs: 'quantity' (needed by 'total') (did you mean 'quantty'?)", "unknown inputs: 'quantty' (did you mean 'quantity'?)"]
```

Each line is the line `execute` raises for that problem on its own, so `validate` reports what `execute` would, all
at once instead of one kind at a time. It covers unknown outputs, inputs and overrides; missing inputs; the types of
input and override values; and outputs no `@config.when*` variant gave. A missing input names only the nodes this
request needs it for, and a function in `final_vars` that no node comes from gets the line `execute` raises for it.
A malformed request still raises a `TypeError`, as does `inputs` or `overrides` that isn't a mapping:

```python
driver.validate("label")
# raises TypeError: final_vars must be a sequence of names, not a single str
```

`prepare` only reads the names of the inputs, so it accepts values that `validate` reports; the checks run when the
plan runs. `AsyncDriver.validate` is the same method, and isn't a coroutine.

## Type checks

fnloom checks types at two points, against your annotations:

- **edges**, once, when you build;
- **inputs and overrides**, on every run, before any node runs.

### Edges, at build

Every parameter fed by another function must accept that function's return type. A mismatch fails the build, with one
`ValueError` listing every such parameter, sorted:

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

A return type feeds a parameter when:

- it's the same type or a subclass;
- either side is `Any`, except that a `TypeVar` producer matches only the same `TypeVar`;
- it's a union and the parameter accepts every member, or the parameter is a union and one member accepts it;
- it's a generic of the same kind, with matching arguments when both sides name them.

!!! warning "Edges you might expect to pass, but that don't"

    These are refused, just as Hamilton refuses them (for the protocol, Hamilton raises a `TypeError` instead, Q42):

    | Producer returns | Parameter wants | Fix: annotate the producer as |
    | --- | --- | --- |
    | `int` | `float` | `-> float` |
    | `Callable[[int], int]` | `Callable[..., int]` | `-> Callable[..., int]` |
    | `tuple[int, int]` | `tuple[int, ...]` | `-> tuple[int, ...]` |
    | a class | a plain (not `runtime_checkable`) protocol it conforms to | the protocol, or mark it `@runtime_checkable` |

    Edge checks can't be turned off: a graph whose edges disagree is a bug, whatever you supply.

### Inputs and overrides, on each run

Before any node runs, each input the plan reads is checked against the input's type, and each override against the
overridden node's return type. Every value that doesn't fit is listed in one `ValueError`, sorted, each as
`<name>: expected <type>, got <type of the value>`:

```python
driver.execute(["total"], inputs={"weight": "2.5", "quantity": 4})
# raises ValueError: weight: expected float, got str
```

A few rules worth knowing:

- `None` fits only a type that admits it: `Optional[T]`, `T | None`, `object`, `Any`, `None` itself, and a `TypeVar`
  that is free or whose bound or a constraint admits it. Hamilton lets `None` through for every type, so the node
  then fails on its own (Q79). A default of `None` doesn't make a parameter optional: annotate it `T | None` to pass
  `None`.
- An input you supply but the plan never reads isn't checked. Every override is, even one for a node the plan never
  reaches.
- The checks a plan needs are worked out once, when it's prepared, so a run only checks values. Checking against a
  plain class like `int` costs one `isinstance` call.

```python
driver.execute(["total"], inputs={"weight": None, "quantity": 4})
# raises ValueError: weight: expected float, got NoneType
```

### What the checks understand

| Annotation | How a value is checked |
| --- | --- |
| A class (yours or built-in), `object` | `isinstance`, so subclasses fit |
| `Any` | Anything fits |
| Unions, `Optional`, `Literal` | Against each member or value |
| `Annotated[T, ...]` | As `T`; metadata never changes the answer |
| `NewType` | As its supertype |
| `TypeVar` | Anything but `None` fits; `None` fits a free one, or one whose bound or a constraint admits it. On an edge it matches only itself |
| A `runtime_checkable` protocol | `isinstance` |
| A PEP 695 alias (`type Weight = float`) | As what it names |
| `list`, `dict`, `tuple` and other plain classes | `isinstance` |
| `tuple[int, str]`, `tuple[()]` and other fixed-length tuples | A tuple of that length whose items each fit, nested tuples included; `tuple[()]` takes only `()` (Q80) |
| `list[T]`, `tuple[T, ...]`, `Sequence[T]`, `set[T]`, `Iterable[T]` | A value of exactly the declared class (a `list` for `list[T]`) by class only, so `["a"]` fits `list[int]`. Any other value by kind, plus its first element. Every item with [deep checks](#deep-checks-and-numeric-widening) |
| `dict[K, V]` | By class only: the value's class must be exactly `dict`. Every key and value with deep checks |
| An annotation that didn't resolve at build, such as `Decimal` imported only under `TYPE_CHECKING` | No value fits: a `TypeError` saying to import it at run time (Q81). See [Annotations](writing-nodes.md#annotations) |
| Other `collections.abc` generics (`Mapping[str, int]`, `Callable[[int], int]`, ...) | By kind only, so any `dict` fits `Mapping[str, int]` |
| An iterator | By kind only. It's never read, so no items are lost |

A generic alias such as `Pair[int]`, for `type Pair[T] = tuple[T, T]`, is read as `tuple[int, int]` when its value
uses its type parameters in the order it declares them. One that reorders them is compared as written. On an edge, an
alias inside another generic's arguments is compared as written too, so a node returning `list[float]` doesn't feed a
`list[Weight]` parameter.

Both checks are public: `fnloom.value_matches(declared, value)` answers as the input check, and
`fnloom.type_matches(produced, expected)` as the edge check.

```python
fnloom.value_matches(tuple[int, str], (1, "a")), fnloom.value_matches(int, None)
# -> (True, False)
fnloom.type_matches(int, int | None)
# -> True
```

### Deep checks and numeric widening

Two opt-ins make the value checks stricter or looser. Both apply to inputs, overrides, extracted fields and
[`validate`](#validate), and both are carried by `build_async`, pickling and copying. Edge checks compare types, not
values, so neither changes them.

```python
# file: boxes.py
def box_total(weights: list[float]) -> float:
    """The sum of every weight in the box."""
    return sum(weights)
```

`Builder.with_deep_type_checks()` checks every item of a `list`, `set`, `tuple[T, ...]` or other standard
collection, every key and value of a `dict` or other mapping, and the items of nested collections in turn, however deep,
without recursion. An iterator is still never read (Q43). By default a `list` passes `list[float]` on its class alone,
as in Hamilton:

```python
import boxes

fnloom.build(boxes).validate(["box_total"], inputs={"weights": [1.5, "2"]})
# -> []
deep = fnloom.Builder().with_modules(boxes).with_deep_type_checks().build()
deep.validate(["box_total"], inputs={"weights": [1.5, "2"]})
# -> ['weights: expected list[float], got list (an item does not fit)']
```

A deep check reads every item on every run, so it costs time in proportion to the value: about 2.6 ms for a list of
100,000 floats (see [Benchmarks](benchmarks.md)). An item whose type is a plain class costs one `isinstance` call. Only
a value of a standard class (`list`, `dict`, `set`, `tuple`, `deque`, ...) has its items read: any other value, such
as an iterable class of your own, keeps the default checks, so an endless one is never read to its end. A subclass of
a standard collection, such as a `list` subclass of your own, is checked by the default rules too. A value of any size
or depth is read in full. A check that fits reads each part the value shares, such as one list held in two places,
once, on a cycle or not, never once per path. A check that fails may read shared parts more than once. A check gives
up only when more than 10,000 of its questions are open about one value at once, which only a cycle no check can see,
such as a type made afresh on each reading, reaches.

`Builder.with_numeric_widening()` lets an `int` (but not a `bool`) and a numpy integer or floating scalar fit
`float`, and a numpy integer scalar fit `int`. Hamilton refuses them all, as fnloom does by default. numpy is
recognised from the value's class and never imported. Widening applies wherever a check reads a value: the value
itself, a fixed tuple's items and the first item a default check reads. Every item of a collection is read only with
deep checks:

```python
widening = fnloom.Builder().with_modules(shipping).with_numeric_widening().build()
widening.execute(["total"], inputs={"weight": 2, "quantity": 4})
# -> {'total': 8}
both = (
    fnloom.Builder()
    .with_modules(boxes)
    .with_deep_type_checks()
    .with_numeric_widening()
    .build()
)
both.execute(["box_total"], inputs={"weights": [1, 2.5]})
# -> {'box_total': 3.5}
```

`fnloom.value_matches(declared, value, deep=True, widen=True)` answers as a driver with both settings.

### Custom value checks

`Builder.with_value_check(check)` admits values the default check refuses. `check(declared, value)` gets the declared
type and the value, and admits the value by returning a true value. Checks are cumulative: they're tried in the order
given, until one admits the value. One that returns `False` refuses nothing the default admits, unlike Hamilton's
`validate_input`, which replaces the default check (Q21):

```python
import decimal


def decimal_floats(declared: object, value: object) -> bool:
    """A Decimal may stand for a float."""
    return declared is float and isinstance(value, decimal.Decimal)


exact = fnloom.Builder().with_modules(shipping).with_value_check(decimal_floats).build()
exact.execute(["total"], inputs={"weight": decimal.Decimal("2.5"), "quantity": 4})
# -> {'total': decimal.Decimal('10.0')}
exact.validate(["total"], inputs={"weight": "2.5", "quantity": 4})
# -> ['weight: expected float, got str']
```

A check is called only for a value the default refuses, so a value that fits costs nothing more. It applies to
inputs, overrides, extracted fields, [results](writing-nodes.md#checking-results) and `validate`, but never to
edges, which compare types, not values (Q10). An annotation that didn't resolve at build (Q81) is passed as its
`unresolved` type, and still raises its `TypeError` unless a check admits the value. A check that raises isn't
caught: its exception fails the run as it is or, checking a result, fails the node. It pickles by Python's own
rules, as a [hook](hooks.md) does.

### Unsupported annotations

Anything else refuses every value. Two examples are a plain protocol (one not marked
`runtime_checkable`), and a `dict` subclass, such as an `OrderedDict`, for `dict[str, int]`.
Either annotate with a supported form, or build with `Builder.without_input_type_checks()`:

```python
lenient = fnloom.Builder().with_modules(shipping).without_input_type_checks().build()
lenient.execute(["total"], inputs={"weight": 2, "quantity": 4})
# -> {'total': 8}
```

To skip only the override checks, build with `Builder.without_override_type_checks()`. Input checks stay on, so an
override of another type, such as `float("nan")` for an `int` node, runs while a badly typed input is still refused:

```python
sentinel = fnloom.Builder().with_modules(shipping).without_override_type_checks().build()
sentinel.execute(["label"], overrides={"total": float("nan")})["label"]
# -> 'nan kg'
```

The `without_input_type_checks()` opt-out turns off both the input and override checks. Edge checks stay on: they
cost nothing per run, and a graph whose edges disagree is usually a bug whatever you supply.

When an edge disagrees on purpose, such as a node returning a wider type than a reader declares, turn its check off
by name with `without_edge_type_checks("label")`: only the edges into `label`, and into the step nodes its
`@pipe_input`, `@pipe_output` or `@mutate` makes, are skipped, and every other edge is still checked (Q149). With no
names, no edge is checked. A name that is no computed node fails the build, with a `did you mean`. Input checks are
unchanged either way, and the export records the setting as `edge_checks`, in `graph_version` too.

## Execution order

Nodes run in topological order, and when two nodes could run in either order, they run in order of their names. The
order never depends on how you list `final_vars` or a function's parameters, so logs and side effects come out the
same way on every run.

## Fan-out and fan-in

A graph that calls an endpoint once per item lists the items, fans out over them, and gathers what each call
returned. In this catalog, `picked` is the expand: annotated `-> Parallelizable[str]`, it runs `details` once per
item, and `summary` collects every item's details with a `Collect[str]` parameter:

```python
# file: catalog_api.py
from dataclasses import dataclass

from fnloom import ABSENT, Absent, Collect, Parallelizable

CATALOG = {"furniture": ["chair", "desk", "lamp"]}


@dataclass(frozen=True)
class Endpoint:
    """Where item details are fetched from."""

    base_url: str


def listing(category: str, item: str | None = None) -> list[str] | Absent:
    """Every item in the category, unless the request names one."""
    return ABSENT if item is not None else CATALOG[category]


def picked(listing: list[str] | Absent, item: str | None = None) -> Parallelizable[str]:
    """The item named, or else every item listed."""
    return [item] if isinstance(listing, Absent) else listing


def details(picked: str, endpoint: Endpoint) -> str:
    """One item's details: a real module would call the endpoint here."""
    return f"{endpoint.base_url}/{picked}"


def summary(details: Collect[str]) -> list[str]:
    """Every item's details, in the order the items came."""
    return details
```

```python
import fnloom
import catalog_api

catalog = fnloom.build(catalog_api)
endpoint = catalog_api.Endpoint("https://api.test")
catalog.execute(["summary"], inputs={"category": "furniture", "endpoint": endpoint})
# -> {'summary': ['https://api.test/chair', 'https://api.test/desk', 'https://api.test/lamp']}
catalog.nodes["details"].block
# -> 'picked'
```

`details` and every node reading it form `picked`'s block, which runs once per item, item after item; `endpoint`,
outside the block, is shared by every item. There is no flag or executor to set. A failing item fails the run, naming
its branch, as in `while computing 'details' for branch (1,) of 'picked'`. Blocks nest, a block may be collected any
number of times, and a block node is neither an output nor an override: request the collect, or override the
expand with the items. [Fan-out and fan-in](writing-nodes.md#fan-out-and-fan-in) has every rule.

With a cache, each item's branch is keyed by the item's content, not its place, so a run that picks items already
fetched, in any order, calls nothing for them, and one more item costs one more call ([Blocks](caching.md#blocks)).
Hooks hear each branch ([The events](hooks.md#the-events)), and drawings draw each block as a cluster
([Blocks](drawing.md#blocks)).

## Absence

A request naming one item needs no listing. `listing` declines by returning `ABSENT`, and `picked` admits it with
`list[str] | Absent`, so it fans out over the item named instead:

```python
catalog.execute(
    ["summary"], inputs={"category": "furniture", "endpoint": endpoint, "item": "desk"}
)
# -> {'summary': ['https://api.test/desk']}
catalog.execute(["listing"], inputs={"category": "furniture", "item": "desk"})
# -> {'listing': fnloom.ABSENT}
catalog.nodes["listing"].may_be_absent
# -> True
```

Whether a node may be absent is known at build, and each parameter reading it decides by its own signature: one
admitting `Absent` receives `ABSENT`, one with a default takes its default, and any other skips its node, which is
absent too. A skipped node runs nothing, and [`RunEnd.skipped`](hooks.md#the-events) lists it. An input is never
`ABSENT`; an override may be, for a node that may be absent. With a cache, a stored absence replays, so the nodes it
skipped are skipped again without running. [Declining](writing-nodes.md#declining) has every rule.

## Lineage

```python
driver.node_names()
# -> ['label', 'quantity', 'total', 'trim', 'trimmed', 'unit', 'weight']
driver.what_is_upstream_of("trimmed")
# -> ['quantity', 'total', 'trim', 'trimmed', 'weight']
driver.what_is_downstream_of("total")
# -> ['label', 'total', 'trimmed']
```

Each query returns sorted names, including the nodes you named. `node_names()` lists every node, inputs included.

`what_is_the_path_between(upstream, downstream)` returns every node on a path from one to the other, both included.
The upstream end may be an input; the downstream end is one only when both ends are that input, since nothing feeds an
input. The order is topological, with ties broken by name, so it's the same on every run:

```python
driver.what_is_the_path_between("weight", "label")
# -> ['weight', 'total', 'trimmed', 'label']
driver.what_is_the_path_between("label", "weight")
# -> []
driver.what_is_the_path_between("total", "total")
# -> ['total']
driver.what_is_the_path_between("prise", "lable")
# raises ValueError: unknown nodes: 'lable', 'prise'
```

No path, as when you ask in the wrong direction, is an empty list. An unknown name raises a `ValueError` naming every
unknown end, sorted. The async driver has the same method.

## Inputs and outputs

`driver.inputs()` maps each input to an `InputView`, in name order: its `type`, whether some consumer `required` it,
and its `consumers`, sorted. `driver.outputs()` lists the nodes nothing reads, sorted: the usual `final_vars`.

```python
driver.inputs()["trim"] == fnloom.InputView(
    "trim", float, required=False, consumers=("trimmed",)
)
# -> True
driver.inputs()["weight"].required
# -> True
list(driver.inputs())
# -> ['quantity', 'trim', 'unit', 'weight']
driver.outputs()
# -> ['label']
driver.modules == (shipping,)
# -> True
```

An input is required when any consumer has no default for it, so `unit` is optional here. `driver.modules` holds
the modules the graph was built from, each once, in the order you gave them. The async driver has all three.

## Inspecting a graph

`driver.nodes` maps every node name, inputs included, to a read-only `NodeView`, in name order:

```python
list(driver.nodes)
# -> ['label', 'quantity', 'total', 'trim', 'trimmed', 'unit', 'weight']
view = driver.nodes["trimmed"]
view.type
# -> float
view.dependencies
# -> ('total', 'trim')
view.is_input
# -> False
view.doc
# -> 'The total after the trim, if one is given.'
[(p.name, p.required) for p in view.params]
# -> [('total', True), ('trim', False)]
```

`origin` is the function the node came from, `module` is its module's name, and `tags` are the ones you wrote with
`@tag`: a `str`, or a tuple of them in written order. `driver.nodes.tagged(owner="ann")` gives the views whose tags
match (a `str` matches an equal value or a tuple holding it, a list or tuple matches any of its items, `fnloom.ANY`
matches any node with the tag), in the mapping's order; a tag no node has matches nothing.
`namespace` is the one its module was given under, or `None`
([Namespaces](writing-nodes.md#namespaces)). An input has no function, so its `origin`, `module`, `doc` and
`namespace` are `None` and its `params` are empty. Its `type`
is the annotation of the parameters that read it:

```python
driver.nodes["quantity"].type
# -> int
driver.nodes["quantity"].origin is None
# -> True
```

The view is made once, when the graph is built, and annotations are evaluated then too. That means:

- a module using `from __future__ import annotations` gives the same types as one without it;
- an annotation can name a class defined further down the module;
- a name that doesn't resolve, such as one imported only under `TYPE_CHECKING`, builds as an opaque `unresolved` type
  (Q81); see [Annotations](writing-nodes.md#annotations);
- if two functions annotate the same input with types that disagree, the build raises a `ValueError` naming the input
  and each function.

## Pickling and process pools

A driver pickles, and copies with `copy.deepcopy`, so you can hand it to a `concurrent.futures.ProcessPoolExecutor`
or any other tool that pickles its arguments. It is pickled as the recipe it was built from: the names of its
modules and their namespaces, the functions given to `with_functions`, by module and qualified name, every setting,
its config, its cache store and its result builder. Loading it
imports those modules by name and builds the driver again, so its compiled plans don't travel; each process compiles
its own.

```python
import pickle

again = pickle.loads(pickle.dumps(driver))
again.execute(["total"], inputs={"weight": 2.0, "quantity": 3})
# -> {'total': 6.0}
```

- Every module must be importable by its name, in the process that loads the driver. A module you made by hand, or
  one that isn't in `sys.modules` under its own name, makes pickling raise a `TypeError` naming it.
- A prepared plan pickles too. It's pickled as its driver and its request, and loading it prepares the same request
  on the rebuilt driver. Plans pickled together with their driver, such as several tasks in one call, share one
  rebuilt driver.
- A `DiskCache` reopens the same directory, so worker processes share its entries with you. A `MemoryCache` comes
  back empty, with the same bounds: its entries belong to the process that stored them.

## Threads

One driver can be shared by any number of threads, on the default build and on the free-threaded `3.14t`, where
fnloom keeps the GIL off. A run keeps its values to itself, and never writes to the `inputs` or `overrides` you pass.
These are safe to call from several threads at once:

- on a `Driver`: `execute`, `prepare`, `validate`, `explain_cache`, and the inspection methods: `nodes`, `inputs()`,
  `outputs()`, `modules`, `what_is_the_path_between` and the lineage methods;
- `PreparedPlan.run` and `run_many`, on one plan shared between threads;
- a `Builder`, whose methods return new builders, its `build`, and `rebuild`;
- `MemoryCache` and `DiskCache`, so a cached driver can be shared too.

An `AsyncDriver`'s synchronous methods, `prepare`, `validate` and inspection, are the `Driver`'s, and as safe.

By default a run stays on the thread that called it, one node at a time, blocks included: a
[fan-out](#fan-out-and-fan-in) runs its items one after another.

### Running in parallel

`Builder.with_threads(max_workers=None)` runs each run's nodes in parallel instead, on the driver's own pool of
`max_workers` workers, `os.process_cpu_count()` by default. The calling thread is one of them, so the pool starts
`max_workers - 1` threads at the first run, named `fnloom-worker-<n>`, and `with_threads(1)` runs on the calling
thread alone. Independent nodes and a block's branches overlap, handed out in reference order as workers free up:

```python
threaded = fnloom.Builder().with_modules(catalog_api).with_threads(8).build()
threaded.execute(["summary"], inputs={"category": "furniture", "endpoint": endpoint})
# -> {'summary': ['https://api.test/chair', 'https://api.test/desk', 'https://api.test/lamp']}
```

- **The GIL build.** On `3.14`, nodes that release the GIL (I/O, `time.sleep`, many C extensions) overlap, and pure
  Python nodes take turns, so they gain nothing; on `3.14t` they all run at once. fnloom gives no warning on `3.14`:
  the results are the same on both.
- **`max_workers` caps the driver, not the run.** Every run of the driver, from any thread, shares its workers, so
  two runs at once of a `with_threads(4)` driver run at most four nodes between them, the calling threads included.
- **Lazy generators.** An expand's generator is read only while fewer than `2 * max_workers` of its branches are
  unfinished, so a large one is never read far ahead.
- **Limits, deadlines and many input sets** come with it: [`@fnloom.limit`](#limits) caps a node's calls on any
  driver, and [`timeout=`](#deadlines-and-cancellation) and [`run_many`](#many-input-sets) run on the pool.
- **The async driver.** An `AsyncDriver` built from such a builder runs its sync nodes on the driver's pool, and its
  `async def` nodes on the loop, holding no worker ([Async](#async)).

### What depends on timing

Results never depend on the worker count or on timing. For the same inputs, a run with no `with_threads`, with one
worker or with eight gives:

- the same values, and each `Collect` in item order;
- the same exception, when one node fails. After a failure no new node starts, the run waits for the running ones,
  then raises the failure that comes first in reference order, as one exception. A `fnloom.StoppedError`, which says
  only that a node was stopped, is never raised in place of the failure that stopped the run;
- the same cache keys, and the same values stored under them;
- the same [`RunEnd.skipped`](hooks.md#the-events) and `RunEnd.absent_errors`, sorted by order key, and the same
  branches made absent by [`absent_on`](writing-nodes.md#partial-results): an error absence is no failure, so the
  run goes on as with one worker.

What can depend on timing:

- the order hooks hear nodes start and end, and the thread each node runs on. Each node event carries `order`, so a
  hook can sort them into the order one worker gives, which is exactly the order without `with_threads`;
- with several failing nodes, which of them ran before the stop, and so what their side effects did. The exception
  is the first in reference order of those that ran;
- when `fnloom.cancelled()` turns `True` in a running node, and whether a run inside a stopping run finished its
  outputs first, so returns them, or raises `StoppedError`;
- whether a branch waiting on an equal key's value shares it or, once that value is in, computes it again
  ([Blocks](caching.md#blocks)): either way it gets the same value;
- which of several calls waiting for a [limit](#limits) gets a freed slot, and whether a run finishes inside its
  `timeout=`;
- in [`run_many`](#many-input-sets), which failing sets ran before the batch stopped. With one failing set, its
  failure is always the one raised.

### What a run shares, and what it serializes

- **Contexts.** Each node's call runs in its own copy of the context the run started in: a `ContextVar` you set
  before `execute` is seen by every node and its hooks, and one a node sets is seen by no other node. An expand's
  generator runs every item in one context, so `decimal.localcontext()` or a variable it sets lasts across its
  yields.
- **Hooks.** A run's hooks are called one at a time, each on the thread that ran its node, in the node's context
  ([With threads](hooks.md#with-threads)). Nothing else of a run is serialized.
- **Stores.** A node's value is stored from its worker, after its hooks and with no fnloom lock held, before any node
  reading it runs ([Stores](caching.md#stores)).
- **Single-flight.** Within a run, a block node whose key equals one already running waits for that value instead of
  computing it, and reports a hit. It holds within one run only: two runs, or two `run_many` sets, that need one key
  both compute it ([Blocks](caching.md#blocks)). It never shares an error absence: a branch waiting on one runs
  itself.
- **Each driver has its own** pool and limit counters. A copy, a rebuilt or an unpickled driver starts its own, and
  never shares a worker or a slot with the driver it came from.

fnloom can't make your own code safe:

- **Your values.** A mutable value you pass, such as a list, is shared by every thread you pass it to, and a cached
  result is shared by every run that hits it.
- **Nodes that mutate.** A node that changes its arguments, or a module global, races with its sibling branches now,
  as it raced with other threads' runs before.
- **Your store.** A store of your own must be safe to call from several threads at once
  ([Stores](caching.md#stores)).
- **Your hooks and result builder.** A hook sees the runs of every thread that uses its driver, and the result
  builder builds each of their results, so one that keeps state across runs must be safe to call from several threads
  at once ([Hooks](hooks.md), [Result builders](#result-builders)).
- **Your value checks.** An output check, and so a custom value check (`with_value_check`) it calls, runs with its
  node, on its worker, so a value check that keeps state must be safe to call from several threads at once.

### Runs inside runs

A node may run a driver, its own or another, and the inner run knows the runs that called it:

- **It never waits for a worker its caller holds.** Its tasks start on the worker the node holds. That holds on a
  thread or task the node starts and waits for too, when it carries the node's context: `asyncio.to_thread`,
  `threading.Thread(context=contextvars.copy_context())`, or any thread on `3.14t`, which inherits its starter's
  context by default. A run lends that worker to one such inner run at a time: a second one, started meanwhile, takes
  a free worker if there is one, or the lent one once the first has ended.
- **On `3.14`, a plain `threading.Thread` doesn't carry the context.** A run it starts waits for a free worker like
  any other run, and under `with_threads(1)` waits for good for the one its node holds. Start the thread with
  `context=contextvars.copy_context()`, or use `asyncio.to_thread`.
- **A slot its callers hold is refused at once.** A nested run that needs a [limit](#limits) slot whose every unit
  the runs that called it hold raises `RuntimeError` naming the node and the slot, at any depth and on any thread,
  instead of waiting for good.
- **Unrelated runs are not.** Two runs that each hold part of a group, and each wait for the other's part, are not
  detected: they wait until a [deadline](#deadlines-and-cancellation) ends one, or for good without one.
- **It stops with its caller**, as [Deadlines and cancellation](#deadlines-and-cancellation) says.
- **An inner run with a `timeout=`** lends the worker to its own nodes, which run on the pool, since its caller only
  waits.

### Thread counts

A driver's threads are its pool's, and a few more:

- **The pool** keeps `max_workers - 1` threads from its first run. A run with a `timeout=`, or an `AsyncDriver`'s
  first sync node, makes it keep `max_workers`, for good, since its caller runs no node. Each run lending its worker
  to its own nodes (an inner run with a deadline) adds one while it runs.
- **An abandoned call** keeps its thread until it returns, and the pool starts another in its place at once. So the
  process has one thread more for each abandoned call still running, and more for each that never returns.
- **`run_many`** starts at most `max_workers - 1` short-lived helper threads per driver, named
  `fnloom-run-many-<n>`, to open its sets. They are not pool threads.
- **Without `with_threads`**, an `AsyncDriver`'s sync nodes run in the loop's default executor, whose threads are
  asyncio's.
- **The pool's life.** Its threads leave once the driver is dropped, and with it each of its prepared plans that you
  kept: a kept plan keeps the pool, and still runs. At interpreter exit, idle threads leave and are joined, waiting at
  most a second in all. A busy thread is never waited for: it doesn't keep the interpreter from exiting, and CPython
  parks it once finalization begins.

### Fork

Each driver records the process that made its pool and limits. In a child made by `os.fork()`, its next run starts a
fresh pool and limits, with every slot free, and the inherited ones are never touched, since threads the child doesn't
have may hold their locks.

- **A run doesn't survive a fork.** A node that forks on the run's calling thread returns, in the child, into a run
  that belongs to the parent: it raises `RuntimeError: this run was started in process <pid>; a run doesn't survive
  os.fork()`, and the child's next run works. The parent's run goes on.
- **A fork on a pool thread** leaves the child with no thread to return to, since only the forking thread survives a
  fork. Once the node returns there, its thread ends, and with it the child. A node that forks on a worker should
  `exec` or `os._exit` in the child itself, as after any fork from a thread.
- **Without `with_threads`**, a run in the child simply finishes, as it does without threads. Only a limited node's
  grant checks the process, and raises the inherited-run error rather than touch the parent's counters.
- `os.fork` doesn't exist on Windows; `multiprocessing`'s `spawn` and `forkserver` start methods pickle the driver
  instead ([Pickling and process pools](#pickling-and-process-pools)).

## Limits

`@fnloom.limit(n)` caps how many calls of a node run at once, across every run of a driver. `@fnloom.limit(group=...)`
puts a node in a group whose members share one cap, and `Builder.with_limits({group: size})` sizes each group. Give
`limit` either or both; a call then needs a slot of each:

```python
# file: catalog_limits.py
from fnloom import Collect, Parallelizable, limit


def picked(items: list[str]) -> Parallelizable[str]:
    """Every item asked for."""
    yield from items


@limit(4, group="api")
def details(picked: str) -> str:
    """One item's details: at most four calls at once, and a slot of `api`."""
    return f"https://api.test/{picked}"


@limit(group="api")
def reviews(picked: str) -> int:
    """How many reviews the item has, from the same endpoint."""
    return len(picked)


def summary(details: Collect[str], reviews: Collect[int]) -> list[tuple[str, int]]:
    """Each item's details and review count, in the order asked."""
    return list(zip(details, reviews, strict=True))
```

```python
import fnloom
import catalog_limits

limited = (
    fnloom.Builder()
    .with_modules(catalog_limits)
    .with_threads(8)
    .with_limits({"api": 6})
    .build()
)
limited.execute(["summary"], inputs={"items": ["chair", "desk"]})
# -> {'summary': [('https://api.test/chair', 5), ('https://api.test/desk', 4)]}
```

Here at most four `details` calls, and six calls of `details` and `reviews` together, run at once.

- **Every driver honours limits**, with or without `with_threads`. A driver shared by several threads runs a
  `@limit(1)` node one call at a time: a call waits until a slot is free, and `Ctrl-C` ends the wait.
- **A slot is held while the node's call runs.** It is taken as the call starts, after the node's `before_node` hooks,
  and given back as it returns, before its `after_node` hooks run or its value is stored, waking whoever waits for it.
  A generator node takes its slots for each item it yields.
- **Slow hooks hold workers.** With `with_threads`, a limited node's task takes its slots, gives them back while its
  `before_node` hooks run, then takes them again for the call, so no hook ever runs holding a slot. With slow hooks,
  more tasks of its group wait there to take their slots back, each holding a worker, and ready nodes outside the
  group start later.
- **Grants aren't fair.** A freed slot goes to whichever waiting call asks first, not the one that waited longest, so
  under steady contention one call can wait long while others pass it.
- **Each driver has its own counters.** A copy, a rebuilt or an unpickled driver starts with every slot free, and so
  does a forked child. Two drivers never share a slot, even over the same modules.
- **Checked at build**, on every driver: a group a node is in that has no size, or a size of a group no node is in, is
  a `ValueError` naming it.
- **Runs inside runs.** A node that runs a driver may need a slot its own call holds, which would wait for good. A
  run started inside a node knows the slots of the runs that called it, so it fails at once instead, with a
  `RuntimeError` naming the node and the slot: a `@limit(1)` node that runs its own driver on a request needing
  itself, or a node in a full group whose inner run needs that group. Two unrelated runs that each hold part of a
  group and wait for each other are not detected.
- **On an `AsyncDriver`**, an `async def` node waiting for a slot holds no thread: its run awaits the release on the
  loop, woken by a release from any thread or loop, while its other nodes go on.

## Deadlines and cancellation

On a driver built `with_threads`, `execute(..., timeout=seconds)` bounds the run, and so does the keyword on
`raw_execute`, `PreparedPlan.run` and `PreparedPlan.run_raw`. Once that many seconds pass, the run stops and raises
`TimeoutError` at once, without waiting for the nodes still running (Q170). A node that runs long can ask
`fnloom.cancelled()` whether its run is stopping, and return early:

```python
# file: lookups.py
import time

import fnloom


def answer(question: str) -> str:
    """Asks a slow service, giving up once the run is stopping."""
    for _ in range(100):
        if fnloom.cancelled():
            return "gave up"
        time.sleep(0.01)
    return f"the answer to {question}"
```

```python
import lookups

patient = fnloom.Builder().with_modules(lookups).with_threads(2).build()
patient.execute(["answer"], inputs={"question": "why"}, timeout=0.1)
# raises TimeoutError: the run did not finish within its timeout of 0.1 s
fnloom.build(lookups).execute(["answer"], inputs={"question": "why"}, timeout=0.1)
# raises TypeError: timeout= needs Builder.with_threads(...)
```

- **Only with `with_threads`.** The calling thread can't abandon a node it runs itself, so a run with a deadline runs
  every node on the pool, which grows to `max_workers` threads for it, and the caller only waits. Without
  `with_threads`, `timeout=` is a `TypeError`. An `AsyncDriver` takes none: wrap its `execute` in `asyncio.timeout`,
  which cancels the run (Q66).
- **Abandoned nodes.** A node still running at the deadline is abandoned. Its worker and its `@limit` slots are given
  back at once, so later runs never wait for it, and the pool starts a thread in its place. Python can't interrupt a
  running function, so the call goes on until it returns, and what it returns is dropped: no hook hears of it, and
  nothing is stored. That has two costs. While an abandoned call still runs, its node's limit may be exceeded, and the
  process keeps one more thread for each abandoned call that never returns. Such a thread doesn't keep the
  interpreter from exiting.
- **Generators.** At the deadline the caller closes each expand's generator that isn't reading an item, before it
  raises. One that is gets closed by its worker once the item comes.
- **A failure first.** A run that had failed and was waiting for its running nodes raises that failure at the
  deadline, not `TimeoutError`.
- **`fnloom.cancelled()`** is `True` inside a node once its run is stopping: its deadline passed, another node failed,
  or it was interrupted. On an `AsyncDriver`, with or without `with_threads`, it turns `True` once the run fails or
  its caller is cancelled. It is `False` outside a run, and in a run of a `Driver` without `with_threads`. A node
  that sees it should return early, or raise `fnloom.StoppedError`, which says only that the node was stopped and is
  never raised in place of the failure that stopped the run. Any other error the node raises counts as a failure of
  its own, and in [`run_many`](#many-input-sets) a stopped set that raises one at a lower index outranks the set that
  failed first.
- **Runs inside runs** stop with the run that started them. A node of a run started inside a node of a stopping run,
  on any driver and at any depth, sees `cancelled()` turn `True` too. A parallel or async inner run also starts no new
  node, and an async one cancels its running `async def` nodes within 50 ms, even while it waits on one alone. Once
  its running nodes return, it returns its outputs if they were all computed. Otherwise it raises
  `fnloom.StoppedError`, a `RuntimeError`: `the run that started this one has stopped or ended, so this one
  stopped`. The outer run never raises that error in place of the failure that stopped it. A context that outlives a
  stopped run, such as one a thread copied, keeps it stopped, so a run started from it later stops at once.
- **`Ctrl-C`** stops a parallel run the same way, with or without a deadline: the nodes still running are abandoned,
  and `KeyboardInterrupt` is raised at once.
- **Cached runs.** On a driver with a cache, the deadline covers the whole run, the `recompute` nodes run first
  included.
- **Hooks.** `after_run` gets `ok=False` and the `TimeoutError`, and fires once any hook call already going on has
  returned, so a run's hook calls never overlap.

## Partial results

A node's own deadline is often an error, not a stop: an endpoint call that times out for one item. Decorate the node
`@fnloom.absent_on(TimeoutError)` and annotate it `-> T | Absent`, and that item's branch is absent instead of
failing the run, so the summary is built from the items whose calls came back:

```python
# file: catalog_partial.py
from dataclasses import dataclass

import fnloom
from fnloom import Absent, Collect, Parallelizable

#: The items whose endpoint call times out, as a real one would for a slow item.
SLOW = {"desk"}


@dataclass(frozen=True)
class Endpoint:
    """Where item details are fetched from."""

    base_url: str


def picked(items: list[str]) -> Parallelizable[str]:
    """Every item asked for."""
    yield from items


@fnloom.absent_on(TimeoutError)
def details(picked: str, endpoint: Endpoint) -> str | Absent:
    """One item's details, absent when its endpoint call times out."""
    if picked in SLOW:
        raise TimeoutError(f"{endpoint.base_url}/{picked}")
    return f"{endpoint.base_url}/{picked}"


def summary(details: Collect[str]) -> list[str]:
    """The details that came back, in the order asked."""
    return details
```

```python
import catalog_partial

partial = fnloom.Builder().with_modules(catalog_partial).with_threads(8).build()
partial.execute(
    ["summary"],
    inputs={
        "items": ["chair", "desk", "lamp"],
        "endpoint": catalog_partial.Endpoint("https://api.test"),
    },
)
# -> {'summary': ['https://api.test/chair', 'https://api.test/lamp']}
```

- **Every driver**, with or without `with_threads`, and the async driver too, gives the same summary: which items
  failed never depends on the worker count.
- **`Collect[str]` drops the branch**; `Collect[str | Absent]` would keep its place as `ABSENT`. `after_node` still
  reports the error with `ok=False`, and [`RunEnd.absent_errors`](hooks.md#the-events) lists each absence by node and
  branch, in reference order.
- **Nothing computed from an absence is cached**, so the next run calls the timed-out item's endpoint again, while
  the items that came back are served from the cache ([Caching](caching.md)).
- **A run's own `timeout=`** is never an absence: it stops the run, as in [Deadlines and
  cancellation](#deadlines-and-cancellation). `Builder.with_absent_on(TimeoutError, tags=...)` covers every node a
  tag query matches. [Partial results](writing-nodes.md#partial-results) has every rule.

## Many input sets

`PreparedPlan.run_many(inputs_seq, *, overrides=None, timeout=None)` runs a [prepared plan](#prepared-plans) once per
input set, and returns each set's result, in order (Q171):

```python
# file: words.py
def shouted(word: str) -> str:
    return word.upper()


def length(shouted: str) -> int:
    return len(shouted)
```

```python
import words

batch = fnloom.Builder().with_modules(words).with_threads(4).build()
lengths = batch.prepare(["length"], inputs={"word": ""})
lengths.run_many([{"word": "tea"}, {"word": "coffee"}, {"word": "cocoa"}])
# -> [{'length': 3}, {'length': 6}, {'length': 5}]
lengths.run_many([{"word": "tea"}, {"word": 3}])
# raises ValueError: word: expected str, got int
```

- **Each set is its own run**, as `run` would run it: its result is what `run` returns, the result builder's included,
  and it fires its own hook events, with its own `run_id`. `overrides` is shared by every set.
- **Checks first.** Every set is checked as `run` checks it before any runs. A set that fails the checks raises, with
  the note `in input set 1 of run_many` (counting from 0), and no set runs.
- **Where the sets run.** On a driver built `with_threads`, the sets run on its pool, under its limits: at most
  `max_workers` sets are open at once, opening in index order as earlier ones end, and `max_workers` caps the nodes of
  all of them together. Without `with_threads`, the sets run one after another on the calling thread, so the same code
  works on either driver. Called inside a node of its own driver, `run_many` runs its sets one after another on the
  worker that node holds.
- **Failures.** Once a set fails, no set opens, and every open set stops as a failed run does: its running nodes
  finish, `fnloom.cancelled()` turns `True` in them, and it starts no new node. Then the failure of the
  lowest-indexed failing set seen is raised, with the note `while running input set 3 of run_many`; with one failing
  set, that is always its failure. A set that was stopped before its outputs were in raises the
  `fnloom.StoppedError` of a [run inside a run](#deadlines-and-cancellation), which is never raised in place of the
  failure that stopped it. A set that never opened fires no hook event.
- **`timeout=`** covers every set together, and needs `with_threads`. Each set gets what is left of it as it opens,
  and a set not open when it passes never opens.
- **`Ctrl-C`** stops every open set and is raised at once. A set the calling thread is running abandons its running
  nodes, as a run does; the other open sets start no new node, and their running nodes finish in the background,
  keeping their workers until they return.
- **Hooks.** Two sets' hook calls may overlap, as two threads' runs do, so a hook that keeps state across runs must be
  safe to call from several threads at once.
- **Caching.** Single-flight is per run: two open sets that need the same cached value both compute it. A set that
  opens after one stored it hits it.
- **On the async driver** there is no `run_many`: `asyncio.gather` over `AsyncPreparedPlan.run` does the same.

## Rebuilding after an edit

A driver keeps the functions it was built with. After `importlib.reload` it still runs the old code, as Hamilton's
does. `rebuild()` builds a new driver from the same builder, with every setting, its config and its cache store, so it
sees the reloaded module (Q83):

```python
fresh = driver.rebuild()
fresh.execute(["total"], inputs={"weight": 2.0, "quantity": 3})
# -> {'total': 6.0}
```

- The old driver is unchanged, and keeps running its old code until you drop it.
- Code versions are read again, so with a shared cache store the edited node, and everything downstream of it,
  misses on the new driver ([Code versions](caching.md#code-versions)).
- A function given to `with_functions` is the object you gave. After a reload, give the new one to a new builder. When
  the builder has the function's module too, `rebuild` raises a `ValueError` saying the function given to
  `with_functions` is stale after a reload.
- `AsyncDriver.rebuild()` keeps `sync_nodes` too. A driver made from a native graph has no builder, and raises a
  `TypeError`.

## Exceptions from your functions

When one of your functions raises, the run stops and the exception reaches you unchanged, with a note naming the
node:

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

Python prints the note, `in node 'ratio'`, under the traceback. It's also in the exception's `__notes__`.

## Hooks

`Builder.with_hooks(...)` attaches hooks that see each run start and end, and each node it calls, with the node's
arguments and its result or error. A driver with no hooks runs exactly as without them. The [Hooks](hooks.md) guide
has the events, their order, and what happens when a hook raises.

## Node wrappers

`Builder.with_node_wrappers(*wrappers)` runs every computed node through a chain of wrappers: around each node's own
call, its [output checks](writing-nodes.md#checking-values) included. A wrapper is called as `wrapper(node, call,
inputs)`, where `node` is the node's `NodeView`, `call()` runs the rest of the chain and then the node, and `inputs` are
the node's arguments by parameter name. What it returns is the node's value (Q111):

```python
import time

timings: dict[str, float] = {}


def timed(node, call, inputs):
    started = time.perf_counter()
    try:
        return call()
    finally:
        timings[node.name] = time.perf_counter() - started


def retried(node, call, inputs):
    try:
        return call()
    except ConnectionError:
        return call()


wrapped = fnloom.Builder().with_modules(shipping).with_node_wrappers(timed, retried)
wrapped.build().execute(["trimmed"], inputs={"weight": 20.0, "quantity": 3})
# -> {'trimmed': 60.0}
sorted(timings)
# -> ['total', 'trimmed']
```

The first wrapper given is the outermost, and any number may be given, where Hamilton allows one `NodeExecutionMethod`
per driver. A wrapper may call `call()` once, several times, or not at all and return a value of its own. Wrappers see
the nodes that run a function of yours: each function's node, each [step](writing-nodes.md#steps) and each `.raw`
node. An extracted field, and a `@pipe_output` node that only passes its last step's value on, run no function of
yours, so they aren't wrapped. `inputs` holds what the node reads, by parameter name: a `value()` binding and an
unused default aren't in it, as in a [hook](hooks.md)'s events. Hooks' node events fire outside the whole chain, so
`after_node` sees what the chain returned; a failed check on a wrapped node is an error the wrappers see. A
`@pipe_output` node's checks run on the node passing its last step's value on, which isn't wrapped, so no wrapper sees
them fail; `after_node` does.

A [cache](caching.md) hit runs no wrapper, and wrappers are not part of a node's cache key: what a chain returns is
stored as the node's value, and a later run with other wrappers, or none, gets that value. Give a driver whose wrappers
change values a store of its own. With output type checks on, what a wrapper returns is checked too. Wrappers run
synchronously: an `async def` wrapper is refused, and on the async driver an `async def` node can't be wrapped yet, so
`build_async` refuses it. Wrappers travel with the driver when it's pickled, copied or rebuilt, as hooks do.

## Async

`Builder.build_async()` builds an `AsyncDriver`, for a graph with `async def` nodes. Building is synchronous, like
`build()`; only running awaits. `execute` is a coroutine, and returns the same dict as the sync driver, in the order
you asked for:

```python
# file: playback.py
import asyncio


async def rate(speed: str) -> float:
    """The playback rate, as a remote service would return it."""
    await asyncio.sleep(0)
    return {"normal": 1.0, "fast": 1.5}[speed]


def covered(rate: float, minutes: float) -> float:
    """The minutes of a recording played in `minutes` at that rate."""
    return minutes * rate
```

```python
import asyncio

import playback

async_driver = fnloom.Builder().with_modules(playback).build_async()
asyncio.run(
    async_driver.execute(
        ["covered", "rate"], inputs={"speed": "fast", "minutes": 10.0}
    )
)
# -> {'covered': 15.0, 'rate': 1.5}
```

Everything else is the sync driver's: inputs, overrides, the type checks and their errors, lineage, `nodes`,
`config`, and [declining](writing-nodes.md#declining), whose consumers are skipped, given `ABSENT` or left to their
defaults as with `build()`. `prepare` is synchronous too, and returns a plan whose `run` is a coroutine:

```python
async_plan = async_driver.prepare(
    ["covered"], inputs={"speed": "normal", "minutes": 0.0}
)
asyncio.run(async_plan.run(inputs={"speed": "normal", "minutes": 4.0}))
# -> {'covered': 4.0}
```

**How nodes run.** A node starts as soon as everything it reads is ready, so independent `async def` nodes await
concurrently. A sync node runs in a worker thread of the loop's default executor, through `loop.run_in_executor` with
your context variables, so the event loop keeps running while it does. A thread costs tens of microseconds, which is
more than a cheap node such as `covered`. For a graph of cheap sync nodes, `build_async(sync_nodes="inline")` runs
them on the loop instead. The nodes `@extract_fields` and `@extract_columns` generate always run on the loop.

**With `with_threads`,** a sync node runs on the driver's own pool instead of the loop's default executor, so at most
`max_workers` of them run at once, on threads named `fnloom-worker-<n>`, while `async def` nodes take no worker and
await on the loop as before ([Threads](#threads)). `sync_nodes="inline"` can't apply then, and is a `ValueError`.
Either way the run is scheduled as the sync driver's is: ready nodes start in reference order, `@limit` and
`with_limits` hold ([Limits](#limits)), and each `NodeStart` and `NodeEnd` carries its `order`. With
`sync_nodes="inline"` and no `async def` node, its events come exactly as the sync driver's do.

**Blocks** run on the async driver as on `Driver` ([Fan-out and fan-in](#fan-out-and-fan-in)), with every branch's
nodes overlapping, and each `Collect` in item order. The expand may be an `async def` generator, whose items are
awaited on the loop:

```python
# file: catalog_async.py
import asyncio

from fnloom import Collect, Parallelizable


async def picked(items: list[str]) -> Parallelizable[str]:
    """Each item, as a paged listing would stream it."""
    for item in items:
        await asyncio.sleep(0)
        yield item


async def details(picked: str) -> str:
    """One item's details, fetched concurrently with the others'."""
    await asyncio.sleep(0)
    return picked.upper()


def summary(details: Collect[str]) -> list[str]:
    """Every item's details, in the order the items came."""
    return details
```

```python
import catalog_async

streamed = fnloom.Builder().with_modules(catalog_async).build_async()
asyncio.run(streamed.execute(["summary"], inputs={"items": ["chair", "desk"]}))
# -> {'summary': ['CHAIR', 'DESK']}
```

A sync generator's items are read where its node would run: in a thread, on the pool, or on the loop. It is closed
on the loop when its node is inline, and else in the loop's default executor, which a hung call never holds. A
generator keeps one context from its first item to its last. When a branch fails, every running node and
every pending read of an async generator is cancelled once, and the generator is closed once, after any read of it in
a thread returns.

An expand is read only while fewer than `2 * max_workers` of its branches are unfinished, or `2 *
os.process_cpu_count()` without `with_threads`, as on the sync driver ([Threads](#threads)). That bounds how many
branches overlap, `async def` ones too: under `with_threads(1)` two at a time. Give a fan-out of slow `async def`
branches enough workers for the overlap you want.

**When a node fails,** the run cancels every node still running, waits for each `async def` node to stop, and raises
the failed node's own exception, with the same note naming the node as the sync driver adds. There's no
`ExceptionGroup`, so `except ZeroDivisionError` works as it does with `Driver`. A sync node in a thread can't be
interrupted: the run doesn't wait for it, and drops its result when it comes, but its `@limit` and worker slots stay
taken until it returns. On the driver's pool, its thread is replaced at once, and leaves the pool once the call
returns. An expand's end, which holds no slot, runs in the loop's default executor, so a generator's `finally` never
waits for a hung call's thread. `asyncio.run` still waits for running threads when the loop shuts down, since it
shuts the default executor down. If the caller is cancelled, as by
`asyncio.timeout`, the run cancels its nodes the same way, and the cancellation reaches the caller once each
`async def` node has stopped. Each node is cancelled once: a second cancellation of the caller, or one after a node
failed, doesn't interrupt a node's cleanup, and the caller still waits for it.

A few edges follow from that, as with `TaskGroup`:

- A node that swallows its cancellation and never finishes makes the caller wait for it.
- An exception a node raises while cleaning up from its cancellation is dropped. The run raises the first failure, or
  the caller's cancellation.
- A node task that something else cancels fails the run as `node 'name' was cancelled`. That holds even when the node
  then raises its own `CancelledError`, which can't be told apart from the outside one.

An async driver can cache too: see [Caching on the async driver](caching.md#on-the-async-driver). One made by a builder
pickles as that builder, as a `Driver` does.
