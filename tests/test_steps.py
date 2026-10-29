"""Steps: `step`, `@pipe_input` and `@pipe_output`, their names, bindings and misuse."""

import asyncio
from typing import override

import pytest

import fnloom
from fnloom import pipe_input, pipe_output, step, value
from helpers import make_module

STEPS = """
from fnloom import pipe_input, pipe_output, source, step, tag, value


def _add(v: int, n: int) -> int:
    return v + n


def _mul(v: int, k: int = 10) -> int:
    return v * k


def _sum(v: int, other: int) -> int:
    return v + other


@tag(team="t")
@pipe_input(
    step(_add, n=value(1)),
    step(_add, n=2),
    step(_mul),
    step(_sum, other=source("y")),
    step(_add, n=value(100)).named("custom"),
)
def fed(x: int, w: int = 0) -> int:
    return x + w


@tag(team="t")
@pipe_output(
    step(_add, n=1),
    step(_mul).when(mode="big"),
    step(_add, n=source("other")).named("last"),
)
def piped(x: int) -> int:
    return x
"""


def _driver(
    source: str = STEPS, name: str = "stepped", mode: str = "big"
) -> fnloom.Driver:
    module = make_module(name, source)
    return fnloom.Builder().with_modules(module).with_config({"mode": mode}).build()


def test_pipe_input_names_and_wiring() -> None:
    """`<fn>.with_<step>`, numbered for repeats, `.named`, chained (KEEP, H8.11)."""
    driver = _driver()
    deps = {name: driver.nodes[name].dependencies for name in driver.node_names()}
    assert deps["fed.with_add"] == ("x",)
    assert deps["fed.with_add_1"] == ("fed.with_add",)
    assert deps["fed.with_mul"] == ("fed.with_add_1",)
    assert deps["fed.with_sum"] == ("fed.with_mul", "y")
    assert deps["fed.custom"] == ("fed.with_sum",)
    assert deps["fed"] == ("fed.custom", "w")
    result = driver.execute(["fed", "fed.with_mul"], inputs={"x": 1, "y": 1000})
    assert result == {"fed": 1140, "fed.with_mul": 40}


def test_pipe_output_names_and_wiring() -> None:
    """`<fn>.raw`, the steps, then the node itself with the last step's value."""
    driver = _driver()
    deps = {name: driver.nodes[name].dependencies for name in driver.node_names()}
    assert deps["piped.raw"] == ("x",)
    assert deps["piped.with_add"] == ("piped.raw",)
    assert deps["piped.with_mul"] == ("piped.with_add",)
    assert deps["piped.last"] == ("other", "piped.with_mul")
    assert deps["piped"] == ("piped.last",)
    assert driver.execute(["piped", "piped.raw"], inputs={"x": 1, "other": 5}) == {
        "piped": 25,
        "piped.raw": 1,
    }


def test_tags_and_modules() -> None:
    """Tags on the final node only; each node's module is its function's (Q104)."""
    driver = _driver()
    assert dict(driver.nodes["piped"].tags) == {"team": "t"}
    assert dict(driver.nodes["piped.raw"].tags) == {}
    assert dict(driver.nodes["fed.with_add"].tags) == {}
    assert driver.nodes["piped"].module == "stepped"
    assert driver.nodes["piped.with_add"].module == "stepped"


def test_a_defaulted_step_parameter_is_no_input() -> None:
    """`k` keeps its default and is never a graph input (Q105, RF 3)."""
    driver = _driver()
    assert "k" not in driver.node_names()
    assert "k" not in driver.inputs()
    with pytest.raises(ValueError, match="unknown inputs: 'k'"):
        driver.execute(["piped"], inputs={"x": 1, "other": 0, "k": 3})


def test_names_do_not_change_with_the_config() -> None:
    """A step the config drops leaves the others' names as written (Q104)."""
    source = """
from fnloom import pipe_output, step

def _add(v: int, n: int) -> int:
    return v + n

@pipe_output(step(_add, n=1).when(mode="a"), step(_add, n=2))
def a(x: int) -> int:
    return x
"""
    with_a = _driver(source, "stable_a", mode="a")
    without = _driver(source, "stable_b", mode="b")
    assert "a.with_add_1" in with_a.node_names()
    assert "a.with_add_1" in without.node_names()
    assert "a.with_add" not in without.node_names()
    assert without.execute(["a"], inputs={"x": 1}) == {"a": 3}


def test_steps_dropped_by_the_config_leave_the_node_alone() -> None:
    """No `.raw` when no step is kept."""
    driver = _driver(mode="small")
    assert "piped.with_mul" not in driver.node_names()
    assert driver.execute(["piped"], inputs={"x": 1, "other": 5}) == {"piped": 7}


def test_on_input() -> None:
    """The steps feed the parameter named, not the first."""
    source = """
from fnloom import pipe_input, step

def _add(v: int, n: int) -> int:
    return v + n

@pipe_input(step(_add, n=1), on_input="b")
def f(a: int, b: int) -> int:
    return a * 100 + b
"""
    driver = _driver(source, "on_input")
    assert driver.nodes["f.with_add"].dependencies == ("b",)
    assert driver.execute(["f"], inputs={"a": 1, "b": 2}) == {"f": 103}


def test_on_output_picks_fields() -> None:
    """On an `@extract_fields` function, the steps apply to the fields named."""
    source = """
from fnloom import extract_fields, pipe_output, step

def _double(v: int) -> int:
    return v * 2

@pipe_output(step(_double).on_output("p"))
@extract_fields({"p": int, "q": int})
def d(x: int) -> dict:
    return {"p": x, "q": x}
"""
    driver = _driver(source, "on_output")
    assert driver.nodes["p"].dependencies == ("p.with_double",)
    assert driver.nodes["q"].dependencies == ("d",)
    assert driver.execute(["p", "q"], inputs={"x": 3}) == {"p": 6, "q": 3}
    unknown = source.replace('.on_output("p")', '.on_output("zz")')
    with pytest.raises(ValueError, match="on_output names 'zz'"):
        _driver(unknown, "on_output_unknown")


def test_rows_each_get_their_chain() -> None:
    """`@pipe_input` with `@parameterize`: one chain per row (Q108)."""
    source = """
from fnloom import parameterize, pipe_input, step, value

def _add(v: int, n: int) -> int:
    return v + n

@pipe_input(step(_add, n=1))
@parameterize(low={"k": value(1)}, high={"k": value(10)})
def scaled(x: int, k: int) -> int:
    return x * k
"""
    driver = _driver(source, "rows")
    assert driver.nodes["low"].dependencies == ("low.with_add",)
    assert driver.execute(["low", "high"], inputs={"x": 1}) == {"low": 2, "high": 20}


def test_a_checked_piped_node_checks_the_final_value() -> None:
    """Whatever the decorator order (Q103)."""
    for index, order in enumerate(
        (
            "@check_output(range=(0, 5))\n@pipe_output(step(_add, n=10))",
            "@pipe_output(step(_add, n=10))\n@check_output(range=(0, 5))",
        )
    ):
        source = f"""
from fnloom import check_output, pipe_output, step

def _add(v: int, n: int) -> int:
    return v + n

{order}
def a(x: int) -> int:
    return x
"""
        driver = _driver(source, f"checked_pipe_{index}")
        with pytest.raises(fnloom.ValidationError, match="a: check range failed: 11"):
            driver.execute(["a"], inputs={"x": 1})
        assert driver.execute(["a.raw"], inputs={"x": 1}) == {"a.raw": 1}


def test_the_head_of_a_chain_is_type_checked() -> None:
    """A first step that can't take the parameter's type fails the build (Q106)."""
    source = """
from fnloom import pipe_input, step

def _upper(v: str) -> int:
    return len(v)

@pipe_input(step(_upper))
def f(x: int) -> int:
    return x
"""
    with pytest.raises(ValueError, match="parameter 'x' is int, but its first step"):
        _driver(source, "head")


def test_a_type_mismatch_inside_the_chain_fails_the_build() -> None:
    """As any edge does (KEEP, H8.18)."""
    source = """
from fnloom import pipe_output, step

def _text(v: int) -> str:
    return str(v)

def _plus(v: int) -> int:
    return v + 1

@pipe_output(step(_text), step(_plus))
def f(x: int) -> int:
    return x
"""
    with pytest.raises(ValueError, match=r"f\.with_plus"):
        _driver(source, "inner")


def _two(v: int, n: int) -> int:
    return v + n


async def _later(v: int) -> int:
    return v


def _bare(v, n: int) -> int:  # noqa: ANN001  # unannotated on purpose
    del v
    return n


def _none() -> int:
    return 0


def _star(v: int, *rest: int) -> int:
    del rest
    return v


@pytest.mark.parametrize(
    ("make", "match"),
    [
        (lambda: step(_two), "unbound required parameters: n"),
        (lambda: step(_two, n=1, zz=2), "no parameter named zz"),
        (lambda: step(_two, 1, n=2), "bound twice: n"),
        (lambda: step(_two, 1, 2), "2 values given for 1 parameters"),
        (lambda: step(_later), "async def"),
        (lambda: step(_bare, n=1), "need annotations: v"),
        (lambda: step(_none), "no parameter to take the value"),
        (lambda: step(_star), r"\*args, \*\*kwargs"),
        (lambda: step(3), "takes a function"),  # ty: ignore[invalid-argument-type]
        (pipe_input, "at least one step"),
        (lambda: pipe_output(_two), "takes step"),  # ty: ignore[invalid-argument-type]
        (lambda: step(_two, n=1).named("not a name"), "identifier"),
        (lambda: step(_two, n=1).on_output(), "field names"),
    ],
)
def test_misuse_is_refused_when_decorating(make: object, match: str) -> None:
    """Each with a message naming the step (Q106)."""
    assert callable(make)
    with pytest.raises((TypeError, ValueError), match=match):
        make()


def test_pipe_input_on_the_wrong_parameter_or_none() -> None:
    """No parameter to feed, or none of the name given (Q106)."""

    def nothing() -> int:
        return 0

    def one(x: int) -> int:
        return x

    with pytest.raises(TypeError, match="no parameter to feed"):
        pipe_input(step(_two, n=1))(nothing)
    with pytest.raises(TypeError, match="no parameter named 'zz'"):
        pipe_input(step(_two, n=1), on_input="zz")(one)


def test_steps_are_cached_and_hooked() -> None:
    """Ordinary nodes: each step is its own cache entry and gets node events."""
    seen: list[str] = []

    class Seen(fnloom.Hook):
        @override
        def after_node(self, event: fnloom.NodeEnd) -> None:
            seen.append(event.node.name)

    module = make_module("cached_steps", STEPS)
    driver = (
        fnloom.Builder()
        .with_modules(module)
        .with_config({"mode": "big"})
        .with_cache(fnloom.MemoryCache())
        .with_hooks(Seen())
        .build()
    )
    driver.execute(["piped"], inputs={"x": 1, "other": 5})
    assert seen == [
        "piped.raw",
        "piped.with_add",
        "piped.with_mul",
        "piped.last",
        "piped",
    ]
    outcomes = driver.explain_cache(["piped"], inputs={"x": 1, "other": 5})
    assert {name: o.status for name, o in outcomes.items()} == dict.fromkeys(
        ["piped", "piped.last", "piped.raw", "piped.with_add", "piped.with_mul"], "hit"
    )


def test_the_async_driver_runs_steps() -> None:
    """The same graph and values."""
    module = make_module("async_steps", STEPS)
    driver = (
        fnloom.Builder().with_modules(module).with_config({"mode": "big"}).build_async()
    )
    result = asyncio.run(
        driver.execute(["piped", "fed"], inputs={"x": 1, "y": 0, "other": 5})
    )
    assert result == {"piped": 25, "fed": 140}


def test_a_step_function_passed_as_an_output_is_no_step() -> None:
    """A step's node stands for no function in a request."""
    source = STEPS.replace("def _add(", "def add(").replace("step(_add", "step(add")
    module = make_module("public_step", source)
    driver = fnloom.Builder().with_modules(module).with_config({"mode": "big"}).build()
    assert driver.execute([module.add], inputs={"v": 1, "n": 2}) == {"add": 3}


def test_namespaced_steps_read_the_namespace() -> None:
    """A step's `source` reads the namespace's node, as a parameter does (Q74)."""
    source = """
from fnloom import pipe_output, source, step

def _plus(v: int, other: int) -> int:
    return v + other

def other() -> int:
    return 100

@pipe_output(step(_plus, other=source("other")))
def a(x: int) -> int:
    return x
"""
    module = make_module("namespaced_steps", source)
    driver = fnloom.Builder().with_modules(module, namespace="ns").build()
    assert driver.nodes["ns.a.with_plus"].dependencies == ("ns.a.raw", "ns.other")
    assert driver.execute(["ns.a"], inputs={"x": 1}) == {"ns.a": 101}


def test_value_bindings_are_constants() -> None:
    """`value(...)` and bare literals are passed as given."""
    module = make_module("bound", "def a(x: int) -> int:\n    return x\n")

    def _join(v: int, sep: str, times: int = 1) -> str:
        return sep.join([str(v)] * times)

    pipe_output(step(_join, sep=value("-"), times=3))(module.a)
    assert fnloom.build(module).execute(["a"], inputs={"x": 7}) == {"a": "7-7-7"}


def test_an_async_function_with_pipe_output_on_the_async_driver() -> None:
    """Its final node is awaited, checked or not."""
    source = """
from fnloom import check_output, pipe_output, step

def _b(v: int) -> int:
    return v + 10

@pipe_output(step(_b))
async def p(x: int) -> int:
    return x

@check_output(range=(0, 50))
@pipe_output(step(_b))
async def q(x: int) -> int:
    return x
"""
    module = make_module("async_piped", source)
    driver = fnloom.Builder().with_modules(module).build_async()
    assert asyncio.run(driver.execute(["p", "q"], inputs={"x": 2})) == {
        "p": 12,
        "q": 12,
    }
    with pytest.raises(fnloom.ValidationError, match="q: check range"):
        asyncio.run(driver.execute(["q"], inputs={"x": 60}))


def test_a_group_parameter_is_never_fed() -> None:
    """Refused at build, whether named or first."""
    for index, on_input in enumerate(("'xs'", "None")):
        source = f"""
from fnloom import inject, pipe_input, source, step, group

def _inc(v: int) -> int:
    return v + 1

@pipe_input(step(_inc), on_input={on_input})
@inject(xs=group(source("a"), source("b")))
def tot(xs: list[int]) -> int:
    return sum(xs)
"""
        with pytest.raises(ValueError, match="can't feed 'xs', a group"):
            _driver(source, f"grouped_{index}")


def test_a_fed_parameter_with_a_default_reads_the_chain() -> None:
    """The chain always gives it a value, so its default never applies."""
    source = """
from fnloom import pipe_input, step

def _inc(v: int) -> int:
    return v + 1

@pipe_input(step(_inc))
def p(x: int = 1) -> int:
    return x
"""
    driver = _driver(source, "defaulted_fed")
    assert driver.execute(["p"], inputs={"x": 5}) == {"p": 6}


def test_a_positional_only_step_parameter_is_refused() -> None:
    """When decorating, rather than crashing at run."""

    def _inc(v: int, /) -> int:
        return v + 1

    with pytest.raises(TypeError, match="positional-only"):
        step(_inc)


def test_a_fed_parameter_bound_to_a_value_says_so() -> None:
    """The message names the `value()` binding."""
    source = """
from fnloom import parameterize, pipe_input, step, value

def _inc(v: int) -> int:
    return v + 1

@pipe_input(step(_inc))
@parameterize(r={"x": value(1)})
def p(x: int) -> int:
    return x
"""
    with pytest.raises(ValueError, match="bound to a value"):
        _driver(source, "value_fed")


def test_one_step_in_pipe_input_and_pipe_output_says_to_name_one() -> None:
    """The duplicate-name error points at `.named`."""
    source = """
from fnloom import pipe_input, pipe_output, step

def _a(v: int) -> int:
    return v

@pipe_input(step(_a))
@pipe_output(step(_a))
def p(x: int) -> int:
    return x
"""
    with pytest.raises(ValueError, match=r"\.named"):
        _driver(source, "named_twice")
