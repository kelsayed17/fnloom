# Roadmap: ahead of Hamilton everywhere

**Status:** in progress, 2026-10-28. Plans 1–4, the Plan 4 follow-up and Plan 3 are merged and released in 0.2.0.
Plans 5 to 7 are merged (#24 to #26), not yet released. Plan 8 is merged (#27), not yet released. Plan 9 is on its own branch, merge pending; a row below marked decided in
Plan 9 describes that branch. Plan 10 and later are not built.

**Goal:** for every capability Hamilton 1.90.0 offers, fnloom has an equivalent that is at least as good, or a
written decision explaining why it doesn't. "At least as good" means: same intent; behaviour proven differentially
against Hamilton wherever it is kept; every deliberate difference recorded in [the register](../quirk-register.md)
with a test; typed; and no slower on the benchmarks.

**Why now:** 0.1.0 already beats Hamilton on the paths it covers. That covers planning, type checks, error messages,
depth, caching correctness and the typed package. But a user who needs anything past that surface still has to
stay on Hamilton. This roadmap orders the remaining gaps into plans so each one closes a whole group.

**Spec:** `docs/design/2026-10-26-fnloom-design.md` §2 and §5 set v1's scope and the v1.x order. This roadmap
follows them and does not change them. Where it adds an item the design doesn't list, the item is marked *new*,
and the design's change log takes a row when the item is approved.

## Where Hamilton is still ahead (measured 2026-10-28, fnloom 0.1.0 against Hamilton 1.90.0)

### Driver surface

`dir()` of a built driver lists these public members on Hamilton that fnloom doesn't have:

| Hamilton | What it gives | fnloom plan |
|---|---|---|
| `list_available_variables()` | every node with its type, tags and whether it is an input | Done in Plan 5: `Driver.nodes` holds all of this, and `Driver.inputs()` and `Driver.outputs()` (*new*) answer the two common questions (Q70) |
| `validate_inputs(...)`, `validate_execution(...)` | check a request without running it | Done in Plan 5: `Driver.validate(...)` (*new*) runs the name and type checks and returns every problem rather than the first, with a suggestion for each misspelled name (Q71) |
| `what_is_the_path_between(a, b)` | nodes on any path from `a` to `b` | Done in Plan 5: in topological order on every hash seed, naming every unknown endpoint (Q70) |
| `display_all_functions`, `display_upstream_of`, `display_downstream_of`, `visualize_path_between`, `visualize_execution` | graphviz drawings | Done in Plan 7: `draw`, `draw_upstream_of`, `draw_downstream_of`, `draw_path_between` and `draw_execution` on both drivers return a `fnloom.Drawing`, whose DOT and Mermaid text need nothing installed and never depend on the hash seed; `render` runs Graphviz's `dot` for an image, with no Python dependency (Q90–Q94) |
| `materialize`, `validate_materialization`, `visualize_materialization` | write outputs through data savers | Not built in (design §2 non-goal). Plan 6's hooks make it writable outside fnloom: an `after_run` hook gets the raw outputs as `RunEnd.results` and can write them, and a result builder can too (Q84, Q19) |
| `raw_execute`, `export_execution` | execute without a result builder; export a run's graph | `raw_execute` done in Plan 6: `Driver.raw_execute`, `PreparedPlan.run_raw` and the async equivalents return the raw dict, skipping the result builder and firing every hook (Q19). `export_execution` done in Plan 7: `Driver.export(...)` describes the graph or a run as a versioned JSON document, with code versions and a graph version (Q95, Q96) |
| `config`, `graph_modules`, `adapter` | what the driver was built from | `config` done in Plan 3: `Driver.config`, a read-only copy (Q56). `modules` done in Plan 5: each module once (Q70). Done in Plan 6: `Driver.hooks`, the hooks in call order (Q84); the result builder, output checks and value checks are builder settings that pickling, `copy`, `rebuild()` and `build_async` carry |
| `has_cycles` | report a cycle | Not needed: fnloom refuses a cycle at build, naming it (Q27) |
| `AsyncDriver` | `async def` nodes | Done in Plan 3: `Builder.build_async()`, with caching (Q11–Q13, Q65–Q69) |

### Decorators

| Hamilton | fnloom plan |
|---|---|
| `@config.when`, `.when_not`, `.when_in`, `.when_not_in`; `Builder.with_config` | Done in Plan 3, with `@config.default` and `name=` on all four forms (Q56–Q60) |
| `@extract_fields`, `@extract_columns` | Done in Plan 3 (Q61–Q64) |
| `@parameterize` row as `(bindings, docstring)`; `group(...)`; `parameterize_sources`, `parameterize_values` | Done in Plan 5, checked when decorating (Q82) |
| `@check_output`, `@check_output_custom` | Done in Plan 8: checks inside the node, adding no node; fail by default with a `ValueError`; over any type; custom checks that read the node's inputs; stacked in any order; part of the cache key (Q99–Q103) |
| `@pipe_input`, `@pipe_output`, `@mutate`, `@step` | Done in Plan 8: steps as ordinary nodes with Hamilton's names, stable across config; defaulted step parameters never inputs; cross-module `@mutate`; `@pipe_input` with `@parameterize` rows (Q104–Q108) |
| `@inject`, `@resolve` | Done in Plan 8: `@inject` keeps the name; `@resolve(decorate_with)` without power-user mode, on a copy per build (Q109, Q110) |
| `@subdag`, `@parameterized_subdag` | Decided in Plan 9: `Builder.with_modules(..., namespace=, bind=)` feeds each copy of a module its own inputs (Q112); `config=` per copy and `@parameterized_subdag` are not planned, a config key per copy and a loop of `with_modules` calls instead |
| `@does`, `@schema.output`, `@dataloader`, `@datasaver`, `@load_from`, `@save_to` | Decided in Plan 9: not planned, each with a register row (Q113 to Q116) and a runnable recipe in [Coming from Hamilton](../from-hamilton.md#hamilton-features-fnloom-leaves-out) |
| `@cache(format=...)`, per-format result stores | Done in the Plan 4 follow-up: `fnloom.Format`, `"pickle"` and `"json"` built in, more through `DiskCache(formats=...)`; resolved at build (Q55), and `json` stores only what loads back exactly (Q54) |

### Execution

| Hamilton | fnloom plan |
|---|---|
| Lifecycle adapters (pre/post node, pre/post graph) | Done in Plan 6: `fnloom.Hook` with `before_run`, `before_node`, `after_node` and `after_run`, each given one frozen event with real values, on both drivers, any number per driver, checked when attached (Q84, Q86); cache outcomes on node events (Q85) |
| Result builders (`do_build_result`, dict / pandas / polars builders) | Done in Plan 6: `Builder.with_result_builder(build)`, any callable given the raw dict in request order, typed through `Builder[R]` (Q19, Q24). No pandas or polars builder: a callable, with recipes, including a strict `frame` recipe that refuses the outer join `pandas.DataFrame` does (Q89) |
| Dynamic execution (`Parallelizable`, `Collect`) | Its own design first (design §2: "the first deferral to design next"), then Plan 10. Free-threaded 3.14t is the target for running nodes in parallel |
| Plugin ecosystem (dask, ray, spark, pandera, mlflow, UI, tracker) | Not built in (design §2 non-goal). Plan 6's hooks make each one writable outside fnloom (Q84) |
| A failing node logs its inputs | Done in Plan 6: `after_node` gets `ok=False` with the node, its inputs and its error, and `fnloom.LogFailures` (*new*) is the ready-made hook that logs them (Q88) |
| `NodeExecutionMethod` and `GracefulErrorAdapter` (node-execution wrappers) | `NodeExecutionMethod` done in Plan 8: `Builder.with_node_wrappers(*wrappers)`, Q24's ordered chain, any number per driver (Q111). Graceful partial failure ships with "Graphs that carry I/O and failure" under [Beyond Hamilton](#beyond-hamilton) |
| `StaticValidator` | Decided in Plan 9: not planned (Q117). A function over `Driver.nodes` after `build()` raises one `ValueError` listing every problem |
| Async hook methods | Out of scope for Plan 6 (its decision 13): hook methods are plain `def`, called on the loop's thread on the async driver, and an `async def` method is a `TypeError` from `with_hooks` (Q84, Q86) |
| A function in `final_vars` | Done in Plan 5: matched by identity, never by name (Q30) |

### Behaviour where Hamilton's stricter rule has users

| Hamilton | fnloom today | Plan |
|---|---|---|
| An override naming an input that wasn't supplied is refused | Accepted: an override can supply a missing input (Q7) | Done in Plan 5: Q7 kept, and `Builder.with_strict_overrides()` (*new*) is an opt-in that refuses it with Hamilton's rule (Q75) |

### Performance

| Item | Plan |
|---|---|
| A cold run with `MemoryCache` costs 5–6× an uncached run, against a target of 1.5× (issue #10) | Done in the Plan 4 follow-up: 2.2× and 1.9×, with the walk and the memory store native, about 0.19 µs per node. The target is restated as at most 0.25 µs per node, since one BLAKE3 block costs half an uncached trivial node |

### Public helpers fnloom keeps private

| Item | Plan |
|---|---|
| `value_matches` and `type_matches` (`fnloom._types`), the input and edge checks | Done in Plan 5 (*new*): `fnloom.value_matches` and `fnloom.type_matches`, so a caller can check a value the way `execute` will. Hamilton's `htypes.check_input_type` is public |

## Hamilton's problems, and what fnloom does about each

Parity is half the goal; the other half is fixing what is wrong with Hamilton itself, not just copying it. This
ledger collects Hamilton 1.90.0's bugs, quirks and design warts from four sources: its open issue tracker
(`apache/hamilton`, triaged 2026-10-28); problems met while running Hamilton in production code; the register's own
research; and a probe of each item against both libraries on Python 3.14. Rows already in the register give their
Q-number, and rows that need one get it when their plan starts.

Status: **fixed** (in 0.1.0, or in the release named); **same** (fnloom behaves as Hamilton does, and should not); **worse** (fnloom is
behind); **n/a** (fnloom has no such feature yet; the plan builds it without the problem).

### fnloom's own regressions, found while probing (fix first, in 0.1.x)

| Problem | Status | Fix |
|---|---|---|
| `inspect.signature` raises `NameError` on 12 public callables (`build`, `Builder.with_modules`, `Driver.execute`, `PreparedPlan.run`, ...): their annotation names are imported only under `TYPE_CHECKING`. Hamilton's resolve | fixed (0.1.1) | Import annotation names at runtime; a test calls `inspect.signature` on every name in `__all__` and its public methods |
| A `Driver` or `PreparedPlan` cannot be pickled or deep-copied (`cannot pickle 'fnloom._core.Graph'`). Hamilton's Driver pickles, which process pools, Ray and Dask rely on | fixed (0.1.1) | `__reduce__` that rebuilds from the modules' names and the builder's settings |
| `build(fn)` given a function instead of a module returns an empty graph (Hamilton does the same) | fixed (0.1.1) | `TypeError: expected a module` (Q52) |
| A module that contributes no node (a package that only re-exports, a hand-made `ModuleType`) is silently empty, found only at `execute` as "unknown outputs" (Hamilton #1281) | fixed (0.1.1) | Refuse a module with no node at build, naming it, and say why its functions were skipped (Q53) |
| The missing-input error lists the names only; Hamilton also names the nodes that need each one | fixed (0.1.1) | Name each missing input's consumers |

### Wiring and inspection

| Problem | Status | Fix |
|---|---|---|
| Recursive walk: `RecursionError` at about 1,000 nodes; cycles build and fail at execute (#1760) | fixed | Q25, Q27 |
| An input or override name that is not in the graph is ignored; an input naming a computed node is dropped | fixed | Q4, Q5: refused by name |
| Node names come from `fn.__name__`, so an alias or a factory-made function collides, and an undecorated wrapper renames the node | fixed | fnloom uses the attribute name |
| Hamilton scans submodules and counts re-exported functions as nodes; the same module passed twice collides | fixed | fnloom takes a module's own functions, and deduplicates |
| A typo in a parameter name or a `source("...")` string becomes a new required input, not an error | fixed (Plan 5, opt-in) | `Builder.with_declared_inputs(...)` refuses any input not declared, naming the closest node (Q75); every unknown or missing name in an error suggests the closest one (Q71) |
| A default on a parameter silently makes an input optional, and adding a node of that name anywhere silently replaces the default; two functions with different defaults for one input pass | fixed (Plan 5) | A build-time error when a node shadows a default, or when defaults for one input differ, with `@defaults(...)` as the per-parameter opt-out (Q76) |
| Every public helper in a module becomes a node, and its parameters phantom inputs; the escape hatch `@hamilton_exclude` is barely documented | fixed (Plan 5) | `@exclude`, bare or called, and `__all__` honoured when a module defines it (Q73). No warning for a node nothing consumes: a graph's outputs are exactly such nodes, so `Driver.outputs()` lists them instead (plan 5, decision 6) |
| One flat namespace per driver: two modules can't define the same node, one input name has one type driver-wide, and Hamilton's opt-in override is last-wins, which mis-wires silently | fixed (Plan 5) | `Builder.with_modules(..., namespace="ns")` names a module's nodes `ns.<name>`; nothing is last-wins (Q74) |
| A graph can only be written as Python functions in importable modules: no graph from a list of functions (#1268), and none from data without generating code | fixed (Plan 5) | `Builder.with_functions(*fns)` uses the functions themselves, with no synthetic module, and `fnloom.node(fn, name=, bind=)` is the node-definition API (Q72). Plan 7's `Driver.export()` writes a graph as data (`fnloom.export/1`); building a graph back from it is not planned, since its nodes are code |
| `@parameterize` nodes exist only in the built graph, invisible to static tools | fixed | `NodeView.origin` and `module` (Q34) |
| Inspection returns a mix of node objects and names, includes config entries as inputs, and has no topological order (#1293, #836) | fixed | `Driver.nodes`, names, topological order (Q26, Q28) |
| Execution order depends on the order of outputs and parameters | fixed | Q26 |
| The `Builder` is mutable, so a "base" builder is changed by every chain off it | fixed | fnloom's builder doesn't alias |

### Types and validation

| Problem | Status | Fix |
|---|---|---|
| No `py.typed`; decorators erase signatures | fixed | Q1 |
| Annotations resolve at build, so a name imported only under `TYPE_CHECKING` raises `NameError` from inside `build` (it reads like a library bug, and linters that move imports break builds) | fixed (Plan 5) | An unresolved name builds as an opaque `unresolved` type that matches only its own text and `Any`; a value check against it says to import it at run time (Q81) |
| Every parameter and the return must be annotated | same, deliberately, with a clearer message | Kept: every edge is typed where it is declared, and the build's `ValueError` names the function. Documented in [What is refused](../writing-nodes.md#what-is-refused) |
| `Any` on one consumer absorbs a concrete type on another, so a shared input goes unchecked (#1180) | n/a: not a 1.90.0 behaviour (H5.10b); both check the shared input as the concrete type | Pinned by the KEEP test `test_any_and_int_consumers_check_the_input_as_int` in `tests/differential/test_semantics.py` |
| `None` passes every input check | fixed (Plan 5) | `None` passes only a type that admits it: `Optional`, `object`, `Any`, `None` and their kin (Q79) |
| Fixed-length `tuple[int, str]` rejects a correct value (#1749 on the output side); fnloom accepts any tuple without checking length or items | fixed (Plan 5) | A fixed-length tuple's length and items are checked (Q80) |
| Container checks are shallow (`dict[str, ndarray]` is "a dict"; lists by their first item) | same, deliberately | Shallow by default, for its cost; `Builder.with_deep_type_checks()` (Plan 5) is the opt-in that checks every item of a standard collection |
| `list[Any]` is not a wildcard on edges; exact matching refuses `str \| None` into `str`, `TypedDict` in a union crashes the build, protocols are refused | fixed | Q39–Q42, Q45 |
| `tuple[T, ...]`, `NewType`, PEP 695 aliases, `Literal`, `Annotated`, `Mapping`/`Callable`, iterators | fixed | Q37–Q45 |
| Numpy scalars refused for `int`/`float`; `int` refused for `float`; 2-D or float32 arrays and mismatched lengths flow into wrong answers | fixed (opt-in widening); contracts deferred | `Builder.with_numeric_widening()` (Plan 5) lets an `int` stand for `float` and numpy scalars for `int` and `float`. `ndim`, `dtype` and length contracts stay in the register's "Not yet decided": they came from one consumer's workloads (plan 5, decision 12f) |
| Overrides bypass type checks, and can change a node's type | fixed, with an opt-out | Q22; `Builder.without_override_type_checks()` (Plan 5) skips only the override checks |
| An adapter implementing the input check replaces the default instead of adding to it; the async path checks no inputs | fixed (Plan 6); fixed (async, Plan 3) | `Builder.with_value_check(check)` adds to the default check: asked only about a value the default refuses, it can only admit it, and it never touches edges (Q21). The async driver checks inputs like the sync one (Q67) |
| An `Any` producer, or a node returning the wrong type, is never checked at run | fixed (Plan 6, opt-in) | `Builder.with_output_type_checks()` checks every node's result by the input checks' rules, on fixed tuples, generics and `NewType`s where Hamilton's checker crashes (#1738, #1749); a node that raises keeps its own error, and an `Any` or `object` return costs nothing (Q87) |

### Execution and errors

| Problem | Status | Fix |
|---|---|---|
| About 100 µs per `execute` re-planning; build of 0.3–30 ms | fixed (execute) | Q3; build cost gets a benchmark row |
| A failed run logs a full traceback at ERROR, then a Slack banner, then raises; a malformed function is reported twice | fixed | Q15; one raise |
| A failing node's inputs are logged by Hamilton, not by fnloom | fixed (Plan 6, opt-in) | `after_node` gets the failing node, its inputs and its error; `fnloom.LogFailures` logs them, each value's `repr` cut to 50 characters. fnloom still logs nothing by default (Q88) |
| A node can't skip (no early return); "don't compute" must be encoded as data | same | Design question for Plan 10: a typed "absent" outcome a node can return, which its consumers either accept as optional or skip on |
| Validation errors (`DataValidationError`) are not `ValueError`, and `importance="warn"` is the default | fixed (Plan 8) | `fnloom.ValidationError` subclasses `ValueError`, with a readable message; fail is the default (Q100) |
| A Driver used from several threads works but is untested (#54); one cache "last run" is shared across threads | fixed (Plan 5) | A documented guarantee ([Threads](../running.md#threads)) with a stress test of 16 threads sharing one cached driver, on the default and free-threaded builds (`tests/test_threads.py`); no cache state is shared between runs |
| A module edit after `reload` is not seen by an existing driver | fixed (Plan 5) | `Driver.rebuild()` and `AsyncDriver.rebuild()` build again with every setting and see the edit; an existing driver keeps its code, as Hamilton's does, and the cache versions code (Q83, Q48) |
| Unsupplied input requested as an output returns `None` when nothing reads it | fixed (Plan 5) | Refused: `missing inputs: 'x' (requested as an output)` (Q78) |

### Config, decorators and results

| Problem | Status | Fix |
|---|---|---|
| A missing or misspelled config key turns a computed node into a required input, with no hint; there is no default variant (#1213) | fixed (Plan 3) | A missing key never matches, and a needed node with no variant fails the build, naming each variant's condition; `@config.default` (Q57) |
| Config keys become input nodes and silently shadow nodes of the same name; config, inputs and overrides are three overlapping channels | fixed (Plan 3) | Config selects the graph's shape only and never becomes a node or a value (Q56) |
| The `__suffix` naming rule is invisible, and a Python redefinition silently keeps the last one | fixed (Plan 3) | An explicit `name=` on all four `@config.when*` forms, a redefinition refused at build, and misuse refused when decorating (Q58, Q59) |
| Decorator stacking order is ignored, and collisions name the wrong decorator (#1163) | fixed so far | Done for config and extract in Plan 3: stacked in any order, and two matching variants name both functions (Q58). Done for Plan 8's decorators: every pair, and each with `@tag`, `@parameterize`, `@config.when` and `@extract_fields`, with and without a `@mutate` step, gives one graph in either order (`tests/test_stacking.py`) |
| `@extract_columns` is a pandas registry (polars after a plugin, which autoload-off disables), with missing columns found only at run; `@extract_fields` checks no types and drops extra keys | fixed (Plan 3) | A generic split over any value with `__getitem__`, with no registry or plugin; fields type-checked on every run; extra names refused; `fill_with` never mutates. A missing column is still found at run, when the value first exists, and the error names the columns the producer returned (Q61–Q64) |
| `@check_output` is pandas-only; `@check_output_custom` sees only the output and renames the node | fixed (Plan 8) | Checks over any type, custom checks seeing inputs and output, inside the node with no renaming (Q99–Q102) |
| `@parameterize` + `value()` silently drops a caller's value for that parameter | already refused (Q4, Q5); documented (Q77) | An input or override naming a parameter only `value()` binds is refused as unknown (Q77) |
| `@pipe_output` can't share work between steps; optional parameters leak in as inputs | fixed (Plan 8) | A defaulted step parameter keeps its default and is never an input (Q105); steps share work by `source("node")`, as in Hamilton, and each step is a node the cache keeps |
| Two adapters can't implement the same lifecycle method (cache + graceful errors, result builder + type-check opt-out); a raising hook skips the others; hook signatures are checked only at run | fixed (Plan 6) | Any number of hooks per event, in the order given; every hook sees an event before its errors are raised, several as one `ExceptionGroup` with a node's own error first; signatures checked by `with_hooks` (Q23, Q24, Q84). One result builder per builder, as Hamilton (Q24) |
| `execute` returns different types depending on the adapter; the legacy constructor returns a pandas frame that broadcasts scalars | fixed | Q19: always a dict |
| `AsyncDriver`: async build, pandas default, a deprecated asyncio call, no sibling cancellation, sync nodes block the loop, no graceful partial failure, no caching, and a builder with methods it refuses | fixed (Plan 3), except graceful partial failure: n/a, a later design | Sync build, dict results in request order, no deprecated call (Q11–Q13); sync nodes off the loop (Q65); cancellation on failure (Q66); the sync driver's checks (Q67); caching (Q68); async rows awaited (Q69). A failure still raises the first exception (Q66): graceful partial failure is designed with "Graphs that carry I/O and failure" under [Beyond Hamilton](#beyond-hamilton). Done in Plan 6: hooks fire on the async driver as the node starts and finishes, with real values, and a result builder is used (Q86, Q19) |

### Caching

| Problem | Status | Fix |
|---|---|---|
| Type-blind and `"<unhashable>"` keys serve wrong values; numpy hashed without dtype or shape | fixed | Q46, Q47 |
| A hit is slower than recomputing: source re-hashed every run, a database write per node, polars hashed row by row in Python | fixed except issue #10 | Q16; polars and numpy fingerprinted vectorised; #10 is the cold-run target |
| Edits to helpers serve stale values | fixed | Q48 |
| One appended row misses every node (no incremental cache) | same | Beyond Hamilton, below |

### Install, logging, tooling

| Problem | Status | Fix |
|---|---|---|
| numpy and pandas required, 180–290 ms import (seconds with autoload); undeclared `packaging` silently disables pandas support | fixed | Q14, Q20: no dependencies, about 30 ms import |
| Import reads `~/.hamilton.conf` and writes the environment; telemetry stubs warn | fixed | No import-time I/O |
| Messages name the old `sf-hamilton` distribution; deprecated spellings and internals exported | fixed | 23 public names |
| Visualisation without graphviz returns `None` and writes nothing; `has_cycles` changes answer with networkx installed | fixed (Plan 7), fixed | DOT and Mermaid text need nothing, and `render` without Graphviz raises a `FileNotFoundError` saying how to install it; there is no extra to name, since rendering runs the `dot` executable (Q94); cycles refused at build (Q27) |
| The CLI can't build or diff a graph that needs config or overrides | fixed (Plan 7) | `fnloom draw`, `export`, `validate` and `diff` take `--config`, `--input`, `--override`, `--outputs` and a JSON or TOML `--context`; modules import their siblings, and `diff` compares two exports (Q97, Q98) |
| Two core releases in 18 months; fixes sit unreleased | — | A release on every merged plan; the changelog and wheels are automated (#13) |

### Beyond Hamilton

These are problems Hamilton has no answer to at all. They need a design of their own before any plan:

- **Incremental and streaming execution.** One appended bar re-runs every node; re-running per new row is O(n).
- **Panel and sweep execution.** Running one graph over many entities, or over a grid of `value()`s, is a Python
  loop of `execute` calls today.
- **Graphs that carry I/O and failure.** An async node that fails can degrade to a declared empty answer, so a fan-out
  returns partial results rather than one exception.

## The plans, in order

Each plan follows Plans 1–4: tasks with tests first; a register row before its `deliberate("Qn")` test; differential
tests against Hamilton for every kept behaviour; benchmarks for anything on the run path.

1. **0.1.x fixes:** the five regressions in the first table above.
2. **Plan 4 follow-up: issue #10 and `@cache(format=...)`.** It's small and on the shipped path.
3. **Plan 3: config, `extract_*`, async.** Done. This is the reservation Plan 2 made: `Builder.with_config`,
   `Driver.config`, `@config.when*` (with `default` and `name=`), `@extract_fields`, `@extract_columns`, and an async
   driver, with the config and async rows of the ledger above.
4. **Plan 5: inspection, validation and authoring.** `inputs()`, `outputs()`, `modules`, `validate(...)`,
   `what_is_the_path_between`, functions in `final_vars`, `with_functions`, `with_strict_overrides()`,
   `with_declared_inputs(...)`, namespacing, `@exclude`, public `value_matches` and `type_matches`, the
   `@parameterize` row forms, and the wiring and types rows of the ledger.
5. **Plan 6: hooks and result builders.** Done: `fnloom.Hook` with pre/post node and pre/post graph
   events on both drivers (Q84, Q86), `fnloom.LogFailures` as the failure hook (Q88), cache outcomes on the node
   events (Q85), an opt-in output check (Q87), additive value checks (Q21), result builders and `raw_execute` (Q19,
   Q24, Q89).
6. **Plan 7: drawing and the CLI.** Done: DOT and Mermaid text for the graph, upstream, downstream,
   path between and an execution, with no dependency (Q90–Q94); `export` of the graph or a run, with code versions
   (Q95, Q96); the `fnloom` command (Q97, Q98).
7. **Plan 8: v1.x decorators.** Done, merge pending: output validators (Q99–Q103), step injection and `@mutate`
   (Q104–Q108), `@inject` and `@resolve` (Q109, Q110), and node wrappers (Q111).
8. **Plan 9: the long tail.** Done, merge pending: a decision per remaining Hamilton decorator, each a register row
   (Q112 to Q117), with `bind=` on a namespaced `with_modules` as the one build (Q112), and Q110's known limit pinned
   by a test.
9. **Design, then Plan 10: dynamic execution**, together with the "Beyond Hamilton" designs.

## Done means

- Every row above is built, or has a register row with the maintainer's decision and the fnloom way to get the same
  result.
- No row of "Hamilton's problems" says **same** or **worse**.
- [Coming from Hamilton](../from-hamilton.md)'s "Not there yet" list is empty.
- The benchmarks show no regression against Hamilton on any path the two share.
