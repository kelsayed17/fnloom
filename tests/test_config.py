"""Tests for the config decorators and for the build resolving them."""

import importlib
import importlib.util
import re
import sys
import textwrap
from types import MappingProxyType
from typing import TYPE_CHECKING, override

import pytest

import fnloom
from fnloom import Builder, cache, config, parameterize, source, tag
from fnloom._config import Condition, ConfigPolicy
from fnloom._decorators import decorations
from helpers import make_module

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path
    from types import ModuleType


def _plain() -> int:
    return 1


def foo__a() -> int:
    """A variant."""
    return 1


def test_when_records_conditions_and_returns_the_function() -> None:
    """The decorator returns its function and records one condition per key."""

    def f() -> int:
        return 1

    assert config.when(mode="a")(f) is f
    assert decorations(f).config == ConfigPolicy(
        (Condition("when", "mode", ("a",)),), default=False, name=None
    )


def test_each_form_records_its_kind_and_values() -> None:
    """`when_in` and `when_not_in` hold every value, the others hold one."""

    def f() -> int:
        return 1

    def g() -> int:
        return 1

    config.when_not(mode="a")(f)
    config.when_in(mode=iter(["a", "b"]))(g)
    assert decorations(f).config == ConfigPolicy(
        (Condition("when_not", "mode", ("a",)),), default=False, name=None
    )
    assert decorations(g).config == ConfigPolicy(
        (Condition("when_in", "mode", ("a", "b")),), default=False, name=None
    )


def test_stacking_ands_in_either_order() -> None:
    """Stacked decorators concatenate their conditions, sorted by key and kind."""

    def one() -> int:
        return 1

    def two() -> int:
        return 1

    config.when(mode="a")(config.when_not(region="eu")(one))
    config.when_not(region="eu")(config.when(mode="a")(two))
    assert decorations(one).config == decorations(two).config
    assert one.__fnloom__.config.conditions == (  # ty: ignore[unresolved-attribute]
        Condition("when", "mode", ("a",)),
        Condition("when_not", "region", ("eu",)),
    )


def test_stacking_equal_keys_is_order_independent() -> None:
    """The same key and kind twice still sorts the same in either order."""

    def one() -> int:
        return 1

    def two() -> int:
        return 1

    config.when(mode="a")(config.when(mode="b")(one))
    config.when(mode="b")(config.when(mode="a")(two))
    assert decorations(one).config == decorations(two).config


@pytest.mark.parametrize(
    "make",
    [
        lambda **kw: config.when(mode="a", **kw),
        lambda **kw: config.when_not(mode="a", **kw),
        lambda **kw: config.when_in(mode=["a"], **kw),
        lambda **kw: config.when_not_in(mode=["a"], **kw),
    ],
)
def test_name_is_the_node_name_on_every_form(
    make: Callable[..., Callable[[Callable[[], int]], Callable[[], int]]],
) -> None:
    """`name=` names the node and is not a condition key."""

    def f() -> int:
        return 1

    make(name="x")(f)
    policy = decorations(f).config
    assert policy is not None
    assert policy.name == "x"
    assert [c.key for c in policy.conditions] == ["mode"]


def test_a_key_called_name_goes_in_the_mapping() -> None:
    """A positional mapping can condition on a key called `name`."""

    def f() -> int:
        return 1

    config.when({"name": "a"})(f)
    assert decorations(f).config == ConfigPolicy(
        (Condition("when", "name", ("a",)),), default=False, name=None
    )


def test_same_name_twice_is_accepted() -> None:
    """Repeating one name is not a conflict, and a name set once is kept."""

    def f() -> int:
        return 1

    config.when(mode="a", name="x")(config.when(region="eu")(f))
    config.when(mode="a", name="x")(f)
    policy = decorations(f).config
    assert policy is not None
    assert policy.name == "x"


def test_misuse_is_refused_when_decorating() -> None:
    """Each misuse raises at once, naming what is wrong."""

    def f() -> int:
        return 1

    with pytest.raises(
        TypeError, match=r"use @config\.when\(\.\.\.\), with parentheses"
    ):
        config.when(f)  # ty: ignore[invalid-argument-type]  # the misuse under test
    with pytest.raises(TypeError, match="as a mapping or keywords, got str"):
        config.when("mode")  # ty: ignore[invalid-argument-type]  # the misuse under test
    with pytest.raises(ValueError, match="needs at least one condition"):
        config.when_in()
    with pytest.raises(ValueError, match="'mode' is given both"):
        config.when({"mode": "a"}, mode="b")
    with pytest.raises(TypeError, match=r"'mode' \(str\)"):
        config.when_in(mode="ab")
    with pytest.raises(TypeError, match=r"'mode' \(bytes\)"):
        config.when_not_in(mode=b"ab")
    with pytest.raises(TypeError, match=r"'mode' \(int\)"):
        config.when_in(mode=3)
    with pytest.raises(ValueError, match="name must not be empty"):
        config.when(mode="a", name="")
    with pytest.raises(TypeError, match="name must be a str"):
        config.when(mode="a", name=3)  # ty: ignore[invalid-argument-type]  # the misuse under test
    with pytest.raises(ValueError, match="'trailing__' ends in '__'"):
        config.when(mode="a")(_named("trailing__"))
    with pytest.raises(ValueError, match="'__lead' has an empty node name"):
        config.when(mode="a")(_named("__lead"))
    with pytest.raises(ValueError, match="node name set twice: 'x' and 'y'"):
        config.when(mode="a", name="y")(config.when(region="eu", name="x")(f))
    kept = decorations(f).config
    assert kept is not None
    assert kept.name == "x"


def _named(name: str) -> Callable[[], int]:
    def f() -> int:
        return 1

    f.__name__ = name
    return f


def test_default_records_and_excludes_when() -> None:
    """`@config.default` is used bare, and cannot share a function with a `when*`."""

    def with_default() -> int:
        return 1

    assert config.default(with_default) is with_default
    assert decorations(with_default).config == ConfigPolicy((), default=True, name=None)

    def default_first() -> int:
        return 1

    def when_first() -> int:
        return 1

    config.default(default_first)
    with pytest.raises(ValueError, match="cannot be combined"):
        config.when(mode="a")(default_first)
    config.when(mode="a")(when_first)
    with pytest.raises(ValueError, match="cannot be combined"):
        config.default(when_first)
    with pytest.raises(TypeError, match="used bare"):
        config.default("x")  # ty: ignore[invalid-argument-type]  # the misuse under test


@pytest.mark.parametrize("order", [0, 1, 2, 3])
def test_config_composes_with_parameterize_tag_and_cache(order: int) -> None:
    """Config sits beside rows, tags and cache policy, whatever the order."""

    def make() -> Callable[[int], int]:
        def f(x: int) -> int:
            return x

        return f

    others = [
        parameterize(a={"x": source("y")}),
        tag(kind="k"),
        cache(behavior="disable"),
    ]
    forward = make()
    for decorator in [config.when(mode="a"), *others]:
        forward = decorator(forward)
    shuffled = make()
    rotated = [*others[order % 3 :], *others[: order % 3]]
    order_list = [*rotated[:2], config.when(mode="a"), *rotated[2:]]
    for decorator in order_list:
        shuffled = decorator(shuffled)
    assert decorations(forward) == decorations(shuffled)
    got = decorations(forward)
    assert got.config is not None
    assert got.rows is not None
    assert got.tags == {"kind": "k"}
    assert got.cache is not None


def test_every_bad_value_is_listed_in_one_message() -> None:
    """All keys without a collection are named, sorted, in one error."""
    with pytest.raises(TypeError) as raised:
        config.when_in(b=3, a="x", c=["ok"])
    assert str(raised.value).endswith(
        "need a collection of values: 'a' (str), 'b' (int)"
    )


def test_set_values_are_stored_in_a_fixed_order() -> None:
    """A set compares equal to the same values given in order."""

    def one() -> int:
        return 1

    def two() -> int:
        return 1

    def mixed() -> int:
        return 1

    config.when_in(mode={"b", "a"})(one)
    config.when_in(mode=("a", "b"))(two)
    assert decorations(one).config == decorations(two).config
    config.when_not_in(mode=frozenset({1, "a"}))(mixed)
    policy = decorations(mixed).config
    assert policy is not None
    assert policy.conditions[0].values == ("a", 1)  # by repr


# A module of two variants of `foo` and a consumer `bar`, for the build tests.
_VARIANTS = """
from fnloom import config

@config.when(mode="a")
def foo__a(x: int) -> int:
    return x + 1

@config.when(mode="b")
def foo__b(x: int) -> int:
    return x + 100

def bar(foo: int) -> int:
    return foo + 1
"""

# Decision 3's message for `_VARIANTS` under `{"mode": "c"}`.
_NO_VARIANT = (
    "no variant of 'foo' matches the config {'mode': 'c'}; its variants need "
    "mode == 'a' (foo__a), mode == 'b' (foo__b); "
    "add @config.default or a variant for it"
)


def _built(
    name: str, source_code: str, settings: Mapping[str, object]
) -> fnloom.Driver:
    module = make_module(name, source_code)
    return Builder().with_config(settings).with_modules(module).build()


def test_the_matching_variant_is_the_node() -> None:
    """Each mode selects its own variant, and the consumer reads it as `foo`."""
    for mode, expected in (("a", 3), ("b", 102)):
        driver = _built(f"cfg_match_{mode}", _VARIANTS, {"mode": mode})
        assert driver.node_names() == ["bar", "foo", "x"]
        assert driver.execute(["bar"], inputs={"x": 1}) == {"bar": expected}


def test_a_missing_key_never_matches() -> None:
    """`when_not(mode="a")` needs a `mode` to differ: an empty config has none."""
    source_code = """
from fnloom import config

@config.when_not(mode="a")
def foo__not_a(x: int) -> int:
    return x

def keep(x: int) -> int:
    return x
"""
    assert _built("cfg_missing_key", source_code, {}).node_names() == ["keep", "x"]
    with_key = _built("cfg_present_key", source_code, {"mode": "b"})
    assert with_key.node_names() == ["foo", "keep", "x"]


def test_a_needed_node_without_a_variant_fails_the_build() -> None:
    """A consumed node no variant matches is refused at build, with its variants."""
    with pytest.raises(ValueError, match=f"^{re.escape(_NO_VARIANT)}$"):
        _built("cfg_needed", _VARIANTS, {"mode": "c"})


def test_every_needed_node_without_a_variant_is_listed() -> None:
    """Two such nodes are named in one error, sorted, each with its reason."""
    source_code = """
from fnloom import config

@config.when(mode="a")
def zed__a() -> int:
    return 1

@config.when_in(mode=["a", "b"])
@config.when_not(region="eu")
def alpha__a() -> int:
    return 1

def use(zed: int, alpha: int) -> int:
    return zed + alpha
"""
    with pytest.raises(ValueError, match=r"^no variant of 'alpha'") as raised:
        _built("cfg_needed_two", source_code, {"mode": "c", "region": "eu"})
    assert str(raised.value).splitlines() == [
        (
            "no variant of 'alpha' matches the config {'mode': 'c', 'region': 'eu'}; "
            "its variants need mode in ['a', 'b'] and region != 'eu' (alpha__a); "
            "add @config.default or a variant for it"
        ),
        (
            "no variant of 'zed' matches the config {'mode': 'c', 'region': 'eu'}; "
            "its variants need mode == 'a' (zed__a); "
            "add @config.default or a variant for it"
        ),
    ]


def test_an_unneeded_node_without_a_variant_is_absent() -> None:
    """Nothing reads `foo`, so the build succeeds; requesting it names the reason."""
    source_code = _VARIANTS.replace("def bar(foo: int)", "def bar(x: int)")
    driver = _built("cfg_unneeded", source_code, {"mode": "c"})
    assert driver.node_names() == ["bar", "x"]
    with pytest.raises(ValueError, match=f"^{re.escape(_NO_VARIANT)}$"):
        driver.prepare(["foo"])
    with pytest.raises(ValueError, match=f"^{re.escape(_NO_VARIANT)}$"):
        driver.execute(["bar", "foo"], inputs={"x": 1})


def test_default_is_used_only_when_nothing_else_matches() -> None:
    """`@config.default` stands in for an unmatched mode, and steps aside otherwise."""
    source_code = (
        _VARIANTS
        + """
@config.default
def foo__fallback(x: int) -> int:
    return x + 1000
"""
    )
    fallback = _built("cfg_default_c", source_code, {"mode": "c"})
    assert fallback.execute(["bar"], inputs={"x": 1}) == {"bar": 1002}
    assert _built("cfg_default_none", source_code, {}).execute(
        ["bar"], inputs={"x": 1}
    ) == {"bar": 1002}
    matched = _built("cfg_default_a", source_code, {"mode": "a"})
    assert matched.execute(["bar"], inputs={"x": 1}) == {"bar": 3}
    assert matched.nodes["foo"].origin is sys.modules["cfg_default_a"].foo__a


def test_two_defaults_for_one_node_are_refused() -> None:
    """At most one default per node, checked at build, naming both."""
    source_code = """
from fnloom import config

@config.default
def foo__y() -> int:
    return 1

@config.default
def foo__x() -> int:
    return 2
"""
    with pytest.raises(
        ValueError,
        match=r"^node 'foo' has more than one @config\.default: 'foo__x', 'foo__y'$",
    ):
        _built("cfg_two_defaults", source_code, {})


def test_two_matching_variants_name_both() -> None:
    """Both matching functions are named, sorted, in one error."""
    source_code = """
from fnloom import config

@config.when_not(mode="b")
def foo__nb() -> int:
    return 1

@config.when(mode="a")
def foo__a() -> int:
    return 2
"""
    with pytest.raises(
        ValueError,
        match=(
            r"^node 'foo' has more than one variant matching the config "
            r"\{'mode': 'a'\}: 'foo__a', 'foo__nb'$"
        ),
    ):
        _built("cfg_two_matches", source_code, {"mode": "a"})


def _file_module(directory: Path, name: str, source_code: str) -> ModuleType:
    """Writes `source_code` to `<directory>/<name>.py` and imports it, with a source."""
    path = directory / f"{name}.py"
    path.write_text(textwrap.dedent(source_code), encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        msg = f"cannot load {path}"
        raise ImportError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_a_redefined_variant_is_refused(tmp_path: Path) -> None:
    """Python keeps the last `def foo__a`; the build refuses it, naming the lines."""
    module = _file_module(
        tmp_path,
        "cfg_redefined",
        """\
from fnloom import config

@config.when(mode="a")
def foo__a() -> int:
    return 1

@config.when(mode="a")
def foo__a() -> int:
    return 2

def plain() -> int:
    return 1

def plain() -> int:
    return 2
""",
    )
    with pytest.raises(
        ValueError,
        match=(
            r"^function 'foo__a' is defined more than once in module "
            r"'cfg_redefined' \(lines 4, 8\); Python keeps only the last$"
        ),
    ):
        Builder().with_config({"mode": "a"}).with_modules(module).build()


def test_a_conditional_definition_is_not_a_redefinition(tmp_path: Path) -> None:
    """Two `def`s under an `if` and its `else` are one definition, not two."""
    module = _file_module(
        tmp_path,
        "cfg_conditional",
        """\
import sys

from fnloom import config

if sys.version_info >= (3, 14):
    @config.when(mode="a")
    def foo__a() -> int:
        return 1
else:
    @config.when(mode="a")
    def foo__a() -> int:
        return 2
""",
    )
    driver = Builder().with_config({"mode": "a"}).with_modules(module).build()
    assert driver.execute(["foo"]) == {"foo": 1}


def test_an_aliased_variant_is_one_variant() -> None:
    """A variant under a second name, re-exported, or its module given twice, is one."""
    source_code = (
        _VARIANTS
        + """
also_foo__a = foo__a
"""
    )
    module = make_module("cfg_alias", source_code)
    # The same module object, imported again under a second name.
    sys.modules["cfg_alias_again"] = module
    again = importlib.import_module("cfg_alias_again")
    reexports = make_module(
        "cfg_alias_reexport",
        "from cfg_alias import foo__a\n\ndef own(x: int) -> int:\n    return x\n",
    )
    driver = (
        Builder()
        .with_config({"mode": "a"})
        .with_modules(module, again, reexports)
        .with_modules(module)
        .build()
    )
    assert driver.node_names() == ["bar", "foo", "own", "x"]
    assert driver.execute(["bar"], inputs={"x": 1}) == {"bar": 3}


class _Elementwise:
    """A value whose `==` returns a list, as an array's does."""

    @override
    def __eq__(self, other: object) -> bool:
        return [True]  # ty: ignore[invalid-return-type]  # the misbehaviour under test

    __hash__ = None  # unhashable, like an array


def test_a_non_bool_comparison_names_the_key() -> None:
    """An `==` returning something other than a bool is a `TypeError` naming the key."""
    with pytest.raises(
        TypeError,
        match=r"^config key 'mode': == returned list, not bool$",
    ):
        _built("cfg_non_bool", _VARIANTS, {"mode": _Elementwise()})


def test_an_unhashable_config_value_compares() -> None:
    """A list as a config value is compared with `==`, never hashed."""
    source_code = """
from fnloom import config

@config.when(columns=["a", "b"])
def pick__ab() -> int:
    return 1

@config.when_in(columns=[["a"], ["a", "b"]])
def other__ab() -> int:
    return 2
"""
    driver = _built("cfg_unhashable", source_code, {"columns": ["a", "b"]})
    assert driver.execute(["pick", "other"]) == {"pick": 1, "other": 2}


def test_config_is_never_an_input() -> None:
    """A parameter named like a config key is an input; the error says so."""
    source_code = """
def usecfg(mode: str) -> str:
    return "mode=" + mode
"""
    driver = _built("cfg_never_input", source_code, {"mode": "fast"})
    assert driver.node_names() == ["mode", "usecfg"]
    assert driver.nodes["mode"].is_input
    with pytest.raises(
        ValueError,
        match=(
            r"^missing inputs: 'mode' \(needed by 'usecfg'\)\n"
            r"'mode' is a config key; config is not passed to nodes: "
            r"supply it in inputs=$"
        ),
    ):
        driver.execute(["usecfg"])
    assert driver.execute(["usecfg"], inputs={"mode": "slow"}) == {
        "usecfg": "mode=slow"
    }


def test_every_missing_config_key_is_hinted() -> None:
    """Two missing inputs named like config keys share one hint, sorted."""
    source_code = """
def both(mode: str, k: int, other: int) -> str:
    return mode * k
"""
    driver = _built("cfg_never_inputs", source_code, {"mode": "a", "k": 2, "z": 0})
    with pytest.raises(ValueError, match=r"^missing inputs: ") as raised:
        driver.execute(["both"])
    assert str(raised.value).splitlines() == [
        (
            "missing inputs: 'k' (needed by 'both'); 'mode' (needed by 'both'); "
            "'other' (needed by 'both')"
        ),
        (
            "'k', 'mode' are config keys; config is not passed to nodes: "
            "supply them in inputs="
        ),
    ]


def test_driver_config_is_a_read_only_copy() -> None:
    """`with_config` copies and merges; `Driver.config` cannot be written."""
    given: dict[str, object] = {"mode": "a", "k": 1}
    builder = Builder().with_config(given).with_config({"k": 2})
    given["mode"] = "b"
    driver = builder.with_modules(make_module("cfg_read_only", _VARIANTS)).build()
    assert dict(driver.config) == {"mode": "a", "k": 2}
    assert isinstance(driver.config, MappingProxyType)
    assert driver.execute(["bar"], inputs={"x": 1}) == {"bar": 3}
    with pytest.raises(TypeError):
        driver.config["mode"] = "b"  # ty: ignore[invalid-assignment]  # read-only on purpose
    assert (
        dict(
            fnloom.build(
                make_module("cfg_no_config", "def f() -> int:\n    return 1\n")
            ).config
        )
        == {}
    )


def test_with_config_refuses_what_is_not_a_config() -> None:
    """A non-mapping, or a key that is not a `str`, is a `TypeError` naming each."""
    with pytest.raises(TypeError, match=r"^with_config takes a mapping, got list$"):
        Builder().with_config([("mode", "a")])  # ty: ignore[invalid-argument-type]  # the misuse under test
    with pytest.raises(TypeError, match=r"^config keys must be str, got: bytes, int$"):
        Builder().with_config({1: "a", b"k": 2, "ok": 3})  # ty: ignore[invalid-argument-type]  # the misuse under test


def test_node_view_lists_its_conditions() -> None:
    """`NodeView.config` holds the conditions that selected the node."""
    source_code = """
from fnloom import config

@config.when(mode="a")
@config.when_not_in(region=["eu"])
def foo__a() -> int:
    return 1

@config.default
def other__d() -> int:
    return 1

def plain(foo: int) -> int:
    return foo
"""
    nodes = _built("cfg_view", source_code, {"mode": "a", "region": "us"}).nodes
    assert nodes["foo"].config == (
        Condition("when", "mode", ("a",)),
        Condition("when_not_in", "region", ("eu",)),
    )
    assert nodes["other"].config == ()
    assert nodes["plain"].config == ()


def test_config_selects_before_rows_and_tags() -> None:
    """A `@parameterize` under `@config.when` gives its rows only when it matches."""
    source_code = """
from fnloom import config, parameterize, tag, value

@tag(owner="data")
@config.when(mode="a")
@parameterize(p1={"k": value(1)}, p2={"k": value(2)})
def par__a(k: int) -> int:
    return k

@parameterize(q1={"k": value(10)})
@config.when(mode="b")
def par__b(k: int) -> int:
    return k
"""
    matched = _built("cfg_rows_a", source_code, {"mode": "a"})
    assert matched.node_names() == ["p1", "p2"]
    assert matched.nodes["p1"].tags == {"owner": "data"}
    assert matched.execute(["p1", "p2"]) == {"p1": 1, "p2": 2}
    other = _built("cfg_rows_b", source_code, {"mode": "b"})
    assert other.node_names() == ["q1"]


def test_a_plain_function_beside_an_unmatched_variant_is_the_node() -> None:
    """A plain `foo` is the node when its variant misses, and clashes on a match."""
    source_code = """
from fnloom import config

def foo() -> int:
    return 1

@config.when(mode="a")
def foo__a() -> int:
    return 2

def bar(foo: int) -> int:
    return foo
"""
    assert _built("cfg_plain_b", source_code, {"mode": "b"}).execute(["bar"]) == {
        "bar": 1
    }
    with pytest.raises(ValueError, match="node 'foo' is defined twice"):
        _built("cfg_plain_a", source_code, {"mode": "a"})


def test_a_module_of_unmatched_variants_is_not_empty() -> None:
    """A module whose variants all miss contributes no node, and that is fine."""
    source_code = """
from fnloom import config

@config.when(mode="a")
def foo__a() -> int:
    return 1
"""
    other = make_module("cfg_other", "def g() -> int:\n    return 1\n")
    module = make_module("cfg_all_unmatched", source_code)
    driver = Builder().with_config({"mode": "b"}).with_modules(module, other).build()
    assert driver.node_names() == ["g"]
