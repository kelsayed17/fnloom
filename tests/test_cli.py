"""The `fnloom` command: loading, requests, each command's output, and errors."""

import json
import subprocess
import sys
import textwrap
from typing import TYPE_CHECKING

import pytest

from fnloom._cli import main

if TYPE_CHECKING:
    from pathlib import Path

FLOW = """
from fnloom import config
from helper import double

def a(x: int) -> int:
    return double(x)

def b(a: int, y: int = 2) -> int:
    return a * y

@config.when(mode="fast")
def c__fast(b: int) -> int:
    return b + 1
"""

HELPER = """
def double(v: int) -> int:
    return v * 2
"""


@pytest.fixture
def flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """`flow.py` beside the `helper.py` it imports; `sys.path` restored after."""
    monkeypatch.setattr(sys, "path", list(sys.path))
    (tmp_path / "helper.py").write_text(textwrap.dedent(HELPER))
    path = tmp_path / "flow.py"
    path.write_text(textwrap.dedent(FLOW))
    return path


def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    status = main(list(argv))
    out, err = capsys.readouterr()
    return status, out, err


def test_a_file_with_a_sibling_import_builds(
    flow: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Its folder goes on `sys.path`, so `from helper import ...` works (Q97)."""
    status, out, err = _run(capsys, "draw", str(flow), "--upstream", "b")
    assert (status, err) == (0, "")
    assert out.startswith("digraph {")
    assert '"a" -> "b"' in out


def test_config_selects_a_variant(
    flow: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without --config the variant is left out, as in Python; with it, built."""
    _, without, _ = _run(capsys, "export", str(flow))
    assert "c" not in [n["name"] for n in json.loads(without)["nodes"]]
    status, out, _ = _run(capsys, "export", str(flow), "--config", "mode=fast")
    document = json.loads(out)
    assert status == 0
    assert document["config"] == {"mode": "'fast'"}
    assert "c" in [n["name"] for n in document["nodes"]]


def test_a_dotted_name_and_a_namespace(
    flow: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dotted name imports from the current folder; `@ns` namespaces it."""
    monkeypatch.chdir(flow.parent)
    status, out, err = _run(capsys, "draw", "flow@ns", "--format", "mermaid")
    assert (status, err) == (0, "")
    assert '  n1["ns.a<br>int"]' in out


def test_values_are_json_or_strings(
    flow: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`x=3` is the int 3; a value that isn't JSON is a plain string."""
    status, out, _ = _run(
        capsys,
        "validate",
        str(flow),
        "--outputs",
        "b",
        "--input",
        "x=3",
        "--input",
        "y=many",
    )
    assert status == 1
    assert out == "y: expected int, got str\n"
    status, out, _ = _run(
        capsys, "validate", str(flow), "--outputs", "b", "--input", "x=3"
    )
    assert (status, out) == (0, "")


def test_a_context_file(
    flow: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """JSON or TOML with config, inputs, overrides and outputs; flags win."""
    toml = tmp_path / "run.toml"
    toml.write_text(
        'outputs = ["c"]\n[config]\nmode = "fast"\n'
        "[inputs]\nx = 1\n[overrides]\nb = 4\n"
    )
    status, out, _ = _run(capsys, "export", str(flow), "--context", str(toml))
    document = json.loads(out)
    assert status == 0
    assert document["request"] == {
        "outputs": ["c"],
        "inputs": ["x"],
        "overrides": ["b"],
    }
    data = tmp_path / "run.json"
    data.write_text(json.dumps({"config": {"mode": "slow"}, "outputs": ["b"]}))
    status, out, _ = _run(
        capsys,
        "export",
        str(flow),
        "--context",
        str(data),
        "--config",
        "mode=fast",
        "--outputs",
        "c",
        "--input",
        "x=1",
    )
    document = json.loads(out)
    assert document["config"] == {"mode": "'fast'"}
    assert document["request"]["outputs"] == ["c"]


@pytest.mark.parametrize(
    ("content", "suffix", "message"),
    [
        ('{"nope": 1}', ".json", "unknown keys nope"),
        ('{"outputs": "c"}', ".json", "outputs must be a list"),
        ('{"outputs": [1]}', ".json", "outputs must be a list of names"),
        ("[1]", ".json", "must hold a table"),
        ("{", ".json", "--context"),
        ("x =", ".toml", "--context"),
        ("{}", ".yaml", "must be a .json or .toml file"),
    ],
)
def test_a_bad_context_file_is_a_usage_error(  # noqa: PLR0913  # the case and fixtures
    flow: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    *,
    content: str,
    suffix: str,
    message: str,
) -> None:
    """Status 2, argparse's way, naming what is wrong."""
    path = tmp_path / f"context{suffix}"
    path.write_text(content)
    status, out, err = _run(capsys, "export", str(flow), "--context", str(path))
    assert (status, out) == (2, "")
    assert message in err


def test_each_draw_scope(flow: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Downstream, path with and without context, and a run."""
    _, down, _ = _run(capsys, "draw", str(flow), "--downstream", "a", "--no-legend")
    assert '"y" [' in down
    assert "cluster_legend" not in down
    _, path, _ = _run(capsys, "draw", str(flow), "--path", "a", "b", "--no-context")
    assert '"y" [' not in path
    _, run, _ = _run(
        capsys,
        "draw",
        str(flow),
        "--execution",
        "--outputs",
        "b",
        "--input",
        "x=1",
        "--orient",
        "TB",
    )
    assert "rankdir=TB" in run
    assert '"b" [label="b\\nint" fillcolor="#FFC857"]' in run
    status, _, err = _run(capsys, "draw", str(flow), "--execution", "--outputs", "b")
    assert status == 1
    assert err == "fnloom: ValueError: missing inputs: 'x' (needed by 'a')\n"
    status, out, _ = _run(
        capsys, "draw", str(flow), "--execution", "--outputs", "b", "--no-validate"
    )
    assert status == 0
    assert '"x" [' in out


def test_writing_to_a_file(
    flow: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`-o` writes the text there and prints nothing."""
    target = tmp_path / "graph.mmd"
    status, out, _ = _run(
        capsys, "draw", str(flow), "--format", "mermaid", "-o", str(target)
    )
    assert (status, out) == (0, "")
    assert target.read_text().startswith("flowchart LR\n")
    exported = tmp_path / "graph.json"
    _run(capsys, "export", str(flow), "-o", str(exported))
    assert json.loads(exported.read_text())["format"] == "fnloom.export/1"


def test_errors_go_to_stderr_with_status_1(
    flow: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """One line on stderr, nothing on stdout (Q98)."""
    status, out, err = _run(capsys, "export", str(flow), "--outputs", "nope")
    assert (status, out) == (1, "")
    assert err == "fnloom: ValueError: unknown outputs: 'nope'\n"
    status, out, err = _run(capsys, "draw", "no_such_module_anywhere")
    assert (status, out) == (1, "")
    assert err.startswith("fnloom: ModuleNotFoundError: ")


@pytest.mark.parametrize(
    "argv",
    [
        ["draw"],
        ["draw", "flow.py", "--upstream", "a", "--downstream", "b"],
        ["draw", "missing.py"],
        ["draw", "flow.py", "--format", "svg"],
        ["draw", "flow.py", "--config", "mode"],
        ["validate", "flow.py"],
        ["draw", "flow.py@"],
        ["draw", "flow.py", "--no-context"],
        ["draw", "flow.py", "--no-validate"],
        ["draw", "flow.py", "--execution"],
    ],
)
def test_usage_errors_have_status_2(
    flow: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
) -> None:
    """Status 2, argparse's, for a command line that can't be run."""
    monkeypatch.chdir(flow.parent)
    status, out, err = _run(capsys, *argv)
    assert (status, out) == (2, "")
    assert "error" in err


def test_diff(flow: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Status 0 when nothing changed, 1 when something did, 2 on a bad file."""
    old = tmp_path / "old.json"
    _run(capsys, "export", str(flow), "-o", str(old))
    status, out, _ = _run(capsys, "diff", str(old), str(old))
    assert status == 0
    assert json.loads(out) == {
        "added": [],
        "removed": [],
        "changed": {},
        "unversioned": [],
    }
    flow.write_text(flow.read_text() + "\ndef d(b: int) -> int:\n    return b\n")
    for name in ("flow", "helper"):
        sys.modules.pop(name, None)
    new = tmp_path / "new.json"
    _run(capsys, "export", str(flow), "-o", str(new))
    status, out, _ = _run(capsys, "diff", str(old), str(new))
    assert status == 1
    assert json.loads(out)["added"] == ["d"]
    bad = tmp_path / "bad.json"
    bad.write_text("{}")
    status, out, err = _run(capsys, "diff", str(old), str(bad))
    assert (status, out) == (2, "")
    assert "not an fnloom export" in err
    status, _, err = _run(capsys, "diff", str(old), str(tmp_path / "none.json"))
    assert status == 2  # noqa: PLR2004  # a file it can't read
    assert err.startswith("fnloom: FileNotFoundError")


def test_python_dash_m(flow: Path) -> None:
    """`python -m fnloom` is the same command."""
    done = subprocess.run(  # noqa: S603  # our own interpreter
        [sys.executable, "-m", "fnloom", "draw", str(flow), "--upstream", "a"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith("digraph {")
    failed = subprocess.run(  # noqa: S603  # our own interpreter
        [sys.executable, "-m", "fnloom", "export", str(flow), "--outputs", "nope"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert (failed.returncode, failed.stdout) == (1, "")
    assert failed.stderr == "fnloom: ValueError: unknown outputs: 'nope'\n"


def test_help_returns_its_status(capsys: pytest.CaptureFixture[str]) -> None:
    """`main` returns an `int` for `--help` too, never raising `SystemExit`."""
    assert main(["--help"]) == 0
    assert "draw" in capsys.readouterr().out


def test_an_image_by_the_suffix(
    flow: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With `-o` and no `--format`, the suffix names the format, in any case."""
    dot = tmp_path / "dot"
    dot.write_text(f"#!{sys.executable}\nimport sys\nsys.stdout.write(sys.argv[1])\n")
    dot.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    status, _, err = _run(capsys, "draw", str(flow), "-o", str(tmp_path / "g.svg"))
    assert (status, err) == (0, "")
    assert (tmp_path / "g.svg").read_text() == "-Tsvg"
    _run(capsys, "draw", str(flow), "--format", "PNG", "-o", str(tmp_path / "g.out"))
    assert (tmp_path / "g.out").read_text() == "-Tpng"
    _run(capsys, "draw", str(flow), "-o", str(tmp_path / "g.dot"))
    assert (tmp_path / "g.dot").read_text().startswith("digraph {")


def test_an_override_flag_and_spaced_outputs(
    flow: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--override` overrides; `--outputs "a, b"` names `a` and `b`."""
    status, out, _ = _run(
        capsys, "export", str(flow), "--outputs", "a, b", "--override", "a=3"
    )
    assert status == 0
    assert json.loads(out)["request"] == {
        "outputs": ["a", "b"],
        "inputs": [],
        "overrides": ["a"],
    }


def test_a_file_named_like_an_imported_module_is_refused(
    flow: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A `json.py`, or a second `flow.py`, is no silent swap for the first."""
    shadow = tmp_path / "json.py"
    shadow.write_text("def j(x: int) -> int:\n    return x\n")
    status, _, err = _run(capsys, "draw", str(shadow))
    assert status == 2  # noqa: PLR2004  # a usage error
    assert "a module named 'json' is already imported" in err
    other = tmp_path / "other"
    other.mkdir()
    (other / "flow.py").write_text("def z(x: int) -> int:\n    return x\n")
    status, _, err = _run(capsys, "draw", str(flow), str(other / "flow.py"))
    assert status == 2  # noqa: PLR2004  # a usage error
    assert "a module named 'flow' is already imported" in err


def test_an_at_sign_in_a_folder_is_part_of_the_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Only `@` and a dotted name after it is a namespace."""
    monkeypatch.setattr(sys, "path", list(sys.path))
    folder = tmp_path / "a@b"
    folder.mkdir()
    (folder / "at_flow.py").write_text("def a(x: int) -> int:\n    return x\n")
    status, out, err = _run(capsys, "draw", str(folder / "at_flow.py"))
    assert (status, err) == (0, "")
    assert '"a" [' in out
    status, out, _ = _run(capsys, "draw", f"{folder / 'at_flow.py'}@ns")
    assert '"ns.a" [' in out


@pytest.mark.parametrize("content", ["[]", '{"format": "fnloom.export/1"}'])
def test_diff_of_a_malformed_export_is_status_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], content: str
) -> None:
    """A file that isn't an export's shape is a bad file, as a missing one is."""
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"format": "fnloom.export/1", "nodes": []}))
    bad = tmp_path / "bad.json"
    bad.write_text(content)
    status, out, err = _run(capsys, "diff", str(good), str(bad))
    assert (status, out) == (2, "")
    assert err.startswith("fnloom: ValueError: new:")


def test_a_closed_pipe_is_quiet(flow: Path) -> None:
    """`fnloom draw ... | head -1` reports no error."""
    head = subprocess.Popen(  # noqa: S603  # our own interpreter
        [sys.executable, "-m", "fnloom", "draw", str(flow)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert head.stdout is not None
    head.stdout.close()
    _, err = head.communicate()
    assert err == b""
