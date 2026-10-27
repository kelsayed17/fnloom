# Changelog

All notable changes to fnloom are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project will adhere to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) from its first release. Before 1.0, a minor release may
change the public API; such changes are listed under **Changed** and **Removed** with what to write instead.

## [Unreleased]

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

[Unreleased]: https://github.com/kelsayed17/fnloom/commits/main
