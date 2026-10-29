"""Exports: the document a graph or a run makes, its versions, and their diff."""

import json
import os
import subprocess
import sys
import textwrap
import typing
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
            "value_version": None,
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
        "graph_version_changed": False,
        "warnings": [],
    }


def test_diff_names_a_node_it_cannot_see_into() -> None:
    """An unversioned node whose entry is otherwise the same is listed apart."""
    old = _driver(_MUTABLE, "exported_m1").export()
    new = _driver(_MUTABLE.replace("[1, 2]", "[9, 2]"), "exported_m2").export()
    found = _export.diff(old, new)
    assert found["unversioned"] == ["capped"]
    assert "capped" not in found["changed"]
    assert found["warnings"] == [
        f"{side} has no graph_version: a change inside an unversioned node "
        "(capped) can't be seen"
        for side in ("old", "new")
    ]
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


SETS = """
from fnloom import parameterize, value

class Marker:
    pass

@parameterize(tagged={"labels": value(frozenset({"alpha", "beta", "gamma"}))})
def labelled(x: int, labels: frozenset) -> int:
    return x

@parameterize(marked={"marker": value(Marker())})
def marking(x: int, marker: Marker) -> int:
    return x
"""

_SEEDED_SETS = textwrap.dedent(
    """
    import json, sys
    sys.path.insert(0, sys.argv[1])
    from helpers import make_module
    from test_export import SETS
    import fnloom
    module = make_module("exported_sets", SETS)
    config = {"names": {"p", "q", "r"}, "thing": object()}
    builder = fnloom.Builder().with_functions(module.labelled)
    tagged = builder.with_config(config).build()
    print(json.dumps(tagged.export(), sort_keys=True))
    print(json.dumps(fnloom.build(module).export(), sort_keys=True))
    """
)


def test_sets_and_addresses_do_not_change_the_text() -> None:
    """Sets sorted and addresses dropped, under three seeds; a set value versioned."""
    tests = os.path.dirname(__file__)  # noqa: PTH120  # a plain path for the child
    texts = {
        subprocess.run(  # noqa: S603  # our own interpreter and script
            [sys.executable, "-c", _SEEDED_SETS, tests],
            env={**os.environ, "PYTHONHASHSEED": seed},
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        for seed in ("0", "1", "2")
    }
    assert len(texts) == 1
    tagged, whole = (json.loads(line) for line in texts.pop().splitlines())
    assert tagged["config"] == {
        "names": "{'p', 'q', 'r'}",
        "thing": "<object object>",
    }
    assert tagged["graph_version"] is not None
    labels = _nodes(tagged)["tagged"]["params"][1]["value"]
    assert labels == "frozenset({'alpha', 'beta', 'gamma'})"
    # A value known only by its address can't be versioned.
    assert whole["graph_version"] is None
    assert _nodes(whole)["marked"]["params"][1]["value"].startswith("<")
    assert " at 0x" not in _nodes(whole)["marked"]["params"][1]["value"]


def test_stable_repr() -> None:
    """Containers through, sets sorted, a one-item tuple kept a tuple."""
    assert _export.stable_repr({"b": [1, (2,)], "a": set()}) == (
        "{'b': [1, (2,)], 'a': set()}"
    )
    assert _export.stable_repr(frozenset()) == "frozenset()"
    assert _export.stable_repr({3, 1, 2}) == "{1, 2, 3}"


def test_a_pinned_version() -> None:
    """`@cache(version=...)` stands for the code, as the result cache takes it."""
    source = _MUTABLE.replace(
        "def capped", "from fnloom import cache\n\n@cache(version='v1')\ndef capped"
    )
    document = _driver(source, "exported_pinned").export()
    capped = _nodes(document)["capped"]
    assert capped["pinned"] == "v1"
    assert capped["version"] is not None
    assert "unversioned" not in capped
    assert document["graph_version"] is not None
    bumped = _driver(source.replace("'v1'", "'v2'"), "exported_bumped").export()
    assert bumped["graph_version"] != document["graph_version"]


_KINDS = """
from typing import TYPE_CHECKING

from fnloom import group, parameterize, source

if TYPE_CHECKING:
    from decimal import Decimal

def frame(x: int) -> dict:
    return {"c1": [x], "c2": [x]}

@parameterize(total={"parts": group(source("x"), source("y"))})
def summed(parts: list[int]) -> int:
    return sum(parts)

def money(x: int) -> "Decimal":
    return x
"""


def test_every_kind_of_node_exports() -> None:
    """Group slots, an unresolved type and a node made with `fnloom.node`."""
    module = make_module("exported_kinds", _KINDS)

    def plain(total: int) -> int:
        return total

    driver = (
        fnloom.Builder()
        .with_modules(module)
        .with_functions(fnloom.node(plain, name="renamed"))
        .build()
    )
    document = driver.export()
    nodes = _nodes(document)
    assert [p["slot"] for p in nodes["total"]["params"]] == [0, 1]
    assert [p["reads"] for p in nodes["total"]["params"]] == ["x", "y"]
    assert nodes["money"]["type"] == "Decimal"
    assert nodes["renamed"]["function"].endswith("plain")
    assert json.loads(json.dumps(document)) == document


def test_diff_compares_marks_and_refuses_a_name_twice() -> None:
    """A run's outputs changing shows; a document naming a node twice is refused."""
    driver = _driver()
    old = driver.export(["b"], inputs={"x": 1})
    new = driver.export(["b", "a"], inputs={"x": 1})
    assert _export.diff(old, new)["changed"] == {"a": ["marks"]}
    twice = {**old, "nodes": [*old["nodes"], old["nodes"][0]]}
    with pytest.raises(ValueError, match="new: nodes named twice: 'x'"):
        _export.diff(old, twice)


_ARRAY = """
from fnloom import parameterize, value

class Opaque:
    def __init__(self, n: int) -> None:
        self.n = n

@parameterize(scaled={{"k": value(list(range(2000)))}})
def scale(x: int, k: list) -> int:
    return x

@parameterize(held={{"thing": value({thing})}})
def hold(x: int, thing: object) -> int:
    return x
"""


def test_diff_sees_a_value_whose_text_hides_the_change() -> None:
    """A changed `value()` shows under `changed`, though its text may not."""
    old = _driver(_ARRAY.format(thing="1"), "exported_array_old").export()
    edited = _ARRAY.replace(
        "list(range(2000))", "[*range(1000), 7, *range(1001, 2000)]"
    )
    new = _driver(edited.format(thing="1"), "exported_array_new").export()
    found = _export.diff(old, new)
    assert found["changed"] == {"scaled": ["params"]}
    assert found["graph_version_changed"] is True
    scaled = _nodes(new)["scaled"]["params"][1]
    assert (
        scaled["value_version"] != _nodes(old)["scaled"]["params"][1]["value_version"]
    )


def test_diff_warns_when_only_the_graph_version_moved() -> None:
    """A safety net: differing graph versions with equal entries still say so."""
    old = _driver().export()
    new = {**old, "graph_version": "0" * 64}
    found = _export.diff(old, new)
    assert found["graph_version_changed"] is True
    assert found["warnings"] == [
        (
            "the graph_versions differ, though no node's exported entry does: "
            "a change the entries don't show"
        )
    ]


SEEDED_CONTAINERS = """
import collections
import dataclasses
import typing

from fnloom import parameterize, value

@dataclasses.dataclass(frozen=True)
class D:
    names: frozenset

class P(typing.NamedTuple):
    names: frozenset

ORDERED = collections.OrderedDict(names=frozenset({"alpha", "beta", "gamma"}))

@parameterize(
    in_dataclass={"held": value(D(frozenset({"alpha", "beta", "gamma"})))},
    in_namedtuple={"held": value(P(frozenset({"alpha", "beta", "gamma"})))},
    in_ordered={"held": value(ORDERED)},
)
def hold(x: int, held: object) -> int:
    return x
"""

_SEEDED_CONTAINERS = textwrap.dedent(
    """
    import collections, json, sys
    sys.path.insert(0, sys.argv[1])
    from helpers import make_module
    from test_export import SEEDED_CONTAINERS
    import fnloom
    module = make_module("exported_containers", SEEDED_CONTAINERS)
    config = {
        "ordered": collections.OrderedDict(s=frozenset({"p", "q", "r"})),
        "named": module.P(frozenset({"p", "q", "r"})),
    }
    driver = fnloom.Builder().with_modules(module).with_config(config).build()
    print(json.dumps(driver.export(), sort_keys=True))
    """
)


def test_sets_inside_any_container_do_not_change_the_text() -> None:
    """Dataclasses, named tuples and OrderedDicts too, under three seeds (finding 2)."""
    tests = os.path.dirname(__file__)  # noqa: PTH120  # a plain path for the child
    texts = {
        subprocess.run(  # noqa: S603  # our own interpreter and script
            [sys.executable, "-c", _SEEDED_CONTAINERS, tests],
            env={**os.environ, "PYTHONHASHSEED": seed},
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        for seed in ("0", "1", "2", "3")
    }
    assert len(texts) == 1
    document = json.loads(texts.pop())
    assert document["graph_version"] is not None
    assert document["config"]["named"] == "P(names=frozenset({'p', 'q', 'r'}))"
    assert document["config"]["ordered"] == (
        "OrderedDict({'s': frozenset({'p', 'q', 'r'})})"
    )
    held = _nodes(document)["in_dataclass"]["params"][1]["value"]
    assert held == "D(names=frozenset({'alpha', 'beta', 'gamma'}))"


def test_stable_repr_of_a_container_holding_itself() -> None:
    """`...` where it recurs, as `repr` writes it."""
    looped: list[object] = [1]
    looped.append(looped)
    assert _export.stable_repr(looped) == "[1, ...]"


def test_a_value_known_only_by_its_address_is_unversioned() -> None:
    """Its text can't tell two apart, so diff lists it, never as unchanged."""
    old = _driver(_ARRAY.format(thing="Opaque(1)"), "exported_opaque").export()
    new = _driver(_ARRAY.format(thing="Opaque(2)"), "exported_opaque").export()
    held = _nodes(old)["held"]["params"][1]
    assert held["value"] == _nodes(new)["held"]["params"][1]["value"]
    assert held["value_version"] is None
    found = _export.diff(old, new)
    assert found["unversioned"] == ["held"]
    assert found["changed"] == {}
    assert old["graph_version"] is None


_CANONICAL = """
import dataclasses

import numpy as np

from fnloom import parameterize, value

@dataclasses.dataclass(frozen=True)
class W:
    tags: frozenset
    arr: object = dataclasses.field(compare=False)

@dataclasses.dataclass(frozen=True)
class H:
    tags: frozenset
    secret: int = dataclasses.field(repr=False)

@parameterize(
    wide={{"held": value(W(frozenset({{"a"}}), {array}))}},
    hidden={{"held": value(H(frozenset({{"a"}}), {secret}))}},
)
def hold(x: int, held: object) -> int:
    return x
"""


def test_a_set_holding_value_is_versioned_from_its_parts_not_its_text() -> None:
    """An elided array element or a `repr=False` field still changes the version."""
    pytest.importorskip("numpy")
    base = {"array": "np.arange(2000)", "secret": "1"}
    old = _driver(_CANONICAL.format(**base), "exported_canonical").export()
    changed_array = {
        **base,
        "array": "np.where(np.arange(2000) == 1000, 7, np.arange(2000))",
    }
    changed_secret = {**base, "secret": "2"}
    for change, expected in ((changed_array, "wide"), (changed_secret, "hidden")):
        new = _driver(_CANONICAL.format(**change), "exported_canonical").export()
        before = _nodes(old)[expected]["params"][1]
        after = _nodes(new)[expected]["params"][1]
        assert before["value"] == after["value"], expected
        assert before["value_version"] != after["value_version"], expected
        found = _export.diff(old, new)
        assert found["changed"] == {expected: ["params"]}
        assert found["graph_version_changed"] is True


class _Raising(dict[str, object]):
    """A mapping whose iteration raises, as a broken user mapping might."""

    @typing.override
    def __iter__(self) -> typing.Iterator[str]:
        raise RuntimeError(_NO_ITERATING)

    @typing.override
    def items(self) -> typing.Never:
        raise RuntimeError(_NO_ITERATING)


_NO_ITERATING = "no iterating"


def test_a_value_that_raises_or_is_too_deep_is_unversioned() -> None:
    """No crash: written as unrepresentable, with no version."""
    raising = _Raising()
    assert _export.stable_repr(raising) == "<unrepresentable _Raising>"
    assert _export.value_version(raising) is None
    deep: list[object] = [frozenset({"a"})]
    for _ in range(3000):
        deep = [deep]
    assert _export.stable_repr(deep) == "<unrepresentable list>"
    assert _export.value_version(deep) is None
    module = make_module(
        "exported_deep",
        "from fnloom import parameterize, value\n"
        "DEEP = []\n"
        "@parameterize(held={'v': value(DEEP)})\n"
        "def hold(x: int, v: object) -> int:\n"
        "    return x\n",
    )
    module.DEEP.append(deep)
    document = fnloom.build(module).export()
    assert _nodes(document)["held"]["params"][1]["value_version"] is None
    assert document["graph_version"] is None
