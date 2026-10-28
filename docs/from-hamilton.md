# Coming from Hamilton

Good news: fnloom uses [Apache Hamilton (incubating)](https://github.com/apache/hamilton)'s model, so **a module of Hamilton
functions is already a module of fnloom nodes.** Your functions don't change. Your driver code changes in a few small,
mechanical ways.

fnloom isn't a drop-in replacement, though. There's no `hamilton` module to import and no compatibility layer.
Hamilton 1.90.0 is fnloom's test oracle: every place the two differ on purpose is a row in
[the register of differences](quirk-register.md), and most rows have a test that checks both sides.

## What you get by switching

- **Deep graphs just work.** Hamilton's graph walk recurses, so a chain of about 1,000 nodes raises `RecursionError`.
  fnloom has no depth limit.
- **Faster repeat runs.** `prepare` compiles a request once, and each run after that is a loop in Rust: up to 24×
  faster than Hamilton's `execute` on the plain graphs of the [benchmarks](benchmarks.md), and more with
  `@extract_fields`.
- **Typos are errors.** A misspelled input or override raises instead of being silently ignored.
- **Stable order.** Nodes run in the same order however you list outputs, so logs and side effects are reproducible.
- **Real types.** fnloom ships `py.typed`, and its decorators keep your function's signature for type checkers.
- **A lighter install.** No runtime dependencies, where Hamilton pulls in numpy, pandas and more.

## What carries over

- **Name-wiring.** A function's name is its node and its parameter names are its edges. A parameter that names no
  function is an input, and one with a default is optional.
- **Modules.** The public functions defined in a module are its nodes. Private and imported functions aren't.
- **The builder.** `fnloom.Builder().with_modules(...).build()` mirrors `driver.Builder().with_modules(...).build()`.
  `fnloom.build(*modules)` is the short form.
- **`execute`.** `execute(final_vars, inputs=..., overrides=...)` returns a dict of the requested outputs, in the
  order you asked for them.

## Decorators

Most decorators fnloom has so far take the same arguments as Hamilton's, so usually only the import changes.
`@cache` is the exception, below.

<div class="fl-compare" markdown>

| Hamilton 1.90.0 | fnloom |
| --- | --- |
| `from hamilton.function_modifiers import parameterize, source, value` | `from fnloom import parameterize, source, value` |
| `@parameterize(row={"param": source("node")})` | The same |
| `@parameterize(row={"param": value(v)})` | The same. `v` is passed as is, never copied, as in Hamilton |
| `@tag(owner="data")` | The same, imported from `fnloom`. A value must be a `str`, not a list (Q36) |
| `@cache(behavior="recompute")`, `@cache(behavior="disable")` | The same, imported from `fnloom` ([Caching](#caching)) |
| `@cache(behavior="ignore")` on a producer | `@cache(ignore=("name",))` on the consumer instead (Q50) |
| `@cache(format=...)` | The same, imported from `fnloom`: a format's name, or a `fnloom.Format` ([Caching](caching.md#stores)) |
| `target_=...` | Not there. fnloom adds `version=` to pin a code version |
| `@config.when`, `when_not`, `when_in`, `when_not_in` | The same, imported from `fnloom`, with the same `foo__variant` naming ([Config](writing-nodes.md#config)) |
| `@config.when(name="x")` | The same on `when`, `when_not` and `when_in`. On `when_not_in`, Hamilton reads `name` as a config key, while fnloom reads it as the node name, as on the other forms (Q59) |
| `@config.default` | Not in Hamilton: the variant used when no other matches |
| `@config(lambda cfg: ...)` | Not there |
| `@extract_fields({"a": int})`, `@extract_fields("a", "b")`, `@extract_fields()` | The same, imported from `fnloom` ([Extracting fields](writing-nodes.md#extracting-fields)). A `Mapping[str, T]` return works too (Q61) |
| `@extract_fields(..., fill_with=v)` | The same, but the producer's mapping is never changed, and `None` is a fill (Q62) |
| `@extract_columns("a", ("b", "doc"))` | The same, imported from `fnloom` ([Extracting columns](writing-nodes.md#extracting-columns)), with no pandas extension to load. A mapping or any value with `in` and `[]` works too (Q64) |
| `@extract_columns(..., fill_with=v)` | The same, but the producer's frame is never changed, and a `("b", "doc")` column fills `b` (Q64) |
| `@parameterize(row=({...}, "doc"))` | `from fnloom import parameterize`: the doc is used as written, and `None` is no doc. A tuple of any other length, or a doc that is no `str`, is a `TypeError` (Q82) |
| `{ghost}` in a `@parameterize` function's docstring prints as `ghost` | `ValueError` when decorating, naming the row and the placeholder; `{xs}` of a `group` is its items joined (Q82) |
| `group(source("a"), value(3))` and `group(k=source("a"))`, misuse refused when decorating, a mis-annotated parameter sometimes listed twice, for example a positional group on an `int` parameter | `from fnloom import group`; each offender is listed once, and a `group` source is required even when the parameter has a default (Q82) |
| `parameterize_sources(row=dict(p="x"))`, where a non-`str` source fails later with a raw `TypeError` | The same call; a non-`str` source is a `TypeError` when decorating (Q82) |
| `parameterize_values("p", {("name", "doc"): v})`, where an empty mapping builds no node | The same call; an empty mapping, or a key that is no `(name, doc)`, is refused when decorating (Q82) |

</div>

## What changes in your code

<div class="fl-compare" markdown>

| | Hamilton 1.90.0 | fnloom |
| --- | --- | --- |
| `inputs` and `overrides` | `overrides` is the second positional parameter | Keyword-only (Q31) |
| A misspelled input or override | Ignored | `ValueError` naming it (Q4, Q5) |
| `final_vars="total"` | One output per character | `TypeError` (Q6) |
| A function in `final_vars` | Its `__name__`, even when no node has that name, and a forged function is accepted | The node it makes, matched by the function itself. A `@parameterize` function is a `TypeError` listing its rows; a function no node comes from is a `ValueError` (Q30) |
| Overriding an input | Only when it's also supplied | Any node. It beats or replaces the input (Q7) ; `with_strict_overrides()` restores Hamilton's rule, and `with_declared_inputs(...)` turns a misspelled parameter into a build error (Q75) |
| A node named like a parameter that has a default | The node silently replaces the default, and a supplied input of that name is ignored | `ValueError` at build, naming the parameter and the node. `@fnloom.defaults(shadowed=("cost",))` on the consumer lets the node win (Q76) |
| Two functions with different defaults for one input | Each keeps its own default | `ValueError` at build, naming every consumer and its default. `@fnloom.defaults(differing=("shared",))` on each consumer but one keeps them (Q76) |
| An input or override naming a parameter only `value()` supplies | Silently dropped, even a wrong-typed one | `ValueError`, as for any unknown name; when another node reads the name, it's an input for that node alone, as in Hamilton (Q77) |
| `__all__` in a module | Ignored: every public function the module defines is a node | Only the functions it names are nodes, by attribute name, so a `@config` variant is listed as `foo__a`. A malformed `__all__` is a `TypeError`. A missing input named like a function it left out says so (Q73) |
| `@hamilton_exclude` | An instance, so only `@hamilton_exclude` works; `@hamilton_exclude()` raises | `@fnloom.exclude` and `@fnloom.exclude()` both work, return the function unchanged, and beat `__all__` (Q73) |
| `ad_hoc_utils.create_temporary_module(fa, fb)` | Copies the functions into a module it registers in `sys.modules`; refuses `_` names; the original function in `final_vars` is refused | `Builder().with_functions(fa, fb)`: the functions themselves, no module made or registered, the original in `final_vars` means its node ([Graphs without modules](writing-nodes.md#graphs-without-modules), Q72) |
| A lambda in `create_temporary_module` | Accepted, failing later | Needs a name: `fnloom.node(fn, name="x")`, which also binds parameters with `bind={...}` (Q72) |
| `allow_module_overrides()` | The later module's function silently replaces the earlier one, for every reader | No override: a name defined twice fails the build. To use one module twice, give it a namespace ([Namespaces](writing-nodes.md#namespaces), Q74) |
| `@subdag(module, ...)` to reuse a module | The module's nodes are named `<function>.<name>`, under a function you write | `Builder().with_modules(module, namespace="ns")` names them `ns.<name>`; inside, a parameter reads the namespace's node first, then the plain name, and inputs keep their plain names (Q74) |
| `what_is_upstream_of` | `HamiltonNode` objects | Sorted names, including the ones you named (Q28) |
| Reusing a request | Re-planned on every call | `prepare(...)` returns a plan to run again (Q3) |
| `validate_execution(final_vars, overrides, inputs)` | Returns `None` or raises one kind of problem; refuses a function in `final_vars`; a missing input names every consumer in the graph | `validate(final_vars, *, inputs, overrides)` returns every problem, one sorted line each, and names only the consumers the request needs; unknown names get a `did you mean` suggestion (Q71) |
| `what_is_the_path_between(a, b)` | `HamiltonNode` objects in an order that changes with `PYTHONHASHSEED`; the error names one unknown name | Sorted-by-dependency names, ties broken by name; both endpoints included, as in Hamilton; the error names every unknown name (Q70) |
| `graph_modules` | A tuple, keeping a module given twice | `driver.modules`: each module once, in build order (Q70) |
| `list_available_variables()` | Every node, including inputs, as objects | `driver.nodes` for the nodes; `driver.inputs()` for the inputs with their types, requiredness and consumers; `driver.outputs()` for the nodes nothing reads (Q70) |
| `htypes.check_input_type(t, v)` | Public, with Hamilton's rules | `fnloom.value_matches(t, v)` answers as the input check, and `fnloom.type_matches(produced, expected)` as the edge check ([What the checks understand](running.md#what-the-checks-understand)) |
| An override's value | Not type checked | Checked as an input is; `without_override_type_checks()` skips only the override checks (Q22) |
| After `importlib.reload(module)` | A new `Builder` to see the edit | `driver.rebuild()` builds again with every setting; the old driver keeps its old code, as Hamilton's does (Q83) |
| `with_adapters(...)` with `NodeExecutionHook` and `GraphExecutionHook` | `run_before_node_execution(**kwargs)` and its kin, each taking keyword arguments; the after-graph hook gets no request | `with_hooks(...)` with a `fnloom.Hook`: `before_run`, `before_node`, `after_node` and `after_run`, each taking one frozen event, and `RunEnd` repeats the request ([Hooks](hooks.md), Q84) |
| An object that is no adapter, or one given twice, in `with_adapters` | Ignored; called once | `TypeError`; `ValueError` (Q84) |
| A hook method that can't take its arguments, or an `async def` hook on the sync driver | Fails at the first call; never awaited | `TypeError` naming the method, from `with_hooks` (Q84) |
| `with_adapters(MyResult())`, a `ResultBuilder` whose `build_result(**outputs)` makes the result | It gets the outputs as keywords | `with_result_builder(build)`: any callable, given the outputs as one dict, in the same order. `execute`'s type follows it ([Result builders](running.md#result-builders), Q19) |
| `with_adapters(base.PandasDataFrameResult())` | Flattens frame outputs into `output.column` columns and logs index warnings | No pandas builder: `with_result_builder(pandas.DataFrame)`, or `polars.DataFrame`, builds by the library's own rules ([pandas and polars frames](running.md#pandas-and-polars-frames), Q89) |
| Two result builders | `ValueError` at `build()`, naming the method | `ValueError` from the second `with_result_builder`, naming both (Q24) |
| `raw_execute(final_vars, overrides, display_graph, inputs)` | Positional, with a `display_graph` flag | `raw_execute(final_vars, *, inputs, overrides)`, and `PreparedPlan.run_raw` (Q19) |

</div>

## What changes in behaviour

<div class="fl-compare" markdown>

| | Hamilton 1.90.0 | fnloom |
| --- | --- | --- |
| A chain of about 1,000 nodes | `RecursionError` | Runs. Nothing recurses over depth (Q25) |
| A dependency cycle | `RecursionError` when executed | `ValueError` naming it, at build (Q27) |
| Execution order | Depends on the order of outputs and parameters | Topological, ties broken by name (Q26) |
| `*args`, `**kwargs`, positional-only | Treated as inputs | `ValueError` at build (Q29) |
| `async def` and generator functions | The coroutine or generator is the value | `ValueError` at `build()`; `build_async()` awaits an `async def` (Q32) |
| Building an async driver | `await async_driver.Builder()...build()` | `Builder().build_async()`, not awaited (Q12) |
| An async driver's result | A DataFrame from `AsyncDriver(...)`; a dict, sorted by name, from its builder | The sync driver's dict, in request order (Q11) |
| A sync node in an async driver | Runs on the event loop, which stops while it does | Runs in a worker thread (`loop.run_in_executor`), or on the loop with `sync_nodes="inline"` (Q65) |
| A failing node in an async driver | Its siblings keep running after `execute` raises | They're cancelled; its own exception is raised once, noted with its name (Q66) |
| `None` for an input declared `x: int` | Passes the check; the node then runs with `None` | `ValueError` before anything runs; annotate `x: int \| None` (Q79) |
| A `tuple[int, str]` input | Every tuple refused, a correct one too | Checked by length and each item (Q80) |
| An annotation imported only under `TYPE_CHECKING` | `NameError` at build, naming no node | Builds as `unresolved 'Decimal'`; a value checked against it is a `TypeError` saying to import it (Q81) |
| An `int` for `float`, a numpy scalar for `int` | Refused | Refused too, unless built `with_numeric_widening()`; items of a collection with `with_deep_type_checks()` |
| Inputs to an async driver | Types unchecked; `NotImplementedError` or `KeyError` for a missing one | Checked as the sync driver checks them, with the same errors (Q67) |
| An `async def` under `@parameterize`, in an async driver | Each row's value is an un-awaited coroutine | Awaited, as any `async def` node (Q69) |
| Hamilton's `AsyncDriver` on Python 3.14 | `DeprecationWarning` on every node; breaks on 3.16 | No warning (Q13) |
| An input nobody supplied, requested as an output | `None` for it | `ValueError: missing inputs: 'x' (requested as an output)` (Q78) |
| One driver shared between threads | Works, undocumented | A documented guarantee, stress-tested on the default and free-threaded builds ([Threads](running.md#threads)) |
| A function passed where a module goes | An empty graph | `TypeError` (Q52) |
| A hook that raises | The later hooks of the event are skipped; a raising before-graph hook skips after-graph too | Every hook sees the event, and after-run always fires (Q23) |
| A failing node and a raising after-node or after-graph hook | The hook's error, with the node's only in `__context__` | One `ExceptionGroup`, the node's error first (Q23) |
| Hooks on a cache hit | Fire as if the node ran, for the hit and every node upstream of it, with nothing saying it was a hit | The hit reports once, with `cache` set to its outcome, `inputs=None` and the loaded value; nodes only hits need report nothing ([Hooks](hooks.md#with-a-cache), Q85) |
| A sync hook on the async driver | Called when a node is scheduled, with `Task`s for its arguments and result; a failing node is reported a success | Called as the node starts and finishes, on the loop's thread, with real values and `ok=False` for a failure (Q86) |
| What after-graph gets as `results` with a result builder | The built result | `RunEnd.results` is always the raw dict (Q19) |
| A `ResultBuilder` on the async driver | Silently dropped: `execute` returns the dict | Used, as on the sync driver (Q19) |
| A module with no nodes, such as a package that only re-exports | Builds; its nodes are unknown at `execute` | `ValueError` at build, saying why each function was skipped (Q53) |
| A row naming no parameter of its function | `InvalidDecoratorException` | `ValueError`, when decorating (Q33) |
| A row's defaulted parameter bound to an unsupplied `source()` | `KeyError` when run | The default applies (Q35) |
| A config key | A node, passed to parameters of its name | Never a node or a value; a parameter of that name is an input (Q56) |
| A config key the config doesn't have | Reads as `None`, so `when_not` matches | Never matches (Q57) |
| A needed node no config variant matches | An input, failing at `execute` | `ValueError` at build, naming what each variant needs (Q57) |
| Two matching variants, or a variant defined twice | Names the wrong function; keeps the last silently | `ValueError` at build naming both, or the lines (Q58) |
| `@config.when` without parentheses, `when_in(key="ab")` | The node vanishes; substring matching | Refused when decorating (Q59) |
| The function's `__name__`, and its tags | Renamed in place; a `hamilton.config` tag | Untouched; `NodeView.config` holds the conditions (Q60) |
| An extracted field of the wrong type, or missing | Passed on unchecked; `InvalidDecoratorException` when run | `ValueError` when run, also with input checks off (Q61) |
| `@extract_fields({"a": str})` on a `dict[str, int]` return | Builds | `ValueError` at build (Q61) |
| `@extract_fields` with a mapping and names, or a name the `TypedDict` lacks | Drops the names; a bare `KeyError` | `ValueError` when decorating (Q61) |
| An input or field annotated with a `TypedDict` | The input check raises `TypeError` | Any `dict` is accepted (Q61) |
| `@tag` on an `@extract_fields` or `@extract_columns` function | Tags the fields or columns only | Tags the producer too (Q63) |
| `@extract_columns` with extensions not autoloaded, or on a `dict` return | Refused when decorating | Works: a frame's columns are its `Series`, a mapping's are typed `Any` (Q64) |
| A column named twice in `@extract_columns`, as `"a", "a"` or `"a", ("a", "doc")` | `ValueError` at build | `ValueError` when decorating, listing every repeat (Q64) |
| A column missing, or named twice in the frame | `InvalidDecoratorException` when run; a DataFrame, unchecked | `ValueError` when run, naming the columns returned; each column is checked against its type (Q64) |

</div>

## Caching

fnloom's cache keeps Hamilton's names for what they mean, and differs wherever Hamilton's cache can return a wrong
value (Q46 to Q51 in [the register](quirk-register.md)). The guide is [Caching](caching.md).

<div class="fl-compare" markdown>

| | Hamilton 1.90.0 | fnloom |
| --- | --- | --- |
| Turning it on | `Builder().with_cache(path=...)` | `Builder().with_cache(fnloom.DiskCache(path))`, or `fnloom.MemoryCache()` |
| Per-node behavior | `@cache(behavior=...)`, or lists of names given to `with_cache` | `@cache(behavior=...)` on the function only |
| `ignore` | `@cache(behavior="ignore")` on the producer hides it from every consumer | `@cache(ignore=("name",))` on a consumer, per parameter; the producer is `disable` (Q50) |
| A code version | The function's source, hashed on every run | Its code, helpers, constants and libraries, read once at build (Q16, Q48) |
| A helper or global the node reads | Not seen | Seen; a mutable global makes the node uncacheable unless it gives `@cache(version=...)` |
| A value that cannot be hashed, nested in a list | A constant hash | The nodes reading it run, and `explain_cache` says why (Q47) |
| `@cache(format=...)` | Formats from a registry of data savers; an unknown name fails every run; `json` turns a tuple into a list on a hit | `"pickle"`, `"json"` or a `fnloom.Format`, checked at build (Q55); `json` stores only what loads back exactly (Q54) |
| A corrupt stored entry | Fails the run | A miss, with a `CacheWarning` (Q51) |
| What a run did with each node | A log that reports a hit on every lookup | `driver.explain_cache(...)` |
| A cache on the async driver | Fails at build | The same keys, store and `explain_cache` as the sync driver (Q68) |

</div>

## Not there yet

fnloom is pre-alpha. The core, the native path, `@parameterize` and its sugar, `@tag`, `@config`, `@extract_fields`,
`@extract_columns`, `@exclude`, graph inspection, `validate`, graphs from plain functions, namespaces, type checks,
result caching, the async driver, hooks and result builders are done. These Hamilton
features come next, roughly in this order:

1. **Graph visualisation.**
2. **Dynamic execution**, materializers, `@subdag`, `@check_output` and `@pipe`.

If your pipeline depends on one of these, it's worth waiting for it before you switch.
