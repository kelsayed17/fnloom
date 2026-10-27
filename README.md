# fnloom

A Rust implementation of the Hamilton dataflow model: plain Python functions are nodes, wired by parameter name into a
DAG, then compiled and executed by a Rust core. Python-facing, fast, typed, and deliberately free of Hamilton's quirks.

The name is provisional, subject to a trademark knock-out. Licensed under Apache-2.0 (see `LICENSE`).

Not affiliated with or endorsed by the Apache Software Foundation or the Apache Hamilton project.

- `crates/fnloom-core/` — the Rust core: node IR, graph queries, plan compiler and executor, with no runtime dependencies.
- `crates/fnloom-py/` — the PyO3 binding, built as the extension module `fnloom._core`.
- `python/fnloom/` — the Python front end: `build`, `Builder`, `Driver` and `PreparedPlan`.
- `tests/` — Python tests, including differential tests that run each case through Hamilton 1.90.0 as well.
- `benches/` — timings against Hamilton on generated graphs; the core's instruction-count gate is in
  `crates/fnloom-core/benches/` and `scripts/bench_gate.py`.
- `docs/design/` — the design.
- `docs/quirk-register.md` — the deliberate differences from Hamilton.
- `scripts/check.sh` — the one local gate; CI runs the same steps.
