"""Generates benchmark graphs as modules, so both runtimes load the same code."""

import itertools
import sys
from contextlib import contextmanager
from types import ModuleType
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Generator

type Shape = Literal["chain", "fan_in", "layered", "rows"]
type Spelling = Literal["hamilton", "fnloom"]
type Request = tuple[list[str], dict[str, object]]

LAYER_WIDTH = 10
# Keeps layered values small however deep the graph is.
_MODULUS = 1_000_003
_counter = itertools.count()
_SEED = 7


def _chain(n: int) -> str:
    lines = ["def n0(seed: int) -> int:", "    return seed", ""]
    lines.extend(
        f"def n{i}(n{i - 1}: int) -> int:\n    return n{i - 1} + 1\n"
        for i in range(1, n)
    )
    return "\n".join(lines)


def _fan_in(n: int) -> str:
    params = ", ".join(f"x{i}: int" for i in range(n))
    terms = ", ".join(f"x{i}" for i in range(n))
    return f"def total({params}) -> int:\n    return sum(({terms},))\n"


def _layered_node(i: int) -> str:
    layer, column = divmod(i, LAYER_WIDTH)
    if layer == 0:
        return (
            f"def l0_{column}(seed: int) -> int:\n"
            f"    return (seed + {column}) % {_MODULUS}\n"
        )
    left = f"l{layer - 1}_{column}"
    right = f"l{layer - 1}_{(column + 1) % LAYER_WIDTH}"
    return (
        f"def l{layer}_{column}({left}: int, {right}: int) -> int:\n"
        f"    return ({left} + {right}) % {_MODULUS}\n"
    )


def _layered(n: int) -> str:
    return "\n".join(_layered_node(i) for i in range(n))


def _rows(n: int, spelling: Spelling) -> str:
    package = "hamilton.function_modifiers" if spelling == "hamilton" else "fnloom"
    rows = ",\n".join(
        f'    r{i}={{"prev": source("{"seed" if i == 0 else f"r{i - 1}"}")}}'
        for i in range(n)
    )
    return (
        f"from {package} import parameterize, source\n\n\n"
        f"@parameterize(\n{rows},\n)\n"
        "def step(prev: int) -> int:\n    return prev + 1\n"
    )


def make_module(shape: Shape, n: int, spelling: Spelling = "fnloom") -> ModuleType:
    """Returns a fresh module of `n` nodes (`n` parameters, for `fan_in`) in `shape`.

    Every function is annotated. `chain` is a line, `fan_in` one function over `n`
    inputs, `layered` rows of ten nodes each reading two nodes of the row above, and
    `rows` one function with `n` `parameterize` rows, each sourcing the row before
    (the first sources `seed`). Only `rows` differs by `spelling`: the decorators come
    from Hamilton or from fnloom.
    """
    if shape == "rows":
        source = _rows(n, spelling)
    else:
        source = {"chain": _chain, "fan_in": _fan_in, "layered": _layered}[shape](n)
    module = ModuleType(f"bench_{shape}_{n}_{next(_counter)}")
    # Hamilton finds a function's module through `sys.modules`.
    sys.modules[module.__name__] = module
    exec(compile(source, module.__name__, "exec"), vars(module))  # noqa: S102  # the source is generated above, from integers only
    return module


def discard(module: ModuleType) -> None:
    """Drops a generated module from `sys.modules`, so its functions can be freed."""
    sys.modules.pop(module.__name__, None)


@contextmanager
def generated(
    shape: Shape, n: int, spelling: Spelling = "fnloom"
) -> Generator[ModuleType]:
    """Yields `make_module(shape, n, spelling)`, discarded on exit, even on an error."""
    module = make_module(shape, n, spelling)
    try:
        yield module
    finally:
        discard(module)


def make_request(shape: Shape, n: int) -> Request:
    """Returns the output names and the inputs that exercise the whole graph."""
    if shape == "chain":
        return [f"n{n - 1}"], {"seed": _SEED}
    if shape == "fan_in":
        return ["total"], {f"x{i}": i for i in range(n)}
    if shape == "rows":
        return [f"r{n - 1}"], {"seed": _SEED}
    last_row = (n - 1) // LAYER_WIDTH
    outputs = [
        f"l{last_row}_{c}" for c in range(LAYER_WIDTH) if last_row * LAYER_WIDTH + c < n
    ]
    return outputs, {"seed": _SEED}


def make_expected(shape: Shape, n: int) -> dict[str, object]:
    """Returns the answer to `make_request(shape, n)`, computed by neither runtime."""
    if shape == "chain":
        return {f"n{n - 1}": _SEED + n - 1}
    if shape == "fan_in":
        return {"total": n * (n - 1) // 2}
    if shape == "rows":
        return {f"r{n - 1}": _SEED + n}
    row = [(_SEED + c) % _MODULUS for c in range(LAYER_WIDTH)]
    for _ in range((n - 1) // LAYER_WIDTH):
        row = [
            (row[c] + row[(c + 1) % LAYER_WIDTH]) % _MODULUS for c in range(LAYER_WIDTH)
        ]
    last_row = (n - 1) // LAYER_WIDTH
    # A partial last row holds only the columns `make_request` asks for.
    return {
        f"l{last_row}_{c}": value
        for c, value in enumerate(row)
        if last_row * LAYER_WIDTH + c < n
    }
