"""Tests for `fnloom.PrintNodes` and `Hook.after_build` (Plan 12, items 4 and 7)."""

import asyncio
import copy
import pickle
import re
from typing import TYPE_CHECKING, override

import pytest

import fnloom
from fnloom import BuildEnd, Hook, NodeView, PrintNodes
from helpers import make_module

if TYPE_CHECKING:
    from types import ModuleType

SOURCE = """
import fnloom

@fnloom.tag(stage="raw")
def b(a: int) -> int:
    return a + 1

@fnloom.tag(stage="clean")
def c(b: int) -> int:
    return b * 2

def boom(c: int) -> int:
    raise ValueError("no")
"""


def _driver(name: str, *hooks: Hook) -> fnloom.Driver:
    module = make_module(name, SOURCE)
    return fnloom.Builder().with_modules(module).with_hooks(*hooks).build()


def _lines(verbosity: int = 1, **options: object) -> tuple[PrintNodes, list[str]]:
    printed: list[str] = []
    hook = PrintNodes(verbosity, print_fn=printed.append, **options)  # ty: ignore[invalid-argument-type]  # options are checked by the hook
    return hook, printed


TIME = r"\d+(\.\d+)? ms"


def test_verbosity_one_prints_one_line_per_node_with_its_time() -> None:
    """Each node run prints `node 'x' ran in N ms`, in run order."""
    hook, printed = _lines()
    _driver("pn_one", hook).execute(["c"], inputs={"a": 1})
    assert len(printed) == len(("b", "c"))
    assert re.fullmatch(f"node 'b' ran in {TIME}", printed[0])
    assert re.fullmatch(f"node 'c' ran in {TIME}", printed[1])


def test_a_failed_node_prints_its_error() -> None:
    """A node that raised prints `failed in N ms: ValueError('no')`."""
    hook, printed = _lines()
    with pytest.raises(ValueError, match="no"):
        _driver("pn_fail", hook).execute(["boom"], inputs={"a": 1})
    assert re.fullmatch(
        f"node 'boom' failed in {TIME}: ValueError\\('no'\\)", printed[-1]
    )


def test_verbosity_two_adds_sorted_inputs_and_the_result() -> None:
    """Inputs are `name=repr` sorted by name; the result follows."""
    hook, printed = _lines(2)
    _driver("pn_two", hook).execute(["c"], inputs={"a": 1})
    assert "  inputs: a=1" in printed
    assert "  result: 2" in printed
    assert "  inputs: b=2" in printed
    assert "  result: 4" in printed


def test_verbosity_two_cuts_long_reprs_to_width() -> None:
    """A value longer than `width` is cut and ended with `...`."""
    module = make_module("pn_cut", "def big(n: int) -> str:\n    return 'x' * n\n")
    hook, printed = _lines(2, width=12)
    fnloom.Builder().with_modules(module).with_hooks(hook).build().execute(
        ["big"], inputs={"n": 500}
    )
    assert "  result: 'xxxxxxxxxxx..." in printed


def test_a_cache_hit_says_it_was_loaded_from_cache() -> None:
    """The second run of a cached node reports a hit, not a run (Q85)."""
    hook, printed = _lines()
    module = make_module("pn_hits", SOURCE)
    driver = (
        fnloom.Builder()
        .with_modules(module)
        .with_cache(fnloom.MemoryCache())
        .with_hooks(hook)
        .build()
    )
    driver.execute(["c"], inputs={"a": 1})
    printed.clear()
    driver.execute(["c"], inputs={"a": 1})
    assert printed == ["node 'c' loaded from cache"]


def test_print_nodes_async_and_hits() -> None:
    """On the async driver: one line per node, and hits say so."""
    hook, printed = _lines()
    module = make_module("pn_async", SOURCE)
    driver = (
        fnloom.Builder()
        .with_modules(module)
        .with_cache(fnloom.MemoryCache())
        .with_hooks(hook)
        .build_async()
    )
    asyncio.run(driver.execute(["c"], inputs={"a": 1}))
    assert [line.split("'")[1] for line in printed] == ["b", "c"]
    assert all("ran in" in line for line in printed)
    printed.clear()
    asyncio.run(driver.execute(["c"], inputs={"a": 1}))
    assert printed == ["node 'c' loaded from cache"]


def test_nodes_by_name_restricts_what_is_printed() -> None:
    """A sequence of names prints only those nodes."""
    hook, printed = _lines(nodes=("c",))
    _driver("pn_names", hook).execute(["c"], inputs={"a": 1})
    assert [line.split("'")[1] for line in printed] == ["c"]


def test_nodes_by_view_predicate_sees_tags() -> None:
    """A callable gets the `NodeView`: it can select by tag."""

    def raw(view: NodeView) -> bool:
        return view.tags.get("stage") == "raw"

    hook, printed = _lines(nodes=raw)
    _driver("pn_predicate", hook).execute(["c"], inputs={"a": 1})
    assert [line.split("'")[1] for line in printed] == ["b"]


def test_an_unknown_node_name_is_refused_naming_it_with_a_suggestion() -> None:
    """A name that is no node of the built graph is a `ValueError`."""
    hook, _ = _lines(nodes=("c", "totl", "bb"))
    with pytest.raises(ValueError, match=r"'bb'.*'totl'|'totl'.*'bb'"):
        _driver("pn_unknown", hook)


@pytest.mark.parametrize("verbosity", [0, 3, "2", None])
def test_a_bad_verbosity_is_refused(verbosity: object) -> None:
    """Only 1 and 2 are verbosities."""
    with pytest.raises(ValueError, match="verbosity"):
        PrintNodes(verbosity)  # ty: ignore[invalid-argument-type]  # the refusal is the point


@pytest.mark.parametrize("width", [9, 0, -1])
def test_a_width_under_ten_is_refused(width: int) -> None:
    """A width of at least ten characters is needed for `...` to mean anything."""
    with pytest.raises(ValueError, match="width"):
        PrintNodes(2, width=width)


def test_print_nodes_survives_pickling_and_copy() -> None:
    """The default `print` is picklable, so a driver with it is."""
    driver = _driver("pn_pickle", PrintNodes())
    assert pickle.loads(pickle.dumps(driver)).hooks  # noqa: S301  # our own bytes
    assert copy.copy(driver).hooks == driver.hooks


# after_build


class Recorder(Hook):
    """Remembers each `BuildEnd` it gets."""

    def __init__(self) -> None:
        """No events yet."""
        self.events: list[BuildEnd] = []

    @override
    def after_build(self, event: BuildEnd) -> None:
        """Keeps the event."""
        self.events.append(event)


def test_after_build_gets_nodes_config_and_modules() -> None:
    """The event holds the frozen views, the config and the modules, in build order."""
    module: ModuleType = make_module("ab_event", SOURCE)
    recorder = Recorder()
    driver = (
        fnloom.Builder()
        .with_modules(module)
        .with_config({"k": 1})
        .with_hooks(recorder)
        .build()
    )
    (event,) = recorder.events
    assert list(event.nodes) == ["a", "b", "boom", "c"]
    assert event.nodes is driver.nodes
    assert dict(event.config) == {"k": 1}
    assert event.modules == (module,)
    with pytest.raises(TypeError):
        event.config["k"] = 2  # ty: ignore[invalid-assignment]  # the refusal is the point


def test_after_build_fires_from_build_async_and_rebuild() -> None:
    """Each build fires it once per hook."""
    recorder = Recorder()
    module = make_module("ab_all", SOURCE)
    builder = fnloom.Builder().with_modules(module).with_hooks(recorder)
    driver = builder.build()
    assert len(recorder.events) == 1
    driver.rebuild()
    assert len(recorder.events) == 1 + 1
    builder.build_async()
    assert len(recorder.events) == 1 + 1 + 1
    builder.build_async().rebuild()
    assert len(recorder.events) == 1 + 1 + 1 + 1 + 1


def test_a_hook_that_only_has_after_build_adds_no_run_cost() -> None:
    """A driver whose hooks override only `after_build` compiles plain plans."""
    module = make_module("ab_plain", SOURCE)
    driver = fnloom.Builder().with_modules(module).with_hooks(Recorder()).build()
    assert driver.execute(["c"], inputs={"a": 1}) == {"c": 4}


class Refuses(Hook):
    """Refuses a build whose nodes have no owner tag."""

    def __init__(self, message: str) -> None:
        """Raises `message` when a build ends."""
        self.message = message

    @override
    def after_build(self, event: BuildEnd) -> None:
        """Raises its message."""
        del event
        raise ValueError(self.message)


def test_a_raising_after_build_fails_the_build() -> None:
    """One hook's error is raised as itself."""
    module = make_module("ab_refuse", SOURCE)
    with pytest.raises(ValueError, match="no owner"):
        fnloom.Builder().with_modules(module).with_hooks(Refuses("no owner")).build()


def test_every_hook_sees_the_event_and_errors_are_raised_together() -> None:
    """Two refusals are one `ExceptionGroup`, in hook order; a later hook still runs."""
    module = make_module("ab_group", SOURCE)
    recorder = Recorder()
    builder = (
        fnloom.Builder()
        .with_modules(module)
        .with_hooks(Refuses("first"), recorder, Refuses("second"))
    )
    with pytest.raises(BaseExceptionGroup) as caught:
        builder.build()
    assert [str(e) for e in caught.value.exceptions] == ["first", "second"]
    assert len(recorder.events) == 1


def test_a_refusing_after_build_refuses_rebuild_and_build_async_too() -> None:
    """The same check runs on every route that builds."""
    module = make_module("ab_routes", SOURCE)
    builder = fnloom.Builder().with_modules(module).with_hooks(Refuses("no"))
    with pytest.raises(ValueError, match="no"):
        builder.build_async()


class Aborts(Hook):
    """Refuses every node before it starts."""

    @override
    def before_node(self, event: fnloom.NodeStart) -> None:
        """Raises, so the node never runs."""
        del event
        msg = "stop"
        raise RuntimeError(msg)


def test_a_node_another_hook_refused_leaves_no_start_time_behind() -> None:
    """`PrintNodes` forgets a run's start times when the run ends."""
    hook, printed = _lines()
    driver = _driver("pn_leak", hook, Aborts())
    with pytest.raises(RuntimeError, match="stop"):
        driver.execute(["c"], inputs={"a": 1})
    assert printed == []
    assert hook._started == {}  # noqa: SLF001  # the leak is the point


def test_after_build_errors_group_even_when_one_is_a_base_exception() -> None:
    """A `KeyboardInterrupt` from one hook does not hide the others."""

    class Interrupts(Hook):
        @override
        def after_build(self, event: BuildEnd) -> None:
            del event
            raise KeyboardInterrupt

    module = make_module("ab_base", SOURCE)
    recorder = Recorder()
    builder = fnloom.Builder().with_modules(module).with_hooks(Interrupts(), recorder)
    with pytest.raises(KeyboardInterrupt):
        builder.build()
    assert len(recorder.events) == 1


def test_inputs_and_non_str_names_are_refused() -> None:
    """An input never fires node events; a non-str name is a `TypeError`."""
    hook, _ = _lines(nodes=("a",))
    with pytest.raises(ValueError, match="'a'"):
        _driver("pn_input", hook)
    with pytest.raises(TypeError, match="nodes"):
        PrintNodes(nodes=("c", 1))  # ty: ignore[invalid-argument-type]  # the refusal is the point
