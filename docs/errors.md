# Errors

fnloom raises as early as it can, and every message names the culprits, so you're never left guessing which name
was wrong.

- A mistake **in the graph** is caught when you build.
- A mistake **in a request** is caught when it's planned, before any node runs.

The examples use this module and driver:

```python
# file: pricing.py
def total(price: float, quantity: int) -> float:
    """What the line costs."""
    return price * quantity


def label(total: float, currency: str = "EUR") -> str:
    """The price as shown to a customer."""
    return f"{total:.2f} {currency}"
```

```python
import fnloom
import pricing

driver = fnloom.build(pricing)
```

## When you build

| Mistake | Raises |
| --- | --- |
| Something that is not a module, such as a function, passed to `build` or `with_modules` | `TypeError: expected a module, got function 'f'` |
| A module that contributes no node, such as a package that only re-exports functions defined elsewhere, each function listed with why it isn't a node | `ValueError: module 'pkg' has no nodes: '_g' is private, 'f' is defined in 'pkg.impl'` |
| The same node name in two modules | `ValueError: node 'f' is defined in both 'first' and 'second'` |
| A dependency cycle | `ValueError: dependency cycle: 'a' -> 'b' -> 'a'` |
| A parameter or the return without an annotation | `ValueError: 'f': parameter 'a' has no type annotation` |
| A positional-only, `*args` or `**kwargs` parameter | `ValueError: 'f': *args parameter 'a' is not supported` |
| An `async def` given to `build()` (use [`build_async()`](running.md#async)) | `ValueError: 'f': async function is not supported by build(), which calls nodes synchronously; build it with build_async()` |
| A generator function, sync or async, given to either build | `ValueError: 'f': generator function is not supported; nodes are called synchronously` |
| `build_async(sync_nodes=...)` with anything but `"thread"` or `"inline"` | `ValueError: sync_nodes must be 'inline' or 'thread', not 'loop'` |
| An annotation naming something that does not exist at run time, such as an import under `TYPE_CHECKING` | `NameError: name 'Decimal' is not defined`, with a note naming the node and the parameter |
| Functions annotating the same input with types neither of which feeds the other (`Base` and `Child` are fine: the input is `Child`) | `ValueError: inputs have conflicting types: 'a' is int in 'f', str in 'g'` |
| A node whose return type a parameter it feeds does not accept, one line per such parameter, sorted (see [Type checks](running.md#type-checks)) | `ValueError: g.f requires int but f returns str` |
| A node that another node reads, with no `@config.when*` variant matching the config and no `@config.default`, one line per node, sorted (see [Config](writing-nodes.md#config)) | `ValueError: no variant of 'foo' matches the config {'mode': 'c'}; its variants need mode == 'a' (foo__a), mode == 'b' (foo__b); add @config.default or a variant for it` |
| Two variants of one node matching the config | `ValueError: node 'foo' has more than one variant matching the config {'mode': 'a'}: 'foo__a', 'foo__nb'` |
| Two `@config.default`s for one node | `ValueError: node 'foo' has more than one @config.default: 'foo__x', 'foo__y'` |
| A config-decorated function defined twice in one module | `ValueError: function 'foo__a' is defined more than once in module 'm' (lines 4, 8); Python keeps only the last` |
| A config value whose `==` returns something other than a `bool` (NumPy's boolean scalar counts as one), such as an array, every such key listed, sorted | `TypeError: == on these config keys did not return a bool: 'mode' (numpy.ndarray)` |
| A config that isn't a mapping, or a key that isn't a `str`, given to `with_config` | `TypeError: config keys must be str, got: int` |

## When you execute or prepare

Each error lists every offending name, sorted. A missing input also names each node that needs it, or says that you
requested it as an output. A request is checked in the order below, and the first kind of mistake found is the one
raised:

```python
driver.execute(["nope"])
# raises ValueError: unknown outputs: 'nope'
driver.execute(["label"], inputs={"price": 1.0, "quantity": 2, "typo": 3})
# raises ValueError: unknown inputs: 'typo'
driver.execute(["label"], overrides={"tota": 5.0})
# raises ValueError: unknown overrides: 'tota'
driver.execute(["label"], inputs={"price": 1.0})
# raises ValueError: missing inputs: 'quantity' (needed by 'total')
driver.execute(["total", "price"])
# raises ValueError: missing inputs: 'price' (requested as an output, needed by 'total'); 'quantity' (needed by 'total')
```

A node that no config variant gave, requested as an output, raises the build's reason: `ValueError: no variant of
'foo' matches the config ...`. A missing input named like a config key gets a second line, since config is never
passed to nodes (Q56): `'mode' is a config key; config is not passed to nodes: supply it in inputs=`.

!!! tip "Replacing a function's value"

    `inputs` only accepts input nodes, so passing a function's name there is an unknown input. To replace a
    function's value, use `overrides`.

The arguments are checked too:

```python
driver.execute("label")
# raises TypeError: final_vars must be a sequence of names, not a single str
```

`inputs` and `overrides` are keyword-only, so a mapping passed positionally is a `TypeError` instead of being
mistaken for one of them.

## Type checks on a run

Before any node runs, each input the run reads and each override is checked against its node's type. Every value
that doesn't fit gets its own line, sorted. The first call below raises with a second line too,
`quantity: expected int, got float`:

```python
driver.execute(["label"], inputs={"price": "cheap", "quantity": 2.5})
# raises ValueError: price: expected float, got str
driver.execute(["label"], overrides={"total": "ten"})
# raises ValueError: total: expected float, got str
```

Build with `Builder.without_input_type_checks()` to skip these (see [Type checks](running.md#type-checks)).

## When you run a prepared plan

A prepared plan accepts the input and override names it was compiled for, and no others:

```python
plan = driver.prepare(["label"], inputs={"price": 0.0, "quantity": 0})
plan.run(inputs={"price": 1.0})
# raises ValueError: inputs ['price'] differ from the prepared ['price', 'quantity']
```

## When a node raises

Your function's own exception reaches you unchanged, with a note naming the node (see
[Running a graph](running.md#exceptions-from-your-functions)). Lineage queries raise
`ValueError: unknown nodes: ...` for a name that isn't in the graph.

An `AsyncDriver` raises a failed node's exception the same way, after cancelling the nodes still running (see
[Async](running.md#async)). On an `AsyncDriver`, a sync node that returns a coroutine or another awaitable is an
error, never a value:

```text
TypeError: node 'f' is a sync function but returned coroutine, an awaitable; make it an async def, so the async driver awaits it
```
