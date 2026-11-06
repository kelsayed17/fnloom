# fnloom Plan 13: instance methods as nodes

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan PR by PR, task by task, with superpowers:test-driven-development
> inside each task and an independent reviewer subagent per PR. Steps use checkbox (`- [ ]`) syntax for tracking.

**Status:** drafted 2026-11-05 for the maintainer's approval, with its design. Nothing here is built.

**Goal:** the [instance nodes design](../design/2026-11-05-instance-nodes-design.md), whole.
`Builder.with_modules(Sales(client))` collects an instance's public methods as nodes, as it collects a module's
functions. `self` is bound at build and is never a dependency, and nothing of the instance's state becomes an input.
Every decorator, namespace, absence and fan-out rule works on methods. Cache keys follow what a method reaches through
`self`, and take the instance in only as its declared fingerprint.

**Motivating use, kept generic:** an app builds a sales API client once at startup, wraps it in a `Sales` instance,
and builds drivers from it. Task 12 writes this as the end-to-end test and the docs example, over a neutral
catalog-and-sales domain with a fake client.

**Architecture:**

- **All Python.** Collection, binding, naming, decorators, code versions, keys, views, pickling and export are front-end
  work. `fnloom-core` and `fnloom-py` don't change: to the core, a method node is a node whose payload is a callable,
  which is what every node already is.
- **An instance becomes a source of `Found` entries**, as a module is. `_scan._found` gains a sibling that reads a
  class's MRO statically and binds each function with `types.MethodType`. Everything downstream of `_found` (config
  selection, parameterize rows, steps, extraction, checks, replacement, duplicates) runs on those entries unchanged,
  with the bound payload in place of the function.
- **Keys reuse the binding's machinery.** A method's code version is computed in `_codeversion`, and the instance's
  fingerprint joins the key's static part, as a `value()` constant does. The per-run path is untouched.
- **Default path untouched.** A builder with no instance takes today's branches everywhere: `_found` for modules, the
  existing pickle reduction, export format and graph version.

**Tech Stack:** as Plans 1 to 12. Python 3.14 and 3.14t, ty strict, ruff ALL, rumdl, pytest with hypothesis, and
`apache-hamilton==1.90.0` as the oracle. Build with `maturin develop --features test-hooks`, test with `UV_NO_SYNC=1`,
and gate with `scripts/check.sh`.

**Spec:** `docs/design/2026-11-05-instance-nodes-design.md`. Its sections:

- §4 (collecting an instance), §5 (code versions), §6 (the instance in the key);
- §7 (decorators), §8 (kinds), §9 (inspection, drawing, export), §10 (pickling);
- §13, the calls and the maintainer's open questions;
- §14, rows Q160 to Q165 and the amended rows;
- §15, the proof and benchmarks.

Also read the [roadmap](2026-10-28-roadmap-ahead-of-hamilton.md), the [caching design](../design/2026-10-27-caching-design.md)
§4 to §6 and §13, and the [dynamic execution design](../design/2026-10-28-dynamic-execution-design.md) §7.

**Order and release:**

- Plan 13 is built after Plan 11 has merged to `main` (roadmap, item 12). Task 7 tests methods under `with_threads`,
  Plan 11's executor, and Task 11 extends the export format Plans 10 and 11 leave.
- It ships in the release after Plan 11's. The version number is the maintainer's call.
- The design's open questions (§13, 1 to 8) are settled before Task 1 starts. Where the maintainer picks an answer
  other than the recommended one, the PR that builds it says so in its body, and the design's change log records it.
- Before each PR, re-read the files it touches on `main`. Plans 10 and 11 move names this plan cites (`_scan`'s
  `core_inputs`, the export format, `_caching`'s key plan). Where they did, follow `main`, and say so in the PR body.

## Global Constraints

- Every Global Constraint of Plans 1 to 12 applies unchanged:
    - Python 3.14 and 3.14t;
    - ty strict, ruff ALL, Clippy pedantic, rumdl, and the crate's MSRV;
    - one commit per task, `scripts/check.sh` green on every commit;
    - author email `4119211+kelsayed17@users.noreply.github.com`, with the trailer
      `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`;
    - branches merge `main` in, and nothing is rebased or force-pushed.
- No Rust change. If a task finds it needs one, it stops and asks.
- **No user code runs at build** beyond what already runs for modules (decorators applied at class creation, `@resolve`
  and `config.matches` predicates, `__fnloom_fingerprint__` and registered fingerprints). Collection reads class
  dictionaries and never calls `getattr` on an instance.
- **Identity, never equality**, for instances: deduplication, `final_vars`, `Driver.instances`, and every map keyed by
  instance uses `id()` with the object held alive beside it.
- Determinism: no name, error, export, graph version or cache key may depend on `PYTHONHASHSEED` or on an `id()`.
  Every error lists all culprits, sorted.
- Register rows come first. Each row gets its text before its `deliberate("Qn")` test, in
  `tests/differential/test_deliberate_differences.py`.
- No new runtime dependency. No Hamilton code copied. Examples use neutral domains (catalogs, regions, sales).
- The default path is untouched. `benches.run` stays within noise, and the instruction-count gate stays green, on a
  graph with no instance.
- Each PR adds a CHANGELOG `[Unreleased]` entry, and rewrites the `from-hamilton.md` rows it changes.
- Every builder setting this plan adds survives pickling, `copy`, `rebuild()` and `build_async`, as Plans 5 to 12
  settled. It adds one kind of builder content (instances), covered by Task 10.

## Calls this plan makes

Where the design leaves the shape to the builder. Change any of them before the build starts.

1. **`Found` stays a triple.** An instance's entry is `(module, attribute name, payload)`, where `module` is the
   defining function's `__module__` and the payload is the bound method. A parallel `owners: dict[int, Instance]`,
   keyed by the payload's `id`, carries the instance and its class to the views, keys and export. This keeps every
   consumer of `Found` unchanged.
2. **`_instances.Instance`** is a frozen record: `obj: object`, `cls: type`, `place: int` (its position among the
   builder's instances, for error text), and `namespace: str | None`.
3. **Statically, an origin is a function.** Everything that today reads `node.origin` as a function (`_codeversion`,
   `_export`'s versions, the `final_vars` match, `_steps`' mutate targets, `_functions.function_of`) reads it through
   one helper, `_instances.function_of(origin) -> FunctionType`, which unwraps a bound method. No `isinstance` checks
   are scattered through those modules.
4. **The self-read check** is `_codeversion.reads_first_local(code) -> bool`. It is true when any instruction loads
   local 0 (`LOAD_FAST`, `LOAD_FAST_CHECK`, `LOAD_FAST_BORROW`, or a combined `LOAD_FAST_LOAD_FAST` that names it), when
   local 0 is a cell variable, or when `co_names` holds `locals`, `vars`, `eval`, `exec` or `super`. It errs towards
   "reads": a false "reads" costs a hit, a false "doesn't" would cost a wrong hit. It reads instruction names through
   `dis`, so 3.14's specialised opcodes are covered by name.
5. **The instance's fingerprint is framed into the key's static part** under a `b"self"` tag, beside the code version,
   once at build. A method that doesn't read `self`, and one that ignores it, frame nothing there, so their keys equal a
   function node's with the same code.
6. **The export's next format.** If Plan 10 leaves `fnloom.export/2` current, a graph with method nodes exports
   `fnloom.export/3`, and every other graph exports `/2`, byte for byte. Task 11 checks `main` and uses whatever
   number follows.
7. **The pickle form of a builder with instances** adds one tuple, `instances: tuple[tuple[int, object], ...]`: each
   instance with its position among the builder's items, so `_builder_of` interleaves them with the re-imported
   modules. A builder with none pickles to today's tuple, byte for byte.
8. **`Driver.instances`** is a `tuple[object, ...]`. It is not generic: an app with several instance classes reads them
   by type, and a typed accessor is a later convenience.

## Register rows

The next free rows in [the register](../quirk-register.md) are Q160 to Q165, reserved by the design (§14). If another
plan has taken them by the time Task 1 starts, renumber them here, in the design, and in every test. Plan 13 writes:

| Row | Task | Disposition (design §14) | Note |
|---|---|---|---|
| Q160 | 1 | FIX, REFUSE | The FIX half in Task 1, the REFUSE half's last cases in Task 2 |
| Q161 | 2 | FIX | |
| Q162 | 1 | FIX | |
| Q163 | 9 | FIX | Code versions in Task 8, the instance in Task 9 |
| Q164 | 4 | FIX | The export's `class` in Task 11 |
| Q165 | 10 | FIX | |

**Amended rows:**

| Row | Task | What changes |
|---|---|---|
| Q30 | 4 | Bound methods in `final_vars` |
| Q48 | 8 | A method's code version follows `self` attributes |
| Q52 | 1 | Instances accepted; a class's message says to give an instance |
| Q53 | 2 | An instance with no nodes, and why each public attribute is not one |
| Q70 | 1 | `Driver.instances` |
| Q72 | 4 | `with_functions` and `fnloom.node` take a bound method |
| Q73 | 2 | A class-body `__all__`; `@exclude` on methods |
| Q74, Q150 | 3 | Instances count as modules for duplicates and `replace=` |
| Q83 | 10 | `rebuild()` refuses a stale class |
| Q95 | 11 | The export's `class` and its format number |
| Q104, Q107 | 6 | Steps and mutators bound to the instance |

## File Structure

```text
python/fnloom/
  _instances.py  NEW  Instance, the refusals of design §4.5, static MRO collection, counted classes,
                      class __all__, binding, function_of, the left-out reasons
  _scan.py       _found's sibling for instances; _functions and collect take mixed items; owners map;
                 the no-nodes message for an instance; shadowing and self checks
  driver.py      _checked_modules -> _checked_items; Builder holds instances in order; with_modules' annotation
                 and docstring; Driver.instances and AsyncDriver.instances; __reduce__ / _builder_of with
                 instances; rebuild's stale-class check; final_vars by bound method
  _functions.py  bound methods in with_functions and fnloom.node
  _view.py       NodeView.instance; origin as the bound method
  _decorators.py TypeError for a staticmethod or classmethod object
  _steps.py      steps and mutators bound to the instance; class mutators' targets
  _codeversion.py method_version: self attributes through the counted MRO; reads_first_local
  _caching.py    method code versions; the instance's fingerprint in the static part; ignore=("self",);
                 explain_cache reasons
  _types.py      typing.Self as the instance's class
  _export.py     the class key; the next format; graph_version; diff reads both
  __init__.py    nothing new is exported (Driver.instances and NodeView.instance are attributes)
tests/
  test_instances.py  test_instances_decorators.py  test_instances_caching.py  test_instances_example.py   NEW
  instance_nodes.py  NEW  importable classes for pickling and process-pool tests
  test_scan.py  test_namespaces.py  test_replace.py  test_mutate.py  test_steps.py  test_async.py
  test_threads.py  test_absence.py  test_blocks.py  test_codeversion.py  test_caching_property.py
  test_pickling.py  test_export.py  test_drawing.py  test_view.py  test_public_api.py
  differential/test_deliberate_differences.py
benchmarks/test_instances.py (CodSpeed)   benches/run.py (a method chain row)   benches/generate.py (method chains)
docs/writing-nodes.md (Nodes from an instance), caching.md (Methods and self), running.md (threads, pickling),
  from-hamilton.md, quirk-register.md, benchmarks.md, index.md, README.md, CHANGELOG.md,
  the roadmap, and the design's change log
```

## PR breakdown

Each PR follows the same steps:

1. TDD, task by task, with each task's register rows written before its tests.
2. `scripts/check.sh` green on every commit.
3. A CHANGELOG entry, then an independent reviewer subagent.
4. The maintainer's go-ahead to squash-merge.

| PR | Tasks | Needs merged first | Runs in parallel with |
|---|---|---|---|
| **A** Instances: collection, binding, names, inspection | 1, 2, 3, 4 | Plan 11 | none |
| **B** Decorators, steps and kinds on methods | 5, 6, 7 | A | C, D |
| **C** Caching methods | 8, 9 | A | B, D |
| **D** Pickling, rebuild, export and drawing | 10, 11 | A | B, C |
| **Z** Close-out | 12 | every other PR | none |

B, C and D touch disjoint code past A: B is `_decorators`, `_steps` and the kind checks; C is `_codeversion` and
`_caching`; D is `driver.py`'s pickling and `_export`.

**Textual collisions between parallel PRs**, each resolved by merging `main` in, never by rebasing:

- **B and C:** `tests/test_instances_caching.py` is C's only, and B's caching cases of decorated methods live in
  `tests/test_instances_decorators.py`. Both append rows to `docs/quirk-register.md` (separate rows) and to
  `CHANGELOG.md`.
- **C and D:** `_export.py` reads code versions through `_codeversion.method_version`, which C adds. D's Task 11 calls
  it if C has merged, and otherwise `code_version(function_of(origin))`, with a TODO that C's merge resolves. D's
  export test of `version` is marked to follow C.
- **B and D:** `docs/writing-nodes.md` (B) and `docs/running.md` (D) are separate files. Both touch `CHANGELOG.md`.

## Review Focus

1. **A class with a raising property, `__getattr__` and `__getattribute__`.** The instance builds, runs, pickles (with a
   `__reduce__`) and exports, and none of the three is ever called. (Task 2,
   `test_collection_never_runs_the_instances_code`)
2. **Two drivers sharing one `DiskCache`, with `Sales(eu_client)` and `Sales(us_client)`, no namespaces.** A method
   reading `self` without a fingerprint is uncacheable in both. With `__fnloom_fingerprint__` returning the region the
   two never share an entry, and a method that doesn't read `self` shares one. (Task 9,
   `test_two_instances_never_share_an_entry_unless_declared`)
3. **The self-read check on 3.14's bytecode.** `return self._client.get(x)`, `return [self.f(y) for y in xs]` (a
   comprehension, inlined in 3.12 and later), `return (lambda: self)()`, `super().total(x)`, `return locals()` all read
   `self`. `return sum(xs)` and `return x * 2` don't. (Task 9, `test_reads_first_local_on_every_form`)
4. **Inheritance across an installed base.** A class deriving from a pydantic `BaseModel` and a user base gets nodes
   from itself and the user base, none of `BaseModel`'s, and the no-nodes message names `BaseModel` when it is the only
   source. (Task 2, `test_library_bases_contribute_no_nodes`)
5. **A driver over an instance in a `ProcessPoolExecutor`.** With a `__reduce__` that rebuilds the client, results
   equal the in-process run. Without one, `pickle.dumps(driver)` raises naming the instance and its namespace. (Task 10,
   `test_a_driver_over_an_instance_runs_in_a_process_pool`)

---

## PR A: instances, collection, binding, names, inspection

### Task 1: `with_modules` takes an instance (Q160, Q162; Q52, Q70 amended)

**Files:**

- Create: `python/fnloom/_instances.py`, `tests/test_instances.py`, `tests/instance_nodes.py`
- Modify: `python/fnloom/driver.py`, `python/fnloom/_scan.py`, `docs/quirk-register.md`,
  `tests/differential/test_deliberate_differences.py`, `tests/test_public_api.py`

**Interfaces:**

- Produces:
    - `Builder.with_modules(*modules: ModuleType | object, ...)`, with the docstring saying what an instance gives. The
      `Builder` constructor and `fnloom.build(*modules)` take the same items.
    - `driver._checked_items(items) -> tuple[ModuleType | Instance, ...]` replaces `_checked_modules`. It refuses a
      class, a function or bound method, and an instance of a built-in, standard-library or fnloom class, each with
      design §4.5's message.
    - `_instances.Instance(obj, cls, place, namespace)`.
    - `_scan.instance_functions(instance: Instance) -> ModuleFunctions`, for now over `cls.__dict__` only (Task 2 adds
      the MRO), binding each public plain function with `types.MethodType`.
    - `Driver.instances` and `AsyncDriver.instances: tuple[object, ...]`, each once by identity, in build order.
    - The node's parameters drop the first positional one; a method without one is refused (design §4.5).
- [ ] **Step 1: Write the register rows Q160 (its FIX half) and Q162**, design §14's text, and their `deliberate` tests.
    - Q160: Hamilton's `with_modules(Sales(client))` lists no variables. fnloom's lists `orders` and `total`.
    - Q162: Hamilton's `create_temporary_module(sales.orders)` raises `ValueError` matching `Missing type hint for self`.
      fnloom's `inputs()` is `{"region"}` only, with no `self` and no client.
- [ ] **Step 2: Write the failing tests** in `tests/test_instances.py`:
    - `test_an_instances_public_methods_are_nodes`: the issue's `Sales` with a fake client, `execute(["total"],
      inputs={"region": "eu"})`.
    - `test_self_is_never_a_dependency_or_an_input`: over `inputs()`, `NodeView.params` and a node wrapper's `inputs`.
    - `test_self_is_read_by_position`: a method whose first parameter is `this` works the same.
    - `test_a_parameter_named_like_an_attribute_is_an_ordinary_input`: `def f(self, _client: int)` reads input `_client`.
    - `test_a_class_is_refused_with_a_hint`: the message names `Sales` and says `with_modules(Sales(...))`.
    - `test_functions_and_bound_methods_are_refused_with_a_hint`: the message says `with_functions`.
    - `test_builtin_stdlib_and_fnloom_instances_are_refused`: `42`, `"x"`, `{}`, `Path(".")`, a `Builder`.
    - `test_a_method_without_a_positional_self_is_refused`: `def f(*, x)` and `def f()`, both named, sorted.
    - `test_modules_and_instances_mix_in_order`: `with_modules(module_a, Sales(c), module_b)`.
    - `test_one_instance_given_twice_counts_once`, and `test_instances_are_compared_by_identity`: an unhashable
      `@dataclass(eq=True)` instance works, and two equal instances of it collide (Task 3's error).
    - `test_driver_instances_lists_each_once_in_build_order`, on both drivers; `Driver.modules` excludes them.
    - `test_a_module_subclass_instance_is_a_module`.
- [ ] **Step 3: Run them to see them fail.** Run `uv run maturin develop --features test-hooks && UV_NO_SYNC=1 uv run
  pytest tests/test_instances.py tests/differential -k "instance or Q160 or Q162"`. Expected: `TypeError: expected a
  module, got Sales`.
- [ ] **Step 4: Implement.** Amend Q52 and Q70 in the register.
- [ ] **Step 5: Run the tests to see them pass**, plus `pytest tests/test_scan.py tests/test_driver.py`. Expected: PASS.
- [ ] **Step 6: Run `scripts/check.sh`, then commit** `with_modules takes an instance: its methods are nodes (Q160,
  Q162)`.

### Task 2: what is collected (Q161; Q160's refusals, Q53, Q73 amended)

**Files:**

- Modify: `python/fnloom/_instances.py`, `python/fnloom/_scan.py`, `python/fnloom/_decorators.py`,
  `docs/quirk-register.md`, `tests/differential/test_deliberate_differences.py`
- Test: `tests/test_instances.py`

**Interfaces:**

- Produces:
    - `_instances.counted(cls) -> tuple[type, ...]`: `cls` and its bases that are user code (`_codeversion.is_user_module`)
      or share `cls`'s top-level package, in MRO order, never `object`.
    - `_instances.static_entry(cls, name) -> tuple[type, object] | None`: the class holding `name` first in `cls.__mro__`
      and its raw `__dict__` entry, as `inspect.getattr_static` finds it, without calling any descriptor.
    - `_instances.listed(cls) -> frozenset[str] | None`: the class-body `__all__` found statically, checked as Q73
      checks a module's.
    - `_instances.left_out(...) -> list[LeftFunction]` with design §4.5's reasons: `is a classmethod`, `is a property`,
      `is a descriptor`, `is defined on '<library class>'`, `not listed in __all__`, `marked @exclude`.
    - The `TypeError` from every fnloom decorator given a `staticmethod` or `classmethod` object.
- [ ] **Step 1: Write the register row Q161** and its `deliberate` test: Hamilton finds nothing in a class; fnloom finds
  a staticmethod and an inherited user method, and not a classmethod or a property. Amend Q53 and Q73.
- [ ] **Step 2: Write the failing tests:**
    - `test_staticmethods_are_nodes_and_classmethods_are_not`;
    - `test_properties_and_descriptors_are_not_nodes`, including `functools.cached_property`;
    - `test_instance_attributes_are_not_nodes`: a callable set in `__init__`;
    - `test_private_and_dunder_methods_are_not_nodes`: `__call__`, `__repr__`, `_helper`;
    - `test_inherited_user_methods_are_nodes_and_overrides_win`: a base `total` overridden by the subclass;
    - `test_library_bases_contribute_no_nodes` (Review Focus 4), with `abc.ABC`, `typing.Generic[T]` and a pydantic
      `BaseModel`;
    - `test_a_base_from_the_same_package_counts_when_installed`: a fixture package placed on a path the library-path
      check treats as installed;
    - `test_a_class_body_all_lists_the_nodes`, `test_a_malformed_class_all_is_a_type_error`,
      `test_exclude_beats_a_class_all`;
    - `test_collection_never_runs_the_instances_code` (Review Focus 1);
    - `test_an_attribute_shadowing_a_method_is_refused`;
    - `test_an_instance_with_no_nodes_names_each_attribute_and_why`;
    - `test_a_missing_input_named_like_a_classmethod_says_why`;
    - `test_decorating_a_staticmethod_object_is_a_type_error`, for `@tag`, `@cache`, `@config.when` and `@parameterize`;
    - `test_nodes_are_found_base_first_in_definition_order`.
- [ ] **Step 3: Run them to see them fail.** Run `UV_NO_SYNC=1 uv run pytest tests/test_instances.py -k "collect or
  static or class or inherit or librar or all or shadow or no_nodes"`. Expected: failures on inherited, static and
  refusal cases.
- [ ] **Step 4: Implement.** `instance_functions` walks `reversed(counted(cls))`, and for each public name of each class
  takes `static_entry(cls, name)` and keeps it only when the holder is that class (so each name is found once, at its
  winner).
- [ ] **Step 5: Run the tests to see them pass**, plus `pytest tests/test_scan.py tests/test_decorators.py`. Expected:
  PASS.
- [ ] **Step 6: Run `scripts/check.sh`, then commit** `instances: what is collected, and why the rest is not (Q161)`.

### Task 3: names, collisions, namespaces and `replace=` (Q74, Q150, Q112 amended)

**Files:**

- Modify: `python/fnloom/_scan.py` (`_duplicate`, `_definition`, `_replacing`), `docs/quirk-register.md`
- Test: `tests/test_instances.py`, `tests/test_namespaces.py`, `tests/test_replace.py`, `tests/test_namespace_config.py`

**Interfaces:**

- Produces: error text naming an instance as `instance of 'app.sales.Sales'`, or `the 2nd instance of
  'app.sales.Sales'` when the build holds several of its class without namespaces (design §4.4).
- [ ] **Step 1: Write the failing tests:**
    - `test_two_instances_of_one_class_collide_without_namespaces`: both named, the 1st and the 2nd;
    - `test_two_instances_under_two_namespaces_build`: `eu.orders` and `us.orders`, each bound to its own client;
    - `test_an_instance_and_a_module_collide_on_a_name`;
    - `test_an_instance_and_a_with_functions_function_collide`;
    - `test_an_instance_replaces_a_modules_node` and `test_a_module_replaces_an_instances_node`;
    - `test_one_instance_under_two_namespaces_shares_its_object`;
    - `test_bind_and_config_per_instance_namespace`: `config={"region": "eu"}` selects `orders__eu`;
    - `test_collision_errors_do_not_depend_on_ids`: run under two `PYTHONHASHSEED`s in subprocesses, equal text.
- [ ] **Step 2: Run them to see them fail.** Run `UV_NO_SYNC=1 uv run pytest tests/test_instances.py -k "collide or
  namespace or replace or bind"`. Expected: the error text names no instance.
- [ ] **Step 3: Implement.** Amend Q74 and Q150 ("a module or an instance"), and Q112's example.
- [ ] **Step 4: Run the tests to see them pass**, plus `pytest tests/test_namespaces.py tests/test_replace.py`.
  Expected: PASS.
- [ ] **Step 5: Run `scripts/check.sh`, then commit** `instances: names, collisions, namespaces and replace`.

### Task 4: views, `final_vars`, `with_functions` (Q164; Q30, Q72 amended)

**Files:**

- Modify: `python/fnloom/_view.py`, `python/fnloom/_functions.py`, `python/fnloom/driver.py` (the `final_vars` match),
  `python/fnloom/_types.py` (`typing.Self`), `docs/quirk-register.md`,
  `tests/differential/test_deliberate_differences.py`
- Test: `tests/test_instances.py`, `tests/test_view.py`

**Interfaces:**

- Produces:
    - `NodeView.instance: object | None`; `NodeView.origin` the bound method; `NodeView.module` the function's module.
    - `final_vars` entries: a bound method matches by `(__func__, __self__)` identity; a plain method function matches
      when one instance provides it, else `TypeError` listing the namespaces.
    - `with_functions(bound_method)` and `fnloom.node(bound_method, name=..., bind=...)`.
    - `typing.Self` resolved to the instance's class in `_types`' checks.
- [ ] **Step 1: Write the register row Q164** and its `deliberate` test: in Hamilton, no node can come from a method;
  in fnloom, `execute([sales.total])` returns `{"total": ...}`. Amend Q30 and Q72.
- [ ] **Step 2: Write the failing tests:**
    - `test_a_bound_method_in_final_vars_means_its_node`, on `execute`, `prepare` and both drivers;
    - `test_a_bound_method_of_another_instance_is_no_node`: a `ValueError`, Q30's message;
    - `test_a_plain_method_function_needs_one_instance`: one instance works, two namespaces give the `TypeError`;
    - `test_node_view_instance_origin_and_module`: an inherited method's `module` is its base's module;
    - `test_a_function_node_has_no_instance`;
    - `test_with_functions_takes_a_bound_method` and `test_node_takes_a_bound_method`;
    - `test_self_annotation_means_the_instances_class`: a method returning `Self` feeds a parameter typed `Sales`;
    - `test_what_is_the_path_between_and_validate_see_method_nodes`.
- [ ] **Step 3: Run them to see them fail.** Run `UV_NO_SYNC=1 uv run pytest tests/test_instances.py -k "view or
  final_vars or with_functions or self_annotation"`. Expected: `AttributeError: 'NodeView' object has no attribute
  'instance'`.
- [ ] **Step 4: Implement**, with `_instances.function_of` as the one unwrapping helper (call 3).
- [ ] **Step 5: Run the tests to see them pass**, plus `pytest tests/test_view.py tests/test_inspection.py`. Expected:
  PASS.
- [ ] **Step 6: Run `scripts/check.sh`, then commit** `instances: views, final_vars and with_functions (Q164)`.

## PR B: decorators, steps and kinds on methods

### Task 5: every decorator on a method

**Files:**

- Create: `tests/test_instances_decorators.py`
- Modify: `python/fnloom/_scan.py` only where a test shows a function-only assumption
- Test: `tests/test_instances_decorators.py`, `tests/test_stacking.py`

**Interfaces:**

- Consumes: PR A.
- Produces: no new API. Each decorator's behaviour on a method equals its behaviour on a module function.
- [ ] **Step 1: Write the failing tests**, as one parametrized parity suite: each case is written once as a module and
  once as a class, and asserts equal results, views (but `instance` and `origin`) and errors.
    - `test_config_when_variants_on_methods`, with `when`, `when_not`, `when_in`, `when_not_in`, `default`, `matches`;
    - `test_parameterize_rows_on_methods`, with `source`, `value`, `group`, and the `parameterize_sources` and
      `parameterize_values` sugars;
    - `test_extract_fields_and_columns_on_methods`;
    - `test_check_output_on_methods`, and `with_output_type_checks`;
    - `test_tag_cache_defaults_inject_resolve_on_methods`;
    - `test_exclude_on_a_method`;
    - `test_decorators_stack_on_methods_in_either_order`, extending `test_stacking.py`'s matrix with a method column.
- [ ] **Step 2: Run them to see them fail.** Run `UV_NO_SYNC=1 uv run pytest tests/test_instances_decorators.py`.
  Expected: some pass already (decorators record on the function); record which fail, and why, in the commit body.
- [ ] **Step 3: Implement** the fixes the failures need. Each is a place that read `origin` as a function, routed
  through `function_of`.
- [ ] **Step 4: Run the tests to see them pass**, plus `pytest tests/test_stacking.py tests/test_config.py
  tests/test_extract.py`. Expected: PASS.
- [ ] **Step 5: Run `scripts/check.sh`, then commit** `instances: every decorator works on methods`.

### Task 6: steps and mutators bound to the instance (Q104, Q107 amended)

**Files:**

- Modify: `python/fnloom/_steps.py` (`_piped`, mutation lowering, `unbuilt_targets`), `docs/quirk-register.md`
- Test: `tests/test_instances_decorators.py`, `tests/test_mutate.py`, `tests/test_steps.py`

**Interfaces:**

- Produces:
    - A step function that is a function of a counted class of the node's instance, by identity, is called bound.
    - Class mutators: bound to their instance, targeting their class's methods only; a class mutator targeting a module
      function is a `ValueError` naming the mutator and the target.
    - A module mutator targeting a method's function applies to every instance node made from it.
- [ ] **Step 1: Write the failing tests:**
    - `test_a_step_defined_in_the_class_reads_self`: `@pipe_output(step(_scaled))`, where `_scaled(self, x)` reads
      `self._factor`;
    - `test_a_module_step_on_a_method_is_called_unbound`;
    - `test_a_staticmethod_step_is_its_function`;
    - `test_a_class_mutator_targets_its_own_method`, with two instances under two namespaces, each mutator bound to its
      own instance;
    - `test_a_class_mutator_targeting_a_module_function_is_refused`;
    - `test_a_module_mutator_targets_a_method_in_every_namespace`;
    - `test_unbuilt_targets_accepts_mutators_from_built_instances`.
- [ ] **Step 2: Run them to see them fail.** Run `UV_NO_SYNC=1 uv run pytest tests/test_instances_decorators.py -k
  "step or mutat"`. Expected: `TypeError: _scaled() missing 1 required positional argument`.
- [ ] **Step 3: Implement.** Amend Q104 and Q107.
- [ ] **Step 4: Run the tests to see them pass**, plus `pytest tests/test_mutate.py tests/test_steps.py`. Expected:
  PASS.
- [ ] **Step 5: Run `scripts/check.sh`, then commit** `instances: steps and mutators bound to their instance`.

### Task 7: async, generator, absent, fan-out and threaded methods

**Files:**

- Modify: `python/fnloom/_scan.py` (`_check_kind`'s message for a method), `python/fnloom/_absence.py` (the return
  check's message)
- Test: `tests/test_instances_decorators.py`, `tests/test_async.py`, `tests/test_absence.py`, `tests/test_blocks.py`,
  `tests/test_threads.py`

**Interfaces:**

- Consumes: Plan 10's `Absent`, `Parallelizable`, `Collect`; Plan 11's `with_threads`, `absent_on`, async blocks.
- Produces: no new API. Error messages name a method as `'orders' (method 'app.sales.Sales.orders')`.
- [ ] **Step 1: Write the failing tests:**
    - `test_an_async_method_runs_on_the_async_driver`, and `test_build_refuses_an_async_method` with today's message;
    - `test_a_generator_method_is_refused_unless_it_expands`;
    - `test_a_parallelizable_method_expands_and_a_collect_method_gathers`;
    - `test_an_async_generator_method_expands_on_the_async_driver`;
    - `test_a_method_may_decline`: `-> list[Order] | Absent`, its consumers skipped by the three rules;
    - `test_a_method_returning_absent_without_admitting_it_names_the_method`;
    - `test_absent_on_a_method_gives_partial_results`;
    - `test_two_methods_of_one_instance_run_concurrently_under_threads`: on 3.14t, a barrier both methods wait on
      completes, which shows fnloom adds no per-instance lock;
    - `test_tag_limits_serialize_one_instances_methods`: with `limits` on a tag, the same barrier would deadlock, so the
      test uses a counter of concurrent calls and asserts it never exceeds 1.
- [ ] **Step 2: Run them to see them fail.** Run `UV_NO_SYNC=1 uv run pytest tests/test_instances_decorators.py -k
  "async or generator or parallel or absent or thread"`. Expected: the message tests fail; record the rest.
- [ ] **Step 3: Implement** the messages, and any fix the kind tests show.
- [ ] **Step 4: Run the tests to see them pass**, plus `pytest tests/test_async.py tests/test_absence.py
  tests/test_blocks.py tests/test_threads.py`. Expected: PASS, on 3.14 and 3.14t.
- [ ] **Step 5: Run `scripts/check.sh`, then commit** `instances: async, generator, absent and threaded methods`.

## PR C: caching methods

### Task 8: a method's code version follows `self` (Q48 amended)

**Files:**

- Modify: `python/fnloom/_codeversion.py`, `python/fnloom/_caching.py` (the call at `code_version(node.origin)`),
  `docs/quirk-register.md`
- Test: `tests/test_codeversion.py`, `tests/test_instances_caching.py` (create)

**Interfaces:**

- Produces:
    - `_codeversion.method_version(fn: FunctionType, cls: type) -> bytes`: `fn`'s version, plus, for each attribute
      name the settled walk reads, that name's entry on every counted class of `cls`'s MRO, by the global rules.
    - `UncacheableError` for a mutable class attribute a method reads, naming it: `class attribute '_seen' of
      'app.sales.Sales' is a list, which may change between runs`.
- [ ] **Step 1: Write the failing tests:**
    - `test_editing_a_helper_reached_through_self_changes_the_version`;
    - `test_editing_an_unreached_method_keeps_the_version`;
    - `test_an_immutable_class_attribute_is_fingerprinted`;
    - `test_a_mutable_class_attribute_read_through_self_is_uncacheable`, and `@cache(version="1")` vouches for it;
    - `test_super_calls_version_every_definition_on_the_mro`;
    - `test_a_property_read_through_self_versions_its_getter`;
    - `test_a_staticmethods_version_is_its_functions`;
    - `test_an_inherited_methods_version_is_the_same_in_every_subclass_that_does_not_override_what_it_reads`.
- [ ] **Step 2: Run them to see them fail.** Run `UV_NO_SYNC=1 uv run pytest tests/test_codeversion.py
  tests/test_instances_caching.py -k version`. Expected: `TypeError: a code version needs a function, not a method`.
- [ ] **Step 3: Implement**, reusing `_Walk`'s module-attribute path with the class as the namespace. Amend Q48.
- [ ] **Step 4: Run the tests to see them pass**, plus `pytest tests/test_codeversion.py tests/test_caching.py`.
  Expected: PASS.
- [ ] **Step 5: Run `scripts/check.sh`, then commit** `code versions of methods follow self`.

### Task 9: the instance in the key (Q163)

**Files:**

- Modify: `python/fnloom/_codeversion.py` (`reads_first_local`), `python/fnloom/_caching.py` (static part, `ignore`,
  reasons), `docs/quirk-register.md`, `tests/differential/test_deliberate_differences.py`
- Test: `tests/test_instances_caching.py`, `tests/test_caching_property.py`

**Interfaces:**

- Produces:
    - `_codeversion.reads_first_local(code: CodeType) -> bool` (call 4).
    - In the key plan, a method node's static part frames `b"self"` and the instance's fingerprint when it reads `self`
      and doesn't ignore it (call 5). The fingerprint is taken once, in `Builder.build`.
    - `@cache(ignore=("<first parameter>",))` accepted on a method; on a function, or any other name, the existing
      `ValueError`.
    - `explain_cache`'s reason for an uncacheable method, design §6 rule 3's text, and for a record form that holds a
      client, the field path (`self._client: SalesClient`).
- [ ] **Step 1: Write the register row Q163** and its `deliberate` test: Hamilton's `hash_value` gives two
  attribute-less clients the same `'<unhashable>'`; fnloom's two `Sales` instances without a fingerprint make
  `orders` uncacheable, and with `__fnloom_fingerprint__` returning their regions give two keys.
- [ ] **Step 2: Write the failing tests:**
    - `test_reads_first_local_on_every_form` (Review Focus 3);
    - `test_a_method_that_does_not_read_self_is_keyed_as_a_function`: its key equals a module function's with the same
      code and identity but `__qualname__`;
    - `test_a_method_reading_self_without_a_fingerprint_is_uncacheable`, with `explain_cache`'s reason;
    - `test_fnloom_fingerprint_on_the_class_keys_the_instance`, and `return ()` keys it by class alone;
    - `test_register_fingerprint_keys_the_instance`;
    - `test_a_frozen_dataclass_instance_is_keyed_by_its_fields`, and one holding a client names the field;
    - `test_ignore_self_leaves_the_instance_out`, by the first parameter's own name;
    - `test_the_instance_is_fingerprinted_once_at_build`: a counting `__fnloom_fingerprint__` is called once over three
      runs, and again after `rebuild()`;
    - `test_a_client_is_never_fingerprinted`: a client type whose every attribute access raises, inside an instance
      without a fingerprint, builds, runs and explains;
    - `test_two_instances_never_share_an_entry_unless_declared` (Review Focus 2);
    - `test_branch_keys_of_a_method_expand_include_the_instance` (Plan 10);
    - in `tests/test_caching_property.py`, `test_equality_property_over_instances`: the caching design's §10 property
      over generated classes, with random edits to methods, class attributes and declared instance state.
- [ ] **Step 3: Run them to see them fail.** Run `UV_NO_SYNC=1 uv run pytest tests/test_instances_caching.py
  tests/differential -k "self or instance or fingerprint or Q163"`. Expected: every method shares one key across
  instances.
- [ ] **Step 4: Implement.**
- [ ] **Step 5: Run the tests to see them pass**, plus `pytest tests/test_caching.py tests/test_cache_lists.py
  tests/test_field_cache.py tests/test_caching_property.py`. Expected: PASS.
- [ ] **Step 6: Run `scripts/check.sh`, then commit** `the instance in a method's key (Q163)`.

## PR D: pickling, rebuild, export and drawing

### Task 10: pickling, `copy`, `rebuild()` and `build_async` (Q165; Q83 amended)

**Files:**

- Modify: `python/fnloom/driver.py` (`Builder.__reduce__`, `_builder_of`, `rebuild`), `docs/quirk-register.md`,
  `tests/differential/test_deliberate_differences.py`
- Test: `tests/test_pickling.py`, `tests/test_instances.py`, `tests/instance_nodes.py`

**Interfaces:**

- Produces:
    - The builder's pickle carries `instances` (call 7). An unpicklable instance joins the sorted `cannot pickle:`
      problems as `instance of 'app.sales.Sales' (namespace 'eu'): <pickle's message>`.
    - `rebuild()` raises `ValueError` for a class that `sys.modules[cls.__module__]` no longer holds under its
      qualified name, with Q83's wording for a stale function.
- [ ] **Step 1: Write the register row Q165** and its `deliberate` test: Hamilton's `Driver` doesn't pickle; fnloom's
  driver over an importable instance round-trips and runs. Amend Q83.
- [ ] **Step 2: Write the failing tests:**
    - `test_a_driver_over_an_instance_pickles_by_value`: the loaded driver's `instances[0]` is a copy, not the original;
    - `test_an_unpicklable_instance_is_named`, with its namespace, sorted with a module problem;
    - `test_a_driver_over_an_instance_runs_in_a_process_pool` (Review Focus 5);
    - `test_a_builder_without_instances_pickles_as_before`: the reduced tuple equals today's;
    - `test_copy_shares_the_instance`;
    - `test_rebuild_reuses_the_instance_and_rereads_its_fingerprint`;
    - `test_rebuild_refuses_a_class_made_stale_by_a_reload`;
    - `test_build_async_shares_the_instance`;
    - `test_a_prepared_plan_over_an_instance_pickles`.
- [ ] **Step 3: Run them to see them fail.** Run `UV_NO_SYNC=1 uv run pytest tests/test_pickling.py
  tests/test_instances.py -k "pickle or copy or rebuild or async"`. Expected: `TypeError: cannot pickle`.
- [ ] **Step 4: Implement.**
- [ ] **Step 5: Run the tests to see them pass**, plus `pytest tests/test_pickling.py tests/test_driver.py`. Expected:
  PASS.
- [ ] **Step 6: Run `scripts/check.sh`, then commit** `instances pickle by value; rebuild refuses a stale class (Q165)`.

### Task 11: export, `graph_version`, `diff` and drawing (Q95 amended)

**Files:**

- Modify: `python/fnloom/_export.py`, `python/fnloom/_drawing.py` (only if a test shows a need), `docs/quirk-register.md`
- Test: `tests/test_export.py`, `tests/test_drawing.py`, `tests/test_instances.py`

**Interfaces:**

- Produces:
    - A method node's export entry gains `"class": "<module>.<qualname>"`; its `version` is `method_version`'s.
    - The format number after the current one for a graph with method nodes (call 6); every other graph is unchanged,
      byte for byte.
    - `graph_version` hashes each method node's class name, never its instance's fingerprint.
    - `diff` reads both formats.
- [ ] **Step 1: Write the failing tests:**
    - `test_a_method_nodes_export_names_its_class`;
    - `test_a_graph_without_instances_exports_as_before`, against the golden files;
    - `test_graph_version_ignores_instance_state`: two clients, one graph version;
    - `test_graph_version_changes_with_the_class`;
    - `test_diff_reads_both_formats`;
    - `test_a_method_node_draws_as_a_function_node`, in DOT, Mermaid and the builtin SVG, against goldens;
    - `test_a_style_can_select_on_the_instance`.
- [ ] **Step 2: Run them to see them fail.** Run `UV_NO_SYNC=1 uv run pytest tests/test_export.py tests/test_drawing.py
  -k "instance or method or class"`. Expected: no `class` key.
- [ ] **Step 3: Implement.** Amend Q95.
- [ ] **Step 4: Run the tests to see them pass**, plus `pytest tests/test_export.py tests/test_drawing.py
  tests/test_svg.py tests/test_cli.py`. Expected: PASS.
- [ ] **Step 5: Run `scripts/check.sh`, then commit** `instances in the export, graph_version and drawings`.

## PR Z: close-out

### Task 12: benchmarks, docs, the example, the roadmap and the design's change log

**Files:**

- Create: `tests/test_instances_example.py`, `benchmarks/test_instances.py`
- Modify: `benches/run.py`, `benches/generate.py`, `docs/writing-nodes.md`, `docs/caching.md`, `docs/running.md`,
  `docs/from-hamilton.md`, `docs/benchmarks.md`, `docs/index.md`, `README.md`, `CHANGELOG.md`,
  `docs/plans/2026-10-28-roadmap-ahead-of-hamilton.md`, `docs/design/2026-11-05-instance-nodes-design.md` (change log)

**Interfaces:** none new.

- [ ] **Step 1: Write the example as a test**, `test_the_sales_example_runs`: the design's §1 code with a fake client,
  two regions under two namespaces, a `recompute` loader, a pure `total`, a `MemoryCache`, and a warm run whose
  `explain_cache` shows `total` hit.
- [ ] **Step 2: Add the benchmark rows**: `method_chain_200` beside `function_chain_200` in `benches/run.py`, and a
  CodSpeed pair in `benchmarks/test_instances.py`. Check the design's §15 bounds: build within 10%, run within noise
  plus one bound-method call per node, the instruction-count gate unchanged.
- [ ] **Step 3: Write the docs.**
    - `writing-nodes.md`: "Nodes from an instance": what is collected, `self`, names, decorators, steps.
    - `caching.md`: "Methods and `self`": the three rules, `__fnloom_fingerprint__` returning `()`, `ignore=("self",)`,
      `recompute` for a client call, the build-time limit.
    - `running.md`: threads and an instance's methods; pickling an instance and its `__reduce__`.
    - `from-hamilton.md`: the client-as-input pattern and its fnloom replacement.
    - `tests/test_docs.py` runs every new snippet.
- [ ] **Step 4: Update the roadmap's Plan 13 entry** to "built", with the PRs, and the design's change log with every
  answer the maintainer gave to §13's questions.
- [ ] **Step 5: Run `scripts/check.sh`, then commit** `Plan 13 close-out: benchmarks, docs and the sales example`.
