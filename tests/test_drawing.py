"""Drawings: which nodes each draws, how they are marked, and the DOT text."""

import os
import pickle
import shutil
import subprocess
import sys
import textwrap
from typing import TYPE_CHECKING

import pytest

import fnloom
from fnloom import Drawing, DrawnNode
from helpers import fake_dot, make_module

if TYPE_CHECKING:
    import types
    from pathlib import Path

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


def test_draw_execution_marks_outputs() -> None:
    """What the run computes and reads, its outputs marked (Q92)."""
    drawing = _driver().draw_execution(["c", "d"], inputs={"x": 1})
    assert _names(drawing) == ["x", "a", "b", "c", "d"]
    assert _marked(drawing) == {"c": {"output"}, "d": {"output"}}


def test_draw_execution_with_an_override() -> None:
    """The override marked, and nothing only it needed (Review Focus 2)."""
    drawing = _driver().draw_execution(["c"], inputs={"x": 1}, overrides={"b": 3})
    assert _names(drawing) == ["b", "c"]
    assert _marked(drawing) == {"b": {"override"}, "c": {"output"}}
    assert drawing.edges == (("b", "c"),)


def test_draw_execution_marks_an_input_requested_as_an_output() -> None:
    """Hamilton leaves it unmarked in a box; fnloom draws and marks it."""
    drawing = _driver().draw_execution(["x", "a"], inputs={"x": 1})
    assert _marked(drawing) == {"x": {"output"}, "a": {"output"}}


def test_draw_execution_draws_a_supplied_optional_input_only() -> None:
    """A defaulted input is drawn when supplied, since only then is it read."""
    driver = _driver()
    assert "y" not in _names(driver.draw_execution(["b"], inputs={"x": 1}))
    assert "y" in _names(driver.draw_execution(["b"], inputs={"x": 1, "y": 3}))


def test_draw_execution_takes_functions() -> None:
    """A function stands for its node, as in `execute` (Q30)."""
    module = _probe()
    driver = fnloom.Builder().with_modules(module).with_config({"mode": "fast"}).build()
    by_function = driver.draw_execution([module.c], inputs={"x": 1})
    assert by_function == driver.draw_execution(["c"], inputs={"x": 1})


def test_draw_execution_validates() -> None:
    """Every problem `validate` finds, in one ValueError, unless told not to."""
    driver = _driver()
    with pytest.raises(ValueError, match=r"missing inputs: 'x' \(needed by 'a'\)"):
        driver.draw_execution(["c"])
    with pytest.raises(ValueError, match="x: expected int, got str"):
        driver.draw_execution(["c"], inputs={"x": "s"})
    lines = driver.validate(["c", "nope"], inputs={"x": "s"})
    with pytest.raises(ValueError, match="unknown outputs") as caught:
        driver.draw_execution(["c", "nope"], inputs={"x": "s"})
    assert str(caught.value) == "\n".join(lines)
    drawn = driver.draw_execution(["c"], validate=False)
    assert _names(drawn) == ["x", "a", "b", "c"]
    assert driver.draw_execution(["c"], inputs={"x": "s"}, validate=False) == drawn


def test_draw_execution_checks_names_without_validating() -> None:
    """Unknown names and a malformed request are refused either way."""
    driver = _driver()
    with pytest.raises(ValueError, match="unknown outputs: 'nope'"):
        driver.draw_execution(["nope"], validate=False)
    with pytest.raises(ValueError, match="unknown overrides: 'nada'"):
        driver.draw_execution(["c"], overrides={"nada": 1}, validate=False)
    with pytest.raises(TypeError):
        driver.draw_execution("c", validate=False)
    with pytest.raises(TypeError, match="inputs must be a mapping"):
        driver.draw_execution(["c"], inputs=["x"], validate=False)  # ty: ignore[invalid-argument-type]


def test_the_async_driver_draws_executions_the_same() -> None:
    """The async driver's execution drawing is the sync driver's."""
    module = _probe()
    built = fnloom.Builder().with_modules(module).with_config({"mode": "fast"})
    inputs, overrides = {"x": 1}, {"b": 2}
    assert built.build_async().draw_execution(
        ["c", "d"], inputs=inputs, overrides=overrides
    ) == built.build().draw_execution(["c", "d"], inputs=inputs, overrides=overrides)


_OPTIONAL = """
def a(x: int) -> int:
    return x

def b(x: int = 5) -> int:
    return x

def opt(y: int = 2) -> int:
    return y
"""


def test_draw_execution_without_validating_supplies_only_missing_inputs() -> None:
    """An input another consumer requires is not drawn when this run skips it."""
    driver = fnloom.build(make_module("drawn_optional", _OPTIONAL))
    assert _names(driver.draw_execution(["b"], validate=False)) == ["b"]
    assert _names(driver.draw_execution(["a"], validate=False)) == ["x", "a"]
    # An optional input requested as an output is drawn, not refused.
    drawn = driver.draw_execution(["y"], validate=False)
    assert _marked(drawn) == {"y": {"output"}}


def test_draw_execution_refuses_a_strict_override_without_validating() -> None:
    """`with_strict_overrides` refuses by names alone, so either way (Q75)."""
    module = make_module("drawn_strict", _OPTIONAL)
    driver = fnloom.Builder().with_modules(module).with_strict_overrides().build()
    with pytest.raises(ValueError, match="override"):
        driver.draw_execution(["a"], overrides={"x": 1}, validate=False)
    with pytest.raises(ValueError, match="override"):
        driver.draw_execution(["a"], overrides={"x": 1})


_SHAPES = """
from fnloom import extract_fields, parameterize, value

@extract_fields({"lo": int, "hi": int})
def bounds(x: int) -> dict:
    return {"lo": x, "hi": x + 1}

@parameterize(plus_one={"step": value(1)}, plus_two={"step": value(2)})
def plus(lo: int, step: int) -> int:
    return lo + step
"""


def test_draw_execution_matches_the_run_on_generated_nodes() -> None:
    """Extracted fields, rows, a namespace, and an output also overridden."""
    driver = (
        fnloom.Builder()
        .with_modules(make_module("drawn_shapes", _SHAPES), namespace="ns")
        .build()
    )
    drawn = driver.draw_execution(["ns.plus_two"], inputs={"x": 1})
    assert _names(drawn) == ["x", "ns.bounds", "ns.lo", "ns.plus_two"]
    both = driver.draw_execution(["ns.lo"], overrides={"ns.lo": 3})
    assert _marked(both) == {"ns.lo": {"output", "override"}}
    assert _names(both) == ["ns.lo"]


def test_a_cached_driver_draws_every_planned_node() -> None:
    """Hits are known only at run time, so the drawing is the plan's."""
    module = make_module("drawn_cached", _SHAPES)
    cached = fnloom.Builder().with_modules(module).with_cache(fnloom.MemoryCache())
    driver = cached.build()
    driver.execute(["plus_one"], inputs={"x": 1})
    drawn = driver.draw_execution(["plus_one"], inputs={"x": 1})
    assert drawn == fnloom.build(module).draw_execution(["plus_one"], inputs={"x": 1})


def test_a_node_named_like_a_legend_entry_keeps_its_own_id() -> None:
    """The legend's ids step aside for a drawn node's name."""
    node = DrawnNode(name="legend: function", kind="function", type="int", title="f")
    dot = Drawing((node,), ()).to_dot()
    assert dot.count('"legend: function" [') == 1
    assert '"_legend: function" [label="function"]' in dot


_DOT = shutil.which("dot")


@pytest.mark.skipif(_DOT is None, reason="Graphviz's dot is not on PATH")
def test_escaped_text_is_valid_dot() -> None:
    """Quotes, backslashes, `<`, `|` and a namespace pass `dot -Tsvg`."""
    module = make_module(
        "drawn_tricky",
        r"""
        from fnloom import tag

        @tag(display_name='a "b" <c>|d\\e {f}')
        def a(x: int) -> int:
            return x
        """,
    )
    driver = fnloom.Builder().with_modules(module, namespace="ns").build()
    assert _DOT is not None
    done = subprocess.run(  # noqa: S603  # Graphviz from PATH, our own text
        [_DOT, "-Tsvg"],
        input=driver.draw().to_dot(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert "<title>ns.a</title>" in done.stdout


def test_drawing_from_an_input() -> None:
    """An input can start a downstream or path drawing; a name twice is once."""
    driver = _driver()
    downstream = driver.draw_downstream_of("y")
    assert _names(downstream) == ["a", "y", "b", "c", "e", "weird"]
    assert _marked(driver.draw_path_between("y", "c")) == {
        "a": {"context"},
        "y": {"path"},
        "b": {"path"},
        "c": {"path"},
    }
    assert driver.draw_upstream_of("c", "c") == driver.draw_upstream_of("c")


def test_mermaid_text() -> None:
    """A flowchart: ids by position, labels escaped, a class per kind and mark."""
    text = _driver().draw_path_between("a", "c").to_mermaid()
    assert text.splitlines() == [
        "flowchart LR",
        '  n0["x<br>int"]',
        '  n1["a<br>int"]',
        '  n2["y<br>int"]',
        '  n3["b<br>int"]',
        '  n4["Bee See<br>int"]',
        "  n0 --> n1",
        "  n1 --> n3",
        "  n2 --> n3",
        "  n3 --> n4",
        "  linkStyle 1,3 stroke:red",
        "  classDef function fill:#b4d8e4,stroke:#333333",
        "  class n1,n3,n4 function",
        "  classDef input fill:#ffffff,stroke:#333333,stroke-dasharray:5 5",
        "  class n0,n2 input",
        "  classDef path stroke:red,stroke-width:2px",
        "  class n1,n3,n4 path",
        "  classDef context fill:#eeeeee,stroke:#999999,color:#777777",
        "  class n0,n2 context",
    ]
    assert _driver().draw().to_mermaid(orient="TB").startswith("flowchart TB\n")
    with pytest.raises(ValueError, match="orient must be one of"):
        _driver().draw().to_mermaid(orient="up")  # ty: ignore[invalid-argument-type]


def test_mermaid_escapes_labels() -> None:
    """Quotes, angle brackets and `#` become Mermaid's entity codes."""
    node = DrawnNode(name="n", kind="function", type="list[int]", title='a "b" <c>#1')
    text = Drawing((node,), ()).to_mermaid()
    assert '  n0["a #quot;b#quot; #lt;c#gt;#35;1<br>list[int]"]' in text


def test_render_writes_text_formats_without_graphviz(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`.dot` and `.mmd` are the drawing's own text; no Graphviz is run."""
    monkeypatch.setattr(shutil, "which", lambda _: None)
    drawing = _driver().draw()
    dot = drawing.render(tmp_path / "graph.dot", orient="TB", legend=False)
    assert dot == tmp_path / "graph.dot"
    assert dot.read_text() == drawing.to_dot(orient="TB", legend=False)
    mermaid = drawing.render(tmp_path / "graph.txt", format="mmd")
    assert mermaid.read_text() == drawing.to_mermaid()


def test_render_without_graphviz_says_how_to_get_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A FileNotFoundError naming Graphviz, never a silent `None` (Q94)."""
    monkeypatch.setattr(shutil, "which", lambda _: None)
    drawing = _driver().draw()
    with pytest.raises(FileNotFoundError, match="needs Graphviz's 'dot' on PATH"):
        drawing.render(tmp_path / "graph.svg")
    assert not (tmp_path / "graph.svg").exists()
    assert drawing._repr_svg_() is None  # Jupyter's hook
    with pytest.raises(ValueError, match="give a format"):
        drawing.render(tmp_path / "graph")


@pytest.mark.skipif(_DOT is None, reason="Graphviz's dot is not on PATH")
def test_render_runs_graphviz(tmp_path: Path) -> None:
    """An image by `dot`; its own error for a format it doesn't know."""
    drawing = _driver().draw()
    svg = drawing.render(tmp_path / "graph.svg")
    assert svg.read_text().lstrip().startswith("<?xml")
    png = drawing.render(tmp_path / "graph.out", format="png")
    assert png.read_bytes().startswith(b"\x89PNG")
    repr_svg = drawing._repr_svg_()  # Jupyter's hook
    assert repr_svg is not None
    assert "<svg" in repr_svg
    with pytest.raises(RuntimeError, match="dot failed"):
        drawing.render(tmp_path / "graph.nope")


def test_render_runs_dot_from_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `dot` on PATH gets `-T<format>` and the DOT text; its stdout is written."""
    fake_dot(
        tmp_path,
        """
        import sys
        text = sys.stdin.read()
        sys.stdout.write(sys.argv[1] + "|" + str(len(text)))
        """,
    )
    monkeypatch.setenv("PATH", str(tmp_path))
    drawing = _driver().draw()
    out = drawing.render(tmp_path / "graph.SVG")
    assert out.read_text() == f"-Tsvg|{len(drawing.to_dot())}"
    pdf = drawing.render(tmp_path / "graph.out", format=".pdf")
    assert pdf.read_text().startswith("-Tpdf|")
    assert drawing._repr_svg_() == f"-Tsvg|{len(drawing.to_dot())}"  # Jupyter's hook


def test_a_failing_dot_is_a_runtime_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its stderr in the message; nothing is written."""
    fake_dot(
        tmp_path,
        """
        import sys
        sys.stderr.write("Format: nope not recognized\\n")
        sys.exit(1)
        """,
    )
    monkeypatch.setenv("PATH", str(tmp_path))
    drawing = _driver().draw()
    with pytest.raises(RuntimeError, match="dot failed: Format: nope not recognized"):
        drawing.render(tmp_path / "graph.nope")
    assert not (tmp_path / "graph.nope").exists()
    assert drawing._repr_svg_() is None  # Jupyter's hook


def test_render_suffixes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Suffixes in any case; `.gv` is DOT text; a missing folder is named."""
    monkeypatch.setattr(shutil, "which", lambda _: None)
    drawing = _driver().draw()
    assert drawing.render(tmp_path / "g.GV").read_text() == drawing.to_dot()
    assert drawing.render(tmp_path / "g.DOT").read_text() == drawing.to_dot()
    assert drawing.render(tmp_path / "g.MMD").read_text() == drawing.to_mermaid()
    with pytest.raises(FileNotFoundError, match=r"no folder to write g\.svg in"):
        drawing.render(tmp_path / "none" / "g.svg")


def test_ipython_shows_the_dot_text() -> None:
    """Without an SVG, IPython's pretty printer gets the DOT text."""
    shown: list[str] = []

    class Printer:
        def text(self, obj: str) -> None:
            shown.append(obj)

    drawing = _driver().draw()
    drawing._repr_pretty_(Printer(), cycle=False)  # IPython's hook
    assert shown == [drawing.to_dot()]


def test_mermaid_escapes_a_namespaced_tricky_label() -> None:
    """`&`, backticks, `|`, a newline and a condition stay inside one label."""
    node = DrawnNode(
        name="ns.a",
        kind="function",
        type="int",
        title="a & `b` | c\nd",
        condition="mode == 'x'",
    )
    text = Drawing((node,), ()).to_mermaid()
    assert "  n0[\"a #38; #96;b#96; | c d<br>int<br>mode == 'x'\"]" in text
