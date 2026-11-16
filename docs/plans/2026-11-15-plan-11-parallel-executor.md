# fnloom Plan 11: the parallel executor and partial results

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan PR by PR, task by task, with superpowers:test-driven-development
> inside each task and an independent reviewer subagent per PR. Steps use checkbox (`- [ ]`) syntax for tracking.

**Status:** implementation plan, drafted 2026-11-15, awaiting the maintainer's approval. Nothing is built. Every
decision the design leaves open is answered here, and gathered in [Open questions](#open-questions) for approval in
one pass.

**Goal:** milestones C to E of the [dynamic execution design](../design/2026-10-28-dynamic-execution-design.md).

- **C, the scheduler and the parallel executor:** a `Scheduler` in the Rust core that hands out ready tasks in a fixed
  order, `Builder.with_threads(max_workers=None, limits=None)` running one run's tasks on a per-driver pool, parallel
  on 3.14t and correct on 3.14, per-node and per-group limits, single-flight per key, deadlines, cooperative
  cancellation, `PreparedPlan.run_many`, and the async driver on the same scheduler, with blocks (§5, §6).
- **D, partial results:** `@fnloom.absent_on(*errors)` and `Builder.with_absent_on(*errors, tags=None)` turn chosen
  errors into absence, so a `Collect` over a fan-out gets the branches that succeeded (§7.5).
- **E, close-out:** benchmarks against Hamilton's thread executor, the docs, the roadmap and the design's change log.

**Motivating use, kept generic:** a catalog graph fans out one endpoint call per listed item. With `with_threads(8)`
the calls overlap, on either build; an endpoint that times out for one item makes that branch absent, and the summary
is built from the rest. Task 13 writes this as the end-to-end test and the docs example.

**Architecture:**

- **The core decides what runs next.** `fnloom-core` gains `Scheduler`, a state machine over one run of one plan,
  with no threads, no clock and no locks. `next()` hands out the ready task with the lowest order key that the
  driver's `Limits` grants; `complete()` takes back its outcome. With one worker it reproduces `Plan::run` task for
  task. `Plan::run` stays as it is: the reference semantics, and the default path.
- **The binding owns threads and the GIL boundary.** `fnloom-py` gains a per-driver pool of Rust threads, a parallel
  run loop that attaches only to call Python and detaches before waiting, one hook lock per run, and a `Stepper`
  that lets the async driver step the same scheduler from its event loop.
- **The front end is Python**, as before: `with_threads`, `limit`, `absent_on`, `with_absent_on`, `cancelled()`,
  `timeout=`, `run_many`, and the async driver's loop.
- **Default path untouched.** A driver built without `with_threads` runs `Plan::run` exactly as 0.5.0 does: same
  thread, same context, same event order, same instruction count.

**Tech Stack:** as Plan 10. Python 3.14 and 3.14t, the Rust core through maturin and PyO3 0.30, ty strict, ruff ALL,
Clippy pedantic, rumdl, pytest with hypothesis, and `apache-hamilton==1.90.0` as the oracle. Build with `maturin
develop --features test-hooks`, test with `UV_NO_SYNC=1`, and gate with `scripts/check.sh`.

**Spec:** `docs/design/2026-10-28-dynamic-execution-design.md`. Its sections:

- §6 (the executor), §7.5 (absent on error), §4.5 and §4.6 where they mention workers, single-flight or error
  absences, §5's last bullet;
- §10, rows Q133 and Q134 and the amended rows Q65, Q66, Q85 and Q86;
- §12.1 and §12.2, the tests and benchmarks that need threads;
- §12.3, tasks 16 to 27;
- §13, the calls, and §14's 2026-11-15 entry, which records how Plan 10 was actually built.

Also read [Plan 10](2026-11-05-plan-10-absence-and-fan-out.md), whose seams this plan builds on, and
`docs/plans/2026-10-28-roadmap-ahead-of-hamilton.md`.

**Order and release:**

- 0.5.0 is not tagged yet: on `main` the version is 0.4.0 and Plan 10 sits under `[Unreleased]`. Plan 11's first PR
  starts after the 0.5.0 release PR merges.
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
      `#![deny(unsafe_code)]`;
    - no recursion over graph depth or nesting depth;
    - register rows before their tests; neutral example domains; a CHANGELOG `[Unreleased]` entry per PR, and the
      `from-hamilton.md` rows it changes.
- **The default path is untouched.** A driver without `with_threads` runs `Plan::run`, on the caller's thread, in the
  caller's context, with today's event order (O2). `benches.run` stays within noise, the instruction-count gate stays
  green, and no existing test changes, except row texts this plan amends.
- **Results never depend on workers or timing** (design goal 3). For `max_workers` in {1, 2, 8}, the same inputs give
  the same values, `Collect` orders, raised exception class (with one failure), cache keys, `RunEnd.skipped` and
  `RunEnd.absent_errors` (O6).
- **The free-threading rules** below hold in every PR. Each rule is pinned by a test in the task that first needs it.
- **No new dependency**, runtime or dev. Threads are `std::thread`; locks are `std::sync`; test watchdogs use
  `faulthandler` (O21).
- Every new setting (`with_threads`, `with_absent_on`) survives pickling, `copy`, `rebuild()` and `build_async`, as
  Plans 5 to 10 settled. The pool and the limits' counters are never pickled or shared between driver objects (O25).
- Every test that starts threads is marked `threads` and runs under the watchdog, so a deadlock fails one test with
  every thread's stack, never hangs CI.

## Free-threading safety rules

These are the rules every task follows. "Attached" means the thread is attached to the interpreter (PyO3
`Python::attach`), which on the GIL build means it holds the GIL.

**Shared and immutable**, read by any thread with no lock:

- the core `Graph` and `Plan`, and the binding's `Plan` (its `calls` and keywords), reached through frozen pyclasses
  (`Py::get` needs no attachment);
- `NodeDef`s, `NodeView`s and the driver's settings;
- `Limits`' capacities (its counters are atomics, below).

**Shared and mutable**, with the one lock each may take:

| State | Owner | Guard | Held across Python? |
|---|---|---|---|
| `Limits` counters: workers, per node, per group | one per driver | atomics only, no lock | never |
| the pool: idle threads, the queue of runs wanting work | one per driver | `Mutex<PoolState>` and a `Condvar` | never |
| one run's `Scheduler` | one per run | `Mutex<Scheduler>` and a `Condvar` | never |
| one run's hooks: `Dispatch`, the observers, `on_value` | one per run | the run's **hook lock**, `Mutex<()>` | yes, the only one |
| one run's `LateKeys` memo tables | one per run | a `threading.Lock` around the tables only | no: store calls run outside it |
| `MemoryCache`'s LRU | one per store | its `Mutex<Lru>`, as today | never; evicted values drop after it |
| the hit-plan cache of `CacheRun` | one per prepared plan | `_plans_lock`, as today | no |
| `PrintNodes` timings | one per hook | its own `threading.Lock`, as today | no |

**How a thread takes a lock:**

1. A Rust lock is taken attached only by `try_lock`. A thread that would wait detaches first (`py.detach(||
   mutex.lock())`), then re-attaches. So no thread ever waits for a lock while attached (design §2 anti-goal), and the
   uncontended case costs no detach.
2. No Rust lock except the hook lock is held while calling into Python, and no Python object is dropped while one is
   held: values the scheduler releases are handed back to the caller of `complete` and dropped after the lock.
3. The hook lock is taken detached, then the thread attaches and calls the hook. On the GIL build its holder may wait
   for the GIL, but whoever holds the GIL is never waiting for the hook lock, so the two can't deadlock.
4. Waiting for work, for a slot, or for the run to end is a detached `Condvar` wait. The calling thread waits in
   slices of 50 ms (O22) and calls `check_signals` between them, so `Ctrl-C` reaches it.

**Values across threads.** A run's values are `Arc<Py<PyAny>>` inside the scheduler, so cloning one for a task's
arguments is pure Rust. A `Py` is only ever dropped attached (rule 2), never through PyO3's deferred pool.

**Contexts.** On a driver with `with_threads`, the run takes `contextvars.copy_context()` once when it starts, and each
task call runs in its own `copy()` of that snapshot, whatever `max_workers` is (§6.2). `current_branch()` and
`cancelled()` are set in that copy. A `ContextVar.set` in one node is never seen by another, and no `Context` is entered
by two threads.

**Hooks.** A run's hook calls never overlap: the hook lock serializes them. A hook runs on the thread that ran the
node. With one worker, events come in exactly `Plan::run`'s order; with several, in the order nodes start and finish,
each carrying `branch` and `position` so a hook can sort (§6.4). A hook shared by several runs (several threads, or
`run_many`) sees their events interleaved, as today, so a hook that keeps state across runs must be thread-safe.

**Caches and stores.** Stores are called from workers, attached, with no fnloom lock held. `MemoryCache` and
`DiskCache` are already safe from several threads (caching design §7); a custom `CacheStore` must be, as today. The
single-flight table is per run, inside the scheduler. `LateKeys` is per run and locked as above. Keys never contain
timing or worker identity.

**The pool's lifetime.** Pool threads are named `fnloom-worker-<n>`, started lazily at the first parallel run, and
shared by the driver's runs. They exit when the driver's `Threads` object is collected, or at interpreter exit through
an `atexit` hook that closes every live pool, so no worker attaches during finalization. After `os.fork()` the child's
pools are reset (`os.register_at_fork(after_in_child=...)`), and its first run starts new threads.

**Your code.** A node that mutates a shared value races with its siblings now, as it raced with other threads' runs
before. Within one run, hooks are serialized and nothing else is (§6.7).

## Calls this plan makes

These are the places where the design leaves the shape to the builder, or where this plan departs from its wording.
The ones the maintainer should rule on are also in [Open questions](#open-questions), by their O-number.

1. **`Scheduler` borrows nothing.** Every method takes `plan: &Plan` and `graph: &Graph<P>`, the ones it was made
   with, so a scheduler can live in an `Arc<Mutex<_>>` that persistent pool threads share. A debug assertion checks
   the step count matches.
2. **One task call path in the binding.** `crates/fnloom-py/src/task.rs` calls a payload, iterates, pulls, finishes,
   gathers and looks up. `Caller` (the `Plan::run` path) and the parallel workers both use it, so the return check,
   `current_branch()`, wrappers and absence behave the same on both.
3. **One entry point.** `_core.Plan.run(..., threads=None, timeout=None)`. With `threads`, it runs on the scheduler;
   without, on `Plan::run`. `CacheRun`, the hooked plans and `run_many` pass the driver's `Threads` through, so no
   Python call site forks.
4. **A branch's cache hits come from a `Probe` task.** When a branch's item arrives, the scheduler hands out one
   `Probe`, which asks `lookup` for the branch's steps in reverse need order, the walk `Plan::run` does. That walk
   moves out of `branch.rs` into `pub(crate) fn needs`, used by both. A probe's outcome also carries each needed
   step's key, for single-flight.
5. **A computed expand is iterated on the worker that computed it**, which completes with the iterator. An expand
   override or hit becomes an `Iterate` task.
6. **A collect step's arguments arrive gathered.** Its task carries `ArgValue::Gather(Vec<V>)` per collect edge, and
   the binding builds the `list`s attached.
7. **Stop is scheduler state.** After a failure, a deadline or an interrupt, `next()` hands out no new compute, then
   one `Finish { exhausted: false }` per open iterator with no pull in flight, innermost first. An iterator whose pull
   is in flight is finished by the worker whose pull returns, so a generator is never closed while it runs.
8. **`RunEnd.skipped` and `RunEnd.absent_errors` are sorted in reference order** at the end of a run with
   `with_threads`, so they equal the one-worker run (O6).
9. **`NodeStart.position` and `NodeEnd.position: int`**, the node's index in `RunStart.nodes`, which design §6.4 calls
   "the node's plan position" (amends Q84; O5).
10. **Error absence through the core**: `Evaluate::absent_on_error(&mut self, node, error) -> Option<V>` (default
    `None`) and `Observer::absent_error(&mut self, node, branch, error)` (default no-op), in `Plan::run` and the
    `Scheduler` alike. `NodeSpec.absent_on_error: bool` makes a node may-be-absent without letting it return `ABSENT`
    itself: the return check still reads the annotation.
11. **`absent_on`'s annotation check runs at build**, not when decorating (O11): on 3.14, annotations are evaluated
    lazily, and fnloom resolves them at build (Q81).
12. **`fnloom.limit` and `fnloom.cancelled` live in a new `_threads.py`**, with `with_threads`'s argument checks, so
    `_decorators.py` gains only the `Decorations.limit` field.
13. **The async driver steps the core scheduler through `_core.Stepper`** (O4). `_async.Schedule` and
    `CachedSchedule`'s own dependency counts go.
14. **`timeout=` needs `with_threads`**, and a run with a deadline does not run tasks on the calling thread (O7, O8).

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

**Evidence for the new rows.** Q167 and Q168 were probed on 2026-11-15 with Python 3.14 and Hamilton 1.90.0's
default builder, whose remote executor is `MultiThreadingExecutor(max_tasks=10)` (`driver.py`, read): a
`ContextVar` set by the caller read `"unset"` in all four branch nodes and `"caller"` in a node outside the block,
since `PoolExecutor.submit_task` calls `ThreadPoolExecutor.submit` without copying the context; a hook's
`run_before_node_execution` was entered by four threads at once. Q169 is design §11 ("Only block tasks go to the
'remote' executor"). Q170 and Q171 are read from `Driver.execute`'s signature and the driver's public members. The
same probe found that a failing branch's `execute` raises only after the running branches finish (0.39 s against a
0.3 s sibling), which Q128's KEEP relies on.

**Amended rows:**

| Row | Task | What changes |
|---|---|---|
| Q32 | 8 | An async generator annotated `-> Parallelizable[T]` is a node on `build_async()` |
| Q65 | 7 | With `with_threads`, a sync node on the async driver runs on the driver's pool; `sync_nodes="inline"` with it is refused |
| Q66 | 9 | The sync driver gains the stop rule: it waits for running steps on failure, and a deadline abandons them |
| Q84 | 4, 11 | `NodeStart.position`, `NodeEnd.position` (Task 4); `RunEnd.absent_errors` (Task 11) |
| Q85 | 5 | A single-flight duplicate reports a hit |
| Q86 | 4 | On a driver with `with_threads`, hooks run on the node's thread, serialized per run |
| Q119, Q122, Q123, Q138 | 3 | "Plan 11 reruns it" becomes the parallel rerun's result |
| Q128 | 9 | The parallel stop: running branches finish, the lowest order key's failure is raised |
| Q132 | 8 | FIX *Planned* becomes FIX: blocks run on `AsyncDriver` |
| Q137 | 3 | `with_threads(...)` is no longer *Planned* |
| Q157 | 11 | DEFER becomes FIX, pointing at Q133 |

## Differential tests against Hamilton

The oracle is `apache-hamilton==1.90.0`, already a dev dependency, on both builds in CI.

- **Executors.** `tests/differential/harness.py`'s `run_hamilton_blocks(..., executor=)` takes `"synchronous"` (as
  today), `"default"` (nothing set: the builder's `MultiThreadingExecutor(10)`), or `("threads", n)`
  (`MultiThreadingExecutor(n)`). A new `run_fnloom_blocks(..., threads: int | None = None)` builds with
  `with_threads(threads)` when given. Each KEEP row with blocks runs fnloom with `threads` in {None, 1, 2, 8} against
  Hamilton's `"synchronous"`, `"default"` and `("threads", 8)`.
- **KEEP rows rerun in parallel:**
    - Q119, collect order, with item *i* sleeping `(n - i) × 10 ms` so branches finish in reverse;
    - Q120, an empty expand;
    - Q121, a list returned, and the expand as an output;
    - Q127, edges;
    - Q128, the exception class and finished branches dropped, plus the probed wait: both runtimes raise only after a
      0.3 s sibling branch finishes;
    - Q138, two blocks with shared inputs.
- **FIX rows rerun:** Q122 (nesting) and Q123 (two collects) under `threads=8`, with Hamilton's `"default"` result.
- **New `deliberate` rows:** Q133, Q134, Q167, Q168, Q169, Q170 and Q171, each a test function in
  `tests/differential/test_deliberate_differences.py` asserting Hamilton's behaviour and fnloom's side by side.
- **What is compared:** values and exception class names only. Never timing, never event order: Hamilton's thread
  order is nondeterministic, and fnloom's is checked against itself (below).
- **Concurrency without timing.** Where a row needs "these ran at once" (Q168, Q169), the nodes meet at a
  `threading.Barrier(n, timeout=5)`: it passes only when *n* run concurrently, on either build, since waiting on a
  barrier releases the GIL.
- **fnloom against itself.** `tests/test_threads_equivalence.py` is the design's executor-equivalence property:
  hypothesis graphs with blocks, nested blocks, declines and one failing node, run with no `with_threads` and with `max_workers`
  in {1, 2, 8}. Values, `Collect` orders, exception classes, `RunEnd.skipped`, `RunEnd.absent_errors` and cache keys
  are equal; and with one worker, the full hook event sequence equals the run without `with_threads`.

## Benchmark targets

From design §12.2. The design names the rows and one number (under 2 µs per branch node, sequential, met by Plan 10
at 0.5 µs). The thresholds for the parallel rows are this plan's (O19).

| Row (§12.2) | Compared with | Target |
|---|---|---|
| fan-out of 1, 100 and 10,000 trivial branches, sequential | Hamilton synchronous | no regression from 0.5.0's 0.5 µs a branch (within 10%) |
| the same with `with_threads(8)`, trivial branches | Hamilton `MultiThreadingExecutor(8)` | faster on every count; under 10 µs a branch |
| 100 sleeping branches (10 ms each), `with_threads(8)` | one worker, and Hamilton `MultiThreadingExecutor(8)` | at least 6× over one worker on 3.14 and 3.14t; no slower than Hamilton |
| 100 CPU-bound pure Python branches (about 1 ms each), `with_threads(8)`, 3.14t | one worker, and Hamilton `MultiThreadingExecutor(8)` | at least 5× over one worker on a machine with 8 or more cores; faster than Hamilton |
| a layered graph without blocks, `with_threads(1)` | `Plan::run` | in the core, at most 2× `Plan::run`'s instructions per node, gated like `run`; from Python, at most 1.5 µs more per node |
| a cached fan-out of 1,000 branches, warm and with one appended item, `with_threads(8)` | the same with one worker | no slower than one worker by more than 20% |
| `ABSENT` through a 200-node chain | 0.5.0 | unchanged: the plain loop is untouched |
| the async benchmarks (`benches.async_`), every shape, on the scheduler | 0.5.0 | within 10% in both `sync_nodes` modes |
| the default path (`benches.run`, every shape) | 0.5.0 | within noise; the instruction-count gate green |

On 3.14 with the GIL, only the sleeping rows are reported for parallel speedup (§12.2). CodSpeed counts simulated
instructions on one core, so it gets the `with_threads(1)` rows only; the parallel rows are wall clock, in
`docs/benchmarks.md`, measured on both builds.

## CI and 3.14t

- **Already in place:** the `check` job runs `scripts/check.sh` on Ubuntu, macOS and Windows, for 3.14 and 3.14t, and
  the release builds one wheel per interpreter. Plan 11's thread tests run in it: on 3.14 they prove correctness, on
  3.14t parallelism.
- **The GIL stays off.** `test_a_parallel_run_keeps_the_gil_off` runs in a fresh interpreter (a subprocess, so no
  import by another test can re-enable the GIL first): it imports `fnloom`, runs a `with_threads(4)` fan-out, and
  asserts `not sys._is_gil_enabled()`. It runs on 3.14t only.
- **No hung jobs.** A `threads` marker is registered in `pyproject.toml` (needed under `--strict-markers`), and an
  autouse fixture in `tests/conftest.py` arms `faulthandler.dump_traceback_later(120, exit=True)` for marked tests and
  cancels it after. The `check` job gets `timeout-minutes: 60`.
- **A stress job** (O20), `stress` in `.github/workflows/ci.yml`, on `ubuntu-latest` for 3.14 and 3.14t:
  `FNLOOM_STRESS_ROUNDS=200 HYPOTHESIS_PROFILE=stress uv run --no-sync pytest -m threads`. The stress tests read
  `FNLOOM_STRESS_ROUNDS` (default 10 in `check.sh`), and `tests/conftest.py` registers a hypothesis profile `stress`
  with `max_examples=500`. The job is not in `check.sh`, which stays the local gate.
- **Benchmarks.** `benchmarks-smoke` gains 3.14t in a matrix, so the parallel benchmarks run once on the build they
  are for. The CodSpeed job stays on 3.14 and skips the parallel rows.
- **The instruction-count gate.** New `schedule` rows in `crates/fnloom-core/benches/core.rs` are reported, not
  gated, until Task 13 commits their baseline with `--write-baseline` in the job's own image.
- **Windows.** Interrupt tests use `_thread.interrupt_main()`, not `os.kill`, so they run on all three systems.
- **Fork.** The fork test is skipped on Windows, which has no `os.fork`.
- **No ThreadSanitizer job** (O20): it needs a TSan build of CPython. The core has no `unsafe`, and the binding none
  either.

## File Structure

```text
crates/fnloom-core/src/
  schedule.rs  NEW  Scheduler, Task, Work, ArgValue, Outcome, Next, Report, OrderKey, Seat
  limits.rs    NEW  Limits: atomic counters for workers, nodes and groups; the Grant trait
  branch.rs    the reverse-need walk moved out as `needs`, shared with schedule.rs
  run.rs       Evaluate::absent_on_error, Observer::absent_error (Task 11)
  node.rs      NodeSpec.absent_on_error, Node.absent_on_error (Task 11)
  graph.rs     may_be_absent from absent_on_error (Task 11)
  lib.rs       pub mod schedule, limits
crates/fnloom-core/benches/core.rs   schedule rows
crates/fnloom-core/tests/schedule_threads.rs   NEW  the scheduler driven from 8 std threads
crates/fnloom-py/src/
  task.rs      NEW  one task's call: payload, return check, branch, block ops, lookup, absent_on
  pool.rs      NEW  Threads (pyclass): Limits, the pool, submit for the async driver, close, fork reset
  parallel.rs  NEW  one parallel run: worker loop, hook lock, contexts, deadline, signals, run_many
  stepper.rs   NEW  Stepper (pyclass): the scheduler stepped from the event loop
  plan.rs      Plan.run(threads=, timeout=), Plan.run_many, Plan.start; Caller over task.rs
  observer.rs  position on node events; absent_error
  graph.rs     node tuple gains absent_on_error (Task 11)
  lib.rs       registers Threads, Stepper, Stop
python/fnloom/
  _threads.py  NEW  limit, cancelled, CANCELLED, checked_threads, make_threads, close_all (atexit, fork)
  driver.py    Builder.with_threads, with_absent_on; Driver/PreparedPlan timeout=, run_many
  _async.py    _Run over Stepper; sync nodes on Threads.submit; blocks; async generator pulls
  _blocks.py   the async refusal goes; async-generator items
  _scan.py     async generator expands; NodeDef.limit, NodeDef.absent_on
  _decorators.py  Decorations.limit, Decorations.absent_on
  _absence.py  absent_on, the build check, with_absent_on coverage
  _caching.py  LateKeys lock; probe keys; error-absence poison
  _hooks.py    NodeStart/NodeEnd.position; RunEnd.absent_errors; sorted skips
  _core.pyi    stubs for every binding change
  __init__.py  limit, cancelled, absent_on
tests/
  test_threads_equivalence.py  test_parallel.py  test_limits.py  test_stopping.py   NEW
  test_run_many.py  test_absent_on.py  test_async_blocks.py   NEW
  test_threads.py  test_async.py  test_hooks.py  test_blocks_caching.py  test_pickling.py  test_public_api.py  conftest.py
  differential/harness.py  differential/test_deliberate_differences.py  differential/test_semantics.py
benchmarks/test_parallel.py (NEW)   benches/parallel.py (NEW)   benches/generate.py
docs/running.md (Threads, Deadlines, Partial results), hooks.md, caching.md, writing-nodes.md, from-hamilton.md,
  quirk-register.md, benchmarks.md, rust-core.md, index.md, README.md, CHANGELOG.md, the roadmap, the design's change log
.github/workflows/ci.yml   pyproject.toml (the threads marker)
```

## PR breakdown

Each PR follows Plan 10's steps: TDD task by task with each task's register rows first, `scripts/check.sh` green on
every commit, a CHANGELOG entry, an independent reviewer subagent, then the maintainer's go-ahead to squash-merge.

| PR | Tasks | Needs merged first | Runs in parallel with |
|---|---|---|---|
| **C1** The scheduler in the core (Rust only) | 1, 2 | the 0.5.0 release PR | none |
| **C2** The parallel sync driver | 3, 4 | C1 | none |
| **C3** Single-flight and limits | 5, 6 | C2 | C4 |
| **C4** The async driver on the scheduler, with blocks | 7, 8 | C2 | C3 |
| **C5** Stopping: failure, deadlines, cancellation, signals | 9 | C3 | D1 |
| **C6** `run_many` | 10 | C5 | D1 |
| **D1** Partial results | 11 | C4 | C5, C6 |
| **C7** Free-threading stress and the Threads docs | 12 | C5, C6, D1 | none |
| **Z** Close-out | 13 | C7 | none |

C1 waits for nothing but the release, and has no Python. C2 is the spine every later PR edits, so it runs alone. C3
and C4 touch disjoint code: C3 the parallel loop's grants and the caches, C4 the async driver and `stepper.rs`. C5
needs C3 because both edit the worker loop's wait in `parallel.rs`. D1 needs C4, since absence on error must hold on
the async driver's scheduler too. It touches the core, which C5 and C6 don't, and `task.rs`, where C5 edits a
different function.

**Textual collisions between parallel PRs**, each resolved by merging `main` in, never by rebasing:

- **C3 and C4:** `python/fnloom/driver.py` (C3: `Builder.with_threads`' `limits=` checks; C4: `build_async`),
  `python/fnloom/_core.pyi` (different classes), `docs/quirk-register.md` (Q85 and Q134 against Q32, Q65 and Q132),
  `CHANGELOG.md`.
- **C5 and D1:** `crates/fnloom-py/src/task.rs` (C5: `cancelled()` in the call's context copy; D1: a node's error
  classes on the failure path), `python/fnloom/__init__.py` and `tests/test_public_api.py` (C5 adds `cancelled`, D1
  `absent_on`),
  `python/fnloom/_hooks.py` (C5 nothing; D1 `RunEnd`), `docs/quirk-register.md`, `CHANGELOG.md`.
- **C6 and D1:** `python/fnloom/driver.py` (C6: `PreparedPlan.run_many`; D1: `Builder.with_absent_on`), `CHANGELOG.md`.

## Review Focus

1. **A branch fails while another worker is inside the expand's generator.** The generator is closed exactly once,
   after its pull returns, with no `ValueError: generator already executing`, and the node's own exception is raised.
   (Task 9, `test_a_failure_during_a_pull_closes_the_generator_after_it`)
2. **A hook raises in `after_node` under eight workers.** The run stops once, the hook lock is released (no other
   worker hangs on it), `after_run` fires once with `ok=False`, and the hook's exception is raised as itself, as on
   `Plan::run`. (Task 4, `test_a_raising_hook_stops_a_parallel_run_once`)
3. **A node calls `execute` on its own driver** with `with_threads(2, limits={"db": 1})`, and both the outer and the
   inner node are in group `db`. The inner run raises `RuntimeError` naming `'db'` at once, never hangs. (Task 6,
   `test_an_inner_run_needing_a_group_its_caller_holds_is_refused`)
4. **Interpreter exit with live pools**: one idle, one with a node abandoned by `timeout=` still sleeping. The process
   exits with status 0 and no fatal error from a worker attaching during finalization. (Task 9,
   `test_the_interpreter_exits_cleanly_with_an_abandoned_task`)
5. **`os.fork()` after a parallel run.** The child's first parallel run starts new threads and completes; the parent's
   pool is unaffected. (Task 3, `test_a_forked_child_gets_a_new_pool`)

---

## PR C1: the scheduler in the core (Rust only)

### Task 1: `Scheduler`, order keys, pulls and probes (design task 16)

**Files:**

- Create: `crates/fnloom-core/src/schedule.rs`, `crates/fnloom-core/src/limits.rs` (the `Grant` trait and a
  `Unlimited` grant only; `Limits` comes in Task 2), `crates/fnloom-core/tests/schedule_threads.rs`
- Modify: `crates/fnloom-core/src/{lib.rs,branch.rs,testing.rs}`, `crates/fnloom-core/benches/core.rs`,
  `docs/rust-core.md`
- Test: `schedule.rs`'s test module, `tests/schedule_threads.rs`

**Interfaces:**

- Consumes: `Plan`, `SubPlan`, `Plan::block_of`, `Plan::is_late`, `Plan::arg_edges`, `Item`, `Slot`, and the
  dependency and collect rules of `run.rs` and `branch.rs`, unchanged.
- Produces, in `schedule.rs`:

    ```rust
    pub struct TaskId(pub u32);
    /// (step position, branch index) pairs from the outermost plan inwards, then the step's position; Ord.
    pub struct OrderKey(Vec<u32>);
    pub enum ArgValue<V> { One(V), Gather(Vec<V>), Omitted }
    pub enum Work<V> {
        Compute { args: Vec<(NodeId, ArgValue<V>)>, expands: bool, late: bool },
        Iterate { iterable: V },
        Pull { iterator: V },
        Finish { iterator: V, exhausted: bool },
        Probe { block: BlockId },
    }
    pub struct Task<V> { pub id: TaskId, pub node: NodeId, pub branch: Vec<usize>, pub key: OrderKey, pub work: Work<V> }
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
        pub skipped: Vec<(NodeId, Vec<usize>)>,
        pub expanded: Option<(NodeId, Vec<usize>, usize)>,
        pub hits: Vec<(NodeId, Vec<usize>, V)>,
    }
    pub enum Seat { Take, Held, Threadless }
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

- Produces, in `limits.rs`: `pub trait Grant: Sync { fn try_start(&self, node: NodeId, seat: Seat) -> bool; fn
  release(&self, node: NodeId, seat: Seat); }`, and `pub struct Unlimited;` granting everything.
- Produces, in `branch.rs`: `pub(crate) fn needs<P, V>(plan: &Plan, graph: &Graph<P>, block: BlockId, lookup: impl
  FnMut(NodeId) -> Option<V>) -> Vec<Need>`, which `Runner` now calls; `Plan::run`'s behaviour is unchanged.
- Rules:
    - Ready tasks are handed out lowest `OrderKey` first, skipping one the grant refuses, so one worker reproduces
      `Plan::run`.
    - A pull is ordered just before its branch's first step (design §4.3). At most one pull of an iterator is in
      flight.
    - A `late` outer compute is looked up by the binding first. A `Hit` outcome completes it without a call.
    - Skips follow §7.2's three rules, and are reported in `Report.skipped`, not as a task.
    - `Report.released` holds values whose last reader completed, for the caller to drop attached.
    - `Next::Wait` means tasks are in flight or refused by the grant; `Next::Done` means nothing is left. Once
      stopped, only `Finish` tasks are handed out (Task 2 adds the stop rules).
- [ ] **Step 1: Write the failing Rust tests.**
    - `one_worker_matches_plan_run_task_for_task`: over diamond, chain, layered, nested-block and absence graphs from
      `testing.rs`, stepping with `Unlimited` and one task at a time gives the same outputs, and the same sequence of
      (node, branch) calls, as `Plan::run` with a recording evaluator;
    - `order_keys_compare_by_step_then_branch`: `(0, 3, 1) < (0, 3, 2) < (1)`, and a pull's key precedes its branch's
      first step;
    - `ready_tasks_are_handed_out_lowest_key_first`: two independent steps both ready; `next` gives the lower first;
    - `a_refused_task_is_skipped_for_the_next_ready_one`: a grant refusing node `a` makes `next` hand out `b`, and
      `a` once the grant allows;
    - `one_pull_per_iterator_in_flight`;
    - `a_collect_waits_for_every_branch_and_gathers_in_branch_order`, with branches completed in reverse;
    - `a_probe_hit_skips_the_branch_upstream_and_is_reported`;
    - `a_late_outer_hit_completes_without_a_call`;
    - `skips_follow_the_three_rules_and_are_reported`;
    - `released_values_are_returned_to_the_caller`, with the `Counted` drop counter: nothing is dropped inside the
      scheduler;
    - `an_expand_requested_alone_finishes_its_items_as_its_value`;
    - `no_recursion_on_a_1000_deep_nesting_or_a_50000_chain_in_a_block`;
    - in `tests/schedule_threads.rs`, `eight_threads_behind_a_mutex_give_plan_runs_outputs`: eight `std::thread`s
      share `Mutex<Scheduler>` and a fake evaluator with random sleeps, over a nested fan-out of 1,000 items; outputs
      equal `Plan::run`'s. This also proves `Scheduler: Send` for `Send` values.
- [ ] **Step 2: Run them to see them fail.** Run `cargo test -p fnloom-core schedule`. Expected: they don't compile.
- [ ] **Step 3: Implement.** `Scheduler` keeps one `Vec<Option<Slot<V>>>` per open branch, the frames of `branch.rs`
  as an explicit stack per expand instance, and a `BinaryHeap<Reverse<(OrderKey, TaskId)>>` of ready tasks. Move the
  reverse-need walk into `branch::needs`, and call it from `Runner`.
- [ ] **Step 4: Run the tests to see them pass**, then `cargo bench --no-run -p fnloom-core`, with new rows
  `schedule.layered_{1000,10000}` and `schedule.chain_{1000,10000}`: a one-worker loop over `next` and `complete`
  with a trivial evaluator. Expected: PASS, and the existing rows' counts unchanged.
- [ ] **Step 5: Document** `Scheduler`, order keys and the task kinds in `docs/rust-core.md`.
- [ ] **Step 6: Run `scripts/check.sh`, then commit** `core: the Scheduler, matching Plan::run task for task`.

### Task 2: limits, the in-flight bound, single-flight and stop, in the core (design tasks 19 to 21, core part)

**Files:**

- Modify: `crates/fnloom-core/src/{schedule.rs,limits.rs}`, `docs/rust-core.md`
- Test: their test modules, `tests/schedule_threads.rs`

**Interfaces:**

- Consumes: Task 1.
- Produces, in `limits.rs`:
    - `pub struct Limits`, implementing `Grant` with atomics only:
      `Limits::new(max_workers: usize, node_caps: Vec<Option<usize>>, node_groups: Vec<Option<u32>>, group_caps:
      Vec<usize>) -> Self`, indexed by `NodeId::index` and group id.
    - A task holds one worker slot unless its seat is `Held` (a nested run's caller) or `Threadless` (an async
      node), plus its node's slot and its group's slot when it computes or pulls that node.
    - `Limits::held(&self, group: u32) -> usize`, for the re-entrancy check of Task 6.
- Produces, in `schedule.rs`:
    - `Options.in_flight`: an expand's iterator is pulled only while fewer than `in_flight` of its branches are
      unfinished (`2 * max_workers`, set by the binding).
    - Single-flight: a `Needs` outcome's `keys[i]` is the key of the branch's step `i`, or `None`. A needed step
      whose key equals a running task's waits on it instead of being handed out. When that task completes with a
      value or a declared absence, the waiter completes as a hit (reported in `Report.hits`). When it fails, the
      waiter becomes ready and runs itself.
    - `Scheduler::blocked(&self) -> Option<NodeId>`: the lowest-key ready task's node, when the grant refused it and
      nothing is in flight.
    - Stop: `stop()`, or a `Failed` outcome, ends handing out compute, pull and probe tasks. Failures are kept with
      their order keys, and `finish` returns the lowest one's error, with the closing errors of `Finish` tasks
      appended as `Plan::run` appends them.
- [ ] **Step 1: Write the failing tests.**
    - `limits_cap_workers_nodes_and_groups_across_two_schedulers`: two schedulers share one `Limits(2, ...)`; never
      more than two tasks out, never more than one of a node capped at 1, never more than the group's cap;
    - `a_held_seat_takes_no_worker_slot` and `a_threadless_seat_takes_node_and_group_slots_only`;
    - `the_in_flight_bound_holds_pulls_back`: with `in_flight: 4`, a 10,000-item expand has at most four unfinished
      branches at any time;
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

### Task 3: `with_threads`, the pool, attach and detach (design task 17, Q134, Q169)

**Files:**

- Create: `crates/fnloom-py/src/{task.rs,pool.rs,parallel.rs}`, `python/fnloom/_threads.py`, `tests/test_parallel.py`,
  `tests/test_threads_equivalence.py`
- Modify:
    - `crates/fnloom-py/src/`: `plan.rs` (`Caller` over `task.rs`; `run(threads=, timeout=)`), `lib.rs`;
    - `python/fnloom/`: `driver.py` (`Builder.with_threads`, `_Settings`, `_Changes`, `_builder_of`, `Driver`),
      `_caching.py` (`CacheRun.run` passes `threads`), `_core.pyi`, `__init__.py` (nothing exported yet);
    - `tests/conftest.py` (the watchdog), `tests/test_pickling.py`, `tests/differential/harness.py`,
      `tests/differential/test_semantics.py`, `tests/differential/test_deliberate_differences.py`;
    - `pyproject.toml` (the `threads` marker), `.github/workflows/ci.yml` (`timeout-minutes`, the `stress` job);
    - `docs/quirk-register.md` (Q134, Q169; Q119, Q122, Q123, Q137, Q138 amended).

**Interfaces:**

- Consumes: Tasks 1 and 2.
- Produces in the binding:
    - `_core.Threads(graph: Graph, max_workers: int, node_caps: dict[str, int], node_groups: dict[str, str],
      group_caps: dict[str, int])`, a frozen pyclass holding `Arc<Limits>` and the pool. `close()` stops and joins
      its threads, detached; it is idempotent.
    - `_core.Plan.run(inputs, overrides, observers=(), on_value=None, late=None, *, threads: Threads | None = None,
      timeout: float | None = None) -> dict`. With `threads`, the run is a `parallel::Run`: an `Arc` holding
      `Mutex<Scheduler<Arc<Py<PyAny>>, PyErr, [u8; 32]>>`, a `Condvar`, the hook lock and the context snapshot. The
      calling thread works too, as one of the `max_workers`.
    - Worker loop: take a task (rule 1), attach, run it through `task.rs`, fire its hooks under the hook lock, then
      `complete`, dropping `Report.released` attached.
- Produces in Python:
    - `Builder.with_threads(max_workers: int | None = None, limits: Mapping[str, int] | None = None) ->
      Builder[R]`. `max_workers=None` is `os.process_cpu_count() or 1`. A `bool`, a non-int or a value under 1 is a
      `TypeError` or `ValueError` naming the argument. `limits` is checked in Task 6; until then a non-empty one is a
      `ValueError` saying groups arrive with `fnloom.limit`.
    - `_threads.make_threads(graph, nodes, max_workers, limits) -> _core.Threads`, and `_threads.close_all()`,
      registered with `atexit` and `os.register_at_fork(after_in_child=...)` over a `weakref.WeakSet` of live
      `Threads`.
    - `Driver` keeps `_threads: _core.Threads | None`, made at build, its pool started at the first run.
- [ ] **Step 1: Write the register rows** Q134 (design §10's text) and Q169, and amend Q119, Q122, Q123, Q137 and
  Q138, then their tests:
    - `deliberate("Q134")`: Hamilton's default runs four branches at once (a `Barrier(4)` passes); fnloom without
      `with_threads` runs one at a time (a `Barrier(2, timeout=0.5)` breaks), and with `with_threads(4)` the
      `Barrier(4)` passes.
    - `deliberate("Q169")`: two independent slow nodes outside any block meet at a `Barrier(2)` under fnloom's
      `with_threads(2)`; under Hamilton's default executor they don't (the barrier times out).
    - The KEEP and FIX reruns of [Differential tests](#differential-tests-against-hamilton), with the harness's new
      `executor=` and `threads=`.
- [ ] **Step 2: Write the failing fnloom tests** in `tests/test_parallel.py` and `tests/test_threads_equivalence.py`:
    - `test_with_threads_overlaps_independent_nodes`: four nodes meet at a `Barrier(4)` with `with_threads(4)`;
    - `test_with_threads_one_runs_on_the_callers_thread`: every node reads `threading.current_thread()` as the caller;
    - `test_no_pool_thread_starts_before_the_first_run` and `test_the_pool_holds_max_workers_minus_one_threads`,
      counted by thread name `fnloom-worker-`;
    - `test_results_are_equal_over_worker_counts` (the hypothesis property; marked `threads`);
    - `test_with_threads_survives_pickle_copy_rebuild_and_build_async`;
    - `test_bad_max_workers_are_refused`: `0`, `-1`, `True`, `1.5`;
    - `test_a_forked_child_gets_a_new_pool` (Review Focus 5; skipped on Windows);
    - `test_a_parallel_run_keeps_the_gil_off` (3.14t only, in a subprocess);
    - `test_a_driver_without_with_threads_calls_plan_run`: a spy on `_core.Plan.run` sees `threads=None`.
- [ ] **Step 3: Run them to see them fail.** Run `uv run maturin develop --features test-hooks && UV_NO_SYNC=1 uv run
  pytest tests/test_parallel.py tests/test_threads_equivalence.py tests/differential -k "parallel or threads or
  Q134 or Q169"`. Expected: `AttributeError` on `with_threads`.
- [ ] **Step 4: Implement**, following the [safety rules](#free-threading-safety-rules). `task.rs` takes over
  `Caller::call`'s body and the `Blocks` methods, so `Caller` is a thin adapter; the `benches.run` rows and the
  instruction-count gate show no change.
- [ ] **Step 5: Run the tests to see them pass**, plus the whole `tests/` suite, on both builds if both are installed.
- [ ] **Step 6: Run `scripts/check.sh`, then commit** `with_threads: a parallel executor on the core scheduler (Q134,
  Q169)`.

### Task 4: hooks per run, per-call contexts, positions (design task 18, Q167, Q168)

**Files:**

- Modify: `crates/fnloom-py/src/{parallel.rs,observer.rs,task.rs}`, `python/fnloom/{_hooks.py,_caching.py,_core.pyi}`,
  `docs/hooks.md`, `docs/quirk-register.md`
- Test: `tests/test_parallel.py`, `tests/test_hooks.py`, `tests/test_blocks_caching.py`,
  `tests/differential/test_deliberate_differences.py`

**Interfaces:**

- Consumes: Task 3's run.
- Produces:
    - `NodeStart.position: int` and `NodeEnd.position: int`, the node's index in `RunStart.nodes`, on every driver.
    - Hooks of one run are serialized by its hook lock and run on the thread that ran the node.
    - Each task call runs in `snapshot.copy()`, with `_blocks.BRANCH` set in the copy.
    - `RunEnd.skipped` is sorted in reference order on a run with `with_threads` (call 8).
    - `LateKeys` gains `_lock: threading.Lock`, held around its memo tables, with store calls outside it.
- [ ] **Step 1: Write the register rows** Q167 and Q168, amend Q84 (positions) and Q86, then their tests:
    - `deliberate("Q167")`: a caller's `ContextVar` reads `"unset"` in Hamilton's block nodes and `"caller"` in
      fnloom's under `with_threads(4)`;
    - `deliberate("Q168")`: Hamilton's before-node hook is entered by four threads at once (a counter's maximum is
      4); fnloom's never by two (maximum 1), with `with_threads(4)`.
- [ ] **Step 2: Write the failing tests.**
    - `test_hook_calls_of_one_run_never_overlap`: a hook that sleeps 5 ms and counts concurrent entries sees at most
      one, over 200 branches on eight workers;
    - `test_a_hook_runs_on_the_thread_that_ran_the_node`;
    - `test_with_one_worker_events_equal_the_sequential_run`, positions included;
    - `test_positions_sort_parallel_events_into_plan_order`;
    - `test_a_contextvar_set_in_one_node_is_not_seen_by_another`, over worker counts 1, 2 and 8;
    - `test_current_branch_on_workers_is_each_nodes_own`;
    - `test_a_raising_hook_stops_a_parallel_run_once` (Review Focus 2);
    - `test_print_nodes_and_log_failures_under_threads`;
    - `test_late_keys_are_safe_under_eight_workers`: a cached nested fan-out with mixed hits gives the sequential
      result and keys.
- [ ] **Step 3: Run them to see them fail. Step 4: Implement. Step 5: Run the tests to see them pass**, plus `pytest
  tests/test_hooks*.py tests/test_print_nodes.py`.
- [ ] **Step 6: Run `scripts/check.sh`, then commit** `Hooks per run and per-call contexts under threads (Q167, Q168;
  amends Q84, Q86)`.

## PR C3: single-flight and limits

### Task 5: single-flight per key (design task 19)

**Files:**

- Modify: `crates/fnloom-py/src/{parallel.rs,task.rs}`, `python/fnloom/_caching.py` (`LateKeys.key` exposed to the
  probe), `docs/caching.md`, `docs/quirk-register.md`
- Test: `tests/test_blocks_caching.py`

**Interfaces:**

- Consumes: Task 2's single-flight, Task 3's probe.
- Produces: the binding's `Probe` asks `late.lookup(node, branch)` as today and `late.key(node, branch)` for each
  needed step, passing the 32-byte digest, or `None` for an uncacheable or `recompute` step. A duplicate's events are
  a hit's (Q85): `cache` outcome `hit`, no wrapper call.
- [ ] **Step 1: Amend Q85**: a single-flight duplicate reports a hit.
- [ ] **Step 2: Write the failing tests.**
    - `test_equal_items_compute_once_per_run`: items `["a", "a", "b"]`, a node counting calls, `with_threads(4)` and a
      `MemoryCache`; `a`'s branch node is called once, and the second fires `cache` outcome `hit`;
    - `test_a_failed_original_does_not_fail_its_duplicate_by_itself`: the duplicate runs, and its own outcome
      decides;
    - `test_recompute_nodes_are_never_deduplicated`;
    - `test_without_a_cache_both_run`;
    - `test_single_flight_never_waits_a_worker`: with `with_threads(2)`, a duplicate of a running slow node frees its
      worker for other branches (a `Barrier(2)` between the slow node and another branch passes).
- [ ] **Step 3: Run them to see them fail. Step 4: Implement. Step 5: Run the tests to see them pass.**
- [ ] **Step 6: Run `scripts/check.sh`, then commit** `Single-flight per key within a run (amends Q85)`.

### Task 6: `limit`, groups, the in-flight bound and runs inside runs (design task 20)

**Files:**

- Create: `tests/test_limits.py`
- Modify: `python/fnloom/{_threads.py,_decorators.py,_scan.py,driver.py,__init__.py,_core.pyi}`,
  `crates/fnloom-py/src/{pool.rs,parallel.rs}`, `docs/running.md`, `docs/quirk-register.md` (Q134's limits),
  `tests/test_public_api.py`, `tests/test_stacking.py`

**Interfaces:**

- Consumes: Task 2's `Limits`, Task 3's `Threads`.
- Produces:
    - `fnloom.limit(n: int | None = None, *, group: str | None = None) -> Callable[[F], F]`. At least one of the two
      is given; both may be (O10). It records `Decorations.limit: LimitPolicy(n, group)`, read into `NodeDef.limit`.
    - A group must be sized by `with_threads(limits={"db": 4})`. A driver that honours limits refuses an unsized
      group at build with `ValueError: group 'db' of 'load' has no size; give it one with
      Builder.with_threads(limits={'db': n})`. A `Driver` without `with_threads` runs one node at a time, so it honours
      every limit already, and checks nothing (O9). `limits=` naming no node's group is a `ValueError` too.
    - `Options.in_flight = 2 * max_workers`.
    - Runs inside runs: a thread-local stack of the seats a thread holds. A run started on a thread that holds a
      worker slot uses `Seat::Held` for the tasks it runs itself. When `Scheduler::blocked()` names a node whose
      group's every slot is held by this run's ancestors (`Limits::held` against the stack), the inner run raises
      `RuntimeError: 'load' needs group 'db', but every slot of it is held by the runs that called this one`.
- [ ] **Step 1: Write the failing tests** in `tests/test_limits.py`:
    - `test_limit_caps_a_nodes_concurrent_calls`: 32 branches, `limit(2)`, eight workers; a concurrency counter's
      maximum is 2;
    - `test_a_group_caps_its_members_together`;
    - `test_limits_hold_across_concurrent_runs_of_one_driver`: four threads run at once; the group cap holds across
      them;
    - `test_an_unsized_group_is_refused_at_build`, and `test_a_sequential_driver_accepts_any_limit`;
    - `test_the_in_flight_bound_keeps_a_large_generator_lazy`: a 10,000-item generator with `with_threads(2)` has at
      most four unfinished branches (a counter of items pulled minus branches finished);
    - `test_a_node_may_run_its_own_driver_inside_its_slot`: nesting never waits for `max_workers`;
    - `test_an_inner_run_needing_a_group_its_caller_holds_is_refused` (Review Focus 3);
    - `test_limit_stacks_with_every_decorator` (extending `tests/test_stacking.py`'s pair test);
    - `test_public_names`: `limit` is in `fnloom.__all__`.
- [ ] **Step 2: Run them to see them fail. Step 3: Implement. Step 4: Run the tests to see them pass.**
- [ ] **Step 5: Run `scripts/check.sh`, then commit** `limit(), groups, the in-flight bound and runs inside runs`.

## PR C4: the async driver on the scheduler, with blocks

### Task 7: `AsyncDriver` on the `Scheduler`, its sync nodes on the pool (design task 22, Q65)

**Files:**

- Create: `crates/fnloom-py/src/stepper.rs`
- Modify: `crates/fnloom-py/src/{plan.rs,pool.rs,lib.rs}`, `python/fnloom/{_async.py,driver.py,_core.pyi}`,
  `docs/running.md`, `docs/quirk-register.md`
- Test: `tests/test_async.py`, `tests/test_hooks_async.py`, `tests/test_parallel.py`

**Interfaces:**

- Consumes: Tasks 1 to 4.
- Produces:
    - `_core.Plan.start(inputs, overrides, *, threads: Threads | None) -> Stepper`. `Stepper.next() -> tuple[int,
      str, tuple[int, ...], str, object] | None` gives `(task, node, branch, work, payload)`, with `work` one of
      `"compute"`, `"iterate"`, `"pull"`, `"finish"` and `"probe"`, or `None` for wait; `Stepper.done: bool`;
      `Stepper.complete(task: int, kind: str, value: object, absent: bool) -> tuple[list[tuple[str, tuple[int,
      ...]]], tuple[str, tuple[int, ...], int] | None]` returns the skipped nodes and the finished expand, for the
      hooks; `Stepper.stop()`; `Stepper.outputs() -> dict`. Every call detaches around the lock (rule 1).
    - `_async._Run` steps a `Stepper`: an async node is a task on the loop, a sync node goes to the loop's default
      executor, or to `Threads.submit(fn, context) -> concurrent.futures.Future` (wrapped by `asyncio.wrap_future`) on
      a driver with `with_threads`. Async nodes take `Seat::Threadless`.
    - `_async.Schedule` and `CachedSchedule`'s dependency counts are removed; `CachedSchedule` keeps its keys and
      hit plans, and its late keys go through the stepper's probes.
    - `with_threads` and `build_async(sync_nodes="inline")` together: `ValueError: with_threads runs sync nodes on
      the driver's pool, so sync_nodes='inline' can't apply; drop one`.
- [ ] **Step 1: Amend Q65**, with `deliberate("Q65")` extended: under `with_threads(2)`, a sync node runs on a
  `fnloom-worker-` thread, not the loop's default executor.
- [ ] **Step 2: Write the failing tests.**
    - every existing test in `tests/test_async.py` and `tests/test_hooks_async.py` passes unchanged on the stepper:
      it is the async driver's regression suite;
    - `test_async_with_threads_runs_sync_nodes_on_the_pool`;
    - `test_async_nodes_hold_no_worker_slot`: `with_threads(1)`, two `async def` nodes meet at an `asyncio.Barrier(2)`;
    - `test_async_limits_apply_to_async_nodes`: `limit(1)` on an async node keeps its calls apart;
    - `test_with_threads_and_inline_are_refused`;
    - `test_async_events_equal_the_sync_drivers_with_one_worker`: for a graph of sync nodes on `sync_nodes="inline"`,
      the event sequence equals `Driver`'s.
- [ ] **Step 3: Run them to see them fail. Step 4: Implement. Step 5: Run the tests to see them pass**, then
  `python -m benches.async_` against 0.5.0: every row within 10% (the [targets](#benchmark-targets)). A row outside
  it is fixed in this task, or the task stops and reports it.
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
      only. `build()` refuses it as any async node (Q12, Q32). Every other async generator stays refused.
    - Pulls: an async generator's `__anext__` is awaited on the loop; a sync iterator's `__next__` runs where the
      expand's sync node would (`sync_nodes`, or the pool). Closing uses `aclose()` and `close()`.
    - A failing branch cancels every running task once, as Q66 does for nodes.
- [ ] **Step 1: Rewrite Q132** as FIX (no longer *Planned*), and amend Q32. `deliberate("Q132")`: Hamilton's async
  builder refuses dynamic execution; fnloom's async driver runs the block.
- [ ] **Step 2: Write the failing tests** in `tests/test_async_blocks.py`:
    - `test_an_async_generator_expand_fans_out`;
    - `test_async_branches_overlap`: four `async def` branch nodes meet at an `asyncio.Barrier(4)`;
    - `test_async_collect_is_in_branch_order` with branches finishing in reverse;
    - `test_a_failing_async_branch_cancels_the_others_and_closes_the_generator_once`;
    - `test_async_blocks_cache_as_sync_blocks_do`: the same keys as `Driver`;
    - `test_an_async_generator_on_build_is_refused`;
    - in `tests/test_absence.py`, the Plan 10 block-absence tests parametrized over `driver_kind` gain `"async"`.
- [ ] **Step 3: Run them to see them fail. Step 4: Implement. Step 5: Run the tests to see them pass.**
- [ ] **Step 6: Run `scripts/check.sh`, then commit** `Blocks on the async driver (Q132; amends Q32)`.

## PR C5: stopping

### Task 9: failure, `timeout=`, `cancelled()`, `close()` on early stop, signals (design task 21, Q128, Q170)

**Files:**

- Create: `tests/test_stopping.py`
- Modify: `crates/fnloom-py/src/{parallel.rs,pool.rs,task.rs}`, `python/fnloom/{_threads.py,driver.py,_caching.py,
  __init__.py,_core.pyi}`, `docs/running.md`, `docs/quirk-register.md`, `tests/test_public_api.py`
- Test: `tests/test_stopping.py`, `tests/differential/test_deliberate_differences.py`

**Interfaces:**

- Consumes: Tasks 2 and 3.
- Produces:
    - `Driver.execute(final_vars, *, inputs=None, overrides=None, timeout: float | None = None)`, and the same
      keyword on `raw_execute`, `PreparedPlan.run` and `PreparedPlan.run_raw`. Without `with_threads` it is a
      `TypeError: timeout= needs Builder.with_threads(...)` (O7). `AsyncDriver` takes no `timeout=`: the caller's
      `asyncio.timeout` already cancels the run (Q66, §6.5).
    - With a deadline, the calling thread runs no tasks: it waits detached in 50 ms slices; the pool grows to
      `max_workers` threads (O8). When the deadline passes, the run stops, `cancelled()` turns `True`, open iterators
      with no pull in flight are finished, and `TimeoutError` is raised at once. Running tasks are abandoned: their
      results are dropped, their slots released when they return.
    - `fnloom.cancelled() -> bool`: `_threads.CANCELLED.get()`, a `ContextVar[_core.Stop | None]` set in each task's
      context copy; `False` outside a run, and on a driver without `with_threads`.
    - On a failure, the sync driver waits for the running tasks, then raises the lowest order key's error, noted as
      today (`while computing 'details' for branch (3,) of 'picked'`).
    - `KeyboardInterrupt` in the caller, raised by `check_signals` or inside a task it runs, stops the run as a
      failure, then propagates.
- [ ] **Step 1: Write the register row** Q170 and amend Q66 and Q128, then their tests:
    - `deliberate("Q170")`: Hamilton has no `timeout`; fnloom's `execute(..., timeout=0.2)` on a node sleeping 5 s
      raises `TimeoutError` within 1 s;
    - `keeps("Q128")` rerun in parallel, as [above](#differential-tests-against-hamilton).
- [ ] **Step 2: Write the failing tests** in `tests/test_stopping.py`:
    - `test_a_failure_waits_for_running_branches`: no branch is still running when `execute` raises;
    - `test_the_lowest_order_key_failure_is_raised`: two failing branches, the later one failing first;
    - `test_every_failure_seen_reaches_after_node`: both get `ok=False`, and one exception is raised, never an
      `ExceptionGroup`;
    - `test_a_failure_during_a_pull_closes_the_generator_after_it` (Review Focus 1);
    - `test_timeout_raises_at_once_and_abandons_running_tasks`;
    - `test_cancelled_turns_true_for_running_nodes`: a node polling `cancelled()` returns early;
    - `test_a_generator_is_closed_on_timeout_when_no_pull_is_in_flight`;
    - `test_timeout_without_with_threads_is_a_type_error`;
    - `test_interrupt_main_stops_a_parallel_run` (`_thread.interrupt_main()` from a node);
    - `test_the_interpreter_exits_cleanly_with_an_abandoned_task` (Review Focus 4, a subprocess);
    - `test_a_deadline_releases_limits_when_the_abandoned_task_returns`;
    - `test_public_names`: `cancelled` is in `fnloom.__all__`.
- [ ] **Step 3: Run them to see them fail. Step 4: Implement. Step 5: Run the tests to see them pass.**
- [ ] **Step 6: Run `scripts/check.sh`, then commit** `Stopping parallel runs: failure, deadlines, cancellation (Q170;
  amends Q66, Q128)`.

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
      None, timeout: float | None = None) -> list[R]`: one result per input set, in order, each built by the result
      builder. `overrides` is shared by every set (O14). Each set is checked as `run` checks it, all before any runs.
    - On a driver with `with_threads`, every set is its own run on the driver's pool, under its limits, with
      `max_workers` shared. Without, the sets run one after another on the calling thread (O16).
    - Each set is its own run for hooks: its own `run_id`, `RunStart` and `RunEnd`, and its own hook lock (O18).
    - A failure stops every set's run, waits as Task 9 does, and raises the error of the lowest-indexed failing set
      among those seen, with the note `while running input set 3 of run_many` (O17).
    - `_core.Plan.run_many(inputs_seq, overrides, observers_seq, on_value_seq, late_seq, *, threads, timeout) ->
      list[dict]`, so the sets share one wait on the calling thread.
- [ ] **Step 1: Write the register row** Q171 and its `deliberate` test: Hamilton has no batch method; fnloom's
  `run_many` over 50 input sets equals 50 `run` calls, in order.
- [ ] **Step 2: Write the failing tests.**
    - `test_run_many_returns_one_result_per_set_in_order`, with sets finishing in reverse;
    - `test_run_many_overlaps_sets_on_the_pool`: four sets meet at a `Barrier(4)` under `with_threads(4)`;
    - `test_run_many_without_threads_runs_sets_in_order`;
    - `test_run_many_checks_every_set_before_running_any`;
    - `test_a_failing_set_raises_with_its_index`;
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
    - `crates/fnloom-core/src/`: `node.rs` (`NodeSpec.absent_on_error`, `Node.absent_on_error`), `graph.rs`
      (`may_be_absent`), `run.rs` (`Evaluate::absent_on_error`, `Observer::absent_error`, both loops), `branch.rs`,
      `schedule.rs` (`Outcome::ErrorAbsent { value, error }`), `testing.rs`;
    - `crates/fnloom-py/src/`: `graph.rs` (the node tuple), `task.rs` (the error classes per node), `observer.rs`,
      `plan.rs`, `stepper.rs`;
    - `python/fnloom/`: `_absence.py`, `_decorators.py`, `_scan.py`, `driver.py` (`Builder.with_absent_on`), `_hooks.py`
      (`RunEnd.absent_errors`), `_caching.py` (the poison), `_async.py`, `_core.pyi`, `__init__.py`;
    - `docs/writing-nodes.md` ("Partial results"), `docs/caching.md`, `docs/hooks.md`, `docs/quirk-register.md`
      (Q133, Q157, Q84 amended);
    - `tests/test_public_api.py`, `tests/test_pickling.py`, `tests/test_stacking.py`.
- Test: `tests/test_absent_on.py`, the core's test modules, `tests/differential/test_deliberate_differences.py`

**Interfaces:**

- Consumes: Plan 10's absence rules and `RunEnd.skipped`; Tasks 1, 7 and 8.
- Produces in the core:
    - `NodeSpec.absent_on_error: bool`. A node with it may be absent. `is_absent` is still asked only of a node that
      `declines`.
    - `Evaluate::absent_on_error(&mut self, node: &Node<P>, error: &Self::Error) -> Option<Self::Value>` (default
      `None`), asked only of a node with `absent_on_error` whose call failed. `Some(v)` makes its outcome absent with
      value `v`.
    - `Observer::absent_error(&mut self, node: &str, branch: &[usize], error: &E) -> Result<(), E>` (default
      `Ok(())`), fired after `post_node_in` reports the error, so `after_node` gets `ok=False` with it.
    - The `Scheduler` takes `Outcome::ErrorAbsent { value, error }` and treats it as an absence that is never shared
      by single-flight.
- Produces in Python:
    - `fnloom.absent_on(*errors: type[BaseException]) -> Callable[[F], F]`. No class, or a non-exception class, is a
      `TypeError` when decorating. The annotation check runs at build: a node whose return annotation doesn't admit
      `Absent` is a `ValueError` naming it and saying to add `| Absent` (O11).
    - `Builder.with_absent_on(*errors: type[BaseException], tags: Mapping[str, object] | None = None) -> Builder[R]`:
      every node, or the nodes matching `tags` by `NodeViews.tagged`'s rules (Q141; O12). Covered nodes may be absent
      whatever their annotation. A node with both its own `absent_on` and the driver's catches the union.
    - `RunEnd.absent_errors: tuple[tuple[str, tuple[int, ...], BaseException], ...]`, in reference order (call 8).
    - Caching: an error absence is never stored; its version is a fresh per-run nonce, so every key chained from it,
      a `Collect` that dropped it included, is uncacheable in that run (design §4.6).
    - `KeyboardInterrupt`, `SystemExit` and `asyncio.CancelledError` are never caught, even when listed: listing one is
      a `TypeError` when decorating.
- [ ] **Step 1: Write the register row** Q133 (design §10's text), rewrite Q157 as FIX pointing at Q133, amend Q84
  (`RunEnd.absent_errors`), then `deliberate("Q133")`: Hamilton's `GracefulErrorAdapter(KeyError)` gives the collect
  `[1, None, 3]`, indistinguishable from a branch returning `None`; fnloom's `absent_on(KeyError)` gives `[1, 3]`, and
  `Collect[int | Absent]` gives `[1, ABSENT, 3]`.
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
    - `test_with_absent_on_covers_tagged_nodes_whatever_their_annotation`;
    - `test_a_covered_node_still_cannot_return_absent_itself`: the return check's `TypeError`;
    - `test_interrupts_and_cancellation_are_never_caught`;
    - `test_with_absent_on_survives_pickle_copy_rebuild_and_build_async`;
    - `test_absent_errors_are_equal_over_worker_counts`;
    - `test_absent_on_stacks_with_every_decorator` (extending `tests/test_stacking.py`).
- [ ] **Step 4: Run them to see them fail. Step 5: Implement. Step 6: Run the tests to see them pass**, plus the
  whole suite.
- [ ] **Step 7: Run `scripts/check.sh`, then commit** `absent_on and with_absent_on: partial results (Q133; resolves
  Q157, amends Q84)`.

## PR C7: free-threading stress and the Threads docs

### Task 12: stress tests and the Threads section (design task 25)

**Files:**

- Modify: `tests/test_threads.py`, `tests/conftest.py` (the `stress` hypothesis profile), `docs/running.md`
  ("Threads" rewritten), `docs/caching.md` ("Stores"), `docs/hooks.md`
- Test: `tests/test_threads.py`

**Interfaces:**

- Consumes: everything in C.
- Produces: `tests/test_threads.py` tests that loop `FNLOOM_STRESS_ROUNDS` times (default 10), each marked `threads`.
- [ ] **Step 1: Write the stress tests.**
    - `test_parallel_runs_share_one_driver_pool_and_cache`: 16 caller threads, each running a nested, cached fan-out
      with `with_threads(8)`, on one driver and one `MemoryCache`; every result equals the single-threaded one, and
      the limits' counters return to zero;
    - `test_parallel_runs_share_one_disk_cache`, the same over `DiskCache`;
    - `test_a_shared_hook_sees_each_parallel_run_whole`: per `run_id`, `before_node` precedes `after_node`, and a node's
      `after_node` follows each computed input's;
    - `test_run_many_and_execute_mixed_on_one_driver`;
    - `test_async_and_sync_drivers_of_one_builder_under_load`;
    - `test_limits_never_overshoot_under_contention`, with a group of 3 over 500 branches on eight workers.
- [ ] **Step 2: Run them on both builds**, with `FNLOOM_STRESS_ROUNDS=200`. Expected: PASS. A failure is a bug in an
  earlier task: fix it there, in its own commit, with a test.
- [ ] **Step 3: Rewrite `docs/running.md`'s Threads section** from the [safety rules](#free-threading-safety-rules):
  what is shared, what one run serializes, what your code must make safe, the GIL build's behaviour (nodes that
  release the GIL overlap; pure Python nodes take turns), deadlines, `cancelled()` and runs inside runs.
- [ ] **Step 4: Run `scripts/check.sh`, then commit** `Free-threading stress tests, and the Threads docs`.

## PR Z: close-out

### Task 13: benchmarks, docs, the example, the roadmap and the design change log (design task 27)

**Files:**

- Create: `benchmarks/test_parallel.py`, `benches/parallel.py`
- Modify:
    - `benches/generate.py` (sleeping and CPU-bound branch bodies), `crates/fnloom-core/benches/baseline.json`
      (the `schedule` rows);
    - `tests/test_fan_out_example.py` (the parallel and partial cases);
    - `docs/`: `benchmarks.md` ("Parallel fan-out", "The scheduler"), `running.md`, `from-hamilton.md`, `index.md`;
    - `README.md`, `CHANGELOG.md`, the roadmap, and the design's §12.3 and §14.

**Interfaces:**

- Consumes: everything above.
- The example: Plan 10's catalog module, with `details` decorated `@fnloom.absent_on(TimeoutError)` and annotated `->
  Detail | Absent`, and `summary(details: Collect[Detail])`, run with `with_threads(8)`.
- [ ] **Step 1: Write the failing end-to-end tests** in `tests/test_fan_out_example.py`:
    - `test_the_listing_fans_out_in_parallel`: eight fake endpoint calls meet at a `Barrier(8)`;
    - `test_a_timed_out_item_is_left_out_of_the_summary`;
    - `test_a_partial_summary_is_not_cached_as_a_whole`: the next run calls the failed item's endpoint again.
- [ ] **Step 2: Run them, implement any gap found, and run them to see them pass.** A gap is a bug in an earlier task:
  fix it there, in its own commit, with a test.
- [ ] **Step 3: Add the benchmarks** of [Benchmark targets](#benchmark-targets): `python -m benches.parallel` prints
  one JSON line per (shape, branches, runtime, workers, build), with `runtime` one of `fnloom`, `fnloom-threads`,
  `hamilton-sync` and `hamilton-threads`. Record them in `docs/benchmarks.md` for 3.14 and 3.14t. A target missed is
  reported in the table, and is a bug by the benchmarks page's rule.
- [ ] **Step 4: Commit the `schedule` rows' baseline** with `scripts/bench_gate.py --write-baseline` in the
  `instruction-counts` job's image, saying why in the commit message.
- [ ] **Step 5: Write the docs.**
    - `docs/running.md` gets "Running in parallel", "Deadlines and cancellation" and "Partial results", with the
      example as their sample.
    - `docs/from-hamilton.md`: rewrite the executor and `GracefulErrorAdapter` rows (Q133, Q134, Q167 to Q171), and
      empty "Not there yet" of both.
- [ ] **Step 6: Update the roadmap and the design.**
    - Roadmap item 10 says Plan 11 is built and ships in 0.6.0, and the status line says so. The ledger rows for
      dynamic execution and `GracefulErrorAdapter` become **fixed (0.6.0)**.
    - The design's §12.3 links this plan, and §14 gets a change-log line saying what was built and every departure
      in [Calls this plan makes](#calls-this-plan-makes) that the maintainer approved.
- [ ] **Step 7: Run `scripts/check.sh`, then commit** `Plan 11 close-out: benchmarks, docs, roadmap`.

The 0.6.0 release PR follows this plan's last merge, as 0.5.0's did. It is not part of this plan.

## Open questions

Each question has the plan's answer marked **Recommended**; the tasks above are written to it. Approving this plan
approves every recommended answer unless the maintainer says otherwise.

### Release and scope

- **O1. Which release?** (a) Plan 11 ships whole as **0.6.0**, after 0.5.0. **Recommended**: one release, as Plan 10
  shipped. (b) C as 0.6.0 and D as 0.7.0.

### Which executor runs what

- **O2. A driver without `with_threads`.** (a) Keeps `Plan::run`, unchanged. **Recommended**: design §2's anti-goal
  ("the default changes nothing") and the instruction-count gate hold by construction, and `Plan::run` stays the
  reference the scheduler is tested against. (b) Runs the scheduler with one worker, as design task 17's wording ("the
  sync driver runs on the `Scheduler`") reads.
- **O3. `with_threads(max_workers=1)`.** (a) Runs the scheduler on the calling thread, with `with_threads`' rules:
  per-call contexts and the hook lock. **Recommended**: §6.2 applies the context rule "whatever `max_workers` is", so
  results can't depend on the worker count. (b) The same as no `with_threads`.
- **O4. The async driver.** (a) Always steps the core scheduler, with or without `with_threads`. **Recommended**:
  §6.1's one set of rules, and blocks on the async driver need it; gated by the async benchmarks within 10%. (b) Only
  with `with_threads`, keeping `_async.Schedule` otherwise, with blocks only then.

### Determinism and events

- **O5. Event positions.** (a) `NodeStart.position` and `NodeEnd.position` are the index in `RunStart.nodes`.
  **Recommended**: a hook sorts by `(position, branch)` with nothing else. (b) The step's index in the plan, nested
  blocks flattened.
- **O6. `RunEnd.skipped` and `RunEnd.absent_errors` with several workers.** (a) Sorted in reference order at the run's
  end. **Recommended**: equal over worker counts, as design goal 3 asks of results. (b) In completion order.

### Deadlines and stopping

- **O7. `timeout=` without `with_threads`.** (a) A `TypeError` naming `with_threads`. **Recommended**: the calling
  thread can't abandon a node it is running itself, and a deadline is for the hung node (§6.5). (b) Checked between
  nodes only.
- **O8. Who runs tasks during a deadline.** (a) Only pool threads; the pool grows to `max_workers` threads for such a
  run, and the caller waits. **Recommended**: the only way the caller can raise at once. (b) The caller works too,
  and raises when its current task returns.

### Limits

- **O9. Where limits apply.** (a) Wherever nodes can overlap: always on `AsyncDriver`, and on `Driver` with
  `with_threads`. A group needs a size from `with_threads(limits=)`, and a driver that would honour an unsized group
  refuses it at build; a sequential `Driver` accepts and ignores every limit. **Recommended**: a module with
  `@limit(group="db")` still builds for a sequential driver. (b) Every driver refuses an unsized group.
- **O10. `limit(n, group=...)` together.** (a) Either or both; a node is in at most one group. **Recommended**: a cap
  per node and a shared cap are independent needs. (b) Exactly one.

### Partial results

- **O11. When `absent_on`'s annotation is checked.** (a) At build, naming the node. **Recommended**: on 3.14,
  annotations are evaluated lazily, and checking when decorating would evaluate them early, failing a forward
  reference that build resolves (Q81). (b) When decorating, as §7.5 says.
- **O12. `with_absent_on(tags=)`.** (a) A query in `NodeViews.tagged`'s form (Q141). **Recommended**: one tag syntax
  in fnloom. (b) A list of tag names.
- **O13. Interrupts listed in `absent_on`.** (a) `KeyboardInterrupt`, `SystemExit` and `asyncio.CancelledError` are a
  `TypeError` when decorating. **Recommended**: an interrupt that becomes absence would make a run unstoppable. (b)
  Allowed.

### `run_many`

- **O14. Its overrides.** (a) One mapping shared by every input set. **Recommended**: design §8's signature
  `run_many(inputs_seq, overrides=None)`. (b) A sequence aligned with the input sets.
- **O15. On the async driver.** (a) Not in Plan 11: `asyncio.gather` over `AsyncPreparedPlan.run` does it.
  **Recommended**. (b) `AsyncPreparedPlan.run_many` too.
- **O16. Without `with_threads`.** (a) The sets run one after another on the calling thread. **Recommended**: the
  same code works on either driver. (b) A `TypeError`.
- **O17. Its failure.** (a) Every set's run stops; the lowest-indexed failing set seen is raised, noted with its
  index. **Recommended**: §4.5's rule, one level up. (b) Run every set and raise an `ExceptionGroup`.
- **O18. Its hooks.** (a) Each set is its own run, with its own hook lock, so two sets' hook calls may overlap, as two
  threads' runs do today. **Recommended**. (b) One hook lock for the whole call.

### Benchmarks and CI

- **O19. The parallel targets.** (a) The thresholds of [Benchmark targets](#benchmark-targets): under 10 µs per
  trivial branch with threads, at least 6× on sleeping branches, at least 5× on CPU-bound ones on 3.14t, at most 2×
  `Plan::run`'s core instructions with one worker, async within 10%, and faster than Hamilton's
  `MultiThreadingExecutor(8)` on every row. **Recommended**. (b) Report only, no thresholds.
- **O20. CI for threads.** (a) The `stress` job on Ubuntu for both builds, 200 rounds and a hypothesis profile of 500
  examples; `benchmarks-smoke` on 3.14t too; no ThreadSanitizer job. **Recommended**: a TSan job needs a TSan build of
  CPython, and the core and binding have no `unsafe`. (b) The same, plus a TSan job on a self-built interpreter.
- **O21. Watchdogs.** (a) `faulthandler.dump_traceback_later` from `tests/conftest.py`, and `timeout-minutes: 60` on
  the `check` job. **Recommended**: no new dev dependency. (b) Add `pytest-timeout`.

### Threads and pools

- **O22. Signal checks.** (a) Every 50 ms while the caller waits. **Recommended**: `Ctrl-C` feels immediate at a cost
  of 20 wake-ups a second. (b) 10 ms. (c) 200 ms.
- **O23. A pool's lifetime.** (a) Its threads live until the driver's `Threads` is collected, or interpreter exit,
  with no public `close()`. **Recommended**: no lifecycle for users to get wrong. (b) An idle timeout. (c) A public
  `Driver.close()`.
- **O24. A warning on the GIL build.** (a) None: `with_threads` is correct on 3.14, and the docs say pure Python nodes
  take turns there. **Recommended**. (b) A `RuntimeWarning` when `with_threads` is used with the GIL enabled.
- **O25. Copies, rebuilds and unpickled drivers.** (a) Each driver object gets its own pool and limit counters.
  **Recommended**: limits are a driver's (§6.6), and sharing across copies would make a copy's runs throttle another's
  silently. (b) A copy shares its original's.
