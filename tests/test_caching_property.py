"""The equality property: a cached run returns what an uncached run returns.

Hypothesis generates graphs of 1 to 12 pure nodes over a few inputs, each node a
fixed arithmetic or string function of its parameters, some also reading a module
constant, a helper function or a `value()` constant. A scenario then edits inputs,
the constant, the helper and the `value()` constants in turn, rebuilding the module
under the same name whenever its code changes, so a key that missed an edit would
hit a stale entry. After every edit the requested outputs from a `MemoryCache` and a
`DiskCache` driver, cold and then warm, must equal an uncached driver's, compared by
`repr` so that `1`, `1.0` and `True`, or `0.0` and `-0.0`, do not pass as equal.
"""

import os
import tempfile
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

from hypothesis import given, settings
from hypothesis import strategies as st

import fnloom
from fnloom import Builder, DiskCache, MemoryCache
from helpers import make_module

if TYPE_CHECKING:
    from collections.abc import Callable

    from fnloom import CacheStore, Driver

# On CI, the same examples every run, and no example database to carry between runs.
settings.register_profile("ci", derandomize=True, database=None)
if "CI" in os.environ:
    settings.load_profile("ci")

MODULE = "caching_property_graph"
INPUTS = ("i0", "i1", "i2")

type Op = Literal["tuple", "repr", "len", "float", "pick", "neg"]
OPS: tuple[Op, ...] = ("tuple", "repr", "len", "float", "pick", "neg")

_scalars = st.one_of(
    st.none(),
    st.integers(min_value=-(2**70), max_value=2**70),
    st.floats(),
    st.text(max_size=4),
)
# Inputs may hold any float; a constant is written into source, where `nan` and
# `inf` are not literals.
_literal_scalars = st.one_of(
    st.none(),
    st.integers(min_value=-1000, max_value=1000),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(max_size=4),
)
_values = st.recursive(
    _scalars, lambda inner: st.tuples(inner, inner) | st.tuples(inner), max_leaves=4
)
_constants = st.recursive(
    _literal_scalars, lambda inner: st.tuples(inner, inner), max_leaves=3
)


@dataclass(frozen=True)
class NodeSpec:
    """One generated node: what it reads and the function of it that it returns.

    Attributes:
        op: The function of its terms.
        params: The nodes and inputs it reads, by name, at least one.
        reads_constant: Whether it also reads the module constant `C`.
        reads_helper: Whether it also calls the helper `_helper` on its first param.
        has_value: Whether it is a `@parameterize` row with a `value()` constant `k`.
    """

    op: Op
    params: tuple[str, ...]
    reads_constant: bool
    reads_helper: bool
    has_value: bool


@dataclass(frozen=True)
class State:
    """Everything a run depends on: the code's edit points and the inputs."""

    constant: int
    helper: str
    values: tuple[object, ...]
    inputs: tuple[object, ...]


# A tag naming what changes, then the new values: new inputs, a new constant, a new
# helper suffix, a node's index and its new `value()` constant, or nothing.
type Edit = tuple[object, ...]


@dataclass(frozen=True)
class Scenario:
    """A graph, the state it starts in, and the edits made to it, run by run."""

    nodes: tuple[NodeSpec, ...]
    start: State
    steps: tuple[tuple[Edit, frozenset[int]], ...]


def _body(spec: NodeSpec) -> str:
    """The node's return expression over its terms."""
    terms = list(spec.params)
    if spec.reads_constant:
        terms.append("C")
    if spec.reads_helper:
        terms.append(f"_helper({spec.params[0]})")
    if spec.has_value:
        terms.append("k")
    first, last, joined = terms[0], terms[-1], ", ".join(terms)
    match spec.op:
        case "tuple":
            # Only the first term nests, so values grow linearly with depth.
            return f"({first}, {len(terms)})"
        case "repr":
            return "(" + " + ".join(f"repr({t})" for t in terms) + ")[:40]"
        case "len":
            return f"len(repr(({joined},))) * 3 - 7"
        case "float":
            return f"len(str(({joined},))) / 4 + 0.5"
        case "pick":
            return f"{first} if len(repr({last})) % 2 else None"
        case "neg":
            # `-0.0` from `0.0`: equal, but not interchangeable downstream.
            return f"-{first} if type({first}) in (int, float) else ({joined},)"


def source(nodes: tuple[NodeSpec, ...], state: State) -> str:
    """The module for `nodes` in `state`."""
    lines = [
        "from fnloom import parameterize, value",
        "",
        f"C = {state.constant!r}",
        "",
        "def _helper(v: object) -> object:",
        f"    return (repr(v) + {state.helper!r})[:30]",
        "",
    ]
    for index, spec in enumerate(nodes):
        name = f"n{index}"
        params = ", ".join(f"{p}: object" for p in spec.params)
        if spec.has_value:
            lines.append(
                f"@parameterize({name}={{'k': value({state.values[index]!r})}})"
            )
            lines.append(f"def row_{name}({params}, k: object) -> object:")
        else:
            lines.append(f"def {name}({params}) -> object:")
        lines.extend((f"    return {_body(spec)}", ""))
    return "\n".join(lines)


def _used_inputs(nodes: tuple[NodeSpec, ...]) -> list[str]:
    """The inputs some node reads, in `INPUTS` order: the graph's inputs."""
    read = {p for spec in nodes for p in spec.params}
    return [name for name in INPUTS if name in read]


@st.composite
def scenarios(draw: st.DrawFn) -> Scenario:
    """A graph of 1 to 12 nodes, its starting state, and 1 to 5 edited runs."""
    count = draw(st.integers(min_value=1, max_value=12))
    nodes: list[NodeSpec] = []
    for index in range(count):
        readable = [*INPUTS, *(f"n{i}" for i in range(index))]
        params = draw(
            st.lists(st.sampled_from(readable), min_size=1, max_size=3, unique=True)
        )
        nodes.append(
            NodeSpec(
                op=draw(st.sampled_from(OPS)),
                params=tuple(params),
                reads_constant=draw(st.booleans()),
                reads_helper=draw(st.booleans()),
                has_value=draw(st.booleans()),
            )
        )
    start = State(
        constant=draw(st.integers(min_value=-5, max_value=5)),
        helper=draw(st.text(max_size=3)),
        values=tuple(draw(_constants) for _ in range(count)),
        inputs=tuple(draw(_values) for _ in INPUTS),
    )
    kinds: list[st.SearchStrategy[Edit]] = [
        st.tuples(st.just("inputs"), st.tuples(_values, _values, _values)),
        st.tuples(st.just("constant"), st.integers(min_value=-5, max_value=5)),
        st.tuples(st.just("helper"), st.text(max_size=3)),
        st.tuples(st.just("none")),
    ]
    # A `value()` edit names a node that has one, so it always changes the code.
    rows = [index for index, spec in enumerate(nodes) if spec.has_value]
    if rows:
        kinds.append(st.tuples(st.just("value"), st.sampled_from(rows), _constants))
    edits = st.one_of(kinds)
    outputs = st.frozensets(st.integers(min_value=0, max_value=count - 1), min_size=1)
    first: Edit = ("none",)
    steps: list[tuple[Edit, frozenset[int]]] = [(first, draw(outputs))]
    steps += draw(st.lists(st.tuples(edits, outputs), min_size=1, max_size=4))
    return Scenario(tuple(nodes), start, tuple(steps))


def _apply(state: State, edit: Edit) -> State:
    """`state` after `edit`."""
    match edit:
        case ("inputs", tuple() as inputs):
            return replace(state, inputs=inputs)
        case ("constant", int() as constant):
            return replace(state, constant=constant)
        case ("helper", str() as helper):
            return replace(state, helper=helper)
        case ("value", int() as index, constant):
            values = list(state.values)
            values[index] = constant
            return replace(state, values=tuple(values))
        case _:
            return state


def _same(got: dict[str, object], expected: dict[str, object]) -> bool:
    """Equal by `repr`: values, their types, and the order of the outputs."""
    return repr(got) == repr(expected)


def check(scenario: Scenario, stores: list[Callable[[], CacheStore]]) -> None:
    """Runs `scenario` uncached and through each store; every result must match."""
    nodes = scenario.nodes
    inputs = _used_inputs(nodes)
    made = [factory() for factory in stores]
    state = scenario.start
    code: str | None = None
    drivers: list[Driver] = []
    uncached: Driver | None = None
    for edit, wanted in scenario.steps:
        state = _apply(state, edit)
        new_code = source(nodes, state)
        if new_code != code:
            code = new_code
            module = make_module(MODULE, code)
            uncached = fnloom.build(module)
            drivers = [
                Builder().with_modules(module).with_cache(store).build()
                for store in made
            ]
        assert uncached is not None
        given = {name: state.inputs[INPUTS.index(name)] for name in inputs}
        outputs = [f"n{i}" for i in sorted(wanted)]
        expected = uncached.execute(outputs, inputs=given)
        for driver in drivers:
            for _ in range(2):
                got = driver.execute(outputs, inputs=given)
                assert _same(got, expected), (code, given, got, expected)


@settings(max_examples=200, deadline=None)
@given(scenario=scenarios())
def test_a_cached_run_equals_an_uncached_run(scenario: Scenario) -> None:
    """Through edits to inputs and code, both stores return what no cache returns."""
    with tempfile.TemporaryDirectory() as directory:
        check(scenario, [MemoryCache, lambda: DiskCache(directory)])
