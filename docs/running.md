# Running a graph

Everything about calling a graph: inputs, overrides, prepared plans, type checks, lineage and inspection. The
examples on this page all use one module:

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

`execute(final_vars, *, inputs=None, overrides=None)` computes the nodes named in `final_vars`. It returns a dict with
exactly those, each once, in the order you asked for them:

```python
driver.execute(["label", "total"], inputs={"price": 2.5, "quantity": 4})
# -> {'label': '10.00 EUR', 'total': 10.0}
```

You can also pass a function instead of its name. It means the node the function makes in this driver, and the
result is still keyed by the node's name:

```python
driver.execute([pricing.label], inputs={"price": 2.5, "quantity": 4})
# -> {'label': '10.00 EUR'}
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
`total`, and `price` and `quantity` are no longer needed:

```python
driver.execute(["label"], overrides={"total": 100.0})
# -> {'label': '100.00 EUR'}
```

An override can name any node, whether a function or an input. It wins over a supplied input of the same name, and
it fills in for a required input you didn't supply.

### Strict overrides

Hamilton refuses an override of an input that you haven't also supplied, because it only cuts off what is upstream of
an overridden node. To get that rule, build with `with_strict_overrides()`. The refusal names every such input,
sorted, with the nodes that need it. It is settled when the request is prepared, never on a run of a prepared plan:

```python
strict = fnloom.Builder().with_modules(pricing).with_strict_overrides().build()
strict.execute(
    ["total"], inputs={"price": 2.0, "quantity": 3}, overrides={"price": 5.0}
)
# -> {'total': 15.0}
strict.execute(["total"], inputs={"quantity": 3}, overrides={"price": 5.0})
# raises ValueError: override of input 'price' refused: not supplied, though needed by 'total' (supply it in inputs= too; strict overrides are on)
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
    fnloom.Builder().with_modules(pricing).with_declared_inputs("price", "quantty")
)
try:
    declared.build()
except ValueError as error:
    lines = str(error).splitlines()
lines
# -> ["declared name 'quantty' is not an input of the graph (did you mean 'quantity'?)", "undeclared input 'currency'", "undeclared input 'discount' (did you mean 'discounted'?)", "undeclared input 'quantity' (did you mean 'quantty'?)"]
```

## Prepared plans

Each `execute` call compiles a plan for its request: which nodes run, in what order, and when each value can be
freed. `prepare` does that work once and hands you the plan, which you can run as often as you like. This is the
fastest way to run the same graph in a loop:

```python
plan = driver.prepare(["label"], inputs={"price": 0.0, "quantity": 0})
plan.run(inputs={"price": 1.5, "quantity": 2})
# -> {'label': '3.00 EUR'}
plan.run(inputs={"price": 4.0, "quantity": 3})
# -> {'label': '12.00 EUR'}
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


listed = fnloom.Builder().with_modules(pricing).with_result_builder(pairs).build()
listed.execute(["total", "price"], inputs={"price": 2.5, "quantity": 4})
# -> ['total=10.0', 'price=2.5']
```

- **What it gets.** Each requested output once, in the order you asked for it, with the inputs and overrides you
  requested as outputs, as Hamilton's result builder gets them (Q19).
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
listed.raw_execute(["total"], inputs={"price": 2.5, "quantity": 4})
# -> {'total': 10.0}
```

### pandas and polars frames

fnloom ships no DataFrame builder, and needs neither library (Q89). Pass the frame class itself:

```python
# file: sales.py
import pandas as pd


def prices() -> pd.Series:
    return pd.Series([1.0, 2.0, 3.0])


def quantities() -> pd.Series:
    return pd.Series([1, 2, 3])


def returns() -> pd.Series:
    return pd.Series([1, 0])


def revenue(prices: pd.Series, quantities: pd.Series) -> pd.Series:
    return prices * quantities
```

```python
import pandas
import sales

frames = fnloom.Builder().with_modules(sales).with_result_builder(pandas.DataFrame)
frames.build().execute(["revenue", "quantities"]).to_dict("list")
# -> {'revenue': [1.0, 4.0, 9.0], 'quantities': [1, 2, 3]}
```

`with_result_builder(polars.DataFrame)` is the polars form. Either builds by the library's own rules, not by those of
Hamilton's `PandasDataFrameResult`:

- a DataFrame output is not flattened into `output.column` columns: pandas refuses a frame as one column;
- no warning about the outputs' index types is logged;
- outputs that are all scalars need an index in pandas, where Hamilton makes a one-row frame.

pandas itself still outer-joins Series of different lengths or indexes, padding the gaps with NaN and turning integers
into floats, as Hamilton does. To refuse that instead, check the indexes in a builder of your own:

```python
def frame(outputs: dict[str, object]) -> pandas.DataFrame:
    """One column per output; Series on different indexes are refused."""
    indexes = [v.index for v in outputs.values() if isinstance(v, pandas.Series)]
    if any(not index.equals(indexes[0]) for index in indexes[1:]):
        raise ValueError(f"outputs on different indexes: {', '.join(outputs)}")
    return pandas.DataFrame(outputs)


strict = fnloom.Builder().with_modules(sales).with_result_builder(frame).build()
strict.execute(["quantities", "returns"])
# raises ValueError: outputs on different indexes: quantities, returns
```

polars has no index, and refuses columns of different lengths itself.

## Validate

`validate` checks a request the way `execute` would, but runs nothing. It returns every problem as one line, sorted,
and an empty list when the request would run:

```python
driver.validate(["label"], inputs={"price": 1.5, "quantity": 2})
# -> []
driver.validate(
    ["labl", "total"], inputs={"price": "cheap", "quantity": 2, "quantty": 3}
)
# -> ["price: expected float, got str", "unknown inputs: 'quantty' (did you mean 'quantity'?)", "unknown outputs: 'labl' (did you mean 'label'?)"]
driver.validate(["total"], inputs={"price": 1.5, "quantty": 3})
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
driver.execute(["total"], inputs={"price": "2.5", "quantity": 4})
# raises ValueError: price: expected float, got str
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
driver.execute(["total"], inputs={"price": None, "quantity": 4})
# raises ValueError: price: expected float, got NoneType
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
| A PEP 695 alias (`type Price = float`) | As what it names |
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
`list[Price]` parameter.

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
# file: baskets.py
def basket_total(prices: list[float]) -> float:
    """The sum of every price in the basket."""
    return sum(prices)
```

`Builder.with_deep_type_checks()` checks every item of a `list`, `set`, `tuple[T, ...]` or other standard
collection, every key and value of a `dict` or other mapping, and the items of nested collections in turn, however deep,
without recursion. An iterator is still never read (Q43). By default a `list` passes `list[float]` on its class alone,
as in Hamilton:

```python
import baskets

fnloom.build(baskets).validate(["basket_total"], inputs={"prices": [1.5, "2"]})
# -> []
deep = fnloom.Builder().with_modules(baskets).with_deep_type_checks().build()
deep.validate(["basket_total"], inputs={"prices": [1.5, "2"]})
# -> ['prices: expected list[float], got list (an item does not fit)']
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
widening = fnloom.Builder().with_modules(pricing).with_numeric_widening().build()
widening.execute(["total"], inputs={"price": 2, "quantity": 4})
# -> {'total': 8}
both = (
    fnloom.Builder()
    .with_modules(baskets)
    .with_deep_type_checks()
    .with_numeric_widening()
    .build()
)
both.execute(["basket_total"], inputs={"prices": [1, 2.5]})
# -> {'basket_total': 3.5}
```

`fnloom.value_matches(declared, value, deep=True, widen=True)` answers as a driver with both settings.

### Unsupported annotations

Anything else refuses every value. Two examples are a plain protocol (one not marked
`runtime_checkable`), and a `dict` subclass, such as an `OrderedDict`, for `dict[str, int]`.
Either annotate with a supported form, or build with `Builder.without_input_type_checks()`:

```python
lenient = fnloom.Builder().with_modules(pricing).without_input_type_checks().build()
lenient.execute(["total"], inputs={"price": 2, "quantity": 4})
# -> {'total': 8}
```

To skip only the override checks, build with `Builder.without_override_type_checks()`. Input checks stay on, so an
override of another type, such as `float("nan")` for an `int` node, runs while a badly typed input is still refused:

```python
sentinel = fnloom.Builder().with_modules(pricing).without_override_type_checks().build()
sentinel.execute(["label"], overrides={"total": float("nan")})["label"]
# -> 'nan EUR'
```

The `without_input_type_checks()` opt-out turns off both the input and override checks. Edge checks stay on: they
cost nothing per run, and a graph whose edges disagree is a bug whatever you supply.

## Execution order

Nodes run in topological order, and when two nodes could run in either order, they run in order of their names. The
order never depends on how you list `final_vars` or a function's parameters, so logs and side effects come out the
same way on every run.

## Lineage

```python
driver.node_names()
# -> ['currency', 'discount', 'discounted', 'label', 'price', 'quantity', 'total']
driver.what_is_upstream_of("discounted")
# -> ['discount', 'discounted', 'price', 'quantity', 'total']
driver.what_is_downstream_of("total")
# -> ['discounted', 'label', 'total']
```

Each query returns sorted names, including the nodes you named. `node_names()` lists every node, inputs included.

`what_is_the_path_between(upstream, downstream)` returns every node on a path from one to the other, both included.
The upstream end may be an input; the downstream end is one only when both ends are that input, since nothing feeds an
input. The order is topological, with ties broken by name, so it's the same on every run:

```python
driver.what_is_the_path_between("price", "label")
# -> ['price', 'total', 'discounted', 'label']
driver.what_is_the_path_between("label", "price")
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
driver.inputs()["discount"] == fnloom.InputView(
    "discount", float, required=False, consumers=("discounted",)
)
# -> True
driver.inputs()["price"].required
# -> True
list(driver.inputs())
# -> ['currency', 'discount', 'price', 'quantity']
driver.outputs()
# -> ['label']
driver.modules == (pricing,)
# -> True
```

An input is required when any consumer has no default for it, so `currency` is optional here. `driver.modules` holds
the modules the graph was built from, each once, in the order you gave them. The async driver has all three.

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

`origin` is the function the node came from, `module` is its module's name, and `tags` are the ones you wrote with
`@tag`. `namespace` is the one its module was given under, or `None`
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
again.execute(["total"], inputs={"price": 2.0, "quantity": 3})
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
- `PreparedPlan.run`, on one plan shared between threads;
- a `Builder`, whose methods return new builders, its `build`, and `rebuild`;
- `MemoryCache` and `DiskCache`, so a cached driver can be shared too.

An `AsyncDriver`'s synchronous methods, `prepare`, `validate` and inspection, are the `Driver`'s, and as safe.

fnloom can't make your own code safe:

- **Your values.** A mutable value you pass, such as a list, is shared by every thread you pass it to, and a cached
  result is shared by every run that hits it.
- **Nodes that mutate.** A node that changes its arguments, or a module global, races with every other run that
  reads them.
- **Your store.** A store of your own must be safe to call from several threads at once
  ([Stores](caching.md#stores)).
- **Your hooks and result builder.** A hook sees the runs of every thread that uses its driver, and the result
  builder builds each of their results, so one that keeps state must be safe to call from several threads at once
  ([Hooks](hooks.md), [Result builders](#result-builders)).

## Rebuilding after an edit

A driver keeps the functions it was built with. After `importlib.reload` it still runs the old code, as Hamilton's
does. `rebuild()` builds a new driver from the same builder, with every setting, its config and its cache store, so it
sees the reloaded module (Q83):

```python
fresh = driver.rebuild()
fresh.execute(["total"], inputs={"price": 2.0, "quantity": 3})
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

## Async

`Builder.build_async()` builds an `AsyncDriver`, for a graph with `async def` nodes. Building is synchronous, like
`build()`; only running awaits. `execute` is a coroutine, and returns the same dict as the sync driver, in the order
you asked for:

```python
# file: rates.py
import asyncio


async def rate(currency: str) -> float:
    """The exchange rate, as a remote service would return it."""
    await asyncio.sleep(0)
    return {"EUR": 1.0, "USD": 1.5}[currency]


def converted(rate: float, amount: float) -> float:
    """The amount in the currency."""
    return amount * rate
```

```python
import asyncio

import rates

async_driver = fnloom.Builder().with_modules(rates).build_async()
asyncio.run(
    async_driver.execute(
        ["converted", "rate"], inputs={"currency": "USD", "amount": 10.0}
    )
)
# -> {'converted': 15.0, 'rate': 1.5}
```

Everything else is the sync driver's: inputs, overrides, the type checks and their errors, lineage, `nodes` and
`config`. `prepare` is synchronous too, and returns a plan whose `run` is a coroutine:

```python
async_plan = async_driver.prepare(
    ["converted"], inputs={"currency": "EUR", "amount": 0.0}
)
asyncio.run(async_plan.run(inputs={"currency": "EUR", "amount": 4.0}))
# -> {'converted': 4.0}
```

**How nodes run.** A node starts as soon as everything it reads is ready, so independent `async def` nodes await
concurrently. A sync node runs in a worker thread of the loop's default executor, through `loop.run_in_executor` with
your context variables, so the event loop keeps running while it does. A thread costs tens of microseconds, which is
more than a cheap node such as `converted`. For a graph of cheap sync nodes, `build_async(sync_nodes="inline")` runs
them on the loop instead. The nodes `@extract_fields` and `@extract_columns` generate always run on the loop.

**When a node fails,** the run cancels every node still running, waits for each `async def` node to stop, and raises
the failed node's own exception, with the same note naming the node as the sync driver adds. There's no
`ExceptionGroup`, so `except ZeroDivisionError` works as it does with `Driver`. A sync node in a thread can't be
interrupted: the run doesn't wait for it, and drops its result when it comes. `asyncio.run` still waits for running
threads when the loop shuts down, since it shuts the default executor down. If the caller is cancelled, as by
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
