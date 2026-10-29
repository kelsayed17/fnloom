"""Shared test helpers: build throwaway modules, forget them afterwards."""

import sys
import textwrap
import types
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


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


def fake_dot(folder: Path, script: str) -> None:
    """A `dot` executable in `folder` that runs the Python `script`.

    A shebang script elsewhere; on Windows a `dot.bat` calling this interpreter,
    which `shutil.which` finds through `PATHEXT`.
    """
    body = folder / "dot_impl.py"
    body.write_text(textwrap.dedent(script))
    if sys.platform == "win32":
        (folder / "dot.bat").write_text(f'@"{sys.executable}" "{body}" %*\n')
        return
    path = folder / "dot"
    path.write_text(f"#!{sys.executable}\n{textwrap.dedent(script)}")
    path.chmod(0o755)
