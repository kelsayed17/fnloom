"""Decorator order: every pair of Plan 8 decorators gives one graph either way round.

Each Plan 8 decorator is stacked with each other one, and with `@tag`,
`@parameterize`, `@config.when`, `@config.matches`, `@cache` and a step
namespace, in both orders, with and without a `@mutate` step on the function;
each is also stacked with `@extract_fields`. Both orders must build the same
nodes, wired the same way, tagged the same, computing the same values with the
same check results, or both must be refused with the same error. The roadmap's
stacking row asks this. Each pair is also built with the function in a block,
reading an expand's items through a node wrapper, so it runs once per item.
"""

import itertools
from typing import TYPE_CHECKING, override

import pytest

import fnloom
from helpers import make_module

if TYPE_CHECKING:
    from collections.abc import Callable

PLAN_8 = {
    "check_output": "check_output(range=(0, 5), importance='warn')",
    "check_output_custom": "check_output_custom(_small, importance='warn')",
    "pipe_input": "pipe_input(step(_inc))",
    "pipe_output": "pipe_output(step(_inc).named('out'))",
    "inject": "inject(k=value(3))",
    "resolve": "resolve(lambda: tag(resolved='1'))",
}
OTHERS = {
    "tag": "tag(team='t')",
    "parameterize": "parameterize(r1={'k': value(2)}, r2={'k': value(5)})",
    "config.when": "config.when(mode='a')",
    "config.matches": "config.matches(lambda mode: mode == 'a')",
    "cache": "cache(behavior='recompute')",
    "step_namespace": "pipe_output(step(_inc).named('out'), namespace='fmt')",
}

HEADER = """
from fnloom import (
    Collect, Parallelizable, cache, check_output, check_output_custom, config,
    extract_fields, inject, mutate, parameterize, pipe_input, pipe_output, resolve,
    source, step, tag, value,
)

def _small(v: int) -> bool:
    return v < 5

def _inc(v: int) -> int:
    return v + 1
"""

MUTATOR = """
@mutate(f__a)
def _plus(v: int) -> int:
    return v + 2
"""

# In a block: `x` expands, so the decorated function runs once per item.
EXPAND = """
def x(n: int) -> Parallelizable[int]:
    yield from (n, n + 1)
"""


class _Checks(fnloom.Hook):
    """Each node's check results, as names and outcomes, whatever their order."""

    def __init__(self) -> None:
        """Starts with no results."""
        self.found: dict[tuple[str, tuple[int, ...]], list[tuple[str, bool]]] = {}

    @override
    def after_node(self, event: fnloom.NodeEnd) -> None:
        """Keeps the node's check results."""
        if event.checks:
            # Sorted: two check decorators run in the order written, by design.
            self.found[event.node.name, event.branch] = sorted(
                (c.name, c.passes) for c in event.checks
            )


def _through(
    _node: fnloom.NodeView, call: Callable[[], object], _inputs: object
) -> object:
    """A node wrapper that changes nothing."""
    return call()


def _collected(builder: fnloom.Builder, source: str) -> str:
    """`source` with a `Collect` of each block node nothing reads, to request it."""
    driver = builder.with_modules(make_module("uncollected", source)).build()
    read = {name for view in driver.nodes.values() for name in view.dependencies}
    return source + "".join(
        f"\ndef all_{name}({name}: Collect[int]) -> list[int]:\n    return {name}\n"
        for name, view in driver.nodes.items()
        if view.block is not None and name not in read
    )


def _outcome(source: str, name: str, *, blocked: bool) -> object:
    """The graph's shape, tags, values and check results, or its refusal."""
    checks = _Checks()
    builder = fnloom.Builder().with_config({"mode": "a"}).with_hooks(checks)
    try:
        if blocked:
            builder = builder.with_node_wrappers(_through)
            source = _collected(builder, source + EXPAND)
        driver = builder.with_modules(make_module(name, source)).build()
    except (TypeError, ValueError) as error:
        return f"{type(error).__name__}: {error}"
    computed = [
        n
        for n, view in driver.nodes.items()
        if not view.is_input and view.block is None
    ]
    shape = {n: driver.nodes[n].dependencies for n in driver.node_names()}
    tags = {n: dict(v.tags) for n, v in driver.nodes.items() if not v.is_input}
    values = driver.execute(computed, inputs={"n" if blocked else "x": 3})
    return shape, tags, values, checks.found


def _graph(first: str, second: str, *, mutated: bool, blocked: bool) -> object:
    source = f"{HEADER}\n@{first}\n@{second}\ndef f__a(x: int, k: int = 1) -> int:\n"
    source += "    return x * k\n"
    if mutated:
        source += MUTATOR
    return _outcome(source, "stacked", blocked=blocked)


PAIRS = [
    (a, b)
    for a, b in itertools.combinations([*PLAN_8, *OTHERS], 2)
    if a in PLAN_8 or b in PLAN_8
]


@pytest.mark.parametrize("blocked", [False, True], ids=["outside", "in_block"])
@pytest.mark.parametrize("mutated", [False, True], ids=["plain", "mutated"])
@pytest.mark.parametrize(("a", "b"), PAIRS, ids=[f"{a}+{b}" for a, b in PAIRS])
def test_a_pair_gives_one_graph_in_either_order(
    a: str, b: str, *, mutated: bool, blocked: bool
) -> None:
    """Both orders: the same nodes, wiring, tags, values and checks, or one refusal."""
    decorators = {**PLAN_8, **OTHERS}
    one = _graph(decorators[a], decorators[b], mutated=mutated, blocked=blocked)
    other = _graph(decorators[b], decorators[a], mutated=mutated, blocked=blocked)
    assert one == other


def test_only_two_pairs_are_refused() -> None:
    """Every other pair builds, in a block or not; those are refused in both orders.

    `@inject` can't bind what a row binds, and a function has one `@pipe_output`.
    """
    decorators = {**PLAN_8, **OTHERS}
    refused = [
        (a, b)
        for blocked in (False, True)
        for a, b in PAIRS
        if isinstance(
            _graph(decorators[a], decorators[b], mutated=False, blocked=blocked), str
        )
    ]
    assert (
        refused == [("pipe_output", "step_namespace"), ("inject", "parameterize")] * 2
    )


ON_FIELDS = {
    "check_output": "check_output(range=(0, 5), field='p', importance='warn')",
    "check_output_custom": "check_output_custom(_small, field='p', importance='warn')",
    "pipe_input": "pipe_input(step(_inc))",
    "pipe_output": "pipe_output(step(_inc).on_output('p'))",
    "inject": "inject(k=value(3))",
    "resolve": "resolve(lambda: tag(resolved='1'))",
    "cache": "cache(fields={'p': 'recompute'})",
}


@pytest.mark.parametrize("blocked", [False, True], ids=["outside", "in_block"])
@pytest.mark.parametrize("decorator", ON_FIELDS)
def test_with_extract_fields_in_either_order(decorator: str, *, blocked: bool) -> None:
    """Each Plan 8 decorator and `@cache(fields=)`, above or below `@extract_fields`."""
    fields = "extract_fields({'p': int, 'q': int})"
    results = [
        _outcome(
            f"{HEADER}\n@{top}\n@{bottom}\ndef d(x: int, k: int = 1) -> dict:\n"
            "    return {'p': x * k, 'q': x}\n",
            "fields_stacked",
            blocked=blocked,
        )
        for top, bottom in (
            (ON_FIELDS[decorator], fields),
            (fields, ON_FIELDS[decorator]),
        )
    ]
    assert not isinstance(results[0], str), results[0]
    assert results[0] == results[1]
