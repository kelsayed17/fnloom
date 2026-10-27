"""Tests for the native binding `fnloom._core`: graphs, plans, runs and errors."""

import ast
import gc
import sys
import weakref
from collections.abc import Callable
from pathlib import Path
from typing import Any, override

import pytest

from fnloom import _core

type NodeTuple = tuple[str, list[tuple[str, bool]], dict[str, str], Callable[..., Any]]

STUB = Path(__file__).parent.parent / "python" / "fnloom" / "_core.pyi"


def _add_one(a: int) -> int:
    return a + 1


def _double(b: int) -> int:
    return b * 2


def _divide_by_zero(a: int) -> float:
    # Every run of the failing graph passes `a=1`.
    return a / (a - 1)


def _chain() -> _core.Graph:
    """The graph `a` (input) -> `b` -> `c`."""
    return _core.Graph(
        [
            ("b", [("a", True)], {}, _add_one),
            ("c", [("b", True)], {"stage": "last"}, _double),
        ]
    )


def _failing() -> _core.Graph:
    """The graph `a` (input) -> `boom`, where `boom` divides by zero."""
    return _core.Graph([("boom", [("a", True)], {}, _divide_by_zero)])


class Recorder(_core.ObserverBase):
    """Records every hook call, and raises from the hooks named in `raise_on`."""

    def __init__(self, label: str = "", raise_on: frozenset[str] = frozenset()) -> None:
        """Starts with no events; `label` tags the errors this observer raises."""
        self.label = label
        self.raise_on = raise_on
        self.events: list[tuple[object, ...]] = []

    def _record(self, *event: object) -> None:
        self.events.append(event)
        if event[0] in self.raise_on:
            msg = f"{self.label} {event[0]}"
            raise RuntimeError(msg)

    @override
    def pre_graph(self) -> None:
        """Records the event."""
        self._record("pre_graph")

    @override
    def pre_node(self, node: str) -> None:
        """Records the event."""
        self._record("pre_node", node)

    @override
    def post_node(self, node: str, *, ok: bool) -> None:
        """Records the event."""
        self._record("post_node", node, ok)

    @override
    def post_graph(self, *, ok: bool) -> None:
        """Records the event."""
        self._record("post_graph", ok)


def test_prepare_and_run_a_chain() -> None:
    """A plan lists its steps in order and its run returns exactly the outputs."""
    plan = _chain().prepare(["c"], ["a"], [])
    assert plan.steps() == [("a", "use_input"), ("b", "compute"), ("c", "compute")]
    assert plan.run({"a": 1}, {}) == {"c": 4}


def test_keyword_names_reach_the_function_interned() -> None:
    """Each keyword name is the interned str, which CPython matches by identity.

    With a fresh str of equal value, CPython's keyword binding falls back to one `==`
    comparison per parameter for each keyword, so a node with n parameters costs O(n²).
    """
    seen: dict[str, bool] = {}

    def record(**kwargs: int) -> int:
        # The literals below are compile-time constants, so `sys.intern` returns them.
        seen.update({name: name is sys.intern(name) for name in kwargs})
        return 0

    graph = _core.Graph([("total", [("alpha", True), ("beta", True)], {}, record)])
    graph.prepare(["total"], ["alpha", "beta"], []).run({"alpha": 1, "beta": 2}, {})
    assert seen == {"alpha": True, "beta": True}


def test_a_renamed_input_reaches_the_function_under_its_keyword_interned() -> None:
    """A rename passes an input under another keyword, interned; others keep theirs."""
    seen: dict[str, tuple[int, bool]] = {}

    def record(**kwargs: int) -> int:
        seen.update({name: (v, name is sys.intern(name)) for name, v in kwargs.items()})
        return 0

    graph = _core.Graph(
        [("total", [("alpha", True), ("beta", True)], {}, record)],
        {"total": {"alpha": "first"}},
    )
    graph.prepare(["total"], ["alpha", "beta"], []).run({"alpha": 1, "beta": 2}, {})
    assert seen == {"first": (1, True), "beta": (2, True)}


def test_a_rename_of_an_unknown_node_raises_value_error_naming_it() -> None:
    """A rename naming no node or input of the graph is refused."""
    nodes: list[NodeTuple] = [("b", [("a", True)], {}, _add_one)]
    with pytest.raises(ValueError, match="'nowhere'"):
        _core.Graph(nodes, {"nowhere": {"a": "x"}})
    with pytest.raises(ValueError, match="'gone'"):
        _core.Graph(nodes, {"b": {"gone": "x"}})


def test_overrides_and_optional_inputs_have_their_actions() -> None:
    """An override replaces a node; an unsupplied optional input is skipped."""
    graph = _core.Graph(
        [
            ("b", [("a", True), ("maybe", False)], {}, _add_one),
            ("c", [("b", True)], {}, _double),
        ]
    )
    assert graph.prepare(["c"], [], ["b"]).steps() == [
        ("b", "use_override"),
        ("c", "compute"),
    ]
    plan = graph.prepare(["b"], ["a"], [])
    assert plan.steps() == [
        ("a", "use_input"),
        ("maybe", "skip_optional_input"),
        ("b", "compute"),
    ]
    assert plan.run({"a": 1}, {}) == {"b": 2}
    assert graph.prepare(["c"], [], ["b"]).run({}, {"b": 10}) == {"c": 20}


def test_queries_return_sorted_names() -> None:
    """Node listing and the reachability queries answer in node names."""
    graph = _chain()
    assert graph.node_names() == ["a", "b", "c"]
    assert graph.upstream_of(["c"]) == ["a", "b"]
    assert graph.downstream_of(["a"]) == ["b", "c"]
    assert graph.between("a", "c") == ["b"]


def test_unknown_query_names_raise_value_error_naming_them() -> None:
    """A query on unknown names raises `ValueError` listing them."""
    with pytest.raises(ValueError, match="unknown nodes: 'ghost'"):
        _chain().upstream_of(["ghost"])


def test_unknown_output_raises_value_error_naming_it() -> None:
    """Preparing an unknown output raises `ValueError` naming it."""
    with pytest.raises(ValueError, match="unknown outputs: 'nope'"):
        _chain().prepare(["nope"], ["a"], [])


def test_missing_input_raises_value_error_naming_it() -> None:
    """Preparing without a required input raises `ValueError` naming it."""
    with pytest.raises(ValueError, match="missing inputs: 'a'"):
        _chain().prepare(["c"], [], [])


def test_missing_run_value_raises_value_error_naming_it() -> None:
    """Running without a value the plan reads raises `ValueError` naming it."""
    with pytest.raises(ValueError, match="missing values: 'a'"):
        _chain().prepare(["c"], ["a"], []).run({}, {})


def test_build_errors_raise_value_error_naming_the_culprits() -> None:
    """Duplicate names and cycles are rejected with `ValueError`."""
    with pytest.raises(ValueError, match="duplicate node name 'b'"):
        _core.Graph([("b", [], {}, _add_one), ("b", [], {}, _add_one)])
    with pytest.raises(ValueError, match="dependency cycle: 'x' -> 'y' -> 'x'"):
        _core.Graph(
            [("x", [("y", True)], {}, _add_one), ("y", [("x", True)], {}, _add_one)]
        )


def test_a_bare_string_of_outputs_raises_type_error() -> None:
    """A `str` is not accepted where a list of names is expected."""
    graph = _chain()
    outputs: Any = "c"
    with pytest.raises(TypeError):
        graph.prepare(outputs, ["a"], [])


def test_a_plan_keeps_its_graph_alive() -> None:
    """A plan runs after every Python reference to its graph is gone."""
    graph = _chain()
    plan = graph.prepare(["c"], ["a"], [])
    del graph
    gc.collect()
    assert plan.run({"a": 2}, {}) == {"c": 6}


def _plan_in_a_cycle() -> weakref.ref[Callable[..., Any]]:
    """Builds the cycle function -> plan -> graph -> function; weakly refers to it."""

    def b(a: int) -> int:
        return a + 1

    graph = _core.Graph([("b", [("a", True)], {}, b)])
    vars(b)["plan"] = graph.prepare(["b"], ["a"], [])
    return weakref.ref(b)


def test_a_graph_in_a_reference_cycle_is_collected() -> None:
    """The GC sees through graphs and plans, so a cycle through one is freed."""
    alive = _plan_in_a_cycle()
    gc.collect()
    assert alive() is None


def test_node_exception_keeps_its_class_and_gets_a_note() -> None:
    """A node's own exception propagates with a note naming the node."""
    plan = _failing().prepare(["boom"], ["a"], [])
    with pytest.raises(ZeroDivisionError) as info:
        plan.run({"a": 1}, {})
    assert "in node 'boom'" in info.value.__notes__


def test_an_exception_object_raised_on_every_run_is_noted_once() -> None:
    """Re-raising one exception object does not stack a note per run."""
    shared = ValueError("raised by every run")

    def boom(a: int) -> int:  # noqa: ARG001  # the node fails whatever its input
        raise shared

    plan = _core.Graph([("boom", [("a", True)], {}, boom)]).prepare(["boom"], ["a"], [])
    for _ in range(2):
        with pytest.raises(ValueError, match="raised by every run"):
            plan.run({"a": 1}, {})
    assert shared.__notes__ == ["in node 'boom'"]


def test_observers_see_post_graph_false_on_failure() -> None:
    """Observers see every event of a failing run, ending in `post_graph(ok=False)`."""
    recorder = Recorder()
    plan = _failing().prepare(["boom"], ["a"], [])
    with pytest.raises(ZeroDivisionError):
        plan.run({"a": 1}, {}, [recorder])
    assert recorder.events == [
        ("pre_graph",),
        ("pre_node", "boom"),
        ("post_node", "boom", False),
        ("post_graph", False),
    ]


def test_observers_see_a_successful_run_in_order() -> None:
    """Every observer sees every event; the base class's hooks are no-ops."""
    first, second = Recorder(), Recorder()
    plan = _chain().prepare(["c"], ["a"], [])
    assert plan.run({"a": 1}, {}, (first, _core.ObserverBase(), second)) == {"c": 4}
    expected = [
        ("pre_graph",),
        ("pre_node", "b"),
        ("post_node", "b", True),
        ("pre_node", "c"),
        ("post_node", "c", True),
        ("post_graph", True),
    ]
    assert first.events == expected
    assert second.events == expected


def test_a_non_observer_is_rejected() -> None:
    """`observers` accepts only `ObserverBase` instances."""
    plan = _chain().prepare(["c"], ["a"], [])
    observers: Any = [object()]
    with pytest.raises(TypeError):
        plan.run({"a": 1}, {}, observers)


def test_one_failing_hook_raises_its_own_exception() -> None:
    """A single hook error is raised as itself, and the run stops."""
    recorder = Recorder("only", frozenset({"pre_node"}))
    plan = _chain().prepare(["c"], ["a"], [])
    with pytest.raises(RuntimeError, match="only pre_node"):
        plan.run({"a": 1}, {}, [recorder])
    assert recorder.events[-1] == ("post_graph", False)


def test_several_failing_hooks_raise_an_exception_group() -> None:
    """Several hook errors are raised together, in observer order."""
    first = Recorder("first", frozenset({"pre_graph"}))
    second = Recorder("second", frozenset({"pre_graph"}))
    plan = _chain().prepare(["c"], ["a"], [])
    with pytest.raises(ExceptionGroup) as info:
        plan.run({"a": 1}, {}, [first, second])
    assert info.value.message == "observer hooks raised during pre_graph"
    assert [str(error) for error in info.value.exceptions] == [
        "first pre_graph",
        "second pre_graph",
    ]


def test_a_node_failure_with_hook_errors_raises_a_group_led_by_the_node() -> None:
    """A node failure plus hook errors raises a group: noted node exception first."""
    recorder = Recorder("hook", frozenset({"post_node", "post_graph"}))
    plan = _failing().prepare(["boom"], ["a"], [])
    with pytest.raises(ExceptionGroup) as info:
        plan.run({"a": 1}, {}, [recorder])
    group = info.value
    assert group.message == "node 'boom' failed and observer hooks raised"
    node_error, *hook_errors = group.exceptions
    assert isinstance(node_error, ZeroDivisionError)
    assert "in node 'boom'" in node_error.__notes__
    assert [str(error) for error in hook_errors] == [
        "hook post_node",
        "hook post_graph",
    ]


def test_a_rust_panic_becomes_a_python_exception() -> None:
    """A panic surfaces as pyo3's `PanicException` and the interpreter survives."""
    with pytest.raises(BaseException, match="deliberate panic") as info:
        _core._panic_for_test()  # noqa: SLF001 - the test-hooks entry point is private by design
    assert type(info.value).__name__ == "PanicException"
    assert _chain().prepare(["c"], ["a"], []).run({"a": 0}, {}) == {"c": 2}


def _panics(a: int) -> int:
    _core._panic_for_test()  # noqa: SLF001 - the test-hooks entry point is private by design
    return a


class PanickingObserver(_core.ObserverBase):
    """Panics in Rust from its `pre_node` hook."""

    @override
    def pre_node(self, node: str) -> None:
        """Panics."""
        _core._panic_for_test()  # noqa: SLF001 - the test-hooks entry point is private by design


def test_a_panicking_node_still_runs_the_closing_hooks() -> None:
    """A panicking node fails like a raising one: closing hooks run, the note is set."""
    recorder = Recorder()
    graph = _core.Graph([("boom", [("a", True)], {}, _panics)])
    with pytest.raises(BaseException, match="deliberate panic") as info:
        graph.prepare(["boom"], ["a"], []).run({"a": 1}, {}, [recorder])
    assert type(info.value).__name__ == "PanicException"
    assert "in node 'boom'" in info.value.__notes__
    assert recorder.events == [
        ("pre_graph",),
        ("pre_node", "boom"),
        ("post_node", "boom", False),
        ("post_graph", False),
    ]


def test_a_panicking_observer_does_not_stop_the_next_one() -> None:
    """A panic in one observer's hook still lets the next observer see the event."""
    second = Recorder()
    plan = _chain().prepare(["c"], ["a"], [])
    with pytest.raises(BaseException, match="deliberate panic") as info:
        plan.run({"a": 1}, {}, [PanickingObserver(), second])
    assert type(info.value).__name__ == "PanicException"
    assert second.events == [("pre_graph",), ("pre_node", "b"), ("post_graph", False)]


def _successor(previous: str) -> Callable[..., int]:
    def successor(**kwargs: int) -> int:
        return kwargs[previous] + 1

    return successor


def test_ten_thousand_node_chain() -> None:
    """A 10,000-node chain builds, plans and runs without recursion limits."""
    size = 10_000
    names = [f"n{i:05d}" for i in range(size)]
    nodes: list[NodeTuple] = [
        (name, [(previous, True)], {}, _successor(previous))
        for previous, name in zip(["x", *names[:-1]], names, strict=True)
    ]
    plan = _core.Graph(nodes).prepare([names[-1]], ["x"], [])
    assert len(plan.steps()) == size + 1
    assert plan.run({"x": 0}, {}) == {names[-1]: size}


def test_stub_lists_exactly_the_runtime_names() -> None:
    """The stub's public names equal the extension's public names."""
    tree = ast.parse(STUB.read_text(encoding="utf-8"))
    stubbed = {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.ClassDef)
        and not node.name.startswith("_")
    }
    runtime = {name for name in dir(_core) if not name.startswith("_")}
    assert stubbed == runtime
