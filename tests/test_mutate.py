"""`@mutate` and `apply_to`: steps added to another function's `@pipe_output`."""

import pytest

import fnloom
from fnloom import apply_to, mutate
from helpers import make_module

MUTATED = """
from fnloom import apply_to, check_output, mutate, pipe_output, source, step, value


def _first(v: int) -> int:
    return v + 1000


@pipe_output(step(_first))
def a(x: int) -> int:
    return x


def b(x: int) -> int:
    return x


@mutate(a, b, n=1)
def _add(v: int, n: int) -> int:
    return v + n


@mutate(a)
def _double(v: int) -> int:
    return v * 2


@mutate(
    apply_to(b, n=value(100)).named("bplus"),
    apply_to(a, n=source("x")).when(mode="a"),
)
def _add2(v: int, n: int) -> int:
    return v + n
"""


def _driver(
    source: str = MUTATED, name: str = "mutated", mode: str = "a"
) -> fnloom.Driver:
    module = make_module(name, source)
    return fnloom.Builder().with_modules(module).with_config({"mode": mode}).build()


def test_steps_follow_the_targets_own_in_definition_order() -> None:
    """After `@pipe_output`'s steps, in the order the mutators are defined (KEEP)."""
    driver = _driver()
    deps = {name: driver.nodes[name].dependencies for name in driver.node_names()}
    assert deps["a.with_first"] == ("a.raw",)
    assert deps["a.with_add"] == ("a.with_first",)
    assert deps["a.with_double"] == ("a.with_add",)
    assert deps["a.with_add2"] == ("a.with_double", "x")
    assert deps["a"] == ("a.with_add2",)
    assert driver.execute(["a"], inputs={"x": 1}) == {"a": (1 + 1000 + 1) * 2 + 1}


def test_apply_to_bindings_replace_the_mutators() -> None:
    """`apply_to(b, n=...)` binds `n` for `b` alone; `.named` and `.when` apply."""
    driver = _driver()
    assert driver.nodes["b.bplus"].dependencies == ("b.with_add",)
    assert driver.execute(["b"], inputs={"x": 1}) == {"b": 102}
    without = _driver(name="mutated_b", mode="b")
    assert "a.with_add2" not in without.node_names()


def test_the_mutator_is_no_node() -> None:
    """Even a public one (KEEP)."""
    source = MUTATED.replace("def _double(", "def double(")
    driver = _driver(source, "public_mutator")
    assert "double" not in driver.node_names()
    assert "a.with_double" in driver.node_names()


def test_a_cross_module_target_works() -> None:
    """When both modules are built (Q107)."""
    target = make_module("mutate_target", "def a(x: int) -> int:\n    return x\n")
    source = """
from fnloom import mutate
import mutate_target

@mutate(mutate_target.a)
def _plus(v: int) -> int:
    return v + 1
"""
    mutator = make_module("mutate_source", source)
    driver = fnloom.build(target, mutator)
    assert driver.execute(["a"], inputs={"x": 1}) == {"a": 2}


def test_a_target_no_built_module_defines_fails_the_build() -> None:
    """Naming the mutator and the target (Q107)."""
    target = make_module("unbuilt_target", "def a(x: int) -> int:\n    return x\n")
    source = """
from fnloom import mutate
import unbuilt_target

@mutate(unbuilt_target.a)
def _plus(v: int) -> int:
    return v + 1

def c(x: int) -> int:
    return x
"""
    mutator = make_module("unbuilt_source", source)
    assert target is not None
    with pytest.raises(ValueError, match="mutate on _plus: its target a is no node"):
        fnloom.build(mutator)


def test_misuse_is_refused_when_decorating() -> None:
    """A name, a lambda, nothing, or a non-function (Q107)."""

    def _step(v: int) -> int:
        return v

    with pytest.raises(TypeError, match="not its name 'a'"):
        mutate("a")(_step)  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="lambda is never a node"):
        mutate(lambda x: x)(_step)
    with pytest.raises(TypeError, match="at least one target"):
        mutate()
    with pytest.raises(TypeError, match="functions or apply_to"):
        mutate(3)  # ty: ignore[invalid-argument-type]


def test_mutating_twice_adds_the_step_once() -> None:
    """As re-running a notebook cell does (Q107)."""
    module = make_module("twice", "def a(x: int) -> int:\n    return x\n")
    for _ in range(2):

        def _plus(v: int) -> int:
            return v + 1

        _plus.__module__ = "twice"
        mutate(module.a)(_plus)
    driver = fnloom.build(module)
    assert driver.execute(["a"], inputs={"x": 1}) == {"a": 2}
    assert [n for n in driver.node_names() if n.startswith("a.")] == [
        "a.raw",
        "a.with_plus",
    ]


def test_a_check_sees_the_mutated_value() -> None:
    """Whatever was defined first (Q103, Review Focus 2)."""
    source = """
from fnloom import check_output, mutate

@check_output(range=(0, 5))
def a(x: int) -> int:
    return x

@mutate(a)
def _big(v: int) -> int:
    return v + 10
"""
    driver = _driver(source, "checked_mutated")
    with pytest.raises(fnloom.ValidationError, match="a: check range failed: 11"):
        driver.execute(["a"], inputs={"x": 1})


def test_on_output_targets_a_field() -> None:
    """`apply_to(d).on_output("p")` mutates the field `p` only (KEEP)."""
    source = """
from fnloom import apply_to, extract_fields, mutate

@extract_fields({"p": int, "q": int})
def d(x: int) -> dict:
    return {"p": x, "q": x}

@mutate(apply_to(d).on_output("p"))
def _double(v: int) -> int:
    return v * 2
"""
    driver = _driver(source, "mutated_field")
    assert driver.execute(["p", "q"], inputs={"x": 3}) == {"p": 6, "q": 3}


def test_apply_to_refuses_a_name() -> None:
    """As `mutate` does."""
    with pytest.raises(TypeError, match="not its name"):
        apply_to("a")  # ty: ignore[invalid-argument-type]
