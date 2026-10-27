"""Tests for cached runs: keys, hit frontiers, policies, and failing stores."""

import os
import random
import threading
import warnings
from typing import TYPE_CHECKING, override

import pytest

from fnloom import (
    MISSING,
    Builder,
    CacheOutcome,
    CacheStore,
    CacheWarning,
    DiskCache,
    MemoryCache,
    build,
)
from helpers import make_module

if TYPE_CHECKING:
    from pathlib import Path
    from types import ModuleType

    from fnloom import Driver

THREADS = 8
RUNS_PER_THREAD = 200

CHAIN = """
def a(x: int) -> int:
    print("a")
    return x + 1

def b(a: int) -> int:
    print("b")
    return a * 10

def c(b: int) -> int:
    print("c")
    return b - 3
"""


class CountingCache(MemoryCache):
    """A memory store that records each `get`, with what it returned, and each `put`."""

    def __init__(self) -> None:
        """Starts empty, with empty records."""
        super().__init__()
        self.gets: list[tuple[bytes, object]] = []
        self.puts: list[bytes] = []

    @override
    def get(self, key: bytes) -> object:
        value = super().get(key)
        self.gets.append((key, value))
        return value

    @override
    def put(self, key: bytes, value: object) -> None:
        self.puts.append(key)
        super().put(key, value)

    def hits(self) -> list[object]:
        """The values every `get` so far returned, misses left out."""
        return [value for _, value in self.gets if value is not MISSING]

    def forget_calls(self) -> None:
        """Clears the records, keeping the entries."""
        self.gets.clear()
        self.puts.clear()


class RaisingCache:
    """A user store that raises from every call."""

    def get(self, key: bytes) -> object:
        """Fails."""
        raise OSError(key.hex())

    def put(self, key: bytes, value: object) -> None:
        """Fails."""
        msg = f"{key.hex()}: {type(value).__name__}"
        raise OSError(msg)

    def discard(self, key: bytes) -> None:
        """Fails."""
        raise OSError(key.hex())


def _cached(module: ModuleType, store: CacheStore) -> Driver:
    """A driver over `module` with the cache on, the store set before the modules."""
    return Builder().with_cache(store).with_modules(module).build()


def _called(capsys: pytest.CaptureFixture[str]) -> list[str]:
    """The nodes that ran since the last call, in order: each prints its name."""
    return capsys.readouterr().out.split()


def test_second_run_hits_and_calls_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    """A repeat with equal inputs is served from the store, calling no node."""
    driver = _cached(make_module("cached_chain", CHAIN), MemoryCache())
    first = driver.execute(["c"], inputs={"x": 1})
    assert _called(capsys) == ["a", "b", "c"]
    second = driver.execute(["c"], inputs={"x": 1})
    assert _called(capsys) == []
    assert second == first == {"c": 17}


def test_a_hit_on_the_output_loads_only_the_output() -> None:
    """The walk stops at the output's hit: one lookup, for `c`, and nothing else."""
    store = CountingCache()
    driver = _cached(make_module("cached_lookups", CHAIN), store)
    driver.execute(["c"], inputs={"x": 1})
    store.forget_calls()
    driver.execute(["c"], inputs={"x": 1})
    assert len(store.gets) == 1
    assert store.hits() == [17]
    assert store.puts == []


DIAMOND = """
def a(x: int) -> int:
    print("a")
    return x + 1

def b(y: int) -> int:
    print("b")
    return y * 2

def c(a: int, b: int) -> int:
    print("c")
    return a + b

def d(a: int) -> int:
    print("d")
    return -a
"""


def test_changed_input_reruns_only_its_downstream(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Changing `y` reruns `b` and `c`; `a` and `d`, upstream only of `x`, hit."""
    driver = _cached(make_module("cached_diamond", DIAMOND), MemoryCache())
    driver.execute(["c", "d"], inputs={"x": 1, "y": 2})
    assert sorted(_called(capsys)) == ["a", "b", "c", "d"]
    result = driver.execute(["c", "d"], inputs={"x": 1, "y": 3})
    assert sorted(_called(capsys)) == ["b", "c"]
    assert result == {"c": 8, "d": -2}


def test_two_nodes_with_equal_values_never_share_entries() -> None:
    """`1` and `"1"` keep their own types across runs (F1)."""
    module = make_module(
        "cached_equal",
        """
        def a() -> int:
            return 1

        def b() -> str:
            return "1"
        """,
    )
    driver = _cached(module, MemoryCache())
    driver.execute(["a", "b"])
    second = driver.execute(["a", "b"])
    assert second == {"a": 1, "b": "1"}
    assert type(second["a"]) is int
    assert type(second["b"]) is str


LOADER = """
from pathlib import Path

from fnloom import cache

@cache(behavior="recompute")
def text(path: str) -> str:
    print("text")
    return Path(path).read_text()

def words(text: str) -> int:
    print("words")
    return len(text.split())
"""


def test_recompute_runs_every_time_and_keys_downstream_by_result(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The loader always runs; its consumer reruns only when its result changed (F5)."""
    file = tmp_path / "words.txt"
    file.write_text("one two")
    driver = _cached(make_module("cached_loader", LOADER), MemoryCache())
    inputs = {"path": str(file)}
    assert driver.execute(["words"], inputs=inputs) == {"words": 2}
    assert _called(capsys) == ["text", "words"]
    assert driver.execute(["words"], inputs=inputs) == {"words": 2}
    assert _called(capsys) == ["text"]
    file.write_text("one two three")
    assert driver.execute(["words"], inputs=inputs) == {"words": 3}
    assert _called(capsys) == ["text", "words"]
    assert driver.execute(["text", "words"], inputs=inputs) == {
        "text": "one two three",
        "words": 3,
    }
    assert _called(capsys) == ["text"]


def test_a_recompute_node_behind_another_is_keyed_by_both_results(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Two loaders in a row: each runs every time; what reads them hits on results."""
    module = make_module(
        "cached_loaders",
        """
        from pathlib import Path

        from fnloom import cache

        @cache(behavior="recompute")
        def first(path: str) -> str:
            print("first")
            return Path(path).read_text()

        def middle(first: str) -> str:
            print("middle")
            return first.upper()

        @cache(behavior="recompute")
        def second(middle: str, other: str) -> str:
            print("second")
            return middle + Path(other).read_text()

        def last(second: str) -> int:
            print("last")
            return len(second)
        """,
    )
    one, two = tmp_path / "one.txt", tmp_path / "two.txt"
    one.write_text("ab")
    two.write_text("c")
    driver = _cached(module, MemoryCache())
    inputs = {"path": str(one), "other": str(two)}
    assert driver.execute(["last"], inputs=inputs) == {"last": 3}
    assert _called(capsys) == ["first", "middle", "second", "last"]
    assert driver.execute(["last"], inputs=inputs) == {"last": 3}
    assert _called(capsys) == ["first", "second"]
    one.write_text("abcd")
    assert driver.execute(["last"], inputs=inputs) == {"last": 5}
    assert _called(capsys) == ["first", "middle", "second", "last"]
    two.write_text("cd")
    assert driver.execute(["last"], inputs=inputs) == {"last": 6}
    assert _called(capsys) == ["first", "second", "last"]


SWITCHED = """
from pathlib import Path

from fnloom import cache

{decorator}
def text(path: str) -> str:
    return Path(path).read_text()

def shout(text: str) -> str:
    return text + "!"
"""


def test_a_policy_switch_never_mixes_key_spaces(tmp_path: Path) -> None:
    """Default, recompute, default: each run's consumer agrees with its loader (Q49).

    The policy is part of a node's key, so the last default run serves the first
    run's entries for both nodes, never the recompute run's `text` beside an older
    `shout`. A node that reads the outside world must stay `recompute`.
    """
    file = tmp_path / "text.txt"
    store = MemoryCache()
    seen: list[dict[str, object]] = []
    for run, decorator in enumerate(("", '@cache(behavior="recompute")', "")):
        file.write_text(str(run + 1))
        module = make_module("cached_switched", SWITCHED.format(decorator=decorator))
        driver = _cached(module, store)
        result = driver.execute(["text", "shout"], inputs={"path": str(file)})
        assert result["shout"] == f"{result['text']}!"
        seen.append(result)
    assert seen == [
        {"text": "1", "shout": "1!"},
        {"text": "2", "shout": "2!"},
        {"text": "1", "shout": "1!"},
    ]


CLIENT = """
from fnloom import cache

@cache(behavior="disable")
def client(url: str) -> object:
    print("client")
    return object()

def fetch(client: object, n: int) -> int:
    print("fetch")
    return n + 1

@cache(ignore=("client",))
def fetch_ignoring(client: object, n: int) -> int:
    print("fetch_ignoring")
    return n + 2
"""


def test_disable_never_stores_and_reruns_downstream(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A disabled node is never stored; its consumers rerun unless they ignore it."""
    store = CountingCache()
    driver = _cached(make_module("cached_client", CLIENT), store)
    inputs = {"url": "db://", "n": 1}
    outputs = ["fetch", "fetch_ignoring"]
    assert driver.execute(outputs, inputs=inputs) == {"fetch": 2, "fetch_ignoring": 3}
    assert sorted(_called(capsys)) == ["client", "fetch", "fetch_ignoring"]
    # One entry, for the consumer that ignores the client: the client has no key,
    # so neither has the other consumer.
    assert len(store.puts) == 1
    store.forget_calls()
    assert driver.execute(outputs, inputs=inputs) == {"fetch": 2, "fetch_ignoring": 3}
    assert sorted(_called(capsys)) == ["client", "fetch"]
    assert store.puts == []
    assert driver.execute(["fetch_ignoring"], inputs=inputs) == {"fetch_ignoring": 3}
    assert _called(capsys) == []


def test_uncacheable_inputs_and_globals_run(capsys: pytest.CaptureFixture[str]) -> None:
    """An input with no fingerprint, or a node reading a mutable global, always runs."""
    module = make_module(
        "cached_uncacheable",
        """
        SCALE = [2]

        class Opaque:
            def __init__(self, size: int) -> None:
                self.size = size

        def size(thing: Opaque) -> int:
            print("size")
            return thing.size

        def scaled(x: int) -> int:
            print("scaled")
            return x * SCALE[0]

        def total(size: int, scaled: int) -> int:
            print("total")
            return size + scaled
        """,
    )
    store = CountingCache()
    driver = _cached(module, store)
    thing = module.Opaque(5)
    assert driver.execute(["total"], inputs={"thing": thing, "x": 1}) == {"total": 7}
    assert sorted(_called(capsys)) == ["scaled", "size", "total"]
    assert store.puts == []
    thing.size = 6
    module.SCALE[0] = 3
    assert driver.execute(["total"], inputs={"thing": thing, "x": 1}) == {"total": 9}
    assert sorted(_called(capsys)) == ["scaled", "size", "total"]
    assert store.puts == []


def test_overrides(capsys: pytest.CaptureFixture[str]) -> None:
    """Overrides are checked, key what reads them, and a hit is not checked again."""
    driver = _cached(make_module("cached_overrides", CHAIN), MemoryCache())
    driver.execute(["c"], inputs={"x": 1})
    _called(capsys)
    # `b` would hit, and its override is still checked.
    with pytest.raises(ValueError, match="b: expected int, got str"):
        driver.execute(["c"], inputs={"x": 1}, overrides={"b": "20"})
    assert driver.execute(["c"], inputs={"x": 1}, overrides={"b": 5}) == {"c": 2}
    assert _called(capsys) == ["c"]
    assert driver.execute(["c"], inputs={"x": 1}, overrides={"b": 5}) == {"c": 2}
    assert _called(capsys) == []
    assert driver.execute(["c"], inputs={"x": 1}, overrides={"b": 6}) == {"c": 3}
    assert _called(capsys) == ["c"]
    # The override of `b` stored nothing under `b`'s key: the plain run still hits.
    assert driver.execute(["c"], inputs={"x": 1}) == {"c": 17}
    assert _called(capsys) == []


def test_a_hit_is_not_type_checked_again(capsys: pytest.CaptureFixture[str]) -> None:
    """A stored value that would fail an override's check is used as it was produced."""
    module = make_module(
        "cached_unchecked",
        """
        def a() -> int:
            print("a")
            return "not an int"

        def b(a: int) -> str:
            print("b")
            return a + "!"
        """,
    )
    driver = _cached(module, MemoryCache())
    with pytest.raises(ValueError, match="a: expected int, got str"):
        driver.execute(["b"], overrides={"a": "not an int"})
    assert driver.execute(["a"]) == {"a": "not an int"}
    _called(capsys)
    assert driver.execute(["b"]) == {"b": "not an int!"}
    assert _called(capsys) == ["b"]


FAILING = """
def a(x: int) -> int:
    print("a")
    return x

def b(a: int) -> int:
    print("b")
    if a < 0:
        msg = "negative"
        raise ArithmeticError(msg)
    return a * 2
"""


def test_a_failing_node_stores_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    """A node that raises is not stored and runs again; earlier entries are kept."""
    store = CountingCache()
    driver = _cached(make_module("cached_failing", FAILING), store)
    assert driver.execute(["b"], inputs={"x": 1}) == {"b": 2}
    stored = dict(store._entries)  # noqa: SLF001  # the entries, compared after a failure
    _called(capsys)
    store.forget_calls()
    with pytest.raises(ArithmeticError, match="negative"):
        driver.execute(["b"], inputs={"x": -1})
    assert _called(capsys) == ["a", "b"]
    # Only `a`, which succeeded, was stored.
    assert len(store.puts) == 1
    with pytest.raises(ArithmeticError, match="negative"):
        driver.execute(["b"], inputs={"x": -1})
    assert _called(capsys) == ["b"]
    assert all(store._entries[key] == entry for key, entry in stored.items())  # noqa: SLF001  # as above
    assert driver.execute(["b"], inputs={"x": 1}) == {"b": 2}
    assert _called(capsys) == []


def _refuse(value: object) -> bytes:
    """A serializer that cannot serialize anything, as pickle cannot a lock."""
    msg = f"cannot pickle {type(value).__name__!r} object"
    raise TypeError(msg)


def _unwritable(tmp_path: Path, kind: str) -> CacheStore:
    """A store whose every write fails: read-only, unserializable, or raising."""
    if kind == "raising":
        return RaisingCache()
    if kind == "unpicklable":
        return DiskCache(tmp_path / "cache", dumps=_refuse)
    directory = tmp_path / "read-only"
    directory.mkdir()
    directory.chmod(0o500)
    if os.access(directory, os.W_OK):
        pytest.skip("this user can write to a read-only directory")
    return DiskCache(directory)


@pytest.mark.parametrize("kind", ["read-only", "unpicklable", "raising"])
def test_store_failures_never_fail_the_run(
    tmp_path: Path, kind: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """A store that cannot write gives the right result, and one warning per node."""
    driver = _cached(
        make_module(f"cached_store_{kind}", CHAIN), _unwritable(tmp_path, kind)
    )
    for _ in range(2):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            assert driver.execute(["c"], inputs={"x": 1}) == {"c": 17}
        assert _called(capsys) == ["a", "b", "c"]
        assert [w.category for w in caught] == [CacheWarning] * 3


SPREAD = """
def a(x: int) -> int:
    return x + 1

def b(y: int) -> int:
    return y * 3

def c(a: int, b: int) -> int:
    return a * b

def d(a: int, c: int) -> int:
    return c - a

def e(d: int, y: int) -> int:
    return d + y
"""


@pytest.mark.parametrize("kind", ["memory", "disk"])
def test_threads_share_a_plan_and_a_store(tmp_path: Path, kind: str) -> None:
    """Threads running one prepared plan over one store all get the uncached results."""
    module = make_module(f"cached_threads_{kind}", SPREAD)
    outputs = ["c", "e"]
    cases = [{"x": x, "y": y} for x in range(5) for y in range(4)]
    plain = build(module)
    expected = [plain.execute(outputs, inputs=case) for case in cases]
    store = MemoryCache() if kind == "memory" else DiskCache(tmp_path / "cache")
    driver = _cached(module, store)
    prepared = driver.prepare(outputs, inputs=cases[0])
    wrong: list[tuple[dict[str, int], dict[str, object]]] = []
    start = threading.Barrier(THREADS)

    def work(seed: int) -> None:
        shuffled = random.Random(seed)  # noqa: S311  # a test's order, not a secret
        order = shuffled.choices(range(len(cases)), k=RUNS_PER_THREAD)
        start.wait()
        for index in order:
            result = prepared.run(inputs=cases[index])
            if result != expected[index]:
                wrong.append((cases[index], result))

    threads = [threading.Thread(target=work, args=(n,)) for n in range(THREADS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert wrong == []
    if kind == "disk":
        files = [p for p in (tmp_path / "cache").rglob("*") if p.is_file()]
        assert [p for p in files if p.name.endswith(".tmp")] == []
        # Five nodes for each of the twenty cases, whole and readable, less the
        # nodes whose keys two cases share: `a` per x, `b` per y.
        assert len(files) == 5 + 4 + 3 * len(cases)
        assert all(store.get(bytes.fromhex(p.name)) is not MISSING for p in files)


ROWS = """
from fnloom import parameterize, value

@parameterize(one={"k": value(1)}, two={"k": value(TWO)})
def scaled(x: int, k: int) -> int:
    print(f"k={k}")
    return x * k
"""


def test_rows_and_value_constants_are_keyed(capsys: pytest.CaptureFixture[str]) -> None:
    """Two rows of one function keep their own entries; a changed constant misses."""
    store = MemoryCache()
    driver = _cached(make_module("cached_rows", "TWO = 2\n" + ROWS), store)
    assert driver.execute(["one", "two"], inputs={"x": 5}) == {"one": 5, "two": 10}
    assert sorted(_called(capsys)) == ["k=1", "k=2"]
    assert driver.execute(["one", "two"], inputs={"x": 5}) == {"one": 5, "two": 10}
    assert _called(capsys) == []
    driver = _cached(make_module("cached_rows", "TWO = 3\n" + ROWS), store)
    assert driver.execute(["one", "two"], inputs={"x": 5}) == {"one": 5, "two": 15}
    assert _called(capsys) == ["k=3"]


FAILING_FINGERPRINTS = """
from fnloom import parameterize, value

class Broken:
    def __fnloom_fingerprint__(self) -> bytes:
        return 1 // 0

BROKEN = Broken()

@parameterize(row={"k": value(BROKEN)})
def constant(x: int, k: Broken) -> int:
    print("constant")
    return x

def default(x: int, k: Broken = BROKEN) -> int:
    print("default")
    return x + 1

def plain(x: int) -> int:
    print("plain")
    return x + 2
"""


def test_a_failing_fingerprint_function_builds_and_runs(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A constant or default whose fingerprint raises makes its node uncacheable."""
    module = make_module("cached_broken", FAILING_FINGERPRINTS)
    with pytest.warns(CacheWarning) as caught:
        driver = _cached(module, MemoryCache())
    constant, default = sorted(str(w.message) for w in caught)
    assert constant.startswith(
        "cannot fingerprint value() of parameter 'k' of node 'row'"
    )
    assert default.startswith("cannot read the code version of 'default'")
    assert all("ZeroDivisionError" in m for m in (constant, default))
    # Both reported at the `build` call.
    assert {w.filename for w in caught} == {__file__}
    outputs = ["row", "default", "plain"]
    for _ in range(2):
        result = driver.execute(outputs, inputs={"x": 1})
        assert result == {"row": 1, "default": 2, "plain": 3}
    # The two uncacheable nodes run each time; `plain` hits the second time.
    assert sorted(_called(capsys)) == [
        "constant",
        "constant",
        "default",
        "default",
        "plain",
    ]


EXPLAINED = """
from fnloom import cache

SCALE = [2]

class Model:
    pass

@cache(behavior="recompute")
def load(path: str) -> str:
    print("load")
    return path.upper()

def parsed(load: str) -> int:
    print("parsed")
    return len(load)

def summary(parsed: int) -> int:
    print("summary")
    return parsed + 1

def base(x: int) -> int:
    print("base")
    return x + 1

def doubled(base: int) -> int:
    print("doubled")
    return base * 2

def scaled(x: int) -> int:
    print("scaled")
    return x * SCALE[0]

def counted(items: list[Model]) -> int:
    print("counted")
    return len(items)

@cache(behavior="disable")
def client(x: int) -> object:
    print("client")
    return object()

def fetched(client: object, base: int) -> int:
    print("fetched")
    return base

def mixed(parsed: int, scaled: int) -> int:
    print("mixed")
    return parsed + scaled
"""


def test_explain_names_every_outcome_and_runs_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Each computed node's outcome, in name order, before and after a run (§6)."""
    module = make_module("cached_explained", EXPLAINED)
    store = CountingCache()
    driver = _cached(module, store)
    outputs = ["summary", "doubled", "counted", "fetched", "mixed"]
    inputs = {"path": "a b", "x": 1, "items": [module.Model()]}
    before = driver.explain_cache(outputs, inputs=inputs)
    assert _called(capsys) == []
    assert store.puts == []
    downstream = CacheOutcome("unknown", "downstream of recompute node 'load'")
    unchanging = {
        "client": CacheOutcome("uncacheable", "policy disable"),
        "counted": CacheOutcome("uncacheable", "items[0]: Model"),
        "fetched": CacheOutcome("uncacheable", "reads uncacheable 'client'"),
        "load": CacheOutcome("miss", "policy recompute"),
        # An uncacheable source outweighs one whose version is not known yet.
        "mixed": CacheOutcome("uncacheable", "reads uncacheable 'scaled'"),
        "parsed": downstream,
        "summary": downstream,
    }
    scaled = before.pop("scaled")
    assert scaled.status == "uncacheable"
    assert scaled.reason is not None
    assert "'SCALE'" in scaled.reason
    assert list(before) == sorted(before)
    miss = CacheOutcome("miss", None)
    assert before == {**unchanging, "base": miss, "doubled": miss}
    driver.execute(outputs, inputs=inputs)
    _called(capsys)
    store.forget_calls()
    after = driver.explain_cache(outputs, inputs=inputs)
    assert _called(capsys) == []
    assert store.puts == []
    hit = CacheOutcome("hit", None)
    assert after == {**unchanging, "base": hit, "doubled": hit, "scaled": scaled}
    assert isinstance(after["base"], tuple)
    # An overridden node is not computed, and what reads it is keyed by its value.
    overridden = driver.explain_cache(
        ["doubled"], inputs={"x": 1}, overrides={"base": 5}
    )
    assert overridden == {"doubled": miss}
    assert _called(capsys) == []


def test_explain_checks_its_request_and_treats_a_store_error_as_a_miss() -> None:
    """The run's checks apply; a store that raises warns and is a miss."""
    module = make_module("cached_explain_errors", CHAIN)
    driver = _cached(module, MemoryCache())
    with pytest.raises(ValueError, match="x: expected int, got str"):
        driver.explain_cache(["c"], inputs={"x": "1"})
    with pytest.raises(ValueError, match="inputs"):
        driver.explain_cache(["c"], inputs={})
    driver = _cached(module, RaisingCache())
    with pytest.warns(CacheWarning) as caught:
        outcomes = driver.explain_cache(["c"], inputs={"x": 1})
    assert [str(w.message).split(" from")[0] for w in caught] == [
        f"cannot load node {name!r}" for name in ("a", "b", "c")
    ]
    miss = CacheOutcome("miss", None)
    assert outcomes == {"a": miss, "b": miss, "c": miss}


def test_explain_without_a_store_is_a_value_error() -> None:
    """Only a driver built with a cache can explain one."""
    driver = build(make_module("uncached_explain", CHAIN))
    with pytest.raises(ValueError, match="with_cache"):
        driver.explain_cache(["c"], inputs={"x": 1})


def test_explain_names_the_nearest_recompute_node() -> None:
    """A `recompute` node behind another is a miss; what reads it names it."""
    module = make_module(
        "cached_explain_loaders",
        """
        from fnloom import cache

        @cache(behavior="recompute")
        def first(path: str) -> str:
            return path

        @cache(behavior="recompute")
        def second(first: str, other: object) -> str:
            return first

        def last(second: str, first: str) -> int:
            return len(second + first)
        """,
    )
    driver = _cached(module, MemoryCache())
    outcomes = driver.explain_cache(["last"], inputs={"path": "p", "other": 1})
    recompute = CacheOutcome("miss", "policy recompute")
    assert outcomes == {
        "first": recompute,
        "last": CacheOutcome("unknown", "downstream of recompute node 'second'"),
        "second": recompute,
    }
    # A `recompute` node with no key is never stored: uncacheable, not a miss.
    outcomes = driver.explain_cache(["second"], inputs={"path": "p", "other": object()})
    assert outcomes["second"] == CacheOutcome("uncacheable", "other: object")
