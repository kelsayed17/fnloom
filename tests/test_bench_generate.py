"""The benchmark graph generator builds the graphs it claims to."""

import inspect
import sys
from typing import TYPE_CHECKING

import pytest
from benches.generate import (
    discard,
    generated,
    make_expected,
    make_module,
    make_request,
)
from hamilton import driver as hamilton_driver

import fnloom

if TYPE_CHECKING:
    from collections.abc import Callable

CHAIN_NODES = 50
FAN_IN_WIDTH = 20
LAYER_WIDTH = 10


def test_generated_chain_has_n_nodes_and_executes() -> None:
    """A chain of n nodes adds one per link, so its last node is seed + n - 1."""
    with generated("chain", CHAIN_NODES) as module:
        driver = fnloom.build(module)
        assert len(set(driver.node_names()) - {"seed"}) == CHAIN_NODES
        result = driver.execute([f"n{CHAIN_NODES - 1}"], inputs={"seed": 7})
    assert result == {f"n{CHAIN_NODES - 1}": 7 + CHAIN_NODES - 1}
    assert result == make_expected("chain", CHAIN_NODES)


def test_fan_in_has_one_wide_function() -> None:
    """Fan-in is one function, `total`, with the n inputs as parameters, summed."""
    with generated("fan_in", FAN_IN_WIDTH) as module:
        assert [name for name, v in vars(module).items() if inspect.isfunction(v)] == [
            "total"
        ]
        assert len(inspect.signature(module.total).parameters) == FAN_IN_WIDTH
        inputs = {f"x{i}": i for i in range(FAN_IN_WIDTH)}
        result = fnloom.build(module).execute(["total"], inputs=inputs)
    assert result == {"total": sum(range(FAN_IN_WIDTH))}


def test_layered_has_n_nodes_in_layers_of_ten() -> None:
    """Layered graphs hold n nodes, ten a layer, each reading two from above."""
    with generated("layered", 3 * LAYER_WIDTH) as module:
        driver = fnloom.build(module)
        assert len(set(driver.node_names()) - {"seed"}) == 3 * LAYER_WIDTH
        assert set(driver.what_is_upstream_of("l2_0")) == {
            "l2_0",
            "l1_0",
            "l1_1",
            "l0_0",
            "l0_1",
            "l0_2",
            "seed",
        }
        outputs = [f"l2_{c}" for c in range(LAYER_WIDTH)]
        result = driver.execute(outputs, inputs={"seed": 7})
    assert result == make_expected("layered", 3 * LAYER_WIDTH)


@pytest.mark.parametrize("n", [LAYER_WIDTH // 2, 2 * LAYER_WIDTH + 5])
def test_layered_expectation_matches_a_partial_last_row(n: int) -> None:
    """When n is not a multiple of ten, the answer has only the last row's nodes."""
    outputs, inputs = make_request("layered", n)
    with generated("layered", n) as module:
        result = fnloom.build(module).execute(outputs, inputs=inputs)
    assert result == make_expected("layered", n)


def test_generated_modules_are_removed_from_sys_modules() -> None:
    """Leaving `generated`, or calling `discard`, drops the module's registration."""
    with generated("chain", CHAIN_NODES) as module:
        assert sys.modules[module.__name__] is module
    assert module.__name__ not in sys.modules
    other = make_module("chain", CHAIN_NODES)
    discard(other)
    assert other.__name__ not in sys.modules


ROWS = 30


def test_rows_shape_has_n_nodes_in_both_spellings() -> None:
    """Rows is one function with n parameterize rows, spelled for each runtime."""
    outputs, inputs = make_request("rows", ROWS)
    with generated("rows", ROWS) as module:
        driver = fnloom.build(module)
        assert set(driver.node_names()) - {"seed"} == {f"r{i}" for i in range(ROWS)}
        assert [name for name, v in vars(module).items() if inspect.isfunction(v)] == [
            "parameterize",
            "source",
            "step",
        ]
        result = driver.execute(outputs, inputs=inputs)
    assert result == make_expected("rows", ROWS)
    with generated("rows", ROWS, spelling="hamilton") as module:
        built = hamilton_driver.Builder().with_modules(module).build()
        assert {v.name for v in built.list_available_variables()} - {"seed"} == {
            f"r{i}" for i in range(ROWS)
        }
        names: list[str | Callable[..., object] | hamilton_driver.Variable] = [*outputs]
        assert built.execute(names, inputs=inputs) == make_expected("rows", ROWS)


FIELDS = 25


def test_extract_shape_has_one_producer_and_n_consumers_in_both_spellings() -> None:
    """One producer of n fields, each read by a consumer; the closed form holds."""
    outputs, inputs = make_request("extract", FIELDS)
    assert outputs == [f"c{i}" for i in range(FIELDS)]
    expected = make_expected("extract", FIELDS)
    assert expected == {f"c{i}": 7 + i + 1 for i in range(FIELDS)}
    fields = {f"f{i}" for i in range(FIELDS)}
    with generated("extract", FIELDS) as module:
        driver = fnloom.build(module)
        assert set(driver.node_names()) == {"seed", "fields", *fields, *outputs}
        assert driver.nodes["f3"].dependencies == ("fields",)
        assert driver.execute(outputs, inputs=inputs) == expected
    with generated("extract", FIELDS, spelling="hamilton") as module:
        built = hamilton_driver.Builder().with_modules(module).build()
        assert {v.name for v in built.list_available_variables()} == {
            "seed",
            "fields",
            *fields,
            *outputs,
        }
        names: list[str | Callable[..., object] | hamilton_driver.Variable] = [*outputs]
        assert built.execute(names, inputs=inputs) == expected
