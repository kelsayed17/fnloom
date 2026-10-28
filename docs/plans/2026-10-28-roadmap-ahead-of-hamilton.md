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
| `config`, `graph_modules`, `adapter` | what the driver was built from | Plan 3 (`config`), Plan 5 (`modules`), Plan 6 (hooks) |
| `has_cycles` | report a cycle | Not needed: fnloom refuses a cycle at build, naming it (Q27) |
| `AsyncDriver` | `async def` nodes | Plan 3 |

### Decorators

| Hamilton | fnloom plan |
|---|---|
| `@config.when`, `.when_not`, `.when_in`, `.when_not_in`; `Builder.with_config` | Plan 3 |
| `@extract_fields`, `@extract_columns` | Plan 3 |
| `@parameterize` row as `(bindings, docstring)`; `group(...)`; `parameterize_sources`, `parameterize_values` | Plan 5 (design §5 v1.x "parameterize sugar") |
| `@check_output`, `@check_output_custom` | Plan 8 ("output validators") |
| `@pipe_input`, `@pipe_output`, `@mutate`, `@step` | Plan 8 ("step injection") |
| `@inject`, `@resolve` | Plan 8 ("dependency injection by config") |
| `@subdag`, `@does`, `@schema`, `@dataloader`, `@datasaver`, `@load_from`, `@save_to` | Plan 9 decides each. Default: not planned, with a register row naming the fnloom way to get the same result |
| `@cache(format=...)`, per-format result stores | Plan 4 follow-up. Today `DiskCache` takes one `dumps`/`loads` pair |

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
| A cold run with `MemoryCache` costs 5–6× an uncached run, against a target of 1.5× (issue #10) | Plan 4 follow-up, before Plan 5 |

### Public helpers fnloom keeps private

| Item | Plan |
|---|---|
| `value_matches` and `type_matches` (`fnloom._types`), the input and edge checks | Plan 5 (*new*): export them, so a caller can check a value the way `execute` will. Hamilton's `htypes.check_input_type` is public |

## The plans, in order

Each plan follows Plans 1–4: tasks with tests first; a register row before its `deliberate("Qn")` test; differential
tests against Hamilton for every kept behaviour; benchmarks for anything on the run path.

1. **Plan 4 follow-up: issue #10 and `@cache(format=...)`.** It's small and on the shipped path.
2. **Plan 3: config, `extract_*`, async.** This is the reservation Plan 2 made: `Builder.with_config`,
   `Driver.config`, `@config.when*`, `@extract_fields`, `@extract_columns`, and an async driver.
3. **Plan 5: inspection, validation and parameterize sugar.** `inputs()`, `outputs()`, `modules`, `validate(...)`,
   `what_is_the_path_between`, functions in `final_vars`, `with_strict_overrides()`, public `value_matches` and
   `type_matches`, and the `@parameterize` row forms.
4. **Plan 6: hooks and result builders.** Pre/post node and pre/post graph hooks, a failure hook, cache outcomes on
   the node hook (as Plan 4 deferred), result builders, and `raw_execute`.
5. **Plan 7: drawing.** DOT text for the graph, upstream, downstream, path between and an execution; export a run.
6. **Plan 8: v1.x decorators.** Output validators, step injection, and dependency injection by config.
7. **Plan 9: the long tail.** A decision per remaining Hamilton decorator, each a register row.
8. **Design, then Plan 10: dynamic execution.**

## Done means

- Every row above is built, or has a register row with the maintainer's decision and the fnloom way to get the same
  result.
- [Coming from Hamilton](../from-hamilton.md)'s "Not there yet" list is empty.
- The benchmarks show no regression against Hamilton on any path the two share.
