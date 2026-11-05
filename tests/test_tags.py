"""Tests for `@tag` sequence values and `NodeViews.tagged` (Plan 12, item 3)."""

import json
from typing import TYPE_CHECKING

import pytest

import fnloom
from fnloom import _cli as cli
from fnloom import _export
from helpers import make_module

if TYPE_CHECKING:
    from pathlib import Path

USAGE = 2
HEADER = "import fnloom\nfrom fnloom import tag\n"

SOURCE = (
    HEADER
    + """
@tag(owner=("ann", "bo"), stage="raw")
def a(x: int) -> int:
    return x

@tag(owner="ann", stage=["raw", "clean"])
def b(a: int) -> int:
    return a

@tag(owner="cy")
def c(b: int) -> int:
    return b

def d(c: int) -> int:
    return c
"""
)


def _names(views: object) -> list[str]:
    return [view.name for view in views]  # ty: ignore[not-iterable]  # a tuple of views


def test_a_sequence_value_is_stored_as_a_tuple_in_written_order() -> None:
    """A list or a tuple reads back as a tuple; a string stays a string."""
    nodes = fnloom.build(make_module("tags_tuple", SOURCE)).nodes
    assert nodes["a"].tags["owner"] == ("ann", "bo")
    assert nodes["a"].tags["stage"] == "raw"
    assert nodes["b"].tags["stage"] == ("raw", "clean")


@pytest.mark.parametrize(
    ("value", "error"),
    [
        ("()", ValueError),
        ('("a", "a")', ValueError),
        ('("a", 1)', TypeError),
        ('{"a", "b"}', TypeError),
        ('frozenset({"a"})', TypeError),
        ('{"a": "b"}', TypeError),
        ("1", TypeError),
        ("[]", ValueError),
    ],
)
def test_bad_tag_values_are_refused_naming_the_tag(
    value: str, error: type[Exception]
) -> None:
    """Empty, repeated, non-str and unordered values fail when decorating."""
    with pytest.raises(error, match="'owner'"):
        make_module(
            "tags_bad",
            HEADER + f"@tag(owner={value})\ndef f(x: int) -> int:\n    return x\n",
        )


def test_tagged_matches_a_string_a_tuple_holding_it_or_any_listed_value() -> None:
    """A string query matches an equal value or a member; a sequence matches any."""
    nodes = fnloom.build(make_module("tags_query", SOURCE)).nodes
    assert _names(nodes.tagged(owner="ann")) == ["a", "b"]
    assert _names(nodes.tagged(owner="bo")) == ["a"]
    assert _names(nodes.tagged(owner=("bo", "cy"))) == ["a", "c"]
    assert _names(nodes.tagged(stage="raw")) == ["a", "b"]
    assert _names(nodes.tagged(owner="ann", stage="clean")) == ["b"]


def test_tagged_any_means_present_and_unknown_names_match_nothing() -> None:
    """`ANY` matches a node with the tag; an unknown tag matches none."""
    nodes = fnloom.build(make_module("tags_any", SOURCE)).nodes
    assert _names(nodes.tagged(owner=fnloom.ANY)) == ["a", "b", "c"]
    assert _names(nodes.tagged(missing=fnloom.ANY)) == []
    assert _names(nodes.tagged(missing="x")) == []


def test_tagged_with_no_query_returns_every_node_in_view_order() -> None:
    """No query is no filter: every view, in the mapping's order."""
    nodes = fnloom.build(make_module("tags_all", SOURCE)).nodes
    assert _names(nodes.tagged()) == list(nodes)


@pytest.mark.parametrize("query", [{"owner": 1}, {"owner": ()}, {"owner": {"a"}}])
def test_a_malformed_query_is_a_type_error(query: dict[str, object]) -> None:
    """A query value must be a str, a non-empty sequence of str, or ANY."""
    nodes = fnloom.build(make_module("tags_malformed", SOURCE)).nodes
    with pytest.raises(TypeError, match="'owner'"):
        nodes.tagged(**query)  # ty: ignore[invalid-argument-type]  # the refusal is the point


def test_export_writes_a_list_and_diff_sees_a_changed_order() -> None:
    """A tuple tag is a JSON list in the export, and its order is compared."""
    one = fnloom.build(make_module("tags_export_one", SOURCE))
    text = SOURCE.replace('("ann", "bo")', '("bo", "ann")')
    two = fnloom.build(make_module("tags_export_two", text))
    first, second = one.export(), two.export()
    entry = next(n for n in first["nodes"] if n["name"] == "a")
    assert entry["tags"]["owner"] == ["ann", "bo"]
    assert entry["tags"]["stage"] == "raw"
    assert _export.diff(first, second)["changed"] == {"a": ["tags"]}


def test_tuple_tags_are_hash_seed_stable() -> None:
    """The export and the drawing of tuple tags are the same text on every hash seed.

    And a `diff` of one seed's export against another's sees no change at all.
    """
    import os  # noqa: PLC0415  # used by this test alone
    import subprocess  # noqa: PLC0415  # used by this test alone
    import sys  # noqa: PLC0415  # used by this test alone
    import textwrap  # noqa: PLC0415  # used by this test alone

    script = textwrap.dedent(
        """
        import sys, types, json, fnloom
        module = types.ModuleType("seeded")
        sys.modules["seeded"] = module
        exec(sys.argv[1], module.__dict__)
        driver = fnloom.build(module)
        drawing = driver.draw()
        print(json.dumps({
            "export": json.dumps(driver.export(), sort_keys=True),
            "dot": drawing.to_dot(),
            "mermaid": drawing.to_mermaid(),
            "str": str(drawing),
        }))
        """
    )
    runs = {
        seed: json.loads(
            subprocess.run(  # noqa: S603  # this interpreter, fixed arguments
                [sys.executable, "-c", script, SOURCE],
                check=True,
                capture_output=True,
                text=True,
                env={**os.environ, "PYTHONHASHSEED": seed},
            ).stdout
        )
        for seed in ("0", "1", "2")
    }
    for part in ("export", "dot", "mermaid", "str"):
        assert len({run[part] for run in runs.values()}) == 1, part
    first = json.loads(runs["0"]["export"])
    nodes = {n["name"]: n for n in first["nodes"]}
    assert nodes["a"]["tags"] == {"owner": ["ann", "bo"], "stage": "raw"}
    changes = _export.diff(first, json.loads(runs["1"]["export"]))
    assert changes["added"] == []
    assert changes["removed"] == []
    assert changes["changed"] == {}
    assert changes["graph_version_changed"] is False


def test_any_is_one_object_after_copy_deepcopy_and_pickle() -> None:
    """`ANY` copied, deep-copied or pickled is `ANY`, and still matches."""
    import copy  # noqa: PLC0415  # used by this test alone
    import pickle  # noqa: PLC0415  # used by this test alone

    nodes = fnloom.build(make_module("tags_any_copy", SOURCE)).nodes
    for again in (
        copy.copy(fnloom.ANY),
        copy.deepcopy(fnloom.ANY),
        pickle.loads(pickle.dumps(fnloom.ANY)),  # noqa: S301  # our own bytes
        copy.deepcopy({"owner": fnloom.ANY})["owner"],
    ):
        assert again is fnloom.ANY
        assert _names(nodes.tagged(owner=again)) == ["a", "b", "c"]


def test_a_str_subclass_value_is_stored_as_plain_str() -> None:
    """A `str` subclass, alone or as an item, reads back as a plain `str`."""
    module = make_module(
        "tags_str_subclass",
        HEADER
        + "class Name(str):\n    __slots__ = ()\n\n"
        + '@tag(owner=Name("ann"), stage=[Name("raw"), "clean"])\n'
        + "def f(x: int) -> int:\n    return x\n",
    )
    view = fnloom.build(module).nodes["f"]
    assert type(view.tags["owner"]) is str
    stage = view.tags["stage"]
    assert isinstance(stage, tuple)
    assert [type(item) for item in stage] == [str, str]
    assert view.tags == {"owner": "ann", "stage": ("raw", "clean")}


def test_the_cli_refuses_a_tag_key_given_twice(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--tag k=a --tag k=b` is a usage error, for `export` and `draw` alike."""
    source = tmp_path / "tagged_twice.py"
    source.write_text(SOURCE)
    for command in ("export", "draw"):
        argv = [command, str(source), "--tag", "owner=ann", "--tag", "owner=bo"]
        assert cli.main(argv) == USAGE
        assert "--tag owner given twice" in capsys.readouterr().err


def test_the_cli_keeps_only_the_tagged_nodes(tmp_path: Path) -> None:
    """`--tag` restricts `draw` and `export` to nodes with every tag; AND-ed."""
    source = tmp_path / "tagged_cli.py"
    source.write_text(SOURCE)
    base = [str(source)]
    out = tmp_path / "out.json"
    assert cli.main(["export", *base, "--tag", "owner=ann", "-o", str(out)]) == 0
    assert [n["name"] for n in json.loads(out.read_text())["nodes"]] == ["a", "b"]
    assert (
        cli.main(
            [
                "export",
                *base,
                "--tag",
                "owner=ann",
                "--tag",
                "stage=clean",
                "-o",
                str(out),
            ]
        )
        == 0
    )
    assert [n["name"] for n in json.loads(out.read_text())["nodes"]] == ["b"]
    dot = tmp_path / "out.dot"
    assert cli.main(["draw", *base, "--tag", "owner=cy", "-o", str(dot)]) == 0
    assert '"c"' in dot.read_text()
    assert '"a"' not in dot.read_text()
    assert cli.main(["export", *base, "--tag", "owner"]) == USAGE


def test_a_tuple_display_name_is_drawn_joined() -> None:
    """A `display_name` given as a tuple is the node's title, joined by ", "."""
    module = make_module(
        "tags_display",
        HEADER
        + '@tag(display_name=("Total", "net"))\ndef t(x: int) -> int:\n    return x\n',
    )
    drawn = fnloom.build(module).draw()
    assert next(n for n in drawn.nodes if n.name == "t").title == "Total, net"


def test_a_tag_call_with_a_mix_of_errors_raises_a_type_error_naming_both() -> None:
    """A `TypeError` and a `ValueError` together: one `TypeError`, every tag named."""
    with pytest.raises(TypeError, match=r"'area'.*'zone'"):
        fnloom.tag(zone=1, area=())  # ty: ignore[invalid-argument-type]  # the refusal is the point
    with pytest.raises(ValueError, match=r"'area'.*'zone'"):
        fnloom.tag(zone=(), area=("a", "a"))


def test_draw_with_a_tag_drops_edges_to_left_out_nodes(tmp_path: Path) -> None:
    """Only edges between kept nodes remain."""
    source = tmp_path / "tagged_edges.py"
    source.write_text(SOURCE)
    dot = tmp_path / "edges.dot"
    assert cli.main(["draw", str(source), "--tag", "owner=ann", "-o", str(dot)]) == 0
    text = dot.read_text()
    assert '"a" -> "b"' in text
    assert '"b" -> "c"' not in text
