#!/usr/bin/env bash
# The one gate: every step is blocking, and CI runs the same steps.
set -euo pipefail
cd "$(dirname "$0")/.."

rustup component add clippy rustfmt >/dev/null
# Cargo's PyO3 build script reads this interpreter's version and config, so the Rust steps
# check against the same Python that maturin builds the extension for.
PYO3_PYTHON="$(uv run python -c 'import sys; print(sys.executable)')"
export PYO3_PYTHON

cargo fmt --check
# test-hooks on, so the feature-gated code is linted too.
cargo clippy --workspace --all-targets --features test-hooks -- -D warnings
RUSTDOCFLAGS="-D warnings" cargo doc --workspace --no-deps
cargo test --workspace
# test-hooks exposes the deliberate-panic hook; release builds never enable it.
uv run maturin develop --features test-hooks
# No more syncing from here on: a sync would reinstall the project without test-hooks.
export UV_NO_SYNC=1
uv run ruff check
uv run ruff format --check
uv run ty check
uv run pytest
