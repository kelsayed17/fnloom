"""Drawings: which nodes each draws, how they are marked, and the DOT text."""

import os
import pickle
import subprocess
import sys
import textwrap
from typing import TYPE_CHECKING

import pytest

import fnloom
from fnloom import Drawing, DrawnNode
from helpers import make_module

if TYPE_CHECKING:
    import types

PROBE = """
from typing import Annotated, NewType
from fnloom import config, extract_fields, tag

UserId = NewType("UserId", int)

def a(x: int) -> int:
    return x + 1

def b(a: int, y: int = 2) -> int:
    return a * y

@tag(team="t", display_name="Bee See")
def c(b: int) -> int:
    return b + 1

def d(a: int, x: int) -> dict[str, list[int]]:
    return {"k": [a, x]}

@extract_fields({"f1": int, "f2": str})
def fields(a: int) -> dict:
    return {"f1": a, "f2": "s"}

@config.when(mode="fast")
def e__fast(c: int) -> int:
    return c

@config.when_in(mode=["slow", "slower"])
def e__slow(c: int) -> int:
    return c * 2

def weird(c: int) -> "list[dict[str, tuple[int, ...]]] | None":
    return None

def other(z: float) -> UserId:
    return UserId(int(z))

def noted(other: UserId) -> Annotated[int, "meta"]:
    return other
"""


# How long a label's type line may be, as Hamilton's.
_LABEL_WIDTH = 80


def _probe() -> types.ModuleType:
    return make_module("drawn_probe", PROBE)


def _driver(mode: str = "fast") -> fnloom.Driver:
    return fnloom.Builder().with_modules(_probe()).with_config({"mode": mode}).build()


def _names(drawing: Drawing) -> list[str]:
    return [n.name for n in drawing.nodes]


def _marked(drawing: Drawing) -> dict[str, set[str]]:
    return {n.name: set(n.marks) for n in drawing.nodes if n.marks}


def test_draw_is_every_node_and_input_in_topological_order() -> None:
    """Inputs are nodes; ties in the order are broken by name."""
    drawing = _driver().draw()
    assert _names(drawing) == [
        "x",
        "a",
        "d",
        "fields",
        "f1",
        "f2",
        "y",
        "b",
        "c",
        "e",
        "weird",
        "z",
        "other",
        "noted",
    ]
    assert drawing.edges == (
        ("x", "a"),
        ("a", "d"),
        ("x", "d"),
        ("a", "fields"),
        ("fields", "f1"),
        ("fields", "f2"),
        ("a", "b"),
        ("y", "b"),
        ("b", "c"),
        ("c", "e"),
        ("c", "weird"),
        ("z", "other"),
        ("other", "noted"),
    )
    assert not _marked(drawing)
    kinds = {n.name: n.kind for n in drawing.nodes}
    assert {name for name, kind in kinds.items() if kind == "input"} == {"x", "y", "z"}


def test_labels() -> None:
    """Whole types, the conditions of a variant, and a display name (Q91)."""
    nodes = {n.name: n for n in _driver().draw().nodes}
    assert nodes["d"].type == "dict[str, list[int]]"
    assert nodes["weird"].type == "list[dict[str, tuple[int, ...]]] | None"
    assert nodes["other"].type == "drawn_probe.UserId"
    assert nodes["noted"].type == "Annotated[int, 'meta']"
    assert nodes["x"].type == "int"
    assert nodes["e"].condition == "mode == 'fast'"
    assert nodes["a"].condition is None
    assert nodes["c"].title == "Bee See"
    assert nodes["a"].title == "a"
    slow = {n.name: n for n in _driver("slow").draw().nodes}
    assert slow["e"].condition == "mode in ['slow', 'slower']"


def test_a_long_type_is_cut_to_80_characters() -> None:
    """79 characters and an ellipsis, as Hamilton's 80 (Q91)."""
    module = make_module(
        "drawn_long",
        """
        Deep = dict[str, dict[str, dict[str, dict[str, dict[str, list[int]]]]]]

        def long(x: int) -> dict[str, Deep] | None:
            return {}
        """,
    )
    node = next(n for n in fnloom.build(module).draw().nodes if n.name == "long")
    assert len(node.type) == _LABEL_WIDTH
    assert node.type.endswith("…")


def test_an_unresolved_type_shows_its_text() -> None:
    """A name only imported under TYPE_CHECKING is drawn as written (Q81)."""
    module = make_module(
        "drawn_unresolved",
        """
        from typing import TYPE_CHECKING
        if TYPE_CHECKING:
            from somewhere import Frame
        def frame(x: int) -> "Frame":
            return x
        """,
    )
    node = next(n for n in fnloom.build(module).draw().nodes if n.name == "frame")
    assert node.type == "Frame"


def test_draw_upstream_of() -> None:
    """The named nodes and all they depend on, unmarked."""
    drawing = _driver().draw_upstream_of("c", "other")
    assert _names(drawing) == ["x", "a", "y", "b", "c", "z", "other"]
    assert not _marked(drawing)


def test_draw_downstream_of_marks_the_other_dependencies_context() -> None:
    """What depends on `b`, plus what else those read, marked context (Q90)."""
    drawing = _driver().draw_downstream_of("b")
    assert _names(drawing) == ["a", "y", "b", "c", "e", "weird"]
    assert _marked(drawing) == {"a": {"context"}, "y": {"context"}}


def test_draw_path_between() -> None:
    """The path marked `path`; context is the other dependencies, unless off."""
    drawing = _driver().draw_path_between("a", "c")
    assert _names(drawing) == ["x", "a", "y", "b", "c"]
    assert _marked(drawing) == {
        "x": {"context"},
        "a": {"path"},
        "y": {"context"},
        "b": {"path"},
        "c": {"path"},
    }
    strict = _driver().draw_path_between("a", "c", context=False)
    assert _names(strict) == ["a", "b", "c"]
    assert strict.edges == (("a", "b"), ("b", "c"))
    assert _names(_driver().draw_path_between("x", "x", context=False)) == ["x"]


def test_no_path_is_a_value_error() -> None:
    """As Hamilton's, but naming both endpoints as written."""
    with pytest.raises(ValueError, match="no path from 'c' to 'a'"):
        _driver().draw_path_between("c", "a")


def test_names_are_checked() -> None:
    """Every unknown name, sorted; a non-str and no name are TypeErrors."""
    driver = _driver()
    with pytest.raises(ValueError, match="unknown nodes: 'nada', 'nope'"):
        driver.draw_upstream_of("nope", "a", "nada")
    with pytest.raises(ValueError, match="unknown nodes: 'nope'"):
        driver.draw_downstream_of("nope")
    with pytest.raises(ValueError, match="unknown nodes: 'nada', 'nope'"):
        driver.draw_path_between("nope", "nada")
    with pytest.raises(TypeError, match="must be str, got int"):
        driver.draw_upstream_of(1)  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="at least one node"):
        driver.draw_downstream_of()


def test_dot_text() -> None:
    """Quoted ids, a label per node, a style per kind and mark, and a legend."""
    dot = _driver().draw_path_between("a", "c").to_dot()
    assert dot.startswith("digraph {\n  graph [rankdir=LR]\n")
    assert '  "c" [label="Bee See\\nint" color=red]\n' in dot
    assert '  "a" -> "b" [color=red]\n' in dot
    assert '  "x" -> "a"\n' in dot
    assert '"legend: path" [label="path" color=red]' in dot
    assert '"legend: output"' not in dot
    assert dot.endswith("}\n")
    assert "cluster_legend" not in _driver().draw().to_dot(legend=False)
    assert "rankdir=TB" in _driver().draw().to_dot(orient="TB")
    assert str(_driver().draw()) == _driver().draw().to_dot()


def test_orient_is_checked() -> None:
    """Hamilton writes any `orient` it is given; fnloom refuses a bad one."""
    with pytest.raises(ValueError, match="orient must be one of LR, TB, RL, BT"):
        _driver().draw().to_dot(orient="XX")  # ty: ignore[invalid-argument-type]


def test_names_and_labels_are_escaped() -> None:
    """A namespaced name and a display name with quotes stay one DOT string."""
    module = make_module(
        "drawn_escaped",
        r"""
        from fnloom import tag

        @tag(display_name='a "b" <c>|d\\e')
        def a(x: int) -> int:
            return x
        """,
    )
    driver = fnloom.Builder().with_modules(module, namespace="ns").build()
    dot = driver.draw().to_dot()
    assert '"ns.a" [label="a \\"b\\" <c>|d\\\\e\\nint"]' in dot
    assert '"x" -> "ns.a"' in dot


def test_drawings_are_plain_data() -> None:
    """Equal by value, pickled whole, and frozen."""
    drawing = _driver().draw()
    assert drawing == _driver().draw()
    assert pickle.loads(pickle.dumps(drawing)) == drawing  # noqa: S301  # our own bytes
    node = drawing.nodes[0]
    assert isinstance(node, DrawnNode)
    with pytest.raises(AttributeError):
        node.name = "other"  # ty: ignore[invalid-assignment]


def test_the_async_driver_draws_the_same() -> None:
    """Every graph drawing is the sync driver's."""
    module = _probe()
    sync = fnloom.Builder().with_modules(module).with_config({"mode": "fast"}).build()

    built = fnloom.Builder().with_modules(module).with_config({"mode": "fast"})
    driver = built.build_async()
    assert [
        driver.draw(),
        driver.draw_upstream_of("c"),
        driver.draw_downstream_of("a"),
        driver.draw_path_between("a", "c", context=False),
    ] == [
        sync.draw(),
        sync.draw_upstream_of("c"),
        sync.draw_downstream_of("a"),
        sync.draw_path_between("a", "c", context=False),
    ]


_SEEDED = textwrap.dedent(
    """
    import sys
    sys.path.insert(0, sys.argv[1])
    from helpers import make_module
    from test_drawing import PROBE
    import fnloom
    driver = (
        fnloom.Builder()
        .with_modules(make_module("drawn_probe", PROBE))
        .with_config({"mode": "fast"})
        .build()
    )
    print(driver.draw().to_dot())
    print(driver.draw_downstream_of("a").to_dot())
    print(driver.draw_path_between("a", "c").to_dot())
    """
)


def test_the_text_does_not_depend_on_the_hash_seed() -> None:
    """The same DOT under three seeds (Q90); Hamilton's changes (H7.3)."""
    tests = os.path.dirname(__file__)  # noqa: PTH120  # a plain path for the child
    texts = {
        subprocess.run(  # noqa: S603  # our own interpreter and script
            [sys.executable, "-c", _SEEDED, tests],
            env={**os.environ, "PYTHONHASHSEED": seed},
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        for seed in ("0", "1", "2")
    }
    assert len(texts) == 1
