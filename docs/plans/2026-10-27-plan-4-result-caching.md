# fnloom Plan 4: result caching

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Result caching that never returns a wrong value for a pure node, costs a hash and a lookup when it hits,
and loads only what a run needs. It comes with an in-memory store and an on-disk store, per-node policies, and a way to
explain every outcome.

**Architecture:** Caching is a layer in the Python front end and the PyO3 binding; the core does not change. At build,
the front end computes each node's code version from code objects and folds it with the node's identity into a
static key part. Per run:

1. Inputs and overrides are fingerprinted, in Rust for the built-in types.
2. Keys chain down the plan (Merkle style). `recompute` nodes run first, so their results can be fingerprinted.
3. A backward walk from the outputs finds the hits.
4. The run goes through the existing core with the hits as overrides.
5. A per-node value callback stores what ran.

**Tech Stack:** as Plan 2, plus the `blake3` crate in `fnloom-py`. hypothesis joins the dev group for the property
test. numpy and pandas, already in the dev environment through Hamilton, are used only when the user's values are
of their types.

**Spec:** `docs/design/2026-10-27-caching-design.md`, approved by the maintainer on 2026-10-27; section numbers below
are its own. The main design is `docs/design/2026-10-26-fnloom-design.md`. Plan 2 is
`docs/plans/2026-10-27-plan-2-decorators-inspection-type-checks.md`, and its "Plan 3" (config, `extract_*`, async)
is still unwritten. This plan does not depend on it, so it is Plan 4.

**Out of this plan:** the observer hook that carries each node's cache outcome (spec §6 leaves its shape to the hooks
plan); `explain_cache` reports the same outcomes now. Remote stores (spec §2).

**Base:** `main` after PR #4 (Plan 2), whose `Plan::run_by_id`, `renames` and plan cache this plan uses.

## Global Constraints

- Every Global Constraint of Plans 1 and 2 applies unchanged: licence, naming, Python 3.14 and 3.14t, ty strict,
  ruff ALL, Clippy pedantic, `#![deny(unsafe_code)]` in the binding, one commit per task, `scripts/check.sh` green on
  every commit, noreply author with a `Co-Authored-By:` trailer, nothing consumer-specific, no Hamilton code copied.
- **No new runtime Python dependency.** `python/fnloom` never imports numpy, pandas or polars. It recognises their
  values by `type(value).__module__` and uses the modules only when the value's module is already in `sys.modules`.
- **The core crate does not change** (spec §8). New Rust code lives in `crates/fnloom-py`.
- **A hit is never wrong** (spec §2, goal 1). When in doubt, a node runs and the reason is recorded, never guessed.
- **The cache never fails a run** (spec §2, anti-goals). A store error, a corrupt entry, an unpicklable result or a
  full disk is a miss or a skipped write, reported through `warnings.warn` with `fnloom.CacheWarning`. A node's own
  exception still fails the run, as without the cache.
- **Nothing per run depends only on names** (Q3, Q16). Code versions, static key parts, policies and the key plan are
  made once, at build or per prepared plan.
- Every difference from Hamilton is a quirk-register row with a `deliberate("Q<n>")` test. New rows start at Q46.

## Review Focus

1. **Float edge values and array layouts.** `-0.0` and `0.0` are different fingerprints, and every NaN is the same.
   A non-contiguous numpy view fingerprints like its C-ordered copy. A pandas `object` column is fingerprinted element
   by element. Task 1 owns the tests.
2. **A node that raises.** Nothing is stored for it, and the next run tries it again. A store holding entries from a
   successful run is not touched by a failed one. Task 6.
3. **Overrides beside hits.** A user override is still type-checked, and so is an override of a node that would hit;
   a hit's value is not checked again. The override's fingerprint is its version downstream. Task 6.
4. **Store failures.** If `DiskCache` cannot write (read-only directory, unpicklable value), the run returns the right
   result and warns once per node per run. Task 4 tests the store, Task 6 the run.
5. **Threads sharing one driver and one store.** Two threads running the same prepared plan with the cache on both
   get correct results, and the store holds one whole entry per key. Task 6, also on 3.14t in CI.

---

### Task 1: Fingerprints

**Files:**

- Create: `crates/fnloom-py/src/fingerprint.rs`, `python/fnloom/_fingerprint.py`, `tests/test_fingerprint.py`
- Modify: `crates/fnloom-py/Cargo.toml` (`blake3 = "1"`), `crates/fnloom-py/src/lib.rs`, `python/fnloom/_core.pyi`,
  `python/fnloom/__init__.py`

**Interfaces:**

- Produces:
    - `_core.fingerprint(value: object, fallback: Callable[[object], object | None]) -> bytes`, 32 bytes.
    - `_core.hash_parts(parts: Sequence[bytes]) -> bytes`, a 32-byte BLAKE3 of length-prefixed parts.
    - `fnloom.fingerprint(value: object) -> bytes`.
    - `fnloom.register_fingerprint[T](cls: type[T], fn: Callable[[T], object]) -> None`.
    - `fnloom.UnfingerprintableError(TypeError)`, with attributes `path: str` (like `"[3].model"`, empty at the top)
      and `type_name: str`, created in Rust with `create_exception!` and re-exported.

- [ ] **Step 1: Write the failing tests** in `tests/test_fingerprint.py`:
    - `test_type_is_part_of_the_fingerprint`: the 12 values `1, 1.0, True, "1", b"1", bytearray(b"1"), (1,), [1],
      {1}, frozenset({1}), {1: 1}, {"1": 1}` have 12 distinct fingerprints.
    - `test_equal_values_share_a_fingerprint`: `fingerprint([1, "a", (2.5, None)])` equals itself rebuilt.
      `fingerprint({2, 1}) == fingerprint({1, 2})`. A fingerprint taken in a subprocess with `PYTHONHASHSEED=1` equals
      one taken with seed 2.
    - `test_float_bits`: `fingerprint(0.0) != fingerprint(-0.0)`, and
      `fingerprint(float("nan")) == fingerprint(-float("nan"))`.
    - `test_dict_order_counts`: `fingerprint({"a": 1, "b": 2}) != fingerprint({"b": 2, "a": 1})`.
    - `test_stdlib_values`:
        - `datetime(2026, 1, 1, tzinfo=UTC)` differs from the naive one;
        - `Decimal("1.0") != Decimal("1.00")`;
        - `PurePosixPath("a") != PureWindowsPath("a")`;
        - an `Enum` member differs from its value;
        - a frozen dataclass `P(1)` differs from `Q(1)` with equal fields;
        - a named tuple differs from the plain tuple.
    - `test_unknown_type_is_refused_at_any_depth`: `fingerprint([1, {"k": object()}])` raises
      `UnfingerprintableError` with `path == '[1]["k"]'` and `type_name == "object"`.
    - `test_protocol_and_registry`: a class with `__fnloom_fingerprint__` returning `(self.x,)` fingerprints by `x`.
      After `register_fingerprint(Foreign, lambda f: f.v)`, `Foreign` instances fingerprint by `v`, and so do subclass
      instances. A more specific registration for a subclass wins.
    - `test_numpy_arrays` (`pytest.importorskip("numpy")`):
        - `zeros((3, 2))` differs from `zeros((2, 3))` and from `zeros((3, 2), dtype="float32")`;
        - `a[:, ::2]` equals `np.ascontiguousarray(a[:, ::2])`;
        - an `object` array of `[object()]` raises `UnfingerprintableError`.
    - `test_pandas_frames` (`pytest.importorskip("pandas")`):
        - `DataFrame({"x": [1, 2]})` differs from `{"y": [1, 2]}` and from the same frame with another index;
        - a Series differs from the same Series with a `name`;
        - an `object` column of strings fingerprints, and one holding `object()` raises `UnfingerprintableError`.
    - `test_polars_frames` (`pytest.importorskip("polars")`, so it skips here and runs where polars is installed):
      frames that differ only in column names differ.
    - `test_python_never_imports_numpy`: in a subprocess, `import fnloom; fnloom.fingerprint([1])` leaves `numpy` out of
      `sys.modules`.

- [ ] **Step 2: Run them**: `UV_NO_SYNC=1 uv run pytest tests/test_fingerprint.py -q`. Expected: fail with
  `ImportError: cannot import name 'fingerprint'`.

- [ ] **Step 3: Implement `fingerprint` in `crates/fnloom-py/src/fingerprint.rs`.** It walks the value. It checks
  exact types (`is_exact_instance_of`, so `bool` is not `int` and a named tuple is not a `tuple`), handles `None`,
  `bool`, `int` (by its signed little-endian bytes), `float` (by bit pattern, with NaN canonicalised), `complex`,
  `str` (UTF-8), `bytes`, `bytearray`, `tuple`, `list`, `dict` (in order), `set` and `frozenset` (sorted element
  digests), and any object exposing a C-contiguous buffer (by format, shape and bytes). For anything else it calls
  `fallback(value)`:
    - `None` back means "cannot be fingerprinted", which raises `UnfingerprintableError` with the path so far.
    - Any other return is a canonical form, and the walk fingerprints it in turn under a tag holding the value type's
      qualified name.

    Each node of the walk hashes a one-byte kind tag, then its parts, length-prefixed. `hash_parts` uses the same
    framing.

- [ ] **Step 4: Implement `_fingerprint.py`**: `fallback(value) -> object | None`, the registry
  (`register_fingerprint`, most specific class through the MRO), and the public `fingerprint(value)`, which calls
  `_core.fingerprint(value, fallback)`. The canonical forms are:
    - `datetime`, `date`, `time` and `timedelta` as `(isoformat, tzname)`;
    - `Decimal` as `as_tuple()`; `Fraction` as `(numerator, denominator)`; `UUID` as `bytes`;
    - `PurePath` as `(type(value).__name__, parts)`; an enum member as `(qualified name, value)`;
    - dataclasses as their field tuple; named tuples as `tuple(value)`;
    - a numpy `ndarray` as `(dtype.str, shape, np.ascontiguousarray(value))`, or, for the `object` dtype,
      `(shape, list(value.flat))`;
    - a pandas `DataFrame` as `(list(columns), [dtype.str per column], index, [column.to_numpy() per column])`, a
      `Series` as `(name, dtype.str, index, to_numpy())` and an `Index` as `(name, to_numpy())`;
    - a polars `DataFrame` as `(columns, [str(dtype) per column], [column.to_numpy() per column])`, and a `Series` as
      `(name, str(dtype), to_numpy())`;
    - then `__fnloom_fingerprint__()` and the registry.

    Detect numpy and pandas values by `type(value).__module__.split(".")[0]` and read the modules from `sys.modules`
    only.

- [ ] **Step 5: Run the tests**: same command. Expected: pass. Then `bash scripts/check.sh`: exit 0.

- [ ] **Step 6: Commit** `python: typed fingerprints for cache keys`.

### Task 2: Code versions

**Files:**

- Create: `python/fnloom/_codeversion.py`, `tests/test_codeversion.py`

**Interfaces:**

- Consumes: `fnloom.fingerprint`, `UnfingerprintableError`, `_core.hash_parts` (Task 1).

- Produces:
    - `code_version(fn: Callable[..., object]) -> bytes`, 32 bytes.
    - `UncacheableError(Exception)`, with `reason: str` (for example
      `"global 'RATES' of 'price' is a dict, which may change; give @cache(version=...)"`).
    - `is_user_module(module: ModuleType) -> bool`.
    - `distribution_version(module_name: str) -> str | None`.

- [ ] **Step 1: Write the failing tests** in `tests/test_codeversion.py`. Each builds modules with
  `helpers.make_module`, takes `code_version` of a function, rebuilds the module with one edit, and compares:
    - `test_editing_the_body_changes_it` changes it; `test_formatting_comments_and_docstrings_do_not` does not.
    - `test_a_helper_it_calls_counts`: `out` calls `helper` in the same module, then calls `helpers.f` in another user
      module. Editing either helper changes `out`'s version. Mutual recursion between two helpers terminates.
    - `test_constants_and_defaults_count`: changing a module-level `K = 1` it reads, a default `x: int = 1`, a
      keyword-only default or a closure cell's value changes it.
    - `test_library_versions_count`: a function calling `json.dumps` hashes `("json", <python version>)`. One calling a
      function of an installed distribution hashes that distribution's version. Patch `distribution_version` to check
      that a different version gives a different code version.
    - `test_mutable_globals_are_refused`: reading a module-level `list`, `dict`, `set` or plain object raises
      `UncacheableError`, whose `reason` names the global and the function.
    - `test_nested_code_counts`: editing a lambda or a comprehension inside the function changes it.
    - `test_a_user_class_counts_through_its_methods`: a function constructing a user class `Model` changes version when
      a method of `Model` is edited.
    - `test_stable_across_processes`: the same source gives the same version in two subprocesses with different
      `PYTHONHASHSEED`.

- [ ] **Step 2: Run them**: `UV_NO_SYNC=1 uv run pytest tests/test_codeversion.py -q`. Expected: fail on import.

- [ ] **Step 3: Implement `_codeversion.py`** per spec §4.
    - Hash the code object recursively: `co_code`, `co_consts` (nested code objects recursed, other constants
      fingerprinted), `co_names`, `co_varnames` and `co_freevars`, without line tables, file names or docstrings
      (drop a leading `str` constant when it is the docstring).
    - Then hash `__defaults__`, `__kwdefaults__`, closure cells and each name in `co_names` resolved in `__globals__`,
      following the rules of §4.
    - `is_user_module` is true when the module has a `__file__` outside every `sysconfig.get_paths()` path (`stdlib`,
      `platstdlib`, `purelib`, `platlib`) and is not in `sys.builtin_module_names`.
    - `distribution_version` maps a top-level module to its distribution through
      `importlib.metadata.packages_distributions()`, read once per process. It returns `None` for the standard library,
      which is versioned by `sys.version` instead.
    - Memoise per function object within one `code_version` call, and cut cycles with a placeholder for a function
      whose version is in progress.

- [ ] **Step 4: Run the tests**, then `bash scripts/check.sh`. Expected: pass, exit 0.

- [ ] **Step 5: Commit** `python: code versions from code objects, helpers and constants`.

### Task 3: The `@cache` decorator

**Files:**

- Modify: `python/fnloom/_decorators.py` (add `cache`, and a `CachePolicy` field on `Decorations`),
  `python/fnloom/__init__.py`
- Test: `tests/test_decorators.py`

**Interfaces:**

- Produces:
    - `fnloom.cache[F](*, behavior: Literal["default", "recompute", "disable"] = "default", ignore: Iterable[str] = (),
      version: str | None = None) -> Callable[[F], F]`.
    - `CachePolicy` (a frozen dataclass): `behavior`, `ignore: frozenset[str]`, `version: str | None`.
    - `Decorations.cache: CachePolicy | None`, which is `None` when the decorator is not used.

- [ ] **Step 1: Write the failing tests**:
    - `test_cache_records_a_policy_and_returns_the_function`: `cache(behavior="recompute")(f) is f`, and
      `decorations(f).cache == CachePolicy("recompute", frozenset(), None)`.
    - `test_cache_composes_with_parameterize_and_tag_in_either_order`.
    - `test_cache_errors`, each listing every culprit, sorted:
        - an unknown behavior is a `ValueError` naming the three allowed values;
        - an `ignore` name that is not a parameter is a `ValueError`;
        - applying `@cache` twice is a `ValueError`;
        - `version=""`, or a non-str `version`, is a `TypeError`.

- [ ] **Step 2: Run them**: `UV_NO_SYNC=1 uv run pytest tests/test_decorators.py -q -k cache`. Expected: fail.

- [ ] **Step 3: Implement it.** Store the metadata only. Check `ignore` against the signature as `parameterize` does,
  reading names without evaluating annotations.

- [ ] **Step 4: Run the tests**, then `bash scripts/check.sh`. Expected: pass.

- [ ] **Step 5: Commit** `python: cache decorator recording a per-node policy`.

### Task 4: Stores

**Files:**

- Create: `python/fnloom/cache.py` (public), `tests/test_stores.py`
  - As built: the module is the private `python/fnloom/_stores.py`, every store name re-exported from `fnloom`,
    since `fnloom.cache` is the decorator of Task 3 and a module of the same name would shadow it.
- Modify: `python/fnloom/__init__.py`

**Interfaces:**

- Produces, all re-exported from `fnloom`:
    - `class CacheStore(Protocol)`, with `get(key: bytes) -> object` (returning `MISSING` on a miss),
      `put(key: bytes, value: object) -> None` and `discard(key: bytes) -> None`.
    - `MISSING: Final`, a unique sentinel.
    - `class CacheWarning(UserWarning)`.
    - `class MemoryCache(max_entries: int = 100_000, max_bytes: int | None = None)`.
    - `class DiskCache(path: str | os.PathLike[str], *, max_bytes: int | None = None,
      dumps: Callable[[object], bytes] = pickle.dumps, loads: Callable[[bytes], object] = pickle.loads,
      serializer: str = "pickle")`, with `clear() -> None` and `prune(max_bytes: int) -> None`.

- [ ] **Step 1: Write the failing tests**:
    - `test_memory_round_trip_and_identity`: `put(k, v)` then `get(k) is v`, and an unknown key gives `MISSING`.
    - `test_memory_evicts_least_recently_used`: with `max_entries=2`, put a, put b, get a, put c. Then b is `MISSING`.
      With `max_bytes` set to just over two entries' `sys.getsizeof`, a third entry evicts the least recently used.
    - `test_memory_is_thread_safe`: 8 threads each `put`/`get` 10,000 keys with no error or lost entries below the
      bound.
    - `test_disk_round_trip_and_layout`: `put` writes exactly `<path>/v1/<hex[:2]>/<hex>`, and `get` returns an equal
      object.
    - `test_disk_write_is_atomic`: a `dumps` that raises halfway leaves no file and no temporary file. Two processes
      writing one key leave one whole entry.
    - `test_disk_bad_entries_are_misses`: a truncated file, random bytes, a header naming another Python version, and a
      header naming another serializer each give `MISSING`. Each removes the entry and warns `CacheWarning` once.
    - `test_disk_unwritable_is_a_warning`: a read-only directory or an unpicklable value makes `put` warn
      `CacheWarning` and return.
    - `test_disk_prune`: after 10 entries of known size, `prune(max_bytes)` keeps the most recently read ones within the
      bound. A `DiskCache(max_bytes=...)` prunes when it opens and after every 100 writes.

- [ ] **Step 2: Run them**: `UV_NO_SYNC=1 uv run pytest tests/test_stores.py -q`. Expected: fail on import.

- [ ] **Step 3: Implement the stores** per spec §7.
    - `MemoryCache`: an `OrderedDict` under one `threading.Lock`, held only for the dict operations.
    - `DiskCache` file format: the magic bytes `b"FNLC"`, a `u16` format version (`1`), then a length-prefixed UTF-8
      JSON header (`serializer`, `python`, `libraries`: the value's type's top-level module mapped to
      `distribution_version`), then the payload.
    - Writes go to `tempfile.NamedTemporaryFile(dir=<shard>, delete=False)`, then `os.replace`.
    - A `get` updates the file's access time with `os.utime`.

- [ ] **Step 4: Run the tests**, then `bash scripts/check.sh`. Expected: pass.

- [ ] **Step 5: Commit** `python: memory and disk cache stores`.

### Task 5: Binding support: key plans and the value callback

**Files:**

- Create: `crates/fnloom-py/src/keys.rs`
- Modify: `crates/fnloom-py/src/plan.rs`, `crates/fnloom-py/src/lib.rs`, `python/fnloom/_core.pyi`
- Test: `tests/test_binding.py`

**Interfaces:**

- Consumes: the framing of `_core.hash_parts` (Task 1).

- Produces:
    - `_core.KeyPlan(nodes: list[tuple[str, bytes | None, list[tuple[str, str | None, bytes | None]]]])`. Each entry is
      one computed node, in plan order: its name, its static key part (`None` when the node is uncacheable), and its
      key parameters in declaration order. A key parameter is its name and then either the name of the node or input
      it reads, or the version of a constant. Ignored parameters are left out.
    - `KeyPlan.keys(versions: dict[str, bytes | None]) -> dict[str, bytes | None]`. `versions` holds the version of
      every input, override and pre-run `recompute` node by name.
        - A computed node's key is `hash_parts([static, *for each param: name, version])`.
        - The version of a source that is absent from both `versions` and the computed nodes is the fixed marker
          `b"\x00absent"`.
        - A key is `None` when the static part, or any version it reads, is `None`.
    - `Plan.run(inputs, overrides, observers=(), on_value: Callable[[str, object], None] | None = None)`. The callback
      is called with each computed node's name and value right after that node succeeds, before downstream nodes run.
      An exception from it propagates like an observer's.

- [ ] **Step 1: Write the failing tests** in `tests/test_binding.py`:
    - `test_key_plan_chains_versions`: a three-node chain's keys change when the input version changes. A node reading
      an input with version `None` gets `None`, and so does everything downstream of it. Equal inputs give equal keys
      across two `KeyPlan` objects.
    - `test_key_plan_marks_absent_optional_inputs`: the key with the optional input absent differs from the key with it
      supplied as the fingerprint of `None`.
    - `test_on_value_sees_each_computed_node_once_in_order`: with the observers' order, and never an input or override.

- [ ] **Step 2: Run them**: `UV_NO_SYNC=1 uv run pytest tests/test_binding.py -q -k "key_plan or on_value"`.
  Expected: fail.

- [ ] **Step 3: Implement `KeyPlan`** in `keys.rs` (a frozen pyclass holding interned names and byte vectors). Add
  `on_value` to `Caller`, called after `function.call`.

- [ ] **Step 4: Run the tests**, then `bash scripts/check.sh`. Expected: pass, Clippy clean.

- [ ] **Step 5: Commit** `binding: key plans and a per-node value callback`.

### Task 6: Cached runs

**Files:**

- Create: `python/fnloom/_caching.py`, `tests/test_caching.py`
- Modify: `python/fnloom/driver.py` (`Builder.with_cache`, `Driver`, `PreparedPlan.run`), `python/fnloom/_scan.py`
  (static key parts at build)

**Interfaces:**

- Consumes: Tasks 1 to 5.

- Produces:
    - `Builder.with_cache(store: CacheStore) -> Builder`.
    - `_caching.NodeKey`, a frozen dataclass of `static: bytes | None`, `reason: str | None`, `behavior: str` and
      `params: tuple[tuple[str, str | None, bytes | None], ...]`, made once per node at build.
    - `_caching.CacheRun.run(inputs, overrides) -> dict[str, object]`, which `PreparedPlan.run` calls when the driver
      has a store.

- [ ] **Step 1: Write the failing tests** in `tests/test_caching.py`. Use a `CountingCache(MemoryCache)` that records
  `get` and `put`, and nodes that append to a call log.
    - `test_second_run_hits_and_calls_nothing`: chain `a -> b -> c`; run twice with equal inputs. The second run calls
      no node and returns the same result.
    - `test_a_hit_on_the_output_loads_only_the_output`: the second run makes exactly one `get` that returns a value,
      for `c`.
    - `test_changed_input_reruns_only_its_downstream`: a diamond with inputs `x` and `y`. Changing `y` reruns only the
      nodes downstream of `y`.
    - `test_two_nodes_with_equal_values_never_share_entries` (F1): `a() -> 1` and `b() -> "1"` keep their own types
      across runs.
    - `test_recompute_runs_every_time_and_keys_downstream_by_result` (F5): a `recompute` loader reads a file through a
      path input. Editing the file reruns the loader and its downstream. Leaving it unchanged reruns the loader only.
    - `test_disable_never_stores_and_reruns_downstream`: no `put` for the node, and its consumers run every time,
      unless the consumer has `@cache(ignore=("client",))`. Then the consumer hits.
    - `test_uncacheable_inputs_and_globals_run`: an input of an unknown type, or a node reading a mutable global, runs
      every time and returns the right value.
    - `test_overrides`: an override is type-checked as before, its fingerprint keys its downstream, and a hit is not
      type-checked again.
    - `test_a_failing_node_stores_nothing`: the next run calls it again (Review Focus 2).
    - `test_store_failures_never_fail_the_run`: with a `DiskCache` in a read-only directory, the run returns the right
      value and warns (Review Focus 4).
    - `test_threads_share_a_plan_and_a_store`: 8 threads, 200 runs each, over shuffled inputs, all equal to an uncached
      driver's results (Review Focus 5).
    - `test_rows_and_value_constants_are_keyed`: two `@parameterize` rows of one function never share entries.
      Changing a `value()` constant misses.

- [ ] **Step 2: Run them**: `UV_NO_SYNC=1 uv run pytest tests/test_caching.py -q`. Expected: fail.

- [ ] **Step 3: Static key parts at build.** In `Builder.build`, when a store is set, give each node its `NodeKey`.
    - `static` is `hash_parts([b"fnloom-cache-1", name, module, qualname, code version, behavior])`.
    - `code version` is `version.encode()` when the policy gives one.
    - The `params` are the node's parameters minus the ignored ones. A `Value` binding enters as the fingerprint of its
      constant.
    - An `UncacheableError` or `UnfingerprintableError` sets `static=None` and `reason`. `disable` sets `static=None`
      with the reason `"policy disable"`.

- [ ] **Step 4: Implement `CacheRun`** (spec §3), one per `PreparedPlan`, holding a `KeyPlan` over the plan's
  computed nodes and an LRU of core plans keyed by the hit set. `run`:
    1. Run the user-facing checks as `PreparedPlan.run` does now.
    2. Fingerprint every input and override the plan reads. An `UnfingerprintableError` gives version `None`.
    3. If the plan contains `recompute` nodes, run a nested `CacheRun` whose outputs are those nodes. Its results
       become overrides of this run, with their fingerprints as their versions.
    4. Compute `keys = key_plan.keys(versions)`.
    5. Walk back from the outputs. A node with a key and the `default` behavior whose `store.get(key)` is not `MISSING`
       is a hit, and the walk stops there. Any other computed node is needed, and the walk continues into its sources.
    6. Prepare the core plan for (outputs, input names, user overrides plus hits plus recompute nodes), cached by that
       set, and run it with the hit values as extra overrides and `on_value` set to a function that `put`s each value
       whose key is not `None` and whose behavior is not `disable`.
    7. Return the outputs in requested order.

    Store errors in `get` and `put` are caught, warned as `CacheWarning` and treated as a miss or a skipped write.

- [ ] **Step 5: Run the tests**, then the whole suite and `bash scripts/check.sh`. Expected: pass. A driver without
  `with_cache` is unchanged, and its per-run benchmark numbers stay within noise.

- [ ] **Step 6: Commit** `python: cached runs with keys, hit frontiers and policies`.

### Task 7: `explain_cache`

**Files:**

- Modify: `python/fnloom/_caching.py`, `python/fnloom/driver.py`, `python/fnloom/__init__.py`
- Test: `tests/test_caching.py`

**Interfaces:**

- Produces:
    - `fnloom.CacheOutcome`, a `NamedTuple` of
      `status: Literal["hit", "miss", "uncacheable", "unknown"]` and `reason: str | None`.
    - `Driver.explain_cache(final_vars, *, inputs=None, overrides=None) -> dict[str, CacheOutcome]`. It covers every
      computed node of the plan, in name order, and runs nothing. A driver without a store raises `ValueError`.

- [ ] **Step 1: Write the failing tests**:
    - `test_explain_names_every_outcome_and_runs_nothing`. Over one graph, the call log stays empty and the outcomes
      are:
        - `hit` after a run;
        - `miss` (reason `None`) before it;
        - `uncacheable` with the reason naming the mutable global, the value path (`items[0]: Model`) or
            `policy disable`;
        - `unknown` with the reason `"downstream of recompute node 'load'"`.
    - `test_explain_without_a_store_is_a_value_error`.

- [ ] **Step 2: Run them**, expecting a failure. **Step 3: implement it** from steps 2, 4 and 5 of `CacheRun.run`,
  without the nested recompute run. **Step 4: run** the tests and `bash scripts/check.sh`.

- [ ] **Step 5: Commit** `python: explain_cache`.

### Task 8: Register rows and the property test

**Files:**

- Modify: `docs/quirk-register.md`, `tests/differential/test_deliberate_differences.py`, `pyproject.toml` (dev group:
  `hypothesis>=6.150`)
- Create: `tests/test_caching_property.py`

**Interfaces:**

- Consumes: everything above. On Hamilton's side, `hamilton.driver.Builder().with_cache(path=tmp_path)`.

- [ ] **Step 1: Write the `deliberate` tests**, each red against Hamilton 1.90.0 and green against fnloom, as in
  Plan 2:
    - **Q46 (F1, F2):** `a() -> 1`, `b() -> "1"`, run twice. Hamilton's second run gives `b == 1`; fnloom's gives `"1"`.
    - **Q47 (F3):** an input list holding a `__slots__` object whose field changes. Hamilton hits stale; fnloom reruns.
    - **Q48 (F4):** a node calling a helper that is edited between builds. Hamilton hits stale; fnloom reruns.
    - **Q49 (F6):** a node returning a counter value, run default, then recompute, then default. Hamilton's third run
      returns the first value; fnloom's returns the second.
    - **Q50 (F10, F11):** Hamilton's `ignore` on a producer of a `threading.Lock` raises `TypeError` on pickling.
      fnloom's `disable` producer with an `ignore` consumer runs and hits.
    - **Q51 (F12):** a truncated stored entry makes Hamilton's run raise; fnloom's recomputes and warns.
    - **Q16:** move it from DEFER to FIX, with text saying that code versions are made once at build.

- [ ] **Step 2: Add the register rows**, in the register's format, with fnloom's behaviour and the fault it fixes.

- [ ] **Step 3: Write `test_caching_property.py`** (spec §10). With hypothesis, generate graphs of 1 to 12 pure
  nodes, each a fixed arithmetic or string function of its parameters, and inputs over `int`, `float`, `str`,
  `tuple` and `None`. Then run a sequence of runs with random input edits and random `value()` and module-constant
  edits (rebuilding where code changes). Assert that every run with a `MemoryCache` and every run with a `DiskCache`
  over a `tmp_path` equals the run of an uncached driver. Use 200 examples, with `deadline=None`.

- [ ] **Step 4: Run** `UV_NO_SYNC=1 uv run pytest tests/differential tests/test_caching_property.py -q`, then
  `bash scripts/check.sh`. Expected: pass.

- [ ] **Step 5: Commit** `tests: caching register rows and the equality property`.

### Task 9: Benchmarks and documentation

**Files:**

- Create: `benches/cache.py`, `docs/caching.md`

- Modify:
    - `docs/benchmarks.md`, `zensical.toml` (nav), `CHANGELOG.md`, `docs/from-hamilton.md` (caching leaves "Not there
      yet");
    - `docs/design/2026-10-26-fnloom-design.md`: caching leaves the §2 non-goals, and a change-log line points to the
      caching design;
    - `docs/design/2026-10-27-caching-design.md`: status becomes "approved; built in Plan 4".

- [ ] **Step 1: Write `benches/cache.py`**: a 200-node chain of trivial nodes and a 1,000-node layered graph. It
  times, as medians over fresh processes:
    - uncached;
    - cold and warm with `MemoryCache`;
    - cold and warm with `DiskCache` in a temporary directory;
    - Hamilton 1.90.0 with `with_cache`, cold and warm.

    It prints JSON lines like `benches.run`.

- [ ] **Step 2: Run it** on a release build and check the spec §8 targets:
    - at most 1 µs of overhead per memory hit;
    - a warm 200-node chain at most about twice the uncached run;
    - a cold run at most 1.5 times uncached with `MemoryCache`.

    A target that is missed is fixed before the task ends, or becomes an issue to open, named in the PR.

- [ ] **Step 3: Write `docs/caching.md`**:
    - how to turn caching on;
    - the purity contract, with `recompute` stated first;
    - the policies, `ignore` and `version=`;
    - what can be fingerprinted, and how to register more;
    - `explain_cache`;
    - the stores, the disk layout, and the pickle warning;
    - the known limit on rebound globals.

    Every Python block in it runs under `tests/test_docs.py`.

- [ ] **Step 4: Add a "Caching" section to `docs/benchmarks.md`** with the measured table, and the other edits listed
  under Files. Run `bash scripts/check.sh`. Expected: exit 0.

- [ ] **Step 5: Commit** `docs: caching guide and benchmarks`.
