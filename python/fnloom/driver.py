"""The user-facing `Builder`, `Driver` and `PreparedPlan`."""

import inspect
import threading
from types import MappingProxyType
from typing import TYPE_CHECKING, Protocol

from fnloom import _core, _types
from fnloom._decorators import Source
from fnloom._scan import NodeDef, collect, core_inputs, resolve
from fnloom._view import NodeView, node_views

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from types import ModuleType

type _PlanKey = tuple[tuple[str, ...], frozenset[str], frozenset[str]]
# A name whose supplied value is checked, and the type it is checked against.
type _Check = tuple[str, object]

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


def _mismatches(checks: Sequence[_Check], values: Mapping[str, object]) -> list[str]:
    """A line for each checked value that does not fit its declared type."""
    return [
        f"{name}: expected {inspect.formatannotation(declared)}, "
        f"got {inspect.formatannotation(type(values[name]))}"
        for name, declared in checks
        if not _types.value_matches(declared, values[name])
    ]


def _edge_mismatches(views: Mapping[str, NodeView]) -> list[str]:
    """A line for each parameter that a function node feeds with a type it rejects."""
    found: list[str] = []
    for view in views.values():
        for p in view.params:
            if not isinstance(p.binding, Source):
                continue
            upstream = views[p.binding.name]
            if upstream.is_input or _types.type_matches(upstream.type, p.annotation):
                continue
            found.append(
                f"{view.name}.{p.name} requires "
                f"{inspect.formatannotation(p.annotation)} but {upstream.name} "
                f"returns {inspect.formatannotation(upstream.type)}"
            )
    return sorted(found)


class PreparedPlan:
    """A plan compiled for fixed outputs and fixed input and override names."""

    def __init__(
        self,
        plan: _core.Plan,
        outputs: Sequence[str],
        input_names: frozenset[str],
        override_names: frozenset[str],
        checks: tuple[Sequence[_Check], Sequence[_Check]] = ((), ()),
    ) -> None:
        """Wraps a compiled plan with the outputs and names it was compiled for.

        `checks` are the input and the override checks each run applies: the
        supplied names whose values are checked, each with its declared type.
        """
        self._plan = plan
        self._outputs = tuple(dict.fromkeys(outputs))
        self._input_names = input_names
        self._override_names = override_names
        self._input_checks, self._override_checks = checks

    def run(
        self,
        *,
        inputs: Mapping[str, object] | None = None,
        overrides: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """Runs the plan; raises `ValueError` if the input or override names differ.

        `ValueError` too, listing each one, when an input the plan reads or an
        override does not fit its node's type (unless the driver was built
        without input type checks). The result holds each requested output once,
        in the order first requested.
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
        mismatches = _mismatches(self._input_checks, given_inputs)
        mismatches += _mismatches(self._override_checks, given_overrides)
        if mismatches:
            raise ValueError("\n".join(sorted(mismatches)))
        values = self._plan.run(given_inputs, given_overrides)
        return {name: values[name] for name in self._outputs}


class Driver:
    """Executes a graph of nodes, caching one compiled plan per request shape.

    The cache keeps the `PLAN_CACHE_SIZE` most recently used plans. Make a driver with
    `Builder` (or its shorthand `build`); the constructor takes an already built
    native graph, the views of its nodes, and whether runs check the types of
    inputs and overrides.
    """

    def __init__(
        self,
        graph: _GraphLike,
        nodes: Mapping[str, NodeView] | None = None,
        *,
        check_inputs: bool = True,
    ) -> None:
        """Wraps a native graph; `Builder` is the way to make one from modules."""
        self._graph = graph
        self._check_inputs = check_inputs
        self._nodes: Mapping[str, NodeView] = (
            MappingProxyType[str, NodeView]({}) if nodes is None else nodes
        )
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
        checks = self._checks(plan, override_names) if self._check_inputs else ((), ())
        return PreparedPlan(plan, outputs, input_names, override_names, checks)

    def _checks(
        self, plan: _core.Plan, override_names: frozenset[str]
    ) -> tuple[tuple[_Check, ...], tuple[_Check, ...]]:
        """The input checks the plan's runs need, and the override checks (Q22).

        Only an input a step reads is checked, as Hamilton; every override is.
        """
        read = sorted(node for node, action in plan.steps() if action == "use_input")
        return self._declared(read), self._declared(sorted(override_names))

    def _declared(self, names: list[str]) -> tuple[_Check, ...]:
        """Each of `names` with its node's type; a name with no view goes unchecked."""
        return tuple((n, self._nodes[n].type) for n in names if n in self._nodes)

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

    @property
    def nodes(self) -> Mapping[str, NodeView]:
        """Every node, inputs included, by name: read-only, iterating in name order."""
        return self._nodes

    def what_is_upstream_of(self, *names: str) -> list[str]:
        """Returns the named nodes and every node they depend on, sorted."""
        return sorted({*names, *self._graph.upstream_of(list(names))})

    def what_is_downstream_of(self, *names: str) -> list[str]:
        """Returns the named nodes and every node depending on them, sorted."""
        return sorted({*names, *self._graph.downstream_of(list(names))})


def _graph_of(nodes: list[NodeDef]) -> _core.Graph:
    """Builds the native graph for scanned nodes: their core inputs and payloads."""
    return _core.Graph(
        [(n.name, core_inputs(n.params), dict(n.tags), n.payload) for n in nodes]
    )


class Builder:
    """Accumulates modules, then builds a `Driver`; methods return new builders."""

    def __init__(
        self, modules: tuple[ModuleType, ...] = (), *, check_inputs: bool = True
    ) -> None:
        """Starts with the given modules (none by default), checking input types."""
        self._modules = modules
        self._check_inputs = check_inputs

    def with_modules(self, *modules: ModuleType) -> Builder:
        """Returns a new builder with `modules` appended to this one's."""
        return Builder((*self._modules, *modules), check_inputs=self._check_inputs)

    def without_input_type_checks(self) -> Builder:
        """Returns a new builder whose driver skips input and override type checks.

        Edge checks, between a node and the parameters it feeds, stay on (Q10).
        """
        return Builder(self._modules, check_inputs=False)

    def build(self) -> Driver:
        """Scans the modules and returns a `Driver`; `ValueError` on a bad node.

        Annotations are evaluated here: a name that does not resolve raises its
        `NameError`, with a note naming the node. Every edge is type checked: a
        node whose return type a parameter it feeds does not accept fails the
        build, with one `ValueError` listing every such parameter.
        """
        nodes = resolve(collect(self._modules))
        views = node_views(nodes)
        graph = _graph_of(nodes)
        if mismatches := _edge_mismatches(views):
            raise ValueError("\n".join(mismatches))
        return Driver(graph, views, check_inputs=self._check_inputs)


def build(*modules: ModuleType) -> Driver:
    """Builds a `Driver` from `modules`; shorthand for the `Builder` chain."""
    return Builder().with_modules(*modules).build()
