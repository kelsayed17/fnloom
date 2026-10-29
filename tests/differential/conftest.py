"""Differential-test setup: Hamilton's environment and the evidence markers."""

import os
import re
import sys
import types
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

# Set before anything imports Hamilton, so its import loads no heavy extensions.
os.environ["HAMILTON_AUTOLOAD_EXTENSIONS"] = "0"

if TYPE_CHECKING:
    from collections.abc import Sequence

from harness import FAKE_GRAPHVIZ  # after the environment is set

REGISTER = Path(__file__).parents[2] / "docs" / "quirk-register.md"
# Each row's ID and disposition, its first and third columns.
_ROW = re.compile(r"^\| (Q\d+) \| .*? \| ([^|]*?) \|", re.MULTILINE)


def pytest_configure(config: pytest.Config) -> None:
    """Registers the `deliberate` and `keeps` markers."""
    config.addinivalue_line(
        "markers",
        "deliberate(id): fnloom deliberately differs from Hamilton; "
        "`id` must be a row of docs/quirk-register.md",
    )
    config.addinivalue_line(
        "markers",
        "keeps(id): fnloom deliberately does what Hamilton does; "
        "`id` must be a row of docs/quirk-register.md",
    )


def pytest_collection_modifyitems(items: Sequence[pytest.Item]) -> None:
    """Fails collection when a `deliberate` or `keeps` marker names no register row."""
    dispositions = dict(_ROW.findall(REGISTER.read_text(encoding="utf-8")))
    for item in items:
        for name in ("deliberate", "keeps"):
            for marker in item.iter_markers(name):
                quirk = marker.args[0] if marker.args else None
                if quirk not in dispositions:
                    msg = (
                        f"{item.nodeid}: {name}({quirk!r}) is no row of {REGISTER.name}"
                    )
                    raise pytest.UsageError(msg)
                if name == "keeps" and "KEEP" not in dispositions[quirk]:
                    msg = f"{item.nodeid}: keeps({quirk!r}) marks a row not kept"
                    raise pytest.UsageError(msg)


@pytest.fixture
def fake_graphviz(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lets Hamilton's drawing methods run without the graphviz package.

    `FAKE_GRAPHVIZ` stands in for it, recording the nodes and edges drawn.
    """
    module = types.ModuleType("graphviz")
    exec(FAKE_GRAPHVIZ, module.__dict__)  # noqa: S102  # our own stand-in
    monkeypatch.setitem(sys.modules, "graphviz", module)
