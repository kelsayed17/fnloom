"""`Builder.without_edge_type_checks(*consumers)`: loose edges (Q149)."""

import copy
import pickle

import pytest

from fnloom import Builder
from helpers import make_module

LOOSE = """
def code() -> str:
    return "7"

def count(code: int) -> int:
    return code

def label(code: float) -> str:
    return str(code)

def checked(n: int) -> int:
    return n
"""


def _loose(name: str = "edge_checks_loose") -> object:
    return make_module(name, LOOSE)


def test_edges_are_checked_by_default() -> None:
    """Every mismatched edge is listed, as before."""
    with pytest.raises(ValueError, match=r"count\.code requires int") as raised:
        Builder().with_modules(_loose()).build()  # ty: ignore[invalid-argument-type]  # a module from make_module
    assert "label.code requires float" in str(raised.value)


def test_no_argument_turns_every_edge_check_off() -> None:
    """With no names, no edge is checked; the run passes the value through."""
    driver = Builder().with_modules(_loose()).without_edge_type_checks().build()  # ty: ignore[invalid-argument-type]  # a module from make_module
    assert driver.execute(["count", "label"]) == {"count": "7", "label": "7"}


def test_names_turn_off_only_the_edges_into_them() -> None:
    """The other consumers' edges are still checked."""
    module = _loose()
    with pytest.raises(ValueError, match=r"label\.code requires float") as raised:
        Builder().with_modules(module).without_edge_type_checks("count").build()  # ty: ignore[invalid-argument-type]  # a module from make_module
    assert "count.code" not in str(raised.value)
    driver = (
        Builder()
        .with_modules(module)  # ty: ignore[invalid-argument-type]  # a module from make_module
        .without_edge_type_checks("count")
        .without_edge_type_checks("label")
        .build()
    )
    assert driver.execute(["count"]) == {"count": "7"}


def test_input_checks_stay_on() -> None:
    """Loose edges are not loose inputs (Q10)."""
    driver = Builder().with_modules(_loose()).without_edge_type_checks().build()  # ty: ignore[invalid-argument-type]  # a module from make_module
    with pytest.raises(ValueError, match="n: expected int, got str"):
        driver.execute(["checked"], inputs={"n": "x"})


def test_an_unknown_name_is_refused_at_build() -> None:
    """A name that is no computed node is a `ValueError`, with a suggestion."""
    module = _loose()
    with pytest.raises(
        ValueError,
        match=r"without_edge_type_checks names no computed node of this graph: "
        r"'cuont' \(did you mean 'count'\?\), 'n'",
    ):
        Builder().with_modules(module).without_edge_type_checks("n", "cuont").build()  # ty: ignore[invalid-argument-type]  # a module from make_module
    with pytest.raises(TypeError, match="takes node names as str, got: int"):
        Builder().without_edge_type_checks(3)  # ty: ignore[invalid-argument-type]  # refused is the point


def test_the_export_and_graph_version_record_the_setting() -> None:
    """`edge_checks` is `on`, `off` or the skipped consumers; `graph_version` moves."""
    module = make_module(
        "edge_checks_export",
        "def a() -> int:\n    return 1\n\ndef b(a: int) -> int:\n    return a\n",
    )
    on = Builder().with_modules(module).build().export()
    off = Builder().with_modules(module).without_edge_type_checks().build().export()
    some = Builder().with_modules(module).without_edge_type_checks("b").build().export()
    assert (on["edge_checks"], off["edge_checks"], some["edge_checks"]) == (
        "on",
        "off",
        ["b"],
    )
    versions = {on["graph_version"], off["graph_version"], some["graph_version"]}
    assert None not in versions
    assert len(versions) == 3  # noqa: PLR2004  # one per setting
    assert on["nodes"] == off["nodes"] == some["nodes"]


def test_pickling_copy_and_rebuild_carry_the_setting() -> None:
    """The setting survives pickling, `copy`, `rebuild()` and `build_async`."""
    import picklable_nodes  # noqa: PLC0415  # importable by name, for pickling

    builder = Builder().with_modules(picklable_nodes).without_edge_type_checks("total")
    driver = builder.build()
    for again in (
        pickle.loads(pickle.dumps(driver)),  # noqa: S301  # our own bytes
        copy.copy(driver),
        driver.rebuild(),
        pickle.loads(pickle.dumps(builder)).build(),  # noqa: S301  # our own bytes
        builder.with_result_builder(dict).build(),
        builder.build_async(),
    ):
        assert again.export()["edge_checks"] == ["total"]


STEPPED = """
from fnloom import mutate, pipe_input, step

def a() -> str:
    return "7"

def _keep(x: int) -> int:
    return x

@pipe_input(step(_keep))
def fed(a: int) -> int:
    return a

def total(a: int) -> int:
    return a

@mutate(total)
def _plus_zero(x: int) -> int:
    return x
"""


def test_a_name_covers_the_nodes_its_steps_lower_to() -> None:
    """Loose `total` and `fed` cover their `@mutate` and `@pipe_input` steps."""
    module = make_module("edge_checks_stepped", STEPPED)
    with pytest.raises(ValueError, match=r"total\.raw\.a requires int") as raised:
        Builder().with_modules(module).without_edge_type_checks("fed").build()
    assert "fed" not in str(raised.value)
    with pytest.raises(ValueError, match=r"fed\.with_keep\.x requires int"):
        Builder().with_modules(module).without_edge_type_checks("total").build()
    driver = (
        Builder().with_modules(module).without_edge_type_checks("total", "fed").build()
    )
    assert driver.execute(["total", "fed"]) == {"total": "7", "fed": "7"}
