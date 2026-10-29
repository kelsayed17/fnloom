# fnloom Plan 8: output validators, step injection and injection by config

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Status:** done, 2026-10-28, on `claude/plan-8-validators-steps-injection`, merge pending. Approved with four
amendments (marked *Amended*). [As built](#as-built) records where the build went beyond the plan or narrowed it.

**Goal:** close the roadmap's step 7, the v1.x decorators. That means:

- **Output validators:** `@check_output` and `@check_output_custom`. Errors subclass `ValueError`, and fail is the
  default.
- **Step injection:** `@pipe_input`, `@pipe_output`, `@mutate` and `step()`, and Q24's ordered chain of
  node-execution wrappers, which Plan 6 deferred here.
- **Injection by config:** `@inject` and `@resolve`.
- **Ledger:** the decorator and execution rows of the roadmap that name Plan 8. None may still say **same** or
  **worse** when this plan is done.

**Architecture:**

- **No Rust change.** Every decorator records what it was given on the function (`Decorations`), as `@parameterize`
  and `@extract_fields` do, and the scan lowers it to node definitions.
- **Validators** wrap a node's payload, as Plan 6's output checks do (Q87). They add no node: the checks run after the
  node returns, inside the node.
- **Steps** lower to ordinary nodes beside their function's own, as `@extract_fields` does. A `step` node's payload
  is the step function with its bindings, so steps are typed, checked, cached and hooked like any node.
- **`@inject`** is a one-row `@parameterize` that keeps the function's name.
- **`@resolve`** is called with the build's config before the scan reads the function's decorations. It returns the
  decorators to apply, and those are recorded as if written on the function.
- **Node wrappers** are an ordered chain the driver composes around each computed node's payload at build. A driver
  without wrappers compiles exactly today's payloads.

**Tech Stack:** as Plans 1–7. Python 3.14 for all tasks, ty strict, ruff ALL, rumdl, pytest, and
`apache-hamilton==1.90.0` as the oracle.

**Spec:**

- `docs/design/2026-10-26-fnloom-design.md`:
    - §2, native API with no compatibility layer;
    - §5, the v1.x order: validators, step injection, injection by config;
    - §6, Q24;
    - §10, pandas types out of core.
- `docs/plans/2026-10-28-roadmap-ahead-of-hamilton.md`: the Plan 8 decorator rows and these problem rows:
    - "Validation errors are not `ValueError`";
    - "`@check_output` is pandas-only";
    - "`@pipe_output` can't share work";
    - "Decorator stacking order is ignored".
- The oracle's behaviour, probed on 2026-10-28, is catalogued in the session notes as
  `hamilton-plan8-validators-steps-injection.md` and cited below as "H8.3" and so on. Tests re-probe whatever they
  rely on.

## fnloom today, probed against the same points

| Point | Hamilton 1.90.0 | fnloom 0.2.0 + Plan 7 |
|---|---|---|
| Checking a node's value | `@check_output`: three nodes per checked node, `warn` by default, `DataValidationError(Exception)` holding a list (H8.1–H8.3) | `with_output_type_checks()` checks types only (Q87) |
| Custom checks | `DataValidator` subclasses; `applies_to` never called; sees only the output (H8.4, H8.6) | `with_value_check` for inputs and types (Q21) |
| Steps around a node | `@pipe_input`, `@pipe_output`, `@mutate`; defaulted step parameters leak as global inputs (H8.11–H8.17) | none |
| Injecting bindings | `@inject` (H8.20) | `@parameterize` with one row renames the node |
| Decorators from config | `@resolve`, power-user mode only (H8.21) | none |
| Wrapping node execution | one `NodeExecutionMethod` per driver (H8.23) | none: hooks observe, never wrap |

## Decisions for review

Each decision becomes a register row, Q99 on, or an update to a row, in the task that builds it. Change any before
the build starts.

1. **Validators add no node (Q99).**
    - `@fnloom.check_output(...)` and `@fnloom.check_output_custom(...)` check a node's value inside the node, after its
      function returns.
    - The node keeps its name and its place in the graph. There is no `x_raw` and no validator node. Hamilton turns
      one node into three, all requestable and hooked (H8.3).
    - A failed `fail` check is the node's own failure, so `after_node` gets `ok=False` with the error. A node that
      raises is never checked (KEEP, H8.9).
    - **Results are on the node event.** `NodeEnd.checks: tuple[CheckResult, ...]` has every check's result, passed
      or not, in order. `fnloom.CheckResult` is frozen, with `name`, `importance`, `passes`, `message` and
      `diagnostics`. Hamilton exposes results only as validator-node outputs and tags (H8.8).
2. **Failing is the default, and the error is a `ValueError` (Q100).**
    - `importance="fail"` by default, `"warn"` on request. Hamilton defaults to warn (H8.2), and the roadmap's row
      says fail.
    - `fnloom.ValidationError(ValueError)` has a readable message, one line per failed `fail` check:
      `x: check range failed: 12 is not in [0, 10]`. Its `results` attribute holds the failed checks' `CheckResult`s.
      Hamilton's `DataValidationError` subclasses `Exception` only, and its message is a list's repr (H8.2).
    - **A `warn` failure** logs one WARNING per failed check per run on the `fnloom` logger, and the run goes on (KEEP
      the level, H8.2). *Amended:* through the standard `logging` module only: no `print` and no `warnings.warn`.
    - **Order:** all of a node's checks run before any decision, then every failed `fail` check is raised in one
      error (KEEP, H8.2).
3. **Built-in checks over any type (Q101).** `check_output(**checks, importance="fail")` takes:
    - `range=(lo, hi)`;
    - `values_in=[...]`;
    - `data_type=T`, checked by `fnloom.value_matches`, so generics and unions work;
    - `allow_none=False`;
    - `contains=` and `does_not_contain=` for `str`;
    - `allow_nans=False` and `max_fraction_nans=f`.

    How they apply:
    - `range`, `values_in` and the NaN checks apply to a scalar, and to each item of a value that iterates (an array,
      a Series, a list), with no pandas or numpy import. A value with `min()` and `max()` methods (numpy, pandas) is
      ranged through them.
    - *Amended:* a `str`, `bytes`, `bytearray` or `Mapping` is one scalar, never iterated as items. A one-shot
      iterator (`iter(v) is v`, a generator) is never consumed: a check on a node annotated `Iterator` or `Generator`
      is a `TypeError` at build, and a one-shot value met at run is a `TypeError` naming the node and the check. The
      value a node returns is never consumed or changed by a check. numpy and pandas values are checked through their
      own vectorised `min`, `max`, `isin` and NaN tests, found by duck typing, never a Python loop.
    - An argument that doesn't apply to the node's return annotation is a `TypeError` at build, naming the node, the
      argument and the type. Hamilton crashes with a raw `TypeError` on `list[int]` or `int | None` (H8.1).
    - Bad argument values are refused when decorating, e.g. `max_fraction_nans=2` and `range=(3, 1)`.
    - Hamilton's pandas-only checks (`max_standard_dev`, `mean_in_range`, pandera `schema=`) are not built in. The
      docs give each as a three-line custom check.
4. **Custom checks (Q102).** `@check_output_custom(*checks)` takes plain functions or `fnloom.Validator` instances.
    - **A function** `def positive(value: int) -> bool` is a check named after the function. It returns `bool`, a
      message `str` (a failure), `None` (a pass) or a `CheckResult`. A check that declares a parameter `inputs` gets
      the node's arguments by parameter name, read-only. Hamilton's validators see only the output (H8.6).
    - **A `fnloom.Validator` subclass** has:
        - `validate(self, value, inputs) -> bool | str | None | CheckResult`;
        - optionally `applies_to(self, declared) -> bool`, called at build, where `False` is a `TypeError` naming the
          node;
        - `name` and `importance` (default `"fail"`).

      Hamilton never calls `applies_to`, and a `bool` result is an `AttributeError` (H8.4).
    - **An `async def` check** is a `TypeError` when decorating, on both drivers. Hamilton fails at run with
      `AttributeError` on the sync driver (H8.7).
5. **Stacking, overrides and caching (Q103).**
    - **Stacking:** `@check_output` and `@check_output_custom` stack in any number and any order; their checks run in
      the order written, top first. Hamilton refuses a second `@check_output` with a duplicate-node error (H8.5).
    - **Which value is checked:** a function's own value, after its `@pipe_output` steps and every `@mutate` step,
      whatever the decorator order. Hamilton checks before or after the steps by decorator order, and a mutated value
      escapes the check (H8.19).
    - **Extracted fields:** `@check_output(..., field="p")` checks an `@extract_fields` or `@extract_columns` field
      rather than the whole value. Hamilton decides by decorator order, or by `target_=` (H8.5).
    - **Other decorators:** with `@parameterize` and `@config.when*`, every row and variant is checked (KEEP, H8.5).
    - **Overrides:** an overridden node is not checked, since it doesn't run (KEEP, H8.7).
    - **Cache hits:** a hit is not checked again. A value that failed a `fail` check is never stored. One that only
      warned is stored, and its warning is not logged again: Q87's rule for type checks, and Hamilton's (KEEP, H8.7).
      Its `NodeEnd.checks` is `()`.
    - *Amended:* **checks are part of the cache key.** A node's static key holds its checks: each built-in's
      arguments by fingerprint, each check function by its code version, each `Validator` by its class version and
      fingerprint. Adding, removing or changing a check misses, so a value stored before a stricter check never comes
      back unchecked. A check with no version makes the node uncacheable, saying why.
6. **Steps: shape and names (Q104).**
    - `fnloom.step(fn, *args, **bindings)` binds `fn`'s parameters after its first one. A binding is
      `source("node")`, `value(v)`, or a bare literal, read as `value` (KEEP, H8.11).
    - A step can be modified with:
        - `.named(name)`;
        - `.when(**)`, `.when_not(**)`, `.when_in(**)` and `.when_not_in(**)`, with `@config.when*`'s rules;
        - `.on_output(*fields)`, for `@pipe_output` and `@mutate` on extracted fields.
    - **`@pipe_input(*steps, on_input=None)`** chains the steps on a parameter, by default the first one. The function
      then reads the last step.
    - **`@pipe_output(*steps)`** runs the function as `<fn>.raw`, then the steps, and the node named `<fn>` is the
      last step's value.
    - **Each step is an ordinary node** named `<fn>.with_<step>`, with `_1`, `_2` and so on for repeats, or `.named`'s
      name. Steps are requestable, typed, cached and hooked (KEEP, H8.11, H8.15).
    - **Repeat numbers count every step written**, so a name never changes with the config. Hamilton counts only the
      steps the config kept, so `f.with_add_1` is a different step under another config (H8.13).
    - **Tags:** the function's `@tag`s go on its final node only (KEEP, H8.11). A step node's `module` is its step
      function's, and the final node's is the function's. Hamilton sets the final node's `module` tag to its own
      `hamilton.function_modifiers.macros` (H8.15).
    - **Public step functions:** a step function that is public in a scanned module is still a node of its own, as any
      public function is (KEEP, H8.11). The docs say to name it `_add` or mark it `@exclude`.
7. **A step's defaulted parameters stay its own (Q105).**
    - A step parameter with a default, not bound in `step(...)`, keeps its default. It is never a graph input.
    - Hamilton makes it an unnamespaced input shared by every step with that parameter name, in any pipe or module,
      which can collide with a node (H8.12).
    - A required parameter left unbound is a `TypeError` when decorating (KEEP, H8.12).
8. **Misuse is refused where Hamilton is silent or raw (Q106).**
    - `on_input` naming no parameter: a `TypeError` when decorating. Hamilton silently uses the first parameter
      (H8.14).
    - `@pipe_input` on a function with no parameters: a `TypeError` when decorating. Hamilton raises a raw `IndexError`
      at build (H8.18).
    - **The head of a `@pipe_input` chain is type checked.** The first step's first parameter must accept the
      function's own annotation for that parameter, by `type_matches`, at build. Hamilton re-types the graph input to
      the step's annotation without a word (H8.18).
    - A step with no parameters, `*args`, an unknown binding, or no annotations is a `TypeError` when decorating,
      naming the step (KEEP, H8.18).
    - An `.on_output` field the function doesn't produce is a `ValueError` at build. Hamilton ignores it (H8.17).
9. **`@mutate` (Q107).**
    - `@fnloom.mutate(*targets, **bindings)` on a step function appends a step to each target's `@pipe_output`, or
      creates one (KEEP, H8.17).
    - Targets are functions, or `apply_to(fn, **bindings)` whose bindings replace the defaults (KEEP).
    - Steps run in definition order (KEEP). The mutator itself is no node (KEEP).
    - **Cross-module targets work** when both modules are built. Hamilton refuses them, pointing at a setting that
      doesn't exist (H8.17).
    - A target no built module defines is a `ValueError` at build, naming it. Hamilton ignores it (H8.17).
    - A string target is a `TypeError` when decorating, saying to pass the function.
    - **Applying the same mutator twice adds its step once**, so re-running a notebook cell doesn't double it.
      Hamilton's source notes the doubling as a TODO (H8.17).
10. **Steps with `@parameterize` and `@inject` (Q108).**
    - `@pipe_input` with `@parameterize` or `@inject` gives each row its own chain, named under the row:
      `row.with_add`. Hamilton refuses the combination at build (H8.19).
    - `@pipe_output` with `@parameterize`, `@extract_*` and `@config.when*` works per row, field and variant, in any
      decorator order (KEEP the shapes, H8.19). The order question is gone, since `.on_output` picks fields.
11. **`@inject` (Q109).**
    - `@fnloom.inject(**bindings)` binds parameters to `source`, `value` or `group` and keeps the function's name and
      docstring (KEEP, H8.20).
    - A bare literal is a `TypeError`, as with `@parameterize`.
    - `@inject` with `@parameterize` on one function is a `TypeError` when decorating, as Hamilton refuses it (KEEP).
12. **`@resolve` (Q110).**
    - `@fnloom.resolve(decorate_with)`: `decorate_with` is called with the config entries its parameters name, and a
      defaulted parameter is an optional key (KEEP, H8.21).
    - It returns one decorator or a sequence of them. They are applied as if written on the function, the first
      outermost.
    - **No power-user mode.** Hamilton requires `hamilton.enable_power_user_mode=True`, and raises a raw `KeyError`
      without the key (H8.21).
    - **A missing required key** is a `ValueError` at build, naming the function and the key.
    - **Refused when decorating:** `*args`/`**kwargs` in `decorate_with` (KEEP). A non-decorator return value is a
      `TypeError` at build, naming the function and what came back.
    - A resolved decorator stacks with the written ones by the usual rules. Hamilton's resolved-plus-written
      `@parameterize` fails with `too many values to unpack` (H8.21).
    - Config keys stay out of the graph (Q56).
    - **Caching:** the result cache keys a resolved node by its own function and bindings, as any node.
13. **Node wrappers, an ordered chain (Q111, Q24 updated).**
    - `Builder.with_node_wrappers(*wrappers)` is cumulative. A wrapper is a callable
      `wrapper(node: NodeView, call: Callable[[], object], inputs: Mapping[str, object]) -> object`.
    - `call()` runs the node: the next wrapper, then the function with its checks. A wrapper may call it zero or more
      times, or return a value of its own (KEEP, H8.23).
    - **The first wrapper given is the outermost.** Any number may be given, where Hamilton refuses a second
      `NodeExecutionMethod` (H8.23).
    - **Wrappers see the user's function, never an internal one.** A step node's wrapper sees its step. Hamilton
      hands generated nodes its own callables (H8.23).
    - **Order with hooks:** `before_node` and `after_node` fire around the whole chain (KEEP, H8.23). Validators run
      inside it, so a wrapper sees a `ValidationError`.
    - **Cache hits:** a hit skips the chain. A wrapper's return value is checked by output checks when they are on.
    - *Amended:* **wrappers are execution-only, never part of the cache key.** What the chain returns is stored like
      any value, a wrapper that calls `call()` zero times included, and a later run with other wrappers, or none,
      gets that value on a hit. A wrapper that changes values is the user's to keep apart, by giving such a driver
      its own store; the docs say so.
    - **Checked when attached:** a non-callable or an `async def` wrapper is a `TypeError` from
      `with_node_wrappers`. On the async driver, wrappers wrap sync nodes. An `async def` node with wrappers is a
      `TypeError` from `build_async`, naming it, until async wrappers are designed.
    - **Carried:** wrappers survive pickling, `copy`, `rebuild()` and `build_async`, as hooks do.
    - Graceful partial failure stays with "Beyond Hamilton", and the docs show a `retry` wrapper.
14. **Out of scope, each with a ledger line:**
    - `flow` and `Applicable.on_input`, which don't exist in 1.90.0 (H8.10);
    - `collapse=True`;
    - Hamilton's deprecated `pipe` alias;
    - pandera `schema=`;
    - async validators and async wrappers;
    - per-step tags.

## Global Constraints

- **Earlier plans' constraints:** every Global Constraint of Plans 1–7 applies unchanged.
    - Python 3.14 and 3.14t.
    - ty strict, ruff ALL, Clippy pedantic, rumdl, and the crate's MSRV.
    - One commit per task, with `scripts/check.sh` green on every commit.
    - Author email `4119211+kelsayed17@users.noreply.github.com`, with the `Co-Authored-By:` trailer.
    - Build with `maturin develop --features test-hooks`, and run pytest with `UV_NO_SYNC=1`.
- **No consumer content, no copying, no new dependency:** no Hamilton code is copied, no runtime dependency is added,
  and numpy and pandas stay test-only.
- **Register rows come first:** each difference gets its row before the `deliberate("Qn")` test that cites it. New
  rows continue from Q99. Each kept behaviour gets a differential test.
- **The undecorated path is untouched:** a graph with no Plan 8 decorator and no wrapper runs exactly today's code.
  `benches.run` stays within noise, and the cost of a checked node and of a one-wrapper chain is recorded in
  `docs/benchmarks.md`.
- **Determinism:** node names, order and errors never depend on `PYTHONHASHSEED`, and every error lists all culprits,
  sorted.
- **Decorator order:** every Plan 8 decorator stacks in any order with every other decorator, and gives the same
  graph. The roadmap's stacking row is closed by a test over each pair's two orders.

## Review Focus

1. **A `warn` and a `fail` check fail on one node.** One WARNING is logged, the `ValidationError` names only the
   `fail` check, and `NodeEnd.checks` holds both results. (Task 1)
2. **`@pipe_output` + `@mutate` + `@check_output` in every decorator order.** The same graph each time, and the check
   sees the mutated value. (Tasks 2, 3)
3. **A defaulted step parameter.** `step(_mul)` with `k: int = 10` adds no input `k`, and two pipes both using `_mul`
   don't share one. (Task 2)
4. **A resolved `@parameterize`, stacked with a written `@tag`.** Each row is tagged, and a missing config key names
   the function. (Task 4)
5. **Two wrappers, a validator and a hook.** Call order is `before_node`, wrapper 1, wrapper 2, the function, the
   checks, back out, then `after_node`. A wrapper calling `call()` twice runs the function twice. (Task 5)

## File Structure

```text
python/fnloom/
  _checks.py     NEW  check_output, check_output_custom, Validator, CheckResult, ValidationError, the built-ins
  _steps.py      NEW  step, pipe_input, pipe_output, mutate, apply_to; lowering to node definitions
  _resolve.py    NEW  inject, resolve
  _decorators.py Decorations gains checks, steps and resolvers
  _scan.py       lowers steps, applies resolvers, wraps checked payloads
  _hooks.py      NodeEnd.checks
  driver.py      with_node_wrappers, the wrapper chain, carried settings
  __init__.py    + the new names
tests/
  test_checks.py  test_steps.py  test_mutate.py  test_resolve.py  test_wrappers.py   NEW
  test_stacking.py (each pair of decorators in both orders)                          NEW
  differential/test_semantics.py, differential/test_deliberate_differences.py
benchmarks/test_checks.py (CodSpeed), benches/run.py (a checked row, a wrapped row)
docs/writing-nodes.md (checks, steps, inject, resolve), running.md (wrappers), hooks.md (NodeEnd.checks),
from-hamilton.md, quirk-register.md, benchmarks.md, index.md, README.md, CHANGELOG.md, the roadmap's ledger
```

---

### Task 1: Output validators

- [ ] Register rows Q99–Q103.
- [ ] Failing tests:
    - each built-in check on a scalar, a list, a numpy array and a pandas Series (skipped without them);
    - `TypeError` at build for a check that doesn't apply;
    - bad argument values refused when decorating;
    - `fail` and `warn`, and Review Focus 1;
    - custom functions, `inputs`, `Validator.applies_to`, `bool`, `str`, `None` and `CheckResult` returns, and
      `async def` refused;
    - stacking order;
    - `field=`;
    - overrides, cache hits, the async driver, and `NodeEnd.checks`.
- [ ] `_checks.py`, the `Decorations` field, payload wrapping at build, and `NodeEnd.checks`.
- [ ] KEEP differential tests: which values pass and fail the built-ins, against Hamilton's validators. Also
  `deliberate("Q99")` through `("Q103")`.
- [ ] Check green; commit.

### Task 2: Steps, `@pipe_input` and `@pipe_output`

- [ ] Register rows Q104–Q106 and Q108.
- [ ] Failing tests:
    - names and graph shape for both decorators;
    - bindings, `.named`, the `.when*` variants and `.on_output`;
    - repeat names stable across config;
    - Review Focus 3;
    - misuse errors;
    - the head-type check;
    - `@parameterize` and `@inject` rows;
    - extracted fields;
    - caching, hooks and the async driver.
- [ ] `_steps.py` and the scan's lowering.
- [ ] KEEP differential tests: shapes, names and values against Hamilton's for the shared cases. Also
  `deliberate("Q104")`, `("Q105")`, `("Q106")` and `("Q108")`.
- [ ] Check green; commit.

### Task 3: `@mutate` and `apply_to`

- [ ] Register row Q107.
- [ ] Failing tests:
    - definition order;
    - `apply_to` bindings;
    - cross-module targets;
    - an unknown target;
    - a string target;
    - a mutator applied twice;
    - Review Focus 2.
- [ ] `mutate` and `apply_to`.
- [ ] KEEP differential test of shape and order, and `deliberate("Q107")`.
- [ ] Check green; commit.

### Task 4: `@inject` and `@resolve`

- [ ] Register rows Q109 and Q110.
- [ ] Failing tests:
    - `inject` with `group`, docstrings, the refusals, and `inject` with `pipe_input`;
    - `resolve` to `@parameterize`, `@extract_columns`, `@tag` and a sequence;
    - optional keys and a missing key;
    - a non-decorator return;
    - Review Focus 4.
- [ ] `_resolve.py`, and resolvers applied in the scan with the build's config.
- [ ] KEEP differential tests of results against Hamilton's (in power-user mode), and `deliberate("Q109")` and
  `("Q110")`.
- [ ] Check green; commit.

### Task 5: Node wrappers

- [ ] Register row Q111, and update Q24.
- [ ] Failing tests:
    - chain order;
    - zero and two calls;
    - Review Focus 5;
    - a wrapper raising;
    - a cache hit skipping the chain;
    - checks on attach;
    - async-node refusal;
    - carried settings (pickle, `copy`, `rebuild`, `build_async`);
    - threads.
- [ ] `with_node_wrappers` and the chain composed at build.
- [ ] `deliberate("Q111")`, against two `NodeExecutionMethod`s refused.
- [ ] Benchmarks: a wrapped and a checked row; the undecorated path within noise.
- [ ] Check green; commit.

### Task 6: Stacking, docs and the ledger

- [ ] `tests/test_stacking.py`: every pair of Plan 8 decorators, and each with `@tag`, `@parameterize`,
  `@extract_fields` and `@config.when`, in both orders. Each gives the same graph.
- [ ] Docs: the guides with tested samples, `from-hamilton` (the method map and "Not there yet"), the changelog, the
  roadmap's ledger and step 7, and this plan's As built.
- [ ] Check green; commit.

## As built

- **Check results reach `NodeEnd.checks` through a per-build token.** A hooked run sets a context variable that its
  driver's checks report to; a check of another driver, run inside a node, can't, so results never leak between
  nested runs. A run with no `after_node` hook collects nothing.
- **Checks, beyond the decisions:**
    - A check that raises fails the node with its own error, noting `in check <name>`, whatever its importance.
    - `allow_none=True` lets `None` pass every check of the node.
    - NaN range bounds are refused when decorating.
    - `range` counts NaN as outside the range at every level of a frame, passes an empty value, and fails rather than
      crashes on values that can't be compared.
    - numpy's `isin` gets the allowed values as objects, so a mixed list isn't turned into strings.
    - A node annotated as a one-shot iterator, or a union with one, can carry no check at all, and a one-shot value
      at run is refused before any check, custom ones included, can read it.
- **`refuse_unreadable` runs after steps are lowered**, so a `@pipe_output` node's checks are read against its last
  step's type.
- **Steps:**
    - A `group(...)` parameter can't be fed by `@pipe_input`.
    - A fed parameter with a default reads the chain: the chain always gives it a value.
    - A positional-only step parameter is refused when decorating.
    - An `async def` function with `@pipe_output` gets an awaited final node.
    - A field's final node under `@pipe_output` is stored by the function's cache policy, unlike a plain field,
      whose value is never stored; its `.raw` node keeps the field's policy.
    - Using one step function in both `@pipe_input` and `@pipe_output` of a function names two nodes alike: the
      error says to use `.named`.
- **`@mutate`:**
    - Applying the same mutator again replaces its steps only when its code changed, as a re-run notebook cell's
      does. Mutators made in a loop or a factory share code and each add a step, and so do several `apply_to`s of one
      target in one call: Hamilton adds them all, and so does fnloom.
    - A module of mutators alone builds (Q53's "no nodes" refusal skips it).
    - A `@config.when` variant the config doesn't select is a valid target.
    - A node mutated by a module that isn't built fails the build, naming the module, since pickling would lose the
      step.
- **`@resolve` works on a copy of the function**, made per build after config selection, so an unselected variant's
  resolver never runs. The node's view names the function as written (`NodeView.origin`), and
  `NodeViews.built_from` keeps the copy for export, which reads its decorations, such as a pinned version.
- **Annotations are read as forward references** wherever Plan 8 reads a signature (wrappers, steps, checks,
  resolvers), so a name imported only under `TYPE_CHECKING` is no error; a wrapper annotated that way failed in the
  first build.
- **Wrappers and output type checks.** With output type checks on, a wrapped node's value is checked inside the chain,
  on the function's value, and again outside it, on what the chain returns. Every computed node is wrapped, step,
  `.raw` and field nodes included, so a wrapper sees each.
- **Benchmarks** are recorded in [Benchmarks](../benchmarks.md#checks-and-wrappers) from the development build that
  `scripts/check.sh` makes. `benches.run` was not re-run: a graph with no Plan 8 decorator and no wrapper builds the
  very node definitions it built before, since `attached`, `lowered`, `wrapped` and the chain return their input
  unchanged when there's nothing to do.
- **Stacking** is tested over 36 pairs (`tests/test_stacking.py`). Every pair gives one graph in either order, except
  `@inject` with `@parameterize`, which is refused in both orders.
- **Register rows:** Q99 to Q111, and Q24 updated.
