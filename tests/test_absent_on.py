"""Partial results: `absent_on`, `with_absent_on` and error absences (task 11, Q133).

Each behaviour runs on the three drivers: `Driver`, `Driver` with `with_threads(4)`,
and `AsyncDriver`. Every wait is bounded, as the `threads` marker asks.
"""

import asyncio
import copy
import logging
import pickle
import threading
from typing import TYPE_CHECKING, Any, Literal, override

import pytest

import fnloom
import picklable_absent
from fnloom import ABSENT, _core
from helpers import make_module, run

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

pytestmark = pytest.mark.threads

type Kind = Literal["sync", "threads", "async"]

KINDS: tuple[Kind, ...] = ("sync", "threads", "async")


def driver_of(
    kind: Kind, builder: fnloom.Builder[Any]
) -> fnloom.Driver[Any] | fnloom.AsyncDriver[Any]:
    """`builder`'s driver of `kind`."""
    if kind == "threads":
        return builder.with_threads(4).build()
    return builder.build_async() if kind == "async" else builder.build()


def executed(
    kind: Kind,
    builder: fnloom.Builder[Any],
    outputs: Sequence[str],
    inputs: Mapping[str, object] | None = None,
) -> Any:  # noqa: ANN401  # the driver's result
    """One run of `outputs` on `builder`'s driver of `kind`."""
    return run(driver_of(kind, builder), outputs, inputs=inputs)


class Ends(fnloom.Hook):
    """Every node end, and the run's end."""

    def __init__(self) -> None:
        """Starts with nothing seen."""
        self.nodes: list[fnloom.NodeEnd] = []
        self.runs: list[fnloom.RunEnd] = []
        self._lock = threading.Lock()

    @override
    def after_node(self, event: fnloom.NodeEnd) -> None:
        with self._lock:
            self.nodes.append(event)

    @override
    def after_run(self, event: fnloom.RunEnd) -> None:
        self.runs.append(event)


# `a` is absent on a `KeyError` and raises one; each consumer reads it its own way.
RULES = """
from fnloom import Absent, absent_on


@absent_on(KeyError)
def a(x: int) -> int | Absent:
    raise KeyError(x)


def req(a: int) -> int:
    return a + 1


def opt(x: int, a: int = 7) -> int:
    return x + a


def adm(a: int | Absent) -> object | Absent:
    return a


def after(req: int) -> int:
    return req * 2
"""


@pytest.mark.parametrize("kind", KINDS)
def test_a_listed_error_becomes_absence_and_its_consumers_follow_the_rules(
    kind: Kind,
) -> None:
    """`a` is absent; `req` and `after` are skipped, `opt` defaults, `adm` gets it."""
    builder = fnloom.Builder().with_modules(make_module(f"rules_{kind}", RULES))
    got = executed(kind, builder, ["a", "req", "opt", "adm", "after"], {"x": 1})
    assert got == {"a": ABSENT, "req": ABSENT, "opt": 8, "adm": ABSENT, "after": ABSENT}
    views = builder.build().nodes
    assert views["a"].may_be_absent
    assert views["after"].may_be_absent
    assert not views["opt"].may_be_absent


SUBCLASS = """
from fnloom import Absent, absent_on

RAISED = [KeyError("missing")]


@absent_on(LookupError)
def a(x: int) -> int | Absent:
    raise RAISED[0]


def b(a: int | Absent) -> object | Absent:
    return a
"""


@pytest.mark.parametrize("kind", KINDS)
def test_a_subclass_is_caught_and_an_unlisted_error_fails_the_run(kind: Kind) -> None:
    """A `KeyError` is a `LookupError`; a `ValueError` is raised as itself."""
    module = make_module(f"subclass_{kind}", SUBCLASS)
    builder = fnloom.Builder().with_modules(module)
    assert executed(kind, builder, ["b"], {"x": 1}) == {"b": ABSENT}
    module.RAISED[0] = ValueError("not listed")
    with pytest.raises(ValueError, match="not listed"):
        executed(kind, builder, ["b"], {"x": 1})


FAN = """
import time

from fnloom import Absent, Collect, Parallelizable, absent_on

CALLS = []


def item(n: int) -> Parallelizable[int]:
    yield from range(n)


@absent_on(TimeoutError)
def detail(item: int) -> int | Absent:
    CALLS.append(item)
    # Later items finish first, with threads.
    time.sleep(0.002 * (10 - item % 10))
    if item % 2:
        raise TimeoutError(f"item {item} timed out")
    return item * 10


def summary(detail: Collect[int]) -> list[int]:
    return list(detail)


def every(detail: Collect[int | Absent]) -> list[object]:
    return list(detail)
"""


@pytest.mark.parametrize("kind", KINDS)
def test_collect_over_a_partial_fan_out_gets_the_branches_that_succeeded(
    kind: Kind,
) -> None:
    """`Collect[T]` drops absent branches; `Collect[T | Absent]` keeps their place."""
    builder = fnloom.Builder().with_modules(make_module(f"fan_{kind}", FAN))
    got = executed(kind, builder, ["summary", "every"], {"n": 6})
    assert got == {
        "summary": [0, 20, 40],
        "every": [0, ABSENT, 20, ABSENT, 40, ABSENT],
    }


@pytest.mark.parametrize("kind", KINDS)
def test_after_node_reports_ok_false_and_run_end_lists_the_error(kind: Kind) -> None:
    """The run succeeds, and each error absence is listed in reference order."""
    ends = Ends()
    builder = (
        fnloom.Builder().with_modules(make_module(f"ends_{kind}", FAN)).with_hooks(ends)
    )
    assert executed(kind, builder, ["summary"], {"n": 4}) == {"summary": [0, 20]}
    failed = [e for e in ends.nodes if not e.ok]
    assert sorted((e.node.name, e.branch) for e in failed) == [
        ("detail", (1,)),
        ("detail", (3,)),
    ]
    assert all(isinstance(e.error, TimeoutError) and e.result is None for e in failed)
    (end,) = ends.runs
    assert end.ok
    assert end.error is None
    assert [(n, b, str(e)) for n, b, e in end.absent_errors] == [
        ("detail", (1,), "item 1 timed out"),
        ("detail", (3,), "item 3 timed out"),
    ]
    by_branch = {e.branch: e.error for e in failed}
    assert [e for _, _, e in end.absent_errors] == [by_branch[1,], by_branch[3,]]


@pytest.mark.parametrize("kind", KINDS)
def test_log_failures_logs_an_error_absence(
    kind: Kind, caplog: pytest.LogCaptureFixture
) -> None:
    """`LogFailures` logs each error absence, as any `ok=False` node (Q88)."""
    builder = (
        fnloom.Builder()
        .with_modules(make_module(f"logged_{kind}", FAN))
        .with_hooks(fnloom.LogFailures())
    )
    with caplog.at_level(logging.ERROR):
        executed(kind, builder, ["summary"], {"n": 2})
    assert "item 1 timed out" in caplog.text
    assert "detail" in caplog.text


CACHED = """
from fnloom import Absent, Collect, Parallelizable, absent_on, cache

FAILING = {1}
CALLS = []


def item(n: int) -> Parallelizable[int]:
    yield from range(n)


# Reads globals it changes: versioned by hand, so it is cached.
@cache(version="1")
@absent_on(KeyError)
def detail(item: int) -> int | Absent:
    CALLS.append(("detail", item))
    if item in FAILING:
        raise KeyError(item)
    return item * 10


# Reads globals it changes: versioned by hand, so it is cached.
@cache(version="1")
def scaled(detail: int) -> int:
    CALLS.append(("scaled", None))
    return detail + 1


# Reads globals it changes: versioned by hand, so it is cached.
@cache(version="1")
def summary(scaled: Collect[int]) -> list[int]:
    CALLS.append(("summary", None))
    return list(scaled)


# Reads globals it changes: versioned by hand, so it is cached.
@cache(version="1")
@absent_on(KeyError)
def outer(n: int) -> int | Absent:
    CALLS.append(("outer", None))
    if FAILING:
        raise KeyError(n)
    return n


# Reads globals it changes: versioned by hand, so it is cached.
@cache(version="1")
def fallback(n: int, outer: int = -1) -> int:
    CALLS.append(("fallback", None))
    return n + outer
"""


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("store", ["memory", "disk"])
def test_an_error_absence_is_never_stored_and_poisons_its_downstream(
    kind: Kind,
    store: str,
    tmp_path: Any,  # noqa: ANN401  # pytest's path
) -> None:
    """A second run calls the node again, and every node computed from it misses."""
    module = make_module(f"cached_{kind}_{store}", CACHED)
    cache = fnloom.MemoryCache() if store == "memory" else fnloom.DiskCache(tmp_path)
    builder = fnloom.Builder().with_modules(module).with_cache(cache)
    driver = driver_of(kind, builder)
    outputs = ["summary", "fallback"]
    assert run(driver, outputs, inputs={"n": 3}) == {"summary": [1, 21], "fallback": 2}
    module.CALLS.clear()
    # Again, still failing: the absence and what it made are computed again.
    assert run(driver, outputs, inputs={"n": 3}) == {"summary": [1, 21], "fallback": 2}
    assert sorted(module.CALLS, key=repr) == sorted(
        [("detail", 1), ("summary", None), ("outer", None), ("fallback", None)],
        key=repr,
    )
    module.CALLS.clear()
    # Once it succeeds, the whole result is computed, then served from the store.
    module.FAILING.clear()
    assert run(driver, outputs, inputs={"n": 3}) == {
        "summary": [1, 11, 21],
        "fallback": 6,
    }
    module.CALLS.clear()
    assert run(driver, outputs, inputs={"n": 3}) == {
        "summary": [1, 11, 21],
        "fallback": 6,
    }
    assert module.CALLS == []


NESTED_CACHED = """
from fnloom import Absent, Collect, Parallelizable, absent_on, cache

FAILING = {(1, 0)}
CALLS = []


def outer(n: int) -> Parallelizable[int]:
    yield from range(n)


def inner(outer: int) -> Parallelizable[int]:
    yield from range(2)


# Reads globals it changes: versioned by hand, so it is cached.
@cache(version="1")
@absent_on(KeyError)
def leaf(outer: int, inner: int) -> int | Absent:
    CALLS.append((outer, inner))
    if (outer, inner) in FAILING:
        raise KeyError((outer, inner))
    return 10 * outer + inner


# Reads globals it changes: versioned by hand, so it is cached.
@cache(version="1")
def per_outer(leaf: Collect[int]) -> list[int]:
    CALLS.append(("per_outer", list(leaf)))
    return list(leaf)


def total(per_outer: Collect[list[int]]) -> list[list[int]]:
    return list(per_outer)
"""


@pytest.mark.parametrize("kind", KINDS)
def test_only_the_branches_an_absence_reaches_are_left_unstored(kind: Kind) -> None:
    """Branch (1, 0)'s absence poisons its collect, branch 1's, and the total only."""
    module = make_module(f"nested_cached_{kind}", NESTED_CACHED)
    builder = fnloom.Builder().with_modules(module).with_cache(fnloom.MemoryCache())
    driver = driver_of(kind, builder)
    want = {"total": [[0, 1], [11], [20, 21]]}
    assert run(driver, ["total"], inputs={"n": 3}) == want
    module.CALLS.clear()
    assert run(driver, ["total"], inputs={"n": 3}) == want
    # Branches 0 and 2 are hits, collects included; branch 1 runs again.
    assert sorted(module.CALLS, key=repr) == sorted(
        [(1, 0), ("per_outer", [11])], key=repr
    )


SINGLE_FLIGHT = """
import threading

from fnloom import Absent, Collect, Parallelizable, absent_on, cache

CALLS = []
LOCK = threading.Lock()


def item(n: int) -> Parallelizable[int]:
    yield from [7] * n


# Reads globals it changes: versioned by hand, so it is cached.
@cache(version="1")
@absent_on(KeyError)
def detail(item: int) -> int | Absent:
    with LOCK:
        CALLS.append(item)
    raise KeyError(item)


def summary(detail: Collect[int | Absent]) -> list[object]:
    return list(detail)
"""


@pytest.mark.parametrize("kind", KINDS)
def test_an_error_absence_is_never_shared_by_single_flight(kind: Kind) -> None:
    """Branches of one key each run: a failure, absent or not, is never shared (Q85)."""
    module = make_module(f"flight_{kind}", SINGLE_FLIGHT)
    builder = fnloom.Builder().with_modules(module).with_cache(fnloom.MemoryCache())
    assert executed(kind, builder, ["summary"], {"n": 4}) == {"summary": [ABSENT] * 4}
    assert module.CALLS == [7] * 4


EXPANDS = """
from fnloom import Absent, Collect, Parallelizable, absent_on

MODE = ["before"]


def _items():
    yield 1
    if MODE[0] == "while":
        raise ConnectionError("lost midway")
    yield 2


@absent_on(ConnectionError)
def item(n: int) -> Parallelizable[int] | Absent:
    if MODE[0] == "before":
        raise ConnectionError("no listing")
    return _items()


def detail(item: int) -> int:
    return item * 10


def req(detail: Collect[int]) -> list[int]:
    return list(detail)


def opt(detail: Collect[int] = []) -> list[int]:
    return list(detail)


def adm(detail: Collect[int] | Absent) -> object | Absent:
    return detail
"""


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("mode", ["before", "while", "never"])
def test_an_expand_absent_on_its_error_has_no_branches(kind: Kind, mode: str) -> None:
    """Before or while it yields: absent, its branches so far dropped (§7.4)."""
    module = make_module(f"expands_{kind}_{mode}", EXPANDS)
    module.MODE[0] = mode
    ends = Ends()
    builder = fnloom.Builder().with_modules(module).with_hooks(ends)
    got = executed(kind, builder, ["req", "opt", "adm"], {"n": 0})
    if mode == "never":
        assert got == {"req": [10, 20], "opt": [10, 20], "adm": [10, 20]}
        assert ends.runs[0].absent_errors == ()
        return
    assert got == {"req": ABSENT, "opt": [], "adm": ABSENT}
    ((node, branch, error),) = ends.runs[0].absent_errors
    assert (node, branch, type(error)) == ("item", (), ConnectionError)


NESTED = """
from fnloom import Absent, Collect, Parallelizable, absent_on


def outer(n: int) -> Parallelizable[int]:
    yield from range(n)


def inner(outer: int) -> Parallelizable[int]:
    yield from range(3)


@absent_on(ArithmeticError)
def leaf(outer: int, inner: int) -> int | Absent:
    return 10 * outer // (inner - outer)


def per_outer(leaf: Collect[int]) -> list[int]:
    return list(leaf)


def total(per_outer: Collect[list[int]]) -> list[list[int]]:
    return list(per_outer)
"""


@pytest.mark.parametrize("kind", KINDS)
def test_an_error_absence_in_a_nested_block_drops_only_its_branch(kind: Kind) -> None:
    """A `ZeroDivisionError` is an `ArithmeticError`: each diagonal branch drops."""
    ends = Ends()
    module = make_module(f"nested_{kind}", NESTED)
    builder = fnloom.Builder().with_modules(module).with_hooks(ends)
    got = executed(kind, builder, ["total"], {"n": 3})
    assert got == {"total": [[0, 0], [-10, 10], [-10, -20]]}
    assert [(n, b) for n, b, _ in ends.runs[0].absent_errors] == [
        ("leaf", (0, 0)),
        ("leaf", (1, 1)),
        ("leaf", (2, 2)),
    ]


def test_absent_on_needs_an_annotation_admitting_absent() -> None:
    """Checked at build, naming the node and saying to add `| Absent` (O11)."""
    module = make_module(
        "unadmitted",
        """
from fnloom import absent_on


@absent_on(KeyError)
def plain(x: int) -> int:
    return x
""",
    )
    with pytest.raises(ValueError, match=r"'plain' is @absent_on.*add `\| Absent`"):
        fnloom.Builder().with_modules(module).build()
    with pytest.raises(ValueError, match="'plain' is @absent_on"):
        fnloom.Builder().with_modules(module).build_async()


def test_absent_on_with_an_unresolved_return_annotation_is_refused_naming_it() -> None:
    """An annotation that did not resolve can't be told to admit `Absent` (Q81)."""
    module = make_module(
        "unresolved_absent_on",
        """
from __future__ import annotations

from typing import TYPE_CHECKING

from fnloom import absent_on

if TYPE_CHECKING:
    from decimal import Decimal


@absent_on(KeyError)
def amount(x: int) -> Decimal:
    raise KeyError(x)
""",
    )
    with pytest.raises(ValueError, match=r"'amount'.*'Decimal' did not resolve"):
        fnloom.Builder().with_modules(module).build()


def test_the_decorator_checks_nothing_until_build() -> None:
    """Decorating evaluates no annotation: a forward reference resolves at build."""
    module = make_module(
        "forward_absent_on",
        """
from fnloom import Absent, absent_on


@absent_on(KeyError)
def later(x: int) -> Later | Absent:
    raise KeyError(x)


class Later:
    pass


def use(later: Later | Absent) -> object | Absent:
    return later
""",
    )
    driver = fnloom.Builder().with_modules(module).build()
    assert driver.execute(["use"], inputs={"x": 1}) == {"use": ABSENT}


TAGGED = """
from fnloom import Absent, tag


@tag(io="net")
def fetched(x: int) -> int:
    raise TimeoutError(x)


@tag(io="disk")
def read(x: int) -> int:
    raise TimeoutError(x)


def from_net(fetched: int = 0) -> int:
    return fetched


def from_disk(read: int) -> int:
    return read
"""


@pytest.mark.parametrize("kind", KINDS)
def test_with_absent_on_covers_tagged_nodes_whatever_their_annotation(
    kind: Kind,
) -> None:
    """`fetched` is `-> int`, covered by its tag; `read` is not covered, and fails."""
    module = make_module(f"tagged_{kind}", TAGGED)
    builder = fnloom.Builder().with_modules(module)
    covered = builder.with_absent_on(TimeoutError, tags={"io": "net"})
    assert executed(kind, covered, ["from_net"], {"x": 1}) == {"from_net": 0}
    assert covered.build().nodes["fetched"].may_be_absent
    with pytest.raises(TimeoutError):
        executed(kind, covered, ["from_disk"], {"x": 1})
    # No tags: every node; a list of values matches any of them.
    for every in (
        builder.with_absent_on(TimeoutError),
        builder.with_absent_on(TimeoutError, tags={"io": ["disk", "net"]}),
    ):
        got = executed(kind, every, ["from_net", "from_disk"], {"x": 1})
        assert got == {"from_net": 0, "from_disk": ABSENT}


BOTH = """
from fnloom import Absent, absent_on

RAISED = [KeyError("own")]


@absent_on(KeyError)
def a(x: int) -> int | Absent:
    raise RAISED[0]


def b(a: int | Absent) -> object | Absent:
    return a
"""


@pytest.mark.parametrize("kind", KINDS)
def test_a_node_with_both_policies_is_absent_on_their_union(kind: Kind) -> None:
    """Its own `KeyError`, and the driver's `OSError`; a `ValueError` still fails."""
    module = make_module(f"both_{kind}", BOTH)
    builder = fnloom.Builder().with_modules(module).with_absent_on(OSError)
    for raised in (KeyError("own"), TimeoutError("the driver's")):
        module.RAISED[0] = raised
        assert executed(kind, builder, ["b"], {"x": 1}) == {"b": ABSENT}
    module.RAISED[0] = ValueError("neither")
    with pytest.raises(ValueError, match="neither"):
        executed(kind, builder, ["b"], {"x": 1})


RETURNS_ABSENT = """
from fnloom import ABSENT


def a(x: int) -> int:
    return ABSENT


def b(a: int = 3) -> int:
    return a
"""


@pytest.mark.parametrize("kind", KINDS)
def test_a_covered_node_still_cannot_return_absent_itself(kind: Kind) -> None:
    """The return check's `TypeError` fails the run, even with `TypeError` listed."""
    module = make_module(f"returns_absent_{kind}", RETURNS_ABSENT)
    builder = fnloom.Builder().with_modules(module).with_absent_on(TypeError, KeyError)
    with pytest.raises(TypeError, match=r"'a' returned fnloom\.ABSENT"):
        executed(kind, builder, ["b"], {"x": 1})


def test_a_sync_node_returning_an_awaitable_is_never_made_absent() -> None:
    """The async driver's refusal is fnloom's own error, not the node's."""
    module = make_module(
        "returns_awaitable",
        """
import asyncio


def a(x: int) -> int:
    return asyncio.sleep(0)


def b(a: int = 3) -> int:
    return a
""",
    )
    builder = fnloom.Builder().with_modules(module).with_absent_on(TypeError)
    for sync_nodes in ("inline", "thread"):
        driver = builder.build_async(sync_nodes=sync_nodes)
        with pytest.raises(TypeError, match="returned coroutine, an awaitable"):
            asyncio.run(driver.execute(["b"], inputs={"x": 1}))


GENERATOR_ABSENT = """
from fnloom import ABSENT, Collect, Parallelizable


def item(n: int) -> Parallelizable[int]:
    yield 1
    return ABSENT


def each(item: int) -> int:
    return item


def total(each: Collect[int]) -> list[int]:
    return list(each)
"""


@pytest.mark.parametrize("kind", KINDS)
def test_a_covered_expand_still_cannot_decline_itself(kind: Kind) -> None:
    """An expand returning `ABSENT` it may not is refused, `TypeError` listed or not."""
    module = make_module(f"generator_absent_{kind}", GENERATOR_ABSENT)
    builder = fnloom.Builder().with_modules(module).with_absent_on(TypeError)
    with pytest.raises(TypeError, match=r"'item' returned fnloom\.ABSENT"):
        executed(kind, builder, ["total"], {"n": 1})


@pytest.mark.parametrize(
    "error",
    [
        BaseException,
        KeyboardInterrupt,
        SystemExit,
        GeneratorExit,
        asyncio.CancelledError,
    ],
)
def test_only_exception_subclasses_may_be_listed(error: type[BaseException]) -> None:
    """An interrupt, an exit, a close or a cancellation never becomes an absence."""
    with pytest.raises(TypeError, match=f"not {error.__qualname__}"):
        fnloom.absent_on(error)  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match=f"not {error.__qualname__}"):
        fnloom.Builder().with_absent_on(error)  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="not 42"):
        fnloom.absent_on(42)  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="needs at least one"):
        fnloom.Builder().with_absent_on()
    with pytest.raises(TypeError, match="tagged: 'io' must be"):
        fnloom.Builder().with_absent_on(KeyError, tags={"io": 3})  # ty: ignore[invalid-argument-type]


def test_absent_on_stacks_and_given_twice_adds_up() -> None:
    """Two `@absent_on` add their classes, in either order."""

    @fnloom.absent_on(KeyError)
    @fnloom.absent_on(ValueError, KeyError)
    def first(x: int) -> int:
        return x

    @fnloom.absent_on(ValueError)
    @fnloom.absent_on(KeyError)
    def second(x: int) -> int:
        return x

    for fn in (first, second):
        recorded = fnloom._decorators.decorations(fn).absent_on  # noqa: SLF001  # the record
        assert set(recorded) == {KeyError, ValueError}


def test_with_absent_on_survives_pickle_copy_rebuild_and_build_async() -> None:
    """Every copy of a driver keeps the rule, and makes the same nodes absent."""
    builder = (
        fnloom.Builder()
        .with_modules(picklable_absent)
        .with_absent_on(TimeoutError, tags={"io": fnloom.ANY})
    )
    original = builder.build()
    want = {"summary": [1, 31]}
    assert original.execute(["summary"], inputs={"n": 4}) == want
    for other in (
        pickle.loads(pickle.dumps(original)),  # noqa: S301  # our own pickle
        copy.copy(original),
        copy.deepcopy(original),
        original.rebuild(),
        pickle.loads(pickle.dumps(builder)).build(),  # noqa: S301  # our own pickle
        builder.with_threads(2).build(),
    ):
        assert other.execute(["summary"], inputs={"n": 4}) == want
    assert asyncio.run(builder.build_async().execute(["summary"], inputs={"n": 4})) == (
        want
    )
    async_copy = pickle.loads(pickle.dumps(builder.build_async()))  # noqa: S301  # our own pickle
    assert asyncio.run(async_copy.execute(["summary"], inputs={"n": 4})) == want


RANDOM_FAN = """
import random
import time

from fnloom import Absent, Collect, Parallelizable, absent_on


def item(n: int) -> Parallelizable[int]:
    yield from range(n)


@absent_on(KeyError)
def detail(item: int) -> int | Absent:
    time.sleep(random.random() / 500)
    if item % 3 == 1:
        raise KeyError(item)
    return item


def skipped_after(detail: int) -> int:
    return detail


def summary(skipped_after: Collect[int]) -> list[int]:
    return list(skipped_after)
"""


def test_absent_errors_are_equal_over_worker_counts() -> None:
    """Values, `absent_errors` and `skipped` are those of one worker, for 1, 2 and 8."""
    module = make_module("random_fan", RANDOM_FAN)
    seen = []
    for workers in (None, 1, 2, 8):
        ends = Ends()
        builder = fnloom.Builder().with_modules(module).with_hooks(ends)
        if workers is not None:
            builder = builder.with_threads(workers)
        result = builder.build().execute(["summary"], inputs={"n": 30})
        (end,) = ends.runs
        seen.append(
            (
                result,
                [(n, b, str(e)) for n, b, e in end.absent_errors],
                end.skipped,
            )
        )
    assert seen[0][1] == [("detail", (i,), str(i)) for i in range(1, 30, 3)]
    assert seen[0][2] == tuple(("skipped_after", (i,)) for i in range(1, 30, 3))
    assert all(other == seen[0] for other in seen)


ORDERED = """
import threading

from fnloom import Absent, Collect, Parallelizable, absent_on

GATE = threading.Event()


def item(n: int) -> Parallelizable[int]:
    yield from range(n)


@absent_on(KeyError)
def detail(item: int) -> int | Absent:
    if item == 0:
        # The absence comes last, after the failure: it still isn't the run's error.
        GATE.wait(0.5)
        raise KeyError(item)
    if item == 1:
        try:
            raise ValueError("the real failure")
        finally:
            GATE.set()
    return item


def summary(detail: Collect[int]) -> list[int]:
    return list(detail)
"""


@pytest.mark.parametrize("kind", KINDS)
def test_a_real_failure_is_raised_whatever_absence_comes_before_it(kind: Kind) -> None:
    """An error absence is no failure: the lowest key's real failure leads."""
    module = make_module(f"ordered_{kind}", ORDERED)
    ends = Ends()
    builder = fnloom.Builder().with_modules(module).with_hooks(ends)
    with pytest.raises(ValueError, match="the real failure") as raised:
        executed(kind, builder, ["summary"], {"n": 3})
    assert raised.value.__notes__ == [
        "while computing 'detail' for branch (1,) of 'item'"
    ]
    (end,) = ends.runs
    assert not end.ok
    assert end.error is raised.value


RAISING_HOOK = """
from fnloom import Absent, absent_on


@absent_on(KeyError)
def a(x: int) -> int | Absent:
    raise KeyError(x)


def b(a: int = 1) -> int:
    return a
"""


class Raising(fnloom.Hook):
    """Raises in `after_node` for a failed node."""

    @override
    def after_node(self, event: fnloom.NodeEnd) -> None:
        if not event.ok:
            msg = f"the hook saw {event.node.name}"
            raise RuntimeError(msg)


@pytest.mark.parametrize("kind", KINDS)
def test_a_hook_raising_on_an_absence_fails_the_run_with_its_own_error(
    kind: Kind,
) -> None:
    """The hook's error is raised, never the node's absorbed one."""
    module = make_module(f"raising_{kind}", RAISING_HOOK)
    store = fnloom.MemoryCache()
    builder = (
        fnloom.Builder().with_modules(module).with_hooks(Raising()).with_cache(store)
    )
    with pytest.raises(RuntimeError, match="the hook saw a"):
        executed(kind, builder, ["b"], {"x": 1})


STOPPED = """
import time

import fnloom
from fnloom import Absent, Collect, Parallelizable, absent_on

INNER = []


def slow(x: int) -> int:
    start = time.monotonic()
    while not fnloom.cancelled() and time.monotonic() - start < 5:
        time.sleep(0.005)
    return x


def after(slow: int) -> int:
    return slow


def picked(n: int) -> Parallelizable[int]:
    yield from range(n)


@absent_on(RuntimeError)
def detail(picked: int) -> int | Absent:
    if picked == 0:
        return INNER[0].execute(["after"], inputs={"x": 7})["after"]
    time.sleep(0.05)
    raise ValueError(f"the real failure, in branch {picked}")


def summary(detail: Collect[int]) -> list[int]:
    return list(detail)
"""


def test_a_stopped_inner_run_is_never_an_absence() -> None:
    """Its `StoppedError` is a `RuntimeError`, but only says that this run stopped."""
    module = make_module("stopped_absent", STOPPED)
    builder = fnloom.Builder().with_modules(module)
    module.INNER.append(builder.with_threads(2).build())
    ends = Ends()
    driver = builder.with_hooks(ends).with_threads(4).build()
    for _ in range(3):
        with pytest.raises(ValueError, match="the real failure, in branch 1"):
            driver.execute(["summary"], inputs={"n": 2})
    assert all(end.absent_errors == () for end in ends.runs)


TIMED = """
import threading

from fnloom import Absent, absent_on

RELEASE = threading.Event()


@absent_on(TimeoutError)
def own(x: int) -> int | Absent:
    raise TimeoutError("the node's own")


@absent_on(TimeoutError)
def hung(x: int) -> int | Absent:
    RELEASE.wait(10)
    return x


def use(own: int = 5, hung: int = 6) -> int:
    return own + hung
"""


def test_a_deadline_is_never_an_absence_but_a_nodes_own_timeout_is() -> None:
    """`timeout=` raises `TimeoutError` from the run; the node's own one is absent."""
    module = make_module("timed_absent", TIMED)
    driver = fnloom.Builder().with_modules(module).with_threads(2).build()
    module.RELEASE.set()
    assert driver.execute(["use"], inputs={"x": 1}, timeout=5) == {"use": 6}
    module.RELEASE.clear()
    try:
        with pytest.raises(TimeoutError, match="did not finish within its timeout"):
            driver.execute(["use"], inputs={"x": 1}, timeout=0.2)
    finally:
        module.RELEASE.set()


LIMITED = """
from fnloom import Absent, Collect, Parallelizable, absent_on, limit


def item(n: int) -> Parallelizable[int]:
    yield from range(n)


@limit(1)
@absent_on(KeyError)
def guarded(item: int) -> int | Absent:
    if item % 2:
        raise KeyError(item)
    return item


def summary(guarded: Collect[int]) -> list[int]:
    return list(guarded)
"""


@pytest.mark.parametrize("kind", KINDS)
def test_an_error_absence_gives_its_limit_slot_back(kind: Kind) -> None:
    """Every branch runs under the limit, and the driver holds no slot after."""
    module = make_module(f"limited_absent_{kind}", LIMITED)
    driver = driver_of(kind, fnloom.Builder().with_modules(module))
    assert run(driver, ["summary"], inputs={"n": 6}) == {"summary": [0, 2, 4]}
    inner = driver._driver if isinstance(driver, fnloom.AsyncDriver) else driver  # noqa: SLF001  # its sync driver
    workers = inner._workers  # noqa: SLF001  # the driver's own limits
    assert workers is not None
    assert _core.held_slots(workers.limits()) == (0, {"guarded": 0}, {})


STEPPED = """
from fnloom import mutate, pipe_output, step, tag


def _double(v: int) -> int:
    return v * 2


@tag(io="net")
@pipe_output(step(_double))
def fetched(x: int) -> int:
    raise TimeoutError(x)


@tag(io="net")
def mutated(x: int) -> int:
    raise TimeoutError(x)


@mutate(mutated)
def _plus(v: int) -> int:
    return v + 1


def use(fetched: int = 0, mutated: int = 0) -> int:
    return fetched + mutated
"""


@pytest.mark.parametrize("kind", KINDS)
def test_a_tag_query_covers_a_function_with_steps(kind: Kind) -> None:
    """The node calling a tagged `@pipe_output` or `@mutate` function is covered."""
    module = make_module(f"stepped_{kind}", STEPPED)
    builder = (
        fnloom.Builder()
        .with_modules(module)
        .with_absent_on(TimeoutError, tags={"io": "net"})
    )
    assert executed(kind, builder, ["use"], {"x": 1}) == {"use": 0}


# fnloom's own checks, each failing inside a call or a pull of a covered node.
OWN_CHECKS = {
    "absent item": """
from fnloom import ABSENT, Collect, Parallelizable


def item(n: int) -> Parallelizable[int]:
    yield 1
    yield ABSENT


def each(item: int) -> int:
    return item


def out(each: Collect[int]) -> list[int]:
    return list(each)
""",
    "item type": """
from fnloom import Collect, Parallelizable


def item(n: int) -> Parallelizable[int]:
    yield "one"


def each(item: int) -> int:
    return item


def out(each: Collect[int]) -> list[int]:
    return list(each)
""",
    "failed check": """
from fnloom import check_output


@check_output(range=(0, 5))
def out(n: int) -> int:
    return n * 10
""",
    "missing field": """
from fnloom import extract_fields


@extract_fields({"p": int, "q": int})
def made(n: int) -> dict:
    return {"p": n}


def out(q: int) -> int:
    return q
""",
}


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("check", OWN_CHECKS)
def test_a_check_of_fnlooms_own_never_becomes_an_absence(
    kind: Kind, check: str
) -> None:
    """An item, a check or a field that fails fails the run, whatever is listed."""
    name = f"own_{check.replace(' ', '_')}_{kind}"
    builder = (
        fnloom.Builder()
        .with_modules(make_module(name, OWN_CHECKS[check]))
        .with_absent_on(TypeError, ValueError)
    )
    with pytest.raises((TypeError, ValueError)):
        executed(kind, builder, ["out"], {"n": 1})


def test_an_output_type_check_never_becomes_an_absence() -> None:
    """`with_output_type_checks`' mismatch is fnloom's `TypeError`, not the node's."""
    module = make_module(
        "own_output_type",
        """
def out(n: int) -> int:
    return "ten"
""",
    )
    builder = (
        fnloom.Builder()
        .with_modules(module)
        .with_output_type_checks()
        .with_absent_on(TypeError)
    )
    with pytest.raises(TypeError, match="out: returned str, expected int"):
        builder.build().execute(["out"], inputs={"n": 1})


CLOSED = """
from fnloom import Absent, Collect, Parallelizable, absent_on

CLOSED = []


class Listing:
    def __init__(self) -> None:
        self.given = 0

    def __iter__(self) -> "Listing":
        return self

    def __next__(self) -> int:
        self.given += 1
        if self.given == 2:
            raise ConnectionError("lost")
        return self.given

    def close(self) -> None:
        CLOSED.append(self.given)


@absent_on(ConnectionError)
def item(n: int) -> Parallelizable[int] | Absent:
    return Listing()


def each(item: int) -> int:
    return item


def out(each: Collect[int] | Absent) -> object | Absent:
    return each
"""


@pytest.mark.parametrize("kind", KINDS)
def test_an_iterator_absent_on_its_error_is_closed_once(kind: Kind) -> None:
    """A custom iterator that raised is closed, once, though the expand is absent."""
    module = make_module(f"closed_{kind}", CLOSED)
    builder = fnloom.Builder().with_modules(module)
    assert executed(kind, builder, ["out"], {"n": 1}) == {"out": ABSENT}
    assert module.CLOSED == [2]


NATIVE = """
from fnloom import Absent, absent_on, cache

FAILING = [True]
CALLS = []


@cache(version="1")
@absent_on(KeyError)
def a(x: int) -> int | Absent:
    if FAILING[0]:
        raise KeyError(x)
    return x


@cache(version="1")
def b(x: int, a: int = 0) -> int:
    CALLS.append("b")
    return x + a
"""


def test_a_plain_memory_cache_stores_nothing_made_from_an_absence() -> None:
    """With no expand, a `MemoryCache` is written natively, but not for this plan."""
    module = make_module("native_absent", NATIVE)
    driver = (
        fnloom.Builder().with_modules(module).with_cache(fnloom.MemoryCache()).build()
    )
    for _ in range(2):
        assert driver.execute(["b"], inputs={"x": 1}) == {"b": 1}
    assert module.CALLS == ["b", "b"]
    module.FAILING[0] = False
    for _ in range(2):
        assert driver.execute(["b"], inputs={"x": 1}) == {"b": 2}
    assert module.CALLS == ["b", "b", "b"]


MANY = """
import threading

from fnloom import Absent, Collect, Parallelizable, absent_on, cache

CALLS = []
LOCK = threading.Lock()


def item(n: int) -> Parallelizable[int]:
    yield from range(n)


# Reads globals it changes: versioned by hand, so it is cached.
@cache(version="1")
@absent_on(KeyError)
def detail(item: int, bad: int) -> int | Absent:
    with LOCK:
        CALLS.append((bad, item))
    if item == bad:
        raise KeyError(item)
    if item == -bad:
        raise ValueError(f"the real failure, item {item}")
    return item * 10


def summary(detail: Collect[int]) -> list[int]:
    return list(detail)
"""


@pytest.mark.parametrize("threads", [None, 1, 4])
def test_each_run_many_set_has_its_own_error_absences(threads: int | None) -> None:
    """Each set drops its own failed branch, lists its own error, and stores none."""
    module = make_module(f"many_{threads}", MANY)
    ends = Ends()
    builder = (
        fnloom.Builder()
        .with_modules(module)
        .with_hooks(ends)
        .with_cache(fnloom.MemoryCache())
    )
    if threads is not None:
        builder = builder.with_threads(threads)
    plan = builder.build().prepare(["summary"], inputs={"n": 3, "bad": 0})
    sets = [{"n": 3, "bad": bad} for bad in (0, 1, 2, 9)]
    want = [
        {"summary": [10, 20]},
        {"summary": [0, 20]},
        {"summary": [0, 10]},
        {"summary": [0, 10, 20]},
    ]
    assert plan.run_many(sets) == want
    by_set = sorted(
        (tuple((n, b) for n, b, _ in end.absent_errors) for end in ends.runs),
        key=repr,
    )
    assert by_set == sorted(
        [(("detail", (0,)),), (("detail", (1,)),), (("detail", (2,)),), ()], key=repr
    )
    module.CALLS.clear()
    # Again: each set's absent branch runs again, every other branch is a hit.
    assert plan.run_many(sets) == want
    assert sorted(module.CALLS) == [(0, 0), (1, 1), (2, 2)]
    # A real failure in one set is raised, noted with its set, whatever absences
    # the other sets have.
    failing = [{"n": 3, "bad": 1}, {"n": 3, "bad": -2}]
    with pytest.raises(ValueError, match="the real failure, item 2") as raised:
        plan.run_many(failing)
    assert "while running input set 1 of run_many" in raised.value.__notes__
