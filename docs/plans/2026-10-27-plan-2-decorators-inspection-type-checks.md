# fnloom Plan 2: parameterize, tags, graph inspection and type checks

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Status:** draft for maintainer review, 2026-10-27. Nothing below is built.

**Goal:** Enough of the v1 surface (design §5) for a real numeric library to move off Hamilton: `@parameterize` with
`source()` and `value()`, `@tag` with readable tags, a public typed read-only graph view, and type checks (edge
checks at build, always on; input and override checks at run, on by default, with a per-driver opt-out).

**Architecture:** All of it lives in the Python front end; the Rust core and the binding do not change. Decorators
only record metadata on the function and return it unchanged (typed as itself, Q1). `_scan` lowers a decorated
function to one `NodeDef` per generated node: core inputs are the distinct upstream names, and the payload is the
function itself or, when a row renames or binds parameters, a small callable that maps the core's keywords onto the
function's parameters. Annotations are resolved to values at build. `Driver.nodes` exposes a frozen `NodeView` per
node. Edge checks run once in `Builder.build`; input and override checks are compiled into each `PreparedPlan` and
run on each `run`.

**Tech Stack:** as Plan 1. Python >= 3.14 (`annotationlib`), ty strict, ruff ALL, pytest, `apache-hamilton==1.90.0`
in the test group as the oracle (`hamilton.function_modifiers`, `hamilton.htypes`).

**Spec:** `docs/design/2026-10-26-fnloom-design.md` (§4 front end owns type checks; §5 scope; §6 and
`docs/quirk-register.md` for Q1, Q2, Q10, Q22, Q28, Q30; §7 proof; §10 type-check matrix). Plan 1 is
`docs/plans/2026-10-26-plan-1-core-and-native-path.md`.

**Out of this plan, moved to Plan 3:** `config.when*` and `Builder.with_config`, `extract_columns`, `extract_fields`,
additive custom input checks (Q21, which needs the adapter API), docstring templating in `@parameterize` rows,
`Driver.validate_inputs`, `between` on the driver, and the async driver. Plan 1's "later plans" list moves these items
one plan later each.

## Global Constraints

- Every Global Constraint of Plan 1 applies unchanged (licence, naming, Python 3.14 and 3.14t, ty strict, ruff ALL,
  Clippy pedantic, clean history, one commit per task, `scripts/check.sh` green on every commit, noreply author with a
  `Co-Authored-By:` trailer).
- **Nothing consumer-specific** (design §8): no test, fixture, name or comment refers to any downstream project.
  Fixtures are invented for the behaviour they pin.
- **No Hamilton code copied** (design §8). Read `hamilton/function_modifiers/expanders.py`, `hamilton/htypes.py` and
  `hamilton/graph.py` in the installed 1.90.0 wheel to learn behaviour; write fnloom's independently.
- **No new runtime dependency.** numpy stays optional and is not imported by `python/fnloom`.
- **Same exception class as Hamilton for every kept behaviour**; every difference is a quirk-register row with a
  `deliberate("Q<n>")` differential test that asserts both sides. New rows continue from Q33.
- A decorator returns **the same object** it was given (`decorated is fn`), typed as the input type.
- Deterministic output: every listing sorted by node name; every error lists all culprits, sorted, in one message.
- Building and checking happen once: no per-`run` work that depends only on names (Q3).

## Review Focus

Inputs the design implies that no obvious test covers; each line has its test in the owning task.
1. A row's parameter that has a default, bound by `source()` to an input the caller does not supply: the function's
   own default applies (the keyword is omitted), never `None`. Task 2.
2. A `value()` holding a mutable object (a list): every call receives that same object, and a node that mutates it
   is visible on the next run, as in Hamilton. Task 2 pins identity against Hamilton.
3. A module written with `from __future__ import annotations` (string annotations, most real code): it builds, and
   its `NodeView.type` values equal those of the same module without the import. Task 3.
4. `None` supplied for a typed input passes the input check (Hamilton KEEP), and `True` supplied for an `int` input
   gets whatever Hamilton's check answers. Task 4 pins both against the oracle.
5. A decorated function imported into a second module that is also built: one node set, owned by the defining
   module, not a duplicate-name error. Task 2.

## File Structure

```
python/fnloom/
  __init__.py        + parameterize, source, value, tag, Source, Value, NodeView, Param
  _decorators.py     NEW  Source, Value, source, value, parameterize, tag; metadata kept on the function
  _scan.py           lowering: rows -> NodeDefs, bound-call payloads, resolved annotations, tags
  _view.py           NEW  Param, NodeView, building views from NodeDefs, input-type resolution
  _types.py          NEW  value_matches, type_matches (the §10 matrix)
  driver.py          Driver.nodes, edge checks in Builder.build, compiled input/override checks,
                     Builder.without_input_type_checks
tests/
  test_decorators.py  test_lowering.py  test_view.py  test_types.py  test_type_checks.py   NEW
  differential/fixtures/{rows_h,rows_f,tags_h,tags_f,typed,typed_future}.py               NEW
  differential/test_semantics.py, test_deliberate_differences.py                           extended
benches/generate.py, benches/run.py   + "rows" shape, input checks on/off
docs/writing-nodes.md, running.md, errors.md, from-hamilton.md, quirk-register.md, CHANGELOG.md
```

---

### Task 1: Decorators record metadata and keep the function's type

**Files:**
- Create: `python/fnloom/_decorators.py`, `tests/test_decorators.py`
- Modify: `python/fnloom/__init__.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True, slots=True) class Source: name: str` and `class Value: value: object`
  - `def source(name: str) -> Source`; `def value(v: object) -> Value`
  - `def parameterize[F: Callable[..., object]](**rows: Mapping[str, Source | Value]) -> Callable[[F], F]`
  - `def tag[F: Callable[..., object]](**tags: str) -> Callable[[F], F]`
  - `@dataclass(frozen=True) class Decorations: rows: Mapping[str, Mapping[str, Source | Value]] | None; tags: Mapping[str, str]`
  - `def decorations(fn: Callable[..., object]) -> Decorations` (empty `Decorations(None, {})` for a plain function)
- Metadata lives in one attribute, `fn.__fnloom__`, holding a `Decorations`; each decorator replaces it with an
  updated copy, so stacking order does not matter.
- Refusals at decoration time (the function is in hand): `parameterize()` with no rows is `ValueError`; a row value
  that is not `Source` or `Value` is `TypeError`; a row naming a parameter the function does not have is `ValueError`
  naming the row and parameter (Hamilton raises `InvalidDecoratorException` at build: register row **Q33**, recorded in
  Task 2); `parameterize` applied twice to one function is `ValueError`; a tag value that is not `str` is `TypeError`;
  the same tag key set twice on one function is `ValueError`.

- [ ] **Step 1: Write the failing tests** in `tests/test_decorators.py`:
  `test_parameterize_returns_the_same_function` (`parameterize(a={"x": value(1)})(f) is f`);
  `test_tag_returns_the_same_function`;
  `test_decorators_keep_the_signature_for_type_checkers` (module-level `assert_type(rowed(1), int)` where
  `rowed = parameterize(a={"x": value(1)})(f)` and `f(x: int) -> int`; ty must report no diagnostic);
  `test_rows_and_tags_are_recorded_in_either_order` (`@tag` over `@parameterize` and under it give equal `decorations(f)`);
  `test_plain_function_has_empty_decorations`;
  `test_empty_parameterize_is_a_value_error`; `test_row_value_of_wrong_type_is_a_type_error` (`{"x": 1}`);
  `test_row_naming_an_unknown_parameter_is_a_value_error` (message contains the row and parameter names);
  `test_parameterize_twice_is_a_value_error`; `test_non_str_tag_is_a_type_error`;
  `test_repeated_tag_key_is_a_value_error`; `test_public_names_are_exported` (`fnloom.parameterize`, `source`,
  `value`, `tag`, `Source`, `Value` exist).
- [ ] **Step 2: Run** `uv run pytest tests/test_decorators.py -v`. Expected: FAIL, `ImportError`.
- [ ] **Step 3: Implement** `_decorators.py` with the signatures above and add the six names to `__init__.__all__`.
- [ ] **Step 4: Run** `bash scripts/check.sh`. Expected: exit 0, including ty on `tests/test_decorators.py`.
- [ ] **Step 5: Commit** `python: parameterize, source, value and tag decorators`.

### Task 2: Lowering `@parameterize` rows into nodes

**Files:**
- Create: `python/fnloom/_view.py` (only `Param` in this task), `tests/test_lowering.py`,
  `tests/differential/fixtures/rows_h.py`, `tests/differential/fixtures/rows_f.py`
- Modify: `python/fnloom/_scan.py`, `python/fnloom/driver.py` (`_graph_of`), `tests/differential/test_semantics.py`,
  `tests/differential/test_deliberate_differences.py`, `docs/quirk-register.md`, `docs/writing-nodes.md`,
  `docs/from-hamilton.md`

**Interfaces:**
- Consumes: Task 1's `decorations`, `Source`, `Value`.
- Produces:
  - `@dataclass(frozen=True) class Param: name: str; binding: Source | Value; required: bool; annotation: object`
    (`name` is the function's parameter; a plain parameter is `binding=Source(name)`; `required` is False when the
    parameter has a default; a `Value`-bound parameter has `required=True`)
  - `NodeDef` becomes `(name: str, params: tuple[Param, ...], tags: Mapping[str, str], payload: Callable[..., object],
    origin: Callable[..., object], module: str, returns: object)`; `origin` is the decorated function, `payload`
    what the core calls.
  - `def core_inputs(params: tuple[Param, ...]) -> list[tuple[str, bool]]`: distinct `Source` names in first-appearance
    order; one is required if any parameter bound to it is required. `_graph_of` passes these and `payload`.
- Lowering rules: a function with rows yields one node per row, named by the row key, and no node of its own name
  (unless a row has that name); parameters a row does not mention stay `Source(<parameter>)`; the payload is
  `origin` itself when every binding is `Source(<own name>)`, else a bound call that builds the function's keywords
  from the core's (a parameter whose source was not supplied is omitted so its default applies) plus the `Value`s,
  passing each `Value` object itself, uncopied. A private (underscore) or imported decorated function yields no nodes,
  exactly as for plain functions.

- [ ] **Step 1: Write failing unit tests** in `tests/test_lowering.py`:
  `test_each_row_is_a_node_and_the_function_is_not`; `test_source_renames_an_edge`;
  `test_value_binds_a_constant_and_is_not_an_edge`; `test_unmentioned_parameter_stays_an_edge_of_its_own_name`;
  `test_two_parameters_from_one_source_get_one_core_input_and_the_same_value`;
  `test_defaulted_parameter_bound_by_source_is_optional_and_omitted_when_unsupplied` (Review Focus 1: result equals
  the default, the function never receives `None`);
  `test_row_can_source_another_row` (three-row chain `a1 -> a2 -> a3`);
  `test_undecorated_function_payload_is_the_function_itself` (`payload is origin`);
  `test_row_named_like_the_function_is_allowed`;
  `test_row_name_clashing_with_another_node_is_a_value_error_naming_both_modules`;
  `test_private_decorated_function_yields_no_nodes`;
  `test_decorated_function_imported_elsewhere_is_one_node_set` (Review Focus 5: build both modules, no error, nodes
  once); `test_node_exception_from_a_row_carries_the_row_name_note`.
- [ ] **Step 2: Write failing differential tests.** `rows_h.py` and `rows_f.py` hold the same functions; only the
  decorator import differs (`hamilton.function_modifiers` vs `fnloom`). Cover: a two-row reducer over one input, a
  row sourcing another row, a defaulted parameter bound by `source()`, two parameters from one source, a `value()`
  holding a list. In `test_semantics.py`: `test_rows_compute_the_same_values` (every row node requested, with and
  without the optional input); `test_rows_list_the_same_nodes` (`nodes_hamilton([rows_h]) == nodes_fnloom([rows_f])`);
  `test_value_object_is_passed_by_identity` (Review Focus 2: the node returns `id()` of its bound list, or appends
  to it; both runtimes behave alike across two runs). In `test_deliberate_differences.py`:
  `test_q33_row_naming_an_unknown_parameter` (Hamilton raises `InvalidDecoratorException`, an `Exception` subclass in
  `hamilton/function_modifiers/base.py`, at `build()`; fnloom raises `ValueError` at decoration). Add row Q33 to
  `docs/quirk-register.md` in this step: the `deliberate` marker fails collection for a row that does not exist.
- [ ] **Step 3: Run** `uv run pytest tests/test_lowering.py tests/differential -v`. Expected: the new tests FAIL.
- [ ] **Step 4: Implement** the lowering in `_scan.py`, `Param` in `_view.py`, `core_inputs`, and `_graph_of`.
  Keep annotations as `_scan` reads them today (`Format.FORWARDREF`); Task 3 resolves them.
- [ ] **Step 5: Run** `bash scripts/check.sh`. Expected: exit 0. A differential test where the two runtimes disagree
  is settled by the register, never by editing an expected value: fix fnloom, or add a row and move the case to
  `test_deliberate_differences.py`.
- [ ] **Step 6: Docs.** A "One function, several nodes" section in
  `docs/writing-nodes.md`; the decorator mapping to `docs/from-hamilton.md`.
- [ ] **Step 7: Commit** `python: lower parameterize rows into nodes`.

### Task 3: Resolved annotations, readable tags and the graph view

**Files:**
- Modify: `python/fnloom/_view.py`, `python/fnloom/_scan.py`, `python/fnloom/driver.py`, `python/fnloom/__init__.py`,
  `docs/quirk-register.md` (Q2, Q28, new Q34), `docs/running.md`
- Create: `tests/test_view.py`, `tests/differential/fixtures/{tags_h,tags_f,typed,typed_future}.py`

**Interfaces:**
- Consumes: Task 2's `NodeDef`, `Param`.
- Produces:
  - `@dataclass(frozen=True) class NodeView`: `name: str`; `type: object` (a function node's resolved return
    annotation; an input's resolved type, below); `is_input: bool`; `origin: Callable[..., object] | None` (`None`
    for an input); `module: str | None`; `params: tuple[Param, ...]` (declaration order; `()` for an input);
    `tags: Mapping[str, str]` (read-only); `doc: str | None` (the origin's docstring); and the property
    `dependencies -> tuple[str, ...]` (the distinct `Source` names, sorted).
  - `Driver.nodes -> Mapping[str, NodeView]`: a read-only mapping, iterating in sorted name order, built once at
    `Builder.build`.
- Annotations are resolved at build with `annotationlib.get_annotations(fn, format=Format.VALUE, eval_str=True)`,
  so string annotations (`from __future__ import annotations`) and forward references to names defined anywhere in
  the module resolve. A name that does not resolve (for example one imported only under `TYPE_CHECKING`) raises the
  `NameError` itself with a note naming the node and parameter, as Hamilton's `get_type_hints` would.
- An input's type is its consumers' annotation: one type if all agree; when they differ and one matches the other by
  `type_matches` (Task 4) the narrower; otherwise `ValueError` at build naming the input and each consumer. Until
  Task 4 lands, this task implements "all agree, else `ValueError`" and Task 4 widens it.
- Q34: Hamilton adds a `module` tag to every node; fnloom's tags are only what the author wrote, and the module is
  `NodeView.module`.
- `NodeView` and `Param` are exported from `fnloom`. Q2 is FIXED by `Driver.nodes`, except that the node's own
  callable is not exposed: a prepared plan replaces the replay it was used for (update the Q2 row's fnloom column).

- [ ] **Step 1: Write failing tests** in `tests/test_view.py`:
  `test_function_node_view_fields` (type, origin, module, params with bindings, doc, `is_input is False`);
  `test_input_node_view_fields` (`origin is None`, `params == ()`, type from its consumer);
  `test_dependencies_are_distinct_sorted_sources`; `test_value_bound_param_is_not_a_dependency`;
  `test_tags_are_readable_on_every_row_of_a_tagged_parameterized_function`;
  `test_nodes_mapping_is_read_only_and_sorted`;
  `test_future_annotations_resolve_like_plain_ones` (Review Focus 3: `typed_future` and `typed` give equal `type`
  and `Param.annotation` values node by node);
  `test_forward_reference_to_a_later_class_resolves`;
  `test_unresolvable_annotation_raises_name_error_with_a_note_naming_the_node`;
  `test_input_with_conflicting_consumer_types_is_a_value_error_naming_both_consumers`.
- [ ] **Step 2: Write failing differential tests:** `test_tags_match_hamilton_except_module` (`@deliberate("Q34")`:
  per node, Hamilton's tags equal `{**view.tags, "module": view.module}`; add row Q34 to the register in this step);
  `test_node_types_match_hamilton`
  (per node of `typed`, Hamilton's node `type` equals `view.type`); `test_unresolvable_annotation_same_class`.
  Pin Plan 1's `test_scan.py::test_forward_reference_annotation_is_accepted` unchanged: it must stay green.
- [ ] **Step 3: Run** `uv run pytest tests/test_view.py tests/differential -v`. Expected: the new tests FAIL.
- [ ] **Step 4: Implement** resolution in `_scan.py`, `NodeView` and input-type resolution in `_view.py`, and
  `Driver.nodes` (the `Driver` constructor gains the views; `Builder.build` passes them).
- [ ] **Step 5: Run** `bash scripts/check.sh`. Expected: exit 0.
- [ ] **Step 6: Docs.** Update the Q2 and Q28 rows; an "Inspecting a graph" section in `docs/running.md`.
- [ ] **Step 7: Commit** `python: resolved annotations, tags and the graph view`.

### Task 4: The type-matching functions, proven against Hamilton's

**Files:**
- Create: `python/fnloom/_types.py`, `tests/test_types.py`
- Modify: `tests/differential/test_semantics.py`, `tests/differential/test_deliberate_differences.py`,
  `docs/quirk-register.md`, `python/fnloom/_view.py` (input-type narrowing from Task 3)

**Interfaces:**
- Produces:
  - `def value_matches(declared: object, value: object) -> bool`: whether a supplied input or override value fits
    the declared type. `None` fits every declaration (Hamilton KEEP).
  - `def type_matches(produced: object, required: object) -> bool`: whether a node returning `produced` may feed a
    parameter annotated `required`.
- The matrix (design §10). Types: `int`, `float`, `bool`, `str`, `object`, `Any`, a user class `Base` and its
  subclass `Child`, `list`, `list[int]`, `dict[str, int]`, `tuple[int, ...]`, `collections.abc.Sequence`,
  `Sequence[int]`, `int | None`, `Optional[int]`, `int | str`, `Literal[1, 2]`, `Annotated[int, "m"]`, a plain
  `Protocol`, a `runtime_checkable` `Protocol`, a `TypeVar`, a `NewType` over `int`. Values: `1`, `1.0`, `True`, `"s"`,
  `None`, `[1]`, `[1.0]`, `{"a": 1}`, `(1, 2)`, `object()`, a `Base()`, a `Child()`.
- The oracle is `hamilton.htypes`: `check_input_type(node_type, input_value)` for `value_matches`, and
  `types_match(param_type, required_node_type)` for `type_matches`. Read the latter's body before mapping
  `produced`/`required` onto its two arguments; the parameter names alone are ambiguous.

- [ ] **Step 1: Write failing tests.** `tests/test_types.py`: `test_none_fits_every_declaration`;
  `test_subclass_fits_its_base_and_not_the_reverse`; `test_any_fits_everything`;
  `test_bool_for_int_answers_as_the_oracle` and `test_int_for_float_answers_as_the_oracle` (Review Focus 4; the
  expected value is the one Step 2's oracle records, written here as a literal). In `test_semantics.py`, two
  parametrized tests over the whole matrix: `test_value_matches_agrees_with_hamilton[declared-value]` and
  `test_type_matches_agrees_with_hamilton[produced-required]`, excluding only the pairs listed in a module-level
  `DELIBERATE: dict[tuple[str, str], str]` mapping a pair's ids to its register row.
- [ ] **Step 2: Run** `uv run pytest tests/test_types.py tests/differential -v`. Expected: FAIL, `ImportError`.
- [ ] **Step 3: Implement** `_types.py`. Where Hamilton's answer is wrong for any user (a `Literal` or `Annotated`
  form Hamilton mishandles, for example), fnloom differs on purpose: add the pair to `DELIBERATE`, a register row
  (Q35 onward) and a `deliberate` test asserting both answers. Where it is merely surprising, KEEP it.
- [ ] **Step 4: Widen Task 3's input-type rule** to "the narrower by `type_matches`" and add
  `test_view.py::test_input_takes_the_narrower_of_two_compatible_consumer_types` (`Base` and `Child` consumers give
  `Child`), with a differential twin against Hamilton's input node type.
- [ ] **Step 5: Run** `bash scripts/check.sh`. Expected: exit 0.
- [ ] **Step 6: Commit** `python: type matching, proven against Hamilton's matrix`.

### Task 5: Edge checks at build; input and override checks at run; the opt-out

**Files:**
- Modify: `python/fnloom/driver.py`, `docs/running.md`, `docs/errors.md`, `docs/quirk-register.md` (Q10, Q22)
- Create: `tests/test_type_checks.py`
- Modify: `tests/differential/test_semantics.py`, `tests/differential/test_deliberate_differences.py`

**Interfaces:**
- Consumes: Task 3's `NodeView`, Task 4's `value_matches`, `type_matches`.
- Produces:
  - Edge checks in `Builder.build`, always on: for every `Source` parameter whose upstream is a function node,
    `type_matches(upstream.type, param.annotation)`; all failures in one `ValueError`, each line
    `"<node>.<param> requires <required> but <upstream> returns <produced>"`, sorted.
  - `Builder.without_input_type_checks() -> Builder` (a new builder, like `with_modules`); `Driver` carries the flag.
  - `PreparedPlan` compiles, at prepare time, the checks its runs need: each supplied input the plan reads (an
    `UseInput` step in `_core.Plan.steps()`) against that input's `NodeView.type`, and each override against the
    overridden node's `NodeView.type` (Q22). A step's action string for a read input is `"use_input"`. `run` applies them, unless checks are off, and raises one `ValueError`
    listing every failure, sorted, each `"<name>: expected <declared>, got <type of value>"`. A supplied input that
    no step reads is not checked (as Hamilton).

- [ ] **Step 1: Write failing tests** in `tests/test_type_checks.py`:
  `test_mismatched_edge_fails_build_naming_node_param_and_upstream`;
  `test_all_edge_mismatches_are_listed_together_sorted`;
  `test_edge_check_runs_even_with_input_checks_off`;
  `test_wrong_input_type_is_a_value_error_listing_every_bad_input`;
  `test_none_input_passes`; `test_unread_input_is_not_checked`;
  `test_override_of_the_wrong_type_is_a_value_error` (Q22);
  `test_without_input_type_checks_turns_off_input_and_override_checks`;
  `test_without_input_type_checks_returns_a_new_builder`;
  `test_checks_are_compiled_once_per_plan` (spy on `value_matches` through a module attribute: one call per checked
  value per run, zero work per run on names).
- [ ] **Step 2: Write failing differential tests:** `test_edge_mismatch_same_exception_class`;
  `test_wrong_input_type_same_exception_class`; `test_q22_override_type_checked` (`@deliberate("Q22")`: Hamilton
  runs the node with the wrong override value, fnloom refuses before running);
  `test_q10_opt_out_is_explicit` (`@deliberate("Q10")`: Hamilton's `NoEdgeAndInputTypeChecking` also turns off edge
  checks; fnloom's opt-out leaves them on).
- [ ] **Step 3: Run** `uv run pytest tests/test_type_checks.py tests/differential -v`. Expected: the new tests FAIL.
- [ ] **Step 4: Implement** in `driver.py`.
- [ ] **Step 5: Run** `bash scripts/check.sh`. Expected: exit 0.
- [ ] **Step 6: Docs.** "Type checks" in `docs/running.md`; each new error in `docs/errors.md`; Q10 and Q22 rows say
  which tests pin them.
- [ ] **Step 7: Commit** `python: edge, input and override type checks`.

### Task 6: Benchmarks and release notes for the new surface

**Files:**
- Modify: `benches/generate.py`, `benches/run.py`, `tests/test_bench_generate.py`, `docs/benchmarks.md`, `CHANGELOG.md`

**Interfaces:**
- Produces: `make_module(shape: Literal["chain", "fan_in", "layered", "rows"], n: int)`, where `"rows"` is one
  function with `n` `@parameterize` rows, each sourcing the previous row (the module is generated twice, in
  Hamilton's and fnloom's spelling, so `make_module` gains `spelling: Literal["hamilton", "fnloom"] = "fnloom"`).
  `benches/run.py` rows gain `"input_checks": bool`; fnloom is measured with checks on and off, Hamilton as it ships
  (checks on).

- [ ] **Step 1: Write a failing test** `test_bench_generate.py::test_rows_shape_has_n_nodes_in_both_spellings`.
- [ ] **Step 2: Run** `uv run pytest tests/test_bench_generate.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the shape and the column.
- [ ] **Step 4: Run** `uv run python -m benches.run > bench.jsonl`; update the table in `docs/benchmarks.md` with the
  machine description. Name every row where fnloom with checks ON is slower than Hamilton: the design calls each a
  bug, so open an issue per row and link it from the table.
- [ ] **Step 5: Changelog.** Under `[Unreleased]`: Added (decorators, `Driver.nodes`, `NodeView`, `Param`, type checks,
  `Builder.without_input_type_checks`); Changed (annotations resolved at build, so an unresolvable one is now a
  `NameError`; undecorated functions with string annotations unchanged).
- [ ] **Step 6: Run** `bash scripts/check.sh`. Expected: exit 0.
- [ ] **Step 7: Commit** `bench: rows shape and input-check cost`.

---

## Self-review

- **Design coverage (§5 rows in scope):** parameterize, source and value (Tasks 1-2); tags readable through inspection
  (1, 3); graph inspection: list nodes, upstream and downstream already exist, plus typed views (3); edge checks at
  build, input checks with an opt-out (4-5); Q1 (1), Q2 and Q28 (3), Q10 and Q22 (5), §10 matrix (4), §3 benchmarks
  (6). Deliberately not here: config, extract, Q21, async, DOT (see "Out of this plan").
- **Names used across tasks:** `Source`, `Value`, `Decorations`, `decorations` (1); `Param`, `NodeDef.payload`,
  `NodeDef.origin`, `core_inputs` (2); `NodeView`, `Driver.nodes` (3); `value_matches`, `type_matches` (4);
  `Builder.without_input_type_checks` (5). Each is defined once, in the task named.
- **Left to execution, on purpose:** Hamilton's exception class for an unresolvable annotation, which side of
  `htypes.types_match` is the producer, and the oracle's answers for `bool`/`int` and `int`/`float`. Each is read
  or measured in the task that needs it, and recorded in the test as a literal.
