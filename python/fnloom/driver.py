"""The user-facing `Builder`, `Driver` and `PreparedPlan`."""

import threading
from typing import TYPE_CHECKING, Protocol

from fnloom import _core
from fnloom._scan import NodeDef, collect

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from types import ModuleType

type _PlanKey = tuple[tuple[str, ...], frozenset[str], frozenset[str]]

# How many compiled plans a driver keeps; the least recently used one goes first.
PLAN_CACHE_SIZE = 128


class _GraphLike(Protocol):
    """The part of `_core.Graph` the driver uses."""

    def node_names(self) -> list[str]:
        """Returns every node name, sorted."""
        ...

    def upstream_of(self, names: list[str]) -> list[str]:
        """Returns the nodes the named nodes depend on."""
        ...

    def downstream_of(self, names: list[str]) -> list[str]:
        """Returns the nodes that depend on the named nodes."""
        ...

    def prepare(
        self, outputs: list[str], inputs: list[str], overrides: list[str]
    ) -> _core.Plan:
        """Compiles a plan for the outputs, given the input and override names."""
        ...


def _check_final_vars(final_vars: Sequence[str]) -> tuple[str, ...]:
    if isinstance(final_vars, str):
        msg = "final_vars must be a sequence of names, not a single str"
        raise TypeError(msg)
    # Copied before checking, so a one-shot iterator is read once and not exhausted.
    names = tuple(final_vars)
    for item in names:
        if not isinstance(item, str):
            msg = f"final_vars items must be str, got {type(item).__name__}"
            raise TypeError(msg)
    return names


def _names(values: Mapping[str, object] | None) -> frozenset[str]:
    return frozenset[str]() if values is None else frozenset[str](values)


def _difference(kind: str, prepared: frozenset[str], given: frozenset[str]) -> str:
    return f"{kind} {sorted(given)} differ from the prepared {sorted(prepared)}"


class PreparedPlan:
    """A plan compiled for fixed outputs and fixed input and override names."""

    def __init__(
        self,
        plan: _core.Plan,
        outputs: Sequence[str],
        input_names: frozenset[str],
        override_names: frozenset[str],
    ) -> None:
        """Wraps a compiled plan with the outputs and names it was compiled for."""
        self._plan = plan
        self._outputs = tuple(dict.fromkeys(outputs))
        self._input_names = input_names
        self._override_names = override_names

    def run(
        self,
        *,
        inputs: Mapping[str, object] | None = None,
        overrides: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """Runs the plan; raises `ValueError` if the input or override names differ.

        The result holds each requested output once, in the order first requested.
        """
        given_inputs = dict(inputs or {})
        given_overrides = dict(overrides or {})
        problems = [
            _difference(kind, prepared, frozenset(given))
            for kind, prepared, given in (
                ("inputs", self._input_names, given_inputs),
                ("overrides", self._override_names, given_overrides),
            )
            if prepared != frozenset(given)
        ]
        if problems:
            raise ValueError("; ".join(problems))
        values = self._plan.run(given_inputs, given_overrides)
        return {name: values[name] for name in self._outputs}


class Driver:
    """Executes a graph of nodes, caching one compiled plan per request shape.

    The cache keeps the `PLAN_CACHE_SIZE` most recently used plans. Make a driver with
    `Builder` (or its shorthand `build`); the constructor takes an already built
    native graph.
    """

    def __init__(self, graph: _GraphLike) -> None:
        """Wraps a native graph; `Builder` is the way to make one from modules."""
        self._graph = graph
        # Insertion order is recency order: a hit moves its plan to the end.
        self._plans: dict[_PlanKey, PreparedPlan] = {}
        self._plans_lock = threading.Lock()

    def prepare(
        self,
        final_vars: Sequence[str],
        *,
        inputs: Mapping[str, object] | None = None,
        overrides: Mapping[str, object] | None = None,
    ) -> PreparedPlan:
        """Returns the plan for this request shape, compiling it on first use."""
        key: _PlanKey = (
            _check_final_vars(final_vars),
            _names(inputs),
            _names(overrides),
        )
        with self._plans_lock:
            prepared = self._plans.pop(key, None)
            if prepared is None:
                prepared = self._compile(key)
            self._plans[key] = prepared
            if len(self._plans) > PLAN_CACHE_SIZE:
                del self._plans[next(iter(self._plans))]
        return prepared

    def _compile(self, key: _PlanKey) -> PreparedPlan:
        outputs, input_names, override_names = key
        plan = self._graph.prepare(
            list(outputs), sorted(input_names), sorted(override_names)
        )
        return PreparedPlan(plan, outputs, input_names, override_names)

    def execute(
        self,
        final_vars: Sequence[str],
        *,
        inputs: Mapping[str, object] | None = None,
        overrides: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """Computes `final_vars`, reusing the cached plan for the same request shape.

        The result holds each requested output once, in the order first requested.
        """
        plan = self.prepare(final_vars, inputs=inputs, overrides=overrides)
        return plan.run(inputs=inputs, overrides=overrides)

    def node_names(self) -> list[str]:
        """Returns every node name, sorted."""
        return self._graph.node_names()

    def what_is_upstream_of(self, *names: str) -> list[str]:
        """Returns the named nodes and every node they depend on, sorted."""
        return sorted({*names, *self._graph.upstream_of(list(names))})

    def what_is_downstream_of(self, *names: str) -> list[str]:
        """Returns the named nodes and every node depending on them, sorted."""
        return sorted({*names, *self._graph.downstream_of(list(names))})


def _graph_of(nodes: list[NodeDef]) -> _core.Graph:
    """Builds the native graph for scanned nodes, passing functions unwrapped."""
    return _core.Graph(
        [
            (n.name, [(p.name, p.required) for p in n.params], dict(n.tags), n.fn)
            for n in nodes
        ]
    )


class Builder:
    """Accumulates modules, then builds a `Driver`; methods return new builders."""

    def __init__(self, modules: tuple[ModuleType, ...] = ()) -> None:
        """Starts with the given modules (none by default)."""
        self._modules = modules

    def with_modules(self, *modules: ModuleType) -> Builder:
        """Returns a new builder with `modules` appended to this one's."""
        return Builder((*self._modules, *modules))

    def build(self) -> Driver:
        """Scans the modules and returns a `Driver`; `ValueError` on a bad node."""
        return Driver(_graph_of(collect(self._modules)))


def build(*modules: ModuleType) -> Driver:
    """Builds a `Driver` from `modules`; shorthand for the `Builder` chain."""
    return Builder().with_modules(*modules).build()
