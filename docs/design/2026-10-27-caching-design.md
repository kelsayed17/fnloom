# fnloom — result caching, design

**Status:** approved by the maintainer, 2026-10-27; to be built in Plan 4
(`docs/plans/2026-10-27-plan-4-result-caching.md`). Written without a question round, at the maintainer's request,
from a study of Hamilton 1.90.0's cache (most faults reproduced against the installed wheel). Every call in this
document is the author's unless it says otherwise; §11 lists them so any can be changed. It extends the main design
(`2026-10-26-fnloom-design.md`), which lists caching as a v1 non-goal; building this moves it into scope.

## 1. Intent

A cache lets a run skip a node whose result is already known: same function, same arguments, so the same value. It
pays off in two places:

- **Across runs in one process**, as when a prepared plan runs once per incoming bar. Nodes that depend only on what
  did not change since the last bar (parameters, reference data, slow-moving inputs) are not run again.
- **Across processes**, as in a research pipeline rerun after an edit. Only the nodes the edit touches, and the nodes
  downstream of them, run again.

Hamilton has a cache, and it is the main reason to design this one carefully instead of copying its shape. In 1.90.0
it can return a **wrong value without any error**. For example:

- It stores results by a weak hash of the value, so `1` and `"1"` collide, as do two DataFrames that differ only in
  column names. One node can then get another node's stored result.
- It folds values it cannot hash into a constant when they are nested inside a container.
- It misses edits to helper functions and module constants.
- It keeps serving old file contents to loaders.
- After a forced recompute, the next normal run serves the old value again.

It is also slow. A fully cached run of 200 trivial nodes took 30 ms, against 0.6 ms with no cache. The faults are
F1 to F21 in the study; §9 maps each to its fix here.

## 2. Goals, non-goals, anti-goals

### Goals

1. **A hit is never wrong.** For a node that is a pure function of its parameters, a cache hit returns a value equal
   to what running the node would return. When fnloom cannot be sure of that, it runs the node, and says why.
2. **Cheap when it hits.** A hit costs a hash and a lookup, never a re-read of source code or a database commit. A
   fully cached run of trivial nodes costs no more than about twice an uncached one, so turning the cache on is never
   a large loss.
3. **Lazy.** A hit on a requested output loads that output only. Nothing upstream of it is loaded or run.
4. **Explainable.** For every node of a run, fnloom can say whether it hit, missed, or could not be cached, and the
   reason.
5. **Two tiers, one model.** An in-memory store for runs in one process and an on-disk store for runs across
   processes, with the same keys and the same rules.

### Non-goals (this design)

- Remote or shared network stores (object storage, Redis). The store interface leaves room for them.
- Deciding by itself whether a function reads external state (files, clocks, the network). That is declared (§6).
- Caching inside dynamic execution (`Parallelizable`/`Collect`), which fnloom does not have yet.
- Hamilton's cache API, store layout or files. There is no compatibility layer.

**Anti-goals.** The project fails, even if every test passes, if:

- a stored value can be returned for a different node, or for different arguments, than the one that produced it;
- an unrecognised value turns into a constant fingerprint, anywhere, at any depth;
- a corrupt or unreadable stored entry fails a run instead of being recomputed;
- turning the cache on makes a run of cheap nodes much slower than turning it off, as Hamilton's does.

## 3. The model

### Keys

Every node in a run gets a **key**, a 256-bit hash. A stored result is found by its key and nothing else, so two
nodes, or one node with two sets of arguments, can never share an entry (F1).

A node's key hashes, in a fixed, length-prefixed encoding (F9):

- the key schema version;
- the node's identity: its name, and its function's module and qualified name;
- its **code version** (§4);
- for each of its parameters, in declaration order: the parameter name, then either the **version** of the value
  that will be passed or a marker for an optional input that is not supplied (distinct from `None`).

A value's **version** depends on where it comes from:

- **An input or override:** the value's **fingerprint** (§5), computed once per run.
- **A computed node:** that node's key. Versions chain from the inputs down, like a Merkle tree. A computed value is
  never fingerprinted, so a large intermediate DataFrame costs nothing to version (F18). The chaining loses a hit
  only when two different computations happen to produce equal values. It never causes a wrong one.
- **A node marked `recompute`** (§6): the fingerprint of its result. Downstream nodes then hit exactly when that
  result is unchanged, which is what a loader needs (F5).
- **A `value()` constant of a `@parameterize` row:** its fingerprint, taken once at build.

Since keys depend only on code versions and input fingerprints, every key in a plan is known **before anything
runs**, with one exception: a node downstream of a `recompute` node is keyed by that node's result. That is what
makes lookups lazy (goal 3).

### A run with the cache on

1. Fingerprint the inputs and overrides, then compute the key of every node in the plan, in plan order. This runs in
   Rust over the prepared plan's steps. A `recompute` node runs first, with whatever it needs, under these same
   rules. The keys downstream of it are computed from its result.
2. Walk back from the requested outputs. A node whose key the store holds is a hit, and the walk stops there: its
   upstream is not needed. Each node the walk passes and does not hit must run.
3. Run the plan with the hits as **overrides**. An override already replaces a node and cuts its upstream, so the
   existing planner prunes the run and the existing plan cache keeps one plan per shape of hit set. Only the hit
   values on the frontier are loaded from the store. The values of hits are not type-checked again, since they were
   checked when they were produced.
4. Store each result that ran, under its key, unless its policy says otherwise (§6). Writes replace (F6).

A node that cannot be cached (§5, §6) still runs. It is reported, and every node downstream of it is uncacheable in
that run, unless the downstream parameter that reads it is declared `ignore` (§6).

## 4. Code versions

The code version answers: has anything this function's result depends on changed? Hamilton hashes the function's
source text on every run. That costs more than a hit saves (Q16), and it misses edits to helper functions and
constants (F4).

fnloom computes each code version **once, at build**, from code objects, not source text. Notebooks, where source
text is unavailable, work too. The code version of a function hashes:

- its code object: bytecode, constants (nested code objects recursively, so lambdas and comprehensions count), and
  names;
- its defaults and keyword-only defaults, by fingerprint;
- its closure cells, treated like globals;
- every global name the code reads, resolved in the function's globals at build:
    - **a function or class defined in the user's own code:** its code version, recursively, with cycles cut. A class
      counts through the functions in its body. User code means a module that is neither the standard library nor
      installed in site-packages.
    - **a module of the user's own code,** read by attribute (`helpers.f`): the code versions of the attributes whose
      names the code reads;
    - **a standard-library or installed function, class or module:** its qualified name and the version of the
      distribution that provides it, so a library upgrade changes the key;
    - **an immutable value** (`int`, `str`, `bytes`, `float`, `bool`, `None`, an enum member, or a tuple or frozenset of
      these): its fingerprint;
    - **anything else** (a list, dict or set, or any other object): the function cannot be cached. A mutable global may
      change between runs without fnloom seeing it, so caching such a function could return a wrong result. The build
      reports it, and `@cache(version=...)` lets the user vouch for it (§6).
- a `@parameterize` row's `value()` constants and its bindings.

**Known limit.** A global rebound after the build, by assignment from outside the function or a reload, is not seen
until the next build. The documentation says so. `Builder.build` is where code versions are read.

## 5. Fingerprints

A fingerprint is a hash of a value's **type and content**. Two values share one only if they are interchangeable for
any node. Each fingerprint starts with a tag for the value's exact type, so `1`, `1.0`, `True`, `"1"` and `b"1"` all
differ, as do `[1, 2]` and `(1, 2)` (F2).

**Built in:**

- `None`, `bool`, `int`, `float` (by bit pattern, so `-0.0` and `0.0` differ, with every NaN the same), `complex`,
  `str`, `bytes`, `bytearray`;
- `tuple` and `list`, by element and in order;
- `dict`, by entries in insertion order. Iteration order is visible to a node, so it is part of the fingerprint.
- `set` and `frozenset`, by sorted element fingerprints. A result that depends on a set's iteration order changes
  with the hash seed, so it is not a pure function of the set anyway.
- `datetime`, `date`, `time`, `timedelta` (with time zone), `Decimal`, `Fraction`, `UUID`, `PurePath` and its
  subclasses (by type and parts), enum members (by type and value);
- dataclass instances and named tuples, by type and fields;
- **numpy arrays** (when numpy is installed): dtype, shape, and the bytes in C order, read through the buffer
  protocol and hashed in Rust. An `object` dtype is fingerprinted element by element.
- **pandas and polars frames and series** (when installed): column names, dtypes, index and name, and the values,
  hashed column by column from their buffers in Rust (F18).

**Extensible.** A type can define `__fnloom_fingerprint__(self) -> bytes | tuple | ...`, which returns something that
can itself be fingerprinted. Or `fnloom.register_fingerprint(type, fn)` can register a function for a type the user
does not own. A registration matches the exact type and its subclasses, the most specific first.

**Everything else cannot be fingerprinted,** at any depth. A list holding one unrecognised object cannot be
fingerprinted. The value is never replaced by a constant (F3) and never by `repr` or `id`. The node reading it runs,
and the report names the value's type and where in the value it sits (`items[3]: Model`).

Hashing is BLAKE3, compiled into the extension, so it adds no Python dependency. Fingerprinting the built-in types,
arrays and frames runs in Rust. Registered functions and the fingerprint protocol call back into Python.

## 6. What the user writes

```python
driver = (
    fnloom.Builder()
    .with_modules(features)
    # Or fnloom.DiskCache(".fnloom-cache") to keep results across processes.
    .with_cache(fnloom.MemoryCache(max_entries=100_000))
    .build()
)
```

`with_cache(store)` caches every node of the driver under the default policy. A node changes its own policy with the
`@cache` decorator, which takes the same keyword arguments as Hamilton's but different values where Hamilton's are
wrong:

| Policy | Runs | Stored | Downstream sees it as |
|---|---|---|---|
| `@cache()` (the default) | on a miss | yes | its key |
| `@cache(behavior="recompute")` | always | yes | the fingerprint of its result |
| `@cache(behavior="disable")` | always | never | uncacheable, so downstream runs too |

- **`recompute`** is for a node that reads the outside world: a loader, a clock, a request. It always runs, so it is
  never stale. Downstream still hits when its result did not change. Its result must be fingerprintable, or
  downstream is uncacheable. Hamilton serves stale file contents here (F5).
- **`disable`** never stores and never versions the result. A client or a connection can be produced this way and
  is never pickled (F11).
- **`@cache(ignore=("client",))`** on a consumer says that this parameter does not affect the consumer's result. The
  parameter is left out of the consumer's key, so the consumer can hit although `client` is uncacheable. Hamilton's
  `ignore` lives on the producer and silently hides every dependency on it (F10). Here it is said where it is true,
  per consumer and per parameter.
- **`@cache(version="2")`** replaces the automatic code version with the user's own string. It is for a function the
  automatic one cannot cover, such as one reading a mutable global or a file next to it. The user bumps it by hand.

The decorator records metadata only, like `@tag`, and reads nothing at decoration time.

**Explaining.** `driver.explain_cache(final_vars, inputs=..., overrides=...)` computes the keys, consults the store,
runs nothing, and returns, for each node of the plan, `hit`, `miss` or `uncacheable` with the reason (the global, the
value path, or the policy). Observers get a `post_node` call that carries the same outcome. The design leaves the
exact hook shape to the hooks plan (main design §10). Hamilton's log reports a hit on every lookup, whatever the
outcome (F21).

## 7. Stores

Both stores implement one small interface: `get(key) -> value | MISSING`, `put(key, value)`, `discard(key)`. A store
is safe to share between threads, and between drivers in one process.

**`MemoryCache(max_entries=..., max_bytes=None)`.**

- Holds the result objects themselves, so a hit costs a lookup and no copy. A node that mutates a value it received
  already corrupts every other reader in the same run. With the cache on, that corruption also reaches later runs.
  The documentation says so plainly: cached nodes must not mutate their arguments (F7).
- Evicts the least recently used entries past either bound (F20). `max_bytes` is measured by `sys.getsizeof` on the
  top-level object only, a documented approximation.
- Safe under free-threaded Python. Per-key work never holds a lock that a node call needs.

**`DiskCache(path, max_bytes=None, serializer=pickle)`.**

- **Layout:** one file per key, at `<path>/v1/<first two hex digits>/<key in hex>`. There is no index database, so
  there is nothing to lock or corrupt. The key is the file name, so a lookup is one `open`.
- **Writes are atomic:** a temporary file in the same directory, then `os.replace`. Concurrent writers of one key
  leave one whole file, the last writer's (F12, F14).
- **Header:** each file starts with a header holding the cache format version, the serializer's name, the Python
  version, and the distribution versions of the value's type's module. An entry whose header does not match the
  running interpreter and libraries is a miss (F13).
- **Any failure to read an entry is a miss:** a truncated file, an unpickling error, a class that moved, a header
  mismatch. The node runs, and the bad entry is replaced and reported (F12). A run never fails because of the cache.
- The serializer is pluggable (`serializer=` takes `dumps`/`loads`). The default is pickle. Loading a pickle can run
  code, so the documentation says to keep the cache directory as private as the code itself.
- `max_bytes` triggers eviction of the oldest entries by access time when the store opens and every N writes.
  `DiskCache.clear()` and `.prune(max_bytes)` are explicit.

## 8. Where the work runs

- **Python front end, at build:** code versions (§4), decorator policies, and fingerprints of `value()` constants and
  defaults. Done once per build, never per run (Q16).
- **Rust binding, per run:** fingerprints of the built-in, array and frame types, key chaining over the plan's
  steps, the backward walk that finds the hit frontier, and the in-memory store. The core stays generic (main design
  §2, goal 7). Caching is a layer in `fnloom-py` and the Python front end, and the core needs nothing beyond what it
  has. The pruned run is an ordinary run with overrides, through the plan cache.
- **Python, per run, only when needed:** registered fingerprint functions, the fingerprint protocol, and the disk
  store's file I/O and serializer.

**Performance targets**, added to `docs/benchmarks.md` as rows against Hamilton's `with_cache` on the same shapes:

- in memory, a hit costs at most 1 µs of overhead per node;
- a fully cached run of a 200-node chain of trivial nodes costs at most about twice the same run uncached;
- a cold run (all misses) costs at most 1.5 times an uncached run, with the memory store;
- a requested output that hits loads one entry, whatever the size of the graph above it.

## 9. Hamilton's faults and what fixes them

| Fault (study) | Fix here |
|---|---|
| F1 results shared by content hash | stored by key, which names the node (§3) |
| F2 fingerprints ignore type and metadata | an exact-type tag and full structure (§5) |
| F3 nested unhashables become a constant | unfingerprintable at any depth means uncacheable, reported (§5) |
| F4 code version is the function's source only | code objects, helpers, constants, libraries, closures (§4) |
| F5 loaders serve stale files | `recompute`, keyed downstream by result fingerprint (§6) |
| F6 recompute doesn't replace the stored value | writes replace (§3, §7) |
| F7 in-memory store hands out mutable objects | documented contract; the same object is shared within a run already (§7) |
| F8 collect order ignored | not applicable until dynamic execution; then order-sensitive |
| F9 sanitised node names collide | length-prefixed identity with module and qualified name (§3) |
| F10 ignore hides real dependencies | `ignore` per consumer parameter (§6) |
| F11 ignore pickles clients | `disable` never stores (§6) |
| F12 corrupt entries crash | atomic writes; any read failure is a miss (§7) |
| F13 pickle tied to unversioned libraries | versions in the header; a mismatch is a miss (§7) |
| F14 concurrency | no index database; atomic replace; thread-safe stores (§7) |
| F15 doesn't compose with other wrappers or async | a layer outside the node call, with keys before the run, so async can reuse it (§8) |
| F16 file names Windows rejects | hex keys only (§7) |
| F17 per-node overhead dwarfs cheap nodes | code versions at build; keys in Rust; no per-node commit (§8) |
| F18 fingerprinting large data is slow | intermediates are never fingerprinted; buffers hashed in Rust (§3, §5) |
| F19 every intermediate on the path is loaded | hits become overrides, so only the frontier loads (§3) |
| F20 unbounded growth, random entries per run | LRU bounds; an uncacheable value is never stored under a random key (§5, §7) |
| F21 the log reports a hit on every lookup | `explain_cache` and observer outcomes (§6) |

Each fixed fault that Hamilton exhibits gets a quirk-register row, starting at Q46, and a `deliberate` differential
test, red against Hamilton 1.90.0 and green against fnloom, as the main design §6 requires. Q16 moves from DEFER to
FIX.

## 10. Proof

- **The equality property, generated.** For generated graphs (hypothesis) of pure nodes over generated inputs: a run
  with the cache on, cold and then warm, after random edits to inputs, constants and helper functions, always
  returns what a run with the cache off returns. This is the test that guards goal 1.
- **Fingerprint distinctness.** For pairs of values of the built-in types that are not interchangeable (the study's
  collision pairs, plus generated ones), the fingerprints differ. Equal values of the same type get the same
  fingerprint across processes and hash seeds.
- **Code-version sensitivity.** An edit to the function, a helper it calls, a constant it reads, a default, a
  `value()` constant or a library version changes the key. Formatting, comments and docstrings do not.
- **Robustness.** Truncated, corrupted and foreign-version entries are misses. Two processes writing one key leave a
  whole file. A free-threaded run with several threads sharing a store passes the equality property.
- **Differential rows.** One per Hamilton fault fixed, as §9 says.
- **Benchmarks.** The targets of §8, measured and published.

## 11. Calls made in this design (change any of them)

1. **Opt-in per driver, every node cached by default** (`with_cache`), with per-node policies. The alternative,
   opting in per node, is safer for code that reads files unannounced, but tedious for large graphs.
2. **Purity is declared, not detected.** A node that reads the outside world must say `recompute`. This is the one
   way left to get a stale result, and the documentation puts it first.
3. **Merkle keys.** A computed value's version is its key, not a fingerprint of the value. It loses a hit only when
   two different computations give equal values, and it makes intermediates free to version.
4. **Mutable globals make a function uncacheable**, unless the user gives `version=`. This is strict, because
   silently caching them can be wrong.
5. **Order-sensitive dicts, order-insensitive sets** (§5).
6. **No copies from the memory store.** This is for speed. Mutation is a documented contract violation.
7. **No index database on disk.** One file per key, atomic replace, and access-time eviction.
8. **Pickle by default**, behind a pluggable serializer, with version headers.
9. **Hits as overrides.** Pruning reuses the planner and the plan cache. In a bar loop where the hit set changes
   every bar, that compiles a plan per new shape. If this shows in the benchmarks, the binding gains a direct
   "run with these nodes available" path.
10. **BLAKE3 in Rust**, compiled into the extension.
11. **Keep Hamilton's names** (`with_cache`, `@cache(behavior=...)`, `recompute`, `disable`) for intent, and move
    `ignore` to the consumer, as a register row.

## 12. Left to the builder

- The exact byte encoding of keys and fingerprints, within §3 and §5.
- How "user code" is told apart from installed code, beyond the rule in §4 (a module outside the standard library
  and site-packages), including editable installs.
- The observer hook's exact shape, in step with the hooks plan.
- Eviction details: the access-time update on hits, and how often `max_bytes` is checked.
- Whether the memory store's LRU lives in Rust or Python, given the 1 µs-per-hit target.
- The order of the plan's tasks. A sensible one: fingerprints, then code versions, then keys and the memory store,
  then the hit frontier and pruning, then the disk store, then the policies and `explain_cache`, then the register
  rows and benchmarks.
