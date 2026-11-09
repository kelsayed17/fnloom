"""Typed absence: a node may decline, and each consumer's signature decides (Q135)."""

import asyncio
import copy
import importlib
import importlib.util
import inspect
import os
import pickle
import subprocess
import sys
import typing
from pathlib import Path
from typing import Annotated, Any, override

import pytest

import export_golden
import fnloom
from fnloom import ABSENT, Absent, _absence, _core, _export
from fnloom._decorators import Source, Value
from fnloom._scan import core_inputs
from fnloom._view import Param
from helpers import DRIVER_KINDS, DriverKind, built, make_module, run

# Imported with this module, so a test's module cleanup never drops it half-way.
_PANDAS = importlib.util.find_spec("pandas") is not None
if _PANDAS:
    importlib.import_module("pandas")
_needs_pandas = pytest.mark.skipif(not _PANDAS, reason="needs pandas")

if typing.TYPE_CHECKING:
    import types

_CONSUMERS = """
from fnloom import ABSENT, Absent

calls = []


def listing(wanted: bool) -> list[str] | Absent:
    return ["a", "b"] if wanted else ABSENT


def count(listing: list[str]) -> int:
    calls.append("count")
    return len(listing)


def shown(listing: list[str] | Absent) -> str:
    calls.append("shown")
    return "none" if isinstance(listing, Absent) else ",".join(listing)


def first(listing: list[str] = ["default"]) -> str:
    calls.append("first")
    return listing[0]


def doubled(count: int) -> int:
    calls.append("doubled")
    return 2 * count
"""


def _consumers(
    name: str, driver_kind: DriverKind = "sync"
) -> tuple[fnloom.Driver[Any] | fnloom.AsyncDriver[Any], list[str]]:
    module = make_module(f"{name}_{driver_kind}", _CONSUMERS)
    calls = typing.cast("list[str]", module.calls)
    return _driven(driver_kind, module), calls


def _driven(
    driver_kind: DriverKind, module: types.ModuleType
) -> fnloom.Driver[Any] | fnloom.AsyncDriver[Any]:
    """`module`'s driver of `driver_kind`."""
    return built(driver_kind, fnloom.Builder().with_modules(module))


@pytest.mark.parametrize("driver_kind", DRIVER_KINDS)
def test_a_plain_consumer_of_a_declined_node_is_skipped(
    driver_kind: DriverKind,
) -> None:
    """`count` and `doubled` after it never run, and both come back `ABSENT`."""
    built, calls = _consumers("absence_skipped", driver_kind)
    result = run(built, ["doubled", "count"], inputs={"wanted": False})
    assert result == {"doubled": ABSENT, "count": ABSENT}
    assert result["count"] is ABSENT
    assert calls == []


@pytest.mark.parametrize("driver_kind", DRIVER_KINDS)
def test_a_present_value_reaches_every_consumer(driver_kind: DriverKind) -> None:
    """A declining node that does produce a value is read like any other."""
    built, calls = _consumers("absence_present", driver_kind)
    result = run(built, ["doubled", "shown", "first"], inputs={"wanted": True})
    assert result == {"doubled": 4, "shown": "a,b", "first": "a"}
    assert sorted(calls) == ["count", "doubled", "first", "shown"]


@pytest.mark.parametrize("driver_kind", DRIVER_KINDS)
def test_a_consumer_admitting_absent_receives_it(driver_kind: DriverKind) -> None:
    """A parameter annotated `list[str] | Absent` is passed `ABSENT` itself."""
    module = make_module(
        "absence_admits",
        """
from fnloom import ABSENT, Absent

seen = []


def listing() -> list[str] | Absent:
    return ABSENT


def shown(listing: list[str] | Absent) -> str:
    seen.append(listing)
    return "none"
""",
    )
    assert run(_driven(driver_kind, module), ["shown"]) == {"shown": "none"}
    assert len(module.seen) == 1
    assert module.seen[0] is ABSENT


@pytest.mark.parametrize("driver_kind", DRIVER_KINDS)
def test_a_defaulted_consumer_gets_its_default(driver_kind: DriverKind) -> None:
    """The argument is left out, so the function's own default applies."""
    built, calls = _consumers("absence_default", driver_kind)
    assert run(built, ["first"], inputs={"wanted": False}) == {"first": "default"}
    assert calls == ["first"]


@pytest.mark.parametrize("driver_kind", DRIVER_KINDS)
def test_returning_absent_without_admitting_it_is_a_type_error(
    driver_kind: DriverKind,
) -> None:
    """The return check runs on every computed node, always."""
    module = make_module(
        "absence_unadmitted",
        """
from fnloom import ABSENT


def listing() -> list[str]:
    return ABSENT
""",
    )
    built = _driven(driver_kind, module)
    with pytest.raises(TypeError) as raised:
        run(built, ["listing"])
    assert str(raised.value) == (
        "node 'listing' returned fnloom.ABSENT, but its return annotation does "
        "not admit fnloom.Absent"
    )


def test_may_be_absent_is_known_at_build() -> None:
    """Declining, then each rule: a required read spreads it, the others don't."""
    built, _ = _consumers("absence_known")
    flags = {name: view.may_be_absent for name, view in built.nodes.items()}
    assert flags == {
        "count": True,
        "doubled": True,
        "first": False,
        "listing": True,
        "shown": False,
        "wanted": False,
    }
    params = {p.name: p.admits_absent for p in built.nodes["shown"].params}
    assert params == {"listing": True}
    assert not built.nodes["count"].params[0].admits_absent


_SHAPES = """
from fnloom import ABSENT, Absent, group, mutate, parameterize, pipe_input, source, step
from fnloom import value


def listing(wanted: bool) -> list[str] | Absent:
    return ["a", "b"] if wanted else ABSENT


@parameterize(row_len={"xs": source("listing")}, fixed_len={"xs": value(["z"])})
def lengthed(xs: list[str]) -> int:
    return len(xs)


@parameterize(grouped={"xs": group(source("listing"), source("extra"))})
def joined(xs: list[list[str]]) -> int:
    return sum(len(x) for x in xs)


def counted(listing: list[str]) -> int:
    return len(listing)


@mutate(counted)
def _plus(v: int) -> int:
    return v + 1


def _with(v: int, other: list[str]) -> int:
    return v + len(other)


@pipe_input(step(_with, other=source("listing")))
def piped(start: int) -> int:
    return start
"""


def _agreed(driver: fnloom.Driver[Any] | fnloom.AsyncDriver[Any]) -> list[str]:
    """The core's set, checked against the views' `may_be_absent`."""
    if isinstance(driver, fnloom.AsyncDriver):
        driver = driver._driver  # noqa: SLF001  # the sync driver it wraps
    graph = driver._graph  # noqa: SLF001  # the native graph on purpose
    assert isinstance(graph, _core.Graph)
    found = graph.may_be_absent()
    assert found == sorted(
        name for name, view in driver.nodes.items() if view.may_be_absent
    )
    return found


def test_the_core_and_the_views_agree_on_what_may_be_absent() -> None:
    """The native graph computes the same set as the views, sorted."""
    built, _ = _consumers("absence_agree")
    assert _agreed(built) == ["count", "doubled", "listing"]


@pytest.mark.parametrize("driver_kind", DRIVER_KINDS)
def test_the_core_and_the_views_agree_through_decorators(
    driver_kind: DriverKind,
) -> None:
    """A `@parameterize` row, a `group(...)`, a `@mutate` target and a pipe step."""
    built = _driven(driver_kind, make_module("absence_agree_shapes", _SHAPES))
    assert _agreed(built) == [
        "counted",
        "counted.raw",
        "counted.with_plus",
        "grouped",
        "listing",
        "piped",
        "piped.with_with",
        "row_len",
    ]
    wanted = ["row_len", "fixed_len", "grouped", "counted", "piped"]
    supplied = {"extra": ["q"], "start": 1}
    assert run(built, wanted, inputs={"wanted": False, **supplied}) == {
        "row_len": ABSENT,
        "fixed_len": 1,
        "grouped": ABSENT,
        "counted": ABSENT,
        "piped": ABSENT,
    }
    assert run(built, wanted, inputs={"wanted": True, **supplied}) == {
        "row_len": 2,
        "fixed_len": 1,
        "grouped": 3,
        "counted": 3,
        "piped": 3,
    }


type _MaybeFloat = float | Absent


def test_admits_absent_reads_the_top_level_only() -> None:
    """Unions, `Annotated` and PEP 695 aliases are read through; generics are not."""
    admits = [
        Absent,
        float | Absent,
        typing.Optional[Absent],  # noqa: UP045  # the old spelling on purpose
        Annotated[float | Absent, "meta"],
        _MaybeFloat,
        _MaybeFloat | None,
    ]
    refuses = [
        float,
        list[float | Absent],
        dict[str, Absent],
        float | None,
        "float | Absent",
        None,
    ]
    assert [_absence.admits_absent(a) for a in admits] == [True] * len(admits)
    assert [_absence.admits_absent(a) for a in refuses] == [False] * len(refuses)
    assert _absence.without_absent(float | Absent) is float
    assert _absence.without_absent(float | int | Absent) == float | int
    assert _absence.without_absent(list[float | Absent]) == list[float | Absent]
    assert _absence.without_absent(Absent) is typing.Any
    assert _absence.without_absent(Annotated[Absent, "meta"]) is typing.Any


def test_absent_is_a_singleton_with_no_truth_value() -> None:
    """Pickle and copy give `ABSENT` back; `bool(ABSENT)` says how to test for it."""
    assert repr(ABSENT) == "fnloom.ABSENT"
    assert type(ABSENT) is Absent
    assert pickle.loads(pickle.dumps(ABSENT)) is ABSENT  # noqa: S301  # our own bytes
    assert pickle.loads(pickle.dumps([ABSENT, {"k": ABSENT}]))[1]["k"] is ABSENT  # noqa: S301  # our own bytes
    assert copy.copy(ABSENT) is ABSENT
    assert copy.deepcopy([ABSENT])[0] is ABSENT
    with pytest.raises(TypeError, match="is ABSENT"):
        bool(ABSENT)
    with pytest.raises(TypeError):
        type(ABSENT)()
    with pytest.raises(TypeError):
        Absent()  # ty: ignore[missing-argument]  # the stub refuses it too
    with pytest.raises(TypeError):
        type("Sub", (Absent,), {})  # ty: ignore[subclass-of-final-class]  # refused on purpose


@pytest.mark.parametrize("driver_kind", DRIVER_KINDS)
def test_t_or_absent_feeds_t_on_an_edge(driver_kind: DriverKind) -> None:
    """Edge checks read `float | Absent` as `float`, for each kind of consumer."""
    module = make_module(
        "absence_edges",
        """
from fnloom import ABSENT, Absent


def price() -> float | Absent:
    return ABSENT


def plain(price: float) -> float:
    return price


def admitting(price: float | Absent) -> bool:
    return isinstance(price, Absent)


def defaulted(price: float = 0.0) -> float:
    return price
""",
    )
    built = _driven(driver_kind, module)
    assert run(built, ["plain", "admitting", "defaulted"]) == {
        "plain": ABSENT,
        "admitting": True,
        "defaulted": 0.0,
    }
    refused = make_module(
        "absence_edges_refused",
        """
from fnloom import Absent


def price() -> float | Absent:
    return 1.0


def label(price: str) -> str:
    return price
""",
    )
    with pytest.raises(ValueError, match=r"label\.price requires str"):
        _driven(driver_kind, refused)


def test_a_default_beside_admitting_absent_is_still_shadowed() -> None:
    """Q76: a parameter admitting `Absent` receives `ABSENT`; its default is unused."""
    module = make_module(
        "absence_shadowed",
        """
from fnloom import Absent


def price() -> float | Absent:
    return 1.0


def admitting(price: float | Absent = 0.0) -> float:
    return 2.0
""",
    )
    with pytest.raises(ValueError, match="shadowed default: parameter 'price'"):
        fnloom.build(module)


@pytest.mark.parametrize("driver_kind", DRIVER_KINDS)
def test_a_node_that_is_absent_alone_feeds_any_parameter(
    driver_kind: DriverKind,
) -> None:
    """`-> Absent` never gives a value: the edge check passes, and `use` is skipped."""
    module = make_module(
        "absence_alone",
        """
from fnloom import ABSENT, Absent


def never() -> Absent:
    return ABSENT


def use(never: int) -> int:
    return never


def fallback(never: int = 7) -> int:
    return never


def seen(never: int | Absent) -> bool:
    return never is ABSENT
""",
    )
    built = _driven(driver_kind, module)
    assert built.nodes["use"].may_be_absent
    assert run(built, ["use", "fallback", "seen"]) == {
        "use": ABSENT,
        "fallback": 7,
        "seen": True,
    }


_MERGED_DEFAULTS = """
from fnloom import ABSENT, Absent, parameterize, source


def listing(wanted: bool) -> list[str] | Absent:
    return ["x"] if wanted else ABSENT


@parameterize(both={{"a": source("listing"), "b": source("listing")}})
def merged(a: list[str]{a_default}, b: list[str] = ["zz"]) -> str:
    return a[0] + b[0]
"""


def test_a_default_beside_a_required_read_of_one_source_is_shadowed() -> None:
    """Q76: `a` has no default, so `both` is skipped on absence; `b`'s is never used."""
    module = make_module(
        "absence_merged_required", _MERGED_DEFAULTS.format(a_default="")
    )
    with pytest.raises(ValueError, match="shadowed default") as raised:
        fnloom.build(module)
    assert str(raised.value) == (
        "shadowed default: parameter 'b' of 'both' has a default, but it reads the "
        "node 'listing', which replaces it; rename one, or let the node win with "
        "@fnloom.defaults(shadowed=('b',)) on 'both'"
    )


@pytest.mark.parametrize("driver_kind", DRIVER_KINDS)
def test_defaults_on_every_read_of_one_source_are_used(driver_kind: DriverKind) -> None:
    """Both parameters have defaults: the source is left out, and both apply."""
    module = make_module(
        "absence_merged_optional", _MERGED_DEFAULTS.format(a_default=' = ["aa"]')
    )
    built = _driven(driver_kind, module)
    assert run(built, ["both"], inputs={"wanted": False}) == {"both": "aazz"}
    assert run(built, ["both"], inputs={"wanted": True}) == {"both": "xx"}


def _param(name: str, annotation: object, *, default: bool) -> Param:
    return Param(name, Source("listing"), not default, annotation)


_ADMITS = list[str] | Absent


@pytest.mark.parametrize(
    ("params", "kind"),
    [
        (
            (_param("a", _ADMITS, default=False), _param("b", _ADMITS, default=True)),
            "admits",
        ),
        (
            (_param("a", _ADMITS, default=True), _param("b", _ADMITS, default=True)),
            "admits_optional",
        ),
        (
            (
                _param("a", list[str], default=True),
                _param("b", list[str], default=False),
            ),
            "required",
        ),
        (
            (
                _param("a", list[str], default=True),
                _param("b", list[str], default=True),
            ),
            "optional",
        ),
        (
            (_param("a", _ADMITS, default=True), _param("b", list[str], default=True)),
            "optional",
        ),
    ],
    ids=["admits", "admits_optional", "required", "optional", "mixed"],
)
def test_core_inputs_merges_kinds_per_source(
    params: tuple[Param, ...], kind: str
) -> None:
    """One source read by several parameters has one kind, by the merge rules."""
    other = Param("n", Value(1), True, int)  # noqa: FBT003  # `required`, in field order
    assert core_inputs((*params, other)) == [("listing", kind, None)]


@pytest.mark.parametrize("driver_kind", DRIVER_KINDS)
def test_an_admitting_and_a_plain_parameter_build_when_the_source_is_present(
    driver_kind: DriverKind,
) -> None:
    """The merge's last row builds only because `listing` can't be absent."""
    module = make_module(
        "absence_merge_input",
        """
from fnloom import Absent, parameterize, source


@parameterize(both={"a": source("listing"), "b": source("listing")})
def merged(a: list[str] | Absent = [], b: list[str] = []) -> int:
    return len(a) + len(b)
""",
    )
    built = _driven(driver_kind, module)
    assert run(built, ["both"], inputs={"listing": ["x"]}) == {"both": 2}
    assert run(built, ["both"]) == {"both": 0}


_MIXED = """
import fnloom
from fnloom import ABSENT, Absent, parameterize, source


def listing() -> list[str]{returns}:
    return ["x"]


@fnloom.defaults(shadowed=("b",))
@parameterize(both={{"a": source("listing"), "b": source("listing")}})
def merged(a: list[str] | Absent, b: list[str] = []) -> int:
    return len(b)
"""


@pytest.mark.parametrize("driver_kind", DRIVER_KINDS)
def test_mixed_absence_handling_of_one_source_is_refused(
    driver_kind: DriverKind,
) -> None:
    """Review Focus 4: `a` admits `Absent` and `b` has a default, on one source."""
    refused = make_module("absence_mixed", _MIXED.format(returns=" | Absent"))
    with pytest.raises(ValueError, match="mixed absence handling") as raised:
        _driven(driver_kind, refused)
    message = str(raised.value)
    for name in ("'both'", "'listing'", "'a'", "'b'"):
        assert name in message
    assert message == (
        "mixed absence handling: 'both' reads 'listing', which may be absent, "
        "through parameters that admit fnloom.Absent ('a') and parameters that "
        "do not ('b'); annotate them alike, or read it once"
    )
    built = _driven(
        driver_kind, make_module("absence_mixed_ok", _MIXED.format(returns=""))
    )
    assert run(built, ["both"]) == {"both": 1}


def test_mixed_absence_handling_through_a_group_is_refused() -> None:
    """A `group(...)` item is one parameter, shown with its slot."""
    module = make_module(
        "absence_mixed_group",
        """
from fnloom import ABSENT, Absent, group, parameterize, source


def listing() -> list[str] | Absent:
    return ABSENT


@parameterize(row={"xs": group(source("listing")), "y": source("listing")})
def both(xs: list[list[str]], y: list[str] | Absent) -> int:
    return len(xs)
""",
    )
    with pytest.raises(ValueError, match="mixed absence handling") as raised:
        fnloom.build(module)
    assert str(raised.value) == (
        "mixed absence handling: 'row' reads 'listing', which may be absent, "
        "through parameters that admit fnloom.Absent ('y') and parameters that "
        "do not ('xs[0]'); annotate them alike, or read it once"
    )


def test_mixed_absence_handling_lists_every_culprit_sorted() -> None:
    """Two sources of one node, read first `zz_list` then `aa_list`: listed sorted."""
    module = make_module(
        "absence_mixed_two",
        """
from fnloom import ABSENT, Absent, parameterize, source


def zz_list() -> list[str] | Absent:
    return ABSENT


def aa_list() -> list[str] | Absent:
    return ABSENT


@parameterize(
    row={
        "a": source("zz_list"),
        "b": source("zz_list"),
        "c": source("aa_list"),
        "d": source("aa_list"),
    }
)
def both(
    a: list[str] | Absent, b: list[str], c: list[str] | Absent, d: list[str]
) -> int:
    return 0
""",
    )
    with pytest.raises(ValueError, match="mixed absence handling") as raised:
        fnloom.build(module)
    line = (
        "mixed absence handling: 'row' reads {!r}, which may be absent, through "
        "parameters that admit fnloom.Absent ({!r}) and parameters that do not "
        "({!r}); annotate them alike, or read it once"
    )
    assert str(raised.value) == "; ".join(
        [line.format("aa_list", "c", "d"), line.format("zz_list", "a", "b")]
    )


_UNDECLARED_ASYNC = """
from fnloom import ABSENT


{prefix}def x() -> int:
    return ABSENT


def y(x: int) -> int:
    return x
"""


@pytest.mark.parametrize(
    ("prefix", "sync_nodes"),
    [("async ", "thread"), ("", "thread"), ("", "inline")],
    ids=["async-def", "sync-in-a-thread", "sync-inline"],
)
def test_build_async_refuses_returning_absent_without_admitting_it(
    prefix: str, sync_nodes: typing.Literal["thread", "inline"]
) -> None:
    """The sync driver's `TypeError`, for an `async def` node and a sync one."""
    module = make_module(
        f"absence_async_undeclared_{prefix.strip() or 'sync'}_{sync_nodes}",
        _UNDECLARED_ASYNC.format(prefix=prefix),
    )
    built = fnloom.Builder().with_modules(module).build_async(sync_nodes=sync_nodes)
    with pytest.raises(TypeError) as raised:
        asyncio.run(built.execute(["y"]))
    assert str(raised.value) == (
        "node 'x' returned fnloom.ABSENT, but its return annotation does "
        "not admit fnloom.Absent"
    )
    assert raised.value.__notes__ == ["in node 'x'"]


_DECLINING_ASYNC = """
from fnloom import ABSENT, Absent

calls = []


{prefix}def listing(wanted: bool) -> list[str] | Absent:
    return ["a"] if wanted else ABSENT


{prefix}def count(listing: list[str]) -> int:
    calls.append("count")
    return len(listing)


{prefix}def shown(listing: list[str] | Absent) -> bool:
    return listing is ABSENT


{prefix}def first(listing: list[str] = ["default"]) -> str:
    return listing[0]


{prefix}def report(count: int, first: str) -> str:
    calls.append("report")
    return f"{{count}} {{first}}"
"""


@pytest.mark.parametrize(
    ("prefix", "sync_nodes"),
    [("async ", "thread"), ("", "thread"), ("", "inline")],
    ids=["async-def", "sync-in-a-thread", "sync-inline"],
)
def test_a_declining_async_node_is_not_refused(
    prefix: str, sync_nodes: typing.Literal["thread", "inline"]
) -> None:
    """`build_async()` builds a node that declines, and its run follows the rules."""
    module = make_module(
        f"absence_async_declining_{prefix.strip() or 'sync'}_{sync_nodes}",
        _DECLINING_ASYNC.format(prefix=prefix),
    )
    driver = fnloom.Builder().with_modules(module).build_async(sync_nodes=sync_nodes)
    outputs = ["report", "shown", "first", "listing"]
    assert asyncio.run(driver.execute(outputs, inputs={"wanted": False})) == {
        "report": ABSENT,
        "shown": True,
        "first": "default",
        "listing": ABSENT,
    }
    assert module.calls == []
    assert asyncio.run(driver.execute(outputs, inputs={"wanted": True})) == {
        "report": "1 a",
        "shown": False,
        "first": "a",
        "listing": ["a"],
    }


class _Events(fnloom.Hook):
    """Every node event and `RunEnd`, in order."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.ends: list[fnloom.RunEnd] = []

    @override
    def before_node(self, event: fnloom.NodeStart) -> None:
        self.events.append(f"before {event.node.name}")

    @override
    def after_node(self, event: fnloom.NodeEnd) -> None:
        self.events.append(f"after {event.node.name}")

    @override
    def after_run(self, event: fnloom.RunEnd) -> None:
        self.ends.append(event)


@pytest.mark.parametrize(
    ("prefix", "sync_nodes", "cached"),
    [
        ("async ", "thread", False),
        ("", "thread", False),
        ("", "inline", False),
        ("async ", "thread", True),
    ],
    ids=["async-def", "sync-in-a-thread", "sync-inline", "cached"],
)
def test_async_skips_fire_no_node_events_and_list_in_run_end(
    prefix: str, sync_nodes: typing.Literal["thread", "inline"], *, cached: bool
) -> None:
    """A skipped node is stored absent, counts for its dependents, and reports once."""
    events = _Events()
    module = make_module(
        f"absence_async_events_{prefix.strip() or 'sync'}_{sync_nodes}_{cached}",
        _DECLINING_ASYNC.format(prefix=prefix),
    )
    builder = fnloom.Builder().with_modules(module).with_hooks(events)
    if cached:
        builder = builder.with_cache(fnloom.MemoryCache())
    driver = builder.build_async(sync_nodes=sync_nodes)
    result = asyncio.run(driver.execute(["report", "shown"], inputs={"wanted": False}))
    assert result == {"report": ABSENT, "shown": True}
    assert sorted(events.events) == [
        "after first",
        "after listing",
        "after shown",
        "before first",
        "before listing",
        "before shown",
    ]
    assert events.events.index("after listing") < events.events.index("before shown")
    assert events.ends[0].skipped == (("count", ()), ("report", ()))
    assert events.ends[0].ok


@pytest.mark.parametrize(
    ("prefix", "sync_nodes"),
    [("async ", "thread"), ("", "thread"), ("", "inline")],
    ids=["async-def", "sync-in-a-thread", "sync-inline"],
)
def test_async_return_check_on_async_and_inline_sync_nodes(
    prefix: str, sync_nodes: typing.Literal["thread", "inline"]
) -> None:
    """Only a node that doesn't decline is refused `ABSENT`, named and noted."""
    module = make_module(
        f"absence_async_return_{prefix.strip() or 'sync'}_{sync_nodes}",
        f"""
from fnloom import ABSENT, Absent


{prefix}def declining() -> int | Absent:
    return ABSENT


{prefix}def plain() -> int:
    return ABSENT
""",
    )
    driver = fnloom.Builder().with_modules(module).build_async(sync_nodes=sync_nodes)
    assert asyncio.run(driver.execute(["declining"])) == {"declining": ABSENT}
    with pytest.raises(TypeError) as raised:
        asyncio.run(driver.execute(["declining", "plain"]))
    assert str(raised.value) == (
        "node 'plain' returned fnloom.ABSENT, but its return annotation does "
        "not admit fnloom.Absent"
    )
    assert raised.value.__notes__ == ["in node 'plain'"]


@pytest.mark.parametrize("driver_kind", DRIVER_KINDS)
def test_async_admits_input_left_out_is_missing_unless_defaulted(
    driver_kind: DriverKind,
) -> None:
    """An input is never absent: `x: int | Absent` needs it, `= 3` doesn't."""
    module = make_module(
        f"absence_admits_input_{driver_kind}",
        """
from fnloom import ABSENT, Absent


def needs(x: int | Absent) -> int:
    return x


def falls_back(y: int | Absent = 3) -> int:
    return y
""",
    )
    driver = _driven(driver_kind, module)
    assert run(driver, ["falls_back"]) == {"falls_back": 3}
    assert run(driver, ["needs", "falls_back"], inputs={"x": 1, "y": 2}) == {
        "needs": 1,
        "falls_back": 2,
    }
    with pytest.raises(ValueError, match=r"missing inputs: 'x' \(needed by 'needs'\)"):
        run(driver, ["needs"])


def test_public_names() -> None:
    """`ABSENT` and `Absent` are public, and `Absent`'s signature resolves."""
    assert {"ABSENT", "Absent"} <= set(fnloom.__all__)
    assert fnloom.ABSENT is ABSENT
    assert fnloom.Absent is Absent
    assert Absent.__module__ == "fnloom"
    inspect.signature(Absent)


# ---- absence at the edges of a run (Task 3) -----------------------------------------

_EDGES = """
from typing import Any

from fnloom import ABSENT, Absent

calls = []


def listing(wanted: bool) -> list[str] | Absent:
    return ["a", "b"] if wanted else ABSENT


def count(listing: list[str]) -> int:
    calls.append("count")
    return len(listing)


def report(count: int) -> str:
    calls.append("report")
    return str(count)


def loose(anything: Any) -> Any:
    return anything


def labelled(label: str) -> str:
    return label
"""


def _edges(name: str) -> types.ModuleType:
    return make_module(name, _EDGES)


@pytest.mark.parametrize("driver_kind", DRIVER_KINDS)
def test_an_absent_output_is_absent_in_the_result_and_given_to_the_builder(
    driver_kind: DriverKind,
) -> None:
    """The raw dict holds `ABSENT`, and a result builder gets it as a value (Q19)."""
    seen: list[dict[str, object]] = []

    def build(outputs: dict[str, object]) -> list[str]:
        seen.append(dict(outputs))
        return list(outputs)

    module = _edges(f"absence_result_{driver_kind}")
    builder = fnloom.Builder().with_modules(module)
    raw = run(
        built(driver_kind, builder), ["count", "listing"], inputs={"wanted": False}
    )
    assert raw == {"count": ABSENT, "listing": ABSENT}
    assert raw["count"] is ABSENT
    with_builder = built(driver_kind, builder.with_result_builder(build))
    outputs = ["report", "listing"]
    assert run(with_builder, outputs, inputs={"wanted": False}) == outputs
    assert seen == [{"report": ABSENT, "listing": ABSENT}]
    assert seen[0]["report"] is ABSENT


@pytest.mark.parametrize("driver_kind", DRIVER_KINDS)
@pytest.mark.parametrize("checked", [True, False], ids=["checked", "unchecked"])
def test_an_absent_input_is_refused(driver_kind: DriverKind, *, checked: bool) -> None:
    """Typed or `Any`, with input checks or without: leave it out instead."""
    builder = fnloom.Builder().with_modules(_edges(f"absence_input_{driver_kind}"))
    if not checked:
        builder = builder.without_input_type_checks()
    driver = built(driver_kind, builder)
    with pytest.raises(TypeError) as raised:
        run(driver, ["loose", "labelled"], inputs={"anything": ABSENT, "label": ABSENT})
    assert str(raised.value) == (
        "input 'anything' is fnloom.ABSENT, which an input can't be; leave it out "
        "instead; input 'label' is fnloom.ABSENT, which an input can't be; leave it "
        "out instead"
    )


@pytest.mark.parametrize("driver_kind", DRIVER_KINDS)
@pytest.mark.parametrize("checked", [True, False], ids=["checked", "unchecked"])
def test_an_absent_override_is_allowed_only_for_a_node_that_may_be_absent(
    driver_kind: DriverKind, *, checked: bool
) -> None:
    """It forces the skip of a node that may be absent; for any other, a TypeError."""
    module = _edges(f"absence_override_{driver_kind}_{checked}")
    builder = fnloom.Builder().with_modules(module)
    if not checked:
        builder = builder.without_override_type_checks()
    driver = built(driver_kind, builder)
    forced = run(
        driver, ["report"], inputs={"wanted": True}, overrides={"listing": ABSENT}
    )
    assert forced == {"report": ABSENT}
    # `count` may be absent, though typed `int`: its override may be `ABSENT` too.
    assert run(driver, ["report"], overrides={"count": ABSENT}) == {"report": ABSENT}
    assert module.calls == []
    with pytest.raises(TypeError) as raised:
        run(
            driver,
            ["loose", "labelled"],
            inputs={"anything": 1, "label": "x"},
            overrides={"loose": ABSENT, "labelled": ABSENT},
        )
    assert str(raised.value) == (
        "override 'labelled' is fnloom.ABSENT, but node 'labelled' can't be absent: "
        "its return annotation does not admit fnloom.Absent, and it reads no node "
        "that may be; override it with a value; override 'loose' is fnloom.ABSENT, "
        "but node 'loose' can't be absent: its return annotation does not admit "
        "fnloom.Absent, and it reads no node that may be; override it with a value"
    )


class _Ends(fnloom.Hook):
    """Keeps every `RunEnd`, and the nodes `before_node` saw."""

    def __init__(self) -> None:
        self.ends: list[fnloom.RunEnd] = []
        self.started: list[str] = []

    @override
    def before_node(self, event: fnloom.NodeStart) -> None:
        self.started.append(event.node.name)

    @override
    def after_run(self, event: fnloom.RunEnd) -> None:
        self.ends.append(event)


class _EndsOnly(fnloom.Hook):
    """Keeps every `RunEnd`, and wants no node event."""

    def __init__(self) -> None:
        self.ends: list[fnloom.RunEnd] = []

    @override
    def after_run(self, event: fnloom.RunEnd) -> None:
        self.ends.append(event)


@pytest.mark.parametrize("driver_kind", DRIVER_KINDS)
def test_run_end_lists_skipped_nodes_in_order(driver_kind: DriverKind) -> None:
    """`(node, branch)` pairs, `()` outside blocks; a skipped node fires no event."""
    hooked, only = _Ends(), _EndsOnly()
    module = _edges(f"absence_run_end_{driver_kind}")
    builder = fnloom.Builder().with_modules(module).with_hooks(hooked, only)
    driver = built(driver_kind, builder)
    assert run(driver, ["report"], inputs={"wanted": False}) == {"report": ABSENT}
    expected = (("count", ()), ("report", ()))
    assert hooked.ends[0].skipped == expected
    assert only.ends[0].skipped == expected
    assert hooked.started == ["listing"]
    run(driver, ["report"], inputs={"wanted": True})
    assert hooked.ends[1].skipped == ()
    assert only.ends[1].skipped == ()
    only_builder = fnloom.Builder().with_modules(module).with_hooks(_EndsOnly())
    lone = only_builder.build()
    lone.execute(["report"], inputs={"wanted": False})
    ends = typing.cast("_EndsOnly", lone.hooks[0]).ends
    assert ends[0].skipped == expected


_BRANCHES = """
from fnloom import ABSENT, Absent


def a() -> int | Absent:
    return ABSENT


def b() -> int | Absent:
    return ABSENT


def y1(a: int) -> int:
    return a


def y2(y1: int) -> int:
    return y1


def z1(b: int) -> int:
    return b


def z2(z1: int) -> int:
    return z1
"""


def test_run_end_lists_skips_in_plan_order_on_the_sync_driver() -> None:
    """Two declining branches: the sync driver skips in its plan's order."""
    ends = _EndsOnly()
    module = make_module("absence_branches", _BRANCHES)
    driver = fnloom.Builder().with_modules(module).with_hooks(ends).build()
    driver.execute(["y2", "z2"])
    native = driver.prepare(["y2", "z2"])._plan  # noqa: SLF001  # the native plan's order
    assert isinstance(native, _core.Plan)
    plan = [name for name, _ in native.steps()]
    assert [name for name, _ in ends.ends[0].skipped] == [
        name for name in plan if name in {"y1", "y2", "z1", "z2"}
    ]


class _Started(fnloom.Hook):
    def __init__(self) -> None:
        self.seen: list[object] = []

    @override
    def before_run(self, event: fnloom.RunStart) -> None:
        self.seen.append(("start", event.nodes))

    @override
    def after_run(self, event: fnloom.RunEnd) -> None:
        self.seen.append(("end", event.ok, type(event.error).__name__))


@pytest.mark.parametrize("driver_kind", DRIVER_KINDS)
def test_an_absent_input_is_refused_after_before_run(driver_kind: DriverKind) -> None:
    """As a value check's failure: `before_run` lists no nodes, `after_run` fails."""
    hook = _Started()
    module = _edges(f"absence_input_hooked_{driver_kind}")
    builder = fnloom.Builder().with_modules(module).with_hooks(hook)
    with pytest.raises(TypeError, match="leave it out instead"):
        run(built(driver_kind, builder), ["loose"], inputs={"anything": ABSENT})
    assert hook.seen == [("start", ()), ("end", False, "TypeError")]


_FIELDS = """
from typing import TypedDict

from fnloom import ABSENT, Absent, extract_columns, extract_fields


class Shares(TypedDict):
    east: float
    west: float


@extract_fields({"net": float, "tax": float})
def totals(wanted: bool) -> dict[str, float] | Absent:
    return {"net": 1.0, "tax": 2.0} if wanted else ABSENT


@extract_fields()
def shares(wanted: bool) -> Shares | Absent:
    return {"east": 3.0, "west": 4.0} if wanted else ABSENT


@extract_columns("north", "south")
def regions(wanted: bool) -> dict[str, list[int]] | Absent:
    return {"north": [1], "south": [2]} if wanted else ABSENT
"""


@pytest.mark.parametrize("driver_kind", DRIVER_KINDS)
def test_fields_of_an_absent_producer_are_absent(driver_kind: DriverKind) -> None:
    """`@extract_fields` and `@extract_columns` over `R | Absent`: every field."""
    module = make_module(f"absence_fields_{driver_kind}", _FIELDS)
    driver = built(driver_kind, fnloom.Builder().with_modules(module))
    names = ["net", "tax", "east", "west", "north", "south"]
    assert all(driver.nodes[name].may_be_absent for name in names)
    assert [driver.nodes[name].type for name in names[:4]] == [float] * 4
    assert run(driver, names, inputs={"wanted": False}) == dict.fromkeys(names, ABSENT)
    assert run(driver, names, inputs={"wanted": True}) == {
        "net": 1.0,
        "tax": 2.0,
        "east": 3.0,
        "west": 4.0,
        "north": [1],
        "south": [2],
    }


_CHECKED = """
from fnloom import ABSENT, Absent, check_output


@check_output(range=(0, 10))
def score(wanted: bool) -> float | Absent:
    return 3.0 if wanted else ABSENT


def total(wanted: bool) -> int | Absent:
    return 1 if wanted else ABSENT


def plain() -> int:
    return ABSENT
"""


class _Checks(fnloom.Hook):
    def __init__(self) -> None:
        self.checks: dict[str, tuple[fnloom.CheckResult, ...]] = {}

    @override
    def after_node(self, event: fnloom.NodeEnd) -> None:
        self.checks[event.node.name] = event.checks


@pytest.mark.parametrize("driver_kind", DRIVER_KINDS)
def test_output_checks_do_not_run_on_absent(driver_kind: DriverKind) -> None:
    """`@check_output` and `with_output_type_checks()`: no value, nothing to check."""
    hook = _Checks()
    module = make_module(f"absence_checks_{driver_kind}", _CHECKED)
    builder = (
        fnloom.Builder().with_modules(module).with_output_type_checks().with_hooks(hook)
    )
    driver = built(driver_kind, builder)
    absent = run(driver, ["score", "total"], inputs={"wanted": False})
    assert absent == {"score": ABSENT, "total": ABSENT}
    assert hook.checks == {"score": (), "total": ()}
    present = run(driver, ["score", "total"], inputs={"wanted": True})
    assert present == {"score": 3.0, "total": 1}
    assert [result.passes for result in hook.checks["score"]] == [True]
    # A node that doesn't decline gets the return check's error, not a check's.
    with pytest.raises(TypeError, match=r"returned fnloom\.ABSENT, but its return"):
        run(driver, ["plain"])


_STEPS = """
from fnloom import ABSENT, Absent, pipe_output, step


def _sorted(v: list[str]) -> list[str]:
    return sorted(v)


def _filled(v: list[str] | Absent) -> list[str]:
    return ["none"] if v is ABSENT else v


@pipe_output(step(_sorted))
def plain(wanted: bool) -> list[str] | Absent:
    return ["b", "a"] if wanted else ABSENT


@pipe_output(step(_filled))
def filled(wanted: bool) -> list[str] | Absent:
    return ["b", "a"] if wanted else ABSENT
"""


@pytest.mark.parametrize("driver_kind", DRIVER_KINDS)
def test_a_pipe_output_step_reads_absence_by_its_signature(
    driver_kind: DriverKind,
) -> None:
    """A step is an ordinary node reading its target: skipped, or given `ABSENT`."""
    module = make_module(f"absence_steps_{driver_kind}", _STEPS)
    driver = built(driver_kind, fnloom.Builder().with_modules(module))
    assert driver.nodes["plain"].may_be_absent
    assert not driver.nodes["filled"].may_be_absent
    assert run(driver, ["plain", "filled"], inputs={"wanted": False}) == {
        "plain": ABSENT,
        "filled": ["none"],
    }
    assert run(driver, ["plain", "filled"], inputs={"wanted": True}) == {
        "plain": ["a", "b"],
        "filled": ["b", "a"],
    }


@pytest.mark.parametrize("driver_kind", DRIVER_KINDS)
def test_a_wrapper_sees_absent_from_call_and_may_replace_it(
    driver_kind: DriverKind,
) -> None:
    """`call()` returns `ABSENT`; a wrapper may give a value, or `ABSENT` back."""
    seen: list[tuple[str, object]] = []

    def filling(
        node: fnloom.NodeView,
        call: typing.Callable[[], object],
        inputs: typing.Mapping[str, object],
    ) -> object:
        del inputs
        value = call()
        seen.append((node.name, value))
        if node.name == "listing" and value is ABSENT:
            return ["z"]
        return value

    module = _edges("absence_wrapper_fills")
    builder = fnloom.Builder().with_modules(module).with_node_wrappers(filling)
    driver = built(driver_kind, builder)
    assert run(driver, ["count"], inputs={"wanted": False}) == {"count": 1}
    assert seen == [("listing", ABSENT), ("count", 1)]

    def absenting(
        node: fnloom.NodeView,
        call: typing.Callable[[], object],
        inputs: typing.Mapping[str, object],
    ) -> object:
        del inputs
        value = call()
        return ABSENT if node.name == "count" else value

    refused = built(
        driver_kind,
        fnloom.Builder()
        .with_modules(_edges("absence_wrapper_refused"))
        .with_node_wrappers(absenting),
    )
    with pytest.raises(TypeError) as raised:
        run(refused, ["count"], inputs={"wanted": True})
    assert str(raised.value) == (
        "node 'count' returned fnloom.ABSENT, but its return annotation does "
        "not admit fnloom.Absent"
    )


@_needs_pandas
@pytest.mark.parametrize("driver_kind", DRIVER_KINDS)
def test_frame_refuses_an_absent_output(driver_kind: DriverKind) -> None:
    """A column can't be absent: a `ValueError` naming each absent output, sorted."""
    module = _edges(f"absence_frame_{driver_kind}")
    builder = fnloom.Builder().with_modules(module).with_result_builder(fnloom.frame())
    driver = built(driver_kind, builder)
    with pytest.raises(ValueError, match=r"fnloom\.ABSENT") as raised:
        run(
            driver,
            ["report", "count", "labelled"],
            inputs={"wanted": False, "label": "x"},
        )
    assert str(raised.value) == (
        "frame() can't make a column of an absent output: 'count', 'report' "
        "are fnloom.ABSENT"
    )
    made = run(driver, ["count"], inputs={"wanted": True, "label": "x"})
    assert list(made["count"]) == [2]


@pytest.mark.parametrize("driver_kind", DRIVER_KINDS)
def test_print_nodes_prints_nothing_for_a_skipped_node(driver_kind: DriverKind) -> None:
    """Only `listing` runs, so only it is printed, its result `fnloom.ABSENT`."""
    lines: list[str] = []
    printing = fnloom.PrintNodes(2, print_fn=lines.append)
    module = _edges("absence_print_nodes")
    builder = fnloom.Builder().with_modules(module).with_hooks(printing)
    run(built(driver_kind, builder), ["report"], inputs={"wanted": False})
    assert [line.split(" ran in ")[0] for line in lines] == [
        "node 'listing'",
        "  inputs: wanted=False",
        "  result: fnloom.ABSENT",
    ]


def test_export_2_only_when_a_node_may_be_absent() -> None:
    """`/1` stays byte for byte what it was; `/2` says `may_be_absent` per node."""
    golden = Path(__file__).parent / "golden" / "export_1.json"
    assert export_golden.text() == golden.read_text(encoding="utf-8")
    driver = fnloom.build(_edges("absence_export"))
    document = driver.export()
    assert document["format"] == "fnloom.export/2"
    flags = {node["name"]: node["may_be_absent"] for node in document["nodes"]}
    assert flags == {
        "anything": False,
        "label": False,
        "wanted": False,
        "listing": True,
        "count": True,
        "report": True,
        "loose": False,
        "labelled": False,
    }
    assert all(node["marks"] == [] for node in document["nodes"])
    run_document = driver.export(["loose"], inputs={"anything": 1})
    assert run_document["format"] == "fnloom.export/1"
    assert "may_be_absent" not in run_document["nodes"][0]


def test_diff_reads_export_1_and_2() -> None:
    """A node that may now be absent is changed in `may_be_absent` and its type."""
    before = make_module(
        "absence_diff_1",
        """
def listing(wanted: bool) -> list[str]:
    return []
""",
    )
    after = make_module(
        "absence_diff_2",
        """
from fnloom import ABSENT, Absent


def listing(wanted: bool) -> list[str] | Absent:
    return []
""",
    )
    old, new = fnloom.build(before).export(), fnloom.build(after).export()
    assert (old["format"], new["format"]) == ("fnloom.export/1", "fnloom.export/2")
    changed = _export.diff(old, new)["changed"]
    assert changed == {"listing": ["type", "may_be_absent"]}
    assert _export.diff(new, new)["changed"] == {}
    assert _export.diff(old, old)["changed"] == {}


_DRAWN = """
from fnloom import ABSENT, Absent


def listing(wanted: bool) -> list[str] | Absent:
    return ABSENT


def count(listing: list[str]) -> int:
    return len(listing)


def shown(listing: list[str] | Absent) -> str:
    return ""
"""

_DRAW_SCRIPT = """
import sys

sys.path.insert(0, sys.argv[1])

import fnloom
import absence_drawn

drawing = fnloom.build(absence_drawn).draw()
sys.stdout.write(drawing.to_dot())
sys.stdout.write(drawing.to_mermaid())
sys.stdout.write(drawing.to_svg())
"""


def test_the_may_be_absent_mark_is_drawn_and_hash_seed_stable(
    tmp_path: Path,
) -> None:
    """Dashed, in the legend, in DOT, Mermaid and SVG, under three hash seeds."""
    (tmp_path / "absence_drawn.py").write_text(_DRAWN)
    script = tmp_path / "draw.py"
    script.write_text(_DRAW_SCRIPT)
    texts = {
        subprocess.run(  # noqa: S603  # this interpreter, on the test's own script
            [sys.executable, str(script), str(tmp_path)],
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, "PYTHONHASHSEED": seed},
        ).stdout
        for seed in ("0", "1", "4242")
    }
    assert len(texts) == 1
    sys.path.insert(0, str(tmp_path))
    try:
        drawing = fnloom.build(importlib.import_module("absence_drawn")).draw()
    finally:
        sys.path.remove(str(tmp_path))
    marks = {node.name: node.marks for node in drawing.nodes}
    assert marks["listing"] == {"may be absent"}
    assert marks["count"] == {"may be absent"}
    assert marks["shown"] == frozenset()
    dot = drawing.to_dot()
    assert '"listing" [label=' in dot
    listing_line = next(line for line in dot.splitlines() if '"listing" [' in line)
    assert "dashed" in listing_line
    assert '"legend: may be absent" [label="may be absent"' in dot
    shown_line = next(line for line in dot.splitlines() if '"shown" [' in line)
    assert "dashed" not in shown_line
    mermaid = drawing.to_mermaid()
    assert "classDef may_be_absent " in mermaid
    assert "stroke-dasharray" in drawing.to_svg()


def test_an_overridden_node_that_may_be_absent_stays_dashed() -> None:
    """An override's own style keeps the dashes of `may be absent` in DOT."""
    driver = fnloom.build(_edges("absence_drawn_override"))
    drawn = driver.draw_execution(
        ["count"], inputs={"wanted": True}, overrides={"listing": ABSENT}
    )
    dot = drawn.to_dot()
    line = next(line for line in dot.splitlines() if line.startswith('  "listing" ['))
    assert 'style="dashed,filled,diagonals"' in line


_TRUTHLESS = """
from fnloom import ABSENT, Absent


def listing(wanted: bool) -> list[str] | Absent:
    return ABSENT


def boom(listing: list[str] | Absent) -> int:
    raise ValueError("no")


def kept(listing: list[str] | Absent) -> list[str] | Absent:
    return listing


def count(listing: list[str]) -> int:
    return len(listing)
"""


@_needs_pandas
def test_fnloom_never_takes_absents_truth_value(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """`bool(ABSENT)` raises, so each path below would fail if it tested truth."""
    lines: list[str] = []
    module = make_module("absence_truthless", _TRUTHLESS)
    hooks = (fnloom.PrintNodes(2, print_fn=lines.append), fnloom.LogFailures())
    driver = fnloom.Builder().with_modules(module).with_hooks(*hooks).build()
    assert driver.execute(["kept", "count"], inputs={"wanted": True}) == {
        "kept": ABSENT,
        "count": ABSENT,
    }
    with (
        caplog.at_level("ERROR", logger="fnloom"),
        pytest.raises(ValueError, match="no"),
    ):
        driver.execute(["boom"], inputs={"wanted": True})
    assert "listing=fnloom.ABSENT" in caplog.text
    assert "  inputs: listing=fnloom.ABSENT" in lines
    built_result = (
        fnloom.Builder().with_modules(module).with_result_builder(dict).build()
    )
    assert built_result.execute(["kept"], inputs={"wanted": True}) == {"kept": ABSENT}
    framed = fnloom.Builder().with_modules(module).with_result_builder(fnloom.frame())
    with pytest.raises(ValueError, match=r"'kept' is fnloom\.ABSENT"):
        framed.build().execute(["kept"], inputs={"wanted": True})
    for store in (fnloom.MemoryCache(), fnloom.DiskCache(tmp_path)):
        cached = fnloom.Builder().with_modules(module).with_cache(store).build()
        for _ in range(2):  # stored, then read back
            outputs = cached.execute(["kept", "count"], inputs={"wanted": True})
            assert outputs == {"kept": ABSENT, "count": ABSENT}
            assert outputs["kept"] is ABSENT
    overrides = {"listing": ABSENT}
    document = driver.export(
        ["kept", "count"], inputs={"wanted": True}, overrides=overrides
    )
    assert document["format"] == "fnloom.export/2"
    drawing = driver.draw_execution(
        ["kept"], inputs={"wanted": True}, overrides=overrides
    )
    assert drawing.to_dot()
