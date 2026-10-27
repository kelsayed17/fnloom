# fnloom — design

**Status:** v2. Approved by the operator 2026-10-26 (v1), revised the same day after an audit (§12). Nothing in this
document is built. Facts about Hamilton were read in the `apache-hamilton 1.90.0` wheel (source-read, not run)
unless marked otherwise; `docs/quirk-register.md` says which behaviours are still unverified.

## 1. Intent

fnloom is a dataflow engine built on Hamilton's model, with its own native API and a Rust core. Plain Python
functions are nodes, their dependencies are edges, a set of modules is compiled into a DAG, and a driver executes
the part of it that the requested outputs need.

The user writes Python. The graph engine (graph model, validation, planning, execution scheduling) is Rust, shipped
as a Python wheel. The reasons, as an earlier investigation of Hamilton found them: per-call re-planning overhead,
no typing for decorated functions, a heavy dependency set, and an executor that cannot be reasoned about or reused
outside Python.

**Direction (operator, 2026-10-26).**
- fnloom will be open source, Apache-2.0. Nothing consumer-specific lives in this repository.
- It must be **fast, stable and superior to Hamilton**.
- There is **no Hamilton compatibility layer**. Existing Hamilton code is not expected to run unchanged; consumers
  adopt the native API.
- A later, separate open-source indicator library will be built on fnloom, with TA-Lib as its oracle, aiming to beat
  TA-Lib on speed (§11).

## 2. Goals, non-goals, anti-goals

**Goals**
1. **Proven behaviour.** Where fnloom keeps a Hamilton behaviour, it produces the same values and the same exception
   classes, proven differentially against Hamilton 1.90.0 (§7). Where it deliberately differs, the difference is a
   register row with a test.
2. **Typed and light.** A `py.typed` package with no required dependency beyond the interpreter (numpy optional).
3. **Compile once, execute many.** Validation and planning happen at build time or in an explicit `prepare()`, not
   on every `execute`.
4. **Fast, measured continuously** (§3).
5. **Stable.** Semantic versioning with the public API frozen at 1.0; the Hamilton baseline is pinned (§10); a Rust
   panic never crosses the FFI boundary except as a Python exception; documented minimum Rust and Python versions;
   no `unsafe` in the core without a written justification.
6. **Superior**, meaning: the capabilities in §5, every fixed quirk in §6, a typed package, a public
   graph-inspection API, reusable plans, and no recursion limit on graph depth.
7. **Open to non-Python nodes.** The core is generic over its node payload and its value type, so Rust-native nodes
   (§11) can be added without changing the core.

**Non-goals (v1)**
- Hamilton's plugin ecosystem (dask, ray, spark, pandera, mlflow, …), UI/tracker integration, materializers,
  caching, dynamic execution (`Parallelizable`/`Collect`). Dynamic execution is the first deferral to design next.
- Faster Python node bodies. A Python node still runs at Python speed.

**Anti-goals** — the project fails even if every test passes if:
- Hamilton code is copied wholesale, or any copied code lacks its Apache-2.0 attribution (§8).
- A behaviour differs from Hamilton without a register row and a test.

## 3. What Rust buys, and how speed is measured

Nodes are Python callables, so for a graph of heavy nodes the Python calls dominate and Rust changes little. What
the Rust core changes:
- Graph build, cycle detection, ordering, upstream/downstream queries and planning leave Python.
- A prepared plan removes the per-`execute` re-walk. An earlier investigation reported a ~0.02 ms floor per call and
  a 3.7–4.3x gain on scalar graphs, on one consumer's workload; not re-measured for a general one.
- Later: parallel scheduling on free-threaded Python 3.14t (§8) and nodes that are not Python at all (§11).

**No go/no-go gate (operator, 2026-10-26): this is a full rebuild.** The whole v1 scope (§5) is built regardless.
Speed is measured from the first commit:
- **Report.** Build, `prepare`, first-execute and steady-execute wall-clock medians against Hamilton 1.90.0, on
  generated graph shapes (chain, fan-in, layered) at 10, 100, 1,000 and 10,000 nodes, plus Hamilton example graphs
  once they are translated (§7). Published per release with the machine description.
- **Gate.** A regression check in CI on **instruction counts** of the Rust core (callgrind-based, Linux), not on
  wall-clock time, which varies too much on shared runners to gate on.
- A row where fnloom is slower than Hamilton is a bug to fix, not a reason to stop.

## 4. Architecture

Three layers; the boundary between the first two is the IR.

1. **Python front end** (`fnloom`): reads functions, signatures, annotations (`annotationlib`, including forward references) and
   decorators, resolves config, and lowers everything to node definitions. It must be Python because it introspects
   live objects at import time. It owns anything that needs Python's type system, notably the edge and input type
   checks (§10), which reach the core as a callback run at build time.
2. **IR** (`fnloom-core`): in v1 a node is `{ name, kind (function | input), inputs: [ { name, dependency
   (required | optional) } ], tags, payload }`. Defaults are not in the IR: an optional input that is not supplied is
   omitted from the call, and Python applies the function's own default. The IR grows only with the feature that needs
   it: `namespace` with `@subdag`, `expand`/`collect` kinds with dynamic execution, mixed payloads with Rust nodes.
3. **Rust core** (`fnloom-core`): graph construction, name and cycle validation, ordering, upstream and downstream
   queries, planning for (outputs, supplied input names, override names), and execution through an `Evaluate`
   trait and `Observer` hooks. It is generic over payload `P` and value `V`. `fnloom-py` is the PyO3 binding that
   instantiates both as Python objects.

**Execution semantics.** An override replaces a node and cuts its upstream; an optional input that is not supplied
is skipped; a required input that is not supplied raises; an intermediate value is dropped after its last use.
**Order:** nodes run in topological order with ties broken by node name, independent of the order of the requested
outputs. This differs from Hamilton's recursive depth-first walk, and is visible to hooks and side effects, so it is
register row Q26.

**Async.** Planning stays in Rust. The async driver is Python: it walks the prepared plan's steps and awaits
coroutine nodes, so the Rust core never has to drive an event loop. Construction is always synchronous (Q12).

Repo layout: `crates/fnloom-core`, `crates/fnloom-py`, `python/fnloom/`, `tests/` (with `tests/differential/`,
Hamilton in the test group only), `benches/`, `docs/`.

## 5. Scope of v1, as capabilities

Wiring is by name (§9). The entry point and the decorators keep Hamilton's names and shapes so the API feels like
Hamilton (operator, 2026-10-26): `Builder().with_modules(...).with_config(...).build()` (with `fnloom.build(...)` as a
shortcut), `parameterize`, `source`, `value`, `tag`, `config.when*`, `extract_fields`, `extract_columns`. Same name
means same intent, not identical behaviour: every difference is a register row. Hamilton's equivalent is given for orientation only.

| Capability | Hamilton equivalent |
|---|---|
| Functions as nodes, dependency wiring, optional dependencies | function name / parameter names / defaults |
| Build a graph from modules, with config | `Builder().with_modules().with_config()` |
| Execute requested outputs with inputs and overrides; reusable prepared plans | `Driver.execute` (no prepared plans) |
| Graph inspection: list nodes, upstream, downstream, between, input validation | `list_available_variables`, `what_is_*`, `validate_inputs` |
| One function producing several nodes from parameter sets | `@parameterize`, `source()`, `value()` |
| Tags readable through inspection | `@tag` |
| Config-driven selection of implementations | `@config.when*` |
| One function producing several outputs from a result | `@extract_columns`, `@extract_fields` |
| Edge type checks at build; input type checks on by default, with an opt-out | default checks, `NoEdgeAndInputTypeChecking` |
| Observer hooks (pre/post node, pre/post graph) and a result builder | lifecycle adapters, `do_build_result` |
| Async execution | `AsyncDriver` |
| DOT text output (graphviz optional) | `display_all_functions` |

v1.x, in this order: execution visualisation, output validators, parameterize sugar, step injection around a node
(`@mutate`/`@pipe_*`), dependency injection by config (`@inject`/`@resolve`).

## 6. Where fnloom differs from Hamilton

**Settled by the operator (2026-10-26): fnloom fixes Hamilton's quirks by default.** It keeps Hamilton's behaviour
where Hamilton is right, and deliberately differs where Hamilton is wrong or silently accepts a mistake. A deviation
needs a reason that holds for any user, and each one is a row in `docs/quirk-register.md` with a test that is red
against Hamilton and green against fnloom.

| Rows | Treatment |
|---|---|
| Q1, Q2, Q12, Q13, Q14, Q15, Q19, Q20 (typing, inspection API, sync construction, deprecated asyncio call, autoload, banner, result type, dependency weight) | FIX or DROP: defects or fat for any user. |
| Q3 (re-plan per execute) | FIX: reusable plans. |
| Q4, Q5, Q6 (unknown input names, misspelled overrides, bare `str` as outputs) | REFUSE, always; no `strict` flag. |
| Q7, Q21, Q22, Q23 | FIX: pass-through overrides, additive custom checks, type-checked overrides, every hook runs before errors are raised. |
| Q8, Q11, Q26 | FIX: deterministic order everywhere, dict results everywhere, name-tie-broken topological execution. |
| Q10 | Edge checks always on; input checks on by default with an explicit opt-out. |
| Q16 | DEFER with caching. |
| Q24 | KEEP a single result builder; hooks compose. |
| Q9, Q17, Q18 (numeric widening, array shape/dtype contracts, length alignment) | Out of core: found against one consumer's numpy workloads, not Hamilton in general. Candidates for an opt-in policy, likely needed by the indicator library (§11). |

## 7. Proof

1. **Differential tests.** Each behaviour is exercised by a program run under Hamilton 1.90.0 and under fnloom, with
   values (bitwise where the types allow) and exception classes compared. For plain-function programs both runtimes
   load the same module. For decorated programs the test carries the Hamilton spelling and the fnloom spelling side
   by side; the decorator translation lives in test code, never in the product. With name-wiring (§9), every plain-function
   program is shared as is.
2. **A test per deliberate difference**, red against Hamilton and green against fnloom.
3. **Hamilton's own tests and examples** as a corpus: run from the pinned upstream wheel, never vendored; fnloom
   versions are translations written here. Apache-2.0 permits this; any test text adapted closely enough to count as
   copied carries attribution (§8).
4. **Generated programs** (hypothesis) over dependency, default, override and config shapes, compared across
   runtimes.
5. **Benchmarks** as in §3.

## 8. Constraints

- **Copying.** Hamilton's source may be read to understand behaviour. The implementation is written independently;
  wholesale copying is out. A narrow, self-contained piece may be copied case by case, with the Apache-2.0 notice, a
  `NOTICE` entry and a modification mark.
- **Naming.** No distribution, module or crate name contains "Hamilton" or "Apache"; no `hamilton` module ships.
  `fnloom` is subject to a trademark knock-out before the first public release; nothing is reserved until then.
- **Open source.** Everything in the repository must be publishable: no consumer-private code, data, paths or
  decisions. The repository was re-created from its tree when it went public, so superseded commits do not exist.
- **Licence:** Apache-2.0.
- **Python 3.14 or later (operator, 2026-10-26).** The front end reads annotations through `annotationlib`
  (deferred evaluation, PEP 649/749), and both the default and the free-threaded (`3.14t`) interpreters are build
  targets.
- **Wheels:** per-interpreter builds, not the stable ABI (`abi3`). As understood when this was written, `abi3`
  cannot target free-threaded CPython, which is the route to running Python nodes in parallel. Re-check PyO3's
  current documentation before the first release.
- **Clean code, enforced (operator, 2026-10-26).** Python passes ty in strict mode (every rule an error) and ruff
  with every rule selected; Rust passes Clippy with `pedantic` and the panic-prone restriction lints denied, plus
  `missing_docs`. No suppression without a stated reason, no dead or commented-out code, every public item
  documented. The exact configuration lives in the plans and in `pyproject.toml` / `Cargo.toml`.
- **Rust edition 2024**, with `rust-version` set to the oldest toolchain CI verifies.

## 9. Decided: authoring API

**Name-wiring (operator, 2026-10-26).** A function's name is its node and its parameter names are its edges, as in
Hamilton; decorators cover everything else (§5). Plain-function programs therefore run unchanged under both runtimes
in the differential tests, and only decorated programs need a fnloom spelling beside the Hamilton one. No questions
are open.

## 10. Calls made in this design (change any of them)

- **Package structure and API:** designed for fnloom, not mirroring Hamilton's names or paths (operator,
  2026-10-26).
- **Coverage:** phased from §5 toward Hamilton's useful core; the plugin ecosystem is out.
- **Type checks:** behaviour-ported in the Python front end, agreeing with Hamilton 1.90.0 on a stated matrix of type
  forms (generics, `Union`/`Optional`, `Literal`, `Annotated`, `Protocol`, subclass relations). Pandas types are out of
  core; forms outside the matrix are documented as unsupported.
- **Hooks:** the executor has pre/post-node and pre/post-graph seams from the first commit; every observer runs for
  an event, then the errors are raised together, keeping their original exception objects (Q23). The rest of the
  hook API arrives with the adapters plan.
- **Hamilton baseline:** pinned at 1.90.0; newer releases are followed only by an explicit decision, with a
  report-only canary against the newest one.
- **No go/no-go gate** (operator); instruction-count regression gate in CI (§3).

## 11. Downstream: the indicator library (not part of fnloom)

A separate open-source library, built on fnloom, with TA-Lib as its oracle and speed as its claim. It is not
designed here; this section lists what fnloom must not rule out, so that v1 choices leave room for it:
- **Mixed graphs.** A Python-driven graph whose nodes are a mix of Python and Rust. The core is already generic over
  `P` and `V`; the binding's Python-only payload and value types are the part that will change.
- **A value type that keeps arrays out of Python** between two Rust nodes (a zero-copy array or buffer value).
- **Streaming and incremental state.** Indicators are often recursive (EMA-style) or windowed, and TA-Lib offers a
  streaming API. A plan that can be stepped bar by bar, with per-node state, is a likely requirement.
- **Array contracts** (shape, dtype, length alignment): the Q9/Q17/Q18 family, as an opt-in policy.
Speed against TA-Lib, which is C, will come from batching, parallelism, SIMD and streaming rather than from the
language: an earlier Rust port of numba kernels showed no compute speedup between two LLVM backends.

## 12. Change log

- **v1 (2026-10-26):** approved.
- **v2 (2026-10-26), after an audit:** scope rewritten as capabilities; compatibility-layer remnants and references
  to the deleted contract removed; anti-goal on copying aligned with §8; execution order made explicit (Q26); async
  model added; IR reduced to what v1 needs; numeric widening removed from the front end's duties; Python floor
  3.10 → 3.14 (operator); `abi3` dropped; edition 2024; benchmark gate moved to instruction counts; indicator-library
  requirements recorded; authoring API decided: name-wiring.
