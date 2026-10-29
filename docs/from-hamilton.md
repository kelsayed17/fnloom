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
| `@check_output(range=..., importance="warn")` | `from fnloom import check_output`, checked inside the node: no `x_raw` or validator node. `importance` defaults to `"fail"`, and a failure is `fnloom.ValidationError`, a `ValueError`. Checks read any type, generics and unions included ([Checking values](writing-nodes.md#checking-values), Q99 to Q101) |
| `@check_output_custom(MyValidator("fail"))` | `check_output_custom` takes plain functions or `fnloom.Validator`s, which may read the node's `inputs`; `applies_to` is called at build (Q102) |
| Two `@check_output` on one function, or one above or below `@extract_fields` or `@pipe_output` | Stack in any number and order; a check sees the final value, and `field="p"` checks a field (Q103) |
| `@pipe_input(step(f, n=1))`, `@pipe_output(...)`, `step(...).named(...)`, `.when(...)` | The same, imported from `fnloom`, with the same `<fn>.with_<step>` and `<fn>.raw` names ([Steps](writing-nodes.md#steps), Q104) |
| A step parameter with a default becoming a graph input | It keeps its default and is never an input (Q105) |
| `@mutate(target)` and `apply_to(target, ...)` | The same, imported from `fnloom`; a target in another module works when both are built (Q107) |
| `@inject(param=source("x"))` | The same, imported from `fnloom` ([Bindings and decorators from config](writing-nodes.md#bindings-and-decorators-from-config), Q109) |
| `@resolve(when=ResolveAt.CONFIG_AVAILABLE, decorate_with=...)` with `hamilton.enable_power_user_mode` | `@fnloom.resolve(decorate_with)`, with no power-user mode (Q110) |
| `@pipe`, `@flow`, `collapse=True`, `@check_output(schema=...)` | Not there: `pipe` is a deprecated alias of `pipe_input`, `flow` doesn't exist in 1.90.0, `collapse=True` raises `NotImplementedError` there, and a pandera schema is a custom check |
| `@pipe_input(..., namespace=...)`, `.named(name, namespace=...)`, `@pipe_output(..., on_output=...)` | Not there: a step's node is always `<node>.<label>`, and `.on_output(...)` on each step picks the fields |

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
| `with_adapters(lifecycle.FunctionInputOutputTypeChecker())` | Checks inputs and results by `isinstance` rules of its own: crashes on fixed tuples, parameterized generics and `NewType`s, and reports a failing node as returning `None` | `with_output_type_checks()` checks every result by the input checks' rules; a node that raises keeps its own error. Inputs and overrides are always checked ([Checking results](writing-nodes.md#checking-results), Q87) |
| An `EdgeConnectionHook` whose `validate_input` (`do_validate_input`) decides an input's type | Replaces the default check, so it can refuse a correct value | `with_value_check(check)` adds to the default: `check(declared, value)` is asked only about a value the default refuses, and can only admit it. It applies to overrides, extracted fields, results and `validate` too ([Custom value checks](running.md#custom-value-checks), Q21) |
| `raw_execute(final_vars, overrides, display_graph, inputs)` | Positional, with a `display_graph` flag | `raw_execute(final_vars, *, inputs, overrides)`, and `PreparedPlan.run_raw` (Q19) |
| `with_adapters(lifecycle.PrintLn())` | Prints each node's name and time, and with `verbosity=2` its inputs and result | No built-in: a three-line hook, a `fnloom.Hook` subclass whose `after_node(self, event)` prints `event.node.name` and `event.result` or `event.error` ([Hooks](hooks.md)) |
| A failing node logged at ERROR | Always, with a support banner | Off by default; `with_hooks(fnloom.LogFailures())` logs the node and its inputs, each cut to 50 characters ([Logging failures](hooks.md#logging-failures), Q88) |
| `GraphConstructionHook`'s `run_after_graph_construction` | Called with the built graph | Read `driver.nodes` after `build()` |
| `lifecycle.NoEdgeAndInputTypeChecking()` | Turns off the input and edge checks | `without_input_type_checks()`; edges are always checked, at build (Q10) |
| A `StaticValidator`, refusing a node or the graph at build | `run_to_validate_node` and `run_to_validate_graph` | Not yet. Until then, check `driver.nodes` after `build()` and raise |
| `NodeExecutionMethod` (`run_to_execute_node`) | Replaces how a node is called; one per driver | `with_node_wrappers(*wrappers)`: any number, an ordered chain, each `wrapper(node, call, inputs)` ([Node wrappers](running.md#node-wrappers), Q111) |
| `lifecycle.default.GracefulErrorAdapter` | Keeps running past a failure, skipping what depends on it | Not yet: on the [roadmap](plans/2026-10-28-roadmap-ahead-of-hamilton.md). A failing node fails the run, and `after_node` sees its error and inputs |
| `TaskExecutionHook` and the other task hooks | Hooks on dynamic execution's tasks | None: fnloom has no dynamic execution yet |
| `display_all_functions()`, `display_upstream_of(*names)`, `display_downstream_of(*names)` | A `graphviz.Digraph`, or `None` and a logged traceback without the graphviz package; node order follows `PYTHONHASHSEED` | `draw()`, `draw_upstream_of(*names)`, `draw_downstream_of(*names)`: a `fnloom.Drawing`, whose `to_dot()` and `to_mermaid()` need nothing installed and give the same text every run ([Drawing](drawing.md), Q90, Q94) |
| `visualize_path_between(a, b, strict_path_visualization=True)` | The path, and its nodes' parents unless strict | `draw_path_between(a, b, context=False)`; with `context=True`, the parents are drawn and marked `context` (Q90) |
| `visualize_execution(final_vars, inputs=, overrides=, bypass_validation=True)` | The run's nodes; an input requested as an output is unmarked | `draw_execution(final_vars, inputs=, overrides=, validate=False)`; that input is marked `output` (Q92) |
| A type in a drawing | `list`, `dict`, `Union` | The whole annotation, `dict[str, list[int]]` (Q91) |
| `output_file_path="graph.svg"`, `keep_dot=True` | Renders through the graphviz package | `drawing.render("graph.svg")` runs Graphviz's `dot`; `render("graph.dot")` writes the DOT text with nothing installed (Q94) |
| `graphviz_kwargs`, `custom_style_function`, `show_schema` | Style the `Digraph` | None: edit the DOT text, or build your own from `Drawing.nodes` |
| `export_execution(final_vars, inputs, overrides)` | A JSON string; an input carries a consumer's source and version | `export(final_vars, *, inputs=, overrides=)`, or `export()` for the whole graph: a `dict` with a format version, config, the request's names, code versions and a graph version ([Exporting](drawing.md#exporting), Q95, Q96) |
| The `hamilton` CLI: `build`, `view`, `validate`, `version`, `diff` | Needs `typer`; files compiled alone; config only from a context file | `fnloom draw`, `export`, `validate` and `diff OLD.json NEW.json`, with `--config`, `--input`, `--override` and `--context`; modules import their siblings ([The CLI](drawing.md#the-cli), Q97, Q98) |

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
| A node returning a value its annotation refuses | Never checked by default | Never checked by default either; `with_output_type_checks()` fails the node with a `TypeError` (Q87) |
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

## Hamilton features fnloom leaves out

fnloom doesn't build these Hamilton features, because plain Python, or something fnloom already has, gets the same
result with less to learn. Each section shows the fnloom way. Each decision is a register row.

### `@does`

Call the function. The node is named, typed and cached as written, and the two signatures are type-checked like any
call (Q113):

```python
# file: summing.py
def _sum_all(a: int, b: int) -> int:
    return a + b


def total(x: int, y: int) -> int:
    """x plus y."""
    return _sum_all(a=x, b=y)
```

```python
import fnloom
import summing

fnloom.build(summing).execute(["total"], inputs={"x": 1, "y": 2})
# -> {'total': 3}
```

`fnloom.node(fn, name=..., bind=...)` makes the node from the function itself, with no wrapper:

```python
driver = (
    fnloom.Builder()
    .with_functions(
        fnloom.node(summing._sum_all, name="summed", bind={"a": fnloom.source("x")})
    )
    .build()
)
driver.execute(["summed"], inputs={"x": 1, "b": 2})
# -> {'summed': 3}
```

### `@schema.output`

Hamilton's `@schema.output` records a tag that nothing checks. To check a frame's columns, use
`@check_output_custom`, which fails the node with a `ValidationError` by default. To record them without checking, use
`@tag` (Q114):

```python
# file: framed.py
import fnloom


def _has_columns(value: dict) -> str | None:
    missing = sorted({"a", "b"} - set(value))
    return f"missing columns {missing}" if missing else None


@fnloom.check_output_custom(_has_columns)
@fnloom.tag(schema="a:int,b:float")
def frame(rows: int) -> dict:
    return {"a": list(range(rows))}
```

```python
import framed
from fnloom import ValidationError

driver = fnloom.build(framed)
driver.nodes["frame"].tags["schema"]
# -> 'a:int,b:float'
driver.execute(["frame"], inputs={"rows": 2})
# raises ValidationError: frame: check _has_columns failed: missing columns ['b']
```

### `@dataloader` and `@datasaver`

A loader is a function that returns the data. If you also want its metadata as a node, return both and split them
with `@extract_fields`. `@tag` marks the node for `Driver.nodes` and drawings (Q115):

```python
# file: loading.py
import fnloom


@fnloom.extract_fields({"data": list, "metadata": dict})
@fnloom.tag(kind="loader")
def raw(path: str = "p.json") -> dict:
    return {"data": [1, 2], "metadata": {"source": path, "rows": 2}}


def total_loaded(data: list) -> int:
    return sum(data)
```

```python
import loading

fnloom.build(loading).execute(["total_loaded", "metadata"])
# -> {'total_loaded': 3, 'metadata': {'source': 'p.json', 'rows': 2}}
```

A saver is a node that writes and returns what it wrote, or a hook (below).

### `@load_from`, `@save_to` and materializers

Load in a node. Its value is type-checked on the edge to each reader, which Hamilton's loaded values are not. Save
from an `after_run` hook, which gets the run's raw outputs. A saver node requested with the outputs works too, just
as a Hamilton saver node writes only when requested (Q116):

```python
# file: stored.py
import json
from pathlib import Path


def numbers(in_path: str) -> dict:
    return json.loads(Path(in_path).read_text(encoding="utf-8"))


def total_stored(numbers: dict) -> int:
    return sum(numbers.values())
```

```python
import json
import tempfile
from pathlib import Path

import stored


class SaveJson(fnloom.Hook):
    """Writes the named outputs of each successful run to one JSON file."""

    def __init__(self, path: Path, names: list[str]) -> None:
        self.path = path
        self.names = names

    def after_run(self, event: fnloom.RunEnd) -> None:
        if event.ok and event.results is not None:
            saved = {name: event.results[name] for name in self.names}
            self.path.write_text(json.dumps(saved), encoding="utf-8")


folder = Path(tempfile.mkdtemp())
(folder / "in.json").write_text('{"a": 1, "b": 2}', encoding="utf-8")
driver = (
    fnloom.Builder()
    .with_modules(stored)
    .with_hooks(SaveJson(folder / "out.json", ["total_stored"]))
    .build()
)
driver.execute(["total_stored"], inputs={"in_path": str(folder / "in.json")})
# -> {'total_stored': 3}
(folder / "out.json").read_text(encoding="utf-8")
# -> '{"total_stored": 3}'
```

To choose the source at run time, use a `@config.when` variant per source, or a value given as an input.

### `StaticValidator`

To check the graph at build, write a function over `Driver.nodes` and call it right after `build()`. It sees the
names, types, tags, dependencies, functions and config that a `StaticValidator` sees, and can report every problem in
one error (Q117):

```python
def checked(driver: fnloom.Driver) -> fnloom.Driver:
    """The driver, if every computed node has an owner tag."""
    problems = [
        f"{name}: no owner tag"
        for name, node in driver.nodes.items()
        if not node.is_input and "owner" not in node.tags
    ]
    if problems:
        raise ValueError("; ".join(sorted(problems)))
    return driver


checked(fnloom.build(summing))
# raises ValueError: total: no owner tag
```

## Not there yet

fnloom is pre-alpha. The core, the native path, `@parameterize` and its sugar, `@tag`, `@config`, `@extract_fields`,
`@extract_columns`, `@exclude`, graph inspection, `validate`, graphs from plain functions, namespaces, type checks,
result caching, the async driver, hooks, result builders, output checks, custom value checks, drawing, export, the
CLI, `@check_output` validators, step injection, `@inject`, `@resolve` and node wrappers are done. These Hamilton
features come next, roughly in this order:

1. **Dynamic execution**, materializers and `@subdag`.
2. **`GracefulErrorAdapter`** and `StaticValidator`. Node wrappers can change how a node is called, but a run still
   stops at the first failure.

If your pipeline depends on one of these, it's worth waiting for it before you switch.
