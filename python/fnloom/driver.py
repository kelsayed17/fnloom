"""The user-facing `Builder`, `Driver`, `PreparedPlan`, and their async forms."""

import copy
import importlib
import inspect
import sys
import threading

# Imported for real, not under TYPE_CHECKING: the public signatures annotate with
# them, and `inspect.signature` resolves those annotations at run time.
from collections.abc import Callable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from types import MappingProxyType, ModuleType
from typing import Any, Literal, Protocol, TypedDict, Unpack, override

from fnloom import _async, _core, _types
from fnloom._caching import Cache, CacheOutcome, CacheRun
from fnloom._decorators import Source
from fnloom._defaults import default_problems
from fnloom._extract import is_generated
from fnloom._functions import (
    FunctionReference,
    FunctionSource,
    NodeSpec,
    checked_functions,
    from_references,
    function_of,
    references,
    unimportable,
)
from fnloom._requests import ABSENT, requested, suggest
from fnloom._requests import output_names as _requested_names
from fnloom._scan import LeftOut, NodeDef, core_inputs, resolve, scan
from fnloom._stores import CacheStore
from fnloom._view import (
    InputView,
    NodeView,
    NodeViews,
    node_views,
    output_names,
    topological,
)

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

    def between(self, a: str, b: str) -> list[str]:
        """Returns the nodes strictly between `a` and `b`, endpoints excluded."""
        ...

    def prepare(
        self, outputs: list[str], inputs: list[str], overrides: list[str]
    ) -> _core.Plan:
        """Compiles a plan for the outputs, given the input and override names."""
        ...


class _Request:
    """The checks of a request that come before its plan, shared by both drivers."""

    @staticmethod
    def check(
        final_vars: Sequence[str | Callable[..., object]],
        views: NodeViews,
        inputs: Mapping[str, object] | None,
        overrides: Mapping[str, object] | None,
    ) -> _PlanKey:
        """The outputs as names, and the input and override names of a request.

        A function becomes its node's name (Q30). `TypeError` for a bare `str`
        (Q6), or an output that is neither a name nor a function. The names are
        checked against the graph when the plan is compiled.
        """
        return (_requested_names(final_vars, views), _names(inputs), _names(overrides))


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


def _input_views(views: NodeViews) -> dict[str, InputView]:
    """Every input of `views`, by name in name order, with its consumers."""
    required: dict[str, bool] = {}
    consumers: dict[str, list[str]] = {}
    for view in views.functions.values():
        for name, needed in core_inputs(view.params):
            if name in views.functions:
                continue
            required[name] = required.get(name, False) or needed
            consumers.setdefault(name, []).append(view.name)
    return {
        name: InputView(
            name, views.type_of(name), required[name], tuple(sorted(consumers[name]))
        )
        for name in sorted(consumers)
    }


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
        # This plan as an `AsyncDriver` runs it, made on its first async use.
        self._async: AsyncPreparedPlan | None = None

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

    def __init__(  # noqa: PLR0913  # keyword-only switches, one per builder setting
        self,
        graph: _GraphLike,
        nodes: Mapping[str, NodeView] | None = None,
        *,
        check_inputs: bool = True,
        check_overrides: bool = True,
        strict_overrides: bool = False,
        cache: Cache | None = None,
        config: Mapping[str, object] | None = None,
    ) -> None:
        """Wraps a native graph; `Builder` is the way to make one from modules.

        `config` is the config the graph was built with, kept read-only.
        `check_overrides` is whether overrides are type checked too (it has no
        effect without `check_inputs`), and `strict_overrides` refuses an override
        of an input that is not supplied (Q75).
        """
        self._graph = graph
        # The modules it was built from, each once: `Builder` sets them.
        self._modules: tuple[ModuleType, ...] = ()
        # Made on first use and kept: a graph with many inputs pays only if asked.
        self._inputs: Mapping[str, InputView] | None = None
        self._outputs: list[str] | None = None
        self._check_inputs = check_inputs
        self._check_overrides = check_overrides
        self._strict_overrides = strict_overrides
        self._cache = cache
        self._config = (
            config
            if isinstance(config, MappingProxyType)
            else MappingProxyType(dict(config or {}))
        )
        # Each node no `@config.when*` variant gave, with why: see `prepare`.
        self._unmatched: Mapping[str, str] = {}
        # Each function `@exclude` or `__all__` left out, with why: see `_hints`.
        self._left_out: Mapping[str, Sequence[str]] = {}
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
            check_overrides=self._check_overrides,
            strict_overrides=self._strict_overrides,
            cache=self._cache,
            config=self._config,
        )
        copied._modules = self._modules
        copied._builder = self._builder
        copied._unmatched = self._unmatched
        copied._left_out = self._left_out
        return copied

    @property
    def config(self) -> Mapping[str, object]:
        """The config the graph was built with: a read-only, shallow copy.

        Empty by default. Shallow: a value the caller changes in place after
        `with_config`, such as a list, shows the change here too.

        Config only selects `@config.when*` variants: it is never passed to a node
        (Q56).
        """
        return self._config

    def prepare(
        self,
        final_vars: Sequence[str | Callable[..., object]],
        *,
        inputs: Mapping[str, object] | None = None,
        overrides: Mapping[str, object] | None = None,
    ) -> PreparedPlan:
        """Returns the plan for this request shape, compiling it on first use.

        A function in `final_vars` means its node (Q30); see `output_names`.
        """
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
        key = _Request.check(final_vars, self._nodes, inputs, overrides)
        with self._plans_lock:
            prepared = self._plans.pop(key, None)
            if prepared is None:
                prepared = self._compile(key)
            self._plans[key] = prepared
            if len(self._plans) > PLAN_CACHE_SIZE:
                del self._plans[next(iter(self._plans))]
            # A list is copied, so a list changed after this call is not a repeat.
            if type(final_vars) is list:
                self._last = (list(key[0]), key[1], key[2], prepared)
            elif type(final_vars) is tuple:
                self._last = (key[0], key[1], key[2], prepared)
        return prepared

    def _compile(self, key: _PlanKey) -> PreparedPlan:
        outputs, input_names, override_names = key
        if self._unmatched and (
            absent := sorted({name for name in outputs if name in self._unmatched})
        ):
            lines = [self._unmatched[name] for name in absent]
            # A misspelled output in the same request is named too, not hidden.
            known = self._nodes
            if unknown := sorted(
                {n for n in outputs if n not in self._unmatched and n not in known}
            ):
                lines.append(_unknown("outputs", unknown, self._graph.node_names()))
            raise ValueError("\n".join(lines))
        try:
            plan = self._graph.prepare(
                list(outputs), sorted(input_names), sorted(override_names)
            )
        except ValueError as error:
            msg = "\n".join(self._explained(str(error), key))
            if msg == str(error):
                raise
            raise ValueError(msg) from None
        if self._strict_overrides and (
            refused := self._unsupplied_overrides(plan, input_names, override_names)
        ):
            raise ValueError(_strict(refused))
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
        overrides = override_names if self._check_overrides else ()
        return self._declared(read), self._declared(sorted(overrides))

    def _unsupplied_overrides(
        self,
        plan: _core.Plan,
        input_names: AbstractSet[str],
        override_names: AbstractSet[str],
    ) -> dict[str, list[str]]:
        """Each overridden input the request does not supply, with who needs it (Q75).

        Hamilton's rule: an override never supplies an input, so one naming an
        input is refused when a node the plan computes requires that input and it
        is not in `input_names`. An input nothing computed requires, such as an
        output itself, is not refused.
        """
        functions = self._nodes.functions
        candidates = {
            n for n in override_names if n in self._nodes and n not in functions
        } - input_names
        if not candidates:
            return {}
        readers: dict[str, list[str]] = {}
        for node, action in plan.steps():
            view = functions.get(node)
            if action != "compute" or view is None:
                continue
            for name, needed in core_inputs(view.params):
                if needed and name in candidates:
                    readers.setdefault(name, []).append(node)
        return {name: sorted(readers[name]) for name in sorted(readers)}

    def _declared(self, names: list[str]) -> tuple[_Check, ...]:
        """Each of `names` with its node's type; a name with no view goes unchecked."""
        # By type, not view: the views of many inputs are never made at all.
        return tuple(
            (n, declared, _types.plain_class(declared))
            for n in names
            if n in self._nodes
            for declared in (self._nodes.type_of(n),)
        )

    def _explained(self, message: str, key: _PlanKey) -> list[str]:
        """The lines of the core's error `message` for request `key`, with suggestions.

        The core's message only says which kind of problem it is: the names are
        worked out here from the request and the views, and the line is rebuilt in
        the core's format, each unknown name with the closest node (Q71). When the
        rebuilt line, without suggestions, is not the core's, the core's is kept.
        A missing input gets a line for each config key it names (Q56); `execute`
        never suggests one, since a supplied name that is no input is raised first.
        """
        for kind in ("outputs", "inputs", "overrides"):
            if message.startswith(f"unknown {kind}: "):
                names = self._unknown_names(kind, key)
                if names and _unknown(kind, names, ()) == message:
                    return [_unknown(kind, names, list(self._nodes))]
                return [message]
        return [message, *self._hints(message)]

    def _hints(self, line: str) -> list[str]:
        """The lines explaining a missing input in `line`: config keys, left out."""
        hints: list[str] = []
        if (hint := _config_hint(line, self._config)) is not None:
            hints.append(hint)
        if line.startswith(_MISSING):
            hints += _left_out_hints(line, self._left_out)
        return hints

    def _unknown_names(self, kind: str, key: _PlanKey) -> list[str]:
        """The names of request `key` of `kind` the graph lacks, sorted.

        An output or override is unknown when it is no node; an input when it is
        no input of the graph.
        """
        outputs, input_names, override_names = key
        if kind == "inputs":
            functions = self._nodes.functions
            return sorted(
                n for n in input_names if n not in self._nodes or n in functions
            )
        names = outputs if kind == "outputs" else override_names
        return sorted({n for n in names if n not in self._nodes})

    def _missing_entries(
        self, plan: _core.Plan, outputs: Sequence[str], supplied: AbstractSet[str]
    ) -> list[tuple[str, str]]:
        """Each input missing from a request, as the core lists it, by name.

        `plan` was compiled with every input of the graph supplied: an input it
        reads that the request does not supply is missing when it was requested,
        or a computed node of the plan reads it by a required edge, as the core's
        `'x' (requested as an output, needed by 'a', 'b')`.
        """
        steps = plan.steps()
        functions = self._nodes.functions
        readers: dict[str, list[str]] = {}
        for node, action in steps:
            view = functions.get(node)
            if action != "compute" or view is None:
                continue
            for name, needed in core_inputs(view.params):
                if needed:
                    readers.setdefault(name, []).append(node)
        wanted = set(outputs)
        entries: list[tuple[str, str]] = []
        unsupplied = (n for n, a in steps if a == "use_input" and n not in supplied)
        for name in sorted(unsupplied):
            reasons = ["requested as an output"] if name in wanted else []
            if consumers := sorted(readers.get(name, ())):
                reasons.append(f"needed by {', '.join(f"'{c}'" for c in consumers)}")
            if reasons:
                entries.append((name, f"'{name}' ({', '.join(reasons)})"))
        return entries

    def validate(
        self,
        final_vars: Sequence[str | Callable[..., object]],
        *,
        inputs: Mapping[str, object] | None = None,
        overrides: Mapping[str, object] | None = None,
    ) -> list[str]:
        """Every problem `execute` would raise for this request, one line each (Q71).

        Runs nothing, and keeps no plan. The lines are sorted, and empty when the
        request is valid; each is the line `execute` raises for that problem on
        its own. They cover functions in `final_vars` no node comes from (Q30);
        unknown outputs, inputs and overrides; missing inputs, naming only the
        consumers this request plans; the types of the input and override values
        (unless the driver was built without input type checks); and outputs no
        `@config.when*` variant gave.

        Only `validate` suggests a name for a missing input: the closest supplied
        name that is no node of the graph. `execute` raises that unknown input
        first, on its own.

        A malformed request raises: `TypeError` for a bare `str`, an item of
        `final_vars` that is neither a name nor a function, or a function that
        makes several nodes, as `execute` raises it; and `TypeError` for `inputs`
        or `overrides` that is not a mapping, which `execute` refuses too.
        """
        for kind, values in (("inputs", inputs), ("overrides", overrides)):
            if values is not None and not isinstance(values, Mapping):
                msg = (
                    f"{kind} must be a mapping of names to values, "
                    f"got {type(values).__name__}"
                )
                raise TypeError(msg)
        outputs, absent_functions = requested(final_vars, self._nodes)
        key: _PlanKey = (outputs, _names(inputs), _names(overrides))
        _, input_names, override_names = key
        wanted = list(dict.fromkeys(outputs))
        lines = [ABSENT + culprit for culprit in absent_functions]
        lines += [
            self._unmatched[n] for n in sorted(set(wanted) & self._unmatched.keys())
        ]
        # An output no variant gave has its own line; an input or override named
        # like one is unknown, as `execute` says.
        unknown = {
            kind: self._unknown_names(kind, key)
            for kind in ("outputs", "inputs", "overrides")
        }
        unknown["outputs"] = [n for n in unknown["outputs"] if n not in self._unmatched]
        nodes = list(self._nodes)
        lines += [
            _unknown(kind, names, nodes) for kind, names in unknown.items() if names
        ]
        planned = [n for n in wanted if n in self._nodes]
        supplied = input_names.difference(unknown["inputs"])
        overridden = override_names.difference(unknown["overrides"])
        plan: _core.Plan | None
        try:
            plan = self._graph.prepare(planned, sorted(supplied), sorted(overridden))
        except ValueError as error:
            plan, line = self._missing_line(
                str(error), (planned, supplied, overridden), input_names
            )
            lines.append(line)
            lines += self._hints(line)
        if plan is not None and self._strict_overrides:
            refused = self._unsupplied_overrides(plan, input_names, overridden)
            lines += _strict(refused).splitlines() if refused else []
        if self._check_inputs and plan is not None:
            read = sorted(
                node
                for node, action in plan.steps()
                if action == "use_input" and node in supplied
            )
            lines += _mismatches(self._declared(read), inputs or _NO_VALUES)
            if self._check_overrides:
                lines += _mismatches(
                    self._declared(sorted(overridden)), overrides or _NO_VALUES
                )
        return sorted(lines)

    def _missing_line(
        self,
        message: str,
        request: tuple[list[str], frozenset[str], frozenset[str]],
        input_names: frozenset[str],
    ) -> tuple[_core.Plan | None, str]:
        """The plan with every input supplied, and the missing-input line.

        `message` is the core's error for `request`, whose names are all known. The
        line is rebuilt from the plan, each input with the closest supplied name
        that is no node (Q71); when the rebuilt line, without suggestions, is not
        the core's, or the plan can't be made, it is `message` unchanged.
        """
        planned, supplied, overridden = request
        functions = self._nodes.functions
        every = supplied | {n for n in self._nodes if n not in functions}
        try:
            plan = self._graph.prepare(planned, sorted(every), sorted(overridden))
        except ValueError:
            return None, message
        entries = self._missing_entries(plan, planned, supplied)
        if not entries or _missing(entries, ()) != message:
            return plan, message
        strangers = sorted(n for n in input_names if n not in self._nodes)
        return plan, _missing(entries, strangers)

    def execute(
        self,
        final_vars: Sequence[str | Callable[..., object]],
        *,
        inputs: Mapping[str, object] | None = None,
        overrides: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """Computes `final_vars`, reusing the cached plan for the same request shape.

        The result holds each requested output once, in the order first requested.
        A function in `final_vars` stands for the node it makes in this driver,
        matched by identity (Q30): the result is still keyed by the node's name.
        `TypeError` when it makes several nodes, as a `@parameterize` function does,
        and `ValueError` when it makes none.
        """
        plan = self.prepare(final_vars, inputs=inputs, overrides=overrides)
        return plan.run(inputs=inputs, overrides=overrides)

    def explain_cache(
        self,
        final_vars: Sequence[str | Callable[..., object]],
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

    @property
    def modules(self) -> tuple[ModuleType, ...]:
        """The modules the graph was built from, each once, in build order (Q70).

        Hamilton's `graph_modules` keeps a module given twice. A function a later
        `Builder.with_functions` adds is no module, so it is not listed.
        """
        return self._modules

    def inputs(self) -> Mapping[str, InputView]:
        """Every input of the graph by name, in name order, with its consumers (Q70).

        An input is a name no node produces. Each `InputView` has its type, whether
        some consumer requires it, and its consumers, sorted. Read-only, and made
        once.
        """
        if self._inputs is None:
            self._inputs = MappingProxyType(_input_views(self._nodes))
        return self._inputs

    def outputs(self) -> list[str]:
        """The nodes no other node reads, sorted (Q70).

        Inputs are never outputs: each is read by the node that makes it an input.
        """
        if self._outputs is None:
            self._outputs = output_names(self._nodes)
        return list(self._outputs)

    def what_is_the_path_between(self, upstream: str, downstream: str) -> list[str]:
        """Returns every node on a path from `upstream` to `downstream` (Q70).

        Both endpoints are included, and an input can be either one. The order is
        topological, ties broken by name, so it never depends on
        `PYTHONHASHSEED`. Empty when `downstream` doesn't depend on `upstream`;
        `[upstream]` when they're the same node. `ValueError` names every unknown
        endpoint, sorted; `TypeError` when one is not a `str`.
        """
        for end in (upstream, downstream):
            if not isinstance(end, str):
                msg = f"node names must be str, got {type(end).__name__}"
                raise TypeError(msg)
        ends = {upstream, downstream}
        if unknown := sorted(n for n in ends if n not in self._nodes):
            msg = f"unknown nodes: {', '.join(map(repr, unknown))}"
            raise ValueError(msg)
        if upstream == downstream:
            return [upstream]
        if downstream not in set(self._graph.downstream_of([upstream])):
            return []
        between = self._graph.between(upstream, downstream)
        return topological({upstream, downstream, *between}, self._nodes)

    def what_is_upstream_of(self, *names: str) -> list[str]:
        """Returns the named nodes and every node they depend on, sorted."""
        return sorted({*names, *self._graph.upstream_of(list(names))})

    def what_is_downstream_of(self, *names: str) -> list[str]:
        """Returns the named nodes and every node depending on them, sorted."""
        return sorted({*names, *self._graph.downstream_of(list(names))})


class AsyncPreparedPlan:
    """A plan an `AsyncDriver` compiled: `PreparedPlan`, with `run` a coroutine.

    Compiled for the same outputs and names, and checked the same way, as a
    `PreparedPlan`; pickled as its driver and its request, as one is.
    """

    def __init__(
        self,
        prepared: PreparedPlan,
        driver: AsyncDriver,
        schedule: _async.Schedule | _async.CachedSchedule,
    ) -> None:
        """The async form of `prepared`, run by `schedule`, for `driver`."""
        self._prepared = prepared
        self._driver = driver
        self._schedule = schedule
        self._outputs = prepared._outputs  # noqa: SLF001  # the plan this wraps

    @override
    def __reduce__(
        self,
    ) -> tuple[Callable[..., AsyncPreparedPlan], tuple[AsyncDriver, _PlanKey]]:
        """The driver and the request, prepared again on load; see the class."""
        prepared = self._prepared
        key: _PlanKey = (
            self._outputs,
            prepared._input_names,  # noqa: SLF001  # the plan this wraps
            prepared._override_names,  # noqa: SLF001  # the plan this wraps
        )
        return _prepare_async_again, (self._driver, key)

    def __copy__(self) -> AsyncPreparedPlan:
        """The plan itself: nothing about a plan changes once it is compiled."""
        return self

    async def run(
        self,
        *,
        inputs: Mapping[str, object] | None = None,
        overrides: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """Runs on the running loop; checked and returned as `PreparedPlan.run` is.

        The first node to fail cancels every node still running and raises its own
        exception, with a note naming it (Q66).
        """
        given_inputs, given_overrides = self._prepared._checked(inputs, overrides)  # noqa: SLF001  # the plan this wraps
        values = await self._schedule.run(given_inputs, given_overrides)
        return {name: values[name] for name in self._outputs}


class AsyncDriver:
    """Executes a graph whose nodes may be `async def`: `execute` is a coroutine.

    Made by `Builder.build_async`, which is synchronous. It plans, checks requests
    and keeps plans as `Driver` does, with the same errors (Q67). A coroutine
    function is awaited; a sync function runs in the loop's default executor, in
    the caller's context, so the loop keeps running (Q65), or on the loop with
    `sync_nodes="inline"`. A generated `extract_*` node always runs on the loop.

    A driver made by a `Builder` pickles as that builder and its `sync_nodes`, as
    `Driver` does.
    """

    def __init__(
        self,
        driver: Driver,
        nodes: Mapping[str, NodeDef],
        *,
        sync_nodes: Literal["thread", "inline"] = "thread",
    ) -> None:
        """Runs `driver`'s plans; `nodes` are its graph's nodes, by name."""
        self._driver = driver
        self._nodes = nodes
        self._sync_nodes: Literal["thread", "inline"] = sync_nodes
        self._calls = _async.calls(nodes, sync_nodes)
        # The builder that built this driver, which pickling rebuilds it from.
        self._builder: Builder | None = None

    @override
    def __reduce__(
        self,
    ) -> tuple[Callable[..., AsyncDriver], tuple[Builder, str]]:
        """The builder and `sync_nodes`, built again on load; see the class."""
        if self._builder is None:
            msg = (
                "cannot pickle an AsyncDriver made from a Driver; "
                "make it with Builder.build_async to pickle it"
            )
            raise TypeError(msg)
        return _build_async_again, (self._builder, self._sync_nodes)

    def __copy__(self) -> AsyncDriver:
        """A driver over the same graph and settings, with no compiled plans."""
        copied = AsyncDriver(
            copy.copy(self._driver), self._nodes, sync_nodes=self._sync_nodes
        )
        copied._builder = self._builder
        return copied

    @property
    def config(self) -> Mapping[str, object]:
        """The config the graph was built with; see `Driver.config`."""
        return self._driver.config

    @property
    def nodes(self) -> Mapping[str, NodeView]:
        """Every node, inputs included, by name; see `Driver.nodes`."""
        return self._driver.nodes

    @property
    def modules(self) -> tuple[ModuleType, ...]:
        """The modules the graph was built from; see `Driver.modules`."""
        return self._driver.modules

    def inputs(self) -> Mapping[str, InputView]:
        """Every input of the graph; see `Driver.inputs`."""
        return self._driver.inputs()

    def outputs(self) -> list[str]:
        """The nodes no other node reads, sorted; see `Driver.outputs`."""
        return self._driver.outputs()

    def what_is_the_path_between(self, upstream: str, downstream: str) -> list[str]:
        """The nodes between two, inclusive; see `Driver.what_is_the_path_between`."""
        return self._driver.what_is_the_path_between(upstream, downstream)

    def node_names(self) -> list[str]:
        """Returns every node name, sorted."""
        return self._driver.node_names()

    def what_is_upstream_of(self, *names: str) -> list[str]:
        """Returns the named nodes and every node they depend on, sorted."""
        return self._driver.what_is_upstream_of(*names)

    def what_is_downstream_of(self, *names: str) -> list[str]:
        """Returns the named nodes and every node depending on them, sorted."""
        return self._driver.what_is_downstream_of(*names)

    def prepare(
        self,
        final_vars: Sequence[str | Callable[..., object]],
        *,
        inputs: Mapping[str, object] | None = None,
        overrides: Mapping[str, object] | None = None,
    ) -> AsyncPreparedPlan:
        """Returns the plan for this request shape, compiling it on first use.

        Synchronous: compiling awaits nothing. The plans are kept as `Driver`
        keeps them.
        """
        prepared = self._driver.prepare(final_vars, inputs=inputs, overrides=overrides)
        plan = prepared._async  # noqa: SLF001  # this driver's own plan
        if plan is None:
            runnable = prepared._plan  # noqa: SLF001  # this driver's own plan
            schedule: _async.Schedule | _async.CachedSchedule
            if isinstance(runnable, CacheRun):
                schedule = _async.CachedSchedule(runnable, self._calls)
            elif isinstance(runnable, _core.Plan):
                schedule = _async.Schedule(runnable, self._calls)
            else:
                msg = f"an AsyncDriver cannot run {type(runnable).__name__}"
                raise TypeError(msg)
            plan = AsyncPreparedPlan(prepared, self, schedule)
            # Two threads may both get here: either plan will do, and one is kept.
            prepared._async = plan  # noqa: SLF001  # this driver's own plan
        return plan

    def validate(
        self,
        final_vars: Sequence[str | Callable[..., object]],
        *,
        inputs: Mapping[str, object] | None = None,
        overrides: Mapping[str, object] | None = None,
    ) -> list[str]:
        """Every problem of this request, sorted; see `Driver.validate` (Q71).

        Synchronous: it runs nothing, so there is nothing to await.
        """
        return self._driver.validate(final_vars, inputs=inputs, overrides=overrides)

    async def execute(
        self,
        final_vars: Sequence[str | Callable[..., object]],
        *,
        inputs: Mapping[str, object] | None = None,
        overrides: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """Computes `final_vars`; checked and returned as `Driver.execute`.

        The result holds each requested output once, in the order first requested
        (Q11). The first node to fail cancels every node still running and raises
        its own exception, with a note naming it (Q66).
        """
        plan = self.prepare(final_vars, inputs=inputs, overrides=overrides)
        return await plan.run(inputs=inputs, overrides=overrides)

    def explain_cache(
        self,
        final_vars: Sequence[str | Callable[..., object]],
        *,
        inputs: Mapping[str, object] | None = None,
        overrides: Mapping[str, object] | None = None,
    ) -> dict[str, CacheOutcome]:
        """See `Driver.explain_cache`; synchronous, since it runs nothing."""
        return self._driver.explain_cache(
            final_vars, inputs=inputs, overrides=overrides
        )


def _build_async_again(builder: Builder, sync_nodes: str) -> AsyncDriver:
    """A pickled `AsyncDriver`, built again by its builder."""
    return builder.build_async(sync_nodes=_async.checked_sync_nodes(sync_nodes))


def _prepare_async_again(driver: AsyncDriver, key: _PlanKey) -> AsyncPreparedPlan:
    """Prepares a pickled async plan's request on its rebuilt driver."""
    outputs, input_names, override_names = key
    return driver.prepare(
        outputs,
        inputs=dict.fromkeys(input_names),
        overrides=dict.fromkeys(override_names),
    )


# How the core's missing-input error starts.
_MISSING = "missing inputs: "


def _unknown(kind: str, names: Sequence[str], candidates: Sequence[str]) -> str:
    """The core's `unknown <kind>: 'a', 'b'` line, each name with its suggestion."""
    listed = ", ".join(f"'{name}'{suggest(name, candidates)}" for name in names)
    return f"unknown {kind}: {listed}"


def _missing(entries: Sequence[tuple[str, str]], candidates: Sequence[str]) -> str:
    """The core's missing-input line for `entries`, each with its suggestion (Q71)."""
    listed = "; ".join(text + suggest(name, candidates) for name, text in entries)
    return _MISSING + listed


def _strict(refused: Mapping[str, Sequence[str]]) -> str:
    """The strict-overrides error: a line per overridden input not supplied (Q75)."""
    return "\n".join(
        f"override of input '{name}' refused: not supplied, though needed by "
        f"{', '.join(f"'{c}'" for c in refused[name])} "
        "(supply it in inputs= too; strict overrides are on)"
        for name in sorted(refused)
    )


def _config_hint(message: str, config: Mapping[str, object]) -> str | None:
    """A line for a missing-input error naming config keys, or `None` (Q56).

    A missing input is listed as `'name' (`, which no other part of the message
    is: a consumer is followed by `,` or `)`.
    """
    if not config or not message.startswith(_MISSING):
        return None
    keys = [key for key in sorted(config) if f"'{key}' (" in message]
    if not keys:
        return None
    listed = ", ".join(repr(key) for key in keys)
    if len(keys) == 1:
        return (
            f"{listed} is a config key; config is not passed to nodes: "
            "supply it in inputs="
        )
    return (
        f"{listed} are config keys; config is not passed to nodes: "
        "supply them in inputs="
    )


def _left_out_hints(message: str, left_out: Mapping[str, Sequence[str]]) -> list[str]:
    """A line for each missing input in `message` that is a function left out (Q73).

    The name of a function `@exclude` marks, or a module's `__all__` does not list,
    is a missing input like any other name: this says why it is not a node.
    """
    return [
        line
        for name in sorted(left_out)
        if f"'{name}' (" in message
        for line in left_out[name]
    ]


def _checked_config(config: object) -> dict[str, object]:
    """A copy of `config`; `TypeError` for a non-mapping or a key that is no `str`."""
    if not isinstance(config, Mapping):
        msg = f"with_config takes a mapping, got {type(config).__name__}"
        raise TypeError(msg)
    given: dict[object, object] = dict(config)
    not_str = sorted(type(k).__name__ for k in given if not isinstance(k, str))
    if not_str:
        msg = f"config keys must be str, got: {', '.join(not_str)}"
        raise TypeError(msg)
    return {str(k): v for k, v in given.items()}


def _checked_names(names: Sequence[object]) -> tuple[str, ...]:
    """`names` once each, sorted; `TypeError` naming every item that is no `str`."""
    strings = {n for n in names if isinstance(n, str)}
    if bad := sorted({type(n).__name__ for n in names if not isinstance(n, str)}):
        msg = f"declared input names must be str, got: {', '.join(bad)}"
        raise TypeError(msg)
    return tuple(sorted(strings))


def _undeclared(views: NodeViews, declared: Sequence[str]) -> list[str]:
    """A line for each input not declared and each declared name no input is (Q75).

    Each name gains the closest node or declared name. Sorted.
    """
    functions = views.functions
    needed = {
        name
        for view in functions.values()
        for name, _ in core_inputs(view.params)
        if name not in functions
    }
    candidates = [*views, *declared]
    lines = [
        f"undeclared input '{name}'{suggest(name, candidates)}"
        for name in sorted(needed.difference(declared))
    ]
    lines += [
        f"declared name '{name}' is not an input of the graph"
        f"{suggest(name, candidates)}"
        for name in declared
        if name not in needed
    ]
    return sorted(lines)


def _unmatched_edges(nodes: Sequence[NodeDef], unmatched: Mapping[str, str]) -> str:
    """Why each node no variant gave that a node reads is absent, sorted; or ``""``."""
    read = {
        p.binding.name
        for node in nodes
        for p in node.params
        if isinstance(p.binding, Source) and p.binding.name in unmatched
    }
    return "\n".join(unmatched[name] for name in sorted(read))


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


def _unimportable_module(module: ModuleType) -> str | None:
    """Why `module` cannot be imported again by its name, or `None` when it can."""
    name = module.__name__
    if sys.modules.get(name) is module:
        return None
    return (
        f"module {name!r} is not importable by its name, so it cannot be loaded again"
    )


def _builder_of(
    names: tuple[str, ...],
    settings: _Settings,
    store: CacheStore | None,
    config: Mapping[str, object] | None = None,
    functions: tuple[FunctionReference, ...] = (),
) -> Builder:
    """A pickled builder, its modules imported again by name, its functions too."""
    modules = tuple(importlib.import_module(name) for name in names)
    return Builder(
        modules,
        store=store,
        config=config,
        functions=from_references(functions),
        **settings,
    )


class _Changes(TypedDict, total=False):
    """The switches a `Builder._with` call replaces."""

    check_inputs: bool
    check_overrides: bool
    strict_overrides: bool
    declared_inputs: tuple[str, ...]


class _Settings(TypedDict):
    """A builder's switches, which pickling and copying carry as one value."""

    check_inputs: bool
    check_overrides: bool
    strict_overrides: bool
    declared_inputs: tuple[str, ...]


class Builder:
    """Accumulates modules and functions, then builds a `Driver`.

    Its methods return new builders. A builder pickles as the names of its
    modules, which must import them, the module and qualified name of each
    function `with_functions` gave it, which must find it again, its settings,
    its config and its store, which must pickle too.
    """

    def __init__(  # noqa: PLR0913  # keyword-only switches, one per builder setting
        self,
        modules: tuple[ModuleType, ...] = (),
        *,
        functions: Sequence[FunctionSource] = (),
        check_inputs: bool = True,
        check_overrides: bool = True,
        strict_overrides: bool = False,
        declared_inputs: Sequence[str] = (),
        store: CacheStore | None = None,
        config: Mapping[str, object] | None = None,
    ) -> None:
        """Starts with the given modules (none by default), checking input types.

        `functions` are the node sources of `with_functions`. `store`, when
        given, caches the results of the driver's runs in it.
        `config`, copied, selects `@config.when*` variants (see `with_config`).
        The other settings are those of `without_input_type_checks`,
        `without_override_type_checks`, `with_strict_overrides` and
        `with_declared_inputs`. `TypeError` when an item of `modules` is not a
        module, or of `declared_inputs` is not a `str`; see `with_functions` for
        the errors `functions` raises.
        """
        self._modules = _checked_modules(modules)
        self._functions = checked_functions(functions)
        self._check_inputs = check_inputs
        self._check_overrides = check_overrides
        self._strict_overrides = strict_overrides
        self._declared_inputs = _checked_names(declared_inputs)
        self._store = store
        # Never changed once made: `with_config` makes a new dict.
        self._config: dict[str, object] = (
            {} if config is None else _checked_config(config)
        )

    @override
    def __reduce__(
        self,
    ) -> tuple[
        Callable[..., Builder],
        tuple[
            tuple[str, ...],
            _Settings,
            CacheStore | None,
            dict[str, object],
            tuple[FunctionReference, ...],
        ],
    ]:
        """The modules' names, the functions' references and the settings.

        Imported again on load. `TypeError` naming every module and function that
        would not be found again, sorted.
        """
        found = [
            *(_unimportable_module(module) for module in self._modules),
            *(unimportable(function_of(item)) for item in self._functions),
        ]
        if problems := sorted({problem for problem in found if problem is not None}):
            msg = f"cannot pickle: {'; '.join(problems)}"
            raise TypeError(msg)
        names = tuple(module.__name__ for module in self._modules)
        return _builder_of, (
            names,
            self._settings(),
            self._store,
            self._config,
            references(self._functions),
        )

    def _settings(self) -> _Settings:
        """This builder's switches, which `_builder_of` makes a builder from."""
        return {
            "check_inputs": self._check_inputs,
            "check_overrides": self._check_overrides,
            "strict_overrides": self._strict_overrides,
            "declared_inputs": self._declared_inputs,
        }

    def _with(
        self,
        modules: tuple[ModuleType, ...] | None = None,
        *,
        functions: tuple[FunctionSource, ...] | None = None,
        store: CacheStore | None = None,
        config: Mapping[str, object] | None = None,
        **settings: Unpack[_Changes],
    ) -> Builder:
        """A new builder: this one's settings, each given one replaced.

        `functions`, when given, are already checked, as this builder's are, so
        they are set rather than checked again: adding functions one call at a
        time stays linear in checks.
        """
        built = Builder(
            self._modules if modules is None else modules,
            store=self._store if store is None else store,
            config=self._config if config is None else config,
            **{**self._settings(), **settings},
        )
        # Already checked, by this builder or `with_functions`.
        built._functions = self._functions if functions is None else functions
        return built

    def with_modules(self, *modules: ModuleType) -> Builder:
        """Returns a new builder with `modules` appended to this one's.

        `TypeError` when an item is not a module, such as a function.
        """
        return self._with((*self._modules, *modules))

    def with_functions(self, *items: Callable[..., object] | NodeSpec) -> Builder:
        """Returns a new builder with `items` appended to this one's functions (Q72).

        Each item is a function, plain or decorated, which makes the nodes it would
        make in a module, named by its `__name__`, or a `fnloom.node(...)` spec.
        Nothing is copied and no module is made or registered: a node's module is
        its function's own `__module__`, and `Driver.modules` lists only modules.
        Cumulative and ordered, after the modules; the same function or spec given
        twice counts once, and so does a function a module given to this builder
        also holds, plain, parameterized or a variant. A node name a module or
        another item also defines is the build's usual duplicate error.

        Raises one error naming every item that is refused, sorted: a `TypeError`
        when some item is no Python function or spec (a module, a class, a
        `functools.partial`, a callable instance), else a `ValueError` for a lambda
        or other function whose name is no identifier (give it `fnloom.node(fn,
        name=...)`) and for a function marked `@exclude`.
        """
        return self._with(functions=(*self._functions, *checked_functions(items)))

    def with_config(self, config: Mapping[str, object]) -> Builder:
        """Returns a new builder with `config` merged into a copy of this one's.

        A key given again takes its later value. The config selects among
        `@config.when*` variants when building, and is never passed to a node
        (Q56); the driver's `config` is a read-only, shallow copy of it.
        `TypeError` for a non-mapping or a key that is not a `str`.
        """
        return self._with(config={**self._config, **_checked_config(config)})

    def without_input_type_checks(self) -> Builder:
        """Returns a new builder whose driver skips input and override type checks.

        Edge checks, between a node and the parameters it feeds, stay on (Q10).
        To skip only the override checks, use `without_override_type_checks`.
        """
        return self._with(check_inputs=False)

    def without_override_type_checks(self) -> Builder:
        """Returns a new builder whose driver skips override type checks only (Q22).

        Input checks stay on, and so do edge checks. For overriding a node with a
        value of another type, such as `nan` for an `int` node, while still
        refusing a badly typed input.
        """
        return self._with(check_overrides=False)

    def with_strict_overrides(self) -> Builder:
        """Returns a new builder whose driver refuses an unsupplied overridden input.

        An override naming an input is refused, as Hamilton refuses it, unless the
        input is supplied in `inputs` too and some node the request plans needs it
        (Q75). Settled when a request shape is prepared, never per run. The default
        stays Q7's pass-through.
        """
        return self._with(strict_overrides=True)

    def with_declared_inputs(self, *names: str) -> Builder:
        """Returns a new builder that must declare every input of its graph (Q75).

        Cumulative: names given again are added. Building fails, with one sorted
        `ValueError`, for an input of the graph not declared, and for a declared
        name that is no input, each naming the closest node or declared name. A
        misspelled parameter or `source(...)` is therefore a build error.
        """
        return self._with(declared_inputs=(*self._declared_inputs, *names))

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
        return self._with(store=store)

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

        The config selects one `@config.when*` variant per node. Two matching
        variants, or a node that a node reads and that no variant or
        `@config.default` gives, are a `ValueError` naming each (Q57, Q58).
        """
        nodes, views, graph, unmatched, left_out = self._scanned(awaits=False)
        # Called here, so a `CacheWarning` points at the caller's `build`.
        cache = None if self._store is None else Cache.of(self._store, nodes)
        return self._driver(graph, views, unmatched, left_out, cache)

    def build_async(
        self, *, sync_nodes: Literal["thread", "inline"] = "thread"
    ) -> AsyncDriver:
        """Scans the modules and returns an `AsyncDriver`; synchronous (Q12).

        As `build`, but an `async def` node is accepted, and awaited when run.
        `sync_nodes` says where a sync node runs: `"thread"` in the loop's
        default executor, so the loop keeps running while it does (Q65), or
        `"inline"` on the loop, which suits a graph of cheap nodes. A generated
        `extract_*` node always runs on the loop. `ValueError` for any other
        `sync_nodes`.

        With `with_cache`, runs cache as `build`'s do, with the same keys and
        store (Q68): each node is stored as it succeeds, and a node that fails,
        or that a failure cancels, stores nothing. Store calls run on the loop.
        """
        checked = _async.checked_sync_nodes(sync_nodes)
        nodes, views, graph, unmatched, left_out = self._scanned(awaits=True)
        # Called here, so a `CacheWarning` points at the caller's `build_async`.
        cache = None if self._store is None else Cache.of(self._store, nodes)
        built = AsyncDriver(
            self._driver(graph, views, unmatched, left_out, cache),
            {node.name: node for node in nodes},
            sync_nodes=checked,
        )
        built._builder = self  # noqa: SLF001  # the builder's own driver
        return built

    def _scanned(
        self, *, awaits: bool
    ) -> tuple[list[NodeDef], NodeViews, _core.Graph, dict[str, str], LeftOut]:
        """The checked nodes, their views and graph; see `build`.

        Also why each node no variant gave is absent, and each function `@exclude` or
        `__all__` left out. With `awaits`, an
        `async def` is a node too.
        """
        scanned = scan(
            self._modules, self._config, awaits=awaits, functions=self._functions
        )
        nodes = resolve(scanned.nodes)
        unmatched = scanned.unmatched
        if unmatched:
            # A field of a forward-referenced `TypedDict` is first known resolved.
            names = {node.name for node in nodes}
            unmatched = {n: why for n, why in unmatched.items() if n not in names}
            if absent := _unmatched_edges(nodes, unmatched):
                raise ValueError(absent)
        views = node_views(
            nodes, frozenset(node.name for node in nodes if is_generated(node))
        )
        graph = _graph_of(nodes)
        if mismatches := _edge_mismatches(views.functions):
            raise ValueError("\n".join(mismatches))
        if problems := default_problems(nodes):
            raise ValueError("\n".join(problems))
        if self._declared_inputs and (
            refused := _undeclared(views, self._declared_inputs)
        ):
            raise ValueError("\n".join(refused))
        return nodes, views, graph, unmatched, scanned.left_out

    def _driver(
        self,
        graph: _core.Graph,
        views: NodeViews,
        unmatched: dict[str, str],
        left_out: LeftOut,
        cache: Cache | None,
    ) -> Driver:
        """This builder's driver over a scanned graph."""
        driver = Driver(
            graph,
            views,
            check_inputs=self._check_inputs,
            check_overrides=self._check_overrides,
            strict_overrides=self._strict_overrides,
            cache=cache,
            config=MappingProxyType(self._config),
        )
        driver._modules = tuple(dict.fromkeys(self._modules))  # noqa: SLF001  # the builder's own driver
        driver._unmatched = unmatched  # noqa: SLF001  # the builder's own driver
        driver._left_out = left_out  # noqa: SLF001  # the builder's own driver
        driver._builder = self  # noqa: SLF001  # the builder's own driver
        return driver


def build(*modules: ModuleType) -> Driver:
    """Builds a `Driver` from `modules`; shorthand for the `Builder` chain."""
    return Builder().with_modules(*modules).build()
