"""`with_cache`'s lists: per-node behavior from the driver, and precedence (Q143)."""

import copy
import pickle
from typing import TYPE_CHECKING

import pytest

from fnloom import Builder, CacheOutcome, MemoryCache
from helpers import make_module

if TYPE_CHECKING:
    from types import ModuleType

    from fnloom import Driver

SALES = """
from fnloom import cache


def units(region: str) -> int:
    print("units")
    return len(region)

@cache(behavior="disable")
def price(units: int) -> float:
    print("price")
    return 2.5

@cache(behavior="recompute")
def revenue(units: int) -> float:
    print("revenue")
    return units * 2.5

def taxes(revenue: float, price: float) -> float:
    print("taxes")
    return revenue * 0.2 + price
"""

REQUEST = {"region": "north"}


def _sales() -> ModuleType:
    return make_module("cache_lists_sales", SALES)


def _ran(driver: Driver, capsys: pytest.CaptureFixture[str]) -> list[str]:
    """What a second run of `taxes` runs, after a first one warmed the store."""
    driver.execute(["taxes"], inputs=REQUEST)
    capsys.readouterr()
    driver.execute(["taxes"], inputs=REQUEST)
    return sorted(capsys.readouterr().out.split())


def _behaviors(driver: Driver) -> dict[str, str]:
    """Each node's settled behavior, as the driver's cache keys it."""
    cache = driver._cache  # noqa: SLF001  # what the cache settled is the point
    assert cache is not None
    return {name: key.behavior for name, key in sorted(cache.keys.items())}


def test_a_named_list_beats_the_decorator(capsys: pytest.CaptureFixture[str]) -> None:
    """`disable=["revenue"]` and `default=["price"]` win over each node's `@cache`."""
    module = _sales()
    driver = (
        Builder()
        .with_modules(module)
        .with_cache(MemoryCache(), disable=["revenue"], default=["price"])
        .build()
    )
    assert _behaviors(driver) == {
        "price": "default",
        "revenue": "disable",
        "taxes": "default",
        "units": "default",
    }
    # `revenue` runs every time, and so does `taxes`, which reads it.
    assert _ran(driver, capsys) == ["revenue", "taxes"]


def test_true_list_does_not_beat_decorator() -> None:
    """`recompute=True` is the default behavior: `@cache(behavior="disable")` wins.

    Review Focus 4: `True` is not a name, so it is less specific than the
    node's own decorator. Hamilton expands it to every name, so there it wins.
    """
    module = _sales()
    driver = (
        Builder().with_modules(module).with_cache(MemoryCache(), recompute=True).build()
    )
    outcomes = driver.explain_cache(["taxes"], inputs=REQUEST)
    assert outcomes["price"] == CacheOutcome(
        "uncacheable", "policy disable (decorator)"
    )
    assert outcomes["revenue"] == CacheOutcome("miss", "policy recompute (decorator)")
    assert outcomes["units"] == CacheOutcome("miss", "policy recompute (default)")
    assert outcomes["taxes"] == CacheOutcome("uncacheable", "reads uncacheable 'price'")


def test_a_list_naming_a_node_beats_true_in_another() -> None:
    """`default=True` with `disable=["units"]`: the name wins, the rest default."""
    module = _sales()
    driver = (
        Builder()
        .with_modules(module)
        .with_cache(MemoryCache(), default=True, disable=["units"])
        .build()
    )
    outcomes = driver.explain_cache(["taxes"], inputs=REQUEST)
    assert outcomes["units"] == CacheOutcome("uncacheable", "policy disable (driver)")
    assert outcomes["price"] == CacheOutcome(
        "uncacheable", "policy disable (decorator)"
    )


def test_default_behavior_applies_where_nothing_else_does(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`default_behavior` reaches the undecorated nodes only."""
    module = _sales()
    driver = (
        Builder()
        .with_modules(module)
        .with_cache(MemoryCache(), default_behavior="disable")
        .build()
    )
    assert _behaviors(driver) == {
        "price": "disable",
        "revenue": "recompute",
        "taxes": "disable",
        "units": "disable",
    }
    assert _ran(driver, capsys) == ["price", "revenue", "taxes", "units"]


def test_a_cache_decorator_without_behavior_takes_the_default_behavior() -> None:
    """`@cache(version=...)` alone sets no behavior, as in Hamilton (no tag)."""
    module = make_module(
        "cache_lists_pinned",
        """
from fnloom import cache

@cache(version="1")
def units(region: str) -> int:
    return len(region)
""",
    )
    driver = (
        Builder()
        .with_modules(module)
        .with_cache(MemoryCache(), default_behavior="recompute")
        .build()
    )
    assert driver.explain_cache(["units"], inputs=REQUEST) == {
        "units": CacheOutcome("miss", "policy recompute (default)")
    }


def test_the_reason_names_a_default_from_a_list_or_decorator() -> None:
    """A `default` node says where its behavior came from, unless the default did."""
    module = _sales()
    driver = (
        Builder()
        .with_modules(module)
        .with_cache(MemoryCache(), default=["price", "revenue"])
        .build()
    )
    driver.execute(["taxes"], inputs=REQUEST)
    outcomes = driver.explain_cache(["taxes"], inputs=REQUEST)
    assert outcomes == {
        "price": CacheOutcome("hit", "policy default (driver)"),
        "revenue": CacheOutcome("hit", "policy default (driver)"),
        "taxes": CacheOutcome("hit", None),
        "units": CacheOutcome("hit", None),
    }


def test_an_unknown_name_fails_the_build_with_did_you_mean() -> None:
    """A name that is no node is refused at build, every one listed."""
    builder = (
        Builder()
        .with_modules(_sales())
        .with_cache(MemoryCache(), recompute=["revenu"], disable=["nowhere"])
    )
    with pytest.raises(ValueError, match="did you mean 'revenue'") as caught:
        builder.build()
    assert "with_cache disable names 'nowhere', which is no node" in str(caught.value)
    with pytest.raises(ValueError, match="did you mean 'revenue'"):
        builder.build_async()


def test_a_name_in_two_lists_is_refused() -> None:
    """A node may have one behavior from the driver."""
    with pytest.raises(ValueError, match=r"'units' is in default and disable"):
        Builder().with_cache(MemoryCache(), default=["units"], disable=["units"])


def test_true_conflicts_are_refused() -> None:
    """Two lists given `True`, or `True` against another `default_behavior`."""
    with pytest.raises(ValueError, match="only one list may be True"):
        Builder().with_cache(MemoryCache(), default=True, disable=True)
    with pytest.raises(ValueError, match="recompute=True sets the default behavior"):
        Builder().with_cache(MemoryCache(), recompute=True, default_behavior="disable")
    # The same behavior twice is no conflict.
    Builder().with_cache(MemoryCache(), recompute=True, default_behavior="recompute")


@pytest.mark.parametrize(
    ("arguments", "error", "match"),
    [
        ({"recompute": "units"}, TypeError, "an iterable of node names"),
        ({"disable": False}, TypeError, "must be True or an iterable"),
        ({"default": ["units", 3]}, TypeError, "names must be str, got: int"),
        ({"default_behavior": "ignore"}, ValueError, "is not one of"),
        ({"ignore": ["taxes"]}, TypeError, "must map each consumer"),
        ({"ignore": {"taxes": "revenue"}}, TypeError, "an iterable of node names"),
    ],
)
def test_malformed_lists_are_refused(
    arguments: dict[str, object], error: type[Exception], match: str
) -> None:
    """Each argument is checked when `with_cache` is called."""
    with pytest.raises(error, match=match):
        Builder().with_cache(MemoryCache(), **arguments)  # ty: ignore[invalid-argument-type]  # refused is the point


IGNORING = """
from fnloom import cache


class Client:
    pass

@cache(behavior="disable")
def client() -> Client:
    return Client()

def total(client: Client, units: int) -> int:
    print("total")
    return units * 2
"""


def test_ignore_leaves_a_consumer_parameter_out_of_its_key(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`ignore={"total": ("client",)}` lets `total` hit beside a disabled client."""
    module = make_module("cache_lists_ignore", IGNORING)
    driver = (
        Builder()
        .with_modules(module)
        .with_cache(MemoryCache(), ignore={"total": ("client",)})
        .build()
    )
    assert driver.execute(["total"], inputs={"units": 3}) == {"total": 6}
    assert driver.execute(["total"], inputs={"units": 3}) == {"total": 6}
    assert capsys.readouterr().out.split() == ["total"]
    plain = Builder().with_modules(module).with_cache(MemoryCache()).build()
    assert plain.explain_cache(["total"], inputs={"units": 3})["total"] == (
        CacheOutcome("uncacheable", "reads uncacheable 'client'")
    )


def test_ignore_names_are_checked_at_build() -> None:
    """A consumer that is no node, or a parameter it lacks, fails the build."""
    module = make_module("cache_lists_ignore_bad", IGNORING)
    with pytest.raises(ValueError, match="did you mean 'total'"):
        Builder().with_modules(module).with_cache(
            MemoryCache(), ignore={"totl": ("client",)}
        ).build()
    with pytest.raises(ValueError, match=r"names 'clent'.*did you mean 'client'"):
        Builder().with_modules(module).with_cache(
            MemoryCache(), ignore={"total": ("clent",)}
        ).build()


def test_the_lists_are_carried_by_pickle_copy_rebuild_and_async() -> None:
    """Every way to get another driver keeps the lists."""
    module = make_module("cache_lists_carried", SALES)
    builder = (
        Builder()
        .with_modules(module)
        .with_cache(MemoryCache(), disable=["revenue"], default_behavior="recompute")
        .with_result_builder(dict)
    )
    driver = builder.build()
    expected = _behaviors(driver)
    assert expected["revenue"] == "disable"
    assert expected["units"] == "recompute"
    for other in (
        pickle.loads(pickle.dumps(driver)),  # noqa: S301  # our own pickle
        copy.copy(driver),
        driver.rebuild(),
        pickle.loads(pickle.dumps(builder)).build(),  # noqa: S301  # our own pickle
        builder.with_config({}).build(),
    ):
        assert _behaviors(other) == expected
    async_outcomes = builder.build_async().explain_cache(["taxes"], inputs=REQUEST)
    assert async_outcomes == driver.explain_cache(["taxes"], inputs=REQUEST)


def test_a_later_with_cache_replaces_the_lists() -> None:
    """`with_cache` sets the store and its lists together."""
    module = _sales()
    driver = (
        Builder()
        .with_modules(module)
        .with_cache(MemoryCache(), disable=["units"])
        .with_cache(MemoryCache())
        .build()
    )
    assert _behaviors(driver)["units"] == "default"
