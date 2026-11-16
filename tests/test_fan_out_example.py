"""The fan-out example end to end: a catalog listing, one endpoint call per item.

A request naming one item makes the listing decline, so it is never fetched, and the
item's branch runs alone. Branch keys are content-addressed, so a branch computed by
one run is served to any later run that picks the same item.
"""

from dataclasses import dataclass

import fnloom
from fnloom import ABSENT, Absent, Collect, MemoryCache, Parallelizable, cache

CATALOG = {"furniture": ["chair", "desk", "lamp"]}

#: What the graph called, in order: `("listing", category)` or `("fetch", url)`. A
#: node reading a mutable global has no automatic code version, so the two nodes that
#: append to it are given one.
calls: list[tuple[str, str]] = []


@dataclass(frozen=True)
class Endpoint:
    """Where details are fetched from; only its base URL, so it fingerprints."""

    base_url: str


@dataclass(frozen=True)
class Detail:
    """One item's details, as the endpoint returns them."""

    item: str
    url: str


@dataclass(frozen=True)
class Summary:
    """Every detail fetched, in the order the items came."""

    items: tuple[str, ...]
    urls: tuple[str, ...]


def _fetch(url: str) -> str:
    """A fake endpoint call: records the call and returns the URL as the body."""
    calls.append(("fetch", url))
    return url


@cache(version="1")
def listing(category: str, item: str | None = None) -> list[str] | Absent:
    """Every item in the category, unless the request names one."""
    if item is not None:
        return ABSENT
    calls.append(("listing", category))
    return CATALOG[category]


def picked(listing: list[str] | Absent, item: str | None = None) -> Parallelizable[str]:
    """The item named, or else every item listed."""
    if isinstance(listing, Absent):
        assert item is not None
        return [item]
    return listing


@cache(version="1")
def details(picked: str, endpoint: Endpoint) -> Detail:
    """One item's details: one endpoint call per item."""
    return Detail(picked, _fetch(f"{endpoint.base_url}/items/{picked}"))


def summary(details: Collect[Detail]) -> Summary:
    """Every item's details, gathered in item order."""
    return Summary(
        tuple(detail.item for detail in details),
        tuple(detail.url for detail in details),
    )


ENDPOINT = Endpoint("https://catalog.example")


def _driver(*, cached: bool = False) -> fnloom.Driver:
    builder = fnloom.Builder().with_functions(listing, picked, details, summary)
    return (builder.with_cache(MemoryCache()) if cached else builder).build()


def _summary(driver: fnloom.Driver, **inputs: object) -> Summary:
    """Runs `summary` for the furniture category, returning its value."""
    calls.clear()
    given = {"category": "furniture", "endpoint": ENDPOINT, **inputs}
    value = driver.execute(["summary"], inputs=given)["summary"]
    assert isinstance(value, Summary)
    return value


def _url(item: str) -> str:
    return f"{ENDPOINT.base_url}/items/{item}"


def test_the_listing_fans_out_one_call_per_item() -> None:
    """The listing is fetched once, then `details` runs once per item, in order."""
    driver = _driver()
    assert driver.nodes["picked"].kind == "expand"
    assert driver.nodes["details"].block == "picked"
    assert driver.nodes["summary"].kind == "collect"
    assert driver.nodes["listing"].may_be_absent

    result = _summary(driver)

    assert result == Summary(
        ("chair", "desk", "lamp"), tuple(map(_url, ["chair", "desk", "lamp"]))
    )
    assert calls == [
        ("listing", "furniture"),
        ("fetch", _url("chair")),
        ("fetch", _url("desk")),
        ("fetch", _url("lamp")),
    ]


def test_naming_one_item_skips_the_listing() -> None:
    """The listing declines, so it is never fetched; the endpoint is called once."""
    result = _summary(_driver(), item="desk")

    assert result == Summary(("desk",), (_url("desk"),))
    assert calls == [("fetch", _url("desk"))]


def test_a_second_run_is_served_from_the_cache() -> None:
    """The same request again calls neither the listing nor the endpoint."""
    driver = _driver(cached=True)
    first = _summary(driver)
    assert len(calls) == 1 + len(CATALOG["furniture"])

    assert _summary(driver) == first
    assert calls == []


def test_naming_one_item_after_a_full_run_hits_its_branch() -> None:
    """A full run stores each item's branch; naming a listed item then fetches nothing.

    The branch key is the item's content, not its index, so `desk`, index 1 in the
    full run and index 0 here, hits.
    """
    driver = _driver(cached=True)
    _summary(driver)

    assert _summary(driver, item="desk") == Summary(("desk",), (_url("desk"),))
    assert calls == []
