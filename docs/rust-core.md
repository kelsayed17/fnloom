# The Rust core

`fnloom-core` is the engine under the Python front end: the node model, the graph, the planner and the executor. It
is a Rust crate of its own, with no dependencies and `#![forbid(unsafe_code)]`, and it is generic over what a node
runs, so it can drive payloads other than Python functions.

!!! note "Not on crates.io"

    The crate is not published yet. Depend on it by git (`fnloom-core = { git = "https://github.com/kelsayed17/fnloom" }`).
    It needs Rust 1.85 or newer.

## The flow

1. **Specs.** Each node is a `NodeSpec<P>`: a name, the names it reads (`InputSpec`, each `Required` or `Optional`),
   tags, and a payload `P` owned by you.
2. **Build.** `Graph::build(specs)` checks for duplicate names and cycles, makes an input node of every name that is
   read but not declared, and gives each node a `NodeId` in sorted name order.
3. **Prepare.** `graph.prepare(&request)` turns a `Request` (outputs, input names, override names) into a `Plan`: the
   steps to run, in topological order with ties broken by id, and the values each step releases.
4. **Run.** `plan.run(&graph, &mut evaluator, &inputs, &overrides, &mut observers)` executes the steps and returns
   the requested outputs by name. A plan runs as often as you like.

## An example

A graph whose nodes add a constant to the sum of their inputs. The payload is the constant, and the evaluator is
what gives payloads their meaning:

```rust
--8<-- "crates/fnloom-core/examples/embed.rs"
```

It prints `x = 1: c = 114` and `x = 2: c = 116`. Run it with `cargo run -p fnloom-core --example embed`.

## Evaluate

`Evaluate<P>` is the seam that runs payloads. It names the value type flowing along edges (`Value: Clone`) and the
error a payload can raise. The executor calls `evaluate(node, args)` once per computed step, with the arguments as
`Arg { node, name, value }` sorted by id; `node` lets an embedder key per-node data by `NodeId::index`, as the Python
binding does for its interned keyword names. An optional input that was not supplied is simply absent from `args`.

## Observers

`Observer<E>` has four hooks, each with a default that does nothing: `pre_graph`, `pre_node`, `post_node(ok)` and
`post_graph(ok)`. They fire only around computed steps. Every observer sees every event, in slice order, even when an
earlier one fails; the first failure then aborts the run, and `post_graph(false)` still runs so observers can close
what they opened. An error from `post_graph` is reported alongside the run's outcome, never instead of it.

## Errors

- `BuildError`: `DuplicateName`, or `Cycle` with the path, its first name repeated at the end.
- `PlanError`: `UnknownOutputs`, `UnknownInputs`, `UnknownOverrides` or `MissingInputs`, checked in that order, each
  with every offending name sorted; `UnknownNodes` from the lineage queries.
- `RunError<E>`: `Node` (a payload failed, with any observer errors raised while handling it), `Observers` (the
  first failing event and every error raised in it), `MissingValues` (a map lacked a name the plan reads; raised
  before any hook fires), or `BrokenPlan` (the plan was run on a graph it was not prepared from).

## Guarantees

- **No recursion over graph depth.** Build, cycle detection, the lineage queries, planning and running are all
  iterative; the tests build and query a chain of 50,000 nodes.
- **Deterministic.** Ids follow sorted names, lineage queries return ids in order, and the execution order depends
  only on the graph and the request.
- **Values are released when dead.** A step's `release` list drops every value no later step reads, unless it is a
  requested output.
- **Instruction counts are gated.** CI measures `build`, `prepare` and `run` under callgrind on chain, fan-in and
  layered graphs, and fails on a rise of more than 2%; see [Benchmarks](benchmarks.md#the-instruction-count-gate).
