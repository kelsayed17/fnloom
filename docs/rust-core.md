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
  `&mut dyn Blocks`, which `iterate`s an expand's value, gives each `next_item` (`Item::Next`, `Item::Done`, or
  `Item::Declined` when the iterator declines after yielding), `finish_items` once exhausted or stopped, and
  `gather`s a collect's argument from the branches' values. The default has none, and such a plan then fails with
  `RunError::BrokenPlan`.

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

- `BuildError`: `DuplicateName`; `ConflictingInput`, a spec listing one input twice with different dependencies (listed
  twice alike, it is one edge); `Cycle` with the path, its first name repeated at the end; or one of the block
  refusals ([Blocks](#blocks)).
- `PlanError`: `UnknownOutputs`, `UnknownInputs`, `UnknownOverrides`, `OutputsInBlocks`, `OverridesInBlocks` or
  `MissingInputs`, checked in that order, each with every offending name sorted; `UnknownNodes` from the lineage
  queries.
- `RunError<E>`: `Node` (a payload failed, with any observer errors raised while handling it, and the branch it
  failed in), `Observers` (the
  first failing event and every error raised in it), `MissingValues` (a map lacked a name the plan reads; raised
  before any hook fires), or `BrokenPlan` (the plan's own state is inconsistent, which is a bug). Run a plan only on
  the graph it was prepared from: on another graph it may read the wrong nodes, or panic.

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
