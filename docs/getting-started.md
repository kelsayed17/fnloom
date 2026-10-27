# Getting started

This page takes about five minutes: install fnloom, write a small graph and run it.

## Install from source

fnloom isn't on PyPI yet, so you build it from a clone. You need:

- Python 3.14 or newer (the free-threaded `3.14t` works too)
- [uv](https://docs.astral.sh/uv/)
- a stable Rust toolchain from [rustup](https://rustup.rs/)

```bash
git clone https://github.com/kelsayed17/fnloom.git
cd fnloom
uv sync
uv run maturin develop --release
```

The last command compiles the Rust extension, `fnloom._core`, with optimisations and installs fnloom into the
project's environment.

!!! tip "Working on fnloom itself?"

    Leave out `--release`. The build is faster, though the extension it makes is slower.

## Your first graph

Write your computation as plain functions in a module. Each function becomes a node with the same name, and each
parameter reads the node it's named after:

```python
# file: shapes.py
def area(width: float, height: float) -> float:
    """The rectangle's area."""
    return width * height


def perimeter(width: float, height: float) -> float:
    """The length of the rectangle's edge."""
    return 2 * (width + height)


def aspect(width: float, height: float) -> float:
    """How much wider than tall the rectangle is."""
    return width / height


def summary(area: float, perimeter: float, aspect: float) -> str:
    """One line describing the rectangle."""
    return f"area {area:g}, perimeter {perimeter:g}, aspect {aspect:g}"
```

No function is called `width` or `height`, so fnloom makes them inputs. Build a driver and run what you need:

```python
import fnloom
import shapes

driver = fnloom.build(shapes)
driver.node_names()
# -> ['area', 'aspect', 'height', 'perimeter', 'summary', 'width']
driver.execute(["summary"], inputs={"width": 4.0, "height": 2.0})
# -> {'summary': 'area 8, perimeter 12, aspect 2'}
```

## Only what you ask for runs

fnloom runs only the functions your outputs depend on. Ask for `area` alone, and `perimeter` and `aspect` are never
called:

```python
driver.execute(["area"], inputs={"width": 4.0, "height": 2.0})
# -> {'area': 8.0}
```

## Where to next

<div class="grid cards" markdown>

-   **[Writing nodes](writing-nodes.md)**

    Which functions become nodes, optional inputs, `@parameterize`, and what's refused.

-   **[Running a graph](running.md)**

    Inputs, overrides, prepared plans, type checks and lineage.

-   **[Errors](errors.md)**

    Every mistake fnloom catches, and the message it gives.

-   **[Coming from Hamilton](from-hamilton.md)**

    What carries over, and what changes in your code.

</div>
