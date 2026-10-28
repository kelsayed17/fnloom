# Changelog

All notable changes to fnloom are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project will adhere to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) from its first release. Before 1.0, a minor release may
change the public API; such changes are listed under **Changed** and **Removed** with what to write instead.

## [Unreleased]

## [0.2.0] - 2026-10-28

### Added

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
