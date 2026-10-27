# fnloom

A Rust implementation of the Hamilton dataflow model: plain Python functions are nodes, wired by parameter name into a
DAG, then compiled and executed by a Rust core. Python-facing, fast, typed, and deliberately free of Hamilton's quirks.
Intended for anyone to use, not tied to any one consumer.

The name is provisional, subject to a trademark knock-out. Licensed under Apache-2.0 (see `LICENSE`).

Not affiliated with or endorsed by the Apache Software Foundation or the Apache Hamilton project.

- `docs/design/` — design (draft).
- `docs/quirk-register.md` — the deliberate differences from Hamilton.
- `crates/fnloom-core/` — the core crate; empty so far.
