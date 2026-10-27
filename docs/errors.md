# Errors

fnloom raises as early as it can, and every message names the culprits. A mistake in the graph is found when you
build; a mistake in a request is found when it is planned, before any node runs.

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
| The same node name in two modules | `ValueError: node 'f' is defined in both 'first' and 'second'` |
| A dependency cycle | `ValueError: dependency cycle: 'a' -> 'b' -> 'a'` |
| A parameter or the return without an annotation | `ValueError: 'f': parameter 'a' has no type annotation` |
| A positional-only, `*args` or `**kwargs` parameter | `ValueError: 'f': *args parameter 'a' is not supported` |
| An `async def` or generator function | `ValueError: 'f': async function is not supported; nodes are called synchronously` |
| An annotation naming something that does not exist at run time, such as an import under `TYPE_CHECKING` | `NameError: name 'Decimal' is not defined`, with a note naming the node and the parameter |
| Functions annotating the same input with different types | `ValueError: inputs have conflicting types: 'a' is int in 'f', str in 'g'` |

## When you execute or prepare

Each error lists every offending name, sorted. A request is checked in this order, and the first kind of mistake
found is the one raised:

```python
driver.execute(["nope"])
# raises ValueError: unknown outputs: 'nope'
driver.execute(["label"], inputs={"price": 1.0, "quantity": 2, "typo": 3})
# raises ValueError: unknown inputs: 'typo'
driver.execute(["label"], overrides={"tota": 5.0})
# raises ValueError: unknown overrides: 'tota'
driver.execute(["label"], inputs={"price": 1.0})
# raises ValueError: missing inputs: 'quantity'
```

An input must be an input node: supplying a value for a function's name under `inputs` is an unknown input. To
replace a function's value, use `overrides`.

The arguments are checked too:

```python
driver.execute("label")
# raises TypeError: final_vars must be a sequence of names, not a single str
```

`inputs` and `overrides` are keyword-only, so a mapping passed positionally is a `TypeError` rather than being taken
for one of them.

## When you run a prepared plan

A prepared plan accepts the input and override names it was compiled for, and no others:

```python
plan = driver.prepare(["label"], inputs={"price": 0.0, "quantity": 0})
plan.run(inputs={"price": 1.0})
# raises ValueError: inputs ['price'] differ from the prepared ['price', 'quantity']
```

## When a node raises

A node's own exception propagates unchanged, with a note naming the node (see
[Running a graph](running.md#exceptions-from-your-functions)). Lineage queries raise
`ValueError: unknown nodes: ...` for a name that is not in the graph.
