"""User hooks: `Hook`, its events, the checks when attached, and the dispatcher.

A run's events go through one `Dispatch`, made for that run. The sync driver feeds
it node events from the core through `_Forward`, a private core observer; the
front end fires the run events itself, around the value checks and the core run.
"""

import contextvars
import functools
import inspect
import logging
import reprlib
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType, ModuleType
from typing import TYPE_CHECKING, Final, override

from fnloom import _checks, _core

# Imported for real, not under TYPE_CHECKING: the events' signatures annotate with
# them, and `inspect.signature` resolves those annotations at run time.
from fnloom._caching import CacheOutcome
from fnloom._checks import CheckResult
from fnloom._requests import suggest
from fnloom._scan import by_parameter
from fnloom._view import NodeView, NodeViews

if TYPE_CHECKING:
    from fnloom._caching import Begun


@dataclass(frozen=True, slots=True, kw_only=True)
class RunStart:
    """A run is about to start; what `Hook.before_run` gets.

    `run_id` is a new `uuid4` hex for each run, shared by all of its events.
    `outputs` are the requested outputs, each once, in request order. `inputs` and
    `overrides` are read-only views of the values given. `nodes` are the nodes this
    run will report, in the order it reports them.
    """

    run_id: str
    outputs: tuple[str, ...]
    inputs: Mapping[str, object]
    overrides: Mapping[str, object]
    nodes: tuple[str, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class NodeStart:
    """A node is about to be called; what `Hook.before_node` gets.

    `node` describes it. `inputs` is a read-only view of the keyword arguments its
    function gets from other nodes and inputs, by parameter name: a parameter left
    to its default is absent, and so is one bound to a `value()`. `cache` is the
    node's cache outcome, `None` on a driver with no cache.
    """

    run_id: str
    node: NodeView
    inputs: Mapping[str, object] | None
    cache: CacheOutcome | None


@dataclass(frozen=True, slots=True, kw_only=True)
class NodeEnd:
    """A node returned or raised; what `Hook.after_node` gets.

    `NodeStart`'s fields, then whether it succeeded: `result` is what it returned,
    `None` unless `ok`, and `error` the very exception it raised, `None` if `ok`.
    `checks` are its output checks' results, in the order they ran, passed or
    not: `()` for a node with none, a cache hit, or a node that raised before
    its checks ran (Q99).
    """

    run_id: str
    node: NodeView
    inputs: Mapping[str, object] | None
    cache: CacheOutcome | None
    ok: bool
    result: object
    error: BaseException | None
    checks: tuple[CheckResult, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class RunEnd:
    """A run ended; what `Hook.after_run` gets.

    `RunStart`'s fields, then whether it succeeded. `error` is what stopped it:
    the node's exception, a failed check's, or what the hooks raised, grouped as
    `execute` raises them unless an `after_run` hook raises too. `results` is a
    read-only view of the raw outputs in request order, before any result
    builder: `None` when no outputs were computed, and there even when only
    the result builder failed.
    """

    run_id: str
    outputs: tuple[str, ...]
    inputs: Mapping[str, object]
    overrides: Mapping[str, object]
    nodes: tuple[str, ...]
    ok: bool
    error: BaseException | None
    results: Mapping[str, object] | None


@dataclass(frozen=True, slots=True, kw_only=True)
class BuildEnd:
    """A driver was built; what `Hook.after_build` gets.

    `nodes` are the built graph's views, inputs included; `config` is the
    config it was built with, read-only; `modules` are the modules it was built
    from, each once, in build order.
    """

    nodes: NodeViews
    config: Mapping[str, object]
    modules: tuple[ModuleType, ...]


class Hook:
    """Watches a driver: subclass it and override any of its five methods.

    Each method is a no-op here, and only the methods a subclass overrides are
    called. Each gets one event, by position. Give hooks to `Builder.with_hooks`;
    every event calls them in the order given. A hook that raises stops the run
    (or the build, for `after_build`) once every hook has seen the event (see the
    hooks guide).
    """

    __slots__ = ()

    def after_build(self, event: BuildEnd) -> None:
        """Called once a driver is built; raising refuses the build."""

    def before_run(self, event: RunStart) -> None:
        """Called once a run's request is known good, before its values are checked."""

    def before_node(self, event: NodeStart) -> None:
        """Called before a node's function is."""

    def after_node(self, event: NodeEnd) -> None:
        """Called after a node's function returned or raised."""

    def after_run(self, event: RunEnd) -> None:
        """Called once the run ended, whether or not it succeeded."""


_METHODS: Final = (
    "after_build",
    "before_run",
    "before_node",
    "after_node",
    "after_run",
)


def _overridden(hook: Hook, name: str) -> bool:
    """Whether `hook`'s class overrides `Hook`'s method `name`."""
    return getattr(type(hook), name) is not getattr(Hook, name)


def _method_problem(hook: Hook, name: str) -> str | None:
    """Why `hook`'s overridden method `name` can't be called with one event, if so."""
    owner = type(hook).__name__
    method = getattr(hook, name)
    if not callable(method):
        return f"{owner}.{name} is not callable"
    if inspect.iscoroutinefunction(method) or inspect.isasyncgenfunction(method):
        return f"{owner}.{name} is an async def: a hook method must be a plain def"
    try:
        signature = inspect.signature(method)
    except TypeError, ValueError:
        # No signature to read, as for some builtins: it's called and seen.
        return None
    try:
        signature.bind(None)
    except TypeError:
        return f"{owner}.{name} must take the event as one positional argument"
    return None


def checked(hooks: Sequence[object]) -> tuple[Hook, ...]:
    """`hooks` as a tuple, once each is a `Hook` whose methods take one event.

    A `TypeError` naming every object that is no `Hook` and every overridden method
    that can't take one positional event or is an `async def`, sorted; else a
    `ValueError` naming each hook given twice.
    """
    problems: list[str] = []
    for hook in hooks:
        if not isinstance(hook, Hook):
            if isinstance(hook, type) and issubclass(hook, Hook):
                name = hook.__name__
                problems.append(
                    f"{name} is a Hook class, not a hook: give an instance, {name}()"
                )
            else:
                problems.append(f"{type(hook).__name__} is not a fnloom.Hook")
            continue
        problems.extend(
            found
            for name in _METHODS
            if _overridden(hook, name)
            and (found := _method_problem(hook, name)) is not None
        )
    if problems:
        raise TypeError("\n".join(sorted(problems)))
    seen: set[int] = set()
    twice: set[str] = set()
    for hook in hooks:
        if id(hook) in seen:
            twice.add(f"the same {type(hook).__name__} is given twice")
        seen.add(id(hook))
    if twice:
        raise ValueError("\n".join(sorted(twice)))
    return tuple(hook for hook in hooks if isinstance(hook, Hook))


def fire_build(hooks: Sequence[Hook], event: BuildEnd) -> None:
    """Calls each hook's `after_build` with `event`, then raises what they raised.

    Every hook sees the event. One error is raised as itself, several as one
    group, in hook order (Q23).
    """
    errors: list[BaseException] = []
    for hook in hooks:
        if not _overridden(hook, "after_build"):
            continue
        try:
            hook.after_build(event)
        except BaseException as error:  # noqa: BLE001  # as every event: raised below, with the others
            errors.append(error)
    if errors:
        raise _combined(errors, None, failed=False)


class Hooks:
    """A driver's hooks, checked, with the methods each event calls, in order.

    Only overridden methods are kept, so a base method is never called.
    """

    __slots__ = ("after_node", "after_run", "before_node", "before_run", "nodes")

    def __init__(self, hooks: tuple[Hook, ...]) -> None:
        """Collects the overridden methods of `hooks`, already `checked`."""
        self.before_run: tuple[Callable[[RunStart], object], ...] = tuple(
            h.before_run for h in hooks if _overridden(h, "before_run")
        )
        self.before_node: tuple[Callable[[NodeStart], object], ...] = tuple(
            h.before_node for h in hooks if _overridden(h, "before_node")
        )
        self.after_node: tuple[Callable[[NodeEnd], object], ...] = tuple(
            h.after_node for h in hooks if _overridden(h, "after_node")
        )
        self.after_run: tuple[Callable[[RunEnd], object], ...] = tuple(
            h.after_run for h in hooks if _overridden(h, "after_run")
        )
        # Whether a run needs node events at all.
        self.nodes = bool(self.before_node or self.after_node)

    @classmethod
    def of(cls, hooks: tuple[Hook, ...]) -> Hooks | None:
        """The hooks to call, or `None` when no hook overrides any method."""
        made = cls(hooks)
        if made.nodes or made.before_run or made.after_run:
            return made
        return None


type Keyed = Callable[[Mapping[str, object]], dict[str, object]]


class Views:
    """The nodes a plan reports: each one's view, and how its inputs are keyed.

    Made once per prepared plan. `names` are the nodes in the order a sync run
    reports them.
    """

    __slots__ = ("_keyed", "_views", "names")

    def __init__(self, names: Sequence[str], views: Mapping[str, NodeView]) -> None:
        """The views of `names`, looked up in `views`, each with its re-keying."""
        self.names = tuple(names)
        self._views = {name: views[name] for name in self.names}
        # Only a node the core calls through `_payload`'s wrapper needs it.
        self._keyed: dict[str, Keyed] = {
            name: keyed
            for name, view in self._views.items()
            if (keyed := by_parameter(view.params)) is not None
        }

    def view(self, name: str) -> NodeView:
        """The view of reported node `name`."""
        return self._views[name]

    def inputs(
        self, name: str, kwargs: Mapping[str, object] | None
    ) -> Mapping[str, object] | None:
        """A read-only view of node `name`'s `kwargs`, keyed by parameter name."""
        if kwargs is None:
            return None
        keyed = self._keyed.get(name)
        return MappingProxyType(kwargs if keyed is None else keyed(kwargs))


def _combined(
    errors: Sequence[BaseException], node: str | None, *, failed: bool
) -> BaseException:
    """One error as itself; several as one group, in the order given.

    `node` names the node whose error leads, and `failed` says that a check or
    the core raised the first one.
    """
    if len(errors) == 1:
        return errors[0]
    if node is not None:
        message = f"node '{node}' failed and hooks raised"
    elif failed:
        message = "the run failed and hooks raised"
    else:
        message = "hooks raised"
    return BaseExceptionGroup(message, list(errors))


_NO_VALUES: Final[Mapping[str, object]] = MappingProxyType({})


class Dispatch:
    """One run's events, sent to every hook; made for each run.

    Each event calls every hook's method, collecting what they raise, then raises
    those errors: one as itself, several as a group. `end` raises everything that
    stopped the run, as `execute` raises it: the node's own error first, if a node
    failed, then the hook errors in event order, and within an event in hook order.
    """

    __slots__ = (
        "_errors",
        "_failed_node",
        "_hooks",
        "_kwargs",
        "_node_error",
        "_owner",
        "_results",
        "_start",
        "_started",
        "_token",
        "_views",
        "outcomes",
    )

    def __init__(
        self,
        hooks: Hooks,
        views: Views,
        outcomes: dict[str, CacheOutcome] | None,
        *,
        checks_owner: object = None,
    ) -> None:
        """A run of `hooks`, reporting the nodes of `views`.

        `checks_owner` is the token the driver's output checks report their
        results under; a check of another driver, run inside a node, can't.

        `outcomes` are the nodes' cache outcomes, `None` on a driver with no cache.
        A cached run fills it, level by level, as `loaded` hears of each level.
        """
        self._hooks = hooks
        self._views = views
        self.outcomes = outcomes
        self._owner = checks_owner
        self._start: RunStart | None = None
        # The hook errors so far, in event order, then hook order.
        self._errors: list[BaseException] = []
        self._node_error: BaseException | None = None
        self._failed_node: str | None = None
        # The last node started and its inputs, so its end needn't key them again.
        self._kwargs: Mapping[str, object] | None = None
        self._started: Mapping[str, object] | None = None
        # Each checked node's check results, collected while the run is on.
        self._results: dict[str, tuple[CheckResult, ...]] | None = None
        self._token: contextvars.Token[_checks.Sink | None] | None = None

    def observers(self) -> tuple[_core.ObserverBase, ...]:
        """What the core run is given: one observer when node events are wanted."""
        return (_Forward(self),) if self._hooks.nodes else ()

    @property
    def refused(self) -> bool:
        """Whether a hook has raised in this run, so the value just in is dropped."""
        return bool(self._errors)

    def loaded(self, begun: Begun) -> None:
        """Takes a cached run level's outcomes, and reports its hits, in plan order.

        Each hit gets `before_node` and `after_node`, with no inputs, since nothing
        upstream was loaded, and its loaded value as its result.
        """
        outcomes = self.outcomes
        reported = begun.outcomes
        if outcomes is None or reported is None:
            msg = "a cached run's level was reported to an uncached run's dispatcher"
            raise RuntimeError(msg)
        outcomes.update(reported)
        hits = begun.hits
        for name in begun.loaded:
            outcome = reported[name]
            self.node_started(name, None, outcome)
            self.node_ended(name, None, outcome, hits[name], None)

    def _raised(self, found: list[BaseException]) -> None:
        """Records an event's hook errors, then raises them, if any."""
        if found:
            self._errors.extend(found)
            raise _combined(found, None, failed=False)

    def start(
        self,
        outputs: tuple[str, ...],
        inputs: Mapping[str, object] | None,
        overrides: Mapping[str, object] | None,
        nodes: tuple[str, ...],
    ) -> None:
        """Fires `before_run`, for the request `outputs`, `inputs` and `overrides`.

        When `after_node` is wanted, the run's checks report their results here.
        """
        if self._token is None:
            # Set even when nothing is collected, so a run enclosing this one
            # never collects this run's checks.
            wanted = bool(self._hooks.after_node) and self._owner is not None
            self._results = {} if wanted else None
            sink = None if self._results is None else (self._owner, self._results)
            self._token = _checks.RESULTS.set(sink)
        start = RunStart(
            run_id=uuid.uuid4().hex,
            outputs=outputs,
            inputs=_NO_VALUES if inputs is None else MappingProxyType(inputs),
            overrides=_NO_VALUES if overrides is None else MappingProxyType(overrides),
            nodes=nodes,
        )
        self._start = start
        found: list[BaseException] = []
        for method in self._hooks.before_run:
            try:
                method(start)
            except BaseException as error:  # noqa: BLE001  # raised once every hook ran
                found.append(error)
        self._raised(found)

    def _run_id(self) -> str:
        start = self._start
        if start is None:
            msg = "a run's node events came before its start"
            raise RuntimeError(msg)
        return start.run_id

    def _inputs(
        self, name: str, kwargs: Mapping[str, object] | None
    ) -> Mapping[str, object] | None:
        if kwargs is not None and kwargs is self._kwargs:
            return self._started
        inputs = self._views.inputs(name, kwargs)
        self._kwargs, self._started = kwargs, inputs
        return inputs

    def node_started(
        self,
        name: str,
        kwargs: Mapping[str, object] | None,
        outcome: CacheOutcome | None,
    ) -> None:
        """Fires `before_node` for node `name`, called with `kwargs`.

        `kwargs` are what the core calls the node with, `None` for a cache hit.
        """
        methods = self._hooks.before_node
        if not methods:
            return
        event = NodeStart(
            run_id=self._run_id(),
            node=self._views.view(name),
            inputs=self._inputs(name, kwargs),
            cache=outcome,
        )
        found: list[BaseException] = []
        for method in methods:
            try:
                method(event)
            except BaseException as error:  # noqa: BLE001  # raised once every hook ran
                found.append(error)
        self._raised(found)

    def node_ended(  # the node, its call and its outcome
        self,
        name: str,
        kwargs: Mapping[str, object] | None,
        outcome: CacheOutcome | None,
        value: object,
        error: BaseException | None,
    ) -> None:
        """Fires `after_node` for node `name`: it returned `value`, or raised `error`.

        Records the error, if any, as the run's node error first.
        """
        if error is not None and self._node_error is None:
            self._node_error, self._failed_node = error, name
        methods = self._hooks.after_node
        if not methods:
            return
        ok = error is None
        results = self._results
        event = NodeEnd(
            run_id=self._run_id(),
            node=self._views.view(name),
            inputs=self._inputs(name, kwargs),
            cache=outcome,
            ok=ok,
            result=value if ok else None,
            error=error,
            checks=() if results is None else results.pop(name, ()),
        )
        found: list[BaseException] = []
        for method in methods:
            try:
                method(event)
            except BaseException as failure:  # noqa: BLE001  # raised once every hook ran
                found.append(failure)
        self._raised(found)

    def _release(self) -> None:
        """Stops collecting check results, in the context the run started in."""
        token, self._token = self._token, None
        if token is None:
            return
        try:
            _checks.RESULTS.reset(token)
        except ValueError:
            # Ended in another context than it started in: just stop collecting.
            _checks.RESULTS.set(None)

    def end(
        self,
        ok: bool,  # noqa: FBT001  # the dispatcher's own interface
        error: BaseException | None,
        results: Mapping[str, object] | None,
    ) -> None:
        """Fires `after_run`, then raises whatever stopped the run, and its errors.

        `error` is what the run raised, if anything. When the dispatcher saw no
        error, it is a check's, the core's or the result builder's own, and
        leads; otherwise it is one the dispatcher already holds, and they are
        raised instead. `results` are the raw outputs, or `None` when none were
        computed; a failed run passes them only when its result builder raised.
        """
        self._release()
        lead: list[BaseException] = []
        if self._node_error is not None:
            lead.append(self._node_error)
        elif error is not None and not self._errors:
            lead.append(error)
        # A check or the core raised what leads, rather than a node.
        failed = bool(lead) and self._node_error is None
        failures = [*lead, *self._errors]
        stopped = (
            _combined(failures, self._failed_node, failed=failed) if failures else None
        )
        start = self._start
        if start is None:
            msg = "a run ended before it started"
            raise RuntimeError(msg)
        methods = self._hooks.after_run
        found: list[BaseException] = []
        if methods:
            succeeded = ok and stopped is None
            event = RunEnd(
                run_id=start.run_id,
                outputs=start.outputs,
                inputs=start.inputs,
                overrides=start.overrides,
                nodes=start.nodes,
                ok=succeeded,
                error=stopped,
                # The raw outputs, unless a hook failed a run that computed them.
                results=None
                if results is None or (ok and not succeeded)
                else MappingProxyType(results),
            )
            for method in methods:
                try:
                    method(event)
                except BaseException as failure:  # noqa: BLE001  # raised once every hook ran
                    found.append(failure)
        if not found:
            if stopped is not None:
                raise stopped
            return
        raise _combined([*failures, *found], self._failed_node, failed=failed)


class _Forward(_core.ObserverBase):
    """The core observer of a hooked run: sends each node event to its `Dispatch`."""

    def __init__(self, dispatch: Dispatch) -> None:
        """Forwards to `dispatch`."""
        self._dispatch = dispatch
        self._outcomes = dispatch.outcomes
        self._kwargs: Mapping[str, object] | None = None

    @override
    def pre_node(self, node: str, kwargs: dict[str, object]) -> None:
        # The core runs one node at a time, so its end is the next `post_node`.
        self._kwargs = kwargs
        outcomes = self._outcomes
        self._dispatch.node_started(
            node, kwargs, None if outcomes is None else outcomes.get(node)
        )

    @override
    def post_node(
        self, node: str, *, ok: bool, value: object, error: BaseException | None
    ) -> None:
        outcomes = self._outcomes
        self._dispatch.node_ended(
            node,
            self._kwargs,
            None if outcomes is None else outcomes.get(node),
            value,
            error,
        )


_CUT: Final = 50


def _unrepresentable(value: object) -> str:
    """What a value whose `repr` raises shows as."""
    return f"<unrepresentable {type(value).__qualname__}>"


class _Bounded(reprlib.Repr):
    """A `repr` bounded before the cut, so a huge input is never shown in full.

    Each container shows its first 20 items, four levels deep, and a string or
    other value its first 73 characters, more than the cut keeps. A `repr` that
    raises, even inside a container, shows as `<unrepresentable TypeName>`.
    """

    def __init__(self, cut: int = _CUT) -> None:
        super().__init__(
            maxlevel=4,
            maxtuple=20,
            maxlist=20,
            maxarray=20,
            maxdict=20,
            maxset=20,
            maxfrozenset=20,
            maxdeque=20,
            maxstring=cut * 3,
            maxlong=cut * 3,
            maxother=cut * 3,
        )

    @override
    def repr_instance(self, x: object, level: int) -> str:
        try:
            text = repr(x)
        except Exception:  # noqa: BLE001  # any `__repr__` may raise; the node's error leads
            return _unrepresentable(x)
        if len(text) > self.maxother:
            # As `reprlib` cuts it: the start and end, with `...` between.
            i = (self.maxother - 3) // 2
            j = self.maxother - 3 - i
            text = f"{text[:i]}{self.fillvalue}{text[len(text) - j :]}"
        return text


@functools.cache
def _bounded(cut: int) -> _Bounded:
    """The bounded `repr` for values cut to `cut` characters."""
    return _Bounded(cut)


def _cut(value: object, width: int = _CUT) -> str:
    """`value`'s `repr`, cut to `width` (50) characters and marked with `...`.

    The `repr` is bounded first, so a huge input is not shown in full only to be
    cut. One that raises shows as `<unrepresentable TypeName>`, so logging a
    failure never replaces the node's error.
    """
    try:
        text = _bounded(width).repr(value)
    except Exception:  # noqa: BLE001  # any `__repr__` may raise; the node's error leads
        return _unrepresentable(value)
    return text if len(text) <= width else f"{text[:width]}..."


class LogFailures(Hook):
    """Logs each node that raised, with its inputs and its exception.

    Logs nothing on success or for a cache hit. The record's message names the
    node and lists its inputs sorted by name, `name=repr`, each `repr` cut to 50
    characters and ended with `...` when it was; the node's error is the record's
    `exc_info`. A node without inputs shows `inputs: none`.
    """

    __slots__ = ("_level", "_logger")

    def __init__(
        self, logger: logging.Logger | None = None, *, level: int = logging.ERROR
    ) -> None:
        """Logs to `logger`, by default the `fnloom` logger, at `level`."""
        self._logger = logging.getLogger("fnloom") if logger is None else logger
        self._level = level

    @override
    def after_node(self, event: NodeEnd) -> None:
        if event.ok:
            return
        inputs = event.inputs
        shown = (
            "none"
            if inputs is None
            else ", ".join(
                f"{name}={_cut(value)}" for name, value in sorted(inputs.items())
            )
            or "none"
        )
        error = event.error
        self._logger.log(
            self._level,
            "node '%s' failed; inputs: %s",
            event.node.name,
            shown,
            exc_info=None
            if error is None
            else (type(error), error, error.__traceback__),
        )


_VERBOSE: Final = 2

type NodeFilter = Sequence[str] | Callable[[NodeView], bool]


type _State = tuple[int, Callable[[str], object], NodeFilter | None, int]


class PrintNodes(Hook):
    """Prints a line for each node a run calls; verbosity 2 adds inputs and result.

    `verbosity=1` prints `node 'x' ran in 1.2 ms`, `node 'x' failed in 0.4 ms:
    ValueError('no')`, or `node 'x' loaded from cache` for a cache hit.
    `verbosity=2` adds `  inputs: a=1, b=2` (sorted by name) and `  result: ...`,
    each value cut to `width` characters as `LogFailures` cuts them. `nodes`
    limits what is printed to those names, or to the nodes a callable accepts
    of their `NodeView`; a name that is no node is a `ValueError` when the
    driver is built. `print_fn` takes one string. Times are kept per run and
    node, so concurrent runs and the async driver stay correct.
    """

    __slots__ = ("_lock", "_nodes", "_print", "_started", "_verbosity", "_width")

    def __init__(
        self,
        verbosity: int = 1,
        *,
        print_fn: Callable[[str], object] = print,
        nodes: NodeFilter | None = None,
        width: int = _CUT,
    ) -> None:
        """A hook printing with `print_fn`; `ValueError` for a bad setting."""
        if isinstance(verbosity, bool) or verbosity not in (1, 2):
            msg = f"verbosity must be 1 or 2, got {verbosity!r}"
            raise ValueError(msg)
        if isinstance(width, bool) or not isinstance(width, int) or width < 10:  # noqa: PLR2004  # the shortest width a cut `...` makes sense at
            msg = f"width must be an int of at least 10, got {width!r}"
            raise ValueError(msg)
        if isinstance(nodes, str) or (
            not callable(nodes)
            and nodes is not None
            and not all(isinstance(n, str) for n in nodes)
        ):
            msg = f"nodes must be a sequence of names or a callable, not {nodes!r}"
            raise TypeError(msg)
        self._verbosity = verbosity
        self._print = print_fn
        self._nodes: NodeFilter | None = (
            nodes if callable(nodes) or nodes is None else tuple(nodes)
        )
        self._width = width
        self._started: dict[tuple[str, str], float] = {}
        self._lock = threading.Lock()

    @override
    def __getstate__(
        self,
    ) -> tuple[int, Callable[[str], object], NodeFilter | None, int]:
        """The settings: a lock and a run's start times are not pickled."""
        return (self._verbosity, self._print, self._nodes, self._width)

    def __setstate__(self, state: _State) -> None:
        """Restores the settings, with no run in flight."""
        self._verbosity, self._print, self._nodes, self._width = state
        self._started = {}
        self._lock = threading.Lock()

    @override
    def after_build(self, event: BuildEnd) -> None:
        names = self._nodes
        if names is None or callable(names):
            return
        unknown = sorted(
            n for n in names if n not in event.nodes or event.nodes[n].is_input
        )
        if unknown:
            listed = ", ".join(f"'{n}'{suggest(n, event.nodes)}" for n in unknown)
            msg = f"PrintNodes: not computed nodes of this graph: {listed}"
            raise ValueError(msg)

    def _shown(self, view: NodeView) -> bool:
        wanted = self._nodes
        if wanted is None:
            return True
        if callable(wanted):
            return bool(wanted(view))
        return view.name in wanted

    @override
    def after_run(self, event: RunEnd) -> None:
        # A node whose `before_node` another hook refused, or that was cancelled,
        # has no `after_node`: drop what this run left.
        with self._lock:
            for key in [k for k in self._started if k[0] == event.run_id]:
                del self._started[key]

    @override
    def before_node(self, event: NodeStart) -> None:
        if not self._shown(event.node):
            return
        with self._lock:
            self._started[event.run_id, event.node.name] = time.perf_counter()

    @override
    def after_node(self, event: NodeEnd) -> None:
        if not self._shown(event.node):
            return
        name = event.node.name
        with self._lock:
            began = self._started.pop((event.run_id, name), None)
        elapsed = "?" if began is None else _milliseconds(time.perf_counter() - began)
        cache = event.cache
        if event.ok and cache is not None and cache.status == "hit":
            self._print(f"node '{name}' loaded from cache")
        elif event.ok:
            self._print(f"node '{name}' ran in {elapsed}")
        else:
            self._print(
                f"node '{name}' failed in {elapsed}: {_cut(event.error, self._width)}"
            )
        if self._verbosity < _VERBOSE:
            return
        inputs = event.inputs
        shown = (
            "none"
            if inputs is None or len(inputs) == 0
            else ", ".join(
                f"{key}={_cut(value, self._width)}"
                for key, value in sorted(inputs.items())
            )
        )
        self._print(f"  inputs: {shown}")
        if event.ok:
            self._print(f"  result: {_cut(event.result, self._width)}")


def _milliseconds(seconds: float) -> str:
    """`seconds` as `1.2 ms`, to one decimal."""
    return f"{seconds * 1000:.1f} ms"
