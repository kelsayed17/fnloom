"""Shared test helpers: build throwaway modules, forget them afterwards."""

import sys
import textwrap
import types
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator


def make_module(name: str, source: str) -> types.ModuleType:
    """Executes `source` as module `name`, so its functions belong to that module."""
    module = types.ModuleType(name)
    sys.modules[name] = module
    exec(compile(textwrap.dedent(source), name, "exec"), module.__dict__)  # noqa: S102  # building a fixture module
    return module


@pytest.fixture(autouse=True)
def _forget_modules() -> Iterator[None]:
    """Removes the modules a test created, so tests cannot see each other's."""
    before = set(sys.modules)
    yield
    for name in set(sys.modules) - before:
        del sys.modules[name]
