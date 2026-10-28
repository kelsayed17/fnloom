"""User hooks: `Hook`, its events, the checks when attached, and the dispatcher.

A run's events go through one `Dispatch`, made for that run. The sync driver feeds
it node events from the core through `_Forward`, a private core observer; the
front end fires the run events itself, around the value checks and the core run.
"""

import inspect
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, override

from fnloom import _core

# Imported for real, not under TYPE_CHECKING: the events' signatures annotate with
# them, and `inspect.signature` resolves those annotations at run time.
from fnloom._caching import CacheOutcome
from fnloom._scan import by_parameter
from fnloom._view import NodeView

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
    """

    run_id: str
    node: NodeView
    inputs: Mapping[str, object] | None
    cache: CacheOutcome | None
    ok: bool
    result: object
    error: BaseException | None


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


class Hook:
    """Watches a driver's runs: subclass it and override any of its four methods.

    Each method is a no-op here, and only the methods a subclass overrides are
    called. Each gets one event, by position. Give hooks to `Builder.with_hooks`;
    every event calls them in the order given. A hook that raises stops the run
    once every hook has seen the event (see the hooks guide).
    """

    __slots__ = ()

    def before_run(self, event: RunStart) -> None:
        """Called once a run's request is known good, before its values are checked."""

    def before_node(self, event: NodeStart) -> None:
        """Called before a node's function is."""

    def after_node(self, event: NodeEnd) -> None:
        """Called after a node's function returned or raised."""

    def after_run(self, event: RunEnd) -> None:
        """Called once the run ended, whether or not it succeeded."""


_METHODS: Final = ("before_run", "before_node", "after_node", "after_run")


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
        "_start",
        "_started",
        "_views",
        "outcomes",
    )

    def __init__(
        self,
        hooks: Hooks,
        views: Views,
        outcomes: dict[str, CacheOutcome] | None,
    ) -> None:
        """A run of `hooks`, reporting the nodes of `views`.

        `outcomes` are the nodes' cache outcomes, `None` on a driver with no cache.
        A cached run fills it, level by level, as `loaded` hears of each level.
        """
        self._hooks = hooks
        self._views = views
        self.outcomes = outcomes
        self._start: RunStart | None = None
        # The hook errors so far, in event order, then hook order.
        self._errors: list[BaseException] = []
        self._node_error: BaseException | None = None
        self._failed_node: str | None = None
        # The last node started and its inputs, so its end needn't key them again.
        self._kwargs: Mapping[str, object] | None = None
        self._started: Mapping[str, object] | None = None

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
        """Fires `before_run`, for the request `outputs`, `inputs` and `overrides`."""
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
        event = NodeEnd(
            run_id=self._run_id(),
            node=self._views.view(name),
            inputs=self._inputs(name, kwargs),
            cache=outcome,
            ok=ok,
            result=value if ok else None,
            error=error,
        )
        found: list[BaseException] = []
        for method in methods:
            try:
                method(event)
            except BaseException as failure:  # noqa: BLE001  # raised once every hook ran
                found.append(failure)
        self._raised(found)

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
