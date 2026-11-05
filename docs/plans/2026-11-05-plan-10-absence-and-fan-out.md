# fnloom Plan 10: typed absence and sequential fan-out

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan PR by PR, task by task, with superpowers:test-driven-development
> inside each task and an independent reviewer subagent per PR. Steps use checkbox (`- [ ]`) syntax for tracking.

**Status:** drafted 2026-11-05 for the maintainer's approval. Nothing here is built.

**Goal:** milestones A and B of the [dynamic execution design](../design/2026-10-28-dynamic-execution-design.md).

- **A, typed absence:** a node annotated `-> T | Absent` may return `fnloom.ABSENT`. Each consumer takes it, uses its
  default, or is skipped, decided at build by its own signature. Absences are cached (§7.1 to §7.4).
- **B, blocks, sequential:** `Parallelizable[T]` and `Collect[T]`. A block is compiled once into a reusable plan and
  runs once per item, in item order. Blocks nest, work across namespaces and `bind=`, fail with the branch named, key
  each branch by content, and are hooked, drawn and exported (§4, §5).

Milestones C to E are Plan 11: the `Scheduler`, `with_threads`, `run_many`, `absent_on`, partial results and their
close-out. This plan builds none of them. Where B has to leave a seam for them, the task says so.

**Motivating use, kept generic:** a graph lists items, then calls an endpoint once per picked item (fan-out). When the
request names one item, the listing declines and is never fetched (absence). Task 16 writes this as the end-to-end
test and the docs example, over a neutral catalog domain.

**Architecture:**

- **Absence lives in the core.** `fnloom-core` gains a third dependency kind (`Admits`) and computes "may be absent"
  at build. `Plan::run` carries an absent flag in its slot table and skips by §7.2's three rules. The binding tells the
  core what is absent through one new `Evaluate` method with a default, so an embedder that never declines changes
  nothing.
- **Blocks live in the core too.** `Graph::build` finds blocks iteratively. `Graph::prepare` compiles each reachable
  block into a `SubPlan`. `Plan::run` runs branches in reference order (§4.3), using an explicit stack and no
  recursion. Running blocks needs a `Blocks` trait that the evaluator opts into. The binding implements it with Python
  iterators and lists.
- **The front end is Python**, as for every earlier plan: annotations, checks, views, hooks, export, drawing and
  caching policy. Per-branch cache keys reuse the binding's key machinery (`keys.rs`) through one more `Evaluate`
  seam, `lookup`.
- **Default path untouched.** A graph with no expand node and no node that may be absent runs today's steps. The only
  addition per computed node is one identity test, the always-on return check (§7.1).

**Tech Stack:** as Plans 1 to 9 and 12. Python 3.14 and 3.14t, the Rust core through maturin and PyO3, ty strict,
ruff ALL, Clippy pedantic, rumdl, pytest with hypothesis, and `apache-hamilton==1.90.0` as the oracle. Build with
`maturin develop --features test-hooks`, test with `UV_NO_SYNC=1`, and gate with `scripts/check.sh`.

**Spec:** `docs/design/2026-10-28-dynamic-execution-design.md`. Its sections:

- §4 (fan-out and fan-in), §5 (the plan in the core), §7.1 to §7.4 (absence);
- §10, rows Q118 to Q138 and the amended rows;
- §12.1 and §12.2, the tests and benchmarks that apply without threads;
- §12.3, tasks 1 to 15 and Plan 10's share of task 27;
- §13, the calls.

Also read `docs/plans/2026-10-28-roadmap-ahead-of-hamilton.md` and
[Plan 12](2026-10-29-plan-12-api-parity-plus.md), whose PRs land first.

**Order and release (orchestrator, 2026-11-05):**

- Plan 10's first PR starts only after Plan 12's PRs A to G have merged to `main`. Those PRs are tags, drawing `Style`,
  `PrintNodes` and `after_build`, step namespaces, driver cache lists with per-field cache, `config.matches`,
  `replace=` with namespace config, and `frame()`.
- Plan 10 ships as **0.5.0**, the release after Plan 12's 0.4.0 (maintainer, 2026-11-03, design §12.3). The design's
  "0.3.0" is stale: 0.3.0 shipped without it. The PR that adds this plan links it from the design's §12.3 and change
  log, and from the roadmap's Plan 10 item.
- Before each PR, re-read the files it touches on `main`. Plan 12 may have moved a name this plan cites. Where it did,
  follow `main`, and say so in the PR body.

## Global Constraints

- Every Global Constraint of Plans 1 to 9 applies unchanged:
    - Python 3.14 and 3.14t;
    - ty strict, ruff ALL, Clippy pedantic, rumdl, and the crate's MSRV;
    - one commit per task, `scripts/check.sh` green on every commit;
    - author email `4119211+kelsayed17@users.noreply.github.com`, with the trailer
      `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`;
    - branches merge `main` in, and nothing is rebased or force-pushed.
- `fnloom-core` stays dependency-free, `#![forbid(unsafe_code)]` and generic over `P` and `V`. `fnloom-py` keeps
  `#![deny(unsafe_code)]`.
- No recursion over graph depth or nesting depth, in Rust or Python. A 50,000-node chain inside a block and a
  1,000-deep nesting build, plan and run (§12.1, "Core").
- Determinism: no result, `Collect` order, cache key, drawing, export or error text may depend on `PYTHONHASHSEED`.
  Every error lists all culprits, sorted.
- Register rows come first. Each row gets its row text before its test: a `keeps("Qn")` differential test for KEEP, a
  `deliberate("Qn")` test for the rest, both in `tests/differential/`.
- No new runtime dependency. No Hamilton code copied. Examples use neutral domains (catalogs, regions, sales).
- The default path is untouched. `benches.run` stays within noise, and the instruction-count gate stays green, on a
  graph with no expand node and no node that may be absent.
- Each PR adds a CHANGELOG `[Unreleased]` entry, and rewrites the `from-hamilton.md` rows it changes.
- Every new `Builder` or driver setting survives pickling, `copy`, `rebuild()` and `build_async`, as Plans 5 to 9
  settled. Plan 10 adds no new builder setting, so this holds by construction.

## Calls this plan makes

These are the places where the design leaves the shape to the builder, or where this plan departs from its wording.
Change any of them before the build starts.

1. **`Evaluate::is_absent(&self, &V) -> bool`** (default `false`). The design's "`evaluate` returns an outcome" would
   change every embedder's signature, against §5's "an embedder that never returns `Absent` sees no change".
    - A skipped node's slot holds a clone of the absent value that caused the skip. The core never has to make an
      absent value of its own.
2. **Blocks through an opt-in trait.** `Evaluate::blocks(&mut self) -> Option<&mut dyn Blocks<..>>` (default `None`).
   It replaces §5's `expand(...) -> impl Iterator`, because an iterator that borrows the evaluator can't interleave
   with the branches it feeds.
    - A plan with blocks, run by an evaluator that returns `None`, fails with `RunError::BrokenPlan`. It never yields
      empty blocks silently.
3. **`InputSpec.collect: Option<Collect>`** instead of `Dependency::Collect`. Collecting composes with the three
   absence rules (§7.4: `Collect[T] | Absent`, a default, or skip), so it is a second axis, not a fourth kind.
   `Collect::Present` drops absent branches, and `Collect::All` keeps their places (§7.3).
4. **Block membership by reading.** A node that reads a block node joins the block (§4.2's definition). §4.2's first
   refusal, "a node outside a block that reads a block node", therefore surfaces as one of two errors:
    - `CollectsOwnBlock`, for a node that also collects the block (Q124);
    - `OutputsInBlocks`, at prepare.

    The one remaining case gets a Python-side refusal in Task 14: a `bind=` to another copy's block node.
5. **Q128 is written in Plan 10**, for the sequential behaviour:
    - the same exception class;
    - a note naming the branch;
    - the iterator closed;
    - finished branches dropped.

    Plan 11's task 21 amends it for parallel stops and deadlines. The design lists Q128 under task 21 only.
6. **Q132: `build_async()` refuses a graph with an expand node** until Plan 11's task 23, with a `TypeError` naming
   each expand node. The row goes in as FIX *Planned*. Absence works on the async driver now (Task 5).
7. **Late keys.** Nodes downstream of an expand get their cache keys during the run, through `Evaluate::lookup`.
    - Inside a branch, the core asks in reverse need order, so a hit at a block's exit skips its upstream in that
      branch (§4.6).
    - Outer nodes after a `Collect` are asked in plan order. A hit there skips only that node.
    - Plan 11's `Scheduler` keeps the same seam.
8. **`bool(ABSENT)` raises `TypeError`** saying to compare with `is ABSENT`, so an absence is never read as a falsy
   value. `ABSENT` pickles, copies and deep-copies to itself.
9. **An absence is stored as an entry marker, not as a format's value.** `DiskCache` writes a header flag with an
   empty payload, in every format, so no JSON value can collide with it. `MemoryCache` and a custom `CacheStore` hold
   the `ABSENT` object itself.
10. **`RunEnd.skipped: tuple[tuple[str, tuple[int, ...]], ...]`** holds `(node, branch)` pairs in the order they were
    skipped, with `()` outside blocks. It takes this final shape in Task 3, so B changes no type.
11. **`fnloom.export/2` arrives in Task 3**, for `may_be_absent`. Task 13 adds `kind` values and `block` to it. A graph
    with neither still exports `fnloom.export/1`, byte for byte.
12. **The builtin SVG engine** (if Plan 12 PR B ships one) shows a block as a `for each item of <expand>` line on each
    member and labelled `expand` and `collect` edges, not as a box. Its layered layout doesn't keep a block's members
    contiguous. DOT and Mermaid draw real clusters.
13. **`frame()` refuses an `ABSENT` output** with a `ValueError` naming it, since a column can't be absent. Every other
    result builder receives `ABSENT` as a value (Q19).
14. **`NodeView.kind`** is `"input"`, `"function"`, `"expand"` or `"collect"`. A node that both collects and expands
    (Q136) is `"expand"`, and `Param.collect` marks the parameters that collect.
15. **The after-expand event is `fnloom.ExpandEnd`**, with fields `run_id`, `node`, `branch` and `items`.
16. **`keys.rs`'s constant `ABSENT`** (an optional input not supplied) is renamed `NOT_SUPPLIED`, so the word "absent"
    means one thing.
17. **`Step.block: Option<BlockId>` instead of §5's action set** (`Expand`, `Compute`, `Collect`). An expand step keeps
    today's `Compute` or `UseOverride` action and names its block, since an overridden expand still runs its block
    over the override's items. A collect is an ordinary `Compute` step whose collect edges `gather` from the block.
18. **`PlanError::OutputsInBlocks(Vec<OutputInBlock>)` and `PlanError::OverridesInBlocks(Vec<String>)`** instead of
    §4.3's single `OutputInBlock`, so one error lists every culprit, as `MissingInputs` does.
19. **`Parallelizable[T | Absent]` is refused at build**, naming the node: an item can't be absent, since an item is
    an input to its block (§7.3). An expand admits absence as a whole with `Parallelizable[T] | Absent`.
20. **`bool(ABSENT)` raising (call 8) is an addition beyond the design**, listed for the maintainer to confirm or drop.
    Task 3 tests that fnloom's own value paths never take a value's truth value.
21. **The run loop is chosen per plan.** A plan with no block and no node that may be absent runs today's
    `execute` loop unchanged (no `Slot` wrapper, no rule checks), so the instruction-count gate sees no change. The
    absence- and block-aware loop runs only plans that need it.

## Register rows

The next free rows in [the register](../quirk-register.md) are Q118 to Q138, reserved by the design. Plan 10 writes
these, each in its task:

| Row | Task | Disposition (design §10) | Note |
|---|---|---|---|
| Q118 | 8 | FIX | No flag |
| Q119 | 8 | KEEP | Against Hamilton's synchronous and thread executors; Plan 11 reruns it with workers |
| Q120 | 8 | KEEP | |
| Q121 | 8 | KEEP | |
| Q122 | 8 | FIX | Rust in Task 7; Plan 11 reruns it in parallel |
| Q123 | 8 | FIX | Rust in Task 6; Plan 11 reruns it in parallel |
| Q124 | 6 | REFUSE | Row text in Task 6; Python differential test in Task 8 |
| Q125 | 9 | FIX, REFUSE | |
| Q126 | 9 | REFUSE | |
| Q127 | 8 | KEEP (edges), FIX (items, typing) | |
| Q128 | 10 | KEEP | Sequential part (call 5); Plan 11 amends |
| Q129 | 12 | FIX | |
| Q130 | 10 | DROP, FIX | |
| Q131 | 13 | FIX | |
| Q132 | 8 | FIX *Planned* | Refused on `build_async` until Plan 11 (call 6) |
| Q135 | 2 | FIX | |
| Q136 | 6 | FIX | Row text in Task 6; Python differential test in Task 8 |
| Q137 | 8 | DROP | Worded with `with_threads(...)` marked *Planned* (Plan 11) |
| Q138 | 8 | KEEP | |

Plan 11's rows are Q133 (`absent_on`, `GracefulErrorAdapter`) and Q134 (executors). Q157 stays DEFER until Plan 11.
Task 16 narrows its text to `GracefulErrorAdapter`, since Q130 answers its task-hook half.

**Amended rows:**

| Row | Task | What changes |
|---|---|---|
| Q19 | 3 | The result dict and result builders may hold `ABSENT` |
| Q84 | 3, 10 | Task 3: `RunEnd.skipped`. Task 10: `NodeStart.branch`, `NodeEnd.branch`, `Hook.after_expand` (a sixth method, after Plan 12's `after_build`) |
| Q95 | 3, 13 | `fnloom.export/2` for graphs with absence or blocks |
| Q111 | 10 | Wrappers are called once per branch and read `current_branch()` |

Q65, Q66, Q85 and Q86 are amended by Plan 11.

## Where Plan 12 already touched these surfaces

| Plan 12 PR | Plan 10 task | What Plan 10 must do on top |
|---|---|---|
| A, tags (Rust `TagValue`, the node tuple's tags) | 1, 2, 8 | Extend the node tuple PR A leaves. Don't re-shape tags. |
| B, `Style`, display options, `DrawnNode.view`, SVG engine | 3, 13 | Draw the may-be-absent mark and the clusters through PR B's style precedence, `kind` styles first. For the builtin engine, see call 12. |
| C, `PrintNodes`, `Hook.after_build`, `BuildEnd` | 3, 10 | `PrintNodes._started` is keyed by `(run_id, node)`: re-key it by `(run_id, node, branch)`, and print the branch for block nodes. `BuildEnd.nodes` views carry `may_be_absent`, `block` and `kind`. `_hooks.checked` learns `after_expand`. |
| D, step `namespace=` and `on_output=` | 14 | Step nodes of a block node are block nodes, under any step namespace. Extend the stacking test. |
| E, `with_cache(recompute=, disable=, default=, default_behavior=)`, per-field policies, `explain_cache` sources | 4, 11, 12 | A driver list naming an expand node decides whether its items are stored (§4.6). `explain_cache`'s block entry gives the source as PR E does. |
| F, `config.matches`, namespace `config=`, `replace=`, `without_edge_type_checks(*consumers)` | 8, 14 | The expand-to-consumer and block-to-`Collect` edges obey per-consumer edge-check opt-outs. `replace=` may replace an expand node. Namespace config selects variants inside blocks. |
| G, `frame()` | 3 | Refuse an `ABSENT` output (call 13). |

Both plans touch hooks, export, cache keys and drawing. Plan 10's hooks, export and key changes add fields and
values. None renames or removes what Plan 12 added.

## File Structure

```text
crates/fnloom-core/src/
  node.rs     Dependency::Admits, Collect, InputSpec.collect, NodeSpec.{declines,expands},
              NodeKind::Expand, Edge.collect, Node.{declines,may_be_absent,block}
  graph.rs    may-be-absent at build; calls block discovery; Graph::block_path
  block.rs    NEW  block discovery and the §4.2 build errors, iterative
  error.rs    BuildError::{CollectOutsideBlock,CollectOfExpand,CollectsOwnBlock,UnrelatedBlocks},
              PlanError::{OutputsInBlocks,OverridesInBlocks}
  plan.rs     BlockId, SubPlan, Step.block, Plan::blocks, sub-plan compilation and releases
  run.rs      Slot<V> with the absent flag, skip rules, the new Evaluate and Observer methods, Blocks
  branch.rs   NEW  running sub-plans in reference order on an explicit stack
  testing.rs  spec helpers for kinds, collect edges and declining nodes
crates/fnloom-core/benches/core.rs   fan-out rows, a skip chain
crates/fnloom-py/src/
  absent.rs   NEW  the `Absent` class and `ABSENT` singleton
  graph.rs    the widened node tuple; Graph.may_be_absent(), Graph.blocks()
  plan.rs     Caller: the return check, is_absent, evaluate_in (current_branch), Blocks, lookup;
              Plan.blocks()
  observer.rs branch-aware pre_node and post_node, post_expand, skipped
  keys.rs     NOT_SUPPLIED; optional reads; BlockKeys and per-run BranchKeys
  fingerprint.rs  ABSENT's fixed fingerprint
  errors.rs   the branch note on a node's error
python/fnloom/
  _absence.py NEW  admits_absent, without_absent, absence of a parameter, refusals, async rules
  _blocks.py  NEW  Parallelizable, Collect, annotation readers, the checked item iterator,
                   current_branch, refusals
  _scan.py    core_inputs with kinds and collect modes; generator expand nodes allowed
  _view.py    NodeView.{may_be_absent,block,kind}; Param.{admits_absent,collect}
  _types.py   edge checks strip Absent, and read Parallelizable and Collect
  _hooks.py   RunEnd.skipped, NodeStart/NodeEnd.branch, ExpandEnd, Hook.after_expand, PrintNodes per branch
  _checks.py, driver.py (_output_checked), _wrappers.py, _extract.py   pass ABSENT through
  _caching.py late keys, branch hits, stored items, collect versions, explain_cache
  _stores.py  the absent entry marker
  _async.py   absence rules; refusal of blocks
  _export.py  export/2; diff reads both
  _drawing.py the may-be-absent mark, clusters, expand and collect edges
  driver.py   input and override refusals, OutputsInBlocks hints, validate, build_async refusal
  __init__.py ABSENT, Absent, Parallelizable, Collect, ExpandEnd, current_branch
tests/
  test_absence.py  test_blocks.py  test_blocks_caching.py  test_fan_out_example.py   NEW
  test_caching.py  test_async.py  test_hooks.py  test_export.py  test_drawing.py  test_stacking.py
  test_namespaces.py  test_threads.py  test_pickling.py  test_public_api.py
  differential/test_deliberate_differences.py  differential/test_semantics.py
benchmarks/test_blocks.py (CodSpeed)   benches/run.py (fan-out rows)   benches/generate.py (block shapes)
docs/writing-nodes.md (Declining, Fan-out and fan-in), running.md, hooks.md, caching.md, drawing.md,
  from-hamilton.md, quirk-register.md, benchmarks.md, rust-core.md, index.md, README.md, CHANGELOG.md,
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
| **A1** Absence: core and front end | 1, 2 | Plan 12 PRs A to G | none |
| **A2** Absence at the edges of a run, and on the async driver | 3, 5 | A1 | A3, B1 |
| **A3** Caching declared absences | 4 | A1 | A2, B1 |
| **B1** Blocks in the core (Rust only) | 6, 7 | A1 | A2, A3 |
| **B2** Blocks in the front end: types, overrides, hooks | 8, 9, 10 | B1, A2 | none |
| **B3** Caching blocks | 11, 12 | B2, A3 | B4 |
| **B4** Drawing and export of blocks | 13 | B2 | B3, B5 |
| **B5** Namespaces, decorators and absence in blocks | 14, 15 | B3 | B4 |
| **Z** Close-out | 16 | every other PR | none |

B1 runs in parallel with A2 and A3 because they touch disjoint code. A2 is Python, except the binding's observer.
A3 is the store, the fingerprints and `keys.rs`. B1 is `fnloom-core` plus the one-line defaults the binding needs to
compile. B1 waits for A1 because both rewrite `run.rs`'s slot table.

**Textual collisions between parallel PRs**, each resolved by merging `main` in, never by rebasing:

- **A2 and A3:** `python/fnloom/_core.pyi`. A2 adds `ObserverBase.skipped`, and A3 changes `KeyPlan`'s node entry.
  The two edits are in different classes. A3 puts its tests in `tests/test_caching.py` and `tests/test_stores.py`,
  not in `tests/test_absence.py`, which A2 parametrizes. Both append to `CHANGELOG.md`.
- **A2 or A3 and B1:** none in code. `crates/fnloom-py/src/graph.rs` gets B1's defaults (`expands: false`, `collect:
  None`) in the node-spec builder, and A2 doesn't touch that file.
- **B3 and B4:** `tests/differential/test_deliberate_differences.py` (Q129 and Q131 append separate test functions),
  `docs/quirk-register.md` (separate rows, plus Q95's amendment in B4 only), and `CHANGELOG.md`.
- **B4 and B5:** `docs/writing-nodes.md` (B5 only; B4 writes `docs/drawing.md`) and `CHANGELOG.md`.

## Review Focus

1. **A generator expand whose branch 2 fails.** The generator's `finally` runs exactly once, and nothing of branches
   0 and 1 is returned. The error is the node's own class, with the note `while computing 'details' for branch (2,)
   of 'picked'`. (Task 10, `test_a_failing_branch_closes_the_generator_once`)
2. **Items reordered between two cached runs.** Every branch node hits, since keys are content-addressed. The
   order-dependent `Collect` node misses and recomputes in the new order. (Task 12,
   `test_reordered_items_hit_branches_and_miss_the_collect`)
3. **Unfingerprintable items under a `recompute` expand.** Each such branch is uncacheable and nothing is stored for
   it. Results are still correct, with no warning per branch, and the next run computes again, never hitting by
   index. (Task 11, `test_unfingerprintable_items_without_stored_items_are_uncacheable`)
4. **One source read by two parameters of one node,** one admitting `Absent` and one defaulted, when the source may
   be absent. The build refuses, naming the node, the source and both parameters. When the source can't be absent,
   it builds. (Task 2, `test_mixed_absence_handling_of_one_source_is_refused`)
5. **`ABSENT` through every store and a pickle round trip.** It comes back as the singleton (`is ABSENT`) from
   `MemoryCache`, from `DiskCache` under both formats and from a custom store, and inside a pickled value. (Task 4,
   `test_absent_comes_back_as_the_singleton_from_every_store`)

---

## PR A1: absence in the core and the front end

### Task 1: the absent outcome in the core (design task 1)

**Files:**

- Modify: `crates/fnloom-core/src/node.rs`, `graph.rs`, `plan.rs`, `run.rs`, `testing.rs`, `examples/embed.rs` (the
  new fields only), `benches/core.rs`
- Modify: `crates/fnloom-py/src/graph.rs` (compiles with `declines: false` until Task 2)
- Test: the `#[cfg(test)]` modules of `graph.rs` and `run.rs`

**Interfaces:**

- Produces:
    - `Dependency::Admits { optional: bool }`: a parameter that receives absence as its value. `optional` says
      whether it has a default, which is all that matters when the edge reads an `Input` node, since an input is never
      absent. `plan.rs` treats `Admits { optional: false }` as `Required` and `Admits { optional: true }` as `Optional`
      wherever it decides whether an input is missing (`plan_actions`, `missing_inputs`, `passed_args`).
    - `NodeSpec.declines: bool` (its annotation admits `Absent`).
    - `Node.declines: bool` and `Node.may_be_absent: bool`.
    - `Evaluate::is_absent(&self, value: &Self::Value) -> bool { false }`, asked only of a node with `declines`, and of
      an override of a node with `may_be_absent`.
    - `Observer::skipped(&mut self, node: &str, branch: &[usize]) -> Result<(), E> { Ok(()) }`.
- Rules, in `Plan::run`, for a consumer reading an absent slot:
    - `Admits` passes the absent value.
    - `Optional` omits the argument.
    - `Required` skips the consumer. It is stored as absent with the culprit's value, fires `skipped` and no node
      event.
- An absent output is returned as its (absent) value.
- `may_be_absent` is computed in `Graph::build` in topological order. A node may be absent if it `declines`, or if it
  is a function with a `Required` edge from a node that may be absent.
- `Plan` records `needs_rules: bool`, true when a step's node may be absent. `Plan::run` keeps today's `execute` loop
  for a plan without it, and runs the absence-aware loop only for a plan with it (call 21).
- [ ] **Step 1: Write the failing Rust tests**
    - `may_be_absent_follows_required_edges_only`: chain `a(declines) -> b(Required) -> c(Optional on b) -> d(Admits
      on b)`. Assert `a` and `b` may be absent, and `c` and `d` may not.
    - `a_declined_value_skips_required_consumers_and_reaches_admitting_ones`: the evaluator returns `-1` for the
      absent marker and `is_absent(v)` is `*v == -1`. Assert the `Required` consumer never evaluates, and the
      `Admits` consumer gets `-1` as its argument.
    - `an_optional_edge_from_an_absent_node_is_omitted`: the consumer's `args` don't contain the absent node.
    - `a_skipped_node_fires_skipped_and_no_node_events`: the observer log is `pre_graph, pre_node a, post_node a,
      skipped b, post_graph`.
    - `an_absent_output_is_returned_with_its_value`.
    - `an_absent_override_skips_consumers`.
    - `is_absent_is_asked_only_of_declining_nodes`: a counting evaluator on a 200-node chain with no declining node
      counts 0 calls.
    - `a_ten_thousand_node_skip_chain_runs_without_recursion`.
    - `an_admits_edge_to_an_unsupplied_input_is_missing_unless_defaulted`: `Admits { optional: false }` on an input
      left out gives `PlanError::MissingInputs` naming it, and `Admits { optional: true }` plans it as
      `SkipOptionalInput`, omitted from the args.
    - `a_plan_with_nothing_absent_runs_the_plain_loop`: `needs_rules` is false for a graph with no declining node.
- [ ] **Step 2: Run them to see them fail.** Run `cargo test -p fnloom-core`. Expected: they don't compile (no
  `Admits`, `declines` or `is_absent`).
- [ ] **Step 3: Implement.** Make the slot table `Vec<Option<Slot<V>>>`, with `struct Slot<V> { value: V, absent:
  bool }`. `gather` builds args by the three rules and returns `Skip(culprit)` for a `Required` absent edge.
  `compute` consults `is_absent` only when `node.declines`. Update `embed.rs` and the doc comments.
- [ ] **Step 4: Run the tests to see them pass**, then `cargo bench --no-run -p fnloom-core`. Expected: PASS, and
  `benches/core.rs` compiles with a new `skip_chain_200` row.
- [ ] **Step 5: Run `scripts/check.sh`, then commit** `core: the absent outcome, Dependency::Admits and skips`.

### Task 2: `Absent`, `ABSENT` and the consumer rules in the front end (design task 2, Q135)

**Files:**

- Create: `crates/fnloom-py/src/absent.rs`, `python/fnloom/_absence.py`, `tests/test_absence.py`
- Modify: `crates/fnloom-py/src/{lib.rs,graph.rs,plan.rs}`, `python/fnloom/{_core.pyi,_scan.py,_view.py,_types.py,
  driver.py,__init__.py}`, `docs/quirk-register.md`, `tests/differential/test_deliberate_differences.py`,
  `tests/test_public_api.py`, `docs/writing-nodes.md` (a "Declining" section)

**Interfaces:**

- Consumes: Task 1's core API.
- Produces:
    - `fnloom.Absent`: a `@final` pyclass, `module = "fnloom"`, with exactly one instance, `fnloom.ABSENT`. Its
      `__repr__` is `"fnloom.ABSENT"`, `__reduce__` returns `"ABSENT"`, and `__copy__` and `__deepcopy__` return
      self. `__bool__` raises `TypeError` (call 8).
    - `_absence.admits_absent(annotation: object) -> bool`: true for `Absent`, or a union holding it at the top level,
      seen through `Annotated` and PEP 695 aliases. It never looks inside generic arguments: `list[int | Absent]`
      and `Collect[T | Absent]` don't admit absence (the second is a collect mode, read in Task 8).
    - `_absence.without_absent(annotation: object) -> object`: the annotation with `Absent` removed.
    - `_absence.dependency(param: Param) -> Literal["required", "optional", "admits", "admits_optional"]`: an
      admitting annotation gives `admits`, or `admits_optional` with a default. Otherwise it is `optional` with a
      default, and `required` without.
    - `_scan.core_inputs(params) -> list[tuple[str, Literal["required","optional","admits","admits_optional"],
      None]]`: the third element is the collect mode, always `None` until Task 8. The binding maps the kinds to
      `Dependency`.
    - **One source read by several parameters** (a `group(...)`, or `@parameterize` binding two parameters to one
      `source`) gets one merged kind. `core_inputs` merges as follows:
        - if every parameter on the source admits `Absent`, the kind is `admits`, or `admits_optional` when every one
          of them has a default;
        - otherwise it is `required` if any parameter lacks a default, and `optional` if all have one.

        A source whose parameters disagree on admitting `Absent` can only be one of these when it can't be absent.
        When it may be absent, `refuse_mixed` refuses it (Review Focus 4).
    - `_core.Graph(nodes, renames)`, where each node is `(name, inputs, tags, payload, declines: bool)`, and
      `Graph.may_be_absent() -> list[str]`, sorted.
    - `NodeView.may_be_absent: bool` and `Param.admits_absent: bool`.
- The return check, in `Caller::call` (binding): a payload that returns `ABSENT` for a node without `declines` raises
  `TypeError: node 'x' returned fnloom.ABSENT, but its return annotation does not admit fnloom.Absent`.
- [ ] **Step 1: Write the register row Q135** (design §10's text, FIX), and its `deliberate("Q135")` test.
    - Hamilton: a node returning a sentinel still runs every consumer.
    - fnloom: `listing() -> list[str] | Absent` returning `ABSENT` skips `count(listing: list[str])`, which never runs.
      `execute(["count"])` returns `{"count": ABSENT}`.
- [ ] **Step 2: Write the failing tests** in `tests/test_absence.py`:
    - `test_a_plain_consumer_of_a_declined_node_is_skipped`;
    - `test_a_consumer_admitting_absent_receives_it` (`is ABSENT`);
    - `test_a_defaulted_consumer_gets_its_default`;
    - `test_returning_absent_without_admitting_it_is_a_type_error`: the message is the one above;
    - `test_may_be_absent_is_known_at_build`: over `driver.nodes`, for a chain with each rule;
    - `test_admits_absent_reads_the_top_level_only`: `float | Absent` admits, and `list[float | Absent]` doesn't;
    - `test_absent_is_a_singleton_with_no_truth_value`: `pickle`, `copy` and `deepcopy` give `is ABSENT`; `bool(ABSENT)`
      raises `TypeError` matching `is ABSENT`;
    - `test_t_or_absent_feeds_t_on_an_edge`: `float | Absent` into `float`, into `float | Absent`, and into
      `x: float = 0.0` all build;
    - `test_core_inputs_merges_kinds_per_source`, one case per row of the merge:
        - two admitting parameters, one defaulted, give `admits`;
        - two admitting parameters, both defaulted, give `admits_optional`;
        - one defaulted parameter and one without a default give `required`;
        - two defaulted parameters give `optional`;
        - an admitting parameter with a plain defaulted one gives `optional`, built only because the source can't be
          absent.
    - `test_mixed_absence_handling_of_one_source_is_refused` (Review Focus 4): a `@parameterize` row binding
      `source("listing")` to `a: list[str] | Absent` and to `b: list[str] = []`. It raises `ValueError` naming the
      node, `'listing'`, `'a'` and `'b'` when `listing` may be absent, and builds when it can't.
    - `test_public_names`: `ABSENT` and `Absent` are in `fnloom.__all__`, and `inspect.signature` resolves.
- [ ] **Step 3: Run them to see them fail.** Run `uv run maturin develop --features test-hooks && UV_NO_SYNC=1 uv run
  pytest tests/test_absence.py tests/differential -k "absence or Q135"`. Expected: `ImportError` on `ABSENT`.
- [ ] **Step 4: Implement.**
    - The class goes in `absent.rs`, registered in `lib.rs`.
    - Re-export it from `__init__.py`.
    - `NodeDef` gets `declines` from `admits_absent(returns)` in `_scan.resolve`.
    - `_edge_mismatches` in `driver.py` compares `without_absent(producer)` with the consumer.
    - The mixed-handling refusal lives in `_absence.refuse_mixed(nodes, may_be_absent)`, called in
      `Builder._scanned` after `_graph_of`.
    - `Caller` holds the `ABSENT` pointer once per run, and compares results by identity.
- [ ] **Step 5: Run the tests to see them pass**, plus `pytest tests/test_view.py tests/test_type_checks.py`.
  Expected: PASS.
- [ ] **Step 6: Run `scripts/check.sh`, then commit** `Absent and ABSENT: a node may decline (Q135)`.

## PR A2: absence at the edges of a run, and on the async driver

### Task 3: outputs, overrides, inputs, builders, hooks, checks, steps, wrappers, export (design task 3)

**Files:**

- Modify:
    - `python/fnloom/`: `driver.py`, `_hooks.py`, `_checks.py`, `_wrappers.py`, `_extract.py`, `_export.py`,
      `_drawing.py`, and `frame()`'s module (Plan 12 PR G);
    - `crates/fnloom-py/src/observer.rs` (`ObserverBase.skipped`, forwarding the core's `Observer::skipped`) and
      `python/fnloom/_core.pyi` (its stub);
    - `docs/`: `hooks.md`, `running.md`, `drawing.md`, `quirk-register.md` (Q19, Q84, Q95 amended).
- Test: `tests/test_absence.py`, `tests/test_hooks.py`, `tests/test_export.py`, `tests/test_drawing.py`,
  `tests/test_results.py`

**Interfaces:**

- Consumes: `NodeView.may_be_absent`.
- Produces in the binding: `ObserverBase.skipped(self, node: str, *, branch: tuple[int, ...]) -> None`.
- Produces:
    - `RunEnd.skipped: tuple[tuple[str, tuple[int, ...]], ...]` (call 10).
    - `_export.FORMAT_2 = "fnloom.export/2"`: a node gains `"may_be_absent": bool`, and the document uses `/2` only
      when some node may be absent.
    - The drawing mark `"may be absent"`: dashed, in the legend.
- [ ] **Step 1: Amend Q19, Q84 and Q95** in the register, as design §10 lists them, for this task's share.
- [ ] **Step 2: Write the failing tests.**
    - `test_an_absent_output_is_absent_in_the_result_and_given_to_the_builder`.
    - `test_an_absent_input_is_refused`: `TypeError` naming the input, saying to leave it out.
    - `test_an_absent_override_is_allowed_only_for_a_node_that_may_be_absent`: otherwise a `TypeError` naming it. The
      check is always on, also under `without_override_type_checks()`.
    - `test_run_end_lists_skipped_nodes_in_order`: `(("count", ()), ("report", ()))`.
    - `test_fields_of_an_absent_producer_are_absent` (`@extract_fields` and `@extract_columns`).
    - `test_output_checks_do_not_run_on_absent`: `@check_output(range=...)` and `with_output_type_checks()`.
      `NodeEnd.checks == ()`.
    - `test_a_pipe_output_step_reads_absence_by_its_signature`.
    - `test_a_wrapper_sees_absent_from_call_and_may_replace_it`: a wrapper returning `ABSENT` for a node that doesn't
      decline gets the return check's `TypeError`.
    - `test_frame_refuses_an_absent_output`.
    - `test_print_nodes_prints_nothing_for_a_skipped_node`.
    - `test_export_2_only_when_a_node_may_be_absent`: a graph without such a node exports byte-identically to `main`'s
      golden file.
    - `test_diff_reads_export_1_and_2`.
    - `test_the_may_be_absent_mark_is_drawn_and_hash_seed_stable` (three `PYTHONHASHSEED` values).
    - `test_fnloom_never_takes_absents_truth_value` (call 20). Run `ABSENT` as an output, a node input and a stored
      value through these paths, each of which must neither raise nor test truth:
        - `PrintNodes` at verbosity 2 and `LogFailures`;
        - `MemoryCache` and `DiskCache`;
        - the default dict result, a result builder and `frame()`'s refusal;
        - `Driver.export` of the run, and `draw_execution`.
- [ ] **Step 3: Run them to see them fail.** Expected: FAIL (`RunEnd` has no `skipped`, and the input isn't refused).
- [ ] **Step 4: Implement.**
    - `Dispatch.skipped(node, branch)` gathers the pairs, and `_Forward.skipped` forwards them.
    - Each payload wrapper returns `ABSENT` untouched before any check: `_output_checked`'s `checked`,
      `_checks.wrapped` and the extractors.
    - Input and override refusals go in `PreparedPlan._checked`.
- [ ] **Step 5: Run the tests to see them pass**, plus `pytest tests/test_hooks.py tests/test_export.py
  tests/test_drawing.py`. Expected: PASS.
- [ ] **Step 6: Run `scripts/check.sh`, then commit** `Absence at the edges of a run (amends Q19, Q84, Q95)`.

### Task 5: absence on the async driver (design task 5)

**Files:**

- Modify: `python/fnloom/_async.py`, `python/fnloom/_absence.py`
- Test: `tests/test_absence.py`, `tests/test_async.py`

**Interfaces:**

- Consumes: `NodeDef.declines`, `_absence.dependency`.
- Produces: `_absence.Rules`, a frozen table built once per `AsyncDriver`. For each node it holds `declines` and,
  for each source it reads, a kind. `Rules.arguments(node, values) -> dict[str, object] | Skip` applies §7.2.
  `_async._Run` uses it in `_started` and `_store`.
- `_async.Schedule` maps `admits` to required and `admits_optional` to optional when it decides which inputs are
  missing, as Task 1 does in `plan.rs`.
- The return check, on the async path: the binding's `Caller` doesn't run async nodes, so `_Run._store` (and
  `_inline` for a sync node run on the loop) applies it. `_absence.returned(node, value) -> object` raises the same
  `TypeError` as the binding when `value is ABSENT` and the node doesn't decline.
- [ ] **Step 1: Write the failing tests.** Parametrize every test in `tests/test_absence.py` over `driver_kind in
  ("sync", "async")`, with a `run(driver_kind, ...)` helper in `tests/helpers.py`. Add
  `test_async_skips_fire_no_node_events_and_list_in_run_end`,
  `test_async_return_check_on_async_and_inline_sync_nodes` and
  `test_async_admits_input_left_out_is_missing_unless_defaulted`.
- [ ] **Step 2: Run them to see them fail.** Expected: FAIL, async only.
- [ ] **Step 3: Implement `Rules`** and use it in `_Run`. A skipped node is stored as absent and completes its
  dependents' counts without being scheduled.
- [ ] **Step 4: Run the tests to see them pass**, plus `pytest tests/test_async.py tests/test_hooks_async.py`.
- [ ] **Step 5: Run `scripts/check.sh`, then commit** `Absence on the async driver`.

## PR A3: caching declared absences

### Task 4: the stored marker, ABSENT's fingerprint and "not supplied" (design task 4)

**Files:**

- Modify:
    - `crates/fnloom-py/src/`: `fingerprint.rs`, `keys.rs`, `store.rs`;
    - `python/fnloom/`: `_stores.py`, `_caching.py`, `_core.pyi`;
    - `docs/caching.md`.
- Test: `tests/test_caching.py` and `tests/test_stores.py` only, so A2's edits to `tests/test_absence.py` don't
  collide

**Interfaces:**

- Produces:
    - `fingerprint(ABSENT)` is a fixed 32-byte digest, distinct from `fingerprint(None)`.
    - `keys.rs`: `NOT_SUPPLIED` (renamed, call 16).
    - `KeyPlan`'s node entry gains a sixth element `optional: list[str]`, the reads by an `Optional` edge. In
      `node_key`, an optional read whose version equals ABSENT's fingerprint is hashed as `NOT_SUPPLIED`.
    - `DiskCache`: a header flag `absent` with an empty payload, written for any format and read back as `ABSENT`.
- A declared absence is stored under the node's key like any value, and a hit replays it as an override, so its
  consumers skip without running.
- [ ] **Step 1: Write the failing tests.**
    - `test_a_declared_absence_is_stored_and_replayed`: the second run doesn't call `listing`, and `count` is still
      skipped.
    - `test_absent_comes_back_as_the_singleton_from_every_store` (Review Focus 5): `MemoryCache`, `DiskCache` with
      `format="pickle"` and `"json"`, a custom store, and a pickled `[ABSENT]`.
    - `test_absent_fingerprint_is_fixed_and_not_none`.
    - `test_an_absent_override_read_optionally_keys_as_not_supplied`: the consumer's key equals its key with the
      optional input left out.
    - `test_a_skipped_node_is_never_stored`.
- [ ] **Step 2: Run them to see them fail.**
- [ ] **Step 3: Implement**, rename the constant, and update the `KeyPlan` constructor in `_caching.CacheRun`.
- [ ] **Step 4: Run the tests to see them pass**, plus `pytest tests/test_caching.py tests/test_caching_property.py
  tests/test_stores.py`.
- [ ] **Step 5: Run `scripts/check.sh`, then commit** `Caching declared absences`.

## PR B1: blocks in the core (Rust only)

### Task 6: `Expand`, `Collect`, block discovery and the build errors (design task 6)

**Files:**

- Create: `crates/fnloom-core/src/block.rs`
- Modify:
    - `crates/fnloom-core/src/`: `node.rs`, `graph.rs`, `error.rs`, `lib.rs`, `testing.rs`;
    - `crates/fnloom-core/src/plan.rs`: `action_for` matches `NodeKind` exhaustively, so `Expand` plans as `Compute`;
    - `crates/fnloom-py/src/graph.rs`: passes `expands: false` and `collect: None` until Task 8;
    - `docs/quirk-register.md`: the row text of Q124 and Q136, from design §10.
- Test: `block.rs`'s test module

**Interfaces:**

- Produces:
    - `NodeKind::Expand(P)`, chosen by `NodeSpec.expands: bool`. `Node::payload()` returns the expand's payload as it
      does a function's, so `run.rs`'s `compute` and the binding's `Graph.__traverse__` (which visits each payload)
      need no change.
    - `pub enum Collect { Present, All }`, with `InputSpec.collect: Option<Collect>` and `Edge.collect:
      Option<Collect>`.
    - `Node.block: Option<NodeId>`, the innermost enclosing expand.
    - `Graph::block_path(&self, id: NodeId) -> &[NodeId]`: enclosing expands, outermost first.
    - `BuildError` gains:
        - `CollectOutsideBlock { consumer, source }`;
        - `CollectOfExpand { consumer, expand }`;
        - `CollectsOwnBlock { consumer, expand, path: Vec<String> }`, where `path` runs from the expand to the consumer
          without crossing a collect;
        - `UnrelatedBlocks { consumer, first, second }`, the two expands sorted.
- Discovery, in topological order with no recursion. Each node's block path is the longest of the contributions of
  its in-edges:
    - A plain edge from `m` contributes `path(m)`, plus `m` itself when `m` is an expand.
    - A collect edge from `m` contributes `path(m)` without its last element. It fails as `CollectOutsideBlock` when
      `path(m)` is empty, and as `CollectOfExpand` when `m` is an expand.
    - Contributions must be prefixes of one another, or the node fails as `UnrelatedBlocks`.
    - The node fails as `CollectsOwnBlock` when its final path extends the full `path(m)` of one of its collect edges.
- **A block that reads its own collect through an outer node.** Suppose a block member reads an outer node `Y`, and
  `Y` reads, possibly through other outer nodes, a collect `c` of the same block. Path discovery passes this graph,
  but Task 7's contracted order would cycle: the expand waits for `Y`, `Y` waits for `c`, and `c` waits for the
  expand. So after discovery, `Graph::build` checks every block, at every nesting level, for a cycle in its
  contracted graph. It uses the iterative colour walk of `find_cycle`, run over the contracted edges. A cycle fails
  as `CollectsOwnBlock`, whose `path` goes through the outer nodes: `'picked' -> 'details' -> 'y' -> 'summary' ->
  'picked'`.
- `may_be_absent` through collects: a collect edge propagates absence only when the collected block's expand may be
  absent, and only by its `dependency` (§7.4).
- An edge from an expand into its own block passes no absence on. Its consumers read items, and an item is never
  absent. An absent expand makes the block run no branches, and the collects take over (§7.4).
- Display texts, quoted the way `quoted` quotes:
    - `'total' reads 'prices' both through Collect and directly, so it would be inside the block of 'tickers' it
      collects: 'tickers' -> 'prices' -> 'total'`;
    - `'pair' reads blocks of 'left' and 'right', which are unrelated; collect both in one node to pair them`.
- [ ] **Step 1: Write the failing tests.**
    - `a_block_holds_every_node_reachable_without_a_collect`;
    - `shared_outer_nodes_stay_outside`;
    - `two_collects_of_one_block_build` (Q123);
    - `reading_a_block_node_and_its_collect_is_refused_with_the_path` (Q124);
    - `collect_outside_a_block_is_refused`;
    - `collect_of_an_expand_is_refused`;
    - `two_unrelated_blocks_are_refused`;
    - `a_node_may_collect_one_block_and_expand_another` (Q136): its path is `[]` and its own block is new;
    - `nested_blocks_record_their_path`;
    - `a_block_reading_its_own_collect_through_an_outer_node_is_refused`;
    - `a_nested_block_reading_its_own_collect_through_an_outer_node_is_refused`: the same shape inside an outer block;
    - `may_be_absent_crosses_a_collect_only_from_an_absent_expand`;
    - `an_expand_that_may_be_absent_does_not_make_its_block_nodes_absent`;
    - `discovery_has_no_recursion`: a 50,000-node chain inside one block, and 1,000 nested blocks.
- [ ] **Step 2: Write the register rows** Q124 and Q136, in design §10's words. Their differential tests come in
  Task 8, the first task that can build a block from Python.
- [ ] **Step 3: Run the tests to see them fail.** Run `cargo test -p fnloom-core block`.
- [ ] **Step 4: Implement `block.rs`**, with `pub(crate) fn discover<P>(nodes: &[Node<P>], order: &[NodeId]) ->
  Result<Vec<Option<NodeId>>, BuildError>`. `Graph` stores the paths as one flat `Vec<NodeId>`, with offsets, so
  `block_path` borrows.
- [ ] **Step 5: Run the tests to see them pass.**
- [ ] **Step 6: Run `scripts/check.sh`, then commit** `core: Expand and Collect, block discovery (Q123, Q124, Q136)`.

### Task 7: sub-plans in `prepare`, and `Plan::run` over blocks (design task 7)

**Files:**

- Create: `crates/fnloom-core/src/branch.rs`
- Modify:
    - `crates/fnloom-core/src/`: `plan.rs`, `run.rs`, `error.rs`;
    - `crates/fnloom-core/benches/core.rs`, `docs/rust-core.md`;
    - `crates/fnloom-py/src/errors.rs`: `run_error` destructures `RunError::Node` without `..`, so it must name the new
      fields to compile. Here it ignores them, and Task 10 adds the note.
- Test: the test modules of `plan.rs` and `branch.rs`

**Interfaces:**

- Consumes: Task 6's blocks, and Task 1's slots.
- Produces in `plan.rs`:
    - `pub struct BlockId(pub u32)`.
    - `pub struct SubPlan { pub expand: NodeId, pub steps: Vec<Step>, pub collected: Vec<NodeId>, pub reads:
      Vec<NodeId> }`. `reads` are the outer values the block reads, sorted. Plan 11's order keys are positions in
      `steps`.
    - `Step.block: Option<BlockId>`, set on an expand step, computed or overridden.
    - `Plan::blocks(&self) -> &[SubPlan]`.
    - **Step order over contracted blocks.** `topological_order` orders a graph in which each reached block is
      contracted into its expand step:
        - the expand step waits for its own inputs and for every node in its block's `reads`;
        - a collect step waits for the expand of each block it collects, in place of the block nodes its collect
          edges read;
        - block members are left out of the outer order, and their in-edges and out-edges are rewritten as above.

        Waiting counts are therefore the rewritten in-degree, not `node.inputs.len()` (which today would leave a
        collect waiting forever). Inside a sub-plan the same rule contracts each nested block, and the sub-plan's own
        expand counts as already done. Ties are broken by id, as today.
    - `PlanError::OutputsInBlocks(Vec<OutputInBlock>)`, with `OutputInBlock { node, expand, collects: Vec<String> }`,
      and `PlanError::OverridesInBlocks(Vec<String>)`. Both are checked after `UnknownOverrides` and before
      `MissingInputs`.
- Produces in `run.rs`:
    - `Evaluate::blocks(&mut self) -> Option<&mut dyn Blocks<P, Self::Value, Self::Error>> { None }`.
    - `Evaluate::evaluate_in(&mut self, node, payload, args, branch: &[usize]) -> Result<V, E>`, whose default calls
      `evaluate`.
    - `Evaluate::lookup(&mut self, node: &Node<P>, branch: &[usize]) -> Result<Option<V>, E> { Ok(None) }`, asked
      only for steps downstream of an expand (call 7).
    - `RunError::Node` gains `branch: Vec<usize>` and `block: Option<String>`.
- The `Blocks` trait:

    ```rust
    pub trait Blocks<P, V, E> {
        /// An iterator over `iterable`: a computed expand's value, or an expand override.
        fn iterate(&mut self, node: &Node<P>, iterable: &V) -> Result<V, E>;
        /// The next item; `Done` when exhausted; `Declined(v)` when the iterator declined while yielding.
        fn next_item(&mut self, node: &Node<P>, iterator: &V) -> Result<Item<V>, E>;
        /// Ends the iterator: exhausted, or stopped early (close it). Returns the expand node's value.
        fn finish_items(&mut self, node: &Node<P>, iterator: &V, exhausted: bool) -> Result<V, E>;
        /// One collect argument from the branches' values, in branch order.
        fn gather(&mut self, consumer: &Node<P>, source: NodeId, values: Vec<V>) -> Result<V, E>;
    }
    ```

- `pub enum Item<V> { Next(V), Done, Declined(V) }`. `Declined` carries the absent value, for a generator expand
  annotated to admit `Absent` that returns `ABSENT` after yielding. The block then counts as an absent expand: the
  branches already run are dropped, and each collect edge follows §7.2 (§7.4).
- `Observer` gains methods whose defaults delegate to the existing ones, so today's observers see no change:
    - `pre_node_in(&mut self, node: &str, branch: &[usize], args)`, defaulting to `pre_node`;
    - `post_node_in(&mut self, node: &str, branch: &[usize], outcome)`, defaulting to `post_node`;
    - `post_expand(&mut self, node: &str, branch: &[usize], items: usize) -> Result<(), E> { Ok(()) }`;
    - `hit_in(&mut self, node: &str, branch: &[usize], value: &V) -> Result<(), E> { Ok(()) }`. The core fires it for
      each step whose `lookup` returned a value, in plan order within the branch, after the reverse walk has chosen
      the hits and before the first needed step runs.
- A `lookup` hit for a node that may be absent is asked `is_absent`, as a computed value is, so a replayed declared
  absence skips its consumers. A hit for a node that can't be absent is never asked.
- Run order (§4.3): pull item *i*, run branch *i*'s sub-plan to its end, then pull the next.
    - A `Collect::Present` edge gathers the present branch values, and `Collect::All` gathers every branch value.
    - An absent expand applies §7.2 to each collect edge as a whole.
    - On a failure inside a branch, `finish_items(.., exhausted: false)` runs for every open iterator, innermost
      first, then the error propagates with its branch.
- [ ] **Step 1: Write the failing tests.**
    - `branches_run_in_item_order_and_collect_in_branch_order`;
    - `an_empty_expand_gives_empty_collects`;
    - `an_expand_requested_alone_returns_a_list`: the output is `finish_items(.., exhausted: true)`'s value (Q121),
      and its block's sub-plan is not compiled, since nothing collects it in the request;
    - `an_outer_value_read_only_by_the_block_and_named_after_the_expand_runs_first`: the block reads `zz_rate`, which
      sorts after the expand `picked`. `zz_rate` still runs before `picked`;
    - the same inside a nested block, `a_nested_blocks_outer_read_named_after_its_expand_runs_first`;
    - `a_collect_runs_after_its_block`: the plan has no step left waiting;
    - `outer_values_are_computed_once_and_shared`;
    - `an_output_in_a_block_is_refused_naming_its_collects`;
    - `an_override_of_a_block_node_is_refused`;
    - `an_expand_override_supplies_the_items`;
    - `a_collect_override_cuts_the_block`;
    - `nested_blocks_gather_per_outer_branch`, with paths `(0,0), (0,1), (1,0)`;
    - `a_branch_failure_closes_every_open_iterator_and_names_its_branch`;
    - `collected_values_are_kept_and_branch_locals_released` (the `Live` drop counter in `run.rs`'s tests);
    - `collect_present_drops_absent_branches`;
    - `collect_all_keeps_their_places`;
    - `an_absent_expand_follows_the_three_rules_per_collect`;
    - `an_expand_declining_midway_drops_the_branches_already_run`;
    - `an_evaluator_without_blocks_gets_a_broken_plan_error`;
    - `observer_events_carry_branch_paths_in_reference_order`;
    - `lookup_is_asked_in_reverse_need_order_and_a_hit_skips_upstream`;
    - `hit_in_fires_for_each_hit_in_plan_order`;
    - `a_lookup_hit_that_is_absent_skips_its_consumers`;
    - `a_plan_without_blocks_or_absence_runs_the_plain_loop` (call 21);
    - `no_recursion_on_a_1000_deep_nesting_or_a_50000_chain_in_a_block`.
- [ ] **Step 2: Run them to see them fail.**
- [ ] **Step 3: Implement.**
    - `prepare` compiles a `SubPlan` per reached block, with the contracted Kahn order above. An outer value read by
      a block is released after the expand step.
    - `Plan::run` dispatches on `needs_rules` and whether `blocks` is empty, so today's loop runs plans with neither.
    - `branch.rs` runs a sub-plan with `struct Frame { block: BlockId, iterator: V, index: usize, position: usize,
      slots, collected: Vec<Vec<Slot<V>>> }` on a `Vec<Frame>`.
    - Before a branch runs, the reverse walk from `collected` asks `lookup` and marks needed steps in a `Vec<bool>`.
- [ ] **Step 4: Run the tests to see them pass**, then the bench rows `fan_out_{1,100,10000}`.
- [ ] **Step 5: Document** `Blocks`, `SubPlan` and the run order in `docs/rust-core.md`.
- [ ] **Step 6: Run `scripts/check.sh`, then commit** `core: sub-plans and Plan::run over blocks, in reference order`.

## PR B2: blocks in the front end

### Task 8: `Parallelizable`, `Collect`, item and edge checks, views (design task 8)

Rows Q118 to Q124, Q127, Q132 (*Planned*), Q136, Q137 and Q138.

**Files:**

- Create: `python/fnloom/_blocks.py`, `tests/test_blocks.py`
- Modify:
    - `crates/fnloom-py/src/`: `graph.rs`, `plan.rs` (`Blocks` for `Caller`), `errors.rs`;
    - `python/fnloom/`: `_core.pyi`, `_scan.py`, `_view.py`, `_types.py`, `_extract.py`, `driver.py`, `_async.py`
      (refusal), `__init__.py`;
    - `docs/writing-nodes.md` ("Fan-out and fan-in"), `docs/quirk-register.md`;
    - `tests/differential/`: `test_semantics.py`, `test_deliberate_differences.py`.

**Interfaces:**

- Consumes: Tasks 6 and 7.
- Produces:
    - `type Parallelizable[T] = Annotated[Iterable[T], _EXPAND]` and `type Collect[T] = Annotated[list[T],
      _COLLECT]`.
    - `_blocks.expanded_type(ann) -> object | None`, the `T` of an expand's return annotation.
    - `_blocks.collect_mode(ann)`, returning `("present", T)`, `("all", T)` or `None`. It is `"all"` when `T` admits
      `Absent`, with `Absent` stripped from `T`.
    - Both readers see through a top-level `Absent` in a union, which is how an expand or a collect parameter admits
      absence as a whole.
    - `Parallelizable[T | Absent]` is a `ValueError` at build: `'picked' yields items of type T | Absent, but an item
      can't be absent; write Parallelizable[T] | Absent to let the expand decline as a whole` (call 19).
    - The node tuple gains `expands: bool`, and inputs become `(source, dependency, collect_mode | None)`. `_core.Graph`
      gains `blocks() -> dict[str, list[str]]` (node to block path) and `Plan.blocks() -> dict[str, list[tuple[str,
      str]]]` (expand to its sub-plan steps).
    - `NodeView.block: str | None`, `NodeView.kind: Literal["input","function","expand","collect"]` (call 14), and
      `Param.collect: Literal["present","all"] | None`.
    - `_blocks.Items`: the Python iterator the binding's `iterate` returns. Its `__next__` checks each item with the
      input rules, as an input of type `T` (off under `without_input_type_checks()`). It has `close()` and
      `finish(exhausted: bool) -> list[object] | Items`, and records items only when the expand is an output or
      stored.
    - `_scan`: a generator function is a node when its return annotation is `Parallelizable[...]`. Every other
      generator stays refused as today. Only the generator-function branch of `_check_kind` (called from
      `_node_defs`) moves to after `resolve`, because only a resolved return annotation says whether the function is
      an expand, and a string annotation is unresolved at scan time. The async-function and async-generator
      refusals (Q32) stay where they are. The existing `tests/test_scan.py` and `deliberate("Q32")` tests must pass
      unchanged; only the generator message's moment changes, never its text.
    - `Builder.build_async()` raises `TypeError: blocks run on the async driver from Plan 11; 'picked' expands` (call
      6, Q132).
- Edge checks:
    - The edge from an expand to a block consumer is checked as `T`.
    - The edge from a block node to a `Collect[T]` parameter is checked as the node's type against `T`, with `Absent`
      stripped. Both honour Plan 12 PR F's per-consumer opt-out.
    - `@extract_fields` or `@extract_columns` on an expand is a `ValueError` at build naming it.
- [ ] **Step 1: Write the register rows** Q118 to Q124, Q127, Q132, Q136, Q137 and Q138, in design §10's words.
  Q132 is marked *Planned*, with today's refusal.
- [ ] **Step 2: Write the differential tests**, on programs shared by import swap, with Hamilton's synchronous and
  thread executors:
    - `keeps("Q119")`, collect order;
    - `keeps("Q120")`, empty;
    - `keeps("Q121")`, a list returned, and the expand as an output gives a `list`;
    - `keeps("Q127")`, edges;
    - `keeps("Q138")`, two independent blocks with shared inputs;
    - `deliberate(...)` for Q118 (no flag), Q122 (nesting works; Hamilton's result is wrong), Q123 (two collects),
      Q124 (refused naming the path; Hamilton is run with a 2 s timeout in a subprocess, since it spins), Q127 (a
      `str` item into an `int` consumer is a `TypeError` naming the expand and the index), Q136 and Q137 (no executor
      settings). Q137's fnloom cell reads: "No executor settings; grouping is found from the graph (§4.2).
      `with_threads(...)` is *Planned* for Plan 11".
- [ ] **Step 3: Write the fnloom-only tests** in `tests/test_blocks.py`:
    - `test_aliases_type_check_as_iterable_and_list`: this task creates `tests/type_snippets/blocks.py`, a module that
      uses `Parallelizable[str]` as a generator return and calls `len()` and `[0]` on a `Collect[float]`. `check.sh`
      already runs `uv run ty check`, and `[tool.ty.src] include` in `pyproject.toml` already lists `tests`, so ty
      checks the snippet with no new step. The pytest test imports the module and runs it;
    - `test_parallelizable_of_t_or_absent_is_refused`;
    - `test_views_report_block_and_kind`;
    - `test_extract_on_an_expand_is_refused`;
    - `test_build_async_refuses_blocks`;
    - `test_items_are_unchecked_without_input_type_checks`;
    - `test_a_generator_not_annotated_parallelizable_stays_refused`.
- [ ] **Step 4: Run them to see them fail.**
- [ ] **Step 5: Implement.** `Caller` implements `Blocks`:
    - `iterate` calls `_blocks.Items(...)`;
    - `next_item` calls `next()`, mapping `StopIteration` to `Done`, or to `Declined` when its `value` is `ABSENT`
      (the return check applies, as for any node);
    - `finish_items` calls `items.finish(exhausted)`;
    - `gather` builds a `list`.
- [ ] **Step 6: Run the tests to see them pass**, plus the whole `tests/` suite.
- [ ] **Step 7: Run `scripts/check.sh`, then commit** `Parallelizable and Collect, with no flag (Q118 to Q124,
  Q127, Q132, Q136 to Q138)`.

### Task 9: overrides, outputs, lineage, `validate` and `inputs()` (design task 9, Q125, Q126)

**Files:**

- Modify: `python/fnloom/driver.py` (`_explained`, `_hints`, `validate`, `_missing_line`), `python/fnloom/_view.py`
- Test: `tests/test_blocks.py`, `tests/differential/test_deliberate_differences.py`

**Interfaces:**

- Consumes: `PlanError::OutputsInBlocks` and `OverridesInBlocks`, surfaced by the binding as `ValueError` lines.
- Produces:
    - An output in a block: `'details' is computed once per item of 'picked', so it is not an output; request a node
      that collects it: 'summary'`. When nothing collects it, the message ends `...; collect it with a parameter
      annotated Collect[Detail]`.
    - An override of a block node: `'details' can't be overridden: it has one value per item of 'picked'; override
      'picked' with the items, or a node outside the block`.
    - `validate(...)` returns both kinds as problems, rather than raising.
- [ ] **Step 1: Write the register rows** Q125 and Q126, and their `deliberate` tests:
    - Hamilton: an expand override feeds the whole list, a block override makes `Collect` return `[]`, and a block
      output raises `TypeError` or `KeyError`.
    - fnloom: items are supplied, and both refusals use the texts above.
- [ ] **Step 2: Write the fnloom tests.**
    - `test_an_expand_override_with_a_generator_is_consumed_lazily`;
    - `test_a_collect_override_cuts_the_block`;
    - `test_a_function_in_final_vars_that_is_a_block_node_is_refused` (Q30);
    - `test_lineage_and_inputs_treat_expand_and_collect_as_ordinary`: `upstream_of`, `downstream_of`,
      `what_is_the_path_between` and `inputs()` keep their shapes;
    - `test_validate_lists_block_problems`.
- [ ] **Step 3: Run them to see them fail. Step 4: Implement. Step 5: Run the tests to see them pass.**
- [ ] **Step 6: Run `scripts/check.sh`, then commit** `Overrides and outputs around blocks (Q125, Q126)`.

### Task 10: hooks, `current_branch()`, wrappers per branch, the failing branch (design task 10, Q128, Q130)

**Files:**

- Modify:
    - `crates/fnloom-py/src/`: `observer.rs`, `plan.rs` (`evaluate_in` sets the context variable), `errors.rs` (the
      note);
    - `python/fnloom/`: `_hooks.py` (`ExpandEnd`, `after_expand`, `branch`, `PrintNodes`), `_blocks.py`, `_wrappers.py`,
      `__init__.py`;
    - `docs/hooks.md`, `docs/quirk-register.md` (Q84, Q111 amended).
- Test: `tests/test_hooks.py`, `tests/test_wrappers.py`, `tests/test_blocks.py`, `tests/differential/*`

**Interfaces:**

- Produces:
    - `NodeStart.branch` and `NodeEnd.branch`, of type `tuple[int, ...]`, defaulting to `()`.
    - `@dataclass(frozen=True, slots=True, kw_only=True) class ExpandEnd: run_id: str; node: NodeView; branch:
      tuple[int, ...]; items: int`.
    - `Hook.after_expand(self, event: ExpandEnd) -> None`.
    - `fnloom.current_branch() -> tuple[int, ...]`, which reads `_blocks.BRANCH: ContextVar[tuple[int, ...]]`
      (default `()`). The binding sets it around a block node's call only, in the caller's context, and resets it after.
    - `RunStart.nodes` lists each block node once, in plan order.
    - The error note: `while computing '<node>' for branch <path> of '<innermost expand>'`, added with `add_note`.
    - `PrintNodes`: timing is keyed by `(run_id, node, branch)`, and a block node's line reads `node 'details' (branch
      (3,)) ran in 1.2 ms`.
- [ ] **Step 1: Write the register rows** Q128 (sequential, call 5) and Q130. Amend Q84 and Q111.
- [ ] **Step 2: Write the failing tests.**
    - `keeps("Q128")`: the exception class is Hamilton's, and finished branches are dropped.
    - `deliberate("Q130")`: no task hooks; `branch` and `after_expand` instead.
    - `test_node_events_carry_branch_in_reference_order`.
    - `test_after_expand_reports_the_item_count_per_branch` (nested).
    - `test_current_branch_inside_a_node_a_check_and_a_wrapper`, and `()` outside.
    - `test_a_wrapper_is_called_once_per_branch`.
    - `test_a_failing_branch_closes_the_generator_once` (Review Focus 1).
    - `test_print_nodes_times_each_branch_apart`.
    - `test_hook_with_after_expand_only_is_accepted_and_checked`, as `_hooks.checked` checks.
- [ ] **Step 3: Run them to see them fail. Step 4: Implement. Step 5: Run the tests to see them pass**, plus
  `pytest tests/test_hooks*.py tests/test_wrappers.py`.
- [ ] **Step 6: Run `scripts/check.sh`, then commit** `Hooks and wrappers per branch (Q128, Q130; amends Q84, Q111)`.

## PR B3: caching blocks

### Task 11: item fingerprints, branch keys, branch hits and the positional fallback (design task 11)

**Files:**

- Create: `tests/test_blocks_caching.py`
- Modify:
    - `crates/fnloom-py/src/`: `keys.rs`, `plan.rs` (`lookup`), `observer.rs` (`hit_in`);
    - `python/fnloom/`: `_caching.py`, `_hooks.py` (`Dispatch.hit`), `_core.pyi`;
    - `docs/caching.md`.
- Test: `tests/test_blocks_caching.py`, `tests/test_threads.py`

**Interfaces:**

- Consumes: `Evaluate::lookup`, `Observer::hit_in` and `post_node_in`, from Task 7.
- Produces:
    - `keys.rs`: `BlockKeys`, one per block per prepared plan, built like `KeyPlan` over the sub-plan's steps.
    - A per-run `BranchKeys` (pyclass) that computes a branch's keys once its item's version is known, memoised by
      `(node, branch)`. It answers `lookup` from the store, and stores each computed late value under its key.
    - The version of an item, in order of preference:
        1. its fingerprint;
        2. otherwise `hash_parts([expand_key, index])`, only when the expand's items are stored or replayed this run
           (Task 12);
        3. otherwise `None`. The branch is then uncacheable downstream.
    - `CacheRun`: late nodes (every node downstream of an expand) enter the `KeyPlan` with `stat = None` and
      `stored = False`, so the walk at `begin` passes through them to their outer sources. A `Collect` node's sources
      include its blocks' expands and their `reads`.
    - `ObserverBase.hit_in(self, node: str, *, branch: tuple[int, ...], value: object) -> None` forwards the core's
      `hit_in` to `Dispatch.hit(node, branch, value)`. That fires `before_node` and `after_node` with `cache` outcome
      `hit`, as an outer hit fires today (Q85).

- [ ] **Step 1: Write the failing tests.**
    - `test_a_second_run_hits_every_branch_and_runs_nothing_inside`.
    - `test_an_appended_item_computes_one_branch`.
    - `test_a_moved_item_hits` (no index in the key).
    - `test_a_hit_at_a_blocks_exit_skips_its_upstream_in_that_branch`.
    - `test_unfingerprintable_items_without_stored_items_are_uncacheable` (Review Focus 3).
    - `test_policies_apply_per_branch`: `recompute` and `disable` on block nodes, and Plan 12 PR E's driver lists.
    - `test_branch_hits_fire_hit_events`: Q85's events, `cache` outcome `hit`, with `branch`, in plan order within
      each branch.
    - In `tests/test_threads.py`, `test_a_block_graph_shared_by_threads`: 16 threads share one cached driver and run
      a nested fan-out with mixed hits and misses. Every result equals the single-threaded one, and every
      `current_branch()` read inside a node equals that node's own branch. Run on 3.14 and 3.14t. It checks that
      `BranchKeys` and `Items` are per run and that the context variable is per call.
- [ ] **Step 2: Run them to see them fail. Step 3: Implement. Step 4: Run the tests to see them pass**, plus
  `pytest tests/test_caching*.py`.
- [ ] **Step 5: Run `scripts/check.sh`, then commit** `Caching blocks: content-addressed branch keys`.

### Task 12: the expand's stored items, ordered collect versions, `explain_cache` (design task 12, Q129)

**Files:**

- Modify: `python/fnloom/_caching.py`, `python/fnloom/_blocks.py` (`Items` records), `crates/fnloom-py/src/keys.rs`,
  `docs/caching.md`, `docs/quirk-register.md`
- Test: `tests/test_blocks_caching.py`, `tests/differential/test_deliberate_differences.py`

**Interfaces:**

- Produces:
    - An expand node with policy `default` stores the `list` of its items under its ordinary key. A hit is applied as
      an override of the expand, through Task 9's path, so the items replay without the expand running.
    - A `recompute` or `disable` expand stays lazy and stores nothing.
    - A `Collect` node's version of a collected parameter is `hash_parts([key(m, b) for b in branches])`, in branch
      order, over every branch, present or absent. Any `None` makes it `None`.
    - `explain_cache(...)` reports a block as `{"<expand>": outcome, "branches": {"hit": n, "miss": n, "uncacheable":
      n}}`. The counts come from the stored items and the store, and nothing runs. `branches` is `None`, with reason
      `the expand node runs, so its branches are known only at run`, when its items aren't stored. The reason names
      its source as Plan 12 PR E does.
- [ ] **Step 1: Write the register row Q129**, and its `deliberate` test: Hamilton recomputes the expand and serves a
  stale order-dependent collect after reordering. fnloom replays the items and misses the collect.
- [ ] **Step 2: Write the failing tests.**
    - `test_the_expand_is_replayed_from_its_stored_items`;
    - `test_a_lazy_expand_marked_disable_stores_nothing` (a 10,000-item generator never held, checked with a weakref
      counter);
    - `test_reordered_items_hit_branches_and_miss_the_collect` (Review Focus 2);
    - `test_the_positional_fallback_holds_under_stored_items`;
    - `test_explain_cache_counts_branches_and_runs_nothing`.
- [ ] **Step 3: Run them to see them fail. Step 4: Implement. Step 5: Run the tests to see them pass.**
- [ ] **Step 6: Run `scripts/check.sh`, then commit** `Caching blocks: stored items, ordered collects (Q129)`.

## PR B4: drawing and export of blocks

### Task 13: clusters, `fnloom.export/2` and `diff` (design task 13, Q131, Q95)

**Files:**

- Modify: `python/fnloom/_drawing.py`, `python/fnloom/_export.py`, `python/fnloom/_cli.py` (nothing new; tests
  only), `docs/drawing.md`, `docs/quirk-register.md`
- Test: `tests/test_drawing.py`, `tests/test_export.py`, `tests/test_cli.py`,
  `tests/differential/test_deliberate_differences.py`

**Interfaces:**

- Produces:
    - DOT: `subgraph "cluster_<expand>" { label="for each item of <expand>"; ... }`, nested as the blocks nest. The
      expand's edges into the block get `label="expand"`, and each collect edge gets `label="collect"`.
    - Mermaid: `subgraph <id> ["for each item of <expand>"]` ... `end`, with edges `-- expand -->` and `-- collect -->`.
    - Both are ordered by node name, so the hash seed changes nothing (Q94).
    - The builtin SVG follows call 12.
    - `draw_execution` draws each block once.
    - `DrawnNode.block: str | None` is excluded from equality, like `view`.
    - Export: a node's `kind` takes `"expand"` or `"collect"`; it gains `"block": str | None`; a param gains
      `"collect"`. The document is `/2` when a block or a may-be-absent node exists. `graph_version` hashes the new
      fields, and `diff` compares them.
- [ ] **Step 1: Write the register row Q131**, and amend Q95's text.
- [ ] **Step 2: Write the failing tests.**
    - `test_blocks_draw_as_nested_clusters_in_dot_and_mermaid`: golden text under three hash seeds;
    - `test_draw_execution_draws_a_block_once`;
    - `test_styles_apply_inside_clusters` (Plan 12 PR B's `styled`);
    - `test_export_2_carries_kind_block_and_collect`;
    - `test_diff_sees_a_param_changed_to_collect`;
    - `test_cli_draws_and_exports_a_block`;
    - `deliberate("Q131")`: Hamilton has no grouping, and fnloom has one cluster.
- [ ] **Step 3: Run them to see them fail. Step 4: Implement. Step 5: Run the tests to see them pass.**
- [ ] **Step 6: Run `scripts/check.sh`, then commit** `Drawing and export of blocks (Q131; amends Q95)`.

## PR B5: namespaces, decorators and absence in blocks

### Task 14: namespaces, `bind=` and every decorator on block nodes (design task 14)

**Files:**

- Modify: `python/fnloom/driver.py` (`_checked_bound`, the bind refusal), `docs/writing-nodes.md`
- Test: `tests/test_namespaces.py`, `tests/test_stacking.py`, `tests/test_blocks.py`

**Interfaces:**

- Produces:
    - A `bind=` source that is a block node of another namespace is a `ValueError` at build: `bind={'x':
      source('a.details')} reads a node computed once per item of 'a.picked'; bind its collect instead` (call 4).
    - Blocks are named by their namespaced expand.
- [ ] **Step 1: Write the failing tests.**
    - `test_two_copies_of_a_module_have_independent_blocks`;
    - `test_bind_to_an_outer_node_is_shared_by_the_copys_branches`;
    - `test_bind_to_another_copys_block_node_is_refused`;
    - `test_bind_to_another_copys_collect_works`;
    - `test_namespace_config_selects_variants_inside_blocks` (Plan 12 PR F);
    - `test_replace_may_replace_an_expand` (PR F);
    - `test_parameterize_on_an_expand_makes_one_block_per_row`.
    - In `tests/test_stacking.py`, extend the pair test with a block node under each decorator. Cover `@parameterize`,
      `@config.when*`, `@config.matches`, `@tag`, `@check_output*`, `@mutate`, `@pipe_*` (with PR D's step
      namespaces), `@inject`, `@resolve`, `@cache` (with `fields=`) and a node wrapper. Each pair, in both orders, gives
      one graph and runs per branch.
- [ ] **Step 2: Run them to see them fail. Step 3: Implement. Step 4: Run the tests to see them pass.**
- [ ] **Step 5: Run `scripts/check.sh`, then commit** `Blocks across namespaces, bind= and decorators`.

### Task 15: absence in blocks (design task 15)

**Files:**

- Modify: `python/fnloom/_blocks.py`, `python/fnloom/_absence.py`, `python/fnloom/_caching.py`,
  `docs/writing-nodes.md`
- Test: `tests/test_absence.py`, `tests/test_blocks.py`, `tests/test_blocks_caching.py`

**Interfaces:**

- Consumes: Task 7's collect modes and absent-expand rules, and Task 4's stored marker.
- Produces the front-end shapes:
    - `Collect[T]` over a block node that may be absent gives the present values.
    - `Collect[T | Absent]` gives one entry per branch.
    - `Collect[T] | Absent` receives `ABSENT` for an absent expand.
    - A defaulted collect parameter gets its default.
    - A plain collect parameter is skipped.
- [ ] **Step 1: Write the failing tests.**
    - `test_collect_drops_declined_branches_in_order`;
    - `test_collect_with_absent_keeps_places`;
    - `test_an_absent_expand_is_not_an_empty_one`: `[]` against `ABSENT`, a default, and a skip;
    - `test_a_replayed_declined_branch_skips_its_consumers`: a branch node's declared `ABSENT`, served by `lookup`,
      is asked `is_absent` (Task 7), so its block consumers skip again without running;
    - `test_items_already_pulled_are_dropped_when_the_expand_declines_midway`: a generator annotated
      `-> Parallelizable[str] | Absent` that returns `ABSENT` after two yields declines as a whole. Without `| Absent`,
      the same generator fails with the return check's `TypeError`;
    - `test_a_declined_branch_is_cached_and_replayed`;
    - `test_edge_check_of_t_or_absent_into_collect_t`;
    - all of these on the sync driver only, since the async driver refuses blocks (call 6).
- [ ] **Step 2: Run them to see them fail. Step 3: Implement. Step 4: Run the tests to see them pass.**
- [ ] **Step 5: Run `scripts/check.sh`, then commit** `Absence in blocks`.

## PR Z: close-out

### Task 16: benchmarks, docs, the example, the roadmap and the design change log (design task 27, Plan 10's share)

**Files:**

- Create: `tests/test_fan_out_example.py`, `benchmarks/test_blocks.py`
- Modify:
    - `benches/run.py`, `benches/generate.py`;
    - `docs/`: `benchmarks.md`, `running.md` ("Fan-out and fan-in", "Absence", and the threads note), `from-hamilton.md`,
      `quirk-register.md` (Q157 narrowed), `index.md`;
    - `README.md`, `CHANGELOG.md`, the roadmap, and the design's change log.

**Interfaces:**

- Consumes: everything above.
- The example is a catalog module:
    - `listing(category: str, item: str | None = None) -> list[str] | Absent` declines when `item` is given.
    - `picked(listing: list[str] | Absent, item: str | None = None) -> Parallelizable[str]` yields `[item]` or the
      listing.
    - `details(picked: str, endpoint: Endpoint) -> Detail` is called once per item. `Endpoint` is a frozen
      dataclass holding only its base URL, so it fingerprints and `details` is cacheable. The test's call log lives
      outside it, in a module-level list the fake fetch appends to.
    - `summary(details: Collect[Detail]) -> Summary`.
- [ ] **Step 1: Write the failing end-to-end tests** in `tests/test_fan_out_example.py`:
    - `test_the_listing_fans_out_one_call_per_item`;
    - `test_naming_one_item_skips_the_listing`: the endpoint's call log has one call and the listing has none;
    - `test_a_second_run_is_served_from_the_cache`;
    - `test_naming_one_item_after_a_full_run_hits_its_branch`: after a full-listing run, a run naming one listed
      item makes no endpoint call. Its one branch is served from what the full run stored, since branch keys are
      content-addressed.
- [ ] **Step 2: Run them, implement any gap found, and run them to see them pass.** A gap is a bug in an earlier
  task: fix it there, in its own commit, with a test.
- [ ] **Step 3: Add the benchmarks** for §12.2's rows without threads, against Hamilton's synchronous executor where
  it has one:
    - fan-out of 1, 100 and 10,000 trivial branches, with a target under 2 µs per branch node;
    - a fully cached fan-out of 1,000 branches, and the same with one appended item;
    - `ABSENT` through a 200-node chain, with no cost on a graph with no node that may be absent.

  Record the results in `docs/benchmarks.md`, and add the CodSpeed rows.
- [ ] **Step 4: Write the docs.**
    - `docs/running.md` gets "Fan-out and fan-in" and "Absence", with the example as their sample. Its Threads section
      says the default stays one node at a time until Plan 11.
    - `docs/from-hamilton.md`:
        - rewrite the rows for `Parallelizable` and `Collect` (Q118 to Q138);
        - the task hooks row cites Q130 instead of Q157;
        - "Not there yet" keeps only the parallel executor and `GracefulErrorAdapter`.
- [ ] **Step 5: Update the roadmap and the design.**
    - Roadmap item 9 says Plan 10 is built and ships in 0.5.0, and the status line says so. The ledger row "A node
      can't skip" becomes **fixed (0.5.0)**.
    - The design gets a §14 change-log line saying what was built, and any departure the PRs made.
- [ ] **Step 6: Run `scripts/check.sh`, then commit** `Plan 10 close-out: benchmarks, docs, roadmap`.

The 0.5.0 release PR itself follows this plan's last merge, as 0.3.0 did (#34), after Plan 12's 0.4.0. It is not
part of this plan.
