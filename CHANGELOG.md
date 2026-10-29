# Changelog

All notable changes to fnloom are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project will adhere to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) from its first release. Before 1.0, a minor release may
change the public API; such changes are listed under **Changed** and **Removed** with what to write instead.

## [Unreleased]

### Added

- `Builder.with_modules(*modules, namespace=, bind={...})` feeds a namespace inputs of its own, as `@subdag`'s
  `inputs=` does. Each key is a name the namespace reads that none of its modules defines, bound to `source("x")`, read
  outside the namespace, or `value(v)`. A key no node reads, or one the namespace defines, is a `ValueError` at build.
  Bindings are in `Driver.nodes`, the export, `graph_version` and the cache key (a `value()` holding a set is keyed the
  same on every hash seed), and survive pickling, `copy` and `rebuild()` (Q112). A `value()` holding a set now has a different cache key and export
  version than before, so such cache entries miss once and `diff` against an older export shows them changed.
- Decisions, each with a register row and a runnable recipe in Coming from Hamilton: `@does`, `@schema.output`,
  `@dataloader`, `@datasaver`, `@load_from`, `@save_to`, materializers and `StaticValidator` are not planned (Q113 to
  Q117); `@parameterized_subdag` and `config=` per `@subdag` copy are not planned either (Q112).
- Output checks. `@fnloom.check_output(range=, values_in=, data_type=, allow_none=, contains=, does_not_contain=,
  allow_nans=, max_fraction_nans=, importance="fail", field=None)` and `@fnloom.check_output_custom(*checks)` check a
  node's value inside the node, adding no node. A custom check is a function, which may read the node's `inputs`, or a
  `fnloom.Validator`. A failed `fail` check raises `fnloom.ValidationError`, a `ValueError`, with one line per check;
  `warn` logs a WARNING on the `fnloom` logger. Every result is on `NodeEnd.checks` as a `fnloom.CheckResult`.
  Checks read any type, numpy and pandas through their own methods, never consume a one-shot iterator, stack in any
  order, and are part of the cache key (Q99 to Q103).
- Steps. `fnloom.step(fn, **bindings)` with `.named`, `.when*` and `.on_output`; `@pipe_input` and `@pipe_output` lower
  each step to an ordinary node named `<node>.with_<step>`, and `@pipe_output` runs the function as `<node>.raw`.
  `@mutate(*targets)` and `apply_to(fn, ...)` add a step to another function's `@pipe_output`, across modules too. A
  defaulted step parameter is never a graph input, names never change with the config, and misuse Hamilton ignores is
  refused (Q104 to Q108).
- `@fnloom.inject(**bindings)`, one `@parameterize` row that keeps the function's name, and
  `@fnloom.resolve(decorate_with)`, which applies the decorators `decorate_with` returns for the config, needing no
  power-user mode (Q109, Q110).
- `Builder.with_node_wrappers(*wrappers)`: an ordered chain of `wrapper(node, call, inputs)` around every computed
  node that runs a function of yours, any number per driver; hooks fire outside it, a wrapped node's checks run inside
  it, and a cache hit skips it (Q111).
- Hooks. Subclass `fnloom.Hook`, override any of `before_run`, `before_node`, `after_node` and `after_run`, and give
  instances to `Builder.with_hooks(*hooks)`, which is cumulative. Each method gets one frozen event, `fnloom.RunStart`,
  `NodeStart`, `NodeEnd` or `RunEnd`, with a `run_id` shared by a run's events, the node's `NodeView`, its inputs by
  parameter name as a read-only mapping, and its result or the very exception it raised. Every computed node a run
  evaluates reports, extracted fields included; inputs and overrides don't. Hooks are called in the order given, and
  only the methods a hook overrides. `with_hooks` refuses an object that is no `Hook`, a method that can't take one
  positional event or is an `async def` (`TypeError`), and a hook given twice (`ValueError`). `Driver.hooks` lists
  them, and pickling, `copy`, `rebuild()` and `build_async` carry them (Q84). A `Driver` made from a native graph
  takes `hooks=` only with `nodes=`, its node views, and refuses it otherwise (`TypeError`).
- A hook that raises: every hook sees the event first, `after_run(ok=False)` still fires, and one error is raised as
  itself, several as one `ExceptionGroup` with a failing node's own exception first. Hamilton skips the later hooks
  and keeps a node's error only in `__context__` (Q23).
- `AsyncDriver` calls the same hooks, on the event loop's thread, as each node starts and finishes, with real values;
  concurrent nodes report in the order they start and finish. A raising hook fails the run as a failing node does
  (Q86).
- On a cached driver, every node event carries the node's `fnloom.CacheOutcome` as `cache`. A hit gets
  `before_node` and `after_node` with `inputs=None` and the loaded value, hits first, in plan order; a node only hits
  need reports nothing (Q85).
- `fnloom.LogFailures(logger=None, *, level=logging.ERROR)`, a ready-made hook that logs each node that raised, with
  its inputs, each `repr` cut to 50 characters, and the exception as `exc_info`, on the `fnloom` logger by default.
  A huge input's `repr` is bounded before the cut, and one whose `repr` raises shows as `<unrepresentable TypeName>`,
  so the node's own exception is still what `execute` raises. fnloom still logs nothing unless you add it (Q88).
- `Builder.with_result_builder(build)` makes `execute`, `PreparedPlan.run` and the async equivalents return
  `build(outputs)`, where `outputs` is the raw dict in request order. `Builder`, `Driver`, `AsyncDriver`,
  `PreparedPlan` and `AsyncPreparedPlan` take a type parameter, defaulting to `dict[str, object]`, so a type checker
  sees the built type. A second `with_result_builder` is a `ValueError` naming both (Q19, Q24). There is no pandas or
  polars builder: pass `pandas.DataFrame` or `polars.DataFrame`, or the strict `frame` recipe in the docs, which
  refuses outputs that would outer-join (Q89).
- `Driver.raw_execute(final_vars, *, inputs=None, overrides=None)`, `PreparedPlan.run_raw` and their async
  equivalents return the raw dict, skipping the result builder and firing every hook (Q19).
- `Builder.with_output_type_checks()` checks each node's result against its return annotation by the input checks'
  rules. A mismatch fails the node with `TypeError: x: returned str, expected int`; a node returning `Any` or `object`
  costs nothing. Off by default, as in Hamilton (Q87).
- `Builder.with_value_check(check)`, cumulative, adds to the default value check: `check(declared, value)` is asked
  only about a value the default refuses, and admits it by returning a true value. It applies to inputs, overrides,
  extracted fields, output checks and `validate`, never to edges. `fnloom.ValueCheck` is its type. Hamilton's
  `do_validate_input` replaces the default instead (Q21).
- `Driver.rebuild()` and `AsyncDriver.rebuild()` build a new driver from the same builder, with its modules, functions,
  namespaces, config, cache store and every setting, and `sync_nodes` for the async driver. After `importlib.reload`,
  the new driver runs the edited code and, with a shared store, misses on the edited node; the old driver keeps its
  code, as Hamilton's does. A driver made from a native graph raises `TypeError` (Q83).
- One `Driver` shared between threads is a documented guarantee: `execute`, `prepare`, `validate`, `explain_cache`,
  inspection and `PreparedPlan.run` are safe to call at once, on the default and free-threaded builds, and a stress
  test of 16 threads over one cached driver pins it. A user's own mutable values, and nodes that mutate their inputs,
  are not covered.
- A function in `final_vars` means the node it makes in this driver, matched by identity with `NodeView.origin`, never
  by name, in `execute`, `prepare`, `validate` and `explain_cache`, on both drivers; the result is keyed by name. An
  `@extract_*` function means its producer. A function that makes several nodes, such as a `@parameterize` function,
  is a `TypeError` listing them, unless one is named like the function; a function no node comes from is a
  `ValueError` naming it and why. Hamilton maps every function to its `__name__` (Q30).
- `@parameterize` sugar. A row may be `(bindings, doc)`, the doc used as written; otherwise the function's docstring is
  a template filled per row (`{output_name}`, each bound parameter), and a placeholder the row lacks is a `ValueError`
  when decorating, where Hamilton prints the bare key. `fnloom.group(*deps)` and `group(**deps)` bind a parameter to a
  list or dict built from `source()` and `value()` items. `parameterize_sources(**rows)` and
  `parameterize_values(parameter, assigned_output)` are the shorthands. Misuse of any is refused when decorating, each
  offender listed once (Q82).
- `Builder.with_functions(*items)` builds from functions, plain or decorated, without a module: nothing is copied and
  nothing is registered in `sys.modules`. `fnloom.node(fn, *, name=None, bind=None)` makes one node from a function,
  named and bound with `source()` and `value()` without decorating it, and returns a `fnloom.NodeSpec`. A lambda needs
  `name=`. Builders and drivers of functions pickle them by module and qualified name (Q72).
- `Builder.with_modules(*modules, namespace="ns")` names each node of those modules `ns.<name>`, so one module can be
  used under several namespaces. Inside a namespace, a parameter reads the namespace's own node when one is defined,
  and otherwise the plain name; inputs are never namespaced. `NodeView.namespace` says which namespace a node is in.
  Names still clash as before: there is no `allow_module_overrides` (Q74).
- `Driver.inputs()` and `outputs()`, `Driver.modules` and `Driver.what_is_the_path_between(a, b)`, on `AsyncDriver` too,
  with `fnloom.InputView`. The path includes both ends and is in topological order, ties broken by name, on every hash
  seed; its error names every unknown node (Q70). An unsupplied input requested as an output stays a `ValueError`
  (Q78).
- `Driver.validate(final_vars, *, inputs=None, overrides=None)`, on `AsyncDriver` too (not a coroutine), returns every
  problem `execute` would raise for a request, one sorted line each, and runs nothing: unknown outputs, inputs and
  overrides, missing inputs (naming only the consumers the request needs), input and override types, and outputs no
  config variant gave, and functions in `final_vars` no node comes from. Every unknown name in an error now ends in
  `(did you mean 'x'?)` when a node is close to it; in `validate`, a missing input also names the closest supplied
  name that is no node of the graph (Q71).
- `Builder.with_strict_overrides()` refuses an override of an input that isn't also supplied, as Hamilton does, and
  `Builder.with_declared_inputs(*names)` fails the build for any input of the graph not declared and any declared name
  that is no input, each naming the closest node (Q75). Both are off by default. `Builder.without_override_type_checks()`
  skips only the override type checks, keeping input checks on, for overriding a node with a value of another type
  such as `nan` (Q22). All three survive pickling, copying and `build_async`.
- `Builder.with_deep_type_checks()` checks every item of a `list`, `set`, `tuple[T, ...]` or other standard
  collection, every key and value of a mapping, and nested collections' items, iteratively; an iterator, or a value of
  any class but a standard collection's, is never read in full. `Builder.with_numeric_widening()` lets an `int` (not a
  `bool`) or a numpy integer or floating scalar fit `float`, and a numpy integer scalar fit `int`, without importing
  numpy. Both apply to inputs, overrides, extracted fields and
  `validate`, not to edges, and survive pickling, copying and `build_async`. `fnloom.value_matches` takes `deep=` and
  `widen=` to answer the same way.
- `fnloom.value_matches(declared, value)` and `fnloom.type_matches(produced, expected)`, the checks `execute` runs on
  each input, override and extracted field and `build` runs on each edge, are public. Their docstrings state the
  rules, as Hamilton's `htypes.check_input_type` is public too.
- An input or override naming a parameter that only `value()` bindings supply is refused as an unknown name, as any
  input nothing reads is (Q4, Q5); Hamilton drops it, even a wrong-typed one. When another node reads the name, the
  caller's value feeds that node alone and the `value()` rows keep their constant (Q77). Documented and tested; no
  behaviour changed.
- `@fnloom.exclude`, bare or called, keeps a public function out of the graph and returns it unchanged. Hamilton's
  `@hamilton_exclude` works bare only. An excluded function's annotations are never read, so it needs none (Q73).
- CodSpeed benchmarks of the Python run path in `benchmarks/` (`uv run pytest benchmarks`): prepared runs on every
  generated shape, repeated `Driver.execute`, cached runs, the async driver, extraction, config and `Builder.build`.
  CI runs them once on every pull request, and measures them under CodSpeed when `CODSPEED_ENABLED` is set.
- Drawing. `draw()`, `draw_upstream_of(*names)`, `draw_downstream_of(*names)`, `draw_path_between(upstream,
  downstream, *, context=True)` and `draw_execution(final_vars, *, inputs=None, overrides=None, validate=True)` on both
  drivers return a `fnloom.Drawing`: its `nodes`, each a `fnloom.DrawnNode` with its title, whole type, `@config.when`
  conditions and marks (`output`, `override`, `path`, `context`), in topological order, and its `edges`. Each input is
  one node; config is never drawn. `to_dot()` and `to_mermaid()` write the text with nothing installed, the same under
  every `PYTHONHASHSEED`. `render(path)` writes `.dot`, `.gv` and `.mmd` as text and any other format through
  Graphviz's `dot` executable, with a `FileNotFoundError` saying how to install it when it is missing. In Jupyter a
  drawing shows as an SVG, or as its DOT text. Hamilton's drawings need the graphviz package and, without it, return
  `None` and log a traceback (Q90 to Q94).
- `Driver.export(final_vars=None, *, inputs=None, overrides=None, validate=True)` and `AsyncDriver.export` describe the
  graph, or the run `execute` would make, as a JSON-ready `fnloom.export/1` document: each node's type, bindings,
  tags, conditions, source and code version, inputs as inputs, and a `graph_version` that changes with any name,
  wiring, type or code. No value is exported (Q95, Q96).
- The `fnloom` command, also `python -m fnloom`: `draw`, `export`, `validate`, and `diff OLD.json NEW.json` of two
  exports, with `--config`, `--input`, `--override`, `--outputs` and a JSON or TOML `--context`. Modules are dotted
  names or `.py` paths, which may import their siblings; `MODULE@NS` namespaces one. stdout carries only the result,
  an error goes to stderr with status 1, and `diff` exits 0, 1 or 2. It needs no dependency (Q97, Q98).

### Changed

- **Breaking:** a module that defines `__all__` now makes nodes of only the functions it names. Hamilton ignores
  `__all__`, and so did fnloom, so a function the module left out stops being a node, and anything reading its name
  now sees an input. A function listed by its attribute name, `price__eu` for a `@config` variant, is the one
  considered. To keep a function as a node, add its name to `__all__`, or remove `__all__`. An `__all__` that is not a
  list or tuple of `str` is a `TypeError` at build. A missing input named like a function `__all__` or `@exclude` left
  out now says so, and so does the error for a module with no nodes (Q73).
- **Breaking:** the build refuses a parameter with a default that reads a computed node, and an input that two
  consumers give different defaults. Hamilton lets the node replace the default, and each consumer keep its own. One
  `ValueError` lists every case, sorted, naming the parameter, the node or the other consumers, and the opt-out. To
  keep the behaviour, write `@fnloom.defaults(shadowed=("cost",))` on the consumer, and
  `@fnloom.defaults(differing=("shared",))` on each consumer of the input but one; or rename the node, or make the
  defaults equal (Q76). `@fnloom.defaults` is new, and records names only.
- **Breaking:** `None` passes an input, override or extracted-field check only when the type admits it: `Optional[T]`,
  `T | None`, `object`, `Any`, `None`, `Literal[None]`, or a `TypeVar` that is free or whose bound or a constraint
  admits it. Hamilton passes `None` for every type, and so did fnloom, so `None` for `x: int` is now a `ValueError`
  before anything runs: `x: expected int, got NoneType`. Annotate the parameter `x: int | None` (a default of `None`
  does not make it optional), or stop passing `None` (Q79).
- **Breaking:** a fixed-length tuple type, `tuple[int, str]` or `tuple[()]`, now checks a value's length and each item
  with the same rules, nested tuples included. Before, any tuple fitted; Hamilton refuses every one. `tuple[int, ...]`
  and bare `tuple` are unchanged (Q80).
- **Breaking:** a `@parameterize` function's docstring is now a template, filled per row with `str.format_map`. In 0.2.0
  each row used it as written, so a docstring with braces, such as `"""Returns {"count": n}."""`, now raises a
  `ValueError` naming the row when the module is imported. Escape literal braces as `{{` and `}}`, or give the row its
  doc as `(bindings, doc)`, which is used as written. Hamilton fails on the same docstring when decorating (Q82).
- An annotation naming something that does not exist at run time, such as a name imported only under
  `TYPE_CHECKING`, no longer fails the build with `NameError`. It becomes an opaque type, `unresolved 'Decimal'`, with
  its text as written; an alias over such a name and a `TypedDict` field type do too. An edge matches it only from or
  to the same text or `Any`, and fails the build, naming the text, otherwise. A value checked against it raises a
  `TypeError` saying to import it at run time. Code that caught the `NameError` at build should instead import the
  names its annotations use, or catch the `TypeError` where a value is checked (Q81).

## [0.2.0] - 2026-10-28

### Added

- `@cache(format=...)` picks how `DiskCache` writes a node's result: a `fnloom.Format` (a name with a `dumps` and
  `loads` pair) or the name of one the store knows. `"pickle"` and `"json"` are built in, and
  `DiskCache(formats=...)` adds more. A name the store does not know is refused when the driver is built (Q55). The
  `json` format refuses any value that would not load back as the same types, such as a tuple, so a hit never
  returns a different value (Q54).
- `len(MemoryCache)`, the number of entries it holds.
- `Builder.with_config(...)` and `Driver.config`, a read-only, shallow copy. `@config.when`, `when_not`, `when_in` and
  `when_not_in` pick a function's variant at build, and `@config.default` marks the variant used when no other matches.
  `name=` names the node on all four forms. Config shapes the graph only: a key is never a node, an input or a value
  (Q56). A key the config doesn't have never matches, and a node something needs that no variant gives fails the build,
  naming what each variant needs; so does a row or extracted field only an unselected variant gives (Q57). Two matching
  variants fail the build, naming both, and so does a config-decorated function defined twice in its module, naming the
  lines of each (Q58). Misuse, such as `@config.when` without parentheses or `when_in(mode="ab")`, is refused when
  decorating (Q59). The user's function is never changed, and `NodeView.config` holds the conditions that picked a node,
  each a `fnloom.Condition` (Q60).
- `@extract_fields` splits a node's mapping into one node per field. Each field's value is checked against its
  declared type on every run, even with input checks off, and declared types are checked at build against a
  `TypedDict` or `dict[str, T]` return (Q61). A `Mapping[str, T]` return works. `fill_with` never changes the
  producer's value, and `None` is a fill (Q62). With a cache, a field or column is never stored or served from the
  store: it reruns from its cached producer whenever it's needed, its check with it, and its key covers its name,
  declared type and fill.
- `@extract_columns` splits any value with `__getitem__`, such as a pandas or polars frame or a mapping, into one node
  per column, with no registry or extension to load (Q64). A missing column raises `ValueError` when run, naming the
  columns the producer did return. `@tag` on an `@extract_*` function tags the producer and every field or column
  (Q63).
- `Builder.build_async()` and `AsyncDriver`, for graphs with `async def` nodes. The build is synchronous (Q12), and
  `execute` and `AsyncPreparedPlan.run` are coroutines that return the sync driver's dict, in request order (Q11). Sync
  nodes run in a worker thread, or on the loop with `build_async(sync_nodes="inline")` (Q65). A failing node cancels its
  running siblings, each once, waits for them and raises its own exception, noted with its name (Q66). Requests are
  checked as the sync driver checks them (Q67), and an `async def` `@parameterize` row is awaited (Q69).
- Caching on the async driver: `with_cache(...).build_async()` uses the sync driver's keys, stores, policies and
  `explain_cache`. A node is stored as it succeeds; one that fails, or that a failure cancels, stores nothing (Q68).

### Changed

- A `TypedDict`-typed input or override accepts any `dict`, its keys unchecked, as the shallow container checks do.
  Before, every value failed the check, so such an input could never be supplied (Q61).
- `build()` still refuses a graph with an `async def` node, and its `ValueError` now names `build_async()` (Q32).
- A cold cached run is 2.7 to 2.9 times faster with `MemoryCache`, and a warm one 2.1 to 3.3 times faster: a plain
  `MemoryCache` keeps its entries in the native extension, which walks it and stores each result with no call into
  Python per node (issue #10). A subclass of `MemoryCache` is used through its methods, like any other store.
  The caching design's cold-run target is restated as at most 0.25 µs of overhead per node, which it meets.
- Cache keys are BLAKE3 keyed by each node's static part (schema `fnloom-cache-2`), so a node reading one value
  hashes one block. Entries written by 0.1.0 are misses, and are replaced as nodes run.

## [0.1.1] - 2026-10-28

### Added

- A `Driver` pickles, and copies with `copy.deepcopy`, so it can go to a process pool. It is pickled as the
  `Builder` that built it: its modules by importable name, whether it checks input types, and its store. Loading it
  builds it again. A `PreparedPlan` pickles as its driver and its request, and loading it prepares the request again.
  A `Builder` pickles too. A `DiskCache` reopens its directory when loaded, and a `MemoryCache` arrives empty with the
  same bounds. Pickling raises `TypeError` for a module that isn't importable by its name, and for a driver made from
  a native graph. See [Pickling and process pools](https://kelsayed17.github.io/fnloom/running/#pickling-and-process-pools).

### Changed

- `build`, `Builder.with_modules` and `Builder` raise `TypeError: expected a module, got function 'f'` for anything
  that isn't a module, instead of building nothing from it (Q52).
- A module that contributes no node fails the build with a `ValueError`. It names the module and says why each of its
  functions was skipped: defined in another module (a re-export) or private. Before, such a module was silently empty
  and its nodes showed up as unknown outputs at `execute` (Q53).
- The missing-input error names, for each input, the nodes that need it, and says when you asked for it as an output:
  `missing inputs: 'quantity' (needed by 'total')`. In the Rust core, `PlanError::MissingInputs` holds a
  `MissingInput` per input, with its consumers.

### Fixed

- `inspect.signature` works on every public callable. Before, 12 of them (`build`, `Builder`, `Driver`, `PreparedPlan`,
  `NodeView`, `DiskCache` and methods such as `Driver.execute`) raised `NameError`, because their annotations named
  types imported only for type checkers.

## [0.1.0] - 2026-10-27

### Added

- The Rust core, `fnloom-core`: a graph built from node specs, with cycle and duplicate detection; upstream,
  downstream and between queries; plans compiled once from outputs, input names and override names; and an executor
  with `Evaluate` and `Observer` seams. No walk recurses over graph depth, and execution order is topological with ties
  broken by node name. `Plan::run_by_id` runs a plan reading its values by node id and returns the outputs by id.
- The Python front end: `fnloom.build(*modules)` and `Builder().with_modules(...).build()` return a `Driver` whose
  nodes are a module's public functions, wired by parameter name. `Driver.execute`, `Driver.prepare` and
  `PreparedPlan.run`; `node_names`, `what_is_upstream_of` and `what_is_downstream_of`.
- The native binding `fnloom._core`, with a typed stub. A node's own exception propagates as itself, with a note
  naming the node; a Rust panic becomes a Python exception.
- Differential tests against Apache Hamilton 1.90.0, and the register of deliberate differences (Q1–Q51).
- Decorators `fnloom.parameterize`, `source`, `value` and `tag`, with the `Source` and `Value` types they build. A
  decorator returns the function it was given, typed as itself. Tag values are strings only.
- `Driver.nodes`, a mapping from node name to a `NodeView` (the node's type, parameters, tags and origin), and the
  `Param` type its parameters are described with.
- Type checks. Each edge is checked at build, always, and a mismatch raises one `ValueError` listing every one. Each
  input and override is checked on every run, and a failing run raises one `ValueError` listing every failure.
  `Builder.without_input_type_checks()` turns the per-run checks off.
- Wall-clock benchmarks against Hamilton, with a `rows` shape (one function with `n` `@parameterize` rows) and fnloom
  measured with input checks on and off, and a CI gate on the core's instruction counts.
- A release workflow: a `vX.Y.Z` tag builds CPython 3.14 and 3.14t wheels for manylinux, musllinux, macOS and
  Windows, and the sdist, smoke-tests each, and publishes them to PyPI through trusted publishing. CI's checks now run
  on Windows too.
- A documentation site, built with Zensical and published to GitHub Pages.
- Result caching. `Builder.with_cache(store)` caches every node of a driver, keyed by the node's code version (read
  once, at build) and the fingerprints of its inputs and overrides, chained from node to node; a run loads only the
  stored results nearest its outputs. Stores `fnloom.MemoryCache` (least recently used eviction) and
  `fnloom.DiskCache` (one file per key, atomic writes, version headers, pluggable serializer), the `CacheStore`
  protocol, `MISSING` and `CacheWarning`: the cache never fails a run. The `@cache` decorator with
  `behavior="recompute"` or `"disable"`, `ignore=` per consuming parameter and `version=`, recorded as a
  `CachePolicy`. `fnloom.fingerprint`, `register_fingerprint` and the `__fnloom_fingerprint__` protocol, with
  `UnfingerprintableError`. `Driver.explain_cache`, which reports each node's `CacheOutcome` and runs nothing. A guide,
  [Caching](https://kelsayed17.github.io/fnloom/caching/), and cached-run benchmarks (`benches/cache.py`).
- Markdown lint with rumdl, run by `scripts/check.sh`, so every page renders as written on the docs site.

### Changed

- Building a graph does less Python work per parameter and per node, so every benchmark shape builds faster than in
  Hamilton (`fan_in` with 10,000 parameters: 17.5 against 32.6 ms). An input's `NodeView` is made when `Driver.nodes`
  first reads it.
- Each run costs less: input checks against a plain class are one `isinstance` call, a run makes no name strings, a
  `@parameterize` row is called without a Python wrapper when each source feeds one parameter, and `Driver.execute`
  skips the plan cache's key and lock when a request repeats the last one. A 10-node chain runs in about 1.9 µs, a
  10-input fan-in in 1.8 µs (13.1 before).
- Annotations are resolved at build with `typing.get_type_hints(include_extras=True)`, so an annotation that cannot be
  resolved is now a `NameError` (or, for a missing attribute such as `decimal.Missing`, an `AttributeError`) at build,
  with a note naming every failing node and parameter. Undecorated functions whose string annotations resolve behave
  as before; one that names a class imported only under `TYPE_CHECKING` now fails the build.
- `fnloom-core`: `NodeKind::Function` carries the node's payload, replacing the `Node::payload` field with the
  `Node::payload()` accessor, and `Evaluate::evaluate` receives the payload as an argument, so an evaluator never
  meets a function node without one.
- The README and docs are rewritten to be easier to read, with a graph diagram, a comparison with Apache Hamilton and
  pages that read well on a phone. Apache Hamilton is named as an incubating project.
- The package version comes from the Cargo workspace (`dynamic = ["version"]`), so the wheel and
  `fnloom._core.core_version()` always agree, and `fnloom.__version__` reports it.
- `scripts/check.sh` runs every Cargo step with `--locked`, and CI pins the instruction-count image by digest.

### Fixed

- `DiskCache` no longer warns on Windows that every cache directory is writable by other users. It judged access by
  POSIX mode bits, which Windows reports as `0o777` for every directory; it now checks them on POSIX only.
- The cache returned a stale result after an edit it should have seen. A module constant read through another name
  (a default, a helper handed the module, a class attribute), a function's own attribute, a name read as a string
  (`globals()["RATE"]`, `getattr(config, "RATE")`), a name a module's `__getattr__` serves, and a method of an input
  value's own class now all count in a code version or a fingerprint. Fingerprinting an input of a user class costs
  about 1 µs more per value.
- A pandas index's frequency and a frame's or series' `attrs` are part of their fingerprints.
- A `set` or `frozenset` is fingerprinted in iteration order, and a numpy array by its strides as well as its
  bytes, so a node that reads a set's order or an array's layout never gets another order's or layout's result. A
  set of strings now fingerprints differently in each process unless `PYTHONHASHSEED` is fixed, so with `DiskCache`
  it misses across processes; pass a sorted tuple to share results.
- Packages installed outside the interpreter's own `site-packages` (an overlay or user environment, a `PYTHONPATH`
  entry) are libraries, versioned by name, not user code whose globals could make a node uncacheable.
- `DiskCache` makes its directories private to their owner, warns when it opens a directory others can write, warns
  about what `clear()` cannot remove, never removes a good entry another process wrote since it read a bad one, and
  refuses a path with a NUL byte. `with_cache` refuses an object that is not a store.
- A refusal of a cyclic or deeply nested value shortens its path, and `inspect.signature(fnloom.register_fingerprint)`
  works.
- An edge check now strips `Annotated` and PEP 695 aliases before it looks for `Any` or a `TypeVar`, so a node
  returning `Annotated[Any, ...]` or an alias of `Any` feeds any parameter, as `Any` does (Q41, Q44).
- An input check against a recursive alias such as `type Tree = list[Tree]` refuses a string instead of raising
  `RecursionError`.
- The type stub `fnloom/_core.pyi` matches the extension: `Graph`, `KeyPlan` and `ObserverBase` construct through `__new__`,
  and `Plan.run`'s `observers` defaults to `()` in its runtime signature too.

[Unreleased]: https://github.com/kelsayed17/fnloom/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/kelsayed17/fnloom/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/kelsayed17/fnloom/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/kelsayed17/fnloom/releases/tag/v0.1.0
