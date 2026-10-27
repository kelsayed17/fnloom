# Hamilton quirk register

Every place fnloom deliberately differs from Apache Hamilton 1.90.0, with the disposition and the test that pins it.
fnloom fixes Hamilton's quirks by default; a deviation needs a reason that holds for any user.

**Dispositions.** KEEP: reproduce Hamilton. FIX: deliberately different, with a test. REFUSE: Hamilton silently
accepts something dubious, fnloom raises. DROP: not carried over. DEFER: decided later.

**Evidence.** The behaviours below were first reported by an earlier investigation (2026-10-22) against Hamilton
1.90.0 and partly confirmed by reading its source. Rows Q4, Q5, Q6, Q7, Q25 to Q33 and Q35 have differential tests in
`tests/differential/test_deliberate_differences.py`, each marked `deliberate("<id>")`, that assert Hamilton's
behaviour and fnloom's side by side; Q3 and Q23 are covered by fnloom-only tests (`tests/test_driver.py`,
`tests/test_binding.py` and the executor's unit tests). Every other row is not yet re-run for this repository and
becomes a differential test before it is relied on. Source line numbers are deliberately omitted.

| ID | Hamilton behaviour | Disposition | fnloom behaviour |
|---|---|---|---|
| Q1 | Ships no `py.typed`; decorator `__call__` is unannotated, so decorated functions lose their signature for type checkers. | FIX | Typed package with `py.typed`; decorators return the function typed as itself. |
| Q2 | No public graph-inspection API; callers reach into `Driver.graph`, `FunctionGraph` and `Node` internals. | FIX | A public, typed, read-only graph view: name, type, dependencies, is-input, callable, tags. |
| Q3 | Every `execute` re-walks the subgraph, re-validates inputs and the executor, and generates a run id. | FIX | Compile once per (outputs, input names, override names); `prepare(...)` returns a reusable plan and `execute` uses it, keeping the 128 most recently used plans per driver. |
| Q4 | An input name that is not an input of the graph is accepted silently. | REFUSE | `ValueError` naming the culprit. |
| Q5 | A misspelled `overrides` key is ignored. | REFUSE | `ValueError`. |
| Q6 | A bare `str` passed as `final_vars` is split into one output per character. | REFUSE | `TypeError`. |
| Q7 | An `overrides` key naming an input is honoured when that input is also supplied and refused ("Required input … not provided") when it is not. | FIX | Pass-through: an override may name any node, input or derived; it beats a supplied input and satisfies a missing one. |
| Q8 | The order in which inputs are listed is not specified. | FIX | One deterministic order everywhere: listing APIs are sorted by node name; a result dict is in requested order, each output once (first occurrence), as in Hamilton. |
| Q10 | Edge and input type checks can only be switched off by an adapter that also changes other behaviour. | FIX | Edge checks always on at compile; input checks on by default; an explicit per-driver opt-out. |
| Q11 | `AsyncDriver` defaults to a pandas DataFrame result; the sync `Builder` defaults to a dict. | FIX | Dict everywhere. |
| Q12 | `async_driver.Builder.build` is async, so it cannot be cached the usual way. | FIX | Construction is always synchronous; only `execute` is async. |
| Q13 | `AsyncDriver` uses `asyncio.iscoroutinefunction`, deprecated and scheduled for removal. | FIX | `inspect.iscoroutinefunction`. |
| Q14 | Extension autoload imports heavy libraries (xgboost, sklearn, …) on `import hamilton` unless disabled by an environment variable. | DROP | No autoload. |
| Q15 | Every failed `execute` logs a community-promotion banner at ERROR level. | DROP | No banner. |
| Q16 | The shipped cache re-hashes source on every execute, so a hit can cost more than recomputing. | DEFER | When caching is built, hash code objects once at compile. |
| Q19 | `Driver.execute` returns different types depending on result builder and adapters. | FIX | Dict by default; builders are explicit. |
| Q20 | Depending on the package pulls in the whole runtime and its dependencies. | FIX | No required dependency beyond the interpreter; numpy optional. |
| Q21 | An adapter implementing the input-check hook **replaces** Hamilton's default check rather than adding to it. | FIX | Custom checks are additive: a value passes if the default check OR a custom check accepts it. |
| Q22 | Override values bypass type checking and fail inside the node. | FIX | Override values are checked against the node's declared type, like inputs. |
| Q23 | A hook that raises aborts the run and skips the other adapters' hooks for that event. | FIX | Every adapter's hook for an event runs, then the first error is raised (an `ExceptionGroup` when several fail). Ordering is documented. |
| Q24 | Two adapters cannot implement the same lifecycle method. | KEEP / FIX | KEEP for the result builder (naturally singular); execution wrappers compose as an ordered chain when they ship. |
| Q25 | The graph walk is recursive, so a chain of about 1,000 dependent nodes raises `RecursionError` (verified at 1,000 and 5,000 on 1.90.0). | FIX | No recursion over graph depth anywhere; a 5,000-node chain runs. |
| Q26 | Execution order follows a recursive depth-first walk from the requested outputs, so it depends on the order the outputs are listed in and on parameter order, and it is visible to hooks and side effects. | FIX | Topological order with ties broken by node name, independent of output order. |
| Q27 | A dependency cycle is not detected up front: the recursive walk overflows and raises `RecursionError`. | FIX | `ValueError` naming the cycle, raised by `Graph::build`, that is by `fnloom.build` and `Builder.build`, before any request. |
| Q28 | `what_is_upstream_of` and `what_is_downstream_of` return `HamiltonNode` objects. | DEFER | The same nodes, the named ones included, as sorted names; a typed graph view (Q2) comes in a later plan. |
| Q29 | A positional-only, `*args` or `**kwargs` parameter is accepted and treated as an input of that name. | REFUSE | `ValueError` at build: fnloom calls a node with one keyword argument per parameter name, and none of these kinds binds such an argument as a plain parameter. |
| Q30 | `final_vars` may name an output by passing its function. | DEFER | `TypeError`: mapping a callable to its node names depends on decorators, which come in the next plan. |
| Q31 | `execute(final_vars, overrides, display_graph, inputs)` takes `overrides` second, so a positional mapping is read as overrides. | FIX | `inputs` and `overrides` are keyword-only on `execute`, `prepare` and `PreparedPlan.run`; a positional mapping is a `TypeError`. |
| Q32 | An `async def` or generator function is accepted as a node, and the sync driver returns the un-awaited coroutine or the generator object as its value. | REFUSE | `ValueError` at build naming the function: nodes are called synchronously, and calling these does not run the body to a value. |
| Q33 | A `@parameterize` row naming a parameter the function does not have raises `InvalidDecoratorException`, an `Exception` subclass that is not a `ValueError`, when the decorator is applied. | FIX | `ValueError`, also when the decorator is applied, listing every unknown row parameter, sorted, like every other malformed-function error. |
| Q35 | A `@parameterize` row binding a parameter that has a default to `source("x")` makes `x` optional, but when `x` is not supplied the node raises `KeyError` instead of using the default. | FIX | The keyword is left out, so the function's own default applies, exactly as for a plain optional parameter. |

## Not yet decided

Array shape/dtype contracts, numeric widening (numpy scalars for `int`/`float`) and length alignment for array-valued
inputs were found against one consumer's numpy workloads, not against Hamilton in general. They are out of core and
may return as an opt-in policy once a general-user case exists.
