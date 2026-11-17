"""The fan-out example end to end: a catalog listing, one endpoint call per item.

A request naming one item makes the listing decline, so it is never fetched, and the
item's branch runs alone. Branch keys are content-addressed, so a branch computed by
one run is served to any later run that picks the same item. With `with_threads(8)` the
endpoint calls overlap, and an item whose call times out is left out of the summary.
"""

import threading
from dataclasses import dataclass

import pytest

import fnloom
from fnloom import ABSENT, Absent, Collect, MemoryCache, Parallelizable, cache

CATALOG = {
    "furniture": ["chair", "desk", "lamp"],
    "tools": ["drill", "file", "hammer", "level", "pliers", "saw", "vise", "wrench"],
}

#: What the graph called: `("listing", category)` or `("fetch", url)`, in order when
#: the run has one worker. A node reading a mutable global has no automatic code
#: version, so the two nodes that append to it are given one.
calls: list[tuple[str, str]] = []
#: Set by a test, a barrier every endpoint call waits at, so they must overlap.
meeting: list[threading.Barrier] = []
#: Items whose endpoint call times out.
timing_out: set[str] = set()


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
    """A fake endpoint call: records the call and returns the URL as the body.

    It waits at the test's barrier, if any, and times out for an item in
    `timing_out`.
    """
    calls.append(("fetch", url))
    if meeting:
        meeting[0].wait()
    if url.rsplit("/", 1)[-1] in timing_out:
        raise TimeoutError(url)
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
@fnloom.absent_on(TimeoutError)
def details(picked: str, endpoint: Endpoint) -> Detail | Absent:
    """One item's details: one endpoint call per item, absent if it times out."""
    return Detail(picked, _fetch(f"{endpoint.base_url}/items/{picked}"))


def summary(details: Collect[Detail]) -> Summary:
    """Every item's details, gathered in item order."""
    return Summary(
        tuple(detail.item for detail in details),
        tuple(detail.url for detail in details),
    )


ENDPOINT = Endpoint("https://catalog.example")


def _driver(*, cached: bool = False, threads: int | None = None) -> fnloom.Driver:
    builder = fnloom.Builder().with_functions(listing, picked, details, summary)
    if threads is not None:
        builder = builder.with_threads(threads)
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


@pytest.mark.threads
def test_the_listing_fans_out_in_parallel() -> None:
    """With `with_threads(8)`, the eight items' endpoint calls run at once."""
    meeting.append(threading.Barrier(8, timeout=5))
    try:
        result = _summary(_driver(threads=8), category="tools")
    finally:
        meeting.clear()

    items = CATALOG["tools"]
    assert result == Summary(tuple(items), tuple(map(_url, items)))
    fetched = [("fetch", _url(item)) for item in items]
    assert sorted(calls) == sorted([("listing", "tools"), *fetched])


@pytest.mark.threads
@pytest.mark.parametrize("threads", [None, 1, 8])
def test_a_timed_out_item_is_left_out_of_the_summary(threads: int | None) -> None:
    """`details` is absent for the item whose call timed out; the rest are summed up."""
    timing_out.add("desk")
    try:
        result = _summary(_driver(threads=threads))
    finally:
        timing_out.clear()

    assert result == Summary(("chair", "lamp"), (_url("chair"), _url("lamp")))
    assert sorted(calls) == sorted(
        [
            ("listing", "furniture"),
            *(("fetch", _url(item)) for item in CATALOG["furniture"]),
        ]
    )


@pytest.mark.threads
def test_a_partial_summary_is_not_cached_as_a_whole() -> None:
    """The next run calls the timed-out item's endpoint again, and nothing else.

    The other items' branches and the listing are served from the cache; the
    summary, gathered from a partial fan-out, was never stored.
    """
    driver = _driver(cached=True, threads=8)
    timing_out.add("desk")
    try:
        partial = _summary(driver)
    finally:
        timing_out.clear()
    assert partial.items == ("chair", "lamp")

    full = _summary(driver)

    items = CATALOG["furniture"]
    assert full == Summary(tuple(items), tuple(map(_url, items)))
    assert calls == [("fetch", _url("desk"))]
