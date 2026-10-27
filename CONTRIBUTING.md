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

It runs rustfmt, Clippy, rustdoc, the Rust tests, maturin, ruff, rumdl (Markdown lint), ty and pytest, in that order,
and stops at the first failure.

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

Apache Hamilton 1.90.0 is the behavioural oracle. `tests/differential/` runs each case through both runtimes:

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

## Releases

A pushed `vX.Y.Z` tag runs `.github/workflows/release.yml`. It builds a wheel per interpreter (CPython 3.14 and the
free-threaded 3.14t; no abi3, which cannot target free-threaded builds) for manylinux and musllinux on x86_64 and
aarch64, macOS on arm64 and x86_64, and Windows on x86_64, plus the sdist. Every wheel is installed into a clean
environment and imported (`scripts/smoke_wheel.py`), and a wheel built from the sdist alone must pass the same test.
Then the `publish` job uploads everything to PyPI through trusted publishing: no token is stored anywhere. A pull
request that changes the workflow builds and tests the same set without publishing.

To release:

1. Set `version` under `[workspace.package]` in `Cargo.toml`. It is the only place the version lives: maturin reads
   it for the package, and `core_version()` compiles it in.
2. Move the "Unreleased" entries in `CHANGELOG.md` under a heading for the new version.
3. Merge that change to `main`, then tag the merge commit and push the tag:

    ```bash
    git tag -a vX.Y.Z -m "fnloom X.Y.Z"
    git push origin vX.Y.Z
    ```

The workflow fails before publishing if the tag is not `v` followed by the Cargo version, or if any wheel's metadata
version differs from its `core_version()`.

Before the first release, the maintainer sets up trusted publishing once:

1. On PyPI, under **Your account → Publishing**, add a pending trusted publisher (it creates the `fnloom` project on
   the first upload): owner `kelsayed17`, repository `fnloom`, workflow `release.yml`, environment `pypi`.
2. On GitHub, under **Settings → Environments**, create the `pypi` environment. Limit its deployment tags to `v*`, and
   add yourself as a required reviewer so every upload waits for an approval.
