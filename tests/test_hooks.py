"""`fnloom.Hook` on the sync driver: its events, its order, and hooks that raise."""

import copy
import pickle
from dataclasses import FrozenInstanceError
from typing import Any, cast, override

import pytest

import fnloom
import picklable_nodes
from fnloom import Hook, NodeEnd, NodeStart, RunEnd, RunStart
from helpers import make_module

GRAPH = """
def a(x: int) -> int:
    return x + 1

def b(a: int, y: int = 2) -> int:
    return a + y

def c(b: int) -> int:
    return b * 10

def unused(x: int) -> int:
    return x
"""

FAILING = """
def a(x: int) -> int:
    return x + 1

def boom(a: int) -> int:
    raise ValueError(f"bad {a}")

def after(boom: int) -> int:
    return boom
"""

type Entry = tuple[str, str, str | None]


class HookError(Exception):
    """What a `Recorder` raises when told to."""


class Recorder(Hook):
    """Records each event it gets as `(name, stage, node)`, and keeps the event.

    `raise_on` names the stages, each with its node or `None` for a run stage or
    any node, where it raises a `HookError` after recording.
    """

    def __init__(
        self,
        name: str = "r",
        log: list[Entry] | None = None,
        raise_on: tuple[tuple[str, str | None], ...] = (),
    ) -> None:
        """A recorder called `name`, appending to `log` (its own when not given)."""
        self.name = name
        self.log: list[Entry] = [] if log is None else log
        self.events: list[object] = []
        self.raise_on = raise_on

    def _record(self, stage: str, event: object, node: str | None) -> None:
        self.log.append((self.name, stage, node))
        self.events.append(event)
        for where, at in self.raise_on:
            if where == stage and at in {None, node}:
                msg = f"{self.name} {stage} {node}"
                raise HookError(msg)

    @override
    def before_run(self, event: RunStart) -> None:
        self._record("before_run", event, None)

    @override
    def before_node(self, event: NodeStart) -> None:
        self._record("before_node", event, event.node.name)

    @override
    def after_node(self, event: NodeEnd) -> None:
        self._record("after_node", event, event.node.name)

    @override
    def after_run(self, event: RunEnd) -> None:
        self._record("after_run", event, None)

    def of(self, kind: type[object]) -> list[Any]:
        """The events of class `kind` it got, in order."""
        return [e for e in self.events if isinstance(e, kind)]


def _driver(source: str = GRAPH, *hooks: Hook) -> fnloom.Driver:
    module = make_module("hooked_graph", source)
    return fnloom.Builder().with_modules(module).with_hooks(*hooks).build()


def _stages(log: list[Entry]) -> list[tuple[str, str | None]]:
    return [(stage, node) for _, stage, node in log]


def test_hooks_see_four_events_in_order_with_their_fields() -> None:
    """A run, then each node in run order, then the end of the run, all filled in."""
    hook = Recorder()
    driver = _driver(GRAPH, hook)
    result = driver.execute(["c", "a"], inputs={"x": 1})
    assert result == {"c": 40, "a": 2}
    assert _stages(hook.log) == [
        ("before_run", None),
        ("before_node", "a"),
        ("after_node", "a"),
        ("before_node", "b"),
        ("after_node", "b"),
        ("before_node", "c"),
        ("after_node", "c"),
        ("after_run", None),
    ]
    start, end = hook.of(RunStart)[0], hook.of(RunEnd)[0]
    assert isinstance(start.run_id, str)
    assert len(start.run_id) == 32  # noqa: PLR2004  # a uuid4 hex
    assert start.outputs == ("c", "a")
    assert dict(start.inputs) == {"x": 1}
    assert dict(start.overrides) == {}
    assert start.nodes == ("a", "b", "c")
    assert (end.run_id, end.outputs, end.nodes) == (
        start.run_id,
        ("c", "a"),
        start.nodes,
    )
    assert dict(end.inputs) == {"x": 1}
    assert (end.ok, end.error) == (True, None)
    assert end.results is not None
    assert list(end.results.items()) == [("c", 40), ("a", 2)]
    starts, ends = hook.of(NodeStart), hook.of(NodeEnd)
    assert [s.node for s in starts] == [driver.nodes[n] for n in ("a", "b", "c")]
    assert {s.run_id for s in (*starts, *ends)} == {start.run_id}
    assert [dict(s.inputs) for s in starts] == [{"x": 1}, {"a": 2}, {"b": 4}]
    assert all(s.cache is None for s in (*starts, *ends))
    assert [(e.ok, e.result, e.error) for e in ends] == [
        (True, 2, None),
        (True, 4, None),
        (True, 40, None),
    ]
    assert [dict(e.inputs) for e in ends] == [{"x": 1}, {"a": 2}, {"b": 4}]
    # A second run is another run.
    driver.execute(["a"], inputs={"x": 1})
    assert hook.of(RunStart)[1].run_id != start.run_id


def test_events_are_frozen_and_their_mappings_read_only() -> None:
    """No hook can change an event, a node's arguments, or the run's result."""
    seen: list[object] = []

    class Meddler(Hook):
        @override
        def before_run(self, event: RunStart) -> None:
            with pytest.raises(TypeError):
                cast("dict[str, object]", event.inputs)["x"] = 100
            with pytest.raises(FrozenInstanceError):
                event.run_id = "x"  # ty: ignore[invalid-assignment]  # the point

        @override
        def before_node(self, event: NodeStart) -> None:
            assert event.inputs is not None
            with pytest.raises(TypeError):
                cast("dict[str, object]", event.inputs)["a"] = 100
            seen.append(event)

        @override
        def after_run(self, event: RunEnd) -> None:
            assert event.results is not None
            with pytest.raises(TypeError):
                cast("dict[str, object]", event.results)["c"] = 0

    driver = _driver(GRAPH, Meddler())
    assert driver.execute(["c"], inputs={"x": 1}) == {"c": 40}
    assert len(seen) == 3  # noqa: PLR2004  # a, b and c


def test_inputs_overrides_and_unneeded_nodes_get_no_events() -> None:
    """Only the nodes the run computes report: no input, override or unused node."""
    hook = Recorder()
    driver = _driver(GRAPH, hook)
    assert driver.execute(["x", "c"], inputs={"x": 1}, overrides={"b": 5}) == {
        "x": 1,
        "c": 50,
    }
    assert _stages(hook.log) == [
        ("before_run", None),
        ("before_node", "c"),
        ("after_node", "c"),
        ("after_run", None),
    ]
    start = hook.of(RunStart)[0]
    assert start.nodes == ("c",)
    assert dict(start.overrides) == {"b": 5}


def test_a_failing_node_ends_the_run_with_its_own_error() -> None:
    """No later node starts; `after_node` and `after_run` get the very error."""
    hook = Recorder()
    driver = _driver(FAILING, hook)
    with pytest.raises(ValueError, match="bad 2") as raised:
        driver.execute(["after"], inputs={"x": 1})
    assert _stages(hook.log) == [
        ("before_run", None),
        ("before_node", "a"),
        ("after_node", "a"),
        ("before_node", "boom"),
        ("after_node", "boom"),
        ("after_run", None),
    ]
    failed = hook.of(NodeEnd)[1]
    assert (failed.ok, failed.result) == (False, None)
    assert failed.error is raised.value
    end = hook.of(RunEnd)[0]
    assert (end.ok, end.error, end.results) == (False, raised.value, None)
    assert raised.value.__notes__ == ["in node 'boom'"]


SHAPES = """
from fnloom import extract_fields, group, parameterize, source, value
from typing import TypedDict


class Pair(TypedDict):
    first: int
    second: int


def a(x: int) -> int:
    return x + 1


@parameterize(scaled={"k": value(3)})
def scale(a: int, k: int, m: int = 1) -> int:
    return a * k * m


@parameterize(twice={"p": source("a"), "q": source("a")})
def both(p: int, q: int, y: int = 2) -> int:
    return p * q + y


@parameterize(
    listed={"items": group(source("a"), source("x"), value(7)), "m": source("x")},
    keyed={"named": group(low=source("x"), fixed=value(9)), "m": source("x")},
)
def gathered(
    items: list[int] | None = None, named: dict[str, int] | None = None, m: int = 0
) -> int:
    return m


@extract_fields()
def pair(a: int) -> Pair:
    return {"first": a, "second": 2}
"""


def test_node_inputs_are_by_parameter_name_without_defaults() -> None:
    """Keyed by parameter, renames and namespaces applied; no constant or default."""
    hook = Recorder()
    shapes = make_module("hooked_shapes", SHAPES)
    driver = (
        fnloom.Builder()
        .with_modules(shapes)
        .with_modules(make_module("hooked_spaced", GRAPH), namespace="eu")
        .with_hooks(hook)
        .build()
    )
    driver.execute(
        ["scaled", "twice", "listed", "keyed", "first", "eu.c"],
        inputs={"x": 1},
    )
    inputs = {s.node.name: dict(s.inputs) for s in hook.of(NodeStart)}
    assert inputs == {
        "a": {"x": 1},
        # A `value()` constant and an unsupplied default are absent.
        "scaled": {"a": 2},
        # One source feeding two parameters.
        "twice": {"p": 2, "q": 2},
        # A group is the list or dict the function gets.
        "listed": {"items": [2, 1, 7], "m": 1},
        "keyed": {"named": {"low": 1, "fixed": 9}, "m": 1},
        "pair": {"a": 2},
        "first": {"pair": {"first": 2, "second": 2}},
        # A namespace's parameter reads `eu.a` under its own name.
        "eu.a": {"x": 1},
        "eu.b": {"a": 2},
        "eu.c": {"b": 4},
    }
    assert [e.node.name for e in hook.of(NodeEnd)] == list(inputs)


def test_hooks_run_in_the_order_given_for_every_event() -> None:
    """Each event calls the hooks in order, after-events too; `with_hooks` adds."""
    log: list[Entry] = []
    first, second, third = (Recorder(n, log) for n in ("A", "B", "C"))
    module = make_module("hooked_order", GRAPH)
    builder = fnloom.Builder().with_modules(module).with_hooks(first, second)
    driver = builder.with_hooks(third).build()
    assert driver.hooks == (first, second, third)
    assert builder.build().hooks == (first, second)
    driver.execute(["a"], inputs={"x": 1})
    assert log == [
        (name, stage, node)
        for stage, node in (
            ("before_run", None),
            ("before_node", "a"),
            ("after_node", "a"),
            ("after_run", None),
        )
        for name in ("A", "B", "C")
    ]


def test_before_run_precedes_the_value_checks() -> None:
    """A request's shape is checked before any hook; its values after `before_run`."""
    hook = Recorder()
    driver = _driver(GRAPH, hook)
    for bad, error in (
        ({"final_vars": ["nope"], "inputs": {"x": 1}}, "unknown outputs"),
        ({"final_vars": ["a"], "inputs": {"typo": 1}}, "unknown inputs"),
        ({"final_vars": "a"}, "final_vars must be a sequence"),
    ):
        with pytest.raises((ValueError, TypeError), match=error):
            driver.execute(**cast("Any", bad))
    assert hook.log == []
    with pytest.raises(ValueError, match="missing inputs: 'x'") as missing:
        driver.execute(["c"])
    with pytest.raises(ValueError, match="x: expected int, got str") as mistyped:
        driver.execute(["c"], inputs={"x": "1"})
    assert _stages(hook.log) == [("before_run", None), ("after_run", None)] * 2
    ends = hook.of(RunEnd)
    assert [(e.ok, e.error, e.results) for e in ends] == [
        (False, missing.value, None),
        (False, mistyped.value, None),
    ]
    # No node runs after a failed value check, so neither run lists any (R5).
    starts = hook.of(RunStart)
    assert [(s.outputs, dict(s.inputs), s.nodes) for s in starts] == [
        (("c",), {}, ()),
        (("c",), {"x": "1"}, ()),
    ]
    assert [e.nodes for e in ends] == [(), ()]
    # The same request with good values lists the nodes it runs.
    driver.execute(["c"], inputs={"x": 1})
    assert hook.of(RunStart)[-1].nodes == ("a", "b", "c")


def test_strict_overrides_are_checked_after_before_run() -> None:
    """A refused override, Hamilton's missing input, comes after `before_run` too."""
    hook = Recorder()
    module = make_module("hooked_strict", GRAPH)
    driver = (
        fnloom.Builder()
        .with_modules(module)
        .with_strict_overrides()
        .with_hooks(hook)
        .build()
    )
    with pytest.raises(ValueError, match="override of input 'x' refused"):
        driver.execute(["c"], overrides={"x": 1})
    assert _stages(hook.log) == [("before_run", None), ("after_run", None)]


class Narrow(Hook):
    """Overrides `before_node` with a signature that takes no event."""

    @override
    def before_node(self) -> None:  # ty: ignore[invalid-method-override]  # the case under test
        """Takes nothing."""


class KeywordOnly(Hook):
    """Overrides `after_run` with a keyword-only event."""

    @override
    def after_run(self, *, event: RunEnd) -> None:  # ty: ignore[invalid-method-override]  # the case under test
        """Takes the event by keyword only."""


class Awaiting(Hook):
    """Overrides `after_node` with an `async def`."""

    @override
    async def after_node(self, event: NodeEnd) -> None:  # ty: ignore[invalid-method-override]  # the case under test
        """Never awaited by Hamilton's sync driver."""


class Wide(Hook):
    """Takes the event with extra optional parameters, which is fine."""

    @override
    def before_run(self, event: RunStart, *extra: object, flag: bool = False) -> None:
        """Takes more than it needs."""


def test_attach_refuses_non_hooks_duplicates_bad_signatures_and_async_methods() -> None:
    """Each mistake is refused by `with_hooks`, every culprit named in one error."""
    builder = fnloom.Builder()
    with pytest.raises(TypeError) as raised:
        builder.with_hooks(cast("Hook", object()), cast("Any", Recorder))
    assert str(raised.value) == (
        "Recorder is a Hook class, not a hook: give an instance, Recorder()\n"
        "object is not a fnloom.Hook"
    )
    with pytest.raises(TypeError) as raised:
        builder.with_hooks(Narrow(), KeywordOnly(), Awaiting())
    assert str(raised.value) == (
        "Awaiting.after_node is an async def: a hook method must be a plain def\n"
        "KeywordOnly.after_run must take the event as one positional argument\n"
        "Narrow.before_node must take the event as one positional argument"
    )
    hook = Recorder()
    with pytest.raises(ValueError, match=r"^the same Recorder is given twice$"):
        builder.with_hooks(hook, Recorder(), hook)
    with pytest.raises(ValueError, match=r"^the same Recorder is given twice$"):
        builder.with_hooks(hook).with_hooks(hook)
    # Extra optional parameters are fine, and so is a hook overriding nothing.
    assert fnloom.Builder().with_hooks(Wide(), Hook()).build().hooks != ()


def test_only_overridden_methods_are_called(monkeypatch: pytest.MonkeyPatch) -> None:
    """A base method is never called; no node method overridden, no node event made."""

    class RunOnly(Hook):
        def __init__(self) -> None:
            self.ends: list[RunEnd] = []

        @override
        def after_run(self, event: RunEnd) -> None:
            self.ends.append(event)

    hook = RunOnly()
    driver = _driver(GRAPH, hook)
    called: list[str] = []

    def refuse(*_args: object, **_kwargs: object) -> None:
        called.append("made")

    monkeypatch.setattr(Hook, "before_run", refuse)
    monkeypatch.setattr(Hook, "before_node", refuse)
    monkeypatch.setattr(Hook, "after_node", refuse)
    monkeypatch.setattr(fnloom._hooks, "NodeStart", refuse)  # noqa: SLF001  # watching the events made
    monkeypatch.setattr(fnloom._hooks, "NodeEnd", refuse)  # noqa: SLF001  # watching the events made
    assert driver.execute(["c"], inputs={"x": 1}) == {"c": 40}
    assert called == []
    assert len(hook.ends) == 1


def test_every_hook_runs_before_errors_are_raised() -> None:
    """Decision 5: every hook sees the event, then its errors stop the run."""

    def run(*hooks: Recorder) -> BaseException:
        driver = _driver(GRAPH, *hooks)
        with pytest.raises(BaseException) as raised:  # noqa: PT011  # each case checks it
            driver.execute(["c"], inputs={"x": 1})
        return raised.value

    # 1. `before_run` raises: the run stops, and `after_run(ok=False)` still fires.
    log: list[Entry] = []
    a = Recorder("A", log, (("before_run", None),))
    b = Recorder("B", log)
    error = run(a, b)
    assert str(error) == "A before_run None"
    assert log == [
        ("A", "before_run", None),
        ("B", "before_run", None),
        ("A", "after_run", None),
        ("B", "after_run", None),
    ]
    end = b.of(RunEnd)[0]
    assert (end.ok, end.error, end.results) == (False, error, None)

    # 2. `before_node` raises: that node isn't called, and gets no `after_node`.
    log = []
    a = Recorder("A", log, (("before_node", "b"),))
    b = Recorder("B", log)
    error = run(a, b)
    assert str(error) == "A before_node b"
    assert not hasattr(error, "__notes__")
    assert log[-4:] == [
        ("A", "before_node", "b"),
        ("B", "before_node", "b"),
        ("A", "after_run", None),
        ("B", "after_run", None),
    ]
    assert b.of(RunEnd)[0].error is error

    # 3. `after_node(ok=True)` raises: the value is discarded and the run stops.
    log = []
    a = Recorder("A", log, (("after_node", "b"),))
    b = Recorder("B", log)
    error = run(a, b)
    assert str(error) == "A after_node b"
    assert log[-4:] == [
        ("A", "after_node", "b"),
        ("B", "after_node", "b"),
        ("A", "after_run", None),
        ("B", "after_run", None),
    ]
    assert ("A", "before_node", "c") not in log
    assert b.of(RunEnd)[0].results is None

    # 4. `after_run(ok=True)` raises: the error replaces the result.
    log = []
    a = Recorder("A", log, (("after_run", None),))
    b = Recorder("B", log)
    error = run(a, b)
    assert str(error) == "A after_run None"
    assert log[-2:] == [("A", "after_run", None), ("B", "after_run", None)]
    assert b.of(RunEnd)[0].ok is True

    # 5. Several errors: one group, in event order, then hook order.
    log = []
    a = Recorder("A", log, (("before_node", "a"),))
    b = Recorder("B", log, (("before_node", "a"), ("after_run", None)))
    error = run(a, b)
    assert isinstance(error, ExceptionGroup)
    assert str(error) == "hooks raised (3 sub-exceptions)"
    assert [str(e) for e in error.exceptions] == [
        "A before_node a",
        "B before_node a",
        "B after_run None",
    ]
    # `after_run` saw the errors that stopped the run.
    stopped = a.of(RunEnd)[0].error
    assert isinstance(stopped, ExceptionGroup)
    assert list(stopped.exceptions) == list(error.exceptions[:2])


def test_node_failure_with_raising_hooks_is_one_group_led_by_the_node() -> None:
    """Review Focus 1: the node's error first, with its note, then the hooks'."""
    log: list[Entry] = []
    a = Recorder("A", log, (("after_node", "boom"),))
    b = Recorder("B", log, (("after_run", None),))
    driver = _driver(FAILING, a, b)
    with pytest.raises(ExceptionGroup) as raised:
        driver.execute(["after"], inputs={"x": 1})
    group = raised.value
    assert str(group) == "node 'boom' failed and hooks raised (3 sub-exceptions)"
    node_error, after_node, after_run = group.exceptions
    assert isinstance(node_error, ValueError)
    assert str(node_error) == "bad 2"
    assert node_error.__notes__ == ["in node 'boom'"]
    assert node_error is a.of(NodeEnd)[1].error
    assert [str(after_node), str(after_run)] == [
        "A after_node boom",
        "B after_run None",
    ]
    assert log[-4:] == [
        ("A", "after_node", "boom"),
        ("B", "after_node", "boom"),
        ("A", "after_run", None),
        ("B", "after_run", None),
    ]
    # `after_run` got the node's error grouped with the `after_node` hook's.
    stopped = b.of(RunEnd)[0].error
    assert isinstance(stopped, ExceptionGroup)
    assert list(stopped.exceptions) == [node_error, after_node]


def test_a_bare_base_exception_from_a_hook_is_raised_too() -> None:
    """A `BaseException` that is no `Exception` is collected and raised as well."""

    class Interrupting(Hook):
        @override
        def before_node(self, event: NodeStart) -> None:
            raise KeyboardInterrupt

    hook = Recorder()
    driver = _driver(GRAPH, Interrupting(), hook)
    with pytest.raises(KeyboardInterrupt):
        driver.execute(["a"], inputs={"x": 1})
    assert _stages(hook.log) == [
        ("before_run", None),
        ("before_node", "a"),
        ("after_run", None),
    ]


def _round_trip[T](value: T) -> T:
    return cast("T", pickle.loads(pickle.dumps(value)))  # noqa: S301  # our own pickle


def test_hooks_survive_pickling_copy_rebuild_and_build_async() -> None:
    """Every way of carrying a driver keeps its hooks, by Python's own rules."""
    hook = Recorder("P")
    builder = fnloom.Builder().with_modules(picklable_nodes).with_hooks(hook)
    driver = builder.build()
    inputs = {"a": 3, "b": 1}
    assert copy.copy(driver).hooks == (hook,)
    assert driver.rebuild().hooks == (hook,)
    assert builder.build_async().hooks == (hook,)
    assert builder.build_async().rebuild().hooks == (hook,)
    assert copy.copy(builder.build_async()).hooks == (hook,)
    for carried in (
        _round_trip(driver),
        copy.deepcopy(driver),
        _round_trip(builder).build(),
        _round_trip(driver.prepare(["total"], inputs=inputs))._driver,  # noqa: SLF001  # the plan's driver
    ):
        assert carried is not None
        (again,) = carried.hooks
        assert isinstance(again, Recorder)
        assert again is not hook
        assert again.name == "P"
        carried.execute(["total"], inputs=inputs)
        assert _stages(again.log)[0] == ("before_run", None)
    (again,) = _round_trip(builder.build_async()).hooks
    assert isinstance(again, Recorder)
    assert hook.log == []

    class Local(Hook):
        @override
        def before_run(self, event: RunStart) -> None:
            """A local class, which pickle cannot find."""

    unpicklable = fnloom.Builder().with_modules(picklable_nodes).with_hooks(Local())
    with pytest.raises((pickle.PicklingError, AttributeError)):
        pickle.dumps(unpicklable.build())


def test_prepared_plan_runs_fire_the_hooks() -> None:
    """`PreparedPlan.run` fires as `execute` does; a name mismatch fires nothing."""
    hook = Recorder()
    driver = _driver(GRAPH, hook)
    plan = driver.prepare(["c"], inputs={"x": 0})
    assert plan.run(inputs={"x": 1}) == {"c": 40}
    assert plan.run(inputs={"x": 2}) == {"c": 50}
    assert _stages(hook.log).count(("before_run", None)) == 2  # noqa: PLR2004  # two runs
    assert [dict(s.inputs) for s in hook.of(RunStart)] == [{"x": 1}, {"x": 2}]
    hook.log.clear()
    with pytest.raises(ValueError, match="differ from the prepared"):
        plan.run(inputs={"y": 1})
    assert hook.log == []
    with pytest.raises(ValueError, match="x: expected int, got str"):
        plan.run(inputs={"x": "1"})
    assert _stages(hook.log) == [("before_run", None), ("after_run", None)]
    # A failed value check runs no node, so the run lists none (R5).
    assert hook.of(RunStart)[-1].nodes == ()
    assert hook.of(RunEnd)[-1].nodes == ()


def test_a_driver_without_hooks_has_none() -> None:
    """`Driver.hooks` is empty by default, and its plans are plain prepared plans."""
    driver = _driver(GRAPH)
    assert driver.hooks == ()
    assert type(driver.prepare(["a"], inputs={"x": 1})) is fnloom.PreparedPlan


def test_a_cached_driver_fires_the_events_of_the_nodes_it_runs() -> None:
    """With a cache, the nodes the run computes report; then the hit reports alone."""
    hook = Recorder()
    module = make_module("hooked_cached", GRAPH)
    driver = (
        fnloom.Builder()
        .with_modules(module)
        .with_cache(fnloom.MemoryCache())
        .with_hooks(hook)
        .build()
    )
    assert driver.execute(["c"], inputs={"x": 1}) == {"c": 40}
    assert [n for stage, n in _stages(hook.log) if stage == "after_node"] == [
        "a",
        "b",
        "c",
    ]
    hook.log.clear()
    assert driver.execute(["c"], inputs={"x": 1}) == {"c": 40}
    # Only the hit reports, as one (Q85); `a` and `b` only fed it.
    assert _stages(hook.log) == [
        ("before_run", None),
        ("before_node", "c"),
        ("after_node", "c"),
        ("after_run", None),
    ]


def test_hook_methods_get_the_event_positionally() -> None:
    """A hook may name its parameter anything: the event is passed by position."""
    got: list[object] = []

    class Positional(Hook):
        @override
        def before_run(self, anything: RunStart, /) -> None:  # ty: ignore[invalid-method-override]  # renamed on purpose
            got.append(anything)

    _driver(GRAPH, Positional()).execute(["a"], inputs={"x": 1})
    assert len(got) == 1
    assert isinstance(got[0], RunStart)
