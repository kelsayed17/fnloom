# fnloom — dynamic execution, design

**Status:** approved by the maintainer 2026-10-29, with the open questions of §13 decided. Nothing here is built.
It designs Plans 10 and 11 of the [roadmap](../plans/2026-10-28-roadmap-ahead-of-hamilton.md): dynamic fan-out and
fan-in, a parallel executor, and a typed absent outcome, and it says which parts of the roadmap's "Beyond Hamilton" it
delivers and which it only leaves room for. Facts about Hamilton were read in the `apache-hamilton 1.90.0` wheel and
probed on Python 3.14 (§11). Every decision is a call this design makes, changeable (§13).

## 1. Intent

Three things a fnloom user can't do today:

1. **Run part of a graph once per item of a list that exists only at run time**, and gather the results: one branch
   per file, per ticker, per parameter set. Hamilton does this with `Parallelizable[T]` and `Collect[T]`, behind an
   "experimental" flag. In fnloom it is a Python loop around `execute`, which loses caching per item, hooks per item,
   and parallelism.
2. **Run independent nodes at the same time.** fnloom runs one node at a time on `Driver`, and concurrently only for
   `async def` nodes on `AsyncDriver`. Free-threaded CPython 3.14t lets Python threads run Python in parallel, and
   fnloom already builds and tests on it ([Threads](../running.md#threads)); nothing uses it yet.
3. **Let a node decline to produce a value.** "Don't compute this" must be encoded as data today, and Hamilton has
   the same hole (roadmap: "A node can't skip"). Without it, a fan-out over flaky I/O can only fail as a whole.

These three are one design because they meet: a branch that declines is how a fan-out returns partial results, and a
fan-out is the first place parallelism pays off.

## 2. Goals, non-goals, anti-goals

### Goals

1. **Fan-out and fan-in at least as good as Hamilton's**, by the roadmap's bar: same intent, kept behaviour proven
   differentially, every difference a register row with a test, typed, and no slower.
2. **One static graph, one reusable plan.** Expansion happens at run time inside a plan prepared once; `prepare`
   stays deterministic and its result reusable across runs and threads (design §2 goal 3).
3. **Results never depend on the number of workers or the timing of nodes.** One worker or sixty-four, the same
   inputs give the same values, in the same order, and the same cache keys.
4. **Parallel on 3.14t, correct on 3.14.** The same executor runs on both builds; on the GIL build it is still
   correct and still overlaps nodes that release the GIL (I/O, numpy).
5. **The Rust core decides what runs next**, so `Driver`, `AsyncDriver` and later Rust-native nodes share one
   scheduler and one set of rules.
6. **Typed absence**: a node may decline, the graph knows at build which nodes may be absent, and every consumer's
   handling of it is chosen by its own signature.

### Non-goals (this design)

- **Processes and clusters.** No multiprocessing, Ray or Dask executor. Threads on 3.14t are the target (roadmap);
  a `Driver` and a `PreparedPlan` already pickle, so a process pool over runs is the user's (design §10).
- **Interrupting a running Python node.** A thread can't be interrupted; cancellation stops what hasn't started
  (§6.5).
- **Streaming, per-node state and incremental execution.** Plan 10 leaves room for them (§9) and builds none.
- **Faster node bodies** (design §2).

### Anti-goals

The design fails, even if every test passes, if:

- a result, a `Collect` order or a cache key depends on worker count, scheduling or hash seed;
- the default (one worker) changes what any existing program does, observes or raises;
- a thread waits for a lock while attached to Python (holding the GIL on the GIL build), or holds a Rust lock across
  a call into Python other than the run's hook lock (§6.2);
- a behaviour differs from Hamilton without a register row and a test (design §2).

## 3. The decisions, in short

| # | Decision |
|---|---|
| D1 | Fan-out is a node annotated `-> Parallelizable[T]` returning any iterable; fan-in is a parameter annotated `Collect[T]`. Hamilton's names and spelling, with no experimental flag (§4.1). |
| D2 | The graph and plan are static. A **block** (the nodes between an expand and its collects) is found at build and compiled once into a sub-plan; a run instantiates it once per item (§4.2). |
| D3 | A branch is named by its **branch path**, the item's index at each level: `(3,)`, or `(3, 1)` when nested. `Collect` gives a `list` in branch order, never completion order (§4.3). |
| D4 | Blocks nest, to any depth, and work across namespaces and `bind=` (§4.4). |
| D5 | A branch node's cache key is its ordinary key with the item's fingerprint as the item's version: content-addressed, not position-addressed, so reordered and appended items hit (§4.6). |
| D6 | Parallelism is opt-in: `Builder.with_threads(max_workers=None, limits=None)`. The default stays one node at a time on the caller's thread (§6). |
| D7 | The Rust core gets a scheduler: a state machine that hands out ready steps in a fixed order and takes back outcomes. Threads, the GIL and event loops stay in the binding and the Python async driver (§6.1). |
| D8 | Hooks are serialized per run and run on the thread that ran the node; with one worker their order is exactly today's (§6.4). |
| D9 | A node may return `fnloom.ABSENT` when its annotation admits `fnloom.Absent`. Each consumer's signature decides: take it, use its default, or be skipped too. Absence is known at build, cached and drawn (§7). |
| D10 | `@fnloom.absent_on(*errors)` turns chosen errors into absence, and `Collect[T]` drops absent branches, so a fan-out returns partial results. This is fnloom's `GracefulErrorAdapter` (§7.5). |

## 4. Fan-out and fan-in

### 4.1 Spelling

```python
from fnloom import Collect, Parallelizable


def tickers(universe: str) -> Parallelizable[str]:
    yield from load_universe(universe)  # or: return [...]


def prices(tickers: str, day: date) -> float:  # runs once per ticker; `day` is shared
    return fetch(tickers, day)


def total(prices: Collect[float]) -> float:  # prices is a list[float], in ticker order
    return sum(prices)
```

- `Parallelizable[T]` and `Collect[T]` keep Hamilton's names and shape (design §5: same name, same intent), so a
  plain-function Hamilton program differs only in its import, and the differential tests share it.
- **No flag.** Hamilton needs `Builder().enable_dynamic_execution(allow_experimental_mode=True)` (§11). fnloom builds
  any graph that uses them.
- **Typed for checkers.** `Parallelizable[T]` is an alias of `Iterable[T]` and `Collect[T]` of `list[T]`, each with
  a marker the front end reads (`Annotated`, through a PEP 695 alias). A checker sees a generator or a list returned,
  and `len(prices)` and `prices[0]` type-check. Hamilton's `Collect` is an `Iterable` subclass, so neither does.
  The exact spelling is the builder's, under that requirement.
- **Any iterable**, not only a generator. It is consumed lazily and in order, one item per **pull** task (§6.1). A
  pull runs on whichever worker takes it, never two pulls of one iterator at once, and the scheduler hands one out
  only while the in-flight bound allows (§6.6), so a pull never blocks a worker. A branch starts as soon as its item
  arrives. When a run stops early (failure, timeout, cancellation), the thread that finishes the run calls the
  iterator's `close()`, so a generator's `finally` runs.
- On the async driver, an `async def` expand node may be an async generator, consumed with `async for`.
- An expand node's item is type-checked against `T` by the input rules, on by default and off with
  `without_input_type_checks()`, since an item is an input to its block. The edge from the expand node to each block
  consumer is checked as `T`, and from a block node to a `Collect[T]` parameter as that node's type against `T`.

### 4.2 Blocks, at build

A **block** belongs to one expand node *E*. It holds every node that depends on *E* along a path that does not pass
through a `Collect` parameter of *E*'s block. A block node may also read nodes outside the block, which are computed
once per run and shared by every branch (Hamilton's behaviour; §11).

The build refuses, naming the nodes and the path:

- a node outside a block that reads a block node other than through `Collect` (it would need one value per branch);
- a `Collect[T]` parameter that reads a node not in any block;
- a `Collect` parameter that reads a block it is itself inside (a branch can't collect its own block). A node that
  reads both a block node and that block's collect is such a node: reading the block node puts it in the block
  (Q124);
- a `Collect` parameter that reads an expand node itself (it is not in its own block; collect a block node instead);
- a node that reads block nodes of two unrelated blocks, other than through `Collect` (there is no pairing of their
  branches; a zip is a node that collects both);
- `@extract_fields` or `@extract_columns` on an expand node.

A node may both collect one block and expand another: blocks then run one after the other (Q136).

The IR grows two node kinds, as design §4 foresaw: `Expand` and `Collect`. The core finds blocks during
`Graph::build`, iteratively, and records each block's members, entry (*E*) and exits (the `Collect` edges).
`NodeView.block` names a node's block (its expand node, or `None`), and `NodeView.kind` gains `"expand"` and
`"collect"`.

### 4.3 Blocks, at prepare and run

`Graph::prepare` plans as today, then compiles each block the request reaches into a **sub-plan**: its steps in
topological order with ties broken by id, its releases, and the outer values it reads. A plan holds its sub-plans,
nested as the blocks are. All of it is fixed by the graph and the request, so a plan is still reusable, deterministic
and free of run-time state; a run is the only thing that knows how many items there were.

At run time the expand node runs as a step; its items are numbered from 0 as they arrive; each item instantiates the
block's sub-plan as a branch with the item bound to *E*'s name; and each `Collect` step waits for every branch, then
reads its node from each branch **in branch order**. The branch path, one index per nesting level, names a branch in
hooks, errors and drawings.

**Reference order.** `Plan::run` is the sequential semantics every executor must match. It runs outer steps in plan
order. At an expand step it pulls item 0, runs branch 0's sub-plan to the end in step order, pulls item 1, and so on;
when the iterable is exhausted it goes on to the next outer step. Nested blocks repeat this inside a branch. Every
task therefore has a total **order key**: the sequence of (step position, branch index) pairs from the outermost plan
inwards, ending in the step's position in its sub-plan, with a pull ordered just before its branch's first step.
Keys compare lexicographically.

- **Empty.** An expand node with no items gives every `Collect` an empty list (§11 for Hamilton).
- **Requested outputs** may not be block nodes: `prepare` refuses one with `PlanError::OutputInBlock`, naming the
  `Collect` nodes that read it, or suggesting one. Outputs are one value per name (Q19).
- **Overrides.** Overriding an expand node supplies its items (any iterable). Overriding a `Collect` node cuts its
  block, as any override cuts its upstream. Overriding a block node is refused: an override is one value and a block
  node has one per branch. Inputs and overrides outside a block are shared by its branches.
- **Lineage.** `upstream_of`, `downstream_of`, `what_is_the_path_between`, `validate` and `inputs()` treat expand and
  collect nodes as ordinary nodes, so their answers do not change shape.

### 4.4 Nesting, namespaces and `bind=`

- **Nesting.** A block may contain an expand node; its block is nested, and its `Collect` must be inside the outer
  block, so it gathers per outer branch. Branch paths grow by one index per level. The scheduler keeps an explicit
  stack of block instances, so nesting never recurses in Rust or Python (design guarantee: no recursion over depth).
  Nesting depth is bounded only by the graph.
- **Namespaces.** Blocks are named by their expand node, which carries its namespace (`ns.tickers`), so two copies of
  a module through `with_modules(..., namespace=)` have two independent blocks.
- **`bind=` (Q112).** A copy's input bound to an outer node becomes a shared value of the copy's blocks. A binding to
  a block node of another copy is refused like any cross-block edge (§4.2); binding to that copy's `Collect` node is
  the way to read it.
- **Decorators.** `@parameterize`, `@config.when*`, `@tag`, `@check_output*`, `@mutate`/`@pipe_*`, `@inject`,
  `@resolve`, `@cache` and node wrappers apply to block nodes per branch, as they would to the node outside a block.
  `@parameterize` on an expand node makes several expand nodes, each its own block.

### 4.5 Errors

By default a failing branch fails the run, as a failing node does today:

- no new step starts, in any branch; steps already running finish (they can't be interrupted, §6.5), and their
  results are dropped;
- the run raises the failing node's own exception once, with a note naming the node and its branch path
  (`while computing 'prices' for branch (3,) of 'tickers'`), never an `ExceptionGroup`, so `except KeyError` works
  as it does today (Q66's rule);
- among the failures seen before the run stopped, the one with the lowest order key (§4.3) is raised. With one
  worker, or one failing node, that is deterministic and is today's behaviour; with several workers and several
  failures, which ones were seen before the stop depends on timing. Every failure seen is reported to `after_node`
  with `ok=False`.

Partial results are opt-in, through absence (§7.5).

### 4.6 Caching

The caching design's rules hold unchanged for nodes outside blocks. For a block:

- **An item's version is its fingerprint** (caching design §5), as an input's is. A branch node's key is then its
  ordinary key: code version, and the version of each parameter, chained. The branch index is not in the key, so an
  item that moved, or that another run also had, hits; appending one item computes one new branch.
- **An item that can't be fingerprinted** is versioned by its expand node's key and its index, but only when the
  expand node's items are stored under that key in this run, or replayed from it: then the key covers exactly what
  was yielded. Otherwise (a `recompute` expand node, or one whose policy or items keep it out of the store) that
  branch is uncacheable, with everything downstream of it, as the caching design treats any uncacheable node.
- **The expand node** is cached as the list of its items, under its ordinary key, so a hit replays the items without
  running it. Storing them means holding every item until the expand is exhausted, which a large or endless
  generator can't afford; such a node is marked `@cache(behavior="disable")` (or `recompute`) and stays lazy (§6.6).
  A `recompute` expand node always runs, as a loader does.
- **A `Collect` node's version** of its collected parameter is a hash of its branches' versions in branch order, so
  the collect hits exactly when every branch's key is unchanged, in the same order (Hamilton sorts them: Q129).
- **Keys of a block are known once its items are**, not before the run. The cache's walk (caching design §3) stops
  at expand nodes, as at `recompute` nodes.
- **Hits inside a block are not overrides.** The caching design applies hits as overrides through the hit-plan
  cache, but a block node can't be overridden (§4.3) and hit sets differ per branch. So per branch, once its item's
  version is known, the scheduler computes the branch's keys, walks back from the block's exits (each `Collect`
  edge) as the caching design's walk does, and marks each node a **hit** (its value loaded from the store, its
  upstream in the branch skipped), a miss, or uncacheable. A hit is a task outcome in the `Scheduler`, not a pruned
  plan. Outside blocks, the override mechanism is unchanged.
- **Code versions** of block nodes are made once at build, like any node's; a branch costs no versioning.
- **Within a run, one computation per key (single-flight).** When a branch task's key equals that of a task already
  running in the run, the scheduler makes it depend on that task instead of handing it out, so no worker waits. When
  the first succeeds, or returns a declared `ABSENT`, the duplicate completes as a hit and fires a hit's events
  (Q85): no wrapper call, `cache` outcome `hit`. A failure or an error absence (§7.5) is not shared: the duplicate
  then runs itself. `recompute` nodes and uncacheable tasks have no key and are never deduplicated. Without a cache
  there are no keys, and both run.
- **An error absence is never stored, and poisons its downstream** for the run: its version is a fresh per-run
  nonce, so every node whose key chains from it, including a consumer that fell back to its default and a `Collect`
  that dropped it, is uncacheable in that run. A partial result is never served as a whole one.
- `explain_cache` reports a block as its expand node's outcome and a count of hits, misses and uncacheable branches.

### 4.7 Hooks, drawing and export

- **Hooks.** Every branch node fires `before_node` and `after_node`; `NodeStart` and `NodeEnd` gain
  `branch: tuple[int, ...]`, `()` outside blocks. `RunStart.nodes` lists each block node once, in plan order. A new
  `Hook.after_expand(event)` fires when an expand node's iterable is exhausted, with the node, its branch path and the
  item count, so a progress hook knows the total. The other hook rules of Q23, Q84 and Q86 are unchanged; the rows
  those three amend are listed in §10.
- **Node wrappers** (Q111) wrap each branch's call, and `NodeView` tells them the node, not the branch; the branch
  path is on a context variable, `fnloom.current_branch()`, readable from a wrapper, a check or the node itself.
- **Drawing.** `draw()` and its kin draw a block as a cluster labelled `for each item of tickers`, the expand node's
  edge into it marked `expand` and each `Collect` edge out marked `collect`, in DOT and Mermaid, independent of the
  hash seed (Q94). `draw_execution` draws the plan, not a run, so a block is drawn once.
- **Export.** Nodes gain `kind: "expand" | "collect"`, `block` and `may_be_absent`. A reader that switches on `kind`
  meets a new value, so a graph with a block or a node that may be absent exports as `fnloom.export/2`; every other
  graph exports exactly as today, as `fnloom.export/1`. `fnloom diff` reads both.

## 5. The plan in the core

`fnloom-core` stays dependency-free and `#![forbid(unsafe_code)]`, generic over `P` and `V`.

- `NodeKind` gains `Expand(P)`; `InputSpec` gains `Dependency::Collect`. `Graph::build` adds block discovery and the
  §4.2 errors (`BuildError::BlockEdge`, `BuildError::CollectOutsideBlock`, ...).
- `Plan` gains its sub-plans; `Step` gains a `Branch`-aware action set: `Expand`, `Compute`, `Collect`.
- `Evaluate<P>` gains one method with a default, `expand(node, payload, args) -> Result<impl Iterator<Item = V>, E>`
  (exact shape the builder's), so an embedder without blocks writes nothing new.
- **Absence is in the core** (§7). `evaluate` returns an outcome, a value or `Absent`. `Dependency` gains a third
  kind beside `Required` and `Optional`: `Admits`, for a parameter that receives absence as an argument (the binding
  turns it into `ABSENT`). The core propagates skips by the three rules of §7.2 in `Plan::run` and the `Scheduler`
  alike, and `Graph::build` computes which nodes may be absent. An embedder that never returns `Absent` sees no
  change.
- `Plan::run` and `run_by_id` keep their signatures and run blocks sequentially, in branch order: the reference
  semantics every other executor must match.
- `Scheduler` (§6.1) is the second way to run a plan.

## 6. The executor

### 6.1 Who does what

- **`fnloom-core::Scheduler`**: a state machine over one run of one plan. `next() -> Option<Task>` hands out a ready
  step (a node, a pull from an expand, or a cache hit to load); `complete(task, outcome)` records a value, an
  error, an absence or a hit and makes dependents ready. Ready tasks are handed out lowest order key first (§4.3),
  so with one worker it reproduces `Plan::run` exactly. It owns dependency counts, value release, the in-flight
  bound, single-flight (§4.6) and stop-on-failure. It asks a `Limits` object (§6.6), shared by every run of a driver,
  whether a task may start, and skips over a task that may not. It has no threads, no clock and no locks of its
  own: the binding holds it behind one.
- **`fnloom-py`**: the worker pool, the GIL boundary (§6.2), calling the Python payloads, hooks (§6.4) and the cache
  (§6.7). Rust-native payloads (design §11), when they come, run on the same workers without attaching to Python at
  all.
- **`AsyncDriver`** (Python): drives the same `Scheduler` from the event loop instead of its own dependency counts,
  so both drivers schedule by one set of rules. Async nodes are tasks, sync nodes go to the pool (§6.3).

### 6.2 Threads and the GIL

`Builder.with_threads(max_workers=None, limits=None)` caps a driver at `max_workers` concurrent task calls (default
`os.process_cpu_count()`, maintainer 2026-10-29), counted by the driver's `Limits` across every run and every thread
that shares the driver.
A thread that calls `execute` works on its own run, and counts against `max_workers` while it runs a task, like a
pool thread. The pool holds `max_workers - 1` threads, started lazily at the first run and shared by the driver's
runs, so `max_workers=1` means no pool thread: the caller runs everything, in reference order, on its own thread.

- Workers are Rust threads. A worker **attaches** (PyO3 `Python::attach`) only to call a node, a pull, a hook, a
  check or a store, and **detaches** (`py.detach`) before taking any lock or waiting for work. The scheduler's and
  the limits' locks guard short Rust-only sections that never call Python. The async driver's loop thread also
  detaches around every scheduler call. So no thread ever waits for a lock while attached.
- The one lock held across a call into Python is the run's **hook lock** (§6.4). It is acquired detached, then the
  thread attaches and calls the hook. On the GIL build the holder may wait for the GIL, but whoever holds the GIL is
  not waiting for the hook lock (that wait is detached), so the two can't deadlock.
- **3.14t**: the module already declares it doesn't need the GIL ([Threads](../running.md#threads)); node calls run in
  parallel.
- **3.14 with the GIL**: the same code is correct. Python node bodies take turns on the GIL; nodes that release it
  (I/O, numpy, Rust payloads) overlap. The docs say so, and the benchmarks show both builds.
- **Context variables.** On a driver built `with_threads`, every task call runs in its own copy of the caller's
  context, taken when the run started, as `run_in_executor` does on the async driver (Q65), whatever `max_workers`
  is. A `ContextVar.set` in one node is therefore never seen by another, so results can't depend on the worker
  count, and no `Context` is entered by two threads. A driver without `with_threads` runs nodes in the caller's
  context, as today. `current_branch()` and `cancelled()` (§4.7, §6.5) are context variables the binding sets in
  that per-call copy (on the caller's context, for the sequential path).
- **Signals.** The calling thread waits detached, in slices, and calls `check_signals` between them, so `Ctrl-C`
  stops a parallel run (§6.5) as it stops a sequential one.

### 6.3 With the async driver

`build_async()` keeps its behaviour without `with_threads`. With it, a sync node runs on the driver's pool instead of
the loop's default executor, under the same limits and context rule, and async nodes count against the same
per-node and group limits (an async node holds no thread, so not against `max_workers`). `with_threads` together
with `build_async(sync_nodes="inline")` is refused at build, since it asks for sync nodes both on and off the loop.
Fan-out
works on the async driver as on `Driver`: each branch's async nodes are tasks, a failing branch cancels every running
task (Q66), and hooks run on the loop's thread (Q86).

### 6.4 Determinism and hooks

- **Values** never depend on workers or timing (goal 3): branches are numbered by item order, `Collect` reads in
  branch order, outputs come in request order (Q19), and the cache keys of §4.6 contain no timing.
- **Hooks** of one run never run concurrently: a run's hook calls are serialized by its hook lock, so a hook needs no
  lock for one run (as Q86 promises on the async driver). A hook runs on the thread that ran the node.
- **Event order.** With one worker, events come in plan order, branches in branch order: exactly the sequential run.
  With several, they come in the order nodes start and finish, as on the async driver (Q86); per node, `before_node`
  precedes `after_node`, and a node's `after_node` follows every computed input's. fnloom does not buffer events into
  plan order: that would hold a progress hook's events until a slow sibling finished. `NodeStart` and `NodeEnd` carry
  the branch path and the node's plan position, so a hook that wants plan order can sort.

### 6.5 Cancellation and deadlines

- **On failure**, the scheduler stops handing out tasks, waits for the running ones, and raises (§4.5). The sync
  driver waits so that no node of the run is still running when `execute` returns or raises, unlike the async driver,
  which can't wait for a thread (Q66).
- **Deadlines.** `execute(..., timeout=seconds)` and `PreparedPlan.run(..., timeout=)` on `Driver`: when it passes,
  no new task starts, `cancelled()` turns `True`, and the run raises `TimeoutError` at once, without waiting for
  running tasks, since a hung node is what a deadline is for. Those tasks are abandoned: they finish in the
  background, their results are dropped, and their limits are released when they return, as the async driver
  abandons a sync node's thread (Q66). On `AsyncDriver`, the caller uses `asyncio.timeout`, which already cancels the
  run (Q66), so it takes no new argument.
- **Runs inside runs.** A node that calls `execute` on its own driver runs the inner run on its own thread, inside
  the slot it already holds, so nesting never waits for `max_workers`. Group limits are not re-entrant: an inner task
  that needs a group whose every slot is held by its own outer callers would wait forever, so the scheduler detects
  that case and raises `RuntimeError` naming the group.
- **Cooperative stop.** `fnloom.cancelled() -> bool`, readable in a node, is `True` once its run is stopping, so a
  long node can return early.
- **`KeyboardInterrupt`** in the caller stops the run like a failure, then propagates.

### 6.6 Resource limits

- `max_workers` bounds concurrent node calls per driver (§6.2).
- **Per-node and per-group limits.** `@fnloom.limit(n)` caps a node's concurrent calls; `@fnloom.limit(group="db")`
  puts it in a named group, and `with_threads(limits={"db": 4})` sizes the group. Limits are the driver's: one
  `Limits` object of atomic counters, shared by every run's scheduler, so they hold across concurrent runs. A
  scheduler hands out a task only when `Limits` grants its slots, so no worker blocks on a limit; when a slot is
  released, waiting runs are woken. An unknown group fails the build.
- **In-flight items.** An expand node's iterable is read no further ahead than `2 * max_workers` unfinished branches,
  so a generator over a large source is not drained into memory first. Branch values a `Collect` still needs are
  kept; the rest are released as today.

### 6.7 What is thread-safe

The [Threads](../running.md#threads) guarantees hold and extend to parallel runs:

- **The plan and graph** are immutable and shared; per-run state lives in the scheduler, one per run.
- **Stores** were already required to be safe from several threads (caching design §7); `MemoryCache` is native and
  `DiskCache` writes atomically. Parallel branches call them from workers. The single-flight table of §4.6 is per run.
- **The hit-plan cache** is shared by the driver's threads already, and is unchanged.
- **Your code** is where it was: a node that mutates a shared value or a global races with its siblings now, as it
  raced with other threads' runs before. A hook, result builder, check, wrapper or store that keeps state across
  runs must be thread-safe; within one run, hooks are serialized and nothing else is.

## 7. Typed absence

### 7.1 Declining

```python
from fnloom import ABSENT, Absent


def quote(ticker: str) -> float | Absent:
    q = feed.get(ticker)
    return ABSENT if q is None else q.price
```

`fnloom.Absent` is a final class with one instance, `fnloom.ABSENT`. It is distinct from `None`, which stays a value
(Q79). A node may return `ABSENT` only if its return annotation admits `Absent`; otherwise returning it is a
`TypeError` naming the node, checked always, at the cost of an identity test.

### 7.2 What a consumer gets

The build marks every node **may be absent** when its annotation admits `Absent`, an `absent_on` policy covers it
(§7.5), or a parameter of the third kind below reads a node that may be absent. Each parameter reading a node that may
be absent decides by its own signature:

1. annotated to admit `Absent` (`float | Absent`): it receives `ABSENT` as a value;
2. otherwise, with a default: it is omitted and the default applies, exactly as an optional input that isn't
   supplied (design §4);
3. otherwise: the consumer doesn't run, and is absent too.

So absence is never a surprise at run time: `NodeView.may_be_absent` says it, a drawing marks it, and a node can't
receive `ABSENT` unless it says it can. A skipped node fires no node event; `RunEnd.skipped` lists them. A requested
output that is absent is `ABSENT` in the result dict.

### 7.3 With `Collect`, inputs, overrides and the cache

- `Collect[T]` over a block node that may be absent gives the present values, in branch order; `Collect[T | Absent]`
  gives one entry per branch, `ABSENT` where it declined, so positions line up with the items.
- An input may not be `ABSENT` (leave it out instead); an override may be, for a node that may be absent, which is how
  a test forces the skip.
- **Caching.** A declared absence is a result: it is stored under the node's key, as a marker both formats encode,
  and a hit replays it, so its consumers skip again without running it. As a version, a declared `ABSENT` has a
  fingerprint of its own. Under rule 2, the omitted parameter is versioned by the caching design's "not supplied"
  marker, which is right: the call is the same as with the input left out. An absence from an error is different
  (§7.5).

### 7.4 Every other place absence meets

- **An expand node that is absent** (declared, or by `absent_on` before or while it yields): the block has no
  branches, items already pulled are dropped and their branches cancelled, and each `Collect` parameter over the
  block is handled by §7.2's rules as a whole: given `ABSENT` if its annotation admits it at the top level
  (`Collect[T] | Absent`), its default if it has one, and otherwise its node is skipped. An absent expand is not an
  empty one: an empty one gives `[]`.
- **`@extract_fields` and `@extract_columns`** of an absent node: every field node is absent.
- **Output checks** (`@check_output*`, `with_output_type_checks`) don't run on `ABSENT`: there is no value to check.
  A `@mutate` or `@pipe_*` step is an ordinary node reading its target, so §7.2 applies to it. A node wrapper
  (Q111) sees `ABSENT` returned by `call()` and may return it, or a value.
- **Edge type checks** strip `Absent` from the producer's type before comparing, so `float | Absent` feeds `float`
  (rules 2 and 3), `float | Absent` (rule 1) and `Collect[float]` (absent branches dropped).
- **Result builders** (Q19) receive `ABSENT` for an absent output, like any other value.
- **Overrides inside a block** are refused (§4.3), absent or not; to force a block node's absence in a test, override
  an upstream node outside the block, or the expand node with items that make it decline.
- **The async driver** follows every rule here, unchanged.

### 7.5 Absent on error: partial results

```python
@fnloom.absent_on(TimeoutError, ConnectionError)
async def quote(ticker: str) -> float | Absent: ...
```

When the node raises one of those (or a subclass), its outcome is `ABSENT` instead of a failure. `after_node` still
reports `ok=False` with the error, so `LogFailures` (Q88) logs it, and `RunEnd.absent_errors` maps each such node and
branch to its error. A `Collect` over the block then gets the branches that succeeded: a fan-out returns partial
results. The node's annotation must admit `Absent`, checked when decorating, so the type says what can happen. An
absence from an error is never cached, and nothing downstream of it is cached in that run (§4.6), so the next run
tries again and a partial result is never stored as a whole one.

For a whole graph, `Builder.with_absent_on(*errors, tags=None)` applies the same policy to every node, or the nodes
with the given tags, which is what Hamilton's `GracefulErrorAdapter` does driver-wide (§11). Nodes it covers may be
absent whatever their annotation, since the absence is the driver's doing, not the node's.

## 8. Not ruling out the rest

What Plan 10 delivers, and what it only leaves room for:

| Item | Plan 10 | Room left |
|---|---|---|
| Mixed Python/Rust graphs (design §11) | The scheduler is in the core and generic over `P`; workers attach only for Python payloads | A Rust payload runs on a worker without the GIL, in parallel even on the GIL build |
| Zero-copy arrays | Branch values are passed by reference; nothing is copied per branch or per worker | A native array value type in the binding |
| Streaming and per-node state | Lazy expand with an in-flight bound; a block's sub-plan is a reusable per-item step | A `Stream[T]` expand whose collect is a fold, and stateful nodes carrying state from branch *i* to *i+1*: a plan stepped bar by bar |
| Incremental execution | Content-addressed branch keys: an appended item computes one branch, and the rest hit | Incremental collect (fold over new branches only) |
| Panel execution | In-graph: expand over entities; and `PreparedPlan.run_many(inputs_seq)`, one prepared plan over many input sets on the pool, results in order (maintainer, 2026-10-29) | Panels that share work across entities |
| Sweep execution | An expand node over parameter sets | A `sweep(...)` helper over a grid of `value()`s |
| Graphs that carry I/O and failure | `absent_on`, `Collect` over partial results, cancellation, deadlines | Retries and backoff, as a node wrapper recipe |
| Array contracts (Q9/Q17/Q18) | Untouched | Unchanged |

## 9. Rejected alternatives

- **Expanding the graph at run time** (new nodes per item, as Hamilton's grouping makes tasks per item): the plan
  would be rebuilt per run, losing goal 2, and node names would carry indexes.
- **Decorators (`@expand`, `@collect`) instead of annotations**: no gain in typing over the aliases, and every
  Hamilton program would need rewriting, against design §5.
- **Position-addressed branch keys**: simpler, but an inserted item misses every later branch, and incremental
  execution would need a new key scheme.
- **`None` as absence**: `None` is a value fnloom checks (Q79), and many nodes return it on purpose.
- **Buffered, plan-ordered hook events with several workers**: deterministic, but late; a hook can sort by plan
  position.
- **A process executor**: pickling every branch value costs more than the node on the workloads fnloom targets, and
  free threading makes it unnecessary for Python; process pools over whole runs already work.

## 10. Register rows

Next free row is Q118. Each row gets its test before its code, as in earlier plans; rows marked KEEP get a
differential test against Hamilton 1.90.0, the rest a `deliberate("Qn")` test red against Hamilton.

| Row | Hamilton 1.90.0 (§11) | Disposition | fnloom |
|---|---|---|---|
| Q118 | A graph with `Parallelizable`/`Collect` builds, then `execute` raises `InvalidExecutorException` unless the builder called `enable_dynamic_execution(allow_experimental_mode=True)`; without the flag that call is a `ValueError` misspelling the flag and calling the feature "remote execution". | FIX | No flag: any graph with them builds and runs (§4.1). |
| Q119 | `Collect` receives values in yield order, whatever the executor and whatever order branches finish in. | KEEP | Branch order (§4.3); differential test with Hamilton's synchronous and thread executors. |
| Q120 | An expand node that yields nothing gives `Collect` an empty list. | KEEP | Same (§4.3). |
| Q121 | An expand node may return a list or any iterable, not only a generator; requesting it as an output returns the materialised list. | KEEP | Same; fnloom reads it lazily (§4.1, §6.6), and as an output it is a `list`. |
| Q122 | Nested `Parallelizable` is not supported and not refused: the outer collect's walk back stops at the inner expand, so the grouping is wrong and the run fails with an unrelated `TypeError`, or is silently wrong when the types line up. | FIX | Nesting is supported to any depth, with branch paths (§4.4). |
| Q123 | Two `Collect` nodes over one `Parallelizable` fail at run with `KeyError: 'Key cb not found in cache'`: both make groups with one id, and the conflict check is a commented-out TODO. | FIX | Any number of collects per block (§4.3). |
| Q124 | A node reading both a block node and that block's `Collect` makes `execute` spin forever at full CPU: its dependency on the block task never becomes ready. | REFUSE | Reading the block node puts the node inside the block, and a node can't collect its own block: the build refuses it, naming the path (§4.2). |
| Q125 | Overriding the expand node removes its group, so block nodes receive the whole list (`TypeError` or a wrong value); overriding a block node makes `Collect` return `[]` silently. | FIX, REFUSE | An expand override supplies the items; a block-node override is refused at prepare (§4.3). |
| Q126 | A block node requested as an output fails with `TypeError`, or `KeyError` when requested with its collect. | REFUSE | `PlanError::OutputInBlock` at prepare, naming the collects that read it (§4.3). |
| Q127 | `Parallelizable[T]` and `Collect[T]` are unwrapped to `T` and edge-checked at build; yielded items are never checked at run, so a `str` reaches an `int` consumer. `Collect[T]` is an `Iterable` subclass, so `len()` and indexing don't type-check. | KEEP (edges), FIX (items, typing) | Edges checked as Hamilton does; each item checked by the input rules, on by default; the aliases type as `Iterable[T]` and `list[T]` (§4.1). |
| Q128 | On a branch failure the run raises the original exception, unwrapped; branches already running finish, unsubmitted ones never start, and finished branches' results are dropped. | KEEP | Same exception class and stop rule, plus a note naming the node and branch path; the sync driver returns only once the running steps have, except on a deadline (§4.5, §6.5). |
| Q129 | With `with_cache()`, the expand node always recomputes. Branch keys hash the item's value, not its index. A collect's key hashes its branch versions **sorted**, so reordering the items serves a stale hit to an order-dependent collect. | FIX | Content-addressed branch keys kept; the expand node's items are cached under its key; a collect's version hashes its branches in branch order (§4.6). |
| Q130 | Task hooks (`TaskExecutionHook`, `TaskGroupingHook`, `post_task_expand`, ...) fire per task, and node hooks get a `task_id` such as `expand-x.3.block-x`; the collect's task id is not namespaced. | DROP, FIX | No task hooks: node events carry `branch`, and `after_expand` reports the item count (§4.7). |
| Q131 | Drawings mark expand and collect nodes with a coloured double border and crow's-foot edges; blocks are not grouped. | FIX | A block is a cluster, its edges marked `expand` and `collect`, in DOT and Mermaid (§4.7). |
| Q132 | The async driver refuses dynamic execution: `enable_dynamic_execution` raises `ValueError` "... not supported for the async driver". | FIX | Blocks on `AsyncDriver`, with async generators as expand nodes (§6.3). |
| Q133 | `GracefulErrorAdapter(error_to_catch, sentinel_value=None)` turns a caught error into the sentinel, and every node downstream returns the sentinel without running, driver-wide. A failed branch appears in its collect as `None`, indistinguishable from a branch that returned `None`, and nothing in the types says a node may come back as the sentinel. | FIX | `absent_on` per node and `with_absent_on` driver-wide make it `ABSENT`; each consumer's signature decides (§7.2); `Collect[T]` drops it and `Collect[T \| Absent]` keeps its place (§7.3, §7.5). |
| Q134 | Executors: synchronous local, `MultiThreadingExecutor(max_tasks)`, `MultiProcessingExecutor(max_tasks)` and plugins for Ray, Dask and Spark. The builder's default remote executor is `MultiThreadingExecutor(10)`, while a hand-built `DefaultExecutionManager` defaults to `MultiProcessingExecutor(5)`. `max_tasks` is the only limit. | DROP (processes, clusters), FIX | `with_threads(max_workers, limits)`; one node at a time by default; per-node and per-group limits (§6.2, §6.6). |
| Q135 | A node cannot decline to produce a value; "don't compute" is data. | FIX | Typed absence (§7). |
| Q136 | A function annotated `-> Parallelizable[T]` that also takes a `Collect` parameter is treated as a collect node only. | FIX | It is both: it collects one block and expands another (§4.2). |
| Q137 | Dynamic execution is configured through `with_local_executor`, `with_remote_executor`, `with_execution_manager` and `with_grouping_strategy`, each a `ValueError` unless dynamic execution is enabled. | DROP | `with_threads(...)` is the only setting; grouping is not a user choice, since blocks are found from the graph (§4.2, §6.2). |
| Q138 | Two independent `Parallelizable`s, each with its own `Collect`, joined downstream, run correctly; inputs and overrides of nodes outside a block are shared by every branch. | KEEP | Same (§4.2, §4.3); differential tests. |

**Existing rows this design amends**, each updated in the task that changes it:

- **Q19**: a result builder, and the result dict, may hold `ABSENT` for an absent output (§7.2, §7.4).
- **Q65**: with `with_threads`, a sync node on the async driver runs on the driver's pool, not the loop's default
  executor; `sync_nodes="inline"` with `with_threads` is refused (§6.3).
- **Q66**: the sync driver gains the same stop rule, but waits for running steps on failure; a deadline abandons them
  as the async driver does (§6.5).
- **Q84**: `Hook` gains a fifth method, `after_expand`; `NodeStart` and `NodeEnd` gain `branch`; `RunEnd` gains
  `skipped` and `absent_errors` (§4.7, §7.2, §7.5).
- **Q85**: a single-flight duplicate reports a hit (§4.6).
- **Q86**: on a driver with `with_threads`, hooks run on the thread that ran the node, serialized per run (§6.4).
- **Q95**: `fnloom.export/2` for graphs with blocks or absence (§4.7).
- **Q111**: wrappers are called once per branch and read `current_branch()` (§4.7).

## 11. Hamilton 1.90.0, as read and probed

Read in `hamilton/execution/{grouping,executors,state,graph_functions}.py`, `htypes.py`, `driver.py`,
`async_driver.py` and `caching/adapter.py`, and probed on Python 3.14.8 with the synchronous and thread executors.
Visualisation, the async driver and the executor list are read from source only.

- **Model.** Grouping is driven by each `Collect`: it walks back to the first `Parallelizable` it meets and forms
  three tasks, `expand-X` (the expand node), `block-X` (every node between) and `collect-X`; every other node is a task
  of its own. Each branch is a task `expand-X.<i>.block-X`, with its item bound as an override inside it. Only block
  tasks go to the "remote" executor. The graph is static; the tasks are made per run.
- **Ordering.** `Collect` gets yield order (Q119), with any executor; the probe finished branches in reverse and still
  collected in order.
- **Shared values.** Nodes outside a block that a block reads are computed once and shared, as in fnloom.
- **Faults.** Nesting is unsupported and unrefused (Q122); two collects of one block fail (Q123); a node reading a
  block node and its collect hangs (Q124); overrides inside or of a block go wrong silently (Q125); block nodes can't
  be outputs (Q126); items are unchecked (Q127); the collect cache key ignores order (Q129); a graceful sentinel is
  indistinguishable from a value (Q133); the flag's error text is misspelled (Q118).
- **Hooks.** Per task: `pre_task_submission`, `pre_task_execute`, the node hooks with a `task_id`, `post_task_execute`,
  `post_task_expand(parameters=...)` for the expand task, `post_task_return`; `post_task_group(task_ids)` once before
  execution (Q130).
- **Failure.** The original exception is re-raised; nothing running is cancelled, unsubmitted tasks never start, and
  finished results are dropped (Q128).

## 12. Proof, benchmarks and tasks

### 12.1 Tests

- **Differential**: every KEEP row of §10, on programs shared by import swap: one block, shared inputs, several
  collects, empty expand, a failing branch's exception class, with Hamilton's synchronous and thread executors.
- **Deliberate**: every FIX and REFUSE row.
- **Executor equivalence** (hypothesis): generated graphs with blocks, nested blocks, absences and failures run with
  `max_workers` in {1, 2, 8} give identical results, `Collect` orders, raised exception classes (with one failure)
  and cache keys; with one worker, the same hook event sequence as `Plan::run`.
- **Free-threading stress**: the existing 16-thread test, extended with parallel runs sharing one driver, pool and
  cache; run on 3.14 and 3.14t in CI.
- **Core**: block discovery, nesting and the scheduler's order, tested in Rust with no Python; no recursion, on a
  50,000-node chain inside a block and a 1,000-deep nesting.

### 12.2 Benchmarks

New rows in `docs/benchmarks.md` and CodSpeed, each against Hamilton's executors where it has one:

- fan-out of 1, 100 and 10,000 trivial branches: per-branch overhead, sequential, against Hamilton's synchronous
  executor; target under 2 µs per branch node;
- the same with `with_threads(8)` on 3.14t against Hamilton's `MultiThreadingExecutor`, with CPU-bound (pure Python)
  and sleeping branches, reporting speedup over one worker; on 3.14 with the GIL, the sleeping case only;
- a layered graph without blocks under `with_threads`: the scheduler's overhead against `Plan::run`, gated by
  instruction count like `run`;
- a fully cached fan-out of 1,000 branches, and the same with one appended item;
- `ABSENT` propagation through a 200-node chain: no cost on a graph with no node that may be absent.

### 12.3 Tasks for the implementation plan

Each task is one PR-sized, independently reviewable change. Its register rows, and their differential or
`deliberate` tests, land in the same task, written before its code. Milestones can ship as separate releases.

**Split (maintainer, 2026-10-29):** Plan 10 is milestones A and B, ending in the 0.3.0 release; Plan 11 is
milestones C, D and E. Each plan closes its own docs, roadmap rows and change log, so task 27's share for Plan 10
is written into Plan 10's last task.

#### A. Absence (needs nothing from B or C)

1. Core: the `Absent` outcome, `Dependency::Admits`, skip propagation in `Plan::run`, may-be-absent at build; Rust
   only.
2. Front end: `Absent`, `ABSENT`, the return check, the three consumer rules, `NodeView.may_be_absent` (Q135).
3. Absence at the edges of a run: outputs, overrides, result builders, `RunEnd.skipped`, extract fields, output
   checks, steps and wrappers (§7.4; amends Q19, Q84).
4. Caching declared absences: the stored marker in both formats, its fingerprint, the "not supplied" version.
5. Absence on the async driver.

#### B. Blocks, sequential

6. Core: `Expand` and `Collect` kinds, block discovery and the build errors, in Rust only (Q124, Q136).
7. Core: sub-plans in `prepare`, `OutputInBlock`, and `Plan::run` over blocks in reference order, nesting included.
8. Front end: the `Parallelizable` and `Collect` aliases, item and edge type checks, `NodeView.block` and `kind`
   (Q118 to Q121, Q127, Q137, Q138).
9. Overrides of expand, collect and block nodes; lineage, `validate`, `inputs()` (Q125, Q126).
10. Hooks: `branch` on node events, `after_expand`, `current_branch()`; node wrappers per branch (Q130; amends Q84,
    Q111).
11. Caching blocks, part 1: item fingerprints, per-branch keys, the branch hit outcome, the positional fallback.
12. Caching blocks, part 2: the expand node's stored items, ordered collect versions, `explain_cache` (Q129).
13. Drawing clusters and `fnloom.export/2` for graphs that need it, with `fnloom diff` reading both (Q131; amends
    Q95).
14. Namespaces and `bind=` with blocks, and every decorator on block nodes (the stacking test extended).
15. Absence in blocks: `Collect` over absent branches, an absent expand node.

#### C. The scheduler and the parallel executor

16. Core: `Scheduler`, with order keys and pulls, matching `Plan::run` task for task; tested in Rust.
17. Binding: the sync driver runs on the `Scheduler`; `with_threads(max_workers)`, the driver's `Limits`, the pool,
    attach and detach; equivalence tests over worker counts (Q122 and Q123 rerun in parallel; amends Q134).
18. Hooks serialized per run, per-call contexts, `current_branch()` on workers (amends Q86).
19. Single-flight per key (amends Q85).
20. `limit(...)` and groups, the in-flight bound, runs inside runs.
21. Failure, `timeout=`, `cancelled()`, `close()` on early stop, signals and `KeyboardInterrupt` (Q128; amends Q66).
22. The async driver on the `Scheduler`, its sync nodes on the pool (amends Q65).
23. Blocks on the async driver, async generators as expand nodes (Q132).
24. `PreparedPlan.run_many(inputs_seq, overrides=None)` on the pool: one result per input set, in order; a failure
    raises as in §4.5, naming the input set's index.
25. Free-threading stress tests, and the [Threads](../running.md#threads) section rewritten.

#### D. Partial results

26. `absent_on` and `with_absent_on`, `RunEnd.absent_errors`, error absences uncached and poisoning their downstream,
    `Collect` over partial results (Q133).

#### E. Close-out

27. Benchmarks (§12.2); `docs/running.md` sections on fan-out, threads and absence; `from-hamilton.md` rows; the
    roadmap and the design change log.

## 13. Calls made in this design (change any of them)

- **Hamilton's names** `Parallelizable` and `Collect`, typed as aliases of `Iterable[T]` and `list[T]` (§4.1).
- **Static blocks, compiled sub-plans** over run-time graph expansion (§4.2, §9).
- **Refuse, don't guess**, for outputs in blocks, overrides of block nodes and cross-block edges (§4.2, §4.3).
- **Content-addressed branch keys**, with a positional fallback for items that can't be fingerprinted (§4.6).
- **Single-flight per key within a run** (§4.6).
- **Default of one worker on the caller's thread**; parallelism only through `with_threads` (§6.2).
- **A per-driver pool and per-driver limits**, shared across concurrent runs (§6.2, §6.6).
- **Hooks serialized per run, on the node's thread, in real order** with several workers rather than buffered (§6.4).
- **The sync driver waits for running steps** before raising (§6.5).
- **`ABSENT` distinct from `None`**, and the three consumer rules decided by signature (§7.2).
- **`Collect[T]` drops absent branches**; `Collect[T | Absent]` keeps their places (§7.3).
- **Error absences are never cached** (§7.5).
- **`fnloom.export/2`** for the new node kinds (§4.7).

**Decided by the maintainer (2026-10-29):**

1. **Scope:** absence and sequential blocks (milestones A and B) are Plan 10, then the 0.3.0 release; the
   scheduler, parallel executor and partial results (C, D, E) are Plan 11.
2. **`PreparedPlan.run_many(inputs_seq)`** is built, in Plan 11 (task 24).
3. **`max_workers`** defaults to `os.process_cpu_count()`.

## 14. Change log

- **2026-10-28:** drafted for review, then revised after an independent review: error absences poison their
  downstream keys; the positional key fallback only under a stored expand; driver-level limits and the caller's slot;
  pulls on any worker; a total order key; branch hits inside the scheduler; single-flight rules; per-call contexts;
  deadlines that abandon; every place absence meets; amended rows listed; export/2 only when needed; tasks split.
- **2026-10-29:** approved by the maintainer; the three open questions decided (§13); `run_many` added as task 24.
