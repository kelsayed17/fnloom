"""Decorator order: every pair of Plan 8 decorators gives one graph either way round.

Each Plan 8 decorator is stacked with each other one, and with `@tag`,
`@parameterize`, `@config.when` and `@extract_fields`, in both orders. Both orders
must build the same nodes, wired the same way, computing the same values, or
both must be refused with the same error. The roadmap's stacking row asks this.
"""

import itertools

import pytest

import fnloom
from helpers import make_module

PLAN_8 = {
    "check_output": "check_output(range=(0, 10_000))",
    "check_output_custom": "check_output_custom(_ok)",
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
    check_output, check_output_custom, config, extract_fields, inject,
    parameterize, pipe_input, pipe_output, resolve, source, step, tag, value,
)

def _ok(v: int) -> bool:
    return True

def _inc(v: int) -> int:
    return v + 1
"""


def _graph(first: str, second: str, name: str) -> object:
    """The built graph's shape and values, or the error both orders should share."""
    source = f"{HEADER}\n@{first}\n@{second}\ndef f__a(x: int, k: int = 1) -> int:\n"
    source += "    return x * k\n"
    try:
        module = make_module(name, source)
        driver = (
            fnloom.Builder().with_modules(module).with_config({"mode": "a"}).build()
        )
    except (TypeError, ValueError) as error:
        return type(error).__name__
    computed = [n for n in driver.node_names() if not driver.nodes[n].is_input]
    shape = {n: driver.nodes[n].dependencies for n in driver.node_names()}
    tags = {n: dict(driver.nodes[n].tags) for n in computed}
    values = driver.execute(computed, inputs={"x": 3})
    return shape, tags, values


PAIRS = [
    (a, b)
    for a, b in itertools.combinations([*PLAN_8, *OTHERS], 2)
    if a in PLAN_8 or b in PLAN_8
]


@pytest.mark.parametrize(("a", "b"), PAIRS, ids=[f"{a}+{b}" for a, b in PAIRS])
def test_a_pair_gives_one_graph_in_either_order(a: str, b: str) -> None:
    """Both orders: the same nodes, wiring, tags and values, or the same refusal."""
    decorators = {**PLAN_8, **OTHERS}
    one = _graph(decorators[a], decorators[b], f"stack_{len(a)}_{len(b)}_1")
    other = _graph(decorators[b], decorators[a], f"stack_{len(a)}_{len(b)}_2")
    assert one == other


FIELDS = """
from fnloom import check_output, extract_fields, pipe_output, step

def _double(v: int) -> int:
    return v * 2
"""


@pytest.mark.parametrize(
    "decorator",
    [
        "check_output(range=(0, 100), field='p')",
        "pipe_output(step(_double).on_output('p'))",
    ],
)
def test_with_extract_fields_in_either_order(decorator: str) -> None:
    """A field check or a field step is the same above or below `@extract_fields`."""
    results = []
    for index, (top, bottom) in enumerate(
        (
            (decorator, "extract_fields({'p': int, 'q': int})"),
            ("extract_fields({'p': int, 'q': int})", decorator),
        )
    ):
        source = f"{FIELDS}\n@{top}\n@{bottom}\ndef d(x: int) -> dict:\n"
        source += "    return {'p': x, 'q': x}\n"
        driver = fnloom.build(make_module(f"fields_order_{index}", source))
        shape = {n: driver.nodes[n].dependencies for n in driver.node_names()}
        results.append((shape, driver.execute(["p", "q"], inputs={"x": 3})))
    assert results[0] == results[1]


def test_inject_and_parameterize_are_refused_in_either_order() -> None:
    """The one pair that can't share a function, refused the same way both times."""
    decorators = {**PLAN_8, **OTHERS}
    assert _graph(decorators["inject"], decorators["parameterize"], "refused_1") == (
        "TypeError"
    )
    assert _graph(decorators["parameterize"], decorators["inject"], "refused_2") == (
        "TypeError"
    )
