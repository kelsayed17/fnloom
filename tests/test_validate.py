"""Validation and requests: functions in `final_vars` (Q30)."""

import asyncio
import threading
from typing import TYPE_CHECKING

import pytest

import fnloom
from helpers import make_module

if TYPE_CHECKING:
    from types import ModuleType

PLAIN = """
from typing import Any

import fnloom


def base(x: int) -> int:
    return x * 10


def top(base: int) -> int:
    return base + 1


@fnloom.parameterize(p1={"y": fnloom.value(1)}, p2={"y": fnloom.value(2)})
def rows(base: int, y: int) -> int:
    return base + y


@fnloom.extract_fields({"f1": int, "f2": str})
def produced(base: int) -> dict[str, Any]:
    return {"f1": base, "f2": "s"}


@fnloom.config.when(k="a")
def variant__a(base: int) -> int:
    return base


@fnloom.config.when(k="b")
def variant__b(base: int) -> int:
    return -base
"""


def _driver(
    name: str, config: dict[str, object] | None = None
) -> tuple[ModuleType, fnloom.Driver]:
    module = make_module(name, PLAIN)
    builder = fnloom.Builder().with_modules(module)
    if config is not None:
        builder = builder.with_config(config)
    return module, builder.build()


def test_a_plain_function_names_its_node() -> None:
    """A function is its node, and the result is keyed by the node's name."""
    module, driver = _driver("req_plain")
    assert driver.execute([module.base], inputs={"x": 1}) == {"base": 10}
    assert driver.execute([module.top], inputs={"x": 1}) == {"top": 11}


def test_an_extract_function_names_its_producer() -> None:
    """An `@extract_fields` function is its producer, never its fields."""
    module, driver = _driver("req_extract")
    produced = driver.execute([module.produced], inputs={"x": 1})
    assert produced == {"produced": {"f1": 10, "f2": "s"}}
    assert driver.execute([module.produced, "f1"], inputs={"x": 1}) == {
        "produced": {"f1": 10, "f2": "s"},
        "f1": 10,
    }


def test_a_parameterize_function_lists_its_rows() -> None:
    """A function that makes several nodes is a `TypeError` naming each row."""
    module, driver = _driver("req_rows")
    with pytest.raises(TypeError, match=r"rows makes 2 nodes: pass 'p1', 'p2'"):
        driver.execute([module.rows], inputs={"x": 1})
    assert driver.execute(["p1", "p2"], inputs={"x": 1}) == {"p1": 11, "p2": 12}


def test_a_foreign_function_an_unselected_variant_and_a_lambda_are_refused() -> None:
    """No node comes from it: a `ValueError` listing every culprit, sorted."""
    module, driver = _driver("req_foreign", {"k": "a"})
    other = make_module(
        "req_foreign_other", "def stray(x: int) -> int:\n    return x\n"
    )
    culprits = [other.stray, module.variant__b, lambda: 1]
    with pytest.raises(ValueError, match=r"^final_vars: ") as info:
        driver.execute(culprits, inputs={"x": 1})
    lines = str(info.value).removeprefix("final_vars: ").split("; ")
    assert len(lines) == len(culprits)
    assert lines == sorted(lines)
    assert "req_foreign.variant__b" in str(info.value)
    assert "req_foreign_other.stray" in str(info.value)
    assert "is a lambda" in str(info.value)
    # The selected variant is its node, which carries the stripped name.
    assert driver.execute([module.variant__a], inputs={"x": 1}) == {"variant": 10}


def test_a_non_callable_item_is_refused_with_the_functions_problems() -> None:
    """An item that is neither a name nor a function is a `TypeError`, all listed."""
    module, driver = _driver("req_items")
    with pytest.raises(TypeError, match="final_vars items must be str") as info:
        driver.execute([5, module.rows, 2.5])  # ty: ignore[invalid-argument-type]  # the refusal is the point
    assert "float 2.5, int 5" in str(info.value)
    assert "rows makes 2 nodes" in str(info.value)
    with pytest.raises(TypeError, match="not a single str"):
        driver.execute("base")


def test_strings_and_functions_mix_and_collapse_to_one_key() -> None:
    """A name and its function are one output, in the order first requested."""
    module, driver = _driver("req_mix")
    result = driver.execute(["top", module.base, "base", module.top], inputs={"x": 1})
    assert result == {"top": 11, "base": 10}
    assert list(result) == ["top", "base"]
    # The names a function stands for are the plan's key: the same plan serves both.
    assert driver.prepare([module.base], inputs={"x": 1}) is driver.prepare(
        ["base"], inputs={"x": 1}
    )


def test_identity_not_name_decides() -> None:
    """A function of the same name that is not the node's own is no node (Focus 4)."""
    module, driver = _driver("req_identity")
    twin = make_module("req_identity_twin", "def base(x: int) -> int:\n    return 0\n")
    with pytest.raises(ValueError, match=r"another function made: pass the name"):
        driver.execute([twin.base], inputs={"x": 1})
    # The driver's own function still works, and the name still works.
    assert driver.execute([module.base, "base"], inputs={"x": 1}) == {"base": 10}
    # A function forged to look like the node is no node either.
    forged = twin.base
    forged.__module__ = module.__name__
    with pytest.raises(ValueError, match="pass the name"):
        driver.execute([forged], inputs={"x": 1})


def test_async_and_prepare_accept_functions() -> None:
    """`prepare` and `AsyncDriver.execute` read a function as its node."""
    module = make_module("req_async", PLAIN)
    driver = fnloom.Builder().with_modules(module).build()
    plan = driver.prepare([module.base], inputs={"x": 0})
    assert plan.run(inputs={"x": 2}) == {"base": 20}
    built = fnloom.Builder().with_modules(module).build_async()
    assert built.prepare([module.top], inputs={"x": 0}) is not None
    assert asyncio.run(built.execute([module.top], inputs={"x": 1})) == {"top": 11}
    with pytest.raises(TypeError, match="makes 2 nodes"):
        asyncio.run(built.execute([module.rows], inputs={"x": 1}))


def test_a_shared_driver_resolves_functions_from_many_threads() -> None:
    """The identity index is built once and shared: threads agree on every answer."""
    module, driver = _driver("req_threads")
    results: list[dict[str, object]] = []
    errors: list[BaseException] = []

    def work() -> None:
        try:
            results.extend(
                driver.execute([module.top], inputs={"x": 1}) for _ in range(50)
            )
        except BaseException as error:  # noqa: BLE001  # collected and asserted empty
            errors.append(error)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert results == [{"top": 11}] * 400
