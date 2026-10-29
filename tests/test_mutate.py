"""`@mutate` and `apply_to`: steps added to another function's `@pipe_output`."""

import contextlib
import importlib
from typing import Any

import pytest

import fnloom
from fnloom import apply_to, mutate, value
from helpers import make_module

# Imported once, here: the helpers forget modules a test imports, and numpy
# can't be imported twice in one process.
_NUMPY: object = None
with contextlib.suppress(ImportError):
    _NUMPY = importlib.import_module("numpy")

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
    with pytest.raises(
        ValueError, match="mutate on _plus: its target a is no function of the build"
    ):
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


def test_redefining_a_mutator_replaces_its_step() -> None:
    """As re-running a notebook cell does: new code under the same name (Q107)."""
    module = make_module("twice", "def a(x: int) -> int:\n    return x\n")
    cell = "@mutate(target.a)\ndef _plus(v: int) -> int:\n    return v + {n}\n"
    space = {"mutate": mutate, "target": module, "__name__": "twice"}
    for n in (1, 5):
        exec(compile(cell.format(n=n), "cell", "exec"), space)  # noqa: S102  # a notebook cell
    driver = fnloom.build(module)
    assert driver.execute(["a"], inputs={"x": 1}) == {"a": 6}
    assert [n for n in driver.node_names() if n.startswith("a.")] == [
        "a.raw",
        "a.with_plus",
    ]


def test_mutators_made_in_a_loop_or_a_factory_each_add_a_step() -> None:
    """Same code, other bindings: distinct mutations, as in Hamilton."""
    module = make_module("looped", "def a(x: int) -> int:\n    return x\n")

    def made(n: int) -> None:
        def _plus(v: int) -> int:
            return v + n

        _plus.__module__ = "looped"
        mutate(module.a)(_plus)

    made(1)
    made(10)
    assert fnloom.build(module).execute(["a"], inputs={"x": 0}) == {"a": 11}


def test_one_call_targeting_one_function_twice_adds_both() -> None:
    """Two `apply_to`s of one target in one `@mutate`, as in Hamilton."""
    source = """
from fnloom import apply_to, mutate, value

def a(x: int) -> int:
    return x

@mutate(apply_to(a, n=value(1)), apply_to(a, n=value(10)).named("ten"))
def _add(v: int, n: int) -> int:
    return v + n
"""
    assert _driver(source, "two_applies").execute(["a"], inputs={"x": 0}) == {"a": 11}


def test_an_unselected_variant_is_a_target_of_the_build() -> None:
    """A `@config.when` variant the config leaves out isn't an unknown target."""
    source = """
from fnloom import config, mutate

@config.when(mode="x")
def a__x(v: int) -> int:
    return v

@config.when(mode="y")
def a__y(v: int) -> int:
    return v * 2

@mutate(a__x, a__y)
def _plus(v: int) -> int:
    return v + 100
"""
    module = make_module("variant_targets", source)
    driver = fnloom.Builder().with_modules(module).with_config({"mode": "x"}).build()
    assert driver.execute(["a"], inputs={"v": 0}) == {"a": 100}


def test_a_mutation_from_an_unbuilt_module_fails_the_build() -> None:
    """Else pickling would lose it; build the mutator's module too (Q107)."""
    target = make_module("lonely_target", "def a(x: int) -> int:\n    return x\n")
    make_module(
        "lonely_mutator",
        "from fnloom import mutate\nimport lonely_target\n\n"
        "@mutate(lonely_target.a)\ndef _plus(v: int) -> int:\n    return v + 1\n",
    )
    with pytest.raises(
        ValueError, match="of module 'lonely_mutator', which isn't built"
    ):
        fnloom.build(target)


def test_rows_and_fields_each_get_the_step() -> None:
    """Every `@parameterize` row; every field without `on_output`."""
    source = """
from fnloom import extract_fields, mutate, parameterize, value

@parameterize(p1={"k": value(1)}, p2={"k": value(2)})
def scaled(x: int, k: int) -> int:
    return x * k

@extract_fields({"f": int, "g": int})
def d(x: int) -> dict:
    return {"f": x, "g": x}

@mutate(scaled, d)
def _plus(v: int) -> int:
    return v + 100
"""
    driver = _driver(source, "rows_fields_mutated")
    assert driver.execute(["p1", "p2", "f", "g"], inputs={"x": 1}) == {
        "p1": 101,
        "p2": 102,
        "f": 101,
        "g": 101,
    }


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


def test_the_same_mutator_twice_adds_one_step() -> None:
    """One function object, bound the same, applied twice or named twice (Q107)."""
    module = make_module("same_object", "def f(x: int) -> int:\n    return x\n")

    def _p(v: int) -> int:
        return v + 2

    _p.__module__ = "same_object"
    mutate(module.f)(_p)
    mutate(module.f)(_p)
    mutate(module.f, module.f)(_p)
    driver = fnloom.build(module)
    assert driver.execute(["f"], inputs={"x": 1}) == {"f": 3}
    assert [n for n in driver.node_names() if n.startswith("f.")] == [
        "f.raw",
        "f.with_p",
    ]


def test_a_mutator_bound_to_an_array_dedupes_without_equality() -> None:
    """Bindings match by identity or fingerprint, so an array's `==` never runs."""
    if _NUMPY is None:
        pytest.skip("numpy is not installed")
    numpy: Any = _NUMPY
    module = make_module("array_bound", "def f(x: int) -> int:\n    return x\n")

    def _p(v: int, w: object) -> int:
        return v + int(numpy.sum(w))

    _p.__module__ = "array_bound"
    same = numpy.array([1, 2])
    mutate(module.f, w=value(same))(_p)
    mutate(module.f, w=value(same))(_p)
    mutate(module.f, w=value(numpy.array([1, 2])))(_p)
    mutate(module.f, w=value(numpy.array([5, 5])))(_p)
    driver = fnloom.build(module)
    assert [n for n in driver.node_names() if n.startswith("f.")] == [
        "f.raw",
        "f.with_p",
        "f.with_p_1",
    ]
    assert driver.execute(["f"], inputs={"x": 1}) == {"f": 14}
