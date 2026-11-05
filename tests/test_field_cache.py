"""Per-field cache control: `@cache(fields=...)` and `fnloom.FieldCache` (Q147)."""

import json
from typing import TYPE_CHECKING

import pytest

from fnloom import (
    Builder,
    CacheOutcome,
    DiskCache,
    FieldCache,
    MemoryCache,
    cache,
)
from fnloom._decorators import decorations
from helpers import make_module

if TYPE_CHECKING:
    from pathlib import Path

    from fnloom import CacheStore, Driver

REPORT = """
from fnloom import FieldCache, cache, extract_fields

@cache(behavior="{producer}", fields={fields})
@extract_fields({{"totals": dict, "rate": float}})
def report(region: str) -> dict:
    print("report")
    return {{"totals": {{"north": len(region)}}, "rate": 0.2}}

def taxed(totals: dict, rate: float) -> float:
    print("taxed")
    return totals["north"] * rate
"""

REQUEST = {"region": "north"}


def _report(producer: str, fields: str, name: str = "field_cache_report") -> Driver:
    module = make_module(name, REPORT.format(producer=producer, fields=fields))
    return Builder().with_modules(module).with_cache(MemoryCache()).build()


def _twice(
    driver: Driver, outputs: list[str], capsys: pytest.CaptureFixture[str]
) -> list[str]:
    """What the second of two equal runs runs."""
    first = driver.execute(outputs, inputs=REQUEST)
    capsys.readouterr()
    assert driver.execute(outputs, inputs=REQUEST) == first
    return capsys.readouterr().out.split()


def test_a_field_is_stored_apart_from_a_disabled_producer(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`disable` on the producer, `default` on a field: the field hits alone."""
    driver = _report("disable", '{"totals": "default"}')
    assert _twice(driver, ["totals"], capsys) == []
    assert driver.explain_cache(["totals"], inputs=REQUEST) == {
        "report": CacheOutcome("uncacheable", "policy disable (decorator)"),
        "totals": CacheOutcome("hit", "policy default (decorator)"),
    }
    # The other field follows its producer, so the producer runs for it.
    assert _twice(driver, ["rate"], capsys) == ["report"]


def test_a_stored_field_of_a_disabled_producer_misses_on_a_new_input(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The field is keyed by what the producer reads."""
    driver = _report("disable", '{"totals": "default"}')
    assert driver.execute(["totals"], inputs=REQUEST) == {"totals": {"north": 5}}
    assert driver.execute(["totals"], inputs={"region": "south-east"}) == {
        "totals": {"north": 10}
    }
    assert capsys.readouterr().out.split() == ["report", "report"]


def test_an_overridden_producer_keys_its_stored_field() -> None:
    """When the producer is an override, the field is keyed by that override."""
    driver = _report("disable", '{"totals": "default"}')
    driver.execute(["totals"], inputs=REQUEST)
    given = {"totals": {"north": 99}, "rate": 0.5}
    assert driver.execute(["totals"], inputs=REQUEST, overrides={"report": given}) == {
        "totals": {"north": 99}
    }
    assert driver.execute(["totals"], inputs=REQUEST) == {"totals": {"north": 5}}


def test_a_stored_field_lets_its_reader_hit(capsys: pytest.CaptureFixture[str]) -> None:
    """A reader of a stored field is keyed by it, so it is cacheable too."""
    driver = _report("disable", '{"totals": "default", "rate": "default"}')
    assert _twice(driver, ["taxed"], capsys) == []
    assert driver.explain_cache(["taxed"], inputs=REQUEST)["taxed"] == (
        CacheOutcome("hit", None)
    )


def test_a_recompute_field_runs_its_producer_again(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`recompute` on a field reruns the producer, never served from the store.

    Hamilton's `target_` recompute reruns only the field's getter, on the
    producer's stored value.
    """
    driver = _report("default", '{"totals": "recompute"}')
    assert _twice(driver, ["totals"], capsys) == ["report"]
    outcomes = driver.explain_cache(["totals"], inputs=REQUEST)
    assert outcomes == {
        "report": CacheOutcome("miss", "runs again for recompute field 'totals'"),
        "totals": CacheOutcome("miss", "policy recompute (decorator)"),
    }
    # The other field is served from the stored producer.
    driver.execute(["rate"], inputs=REQUEST)
    capsys.readouterr()
    assert _twice(driver, ["rate"], capsys) == []


def test_a_recompute_field_keys_its_reader_by_its_value(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A reader of a `recompute` field hits when the field's value is unchanged."""
    driver = _report("default", '{"totals": "recompute"}')
    assert _twice(driver, ["taxed"], capsys) == ["report"]


def test_a_disabled_field_makes_its_readers_run(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`disable` on a field: what reads it runs, as for a disabled producer (Q50)."""
    driver = _report("default", '{"rate": "disable"}')
    assert _twice(driver, ["taxed"], capsys) == ["taxed"]
    outcomes = driver.explain_cache(["taxed"], inputs=REQUEST)
    assert outcomes["rate"] == CacheOutcome("uncacheable", "policy disable (decorator)")
    assert outcomes["taxed"] == CacheOutcome("uncacheable", "reads uncacheable 'rate'")
    assert outcomes["report"] == CacheOutcome("hit", "policy default (decorator)")


def test_a_field_without_a_policy_keeps_todays_behavior() -> None:
    """A field `fields=` does not name is keyed with its producer, never stored."""
    driver = _report("default", '{"rate": "default"}')
    driver.execute(["totals", "rate"], inputs=REQUEST)
    outcomes = driver.explain_cache(["totals", "rate"], inputs=REQUEST)
    assert outcomes["totals"] == CacheOutcome(
        "miss", "extracted field: runs whenever needed; never served from the store"
    )
    assert outcomes["rate"] == CacheOutcome("hit", "policy default (decorator)")


def test_a_driver_list_naming_a_field_beats_fields(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A list naming a field node is the most specific, as for any node."""
    module = make_module(
        "field_cache_listed",
        REPORT.format(producer="disable", fields='{"totals": "recompute"}'),
    )
    driver = (
        Builder()
        .with_modules(module)
        .with_cache(MemoryCache(), default=["totals"])
        .build()
    )
    assert _twice(driver, ["totals"], capsys) == []
    assert driver.explain_cache(["totals"], inputs=REQUEST)["totals"] == (
        CacheOutcome("hit", "policy default (driver)")
    )


def test_a_field_format_stores_the_field_in_it(tmp_path: Path) -> None:
    """`FieldCache(format="json")` stores the field as JSON in a `DiskCache`."""
    module = make_module(
        "field_cache_json",
        REPORT.format(
            producer="disable",
            fields='{"totals": FieldCache(behavior="default", format="json")}',
        ),
    )
    store: CacheStore = DiskCache(tmp_path)
    driver = Builder().with_modules(module).with_cache(store).build()
    driver.execute(["totals"], inputs=REQUEST)
    (entry,) = (path for path in tmp_path.rglob("*") if path.is_file())
    assert json.dumps({"north": 5}).encode() in entry.read_bytes()
    assert driver.explain_cache(["totals"], inputs=REQUEST)["totals"].status == "hit"


def test_a_field_format_unknown_to_the_store_fails_the_build(tmp_path: Path) -> None:
    """A field's format is resolved against the store at build, as a node's is."""
    module = make_module(
        "field_cache_bad_format",
        REPORT.format(
            producer="default", fields='{"rate": FieldCache(format="parquet")}'
        ),
    )
    with pytest.raises(ValueError, match="node 'rate' names cache format 'parquet'"):
        Builder().with_modules(module).with_cache(DiskCache(tmp_path)).build()


def test_a_field_the_function_does_not_extract_fails_the_build() -> None:
    """A name in `fields=` that is no extracted field, with a `did you mean`."""
    module = make_module(
        "field_cache_unknown",
        REPORT.format(producer="default", fields='{"total": "default"}'),
    )
    with pytest.raises(ValueError, match=r"names 'total'.*did you mean 'totals'"):
        Builder().with_modules(module).build()


def test_fields_on_a_function_that_extracts_nothing_fails_the_build() -> None:
    """`fields=` needs `@extract_fields` or `@extract_columns`."""
    module = make_module(
        "field_cache_plain",
        """
from fnloom import cache

@cache(fields={"a": "default"})
def plain(region: str) -> dict:
    return {"a": 1}
""",
    )
    with pytest.raises(TypeError, match="needs @extract_fields or @extract_columns"):
        Builder().with_modules(module).build()


@pytest.mark.parametrize(
    ("fields", "error", "match"),
    [
        (["totals"], TypeError, "must be a mapping"),
        ({1: "default"}, TypeError, "names must be non-empty str"),
        ({"totals": "ignore"}, ValueError, "is not one of"),
        ({"totals": 3}, TypeError, r"a behavior's name or a fnloom.FieldCache"),
    ],
)
def test_malformed_fields_are_refused_when_decorating(
    fields: object, error: type[Exception], match: str
) -> None:
    """`fields=` is checked when `@cache` is called."""
    with pytest.raises(error, match=match):
        cache(fields=fields)  # ty: ignore[invalid-argument-type]  # refused is the point


def test_field_cache_checks_its_arguments() -> None:
    """A `FieldCache` refuses an unknown behavior or a format of the wrong type."""
    with pytest.raises(ValueError, match="is not one of"):
        FieldCache(behavior="ignore")  # ty: ignore[invalid-argument-type]  # refused is the point
    with pytest.raises(TypeError, match="must be a Format or its name"):
        FieldCache(format=3)  # ty: ignore[invalid-argument-type]  # refused is the point


def test_fields_are_recorded_sorted_with_names_as_policies() -> None:
    """A behavior's name stands for a `FieldCache` of it."""

    @cache(fields={"b": "disable", "a": FieldCache(format="json")})
    def f() -> dict[str, object]:
        return {}

    policy = decorations(f).cache
    assert policy is not None
    assert policy.fields == (
        ("a", FieldCache("default", "json")),
        ("b", FieldCache("disable")),
    )


def test_a_namespaced_field_takes_its_policy(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Under a namespace, `fields=` names the field as the function extracts it."""
    module = make_module(
        "field_cache_namespaced",
        REPORT.format(producer="disable", fields='{"totals": "default"}'),
    )
    driver = (
        Builder().with_modules(module, namespace="eu").with_cache(MemoryCache()).build()
    )
    assert _twice(driver, ["eu.totals"], capsys) == []


def test_a_column_takes_its_policy(capsys: pytest.CaptureFixture[str]) -> None:
    """`fields=` applies to `@extract_columns` columns too."""
    pd = pytest.importorskip("pandas")
    module = make_module(
        "field_cache_columns",
        """
import pandas as pd
from fnloom import cache, extract_columns

@cache(behavior="disable", fields={"units": "default"})
@extract_columns("units", "price")
def sales(region: str) -> pd.DataFrame:
    print("sales")
    return pd.DataFrame({"units": [len(region)], "price": [2.5]})
""",
    )
    driver = Builder().with_modules(module).with_cache(MemoryCache()).build()
    first = driver.execute(["units"], inputs=REQUEST)["units"]
    capsys.readouterr()
    second = driver.execute(["units"], inputs=REQUEST)["units"]
    assert isinstance(second, pd.Series)
    assert second.equals(first)
    assert capsys.readouterr().out.split() == []


def test_async_driver_serves_a_stored_field(capsys: pytest.CaptureFixture[str]) -> None:
    """`build_async` keys and stores fields as `build` does."""
    import asyncio  # noqa: PLC0415  # only this test runs a loop

    module = make_module(
        "field_cache_async",
        REPORT.format(producer="disable", fields='{"totals": "default"}'),
    )
    driver = Builder().with_modules(module).with_cache(MemoryCache()).build_async()
    asyncio.run(driver.execute(["totals"], inputs=REQUEST))
    capsys.readouterr()
    assert asyncio.run(driver.execute(["totals"], inputs=REQUEST)) == {
        "totals": {"north": 5}
    }
    assert capsys.readouterr().out.split() == []
