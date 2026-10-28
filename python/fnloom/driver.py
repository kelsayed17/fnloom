"""The user-facing `Builder`, `Driver` and `PreparedPlan`."""

import importlib
import inspect
import sys
import threading

# Imported for real, not under TYPE_CHECKING: the public signatures annotate with
# them, and `inspect.signature` resolves those annotations at run time.
from collections.abc import Callable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from types import ModuleType
from typing import Any, Protocol, override

from fnloom import _core, _types
from fnloom._caching import Cache, CacheOutcome, CacheRun
from fnloom._decorators import Source
from fnloom._scan import NodeDef, collect, core_inputs, resolve
from fnloom._stores import CacheStore
from fnloom._view import NodeView, NodeViews, node_views

type _PlanKey = tuple[tuple[str, ...], frozenset[str], frozenset[str]]
# A request's final_vars (a list or tuple, copied), its input and override names,
# and its plan.
type _LastRequest = tuple[Sequence[str], frozenset[str], frozenset[str], PreparedPlan]
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
    """`values`, which is not an exact dict, as a plain dict to check and run.

    The caller passes an exact dict through itself, so this only copies: a dict
    subclass or another mapping, so the checks and the native run read the same
    plain values.
    """
    # Any falsy value is no values, as `inputs or {}` always read it.
    if values is None or not values:
        return _NO_VALUES
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
    """A plan compiled for fixed outputs and fixed input and override names.

    Pickled or copied, a plan is its driver and its request: loading it rebuilds
    the driver (see `Driver`) and prepares the same request again. Pickled together
    with its driver, or with other plans of one driver, they share one rebuilt
    driver.
    """

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
        values are checked, each with its declared type and, for a plain class,
        the class `isinstance` checks it against.
        """
        self._plan = plan
        self._outputs = tuple(dict.fromkeys(outputs))
        # The native run returns its outputs sorted by name: already in the order
        # asked for when that order is sorted.
        self._sorted = list(self._outputs) == sorted(self._outputs)
        self._input_names = input_names
        self._override_names = override_names
        self._input_checks, self._override_checks = checks
        # The driver that prepared this plan, which pickling rebuilds it through.
        self._driver: Driver | None = None

    @override
    def __reduce__(
        self,
    ) -> tuple[Callable[..., PreparedPlan], tuple[Driver, _PlanKey]]:
        """The driver and the request, prepared again on load; see the class."""
        if self._driver is None:
            msg = "cannot pickle a PreparedPlan made without a Driver"
            raise TypeError(msg)
        return _prepare_again, (
            self._driver,
            (self._outputs, self._input_names, self._override_names),
        )

    def __copy__(self) -> PreparedPlan:
        """The plan itself: nothing about a plan changes once it is compiled."""
        return self

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

    A driver made by a `Builder` pickles, and copies with `copy.deepcopy`, as that
    builder: loading it imports its modules by name and builds again, with the
    same type-check setting and store, so it can go to a process pool. Its compiled
    plans are not carried over. A driver made from a native graph directly does not
    pickle.
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
        # The builder that built this driver, which pickling rebuilds it from.
        self._builder: Builder | None = None

    @override
    def __reduce__(self) -> tuple[Callable[[Builder], Driver], tuple[Builder]]:
        """The builder, built again on load; see the class."""
        if self._builder is None:
            msg = (
                "cannot pickle a Driver made from a native graph; "
                "make it with Builder or build to pickle it"
            )
            raise TypeError(msg)
        return Builder.build, (self._builder,)

    def __copy__(self) -> Driver:
        """A driver over the same graph, store and settings, with no compiled plans.

        Cheap, unlike pickling: nothing is rebuilt, and a driver made from a native
        graph copies too.
        """
        copied = Driver(
            self._graph,
            self._nodes,
            check_inputs=self._check_inputs,
            cache=self._cache,
        )
        copied._builder = self._builder
        return copied

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
        runnable: _Runnable = plan
        if self._cache is not None:
            runnable = CacheRun(
                self._cache, self._graph, plan, outputs, input_names, override_names
            )
        prepared = PreparedPlan(runnable, outputs, input_names, override_names, checks)
        prepared._driver = self  # noqa: SLF001  # the driver's own plan
        return prepared

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

        The store is consulted for every `default`-policy node with a key, including
        nodes upstream of a hit, which a run would neither load nor run: with a
        `DiskCache`, that reads each of their entries.
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


def _prepare_again(driver: Driver, key: _PlanKey) -> PreparedPlan:
    """Prepares a pickled plan's request on its rebuilt driver."""
    outputs, input_names, override_names = key
    return driver.prepare(
        outputs,
        inputs=dict.fromkeys(input_names),
        overrides=dict.fromkeys(override_names),
    )


def _graph_of(nodes: list[NodeDef]) -> _core.Graph:
    """Builds the native graph for scanned nodes: their core inputs and payloads."""
    return _core.Graph(
        [(n.name, core_inputs(n.params), dict(n.tags), n.payload) for n in nodes],
        {n.name: dict(n.renames) for n in nodes if n.renames},
    )


def _checked_modules(modules: Sequence[object]) -> tuple[ModuleType, ...]:
    """`modules` as a tuple; `TypeError` naming the first item that is not a module.

    A function or a class is a common slip for its module, and would otherwise
    build no nodes at all (Q52).
    """
    checked: list[ModuleType] = []
    for item in modules:
        if not isinstance(item, ModuleType):
            name = getattr(item, "__qualname__", None)
            what = type(item).__name__ + ("" if name is None else f" {name!r}")
            msg = f"expected a module, got {what}"
            raise TypeError(msg)
        checked.append(item)
    return tuple(checked)


def _importable_name(module: ModuleType) -> str:
    """`module`'s name, which imports it; `TypeError` when it does not."""
    name = module.__name__
    if sys.modules.get(name) is not module:
        msg = (
            f"cannot pickle: module {name!r} is not importable by its name, "
            "so it cannot be loaded again"
        )
        raise TypeError(msg)
    return name


def _builder_of(
    names: tuple[str, ...],
    check_inputs: bool,  # noqa: FBT001  # pickle passes arguments positionally
    store: CacheStore | None,
) -> Builder:
    """A pickled builder, its modules imported again by name."""
    modules = tuple(importlib.import_module(name) for name in names)
    return Builder(modules, check_inputs=check_inputs, store=store)


class Builder:
    """Accumulates modules, then builds a `Driver`; methods return new builders.

    A builder pickles as the names of its modules, which must import them, its
    type-check setting and its store, which must pickle too.
    """

    def __init__(
        self,
        modules: tuple[ModuleType, ...] = (),
        *,
        check_inputs: bool = True,
        store: CacheStore | None = None,
    ) -> None:
        """Starts with the given modules (none by default), checking input types.

        `store`, when given, caches the results of the driver's runs in it.
        `TypeError` when an item of `modules` is not a module.
        """
        self._modules = _checked_modules(modules)
        self._check_inputs = check_inputs
        self._store = store

    @override
    def __reduce__(
        self,
    ) -> tuple[Callable[..., Builder], tuple[tuple[str, ...], bool, CacheStore | None]]:
        """The modules' names and the settings, imported again on load."""
        names = tuple(_importable_name(module) for module in self._modules)
        return _builder_of, (names, self._check_inputs, self._store)

    def with_modules(self, *modules: ModuleType) -> Builder:
        """Returns a new builder with `modules` appended to this one's.

        `TypeError` when an item is not a module, such as a function.
        """
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
        if not all(
            callable(getattr(store, m, None)) for m in ("get", "put", "discard")
        ):
            msg = f"a cache store needs get, put and discard methods, not {store!r}"
            raise TypeError(msg)
        return Builder(self._modules, check_inputs=self._check_inputs, store=store)

    def build(self) -> Driver:
        """Scans the modules and returns a `Driver`; `ValueError` on a bad node.

        A module that contributes no node is a `ValueError` too, naming the module
        and why each of its functions was skipped: defined in another module (a
        re-export), or private.

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
        driver = Driver(graph, views, check_inputs=self._check_inputs, cache=cache)
        driver._builder = self  # noqa: SLF001  # the builder's own driver
        return driver


def build(*modules: ModuleType) -> Driver:
    """Builds a `Driver` from `modules`; shorthand for the `Builder` chain."""
    return Builder().with_modules(*modules).build()
