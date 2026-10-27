# Hamilton quirk register

Every place fnloom deliberately differs from Apache Hamilton 1.90.0, what it does instead, and the test that pins it.
fnloom fixes Hamilton's quirks by default, but each change needs a reason that holds for any user, not just one
workload.

!!! tip "Just moving code over?"

    [Coming from Hamilton](from-hamilton.md) summarises the rows that affect your code. This page is the full
    reference.

**What the dispositions mean:**

- **KEEP**: fnloom does what Hamilton does.
- **FIX**: deliberately different, with a test.
- **REFUSE**: Hamilton silently accepts something dubious; fnloom raises.
- **DROP**: not carried over.
- **DEFER**: to be decided later.

**Evidence.** The behaviours below were first reported by an earlier investigation (2026-10-22) against Hamilton 1.90.0
and partly confirmed by reading its source. Rows Q4, Q5, Q6, Q7, Q10, Q22 and Q25 to Q45 have differential tests in
`tests/differential/test_deliberate_differences.py`, each marked `deliberate("<id>")`, that assert Hamilton's behaviour
and fnloom's side by side; Q3 and Q23 are covered by fnloom-only tests (`tests/test_driver.py`, `tests/test_binding.py`
and the executor's unit tests). Every other row is not yet re-run for this repository and becomes a differential test
before it is relied on. Source line numbers are deliberately omitted.

| ID | Hamilton behaviour | Disposition | fnloom behaviour |
|---|---|---|---|
| Q1 | Ships no `py.typed`; decorator `__call__` is unannotated, so decorated functions lose their signature for type checkers. | FIX | Typed package with `py.typed`; decorators return the function typed as itself. |
| Q2 | No public graph-inspection API; callers reach into `Driver.graph`, `FunctionGraph` and `Node` internals. | FIX | `Driver.nodes`: a read-only mapping, sorted by name, of a frozen `NodeView` per node and input, with its name, resolved type, dependencies, `is_input`, origin function, module, params, tags and docstring. The node's own callable is not exposed: a prepared plan replaces the replay it was used for. |
| Q3 | Every `execute` re-walks the subgraph, re-validates inputs and the executor, and generates a run id. | FIX | Compile once per (outputs, input names, override names); `prepare(...)` returns a reusable plan and `execute` uses it, keeping the 128 most recently used plans per driver. |
| Q4 | An input name that is not an input of the graph is accepted silently. | REFUSE | `ValueError` naming the culprit. |
| Q5 | A misspelled `overrides` key is ignored. | REFUSE | `ValueError`. |
| Q6 | A bare `str` passed as `final_vars` is split into one output per character. | REFUSE | `TypeError`. |
| Q7 | An `overrides` key naming an input is honoured when that input is also supplied and refused ("Required input … not provided") when it is not. | FIX | Pass-through: an override may name any node, input or derived; it beats a supplied input and satisfies a missing one. |
| Q8 | The order in which inputs are listed is not specified. | FIX | One deterministic order everywhere: listing APIs are sorted by node name; a result dict is in requested order, each output once (first occurrence), as in Hamilton. |
| Q10 | Edge and input type checks can only be switched off together, by an adapter (`NoEdgeAndInputTypeChecking`) that also changes other behaviour. | FIX | Edge checks always on at build; input and override checks on by default; `Builder.without_input_type_checks()` turns off only those, and a mismatched edge still fails the build. Pinned by `test_q10_opt_out_is_explicit`, and by `tests/test_type_checks.py`. |
| Q11 | `AsyncDriver` defaults to a pandas DataFrame result; the sync `Builder` defaults to a dict. | FIX | Dict everywhere. |
| Q12 | `async_driver.Builder.build` is async, so it cannot be cached the usual way. | FIX | Construction is always synchronous; only `execute` is async. |
| Q13 | `AsyncDriver` uses `asyncio.iscoroutinefunction`, deprecated and scheduled for removal. | FIX | `inspect.iscoroutinefunction`. |
| Q14 | Extension autoload imports heavy libraries (xgboost, sklearn, …) on `import hamilton` unless disabled by an environment variable. | DROP | No autoload. |
| Q15 | Every failed `execute` logs a community-promotion banner at ERROR level. | DROP | No banner. |
| Q16 | The shipped cache re-hashes source on every execute, so a hit can cost more than recomputing. | DEFER | When caching is built, hash code objects once at compile. |
| Q19 | `Driver.execute` returns different types depending on result builder and adapters. | FIX | Dict by default; builders are explicit. |
| Q20 | Depending on the package pulls in the whole runtime and its dependencies. | FIX | No required dependency beyond the interpreter; numpy optional. |
| Q21 | An adapter implementing the input-check hook **replaces** Hamilton's default check rather than adding to it. | FIX | Custom checks are additive: a value passes if the default check OR a custom check accepts it. |
| Q22 | Override values bypass type checking and fail inside the node, or flow on as the wrong type. | FIX | Override values are checked against the node's declared type, like inputs, and a mismatch is a `ValueError` before any node runs. Pinned by `test_q22_override_type_checked`, and by `tests/test_type_checks.py`. |
| Q23 | A hook that raises aborts the run and skips the other adapters' hooks for that event. | FIX | Every adapter's hook for an event runs, then the first error is raised (an `ExceptionGroup` when several fail). Ordering is documented. |
| Q24 | Two adapters cannot implement the same lifecycle method. | KEEP / FIX | KEEP for the result builder (naturally singular); execution wrappers compose as an ordered chain when they ship. |
| Q25 | The graph walk is recursive, so a chain of about 1,000 dependent nodes raises `RecursionError` (verified at 1,000 and 5,000 on 1.90.0). | FIX | No recursion over graph depth anywhere; a 5,000-node chain runs. |
| Q26 | Execution order follows a recursive depth-first walk from the requested outputs, so it depends on the order the outputs are listed in and on parameter order, and it is visible to hooks and side effects. | FIX | Topological order with ties broken by node name, independent of output order. |
| Q27 | A dependency cycle is not detected up front: the recursive walk overflows and raises `RecursionError`. | FIX | `ValueError` naming the cycle, raised by `Graph::build`, that is by `fnloom.build` and `Builder.build`, before any request. |
| Q28 | `what_is_upstream_of` and `what_is_downstream_of` return `HamiltonNode` objects. | FIX | The same nodes, the named ones included, as sorted names; `Driver.nodes[name]` gives each one's typed view (Q2). |
| Q29 | A positional-only, `*args` or `**kwargs` parameter is accepted and treated as an input of that name. | REFUSE | `ValueError` at build: fnloom calls a node with one keyword argument per parameter name, and none of these kinds binds such an argument as a plain parameter. |
| Q30 | `final_vars` may name an output by passing its function. | DEFER | `TypeError`: mapping a callable to its node names depends on decorators, which come in the next plan. |
| Q31 | `execute(final_vars, overrides, display_graph, inputs)` takes `overrides` second, so a positional mapping is read as overrides. | FIX | `inputs` and `overrides` are keyword-only on `execute`, `prepare` and `PreparedPlan.run`; a positional mapping is a `TypeError`. |
| Q32 | An `async def` or generator function is accepted as a node, and the sync driver returns the un-awaited coroutine or the generator object as its value. | REFUSE | `ValueError` at build naming the function: nodes are called synchronously, and calling these does not run the body to a value. |
| Q33 | A `@parameterize` row naming a parameter the function does not have raises `InvalidDecoratorException`, an `Exception` subclass that is not a `ValueError`, when the decorator is applied. | FIX | `ValueError`, also when the decorator is applied, listing every unknown row parameter, sorted, like every other malformed-function error. |
| Q34 | Every function node carries a `module` tag naming its module, beside the tags the author wrote; inputs carry no tags (`{}`). | FIX | A node's tags are only what the author wrote with `@tag`; its module is `NodeView.module`. Inputs carry no tags (`{}`) and their `module` is `None`, so the test compares inputs as `{}` on both sides. |
| Q35 | A `@parameterize` row binding a parameter that has a default to `source("x")` makes `x` optional, but when `x` is not supplied the node raises `KeyError` instead of using the default. | FIX | The keyword is left out, so the function's own default applies, exactly as for a plain optional parameter. |
| Q36 | `@tag` accepts a list of strings as a tag value, as well as a string. | REFUSE | A tag value must be a `str`, because `NodeView.tags` is a `Mapping[str, str]`, so a tag always reads as one string; anything else is a `TypeError` when the decorator is applied, naming the tag. |
| Q37 | `tuple[int, ...]` is not treated as a generic: the input check rejects `(1, 2)`, and a node returning `tuple[int, ...]` does not feed a `Sequence[int]` parameter, because the `...` is compared as a second argument. | FIX | `tuple[T, ...]` takes a tuple, as `list[T]` takes a list (its items unchecked, as in Hamilton), and feeds `Sequence[T]`: across kinds its one element type is what is compared. |
| Q38 | A `NewType` over `int` takes no value (its values are plain `int`s at runtime, so even `1` is rejected), and a node returning it feeds neither `int` nor any type `int` would feed. | FIX | A `NewType` answers as its supertype: `1` fits it, and it feeds whatever its supertype feeds. Only the `NewType` itself (or `Any`) feeds a parameter annotated with it, as in Hamilton. |
| Q39 | A node returning `Literal[1, 2]` feeds only `Literal[1, 2]` and `Any`, not `int`, `object` or a union holding `int`. | FIX | A `Literal` producer answers as the types of its values: it feeds a parameter when each value's type does. |
| Q40 | A node returning a union (`int \| None`, `int \| str`) feeds only that same union and `Any`, not even `object`. | FIX | A union producer feeds a parameter when every member does, so `int \| None` feeds `object` and `int \| str \| None`; a union parameter still takes a producer that feeds one member. |
| Q41 | `Annotated[T, ...]` is checked by exact type in the input check, so `True` fits `int` but not `Annotated[int, "m"]`, and `Child()` does not fit `Annotated[Base, "m"]`. | FIX | Metadata never changes an answer: `Annotated[T, ...]` is checked as `T`, on both the input and the edge check. |
| Q42 | A parameter annotated with a plain (not runtime-checkable) `Protocol` and fed by a node returning a class makes the build raise the `TypeError` of `issubclass`, which names no node; a value that conforms to a `runtime_checkable` protocol is rejected by the input check. | FIX | A plain protocol cannot be checked at runtime, so it is no match (`False`), never an error; a `runtime_checkable` protocol takes any value that `isinstance` accepts. A plain-protocol input still takes no value but `None`, as in Hamilton. |
| Q43 | The input check reads the first item of a value declared `Iterable[T]` to check it, so a generator or other iterator supplied as an input loses that item before the node runs, and is accepted or rejected by it. | FIX | An iterator is never looked inside: it is checked by its kind alone (it fits `Iterable[T]` and `Iterator[T]`, not `list[T]`), and reaches the node with every item. |
| Q44 | A PEP 695 alias (`type Price = float`) is compared as itself: the input check takes no `float` for a `Price` input, and a node returning `float` feeding a `Price` parameter, or returning `Price` feeding a `float` one, fails the build; the opt-out is the only way through, and it drops every check. | FIX | An alias is checked as what it names, read through any number of aliases, on the input and the edge check. A subscripted generic alias (`Pair[int]` for `type Pair[T] = tuple[T, T]`) is read with its arguments put in when its value is one of its type parameters or a generic over exactly those parameters, in order; any other generic alias answers as before. An alias nested inside another generic's arguments (`list[Price]` fed by `list[float]`) is compared as written on the edge check. Aliases naming each other are compared as written. Every alias's value is evaluated at build, so an alias over a name imported only for type checkers fails the build with the `NameError` and note of any unresolvable annotation. |
| Q45 | `Mapping[str, int]` takes no `dict`, `Callable[[int], int]` takes no function, and likewise every other parameterized abstract collection outside the `Sequence` and `Iterable` families (`MutableMapping`, `Set`, `Collection`, `MutableSequence`, ...): the input check refuses every value but `None`. | FIX | A parameterized `collections.abc` type outside those two families is checked by its kind alone, with `isinstance`, its items or signature unchecked; `Sequence[T]` and `Iterable[T]` keep the first-element check, and an iterator is still never read (Q43). The edge check already agrees with Hamilton on these forms (`dict[str, int]` feeds `Mapping[str, int]`) and is unchanged. |

## Not yet decided

Array shape/dtype contracts, numeric widening (numpy scalars for `int`/`float`) and length alignment for array-valued
inputs were found against one consumer's numpy workloads, not against Hamilton in general. They are out of core and
may return as an opt-in policy once a general-user case exists.
