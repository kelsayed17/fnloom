"""The user-facing `Builder`, `Driver` and `PreparedPlan`."""

import inspect
import threading
from typing import TYPE_CHECKING, Any, Protocol

from fnloom import _core, _types
from fnloom._caching import Cache, CacheOutcome, CacheRun
from fnloom._decorators import Source
from fnloom._scan import NodeDef, collect, core_inputs, resolve
from fnloom._view import NodeView, NodeViews, node_views

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from collections.abc import Set as AbstractSet
    from types import ModuleType

    from fnloom._stores import CacheStore

type _PlanKey = tuple[tuple[str, ...], frozenset[str], frozenset[str]]
# A request's final_vars (a list or tuple, copied), its input and override names,
# and its plan.
type _LastRequest = tuple[Sequence[str], frozenset[str], frozenset[str], PreparedPlan]
# A name whose supplied value is checked, and the type it is checked against.
# A checked name, its declared type, and the class `isinstance` checks it against
# when the type is a plain class (`None` for any other type).
type _Check = tuple[str, object, type | None]

# How many compiled plans a driver keeps; the least recently used one goes first.
PLAN_CACHE_SIZE = 128


class _Runnable(Protocol):
    """What a prepared plan runs: a native plan, or a cached run over one."""

    def run(
        self, inputs: dict[str, Any], overrides: dict[str, Any], /
    ) -> dict[str, object]:
        """Runs with checked values; exactly the outputs, keyed by name, sorted."""
        ...


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


# No names, made once: calling `frozenset[str]` costs more than the check it serves.
_NO_NAMES = frozenset[str]()


# The one type a repeat's names may have, checked in C rather than in a loop.
_STR_ONLY = frozenset({str})

# No values, passed for a missing mapping; read, never written.
_NO_VALUES: dict[str, object] = {}


def _names(values: Mapping[str, object] | None) -> frozenset[str]:
    return _NO_NAMES if values is None else frozenset(values)


def _keys(values: Mapping[str, object] | None) -> AbstractSet[str]:
    """The names of `values`, compared with a set without building one."""
    return _NO_NAMES if values is None else values.keys()


def _as_dict(values: Mapping[str, object] | None) -> dict[str, object]:
    """`values` as a dict: itself when it is one, as the native run copies it."""
    # Any falsy value is no values, as `inputs or {}` always read it.
    if values is None or not values:
        return _NO_VALUES
    # An exact dict only; `isinstance` narrows the type for the type checker.
    if isinstance(values, dict) and type(values) is dict:
        return values
    # A copy for anything else, a dict subclass included, so the checks and the
    # native run read the same plain values.
    return dict(values)


def _difference(kind: str, prepared: frozenset[str], given: frozenset[str]) -> str:
    return f"{kind} {sorted(given)} differ from the prepared {sorted(prepared)}"


def _mismatches(checks: Sequence[_Check], values: Mapping[str, object]) -> list[str]:
    """A line for each checked value that does not fit its declared type."""
    found: list[str] = []
    for name, declared, cls in checks:
        value = values[name]
        if value is None:
            continue
        if cls is not None:
            if isinstance(value, cls):
                continue
        elif _types.value_matches(declared, value):
            continue
        found.append(
            f"{name}: expected {inspect.formatannotation(declared)}, "
            f"got {inspect.formatannotation(type(value))}"
        )
    return found


def _edge_mismatches(functions: Mapping[str, NodeView]) -> list[str]:
    """A line for each parameter that a function node feeds with a type it rejects.

    `functions` are the function nodes' views; a parameter reading anything else
    reads an input, and has no edge to check.
    """
    found: list[str] = []
    for view in functions.values():
        for p in view.params:
            if not isinstance(p.binding, Source):
                continue
            upstream = functions.get(p.binding.name)
            if upstream is None or _types.type_matches(upstream.type, p.annotation):
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
        plan: _Runnable,
        outputs: Sequence[str],
        input_names: frozenset[str],
        override_names: frozenset[str],
        checks: tuple[Sequence[_Check], Sequence[_Check]] = ((), ()),
    ) -> None:
        """Wraps a compiled plan with the outputs and names it was compiled for.

        `plan` is the native plan, or the `CacheRun` over it when the driver has
        a cache: either is called with values already checked. `checks` are the
        input and the override checks each run applies: the supplied names whose
        values are checked, each with its declared type.
        """
        self._plan = plan
        self._outputs = tuple(dict.fromkeys(outputs))
        # The native run returns its outputs sorted by name: already in the order
        # asked for when that order is sorted.
        self._sorted = list(self._outputs) == sorted(self._outputs)
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
        given_inputs, given_overrides = self._checked(inputs, overrides)
        values = self._plan.run(given_inputs, given_overrides)
        if self._sorted:
            return values
        return {name: values[name] for name in self._outputs}

    def _explain(
        self,
        inputs: Mapping[str, object] | None,
        overrides: Mapping[str, object] | None,
    ) -> dict[str, CacheOutcome]:
        """`Driver.explain_cache` over this plan, which runs through a cache."""
        given_inputs, given_overrides = self._checked(inputs, overrides)
        if not isinstance(self._plan, CacheRun):
            msg = "this plan does not run through a cache"
            raise TypeError(msg)
        return self._plan.explain(given_inputs, given_overrides)

    def _checked(
        self,
        inputs: Mapping[str, object] | None,
        overrides: Mapping[str, object] | None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """The inputs and overrides as dicts, once a run's checks pass; see `run`."""
        # Inline for a dict, the common case: this runs once per run.
        given_inputs = inputs if type(inputs) is dict else _as_dict(inputs)
        given_overrides = overrides if type(overrides) is dict else _as_dict(overrides)
        # A keys view compares with a set without building one.
        if (
            given_inputs.keys() != self._input_names
            or given_overrides.keys() != self._override_names
        ):
            problems = [
                _difference(kind, prepared, frozenset(given))
                for kind, prepared, given in (
                    ("inputs", self._input_names, given_inputs),
                    ("overrides", self._override_names, given_overrides),
                )
                if prepared != frozenset(given)
            ]
            raise ValueError("; ".join(problems))
        if self._input_checks or self._override_checks:
            mismatches = _mismatches(self._input_checks, given_inputs)
            mismatches += _mismatches(self._override_checks, given_overrides)
            if mismatches:
                raise ValueError("\n".join(sorted(mismatches)))
        return given_inputs, given_overrides


class Driver:
    """Executes a graph of nodes, caching one compiled plan per request shape.

    The cache keeps the `PLAN_CACHE_SIZE` most recently used plans. Make a driver with
    `Builder` (or its shorthand `build`); the constructor takes an already built
    native graph, the views of its nodes, whether runs check the types of inputs
    and overrides, and the cache, if runs use one.
    """

    def __init__(
        self,
        graph: _GraphLike,
        nodes: Mapping[str, NodeView] | None = None,
        *,
        check_inputs: bool = True,
        cache: Cache | None = None,
    ) -> None:
        """Wraps a native graph; `Builder` is the way to make one from modules."""
        self._graph = graph
        self._check_inputs = check_inputs
        self._cache = cache
        self._nodes = (
            nodes if isinstance(nodes, NodeViews) else NodeViews(nodes or {}, {})
        )
        # Insertion order is recency order: a hit moves its plan to the end.
        self._plans: dict[_PlanKey, PreparedPlan] = {}
        self._plans_lock = threading.Lock()
        # The last request and its plan, which is the most recently used one, so a
        # repeat of it, the common case in a loop, skips the key and the lock.
        self._last: _LastRequest | None = None

    def prepare(
        self,
        final_vars: Sequence[str],
        *,
        inputs: Mapping[str, object] | None = None,
        overrides: Mapping[str, object] | None = None,
    ) -> PreparedPlan:
        """Returns the plan for this request shape, compiling it on first use."""
        last = self._last
        if (
            last is not None
            and type(final_vars) is type(last[0])
            and final_vars == last[0]
            # Names only: an item of another type could compare equal to one.
            and _STR_ONLY.issuperset(map(type, final_vars))
            and _keys(inputs) == last[1]
            and _keys(overrides) == last[2]
        ):
            return last[3]
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
            # A list is copied, so a list changed after this call is not a repeat.
            if type(final_vars) is list:
                self._last = (list(final_vars), key[1], key[2], prepared)
            elif type(final_vars) is tuple:
                self._last = (final_vars, key[1], key[2], prepared)
        return prepared

    def _compile(self, key: _PlanKey) -> PreparedPlan:
        outputs, input_names, override_names = key
        plan = self._graph.prepare(
            list(outputs), sorted(input_names), sorted(override_names)
        )
        checks = self._checks(plan, override_names) if self._check_inputs else ((), ())
        if self._cache is None:
            return PreparedPlan(plan, outputs, input_names, override_names, checks)
        cached = CacheRun(
            self._cache, self._graph, plan, outputs, input_names, override_names
        )
        return PreparedPlan(cached, outputs, input_names, override_names, checks)

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
        # By type, not view: the views of many inputs are never made at all.
        return tuple(
            (n, declared, _types.plain_class(declared))
            for n in names
            if n in self._nodes
            for declared in (self._nodes.type_of(n),)
        )

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

    def explain_cache(
        self,
        final_vars: Sequence[str],
        *,
        inputs: Mapping[str, object] | None = None,
        overrides: Mapping[str, object] | None = None,
    ) -> dict[str, CacheOutcome]:
        """What a cached run of this request would do with each node; runs nothing.

        Returns every computed node of the plan, overridden ones left out, by name
        in name order, each with its `CacheOutcome`. The request is checked as
        `execute` checks it. A store error is a miss, and warns `CacheWarning`.
        Raises `ValueError` for a driver built without `Builder.with_cache`.

        The store is consulted for every node with a key, including nodes upstream
        of a hit, which a run would neither load nor run: with a `DiskCache`, that
        reads each of their entries.
        """
        if self._cache is None:
            msg = "explain_cache needs a driver built with Builder.with_cache"
            raise ValueError(msg)
        plan = self.prepare(final_vars, inputs=inputs, overrides=overrides)
        return plan._explain(inputs, overrides)  # noqa: SLF001  # the driver's own plan

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
        [(n.name, core_inputs(n.params), dict(n.tags), n.payload) for n in nodes],
        {n.name: dict(n.renames) for n in nodes if n.renames},
    )


class Builder:
    """Accumulates modules, then builds a `Driver`; methods return new builders."""

    def __init__(
        self,
        modules: tuple[ModuleType, ...] = (),
        *,
        check_inputs: bool = True,
        store: CacheStore | None = None,
    ) -> None:
        """Starts with the given modules (none by default), checking input types.

        `store`, when given, caches the results of the driver's runs in it.
        """
        self._modules = modules
        self._check_inputs = check_inputs
        self._store = store

    def with_modules(self, *modules: ModuleType) -> Builder:
        """Returns a new builder with `modules` appended to this one's."""
        return Builder(
            (*self._modules, *modules),
            check_inputs=self._check_inputs,
            store=self._store,
        )

    def without_input_type_checks(self) -> Builder:
        """Returns a new builder whose driver skips input and override type checks.

        Edge checks, between a node and the parameters it feeds, stay on (Q10).
        """
        return Builder(self._modules, check_inputs=False, store=self._store)

    def with_cache(self, store: CacheStore) -> Builder:
        """Returns a new builder whose driver caches every node's result in `store`.

        A node's `@cache` policy changes how it is cached. Each run computes every
        node's key from its code and the versions of what it reads, loads only the
        stored results nearest the outputs, and runs and stores the rest. Code
        versions are read at build, so rebuild after editing code. A stored value is
        returned as it is, not copied: a cached node must not mutate its arguments.
        """
        return Builder(self._modules, check_inputs=self._check_inputs, store=store)

    def build(self) -> Driver:
        """Scans the modules and returns a `Driver`; `ValueError` on a bad node.

        Annotations are evaluated here: a name that does not resolve raises its
        `NameError` (`AttributeError` for a missing attribute), with a note naming
        the node. Every edge is type checked: a node whose return type a parameter
        it feeds does not accept fails the build, with one `ValueError` listing
        every such parameter.
        """
        nodes = resolve(collect(self._modules))
        views = node_views(nodes)
        graph = _graph_of(nodes)
        if mismatches := _edge_mismatches(views.functions):
            raise ValueError("\n".join(mismatches))
        cache = None if self._store is None else Cache.of(self._store, nodes)
        return Driver(graph, views, check_inputs=self._check_inputs, cache=cache)


def build(*modules: ModuleType) -> Driver:
    """Builds a `Driver` from `modules`; shorthand for the `Builder` chain."""
    return Builder().with_modules(*modules).build()
