"""Runs one request through Hamilton and through fnloom, as `Outcome`s."""

import importlib.util
import itertools
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from hamilton import driver as hamilton_driver

import fnloom

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from types import ModuleType

FIXTURES = Path(__file__).parent / "fixtures"

# A stand-in for the `graphviz` package, which the test environment doesn't have:
# enough of `Digraph` for Hamilton's drawing code to run and record what it drew.
FAKE_GRAPHVIZ = """
class Digraph:
    def __init__(self, name=None, **kwargs):
        self.name = name
        self.kwargs = kwargs
        self.nodes = []
        self.edges = []
        self.subgraphs = []

    def node(self, name, label=None, **attrs):
        self.nodes.append((name, label, attrs))

    def edge(self, tail, head, **attrs):
        self.edges.append((tail, head, attrs))

    def subgraph(self, graph=None, name=None):
        self.subgraphs.append(graph)

    def attr(self, **attrs):
        pass
"""
_load_counter = itertools.count()


@dataclass(frozen=True)
class Outcome:
    """What a run produced: the values, or the class name of the exception it raised."""

    values: dict[str, object] | None
    error: str | None


@dataclass(frozen=True)
class Nodes:
    """What a build produced: the sorted node names, or the exception class name."""

    names: list[str] | None
    error: str | None


type Direction = Literal["upstream", "downstream"]


def load_fixture(name: str) -> ModuleType:
    """Loads `fixtures/<name>.py` under a unique module name.

    The same module object goes to both runtimes. Registering it in `sys.modules` lets
    both of them resolve the functions' `__module__`.
    """
    unique = f"fnloom_fixture_{name}_{next(_load_counter)}"
    spec = importlib.util.spec_from_file_location(unique, FIXTURES / f"{name}.py")
    if spec is None or spec.loader is None:
        msg = f"cannot load fixture {name!r}"
        raise ImportError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules[unique] = module
    spec.loader.exec_module(module)
    return module


def run_hamilton(
    modules: Sequence[ModuleType],
    outputs: Sequence[str | Callable[..., object]],
    inputs: Mapping[str, object] | None = None,
    overrides: Mapping[str, object] | None = None,
) -> Outcome:
    """Builds and executes with Hamilton; building is guarded too, as it can raise."""
    try:
        built = hamilton_driver.Builder().with_modules(*modules).build()
        result = built.execute(
            list(outputs),
            inputs=dict(inputs or {}),
            overrides=dict(overrides or {}),
        )
    except Exception as error:  # noqa: BLE001  # the exception class is the thing being compared
        return Outcome(None, type(error).__name__)
    return Outcome(dict(result), None)


def run_fnloom(
    modules: Sequence[ModuleType],
    outputs: Sequence[str | Callable[..., object]],
    inputs: Mapping[str, object] | None = None,
    overrides: Mapping[str, object] | None = None,
) -> Outcome:
    """Builds and executes with fnloom, with the same guard as `run_hamilton`."""
    try:
        result = fnloom.build(*modules).execute(
            outputs, inputs=inputs, overrides=overrides
        )
    except Exception as error:  # noqa: BLE001  # the exception class is the thing being compared
        return Outcome(None, type(error).__name__)
    return Outcome(dict(result), None)


def nodes_hamilton(modules: Sequence[ModuleType]) -> Nodes:
    """Builds with Hamilton and lists every node, inputs included."""
    try:
        built = hamilton_driver.Builder().with_modules(*modules).build()
    except Exception as error:  # noqa: BLE001  # the exception class is the thing being compared
        return Nodes(None, type(error).__name__)
    return Nodes(sorted(v.name for v in built.list_available_variables()), None)


def nodes_fnloom(modules: Sequence[ModuleType]) -> Nodes:
    """Builds with fnloom and lists every node, guarded as `nodes_hamilton` is."""
    try:
        built = fnloom.build(*modules)
    except Exception as error:  # noqa: BLE001  # the exception class is the thing being compared
        return Nodes(None, type(error).__name__)
    return Nodes(built.node_names(), None)


def lineage_hamilton(
    modules: Sequence[ModuleType], direction: Direction, names: Sequence[str]
) -> list[str]:
    """Hamilton's `what_is_<direction>_of(*names)`, reduced to sorted node names."""
    built = hamilton_driver.Builder().with_modules(*modules).build()
    query = (
        built.what_is_upstream_of
        if direction == "upstream"
        else built.what_is_downstream_of
    )
    return sorted(node.name for node in query(*names))


def lineage_fnloom(
    modules: Sequence[ModuleType], direction: Direction, names: Sequence[str]
) -> list[str]:
    """Returns fnloom's `what_is_<direction>_of(*names)`."""
    built = fnloom.build(*modules)
    query = (
        built.what_is_upstream_of
        if direction == "upstream"
        else built.what_is_downstream_of
    )
    return query(*names)
