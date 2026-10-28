# fnloom Plan 5: inspection, validation and authoring

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Status:** approved by the maintainer, 2026-10-28. Being built on `claude/plan-5-inspection-validation-authoring`.

**Goal:** close the roadmap's step 4. That means:

- **Inspection:** the inspection Hamilton has and fnloom lacks.
- **Validation:** a `validate` that reports every problem instead of raising the first.
- **Authoring without modules:** functions in `final_vars`, graphs built from plain functions, and namespacing.
- **Opt-ins:** the strict opt-ins Hamilton users rely on.
- **Ledger:** the wiring and types rows of the roadmap's "Hamilton's problems" ledger. None may still say **same** or
  **worse** when this plan is done.

**Architecture:** almost all of this is front end (`python/fnloom`). The core already has `Graph.between`. Inspection
and validation read the scanned `NodeDef`s and the built graph. Authoring changes `_scan.collect`: it gains function
sources, namespaces, `__all__` and `@exclude`. Type rules change in `_types`. The new type rules and the opt-ins are
settled at build, so a run pays for them only where a check already runs. The Rust core is untouched unless Task 8
needs a renamed-input hook, which it should not: names are rewritten before the graph is built.

**Tech Stack:** as Plans 1–4 and 3. Python 3.14 (`annotationlib`, `difflib`), ty strict, ruff ALL, pytest, and
`apache-hamilton==1.90.0` as the oracle.

**Spec:**

- `docs/design/2026-10-26-fnloom-design.md`: §2, §5 (v1.x "between", "parameterize sugar"), §7 and §8.
- `docs/plans/2026-10-28-roadmap-ahead-of-hamilton.md`: the Plan 5 gap rows and the wiring, types and execution rows
  of the ledger.
- Register rows Q4, Q5, Q7, Q22, Q30 and Q43.
- The oracle's behaviour, probed on 2026-10-28, is catalogued in the session notes as
  `hamilton-plan5-inspection-validation-authoring.md` and cited below as "H5.2c" and so on. Tests re-probe whatever
  they rely on.

## fnloom today, probed against the same points

| Point | Hamilton 1.90.0 | fnloom 0.1.1 + Plan 3 |
|---|---|---|
| Unsupplied input requested as an output | `{'x': None}` (H5.12) | `ValueError: missing inputs: 'x' (requested as an output)`, with no register row yet |
| A name bound with `value()` passed as an input | silently dropped (H5.4e) | `unknown inputs: 'p'` (Q4) when nothing else reads it |
| `None` for `x: int` | passes the check (H5.2i) | passes the check |
| `tuple[int, str]` input | every tuple refused (H5.10d) | any tuple accepted, items unchecked |
| `int` for `float`, numpy scalar for `int` | refused (H5.10f) | refused |
| `Any` and `int` consumers of one input | checked as `int` (H5.10b) | checked as `int` |
| Node shadowing a parameter default | node wins silently (H5.6c) | node wins silently |
| Override of an input not supplied | refused (H5.5) | accepted (Q7) |
| Function in `final_vars` | mapped by `__name__`; wrong for rows (H5.3) | `TypeError` (Q30, DEFER) |
| `TYPE_CHECKING`-only annotation | `NameError` at build (H5.10a) | `NameError` at build, noted with the node |

## Decisions for review

Each decision becomes a register row (Q70 on) in the task that builds it. Change any before the build starts.

1. **Inspection (Q70).**
    - `Driver.inputs() -> Mapping[str, InputView]`: every graph input, with its type, whether it is required for the
      whole graph, and its consumers, sorted.
    - `Driver.outputs() -> list[str]`: the nodes nothing consumes, sorted.
    - `Driver.modules -> tuple[ModuleType, ...]`: the modules as given, each once. Hamilton's `graph_modules` keeps
      duplicates (H5.1f).
    - `Driver.what_is_the_path_between(a, b) -> list[str]`:
        - **Endpoints:** inclusive of both, as in Hamilton.
        - **Order:** topological, ties broken by name. Hamilton's order changes with `PYTHONHASHSEED` (H5.1e).
        - **Errors:** a `ValueError` naming every unknown endpoint. Hamilton names only the first.
    - `AsyncDriver` gets all four.
    - The roadmap's `list_available_variables` gap is met by `Driver.nodes` plus these. fnloom gets no unhashable
      `HamiltonNode` copy.
2. **`Driver.validate(final_vars, *, inputs=None, overrides=None) -> list[str]` (Q71).** It runs everything `prepare`
   and a run's input checks would, without running anything, and returns every problem as one sorted line each.
    - **What it covers:** unknown outputs, unknown inputs and overrides, missing inputs, input and override types, and
      unmatched config variants.
    - **Malformed arguments raise:** a malformed request (a bare `str`, a non-mapping) raises as `prepare` does.
    - **Consumers in the missing-input line:** it names only the consumers inside this request. Hamilton lists the
      whole graph's (H5.2d).
    - **Hamilton's rough edges it avoids:** Hamilton's `validate_execution` raises, takes `overrides` before
      `inputs`, and refuses functions that `execute` accepts (H5.2a, H5.2e).
    - **Async:** `AsyncDriver.validate` is the same function, and is synchronous.
3. **Spelling suggestions by default (Q71).** Every "unknown" or "missing" name in an error gets a
   `did you mean 'revenue'?` suggestion when `difflib.get_close_matches` finds a node or input within cutoff 0.8.
   Hamilton never suggests (H5.6a).
4. **Functions in `final_vars` (Q30 changes from DEFER to FIX).** A function means the node it produces in this
   driver, matched by identity with `NodeView.origin`.
    - **One node:** a plain function means its node, and an `@extract_*` function means its producer.
    - **Several nodes:** a `@parameterize` function makes several, so it raises `TypeError` listing its rows and
      saying to pass their names. Hamilton maps it to its `__name__`, which is not a node (H5.3b).
    - **No node:** a function from another module, an unselected `@config` variant, or a lambda raises `ValueError`
      naming the function and the reason.
    - **Where it works:** `execute`, `prepare`, `validate` and both drivers. The result is still keyed by name.
5. **Graphs from functions (Q72).**
    - `Builder.with_functions(*fns)` adds plain or decorated functions without a module.
        - **Node name:** each node is named by `fn.__name__`.
        - **No copies:** functions are not copied and no module is registered in `sys.modules`.
          `ad_hoc_utils.create_temporary_module` does both (H5.9a).
        - **Code versions and identity:** these use the function's own `__module__` and qualified name.
    - **`fnloom.node(fn, *, name=None, bind=None) -> NodeSpec`** is the public node-definition API. It makes one node
      from any annotated callable, named and bound without decorating it (`bind` takes `source()` and `value()` as
      `@parameterize` rows do). `with_functions` accepts it.
    - **Refused:** a lambda or other unnamed callable without `name=`.
    - **Pickling:** a driver built this way pickles when its functions do (by qualified name). Otherwise it raises the
      0.1.1 `TypeError`.
6. **Module scanning (Q73).**
    - **`@fnloom.exclude`** keeps a public function out of the graph. Hamilton's `@hamilton_exclude` is an instance
      that only works bare (H5.7c). fnloom's works bare and called.
    - **`__all__` is honoured:** when a module defines it, only the functions it names are nodes. Hamilton ignores
      `__all__` (H5.7b).
    - **Excluded-by-`__all__` hint:** a missing input whose name is a function the module's `__all__` left out says so
      in the error.
    - **No warning for unconsumed nodes.** The roadmap suggested one, but every output is a node nothing consumes, so
      it would fire on every graph. `Driver.outputs()` answers the question instead.
7. **Namespaces (Q74).**
    - **API:** `Builder.with_modules(*modules, namespace="ns")`.
    - **Names:** each node gets the dotted name `ns.name`, as Hamilton's `@subdag` names them (H5.8c).
    - **Parameter resolution:**
        - inside a namespace, a parameter reads `ns.param` when the namespace defines it;
        - otherwise it reads the un-namespaced `param`, an outer node or a graph input;
        - a parameter can't reach into another namespace.
    - **Reuse:** one module may be given under several namespaces. That's the reuse `allow_module_overrides` can't
      give.
    - **Collisions:** names still clash as today. fnloom offers no last-wins override (H5.8a).
    - **Unchanged:** cache keys use the full name, so nothing else changes.
8. **Opt-in strictness (Q75).** Both settle at build, so the default stays as it is.
    - **`Builder.with_strict_overrides()`** refuses an override naming an input that isn't also supplied. That's
      Hamilton's rule (H5.5), and Q7 stays the default.
    - **`Builder.with_declared_inputs(*names)`** fails the build for any graph input not declared, naming the closest
      node or declared name. A misspelled parameter or `source("...")` (H5.6a) becomes a build error.
9. **Defaults (Q76).**
    - **Two new build errors:**
        - a computed node with the same name as a parameter default, which Hamilton lets win silently (H5.6c);
        - two consumers giving one input different defaults (H5.6d).
    - **Opt-out:** per parameter, with `@fnloom.defaults(shadowed=("cost",), differing=("shared",))` on the
      consumer.
    - **Breaking:** this is a deliberate breaking change. A module that relied on either pattern gets an error naming
      the parameter, the node, and the opt-out.
10. **`value()`-bound names (Q77).** An input or override that no node reads is already refused (Q4). Q77 records that
    Hamilton silently drops it instead (H5.4e). When another node does read the name, it's a real input, and the
    `value()` row keeps its constant. Both sides are tested.
11. **Unsupplied input as an output (Q78).** fnloom's refusal is kept and gets its row and test. Hamilton returns
    `None`, even for an input with a default (H5.12).
12. **Types (Q79–Q81).**
    - **a. `None` (Q79).** `None` passes an input check only when the type admits it: `Optional[...]`,
      `X | None`, `object`, `Any`, `None` itself, or a `TypeVar`.
        - Hamilton passes `None` for every type (H5.2i). Its node then fails with an unrelated `TypeError`.
        - This is a deliberate breaking change for callers who pass `None` to non-optional inputs.
    - **b. Fixed-length tuples (Q80).** `tuple[int, str]` checks length and each item with the same matcher. Hamilton
      refuses every tuple (H5.10d), and fnloom 0.1.1 accepted any tuple.
    - **c. Unresolvable annotations (Q81).**
        - **What it becomes:** a name that doesn't resolve at build becomes a named opaque type, e.g. one imported only
          under `TYPE_CHECKING`. It matches only an annotation naming the same thing, and `Any`.
        - **No `NameError`:** the build no longer raises `NameError`, which Hamilton does (H5.10a).
        - **When it surfaces:** only when a check needs it. An edge check with any other type fails, naming the
          unresolved name. A run's input check against it fails with `TypeError`, and the message says to import the
          name at run time.
    - **d. Opt-in deep checks:** `Builder.with_deep_type_checks()` checks every item of `list`, `dict`, `set`,
      `tuple[T, ...]` and nested generics, iteratively. Shallow stays the default, for cost. The roadmap keeps that
      choice, and a benchmark row records the deep cost.
    - **e. Public matchers:** `fnloom.value_matches(type, value)` and `fnloom.type_matches(produced, expected)` are
      public. They're the checks `execute` and `build` use, and Hamilton's `htypes.check_input_type` is public too
      (H5.10g).
    - **f. Opt-in numeric widening:** `Builder.with_numeric_widening()` lets an `int` stand for `float`, and numpy
      integer and floating scalars stand for `int` and `float`.
        - numpy is detected by type module, without importing it.
        - Hamilton refuses all of these (H5.10f).
        - The register's "Not yet decided" note on `ndim`, `dtype` and length contracts stays deferred. They came from
          one consumer's workloads, and the design's §8 forbids consumer-specific content.
    - **g. The roadmap's `Any` row (#1180).** It says `Any` on one consumer absorbs a concrete type. Probing shows
      Hamilton 1.90.0 checks such an input as `int` (H5.10b), as fnloom does. The row is corrected to "n/a: not a
      1.90.0 behaviour", with a differential KEEP test pinning both.
13. **`@parameterize` sugar (Q82).**
    - **Docstring forms:**
        - A row may be `(bindings, docstring)`, and the docstring is used verbatim, as in Hamilton (H5.4a).
        - The function's docstring is templated per row with `{output_name}` and each bound parameter.
        - An unknown placeholder raises when decorating. Hamilton silently prints the bare key (H5.4a).
    - **`group(*deps)` / `group(**deps)`** makes a list- or dict-valued parameter. Misuse is refused when decorating,
      with Hamilton's checks (H5.4b), and each offender is listed once.
    - **`parameterize_sources(**rows)` and `parameterize_values(parameter, assigned_output)`** are the shorthands.
        - A non-`str` source is refused when decorating. Hamilton fails at build with a raw `TypeError` (H5.4c).
        - An empty `assigned_output` is refused. Hamilton builds zero nodes silently (H5.4d).
14. **Threads and reload (Q83).**
    - **Thread safety:** a `Driver` is documented as safe to share between threads, on default and free-threaded
      builds, with a stress test.
    - **`Driver.rebuild() -> Driver`:** builds the driver again from its builder. After `importlib.reload`, the new
      driver sees the edit. An existing driver keeps its code, as Hamilton's does (H5.11b), and that stays
      documented.

## Global Constraints

- **Plans 1–4 and 3 constraints:** every Global Constraint of Plans 1–4 and 3 applies unchanged.
    - Python 3.14 and 3.14t.
    - ty strict, ruff ALL, Clippy pedantic and rumdl.
    - One commit per task, with `scripts/check.sh` green on every commit.
    - Author email `4119211+kelsayed17@users.noreply.github.com`, with the `Co-Authored-By:` trailer.
- **Nothing consumer-specific** (design §8). No Hamilton code is copied. No new runtime dependency: numpy is read only
  from a value's own type.
- **Register rows come first:** every difference from Hamilton 1.90.0 gets its register row before the
  `deliberate("Qn")` test that cites it is collected. New rows continue from Q70. Every kept behaviour gets a
  differential test in `tests/differential/test_semantics.py`.
- **Breaking changes are called out:** each breaking change to fnloom's own behaviour (decisions 6, 9, 12a) gets a
  CHANGELOG entry under **Changed**, saying what to write instead.
- **Determinism:** every listing is sorted, and every error lists all culprits, sorted, in one message.
- **Performance:**
    - **No per-run cost for build-time rules:** every rule that depends only on names or types is settled at build.
    - **Benchmarks:** the run path gets a benchmark for the `None` rule, fixed tuples, deep checks and widening.
      Builds get one for `__all__`, namespaces and `with_functions`.
    - **The sync numbers must hold:** `benches.run` steady execute within noise on every existing shape.
- **No recursion** over graph depth or container depth.

## Review Focus

These are inputs the decisions imply but no obvious test covers. Each line has its test in the owning task.

1. A namespaced module whose function parameter has the same name as a node in the namespace *and* an outer node.
   The namespace's node must win. Requesting the outer node must still work. (Task 8)
2. A module whose `__all__` names a function that `@exclude` also marks. It is excluded, and there's no error. A module
   whose `__all__` is not a list or tuple of `str` gets a `TypeError` at build, naming the module. (Task 6)
3. `None` passed for an `Optional[int]` input whose consumer also has a default. It reaches the node as `None`, not the
   default, as today. (Task 9)
4. A function passed in `final_vars` that is both a node in this driver and decorated with `@exclude` in a second
   module given to the same driver. The answer is decided by identity, so that module's own object is never a node.
   (Task 2)
5. `validate` on a request that `prepare` would accept but whose input values fail the checks. It returns the type
   lines and runs nothing. `prepare` on the same request succeeds, because values are checked at run. (Task 3)

## File Structure

```text
python/fnloom/
  __init__.py      + exclude, node, NodeSpec, defaults, group, parameterize_sources, parameterize_values,
                     value_matches, type_matches, InputView
  _view.py         InputView; Driver.inputs/outputs support
  _scan.py         function sources, namespaces, __all__, @exclude, NodeSpec lowering
  _requests.py     NEW  final_vars normalisation (strings and functions), suggestions, validate()
  _decorators.py   exclude, defaults, group, parameterize row tuples and doc templating, the two shorthands
  _types.py        None rule, fixed-length tuples, unresolved opaque types, deep checks, widening; public matchers
  driver.py        inputs, outputs, modules, what_is_the_path_between, validate, rebuild, with_functions,
                   with_strict_overrides, with_declared_inputs, with_deep_type_checks, with_numeric_widening,
                   namespace= on with_modules; pickling carries every new builder setting
tests/
  test_inspection.py  test_validate.py  test_authoring.py  test_namespaces.py  test_defaults.py
  test_type_rules.py  test_parameterize_sugar.py                                        NEW
  test_threads.py (stress test), differential/test_semantics.py, test_deliberate_differences.py
benches/run.py (a "types" shape; build rows for namespaces and with_functions)
docs/writing-nodes.md, running.md, errors.md, from-hamilton.md, quirk-register.md, benchmarks.md, CHANGELOG.md,
the roadmap's ledger
```

---

### Task 1: Inspection

**Files:**

- Modify: `python/fnloom/driver.py`, `_view.py`, `__init__.py`, `docs/running.md`, `docs/from-hamilton.md`,
  `docs/quirk-register.md`
- Test: `tests/test_inspection.py`, `tests/differential/*`

**Interfaces:**

- Produces:
    - `@dataclass(frozen=True, slots=True) class InputView`, with fields:
        - `name: str`
        - `type: object`
        - `required: bool`: required by some consumer
        - `consumers: tuple[str, ...]`: sorted
    - On `Driver` and `AsyncDriver`:
        - `inputs() -> Mapping[str, InputView]`: sorted by name.
        - `outputs() -> list[str]`.
        - `modules: tuple[ModuleType, ...]`: build order, each once. `with_functions` sources aren't modules, so they
          don't appear.
        - `what_is_the_path_between(upstream: str, downstream: str) -> list[str]`.
- Register rows Q70 (decision 1) and Q78 (decision 11) go in first.
- [ ] **Step 1: Write the failing tests:**
    - `test_inputs_lists_every_input_with_type_requiredness_and_consumers`
    - `test_outputs_are_the_unconsumed_nodes`
    - `test_modules_are_unique_in_build_order`
    - `test_path_between_is_inclusive_and_topological`, which runs the query in two subprocesses with different
      `PYTHONHASHSEED` and expects equal results
    - `test_path_between_unknown_names_are_all_named`
    - `test_path_between_no_path_is_empty`
    - `test_async_driver_has_the_same_inspection`
    - differential KEEP tests: endpoints included; an empty result for no path or a reversed pair; `a == b` gives
      `[a]`
    - `deliberate` Q70 (order and unknown names, H5.1e) and Q78 (H5.12)
- [ ] **Step 2: Run** `UV_NO_SYNC=1 uv run pytest tests/test_inspection.py -q`. Expected: fail.
- [ ] **Step 3: Implement.**
    - `what_is_the_path_between` uses the core's `Graph.between`. Check its argument order and its endpoint
      inclusion, and order the result with the plan's topological order.
    - `inputs()` is built once at build, from `core_inputs` and the views.
- [ ] **Step 4: Run** `bash scripts/check.sh`. Expected: exit 0.
- [ ] **Step 5: Commit** `inspection: inputs, outputs, modules and path between`.

### Task 2: Functions in `final_vars`

**Files:**

- Create: `python/fnloom/_requests.py`
- Modify: `driver.py`, `_async.py`, `docs/quirk-register.md` (Q30), `docs/running.md`, `docs/from-hamilton.md`
- Test: `tests/test_validate.py` (the normalisation half), `tests/differential/*`

**Interfaces:**

- Produces `_requests.output_names(final_vars: Sequence[str | Callable[..., object]], views: NodeViews) ->
  tuple[str, ...]`. It runs the existing `_check_final_vars` rules for strings and decision 4 for callables, with
  every error listed in one message.
    - `TypeError` when several nodes match.
    - `ValueError` when none match.
    - A bare `str` stays a `TypeError` (Q6).
- Q30's row changes from DEFER to FIX. Its old deliberate test changes to assert the new rule on fnloom's side and
  Hamilton's mapping (H5.3a–g) on Hamilton's.
- [ ] **Step 1: Write the failing tests:**
    - `test_a_plain_function_names_its_node`
    - `test_an_extract_function_names_its_producer`
    - `test_a_parameterize_function_lists_its_rows`
    - `test_a_foreign_function_an_unselected_variant_and_a_lambda_are_refused`
    - `test_strings_and_functions_mix_and_collapse_to_one_key`
    - `test_identity_not_name_decides` (Review Focus 4)
    - `test_async_and_prepare_accept_functions`
    - the `deliberate("Q30")` rewrite
- [ ] **Step 2: Run** them. Expected: fail.
- [ ] **Step 3: Implement.** Map views by `id(origin)` once per driver, held with the views.
- [ ] **Step 4: Run** `bash scripts/check.sh`, and `uv run python -m benches.run` for steady execute on `chain 1,000`.
  Expected: exit 0, and execute within noise, since string-only requests take the existing fast path.
- [ ] **Step 5: Commit** `requests: functions in final_vars`.

### Task 3: `validate` and spelling suggestions

**Files:**

- Modify: `_requests.py`, `driver.py`, `_async.py`, `docs/running.md`, `docs/errors.md`, `docs/quirk-register.md`
- Test: `tests/test_validate.py`, `tests/differential/*`

**Interfaces:**

- Produces:
    - `Driver.validate(final_vars, *, inputs: Mapping | None = None, overrides: Mapping | None = None) -> list[str]`,
      also on `AsyncDriver`.
    - `_requests.suggest(name: str, candidates: Iterable[str]) -> str`, which returns `""` or
      `" (did you mean 'x'?)"`.
- Every existing "unknown inputs", "unknown overrides", "unknown outputs" and "missing inputs" message gains the
  suggestion, so the core's messages are wrapped where they're raised in Python. The 0.1.1 missing-input consumers
  format stays.
- Register row Q71 (decisions 2 and 3) goes in first.
- [ ] **Step 1: Write the failing tests:**
    - `test_validate_returns_every_problem_sorted`: unknown output, misspelled input, missing input, wrong input
      type, wrong override type, and an unmatched variant, in one call
    - `test_validate_on_a_valid_request_is_empty`
    - `test_validate_runs_nothing` (Review Focus 5)
    - `test_missing_lines_name_only_requested_consumers`
    - `test_errors_suggest_close_names`, plus a case with no suggestion below the 0.8 cutoff
    - `test_validate_matches_execute_messages`: each line equals the line `execute` raises for the same lone problem
    - a `deliberate("Q71")` test against `validate_execution` (H5.2a–e)
- [ ] **Step 2: Run** them. Expected: fail.
- [ ] **Step 3: Implement.** `validate` reuses `_Request.check` and the compiled checks without raising at the first
  problem, collecting lines instead.
- [ ] **Step 4: Run** `bash scripts/check.sh`. Expected: exit 0.
- [ ] **Step 5: Commit** `requests: validate returns every problem; suggest close names`.

### Task 4: Strict overrides and declared inputs

**Files:**

- Modify: `driver.py`, `docs/running.md`, `docs/quirk-register.md`, `docs/from-hamilton.md`
- Test: `tests/test_validate.py`, `tests/differential/*`

**Interfaces:**

- Produces:
    - `Builder.with_strict_overrides() -> Builder`
    - `Builder.with_declared_inputs(*names: str) -> Builder`. Calling it again adds names. Declaring a name that isn't
      a graph input fails the build too, with a suggestion.
- Both settings are carried through pickling and `rebuild()`.
- Register row Q75 goes in first.
- [ ] **Step 1: Write the failing tests:**
    - `test_strict_overrides_refuse_an_unsupplied_input`: the message matches Q7's old Hamilton rule, and the
      default is unchanged
    - `test_declared_inputs_refuse_an_undeclared_input_naming_the_closest`
    - `test_a_misspelled_source_fails_the_build_when_declared`
    - `test_declaring_a_non_input_fails`
    - `test_settings_survive_pickling`
    - differential KEEP: strict overrides behave as Hamilton (H5.5) on a fixture
- [ ] **Step 2: Run** them. Expected: fail.
- [ ] **Step 3: Implement** both in `Builder` and `Driver`.
- [ ] **Step 4: Run** `bash scripts/check.sh`. Expected: exit 0.
- [ ] **Step 5: Commit** `builder: strict overrides and declared inputs`.

### Task 5: Defaults, and `value()`-bound names

**Files:**

- Modify: `_decorators.py`, `_scan.py`, `driver.py`, `__init__.py`, `docs/writing-nodes.md`,
  `docs/quirk-register.md`, `CHANGELOG.md`
- Test: `tests/test_defaults.py`, `tests/differential/*`

**Interfaces:**

- Produces `defaults[F](*, shadowed: Iterable[str] = (), differing: Iterable[str] = ()) -> Callable[[F], F]`, which
  records names on `Decorations.defaults`. A name that isn't a parameter with a default is refused when decorating.
- The build errors for decision 9 are `ValueError`s. Each lists every case, sorted, naming the parameter, the node or
  the other consumer, and the opt-out.
- Register rows Q76 and Q77 go in first.
- [ ] **Step 1: Write the failing tests:**
    - `test_a_node_shadowing_a_default_fails_the_build`
    - `test_differing_defaults_fail_the_build`
    - `test_opt_outs_restore_hamilton_behaviour`, where shadowed takes the node and differing keeps each default
    - `test_equal_defaults_are_fine`
    - `test_a_value_bound_name_is_refused_unless_read_elsewhere`
    - `deliberate` Q76 (H5.6c, H5.6d) and Q77 (H5.4e)
    - a CHANGELOG **Changed** entry
- [ ] **Step 2: Run** them. Expected: fail.
- [ ] **Step 3: Implement** the checks in the build, once.
- [ ] **Step 4: Run** `bash scripts/check.sh`. Expected: exit 0.
- [ ] **Step 5: Commit** `wiring: refuse shadowed and differing defaults`.

### Task 6: `@exclude` and `__all__`

**Files:**

- Modify: `_decorators.py`, `_scan.py`, `__init__.py`, `docs/writing-nodes.md`, `docs/quirk-register.md`,
  `CHANGELOG.md`
- Test: `tests/test_authoring.py`, `tests/differential/*`

**Interfaces:**

- Produces `exclude`, usable as `@exclude` and `@exclude()`. It records `Decorations.excluded = True`, and returns
  the function.
- `_scan` skips excluded functions and, when `__all__` is defined, every function not named in it. It keeps the
  skipped names per module, for the missing-input hint and the 0.1.1 empty-module error, which must now also give
  "excluded" and "not in `__all__`" as reasons.
- Register row Q73 goes in first.
- [ ] **Step 1: Write the failing tests:**
    - `test_exclude_bare_and_called`
    - `test_all_limits_nodes`
    - `test_a_missing_input_named_like_a_function_left_out_of_all_says_so`
    - `test_exclude_and_all_together` (Review Focus 2)
    - `test_a_malformed_all_is_a_type_error`
    - `test_an_all_only_module_with_no_listed_function_is_refused_as_empty`, giving the reason
    - `deliberate` Q73 (H5.7b, H5.7c)
    - a CHANGELOG **Changed** entry, since `__all__` can remove nodes from existing modules
- [ ] **Step 2: Run** them. Expected: fail.
- [ ] **Step 3: Implement** in `_scan._is_node` and `collect`.
- [ ] **Step 4: Run** `bash scripts/check.sh` and the build benchmark (`chain` and `rows` at 1,000). Expected: exit
  0, and build within noise.
- [ ] **Step 5: Commit** `scan: @exclude and __all__`.

### Task 7: `with_functions` and `fnloom.node`

**Files:**

- Modify: `_scan.py`, `driver.py`, `__init__.py`, `docs/writing-nodes.md`, `docs/quirk-register.md`,
  `docs/from-hamilton.md`
- Test: `tests/test_authoring.py`, `tests/test_pickling.py`, `tests/differential/*`

**Interfaces:**

- Produces:
    - `@dataclass(frozen=True, slots=True) class NodeSpec`, with fields:
        - `fn: Callable[..., object]`
        - `name: str`
        - `bind: Mapping[str, Source | Value]`
    - `node(fn, *, name: str | None = None, bind: Mapping[str, Source | Value] | None = None) -> NodeSpec`, which
      checks names and bindings as `@parameterize` does.
    - `Builder.with_functions(*items: Callable[..., object] | NodeSpec) -> Builder`, cumulative and ordered.
- `collect` takes function sources beside modules. Duplicates across modules and functions get the existing clash
  message. Config, extract, cache and tags work on them.
- Register row Q72 goes in first.
- [ ] **Step 1: Write the failing tests:**
    - `test_with_functions_builds_without_a_module`, where `sys.modules` is unchanged
    - `test_decorated_functions_keep_their_decorations`
    - `test_node_names_and_binds_a_callable`
    - `test_a_lambda_needs_a_name`
    - `test_functions_and_modules_mix_and_clash_by_name`
    - `test_cache_keys_use_the_function_module`
    - `test_pickling_by_qualified_name`, plus the `TypeError` for a local function
    - `deliberate` Q72 (H5.9a: `create_temporary_module` copies functions and registers a module)
- [ ] **Step 2: Run** them. Expected: fail.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `bash scripts/check.sh`. Expected: exit 0.
- [ ] **Step 5: Commit** `authoring: graphs from functions; fnloom.node`.

### Task 8: Namespaces

**Files:**

- Modify: `_scan.py`, `_config.py` (unmatched names are namespaced), `driver.py`, `docs/writing-nodes.md`,
  `docs/quirk-register.md`, `docs/from-hamilton.md`
- Test: `tests/test_namespaces.py`, `tests/differential/*`

**Interfaces:**

- Produces `Builder.with_modules(*modules, namespace: str | None = None)`. A namespace is a non-empty dotted
  identifier path, and is refused otherwise.
- Lowering:
    - Each `NodeDef` from a namespaced module has `name = f"{ns}.{name}"`.
    - Each `Source` binding is resolved by decision 7 after every module is scanned, so the rule can see the
      namespace's own names.
- `NodeView` gains `namespace: str | None`.
- Register row Q74 goes in first.
- [ ] **Step 1: Write the failing tests:**
    - `test_a_namespace_prefixes_every_node`
    - `test_parameters_resolve_inside_then_outside`
    - `test_one_module_under_two_namespaces`
    - `test_a_namespace_cannot_read_another`
    - `test_inner_name_beats_outer` (Review Focus 1)
    - `test_namespaced_config_extract_and_parameterize`
    - `test_bad_namespace_is_refused`
    - `deliberate` Q74 (H5.8a: last-wins; H5.8c: only `@subdag`)
- [ ] **Step 2: Run** them. Expected: fail.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `bash scripts/check.sh` and a new build benchmark row: `layered` at 1,000 split over 10
  namespaces. Expected: exit 0, and a build within 10% of the un-namespaced one.
- [ ] **Step 5: Commit** `wiring: namespaces`.

### Task 9: Type rules I, covering `None`, fixed tuples, public matchers and the `Any` row

**Files:**

- Modify: `_types.py`, `__init__.py`, `docs/quirk-register.md`, `docs/writing-nodes.md`, `CHANGELOG.md`,
  the roadmap
- Test: `tests/test_type_rules.py`, `tests/test_types.py`, `tests/differential/*`

**Interfaces:**

- Produces:
    - `value_matches(declared: object, value: object) -> bool`
    - `type_matches(produced: object, expected: object) -> bool`
    - Both are public, exported, and documented with the rules they implement.
- The `None` rule (decision 12a) and fixed-length tuples (decision 12b) live in `_fits`.
- Register rows Q79 and Q80 go in first.
- [ ] **Step 1: Write the failing tests:**
    - `test_none_passes_only_types_that_admit_it`: the table of decision 12a
    - `test_optional_default_still_receives_none` (Review Focus 3)
    - `test_fixed_tuples_check_length_and_items`, with nested tuples and `tuple[()]`
    - `test_public_matchers_equal_the_driver_checks`
    - a KEEP differential test for the `Any` and `int` consumers (H5.10b)
    - `deliberate` Q79 (H5.2i) and Q80 (H5.10d)
    - a CHANGELOG **Changed** entry for `None`
- [ ] **Step 2: Run** them. Expected: fail.
- [ ] **Step 3: Implement.** Correct the roadmap's `Any` row per decision 12g.
- [ ] **Step 4: Run** `bash scripts/check.sh`. Then add a `types` shape to `benches/run.py`: one node whose inputs are
  `Optional[int]`, `tuple[int, str, float]` and `list[int]`. Run it, and record its rows. Expected: steady execute on
  existing shapes within noise.
- [ ] **Step 5: Commit** `types: None only where admitted; fixed tuples checked; public matchers`.

### Task 10: Type rules II, covering unresolved annotations, deep checks and widening

**Files:**

- Modify: `_types.py`, `_scan.py`, `driver.py`, `docs/quirk-register.md`, `docs/writing-nodes.md`,
  `docs/benchmarks.md`
- Test: `tests/test_type_rules.py`, `tests/differential/*`

**Interfaces:**

- Produces:
    - `_types.Unresolved(name: str)`: a frozen opaque type marker. Its `repr` is `unresolved 'decimal.Decimal'`.
    - `Builder.with_deep_type_checks() -> Builder`.
    - `Builder.with_numeric_widening() -> Builder`.
- `resolve` maps a failing annotation to `Unresolved` instead of raising. The Plan 1 `NameError` tests change to
  assert the new behaviour. The register keeps the old behaviour as Hamilton's side.
- Register row Q81 goes in first. Decisions 12d and 12f are opt-ins, documented in their pages, and need no row.
- [ ] **Step 1: Write the failing tests:**
    - `test_a_type_checking_import_builds`
    - `test_an_unresolved_edge_names_the_annotation`
    - `test_an_unresolved_input_check_says_to_import_it`
    - `test_same_unresolved_name_matches_itself`
    - `test_deep_checks_find_a_bad_item_in_nested_containers_iteratively`, at depth 10,000
    - `test_widening_accepts_int_for_float_and_numpy_scalars`, guarded by `importorskip("numpy")` for the numpy half
    - `test_widening_is_off_by_default`
    - `deliberate` Q81 (H5.10a)
- [ ] **Step 2: Run** them. Expected: fail.
- [ ] **Step 3: Implement.** The deep check walks with an explicit stack, and uses `plain_class` fast paths per item.
- [ ] **Step 4: Run** `bash scripts/check.sh`. Then run the `types` shape with deep checks on and off, and record
  both. Expected: shallow within noise of Task 9's rows.
- [ ] **Step 5: Commit** `types: unresolved annotations, deep checks, numeric widening`.

### Task 11: `@parameterize` sugar

**Files:**

- Modify: `_decorators.py`, `_scan.py` (row docs), `_view.py` (`NodeView.doc` if not already present),
  `__init__.py`, `docs/writing-nodes.md`, `docs/quirk-register.md`, `docs/from-hamilton.md`
- Test: `tests/test_parameterize_sugar.py`, `tests/differential/*`

**Interfaces:**

- Produces:
    - `parameterize` rows may be `(bindings, doc)`.
    - `group(*deps: Source | Value) -> Group`, and `group(**deps: Source | Value) -> Group`.
    - `parameterize_sources[F](**rows: Mapping[str, str]) -> Callable[[F], F]`.
    - `parameterize_values[F](parameter: str, assigned_output: Mapping[tuple[str, str | None], object]) ->
      Callable[[F], F]`.
- Doc templating: `str.format_map` with a mapping that raises a `ValueError` naming any unknown placeholder, when
  decorating.
- Register row Q82 goes in first.
- [ ] **Step 1: Write the failing tests:**
    - `test_tuple_rows_keep_their_doc_verbatim`
    - `test_function_doc_is_templated_per_row`
    - `test_unknown_placeholder_is_refused`
    - `test_group_list_and_dict`, plus its misuse refusals, each listed once
    - `test_parameterize_sources_and_values`, plus the non-`str` source and empty `assigned_output` refusals
    - differential KEEP: values, docs, and `group` results (H5.4a–d)
    - `deliberate` Q82
- [ ] **Step 2: Run** them. Expected: fail.
- [ ] **Step 3: Implement.** `group` lowers to a payload that assembles the list or dict from the core's keywords. Its
  sources become ordinary core inputs, typed by the element type.
- [ ] **Step 4: Run** `bash scripts/check.sh`. Expected: exit 0.
- [ ] **Step 5: Commit** `decorators: parameterize sugar`.

### Task 12: Threads, `rebuild`, and closing the ledger

**Files:**

- Modify: `driver.py`, `docs/running.md`, `docs/quirk-register.md`, the roadmap, `docs/from-hamilton.md`,
  `README.md`, `docs/index.md`, `CHANGELOG.md`, the design change log, and this plan's status
- Test: `tests/test_threads.py`, `tests/test_inspection.py`, `tests/differential/*`

**Interfaces:**

- Produces `Driver.rebuild() -> Driver`, and `AsyncDriver.rebuild() -> AsyncDriver`. Each builds from the stored
  builder, every setting included.
- Register row Q83 goes in first.
- [ ] **Step 1: Write the failing tests:**
    - `test_one_driver_across_threads`: 16 threads × 200 runs over a mixed graph with config, extract and a cache.
      Results are checked, and no shared state leaks. CI runs it on 3.14t.
    - `test_rebuild_sees_a_reloaded_module`
    - `test_rebuild_keeps_every_setting`
    - `deliberate` Q83 (H5.11b)
- [ ] **Step 2: Run** them. Expected: fail.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `bash scripts/check.sh` and `uv run python -m benches.run`. Expected: exit 0, and every
  existing steady row within noise.
- [ ] **Step 5: Close the ledger:**
    - **Rows fixed:** mark each wiring, types and execution row this plan covers as fixed, with its Q-number.
    - **Rows corrected:** correct the `Any` row (decision 12g), and the "numpy scalars" row to "fixed (opt-in
      widening); contracts deferred".
    - **Gap tables:** update them.
    - **`from-hamilton.md`:** update "Not there yet" and the tables.
    - **README and index:** update them.
    - **Changelog:** write the entries.
    - **Design change log:** add a row for the new public names.
    - **This plan:** tick it and set its status to done.
    - **Expected:** no wiring or types row says **same** or **worse**, except "Container checks are shallow", which
      says "same, deliberately" with the opt-in named, and the roadmap allows that.
- [ ] **Step 6: Commit** `threads and rebuild; close the inspection, wiring and types ledger rows`.

## Self-review

- **Spec coverage.** The roadmap's step 4 lists:
    - `inputs()`, `outputs()`, `modules` and `what_is_the_path_between` (Task 1)
    - `validate(...)` (Task 3)
    - functions in `final_vars` (Task 2)
    - `with_functions` (Task 7)
    - `with_strict_overrides()` and `with_declared_inputs(...)` (Task 4)
    - namespacing (Task 8)
    - `@exclude` (Task 6)
    - public matchers (Task 9)
    - the `@parameterize` row forms (Task 11)
- **Ledger rows by task:**
    - the typo row: Tasks 3–4
    - defaults: Task 5
    - helpers and `__all__`: Task 6
    - flat namespace: Task 8
    - no graph from functions: Task 7
    - `TYPE_CHECKING`: Task 10
    - `Any`: Task 9, corrected
    - `None`: Task 9
    - fixed tuples: Task 9
    - shallow containers: Task 10's opt-in
    - numpy: Task 10's opt-in
    - threads and reload: Task 12
    - unsupplied output: Task 1
    - `value()` drop: Task 5
- **Left out, with reasons:**
    - The unconsumed-node warning. Decision 6 explains it.
    - `ndim`, `dtype` and length contracts. Decision 12f explains it.
    - A data-only graph format with no Python functions. `fnloom.node` covers the programmatic case, and a
      serialised format waits for Plan 7's export.
- **Proportion.** This plan is twelve tasks, the largest so far, because the roadmap folds every wiring and types row
  into it. Tasks 6 and 9 carry the breaking changes. A reviewer who wants them in a separate release can move those
  two tasks to the end, unchanged.
