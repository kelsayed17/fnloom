"""Caching blocks: content-addressed branch keys, stored items, ordered collects."""

import warnings
from typing import TYPE_CHECKING, override

import pytest

import fnloom
from fnloom import CacheOutcome, MemoryCache
from fnloom._stores import MISSING
from helpers import make_module

if TYPE_CHECKING:
    from collections.abc import Sequence
    from types import ModuleType

    from fnloom import Driver


class CountingCache(MemoryCache):
    """A memory store that records each `get` and each `put`."""

    def __init__(self) -> None:
        """Starts empty, with empty records."""
        super().__init__()
        self.gets: list[tuple[bytes, object]] = []
        self.puts: list[tuple[bytes, object]] = []

    @override
    def get(self, key: bytes) -> object:
        value = super().get(key)
        self.gets.append((key, value))
        return value

    @override
    def put(self, key: bytes, value: object) -> None:
        self.puts.append((key, value))
        super().put(key, value)

    def forget_calls(self) -> None:
        """Clears the records, keeping the entries."""
        self.gets.clear()
        self.puts.clear()


ORDERED = """
from fnloom import Collect, Parallelizable



def picked(order: list[int]) -> Parallelizable[int]:
    print("picked")
    yield from order


def details(picked: int, rate: int) -> int:
    print(f"details {picked}")
    return picked * rate


def scaled(details: int) -> int:
    print(f"scaled {details}")
    return details + 1


def summary(scaled: Collect[int]) -> list[int]:
    print("summary")
    return list(scaled)
"""


def _cached(
    module: ModuleType,
    store: MemoryCache | None = None,
    *,
    recompute: Sequence[str] = (),
    disable: Sequence[str] = (),
) -> Driver:
    """A driver over `module` caching in `store`, with `with_cache`'s lists."""
    cache = MemoryCache() if store is None else store
    builder = fnloom.Builder().with_modules(module)
    return builder.with_cache(cache, recompute=recompute, disable=disable).build()


def _called(capsys: pytest.CaptureFixture[str]) -> list[str]:
    """The nodes that ran since the last call, in order: each prints a line."""
    return capsys.readouterr().out.splitlines()


def _run(
    driver: Driver, capsys: pytest.CaptureFixture[str], order: list[int]
) -> list[str]:
    """Runs `summary` over `order`, checks its value, and returns the calls made."""
    _called(capsys)
    result = driver.execute(["summary"], inputs={"order": order, "rate": 10})
    assert result == {"summary": [item * 10 + 1 for item in order]}
    return _called(capsys)


def test_a_second_run_hits_every_branch_and_runs_nothing_inside(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The expand replays, each branch hits, and so does the collect."""
    module = make_module("cached_blocks_again", ORDERED)
    driver = _cached(module)
    assert _run(driver, capsys, [1, 2]) == [
        "picked",
        "details 1",
        "scaled 10",
        "details 2",
        "scaled 20",
        "summary",
    ]
    assert _run(driver, capsys, [1, 2]) == []


def test_an_appended_item_computes_one_branch(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A new item runs its own branch only; the collect runs over all of them."""
    module = make_module("cached_blocks_appended", ORDERED)
    driver = _cached(module)
    _run(driver, capsys, [1, 2])
    assert _run(driver, capsys, [1, 2, 3]) == [
        "picked",
        "details 3",
        "scaled 30",
        "summary",
    ]


@pytest.mark.parametrize("summary", ["default", "recompute"])
def test_an_overridden_expand_keys_each_branch_by_its_item(
    capsys: pytest.CaptureFixture[str], summary: str
) -> None:
    """An expand override's items key their branches, as computed items do."""
    module = make_module(f"cached_blocks_overridden_{summary}", ORDERED)
    driver = _cached(module, recompute=["summary"] if summary == "recompute" else ())
    for items, ran in (
        ([1, 2], ["details 1", "scaled 10", "details 2", "scaled 20", "summary"]),
        ([1, 2], [] if summary == "default" else ["summary"]),
        ([2, 3], ["details 3", "scaled 30", "summary"]),
    ):
        _called(capsys)
        result = driver.execute(
            ["summary"], inputs={"rate": 10}, overrides={"picked": items}
        )
        assert result == {"summary": [item * 10 + 1 for item in items]}
        assert _called(capsys) == ran
    explained = driver.explain_cache(
        ["summary"], inputs={"rate": 10}, overrides={"picked": [2, 3]}
    )
    assert explained["details"] == CacheOutcome("unknown", "keyed per item of 'picked'")


def test_a_moved_item_hits(capsys: pytest.CaptureFixture[str]) -> None:
    """A branch key holds the item's fingerprint, not its index."""
    module = make_module("cached_blocks_moved", ORDERED)
    driver = _cached(module)
    _run(driver, capsys, [1, 2])
    assert _run(driver, capsys, [3, 2]) == [
        "picked",
        "details 3",
        "scaled 30",
        "summary",
    ]


def test_a_hit_at_a_blocks_exit_skips_its_upstream_in_that_branch(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A collected node that hits is never computed, and neither is its upstream."""
    module = make_module("cached_blocks_exit", ORDERED)
    store = CountingCache()
    driver = _cached(module, store)
    _run(driver, capsys, [1, 2])
    assert [value for _, value in store.puts] == [10, 11, 20, 21, [1, 2], [11, 21]]
    store.forget_calls()
    assert _run(driver, capsys, [1, 2, 3]) == [
        "picked",
        "details 3",
        "scaled 30",
        "summary",
    ]
    # Each old branch looks up its exit only: `details` is never asked there.
    found = [value for _, value in store.gets if value is not MISSING]
    assert found == [11, 21]


UNFINGERPRINTABLE = """
from fnloom import Collect, Parallelizable, cache



class Model:
    def __init__(self, value: int) -> None:
        self.value = value


@cache(behavior="{behavior}")
def picked(values: list[int]) -> Parallelizable[Model]:
    print("picked")
    for value in values:
        yield Model(value)


def details(picked: Model) -> int:
    print(f"details {{picked.value}}")
    return picked.value


def summary(details: Collect[int]) -> list[int]:
    print("summary")
    return list(details)
"""


@pytest.mark.parametrize("behavior", ["recompute", "disable"])
def test_unfingerprintable_items_without_stored_items_are_uncacheable(
    behavior: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """With its items not stored, a branch of an item with no fingerprint never hits.

    Nothing is stored for it, nothing warns, and a later run over other items at
    the same indices computes them: never a hit by index (Review Focus 3).
    """
    module = make_module(
        f"cached_blocks_unversioned_{behavior}",
        UNFINGERPRINTABLE.format(behavior=behavior),
    )
    store = CountingCache()
    driver = _cached(module, store)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert driver.execute(["summary"], inputs={"values": [1, 2]}) == {
            "summary": [1, 2]
        }
        assert store.puts == []
        _called(capsys)
        assert driver.execute(["summary"], inputs={"values": [5, 6]}) == {
            "summary": [5, 6]
        }
    assert _called(capsys) == ["picked", "details 5", "details 6", "summary"]
    assert store.puts == []


def test_the_positional_fallback_holds_under_stored_items(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Items with no fingerprint, stored with their expand, key branches by index."""
    module = make_module(
        "cached_blocks_positional", UNFINGERPRINTABLE.format(behavior="default")
    )
    driver = _cached(module)
    driver.execute(["summary"], inputs={"values": [1, 2]})
    _called(capsys)
    assert driver.execute(["summary"], inputs={"values": [1, 2]}) == {"summary": [1, 2]}
    assert _called(capsys) == []
    # Other inputs make another expand key, so no branch hits by index.
    assert driver.execute(["summary"], inputs={"values": [5, 6]}) == {"summary": [5, 6]}
    assert _called(capsys) == ["picked", "details 5", "details 6", "summary"]


@pytest.mark.parametrize(
    ("decorated", "lists"),
    [
        ("recompute", {}),
        ("disable", {}),
        (None, {"recompute": ["details"]}),
        (None, {"disable": ["details"]}),
    ],
)
def test_policies_apply_per_branch(
    decorated: str | None,
    lists: dict[str, list[str]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A `recompute` or `disable` block node runs in every branch, every run.

    What reads it in the branch is then uncacheable, as outside a block; the
    expand, upstream of it, still replays.
    """
    source = ORDERED
    if decorated is not None:
        source = source.replace(
            "def details(", f'@cache(behavior="{decorated}")\ndef details('
        ).replace("import Collect,", "import Collect, cache,")
    module = make_module(f"cached_blocks_policy_{decorated}_{len(lists)}", source)
    driver = _cached(
        module, recompute=lists.get("recompute", ()), disable=lists.get("disable", ())
    )
    _run(driver, capsys, [1, 2])
    assert _run(driver, capsys, [1, 2]) == [
        "details 1",
        "scaled 10",
        "details 2",
        "scaled 20",
        "summary",
    ]


class _Recorder(fnloom.Hook):
    """Records each node event as `(event, node, branch, cache status)`."""

    def __init__(self) -> None:
        """Starts with no events."""
        self.events: list[tuple[str, str, tuple[int, ...], str | None]] = []

    @override
    def before_node(self, event: fnloom.NodeStart) -> None:
        status = None if event.cache is None else event.cache.status
        self.events.append(("before", event.node.name, event.branch, status))

    @override
    def after_node(self, event: fnloom.NodeEnd) -> None:
        status = None if event.cache is None else event.cache.status
        self.events.append(("after", event.node.name, event.branch, status))


def test_branch_hits_fire_hit_events() -> None:
    """A branch hit fires both node events, `cache` a hit, with its branch (Q85)."""
    module = make_module("cached_blocks_events", ORDERED)
    recorder = _Recorder()
    driver = (
        fnloom.Builder()
        .with_modules(module)
        .with_cache(MemoryCache())
        .with_hooks(recorder)
        .build()
    )
    driver.execute(["summary"], inputs={"order": [1], "rate": 10})
    assert recorder.events == [
        ("before", "picked", (), "miss"),
        ("after", "picked", (), "miss"),
        ("before", "details", (0,), "miss"),
        ("after", "details", (0,), "miss"),
        ("before", "scaled", (0,), "miss"),
        ("after", "scaled", (0,), "miss"),
        ("before", "summary", (), "miss"),
        ("after", "summary", (), "miss"),
    ]
    recorder.events.clear()
    driver.execute(["summary"], inputs={"order": [1, 2], "rate": 10})
    assert recorder.events == [
        ("before", "picked", (), "miss"),
        ("after", "picked", (), "miss"),
        ("before", "scaled", (0,), "hit"),
        ("after", "scaled", (0,), "hit"),
        ("before", "details", (1,), "miss"),
        ("after", "details", (1,), "miss"),
        ("before", "scaled", (1,), "miss"),
        ("after", "scaled", (1,), "miss"),
        ("before", "summary", (), "miss"),
        ("after", "summary", (), "miss"),
    ]
    recorder.events.clear()
    driver.execute(["summary"], inputs={"order": [1, 2], "rate": 10})
    assert recorder.events == [
        ("before", "picked", (), "hit"),
        ("after", "picked", (), "hit"),
        ("before", "scaled", (0,), "hit"),
        ("after", "scaled", (0,), "hit"),
        ("before", "scaled", (1,), "hit"),
        ("after", "scaled", (1,), "hit"),
        ("before", "summary", (), "hit"),
        ("after", "summary", (), "hit"),
    ]


NESTED = """
from fnloom import Collect, Parallelizable



def outer(sizes: list[int]) -> Parallelizable[int]:
    print("outer")
    yield from sizes


def inner(outer: int) -> Parallelizable[int]:
    print(f"inner {outer}")
    yield from range(outer)


def leaf(inner: int, outer: int) -> int:
    print(f"leaf {outer} {inner}")
    return outer * 10 + inner


def per_outer(leaf: Collect[int]) -> int:
    print(f"per_outer {len(leaf)}")
    return sum(leaf)


def everything(per_outer: Collect[int]) -> list[int]:
    print("everything")
    return list(per_outer)
"""


def test_nested_blocks_hit_per_branch(capsys: pytest.CaptureFixture[str]) -> None:
    """A nested block keys each inner branch, and its collect from stored items."""
    module = make_module("cached_blocks_nested", NESTED)
    driver = _cached(module)
    assert driver.execute(["everything"], inputs={"sizes": [1, 2]}) == {
        "everything": [10, 41]
    }
    _called(capsys)
    assert driver.execute(["everything"], inputs={"sizes": [1, 2]}) == {
        "everything": [10, 41]
    }
    assert _called(capsys) == []
    assert driver.execute(["everything"], inputs={"sizes": [2, 3]}) == {
        "everything": [41, 93]
    }
    assert _called(capsys) == [
        "outer",
        "inner 3",
        "leaf 3 0",
        "leaf 3 1",
        "leaf 3 2",
        "per_outer 3",
        "everything",
    ]


# The expand's stored items, ordered collects and `explain_cache` (task 12).


def test_the_expand_is_replayed_from_its_stored_items(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A stored expand is not called again: its items replay, as an output too."""
    module = make_module("cached_blocks_replayed", ORDERED)
    driver = _cached(module)
    inputs = {"order": [1, 2], "rate": 10}
    assert driver.execute(["picked", "summary"], inputs=inputs) == {
        "picked": [1, 2],
        "summary": [11, 21],
    }
    _called(capsys)
    assert driver.execute(["picked", "summary"], inputs=inputs) == {
        "picked": [1, 2],
        "summary": [11, 21],
    }
    assert _called(capsys) == []


LAZY = """
import weakref

from fnloom import Collect, Parallelizable, cache

ALIVE = weakref.WeakSet()
MOST = [0]


class Item:
    def __init__(self, value: int) -> None:
        self.value = value
        ALIVE.add(self)
        MOST[0] = max(MOST[0], len(ALIVE))

    def __fnloom_fingerprint__(self) -> int:
        return self.value


@cache(behavior="disable")
def picked(n: int) -> Parallelizable[Item]:
    print("picked")
    for value in range(n):
        yield Item(value)


def details(picked: Item) -> int:
    return picked.value


def total(details: Collect[int]) -> int:
    return sum(details)
"""


def test_a_lazy_expand_marked_disable_stores_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A `disable` expand's 10,000 items are read one at a time, and never stored."""
    module = make_module("cached_blocks_lazy", LAZY)
    store = CountingCache()
    driver = _cached(module, store)
    assert driver.execute(["total"], inputs={"n": 10_000}) == {"total": 49_995_000}
    # The item being read, and at most the one before it, not yet collected.
    assert module.MOST[0] <= 2  # noqa: PLR2004  # one item, and its predecessor
    assert not any(isinstance(value, list) for _, value in store.puts)
    _called(capsys)
    assert driver.execute(["total"], inputs={"n": 10_000}) == {"total": 49_995_000}
    assert _called(capsys) == ["picked"]


def test_reordered_items_hit_branches_and_miss_the_collect(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Reordered items hit every branch; the collect runs again in the new order.

    Review Focus 2: a collect's version hashes its branches in branch order.
    """
    module = make_module("cached_blocks_reordered", ORDERED)
    driver = _cached(module)
    _run(driver, capsys, [1, 2, 3])
    assert _run(driver, capsys, [3, 1, 2]) == ["picked", "summary"]


def test_explain_cache_counts_branches_and_runs_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`explain_cache` counts a block's branches from its stored items; nothing runs."""
    module = make_module("cached_blocks_explained", ORDERED)
    driver = _cached(module)
    inputs = {"order": [1, 2], "rate": 10}
    _called(capsys)
    before = driver.explain_cache(["summary"], inputs=inputs)
    assert _called(capsys) == []
    unknown = "the expand node runs, so its branches are known only at run"
    assert before == {
        "picked": CacheOutcome("miss", unknown),
        "details": CacheOutcome("unknown", "keyed per item of 'picked'"),
        "scaled": CacheOutcome("unknown", "keyed per item of 'picked'"),
        "summary": CacheOutcome("unknown", "reads items that are known only at run"),
    }
    _run(driver, capsys, [1, 2])
    _called(capsys)
    after = driver.explain_cache(["summary"], inputs=inputs)
    assert _called(capsys) == []
    assert after["picked"] == CacheOutcome(
        "hit", "branches: 2 hit, 0 miss, 0 uncacheable"
    )
    assert after["summary"] == CacheOutcome("hit", None)
    grown = driver.explain_cache(["summary"], inputs={"order": [1, 2, 3], "rate": 10})
    assert grown["picked"] == CacheOutcome("miss", unknown)
