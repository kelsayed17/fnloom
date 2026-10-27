# Contributing

## Setup

fnloom needs Python 3.14, [uv](https://docs.astral.sh/uv/) and a stable Rust toolchain with `clippy` and `rustfmt`
(see [rustup](https://rustup.rs/)).

```bash
uv sync
uv run maturin develop --features test-hooks
```

`maturin develop` builds the Rust extension `fnloom._core` into the environment. Run it again after changing Rust
code. `test-hooks` compiles in a deliberate panic that the binding tests use; release builds never enable it.

## Checks

One script runs every check, and CI runs the same one:

```bash
bash scripts/check.sh
```

It runs rustfmt, Clippy, rustdoc, the Rust tests, maturin, ruff, ty and pytest, in that order, and stops at the first
failure.

- **Lints are strict.** Clippy denies `pedantic` and the panic lints (`unwrap_used`, `expect_used`, `panic`); ruff
  selects every rule; ty treats every rule as an error. Fix the cause of a finding. When a suppression is the right
  call, it names its rule and gives its reason on the same line, as `# noqa: S101  # pytest tests are asserts` or
  `#[expect(clippy::panic, reason = "...")]`.
- **No recursion over graph depth.** Every walk is iterative, so a graph of any depth builds, plans and runs.
- **Deterministic everywhere.** Listings are sorted by node name, and execution order is topological with ties broken
  by name.
- **Docs are tested.** Every Python sample in `README.md` and `docs/` runs as part of the tests, and every link in
  them must resolve (`tests/test_docs.py`). The site builds with `--strict`, so a broken link or a page missing from
  the navigation fails CI too.

Preview the docs site with `uv run --group docs zensical serve`.

## Differences from Hamilton

Hamilton 1.90.0 is the behavioural oracle. `tests/differential/` runs each case through both runtimes:

- `test_semantics.py` asserts that both give the same values or the same exception class.
- `test_deliberate_differences.py` asserts each deliberate difference, on both sides, under
  `@pytest.mark.deliberate("Q<n>")`.

A new difference needs a row in [`docs/quirk-register.md`](docs/quirk-register.md) and a test marked with its id. The
marker fails collection when it names no row of the register.

## Benchmarks

`benches/` times fnloom against Hamilton by wall clock; see [the benchmarks page](docs/benchmarks.md).

CI gates the Rust core on instruction counts under callgrind, which are stable on shared runners where wall-clock time
is not. A rise of more than 2% over `crates/fnloom-core/benches/baseline.json` fails the `instruction-counts` job. When
a change costs instructions on purpose, regenerate the baseline in the job's environment (the `rust:1.99.1` image on
aarch64 Linux) with `scripts/bench_gate.py --write-baseline`, and say why in the commit message.

## Pull requests

Keep a pull request to one change, describe the behaviour it changes, and add an entry to `CHANGELOG.md` under
"Unreleased". Pull requests are squash-merged, so `main` stays linear; the pull request's title becomes the commit's
summary, written as `area: imperative summary`.
