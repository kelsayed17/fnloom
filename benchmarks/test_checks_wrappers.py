"""Checked and wrapped runs: a 1,000-node chain, every node checked or wrapped once.

Next to ``test_run.py``'s plain chain, the difference over 1,000 is what one
`check_output_custom` check, or one pass-through node wrapper, costs per node.
"""

from typing import TYPE_CHECKING

import pytest
from benches.generate import make_expected, make_request

import fnloom

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from pytest_codspeed import BenchmarkFixture

    from benchmarks.conftest import LoadGraph

N = 1000


def _passes(value: object) -> bool:
    """A check that reads the value and passes: the least a check can do."""
    del value
    return True


def _through(
    node: fnloom.NodeView, call: Callable[[], object], inputs: Mapping[str, object]
) -> object:
    """A wrapper that runs the node and returns its value: the least one can do."""
    del node, inputs
    return call()


@pytest.mark.parametrize("variant", ["checked", "wrapped"])
def test_checked_or_wrapped_run(
    benchmark: BenchmarkFixture, load_graph: LoadGraph, variant: str
) -> None:
    """Measure one run of a prepared 1,000-node chain, every node checked or wrapped."""
    module = load_graph("chain", N)
    builder = fnloom.Builder().with_modules(module)
    if variant == "checked":
        for name, value in vars(module).items():
            if (
                callable(value)
                and getattr(value, "__module__", None) == module.__name__
            ):
                fnloom.check_output_custom(_passes)(value)
                del name
    else:
        builder = builder.with_node_wrappers(_through)
    outputs, inputs = make_request("chain", N)
    prepared = builder.build().prepare(outputs, inputs=inputs)
    assert prepared.run(inputs=inputs) == make_expected("chain", N)
    benchmark(prepared.run, inputs=inputs)
