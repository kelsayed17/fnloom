# fnloom Plan 9: the long tail

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Status:** built, merge pending; approved 2026-10-28, as drafted, with the recommended option for decision 1. Building on
`claude/plan-9-decisions`.

**Goal:** close the roadmap's step 8 by deciding each Hamilton feature the roadmap leaves to Plan 9: `@subdag` (with
`@parameterized_subdag`), `@does`, `@schema`, `@dataloader`, `@datasaver`, `@load_from`, `@save_to` and
`StaticValidator`. Not planned is the default. Each decision is a register row that names the fnloom way to get the
same result. The fnloom way is a runnable recipe in the docs, and a differential test shows that the recipe gives
Hamilton's values. When this plan is done:

- the roadmap's decorator row and its `StaticValidator` row say what was decided;
- [Coming from Hamilton](../from-hamilton.md)'s "Not there yet" list names only dynamic execution and graceful partial
  failure, which have their own design.

**Architecture:**

- Not-planned rows add no code: a recipe in the docs, a register row, and a `deliberate("Qn")` test.
- One small build is put up for decision 1: `bind=` on a namespaced `with_modules`. It is resolved in the scan, where
  namespace names are already resolved (Q74), and adds no Rust.

**Tech Stack:** as Plans 1–8. Python 3.14, ty strict, ruff ALL, rumdl, pytest, and `apache-hamilton==1.90.0` as the
oracle.

**Spec:**

- `docs/design/2026-10-26-fnloom-design.md` §2. Materializers and the plugin ecosystem are v1 non-goals.
- `docs/plans/2026-10-28-roadmap-ahead-of-hamilton.md`: the `@subdag … @save_to` decorator row, the `StaticValidator`
  execution row, and the `materialize` driver row.
- The oracle's behaviour, probed on 2026-10-28, is catalogued in the session notes as `hamilton-plan9-long-tail.md`
  and cited below as "H9.1" and so on. Tests re-probe whatever they rely on.

## Hamilton and fnloom on each point

| Point | Hamilton 1.90.0 | fnloom today |
|---|---|---|
| A module used twice, each copy fed its own data | `@subdag(module, inputs={...}, config={...})`: inner nodes named `<fn>.<name>`, the function body as the final node (H9.1) | `with_modules(m, namespace="eu")` (Q74). Inputs are never namespaced, so two copies read one input |
| One function's body replaced by another | `@does(fn, **argument_mapping)`, with no type check between the two (H9.7, H9.8) | A one-line function, or `fnloom.node(fn, bind=...)` |
| A frame's columns declared | `@schema.output(("a", "int"), ...)`: a string tag, checked by nothing (H9.9) | `@check_output_custom` checks at run; `@tag` records |
| Loader and saver metadata | `@dataloader()` / `@datasaver()`: tuple returns and tags for the UI and tracker (H9.11–H9.13) | Plain functions; `after_node` events carry the values |
| I/O by adapter | `@load_from.<fmt>`, `@save_to.<fmt>`, materializers (H9.14–H9.17) | Non-goal (design §2); plain loader nodes; `after_run` sees `RunEnd.results` (Q84) |
| Graph checks at build | `StaticValidator` adapter, `(bool, message)` per node and per graph, raising `ValidationException(Exception)` (H9.18, H9.19) | `Driver.nodes` after `build` (Q70) |

## Decisions for review

Each decision becomes a register row, starting at Q112. Change any before the build starts.

1. **`@subdag` and `@parameterized_subdag`: the decorators are not planned, but `bind=` is built (Q112).**
   *Recommended; the alternative is not planned at all.*
    - **Why:** namespaces (Q74) already give a module's nodes their own names, read a namespace's own nodes first,
      and select `@config` variants per namespace. The one thing `@subdag` does that fnloom can't is feed each copy its
      own input: an input is never namespaced, so `eu.net` and `us.net` both read `gross`. That is what `@subdag`'s
      `inputs=` is mostly for (H9.2).
    - **The build:** `Builder.with_modules(*modules, namespace="eu", bind={"gross": source("eu_gross")})`.
        - Each key is a name the namespace reads that none of its modules defines. Its binding is `source(...)`, read
          as a name outside the namespace, or `value(...)`, a constant.
        - It is refused without `namespace=`.
        - A key that no node of the namespace reads is a `ValueError` naming it. Hamilton silently drops an unused
          `source()` and makes an orphan node for an unused `value()` (H9.2).
        - A key the namespace defines itself is a `ValueError` too. Hamilton's error there says "defined more than
          once" (H9.2).
        - Bindings show in `Driver.nodes` as each reader's dependencies, are in the export and the cache key as a
          parameter's binding already is, and are carried through pickling, `copy` and `rebuild()`.
    - **The final node:** `@subdag`'s function body is an ordinary function reading the namespace's nodes by
      `source("eu.net")`, as [Namespaces](../writing-nodes.md#namespaces) shows today.
    - **Not planned:**
        - **`config=` per copy.** The fnloom way is a config key per copy (`eu_mode`, `us_mode`) read by
          `@config.when`. A different graph per copy can also run as its own driver inside a node.
        - **`@parameterized_subdag`.** The fnloom way is a loop of `with_modules` calls, one namespace each.
    - **Fixes Hamilton's subdag edges:**
        - A misspelled final-function parameter becomes a namespaced input (H9.2); in fnloom it is a missing-input
          error.
        - A namespaced input passed at run is silently ignored (H9.2); in fnloom an input name is never namespaced.
        - `value()` inside `config=` puts the wrapper object into the node (H9.3); fnloom has no such path.
    - **Alternative:** not planned. The fnloom way is then a glue module given under the same namespace, defining
      `def gross(eu_gross: float) -> float`. Its parameter reads the plain input, and the namespace's readers read the
      glue node. This works today, but needs one module per copy.
2. **`@does`: not planned (Q113).**
    - **The fnloom way:** call the function. `def total(a: int, b: int) -> int: return _sum_all(a, b)` names, types
      and caches the node as written. `fnloom.node(_sum_all, bind=...)` also makes the node from the function with no
      wrapper.
    - **Why:** `@does` saves one line and costs a type check. Hamilton never compares the two signatures, so a `str`
      comes back from a node typed `int` (H9.8). A public replacing function in the same module is a node too (H9.8).
      The empty-body check compares bytecode (H9.7).
3. **`@schema.output`: not planned (Q114).**
    - **The fnloom way:**
        - A check that runs: `@check_output_custom` with a function testing the frame's columns and dtypes, failing
          by default with a `ValidationError(ValueError)` (Q99–Q101).
        - Metadata only: `@tag(schema="a:int,b:float")`.
    - **Why:** Hamilton's decorator is a string tag checked by nothing at run. With no extensions loaded it refuses
      even `pd.DataFrame`, it never parses its field strings, and its only reader expects `:` where the tag writes `=`
      (H9.9). pandera's `check_output(schema=...)` (H9.10) belongs to the plugin non-goal.
4. **`@dataloader` and `@datasaver`: not planned (Q115).**
    - **The fnloom way:**
        - A loader is a plain function returning the data.
        - Metadata it wants recorded is a second output through `@extract_fields({"data": ..., "metadata": dict})`,
          or is logged by a hook from `after_node`'s value.
        - A saver is a node that writes and returns what it wrote.
        - `@tag(kind="loader")` marks either for `Driver.nodes` and drawings.
    - **Why:** the tags feed only the UI, the tracker and the MLflow plugin (H9.13), which are non-goals. Hamilton
      checks no return shape at run, so a list yields its first element (H9.11). Its saver refuses `dict[str, Any]`,
      and `@parameterize` can't stack on the loader (H9.12).
5. **`@load_from`, `@save_to` and materializers: not planned (Q116).**
    - **The fnloom way:**
        - **Loading:** a plain loader node per source, `@parameterize`d for several. Its value is type-checked on
          its edge, which Hamilton's isn't (H9.15).
        - **Saving:**
            - an `after_run` hook writing `RunEnd.results` (Q84);
            - or a saver node requested with the outputs, as a Hamilton saver node must be (H9.16).
        - **Choosing at run what to load or save:** a `@config.when` loader variant, or a hook given at build.
    - **Why:** materializers are a design §2 non-goal. Hamilton's versions are not ones to copy:
        - a loader silently shadows a node of the same name (H9.15);
        - the loader's declared type has its tuple backwards (H9.15);
        - two stacked savers save each other's metadata (H9.16);
        - a lifecycle hook hides `materialize`'s real error behind `UnboundLocalError` (H9.17).
6. **`StaticValidator`: not planned (Q117).**
    - **The fnloom way:** a function over `Driver.nodes` that collects every problem, run right after `build()`:

        ```python
        def checked(driver):
            problems = [
                f"{name}: no owner tag"
                for name, n in driver.nodes.items()
                if not n.is_input and "owner" not in n.tags
            ]
            if problems:
                raise ValueError("; ".join(sorted(problems)))
            return driver
        ```

        It sees what a `StaticValidator` sees: names, types, tags, dependencies, origins and config. It raises a
        `ValueError` listing every culprit.
    - **Why:** a builder setting would only move one call. Hamilton's version has these problems:
        - A class given instead of an instance is accepted and never runs (H9.18).
        - A `None` message crashes with a `TypeError` (H9.19).
        - Graph checks are skipped once a node check fails (H9.19).
        - It counts nodes, not errors (H9.19).
        - It checks inputs as if they were nodes (H9.19).
    - **Reconsider** if more than one user asks for graph checks carried through `rebuild()`. That is the one thing a
      builder setting would add.
7. **Q110, a known limitation (no behaviour change).** While a resolver runs, `@mutate` is refused, so a resolver
   that imports a module defining a `@mutate` is refused too. The fnloom way is to import that module at the top of
   the file, outside the resolver. A test pins the refusal.

## Global Constraints

- **Earlier plans' constraints:** every Global Constraint of Plans 1–8 applies unchanged.
    - Python 3.14 and 3.14t.
    - One commit per task, with `scripts/check.sh` green.
    - The author email and the `Co-Authored-By:` trailer.
    - `maturin develop --features test-hooks`, and `UV_NO_SYNC=1`.
- **No copying, no new dependency.** Hamilton is the test oracle only.
- **Register rows come first.** Each row exists before the `deliberate("Qn")` test that cites it. Rows start at Q112.
- **Every recipe runs.** Each recipe in the docs is a code block that `tests/test_docs.py` executes. Each Q113–Q117
  test asserts Hamilton's behaviour (the decorator's graph and values) and that fnloom's recipe gives the same
  values.
- **The undecorated path is untouched.** `bind=` costs nothing when not given, and `benches.run` stays within noise.
- **Determinism.** Errors list every culprit, sorted, on any `PYTHONHASHSEED`.

## Review Focus

1. **`bind=` with two namespaces of one module.** `eu.net` reads `eu_gross` and `us.net` reads `value(5.0)`.
   `inputs()` lists `eu_gross` and not `gross`. Each value matches Hamilton's two `@subdag`s with `inputs=`. (Task 2)
2. **An unused or self-defined `bind=` key** is refused, naming the key and the namespace. (Task 2)
3. **Each not-planned recipe gives Hamilton's values** for the probed cases. (Task 1)

## File Structure

```text
python/fnloom/driver.py       with_modules(..., bind=); carried in settings          (Task 2, if decision 1 stands)
python/fnloom/_scan.py        namespace reads resolved through bind=                 (Task 2)
tests/test_namespaces.py      bind= tests                                            (Task 2)
tests/differential/test_deliberate_differences.py   Q112-Q117
docs/from-hamilton.md         "Hamilton features fnloom leaves out": one recipe each
docs/writing-nodes.md         Namespaces: bind=
docs/quirk-register.md        Q112-Q117; Q110's limitation
docs/plans/2026-10-28-roadmap-ahead-of-hamilton.md, README.md, docs/index.md, CHANGELOG.md
```

---

### Task 1: Not-planned rows and their recipes

- [ ] Register rows Q113–Q117, the Q110 limitation, and Q112's not-planned parts.
- [ ] Recipes in `docs/from-hamilton.md`, one section per decision:
    - the glue-module and `source()` final-node forms of `@subdag`;
    - `@does` as a call;
    - a column check with `@check_output_custom`;
    - a loader with metadata through `@extract_fields`;
    - a JSON loader node and an `after_run` saver hook;
    - the `checked(driver)` function.
- [ ] `deliberate("Q113")` to `("Q117")`: Hamilton's decorator, probed, against the recipe's values. A test of the
  Q110 import refusal.
- [ ] Check green; commit.

### Task 2: `bind=` on a namespaced `with_modules` (only if decision 1 stands)

- [ ] Register row Q112.
- [ ] Failing tests:
    - Review Focus 1 and 2;
    - `source()` reading a namespaced node by its full name;
    - `value()` constants in the cache key and the export;
    - `validate`, `inputs()` and drawings;
    - pickling, `copy` and `rebuild()`;
    - a `@parameterize` row and `@inject` inside the namespace, reading a bound name;
    - with no `namespace=`, a `TypeError`.
- [ ] The setting, and the scan's name resolution.
- [ ] `deliberate("Q112")` against two Hamilton `@subdag`s with `inputs=`, including H9.2's misspelled parameter.
- [ ] Check green; commit.

### Task 3: Ledger and docs

- [ ] The roadmap:
    - the decorator, `StaticValidator` and `materialize` rows;
    - step 8 marked done;
    - "Not there yet" in `from-hamilton.md`;
    - `README.md`, `docs/index.md` and `CHANGELOG.md`.
- [ ] As built.
- [ ] Check green; commit.

## As built

- **`bind=` is resolved in the scan and in step lowering**, through one `Within.read`: the namespace's own node first,
  then its binding. A plain function reading a bound name becomes one row binding nothing, so the row path's payload
  passes a constant, or a source two parameters share.
- **A `value()` binding is keyed by a canonical form.** `stable_fingerprint` (`python/fnloom/_canonical.py`, moved out
  of `_export.py`) is the fingerprint of a value with no set, and a hash of a canonical form otherwise. The cache key
  and `value_version` both use it, so a `frozenset` constant keys the same on every `PYTHONHASHSEED`. This also fixes
  the same value in any `value()` parameter, not only a bound one.
- **Pickling carries `bind=` as plain dicts**, since a `MappingProxyType` doesn't pickle.
- **Checks.** `with_modules` refuses a missing namespace, a binding that is no `source` or `value`, a key that is no
  identifier, and a key bound otherwise by an earlier call. `build()` refuses a key the namespace defines or no node
  reads, each listed, sorted.
- **Known follow-ups, unchanged:** `fingerprint` hashes dicts in insertion order, and `class_version` can't tell
  exec-defined classes apart.
