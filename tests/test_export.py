"""Exports: the document a graph or a run makes, its versions, and their diff."""

import json
import os
import subprocess
import sys
import textwrap
from typing import Any

import pytest

import fnloom
from fnloom import _export
from fnloom._codeversion import code_version
from helpers import make_module

GRAPH = """
from fnloom import config, parameterize, tag, value

@tag(team="t")
def a(x: int) -> int:
    \"\"\"Adds one.\"\"\"
    return x + 1

def b(a: int, y: int = 2) -> int:
    return a * y

@config.when(mode="fast")
def c__fast(b: int) -> dict[str, list[int]]:
    return {"k": [b]}

@parameterize(scaled={"factor": value(3)})
def scale(a: int, factor: int) -> int:
    return a * factor
"""


def _driver(source: str = GRAPH, name: str = "exported") -> fnloom.Driver:
    module = make_module(name, source)
    return fnloom.Builder().with_modules(module).with_config({"mode": "fast"}).build()


def _nodes(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {node["name"]: node for node in document["nodes"]}


def test_the_whole_graph() -> None:
    """Every node and input, in drawing order, with the format and config."""
    document = _driver().export()
    assert document["format"] == "fnloom.export/1"
    assert document["fnloom"] == fnloom.__version__
    assert document["config"] == {"mode": "'fast'"}
    assert document["request"] is None
    assert [n["name"] for n in document["nodes"]] == [
        "x",
        "a",
        "scaled",
        "y",
        "b",
        "c",
    ]
    assert json.loads(json.dumps(document)) == document


def test_a_function_node() -> None:
    """Its type whole, its parameters' bindings, tags, doc, source and version."""
    driver = _driver()
    nodes = _nodes(driver.export())
    a = nodes["a"]
    assert a["kind"] == "function"
    assert a["type"] == "int"
    assert a["params"] == [
        {
            "name": "x",
            "slot": None,
            "reads": "x",
            "value": None,
            "required": True,
            "type": "int",
        }
    ]
    assert a["dependencies"] == ["x"]
    assert a["tags"] == {"team": "t"}
    assert a["doc"] == "Adds one."
    assert a["module"] == "exported"
    assert a["function"] == "exported.a"
    assert a["source"] is None  # made by `exec`, so Python has no source for it
    origin = driver.nodes["a"].origin
    assert origin is not None
    assert a["version"] == code_version(origin).hex()
    assert "unversioned" not in a
    assert nodes["c"]["type"] == "dict[str, list[int]]"
    assert nodes["c"]["config"] == ["mode == 'fast'"]
    assert nodes["scaled"]["params"][1]["value"] == "3"
    assert nodes["scaled"]["params"][1]["reads"] is None


def test_an_input_node() -> None:
    """An input is an input: no source or version of a consumer's (Q95)."""
    x = _nodes(_driver().export())["x"]
    assert x["kind"] == "input"
    assert x["source"] is None
    assert x["version"] is None
    assert x["function"] is None
    assert x["required"] is True
    assert x["consumers"] == ["a"]
    assert _nodes(_driver().export())["y"]["required"] is False


def test_a_run() -> None:
    """The run's nodes and marks; names only in `request`."""
    document = _driver().export(["c", "x"], inputs={"x": 1}, overrides={"b": 5})
    assert document["request"] == {
        "outputs": ["c", "x"],
        "inputs": ["x"],
        "overrides": ["b"],
    }
    nodes = _nodes(document)
    assert list(nodes) == ["b", "c", "x"]
    assert nodes["b"]["marks"] == ["override"]
    assert nodes["c"]["marks"] == ["output"]
    assert nodes["x"]["marks"] == ["output"]
    assert "5" not in json.dumps(document["request"])


def test_a_run_is_validated_unless_told_not_to() -> None:
    """As `draw_execution`; without validating, a missing input is still listed."""
    driver = _driver()
    with pytest.raises(ValueError, match="missing inputs"):
        driver.export(["b"])
    document = driver.export(["b"], validate=False)
    assert document["request"] == {"outputs": ["b"], "inputs": [], "overrides": []}
    assert "x" in _nodes(document)
    with pytest.raises(TypeError, match="give final_vars too"):
        driver.export(inputs={"x": 1})


def test_functions_in_the_request() -> None:
    """A function stands for its node, as in `execute` (Q30)."""
    module = make_module("exported_fn", GRAPH)
    driver = fnloom.Builder().with_modules(module).with_config({"mode": "fast"}).build()
    assert driver.export([module.a], inputs={"x": 1}) == driver.export(
        ["a"], inputs={"x": 1}
    )


_MUTABLE = """
LIMITS = [1, 2]

def capped(x: int) -> int:
    return min(x, LIMITS[0])

def plain(x: int) -> int:
    return x
"""


def test_an_unversioned_node() -> None:
    """No code version: `version` null with why, and no graph version (Q96)."""
    document = _driver(_MUTABLE, "exported_mutable").export()
    capped = _nodes(document)["capped"]
    assert capped["version"] is None
    assert "LIMITS" in capped["unversioned"]
    assert document["graph_version"] is None


def _graph_version(source: str, name: str) -> object:
    return fnloom.build(make_module(name, source)).export()["graph_version"]


def test_the_graph_version_sees_names_wiring_types_and_code() -> None:
    """A rename, a rewired edge, a type or a constant each change it (Q96)."""
    base = """
K = 1

def a(x: int) -> int:
    return x + K

def b(a: int) -> int:
    return a
"""
    version = _graph_version(base, "gv_base")
    assert isinstance(version, str)
    assert _graph_version(base, "gv_again") == version
    changes = {
        "rename": base.replace("def b(a: int)", "def b2(a: int)"),
        "rewire": base.replace(
            "def b(a: int) -> int:\n    return a", "def b(x: int) -> int:\n    return x"
        ),
        "type": base.replace("def b(a: int) -> int", "def b(a: int) -> float"),
        "constant": base.replace("K = 1", "K = 2"),
    }
    for change, source in changes.items():
        assert _graph_version(source, f"gv_{change}") != version, change


def test_diff() -> None:
    """Added, removed, changed with the fields, and unversioned nodes."""
    old = _driver().export()
    new_source = (
        GRAPH.replace("return x + 1", "return x + 2")
        .replace('@tag(team="t")', '@tag(team="u")')
        .replace("def b(a: int, y: int = 2) -> int:", "def b(a: int) -> int:")
        .replace("a * y", "a * 2")
        + "\ndef d(b: int) -> int:\n    return b\n"
    )
    new = _driver(new_source, "exported_new").export()
    found = _export.diff(old, new)
    assert found["added"] == ["d"]
    assert found["removed"] == ["y"]
    assert found["changed"]["a"] == ["tags", "version"]
    assert found["changed"]["b"] == ["params", "version"]
    assert "x" not in found["changed"]
    assert found["unversioned"] == []
    assert _export.diff(old, old) == {
        "added": [],
        "removed": [],
        "changed": {},
        "unversioned": [],
    }


def test_diff_names_a_node_it_cannot_see_into() -> None:
    """An unversioned node whose entry is otherwise the same is listed apart."""
    old = _driver(_MUTABLE, "exported_m1").export()
    new = _driver(_MUTABLE.replace("[1, 2]", "[9, 2]"), "exported_m2").export()
    found = _export.diff(old, new)
    assert found["unversioned"] == ["capped"]
    assert "capped" not in found["changed"]
    with pytest.raises(ValueError, match="new: not an fnloom export"):
        _export.diff(old, {"nodes": []})


def test_the_async_driver_exports_the_same() -> None:
    """The async driver's document is the sync driver's."""
    module = make_module("exported_async", GRAPH)
    built = fnloom.Builder().with_modules(module).with_config({"mode": "fast"})
    assert built.build_async().export() == built.build().export()
    assert built.build_async().export(["b"], inputs={"x": 1}) == built.build().export(
        ["b"], inputs={"x": 1}
    )


_SEEDED = textwrap.dedent(
    """
    import json, sys
    sys.path.insert(0, sys.argv[1])
    from helpers import make_module
    from test_export import GRAPH
    import fnloom
    driver = (
        fnloom.Builder()
        .with_modules(make_module("exported", GRAPH))
        .with_config({"mode": "fast"})
        .build()
    )
    print(json.dumps(driver.export(), sort_keys=True))
    print(json.dumps(driver.export(["c"], inputs={"x": 1}), sort_keys=True))
    """
)


def test_the_document_does_not_depend_on_the_hash_seed() -> None:
    """The same JSON under three seeds (Review Focus 1)."""
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
