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
    """`.raw` is still made, so names don't depend on the config (Q104)."""
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


def test_raw_is_made_when_the_config_keeps_no_step() -> None:
    """Names never depend on the config: `.raw` stays (Q104)."""
    source = """
from fnloom import pipe_output, step

def _inc(v: int) -> int:
    return v + 1

@pipe_output(step(_inc).when(mode="a"))
def a(x: int) -> int:
    return x
"""
    kept = _driver(source, "raw_kept", mode="a")
    dropped = _driver(source, "raw_dropped", mode="b")
    assert "a.raw" in kept.node_names()
    assert "a.raw" in dropped.node_names()
    assert dropped.execute(["a"], inputs={"x": 1}) == {"a": 1}


# ---- namespace= and on_output= (Q140) ---------------------------------------------

NAMESPACED = """
from fnloom import pipe_input, pipe_output, step

def _inc(v: int) -> int:
    return v + 1

def _dbl(v: int) -> int:
    return v * 2

@pipe_input(step(_inc), namespace=None)
def bare(a: int) -> int:
    return a

@pipe_input(step(_inc), step(_dbl), namespace="sales")
def shared(a: int) -> int:
    return a

@pipe_input(step(_inc).named("bump", namespace="taxes"), step(_dbl).named("twice"))
def renamed(a: int) -> int:
    return a

@pipe_input(step(_inc).named("lone", namespace=None))
def alone(a: int) -> int:
    return a

@pipe_output(step(_dbl), namespace=None)
def out_bare(a: int) -> int:
    return a

@pipe_output(step(_inc), namespace="regions")
def out_shared(a: int) -> int:
    return a

@pipe_output(step(_inc).named("first", namespace="eu"), step(_dbl).named("then"))
def out_named(a: int) -> int:
    return a
"""


def test_namespace_names_the_steps() -> None:
    """`...` prefixes `<node>.`, `None` nothing, a `str` itself (Q140)."""
    driver = _driver(NAMESPACED, "namespaced")
    deps = {name: driver.nodes[name].dependencies for name in driver.node_names()}
    assert deps["with_inc"] == ("a",)
    assert deps["bare"] == ("with_inc",)
    assert deps["sales.with_inc"] == ("a",)
    assert deps["sales.with_dbl"] == ("sales.with_inc",)
    assert deps["shared"] == ("sales.with_dbl",)
    assert deps["taxes.bump"] == ("a",)
    assert deps["renamed.twice"] == ("taxes.bump",)
    assert deps["lone"] == ("a",)
    # `.raw` and the node itself keep their names; only the steps move.
    assert deps["with_dbl"] == ("out_bare.raw",)
    assert deps["out_bare"] == ("with_dbl",)
    assert deps["regions.with_inc"] == ("out_shared.raw",)
    assert deps["eu.first"] == ("out_named.raw",)
    assert deps["out_named.then"] == ("eu.first",)
    names = [
        "bare",
        "shared",
        "renamed",
        "alone",
        "out_bare",
        "out_shared",
        "out_named",
    ]
    assert driver.execute(names, inputs={"a": 3}) == {
        "bare": 4,
        "shared": 8,
        "renamed": 8,
        "alone": 4,
        "out_bare": 6,
        "out_shared": 4,
        "out_named": 8,
    }


def test_a_steps_own_namespace_wins() -> None:
    """`.named(n, namespace=)` given beats the decorator's; `...` inherits it."""
    source = """
from fnloom import pipe_input, step

def _inc(v: int) -> int:
    return v + 1

@pipe_input(
    step(_inc).named("own", namespace="eu"),
    step(_inc).named("kept"),
    step(_inc).named("none", namespace=None),
    namespace="sales",
)
def f(a: int) -> int:
    return a
"""
    driver = _driver(source, "own_namespace")
    assert {"eu.own", "sales.kept", "none"} <= set(driver.node_names())
    assert driver.execute(["f"], inputs={"a": 0}) == {"f": 3}


def test_namespaces_under_a_module_namespace() -> None:
    """Under `with_modules(namespace=)`, a step's namespace is within it (Q74)."""
    module = make_module("module_namespaced", NAMESPACED)
    driver = fnloom.Builder().with_modules(module, namespace="eu").build()
    names = set(driver.node_names())
    assert {"eu.with_inc", "eu.sales.with_inc", "eu.taxes.bump", "eu.lone"} <= names
    assert {"eu.renamed.twice", "eu.with_dbl", "eu.out_bare.raw"} <= names
    assert driver.execute(["eu.shared"], inputs={"a": 3}) == {"eu.shared": 8}


def _namespaced_input(namespace: object) -> object:
    return pipe_input(step(_two, n=1), namespace=namespace)  # ty: ignore[invalid-argument-type]  # refused is the point


def _namespaced_output(namespace: object) -> object:
    return pipe_output(step(_two, n=1), namespace=namespace)  # ty: ignore[invalid-argument-type]  # refused is the point


def _named_in(namespace: object) -> object:
    return step(_two, n=1).named("x", namespace=namespace)  # ty: ignore[invalid-argument-type]  # refused is the point


def _fields(on_output: object) -> object:
    return pipe_output(step(_two, n=1), on_output=on_output)  # ty: ignore[invalid-argument-type]  # refused is the point


@pytest.mark.parametrize(
    ("make", "given", "error", "match"),
    [
        (_namespaced_input, "1x", ValueError, "'1x' is not a dotted"),
        (_namespaced_input, "", ValueError, "dotted"),
        (_namespaced_output, "a.", ValueError, "dotted"),
        (_namespaced_output, 3, TypeError, "str or None"),
        (_named_in, "a b", ValueError, "dotted"),
        (_named_in, 1, TypeError, "str or None"),
        (_fields, (), TypeError, "on_output"),
        (_fields, 3, TypeError, "a str, list or tuple"),
        (_fields, ["p", 3], TypeError, "on_output"),
        (_fields, {"p"}, TypeError, "on_output"),
        (_fields, ["p", "p"], ValueError, "'p' twice"),
    ],
)
def test_bad_namespaces_and_fields_are_refused_when_decorating(
    make: object, given: object, error: type[Exception], match: str
) -> None:
    """A namespace must be a dotted identifier path, as a module's is (Q74, Q140)."""
    assert callable(make)
    with pytest.raises(error, match=match):
        make(given)


def test_dotted_namespaces_are_paths() -> None:
    """`namespace="eu.sales"` prefixes both parts."""
    source = """
from fnloom import pipe_input, step

def _inc(v: int) -> int:
    return v + 1

@pipe_input(step(_inc), namespace="eu.sales")
def f(a: int) -> int:
    return a
"""
    driver = _driver(source, "dotted_steps")
    assert driver.nodes["f"].dependencies == ("eu.sales.with_inc",)


ON_OUTPUT = """
from fnloom import extract_fields, pipe_output, step

def _inc(v: int) -> int:
    return v + 1

def _dbl(v: int) -> int:
    return v * 2

@pipe_output(step(_inc), step(_dbl).on_output("z"), on_output=("x", "y"))
@extract_fields({"x": int, "y": int, "z": int})
def d(a: int) -> dict:
    return {"x": a, "y": a * 10, "z": a * 100}
"""


def test_on_output_applies_every_step_and_a_steps_own_wins() -> None:
    """`on_output=` picks the fields for each step that names none (Q140)."""
    driver = _driver(ON_OUTPUT, "on_output_all")
    names = set(driver.node_names())
    assert {"x.with_inc", "y.with_inc", "z.with_dbl"} <= names
    assert not {"x.with_dbl", "y.with_dbl", "z.with_inc"} & names
    assert driver.execute(["x", "y", "z"], inputs={"a": 1}) == {
        "x": 2,
        "y": 11,
        "z": 200,
    }
    one = ON_OUTPUT.replace('on_output=("x", "y")', 'on_output="y"')
    assert _driver(one, "on_output_one").execute(["x", "y"], inputs={"a": 1}) == {
        "x": 1,
        "y": 11,
    }


def test_on_output_naming_no_field_is_refused_at_build() -> None:
    """A field the function doesn't extract is a `ValueError` naming it (Q140)."""
    unknown = ON_OUTPUT.replace('on_output=("x", "y")', 'on_output=("x", "w")')
    with pytest.raises(ValueError, match="d: on_output names 'w'"):
        _driver(unknown, "on_output_w")


ON_ITSELF = """
from fnloom import pipe_output, step

def _inc(v: int) -> int:
    return v + 1

@pipe_output(step(_inc), on_output="k")
def k(a: int) -> int:
    return a
"""


@pytest.mark.parametrize(
    "changed",
    [
        ON_ITSELF.replace('on_output="k"', 'on_output="zz"'),
        ON_ITSELF.replace('step(_inc), on_output="k"', 'step(_inc).on_output("zz")'),
    ],
)
def test_on_output_on_a_function_without_fields(changed: str) -> None:
    """Its own name is the one target; any other is refused, not ignored."""
    assert _driver(ON_ITSELF, "own_target").execute(["k"], inputs={"a": 1}) == {"k": 2}
    with pytest.raises(ValueError, match=r"k: on_output names 'zz'.*no fields"):
        _driver(changed, "other_target")


_TWICE_HEADER = """
from fnloom import extract_fields, pipe_input, pipe_output, step

def _inc(v: int) -> int:
    return v + 1

def _dbl(v: int) -> int:
    return v * 2
"""


@pytest.mark.parametrize(
    ("source", "match"),
    [
        (
            """
@pipe_input(step(_inc), namespace=None)
def f(a: int) -> int:
    return a

@pipe_input(step(_dbl).named("with_inc", namespace=None))
def g(a: int) -> int:
    return a
""",
            r"'with_inc' is made twice: by a step of 'f' and by a step of 'g'",
        ),
        (
            """
def with_inc(a: int) -> int:
    return a

@pipe_input(step(_inc), namespace=None)
def f(a: int) -> int:
    return a
""",
            r"'with_inc' is made twice: by a step of 'f' and by the function 'with_inc",
        ),
        (
            """
@pipe_input(step(_inc), namespace="sales")
def f(a: int) -> int:
    return a

@pipe_output(step(_inc), namespace="sales")
def g(a: int) -> int:
    return a
""",
            r"'sales.with_inc' is made twice: by a step of 'f' and by a step of 'g'",
        ),
        (
            """
@pipe_output(step(_inc), namespace=None)
@extract_fields({"x": int, "y": int})
def d(a: int) -> dict:
    return {"x": a, "y": a}
""",
            r"'with_inc' is made twice: by a step of 'x' and by a step of 'y'",
        ),
    ],
)
def test_a_name_made_twice_names_both(source: str, match: str) -> None:
    """A shared or prefix-free step name another node has fails the build (Q140)."""
    with pytest.raises(ValueError, match=match):
        _driver(_TWICE_HEADER + source, "made_twice")


def test_namespaced_names_do_not_change_with_the_config() -> None:
    """A dropped step leaves the others' numbers, under a namespace too (Q104)."""
    source = """
from fnloom import pipe_input, step

def _inc(v: int) -> int:
    return v + 1

@pipe_input(step(_inc).when(mode="a"), step(_inc), namespace=None)
def f(a: int) -> int:
    return a
"""
    with_a = _driver(source, "ns_stable_a", mode="a")
    without = _driver(source, "ns_stable_b", mode="b")
    assert {"with_inc", "with_inc_1"} <= set(with_a.node_names())
    assert "with_inc_1" in without.node_names()
    assert "with_inc" not in without.node_names()
    assert without.execute(["f"], inputs={"a": 0}) == {"f": 1}


def test_mutate_targets_take_a_namespace() -> None:
    """`apply_to(fn).named(n, namespace=)` names the step as `Step.named` does."""
    source = """
from fnloom import apply_to, mutate

def total(a: int) -> int:
    return a

@mutate(apply_to(total).named("rounded", namespace="taxes"))
def _round(v: int) -> int:
    return v + 1
"""
    driver = _driver(source, "mutate_namespace")
    assert driver.nodes["total"].dependencies == ("taxes.rounded",)
    assert driver.execute(["total"], inputs={"a": 1}) == {"total": 2}


def test_on_output_picks_parameterize_rows() -> None:
    """Without fields, `on_output` names rows: the others are left alone (Q140)."""
    source = """
from fnloom import parameterize, pipe_output, step, value

def _inc(v: int) -> int:
    return v + 1

@pipe_output(step(_inc), on_output="p1")
@parameterize(p1={"k": value(1)}, p2={"k": value(2)})
def base(k: int) -> int:
    return k
"""
    driver = _driver(source, "rows_on_output")
    assert {"p1.raw", "p1.with_inc"} <= set(driver.node_names())
    assert "p2.raw" not in driver.node_names()
    assert driver.execute(["p1", "p2"]) == {"p1": 2, "p2": 2}
    with pytest.raises(ValueError, match=r"names 'zz'.*name 'p1', 'p2'"):
        _driver(source.replace('"p1")', '"zz")'), "rows_on_output_zz")


def test_a_step_a_module_namespace_reads_from_outside_is_refused() -> None:
    """Under `with_modules(namespace=)`, a prefix-free step can't shadow a read."""
    source = """
from fnloom import pipe_output, step

def _inc(v: int) -> int:
    return v + 1

@pipe_output(step(_inc).named("z", namespace=None))
def f(x: int) -> int:
    return x

def g(z: int) -> int:
    return z * 100
"""
    module = make_module("shadowed", source)
    assert fnloom.build(module).execute(["g"], inputs={"x": 1}) == {"g": 200}
    with pytest.raises(ValueError, match=r"eu\.g reads 'z' from outside namespace"):
        fnloom.Builder().with_modules(module, namespace="eu").build()


def test_a_named_step_counts_toward_the_numbers() -> None:
    """As in Hamilton, a named step of a function numbers the unnamed after it."""
    source = """
from fnloom import pipe_input, step

def _inc(v: int) -> int:
    return v + 1

@pipe_input(step(_inc).named("with_inc", namespace="p"), step(_inc))
def f(a: int) -> int:
    return a
"""
    driver = _driver(source, "named_counts")
    assert driver.nodes["f"].dependencies == ("f.with_inc_1",)
    assert driver.nodes["f.with_inc_1"].dependencies == ("p.with_inc",)


_BOUND = """
from fnloom import pipe_output, step

def _inc(v: int) -> int:
    return v + 1

@pipe_output(step(_inc).named("x", namespace=None))
def f(a: int) -> int:
    return a

def g(x: int) -> int:
    return x * 100
"""


def test_a_step_named_like_a_bind_key_is_refused() -> None:
    """A step is a node of its namespace: `bind=` of its name is refused (Q140).

    A function of that name would be refused too (Q112).
    """
    module = make_module("bound_step", _BOUND)
    with pytest.raises(
        ValueError, match=r"bind names 'x' under namespace 'eu'.*'eu\.x'"
    ):
        fnloom.Builder().with_modules(
            module, namespace="eu", bind={"x": fnloom.source("y")}
        ).build()


def test_a_step_reading_its_own_name_from_outside_is_a_cycle() -> None:
    """The step is the reader: the message says it would read itself."""
    source = """
from fnloom import pipe_input, step

def _inc(v: int) -> int:
    return v + 1

@pipe_input(step(_inc).named("a", namespace=None))
def f(a: int) -> int:
    return a
"""
    module = make_module("self_read", source)
    with pytest.raises(ValueError, match=r"step 'eu\.a' reads 'a'.*itself, a cycle"):
        fnloom.Builder().with_modules(module, namespace="eu").build()
