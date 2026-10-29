"""Decorator order: every pair of Plan 8 decorators gives one graph either way round.

Each Plan 8 decorator is stacked with each other one, and with `@tag`,
`@parameterize` and `@config.when`, in both orders, with and without a `@mutate`
step on the function; each is also stacked with `@extract_fields`. Both orders
must build the same nodes, wired the same way, tagged the same, computing the
same values with the same check results, or both must be refused with the same
error. The roadmap's stacking row asks this.
"""

import itertools
from typing import override

import pytest

import fnloom
from helpers import make_module

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
}

HEADER = """
from fnloom import (
    check_output, check_output_custom, config, extract_fields, inject, mutate,
    parameterize, pipe_input, pipe_output, resolve, source, step, tag, value,
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


class _Checks(fnloom.Hook):
    """Each node's check results, as names and outcomes, whatever their order."""

    def __init__(self) -> None:
        """Starts with no results."""
        self.found: dict[str, list[tuple[str, bool]]] = {}

    @override
    def after_node(self, event: fnloom.NodeEnd) -> None:
        """Keeps the node's check results."""
        if event.checks:
            # Sorted: two check decorators run in the order written, by design.
            self.found[event.node.name] = sorted(
                (c.name, c.passes) for c in event.checks
            )


def _outcome(source: str, name: str, outputs: list[str] | None = None) -> object:
    """The graph's shape, tags, values and check results, or its refusal."""
    checks = _Checks()
    try:
        module = make_module(name, source)
        driver = (
            fnloom.Builder()
            .with_modules(module)
            .with_config({"mode": "a"})
            .with_hooks(checks)
            .build()
        )
    except (TypeError, ValueError) as error:
        return f"{type(error).__name__}: {error}"
    computed = outputs or [
        n for n in driver.node_names() if not driver.nodes[n].is_input
    ]
    shape = {n: driver.nodes[n].dependencies for n in driver.node_names()}
    tags = {n: dict(driver.nodes[n].tags) for n in computed}
    values = driver.execute(computed, inputs={"x": 3})
    return shape, tags, values, checks.found


def _graph(first: str, second: str, *, mutated: bool) -> object:
    source = f"{HEADER}\n@{first}\n@{second}\ndef f__a(x: int, k: int = 1) -> int:\n"
    source += "    return x * k\n"
    if mutated:
        source += MUTATOR
    return _outcome(source, "stacked")


PAIRS = [
    (a, b)
    for a, b in itertools.combinations([*PLAN_8, *OTHERS], 2)
    if a in PLAN_8 or b in PLAN_8
]


@pytest.mark.parametrize("mutated", [False, True], ids=["plain", "mutated"])
@pytest.mark.parametrize(("a", "b"), PAIRS, ids=[f"{a}+{b}" for a, b in PAIRS])
def test_a_pair_gives_one_graph_in_either_order(
    a: str, b: str, *, mutated: bool
) -> None:
    """Both orders: the same nodes, wiring, tags, values and checks, or one refusal."""
    decorators = {**PLAN_8, **OTHERS}
    one = _graph(decorators[a], decorators[b], mutated=mutated)
    other = _graph(decorators[b], decorators[a], mutated=mutated)
    assert one == other


def test_only_inject_with_parameterize_is_refused() -> None:
    """Every other pair builds; that one is refused the same way in both orders."""
    decorators = {**PLAN_8, **OTHERS}
    refused = [
        (a, b)
        for a, b in PAIRS
        if isinstance(_graph(decorators[a], decorators[b], mutated=False), str)
    ]
    assert refused == [("inject", "parameterize")]


ON_FIELDS = {
    "check_output": "check_output(range=(0, 5), field='p', importance='warn')",
    "check_output_custom": "check_output_custom(_small, field='p', importance='warn')",
    "pipe_input": "pipe_input(step(_inc))",
    "pipe_output": "pipe_output(step(_inc).on_output('p'))",
    "inject": "inject(k=value(3))",
    "resolve": "resolve(lambda: tag(resolved='1'))",
}


@pytest.mark.parametrize("decorator", ON_FIELDS)
def test_with_extract_fields_in_either_order(decorator: str) -> None:
    """Each Plan 8 decorator, above or below `@extract_fields`."""
    fields = "extract_fields({'p': int, 'q': int})"
    results = [
        _outcome(
            f"{HEADER}\n@{top}\n@{bottom}\ndef d(x: int, k: int = 1) -> dict:\n"
            "    return {'p': x * k, 'q': x}\n",
            "fields_stacked",
            ["p", "q"],
        )
        for top, bottom in (
            (ON_FIELDS[decorator], fields),
            (fields, ON_FIELDS[decorator]),
        )
    ]
    assert not isinstance(results[0], str), results[0]
    assert results[0] == results[1]
