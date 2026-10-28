"""Fixtures shared by the benchmarks: generated graphs and one event loop.

A graph is written to a file and imported, as `benches/cache.py` does, because the
cache versions a node by its source and `exec` leaves it none. The modules are
dropped from `sys.modules` when the session ends.

pytest-codspeed measures synchronous callables, so each async benchmark runs a batch
of operations inside one coroutine: the event loop's own entry cost is paid once per
batch, not once per operation, and what is measured is the operation.
"""

import asyncio
import importlib.util
import sys
from collections.abc import Awaitable, Callable, Iterator
from typing import TYPE_CHECKING, Protocol

import pytest
from benches.generate import Shape, module_source

if TYPE_CHECKING:
    from types import ModuleType

    from pytest_codspeed import BenchmarkFixture

#: Operations per measured batch for the async benchmarks.
BATCH = 200

type Operation = Callable[[], Awaitable[object]]


class LoadSource(Protocol):
    """Imports a module from source text, under a name unique to the session."""

    def __call__(self, name: str, source: str) -> ModuleType:
        """Return the module called `name`, importing it on first use."""
        ...


class LoadGraph(Protocol):
    """Imports a generated graph."""

    def __call__(
        self, shape: Shape, n: int, *, asynchronous: bool = False
    ) -> ModuleType:
        """Return the graph of `n` nodes in `shape`.

        Every node is an `async def` when `asynchronous`.
        """
        ...


@pytest.fixture(scope="session")
def load_source(tmp_path_factory: pytest.TempPathFactory) -> Iterator[LoadSource]:
    """Provide a function that imports source text from a file.

    The modules are dropped from `sys.modules` at the end of the session.
    """
    directory = tmp_path_factory.mktemp("benchmark_modules")
    loaded: dict[str, ModuleType] = {}

    def load(name: str, source: str) -> ModuleType:
        if name not in loaded:
            path = directory / f"{name}.py"
            path.write_text(source, encoding="utf-8")
            spec = importlib.util.spec_from_file_location(name, path)
            if spec is None or spec.loader is None:
                msg = f"cannot import {path}"
                raise ImportError(msg)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
            loaded[name] = module
        return loaded[name]

    yield load
    for name in loaded:
        sys.modules.pop(name, None)


@pytest.fixture(scope="session")
def load_graph(load_source: LoadSource) -> LoadGraph:
    """Provide a function that imports a generated graph once per session."""

    def load(shape: Shape, n: int, *, asynchronous: bool = False) -> ModuleType:
        source = module_source(shape, n)
        if asynchronous:
            source = source.replace("def ", "async def ")
        return load_source(
            f"bench_{shape}_{n}_{'async' if asynchronous else 'sync'}", source
        )

    return load


@pytest.fixture(scope="session")
def runner() -> Iterator[asyncio.Runner]:
    """Provide one event loop for the whole benchmark session."""
    with asyncio.Runner() as session_runner:
        yield session_runner


@pytest.fixture
def run_batch(
    benchmark: BenchmarkFixture, runner: asyncio.Runner
) -> Callable[[Operation], None]:
    """Return a function that benchmarks ``BATCH`` awaits of an operation."""

    def measure(operation: Operation) -> None:
        async def batch() -> None:
            for _ in range(BATCH):
                await operation()

        runner.run(batch())  # warm up: first-call caches
        benchmark(lambda: runner.run(batch()))

    return measure
