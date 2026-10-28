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

- `None` fits every type.
- An input you supply but the plan never reads isn't checked. Every override is, even one for a node the plan never
  reaches.
- The checks a plan needs are worked out once, when it's prepared, so a run only checks values. Checking against a
  plain class like `int` costs one `isinstance` call.

### What the checks understand

| Annotation | How a value is checked |
| --- | --- |
| A class (yours or built-in), `object` | `isinstance`, so subclasses fit |
| `Any` | Anything fits |
| Unions, `Optional`, `Literal` | Against each member or value |
| `Annotated[T, ...]` | As `T`; metadata never changes the answer |
| `NewType` | As its supertype |
| `TypeVar` | Anything fits; on an edge it matches only itself |
| A `runtime_checkable` protocol | `isinstance` |
| A PEP 695 alias (`type Price = float`) | As what it names |
| `list`, `dict`, `tuple` and other plain classes | `isinstance` |
| `list[T]`, `tuple[T, ...]`, `Sequence[T]`, `set[T]`, `Iterable[T]` | A value of exactly the declared class (a `list` for `list[T]`) by class only, so `["a"]` fits `list[int]`. Any other value by kind, plus its first element |
| `dict[K, V]` | By class only: the value's class must be exactly `dict` |
| Other `collections.abc` generics (`Mapping[str, int]`, `Callable[[int], int]`, ...) | By kind only, so any `dict` fits `Mapping[str, int]` |
| An iterator | By kind only. It's never read, so no items are lost |

A generic alias such as `Pair[int]`, for `type Pair[T] = tuple[T, T]`, is read as `tuple[int, int]` when its value
uses its type parameters in the order it declares them. One that reorders them is compared as written. On an edge, an
alias inside another generic's arguments is compared as written too, so a node returning `list[float]` doesn't feed a
`list[Price]` parameter.

### Unsupported annotations

Anything else refuses every value except `None`. Two examples are a plain protocol (one not marked
`runtime_checkable`), and a `dict` subclass, such as an `OrderedDict`, for `dict[str, int]`.
Either annotate with a supported form, or build with `Builder.without_input_type_checks()`:

```python
lenient = fnloom.Builder().with_modules(pricing).without_input_type_checks().build()
lenient.execute(["total"], inputs={"price": 2, "quantity": 4})
# -> {'total': 8}
```

The opt-out turns off only the input and override checks. Edge checks stay on: they cost nothing per run, and a graph
whose edges disagree is a bug whatever you supply.

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
Either end may be an input. The order is topological, with ties broken by name, so it's the same on every run:

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
`@tag`. An input has no function, so its `origin`, `module` and `doc` are `None` and its `params` are empty. Its `type`
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
- a name that doesn't resolve, such as one imported only under `TYPE_CHECKING`, fails the build with its `NameError`
  and a note naming the node and the parameter;
- if two functions annotate the same input with types that disagree, the build raises a `ValueError` naming the input
  and each function.

## Pickling and process pools

A driver pickles, and copies with `copy.deepcopy`, so you can hand it to a `concurrent.futures.ProcessPoolExecutor`
or any other tool that pickles its arguments. It is pickled as the recipe it was built from: the names of its
modules, whether it checks input types, its config, and its cache store. Loading it imports those modules by name
and builds the driver again, so its compiled plans don't travel; each process compiles its own.

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
