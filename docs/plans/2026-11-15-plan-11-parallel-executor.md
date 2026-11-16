# fnloom Plan 11: the parallel executor and partial results

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan PR by PR, task by task, with superpowers:test-driven-development
> inside each task and an independent reviewer subagent per PR. Steps use checkbox (`- [ ]`) syntax for tracking.

**Status:** implementation plan, awaiting the maintainer's approval. Nothing is built. Drafted 2026-11-15, and revised
2026-11-16 after an independent review (#99). Every decision the design leaves open is answered here, and gathered in
[Open questions](#open-questions) for approval in one pass.

**Goal:** milestones C to E of the [dynamic execution design](../design/2026-10-28-dynamic-execution-design.md).

- **C, the scheduler and the parallel executor:**
    - a `Scheduler` in the Rust core that hands out ready tasks in a fixed order;
    - `Builder.with_threads(max_workers=None)`, running one run's tasks on a per-driver pool, parallel on 3.14t and
      correct on 3.14;
    - per-node and per-group limits, honoured by every driver;
    - single-flight per key within a run;
    - deadlines and cooperative cancellation;
    - `PreparedPlan.run_many`;
    - the async driver on the same scheduler, with blocks (§5, §6).
- **D, partial results:** `@fnloom.absent_on(*errors)` and `Builder.with_absent_on(*errors, tags=None)` turn chosen
  errors into absence, so a `Collect` over a fan-out gets the branches that succeeded (§7.5).
- **E, close-out:** benchmarks against Hamilton's thread executor, the docs, the roadmap and the design's change log.

**Non-goals:**

- Single-flight across runs. Two runs of one driver, `run_many`'s sets included, that need one key both compute it.
  Design §4.6 scopes single-flight to a run.
- Processes, interrupting a running node, and streaming (design §2).

**Motivating use, kept generic:** a catalog graph fans out one endpoint call per listed item. With `with_threads(8)`
the calls overlap, on either build. An endpoint that times out for one item makes that branch absent, and the summary
is built from the rest. Task 13 writes this as the end-to-end test and the docs example.

**Architecture:**

- **The core decides what runs next.** `fnloom-core` gains `Scheduler`, a state machine over one run of one plan,
  with no threads, no clock and no locks. `next()` hands out the ready task with the lowest order key that the
  driver's `Limits` grants; `complete()` takes back its outcome. With one worker it reproduces `Plan::run` task for
  task. `Plan::run` stays as it is: the reference semantics, and the default path.
- **The binding owns threads and the GIL boundary.** `fnloom-py` gains:
    - per-driver `Limits`, with a wake for whoever waits on a slot;
    - a per-driver pool of Rust threads;
    - a parallel run loop that attaches only to call Python and does its waiting inside `py.detach`;
    - one hook busy flag per run;
    - a `Stepper` that lets the async driver step the same scheduler from its event loop.
- **The front end is Python**, as before: `with_threads`, `with_limits`, `limit`, `absent_on`, `with_absent_on`,
  `cancelled()`, `timeout=`, `run_many`, and the async driver's loop.
- **Default path untouched.** A driver built without `with_threads` runs `Plan::run` exactly as 0.5.0 does: the
  same thread, context, event order and instruction count. The only addition is a limit grant around a node that
  has a limit (O9), which costs an unlimited node one flag test in the binding.

**Tech Stack:** as Plan 10. Python 3.14 and 3.14t, the Rust core through maturin and PyO3 0.30, ty strict, ruff ALL,
Clippy pedantic, rumdl, pytest with hypothesis, and `apache-hamilton==1.90.0` as the oracle. Build with `maturin
develop --features test-hooks`, test with `UV_NO_SYNC=1`, and gate with `scripts/check.sh`.

**Spec:** `docs/design/2026-10-28-dynamic-execution-design.md`. Its sections:

- §6 (the executor), §7.5 (absent on error), §4.5 and §4.6 where they mention workers, single-flight or error
  absences, and §5's last bullet;
- §10, rows Q133 and Q134 and the amended rows Q65, Q66, Q85 and Q86;
- §12.1 and §12.2, the tests and benchmarks that need threads;
- §12.3, tasks 16 to 27;
- §13, the calls, and §14's 2026-11-15 entry, which records how Plan 10 was actually built. One detail of that
  entry is wrong against the code: the expand-to-block map is `Plan::block_of(expand)`, kept beside the steps, not a
  `Step.block` field. Task 13 corrects it.

Also read [Plan 10](2026-11-05-plan-10-absence-and-fan-out.md), whose seams this plan builds on, and
`docs/plans/2026-10-28-roadmap-ahead-of-hamilton.md`.

**Order and release:**

- 0.5.0 is released (#98, tagged `v0.5.0`), so C1 can start once this plan is approved.
- Plan 11 ships as **0.6.0** (O1).
- Before each PR, re-read the files it touches on `main`. Where a name this plan cites has moved, follow `main` and
  say so in the PR body.

## Global Constraints

- Every Global Constraint of Plan 10 applies unchanged:
    - Python 3.14 and 3.14t;
    - ty strict, ruff ALL, Clippy pedantic, rumdl, and the crate's MSRV (1.85);
    - one commit per task, `scripts/check.sh` green on every commit;
    - author email `4119211+kelsayed17@users.noreply.github.com`, with the trailer
      `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`;
    - branches merge `main` in, and nothing is rebased or force-pushed;
    - `fnloom-core` dependency-free, `#![forbid(unsafe_code)]` and generic over `P` and `V`; `fnloom-py`
      `#![deny(unsafe_code)]`, with its one `#[expect(unsafe_code)]` (the vectorcall in `call.rs`) and no new one;
    - no recursion over graph depth or nesting depth;
    - register rows before their tests; neutral example domains; a CHANGELOG `[Unreleased]` entry per PR, and the
      `from-hamilton.md` rows it changes.
- **The default path is untouched.** A driver without `with_threads` runs `Plan::run`, on the caller's thread, in the
  caller's context, with today's event order (O2). `benches.run` stays within noise, the instruction-count gate stays
  green, and no existing test changes, except the row texts this plan amends.
- **Results never depend on workers or timing** (design goal 3). For `max_workers` in {1, 2, 8}, the same inputs give
  the same values, `Collect` orders, raised exception class (with one failure), cache keys, `RunEnd.skipped` and
  `RunEnd.absent_errors` (O6).
- **The free-threading rules** below hold in every PR. Each rule is pinned by a test in the task that first needs it.
- **No new dependency**, runtime or dev. Threads are `std::thread`; locks are `std::sync`; the test watchdog is
  `faulthandler` (O21).
- Every new setting (`with_threads`, `with_limits`, `with_absent_on`) survives pickling, `copy`, `rebuild()` and
  `build_async`, as Plans 5 to 10 settled. The pool and the limits' counters are never pickled or shared between
  driver objects (O25).
- **Every wait in a test is bounded.** A test that starts threads is marked `threads`. Its barriers, joins and events
  take a timeout, and its parallel runs a `timeout=` where they can, so a deadlock usually fails that one test. The
  `faulthandler` watchdog is the last resort. It ends the whole pytest session, printing every Python thread's stack
  but no Rust frames (O21).

## Free-threading safety rules

These are the rules every task follows. "Attached" means the thread is attached to the interpreter (PyO3
`Python::attach`), which on the GIL build means it holds the GIL.

**Shared and immutable**, read by any thread with no lock:

- the core `Graph` and `Plan`, and the binding's `Plan` (its `calls` and keywords), reached through frozen pyclasses
  (`Py::get` needs no attachment);
- `NodeDef`s, `NodeView`s and the driver's settings;
- `Limits`' capacities.

**Shared and mutable**, with the one guard each may take:

| State | Owner | Guard | Held while calling Python? |
|---|---|---|---|
| `Limits` counters: workers, per node, per group | one per driver | atomics | never |
| the limits' wake: a release counter, waiting threads, async wakers | one per driver | `Mutex<Wake>` and a `Condvar` | never; wakers are called after it |
| the pool: idle threads, runs wanting work | one per driver | `Mutex<PoolState>` and a `Condvar` | never |
| one run's `Scheduler` | one per run | `Mutex<Option<Scheduler>>` and a `Condvar` | never |
| one run's hooks: `Dispatch` and the observers | one per run | the run's **hook busy flag**, `Mutex<bool>` and a `Condvar` | the flag is; no guard is |
| one run's `LateKeys` tables | one per run | a `threading.Lock` around the tables only | no: store calls run outside it |
| a run's check results | one per run | none: keyed by `(node, branch)`, so no two writers share a key | no |
| `MemoryCache`'s LRU | one per store | its `Mutex<Lru>`, as today: the exemption to rule 1 | never; evicted values drop after it |
| the hit-plan cache of `CacheRun` | one per prepared plan | `_plans_lock`, as today | no |
| `PreparedPlan._async`, the async plans made from it | one per prepared plan | a new `threading.Lock`, `_async_lock` | no |
| `PrintNodes` timings | one per hook | its own `threading.Lock`, as today | no |

**How a thread takes a lock:**

1. **No thread waits for a Rust lock while attached.** It first calls `try_lock` while attached. If that fails, it
   runs the whole critical section inside `py.detach(|| ...)`: the closure locks, does the scheduler or pool work,
   and returns only `Send` data, a `Next` or a `Report` of `Arc<Py<PyAny>>`. PyO3 0.30's `detach` requires `T:
   Ungil`, which is `Send` on stable. So no `MutexGuard` ever crosses `attach` or `detach`.
    - **The exemption** is `MemoryStore::lock` (`store.rs`), which takes the LRU's lock blocking while attached, as
      today. It holds the lock for O(1) Rust-only work and never calls Python inside, so a waiter is delayed only by
      a holder that is already running.
2. **No Rust lock is held while calling into Python, and no `Py` is dropped while one is held.** The scheduler hands
   released values back in `Report.released`, and the caller drops them after the lock, attached.
3. **The hook busy flag is the one thing held across Python.** A thread waits detached until the flag is clear, sets
   it, and returns from `detach`. It then attaches, calls the hooks, clears the flag and notifies one waiter.
    - On the GIL build the flag's holder may wait for the GIL. Whoever holds the GIL is never waiting for the flag,
      since that wait is detached, so the two can't deadlock.
4. **Waiting** for work, for a slot or for the run to end is a `Condvar` wait inside `detach`. The calling thread
   waits in slices of 50 ms (O22), and calls `check_signals` between them, attached.
5. **Poison.** Every lock but the scheduler's recovers with `PoisonError::into_inner`, since each guards a counter, a
   flag or a queue that is consistent at every unlock. A poisoned scheduler lock marks the run broken. Every thread
   leaves it, and the run raises `RuntimeError: fnloom's scheduler panicked: <message>`.
6. **Panics.** Node calls already go through `caught` (`errors.rs`). The worker loop's body also runs inside
   `catch_unwind`, so a panic outside a node call fails the run as a `PanicException`, raised once. The thread then
   goes back to the pool.

**Values across threads.** A run's values are `Arc<Py<PyAny>>` inside the scheduler: `Py` is `Send` and `Sync`, and
cloning an `Arc` for a task's arguments is pure Rust. A `Py` is only ever dropped attached (rule 2), never through
PyO3's deferred pool.

**Observers across threads.** `PyObserver<'py, 's>` is not `Send`. The parallel path therefore keeps its observers,
`on_value` and `late` as `Py<...>` in `shared.rs`'s `SharedObservers`, and builds a `PyObserver` per event while
attached, under the busy flag.

**What runs under the busy flag.** Only the hooks, and reading `Dispatch.refused`. `on_value` and `late.store` write
to the store after the flag is cleared and before the task's `complete`, so no store I/O, and no custom store's
code, runs while the flag is held. They still store nothing a hook refused (R8), and still store before any
downstream node runs.

**Contexts.**

- On a driver with `with_threads`, the run takes `contextvars.copy_context()` once when it starts. Each task call
  runs in its own `copy()` of that snapshot, whatever `max_workers` is (§6.2).
- `current_branch()`, `cancelled()` and `_threads.HELD` are set in that copy.
- A `ContextVar.set` in one node is never seen by another, and no `Context` is entered by two threads.

**Ancestry for runs inside runs.** The slots a run's tasks hold are recorded in `_threads.HELD`, a context variable
holding `(limits id, group id)` pairs. It is set in each task's context, or, on the sequential path, set and reset
around a limited node's call. A run started from inside a node reads it at its start and keeps it on the run, so the
re-entrancy check holds at any depth and on any thread (Task 6).

**Hooks.** A run's hook calls never overlap, and a hook runs on the thread that ran the node.

- With one worker, events come in exactly `Plan::run`'s order. With several, they come in the order nodes start and
  finish, each carrying `order`, the task's order key, so a hook can sort them into reference order (§6.4, O5).
- A hook shared by several runs (several threads, or `run_many`) sees their events interleaved, as today. A hook that
  keeps state across runs must be thread-safe.

**Caches and stores.**

- Stores are called from workers, attached, with no fnloom lock held. `MemoryCache` and `DiskCache` are already safe
  from several threads (caching design §7); a custom `CacheStore` must be, as today.
- The single-flight table is per run, inside the scheduler.
- `LateKeys` is per run. Every call names its branch, and its tables are locked as above (Task 4).
- Keys never contain timing or worker identity.

**Limits wake whoever waits.** Releasing a slot goes through the binding's `Limits`. It bumps the wake's counter,
notifies the `Condvar`, then calls any async wakers registered, attached and after the lock. So a run of the same
driver waiting on that slot is woken, on any thread or loop (Tasks 6 and 7).

**The pool's lifetime** (O23):

- **Start and naming.** Pool threads start lazily at the first parallel run and are shared by the driver's runs.
  Each is named `fnloom-worker-<n>` in Rust, and on its first attach it renames its Python `_DummyThread` to the same
  name, so `threading.current_thread().name` says it (O28).
- **When threads exit.** They exit when the driver's `Threads` is collected, or at interpreter exit. `Threads` is
  `#[pyclass(weakref)]`, so `_threads` can keep a `WeakSet` of live ones for its `atexit` hook.
- **Joining.** Neither the collector nor the `atexit` hook ever joins a busy thread. They join idle threads, waiting
  at most 1 s in all, and set a `finalizing` flag. A busy worker checks that flag before it attaches, and exits
  without attaching once it is set.
- **Abandoned tasks.** A task abandoned by a deadline releases its slots at once, and the pool starts a replacement
  thread. The abandoned thread exits when its call returns (O27).

**Fork.** Each `Threads` and `Limits` records the pid that made it.

- A driver whose pid differs builds a fresh pair at its next run, and keeps the inherited pair in a module list that
  is never dropped. Dropping it would lock or free mutexes another process's threads held.
- Nothing inherited is ever locked.
- A run inherited across a fork, as when a node calls `os.fork()`, raises `RuntimeError: this run was started in
  process <pid>; a run doesn't survive os.fork()` in the child, before touching any lock.

**Your code.** A node that mutates a shared value races with its siblings now, as it raced with other threads' runs
before. Within one run, hooks are serialized and nothing else is (§6.7).

## Calls this plan makes

These are the places where the design leaves the shape to the builder, or where this plan departs from its wording.
The ones the maintainer should rule on are also in [Open questions](#open-questions), by their O-number.

1. **`Scheduler` borrows nothing.** Every method takes `plan: &Plan` and `graph: &Graph<P>`, the ones it was made
   with. So a scheduler can live in an `Arc<Mutex<Option<Scheduler>>>` that persistent pool threads share.
   `finish(self)` takes it out of the `Option`. A step-count mismatch is a `RunError::BrokenPlan`, not a panic.
2. **One task call path in the binding.** `crates/fnloom-py/src/task.rs` calls a payload, iterates, pulls, finishes,
   gathers and looks up. `Caller` (the `Plan::run` path) and the parallel workers both use it, so the return check,
   `current_branch()`, wrappers, limits and absence behave the same on both.
3. **One entry point.** `_core.Plan.run(..., threads=None, limits=None, timeout=None)`. With `threads`, the run is on
   the scheduler; without, on `Plan::run`, with `limits` granting around limited nodes. `CacheRun`, the hooked plans
   and `run_many` pass the driver's objects through, so no Python call site forks.
4. **A branch's cache hits come from a `Probe` task.** When a branch's item arrives, the scheduler hands out one
   `Probe`. It asks `lookup` for the branch's steps in reverse need order, the walk `Plan::run` does. That walk moves
   out of `branch.rs` into `pub(crate) fn needs`, used by both. A probe's outcome also carries each needed step's key,
   for single-flight.
5. **A computed expand is iterated on the worker that computed it**, which completes with the iterator. An expand
   override or hit becomes an `Iterate` task.
6. **A collect step's arguments arrive gathered.** Its task carries `ArgValue::Gather(Vec<V>)` per collect edge, and
   the binding builds the `list`s attached.
7. **Stop is scheduler state.** After a failure, a deadline or an interrupt, `next()` hands out no new compute. It
   then hands out one `Finish { exhausted: false }` per open iterator with no pull in flight, innermost first. An
   iterator whose pull is in flight is finished by the worker whose pull returns, so a generator is never closed while
   it runs.
8. **Order keys are public.** `Plan::order_key(node, branch) -> OrderKey` gives the key of any step, on both paths.
   `NodeStart.order`, `NodeEnd.order` and `ExpandEnd.order` carry it as a `tuple[int, ...]`. `RunEnd.skipped` and
   `RunEnd.absent_errors` are sorted by it on every driver, so they equal the one-worker run (O5, O6; amends Q84).
9. **`LateKeys` is told the branch.** `records(name, branch)`, `pulled(name, branch, item)` and `finished(name,
   branch, value)` name the expand's own branch. `_blocks.Items` reports `pulled` and `finished` itself, from
   `Items._branch`. `_pulling` is keyed by `(name, prefix)`, and `_current` goes.
10. **Check results are keyed by `(node, branch)`** in the run's sink (`_checks.RESULTS`), and `Dispatch` pops them
    by the same pair.
11. **Error absence through the core.** `Evaluate::absent_on_error(&mut self, node, error) -> Option<V>` (default
    `None`) and `Observer::absent_error(&mut self, node, branch, error)` (default no-op) apply in `Plan::run` and the
    `Scheduler` alike. `NodeSpec.absent_on_error: bool` makes a node may-be-absent without letting it return `ABSENT`
    itself: the return check still reads the annotation.
12. **`absent_on`'s annotation check runs at build**, not when decorating (O11). On 3.14, annotations are evaluated
    lazily, and fnloom resolves them at build (Q81).
13. **`fnloom.limit`, `fnloom.cancelled` and the thread settings' checks live in a new `_threads.py`.** So
    `_decorators.py` gains only the `Decorations.limit` and `Decorations.absent_on` fields.
14. **Group sizes are their own setting**: `Builder.with_limits(groups)`, and `with_threads(max_workers=None)` sizes
    only the pool (O26). This departs from §6.2's `with_threads(max_workers, limits)`.
15. **The async driver steps the core scheduler through `_core.Stepper`** (O4). `_async.Schedule` and
    `CachedSchedule`'s own dependency counts go.
16. **`timeout=` needs `with_threads`**, and a run with a deadline does not run node tasks on the calling thread (O7,
    O8).

## Register rows

Design §10 reserved Q118 to Q138. Plan 10 wrote all but **Q133** and **Q134**, which are Plan 11's (Plan 10,
"Register rows"). Q160 to Q165 are reserved for Plan 13 and Q166 is taken, so new rows start at **Q167**.

| Row | Task | Disposition | Note |
|---|---|---|---|
| Q133 | 11 | FIX | `GracefulErrorAdapter`'s sentinel against `absent_on`, design §10's text |
| Q134 | 3 | DROP (processes, clusters), FIX | Executors, design §10's text; limits added by Task 6 |
| Q167 | 4 | FIX | Hamilton's thread executor runs block nodes without the caller's context variables |
| Q168 | 4 | FIX | Hamilton calls node hooks from pool threads concurrently |
| Q169 | 3 | FIX | Hamilton runs only block tasks on its remote executor; nodes outside blocks never overlap |
| Q170 | 9 | FIX | Hamilton's `execute` has no deadline and no cooperative stop |
| Q171 | 10 | FIX | Hamilton has no way to run one driver over many input sets but a loop |

**Evidence for the new rows.** Q167 and Q168 were probed on 2026-11-15, with Python 3.14 and Hamilton 1.90.0's
default builder. Its remote executor is `MultiThreadingExecutor(max_tasks=10)` (`driver.py`, read).

- A `ContextVar` set by the caller read `"unset"` in all four branch nodes, and `"caller"` in a node outside the
  block, since `PoolExecutor.submit_task` calls `ThreadPoolExecutor.submit` without copying the context.
- A hook's `run_before_node_execution` was entered by four threads at once.
- Q169 is design §11: "Only block tasks go to the 'remote' executor".
- Q170 and Q171 are read from `Driver.execute`'s signature and the driver's public members.
- The same probe found that a failing branch's `execute` raises only after the running branches finish: after 0.39 s,
  against a sibling that sleeps 0.3 s. Q128's KEEP relies on this.

**Amended rows:**

| Row | Task | What changes |
|---|---|---|
| Q32 | 8 | An async generator annotated `-> Parallelizable[T]` is a node on `build_async()` |
| Q65 | 7 | With `with_threads`, a sync node on the async driver runs on the driver's pool; `sync_nodes="inline"` with it is refused |
| Q66 | 3, 9 | Task 3: the sync driver waits for running steps on failure. Task 9: a deadline abandons them |
| Q84 | 4, 11 | `order` on `NodeStart`, `NodeEnd` and `ExpandEnd`, and `RunEnd.skipped` sorted by it (Task 4); `RunEnd.absent_errors` (Task 11) |
| Q85 | 5 | A single-flight duplicate reports a hit |
| Q86 | 4 | On a driver with `with_threads`, hooks run on the node's thread, serialized per run |
| Q119, Q122, Q123, Q138 | 3 | "Plan 11 reruns it" becomes the parallel rerun's result |
| Q128 | 3 | The parallel stop: running branches finish, and the lowest order key's failure is raised |
| Q132 | 8 | FIX *Planned* becomes FIX: blocks run on `AsyncDriver` |
| Q137 | 3 | `with_threads(...)` is no longer *Planned* |
| Q157 | 11 | DEFER becomes FIX, pointing at Q133 |

## Differential tests against Hamilton

The oracle is `apache-hamilton==1.90.0`, already a dev dependency, on both builds in CI.

- **Executors.** `run_hamilton_blocks(..., executor=)` in `tests/differential/harness.py` takes one of:
    - `"synchronous"`, as today;
    - `"default"`, with nothing set, which is the builder's `MultiThreadingExecutor(10)`;
    - `("threads", n)`, which is `MultiThreadingExecutor(n)`.

    A new `run_fnloom_blocks(..., threads: int | None = None)` builds with `with_threads(threads)` when given. Each
    KEEP row with blocks runs fnloom with `threads` in {None, 1, 2, 8}, against Hamilton's `"synchronous"`,
    `"default"` and `("threads", 8)`.
- **KEEP rows rerun in parallel:**
    - Q119, collect order, with item *i* sleeping `(n - i) × 10 ms` so branches finish in reverse;
    - Q120, an empty expand;
    - Q121, a list returned, and the expand as an output;
    - Q127, edges;
    - Q128, the exception class and finished branches dropped. Also the probed wait: both runtimes raise only after a
      sibling branch that sleeps 0.3 s finishes;
    - Q138, two blocks with shared inputs.
- **FIX rows rerun:** Q122 (nesting) and Q123 (two collects) under `threads=8`, against Hamilton's `"default"`
  result.
- **New `deliberate` rows:** Q133, Q134, Q167, Q168, Q169, Q170 and Q171. Each is a test function in
  `tests/differential/test_deliberate_differences.py` asserting Hamilton's behaviour and fnloom's side by side.
- **What is compared:** values and exception class names only. Never timing, and never event order: Hamilton's
  thread order is nondeterministic, and fnloom's is checked against itself (below).
- **Concurrency without timing.** Where a row needs "these ran at once" (Q134, Q168, Q169), the nodes or hooks meet
  at a `threading.Barrier(n, timeout=5)`. It passes only when *n* run concurrently, on either build, since waiting on
  a barrier releases the GIL. "Never at once" is checked with a lock taken by `acquire(blocking=False)`, which must
  never find it held.
- **fnloom against itself.** `tests/test_threads_equivalence.py` is the design's executor-equivalence property:
    - hypothesis graphs with blocks, nested blocks, declines and one failing node;
    - each run with no `with_threads`, and with `max_workers` in {1, 2, 8};
    - their values, `Collect` orders, exception classes, `RunEnd.skipped`, `RunEnd.absent_errors` and cache keys are
      equal;
    - with one worker, the full hook event sequence equals the run without `with_threads`.

## Benchmark targets

From design §12.2. The design names the rows and one number: under 2 µs per branch node, sequential, which Plan 10
met at 0.5 µs. The thresholds for the parallel rows are this plan's (O19).

| Row (§12.2) | Compared with | Target |
|---|---|---|
| fan-out of 1, 100 and 10,000 trivial branches, sequential | Hamilton synchronous | no regression from 0.5.0's 0.5 µs a branch (within 10%) |
| the same with `with_threads(8)`, trivial branches | Hamilton `MultiThreadingExecutor(8)` | faster on every count; under 10 µs a branch |
| 100 sleeping branches (10 ms each), `with_threads(8)` | one worker, and Hamilton `MultiThreadingExecutor(8)` | at least 6× over one worker on 3.14 and 3.14t; no slower than Hamilton |
| 100 CPU-bound pure Python branches (about 1 ms each), `with_threads(8)`, 3.14t | one worker, and Hamilton `MultiThreadingExecutor(8)` | at least 5× over one worker; faster than Hamilton |
| a layered graph without blocks, `with_threads(1)` | `Plan::run` | in the core, at most 2× `Plan::run`'s instructions per node; from Python, at most 1.5 µs more per node |
| a cached fan-out of 1,000 branches, warm and with one appended item, `with_threads(8)` | the same with one worker | no slower than one worker by more than 20% |
| `ABSENT` through a 200-node chain | 0.5.0 | unchanged: the plain loop is untouched |
| the async benchmarks (`benches.async_`), every shape, on the scheduler | 0.5.0 | within 10% in both `sync_nodes` modes |
| the default path (`benches.run`, every shape) | 0.5.0 | within noise; the instruction-count gate green |

**Where each is measured:**

- **Wall-clock rows** are measured locally, on the benchmarks page's machine (an Apple M2 Pro; `docs/benchmarks.md`),
  on both builds.
- **The CPU-bound row** needs 8 or more cores. `benches/parallel.py` records `os.process_cpu_count()` with each
  result, and on fewer cores reports the row without judging it. On 3.14 with the GIL, only the sleeping rows are
  reported for parallel speedup (§12.2).
- **The 2× instruction ratio** is checked in the `instruction-counts` job. Task 13 gives `scripts/bench_gate.py` a
  `--ratio NUMERATOR:DENOMINATOR:LIMIT` option. CI runs it as `--ratio
  core::engine::schedule.layered_1000:core::engine::run.layered_1000:2.0`, and the same for 10,000. The gate's 2%
  rise check then covers the `schedule` rows once their baseline is committed.
- **CodSpeed** counts simulated instructions on one core, so it gets the `with_threads(1)` rows only.

## CI and 3.14t

- **Already in place:** the `check` job runs `scripts/check.sh` on Ubuntu, macOS and Windows, for 3.14 and 3.14t, and
  the release builds one wheel per interpreter. Plan 11's thread tests run in it: on 3.14 they prove correctness, on
  3.14t parallelism.
- **The GIL stays off.** `test_a_parallel_run_keeps_the_gil_off` runs on 3.14t only, in a fresh interpreter (a
  subprocess), so no import by another test can re-enable the GIL first. It imports `fnloom`, runs a
  `with_threads(4)` fan-out, and asserts `not sys._is_gil_enabled()`.
- **Bounded waits, then a watchdog** (O21). A `threads` marker is registered in `pyproject.toml`, which
  `--strict-markers` requires. An autouse fixture in `tests/conftest.py` arms `faulthandler.dump_traceback_later(300,
  exit=True)` for marked tests, and cancels it after.
    - When it fires, it prints every Python thread's stack and ends the session.
    - Rust frames are not shown. A worker waiting inside `detach` shows its last Python frame, or none.
    - The `check` job gets `timeout-minutes: 60`.
- **A stress job** (O20), `stress` in `.github/workflows/ci.yml`, on `ubuntu-latest` for 3.14 and 3.14t, runs
  `FNLOOM_STRESS_ROUNDS=200 HYPOTHESIS_PROFILE=stress uv run --no-sync pytest -m threads`.
    - The stress tests read `FNLOOM_STRESS_ROUNDS` (default 10 in `check.sh`).
    - `tests/conftest.py` registers a hypothesis profile `stress` with `max_examples=500`.
    - The job is not in `check.sh`, which stays the local gate.
- **Benchmarks.** `benchmarks-smoke` gains 3.14t in a matrix, so the parallel benchmarks run once on the build they
  are for. The CodSpeed job stays on 3.14 and skips the parallel rows.
- **The instruction-count gate.** New `schedule` rows in `crates/fnloom-core/benches/core.rs` are reported, not
  gated, until Task 13 commits their baseline with `--write-baseline` in the job's own image, and adds the `--ratio`
  checks.
- **Windows.** Interrupt tests use `_thread.interrupt_main()`, not `os.kill`, so they run on all three systems. The
  fork tests are skipped on Windows, which has no `os.fork`.
- **No ThreadSanitizer job** (O20). It needs a TSan build of CPython, which neither `uv` nor the runners provide.
    - The core is `#![forbid(unsafe_code)]`.
    - The binding's one `unsafe` block is the vectorcall in `call.rs`. It is called attached, by the thread that owns
      its arguments, on `Bound` values it does not share.
    - So data races are left to Rust's types, the stress job and the review.

## File Structure

```text
crates/fnloom-core/src/
  schedule.rs  NEW  Scheduler, Task, Work, ArgValue, Outcome, Next, Report, OrderKey, Seat
  limits.rs    NEW  Limits: atomic counters for workers, nodes and groups; the Grant trait
  plan.rs      Plan::order_key
  branch.rs    the reverse-need walk moved out as `needs`, shared with schedule.rs
  run.rs       Evaluate::absent_on_error, Observer::absent_error (Task 11)
  node.rs      NodeSpec.absent_on_error, Node.absent_on_error (Task 11)
  graph.rs     may_be_absent from absent_on_error (Task 11)
  lib.rs       pub mod schedule, limits
crates/fnloom-core/benches/core.rs   schedule rows
crates/fnloom-core/tests/schedule_threads.rs   NEW  the scheduler driven from 8 std threads
crates/fnloom-py/src/
  task.rs      NEW  one task's call: payload, return check, branch, block ops, lookup, limits, absent_on
  limits.rs    NEW  Limits (pyclass): the core Limits, its wake, async wakers, the grant around a sequential call
  pool.rs      NEW  Threads (pyclass, weakref): the pool, naming, submit, abandonment, close, fork, live_workers
  parallel.rs  NEW  one parallel run: the worker loop, the busy flag, contexts, deadline, signals, run_many
  shared.rs    NEW  SharedObservers: observers, on_value and late as Py, bound per event
  stepper.rs   NEW  Stepper (pyclass): the scheduler stepped from the event loop
  plan.rs      Plan.run(threads=, limits=, timeout=), Plan.run_many, Plan.start; Caller over task.rs
  observer.rs  order on node events; absent_error
  graph.rs     the node tuple gains absent_on_error (Task 11)
  lib.rs       registers Limits, Threads, Stepper, Stop; test-hooks: live_workers, arm_worker_panic
python/fnloom/
  _threads.py  NEW  limit, cancelled, CANCELLED, HELD, the setting checks, make_limits, make_threads, atexit, fork
  driver.py    Builder.with_threads, with_limits, with_absent_on; timeout=; run_many; PreparedPlan._async_lock
  _async.py    _Run over Stepper; sync nodes on Threads.submit; limit waits; blocks; async generator pulls
  _blocks.py   the async refusal goes; Items reports pulled and finished with its branch
  _scan.py     async generator expands; NodeDef.limit, NodeDef.absent_on
  _decorators.py  Decorations.limit, Decorations.absent_on
  _absence.py  absent_on, the build check, with_absent_on coverage
  _caching.py  LateKeys by explicit branch, locked; probe keys; error-absence poison; finish outside the flag
  _checks.py   check results keyed by (node, branch)
  _hooks.py    order on events; RunEnd.skipped and absent_errors sorted by order; results by (node, branch)
  _core.pyi    stubs for every binding change
  __init__.py  limit, cancelled, absent_on
tests/
  test_threads_equivalence.py  test_parallel.py  test_limits.py  test_stopping.py   NEW
  test_run_many.py  test_absent_on.py  test_async_blocks.py   NEW
  test_threads.py  test_async.py  test_hooks.py  test_blocks_caching.py  test_checks.py
  test_pickling.py  test_public_api.py  test_stacking.py  conftest.py
  differential/harness.py  differential/test_deliberate_differences.py  differential/test_semantics.py
benchmarks/test_parallel.py (NEW)   benches/parallel.py (NEW)   benches/generate.py
scripts/bench_gate.py (--ratio)   scripts/test_bench_gate.py
docs/running.md (Threads, Limits, Deadlines, Partial results), hooks.md, caching.md, writing-nodes.md,
  from-hamilton.md, quirk-register.md, benchmarks.md, rust-core.md, index.md, README.md, CHANGELOG.md,
  the roadmap, and the design's §12.3 and §14
.github/workflows/ci.yml   pyproject.toml (the threads marker)
```

## PR breakdown

Each PR follows Plan 10's steps: TDD task by task with each task's register rows first, `scripts/check.sh` green on
every commit, a CHANGELOG entry, an independent reviewer subagent, then the maintainer's go-ahead to squash-merge.

| PR | Tasks | Needs merged first | Runs in parallel with |
|---|---|---|---|
| **C1** The scheduler in the core (Rust only) | 1, 2 | this plan's approval | none |
| **C2** The parallel sync driver | 3, 4 | C1 | none |
| **C3** Single-flight and limits | 5, 6 | C2 | none |
| **C4** The async driver on the scheduler, with blocks | 7, 8 | C3 | C5 |
| **C5** Stopping: deadlines, cancellation, signals | 9 | C3 | C4, D1 |
| **C6** `run_many` | 10 | C5 | D1 |
| **D1** Partial results | 11 | C4 | C5, C6 |
| **C7** Free-threading stress and the Threads docs | 12 | C4, C5, C6, D1 | none |
| **Z** Close-out | 13 | C7 | none |

The order, and why:

- **C1** has no Python.
- **C2** is the spine every later PR edits, so it runs alone. It already waits for running tasks on a failure and
  raises the lowest order key's error, since its own tests need that.
- **C3** runs alone because C4's async limits test uses its `fnloom.limit` and `Limits` wake.
- **C4 and C5** both need C3, and touch different parts of the binding: the async driver against the deadline loop.
- **D1** needs C4, since absence on error must hold on the async driver's scheduler too.
- **C3 and D1 never collide.** C3 and D1 share `_decorators.py`, `_scan.py`, `_caching.py`, `__init__.py`,
  `tests/test_public_api.py` and `tests/test_stacking.py`. Since D1 needs C4, which needs C3, D1 always starts from
  C3's version of them.

**Textual collisions between parallel PRs**, each resolved by merging `main` in, never by rebasing:

- **C4 and C5:**
    - `crates/fnloom-py/src/pool.rs`: C4 adds `submit`, C5 abandonment and replacement threads;
    - `python/fnloom/driver.py`: C4 `build_async`, C5 `timeout=`;
    - `python/fnloom/_core.pyi`: different classes;
    - `docs/quirk-register.md`: Q32, Q65 and Q132 against Q66 and Q170;
    - `CHANGELOG.md`.
- **C5 and D1:**
    - `crates/fnloom-py/src/task.rs`: C5 sets `cancelled()` in the call's context copy, D1 reads a node's error
      classes on the failure path;
    - `python/fnloom/__init__.py` and `tests/test_public_api.py`: C5 adds `cancelled`, D1 `absent_on`;
    - `docs/quirk-register.md`;
    - `CHANGELOG.md`.
- **C6 and D1:**
    - `python/fnloom/driver.py`: C6 `PreparedPlan.run_many`, D1 `Builder.with_absent_on`;
    - `CHANGELOG.md`.

## Review Focus

1. **A branch fails while another worker is inside the expand's generator.** The generator is closed exactly once,
   after its pull returns, with no `ValueError: generator already executing`, and the node's own exception is raised.
   (Task 9, `test_a_failure_during_a_pull_closes_the_generator_after_it`)
2. **A hook raises in `after_node` under eight workers.** The run stops once, and the busy flag is cleared, so no
   other worker hangs on it. `after_run` fires once with `ok=False`, and the hook's exception is raised as itself, as
   on `Plan::run`. (Task 4, `test_a_raising_hook_stops_a_parallel_run_once`)
3. **A node calls `execute` on its own driver**, built with `with_threads(2)` and `with_limits({"db": 1})`, and both
   the outer and the inner node are in group `db`. The inner run raises `RuntimeError` naming `'db'` at once, never
   hangs, also two levels down and when the outer run started on a pool thread. (Task 6,
   `test_an_inner_run_needing_a_group_its_callers_hold_is_refused`)
4. **Two hung nodes abandoned by `timeout=`** on a `with_threads(2)` driver. A third run completes, and the
   interpreter then exits with status 0. (Task 9, `test_hung_abandoned_tasks_do_not_starve_later_runs` and
   `test_the_interpreter_exits_cleanly_with_an_abandoned_task`)
5. **`os.fork()` from inside a running node.** The child's node returns into a `RuntimeError`, and the child's next
   run starts a fresh pool and completes. The parent's run completes too. (Task 3,
   `test_a_fork_inside_a_node_raises_in_the_child`)

---

## PR C1: the scheduler in the core (Rust only)

### Task 1: `Scheduler`, order keys, pulls and probes (design task 16)

**Files:**

- Create:
    - `crates/fnloom-core/src/schedule.rs`;
    - `crates/fnloom-core/src/limits.rs`, holding the `Grant` trait and an `Unlimited` grant only; `Limits` comes in
      Task 2;
    - `crates/fnloom-core/tests/schedule_threads.rs`.
- Modify: `crates/fnloom-core/src/{lib.rs,plan.rs,branch.rs,testing.rs}`, `crates/fnloom-core/benches/core.rs`,
  `docs/rust-core.md`
- Test: `schedule.rs`'s test module, `tests/schedule_threads.rs`

**Interfaces:**

- Consumes: `Plan`, `SubPlan`, `Plan::block_of`, `Plan::is_late`, `Plan::arg_edges`, `Item`, `Slot`, and the
  dependency and collect rules of `run.rs` and `branch.rs`, unchanged.
- Produces, in `schedule.rs`:

    ```rust
    pub struct TaskId(pub u32);
    /// (step position, branch index) pairs from the outermost plan inwards, then the step's position; Ord.
    pub struct OrderKey(pub Vec<u32>);
    pub enum ArgValue<V> { One(V), Gather(Vec<V>), Omitted }
    pub enum Work<V> {
        Compute { args: Vec<(NodeId, ArgValue<V>)>, expands: bool, late: bool },
        Iterate { iterable: V },
        Pull { iterator: V },
        Finish { iterator: V, exhausted: bool },
        Probe { block: BlockId },
    }
    pub struct Task<V> {
        pub id: TaskId, pub node: NodeId, pub branch: Vec<usize>, pub key: OrderKey, pub work: Work<V>,
    }
    pub enum Outcome<V, E, K> {
        Value { value: V, absent: bool },
        Hit { value: V, absent: bool },
        Iterator(V),
        Item(Item<V>),
        Finished(V),
        Needs { hits: Vec<(usize, V, bool)>, keys: Vec<Option<K>> },
        Failed(E),
    }
    pub enum Next<V> { Task(Task<V>), Wait, Done }
    pub struct Report<V> {
        pub released: Vec<V>,
        pub skipped: Vec<(NodeId, Vec<usize>, OrderKey)>,
        pub expanded: Option<(NodeId, Vec<usize>, usize)>,
        pub hits: Vec<(NodeId, Vec<usize>, V)>,
    }
    pub enum Seat { Take, Held, Lent, Threadless }
    pub struct Options { pub in_flight: usize }
    impl<V: Clone, E, K: Ord + Clone> Scheduler<V, E, K> {
        pub fn new<P>(plan: &Plan, graph: &Graph<P>, seeded: Vec<Option<V>>, options: Options) -> Self;
        pub fn next<P>(&mut self, plan: &Plan, graph: &Graph<P>, grant: &dyn Grant, seat: Seat) -> Next<V>;
        pub fn complete<P>(&mut self, plan: &Plan, graph: &Graph<P>, grant: &dyn Grant, id: TaskId,
                           outcome: Outcome<V, E, K>) -> Report<V>;
        pub fn stop(&mut self);
        pub fn is_stopping(&self) -> bool;
        pub fn finish<P>(self, plan: &Plan, graph: &Graph<P>) -> Result<Outputs<V>, RunError<E>>;
    }
    ```

- Produces, in `plan.rs`: `pub fn order_key(&self, node: NodeId, branch: &[usize]) -> OrderKey`, the key the
  scheduler gives that step. It is pure, so the `Plan::run` path's events can carry it too.
- Produces, in `limits.rs`: `pub trait Grant: Sync { fn try_start(&self, node: NodeId, seat: Seat) -> bool; fn
  release(&self, node: NodeId, seat: Seat); }`, and `pub struct Unlimited;`, which grants everything.
- Produces, in `branch.rs`: `pub(crate) fn needs<P, V>(plan: &Plan, graph: &Graph<P>, block: BlockId, lookup: impl
  FnMut(NodeId) -> Option<V>) -> Vec<Need>`, which `Runner` now calls. `Plan::run`'s behaviour is unchanged.
- Rules:
    - Ready tasks are handed out lowest `OrderKey` first, skipping any the grant refuses, so one worker reproduces
      `Plan::run`.
    - A pull is ordered just before its branch's first step (design §4.3). At most one pull of an iterator is in
      flight.
    - A `late` outer compute is looked up by the binding first. A `Hit` outcome completes it without a call.
    - Skips follow §7.2's three rules, and are reported in `Report.skipped` with their keys, not as tasks.
    - `Report.released` holds the values whose last reader completed, for the caller to drop attached.
    - `Next::Wait` means tasks are in flight, or refused by the grant. `Next::Done` means nothing is left. Once the
      run is stopped, only `Finish` tasks are handed out (Task 2 adds the stop rules).
- [ ] **Step 1: Write the failing Rust tests.**
    - `one_worker_matches_plan_run_task_for_task`: over diamond, chain, layered, nested-block and absence graphs from
      `testing.rs`, stepping with `Unlimited` one task at a time gives the same outputs, and the same sequence of
      `(node, branch)` calls, as `Plan::run` with a recording evaluator;
    - `order_keys_compare_by_step_then_branch`: `(0, 3, 1) < (0, 3, 2) < (1)`, and a pull's key precedes its
      branch's first step;
    - `order_key_matches_the_key_the_scheduler_gives`: for every task of a nested fan-out;
    - `ready_tasks_are_handed_out_lowest_key_first`: two independent steps both ready, and `next` gives the lower;
    - `a_refused_task_is_skipped_for_the_next_ready_one`: a grant refusing node `a` makes `next` hand out `b`, and `a`
      once the grant allows it;
    - `one_pull_per_iterator_in_flight`;
    - `a_collect_waits_for_every_branch_and_gathers_in_branch_order`, with branches completed in reverse;
    - `a_probe_hit_skips_the_branch_upstream_and_is_reported`;
    - `a_late_outer_hit_completes_without_a_call`;
    - `skips_follow_the_three_rules_and_are_reported_with_keys`;
    - `released_values_are_returned_to_the_caller`, with the `Counted` drop counter: nothing is dropped inside the
      scheduler;
    - `an_expand_requested_alone_finishes_its_items_as_its_value`;
    - `a_plan_from_another_graph_is_a_broken_plan_error`;
    - `no_recursion_on_a_1000_deep_nesting_or_a_50000_chain_in_a_block`;
    - in `tests/schedule_threads.rs`, `eight_threads_behind_a_mutex_give_plan_runs_outputs`: eight `std::thread`s share
      a `Mutex<Option<Scheduler>>` and a fake evaluator with random sleeps, over a nested fan-out of 1,000 items, and
      the outputs equal `Plan::run`'s. This also proves `Scheduler: Send` for `Send` values.
- [ ] **Step 2: Run them to see them fail.** Run `cargo test -p fnloom-core schedule`. Expected: they don't compile.
- [ ] **Step 3: Implement.** `Scheduler` keeps:
    - one `Vec<Option<Slot<V>>>` per open branch;
    - the frames of `branch.rs`, as an explicit stack per expand instance;
    - a `BinaryHeap<Reverse<(OrderKey, TaskId)>>` of ready tasks.

    Move the reverse-need walk into `branch::needs`, and call it from `Runner`.
- [ ] **Step 4: Run the tests to see them pass**, then `cargo bench --no-run -p fnloom-core`. Add the rows
  `schedule.layered_{1000,10000}` and `schedule.chain_{1000,10000}`: a one-worker loop over `next` and `complete`, with
  a trivial evaluator. Expected: PASS, and the existing rows' counts unchanged.
- [ ] **Step 5: Document** `Scheduler`, order keys and the task kinds in `docs/rust-core.md`.
- [ ] **Step 6: Run `scripts/check.sh`, then commit** `core: the Scheduler, matching Plan::run task for task`.

### Task 2: limits, the in-flight bound, single-flight and stop, in the core (design tasks 19 to 21, core part)

**Files:**

- Modify: `crates/fnloom-core/src/{schedule.rs,limits.rs}`, `docs/rust-core.md`
- Test: their test modules, `tests/schedule_threads.rs`

**Interfaces:**

- Consumes: Task 1.
- Produces, in `limits.rs`:
    - `pub struct Limits`, implementing `Grant` with atomics only: `Limits::new(max_workers: Option<usize>, node_caps:
      Vec<Option<usize>>, node_groups: Vec<Option<u32>>, group_caps: Vec<usize>) -> Self`, indexed by `NodeId::index`
      and by group id. `None` workers means no worker cap: a driver without `with_threads`.
    - Which slots a task holds:
        - one worker slot, unless its seat is `Held` (a nested run's caller), `Lent` (a slot a waiting caller lent its
          run) or `Threadless` (an async node);
        - plus its node's slot and its group's slot, when it computes or pulls that node.
    - `Limits::held(&self, group: u32) -> usize`, for Task 6's re-entrancy check.
    - `Limits` has no wake: the binding's wrapper adds one (Task 6), since the core has no locks.
- Produces, in `schedule.rs`:
    - `Options.in_flight`: an expand's iterator is pulled only while fewer than `in_flight` of its branches are
      unfinished. The binding sets it.
    - Single-flight. A `Needs` outcome's `keys[i]` is the key of the branch's step `i`, or `None`.
        - A needed step whose key equals a running task's key waits on that task instead of being handed out.
        - When that task completes with a value or a declared absence, the waiter completes as a hit, reported in
          `Report.hits`.
        - When it fails, the waiter becomes ready and runs itself.
    - `Scheduler::blocked(&self) -> Option<NodeId>`: the lowest-key ready task's node, when the grant refused it and
      nothing of this run is in flight.
    - `Scheduler::lend(&mut self)`: one task of this run may take `Seat::Lent`, used by Task 9.
    - Stop.
        - `stop()`, or a `Failed` outcome, ends the handing out of compute, pull and probe tasks.
        - Failures are kept with their order keys.
        - `finish` returns the lowest key's error, with the closing errors of `Finish` tasks appended, as `Plan::run`
          appends them.
- [ ] **Step 1: Write the failing tests.**
    - `limits_cap_workers_nodes_and_groups_across_two_schedulers`: two schedulers share one `Limits(Some(2), ...)`.
      Never more than two tasks are out, never more than one of a node capped at 1, and never more than the group's
      cap;
    - `no_worker_cap_without_max_workers`;
    - `a_held_seat_takes_no_worker_slot`, `a_lent_seat_is_used_once`, and
      `a_threadless_seat_takes_node_and_group_slots_only`;
    - `the_in_flight_bound_holds_pulls_back`: with `in_flight: 4`, a 10,000-item expand never has more than four
      unfinished branches;
    - `a_duplicate_key_waits_and_completes_as_a_hit`;
    - `a_failed_original_lets_the_duplicate_run`;
    - `steps_without_keys_are_never_deduplicated`;
    - `after_a_failure_only_finish_tasks_are_handed_out_innermost_first`;
    - `an_iterator_with_a_pull_in_flight_is_finished_after_the_pull`;
    - `the_lowest_order_key_failure_is_returned`: three failures completed in reverse key order;
    - `blocked_names_the_refused_node`.
- [ ] **Step 2: Run them to see them fail. Step 3: Implement. Step 4: Run them to see them pass**, plus the whole
  `cargo test -p fnloom-core`.
- [ ] **Step 5: Run `scripts/check.sh`, then commit** `core: limits, in-flight bound, single-flight and stop`.

## PR C2: the parallel sync driver

### Task 3: `with_threads`, the pool, attach and detach, failure (design task 17, Q134, Q169, Q128)

**Files:**

- Create:
    - `crates/fnloom-py/src/{task.rs,limits.rs,pool.rs,parallel.rs,shared.rs}`;
    - `python/fnloom/_threads.py`;
    - `tests/test_parallel.py`, `tests/test_threads_equivalence.py`.
- Modify:
    - `crates/fnloom-py/src/`: `plan.rs` (`Caller` over `task.rs`; `run(threads=, limits=, timeout=)`), `lib.rs`;
    - `python/fnloom/`: `driver.py` (`Builder.with_threads`, `_Settings`, `_Changes`, `_builder_of`, `Driver`),
      `_caching.py` (`CacheRun.run` passes `threads` and `limits`), `_core.pyi`;
    - `tests/conftest.py` (the watchdog fixture), `tests/test_pickling.py`, `tests/differential/harness.py`,
      `tests/differential/test_semantics.py`, `tests/differential/test_deliberate_differences.py`;
    - `pyproject.toml` (the `threads` marker), `.github/workflows/ci.yml` (`timeout-minutes`, the `stress` job);
    - `docs/quirk-register.md`: Q134 and Q169 written; Q66, Q119, Q122, Q123, Q128, Q137 and Q138 amended.

**Interfaces:**

- Consumes: Tasks 1 and 2.
- Produces in the binding:
    - `_core.Limits(graph: Graph, max_workers: int | None, node_caps: dict[str, int], node_groups: dict[str, str],
      group_caps: dict[str, int])`, a frozen pyclass over `Arc<core::Limits>` and its wake. In this task, only
      `max_workers` is set; Task 6 adds the caps.
    - `_core.Threads(limits: Limits, max_workers: int)`, a frozen `#[pyclass(weakref)]` holding the pool.
        - `close()` stops its idle threads, joining with a bounded wait, detached. It is idempotent.
        - Under `test-hooks`, the module function `_core.live_workers() -> int` counts live pool threads process-wide,
          and `_core.arm_worker_panic()` makes the next worker-loop iteration panic outside a node call.
    - `_core.Plan.run(inputs, overrides, observers=(), on_value=None, late=None, *, threads: Threads | None = None,
      limits: Limits | None = None, timeout: float | None = None) -> dict`.
        - With `threads`, the run is a `parallel::Run`, an `Arc` holding:
            - `Mutex<Option<Scheduler<Arc<Py<PyAny>>, PyErr, [u8; 32]>>>` and a `Condvar`;
            - the busy flag;
            - the context snapshot;
            - `SharedObservers`;
            - the pid.
        - The calling thread works too, as one of the `max_workers`.
    - The worker loop, inside `catch_unwind`, by the [safety rules](#free-threading-safety-rules):
        1. take a task, by rule 1;
        2. attach, and run the task through `task.rs`;
        3. fire its hooks under the busy flag;
        4. clear the flag, and call `on_value` or `late.store`;
        5. `complete` the task, by rule 1;
        6. drop `Report.released`, attached.
    - The stop on failure. No new task starts. The run waits for the running tasks, then raises the lowest order key's
      error, with the note as today (`while computing 'details' for branch (3,) of 'picked'`). Every failure seen
      reaches `after_node` with `ok=False`.
- Produces in Python:
    - `Builder.with_threads(max_workers: int | None = None) -> Builder[R]`.
        - `max_workers=None` is `os.process_cpu_count() or 1`.
        - A `bool` or another non-int is a `TypeError` naming the argument, and a value under 1 a `ValueError`.
    - `_threads.make_limits(graph, nodes, max_workers) -> _core.Limits` and `_threads.make_threads(limits,
      max_workers) -> _core.Threads`.
    - `_threads.close_all()`, registered with `atexit` over a `weakref.WeakSet` of live `Threads`.
    - `Driver` keeps `_limits: _core.Limits` on every driver, and `_threads: _core.Threads | None`. They are made at
      build, remade when the pid differs, and the pool starts at the first run.
- [ ] **Step 1: Write the register rows** Q134 (design §10's text) and Q169. Amend Q66 (the wait), Q119, Q122, Q123,
  Q128, Q137 and Q138. Then write their tests:
    - `deliberate("Q134")`:
        - Hamilton's default runs four branches at once: a `Barrier(4, timeout=5)` passes;
        - fnloom without `with_threads` runs one at a time: a lock taken with `acquire(blocking=False)` is never found
          held;
        - with `with_threads(4)`, the `Barrier(4, timeout=5)` passes.
    - `deliberate("Q169")`: two independent slow nodes outside any block meet at a `Barrier(2, timeout=5)` under
      fnloom's `with_threads(2)`. Under Hamilton's default executor they never overlap, checked by the non-blocking
      lock.
    - The KEEP and FIX reruns of [Differential tests](#differential-tests-against-hamilton), with the harness's new
      `executor=` and `threads=`. Q128's rerun is part of this.
- [ ] **Step 2: Write the failing fnloom tests** in `tests/test_parallel.py` and `tests/test_threads_equivalence.py`:
    - `test_with_threads_overlaps_independent_nodes`: four nodes meet at a `Barrier(4, timeout=5)` with
      `with_threads(4)`;
    - `test_with_threads_one_runs_on_the_callers_thread`: every node sees `threading.get_ident()` of the caller;
    - `test_pool_threads_are_named_fnloom_worker`: under `with_threads(4)`, a node on another thread than the caller's
      sees `threading.current_thread().name` starting with `fnloom-worker-`;
    - `test_no_pool_thread_starts_before_the_first_run`, and `test_the_pool_holds_max_workers_minus_one_threads`,
      counted with `_core.live_workers()`;
    - `test_a_failure_waits_for_running_branches`: no branch is still running when `execute` raises;
    - `test_the_lowest_order_key_failure_is_raised`: two failing branches, the later one failing first;
    - `test_every_failure_seen_reaches_after_node`: both get `ok=False`, and one exception is raised, never an
      `ExceptionGroup`;
    - `test_results_are_equal_over_worker_counts`, the hypothesis property, marked `threads`;
    - `test_with_threads_survives_pickle_copy_rebuild_and_build_async`;
    - `test_bad_max_workers_are_refused`: `0`, `-1`, `True` and `1.5`;
    - `test_a_fork_inside_a_node_raises_in_the_child` (Review Focus 5; skipped on Windows), and
      `test_a_forked_child_gets_a_new_pool`;
    - `test_a_panic_in_the_worker_loop_fails_the_run_once`, through `_core.arm_worker_panic()`;
    - `test_a_parallel_run_keeps_the_gil_off`: 3.14t only, in a subprocess;
    - `test_a_driver_without_with_threads_calls_plan_run`: a spy on `_core.Plan.run` sees `threads=None`.
- [ ] **Step 3: Run them to see them fail.** Run `uv run maturin develop --features test-hooks && UV_NO_SYNC=1 uv run
  pytest tests/test_parallel.py tests/test_threads_equivalence.py tests/differential -k "parallel or threads or
  Q134 or Q169"`. Expected: `AttributeError` on `with_threads`.
- [ ] **Step 4: Implement**, following the [safety rules](#free-threading-safety-rules). `task.rs` takes over
  `Caller::call`'s body and the `Blocks` methods, so `Caller` is a thin adapter. The `benches.run` rows and the
  instruction-count gate show no change.
- [ ] **Step 5: Run the tests to see them pass**, plus the whole `tests/` suite, on both builds if both are installed.
- [ ] **Step 6: Run `scripts/check.sh`, then commit** `with_threads: a parallel executor on the core scheduler (Q134,
  Q169; amends Q66, Q128)`.

### Task 4: hooks per run, contexts, order keys, branch-safe keys and checks (design task 18, Q167, Q168)

**Files:**

- Modify:
    - `crates/fnloom-py/src/{parallel.rs,shared.rs,observer.rs,task.rs,plan.rs}`;
    - `python/fnloom/{_hooks.py,_caching.py,_blocks.py,_checks.py,_core.pyi}`;
    - `docs/hooks.md`, `docs/quirk-register.md`.
- Test: `tests/test_parallel.py`, `tests/test_hooks.py`, `tests/test_blocks_caching.py`, `tests/test_checks.py`,
  `tests/differential/test_deliberate_differences.py`

**Interfaces:**

- Consumes: Task 3's run.
- Produces:
    - `NodeStart.order`, `NodeEnd.order` and `ExpandEnd.order`, each a `tuple[int, ...]` from `Plan::order_key`, on
      every driver (call 8).
    - `RunEnd.skipped` sorted by order on every driver. On the `Plan::run` path it is already in that order, and a
      test pins that.
    - A run's hooks serialized by its busy flag, each on the thread that ran the node. `on_value` and `late.store` are
      called after the flag is cleared, with `Dispatch.refused` read under it.
    - Each task call runs in `snapshot.copy()`, with `_blocks.BRANCH` set in the copy.
    - `LateKeys` (call 9):
        - `records(name, branch)`, `pulled(name, branch, item)` and `finished(name, branch, value)`;
        - `_pulling: dict[tuple[str, _Branch], list[bytes | None]]`, and no `_current`;
        - a `threading.Lock` around `_memo`, `_items`, `_pulling` and `_replayed`, with no store call inside it.
    - The binding stops calling `pulled` and `finished` (`plan.rs`'s `next_item` and `finish_items`). `Items` makes
      those calls with its `_branch`, given `late` by the factory.
    - `_checks.RESULTS`' sink is keyed by `(node, branch)`, with the branch from `current_branch()`. `Dispatch` pops
      `(name, branch)` (call 10).
- [ ] **Step 1: Write the register rows** Q167 and Q168, and amend Q84 (`order`, sorted skips) and Q86. Then write
  their tests:
    - `deliberate("Q167")`: a caller's `ContextVar` reads `"unset"` in Hamilton's block nodes and `"caller"` in
      fnloom's, under `with_threads(4)`.
    - `deliberate("Q168")`: Hamilton's before-node hook waits at a `Barrier(4, timeout=5)`, which passes, since four
      threads call it at once. fnloom's hook, under `with_threads(4)`, takes a lock with `acquire(blocking=False)` and
      never finds it held.
- [ ] **Step 2: Write the failing tests.**
    - `test_hook_calls_of_one_run_never_overlap`: the non-blocking-lock hook, sleeping 5 ms, over 200 branches on
      eight workers;
    - `test_a_hook_runs_on_the_thread_that_ran_the_node`;
    - `test_with_one_worker_events_equal_the_sequential_run`, orders included;
    - `test_order_sorts_parallel_events_into_reference_order`: two blocks interleaved, sorted to a0, b0, a1, b1 when
      that is the reference order;
    - `test_skipped_is_sorted_by_order_on_both_paths`;
    - `test_a_contextvar_set_in_one_node_is_not_seen_by_another`, for worker counts 1, 2 and 8;
    - `test_current_branch_on_workers_is_each_nodes_own`;
    - `test_a_raising_hook_stops_a_parallel_run_once` (Review Focus 2);
    - `test_a_slow_store_does_not_hold_the_busy_flag`: a custom store whose `put` waits on a `Barrier(2, timeout=5)`
      with a hook of another branch passes;
    - `test_print_nodes_and_log_failures_under_threads`;
    - `test_nested_expands_pulled_concurrently_key_each_branch`: inner expands of different outer branches are pulled
      interleaved under `with_threads(8)`, and every key equals the sequential run's;
    - `test_check_results_are_per_branch`: two branches of a checked node under eight workers each report their own
      `NodeEnd.checks`.
- [ ] **Step 3: Run them to see them fail. Step 4: Implement. Step 5: Run the tests to see them pass**, plus `pytest
  tests/test_hooks*.py tests/test_print_nodes.py tests/test_blocks_caching.py tests/test_checks.py`.
- [ ] **Step 6: Run `scripts/check.sh`, then commit** `Hooks, contexts, keys and checks per branch under threads (Q167,
  Q168; amends Q84, Q86)`.

## PR C3: single-flight and limits

### Task 5: single-flight per key (design task 19)

**Files:**

- Modify: `crates/fnloom-py/src/{parallel.rs,task.rs}`, `python/fnloom/_caching.py` (`LateKeys.key` exposed to the
  probe), `docs/caching.md`, `docs/quirk-register.md`
- Test: `tests/test_blocks_caching.py`

**Interfaces:**

- Consumes: Task 2's single-flight, and Task 3's probe.
- Produces:
    - The binding's `Probe` asks `late.lookup(node, branch)` as today. It also asks `late.key(node, branch)` for each
      needed step, passing the 32-byte digest, or `None` for an uncacheable or `recompute` step.
    - A duplicate's events are a hit's (Q85): `cache` outcome `hit`, and no wrapper call.
    - `docs/caching.md` says single-flight is per run only (see the non-goals).
- [ ] **Step 1: Amend Q85**: a single-flight duplicate reports a hit.
- [ ] **Step 2: Write the failing tests.**
    - `test_equal_items_compute_once_per_run`: items `["a", "a", "b"]`, a node counting its calls under a
      `threading.Lock`, `with_threads(4)` and a `MemoryCache`. `a`'s branch node is called once, and the second
      branch fires `cache` outcome `hit`;
    - `test_a_failed_original_does_not_fail_its_duplicate_by_itself`: the duplicate runs, and its own outcome decides;
    - `test_recompute_nodes_are_never_deduplicated`;
    - `test_without_a_cache_both_run`;
    - `test_two_runs_do_not_share_single_flight`: two concurrent runs of one driver both call the node;
    - `test_single_flight_never_waits_a_worker`: with `with_threads(2)`, a duplicate of a running slow node frees its
      worker for other branches, so a `Barrier(2, timeout=5)` between the slow node and another branch passes.
- [ ] **Step 3: Run them to see them fail. Step 4: Implement. Step 5: Run the tests to see them pass.**
- [ ] **Step 6: Run `scripts/check.sh`, then commit** `Single-flight per key within a run (amends Q85)`.

### Task 6: `limit`, `with_limits`, the wake, the in-flight bound and runs inside runs (design task 20)

**Files:**

- Create: `tests/test_limits.py`
- Modify:
    - `python/fnloom/{_threads.py,_decorators.py,_scan.py,driver.py,__init__.py,_core.pyi}`;
    - `crates/fnloom-py/src/{limits.rs,task.rs,plan.rs,parallel.rs}`;
    - `docs/running.md` ("Limits"), `docs/quirk-register.md` (Q134's limits);
    - `tests/test_public_api.py`, `tests/test_stacking.py`, `tests/test_pickling.py`.

**Interfaces:**

- Consumes: Task 2's `Limits`, and Task 3's `_core.Limits` and `Threads`.
- Produces:
    - `fnloom.limit(n: int | None = None, *, group: str | None = None) -> Callable[[F], F]`. At least one of the two is
      given, and both may be (O10). It records `Decorations.limit: LimitPolicy(n, group)`, read into `NodeDef.limit`.
    - `Builder.with_limits(groups: Mapping[str, int]) -> Builder[R]` sizes groups for every driver (O26). The checks:
        - a name or size that isn't a `str` mapped to an `int` of at least 1 is a `TypeError` or `ValueError`;
        - a group no node uses is a `ValueError`;
        - an unsized group is refused at build, on every driver (O9): `ValueError: group 'db' of 'load' has no size;
          give it one with Builder.with_limits({'db': n})`.
    - **Every driver honours limits** (O9). On the `Plan::run` path, `task.rs` grants around the call of a node that
      has a limit:
        1. `try_start` while attached;
        2. when refused, wait for the wake inside `detach` until granted;
        3. call the node, with `_threads.HELD` set and reset around the call;
        4. release, which notifies.
    - The plan's `Call` carries a `limited: bool`, so an unlimited node pays one flag test, and `Plan::run` is
      untouched.
    - **The wake.** `_core.Limits.release` bumps the wake's counter and notifies its `Condvar`. Then, attached and after
      the lock, it calls each registered async waker once. `_core.Limits.add_waker(callback)` registers one, and Task 7
      uses it.
    - `Options.in_flight` is `2 * max_workers`. Without `with_threads` it is `2 * (os.process_cpu_count() or 1)`.
    - **Runs inside runs**, by the safety rules' "Ancestry for runs inside runs":
        - A run reads `_threads.HELD` when it starts, and keeps it on the run.
        - A run started inside a task that holds a worker slot uses `Seat::Held` for the tasks its caller runs.
        - When `Scheduler::blocked()`, or a refused sequential grant, names a node whose group's every slot is held by
          this run's ancestors (`Limits::held` against the run's record), the run raises `RuntimeError: 'load' needs
          group 'db', but every slot of it is held by the runs that called this one`.
- [ ] **Step 1: Write the failing tests** in `tests/test_limits.py`. Every counter is updated under a
  `threading.Lock`.
    - `test_limit_caps_a_nodes_concurrent_calls`: 32 branches, `limit(2)` and eight workers. A concurrency counter's
      maximum is 2;
    - `test_a_group_caps_its_members_together`;
    - `test_limits_hold_across_concurrent_runs_of_one_driver`: four threads run at once, and the group cap holds
      across them, on a driver with `with_threads` and on one without;
    - `test_a_sequential_driver_shared_by_threads_honours_limit_one`: four threads `execute` a `limit(1)` node at once,
      and the non-blocking lock is never found held;
    - `test_a_released_slot_wakes_a_waiting_run`: thread A's run holds the slot until an `Event` is set, and thread
      B's run waits for it. Setting the event lets B finish within 1 s;
    - `test_an_unsized_group_is_refused_at_build`, on `Driver`, `with_threads` and `AsyncDriver`;
    - `test_with_limits_survives_pickle_copy_rebuild_and_build_async`;
    - `test_the_in_flight_bound_keeps_a_large_generator_lazy`: a 10,000-item generator under `with_threads(2)`, whose
      branches end in a computed node, with no cache and nothing declining. Items pulled minus branch-exit calls,
      both counted under a lock, never exceed 4;
    - `test_a_node_may_run_its_own_driver_inside_its_slot`: nesting never waits for `max_workers`;
    - `test_an_inner_run_needing_a_group_its_callers_hold_is_refused` (Review Focus 3): at one and two levels of
      nesting, from a caller's thread and from a pool thread, and on a sequential driver;
    - `test_limit_stacks_with_every_decorator`, extending `tests/test_stacking.py`'s pair test;
    - `test_public_names`: `limit` is in `fnloom.__all__`.
- [ ] **Step 2: Run them to see them fail. Step 3: Implement. Step 4: Run the tests to see them pass**, then
  `benches.run`: within noise.
- [ ] **Step 5: Run `scripts/check.sh`, then commit** `limit(), with_limits, the wake, and runs inside runs`.

## PR C4: the async driver on the scheduler, with blocks

### Task 7: `AsyncDriver` on the `Scheduler`, its sync nodes on the pool (design task 22, Q65)

**Files:**

- Create: `crates/fnloom-py/src/stepper.rs`
- Modify: `crates/fnloom-py/src/{plan.rs,pool.rs,lib.rs}`, `python/fnloom/{_async.py,driver.py,_core.pyi}`,
  `docs/running.md`, `docs/quirk-register.md`
- Test: `tests/test_async.py`, `tests/test_hooks_async.py`, `tests/test_parallel.py`, `tests/test_limits.py`

**Interfaces:**

- Consumes: Tasks 1 to 6.
- Produces:
    - `_core.Plan.start(inputs, overrides, *, limits: Limits, threads: Threads | None, in_flight: int) -> Stepper`.
      Every driver has a `Limits` (Task 6), and `in_flight` is Task 6's.
    - `Stepper`'s methods:
        - `next() -> tuple[int, str, tuple[int, ...], str, object] | None` gives `(task, node, branch, work, payload)`,
          with `work` one of `"compute"`, `"iterate"`, `"pull"`, `"finish"` and `"probe"`, or `None` to wait;
        - `waiting_on_limit: bool`;
        - `done: bool`;
        - `complete(task: int, kind: str, value: object, absent: bool)` returns the skipped nodes with their orders,
          and the finished expand, for the hooks;
        - `stop()`;
        - `outputs() -> dict`.
    - Every `Stepper` call follows rule 1.
    - `_async._Run` steps a `Stepper`.
        - An async node is a task on the loop, and takes `Seat::Threadless`.
        - A sync node goes to the loop's default executor, or on a driver with `with_threads` to
          `Threads.submit(fn, context) -> concurrent.futures.Future`, wrapped by `asyncio.wrap_future`.
        - When `next()` waits on a limit, the run registers a waker with `Limits.add_waker`, which calls
          `loop.call_soon_threadsafe` to resolve a future it awaits.
    - `_async.Schedule` and `CachedSchedule`'s dependency counts are removed. `CachedSchedule` keeps its keys and hit
      plans, and its late keys go through the stepper's probes.
    - `RunEnd.skipped` on the async driver is sorted by order (O6).
    - `PreparedPlan._async` is read and written under the plan's new `_async_lock` (`driver.py`, around the `kept`
      update).
    - `with_threads` together with `build_async(sync_nodes="inline")` is a `ValueError: with_threads runs sync nodes on
      the driver's pool, so sync_nodes='inline' can't apply; drop one`.
- [ ] **Step 1: Amend Q65**, and extend `deliberate("Q65")`: under `with_threads(2)`, a sync node runs on a thread
  named `fnloom-worker-`, not in the loop's default executor.
- [ ] **Step 2: Write the failing tests.**
    - Every existing test in `tests/test_async.py` and `tests/test_hooks_async.py` passes unchanged on the stepper:
      they are the async driver's regression suite;
    - `test_async_with_threads_runs_sync_nodes_on_the_pool`;
    - `test_async_nodes_hold_no_worker_slot`: under `with_threads(1)`, two `async def` nodes meet at an
      `asyncio.Barrier(2)` within 5 s;
    - in `tests/test_limits.py`, `test_async_limits_apply_to_async_nodes`: `limit(1)` keeps an async node's calls
      apart, with and without `with_threads`;
    - `test_an_async_run_waiting_on_a_limit_is_woken_by_another_thread`;
    - `test_with_threads_and_inline_are_refused`;
    - `test_async_events_equal_the_sync_drivers_with_one_worker`: for a graph of sync nodes on
      `sync_nodes="inline"`, the event sequence equals `Driver`'s, orders included;
    - `test_async_plans_of_one_prepared_plan_from_many_threads`: 16 threads make async plans of one prepared plan, and
      no error is raised.
- [ ] **Step 3: Run them to see them fail. Step 4: Implement. Step 5: Run the tests to see them pass**, then compare
  `python -m benches.async_` against 0.5.0: every row must be within 10% (the [targets](#benchmark-targets)). A row
  outside it is fixed in this task, or the task stops and reports it.
- [ ] **Step 6: Run `scripts/check.sh`, then commit** `The async driver on the core scheduler (amends Q65)`.

### Task 8: blocks on the async driver, async generators as expand nodes (design task 23, Q132)

**Files:**

- Create: `tests/test_async_blocks.py`
- Modify: `python/fnloom/{_async.py,_blocks.py,_scan.py}`, `docs/running.md`, `docs/writing-nodes.md`,
  `docs/quirk-register.md`
- Test: `tests/test_async_blocks.py`, `tests/test_absence.py`, `tests/differential/test_deliberate_differences.py`

**Interfaces:**

- Consumes: Task 7.
- Produces:
    - `_blocks.refuse(nodes, awaits=True)` no longer refuses expands.
    - `_scan` accepts an `async def` generator whose return annotation is `Parallelizable[...]`, on `build_async()`
      only. `build()` refuses it as it refuses any async node (Q12, Q32). Every other async generator stays refused.
    - Pulls:
        - an async generator's `__anext__` is awaited on the loop;
        - a sync iterator's `__next__` runs where the expand's sync node would, by `sync_nodes` or on the pool;
        - closing uses `aclose()` and `close()`.
    - `Items` reports `pulled` and `finished` from the loop's thread, with its branch (Task 4).
    - A failing branch cancels every running task once, as Q66 does for nodes.
- [ ] **Step 1: Rewrite Q132** as FIX, no longer *Planned*, and amend Q32. `deliberate("Q132")`: Hamilton's async
  builder refuses dynamic execution, and fnloom's async driver runs the block.
- [ ] **Step 2: Write the failing tests** in `tests/test_async_blocks.py`:
    - `test_an_async_generator_expand_fans_out`;
    - `test_async_branches_overlap`: four `async def` branch nodes meet at an `asyncio.Barrier(4)` within 5 s;
    - `test_async_collect_is_in_branch_order`, with branches finishing in reverse;
    - `test_a_failing_async_branch_cancels_the_others_and_closes_the_generator_once`;
    - `test_async_blocks_cache_as_sync_blocks_do`: the same keys as `Driver`;
    - `test_an_async_generator_on_build_is_refused`;
    - in `tests/test_absence.py`, the Plan 10 block-absence tests, parametrized over `driver_kind`, gain `"async"`.
- [ ] **Step 3: Run them to see them fail. Step 4: Implement. Step 5: Run the tests to see them pass.**
- [ ] **Step 6: Run `scripts/check.sh`, then commit** `Blocks on the async driver (Q132; amends Q32)`.

## PR C5: stopping

### Task 9: `timeout=`, `cancelled()`, `close()` on early stop, abandonment, signals (design task 21, Q170)

**Files:**

- Create: `tests/test_stopping.py`
- Modify:
    - `crates/fnloom-py/src/{parallel.rs,pool.rs,task.rs}`;
    - `python/fnloom/{_threads.py,driver.py,_caching.py,__init__.py,_core.pyi}`;
    - `docs/running.md` ("Deadlines and cancellation"), `docs/quirk-register.md`, `tests/test_public_api.py`.
- Test: `tests/test_stopping.py`, `tests/differential/test_deliberate_differences.py`

**Interfaces:**

- Consumes: Tasks 2, 3 and 6.
- Produces:
    - `Driver.execute(final_vars, *, inputs=None, overrides=None, timeout: float | None = None)`, and the same keyword
      on `raw_execute`, `PreparedPlan.run` and `PreparedPlan.run_raw`.
        - Without `with_threads` it is a `TypeError: timeout= needs Builder.with_threads(...)` (O7).
        - `AsyncDriver` takes no `timeout=`: the caller's `asyncio.timeout` already cancels the run (Q66, §6.5).
    - **A deadline run** (O8):
        - The calling thread runs no node, pull or probe task. It waits inside `detach`, in 50 ms slices.
        - The pool grows to `max_workers` threads for it.
        - A caller that holds a worker slot itself, a nested run, lends it: `Scheduler::lend()` lets one of the run's
          tasks take `Seat::Lent`, so a deadline run inside a node never waits for a slot its own caller holds.
    - **When the deadline passes:**
        1. the run stops, and `cancelled()` turns `True`;
        2. the calling thread runs the `Finish` tasks of the iterators with no pull in flight, attached;
        3. it raises `TimeoutError`.
    - An iterator whose pull is in flight at the deadline is finished by the worker whose pull returns.
    - **Abandonment** (O27). Each running task is abandoned. Its slots, worker, node and group, are released at once,
      with a wake, and the pool starts a replacement thread. The abandoned thread's result is dropped when it returns,
      and the thread then exits. While an abandoned call is still running, a limit may be exceeded, and the docs say
      so.
    - **`fnloom.cancelled() -> bool`** reads `_threads.CANCELLED`, a `ContextVar[_core.Stop | None]` set in each task's
      context copy.
        - It is `True` once its run is stopping, by a deadline, by a sibling's failure or by an interrupt.
        - It is `False` outside a run, and on a driver without `with_threads`.
    - **`KeyboardInterrupt`** in the caller stops the run as a failure, then propagates. That covers one raised by
      `check_signals` while the caller waits, and one raised inside a task it runs.
- [ ] **Step 1: Write the register row** Q170, and amend Q66 (deadlines). `deliberate("Q170")`: Hamilton has no
  `timeout`. fnloom's `execute(..., timeout=0.2)` over a node sleeping 5 s raises `TimeoutError` within 1 s.
- [ ] **Step 2: Write the failing tests** in `tests/test_stopping.py`:
    - `test_a_failure_during_a_pull_closes_the_generator_after_it` (Review Focus 1);
    - `test_timeout_raises_at_once_and_abandons_running_tasks`;
    - `test_cancelled_turns_true_on_a_deadline_a_siblings_failure_and_an_interrupt`: a node polling `cancelled()`
      returns early in each case;
    - `test_a_generator_is_closed_on_timeout_by_the_caller_when_no_pull_is_in_flight`;
    - `test_a_nested_run_with_a_deadline_uses_its_callers_lent_slot`: under `with_threads(1)`, a node runs
      `execute(..., timeout=1)` on its own driver, which completes;
    - `test_hung_abandoned_tasks_do_not_starve_later_runs` (Review Focus 4): under `with_threads(2)`, two runs abandon
      nodes blocked on an `Event`, and a third run completes within 2 s;
    - `test_a_deadline_releases_limits_at_abandonment`;
    - `test_timeout_without_with_threads_is_a_type_error`;
    - `test_interrupt_main_stops_a_parallel_run`, with `_thread.interrupt_main()` from a node;
    - `test_the_interpreter_exits_cleanly_with_an_abandoned_task` (Review Focus 4): a subprocess exits with status 0
      within 10 s;
    - `test_a_collected_driver_stops_its_threads`: `_core.live_workers()` returns to its earlier count within 1 s of
      `del driver; gc.collect()`, while a busy thread finishes its call first;
    - `test_public_names`: `cancelled` is in `fnloom.__all__`.
- [ ] **Step 3: Run them to see them fail. Step 4: Implement. Step 5: Run the tests to see them pass.**
- [ ] **Step 6: Run `scripts/check.sh`, then commit** `Stopping parallel runs: deadlines, cancellation, abandonment
  (Q170; amends Q66)`.

## PR C6: `run_many`

### Task 10: `PreparedPlan.run_many` (design task 24, Q171)

**Files:**

- Create: `tests/test_run_many.py`
- Modify: `crates/fnloom-py/src/{parallel.rs,plan.rs}`, `python/fnloom/{driver.py,_caching.py,_core.pyi}`,
  `docs/running.md`, `docs/quirk-register.md`
- Test: `tests/test_run_many.py`, `tests/differential/test_deliberate_differences.py`

**Interfaces:**

- Consumes: Tasks 3 and 9.
- Produces:
    - `PreparedPlan.run_many(inputs_seq: Iterable[Mapping[str, object]], *, overrides: Mapping[str, object] | None =
      None, timeout: float | None = None) -> list[R]`.
        - It returns one result per input set, in order, each built by the result builder.
        - `overrides` is shared by every set (O14).
        - Every set is checked as `run` checks it, all before any runs.
    - **Where the sets run.** On a driver with `with_threads`, each set is its own run on the driver's pool, under its
      limits, with `max_workers` shared. At most `max_workers` sets are open at once: a set is open from its
      `before_run` to its `after_run`. The next set, by index, opens as one closes (O17). Without `with_threads`, the
      sets run one after another on the calling thread (O16).
    - **Hooks.** Each set is its own run, with its own `run_id`, `RunStart` and `RunEnd`, and its own busy flag (O18).
    - **A failure** stops every open set, waits as Task 3 does, and raises the error of the lowest-indexed failing set
      seen, with the note `while running input set 3 of run_many` (O17). A set that never opened fires no `RunStart`
      and no `RunEnd`.
    - **Single-flight** is per set, since it is per run: `docs/running.md` says two sets that need one key both compute
      it (see the non-goals).
    - `_core.Plan.run_many(inputs_seq, overrides, observers_seq, on_value_seq, late_seq, *, threads, limits, timeout)
      -> list[dict]`, so the sets share one wait on the calling thread.
- [ ] **Step 1: Write the register row** Q171 and its `deliberate` test: Hamilton has no batch method, and fnloom's
  `run_many` over 50 input sets equals 50 `run` calls, in order.
- [ ] **Step 2: Write the failing tests.**
    - `test_run_many_returns_one_result_per_set_in_order`, with sets finishing in reverse;
    - `test_run_many_overlaps_sets_on_the_pool`: four sets meet at a `Barrier(4, timeout=5)` under `with_threads(4)`;
    - `test_run_many_opens_at_most_max_workers_sets`: a hook counts open runs under a lock, and the maximum is 2 under
      `with_threads(2)` over 20 sets;
    - `test_run_many_without_threads_runs_sets_in_order`;
    - `test_run_many_checks_every_set_before_running_any`;
    - `test_a_failing_set_raises_with_its_index`, and `test_sets_that_never_opened_fire_no_run_events`;
    - `test_each_set_fires_its_own_run_events`;
    - `test_run_many_through_a_cache_and_a_result_builder`;
    - `test_run_many_with_timeout`.
- [ ] **Step 3: Run them to see them fail. Step 4: Implement. Step 5: Run the tests to see them pass.**
- [ ] **Step 6: Run `scripts/check.sh`, then commit** `PreparedPlan.run_many (Q171)`.

## PR D1: partial results

### Task 11: `absent_on`, `with_absent_on`, error absences (design task 26, Q133)

**Files:**

- Create: `tests/test_absent_on.py`
- Modify:
    - `crates/fnloom-core/src/`:
        - `node.rs`: `NodeSpec.absent_on_error`, `Node.absent_on_error`;
        - `graph.rs`: `may_be_absent`;
        - `run.rs`: `Evaluate::absent_on_error` and `Observer::absent_error`, in both loops;
        - `branch.rs`;
        - `schedule.rs`: `Outcome::ErrorAbsent { value, error }`;
        - `testing.rs`.
    - `crates/fnloom-py/src/`:
        - `graph.rs`: the node tuple;
        - `task.rs`: the error classes per node;
        - `observer.rs`, `shared.rs`, `plan.rs` and `stepper.rs`.
    - `python/fnloom/`:
        - `_absence.py`, `_decorators.py` and `_scan.py`;
        - `driver.py`: `Builder.with_absent_on`;
        - `_hooks.py`: `RunEnd.absent_errors`;
        - `_caching.py`: the poison;
        - `_async.py`, `_core.pyi` and `__init__.py`.
    - Docs:
        - `docs/writing-nodes.md`: "Partial results";
        - `docs/caching.md` and `docs/hooks.md`;
        - `docs/quirk-register.md`: Q133 written; Q157 and Q84 amended.
    - Tests: `tests/test_public_api.py`, `tests/test_pickling.py`, `tests/test_stacking.py`.
- Test: `tests/test_absent_on.py`, the core's test modules, `tests/differential/test_deliberate_differences.py`

**Interfaces:**

- Consumes: Plan 10's absence rules and `RunEnd.skipped`; Tasks 1, 4, 7 and 8.
- Produces in the core:
    - `NodeSpec.absent_on_error: bool`. A node with it may be absent. `is_absent` is still asked only of a node that
      `declines`.
    - `Evaluate::absent_on_error(&mut self, node: &Node<P>, error: &Self::Error) -> Option<Self::Value>`, defaulting
      to `None`. It is asked only of a node with `absent_on_error` whose call failed. `Some(v)` makes its outcome
      absent, with value `v`.
    - `Observer::absent_error(&mut self, node: &str, branch: &[usize], error: &E) -> Result<(), E>`, defaulting to
      `Ok(())`. It fires after `post_node_in` reports the error, so `after_node` gets `ok=False` with it.
    - The `Scheduler` takes `Outcome::ErrorAbsent { value, error }`, and treats it as an absence that single-flight
      never shares.
- Produces in Python:
    - `fnloom.absent_on(*errors: type[BaseException]) -> Callable[[F], F]` (O13). Each class must be a subclass of
      `Exception`. No class, a non-class, or a class that isn't an `Exception` subclass is a `TypeError` when
      decorating, naming it. That covers `BaseException`, `KeyboardInterrupt`, `SystemExit`, `GeneratorExit` and
      `asyncio.CancelledError`, so an error absence never swallows an interrupt, an exit, a generator's close or a
      cancellation.
    - `Builder.with_absent_on(*errors: type[BaseException], tags: Mapping[str, object] | None = None) -> Builder[R]`:
        - it covers every node, or the nodes matching `tags` by `NodeViews.tagged`'s rules (Q141; O12);
        - `errors` are checked as `absent_on`'s are;
        - a covered node may be absent whatever its annotation;
        - a node with both its own `absent_on` and the driver's catches the union.
    - **The build check** (O11). A node decorated `absent_on` whose return annotation doesn't admit `Absent` is a
      `ValueError` naming it, and saying to add `| Absent`. When its return annotation is Q81's `Unresolved`, the
      `ValueError` says the annotation did not resolve, so fnloom can't tell whether it admits `Absent`, and names the
      unresolved text. Nodes covered only by `with_absent_on` need no such annotation.
    - `RunEnd.absent_errors: tuple[tuple[str, tuple[int, ...], BaseException], ...]`, sorted by order on every driver
      (call 8).
    - **Caching.** An error absence is never stored. Its version is a fresh per-run nonce, so every key chained from
      it is uncacheable in that run, including a `Collect` that dropped it (design §4.6).
- [ ] **Step 1: Write the register row** Q133 (design §10's text), rewrite Q157 as FIX pointing at Q133, and amend Q84
  (`RunEnd.absent_errors`). Then write `deliberate("Q133")`:
    - Hamilton's `GracefulErrorAdapter(KeyError)` gives the collect `[1, None, 3]`, indistinguishable from a branch
      returning `None`;
    - fnloom's `absent_on(KeyError)` gives `[1, 3]`, and `Collect[int | Absent]` gives `[1, ABSENT, 3]`.
- [ ] **Step 2: Write the failing Rust tests**: `an_absent_on_error_node_may_be_absent`,
  `an_error_absence_skips_required_consumers`, `an_error_absence_fires_post_node_err_then_absent_error`, and
  `an_error_absence_is_not_shared_by_single_flight`, on `Plan::run` and the `Scheduler` alike.
- [ ] **Step 3: Write the failing Python tests** in `tests/test_absent_on.py`, each over `Driver`, `with_threads(4)`
  and `AsyncDriver`:
    - `test_a_listed_error_becomes_absence_and_its_consumers_follow_the_rules`;
    - `test_a_subclass_is_caught_and_an_unlisted_error_fails_the_run`;
    - `test_collect_over_a_partial_fan_out_gets_the_branches_that_succeeded`;
    - `test_after_node_reports_ok_false_and_run_end_lists_the_error`;
    - `test_log_failures_logs_an_error_absence`;
    - `test_an_error_absence_is_never_stored_and_poisons_its_downstream`: a second run calls the node again, and the
      collect misses;
    - `test_absent_on_needs_an_annotation_admitting_absent`, checked at build;
    - `test_absent_on_with_an_unresolved_return_annotation_is_refused_naming_it`;
    - `test_with_absent_on_covers_tagged_nodes_whatever_their_annotation`;
    - `test_a_covered_node_still_cannot_return_absent_itself`: the return check's `TypeError`;
    - `test_only_exception_subclasses_may_be_listed`: `BaseException`, `KeyboardInterrupt`, `SystemExit`,
      `GeneratorExit` and `asyncio.CancelledError`, for both `absent_on` and `with_absent_on`;
    - `test_with_absent_on_survives_pickle_copy_rebuild_and_build_async`;
    - `test_absent_errors_are_equal_over_worker_counts`;
    - `test_absent_on_stacks_with_every_decorator`, extending `tests/test_stacking.py`.
- [ ] **Step 4: Run them to see them fail. Step 5: Implement. Step 6: Run the tests to see them pass**, plus the
  whole suite.
- [ ] **Step 7: Run `scripts/check.sh`, then commit** `absent_on and with_absent_on: partial results (Q133; resolves
  Q157, amends Q84)`.

## PR C7: free-threading stress and the Threads docs

### Task 12: stress tests and the Threads section (design task 25)

**Files:**

- Modify:
    - `tests/test_threads.py`;
    - `tests/conftest.py`: the `stress` hypothesis profile;
    - `docs/running.md`: "Threads" rewritten;
    - `docs/caching.md` ("Stores") and `docs/hooks.md`.
- Test: `tests/test_threads.py`

**Interfaces:**

- Consumes: everything in C and D.
- Produces: tests in `tests/test_threads.py`, each marked `threads`, that loop `FNLOOM_STRESS_ROUNDS` times (default
  10). Every counter they keep is updated under a `threading.Lock`, which 3.14t needs, since `+=` on an attribute is
  not atomic there.
- [ ] **Step 1: Write the stress tests.**
    - `test_parallel_runs_share_one_driver_pool_and_cache`: 16 caller threads each run a nested, cached fan-out with
      `with_threads(8)`, on one driver and one `MemoryCache`. Every result equals the single-threaded one, and the
      limits' counters return to zero;
    - `test_parallel_runs_share_one_disk_cache`, the same over `DiskCache`;
    - `test_a_shared_hook_sees_each_parallel_run_whole`: per `run_id`, `before_node` precedes `after_node`, and a
      node's `after_node` follows each computed input's;
    - `test_run_many_and_execute_mixed_on_one_driver`;
    - `test_async_and_sync_drivers_of_one_builder_under_load`;
    - `test_limits_never_overshoot_under_contention`, with a group of 3 over 500 branches on eight workers;
    - `test_a_sequential_driver_under_16_threads_honours_its_limits`.
- [ ] **Step 2: Run them on both builds**, with `FNLOOM_STRESS_ROUNDS=200`. Expected: PASS. A failure is a bug in an
  earlier task: fix it there, in its own commit, with a test.
- [ ] **Step 3: Rewrite `docs/running.md`'s Threads section** from the [safety rules](#free-threading-safety-rules).
  It covers:
    - what is shared, what one run serializes, and what your code must make safe;
    - the GIL build's behaviour: nodes that release the GIL overlap, and pure Python nodes take turns;
    - limits on every driver, deadlines, `cancelled()`, runs inside runs, and fork.
- [ ] **Step 4: Run `scripts/check.sh`, then commit** `Free-threading stress tests, and the Threads docs`.

## PR Z: close-out

### Task 13: benchmarks, docs, the example, the roadmap and the design change log (design task 27)

**Files:**

- Create: `benchmarks/test_parallel.py`, `benches/parallel.py`
- Modify:
    - `benches/generate.py`: sleeping and CPU-bound branch bodies;
    - `crates/fnloom-core/benches/baseline.json`: the `schedule` rows;
    - `scripts/bench_gate.py` (`--ratio`), `scripts/test_bench_gate.py`, and `.github/workflows/ci.yml` (the
      `--ratio` arguments);
    - `tests/test_fan_out_example.py`: the parallel and partial cases;
    - `docs/`: `benchmarks.md` ("Parallel fan-out", "The scheduler"), `running.md`, `from-hamilton.md`, `index.md`;
    - `README.md`, `CHANGELOG.md`, the roadmap, and the design's §12.3 and §14.

**Interfaces:**

- Consumes: everything above.
- The example is Plan 10's catalog module, run with `with_threads(8)`:
    - `details` is decorated `@fnloom.absent_on(TimeoutError)` and annotated `-> Detail | Absent`;
    - `summary(details: Collect[Detail])`.
- `scripts/bench_gate.py --ratio NUMERATOR:DENOMINATOR:LIMIT`, repeatable, fails when the numerator id's count exceeds
  `LIMIT` times the denominator id's.
- [ ] **Step 1: Write the failing end-to-end tests** in `tests/test_fan_out_example.py`:
    - `test_the_listing_fans_out_in_parallel`: eight fake endpoint calls meet at a `Barrier(8, timeout=5)`;
    - `test_a_timed_out_item_is_left_out_of_the_summary`;
    - `test_a_partial_summary_is_not_cached_as_a_whole`: the next run calls the failed item's endpoint again.
- [ ] **Step 2: Run them, implement any gap found, and run them to see them pass.** A gap is a bug in an earlier task:
  fix it there, in its own commit, with a test.
- [ ] **Step 3: Add the benchmarks** of [Benchmark targets](#benchmark-targets). `python -m benches.parallel` prints one
  JSON line per (shape, branches, runtime, workers, build, cores). `runtime` is one of `fnloom`, `fnloom-threads`,
  `hamilton-sync` and `hamilton-threads`. Record the results in `docs/benchmarks.md`, for 3.14 and 3.14t on the
  benchmarks machine. A missed target is reported in the table, and is a bug by the benchmarks page's rule.
- [ ] **Step 4: Add `--ratio` to `scripts/bench_gate.py`**, with tests in `scripts/test_bench_gate.py`. Then commit
  the `schedule` rows' baseline with `--write-baseline` in the `instruction-counts` job's image, saying why in the
  commit message, and add the two `--ratio` checks to the job.
- [ ] **Step 5: Write the docs.**
    - `docs/running.md` gets "Running in parallel", "Limits", "Deadlines and cancellation" and "Partial results", with
      the example as their sample.
    - `docs/from-hamilton.md`: rewrite the executor and `GracefulErrorAdapter` rows (Q133, Q134, Q167 to Q171), and
      empty "Not there yet" of both.
- [ ] **Step 6: Update the roadmap and the design.**
    - Roadmap item 10 and the status line say Plan 11 is built and ships in 0.6.0. The ledger rows for dynamic
      execution and `GracefulErrorAdapter` become **fixed (0.6.0)**.
    - The design's §12.3 links this plan.
    - §14 gets a change-log line saying what was built, and every departure in [Calls this plan
      makes](#calls-this-plan-makes) that the maintainer approved.
    - The 2026-11-15 entry is corrected: Plan 10 built `Plan::block_of(expand)` beside the steps, not `Step.block`.
- [ ] **Step 7: Run `scripts/check.sh`, then commit** `Plan 11 close-out: benchmarks, docs, roadmap`.

The 0.6.0 release PR follows this plan's last merge, as 0.5.0's did (#98). It is not part of this plan.

## Open questions

Each question has the plan's answer marked **Recommended**, and the tasks above are written to it. Approving this
plan approves every recommended answer, unless the maintainer says otherwise. O9, O4, O5, O6, O8, O11, O13, O17,
O20, O21 and O23 were revised after the review of #99, and O26 to O28 are new.

### Release and scope

- **O1. Which release?**
    - (a) Plan 11 ships whole as **0.6.0**, after 0.5.0. **Recommended**: one release, as Plan 10 shipped.
    - (b) C as 0.6.0, and D as 0.7.0.

### Which executor runs what

- **O2. A driver without `with_threads`.**
    - (a) It keeps `Plan::run`, unchanged. **Recommended**: design §2's anti-goal ("the default changes nothing") and
      the instruction-count gate hold by construction, and `Plan::run` stays the reference the scheduler is tested
      against.
    - (b) It runs the scheduler with one worker, as design task 17's wording ("the sync driver runs on the
      `Scheduler`") reads.
- **O3. `with_threads(max_workers=1)`.**
    - (a) It runs the scheduler on the calling thread, with `with_threads`' rules: per-call contexts and the busy flag.
      **Recommended**: §6.2 applies the context rule "whatever `max_workers` is", so results can't depend on the worker
      count.
    - (b) The same as no `with_threads`.
- **O4. The async driver.**
    - (a) It always steps the core scheduler, with or without `with_threads`, gated by the async benchmarks staying
      within 10%. **Recommended**: §6.1's one set of rules, and blocks on the async driver need it.
        - Every async driver has a `Limits` from build: no worker cap without `with_threads`, node and group caps from
          `limit` and `with_limits`.
        - Its `in_flight` is `2 * max_workers`, or `2 * (os.process_cpu_count() or 1)` without `with_threads`.
    - (b) Only with `with_threads`, keeping `_async.Schedule` otherwise, and with blocks only then.

### Determinism and events

- **O5. How events say where they belong.**
    - (a) `NodeStart.order`, `NodeEnd.order` and `ExpandEnd.order` are each a `tuple[int, ...]`, the core order key
      from `Plan::order_key`, on every driver. **Recommended**: sorting by it gives reference order across blocks (a0,
      b0, a1, b1 when that is the plan's order), where sorting by a position and then a branch would not.
    - (b) A position in `RunStart.nodes`, and the branch.
- **O6. `RunEnd.skipped` and `RunEnd.absent_errors` with several workers.**
    - (a) Both are sorted by order key, on both drivers. **Recommended**: equal over worker counts, as design goal 3
      asks of results.
    - (b) Completion order.

### Deadlines and stopping

- **O7. `timeout=` without `with_threads`.**
    - (a) A `TypeError` naming `with_threads`. **Recommended**: the calling thread can't abandon a node it is running
      itself, and a deadline is for the hung node (§6.5).
    - (b) Checked between nodes only.
- **O8. Who runs tasks during a deadline.**
    - (a) Only pool threads run node, pull and probe tasks; the pool grows to `max_workers` threads, and the caller
      waits. **Recommended**: the only way the caller can raise at once. Two rules go with it:
        - A nested deadline run lends its caller's worker slot to the run (`Seat::Lent`), so it never waits for a slot
          its own caller holds.
        - At the deadline, the caller runs the `Finish` tasks of iterators with no pull in flight, before raising. An
          iterator whose pull is in flight is finished by the worker whose pull returns.
    - (b) The caller works too, and raises when its current task returns.
- **O27. Abandoned tasks.**
    - (a) An abandoned task releases its worker, node and group slots at once, the pool starts a replacement thread,
      and the abandoned thread exits when its call returns. **Recommended**: hung nodes never starve later runs. The
      cost is that a limit may be exceeded while an abandoned call still runs, which the docs say.
    - (b) Keep node and group slots until the call returns, so limits hold strictly, but a hung limited node then
      blocks its group for good.

### Limits

- **O9. Where limits apply.**
    - (a) On every driver. **Recommended**: a sequential `Driver` shared by threads (`docs/running.md`, Threads) would
      otherwise run a `@limit(1)` node four at once.
        - The `Plan::run` path takes the grant around a limited node's call, waiting inside `detach`. That costs an
          unlimited node one flag test, and leaves `Plan::run` untouched.
        - An unsized group is refused at build on every driver.
    - (b) Only where one run's nodes overlap, so `AsyncDriver` and `with_threads`, while a sequential driver ignores
      limits.
- **O10. `limit(n, group=...)` together.**
    - (a) Either or both, with a node in at most one group. **Recommended**: a cap per node and a shared cap are
      independent needs.
    - (b) Exactly one.
- **O26. Where group sizes are set.**
    - (a) `Builder.with_limits(groups)`, for every driver, while `with_threads(max_workers=None)` sizes only the pool.
      **Recommended**: limits apply without threads (O9), so sizing them can't need `with_threads`. This departs from
      §6.2's `with_threads(max_workers, limits)`.
    - (b) §6.2's `with_threads(limits=)`, with a sequential or async driver needing `with_threads` to size a group.

### Partial results

- **O11. When `absent_on`'s annotation is checked.**
    - (a) At build, naming the node. **Recommended**: on 3.14, annotations are evaluated lazily, and checking when
      decorating would evaluate them early, failing a forward reference that build resolves (Q81). A return
      annotation that stays unresolved at build (Q81's `Unresolved`) is refused too, naming the node and the
      unresolved text, since fnloom can't tell whether it admits `Absent`.
    - (b) When decorating, as §7.5 says.
- **O12. `with_absent_on(tags=)`.**
    - (a) A query in `NodeViews.tagged`'s form (Q141). **Recommended**: one tag syntax in fnloom.
    - (b) A list of tag names.
- **O13. Which errors may be listed**, in `absent_on` and `with_absent_on`.
    - (a) Only subclasses of `Exception`. `BaseException` itself, `KeyboardInterrupt`, `SystemExit`, `GeneratorExit`
      and `asyncio.CancelledError` are a `TypeError` when decorating or at the builder call. **Recommended**: an
      interrupt, an exit, a generator's close or a cancellation that became absence would make a run unstoppable, or
      break a generator.
    - (b) Any class, with those five never caught.

### `run_many`

- **O14. Its overrides.**
    - (a) One mapping shared by every input set. **Recommended**: design §8's signature, `run_many(inputs_seq,
      overrides=None)`.
    - (b) A sequence aligned with the input sets.
- **O15. On the async driver.**
    - (a) Not in Plan 11, since `asyncio.gather` over `AsyncPreparedPlan.run` does it. **Recommended**.
    - (b) `AsyncPreparedPlan.run_many` too.
- **O16. Without `with_threads`.**
    - (a) The sets run one after another on the calling thread. **Recommended**: the same code works on either driver.
    - (b) A `TypeError`.
- **O17. How many sets are open, and its failure.**
    - (a) At most `max_workers` sets are open at once, opening in index order. A failure stops every open set, and
      the lowest-indexed failing set seen is raised, noted with its index. A set that never opened fires no `RunStart`
      or `RunEnd`. **Recommended**: §4.5's rule, one level up, with memory bounded by the pool.
    - (b) Open every set at once, run every set, and raise an `ExceptionGroup`.
- **O18. Its hooks.**
    - (a) Each set is its own run, with its own busy flag, so two sets' hook calls may overlap, as two threads' runs do
      today. **Recommended**.
    - (b) One busy flag for the whole call.

### Benchmarks and CI

- **O19. The parallel targets.**
    - (a) The thresholds of [Benchmark targets](#benchmark-targets). **Recommended**:
        - under 10 µs per trivial branch with threads;
        - at least 6× on sleeping branches;
        - at least 5× on CPU-bound ones on 3.14t, with 8 or more cores;
        - at most 2× `Plan::run`'s core instructions with one worker;
        - async within 10%;
        - faster than Hamilton's `MultiThreadingExecutor(8)` on every row.
    - (b) Report only, with no thresholds.
- **O20. CI for threads.**
    - (a) The `stress` job on Ubuntu for both builds, with 200 rounds and a hypothesis profile of 500 examples;
      `benchmarks-smoke` on 3.14t too; and no ThreadSanitizer job. **Recommended**: TSan needs a TSan build of
      CPython, which neither `uv` nor the runners provide. The core forbids `unsafe`, and the binding's one `unsafe`
      block, the vectorcall in `call.rs`, is called attached on values its thread owns.
    - (b) The same, plus a TSan job on a self-built interpreter.
- **O21. Watchdogs.**
    - (a) Bounded waits in every thread test, so a deadlock usually fails one test. **Recommended**: no new dev
      dependency, and `pytest-timeout`'s thread method ends the session too.
        - `faulthandler.dump_traceback_later(300, exit=True)` from `tests/conftest.py` is the last resort. It ends the
          whole session and prints Python stacks only, no Rust frames.
        - `timeout-minutes: 60` on the `check` job.
    - (b) Add `pytest-timeout`.

### Threads and pools

- **O22. Signal checks.**
    - (a) Every 50 ms while the caller waits. **Recommended**: `Ctrl-C` feels immediate, at a cost of 20 wake-ups a
      second.
    - (b) 10 ms.
    - (c) 200 ms.
- **O23. A pool's lifetime.**
    - (a) Its threads live until the driver's `Threads` is collected, or the interpreter exits, with no public
      `close()`. **Recommended**: no lifecycle for users to get wrong. The rules that go with it:
        - a busy thread is never joined, at exit or by the collector;
        - idle threads are joined with a bounded wait of at most 1 s in all;
        - a `finalizing` flag stops busy threads attaching once exit has begun;
        - `Threads` is `#[pyclass(weakref)]`, for the `atexit` `WeakSet`;
        - `test_a_collected_driver_stops_its_threads` pins it.
    - (b) An idle timeout.
    - (c) A public `Driver.close()`.
- **O24. A warning on the GIL build.**
    - (a) None: `with_threads` is correct on 3.14, and the docs say pure Python nodes take turns there.
      **Recommended**.
    - (b) A `RuntimeWarning` when `with_threads` is used with the GIL enabled.
- **O25. Copies, rebuilds and unpickled drivers.**
    - (a) Each driver object gets its own pool and limit counters. **Recommended**: limits are a driver's (§6.6), and
      sharing them across copies would make one copy's runs throttle another's silently.
    - (b) A copy shares its original's.
- **O28. How a pool thread shows up in Python.**
    - (a) It renames its `_DummyThread` to `fnloom-worker-<n>` on its first attach, and `_core.live_workers()` (under
      `test-hooks`) counts pool threads for tests. **Recommended**: tracebacks, logs and `current_thread()` name the
      worker. Rust threads never appear in `threading.enumerate()` as threads of their own, so counting needs the
      hook.
    - (b) Expose native thread ids on `NodeStart`.
