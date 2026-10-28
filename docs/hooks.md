# Hooks

A hook watches a driver's runs: when each run starts and ends, and each node it calls, with the node's arguments and
its result or error. Use one to log, time, trace or count. A hook can't change what a run computes. The examples on
this page use one module:

```python
# file: orders.py
def subtotal(price: float, quantity: int) -> float:
    """What the items cost."""
    return price * quantity


def shipping(subtotal: float, free_above: float = 50.0) -> float:
    """Free above a threshold, a flat rate below it."""
    return 0.0 if subtotal > free_above else 4.5


def total(subtotal: float, shipping: float) -> float:
    """What the customer pays."""
    return subtotal + shipping
```

## Writing a hook

Subclass `fnloom.Hook` and override any of its four methods. Each gets one event:

| Method | Called | Event |
| --- | --- | --- |
| `before_run` | once the request is known good, before its values are checked | `RunStart` |
| `before_node` | before a node's function is called | `NodeStart` |
| `after_node` | after it returned or raised | `NodeEnd` |
| `after_run` | once the run ended, whether or not it succeeded | `RunEnd` |

Give hooks to `Builder.with_hooks`:

```python
import fnloom
import orders


class Trace(fnloom.Hook):
    """Remembers which nodes ran, and with what."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def after_node(self, event: fnloom.NodeEnd) -> None:
        self.lines.append(f"{event.node.name}{dict(event.inputs)} -> {event.result}")


trace = Trace()
driver = fnloom.Builder().with_modules(orders).with_hooks(trace).build()
driver.execute(["total"], inputs={"price": 2.5, "quantity": 4})
# -> {'total': 14.5}
trace.lines
# -> ["subtotal{'price': 2.5, 'quantity': 4} -> 10.0", "shipping{'subtotal': 10.0} -> 4.5", "total{'shipping': 4.5, 'subtotal': 10.0} -> 14.5"]
```

`free_above` isn't in `shipping`'s inputs: a parameter left to its default is absent, as it is in Hamilton.

Only the methods a hook overrides are called. A run whose hooks override neither node method makes no node events,
so a hook that only times whole runs costs nothing per node. A driver with no hooks runs exactly as before.

`with_hooks` is cumulative, and `driver.hooks` lists them. Every event calls the hooks in the order given, after-events
too, as Hamilton does:

```python
class Named(fnloom.Hook):
    def __init__(self, name: str, log: list[str]) -> None:
        self.name, self.log = name, log

    def before_run(self, event: fnloom.RunStart) -> None:
        self.log.append(f"{self.name} starts")

    def after_run(self, event: fnloom.RunEnd) -> None:
        self.log.append(f"{self.name} ends")


log: list[str] = []
first, second = Named("first", log), Named("second", log)
both = (
    fnloom.Builder().with_modules(orders).with_hooks(first).with_hooks(second).build()
)
both.hooks == (first, second)
# -> True
both.execute(["subtotal"], inputs={"price": 1.0, "quantity": 1})
# -> {'subtotal': 1.0}
log
# -> ['first starts', 'second starts', 'first ends', 'second ends']
```

## The events

Every event is a frozen dataclass, so a hook can't change it, and its mappings are read-only views. A hook gets its
event by position, so a new field never breaks a hook.

- **`RunStart`**
    - `run_id`: a new `uuid4` hex for each run, shared by all of its events.
    - `outputs`: the requested outputs, each once, in request order.
    - `inputs` and `overrides`: the values given.
    - `nodes`: the nodes this run will report, in the order it reports them. That is the order they run in
      ([Execution order](running.md#execution-order)), which never changes from one run to the next. It's empty when a
      value check fails, since no node runs. On a driver with a cache, it lists the hits first
      ([With a cache](#with-a-cache)).
- **`NodeStart`**
    - `run_id`
    - `node`: the node's [`NodeView`](running.md#inspecting-a-graph), with its name, tags, type and function.
    - `inputs`: the keyword arguments its function gets from other nodes and inputs, by parameter name. A parameter
      left to its default is absent, and so is one bound to a `value()`. A `group(...)` parameter is the list or dict
      the function gets.
    - `cache`: the node's [cache outcome](caching.md#explaining-a-run); `None` on a driver with no cache.
- **`NodeEnd`**: `NodeStart`'s fields, plus:
    - `ok`: whether the node returned.
    - `result`: what it returned; `None` unless `ok`.
    - `error`: the very exception it raised; `None` if `ok`.
- **`RunEnd`**: `RunStart`'s fields, plus:
    - `ok`: whether the run succeeded.
    - `error`: what stopped it, as `execute` raises it unless an `after_run` hook raises too; `None` if `ok`.
    - `results`: the outputs, in request order; `None` unless `ok`.

```python
class Ends(fnloom.Hook):
    def __init__(self) -> None:
        self.ends: list[fnloom.RunEnd] = []

    def after_run(self, event: fnloom.RunEnd) -> None:
        self.ends.append(event)


ends = Ends()
watched = fnloom.Builder().with_modules(orders).with_hooks(ends).build()
watched.execute(["total"], inputs={"price": 30.0, "quantity": 2})
# -> {'total': 60.0}
end = ends.ends[0]
(end.ok, end.outputs, end.nodes, dict(end.results))
# -> (True, ('total',), ('subtotal', 'shipping', 'total'), {'total': 60.0})
```

## Which nodes report

Every node a run calls gets a `before_node` and an `after_node`, an extracted field included. These get none, as in
Hamilton:

- inputs, even one requested as an output;
- overridden nodes, and the nodes only they needed;
- nodes the request doesn't need.

```python
ends.ends.clear()
watched.execute(
    ["total"], inputs={"price": 30.0, "quantity": 2}, overrides={"shipping": 1.0}
)
# -> {'total': 61.0}
ends.ends[0].nodes
# -> ('subtotal', 'total')
```

When a node raises, no later node starts. Its `after_node` gets `ok=False` and the node's own exception, and
`after_run` gets `ok=False`. The exception then reaches you as without hooks, with its `in node` note.

## When `before_run` fires

A request's shape is checked before any hook: an unknown output, input or override name, or `final_vars` given as
one `str`. Those raise from `prepare`, so no event fires.

Its values are checked after `before_run`, as Hamilton checks them: a missing input, and the type of each input and
override. So `after_run` sees their failure:

```python
ends.ends.clear()
watched.execute(["total"], inputs={"price": 30.0})
# raises ValueError: missing inputs: 'quantity' (needed by 'subtotal')
(ends.ends[0].ok, ends.ends[0].nodes)
# -> (False, ())
```

A [prepared plan](running.md#prepared-plans) fires the same events on each `run`. One given other input or override
names than it was prepared for raises before any hook.

## A hook that raises

Every hook sees an event before its errors are raised. Hamilton skips the later hooks of the event. Then:

- an error in `before_run` stops the run, and `after_run(ok=False)` still fires;
- one in `before_node` stops the run before the node is called, and that node gets no `after_node`;
- one in `after_node` for a node that returned stops the run, and the node's value is discarded;
- one in `after_run` for a run that succeeded replaces its result.

One error is raised as itself. Several are one `ExceptionGroup`: the node's own exception first, if a node failed,
then the hook errors in the order of their events, and within an event in the order of the hooks. A node's error is
never lost. Hamilton raises the hook's error and keeps the node's only in its `__context__` (Q23).

```python
class Strict(fnloom.Hook):
    def after_node(self, event: fnloom.NodeEnd) -> None:
        if event.ok and event.result == 0:
            raise ValueError(f"{event.node.name} is zero")


guarded = fnloom.Builder().with_modules(orders).with_hooks(Strict()).build()
guarded.execute(["total"], inputs={"price": 0.0, "quantity": 1})
# raises ValueError: subtotal is zero
```

## Checked when attached

`with_hooks` refuses, with one error naming every culprit:

- an object that is no `fnloom.Hook`: a `TypeError`. Hamilton accepts and ignores it.
- an overridden method that can't take one positional event, or is an `async def`: a `TypeError` naming the method.
  Hamilton fails at the first call, and never awaits an `async def` hook on its sync driver.
- the same hook given twice: a `ValueError`. Hamilton calls it once.

```python
class Late(fnloom.Hook):
    async def after_run(self, event: fnloom.RunEnd) -> None:
        pass


fnloom.Builder().with_hooks(Late())
# raises TypeError: Late.after_run is an async def: a hook method must be a plain def
```

## Carrying hooks

A driver's hooks travel with it. `copy.copy` and `rebuild()` keep the same hook objects. Pickling, and
`copy.deepcopy`, pickle them by Python's own rules: the hooks that come back are copies, and a hook that doesn't
pickle, such as an instance of a class defined inside a function, makes the driver fail to pickle, with pickle's own
error.

`build_async` keeps the hooks too, and `AsyncDriver.hooks` lists them.

## With a cache

On a driver [with a cache](caching.md), every node event carries the node's `CacheOutcome` as `cache`: `hit`, `miss`
or `uncacheable`, with the reason [`explain_cache`](caching.md#explaining-a-run) gives.

- A node loaded from the store gets a `before_node` and an `after_node`, as a node that runs does, so a hook that pairs
  them still pairs. Its `inputs` are `None`, since nothing upstream of it is loaded, and its `result` is the loaded
  value.
- The hits report first, in plan order, since they are loaded before any node runs. Then the nodes the run computes
  report, as they would without a cache.
- A node only the hits need doesn't run, so it reports nothing.
- `RunStart.nodes` lists exactly the nodes the run reports, hits first. When a `recompute` node's result keys the nodes
  that read it, it runs before anything else is looked up, and which of the other nodes hit is known only after it
  ran. It and the nodes it needs report first, and `nodes` lists them, then every other node the run could report.
- A node whose `after_node` raises is not stored: its value is discarded.

```python
class Outcomes(fnloom.Hook):
    def __init__(self) -> None:
        self.seen: list[tuple[str, object, object, object]] = []

    def after_node(self, event: fnloom.NodeEnd) -> None:
        self.seen.append((event.node.name, event.cache, event.inputs, event.result))


outcomes = Outcomes()
cached = (
    fnloom.Builder()
    .with_modules(orders)
    .with_cache(fnloom.MemoryCache())
    .with_hooks(outcomes)
    .build()
)
cached.execute(["total"], inputs={"price": 2.5, "quantity": 4})
# -> {'total': 14.5}
[(name, cache.status) for name, cache, _, _ in outcomes.seen]
# -> [('subtotal', 'miss'), ('shipping', 'miss'), ('total', 'miss')]
outcomes.seen.clear()
cached.execute(["total"], inputs={"price": 2.5, "quantity": 4})
# -> {'total': 14.5}
[(name, tuple(cache), inputs, result) for name, cache, inputs, result in outcomes.seen]
# -> [('total', ('hit', None), None, 14.5)]
```

Hamilton fires the hooks of a hit, and of every node upstream of it, as if they ran, with nothing saying they didn't
(Q85). An `AsyncDriver` fires the same events as a `Driver` does.

## On the async driver

An [`AsyncDriver`](running.md#async) fires the same events as a `Driver`, with the same rules, and so does its
`AsyncPreparedPlan.run`. A hook is still a plain `def`; it's never awaited.

- `before_node` fires as a node starts, and `after_node` once its value or error is in. Both carry real values: the
  node's arguments, then its result, or `ok=False` and its own exception.
- Nodes that run at once report in the order they start and finish, so another node's events can come between a
  node's `before_node` and its `after_node`.
- `RunStart.nodes` lists the nodes in plan order ([Execution order](running.md#execution-order)), not the order of
  the events, which can change from one run to the next. On a driver with a cache it lists the hits first, then the
  nodes the run computes in plan order, as on `Driver` ([With a cache](#with-a-cache)).
- Every hook runs on the event loop's thread, never in a worker thread, even for a sync node run in one. A hook needs no
  lock to keep state for one run.
- A hook that raises fails the run as a failing node does: every node still running is cancelled, and the run waits
  for each `async def` node to stop. A node the failure cancels gets no `after_node`.
- A missing or mistyped input fails after `before_run`, as on `Driver`.

```python
import asyncio


class Starts(fnloom.Hook):
    def __init__(self) -> None:
        self.names: list[str] = []

    def before_node(self, event: fnloom.NodeStart) -> None:
        self.names.append(event.node.name)


starts = Starts()
async_driver = fnloom.Builder().with_modules(orders).with_hooks(starts).build_async()
asyncio.run(async_driver.execute(["total"], inputs={"price": 2.5, "quantity": 4}))
# -> {'total': 14.5}
starts.names
# -> ['subtotal', 'shipping', 'total']
```

Hamilton's async driver calls a sync hook when it schedules a node, not when the node runs. The hook gets the
`asyncio.Task`s of the node's dependencies as its arguments and the node's `Task` as its result, and reports a node
that then fails as a success (Q86).
