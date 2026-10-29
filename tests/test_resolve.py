"""`@inject` and `@resolve`: bindings that keep the name, and decorators from config."""

from typing import TYPE_CHECKING

import pytest

import fnloom
from fnloom import inject, parameterize, resolve, value
from helpers import make_module

if TYPE_CHECKING:
    from collections.abc import Mapping

INJECTED = """
from fnloom import group, inject, source, value

@inject(nums=group(source("a"), value(10), source("b")), k=value(2))
def total(nums: list[int], k: int, extra: int = 0) -> int:
    \"\"\"The total, times {k}.\"\"\"
    return (sum(nums) + extra) * k

@inject(parts=group(first=source("a"), second=value(5)))
def keyed(parts: dict[str, int]) -> int:
    return parts["first"] * 100 + parts["second"]
"""


def test_inject_keeps_the_name_and_templates_the_doc() -> None:
    """One node of the function's name; groups as lists and dicts (KEEP, Q109)."""
    module = make_module("injected", INJECTED)
    driver = fnloom.build(module)
    assert driver.nodes["total"].dependencies == ("a", "b", "extra")
    assert driver.nodes["total"].doc == "The total, times 2."
    assert driver.execute(["total", "keyed"], inputs={"a": 1, "b": 2}) == {
        "total": 26,
        "keyed": 105,
    }
    assert driver.execute([module.total], inputs={"a": 1, "b": 2}) == {"total": 26}


def test_inject_refusals() -> None:
    """A bare value, an unknown parameter, nothing, and with `@parameterize`."""

    def f(x: int, y: int) -> int:
        return x + y

    with pytest.raises(TypeError, match="expected source"):
        inject(x=3)(f)  # ty: ignore[invalid-argument-type]
    with pytest.raises(ValueError, match="parameters named by"):
        inject(z=value(1))(f)
    with pytest.raises(TypeError, match="at least one binding"):
        inject()
    with pytest.raises(TypeError, match="can't share a function"):
        inject(x=value(1))(parameterize(r={"y": value(2)})(f))

    def g(x: int, y: int) -> int:
        return x + y

    with pytest.raises(TypeError, match="can't share a function"):
        parameterize(r={"y": value(2)})(inject(x=value(1))(g))


def test_inject_with_a_namespace_and_config() -> None:
    """Named under the namespace; a variant keeps its node name."""
    source_text = """
from fnloom import config, inject, source

def base() -> int:
    return 7

@inject(v=source("base"))
@config.when(mode="a")
def picked__a(v: int) -> int:
    return v
"""
    module = make_module("injected_variant", source_text)
    driver = (
        fnloom.Builder()
        .with_modules(module, namespace="ns")
        .with_config({"mode": "a"})
        .build()
    )
    assert driver.nodes["ns.picked"].dependencies == ("ns.base",)
    assert driver.execute(["ns.picked"]) == {"ns.picked": 7}


RESOLVED = """
from fnloom import parameterize, resolve, source, tag, value, extract_columns

@resolve(
    lambda pairs, scale=3: parameterize(
        **{
            f"sum_{a}_{b}": {"p": source(a), "q": source(b), "s": value(scale)}
            for a, b in pairs
        }
    )
)
@tag(team="t")
def summation(p: int, q: int, s: int) -> int:
    return (p + q) * s

@resolve(lambda cols: extract_columns(*cols))
def frame(x: int) -> dict:
    return {"c1": [x], "c2": [x * 2], "c3": [0]}
"""


def _resolved(config: Mapping[str, object], name: str = "resolved") -> fnloom.Driver:
    module = make_module(name, RESOLVED)
    return fnloom.Builder().with_modules(module).with_config(config).build()


def test_resolve_applies_what_the_config_says() -> None:
    """Rows and columns from config; optional keys by default (KEEP, Q110, RF 4)."""
    config = {"pairs": [("u", "v"), ("v", "w")], "cols": ["c1", "c2"]}
    driver = _resolved(config)
    assert "summation" not in driver.node_names()
    assert dict(driver.nodes["sum_u_v"].tags) == {"team": "t"}
    assert driver.execute(
        ["sum_u_v", "sum_v_w", "c2"], inputs={"u": 1, "v": 2, "w": 3, "x": 1}
    ) == {"sum_u_v": 9, "sum_v_w": 15, "c2": [2]}
    scaled = _resolved({**config, "scale": 10}, "resolved_scaled")
    assert scaled.execute(["sum_u_v"], inputs={"u": 1, "v": 2}) == {"sum_u_v": 30}


def test_resolve_leaves_the_function_alone() -> None:
    """Two builds with other configs each see their own decorators."""
    module = make_module("resolved_twice", RESOLVED)
    first = (
        fnloom.Builder()
        .with_modules(module)
        .with_config({"pairs": [("u", "v")], "cols": ["c1"]})
    )
    second = (
        fnloom.Builder()
        .with_modules(module)
        .with_config({"pairs": [("a", "b")], "cols": ["c2"]})
    )
    assert "sum_u_v" in first.build().node_names()
    names = second.build().node_names()
    assert "sum_a_b" in names
    assert "sum_u_v" not in names
    assert fnloom._decorators.decorations(module.summation).rows is None  # noqa: SLF001  # the function's own record


def test_a_resolved_node_is_the_function_as_written() -> None:
    """In requests and views, the copy stands for the function."""
    module = make_module("resolved_origin", RESOLVED)
    driver = (
        fnloom.Builder()
        .with_modules(module)
        .with_config({"pairs": [("u", "v")], "cols": ["c1"]})
        .build()
    )
    assert driver.nodes["frame"].origin is module.frame
    assert driver.execute([module.frame], inputs={"x": 1}) == {
        "frame": {"c1": [1], "c2": [2], "c3": [0]}
    }


def test_a_missing_key_names_the_function() -> None:
    """A ValueError at build, rather than Hamilton's raw KeyError (Q110)."""
    with pytest.raises(
        ValueError, match="resolve on summation: the config has no 'pairs'"
    ):
        _resolved({"cols": ["c1"]}, "resolved_missing")


def test_resolve_refusals() -> None:
    """Variadic resolvers when decorating; bad returns at build."""
    with pytest.raises(TypeError, match="must name each config key"):
        resolve(lambda **kw: kw)
    with pytest.raises(TypeError, match="takes a function"):
        resolve(3)  # ty: ignore[invalid-argument-type]
    for index, (made, match) in enumerate(
        (
            ("42", "must return decorators, got int"),
            ("fnloom.config.when(mode='a')", r"can't apply @config\.when"),
            ("fnloom.exclude", "can't apply @exclude"),
        )
    ):
        source_text = f"""
import fnloom

@fnloom.resolve(lambda: {made})
def a(x: int) -> int:
    return x
"""
        module = make_module(f"bad_resolve_{index}", source_text)
        with pytest.raises(TypeError, match=match):
            fnloom.build(module)


def test_several_decorators_and_resolvers_stack() -> None:
    """A sequence, first outermost, and two `@resolve`s."""
    source_text = """
import fnloom

@fnloom.resolve(lambda label: fnloom.tag(label=label))
@fnloom.resolve(lambda: [fnloom.check_output(range=(0, 5)), fnloom.tag(kind="k")])
def a(x: int) -> int:
    return x
"""
    module = make_module("stacked_resolve", source_text)
    driver = fnloom.Builder().with_modules(module).with_config({"label": "l"}).build()
    assert dict(driver.nodes["a"].tags) == {"kind": "k", "label": "l"}
    with pytest.raises(fnloom.ValidationError):
        driver.execute(["a"], inputs={"x": 9})


def test_injected_rows_take_pipe_input() -> None:
    """`@pipe_input` with `@inject`: the one row gets the chain (Q108)."""
    source_text = """
from fnloom import inject, pipe_input, source, step

def _inc(v: int) -> int:
    return v + 1

@pipe_input(step(_inc))
@inject(v=source("a"))
def piped(v: int) -> int:
    return v
"""
    driver = fnloom.build(make_module("injected_piped", source_text))
    assert driver.nodes["piped.with_inc"].dependencies == ("a",)
    assert driver.execute(["piped"], inputs={"a": 1}) == {"piped": 2}


def test_an_unselected_variant_s_resolver_never_runs() -> None:
    """Only the variants the config keeps are resolved."""
    source_text = """
from fnloom import config, resolve, tag

@config.when(mode="a")
@resolve(lambda needed: tag(n=needed))
def var__a(x: int) -> int:
    return x

@config.when(mode="b")
def var__b(x: int) -> int:
    return x * 2
"""
    module = make_module("resolve_variant", source_text)
    driver = fnloom.Builder().with_modules(module).with_config({"mode": "b"}).build()
    assert driver.execute(["var"], inputs={"x": 1}) == {"var": 2}


def test_resolved_fields_are_namespace_names() -> None:
    """A field a resolved `@extract_fields` adds is read within the namespace."""
    source_text = """
from fnloom import extract_fields, resolve

@resolve(lambda: extract_fields({"p": int}))
def fields(x: int) -> dict:
    return {"p": x}

def reads_p(p: int) -> int:
    return p
"""
    module = make_module("resolve_namespaced", source_text)
    driver = fnloom.Builder().with_modules(module, namespace="ns").build()
    assert driver.nodes["ns.reads_p"].dependencies == ("ns.p",)
    assert driver.execute(["ns.reads_p"], inputs={"x": 4}) == {"ns.reads_p": 4}


def test_export_reads_the_resolved_decorators() -> None:
    """A `@cache(version=...)` a resolver applies is the exported pin."""
    source_text = """
import fnloom

@fnloom.resolve(lambda: fnloom.cache(version="v9"))
def a(x: int) -> int:
    return x
"""
    driver = fnloom.build(make_module("resolve_export", source_text))
    (node,) = [n for n in driver.export()["nodes"] if n["name"] == "a"]
    assert node["pinned"] == "v9"


def test_a_resolver_s_own_error_names_it() -> None:
    """Its exception, with a note saying which `@resolve` raised it."""
    source_text = """
import fnloom

@fnloom.resolve(lambda: fnloom.tag(n=3))
def a(x: int) -> int:
    return x
"""
    with pytest.raises(TypeError) as caught:
        fnloom.build(make_module("resolve_raising", source_text))
    assert "in resolve on a" in caught.value.__notes__


def test_a_refused_resolve_changes_no_other_function() -> None:
    """A resolver applying `@mutate` is refused before it writes to a target (Q110)."""
    source_text = """
import fnloom

def target(x: int) -> int:
    return x

def _host(v: int) -> int:
    return v + 100

@fnloom.resolve(lambda flag=False: fnloom.mutate(target) if flag else fnloom.tag(t="1"))
def host(x: int) -> int:
    return x
"""
    module = make_module("resolve_mutate", source_text)
    with pytest.raises(TypeError, match="can't apply @mutate"):
        fnloom.Builder().with_modules(module).with_config({"flag": True}).build()
    clean = fnloom.Builder().with_modules(module).with_config({"flag": False}).build()
    assert "target.raw" not in clean.node_names()
    assert clean.execute(["target"], inputs={"x": 1}) == {"target": 1}
