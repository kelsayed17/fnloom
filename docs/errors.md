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
| An edge between an annotation naming something that does not exist at run time, such as an import under `TYPE_CHECKING`, and any type but the same text or `Any` (Q81) | `ValueError: total.rate requires float but rate returns unresolved 'Decimal'; import what the unresolved annotation names at run time` |
| Functions annotating the same input with types neither of which feeds the other (`Base` and `Child` are fine: the input is `Child`) | `ValueError: inputs have conflicting types: 'a' is int in 'f', str in 'g'` |
| A node whose return type a parameter it feeds does not accept, one line per such parameter, sorted (see [Type checks](running.md#type-checks)) | `ValueError: g.f requires int but f returns str` |
| A node that another node reads, with no `@config.when*` variant matching the config and no `@config.default`, one line per node, sorted (see [Config](writing-nodes.md#config)) | `ValueError: no variant of 'foo' matches the config {'mode': 'c'}; its variants need mode == 'a' (foo__a), mode == 'b' (foo__b); add @config.default or a variant for it` |
| Two variants of one node matching the config | `ValueError: node 'foo' has more than one variant matching the config {'mode': 'a'}: 'foo__a', 'foo__nb'` |
| Two `@config.default`s for one node | `ValueError: node 'foo' has more than one @config.default: 'foo__x', 'foo__y'` |
| A config-decorated function defined twice in one module | `ValueError: function 'foo__a' is defined more than once in module 'm' (lines 4, 8); Python keeps only the last` |
| A config value whose `==` returns something other than a `bool` (NumPy's boolean scalar counts as one), such as an array, every such key listed, sorted | `TypeError: == on these config keys did not return a bool: 'mode' (numpy.ndarray)` |
| A config that isn't a mapping, or a key that isn't a `str`, given to `with_config` | `TypeError: config keys must be str, got: int` |
| A parameter with a default that reads a node of its name (Q76) | `ValueError: shadowed default: parameter 'cost' of 'f' has a default, but it reads the node 'cost', which replaces it; rename one, or let the node win with @fnloom.defaults(shadowed=('cost',)) on 'f'` |
| One input given different defaults by its consumers (Q76) | `ValueError: differing defaults: input 'x' has different defaults in 'f' (parameter 'x') = 1, 'g' (parameter 'x') = 2; make them equal, or write @fnloom.defaults(differing=(<parameter>,)) on each of these consumers but one` |
| With `with_declared_inputs`, an input not declared, or a declared name that is no input, one line each (Q75) | `ValueError: declared name 'prise' is not an input of the graph (did you mean 'price'?)`, then `undeclared input 'price' (did you mean 'prise'?)` |
| A module's `__all__` that isn't a list or tuple of `str` (Q73) | `TypeError: module 'm': __all__ must be a list or tuple of str, got str` |
| Something that isn't a plain function given to `with_functions` or `node()`, such as a `functools.partial` (Q72) | `TypeError: a functools.partial of 'm.f' is not a function: give the function itself with bind={'parameter': value(...)}` |
| A function whose name no node can take, such as a lambda, given to `with_functions` (Q72) | `ValueError: function 'm.<lambda>' has no name a node can take: give it one with fnloom.node(fn, name=...)` |
| A `node(name=...)` that isn't an identifier (Q72) | `ValueError: node name 'a.b' is not an identifier` |
| A namespace that isn't a dotted identifier path, every one listed (Q74) | `ValueError: namespace 'a-b' is not a dotted identifier path, such as 'a' or 'a.b'` |
| `rebuild()` after a reload, with a function given to `with_functions` beside its module (Q83) | `ValueError: node 'k' is defined twice in 'm': function 'm.k' given to with_functions is stale after a reload: give the reloaded function to a new builder` |
| A `group(...)` parameter whose annotation names something defined further down and, once resolved, is not `list[X]` or `dict[str, X]` (Q82) | `TypeError: group() parameters must be annotated as their group: 'm.f': 'xs' (int) needs list[X] for its group` |
| A `group(...)` parameter annotated with a name that never resolves, such as one imported only under `TYPE_CHECKING` (Q81, Q82) | `TypeError: group() parameters must be annotated as their group: 'm.f': 'xs' (unresolved 'Foo') needs list[X] for its group` |

## When you decorate

`@parameterize` and its shorthands check their rows when the module is imported, listing every culprit in one
message (Q82):

| Mistake | Raises |
| --- | --- |
| A docstring placeholder a row doesn't bind, a positional field or a stray brace | `ValueError: the docstring of 'f' is a template for each row's doc: row 'a': placeholder {ghost} is not 'output_name' or a parameter the row binds; write a literal brace as {{ or }}, or give the row its own doc with (bindings, doc)` |
| A row binding a parameter the function doesn't have | `ValueError: the function does not have the parameters named by: row 'a' parameter 'nope'` |
| A `group(...)` parameter not annotated as `list[X]` or `dict[str, X]` | `TypeError: group() parameters must be annotated as their group: 'p' (int) needs list[X] for its group` |
| A `group(...)` item that isn't `source(...)` or `value(...)` | `TypeError: group() takes source(...) or value(...), not: item 0 (str)` |

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
# raises ValueError: unknown overrides: 'tota' (did you mean 'total'?)
driver.execute(["label"], inputs={"price": 1.0})
# raises ValueError: missing inputs: 'quantity' (needed by 'total')
driver.execute(["total", "price"])
# raises ValueError: missing inputs: 'price' (requested as an output, needed by 'total'); 'quantity' (needed by 'total')
```

An unknown name is followed by `(did you mean 'x'?)`, after a space, when a node is close to it: the closest one that
`difflib.get_close_matches` finds at cutoff 0.8, as for `'tota'` above. Only
[`validate`](running.md#validate), which lists every problem at once, suggests a name for a missing input: the closest
supplied name that is no node of the graph, so a misspelled key and the input it was meant for point at each other.
`execute` raises that unknown input first, on its own (Q71).

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

Some mistakes are found only with a setting, or with a function in `final_vars`. A function's problem is raised with
the request's other output problems, each kind on its own line, in one error:

| Mistake | Raises |
| --- | --- |
| A function in `final_vars` that makes several nodes, such as a `@parameterize` one (Q30) | `TypeError: final_vars items must be str or a function: function m.rows makes 2 nodes: pass 'a', 'b' instead` |
| A function in `final_vars` that no node of the driver comes from (Q30) | `ValueError: final_vars: function m.f is not the function of any node of this driver (...): pass a node name` |
| With `with_strict_overrides`, an override of an input not supplied in `inputs` too (Q75) | `ValueError: override of input 'price' refused: not supplied, though needed by 'total' (supply it in inputs= too; strict overrides are on)` |
| A value checked against an annotation that didn't resolve at build (Q81) | `TypeError: amount: cannot check a value against unresolved 'Decimal': ...` |
| With `with_output_type_checks`, a node returning a value its annotation refuses (Q87) | `TypeError: score: returned int, expected float`, as the node's failure, with its `in node` note |
| A custom value check that raises (Q21) | Its own exception, as it is: from the run's checks, or from a node when it checks a result |
| A deep check with more than 10,000 questions open about one value (see below) | `TypeError: value too cyclic to check against ...: more than 10,000 groups open about one value` |

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

`None` is a mismatch too, unless the type admits it: `Optional[T]`, `T | None`, `object`, `Any`, `Literal[None]`, or
a `TypeVar` that is free or whose bound or a constraint admits it (Q79). Annotate the parameter `float | None` to
accept it. A fixed-length tuple is checked by length and by item, so `(1,)` for `tuple[int, str]` is refused (Q80):

```python
driver.execute(["label"], inputs={"price": None, "quantity": 2})
# raises ValueError: price: expected float, got NoneType
```

A value checked against an annotation that didn't resolve at build, such as a name imported only under
`TYPE_CHECKING`, raises a `TypeError` instead, listing the plain mismatches with it (Q81):
`amount: cannot check a value against unresolved 'Decimal': it did not resolve when the graph was built; import what
it names at run time, not only under TYPE_CHECKING`. An extracted field names its producer and field first.

A [custom value check](running.md#custom-value-checks) can admit a value these refuse; one that raises fails the
run with its own exception, not a `ValueError`.

Build with `Builder.without_input_type_checks()` to skip these (see [Type checks](running.md#type-checks)). With
`Builder.with_deep_type_checks()`, a collection's items are checked too, and with `Builder.with_numeric_widening()`
an `int` fits `float` (see [Deep checks and numeric widening](running.md#deep-checks-and-numeric-widening)). A check
gives up when more than 10,000 of its questions are open about one value at once, which a value of any size or depth
never reaches: only the type's nesting is open about one value. Only a cycle no check can see, such as a type made
afresh on each reading, piles questions onto one value, and raises
`TypeError: value too cyclic to check against ...`, the type cut to 200 characters.

## When you run a prepared plan

A prepared plan accepts the input and override names it was compiled for, and no others:

```python
plan = driver.prepare(["label"], inputs={"price": 0.0, "quantity": 0})
plan.run(inputs={"price": 1.0})
# raises ValueError: inputs ['price'] differ from the prepared ['price', 'quantity']
```

## When a node raises

Your function's own exception reaches you unchanged, with a note naming the node (see
[Running a graph](running.md#exceptions-from-your-functions)). With
[`with_output_type_checks`](writing-nodes.md#checking-results), a result its annotation refuses fails the node with a
`TypeError` of its own, `score: returned int, expected float`, noted the same way; a node that raises keeps its own
error. Lineage queries raise
`ValueError: unknown nodes: ...` for a name that isn't in the graph.

## When a hook raises

A [hook](hooks.md) that raises stops the run once every hook has seen the event. One error is raised as itself.
Several are one `ExceptionGroup`, and a node's own exception is never lost: it comes first, with its `in node` note,
then the hooks' errors in the order they were raised. A group led by a node's error says so, as in
`node 'boom' failed and hooks raised`. See [A hook that raises](hooks.md#a-hook-that-raises).

`with_hooks` refuses a hook it can't call when you attach it: a `TypeError` for an object that is no `fnloom.Hook`,
or for a method that can't take one event or is an `async def`, and a `ValueError` for a hook given twice
([Checked when attached](hooks.md#checked-when-attached)).

fnloom does not log a failed node. To have each one logged, with its inputs and traceback, attach
[`fnloom.LogFailures`](hooks.md#logging-failures).

An `AsyncDriver` raises a failed node's exception the same way, after cancelling the nodes still running (see
[Async](running.md#async)). On an `AsyncDriver`, a sync node that returns a coroutine or another awaitable is an
error, never a value:

```text
TypeError: node 'f' is a sync function but returned coroutine, an awaitable; make it an async def, so the async driver awaits it
```
