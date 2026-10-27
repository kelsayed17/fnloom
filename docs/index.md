---
title: A Rust dataflow engine for plain Python functions
hide:
  - toc
---

<div class="fl-hero" markdown>

<div markdown>

# fnloom

**Plain Python functions are nodes, their parameter names are edges, and a Rust core plans and runs the graph.**
fnloom is a dataflow engine on [Apache Hamilton](https://github.com/apache/hamilton)'s model, rebuilt for speed,
determinism and fewer surprises.

[Get started](getting-started.md){ .md-button .md-button--primary }
[Coming from Hamilton](from-hamilton.md){ .md-button }
[:fontawesome-brands-github: GitHub](https://github.com/kelsayed17/fnloom){ .md-button }

</div>

</div>

<div class="grid cards" markdown>

-   :lucide-function-square:{ .lg .fl-icon } **Functions are the graph**

    ---

    A function's name is its node and its parameter names are its edges. No decorators, no registration, no
    configuration object: write the module, build the driver.

    [:lucide-arrow-right: Writing nodes](writing-nodes.md)

-   :lucide-zap:{ .lg .fl-icon } **Plan once, run many times**

    ---

    `prepare` compiles a request into a plan you keep; each run is a loop over its steps. A steady run of a
    1,000-node graph takes about a fifth of a millisecond.

    [:lucide-arrow-right: Running a graph](running.md) · [Benchmarks](benchmarks.md)

-   :lucide-list-ordered:{ .lg .fl-icon } **The same order every time**

    ---

    Nodes run in topological order with ties broken by name, whatever order you list the outputs in. Every listing is
    sorted.

    [:lucide-arrow-right: Execution order](running.md#execution-order)

-   :lucide-infinity:{ .lg .fl-icon } **Any depth, any cycle caught**

    ---

    Nothing recurses over graph depth, so a chain of 50,000 nodes builds and runs. A cycle is a `ValueError` naming
    it, raised when you build.

    [:lucide-arrow-right: Errors](errors.md)

-   :lucide-shield-check:{ .lg .fl-icon } **Mistakes are refused**

    ---

    A misspelled input or override, a bare string of outputs, an `async def` node: Hamilton accepts each silently,
    and fnloom raises naming the culprit.

    [:lucide-arrow-right: Differences from Hamilton](quirk-register.md)

-   :lucide-cpu:{ .lg .fl-icon } **A Rust core you can embed**

    ---

    `fnloom-core` has no dependencies and no `unsafe`. Its graph, planner and executor are generic over what a node
    runs.

    [:lucide-arrow-right: The Rust core](rust-core.md)

</div>

## A graph in one screen

A module of plain functions:

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

`price` and `quantity` are no function's name, so they are inputs; `discount` and `currency` have defaults, so they
are optional inputs. Build a driver from the module and ask for what you want:

```python
import fnloom
import pricing

driver = fnloom.build(pricing)
driver.execute(["label"], inputs={"price": 2.5, "quantity": 4})
# -> {'label': '10.00 EUR'}
driver.execute(["label"], inputs={"price": 2.5, "quantity": 4, "discount": 0.1})
# -> {'label': '9.00 EUR'}
```

!!! note "Status"

    fnloom is pre-release and not yet on PyPI: [build it from source](getting-started.md). Plan 1, the Rust core and
    the native Python path, is done. Decorators, configuration, adapters and async execution come in later plans; see
    [the design](design/2026-10-26-fnloom-design.md).
