"""Validation and requests: functions in `final_vars` (Q30), and `validate` (Q71)."""

import asyncio
import threading
from typing import TYPE_CHECKING, Any, cast

import pytest

import fnloom
from fnloom._requests import suggest
from fnloom._view import NodeViews
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
    assert "float 2.5; function" in str(info.value)
    assert "; int 5" in str(info.value)
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


def test_every_culprit_of_every_kind_is_listed_once_in_one_message() -> None:
    """A malformed item, a several-node function, absent functions and a repeat."""
    module, driver = _driver("req_mixed", {"k": "a"})
    culprits: list[Any] = [module.rows, module.variant__b, 5, module.base, lambda: 1]
    culprits.append(module.variant__b)
    with pytest.raises(TypeError, match="final_vars items must be str") as info:
        driver.execute(culprits, inputs={"x": 1})
    message = str(info.value)
    parts = message.split(": ", 1)[1].split("; ")
    assert parts == sorted(set(parts))
    # `base` is fine and `variant__b` is repeated: two fewer than requested.
    assert len(parts) == len(culprits) - 2
    assert message.count("req_mixed.variant__b") == 1
    assert "int 5" in message
    assert "rows makes 2 nodes" in message
    assert "is a lambda" in message
    # Without a malformed item, the same absent functions are a `ValueError`.
    with pytest.raises(ValueError, match=r"req_mixed\.variant__b") as absent:
        driver.execute([module.variant__b, lambda: 1, module.variant__b])
    assert str(absent.value).count("req_mixed.variant__b") == 1


ALIASED = """
import fnloom


def base(x: int) -> int:
    return x * 10


alias = base


@fnloom.parameterize(rows={"y": fnloom.value(1)}, other={"y": fnloom.value(2)})
def rows(base: int, y: int) -> int:
    return base + y


@fnloom.parameterize(a={"y": fnloom.value(1)}, b={"y": fnloom.value(2)})
def pair(base: int, y: int) -> int:
    return base + y
"""


def test_an_alias_and_a_row_named_like_the_function_resolve_to_it() -> None:
    """Several nodes share an origin: the one named like the function is its node."""
    module = make_module("req_alias", ALIASED)
    driver = fnloom.build(module)
    assert {"base", "alias"} <= set(driver.nodes)
    assert driver.execute([module.base], inputs={"x": 1}) == {"base": 10}
    assert driver.execute([module.rows], inputs={"x": 1}) == {"rows": 11}
    with pytest.raises(TypeError, match=r"pair makes 2 nodes: pass 'a', 'b'"):
        driver.execute([module.pair], inputs={"x": 1})


def test_a_view_mapping_without_the_generated_set_still_skips_fields() -> None:
    """A `NodeViews` made from a bare mapping derives which nodes are fields."""
    module, driver = _driver("req_bare")
    views = cast("NodeViews", driver.nodes)
    bare = NodeViews(dict(views.functions), {})
    owned = bare.owners[id(module.produced)]
    assert owned == ("produced",)
    assert bare.owners == views.owners


PRICED = """
import fnloom

calls: list[str] = []


def profit(revenue: int, cost: int) -> int:
    calls.append("profit")
    return revenue - cost


def margin(profit: int, revenue: int, scale: float) -> float:
    calls.append("margin")
    return profit / revenue * scale


def report(revenue: int) -> str:
    calls.append("report")
    return str(revenue)


@fnloom.config.when(region="eu")
def tax__eu(profit: int) -> float:
    calls.append("tax")
    return profit * 0.2
"""

# A request with no problem, and the values it is run with.
GOOD: dict[str, object] = {"revenue": 10, "cost": 4, "scale": 1.0}


def _priced(name: str) -> tuple[ModuleType, fnloom.Driver]:
    module = make_module(name, PRICED)
    return module, fnloom.Builder().with_config({"region": "us"}).with_modules(
        module
    ).build()


def test_validate_returns_every_problem_sorted() -> None:
    """Every problem of a request, one line each, sorted (Q71)."""
    _, driver = _priced("val_every")
    lines = driver.validate(
        ["margin", "profitt", "tax"],
        inputs={"revenu": 1, "cost": "s", "scale": 1.0},
        overrides={"scale": "big"},
    )
    with pytest.raises(ValueError, match="region") as unmatched:
        driver.execute(["tax"], inputs=GOOD)
    assert str(unmatched.value).startswith("no variant of 'tax'")
    assert lines == sorted(
        [
            "cost: expected int, got str",
            (
                "missing inputs: 'revenue' (needed by 'margin', 'profit') "
                "(did you mean 'revenu'?)"
            ),
            str(unmatched.value),
            "scale: expected float, got str",
            "unknown inputs: 'revenu' (did you mean 'revenue'?)",
            "unknown outputs: 'profitt' (did you mean 'profit'?)",
        ]
    )


def test_validate_on_a_valid_request_is_empty() -> None:
    """A request `execute` would run gives no lines, on both drivers."""
    module, driver = _priced("val_empty")
    assert driver.validate(["margin", "report"], inputs=GOOD) == []
    overridden = driver.validate([module.margin], inputs=GOOD, overrides={"profit": 3})
    assert overridden == []
    built = fnloom.Builder().with_config({"region": "us"}).with_modules(module)
    async_driver = built.build_async()
    assert async_driver.validate(["margin"], inputs=GOOD) == []
    assert async_driver.validate(["margin"], inputs={"cost": 1, "scale": 1.0}) == [
        "missing inputs: 'revenue' (needed by 'margin', 'profit')"
    ]


def test_validate_runs_nothing() -> None:
    """Values `prepare` accepts but the checks refuse: lines, and no run (Focus 5)."""
    module, driver = _priced("val_runs")
    bad = {"revenue": "ten", "cost": 4, "scale": 1}
    assert driver.validate(["margin"], inputs=bad) == [
        "revenue: expected int, got str",
        "scale: expected float, got int",
    ]
    assert driver.validate(["margin"], inputs=GOOD) == []
    assert module.calls == []
    # `prepare` only reads the names: the values are checked when the plan runs.
    plan = driver.prepare(["margin"], inputs=bad)
    assert module.calls == []
    with pytest.raises(ValueError, match="revenue: expected int, got str"):
        plan.run(inputs=bad)
    assert module.calls == []


def test_validate_refuses_a_malformed_request() -> None:
    """A bare `str` or a non-mapping raises, as `prepare` does."""
    _, driver = _priced("val_malformed")
    with pytest.raises(TypeError, match="not a single str"):
        driver.validate("margin")
    with pytest.raises(TypeError, match="inputs must be a mapping"):
        driver.validate(["margin"], inputs=["revenue"])  # ty: ignore[invalid-argument-type]  # the refusal is the point
    with pytest.raises(TypeError, match="overrides must be a mapping"):
        driver.validate(["margin"], overrides=5)  # ty: ignore[invalid-argument-type]  # the refusal is the point


def test_missing_lines_name_only_requested_consumers() -> None:
    """`report` reads `revenue` too, but is not requested, so it is not named."""
    _, driver = _priced("val_consumers")
    line = "missing inputs: 'revenue' (needed by 'profit')"
    assert driver.validate(["profit"], inputs={"cost": 1}) == [line]
    with pytest.raises(ValueError, match=r"^missing inputs: .*\)$") as info:
        driver.execute(["profit"], inputs={"cost": 1})
    assert str(info.value) == line


def test_errors_suggest_close_names() -> None:
    """Each unknown name gets the closest node within cutoff 0.8, and none below."""
    _, driver = _priced("val_suggest")
    with pytest.raises(ValueError, match=r"^unknown outputs: ") as outputs:
        driver.execute(["margn", "zzz"], inputs=GOOD)
    assert str(outputs.value) == (
        "unknown outputs: 'margn' (did you mean 'margin'?), 'zzz'"
    )
    with pytest.raises(ValueError, match=r"^unknown inputs: ") as inputs:
        driver.execute(["report"], inputs={"revenue": 1, "reveneu": 2})
    assert str(inputs.value) == "unknown inputs: 'reveneu' (did you mean 'revenue'?)"
    with pytest.raises(ValueError, match=r"^unknown overrides: ") as overrides:
        driver.execute(["report"], inputs={"revenue": 1}, overrides={"profi": 2})
    assert str(overrides.value) == (
        "unknown overrides: 'profi' (did you mean 'profit'?)"
    )
    # A computed node supplied as an input is never suggested to itself.
    with pytest.raises(ValueError, match=r"^unknown inputs: 'profit'$"):
        driver.execute(["margin"], inputs={**GOOD, "profit": 1})
    assert suggest("revenu", ["cost", "revenue"]) == " (did you mean 'revenue'?)"
    assert suggest("xyz", ["cost", "revenue"]) == ""
    assert suggest("revenue", ["revenue"]) == ""
    # Ties go the same way whatever the candidates' order.
    tied = " (did you mean 'abcdx'?)"
    assert suggest("abcde", ["abcdf", "abcdx"]) == suggest("abcde", ["abcdx", "abcdf"])
    assert suggest("abcde", ["abcdf", "abcdx"]) == tied


def test_validate_matches_execute_messages() -> None:
    """Each line is the line `execute` raises for the same problem on its own."""
    _, driver = _priced("val_same")
    lone: list[tuple[list[str], dict[str, object], dict[str, object]]] = [
        (["margn"], GOOD, {}),
        (["tax"], GOOD, {}),
        (["margin"], {**GOOD, "revenu": 1}, {}),
        (["margin"], GOOD, {"scal": 1.0}),
        (["margin"], {"cost": 1, "scale": 1.0}, {}),
        (["revenue"], {}, {}),
        (["margin"], {**GOOD, "cost": "s"}, {}),
        (["margin"], GOOD, {"profit": "p"}),
    ]
    for final_vars, inputs, overrides in lone:
        with pytest.raises(ValueError, match=r".") as info:
            driver.execute(final_vars, inputs=inputs, overrides=overrides)
        lines = driver.validate(final_vars, inputs=inputs, overrides=overrides)
        assert lines == [str(info.value)], final_vars
    # A config key named as a missing input: the hint is a line of its own, as in
    # the message.
    keyed = make_module(
        "val_same_keyed", "def taxed(region: str) -> str:\n    return region\n"
    )
    configured = fnloom.Builder().with_config({"region": "us"}).with_modules(keyed)
    built = configured.build()
    with pytest.raises(ValueError, match="config key") as hinted:
        built.execute(["taxed"])
    assert built.validate(["taxed"]) == sorted(str(hinted.value).split("\n"))
