# Getting started

## Install from source

fnloom is not on PyPI yet. Building it needs Python 3.14 or newer (the free-threaded `3.14t` works too),
[uv](https://docs.astral.sh/uv/) and a stable Rust toolchain from [rustup](https://rustup.rs/).

```bash
git clone https://github.com/kelsayed17/fnloom.git
cd fnloom
uv sync
uv run maturin develop --release
```

`maturin develop --release` compiles the Rust extension `fnloom._core` with optimisations and installs fnloom into
the project's environment. Leave out `--release` for a faster build of a slower extension, as when working on fnloom
itself.

## Your first graph

Write the computation as plain functions in a module. Each function is a node named after itself, and each parameter
reads the node of the same name:

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

`width` and `height` are no function's name, so fnloom makes them inputs. Build a driver and execute what you need:

```python
import fnloom
import shapes

driver = fnloom.build(shapes)
driver.node_names()
# -> ['area', 'aspect', 'height', 'perimeter', 'summary', 'width']
driver.execute(["summary"], inputs={"width": 4.0, "height": 2.0})
# -> {'summary': 'area 8, perimeter 12, aspect 2'}
```

Only what the outputs need runs: asking for `area` alone does not call `perimeter` or `aspect`.

```python
driver.execute(["area"], inputs={"width": 4.0, "height": 2.0})
# -> {'area': 8.0}
```

## Next

- [Writing nodes](writing-nodes.md): which functions become nodes, optional inputs, and what is refused.
- [Running a graph](running.md): inputs, overrides, prepared plans and lineage.
- [Errors](errors.md): what each mistake raises.
- [Coming from Hamilton](from-hamilton.md): what carries over and what changes.
