"""Shared test helpers: build throwaway modules, forget them afterwards."""

import asyncio
import sys
import textwrap
import types
from typing import TYPE_CHECKING, Any, Literal

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence
    from pathlib import Path

    import fnloom

type DriverKind = Literal["sync", "async"]

# The drivers a test parametrized over `driver_kind` runs on.
DRIVER_KINDS: tuple[DriverKind, ...] = ("sync", "async")


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


def built(
    driver_kind: DriverKind, builder: fnloom.Builder[Any]
) -> fnloom.Driver[Any] | fnloom.AsyncDriver[Any]:
    """`builder`'s driver of `driver_kind`: `build()` or `build_async()`."""
    return builder.build() if driver_kind == "sync" else builder.build_async()


def run(
    driver: fnloom.Driver[Any] | fnloom.AsyncDriver[Any],
    outputs: Sequence[str],
    *,
    inputs: Mapping[str, object] | None = None,
    overrides: Mapping[str, object] | None = None,
) -> Any:  # noqa: ANN401  # what the driver's result builder makes
    """`driver.execute(...)`, run to completion on a new loop for an `AsyncDriver`."""
    result = driver.execute(outputs, inputs=inputs, overrides=overrides)
    if asyncio.iscoroutine(result):
        return asyncio.run(result)
    return result
