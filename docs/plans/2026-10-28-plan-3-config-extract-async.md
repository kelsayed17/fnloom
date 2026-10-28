# fnloom Plan 3: config, extract and async

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Status:** done. Every task below was built and tested on the `claude/plan-3-config-extract-async` branch on
2026-10-28, one commit per task with its review's fixes on top. The plan is kept as written, as the record of what
was decided. Where the build went further, the register and the changelog say so: register row Q69 (an `async def`
`@parameterize` row is awaited, where Hamilton returns its coroutine); a `TypedDict`-typed input or field now
accepts any `dict`, shallowly, since before it accepted nothing (Q61); and an extracted field or column is keyed
by its extraction but never looked up or stored, so its check runs on every run.

**Goal:** the three Hamilton capabilities Plan 2 reserved, without Hamilton's problems: `Builder.with_config`,
`Driver.config` and `@config.when*` with `@config.default` and `name=`; `@extract_fields` and `@extract_columns`;
and an async driver with the sync driver's whole feature set. This plan closes the config, extract and async rows of
the roadmap's "Hamilton's problems" ledger.

**Architecture:** Config is resolved in the Python front end at build, before rows and extraction are lowered: a
config-decorated function either becomes its node or doesn't exist, and config itself never becomes a node, an
input or a value. `extract_*` lowers to a producer node plus one generated node per field or column, each a small
callable that reads its source by key and checks the value's type. The async driver is Python. It plans in Rust as
today, through `_core.Graph.prepare`, then walks the plan's steps on an event loop: async nodes become tasks, and
sync nodes run in a thread. The Rust core and binding don't change.

**Tech Stack:** as Plans 1, 2 and 4. Python 3.14 (`annotationlib`, `asyncio`), ty strict, ruff ALL, pytest,
`apache-hamilton==1.90.0` as the oracle. pandas is already in the venv as Hamilton's dependency, and is used only by
tests.

**Spec:** `docs/design/2026-10-26-fnloom-design.md` (§2 goals and anti-goals, §4 "Async", §5 v1 scope, §7 proof, §8
constraints); `docs/plans/2026-10-28-roadmap-ahead-of-hamilton.md` (the gap table and the ledger rows this plan
closes); register rows Q11, Q12, Q13 and Q32. The oracle's behaviour, probed on 2026-10-28, is catalogued in the
session notes as `hamilton-config-extract-async.md`. Rows of that catalogue are cited below as "H1.4a" and so on,
and tests re-probe whatever they rely on.

## Decisions for review

These are the calls this plan makes where Hamilton's behaviour is a problem. Each becomes a register row (Q56 on) in
the task that builds it. Change any of them before the build starts.

1. **Config shapes the graph and nothing else (Q56).** A config key is never a node, an input, a parameter value or
   an output. Hamilton makes every key an external node (H1.5a). A key silently replaces a function of the same name
   (H1.5g) and can't also be passed as an input (H1.5e). A parameter named like a config key is an ordinary input in
   fnloom. When it's missing, the missing-input error says "`mode` is a config key; config is not passed to nodes:
   supply it in `inputs=`".
2. **A missing key never matches (Q57).** Hamilton reads a missing key as `None`, so `when_not(mode="a")` matches an
   empty config (H1.2a–d). In fnloom a condition on a key the config doesn't have is false, whichever form it takes.
3. **A needed node with no variant is a build error (Q57).** A node can have no variant because none matches and
   there's no `@config.default`. Hamilton turns it into a required input and fails at execute (H1.4a, #1213). fnloom
   refuses at build if anything consumes the node: `no variant of 'foo' matches the config {'mode': 'c'}; its variants
   need mode == 'a' (foo__a), mode == 'b' (foo__b); add @config.default or a variant for it`. A node nothing consumes
   is simply absent, and requesting it names the same reason.
4. **`@config.default`** marks the variant used when no other variant of that node matches. There is at most one
   per node.
5. **Two matching variants are a build error naming both functions (Q58).** Hamilton names the wrong one (H1.4e).
   Python redefinition of a config-decorated function in one module is also refused at build (Q58): Hamilton keeps
   the last definition silently (H1.4h). Redefinition is found by reading the module's source with `ast`, once per
   build, and only for modules that have a config-decorated function. A module without source skips the check.
6. **Misuse is refused when decorating (Q59).** That covers `@config.when` without parentheses (Hamilton silently
   drops the node, H1.3e), a positional string as the name (H1.3b), `when_in`/`when_not_in` given a `str`, `bytes`
   or non-iterable (H1.2j–k), a function name ending in `__` (H1.1c), and no conditions on `when*`.
    - `name=` is the node name on all four forms. Hamilton's `when_not_in` treats `name` as a config key (H1.3d).
    - A condition on a key literally called `name` goes in a positional mapping: `config.when({"name": "x"})`.
7. **Kept from Hamilton, with differential tests:**
    - The node's name is the function's attribute name cut at the last `__` (H1.1b).
    - `when` with several keys is an AND of equalities, and `when_not` with several keys is an AND of
      not-equals (H1.2e, H1.2g).
    - Values compare with `==` (H1.2h–i).
    - Stacked `@config.when`s AND together (H1.2m).
    - Decorator order never matters.
8. **No mutation, no magic tag (Q60).** fnloom never changes the user's function (H1.1d) and adds no
   `hamilton.config` tag (H1.1e). `NodeView` gains `config`, the conditions that selected the node.
9. **Config is copied at `with_config`, and `Driver.config` is a read-only mapping.** Hamilton's legacy constructor
   aliases the caller's dict (H1.6c). A pickled driver carries its config (0.1.1 pickles a driver as its builder).
   Custom `@config(lambda ...)` resolvers aren't in this plan. Plan 9 decides on them.
10. **`extract_fields` checks what it promises (Q61).**
    - Each field's value is checked against its declared type on every run, with the input checks' matcher. The
      check is on even when input checks are off: it's the decorator's contract, and as cheap as an input check
      (H2.2d).
    - Declared field types are checked at build against a `TypedDict` or `dict[str, T]` return (H2.1j).
    - A field missing at run raises `ValueError` naming the node and the fields it did return (H2.2a raises
      `InvalidDecoratorException`).
    - Mixing a mapping with extra names is refused (H2.1e drops them silently).
    - A missing `TypedDict` field is a clear `ValueError` at decoration (H2.1h raises a raw `KeyError`).
    - `Mapping[str, T]` returns are accepted (H2.1o refuses them).
11. **`fill_with` never mutates the producer's value, and `None` is a valid fill (Q62).** H2.2h, H2.2i and H2.3g show
    Hamilton mutating the value, and H2.3h fills a column named by a tuple.
12. **`@tag` lands on the producer and on every extracted node (Q63).** Hamilton puts it on the fields only (H2.2j).
13. **`extract_columns` is generic (Q64).** It splits any value with `__getitem__`, so it needs no registry and no
    pandas extension (H2.3: with autoload off, Hamilton fails at import).
    - A column node's type comes from the producer's return type: `pandas.DataFrame` gives `pandas.Series`,
      `polars.DataFrame` gives `polars.Series`, and anything else gives `typing.Any`. The type is read by module and
      name without importing either library.
    - `fill_with` builds a full column for pandas and polars frames, and raises `TypeError` at run for any other
      type, naming it.
    - A missing column raises `ValueError` at run, naming the columns the producer did return.
14. **More than one `@parameterize` row under `extract_*` is refused at decoration,** as Hamilton refuses at build
    (H2.2l). One row works, and its producer takes the row's name.
15. **Async is `Builder.build_async()`, built synchronously (Q12),** and returns an `AsyncDriver` whose `execute` and
    whose `AsyncPreparedPlan.run` are coroutines.
    - The result is a dict in request order (Q11). Hamilton gives a DataFrame from the constructor (H3.3) and sorted
      keys from the builder (H3.6).
    - A sync `build()` of a graph with an `async def` node still refuses it (Q32), and the message now names
      `build_async()`.
16. **Sync nodes run off the event loop (Q65),** in `asyncio.to_thread`, so an awaiting sibling keeps running.
    Hamilton runs them inline (H3.7).
    - `build_async(sync_nodes="inline")` runs them on the loop for graphs of cheap nodes.
    - Generated `extract_*` nodes always run inline.
17. **A failing node cancels its running siblings and raises its own exception once,** noted with the node's name
    as the sync driver does (Q66). Hamilton leaves the siblings running (H3.8). With no `ExceptionGroup`, a caller's
    `except KeyError` works as it does on the sync driver.
18. **The async driver checks what the sync one checks (Q67).**
    - Input and override types, with the same errors (H3.9).
    - A missing input is the sync `ValueError` (H3.10 raises `NotImplementedError`).
    - An unsupplied input requested as an output behaves as on the sync driver (H3.11 raises `KeyError`).
19. **The async driver caches (Q68).** `with_cache` works with the same keys, stores and `explain_cache`. Hamilton
    fails at build (H3.18).

## Global Constraints

- Every Global Constraint of Plans 1, 2 and 4 applies unchanged:
    - Python 3.14 and 3.14t.
    - ty strict, ruff ALL, Clippy pedantic, rumdl.
    - One commit per task, with `scripts/check.sh` green on every commit.
    - Noreply author `4119211+kelsayed17@users.noreply.github.com`, with the `Co-Authored-By:` trailer.
- **Nothing consumer-specific** (design §8). Fixtures are invented for the behaviour they pin.
- **No Hamilton code copied** (design §8). Read `hamilton/function_modifiers/configuration.py`, `expanders.py` and
  `hamilton/async_driver.py` to learn behaviour, and write fnloom's independently.
- **No new runtime dependency.** pandas and polars are only ever read from `sys.modules` or the value's own type, and
  never imported by `python/fnloom`.
- **Every difference from Hamilton 1.90.0 gets its register row before the `deliberate("Qn")` test that cites it is
  collected.** New rows continue from Q56. Every kept behaviour gets a differential test in
  `tests/differential/test_semantics.py`.
- **A decorator returns the same object, typed as itself** (Q1). It records metadata only, and the order of stacked
  decorators never matters.
- Deterministic output: every listing is sorted, and every error lists all culprits, sorted, in one message.
- **Anything on the run path gets a benchmark:** extraction (Task 4) and async execution (Task 6).
- **No recursion over graph depth** in the async walk.

## Review Focus

Inputs the decisions imply that no obvious test covers. Each line has its test in the owning task.

1. A config value that is unhashable (a list) or a NumPy array compared with `==`. The match must be a plain `bool`:
   `==` returning an array raises `TypeError` naming the key, rather than taking the array's truthiness. (Task 2)
2. A variant module imported twice under two names, or a function aliased under a second attribute name. It's one
   variant, not a redefinition and not two matching variants. (Task 2)
3. An extracted field whose declared type is a `TypedDict`, a union, or `Any`. The run-time check accepts exactly
   what `value_matches` accepts for inputs. (Task 3)
4. An async node that never awaits and raises immediately, while a sync sibling sleeps in its thread. The run
   raises at once, without waiting for the thread. The thread's result is discarded, and the next run isn't affected.
   (Task 5)
5. An `AsyncDriver` shared by two concurrent `execute` calls on one loop, and by two threads each running its own
   loop. Results never mix, and the plan cache stays consistent. (Task 5)

## File Structure

```text
python/fnloom/
  __init__.py      + config, extract_fields, extract_columns, AsyncDriver, AsyncPreparedPlan
  _config.py       NEW  config.when/when_not/when_in/when_not_in/default; Condition; select(); redefinition check
  _extract.py      NEW  extract_fields, extract_columns; field and column payloads; column-type table; fill
  _async.py        NEW  AsyncDriver, AsyncPreparedPlan, the step walker, async cached runs
  _decorators.py   Decorations gains `config` and `extract`
  _scan.py         collect() takes the config; selection before rows; extraction lowered per row; async allowed
                   when building async
  _view.py         NodeView.config
  driver.py        Builder.with_config, Builder.build_async, Driver.config, config-key hint in missing inputs,
                   pickling carries config
  _caching.py      CacheRun split so the async walker reuses keys, frontier and stores
tests/
  test_config.py  test_extract.py  test_async.py                                           NEW
  differential/test_semantics.py, test_deliberate_differences.py                         extended
benches/run.py (+ an "extract" shape), benches/async_.py (NEW)
docs/writing-nodes.md (config, extract), running.md (async), from-hamilton.md, quirk-register.md, errors.md,
benchmarks.md, CHANGELOG.md, the roadmap's ledger
```

---

### Task 1: The config decorators

**Files:**

- Create: `python/fnloom/_config.py`, `tests/test_config.py`
- Modify: `python/fnloom/_decorators.py`, `python/fnloom/__init__.py`

**Interfaces:**

- Produces:
    - `@dataclass(frozen=True, slots=True) class Condition: kind: Literal["when", "when_not", "when_in",
      "when_not_in"]; key: str; values: tuple[object, ...]`. `when` and `when_not` hold one value.
    - `@dataclass(frozen=True, slots=True) class ConfigPolicy: conditions: tuple[Condition, ...]; default: bool;
      name: str | None`. Stacked decorators AND their conditions: they concatenate, sorted by `(key, kind)`.
    - `class config` is a namespace of static methods. Each of `when`, `when_not`, `when_in` and `when_not_in` has
      the signature `(conditions: Mapping[str, object] | None = None, /, *, name: str | None = None, **keys:
      object) -> Callable[[F], F]`. `config.default` is used bare (`@config.default`) and returns the function.
    - `Decorations.config: ConfigPolicy | None = None`.
- Decoration-time refusals (Q59), each its own test:
    - A callable as the positional argument: `TypeError: use @config.when(...), with parentheses`.
    - A positional non-mapping.
    - No conditions on `when*`.
    - A key given both in the mapping and as a keyword.
    - `when_in`/`when_not_in` with a `str`, `bytes` or non-iterable value: `TypeError` naming the key.
    - A name, from `name=` or the cut function name, that is empty, or a function name ending in `__`.
    - `@config.default` combined with a `when*` on the same function.
    - Applying `name=` twice with different names.
- [x] **Step 1: Write the failing tests** in `tests/test_config.py`:
    - `test_when_records_conditions_and_returns_the_function`: `config.when(mode="a")(f) is f` and
      `decorations(f).config == ConfigPolicy((Condition("when", "mode", ("a",)),), False, None)`.
    - `test_stacking_ands_in_either_order`: both orders give equal `decorations`.
    - `test_name_is_the_node_name_on_every_form`: covers `config.when_not_in(mode=["a"], name="x")`.
    - `test_a_key_called_name_goes_in_the_mapping`: `config.when({"name": "a"})` records a condition on `name`.
    - `test_misuse_is_refused_when_decorating`: one `pytest.raises` per refusal above, matching its message.
    - `test_default_records_and_excludes_when`.
    - `test_config_composes_with_parameterize_tag_and_cache`: each order gives equal `decorations`.
- [x] **Step 2: Run** `UV_NO_SYNC=1 uv run pytest tests/test_config.py -q`. Expected: fails to import.
- [x] **Step 3: Implement** `_config.py` and the `Decorations` field, and export `config` from `fnloom`.
- [x] **Step 4: Run** the tests, then `bash scripts/check.sh`. Expected: pass, exit 0.
- [x] **Step 5: Commit** `config: record @config.when* and @config.default`.

### Task 2: Config resolution at build

**Files:**

- Modify: `python/fnloom/_config.py`, `_scan.py`, `_view.py`, `driver.py`, `docs/quirk-register.md`,
  `docs/writing-nodes.md`, `docs/errors.md`, `docs/from-hamilton.md`
- Test: `tests/test_config.py`, `tests/differential/test_semantics.py`,
  `tests/differential/test_deliberate_differences.py`, `tests/test_pickling.py`

**Interfaces:**

- Consumes: `ConfigPolicy`, `Condition` (Task 1).
- Produces:
    - `Builder.with_config(config: Mapping[str, object]) -> Builder`. It merges into a copy, and a later key wins.
    - `Driver.config -> Mapping[str, object]`, a `MappingProxyType` over the builder's copy.
    - `_config.matches(policy: ConfigPolicy, config: Mapping[str, object]) -> bool`. A missing key is false. An
      `==` that returns a non-`bool` raises `TypeError` naming the key.
    - `_config.select(functions: Sequence[tuple[ModuleType, str, Callable[..., object]]], config) ->
      list[tuple[ModuleType, str, Callable[..., object]]]`. It returns one function per node name, refuses two
      matches, and applies the default.
    - `collect(modules, config: Mapping[str, object] = MappingProxyType({}))`.
    - `NodeView.config: tuple[Condition, ...]`.
    - `_config.unmatched(name) -> str`: the "no variant of ..." reason that `build` raises for a consumed node and
      `prepare` gives for a requested one.
- Register rows Q56, Q57, Q58, Q59 and Q60, from decisions 1–8, go in first, in this task's commit.
- [x] **Step 1: Write the failing tests:**
    - `tests/test_config.py`:
        - `test_the_matching_variant_is_the_node`: two variants of `foo` and a consumer `bar(foo)` give
          `bar == foo + 1` for mode a, and the other value for mode b.
        - `test_a_missing_key_never_matches`: `when_not(mode="a")` with `{}` gives no node.
        - `test_a_needed_node_without_a_variant_fails_the_build`: matches the exact message of decision 3.
        - `test_an_unneeded_node_without_a_variant_is_absent`: `prepare(["foo"])` raises `ValueError` with the
          same reason.
        - `test_default_is_used_only_when_nothing_else_matches`.
        - `test_two_matching_variants_name_both`: the message names both functions, sorted.
        - `test_a_redefined_variant_is_refused`: a module source with two `def foo__a` fails, naming the lines.
        - `test_an_aliased_variant_is_one_variant`: Review Focus 2.
        - `test_a_non_bool_comparison_names_the_key`: Review Focus 1, with an object whose `__eq__` returns a list.
        - `test_config_is_never_an_input`: a parameter `mode` with config `{"mode": "a"}` is a required input. The
          missing-input error carries the config-key hint, and `inputs={"mode": ...}` works.
        - `test_driver_config_is_a_read_only_copy`.
        - `test_node_view_lists_its_conditions`.
        - `test_config_selects_before_rows_and_tags`: a `@parameterize` under `@config.when` yields its rows only
          when the variant matches.
    - `tests/differential/test_semantics.py` (KEEP): the variant naming rule, `when` and `when_not` with two keys,
      `==` matching (`1 == True`), and stacked `when`s.
    - `tests/differential/test_deliberate_differences.py`: one `deliberate` test per row, Q56 to Q60, asserting
      Hamilton's side (from H1.5, H1.2a–d with H1.4a, H1.4e with H1.4h, H1.3 with H1.2j–k and H1.1c, and H1.1d–e)
      and fnloom's.
    - `tests/test_pickling.py`: `test_a_pickled_driver_keeps_its_config`.
- [x] **Step 2: Run** `UV_NO_SYNC=1 uv run pytest tests/test_config.py tests/differential tests/test_pickling.py -q`.
  Expected: the new tests fail.
- [x] **Step 3: Implement.**
    - In `collect`, gather each module's public functions, then call `select`, then lower the selected ones.
    - Lowering stays as Plan 2 built it, with the node name taken from `policy.name` or the cut attribute name.
    - The AST redefinition check reads `inspect.getsource(module)` and runs only when a module has a
      config-decorated function. It skips an `OSError` or `TypeError`.
    - `Builder.build` raises decision 3's error for any edge into an unmatched node, all culprits sorted.
- [x] **Step 4: Run** the tests, then `bash scripts/check.sh`. Expected: pass, exit 0. Then run
  `uv run python -m benches.run` for the `chain` and `rows` shapes at 1,000. Expected: build times within noise of
  `docs/benchmarks.md`, since a build without config skips selection.
- [x] **Step 5: Commit** `config: select variants at build; config never becomes a node`.

### Task 3: `@extract_fields`

**Files:**

- Create: `python/fnloom/_extract.py`, `tests/test_extract.py`
- Modify: `_decorators.py`, `_scan.py`, `__init__.py`, `docs/quirk-register.md`, `docs/writing-nodes.md`,
  `docs/from-hamilton.md`
- Test: `tests/differential/test_semantics.py`, `tests/differential/test_deliberate_differences.py`

**Interfaces:**

- Produces:
    - `extract_fields[F](fields: Mapping[str, object] | Iterable[str] | None = None, /, *names: str, fill_with:
      object = MISSING_FILL) -> Callable[[F], F]`. `MISSING_FILL` is a private sentinel, so `None` is a valid fill.
    - `@dataclass(frozen=True, slots=True) class Extraction: kind: Literal["fields", "columns"]; items:
      tuple[tuple[str, object | None, str | None], ...]; fill: object`. Each item is (name, declared type or `None`
      for "from the return type", doc).
    - `Decorations.extract: Extraction | None = None`.
    - `_extract.lower(producer: NodeDef, extraction: Extraction, *, check: bool) -> list[NodeDef]`. It returns the
      producer unchanged plus one `NodeDef` per item. Each generated payload is a callable reading `{producer.name:
      value}`, and its one core input is the producer.
- Register rows Q61, Q62 and Q63 go in first.
- [x] **Step 1: Write the failing tests** in `tests/test_extract.py`:
    - `test_mapping_form_makes_typed_fields_and_keeps_the_producer`.
    - `test_names_take_types_from_a_typed_dict_or_dict_value_type`: covers `TypedDict`, `dict[str, int]` and
      `Mapping[str, int]`.
    - `test_no_arguments_extracts_every_typed_dict_field`.
    - `test_declared_types_must_fit_the_return_type`: `{"a": str}` on `dict[str, int]` fails the build, naming
      `a`.
    - `test_mixing_a_mapping_and_names_is_refused`.
    - `test_a_missing_typed_dict_field_is_a_value_error`.
    - `test_a_field_of_the_wrong_type_fails_the_run`: a `ValueError` naming node, field, declared and actual types.
      It happens with input checks off too.
    - `test_a_missing_field_names_what_was_returned`.
    - `test_extra_keys_stay_on_the_producer`.
    - `test_fill_with_fills_without_mutating`: the producer returns a module-level dict, and after the run that dict
      is unchanged.
    - `test_none_is_a_fill_value`.
    - `test_tags_land_on_producer_and_fields`.
    - `test_one_parameterize_row_works_and_two_are_refused`.
    - `test_composes_with_config_in_either_order`.
    - `test_field_checks_match_input_checks`: Review Focus 3, field types `TypedDict`, `int | None` and `Any`.
    - Differential KEEP tests: the producer is kept under its own name, the field set and its types, and the
      decoration-time refusals Hamilton also makes (empty mapping, non-type values). Each must raise fnloom's own
      class at the same moment.
    - Differential `deliberate` tests for Q61, Q62 and Q63.
- [x] **Step 2: Run** `UV_NO_SYNC=1 uv run pytest tests/test_extract.py -q`. Expected: fails to import.
- [x] **Step 3: Implement** the decorator in `_extract.py` and lowering in `_scan._node_defs` after rows.
    - A field check uses `_types.plain_class` for the fast `isinstance` path, or `value_matches` otherwise, as
      `driver._checks` does for inputs.
    - Fill copies the mapping with `{**value, name: fill}` semantics: only the field node sees the fill, and the
      producer's value is untouched.
- [x] **Step 4: Run** the tests, then `bash scripts/check.sh`. Expected: pass, exit 0.
- [x] **Step 5: Commit** `extract: @extract_fields with checked fields`.

### Task 4: `@extract_columns`, and the extraction benchmark

**Files:**

- Modify: `python/fnloom/_extract.py`, `__init__.py`, `docs/quirk-register.md`, `docs/writing-nodes.md`,
  `docs/from-hamilton.md`, `benches/generate.py`, `benches/run.py`, `docs/benchmarks.md`
- Test: `tests/test_extract.py`, `tests/differential/*`, `tests/test_bench_generate.py`

**Interfaces:**

- Produces:
    - `extract_columns[F](*columns: str | tuple[str, str], fill_with: object = MISSING_FILL) -> Callable[[F], F]`.
      A `(name, doc)` tuple sets that node's doc.
    - `_extract.column_type(frame_type: object) -> object`. `pandas.DataFrame` gives `pandas.Series` and
      `polars.DataFrame` gives `polars.Series`, matched by `__module__` and `__qualname__` and resolved from
      `sys.modules`. Anything else gives `typing.Any`.
    - A new benchmark shape `extract` in `benches/generate.py`: one producer returning a dict of `n` fields, with
      `n` consumers.
- Register row Q64 goes in first. Decision 13 says what it records, against H2.3's autoload failure, the tuple fill
  bug and DataFrame-valued duplicates.
- [x] **Step 1: Write the failing tests:**
    - `test_columns_split_a_mapping`.
    - `test_columns_split_a_pandas_frame_into_series`: skipped without pandas, and checks `NodeView.type is
      pandas.Series`.
    - `test_a_list_argument_is_refused_with_the_star_hint`: the same advice Hamilton gives (H2.3c).
    - `test_a_missing_column_names_the_columns_returned`.
    - `test_fill_with_builds_a_full_column_without_mutating`: pandas, and polars where installed.
    - `test_fill_with_on_another_frame_type_names_it`.
    - `test_tuple_doc_form_sets_the_doc_and_fills_by_name`.
    - Differential KEEP tests with pandas: the column nodes, their values and the kept producer. Hamilton's side
      loads the pandas extension explicitly first.
    - The Q64 `deliberate` test.
    - `tests/test_bench_generate.py`: the `extract` shape's closed form.
- [x] **Step 2: Run** them. Expected: fail.
- [x] **Step 3: Implement** the decorator and the column payloads.
- [x] **Step 4: Run** `bash scripts/check.sh`, then `uv run python -m benches.run` for `extract` at 10, 100, 1,000
  and 10,000. Add the four rows to `docs/benchmarks.md`. Expected: fnloom with field checks on beats Hamilton's
  steady execute on every row. A row that doesn't is a bug to fix in this task.
- [x] **Step 5: Commit** `extract: generic @extract_columns; extraction benchmarks`.

### Task 5: The async driver

**Files:**

- Create: `python/fnloom/_async.py`, `tests/test_async.py`, `benches/async_.py`
- Modify: `_scan.py` (async functions allowed when building async; the Q32 message names `build_async()`),
  `driver.py` (`Builder.build_async`; the request checks shared with `Driver`), `__init__.py`, `_core.pyi` (none:
  `_core` is unchanged), `docs/quirk-register.md`, `docs/running.md`, `docs/from-hamilton.md`, `docs/benchmarks.md`
- Test: `tests/differential/*`

**Interfaces:**

- Consumes: `Driver`'s request validation and input checks, factored out of `Driver.prepare` into
  `driver._Request.check(final_vars, inputs, overrides) -> tuple[outputs, input_names, override_names]`, with
  behaviour unchanged.
- Produces:
    - `Builder.build_async(*, sync_nodes: Literal["thread", "inline"] = "thread") -> AsyncDriver`, which is
      synchronous.
    - `class AsyncDriver` has `config`, `nodes`, `node_names()`, `what_is_upstream_of()` and
      `what_is_downstream_of()` as on `Driver`, plus:
        - `async execute(final_vars, *, inputs=None, overrides=None) -> dict[str, object]`.
        - `prepare(final_vars, *, inputs=None, overrides=None) -> AsyncPreparedPlan`, which is sync.
        - `explain_cache(...)`, as on `Driver`.
    - `class AsyncPreparedPlan` has `async run(*, inputs=None, overrides=None) -> dict[str, object]`.
    - `_async.walk(plan: _core.Plan, nodes: Mapping[str, NodeDef], inputs, overrides, *, sync_nodes) ->
      dict[str, object]` is a coroutine.
- How the walk runs:
    - It reads `plan.steps()` once per prepared plan and precomputes each computed node's dependents and pending
      count.
    - A node starts when its count reaches zero. It awaits a coroutine function, and runs a sync function through
      `asyncio.to_thread`, or inline when `sync_nodes="inline"` or the node is a generated extract node.
    - On the first failure it cancels every running task, awaits them, adds the node note, and raises that
      exception.
    - The result is a dict in request order.
    - It holds no per-run state on the driver, so concurrent runs are independent (Review Focus 5).
- Register rows: Q11, Q12 and Q13 lose *Planned*, and their tests land here. Q65, Q66 and Q67 are new (decisions
  16–18).
- [x] **Step 1: Write the failing tests** in `tests/test_async.py` (with `asyncio.run` in plain tests, no plugin):
    - `test_build_is_sync_and_execute_returns_a_dict_in_request_order`.
    - `test_async_and_sync_nodes_mix`.
    - `test_sync_nodes_do_not_block_the_loop`: a sync node sleeping 0.2 s and an async ticker that must tick
      within 0.05 s.
    - `test_inline_sync_nodes_run_on_the_loop_thread`.
    - `test_a_failure_cancels_siblings_and_raises_the_node_exception`: the sibling's `CancelledError` is observed,
      and the raised class is the node's own, with the node note.
    - `test_a_failure_does_not_wait_for_a_sleeping_thread`: Review Focus 4.
    - `test_inputs_are_checked_like_the_sync_driver`, for types, missing, unknown and override types. Each pair of
      messages is equal to `Driver`'s.
    - `test_overrides_prune_upstream`.
    - `test_concurrent_runs_on_one_driver`: Review Focus 5, with `asyncio.gather` of 50 runs with different inputs
      and two threads each running a loop.
    - `test_prepared_plan_runs_again`.
    - `test_sync_build_refuses_async_nodes_naming_build_async`.
    - `test_config_and_extract_work_async`: an async producer under `extract_fields`.
    - A differential KEEP test against Hamilton's `AsyncDriver` with the dict result builder: the values for a
      mixed graph.
    - `deliberate` tests for Q11, Q12, Q13, Q65, Q66 and Q67. Q13 asserts that the 3.14 `DeprecationWarning` comes
      from Hamilton and not from fnloom.
- [x] **Step 2: Run** `UV_NO_SYNC=1 uv run pytest tests/test_async.py -q`. Expected: fail.
- [x] **Step 3: Implement** `_async.py` and `build_async`.
- [x] **Step 4: Run** `bash scripts/check.sh`. Then write `benches/async_.py`, which runs fnloom `AsyncDriver` against
  Hamilton's on the `chain` and `layered` shapes at 100 and 1,000, with all nodes async and with all nodes sync
  (fnloom in both `sync_nodes` modes). Add its table to `docs/benchmarks.md`. Expected: fnloom inline beats
  Hamilton everywhere. Thread mode's per-node cost is reported as it is, with the reason.
- [x] **Step 5: Commit** `async: AsyncDriver, built synchronously, with cancellation and checked inputs`.

### Task 6: Async cached runs, and closing the ledger

**Files:**

- Modify: `python/fnloom/_caching.py`, `_async.py`, `docs/caching.md`, `docs/quirk-register.md`,
  `docs/plans/2026-10-28-roadmap-ahead-of-hamilton.md`, `docs/from-hamilton.md`, `CHANGELOG.md`,
  `docs/design/2026-10-26-fnloom-design.md` (change log)
- Test: `tests/test_async.py`, `tests/test_caching_property.py`, `tests/differential/test_deliberate_differences.py`

**Interfaces:**

- Consumes: `CacheRun` (Plan 4 and its follow-up) and `_async.walk` (Task 5).
- Produces:
    - `CacheRun.begin(inputs, overrides) -> _Begun` holds the versions, keys and hits, and the core plan to run, with
      a recompute pre-run done synchronously, as today.
    - `CacheRun.finish(begun, name, value)` stores one result.
    - The sync `run` becomes `begin`, then `plan.run`, then the stores, with behaviour and benchmarks unchanged. The
      async walker calls `begin`, walks, and calls `finish` per node as it succeeds.
    - A plain `MemoryCache` keeps its native fast path on the sync driver. On the async driver its store calls go
      through Python: the walk is already Python.
- Register row Q68 goes in first.
- [x] **Step 1: Write the failing tests:**
    - `test_async_cached_run_hits_and_explains`.
    - `test_async_cached_failure_stores_nothing_for_the_failed_node`.
    - The hypothesis property in `tests/test_caching_property.py`, extended so that every store is also driven
      through `build_async()`, with results equal to the uncached sync driver's.
    - The `deliberate("Q68")` test (Hamilton's async `with_cache` fails at build, H3.18).
- [x] **Step 2: Run** them. Expected: fail.
- [x] **Step 3: Implement** the split and the async use.
- [x] **Step 4: Run** `bash scripts/check.sh` and `uv run python -m benches.cache`. Expected: the sync cache rows are
  within noise of the follow-up's figures in `docs/benchmarks.md`.
- [x] **Step 5: Close the ledger.**
    - Mark each config, extract and async row of the roadmap's "Hamilton's problems" fixed, with its Q-number.
    - Strike config, extract and async from `from-hamilton.md`'s "Not there yet".
    - Add the design change-log row for Plan 3's new items (`@config.default`, `name=` on all four forms,
      `sync_nodes=`).
    - Write the changelog entries.
    - Expected: no config, extract or async row of the ledger says **same** or **worse**.
- [x] **Step 6: Commit** `async: cached runs; close the config, extract and async ledger rows`.

## Self-review

- **Spec coverage:**
    - The roadmap's step 3 lists `with_config` (Task 2), `Driver.config` (Task 2), `@config.when*` with `default`
      and `name=` (Tasks 1–2), `@extract_fields` (Task 3), `@extract_columns` (Task 4) and the async driver (Tasks
      5–6).
    - Its config ledger rows: the missing key (Task 2), config keys as nodes (Task 2), `__suffix` and redefinition
      (Task 2), and stacking order (Tasks 1–3).
    - Its extract rows: Tasks 3 and 4.
    - Its async rows: `AsyncDriver` (Tasks 5–6), and the async half of "the async path checks no inputs" (Task 5).
      The adapter half of that row stays with Plan 6.
- **Left to other plans:**
    - Custom `@config(lambda)` resolvers go to Plan 9.
    - Hooks on the async driver go to Plan 6, which must give both drivers the same hooks.
    - Functions in `final_vars` (Q30) go to Plan 5, for both drivers.
