"""Typed absence: a node may decline, and each consumer's signature decides (Q135)."""

import copy
import inspect
import pickle
import typing
from typing import Annotated

import pytest

import fnloom
from fnloom import ABSENT, Absent, _absence, _core
from fnloom._decorators import Source, Value
from fnloom._scan import core_inputs
from fnloom._view import Param
from helpers import make_module

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


def _consumers(name: str) -> tuple[fnloom.Driver, list[str]]:
    module = make_module(name, _CONSUMERS)
    calls = typing.cast("list[str]", module.calls)
    return fnloom.build(module), calls


def test_a_plain_consumer_of_a_declined_node_is_skipped() -> None:
    """`count` and `doubled` after it never run, and both come back `ABSENT`."""
    built, calls = _consumers("absence_skipped")
    result = built.execute(["doubled", "count"], inputs={"wanted": False})
    assert result == {"doubled": ABSENT, "count": ABSENT}
    assert result["count"] is ABSENT
    assert calls == []


def test_a_present_value_reaches_every_consumer() -> None:
    """A declining node that does produce a value is read like any other."""
    built, calls = _consumers("absence_present")
    result = built.execute(["doubled", "shown", "first"], inputs={"wanted": True})
    assert result == {"doubled": 4, "shown": "a,b", "first": "a"}
    assert sorted(calls) == ["count", "doubled", "first", "shown"]


def test_a_consumer_admitting_absent_receives_it() -> None:
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
    assert fnloom.build(module).execute(["shown"]) == {"shown": "none"}
    assert len(module.seen) == 1
    assert module.seen[0] is ABSENT


def test_a_defaulted_consumer_gets_its_default() -> None:
    """The argument is left out, so the function's own default applies."""
    built, calls = _consumers("absence_default")
    assert built.execute(["first"], inputs={"wanted": False}) == {"first": "default"}
    assert calls == ["first"]


def test_returning_absent_without_admitting_it_is_a_type_error() -> None:
    """The return check runs on every computed node, always."""
    module = make_module(
        "absence_unadmitted",
        """
from fnloom import ABSENT


def listing() -> list[str]:
    return ABSENT
""",
    )
    built = fnloom.build(module)
    with pytest.raises(TypeError) as raised:
        built.execute(["listing"])
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


def test_the_core_and_the_views_agree_on_what_may_be_absent() -> None:
    """The native graph computes the same set as the views, sorted."""
    built, _ = _consumers("absence_agree")
    graph = built._graph  # noqa: SLF001  # the native graph on purpose
    assert isinstance(graph, _core.Graph)
    assert graph.may_be_absent() == ["count", "doubled", "listing"]
    assert graph.may_be_absent() == sorted(
        name for name, view in built.nodes.items() if view.may_be_absent
    )


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
        type("Sub", (Absent,), {})  # ty: ignore[subclass-of-final-class]  # refused on purpose


def test_t_or_absent_feeds_t_on_an_edge() -> None:
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
    built = fnloom.build(module)
    assert built.execute(["plain", "admitting", "defaulted"]) == {
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
        fnloom.build(refused)


def test_a_default_used_on_absence_is_no_shadowed_default() -> None:
    """Q76 refuses a default a node always replaces; one taken on absence is used."""
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


def test_an_admitting_and_a_plain_parameter_build_when_the_source_is_present() -> None:
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
    built = fnloom.build(module)
    assert built.execute(["both"], inputs={"listing": ["x"]}) == {"both": 2}
    assert built.execute(["both"]) == {"both": 0}


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


def test_mixed_absence_handling_of_one_source_is_refused() -> None:
    """Review Focus 4: `a` admits `Absent` and `b` has a default, on one source."""
    refused = make_module("absence_mixed", _MIXED.format(returns=" | Absent"))
    with pytest.raises(ValueError, match="mixed absence handling") as raised:
        fnloom.build(refused)
    message = str(raised.value)
    for name in ("'both'", "'listing'", "'a'", "'b'"):
        assert name in message
    assert message == (
        "mixed absence handling: 'both' reads 'listing', which may be absent, "
        "through parameters that admit fnloom.Absent ('a') and parameters that "
        "do not ('b'); annotate them alike, or read it once"
    )
    built = fnloom.build(make_module("absence_mixed_ok", _MIXED.format(returns="")))
    assert built.execute(["both"]) == {"both": 1}


def test_build_async_refuses_a_declining_node_for_now() -> None:
    """The async walk doesn't follow the rules yet, so it refuses, naming each node."""
    module = make_module("absence_async", _CONSUMERS)
    with pytest.raises(TypeError) as raised:
        fnloom.Builder().with_modules(module).build_async()
    assert str(raised.value) == (
        "build_async() does not run a node that may decline yet: 'listing'; "
        "build() does"
    )


def test_public_names() -> None:
    """`ABSENT` and `Absent` are public, and `Absent`'s signature resolves."""
    assert {"ABSENT", "Absent"} <= set(fnloom.__all__)
    assert fnloom.ABSENT is ABSENT
    assert fnloom.Absent is Absent
    assert Absent.__module__ == "fnloom"
    inspect.signature(Absent)
