# fnloom Plan 6: hooks and result builders

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Status:** done, 2026-10-28, on `claude/plan-6`, merge pending. [As built](#as-built) records where the build went
beyond the plan or narrowed it.

**Goal:** close the roadmap's step 5. That means:

- **Hooks:** before and after a run and each node, on both drivers, with values, any number per driver.
- **Failures:** a failure hook gets the node and its inputs.
- **Cache outcomes:** the node hooks carry each node's outcome, as Plan 4 deferred.
- **Output check:** an opt-in check of each node's result against its annotation.
- **Custom checks:** custom value checks that add to the default check (Q21).
- **Results:** result builders and `raw_execute`.
- **Ledger:** the execution rows of the roadmap's ledger that name Plan 6. None may still say **same** or **worse**
  when this plan is done.

**Architecture:** the core already fires `pre_node` and `post_node` around every computed node and runs every
observer before raising (Q23). Task 1 makes those events carry the node's arguments and its result or error. The
Python front end owns run-level events: it fires `before_run`, runs the value checks and the core run, then fires
`after_run`. A cache hit becomes an override in the core plan, so the core never sees it; the front end reports hits
itself, between `before_run` and the core run. The async driver's Python scheduler fires the same events from its own
callbacks, through the same dispatcher. A driver with no hooks takes today's path unchanged. A result builder is
applied after the run, and `raw_execute` skips it.

**Tech Stack:** as Plans 1–5. Rust (edition 2024, the crate's declared MSRV) for Task 1, Python 3.14 for the rest,
ty strict, ruff ALL, Clippy pedantic, pytest, and `apache-hamilton==1.90.0` as the oracle.

**Spec:**

- `docs/design/2026-10-26-fnloom-design.md`, especially these sections:
    - §2: native API, no compatibility layer.
    - §5: observer hooks and a result builder.
    - §6: Q19, Q21, Q23 and Q24.
    - §10: "Hooks", and the rule that pandas types are out of core.
- `docs/design/2026-10-27-caching-design.md:209`: observers get a `post_node` call that carries the cache outcome.
- `docs/plans/2026-10-28-roadmap-ahead-of-hamilton.md`: the Plan 6 gap rows, and the execution and config rows of the
  ledger.
- The oracle's behaviour, probed on 2026-10-28, is catalogued in the session notes as
  `hamilton-plan6-hooks-result-builders.md` and cited below as "H6.5" and so on. Tests re-probe whatever they rely on.

## fnloom today, probed against the same points

| Point | Hamilton 1.90.0 | fnloom 0.2.0 + Plan 5 |
|---|---|---|
| Hooks a user can attach | lifecycle adapters, kwargs per event (H6.1) | none: `_core.ObserverBase` is private, and the drivers pass no observers |
| What a node event carries | name, tags, kwargs, return type, result, error (H6.1) | the name, and `ok` after the node |
| A raising hook | skips later hooks of its stage, and masks the node's error (H6.5) | every observer runs; a node error leads the group (core, Q23) |
| Hook on a cache hit | fires as if the node ran, with no outcome (H6.10) | nothing fires: a hit is an override |
| Async driver hooks | sync hooks get `Task`s and `success=True` for a failing node; hook-less adapters dropped (H6.9) | none |
| A failing node | logs its inputs at ERROR; the exception carries no node name (H6.11) | a note `in node 'x'` on the exception; nothing logged |
| Result type | builder's choice; dict with `Builder` (H6.12) | always a dict (Q19) |
| Node result against its annotation | unchecked; the opt-in checker crashes on fixed tuples and generics (H6.14, H6.17) | unchecked |
| Custom input check | replaces the default (H6.15) | none |

## Decisions for review

Each decision becomes a register row, Q84 on, or an update to a row, in the task that builds it. Change any before
the build starts.

1. **The hook API (Q84).**
    - `class fnloom.Hook` has four methods, each a no-op until a subclass overrides it:
        - `before_run(self, event: RunStart) -> None`
        - `before_node(self, event: NodeStart) -> None`
        - `after_node(self, event: NodeEnd) -> None`
        - `after_run(self, event: RunEnd) -> None`
    - Each method gets one typed, frozen event, not Hamilton's keyword set. A new field never breaks a hook.
    - `Builder.with_hooks(*hooks: Hook) -> Builder` is cumulative. Hooks run in the order given, for every event,
      after-events included. That keeps Hamilton's order (H6.4).
    - **Checked when attached:**
        - A non-`Hook` object is a `TypeError`. Hamilton accepts and ignores it (H6.4).
        - The same instance given twice is a `ValueError`. Hamilton silently calls it once.
        - An overridden method that cannot take one positional event is a `TypeError` naming the method. Hamilton
          fails at the first call (H6.6).
        - An `async def` method is a `TypeError` naming the method, on both drivers. Hamilton never awaits it on the sync
          driver (H6.6).
    - **Cost:** only the methods a hook overrides are called. A run whose hooks override no node method builds no
      node events.
2. **Events.** All of them are frozen, slotted dataclasses exported from `fnloom`.
    - `RunStart`:
        - `run_id: str`: a new `uuid4` hex for each run, as Hamilton's.
        - `outputs: tuple[str, ...]`: in request order.
        - `inputs` and `overrides`: `Mapping[str, object]`, read-only views.
        - `nodes: tuple[str, ...]`: the nodes this run will report, in event order. Hamilton's `execution_path` order
          changes with `PYTHONHASHSEED` (H6.1).
    - `NodeStart`:
        - `run_id: str`
        - `node: NodeView`, which gives the name, tags, type and origin.
        - `inputs: Mapping[str, object] | None`: the keyword arguments the node's callable gets from other nodes and
          inputs, by parameter name. A parameter left to its default is absent, as in Hamilton (H6.1). It is `None`
          on a cache hit.
        - `cache: CacheOutcome | None`: `None` on a driver with no cache.
    - `NodeEnd`: `NodeStart`'s fields, plus:
        - `ok: bool`
        - `result: object`: `None` unless `ok`.
        - `error: BaseException | None`: the very object the node raised, as in Hamilton (H6.3).
    - `RunEnd`: `RunStart`'s fields, plus:
        - `ok: bool`
        - `error: BaseException | None`
        - `results: Mapping[str, object] | None`: the raw outputs in request order, before any result builder, and
          `None` when a node failed. Hamilton's after-graph gets no request, and gets the built result (H6.1, H6.12).
3. **Which nodes get events (KEEP).** Every computed node a run evaluates gets events, extracted fields included. No
   events are fired for:
    - inputs, including an input requested as an output;
    - overridden nodes;
    - nodes the request does not need (H6.2).
   On a node failure, no later node starts. `after_node` gets `ok=False` and the node's own error, and `after_run`
   gets `ok=False` (H6.3).
4. **When `before_run` fires (KEEP, H6.3).**
    - A request-shape error (an unknown output, input or override name; a bare `str`) is raised by `prepare`, before
      any hook. That matches Hamilton with a graph hook attached.
    - Value checks run after `before_run`: missing inputs, input and override types. So `after_run` sees their
      failure, as Hamilton's does.
5. **A hook that raises (Q23 updated).**
    - Every hook sees an event before its errors are raised. Hamilton skips the later hooks of the stage (H6.5).
    - An error in `before_run` stops the run, and `after_run(ok=False)` still fires. Hamilton skips after-graph.
    - An error in `before_node` stops the run before the node is called. That node gets no `after_node`, as in
      Hamilton.
    - An error in `after_node(ok=True)` stops the run, and the node's value is discarded.
    - An error in `after_run(ok=True)` replaces the result with the error, as in Hamilton.
    - **Several errors:** one error is raised as itself. Several raise one `ExceptionGroup`, its items in this order:
      the node's own exception first, if a node failed; then the hook errors in event order, and within an event in
      hook order. A node error is never masked. Hamilton keeps it only in `__context__` (H6.5).
    - The row's current wording, "the first error is raised", is corrected to this.
6. **Hooks on the async driver (Q86).**
    - The events and rules are the same as on the sync driver, through the same dispatcher.
    - `before_node` fires when the node actually starts, `after_node` when its value or error is in. Both carry
      real values.
    - Hooks always run on the event loop's thread, never in the executor, so a hook needs no locking for one run.
    - Concurrent nodes report in the order they start and finish.
    - A raising hook fails the run like a failing node: the rest are cancelled (Q66).
    - Hamilton calls sync hooks at scheduling time with `Task` objects, reports `success=True` for a failing node,
      and drops a result builder (H6.9).
7. **Cache outcomes on node events (Q85).**
    - On a cached driver, every node event carries the node's `CacheOutcome`: `hit`, `miss` or `uncacheable`, with
      the reason `explain_cache` would give.
    - A hit gets `before_node` and `after_node` like a run node, so hooks that pair the two still pair. They come with
      `inputs=None`, because nothing upstream was loaded, and `result` is the loaded value.
    - Hits are reported first, in plan order, since they are loaded before any node runs. Nodes needed only by hits
      don't run, so they get no events.
    - Hamilton fires hooks on a hit exactly as if the node ran, with no outcome, after loading each upstream value
      too (H6.10).
8. **Failure logging (Q88).**
    - fnloom keeps not logging by default. A library that logs every failure at ERROR, with a support banner, makes
      noise its caller can't turn off (H6.11).
    - `fnloom.LogFailures(logger: logging.Logger | None = None, *, level: int = logging.ERROR)` is a ready-made `Hook`.
      Its `after_node(ok=False)` logs the node's name and its inputs, each value's `repr` cut to 50 characters. The
      record carries `exc_info`. The default logger is `logging.getLogger("fnloom")`.
    - The exception keeps its `in node 'x'` note.
    - The roadmap's "worse" row becomes "fixed (opt-in)".
9. **Output type checks (Q87).**
    - `Builder.with_output_type_checks() -> Builder` checks each node's returned value against its return
      annotation, with the driver's rules: deep checks and widening if opted in, and Q79's `None` rule.
    - Every result is checked, extracted fields' producers included. Extracted fields already are checked.
    - A mismatch is a `TypeError`, `x: returned str, expected int`, raised as the node's failure. So `after_node`
      sees `ok=False` with that error.
    - An unresolved annotation is Q81's `TypeError`.
    - `Any` and `object` returns cost nothing: the check is compiled away at build.
    - Hamilton's opt-in checker crashes on fixed tuples, parameterized generics and `NewType`. It refuses `int` for
      `float`, and it turns a node's failure into a `NoneType` mismatch (H6.17). fnloom's rules have none of these
      problems.
    - Hamilton checks nothing by default (H6.14), and neither does fnloom (KEEP).
10. **Custom value checks (Q21).**
    - `Builder.with_value_check(check: Callable[[object, object], bool]) -> Builder` is cumulative.
    - A value passes if the default check passes, or else if any custom check returns `True` for
      `(declared_type, value)`.
    - It applies to inputs, overrides, extracted fields, output checks and `validate`. It does not apply to edges:
      Q10 keeps edges always checked.
    - A custom check is called only on a value the default refuses, so passing values cost nothing extra.
    - Hamilton's `do_validate_input` replaces the default check (H6.15).
11. **Result builders (Q19, Q24 updated).**
    - `Builder.with_result_builder[T](build: Callable[[dict[str, object]], T]) -> Builder[T]` sets how `execute`
      returns its outputs.
    - `build` gets the raw outputs as a `dict` in request order, inputs and overrides requested as outputs included,
      as in Hamilton (H6.12). Its return value is what `execute`, `PreparedPlan.run` and the async equivalents return.
    - **Typing:** `Builder`, `Driver`, `AsyncDriver`, `PreparedPlan` and `AsyncPreparedPlan` gain a type parameter
      `R`, defaulting to `dict[str, object]` (PEP 696), so ty sees the built type.
    - A second `with_result_builder` is a `ValueError` naming both. That is Q24's KEEP: Hamilton refuses two at build.
    - A builder that raises fails the run: `after_run` gets `ok=False`, `results` the raw dict, and the error.
    - **Raw results:** `Driver.raw_execute(final_vars, *, inputs=None, overrides=None) -> dict[str, object]` and
      `PreparedPlan.run_raw(...)`, with their async equivalents, run every hook and skip the builder (KEEP, H6.13).
    - The async driver honours the builder. Hamilton silently drops a `ResultBuilder` subclass there (H6.9, Q86).
12. **No pandas or polars builder in fnloom (Q89).**
    - Design §10 keeps pandas types out of core, and fnloom has no runtime dependency.
    - Hamilton's `PandasDataFrameResult` outer-joins Series of different lengths or indexes into NaN-padded floats
      without a word (H6.12).
    - The docs show four-line builders for both, `pandas.DataFrame(outputs)` and `polars.DataFrame(outputs)`, and say
      which Hamilton rules they don't copy: column flattening, the index warnings and the outer join.
    - The roadmap row is closed as "a callable, with recipes".
13. **Out of scope, each with a ledger line:**
    - Node-execution wrappers (Hamilton's `NodeExecutionMethod`, `GracefulErrorAdapter`). Q24's chain ships with step
      injection in Plan 8, and graceful partial failure with the "Beyond Hamilton" design.
    - `StaticValidator`. Plan 9 decides it; `Driver.nodes` already allows the same check after `build`.
    - Async hook methods.

## Global Constraints

- **Earlier plans' constraints:** every Global Constraint of Plans 1–5 applies unchanged.
    - Python 3.14 and 3.14t.
    - ty strict, ruff ALL, Clippy pedantic, rumdl, and the crate's MSRV.
    - One commit per task, with `scripts/check.sh` green on every commit.
    - Author email `4119211+kelsayed17@users.noreply.github.com`, with the `Co-Authored-By:` trailer.
    - The Python extension is built with `maturin develop --features test-hooks`, as `scripts/check.sh` does.
- **No consumer content, no copying, no new dependency:** nothing consumer-specific (design §8). No Hamilton code is
  copied. No new runtime dependency.
- **Register rows come first:** every difference from Hamilton 1.90.0 gets its register row before the
  `deliberate("Qn")` test that cites it is collected. New rows continue from Q84. Every kept behaviour gets a
  differential test in `tests/differential/test_semantics.py`.
- **The no-hook path is untouched:**
    - A driver with no hooks, no output checks, no value checks and no builder runs exactly today's code.
    - `benches.run` steady execute stays within noise on every shape.
    - The core's instruction counts (`crates/fnloom-core/benches`) stay within the CI gate for the no-observer run.
- **Hooked runs are measured:** the cost per node of a hook that overrides all four methods is measured on both
  drivers and recorded in `docs/benchmarks.md`. A hooked fnloom run must stay faster than an unhooked Hamilton run on
  every shape `benches.run` measures.
- **Carried settings:** every new builder setting (hooks, output checks, value checks, the result builder) survives
  pickling, `copy`, `rebuild()` and `build_async`. Hooks and builders pickle by Python's own rules: an unpicklable hook
  makes the driver unpicklable, with pickle's error.
- **Determinism:** event order is fixed for a given request on the sync driver, and every error lists all culprits,
  sorted, in one message.

## Review Focus

Each line has its test in the owning task.

1. **Node failure with every hook raising.** A node fails, its `after_node` hook raises, and `after_run` raises too.
   One `ExceptionGroup` is raised, led by the node's exception, which keeps its `in node` note. The hook errors follow
   in event order. (Task 2)
2. **A raising hook on the async driver.** A `before_node` hook raises while a sibling async node is in flight. The
   sibling is cancelled, the raising node gets no `after_node`, and `after_run` fires once, with `ok=False`.
   (Task 3)
3. **Every requested node a cache hit.** The events are `before_run`, the hits' paired events, then `after_run`. The
   core runs no step, and no other node gets an event. (Task 4)
4. **A result builder that raises.** `execute` raises the builder's error, and `after_run` has the raw `results`. On
   the same driver, `raw_execute` returns the dict. (Task 5)
5. **`with_output_type_checks` and widening.** `int` for `-> float` fails without widening and passes with it.
   `None` passes `-> int | None`. A node that raises reports its own error, never a mismatch on `None`. (Task 6)

## File Structure

```text
crates/fnloom-core/src/run.rs     Observer<V, E>: pre_node gets the args, post_node the result or error
crates/fnloom-py/src/observer.rs  ObserverBase hooks get kwargs, value and error; private still
crates/fnloom-py/src/plan.rs      builds kwargs per node only when observers are given
python/fnloom/
  _hooks.py      NEW  Hook, RunStart, NodeStart, NodeEnd, RunEnd, LogFailures, attach checks, the dispatcher
  driver.py      with_hooks, with_output_type_checks, with_value_check, with_result_builder, raw_execute,
                 run_raw, the R type parameter, before_run/after_run around PreparedPlan.run
  _async.py      the scheduler fires node events through the dispatcher
  _caching.py    outcomes per node for the dispatcher; hit events
  _types.py      custom value checks folded into the compiled checks; output checks
  __init__.py    + Hook, RunStart, NodeStart, NodeEnd, RunEnd, LogFailures
tests/
  test_hooks.py  test_hooks_async.py  test_hooks_cache.py  test_results.py  test_output_checks.py   NEW
  test_binding.py, test_threads.py, differential/test_semantics.py, differential/test_deliberate_differences.py
benchmarks/test_hooks.py (CodSpeed), benches/run.py (hooked rows)
docs/hooks.md (NEW, in the site nav), running.md, errors.md, caching.md, from-hamilton.md, rust-core.md,
benchmarks.md, quirk-register.md, index.md, README.md, CHANGELOG.md, the roadmap's ledger
```

---

### Task 1: The core's node events carry values

**Files:**

- Modify: `crates/fnloom-core/src/run.rs`, `crates/fnloom-py/src/observer.rs`, `plan.rs`, `python/fnloom/_core.pyi`,
  `docs/rust-core.md`
- Test: the `run.rs` unit tests, `crates/fnloom-core/benches/core.rs`, `tests/test_binding.py`

**Interfaces:**

- Produces:
    - Core: `trait Observer<V, E>`, with these methods:
        - `pre_graph(&mut self)`
        - `pre_node(&mut self, node: &str, args: &[Arg<'_, V>])`
        - `post_node(&mut self, node: &str, outcome: Result<&V, &E>)`
        - `post_graph(&mut self, ok: bool)`
    - Every method defaults to `Ok(())`, and the Q23 semantics are unchanged.
    - Binding: `ObserverBase` has these Python hooks:
        - `pre_graph()`
        - `pre_node(node, kwargs)`: `kwargs` is the dict the payload is called with, by parameter name.
        - `post_node(node, *, ok, value, error)`
        - `post_graph(*, ok)`
    - The binding builds no kwargs dict and no value object for a run with no observers.
- [x] **Step 1: Write the failing tests:**
    - core: `observers_see_each_nodes_arguments_and_result` and `observers_see_the_error_a_node_returned`
    - binding: `test_pre_node_gets_the_kwargs_the_payload_gets`, `test_post_node_gets_the_value_or_the_error`, and
      `test_a_parameter_left_to_its_default_is_absent`
- [x] **Step 2: Run** `cargo test -p fnloom-core` and `uv run pytest tests/test_binding.py -q`. Expected: fail.
- [x] **Step 3: Implement.**
    - In `compute`, pass `&args` to `pre_node`, and the evaluate result by reference to `post_node`.
    - In the binding, convert to Python only inside `PyObserver`.
    - Update the existing `Recorder` tests to the new hook signatures.
- [x] **Step 4: Run** `bash scripts/check.sh` and the callgrind bench against `baseline.json`. Expected: exit 0, and
  no instruction-count regression for the no-observer run.
- [x] **Step 5: Commit** `core: node events carry arguments and outcomes`.

### Task 2: Hooks on the sync driver

**Files:**

- Create: `python/fnloom/_hooks.py`
- Modify: `driver.py`, `__init__.py`, `docs/quirk-register.md` (Q84, Q23), `docs/hooks.md`, `docs/running.md`,
  `docs/errors.md`, `docs/from-hamilton.md`
- Test: `tests/test_hooks.py`, `tests/differential/*`, `tests/differential/test_deliberate_differences.py`

**Interfaces:**

- Produces:
    - Decisions 1–5 as public API.
    - A private `_hooks.Dispatch(hooks, views, outcomes)`, made once per run, used by both drivers and by Task 4. Its
      methods:
        - `start(outputs, inputs, overrides, nodes)`
        - `node_started(name, kwargs, outcome)`
        - `node_ended(name, kwargs, outcome, value, error)`
        - `end(ok, error, results)`
    - It runs every hook for an event, then raises the event's errors as decision 5 says.
    - `Builder.with_hooks(*hooks)`, and `Driver.hooks -> tuple[Hook, ...]`.
- Register rows Q84 and the Q23 update go in first.
- [x] **Step 1: Write the failing tests:**
    - `test_hooks_see_four_events_in_order_with_their_fields`
    - `test_inputs_overrides_and_unneeded_nodes_get_no_events`
    - `test_node_inputs_are_by_parameter_name_without_defaults`, with a `source()` binding and a namespace
    - `test_hooks_run_in_the_order_given_for_every_event`
    - `test_before_run_precedes_the_value_checks`
    - `test_attach_refuses_non_hooks_duplicates_bad_signatures_and_async_methods`
    - `test_only_overridden_methods_are_called`
    - `test_every_hook_runs_before_errors_are_raised`, covering each of the five cases in decision 5
    - `test_node_failure_with_raising_hooks_is_one_group_led_by_the_node` (Review Focus 1)
    - `test_hooks_survive_pickling_copy_rebuild_and_build_async`
    - `test_prepared_plan_runs_fire_the_hooks`
    - differential KEEP tests for decisions 3 and 4, and the order across hooks (H6.2–H6.4)
    - `deliberate` Q84 (H6.1, H6.4, H6.6) and Q23 (H6.5)
- [x] **Step 2: Run** `UV_NO_SYNC=1 uv run pytest tests/test_hooks.py -q`. Expected: fail.
- [x] **Step 3: Implement.**
    - `PreparedPlan.run` gains a hooked branch, chosen at `_compile`, and leaves the unhooked branch as it is.
    - The core gets one private observer that forwards node events to the `Dispatch`.
    - `Dispatch` maps a core group to decision 5's flat order. The core's own grouping is replaced in the hooked
      branch only; the no-hook path still raises the core's errors.
- [x] **Step 4: Run** `bash scripts/check.sh` and `uv run python -m benches.run`. Expected: exit 0, and steady
  execute within noise on every shape.
- [x] **Step 5: Commit** `hooks: Hook and its events on the sync driver`.

### Task 3: Hooks on the async driver

**Files:**

- Modify: `python/fnloom/_async.py`, `driver.py`, `docs/quirk-register.md` (Q86), `docs/hooks.md`
- Test: `tests/test_hooks_async.py`, `tests/differential/test_deliberate_differences.py`

**Interfaces:**

- Consumes `_hooks.Dispatch` from Task 2.
- Produces decision 6 on `AsyncDriver` and `AsyncPreparedPlan`.
- Register row Q86 goes in first.
- [x] **Step 1: Write the failing tests:**
    - `test_async_events_carry_values_not_tasks`
    - `test_a_failing_async_node_reports_ok_false`
    - `test_hooks_run_on_the_loop_thread_for_thread_and_inline_sync_nodes`
    - `test_concurrent_nodes_report_in_start_and_finish_order`
    - `test_a_raising_hook_cancels_the_rest` (Review Focus 2)
    - `test_the_same_hook_sees_the_same_events_on_both_drivers`, on a chain and a diamond
    - `deliberate` Q86 (H6.9)
- [x] **Step 2: Run** them. Expected: fail.
- [x] **Step 3: Implement.** Fire `node_started` just before a node's task or executor call is made, and
  `node_ended` in `_store` and in `_fail`. An error from a hook goes through `_fail`.
- [x] **Step 4: Run** `bash scripts/check.sh` and `uv run python -m benches.async_`. Expected: exit 0, and unhooked
  async within noise.
- [x] **Step 5: Commit** `hooks: the async driver fires the same events`.

### Task 4: Cache outcomes on node events

**Files:**

- Modify: `python/fnloom/_caching.py`, `_hooks.py`, `_async.py`, `docs/quirk-register.md` (Q85), `docs/caching.md`,
  `docs/hooks.md`
- Test: `tests/test_hooks_cache.py`, `tests/differential/test_deliberate_differences.py`

**Interfaces:**

- Consumes `Dispatch`'s `outcome` argument.
- Produces decision 7. On a cached, hooked run, `CacheRun.begin` and `CachedSchedule` hand the dispatcher:
    - the per-node `CacheOutcome`s;
    - the hits, in plan order.
  Both the native `KeyPlan.walk` path and the Python path must expose the names of their hits.
- Register row Q85 goes in first.
- [x] **Step 1: Write the failing tests:**
    - `test_a_miss_then_a_hit_carry_their_outcomes`
    - `test_a_hit_is_paired_with_no_inputs_and_the_loaded_result`
    - `test_hits_come_first_in_plan_order`
    - `test_nodes_needed_only_by_hits_get_no_events`
    - `test_all_hits_fire_no_computed_node` (Review Focus 3)
    - `test_uncacheable_nodes_say_why`
    - the same assertions on `AsyncDriver`, on `MemoryCache` and on `DiskCache`
    - `deliberate` Q85 (H6.10)
- [x] **Step 2: Run** them. Expected: fail.
- [x] **Step 3: Implement.** The outcomes come from the keys `begin` already computes. A hook-free cached run gathers
  nothing new.
- [x] **Step 4: Run** `bash scripts/check.sh` and `uv run python -m benches.cache`. Expected: exit 0, and unhooked
  cached runs within noise.
- [x] **Step 5: Commit** `hooks: cache outcomes on node events`.

### Task 5: Result builders and `raw_execute`

**Files:**

- Modify: `driver.py`, `_async.py`, `docs/quirk-register.md` (Q19, Q24, Q89), `docs/running.md`, `docs/hooks.md`,
  `docs/from-hamilton.md`
- Test: `tests/test_results.py`, `tests/differential/*`, `tests/differential/test_deliberate_differences.py`

**Interfaces:**

- Produces decisions 11 and 12. The new signatures:
    - `class Builder[R = dict[str, object]]`, and likewise `Driver[R]`, `AsyncDriver[R]`, `PreparedPlan[R]` and
      `AsyncPreparedPlan[R]`.
    - `Builder.with_result_builder[T](self, build: Callable[[dict[str, object]], T]) -> Builder[T]`
    - `Driver.raw_execute(final_vars, *, inputs=None, overrides=None) -> dict[str, object]`, and the async
      equivalent.
    - `PreparedPlan.run_raw(*, inputs=None, overrides=None) -> dict[str, object]`, and the async equivalent.
- Q19, Q24 and the new Q89 are written first.
- [x] **Step 1: Write the failing tests:**
    - `test_execute_returns_what_the_builder_returns`
    - `test_the_builder_gets_request_order_with_requested_inputs_and_overrides`
    - `test_raw_execute_and_run_raw_skip_the_builder_and_run_the_hooks`
    - `test_a_second_builder_is_refused`
    - `test_a_raising_builder_fails_the_run_with_raw_results` (Review Focus 4)
    - `test_the_async_driver_uses_the_builder`
    - `test_the_builder_survives_pickling_copy_rebuild_and_build_async`
    - `test_the_documented_pandas_recipe_builds_a_frame`, skipped when pandas is absent
    - module-level `assert_type` lines, checked by ty as in `tests/test_decorators.py`: `execute` returns
      `dict[str, object]` with no builder and the builder's type with one
    - differential KEEP tests: request order, requested inputs included, `raw_execute` skips the builder (H6.12,
      H6.13)
    - `deliberate` Q19 and Q24 (H6.12), and Q89 (H6.12, the NaN outer join)
- [x] **Step 2: Run** them. Expected: fail.
- [x] **Step 3: Implement.** The builder is applied after the run, in the same place on both drivers. The unhooked,
  builder-free path returns the dict exactly as today.
- [x] **Step 4: Run** `bash scripts/check.sh` and `uv run python -m benches.run`. Expected: exit 0, and steady
  execute within noise.
- [x] **Step 5: Commit** `results: result builders and raw_execute`.

### Task 6: Output checks and custom value checks

**Files:**

- Modify: `driver.py`, `_types.py`, `_async.py`, `_extract.py`, `docs/quirk-register.md` (Q87, Q21),
  `docs/writing-nodes.md`, `docs/errors.md`, `docs/from-hamilton.md`
- Test: `tests/test_output_checks.py`, `tests/differential/*`, `tests/differential/test_deliberate_differences.py`

**Interfaces:**

- Produces decisions 9 and 10:
    - `Builder.with_output_type_checks() -> Builder`
    - `Builder.with_value_check(check: Callable[[object, object], bool]) -> Builder`
- Both are compiled at build into each node's checks; a node with an `Any` or `object` return gets no output check.
- Q87 and the Q21 update go in first.
- [x] **Step 1: Write the failing tests:**
    - `test_a_wrong_result_fails_its_node_with_a_type_error`
    - `test_fixed_tuples_generics_and_newtypes_check_correctly`
    - `test_widening_and_none_follow_the_driver_rules` (Review Focus 5)
    - `test_a_raising_node_keeps_its_own_error`
    - `test_after_node_sees_the_mismatch_as_the_failure`
    - `test_a_custom_check_admits_what_the_default_refuses_and_is_not_called_otherwise`
    - `test_custom_checks_apply_to_inputs_overrides_fields_outputs_and_validate`
    - `test_both_settings_survive_pickling_copy_rebuild_and_build_async`
    - the same assertions on `AsyncDriver`
    - differential KEEP: no output check by default (H6.14)
    - `deliberate` Q87 (H6.17) and Q21 (H6.15)
- [x] **Step 2: Run** them. Expected: fail.
- [x] **Step 3: Implement.** The output check wraps only the payloads that need it, at build. A custom check is tried
  after the compiled default refuses a value.
- [x] **Step 4: Run** `bash scripts/check.sh` and `uv run python -m benches.run`. Expected: exit 0, and steady
  execute within noise with neither setting.
- [x] **Step 5: Commit** `checks: output type checks and custom value checks`.

### Task 7: `LogFailures`

**Files:**

- Modify: `_hooks.py`, `__init__.py`, `docs/quirk-register.md` (Q88), `docs/hooks.md`, `docs/errors.md`
- Test: `tests/test_hooks.py`, `tests/differential/test_deliberate_differences.py`

**Interfaces:**

- Produces decision 8's `LogFailures`.
- Q88 goes in first.
- [x] **Step 1: Write the failing tests:**
    - `test_nothing_is_logged_by_default`
    - `test_log_failures_logs_the_node_and_its_cut_inputs_with_exc_info`
    - `test_log_failures_uses_the_given_logger_and_level`
    - `test_log_failures_is_silent_on_success_and_on_hits`
    - `deliberate` Q88 (H6.11)
- [x] **Step 2: Run** them. Expected: fail.
- [x] **Step 3: Implement** it as a `Hook` subclass with only `after_node`.
- [x] **Step 4: Run** `bash scripts/check.sh`. Expected: exit 0.
- [x] **Step 5: Commit** `hooks: LogFailures`.

### Task 8: Benchmarks, threads, docs and the ledger

**Files:**

- Create: `benchmarks/test_hooks.py`
- Modify: `benches/run.py`, `benches/async_.py`, `tests/test_threads.py`, `docs/benchmarks.md`, `docs/hooks.md`,
  `docs/from-hamilton.md`, `docs/index.md`, `README.md`, `CHANGELOG.md`, the roadmap
- Test: the CodSpeed suite, `tests/test_threads.py`
- [x] **Step 1: Add the measurements:**
    - CodSpeed: a chain of 1,000 nodes with a hook overriding all four methods, on both drivers, plus output checks
      on.
    - `benches.run` and `benches.async_`: rows for a hooked chain and a hooked fan-in.
- [x] **Step 2: Add** `test_one_hook_shared_by_threads_sees_each_run_whole`: 16 threads over one hooked, cached
  driver, events grouped by `run_id`. Each run's events form a complete, ordered sequence.
- [x] **Step 3: Run** `uv run python -m benches.run` and `uv run python -m benches.async_` on `main` and on this
  branch.
    - Record the hooked per-node cost in `docs/benchmarks.md`.
    - Check the Global Constraints: no-hook runs within noise, and hooked fnloom faster than unhooked Hamilton.
- [x] **Step 4: Close the docs:**
    - Close the roadmap rows that name Plan 6, and add decision 13's lines.
    - In `from-hamilton.md`, map each lifecycle adapter, `ResultBuilder`, `FunctionInputOutputTypeChecker`, `PrintLn`
      and `GracefulErrorAdapter` to what fnloom offers, and update "Not there yet".
    - Update `README.md` and `index.md`.
    - Put the CHANGELOG entries under `[Unreleased]`.
- [x] **Step 5: Run** `bash scripts/check.sh` and `uv run pytest benchmarks -q`. Expected: exit 0.
- [x] **Step 6: Commit** `plan 6: benchmarks, threads, docs and the ledger`.

## As built

- **`RunStart.nodes` on the async driver is the plan's step order** (topological, ties broken by name), not the order
  the events come in: concurrent nodes have no fixed event order (ruling R1). Q86 and the hooks guide say so.
- **Node inputs are read-only.** `NodeStart.inputs` and `NodeEnd.inputs` are a `MappingProxyType` over the call's
  arguments, so a hook can't change what a node is called with (R3), as decision 2 meant by a read-only mapping.
- **Inputs are keyed by parameter name for every node.** A node the core calls through a wrapper, such as one input
  feeding several parameters or a `group(...)`, has its arguments re-keyed to its parameter names when hooked (R4).
- **`RunStart.nodes` is `()` when a value check fails before any node runs**, for a missing input and a mistyped one
  alike, since such a run reports no node (R5). A custom value check that raises ends the run the same way, after
  the final review: `before_run` with no nodes, then `after_run(ok=False)` with its very error, which `execute`
  raises unwrapped (decision 4).
- **Final review fixes.** `LogFailures` bounds each input's `repr` with `reprlib` before the 50-character cut, and
  shows one that raises as `<unrepresentable TypeName>`, so the node's own exception is still the one raised
  (decision 8). `RunEnd.results` is a copy of its own, never the dict `execute` returns. `Driver(graph, hooks=...)`
  without `nodes` is a `TypeError` up front rather than a `KeyError` at the first `execute`.
- **A node whose `after_node(ok=True)` hook raised is not stored in the cache**, on either driver. Decision 5
  discards its value, so storing it would make the next run's hit report a value the hooks refused (R8).
- **With a `recompute` node, `RunStart.nodes` is an upper bound.** It is the inner level's exact list, then every
  other node the run could report, in plan order: which later nodes hit is known only after the `recompute` node ran,
  which is after `before_run`. It is exact for a run with no `recompute` node (R9, Q85 and the hooks guide).
- **A hooked `MemoryCache` run takes the Python walk**, not the native `KeyPlan.walk`, because the outcomes need the
  keys; unhooked runs keep the native walk (R10). Its cost is measured in [Benchmarks](../benchmarks.md#hooks). Making
  the native walk return its keys is the remedy if it matters.
- **The async scheduler's `_drain` is copied, not shared.** The hooked run has its own `_drain`, the plain one plus a
  `_begin` line, since the no-hook path may not change. `test_the_hooked_drain_mirrors_the_plain_drain` compares the
  two methods' source with that line removed and fails, naming both, if they drift (R6).
- **`fnloom.ValueCheck` is public**, the type `with_value_check` takes, so its signature names no private type (R14).
  In the same round, the async driver kept its guided "returned an awaitable" refusal for a sync node when output
  checks are on.
- **Decision 12 was wrong about the outer join.** It said the recipes don't copy Hamilton's outer join, but
  `pandas.DataFrame` itself outer-joins Series of different lengths or indexes, padding with NaN (R12). The docs say
  so, and give a strict `frame` recipe that refuses outputs on different indexes, tested in `tests/test_results.py`.
  Q89 records both.
- **polars is a prose line, not a running block** (R13): polars is not installed in development, and a block that
  can't run can't be tested.
- **Hooked runs, measured** ([Benchmarks](../benchmarks.md#hooks)). Unhooked runs are within noise of the base
  (steady 0.94× to 1.06× on `benches.run`). A hook overriding all four methods costs about 2 µs per reported node on
  both drivers, and a hooked `MemoryCache` run about 1 µs per node more. Hooked steady execution beats unhooked
  Hamilton on every `benches.run` shape and size, by 1.22× at the least. The first `execute` does not on seven cells
  of 10 or 100 nodes, 3% to 43% slower in one sample each, since a hooked plan's first run also makes the node views;
  this is recorded, not fixed.
- **Register rows:** Q84 (hooks), Q85 (cache outcomes on node events), Q86 (hooks on the async driver), Q87 (output
  checks), Q88 (failure logging) and Q89 (no DataFrame builder), with Q19, Q21, Q23 and Q24 updated from *Planned*.
