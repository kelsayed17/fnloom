# Roadmap: ahead of Hamilton everywhere

**Status:** draft for maintainer review, 2026-10-28. Nothing below is built.

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
| `list_available_variables()` | every node with its type, tags and whether it is an input | Plan 5: `Driver.nodes` already holds all of this. Plan 5 adds `Driver.inputs()` and `Driver.outputs()` (*new*) for the two common questions |
| `validate_inputs(...)`, `validate_execution(...)` | check a request without running it | Plan 5: `prepare(...)` already validates names. Plan 5 adds `validate(...)` (*new*), which also runs the type checks and returns every problem rather than the first |
| `what_is_the_path_between(a, b)` | nodes on any path from `a` to `b` | Plan 5 (design §5 lists "between") |
| `display_all_functions`, `display_upstream_of`, `display_downstream_of`, `visualize_path_between`, `visualize_execution` | graphviz drawings | Plan 7: DOT text with no dependency; `graphviz` optional for rendering |
| `materialize`, `validate_materialization`, `visualize_materialization` | write outputs through data savers | Not planned. A design §2 non-goal: the hooks of Plan 6 let a user write outputs in a post-graph hook |
| `raw_execute`, `export_execution` | execute without a result builder; export a run's graph | Plan 6 (result builders), Plan 7 (export) |
| `config`, `graph_modules`, `adapter` | what the driver was built from | `config` done in Plan 3: `Driver.config`, a read-only copy (Q56). Plan 5 (`modules`), Plan 6 (hooks) |
| `has_cycles` | report a cycle | Not needed: fnloom refuses a cycle at build, naming it (Q27) |
| `AsyncDriver` | `async def` nodes | Done in Plan 3: `Builder.build_async()`, with caching (Q11–Q13, Q65–Q69) |

### Decorators

| Hamilton | fnloom plan |
|---|---|
| `@config.when`, `.when_not`, `.when_in`, `.when_not_in`; `Builder.with_config` | Done in Plan 3, with `@config.default` and `name=` on all four forms (Q56–Q60) |
| `@extract_fields`, `@extract_columns` | Done in Plan 3 (Q61–Q64) |
| `@parameterize` row as `(bindings, docstring)`; `group(...)`; `parameterize_sources`, `parameterize_values` | Plan 5 (design §5 v1.x "parameterize sugar") |
| `@check_output`, `@check_output_custom` | Plan 8 ("output validators") |
| `@pipe_input`, `@pipe_output`, `@mutate`, `@step` | Plan 8 ("step injection") |
| `@inject`, `@resolve` | Plan 8 ("dependency injection by config") |
| `@subdag`, `@does`, `@schema`, `@dataloader`, `@datasaver`, `@load_from`, `@save_to` | Plan 9 decides each. Default: not planned, with a register row naming the fnloom way to get the same result |
| `@cache(format=...)`, per-format result stores | Done in the Plan 4 follow-up: `fnloom.Format`, `"pickle"` and `"json"` built in, more through `DiskCache(formats=...)`; resolved at build (Q55), and `json` stores only what loads back exactly (Q54) |

### Execution

| Hamilton | fnloom plan |
|---|---|
| Lifecycle adapters (pre/post node, pre/post graph) | Plan 6 |
| Result builders (`do_build_result`, dict / pandas / polars builders) | Plan 6 |
| Dynamic execution (`Parallelizable`, `Collect`) | Its own design first (design §2: "the first deferral to design next"), then Plan 10. Free-threaded 3.14t is the target for running nodes in parallel |
| Plugin ecosystem (dask, ray, spark, pandera, mlflow, UI, tracker) | Not planned (design §2 non-goal). Plan 6's hooks make each one writable outside fnloom |
| A failing node logs its inputs | Plan 6: fnloom notes the node's name on the exception today. A failure hook (*new*) gets the node's name and its inputs, and logging them is one hook |
| A function in `final_vars` | Plan 5: lifts Q30 |

### Behaviour where Hamilton's stricter rule has users

| Hamilton | fnloom today | Plan |
|---|---|---|
| An override naming an input that wasn't supplied is refused | Accepted: an override can supply a missing input (Q7) | Plan 5: keep Q7, and add `Builder.with_strict_overrides()` (*new*), an opt-in that refuses it with Hamilton's rule. A user who relies on the refusal shouldn't have to re-implement it |

### Performance

| Item | Plan |
|---|---|
| A cold run with `MemoryCache` costs 5–6× an uncached run, against a target of 1.5× (issue #10) | Done in the Plan 4 follow-up: 2.2× and 1.9×, with the walk and the memory store native, about 0.19 µs per node. The target is restated as at most 0.25 µs per node, since one BLAKE3 block costs half an uncached trivial node |

### Public helpers fnloom keeps private

| Item | Plan |
|---|---|
| `value_matches` and `type_matches` (`fnloom._types`), the input and edge checks | Plan 5 (*new*): export them, so a caller can check a value the way `execute` will. Hamilton's `htypes.check_input_type` is public |

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
| A typo in a parameter name or a `source("...")` string becomes a new required input, not an error | same | Plan 5: `Builder.with_declared_inputs(...)`, an opt-in that refuses any input not declared, naming the closest node |
| A default on a parameter silently makes an input optional, and adding a node of that name anywhere silently replaces the default; two functions with different defaults for one input pass | same | Plan 5: build-time error when a node shadows a default, or when defaults for one input differ (opt-out per parameter) |
| Every public helper in a module becomes a node, and its parameters phantom inputs; the escape hatch `@hamilton_exclude` is barely documented | same | Plan 5: `@exclude`, honour `__all__` when a module defines it, and a build warning for a node nothing consumes |
| One flat namespace per driver: two modules can't define the same node, one input name has one type driver-wide, and Hamilton's opt-in override is last-wins, which mis-wires silently | fixed (clearer refusal) / missing opt-in | Plan 5: explicit namespacing (`Builder.with_modules(..., prefix=)` or a namespace argument) instead of last-wins |
| A graph can only be written as Python functions in importable modules: no graph from a list of functions (#1268), and none from data without generating code | n/a | Plan 5: `Builder.with_functions(*fns)` with a stable synthetic module name; a public node-definition API over the core's generic graph |
| `@parameterize` nodes exist only in the built graph, invisible to static tools | fixed | `NodeView.origin` and `module` (Q34) |
| Inspection returns a mix of node objects and names, includes config entries as inputs, and has no topological order (#1293, #836) | fixed | `Driver.nodes`, names, topological order (Q26, Q28) |
| Execution order depends on the order of outputs and parameters | fixed | Q26 |
| The `Builder` is mutable, so a "base" builder is changed by every chain off it | fixed | fnloom's builder doesn't alias |

### Types and validation

| Problem | Status | Fix |
|---|---|---|
| No `py.typed`; decorators erase signatures | fixed | Q1 |
| Annotations resolve at build, so a name imported only under `TYPE_CHECKING` raises `NameError` from inside `build` (it reads like a library bug, and linters that move imports break builds) | same | Plan 5: read annotations with `annotationlib`'s forward-reference format; an unresolved name becomes a named opaque type that matches only itself and `Any`, and the build reports it if a check needs it |
| Every parameter and the return must be annotated | same, clearer message | Keep; document |
| `Any` on one consumer absorbs a concrete type on another, so a shared input goes unchecked (#1180) | same | Plan 5: unify to the most specific annotation; `Any` never widens a typed input |
| `None` passes every input check | same | Plan 5: `None` only for `Optional`, `object` or `Any` (register row) |
| Fixed-length `tuple[int, str]` rejects a correct value (#1749 on the output side); fnloom accepts any tuple without checking length or items | fixed (no false refusal), partial | Plan 5: check a fixed-length tuple's length and items |
| Container checks are shallow (`dict[str, ndarray]` is "a dict"; lists by their first item) | same, deliberately | Plan 5: keep shallow by default (cost); an opt-in deep check |
| `list[Any]` is not a wildcard on edges; exact matching refuses `str \| None` into `str`, `TypedDict` in a union crashes the build, protocols are refused | fixed | Q39–Q42, Q45 |
| `tuple[T, ...]`, `NewType`, PEP 695 aliases, `Literal`, `Annotated`, `Mapping`/`Callable`, iterators | fixed | Q37–Q45 |
| Numpy scalars refused for `int`/`float`; `int` refused for `float`; 2-D or float32 arrays and mismatched lengths flow into wrong answers | same (register: not yet decided) | Plan 5: an opt-in numeric policy (`Builder.with_numeric_inputs(...)`: widening, `ndim`/`dtype` contracts, length groups), so users stop re-writing it around `execute` |
| Overrides bypass type checks, and can change a node's type | fixed | Q22 |
| An adapter implementing the input check replaces the default instead of adding to it; the async path checks no inputs | n/a (adapter); fixed (async, Plan 3) | Plan 6: hooks add to the default check. The async driver checks inputs like the sync one (Q67) |
| An `Any` producer, or a node returning the wrong type, is never checked at run | same | Plan 6: an opt-in output check, done right on the types where Hamilton's crashes (#1738, #1749) |

### Execution and errors

| Problem | Status | Fix |
|---|---|---|
| About 100 µs per `execute` re-planning; build of 0.3–30 ms | fixed (execute) | Q3; build cost gets a benchmark row |
| A failed run logs a full traceback at ERROR, then a Slack banner, then raises; a malformed function is reported twice | fixed | Q15; one raise |
| A failing node's inputs are logged by Hamilton, not by fnloom | worse | Plan 6: a failure hook receives the node and its inputs |
| A node can't skip (no early return); "don't compute" must be encoded as data | same | Design question for Plan 10: a typed "absent" outcome a node can return, which its consumers either accept as optional or skip on |
| Validation errors (`DataValidationError`) are not `ValueError`, and `importance="warn"` is the default | n/a | Plan 8: validator errors subclass `ValueError`; fail is the default |
| A Driver used from several threads works but is untested (#54); one cache "last run" is shared across threads | same | Plan 5: a documented thread-safety guarantee with a stress test on the default and free-threaded builds |
| A module edit after `reload` is not seen by an existing driver | same | Plan 5: `Driver.rebuild()`; the cache already versions code (Q48) |
| Unsupplied input requested as an output returns `None` when nothing reads it | — | Check, then register row |

### Config, decorators and results

| Problem | Status | Fix |
|---|---|---|
| A missing or misspelled config key turns a computed node into a required input, with no hint; there is no default variant (#1213) | fixed (Plan 3) | A missing key never matches, and a needed node with no variant fails the build, naming each variant's condition; `@config.default` (Q57) |
| Config keys become input nodes and silently shadow nodes of the same name; config, inputs and overrides are three overlapping channels | fixed (Plan 3) | Config selects the graph's shape only and never becomes a node or a value (Q56) |
| The `__suffix` naming rule is invisible, and a Python redefinition silently keeps the last one | fixed (Plan 3) | An explicit `name=` on all four `@config.when*` forms, a redefinition refused at build, and misuse refused when decorating (Q58, Q59) |
| Decorator stacking order is ignored, and collisions name the wrong decorator (#1163) | fixed so far | Done for config and extract in Plan 3: stacked in any order, and two matching variants name both functions (Q58). Plan 8 does the same for its decorators |
| `@extract_columns` is a pandas registry (polars after a plugin, which autoload-off disables), with missing columns found only at run; `@extract_fields` checks no types and drops extra keys | fixed (Plan 3) | A generic split over any value with `__getitem__`; fields type-checked on every run; extra names refused; `fill_with` never mutates (Q61–Q64) |
| `@check_output` is pandas-only; `@check_output_custom` sees only the output and renames the node | n/a | Plan 8: validators over any type, seeing inputs and output, without renaming |
| `@parameterize` + `value()` silently drops a caller's value for that parameter | — | Plan 5: refuse an input or override naming a `value()`-bound parameter |
| `@pipe_output` can't share work between steps; optional parameters leak in as inputs | n/a | Plan 8 |
| Two adapters can't implement the same lifecycle method (cache + graceful errors, result builder + type-check opt-out); a raising hook skips the others; hook signatures are checked only at run | n/a | Plan 6: any number of hooks per event, run in a defined order, signatures checked when attached (Q23, Q24) |
| `execute` returns different types depending on the adapter; the legacy constructor returns a pandas frame that broadcasts scalars | fixed | Q19: always a dict |
| `AsyncDriver`: async build, pandas default, a deprecated asyncio call, no sibling cancellation, sync nodes block the loop, no graceful partial failure, no caching, and a builder with methods it refuses | fixed (Plan 3) | Sync build, dict results in request order, no deprecated call (Q11–Q13); sync nodes off the loop (Q65); cancellation on failure (Q66); the sync driver's checks (Q67); caching (Q68); async rows awaited (Q69) |

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
| Visualisation without graphviz returns `None` and writes nothing; `has_cycles` changes answer with networkx installed | n/a, fixed | Plan 7 raises an `ImportError` naming the extra, and DOT/Mermaid text needs nothing; cycles refused at build (Q27) |
| The CLI can't build or diff a graph that needs config or overrides | n/a | Plan 7: a CLI over `Builder` arguments |
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
5. **Plan 6: hooks and result builders.** Pre/post node and pre/post graph hooks, a failure hook, cache outcomes on
   the node hook (as Plan 4 deferred), an opt-in output check, result builders, and `raw_execute`.
6. **Plan 7: drawing and the CLI.** DOT text for the graph, upstream, downstream, path between and an execution;
   export a run; a CLI.
7. **Plan 8: v1.x decorators.** Output validators, step injection, and dependency injection by config.
8. **Plan 9: the long tail.** A decision per remaining Hamilton decorator, each a register row.
9. **Design, then Plan 10: dynamic execution**, together with the "Beyond Hamilton" designs.

## Done means

- Every row above is built, or has a register row with the maintainer's decision and the fnloom way to get the same
  result.
- No row of "Hamilton's problems" says **same** or **worse**.
- [Coming from Hamilton](../from-hamilton.md)'s "Not there yet" list is empty.
- The benchmarks show no regression against Hamilton on any path the two share.
