# fnloom Plan 1: Rust core and native Python path

**Status:** done. Every task below was built, tested and merged to `main` on 2026-10-27. The plan is kept as written,
as the record of what was decided, apart from the updates noted where they apply.

**Goal:** A Rust graph core (build, validate, plan, execute) behind a PyO3 binding, a minimal native Python front end that turns plain functions into nodes, a differential harness that runs the same programs under Hamilton 1.90.0 and fnloom, and a benchmark harness.

**Architecture:** `fnloom-core` is a dependency-free Rust crate generic over a node payload `P`; it builds a `Graph<P>`, turns a request (outputs, supplied input names, override names) into a reusable `Plan`, and runs a plan through an `Evaluate` trait and `Observer` seams. `fnloom-py` instantiates `P` and the value type as Python objects. `python/fnloom` scans modules into node definitions and exposes `Builder().with_modules(...).build() -> Driver`, with `fnloom.build(*modules)` as a shortcut. Everything is iterative (no recursion), deterministic (sorted by name), and compiled once per request.

**Tech Stack:** Rust (edition 2024), PyO3 (per-interpreter builds, no `abi3`), maturin, Python >= 3.14 (default and free-threaded `3.14t`), uv, pytest, ruff, ty (strict), Clippy (pedantic), `apache-hamilton==1.90.0` (test group only).

**Spec:** `docs/design/2026-10-26-fnloom-design.md`; deviations from Hamilton: `docs/quirk-register.md`.

**Later plans (not in this one):** 2 decorators and config (`parameterize`, `source`, `value`, `tag`, `config.when*`, `extract_*`) plus edge/input type checking; 3 adapters and hooks (full API), result builders, async execution; 4 DOT output and visualisation; 5+ phased coverage (dynamic execution, caching, materializers, `@subdag`, `@check_output`, `@pipe_*`); there is no Hamilton compat layer.

## Global Constraints

- Licence Apache-2.0; no file copied from Hamilton; no module, package or crate name containing "hamilton" or "apache" (test-group dependency `apache-hamilton` excepted).
- Python floor 3.14; CI tests both `3.14` and `3.14t`; no `abi3` (it cannot target free-threaded CPython); Rust edition 2024 with `rust-version` set to the oldest toolchain CI verifies.
- Nodes execute in topological order with ties broken by node name, independent of the order of requested outputs (register Q26).
- Authoring API: name-wiring (design §9, decided 2026-10-26).
- `fnloom-core` has zero dependencies and `#![forbid(unsafe_code)]`.
- A Rust panic never crosses the FFI boundary as anything but a Python exception.
- **Clean code (maintainer, 2026-10-26), enforced by tools, every one blocking in `scripts/check.sh`:**
  - **ty, strict:** `[tool.ty.rules] all = "error"`, `[tool.ty.analysis] respect-type-ignore-comments = false`,
    `python-version = "3.14"`, over `python/`, `tests/`, `benches/` and `scripts/`. A suppression is
    `# ty: ignore[<rule>]` with its reason on the same line. Pin `ty` in the dev group (0.0.89 or newer).
  - **ruff:** `select = ["ALL"]`; every entry in `ignore` carries a comment saying why (the formatter-conflicting
    rules `COM812`/`ISC001`, and `D203`/`D213` in favour of `D211`/`D212`, are the expected starting set). `ruff
    format` is the formatter. A `noqa` must name its rule (`PGH004`) and give a reason.
  - **Clippy, strict:** `[workspace.lints.clippy]` with `all` and `pedantic` at `deny`, plus `unwrap_used`,
    `expect_used`, `panic`, `todo`, `unimplemented`, `dbg_macro`, `print_stdout` and `allow_attributes_without_reason`
    at `deny` (test code may `#[expect(..., reason = "...")]` the panic lints). `[workspace.lints.rust]`:
    `missing_docs = "deny"`, `unsafe_code = "forbid"` in `fnloom-core` (the binding may need PyO3's `unsafe` and
    states why), `unused = "deny"`. Each crate has `[lints] workspace = true`. `cargo clippy --workspace
    --all-targets -- -D warnings` and `RUSTDOCFLAGS="-D warnings" cargo doc --no-deps` both pass.
  - **No dead code, no commented-out code, no TODO without a linked issue** (ruff `ERA`, `FIX`, `TD`; Rust
    `dead_code` under `unused`).
  - **Every public Python and Rust item has a docstring or doc comment** saying what it does (ruff `D`,
    `missing_docs`). Comments explain why, not what.
  - **Small, single-purpose functions** (Clippy `too_many_lines` at its default, ruff `C901`/`PLR0912`/`PLR0915`).
    Exceeding them is a split, not a suppression.
- All output ordering is deterministic and sorted by node name.
- No recursion over graph depth anywhere (Hamilton's depth-first walk is recursive).
- Exceptions: unknown names, missing required inputs, cycles, duplicate node names raise `ValueError`; a bare `str` for `final_vars` raises `TypeError`.
- The Hamilton oracle is pinned at `apache-hamilton==1.90.0`.
- **Clean history (maintainer, 2026-10-26).** One logical commit per task, linear on `main`: no WIP, fixup, "address
  review" or revert-then-redo commits. If a task needs a correction before it is pushed, amend or squash it; never
  push a broken intermediate state. Each commit builds and passes `scripts/check.sh` on its own. Messages are
  `area: imperative summary` with a short body saying why. No force-push once the repo is public.
- Commit author is the repo's noreply email; every commit ends with a `Co-Authored-By:` trailer naming the model that wrote it.

## Review Focus

Inputs and conditions the design implies but no obvious test covers; each has a test in the owning task:
1. A 10,000-node chain executes (Hamilton raises `RecursionError`; fnloom must not). Task 5, Task 7.
2. `None` supplied as an input or override value counts as supplied, not missing. Task 4, Task 5.
3. A node function that raises: the original exception class reaches the caller, the node name is attached, and observers still see `post_graph(false)`. Task 5, Task 6.
4. The same function name defined in two modules: `ValueError` naming both modules. Task 7.
5. `execute([])` returns `{}`; building from zero modules gives an empty graph; supplying an input nothing requested needs is valid. Task 4, Task 7.

## File Structure

```
Cargo.toml                         workspace: crates/fnloom-core, crates/fnloom-py
pyproject.toml                     maturin build, uv dev group, ruff and ty config
scripts/check.sh                   the single local gate (fmt, clippy, cargo doc, cargo test, ruff, ty, pytest)
.github/workflows/ci.yml           runs scripts/check.sh pieces
crates/fnloom-core/src/
  lib.rs                           module wiring, re-exports
  node.rs                          NodeId, Dependency, NodeSpec, Node, NodeKind
  graph.rs                         Graph::build, lookup, queries
  plan.rs                          Request, Plan, Step, Action, Graph::prepare
  run.rs                           Evaluate, Observer, RunError, Plan::run
  error.rs                         BuildError, PlanError
crates/fnloom-py/src/lib.rs        PyO3 module fnloom._core
python/fnloom/
  __init__.py  _scan.py  driver.py  py.typed
tests/
  test_smoke.py  test_scan.py  test_driver.py
  differential/  conftest.py  fixtures/*.py  test_semantics.py  test_deliberate_differences.py
benches/generate.py  benches/run.py
```

---

### Task 1: Workspace, tooling and the smoke test

**Files:**
- Modify: `Cargo.toml` (add `crates/fnloom-py`, `rust-version`)
- Create: `pyproject.toml`, `crates/fnloom-py/Cargo.toml`, `crates/fnloom-py/src/lib.rs`, `python/fnloom/__init__.py`, `python/fnloom/py.typed`, `tests/test_smoke.py`, `scripts/check.sh`, `.github/workflows/ci.yml`

**Interfaces:**
- Produces: importable `fnloom` package; `fnloom._core.core_version() -> str`; `scripts/check.sh` exits non-zero on any failure.

- [x] **Step 1: Write the failing test** `tests/test_smoke.py::test_core_version_is_a_string`: `import fnloom._core as c; assert isinstance(c.core_version(), str) and c.core_version()`.
- [x] **Step 2: Run** `uv run pytest tests/test_smoke.py -v`. Expected: FAIL, `ModuleNotFoundError: fnloom`.
- [x] **Step 3: Scaffold.** Set the workspace `edition = "2024"` in `Cargo.toml`. `pyproject.toml` with `[build-system] maturin`, `[tool.maturin] manifest-path = "crates/fnloom-py/Cargo.toml"`, `python-source = "python"`, `module-name = "fnloom._core"`, `requires-python = ">=3.14"`, a `dev` dependency group (`pytest`, `ruff`, `ty>=0.0.89`, `maturin`, `apache-hamilton==1.90.0`), and the ruff and ty configuration from Global Constraints. `Cargo.toml` gets the `[workspace.lints]` tables from Global Constraints, and each crate `[lints] workspace = true`. `fnloom-py` is a `cdylib` named `_core` with `pyo3` (feature `extension-module`) and a `#[pyfunction] core_version() -> &'static str` returning `env!("CARGO_PKG_VERSION")`. `python/fnloom/__init__.py` re-exports nothing yet.
- [x] **Step 4: Write `scripts/check.sh`** (`set -euo pipefail`): `cargo fmt --check`, `cargo clippy --workspace --all-targets -- -D warnings`, `RUSTDOCFLAGS="-D warnings" cargo doc --workspace --no-deps`, `cargo test --workspace`, `uv run maturin develop`, `uv run ruff check`, `uv run ruff format --check`, `uv run ty check`, `uv run pytest`. Run `rustup component add clippy rustfmt` first if missing. `.github/workflows/ci.yml` runs the same steps (the repo's Actions availability is unverified; the script is the gate either way).
- [x] **Step 5: Run** `uv sync && uv run maturin develop && uv run pytest tests/test_smoke.py -v`. Expected: PASS. Then `bash scripts/check.sh`: expected exit 0.
- [x] **Step 6: Commit** `build: workspace, maturin packaging, check script`.

### Task 2: Core IR and `Graph::build`

**Files:**
- Create: `crates/fnloom-core/src/{node.rs,graph.rs,error.rs}`; Modify: `lib.rs` (add `#![forbid(unsafe_code)]`, `pub mod`s)

**Interfaces:**
- Produces:
  - `pub struct NodeId(pub u32)` (`Copy, Eq, Ord, Hash, Debug`)
  - `pub enum Dependency { Required, Optional }`
  - `pub struct InputSpec { pub name: String, pub dependency: Dependency }`
  - `pub struct NodeSpec<P> { pub name: String, pub inputs: Vec<InputSpec>, pub tags: BTreeMap<String, String>, pub payload: P }`
  - `pub enum NodeKind { Function, Input }`
  - `pub struct Edge { pub node: NodeId, pub dependency: Dependency }`
  - `pub struct Node<P> { pub id: NodeId, pub name: String, pub kind: NodeKind, pub inputs: Vec<Edge>, pub tags: BTreeMap<String, String>, pub payload: Option<P> }` (`None` for `Input`)
  - `pub enum BuildError { DuplicateName { name: String }, Cycle { path: Vec<String> } }` (`Display`, `Error`)
  - `impl<P> Graph<P> { pub fn build(specs: Vec<NodeSpec<P>>) -> Result<Graph<P>, BuildError>; pub fn nodes(&self) -> &[Node<P>]; pub fn node(&self, id: NodeId) -> &Node<P>; pub fn id_of(&self, name: &str) -> Option<NodeId> }`

- [x] **Step 1: Write failing tests** in `graph.rs`: `build_synthesises_input_nodes_for_unresolved_parameters` (spec `b(a)`, graph has `a` as `NodeKind::Input` with `payload.is_none()`); `build_rejects_duplicate_names` (two specs named `x` give `DuplicateName{name:"x"}`); `build_reports_a_cycle_with_its_path` (`a(b)`, `b(a)` gives `Cycle` whose path contains `a` and `b`); `build_of_zero_specs_is_an_empty_graph`; `node_ids_follow_sorted_name_order`.
- [x] **Step 2: Run** `cargo test -p fnloom-core`. Expected: FAIL (items undefined).
- [x] **Step 3: Implement** the types and `build`. Assign ids in sorted-name order; synthesise `Input` nodes for any dependency name that is not a spec; detect cycles with an iterative three-colour DFS (no recursion).
- [x] **Step 4: Run** `cargo test -p fnloom-core`. Expected: PASS.
- [x] **Step 5: Commit** `core: node IR and Graph::build`.

### Task 3: Graph queries

**Files:** Modify `crates/fnloom-core/src/graph.rs`

**Interfaces:**
- Consumes: Task 2's `Graph`.
- Produces: on `Graph<P>`: `pub fn upstream_of(&self, names: &[&str]) -> Result<Vec<NodeId>, PlanError>`, `pub fn downstream_of(&self, names: &[&str]) -> Result<Vec<NodeId>, PlanError>`, `pub fn between(&self, from: &str, to: &str) -> Result<Vec<NodeId>, PlanError>`. All results sorted by `NodeId`, excluding the starting nodes themselves. `PlanError::UnknownNodes(Vec<String>)` is introduced in `error.rs` here (more variants in Task 4).

- [x] **Step 1: Write failing tests** on a diamond `a -> b, a -> c, b -> d, c -> d`: `upstream_of(["d"])` is `[a,b,c]`; `downstream_of(["a"])` is `[b,c,d]`; `between("a","d")` is `[b,c]`; `upstream_of(["nope"])` is `UnknownNodes(["nope"])`.
- [x] **Step 2: Run** `cargo test -p fnloom-core queries`. Expected: FAIL.
- [x] **Step 3: Implement** with iterative worklists over precomputed forward/backward adjacency stored in `Graph`.
- [x] **Step 4: Run** `cargo test -p fnloom-core`. Expected: PASS.
- [x] **Step 5: Commit** `core: upstream, downstream and between queries`.

### Task 4: `Request`, `Plan` and `Graph::prepare`

**Files:** Create `crates/fnloom-core/src/plan.rs`; Modify `error.rs`, `lib.rs`

**Interfaces:**
- Consumes: Task 2 and 3 types.
- Produces:
  - `pub struct Request { pub outputs: Vec<String>, pub inputs: BTreeSet<String>, pub overrides: BTreeSet<String> }`
  - `pub enum Action { UseOverride, UseInput, SkipOptionalInput, Compute }`
  - `pub struct Step { pub node: NodeId, pub action: Action, pub args: Vec<NodeId>, pub release: Vec<NodeId> }` (`args` = the edges actually passed, in sorted-name order; `release` = nodes whose value is dead after this step and not a requested output)
  - `pub struct Plan { /* private */ }` with `pub fn steps(&self) -> &[Step]`, `pub fn outputs(&self) -> &[NodeId]`
  - `PlanError::{UnknownNodes(Vec<String>), UnknownOutputs(Vec<String>), UnknownInputs(Vec<String>), UnknownOverrides(Vec<String>), MissingInputs(Vec<String>)}` (all name lists sorted)
  - `impl<P> Graph<P> { pub fn prepare(&self, request: &Request) -> Result<Plan, PlanError> }`
- Semantics (quirk register Q3, Q4, Q5, Q7): `UnknownInputs` for any `inputs` name that is not an `Input` node; `UnknownOverrides` for any override name that is not a node; an override replaces its node and cuts its upstream; an override satisfies a missing input and beats a supplied one; a `Required` edge to an unsupplied, non-overridden input is `MissingInputs`; an `Optional` edge to an unsupplied input becomes `SkipOptionalInput` and is omitted from the consumer's `args`; an output that is itself an unsupplied input is `MissingInputs`.

- [x] **Step 1: Write failing tests** in `plan.rs`, each asserting the exact variant and sorted names: `unknown_output_is_refused`; `unknown_input_name_is_refused` (Q4); `unknown_override_name_is_refused` (Q5); `missing_required_input_lists_all_missing_sorted`; `override_cuts_its_upstream` (an input only upstream of an overridden node is not required); `override_satisfies_a_missing_input_and_beats_a_supplied_one` (Q7); `optional_input_not_supplied_is_skipped_and_omitted_from_args`; `empty_outputs_give_an_empty_plan`; `supplying_an_input_nothing_needs_is_valid`; `release_lists_intermediates_after_last_use_and_never_outputs`; `execution_order_is_independent_of_output_order` (outputs `[d, b]` and `[b, d]` give identical `steps()`).
- [x] **Step 2: Run** `cargo test -p fnloom-core plan`. Expected: FAIL.
- [x] **Step 3: Implement** `prepare`: iterative reverse traversal from outputs honouring overrides; Kahn topological order with ties broken by `NodeId` (ids are assigned in name order, so this is the name tie-break of Q26); compute `release` from last-use positions.
- [x] **Step 4: Run** `cargo test -p fnloom-core`. Expected: PASS.
- [x] **Step 5: Commit** `core: Request, Plan and Graph::prepare`.

### Task 5: Execution with `Evaluate` and `Observer`

**Files:** Create `crates/fnloom-core/src/run.rs`; Modify `lib.rs`

**Interfaces:**
- Consumes: Task 4's `Plan`, `Step`, `Action`.
- Produces:
  - `pub trait Evaluate<P> { type Value: Clone; type Error; fn evaluate(&mut self, node: &Node<P>, args: &[Arg<'_, Self::Value>]) -> Result<Self::Value, Self::Error>; }`, where `Arg { node, name, value }` (updated after review: the binding needed each argument's `NodeId` to look up names interned at build; the plan first had `(&str, Self::Value)` pairs)
  - `pub trait Observer<E> { fn pre_graph(&mut self) -> Result<(), E> { Ok(()) } fn pre_node(&mut self, node: &str) -> Result<(), E> { Ok(()) } fn post_node(&mut self, node: &str, ok: bool) -> Result<(), E> { Ok(()) } fn post_graph(&mut self, ok: bool) -> Result<(), E> { Ok(()) } }` (generic error, so the binding keeps a hook's original Python exception)
  - `pub enum RunError<E> { Node { node: String, source: E }, Observers { event: &'static str, errors: Vec<E> } }`
  - `impl Plan { pub fn run<P, V: Evaluate<P>>(&self, graph: &Graph<P>, eval: &mut V, inputs: &BTreeMap<String, V::Value>, overrides: &BTreeMap<String, V::Value>, observers: &mut [&mut dyn Observer<V::Error>]) -> Result<BTreeMap<String, V::Value>, RunError<V::Error>> }`
- Semantics: steps run in plan order; released values are dropped; `UseInput`/`UseOverride` read from the maps; a present `None`-like value is still supplied (presence is by key); on a node error, `post_graph(false)` still runs, then the node error is returned; per Q23 every observer's hook for an event runs, then all errors are returned together as `RunError::Observers`.

- [x] **Step 1: Write failing tests** with a test `Evaluate` over `i64` that adds its args: `runs_a_diamond_and_returns_only_requested_outputs`; `a_none_valued_input_counts_as_supplied` (use `Option<i64>` values); `released_intermediates_are_dropped` (a counting value type); `a_node_error_returns_node_name_and_still_calls_post_graph_false`; `every_observer_runs_for_an_event_before_errors_are_returned` (two observers, first fails in `post_node`, second still saw the node, result is `Observers{errors.len()==1}`); `a_chain_of_ten_thousand_nodes_runs_without_stack_overflow`.
- [x] **Step 2: Run** `cargo test -p fnloom-core run`. Expected: FAIL.
- [x] **Step 3: Implement** `run` as a single loop over `steps()`; build `args` as `Arg` values, cloned from a `Vec<Option<Value>>` slot table indexed by `NodeId`.
- [x] **Step 4: Run** `cargo test --workspace`. Expected: PASS (the ten-thousand-node test in a debug build too).
- [x] **Step 5: Commit** `core: executor with Evaluate and Observer seams`.

### Task 6: PyO3 binding

**Files:** Modify `crates/fnloom-py/src/lib.rs`, `crates/fnloom-py/Cargo.toml` (depends on `fnloom-core`); Create `python/fnloom/_core.pyi` (the typed stub strict ty needs for the extension), `tests/test_binding.py`

**Interfaces:**
- Consumes: all of `fnloom-core`.
- Produces, in module `fnloom._core`:
  - `class Graph`: `Graph(nodes: list[tuple[str, list[tuple[str, bool]], dict[str, str], Callable[..., Any]]])` where each tuple is `(name, [(param, required)], tags, fn)`; `node_names() -> list[str]`; `upstream_of(names: list[str]) -> list[str]`; `downstream_of(names: list[str]) -> list[str]`; `between(a: str, b: str) -> list[str]`; `prepare(outputs: list[str], inputs: list[str], overrides: list[str]) -> Plan`
  - `class Plan`: `run(inputs: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]`; `steps() -> list[tuple[str, str]]` (`(node, action)`)
  - `class ObserverBase`: Python subclasses may define `pre_graph`, `pre_node`, `post_node`, `post_graph`; `Plan.run(..., observers=[...])` accepts them. Observer errors raise the original exception objects: an `ExceptionGroup` when several fail, the exception itself when one does.
- Error mapping: `BuildError` and `PlanError` raise `ValueError` (message names the culprits); a node's own exception propagates unchanged with `add_note(f"in node {name!r}")`.

- [x] **Step 1: Write failing tests** in `tests/test_binding.py`: `test_prepare_and_run_a_chain`; `test_unknown_output_raises_value_error_naming_it`; `test_node_exception_keeps_its_class_and_gets_a_note` (`ZeroDivisionError`, note contains the node name); `test_observers_see_post_graph_false_on_failure`; `test_a_rust_panic_becomes_a_python_exception` (expose a `_core._panic_for_test()` compiled only under a `test-hooks` feature, assert `pyo3_runtime.PanicException`-style `BaseException` is raised and the interpreter survives); `test_ten_thousand_node_chain` (build via generated functions); `test_stub_lists_exactly_the_runtime_names` (the public names in `_core.pyi` equal `dir(fnloom._core)` minus dunders).
- [x] **Step 2: Run** `uv run maturin develop && uv run pytest tests/test_binding.py -v`. Expected: FAIL.
- [x] **Step 3: Implement** the classes. `P` is `Py<PyAny>` (the function); `Value` is `Py<PyAny>`; `evaluate` calls the function with `kwargs` built from `args`. Hold the GIL for the whole run (threads are a later plan).
- [x] **Step 4: Run** the same command plus `cargo test --workspace`. Expected: PASS.
- [x] **Step 5: Commit** `py: Graph, Plan and observer bindings`.

### Task 7: Native front end and `Driver`

Implements name-wiring (design §9).

**Files:** Create `python/fnloom/_scan.py`, `python/fnloom/driver.py`, `tests/test_scan.py`, `tests/test_driver.py`; Modify `python/fnloom/__init__.py`

**Interfaces:**
- Consumes: `fnloom._core.Graph`, `Plan`.
- Produces:
  - `fnloom._scan.collect(modules: Sequence[ModuleType]) -> list[NodeDef]` where `NodeDef` is a frozen dataclass `(name: str, params: tuple[ParamDef, ...], tags: Mapping[str, str], fn: Callable[..., object], module: str)` and `ParamDef` is `(name: str, required: bool)`.
  - `class Builder`: `with_modules(*modules: ModuleType) -> Builder` (returns a new builder; calls accumulate modules in order) and `build() -> Driver`. `with_config`, `with_observers` and the type-check opt-out are added to it in later plans.
  - `fnloom.build(*modules: ModuleType) -> Driver`, equivalent to `Builder().with_modules(*modules).build()`
  - `class Driver`: `execute(final_vars: Sequence[str], inputs: Mapping[str, object] | None = None, overrides: Mapping[str, object] | None = None) -> dict[str, object]`; `prepare(final_vars, inputs=None, overrides=None) -> PreparedPlan` where `PreparedPlan.run(inputs: Mapping[str, object] | None = None, overrides: Mapping[str, object] | None = None) -> dict[str, object]` reuses the compiled plan for the same input and override *names*; `node_names() -> list[str]`; `what_is_upstream_of(*names: str) -> list[str]`; `what_is_downstream_of(*names: str) -> list[str]`.
- Scan rules (Hamilton behaviour, each pinned by the differential tests in Task 8): a function is a node if it is public (no leading underscore), is a function, and was defined in that module; every parameter and the return value needs an annotation, else `ValueError`; a parameter with a default is `required=False`; the same node name in two modules is `ValueError` naming both modules.

- [x] **Step 1: Write failing tests**: `test_scan.py::test_underscore_and_imported_functions_are_not_nodes`; `::test_missing_annotation_raises_value_error`; `::test_default_parameter_is_optional`; `::test_duplicate_name_across_modules_names_both_modules`; `test_driver.py::test_execute_chain`; `::test_builder_and_build_shortcut_give_the_same_graph`; `::test_with_modules_returns_a_new_builder`; `::test_bare_string_final_vars_raises_type_error` (Q6); `::test_execute_empty_list_returns_empty_dict`; `::test_build_from_zero_modules_is_empty`; `::test_prepare_reuses_plan_across_runs` (the node function is called once per run, and `prepare` is invoked once, observed through a counter on a wrapper); `::test_ten_thousand_node_chain_executes`.
- [x] **Step 2: Run** `uv run pytest tests/test_scan.py tests/test_driver.py -v`. Expected: FAIL.
- [x] **Step 3: Implement** `_scan.collect` with `inspect.signature` and `annotationlib.get_annotations` (resolve forward references; add `test_scan.py::test_forward_reference_annotation_is_accepted`) (annotation values are checked for presence only; type checking is Plan 2), and `Driver` over `_core.Graph`. Cache prepared plans keyed by `(tuple(final_vars), frozenset(inputs), frozenset(overrides))`.
- [x] **Step 4: Run** `bash scripts/check.sh`. Expected: exit 0 (tests pass, `ty check` reports 0 diagnostics, ruff and Clippy clean).
- [x] **Step 5: Commit** `python: native front end and Driver`.

### Task 8: Differential harness against Hamilton 1.90.0

**Files:** Create `tests/differential/conftest.py`, `tests/differential/fixtures/{chain,diamond,optional,override,missing}.py`, `tests/differential/test_semantics.py`, `tests/differential/test_deliberate_differences.py`

**Interfaces:**
- Produces: in `conftest.py`, `@dataclass(frozen=True) class Outcome: values: dict[str, object] | None; error: str | None` (`error` is the exception class name); `run_hamilton(module, outputs, inputs, overrides) -> Outcome` using `hamilton.driver.Builder().with_modules(module).build().execute(...)`; `run_fnloom(module, outputs, inputs, overrides) -> Outcome`; a marker `@pytest.mark.deliberate("Q4")` that requires the id to exist in `docs/quirk-register.md`.
- The conftest sets `HAMILTON_AUTOLOAD_EXTENSIONS=0` and the telemetry-off variable (confirm its exact name in the installed wheel's `telemetry.py`) before importing Hamilton, and runs both runtimes on the same fixture module object.

- [x] **Step 1: Write failing tests**, one per behaviour, comparing `run_hamilton(...) == run_fnloom(...)`: `test_chain`, `test_diamond`, `test_optional_input_used_when_supplied`, `test_optional_input_default_when_not_supplied`, `test_required_input_missing_same_exception_class`, `test_unknown_output_same_exception_class`, `test_override_cuts_upstream`, `test_cycle_same_exception_class` (fixture built from a module with mutually dependent functions), `test_duplicate_across_modules_same_exception_class`.
- [x] **Step 2: Run** `uv run pytest tests/differential -v`. Expected: FAIL until the harness exists. Once it exists, any test failing because Hamilton and fnloom disagree must be settled by reading the register: either fix fnloom, or (if the register already lists a deliberate difference) move the case to `test_deliberate_differences.py`. Never edit an expected value to make a test pass.
- [x] **Step 3: Write the deliberate-difference tests**, each asserting Hamilton's behaviour *and* fnloom's: `test_q4_unknown_input_name` (`@deliberate("Q4")`: Hamilton returns values, fnloom raises `ValueError`); `test_q5_misspelled_override`; `test_q6_bare_string_final_vars` (Hamilton splits characters, fnloom `TypeError`); `test_q7_override_naming_an_input` (Hamilton refuses when the input is not supplied, fnloom accepts); `test_deep_chain_hamilton_recursion_error` (Hamilton `RecursionError` at 5,000 nodes, fnloom succeeds; record as a new register row `Q25` in `docs/quirk-register.md` in this task).
- [x] **Step 4: Run** `uv run pytest tests/differential -v`. Expected: PASS. Record in the commit message any behaviour where Hamilton surprised the register.
- [x] **Step 5: Commit** `tests: differential harness against Hamilton 1.90.0`.

### Task 9: Benchmark harness

**Files:** Create `benches/generate.py`, `benches/run.py`, `benches/README.md`

**Interfaces:**
- Produces: `benches/generate.py::make_module(shape: Literal["chain", "fan_in", "layered"], n: int) -> types.ModuleType` (generated source executed into a module; every function annotated; `fan_in` is one function with `n` parameters); `benches/run.py` prints JSON lines `{"shape", "n", "runtime", "build_ms", "first_execute_ms", "steady_execute_ms", "error"}` for `runtime in {"hamilton", "fnloom"}` and `n in {10, 100, 1000, 10000}`, taking the median of 7 steady runs after one warm run. A runtime that raises records its exception class in `error` and null timings.

- [x] **Step 1: Write a failing test** `tests/test_bench_generate.py::test_generated_chain_has_n_nodes_and_executes` (n=50 under fnloom, result equals the closed form) and `::test_fan_in_has_one_wide_function`.
- [x] **Step 2: Run** `uv run pytest tests/test_bench_generate.py -v`. Expected: FAIL.
- [x] **Step 3: Implement** `generate.py` and `run.py`.
- [x] **Step 4: Run** `uv run python -m benches.run > bench.jsonl` and `uv run pytest -v`. Expected: PASS; the JSON has 24 rows. Paste the table of medians into `benches/README.md` (since moved to `docs/benchmarks.md`) with the machine description; state plainly any row where fnloom is slower than Hamilton, since the design calls that a bug.
- [x] **Step 5: Commit** `bench: generated-graph harness and first numbers`.

### Task 10: Instruction-count regression gate for the core

**Files:** Create `crates/fnloom-core/benches/core.rs`, `crates/fnloom-core/benches/baseline.json`, `scripts/bench_gate.py`; Modify `crates/fnloom-core/Cargo.toml` (dev-dependency, `[[bench]]`), `.github/workflows/ci.yml` (Linux job)

**Interfaces:**
- Consumes: `Graph::build`, `Graph::prepare`, `Plan::run` with the test `Evaluate` from Task 5.
- Produces: `cargo bench -p fnloom-core --bench core` reporting instruction counts for build, prepare and run on chain, fan-in and layered graphs at 1,000 and 10,000 nodes; `scripts/bench_gate.py --baseline crates/fnloom-core/benches/baseline.json` exits 1 if any count rises more than 2% over the baseline.
- Harness: a callgrind-based Rust benchmark crate. Check the current name, maintenance and licence first (it was published as `iai-callgrind`; a successor name may exist) and record the choice in the bench file's header comment. Linux only; skipped with a message on macOS.

- [x] **Step 1: Write a failing test** `scripts/test_bench_gate.py::test_gate_fails_on_a_three_percent_rise_and_passes_on_one` (synthetic JSON in, exit codes out).
- [x] **Step 2: Run** `uv run pytest scripts/test_bench_gate.py -v`. Expected: FAIL.
- [x] **Step 3: Implement** the benches and the gate script; generate `baseline.json` on the CI Linux runner and commit it.
- [x] **Step 4: Run** the gate against its own baseline on Linux. Expected: exit 0. Then on a branch that adds a deliberate extra pass over the nodes, expected: exit 1. Discard that branch.
- [x] **Step 5: Commit** `bench: instruction-count regression gate for the core`.

---

## Self-review

- **Spec coverage:** IR, planning, executor, Observer seams and Q23 (Tasks 2-5); binding with panic and exception mapping (6); native front end, Q6, duplicate names (7); instruction-count gate (10); differential harness, per-row deliberate-difference tests, Q4/Q5/Q7 (8); continuous benchmarks (9); stable-API items (panic safety, abi3, MSRV via `rust-version`) in Task 1 and Global Constraints. Not in this plan, by design: decorators, type checks, adapters, async, compat layer, DOT, dynamic execution (Plans 2-6+).
- **Types:** `NodeId`, `Edge`, `Step.args`, `Request`, `PlanError` variants and the `_core.Graph` tuple shape are used identically in Tasks 3-7.
- **Gap to resolve at execution time:** MSRV value (set `rust-version` to the oldest toolchain CI verifies) and the telemetry variable name are confirmed in Tasks 1 and 8, not guessed here.
