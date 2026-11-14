"""How a plan calls each node's function: by vectorcall, with its keywords named once.

A run with no observers passes a node's arguments in place, named by a tuple made when
the plan is prepared (`crates/fnloom-py/src/call.rs`), rather than in a dict. These
tests pin that call to what a dict call does: every kind of callable gets the same
arguments, an exception comes through as itself, and no reference is gained or lost.
"""

import contextlib
import functools
import gc
import sys
import sysconfig
from typing import TYPE_CHECKING, Any

import pytest

from fnloom import _core

if TYPE_CHECKING:
    from collections.abc import Callable

    from fnloom._core import CollectMode, Kind

    type NodeTuple = tuple[
        str,
        list[tuple[str, Kind, CollectMode | None]],
        dict[str, str],
        Callable[..., Any],
        bool,
        Callable[..., Any] | None,
    ]

# More than the eight arguments the call passes from the stack, so the heap path runs.
WIDE = 12
RUNS = 1000

# Under free threading a count is split between the owning thread and the others, and
# objects may be made immortal, so a count read back need not match exactly there.
counts_are_exact = pytest.mark.skipif(
    bool(sysconfig.get_config_var("Py_GIL_DISABLED")),
    reason="reference counts are not exact under free threading",
)


class BoomError(Exception):
    """The exception a failing node raises."""


def _node(name: str, sources: list[str], function: Callable[..., Any]) -> NodeTuple:
    """Node `name`, calling `function`, reading each of `sources` by a required edge."""
    edges: list[tuple[str, Kind, CollectMode | None]] = [
        (source, "required", None) for source in sources
    ]
    return (name, edges, {}, function, False, None)


def _run(function: Callable[..., Any], sources: list[str], **inputs: object) -> object:
    """Runs one node `out` reading `sources`, all inputs, and returns its value."""
    plan = _core.Graph([_node("out", sources, function)]).prepare(["out"], sources, [])
    return plan.run(inputs, {})["out"]


def _wide_names() -> list[str]:
    return [f"p{index:02d}" for index in range(WIDE)]


def test_each_argument_binds_to_its_parameter_by_name() -> None:
    """Arguments reach the parameters named for them, whatever the declared order."""

    def ordered(b: int, a: int, c: int) -> tuple[int, int, int]:
        return a, b, c

    assert _run(ordered, ["a", "b", "c"], a=1, b=2, c=3) == (1, 2, 3)


def test_a_node_with_more_arguments_than_the_stack_holds_gets_them_all() -> None:
    """Twelve arguments, past the eight passed from the stack, all bind by name."""
    names = _wide_names()
    namespace: dict[str, Any] = {}
    exec(  # noqa: S102  # a function with twelve named parameters, made for the test
        f"def wide({', '.join(names)}):\n    return [{', '.join(names)}]", namespace
    )
    values = {name: index for index, name in enumerate(names)}
    assert _run(namespace["wide"], names, **values) == list(range(WIDE))


def test_keyword_only_parameters_are_bound() -> None:
    """A keyword-only parameter gets its value, as from a dict call."""

    def keyword_only(*, a: int, b: int) -> tuple[int, int]:
        return a, b

    assert _run(keyword_only, ["a", "b"], a=1, b=2) == (1, 2)


def test_a_var_keyword_function_gets_every_argument_as_a_fresh_dict() -> None:
    """`**kwargs` receives each argument by name, in a dict of its own each call."""
    seen: list[dict[str, int]] = []

    def collect(**kwargs: int) -> int:
        seen.append(kwargs)
        return len(kwargs)

    plan = _core.Graph([_node("out", ["a", "b"], collect)]).prepare(
        ["out"], ["a", "b"], []
    )
    assert plan.run({"a": 1, "b": 2}, {}) == {"out": 2}
    assert plan.run({"a": 3, "b": 4}, {}) == {"out": 2}
    assert seen == [{"a": 1, "b": 2}, {"a": 3, "b": 4}]
    assert seen[0] is not seen[1]


def test_wrapped_and_other_callables_are_called_alike() -> None:
    """A decorated function, a partial, a bound method and a callable object all work.

    A partial and a bound method prepend an argument in the spare slot the call leaves;
    an object without vectorcall is called through the dict CPython builds for it.
    """

    def add(a: int, b: int) -> int:
        return a + b

    @functools.wraps(add)
    def decorated(*args: int, **kwargs: int) -> int:
        return add(*args, **kwargs) * 10

    class Adder:
        def __init__(self, base: int) -> None:
            self.base = base

        def method(self, a: int, b: int) -> int:
            return self.base + a + b

        def __call__(self, a: int, b: int) -> int:
            return -(a + b)

    def scaled(scale: int, a: int, b: int) -> int:
        return scale * (a + b)

    cases: list[tuple[Callable[..., Any], int]] = [
        (decorated, 30),
        (functools.partial(scaled, 100), 300),
        (Adder(1000).method, 1003),
        (Adder(0), -3),
    ]
    for function, expected in cases:
        assert _run(function, ["a", "b"], a=1, b=2) == expected


def test_a_raising_node_raises_its_own_exception() -> None:
    """The exception a node raises comes through as itself, args intact, with a note."""
    raised = BoomError("bad input", 7)

    def boom(a: int) -> int:
        del a
        raise raised

    plan = _core.Graph([_node("boom", ["a"], boom)]).prepare(["boom"], ["a"], [])
    with pytest.raises(BoomError) as info:
        plan.run({"a": 1}, {})
    assert info.value is raised
    assert info.value.args == ("bad input", 7)
    assert "in node 'boom'" in info.value.__notes__


def test_a_wrong_keyword_raises_type_error_as_a_dict_call_does() -> None:
    """A node whose function lacks a parameter fails with CPython's own `TypeError`."""

    def takes_a(a: int) -> int:
        return a

    with pytest.raises(TypeError, match="unexpected keyword argument 'b'"):
        _run(takes_a, ["a", "b"], a=1, b=2)


class _Token:
    """A value whose reference count the tests read; never interned or cached."""


@counts_are_exact
def test_runs_leave_every_reference_count_as_it_was() -> None:
    """An input, and a value one node returns and the next consumes, keep their counts.

    Covers both argument paths: `mid` reads one argument from the stack, and `wide`
    reads twelve, which go on the heap.
    """
    produced = _Token()
    given = _Token()
    names = _wide_names()

    def make(seed: object) -> object:
        del seed
        return produced

    def mid(made: object) -> object:
        return made

    def wide(**kwargs: object) -> int:
        return len(kwargs)

    nodes = [
        _node("made", ["seed"], make),
        _node("passed", ["made"], mid),
        _node("total", [*names, "passed"], wide),
    ]
    plan = _core.Graph(nodes).prepare(["total"], ["seed", *names], [])
    inputs: dict[str, object] = {"seed": given, **dict.fromkeys(names, given)}
    assert plan.run(inputs, {}) == {"total": WIDE + 1}
    gc.collect()
    before = sys.getrefcount(given), sys.getrefcount(produced)
    for _ in range(RUNS):
        plan.run(inputs, {})
    gc.collect()
    assert (sys.getrefcount(given), sys.getrefcount(produced)) == before


@counts_are_exact
def test_a_raising_node_leaves_its_arguments_counts_as_they_were() -> None:
    """A node that raises, on either argument path, leaks none of its arguments."""
    given = _Token()
    names = _wide_names()

    def narrow(a: object) -> int:
        del a
        raise BoomError

    def wide(**kwargs: object) -> int:
        del kwargs
        raise BoomError

    for function, sources in ((narrow, ["a"]), (wide, names)):
        plan = _core.Graph([_node("boom", sources, function)]).prepare(
            ["boom"], sources, []
        )
        inputs = dict.fromkeys(sources, given)
        gc.collect()
        before = sys.getrefcount(given)
        for _ in range(RUNS):
            with contextlib.suppress(BoomError):
                plan.run(inputs, {})
        gc.collect()
        assert sys.getrefcount(given) == before
