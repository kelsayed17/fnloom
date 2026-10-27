<h1 align="center">fnloom</h1>

<p align="center">
  <strong>Plain Python functions, woven into a dataflow graph by a Rust core.</strong><br>
  A function's name is its node and its parameter names are its edges, as in
  <a href="https://github.com/apache/hamilton">Apache Hamilton</a>, with a planner and executor rebuilt in Rust.
</p>

<p align="center">
  <a href="https://github.com/kelsayed17/fnloom/actions/workflows/ci.yml"><img src="https://github.com/kelsayed17/fnloom/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI"></a>
  <a href="https://kelsayed17.github.io/fnloom/"><img src="https://img.shields.io/badge/docs-kelsayed17.github.io%2Ffnloom-5b4bc4" alt="Docs"></a>
  <img src="https://img.shields.io/badge/python-3.14%20%7C%203.14t-1e1b4b" alt="Python 3.14 and 3.14t">
  <img src="https://img.shields.io/badge/status-pre--alpha-f59e0b" alt="Status: pre-alpha">
  <a href="https://github.com/kelsayed17/fnloom/blob/main/LICENSE"><img src="https://img.shields.io/badge/licence-Apache--2.0-blue" alt="Licence: Apache-2.0"></a>
</p>

<p align="center">
  <a href="https://kelsayed17.github.io/fnloom/"><b>Documentation</b></a> ·
  <a href="https://kelsayed17.github.io/fnloom/getting-started/"><b>Getting started</b></a> ·
  <a href="https://kelsayed17.github.io/fnloom/from-hamilton/"><b>Coming from Hamilton</b></a> ·
  <a href="https://kelsayed17.github.io/fnloom/benchmarks/"><b>Benchmarks</b></a> ·
  <a href="https://github.com/kelsayed17/fnloom/blob/main/CHANGELOG.md"><b>Changelog</b></a>
</p>

## Why fnloom

### 🧵 Functions are the graph

No decorators, no registration, no configuration object. Write a module of typed functions; fnloom reads the graph
from their names and parameters.

### ⚡ Plan once, run many times

`prepare` compiles a request into a plan you keep, and each run is a loop over its steps in Rust. A steady run of a
1,000-node graph takes about a fifth of a millisecond.

### 🎯 Deterministic, and strict about mistakes

Nodes run in topological order with ties broken by name, on every run. A misspelled input, a cycle or an `async def`
node raises a `ValueError` naming it, where Hamilton accepts or overflows.

## A graph in one screen

```python
# file: pricing.py
def total(price: float, quantity: int) -> float:
    """What the line costs before any discount."""
    return price * quantity


def discounted(total: float, discount: float = 0.0) -> float:
    """The total after the discount, if one is given."""
    return total * (1 - discount)


def label(discounted: float, currency: str = "EUR") -> str:
    """The price as shown to a customer."""
    return f"{discounted:.2f} {currency}"
```

`price` and `quantity` name no function, so they are inputs; `discount` and `currency` have defaults, so they are
optional. Build a driver and ask for what you need:

```python
import fnloom
import pricing

driver = fnloom.build(pricing)
driver.execute(["label"], inputs={"price": 2.5, "quantity": 4})
# -> {'label': '10.00 EUR'}

plan = driver.prepare(["label"], inputs={"price": 0.0, "quantity": 0})  # compiled once
plan.run(inputs={"price": 4.0, "quantity": 3})
# -> {'label': '12.00 EUR'}
```

The [guides](https://kelsayed17.github.io/fnloom/writing-nodes/) cover which functions become nodes, overrides,
prepared plans, lineage and every error.

## How it differs from Hamilton

Hamilton 1.90.0 is fnloom's test oracle: each case runs through both, and each deliberate difference is
[a row in the register](https://kelsayed17.github.io/fnloom/quirk-register/) with a test asserting both behaviours.

| | Hamilton 1.90.0 | fnloom |
| --- | --- | --- |
| A chain of 1,000 nodes | `RecursionError` | Runs; a 50,000-node chain takes 0.6 s to build and run |
| A dependency cycle | `RecursionError` when executed | `ValueError` naming the cycle, at build |
| A misspelled input or override | Silently ignored | `ValueError` naming it |
| Execution order | Follows the order of outputs and parameters | Topological, ties broken by name |
| Repeating a request | Re-planned every call | `prepare` once, `run` many times |

## Performance

Milliseconds on an Apple M2 Pro, Hamilton / fnloom, from [the benchmarks](https://kelsayed17.github.io/fnloom/benchmarks/):

| Graph | Build | Steady execute |
| --- | --- | --- |
| layered, 1,000 nodes | 28.8 / 13.1 | 5.185 / 0.227 |
| fan-in, 1,000 parameters | 3.638 / 2.455 | 3.386 / 0.638 |
| chain, 10,000 nodes | 249.2 / 109.1 | `RecursionError` / 1.688 |

## Install

fnloom is not on PyPI yet. Build it from source with Python 3.14 or newer, [uv](https://docs.astral.sh/uv/) and a
stable Rust toolchain:

```bash
git clone https://github.com/kelsayed17/fnloom.git
cd fnloom
uv sync
uv run maturin develop --release
```

## Status

Pre-alpha. Plan 1, the Rust core and the native Python path, is done. Decorators and configuration, adapters and
async execution, and visualisation follow in later plans; see the
[design](https://kelsayed17.github.io/fnloom/design/2026-10-26-fnloom-design/). The name is provisional, subject to a
trademark check before the first release.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the setup and the one check script. Report security problems privately, as
[SECURITY.md](SECURITY.md) describes.

Licensed under [Apache-2.0](LICENSE). Not affiliated with or endorsed by the Apache Software Foundation or the Apache
Hamilton project.
