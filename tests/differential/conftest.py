"""Differential-test setup: Hamilton's environment and the `deliberate` marker."""

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
_ROW_ID = re.compile(r"^\| (Q\d+) \|", re.MULTILINE)


def pytest_configure(config: pytest.Config) -> None:
    """Registers the `deliberate` marker."""
    config.addinivalue_line(
        "markers",
        "deliberate(id): fnloom deliberately differs from Hamilton; "
        "`id` must be a row of docs/quirk-register.md",
    )


def pytest_collection_modifyitems(items: Sequence[pytest.Item]) -> None:
    """Fails collection when a `deliberate` marker names no row of the register."""
    known = set(_ROW_ID.findall(REGISTER.read_text(encoding="utf-8")))
    for item in items:
        for marker in item.iter_markers("deliberate"):
            quirk = marker.args[0] if marker.args else None
            if quirk not in known:
                msg = (
                    f"{item.nodeid}: deliberate({quirk!r}) is no row of {REGISTER.name}"
                )
                raise pytest.UsageError(msg)


@pytest.fixture
def fake_graphviz(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lets Hamilton's drawing methods run without the graphviz package.

    `FAKE_GRAPHVIZ` stands in for it, recording the nodes and edges drawn.
    """
    module = types.ModuleType("graphviz")
    exec(FAKE_GRAPHVIZ, module.__dict__)  # noqa: S102  # our own stand-in
    monkeypatch.setitem(sys.modules, "graphviz", module)
