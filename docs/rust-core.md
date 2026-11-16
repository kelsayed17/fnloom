# The Rust core

`fnloom-core` is the engine under the Python front end: the node model, the graph, the planner and the executor. It's
a Rust crate of its own, with no dependencies and `#![forbid(unsafe_code)]`. It's generic over what a node runs, so
you can use it to drive payloads other than Python functions.

!!! note "Not on crates.io"

    The crate is not published yet. Depend on it by git (`fnloom-core = { git = "https://github.com/kelsayed17/fnloom" }`).
    It needs Rust 1.85 or newer.

## The flow

1. **Specs.** Each node is a `NodeSpec<P>`: a name, the names it reads (`InputSpec`, each `Required`, `Optional`
   or `Admits`, and each optionally a `Collect`), tags, a payload `P` owned by you, `declines`, whether its value
   may be absent ([Absence](#absence)), and `expands`, whether it is an expand ([Blocks](#blocks)).
2. **Build.** `Graph::build(specs)` checks for duplicate names and cycles, makes an input node of every name that is
   read but not declared, and gives each node a `NodeId` in sorted name order. A node's `kind` is
   `NodeKind::Function(payload)`, `NodeKind::Expand(payload)` or `NodeKind::Input`, so only inputs carry no
   payload. It also finds each node's block.
3. **Prepare.** `graph.prepare(&request)` turns a `Request` (outputs, input names, override names) into a `Plan`: the
   steps to run, in topological order with ties broken by id, the values each step releases, and a sub-plan for
   each block the request reaches.
4. **Run.** `plan.run(&graph, &mut evaluator, &inputs, &overrides, &mut observers)` executes the steps and returns
   the requested outputs by name. A plan runs as often as you like. `plan.run_by_id(&graph, &mut evaluator, lookup,
   &mut observers)` runs it the same way but reads each input and override through `lookup(action, node_id)` and
   returns the outputs by `NodeId`, so an embedder that keys its values by node needs no name strings per run.

## An example

A graph whose nodes add a constant to the sum of their inputs. The payload is the constant, and the evaluator is
what gives payloads their meaning:

```rust
--8<-- "crates/fnloom-core/examples/embed.rs"
```

It prints `x = 1: c = 114` and `x = 2: c = 116`. Run it with `cargo run -p fnloom-core --example embed`.

## Evaluate

`Evaluate<P>` is the seam that runs payloads. It names the value type flowing along edges (`Value: Clone`) and the
error a payload can raise. The executor calls `evaluate(node, payload, args)` once per computed step, with the node's
payload already taken out of its `NodeKind::Function`, and the arguments as `Arg { node, name, value }` sorted by
id; an argument's `node` lets an embedder key per-node data by `NodeId::index`, as the Python binding does for its
interned keyword names. An optional input that was not supplied is simply absent from `args`.

`is_absent(&value)` says whether a value is absent. Its default says none is, so an embedder whose nodes never
decline leaves it out.

## Absence

A node with `declines: true` may decline to produce a value: its payload returns a value your `is_absent`
recognises. The core asks only of such a node's value, and of an override of a node that may be absent. Each
consumer of an absent value decides by its own dependency on it:

- `Admits` receives the absent value as its argument;
- `Optional` has the argument omitted, as for an optional input that wasn't supplied;
- `Required` is skipped: it is absent too, its slot holds the absent value that skipped it, and observers see
  `skipped(node, branch)` instead of its node events.

`Admits { optional }` says whether the consumer has a default, which is what counts when the edge reads an input
that wasn't supplied, since an input is never absent. The build marks each node `may_be_absent` when it declines or
reads a node that may be absent by a `Required` edge. A requested output that is absent is returned as its absent
value. A plan where nothing may be absent (`Plan::needs_rules` is false) runs the plain loop, at no extra cost.

A node with `absent_on_error: true` may be absent on an error instead: when its payload fails, or for an expand a
pull of its items, the core asks `Evaluate::absent_on_error(node, &error)`, whose default is `None`. `Some(value)`
makes the node absent, holding `value`, after `post_node` reported the error, and observers then see
`absent_error(node, branch, &error)`; an expand absent while it yields drops its branches, as `Item::Declined` does.
`None` fails the run as before. Such a node may be absent, so its consumers follow the rules above, but it doesn't
decline: an absent value it returns itself is still yours to refuse. A `Scheduler` takes such an outcome as
`Outcome::ErrorAbsent(value)`, from a compute or a pull, and single-flight never shares it: a step waiting on its
key runs itself. An `Item::Declined` from a node that doesn't decline is still a broken plan.

## Blocks

An expand (`expands: true`) is computed like a function, and its value is then iterated: each item runs the
expand's **block** once, as a **branch**. The block holds every node that reads the expand along a path that crosses
no collect of it. A parameter with `collect: Some(Collect::Present)` or `Some(Collect::All)` collects a block: it
reads a node inside it, and receives one value gathered from every branch, in branch order. `Present` drops the
branches where that node is absent, and `All` keeps their places. A block node may also read nodes outside the
block: they are computed once, before the expand, and shared by every branch.

- **Build.** `Node.block` is a node's innermost expand, and `graph.block_path(id)` all of them, outermost first.
  Blocks nest, any number of collects may read one block, and a node may collect one block and expand another. The
  build refuses a collect of a node in no block (`CollectOutsideBlock`) or of an expand (`CollectOfExpand`), a node
  reading the blocks of two unrelated expands (`UnrelatedBlocks`), and a node collecting a block it is itself
  inside (`CollectsOwnBlock`), by reading it directly too, or because the block reads its own collect back through
  nodes outside it.
- **Prepare.** `plan.blocks()` holds a `SubPlan` per block the request reaches: its `expand`, its `steps` for one
  branch, the nodes its collects read (`collected`), and the outer values it reads (`reads`). `plan.block_of(expand)`
  names an expand's own; a nested block is one step of its outer sub-plan. An expand requested with nothing
  reading its block has no sub-plan. A requested output inside a block (`PlanError::OutputsInBlocks`) and an
  override of a block node (`OverridesInBlocks`) are refused, since each has one value per branch; an override of an
  expand supplies its items, and one of a collect cuts its block. `plan.is_late(node)` says whether a step is
  downstream of an expand.
- **Run.** A plan with an expand step needs an evaluator that opts in to blocks: `Evaluate::blocks` returns a
  `&mut dyn Blocks`, which `iterate`s an expand's value in the branch the expand ran in, gives each `next_item`
  (`Item::Next`, `Item::Done`, or `Item::Declined` when the iterator declines after yielding), `finish_items` once
  exhausted or stopped, and `gather`s a collect's argument from the branches' values. The default has none, and such
  a plan then fails with `RunError::BrokenPlan`.

Branches run in **reference order**: the outer steps in plan order, and at an expand step, pull item 0, run branch
0's steps to the end, pull item 1, and so on, then the next outer step. Nested blocks repeat this inside a branch, on
an explicit stack. A branch path names a branch, one item index per level of nesting, outermost first.

- An expand with no items gives every collect an empty value. An expand requested as an output returns what
  `finish_items` returned.
- An absent expand has no branches, and each collect of its block follows the three absence rules as a whole. An
  iterator that declines midway drops the branches already run.
- On a failure in a branch, `finish_items(.., false)` runs for every open iterator, innermost first, and the error
  carries its `branch` and `block`.
- `Evaluate::evaluate_in` (default: `evaluate`) and the observers' `pre_node_in` and `post_node_in` (default:
  `pre_node` and `post_node`) get the branch path; `post_expand` reports an expand's item count.
- `Evaluate::lookup(node, branch)` (default: none) may give a step's value without computing it, such as a cache
  hit, for the steps downstream of an expand. Before a branch runs, its steps are asked in reverse, from the nodes
  its collects read, so a hit skips what only it needed; outside a block, a late step is asked when it is reached.
  Each hit fires `hit_in`.

A plan with no expand step runs the plain loop, unchanged.

## Observers

`Observer<V, E>`, with `V` and `E` the evaluator's value and error types, has these hooks, each with a default that
does nothing, or calls the one it extends:

- `pre_graph()`, once before anything runs.
- `pre_node(node, args)`, before a computed node is evaluated, with the very `args` `evaluate` gets.
- `post_node(node, outcome)`, after it, with `Ok(&value)` or `Err(&error)`.
- `skipped(node, branch)`, instead of the node events, for a node skipped because a `Required` argument is
  absent; `branch` is empty.
- `pre_node_in`, `post_node_in`, `post_expand` and `hit_in`, for a plan with blocks ([Blocks](#blocks)).
- `post_graph(ok)`, once after the run ended.

The node hooks fire only around computed steps, and get the values by reference, so an observer that ignores them
costs nothing. Every observer sees every event, in slice order, even when an earlier one fails; the first failure then
aborts the run, and `post_graph(false)` still runs so observers can close what they opened. An error from
`post_graph` is reported alongside the run's outcome, never instead of it.

## Errors

- `BuildError`: `DuplicateName`; `ConflictingInput`, a spec listing one input twice with different dependencies or collect
  modes (listed twice alike, it is one edge); `Cycle` with the path, its first name repeated at the end; or one of the block
  refusals ([Blocks](#blocks)).
- `PlanError`: `UnknownOutputs`, `UnknownInputs`, `UnknownOverrides`, `OutputsInBlocks`, `OverridesInBlocks` or
  `MissingInputs`, checked in that order, each with every offending name sorted; `UnknownNodes` from the lineage
  queries.
- `RunError<E>`: `Node` (a payload failed, with any observer errors raised while handling it, and the branch it
  failed in), `Observers` (the
  first failing event and every error raised in it), `MissingValues` (a map lacked a name the plan reads; raised
  before any hook fires), or `BrokenPlan` (the plan's own state is inconsistent, which is a bug). Its message shows a
  branch as `run::branch_text` writes it, a Python tuple such as `(0, 1)`. Run a plan only on the graph it was
  prepared from: on another graph it may read the wrong nodes, or panic.

## The scheduler

`schedule::Scheduler` is the second way to run a plan. It is a state machine over one run, with no threads, no clock
and no locks, and it borrows nothing: every method takes the plan and graph it was made with, so a caller with several
workers keeps it behind one lock. `Plan::run` stays the reference it is tested against.

- `schedule::Layout::new(&plan, &graph)` computes, once per plan, what every run of it starts from: per scope, what
  each step waits for and how many steps read its value. Keep it in an `Arc` beside the plan.
- `Scheduler::new(&plan, &graph, layout, seeded, options, &mut report)` takes the plan's layout and each input's and
  override's value by node id, as a `run::Slot` that says whether it is absent, and adds to `report` what they settle
  before any task.
- `next(&plan, &graph, &grant, seat)` hands out the ready task with the lowest **order key** that the `Grant` lets the
  `Seat` start, skipping over the ones it refuses. It gives `Next::Wait` while tasks are in flight or refused, and
  `Next::Done` when nothing is left.
- `complete(&plan, &graph, &grant, id, outcome, &mut report)` takes back a task's `Outcome` and adds to the `Report`
  the values no step reads any more (`released`, for the caller to drop, so no value is dropped under its lock), and
  the nodes skipped, the expands exhausted and the hits that completion settled, for the observer events. The caller
  keeps one report, and `Report::clear`s it between calls.
- `recycle(args)` hands a compute's emptied argument vector back, so the next compute reuses its buffer.
- `finish(&plan, &graph)` returns the outputs by id, or the failure with the lowest order key.

The ready set is a heap of entry ids whose keys are compared in place, never stored: an `OrderKey` is built only for a
task handed out, and one of up to five parts (`OrderKey::new`, `as_slice`) is held inline, with no allocation.

A plan without an expand or absence rules runs **flat**: it has one scope, no probe, no single-flight key and no skip,
so its steps live in one table by position, the ready set is a heap of positions, and a task's id is its step's
position. With one worker that costs about 1.8 times `Plan::run`'s instructions per node, which the
[instruction-count gate](benchmarks.md#the-instruction-count-gate) holds under 2. Every other plan runs on the general
path above. Neither changes what a caller sees: the same tasks, keys, reports and outputs.

An order key (`OrderKey`, also given by `Plan::order_key(node, branch)`) is the (step position, branch index) pairs of
the expands whose blocks hold a step, outermost first, then the step's position. Keys compare lexicographically, so
handing out the lowest ready one reproduces reference order: with one worker, stepping one task at a time gives the
same calls, in the same order, as `Plan::run`. A pull of item `i` is keyed just before that branch's first step.

A `Task` names its node, its branch and its key, and its `Work` is one of:

- `Compute`: run the payload on `args`, each an `ArgValue` by the absence rules (`One`, `Gather` for a collect, or
  `Omitted`). An expand iterates what it computed on the same worker. A `late` step outside every block is looked up
  first, and a hit completes it with `Outcome::Hit`.
- `Iterate`: iterate an expand's override or hit.
- `Pull`: take an iterator's next item. At most one pull of an iterator is in flight.
- `Finish`: end an iterator, exhausted or stopped.
- `Probe`: ask a new branch's hits. `schedule::probe` runs it with your lookup, in the reverse need order `Plan::run`
  asks, and gives the `Outcome::Needs` to complete it with.

A node skipped by the absence rules becomes no task: it is reported in `Report::skipped` with its key. A collect waits
for every branch of its block and gathers them in branch order, whatever order they finished in.

- **Limits.** `limits::Limits::new(max_workers, node_caps, node_groups, group_caps)` is a `Grant` of atomic counters
  only, shared by every run of a driver. A task takes a worker slot when its seat is `Seat::Take`, and none when it is
  `Held` (a run's caller), `Lent` or `Threadless` (an async node); a compute or a pull also takes its node's slot and
  its group's. Every cap is a `NonZeroUsize`, since a cap of 0 would refuse for good. `held(Resource)` says how many
  are taken. `limits::Unlimited` refuses nothing.
- **In flight.** `Options::in_flight` bounds how many branches of an expand are unfinished at once: its iterator is
  pulled only while fewer are.
- **Single-flight.** A probe's `Outcome::Needs` gives each step's key, or `None`. A step whose key a running compute
  has waits for it and completes as a hit with its value or declared absence (in `Report::hits`); if that compute
  fails, the step is ready again. Steps without a key never wait.
- **Stopping.** A failure, or `stop()`, ends the handing out of computes, pulls, iterates and probes. Then one
  `Finish` is handed out per open iterator with no pull in flight, innermost first; an iterator whose pull is in flight
  is finished once its pull returns. Failures keep their order keys, and `finish` returns the lowest key's.
- **Task ids.** A `TaskId` names one hand-out, so `complete` ignores one already completed (a stale or repeated id),
  releasing its outcome's values and giving back no slot. A layout, a plan or a graph other than the run's is a
  `RunError::BrokenPlan`, even of the same shape: each plan has its own identity from `prepare`, which a clone keeps.
- **Events.** A caller that sends observer events sends each report's `hits`, then its `expanded`, then its
  `skipped`: with one worker they then come in `Plan::run`'s order.
- `blocked()` names the lowest-key ready task's node when the grant refused it and nothing of the run is in flight,
  and `lend()` gives the run one lent seat, which `Seat::Lent` tasks take one at a time.

## Guarantees

- **No recursion over graph depth or nesting depth.** Build, cycle detection, block discovery, the lineage queries,
  planning and running are all iterative; the tests build and query a chain of 50,000 nodes, and build, plan and
  run 1,000 nested blocks and a 50,000-node chain inside one.
- **Deterministic.** Ids follow sorted names, lineage queries return ids in order, and the execution order depends
  only on the graph and the request.
- **Values are released when dead.** A step's `release` list drops every value no later step reads, unless it is a
  requested output.
- **Instruction counts are gated.** CI measures `build`, `prepare` and `run` under callgrind on chain, fan-in and
  layered graphs, and `run` on a fan-out of 1, 100 and 10,000 branches, and fails on a rise of more than 2%; see [Benchmarks](benchmarks.md#the-instruction-count-gate).
