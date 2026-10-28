"""Authoring: which functions are nodes (Q73), and graphs from functions (Q72)."""

import asyncio
import copy
import functools
import pickle
import re
import sys
from typing import TYPE_CHECKING, Any, TypedDict, cast

import pytest

import fnloom
from fnloom._decorators import decorations
from helpers import make_module

if TYPE_CHECKING:
    from types import ModuleType


def test_exclude_bare_and_called() -> None:
    """Both spellings keep a public function out, and return it unchanged."""
    module = make_module(
        "auth_exclude",
        """
        import fnloom


        @fnloom.exclude
        def bare(x: int) -> int:
            return x + 1


        @fnloom.exclude()
        def called(x: int) -> int:
            return x + 2


        def kept(x: int) -> int:
            return x + 3
        """,
    )
    assert fnloom.build(module).node_names() == ["kept", "x"]
    for name, offset in (("bare", 1), ("called", 2)):
        function = getattr(module, name)
        assert function.__name__ == name
        assert function(1) == 1 + offset
        assert decorations(function).excluded is True
    assert decorations(module.kept).excluded is False


def test_exclude_returns_the_same_function() -> None:
    """The decorator records and gives the function back: identity, both ways."""

    def plain(x: int) -> int:
        return x

    def other(x: int) -> int:
        return x

    assert fnloom.exclude(plain) is plain
    assert fnloom.exclude()(other) is other


@pytest.mark.parametrize("bad", ["name", 3, ["f"]])
def test_exclude_refuses_a_non_function(bad: object) -> None:
    """`@exclude("name")` is a mistake, and says so when decorating."""
    with pytest.raises(TypeError, match=r"exclude takes a function or nothing, got"):
        fnloom.exclude(cast("Any", bad))  # the mistake under test


def test_exclude_with_other_decorators_in_either_order() -> None:
    """The mark survives `@tag`, `@parameterize`, `@cache` and `@config` either way."""
    module = make_module(
        "auth_orders",
        """
        import fnloom


        @fnloom.exclude
        @fnloom.tag(kind="a")
        def above_tag(x: int) -> int:
            return x


        @fnloom.tag(kind="b")
        @fnloom.exclude()
        def below_tag(x: int) -> int:
            return x


        @fnloom.exclude
        @fnloom.parameterize(r1={"y": fnloom.value(1)}, r2={"y": fnloom.value(2)})
        def above_rows(x: int, y: int) -> int:
            return x + y


        @fnloom.parameterize(r3={"y": fnloom.value(1)})
        @fnloom.exclude
        def below_rows(x: int, y: int) -> int:
            return x + y


        @fnloom.cache(behavior="recompute")
        @fnloom.exclude
        def below_cache(x: int) -> int:
            return x


        @fnloom.exclude
        @fnloom.config.when(mode="a")
        def variant__a(x: int) -> int:
            return x


        def kept(x: int) -> int:
            return x
        """,
    )
    assert fnloom.build(module).node_names() == ["kept", "x"]
    assert decorations(module.below_rows).excluded
    assert decorations(module.above_rows).excluded


def test_an_excluded_function_is_never_resolved() -> None:
    """Its annotations are not read: a `TYPE_CHECKING`-only name on it is fine."""
    module = make_module(
        "auth_unresolved",
        """
        from typing import TYPE_CHECKING, Any, cast

        import fnloom

        if TYPE_CHECKING:
            from decimal import Decimal


        @fnloom.exclude
        def legacy(x: "Decimal") -> "Decimal":
            return x


        def kept(x: int) -> int:
            return x
        """,
    )
    assert fnloom.build(module).node_names() == ["kept", "x"]


def test_an_excluded_function_needs_no_annotations() -> None:
    """Hamilton lets a legacy function without hints through; so does fnloom."""
    module = make_module(
        "auth_legacy",
        """
        import fnloom


        @fnloom.exclude
        def legacy(x, y):
            return x + y


        def kept(x: int) -> int:
            return x
        """,
    )
    assert fnloom.build(module).execute(["kept"], inputs={"x": 2}) == {"kept": 2}


ALL_MODULE = """
from math import floor

import fnloom

__all__ = ["listed", "floor", "LIMIT", "variant__a", "missing_name"]

LIMIT = 3


def listed(x: int) -> int:
    return x


def not_listed(x: int) -> int:
    return x + 1


@fnloom.config.when(mode="a")
def variant__a(x: int) -> int:
    return x


@fnloom.config.when(mode="b")
def variant__b(x: int) -> int:
    return -x
"""


def test_all_limits_nodes() -> None:
    """Only the functions `__all__` names are nodes; its other names are ignored."""
    module = make_module("auth_all", ALL_MODULE)
    driver = fnloom.Builder().with_modules(module).with_config({"mode": "a"}).build()
    assert driver.node_names() == ["listed", "variant", "x"]
    assert driver.execute(["variant"], inputs={"x": 5}) == {"variant": 5}


def test_all_names_the_attribute_so_a_variant_is_listed_by_its_own_name() -> None:
    """`variant__b` is not listed, so `mode="b"` selects nothing."""
    module = make_module("auth_all_variant", ALL_MODULE)
    driver = fnloom.Builder().with_modules(module).with_config({"mode": "b"}).build()
    assert "variant" not in driver.node_names()
    assert driver.node_names() == ["listed", "x"]


def test_all_as_a_tuple() -> None:
    """A tuple works as a list does."""
    module = make_module(
        "auth_all_tuple",
        """
        __all__ = ("a",)


        def a(x: int) -> int:
            return x


        def b(x: int) -> int:
            return x
        """,
    )
    assert fnloom.build(module).node_names() == ["a", "x"]


def test_a_module_without_all_keeps_every_public_function() -> None:
    """No `__all__`, no change from before."""
    module = make_module(
        "auth_no_all",
        """
        def a(x: int) -> int:
            return x


        def b(x: int) -> int:
            return x
        """,
    )
    assert fnloom.build(module).node_names() == ["a", "b", "x"]


def test_all_does_not_make_a_private_function_a_node() -> None:
    """Listing `_helper` in `__all__` does not make it a node."""
    module = make_module(
        "auth_all_private",
        """
        __all__ = ["a", "_helper"]


        def a(x: int) -> int:
            return x


        def _helper(x: int) -> int:
            return x
        """,
    )
    assert fnloom.build(module).node_names() == ["a", "x"]


def test_an_unlisted_function_is_not_resolved_either() -> None:
    """A function `__all__` leaves out has no annotations read."""
    module = make_module(
        "auth_all_unresolved",
        """
        __all__ = ["kept"]


        def kept(x: int) -> int:
            return x


        def legacy(x, y):
            return x + y
        """,
    )
    assert fnloom.build(module).node_names() == ["kept", "x"]


def test_exclude_and_all_together() -> None:
    """A function `__all__` names and `@exclude` marks is excluded, with no error."""
    module = make_module(
        "auth_both",
        """
        import fnloom

        __all__ = ["both", "only_all", "only_exclude", "reader"]


        @fnloom.exclude
        def both(x: int) -> int:
            return x


        def only_all(x: int) -> int:
            return x


        @fnloom.exclude()
        def only_exclude(x: int) -> int:
            return x


        def neither(x: int) -> int:
            return x


        def reader(both: int) -> int:
            return both
        """,
    )
    driver = fnloom.build(module)
    assert driver.node_names() == ["both", "only_all", "reader", "x"]
    assert driver.execute(["reader"], inputs={"both": 1}) == {"reader": 1}
    with pytest.raises(ValueError, match=r"missing inputs: 'both'") as raised:
        driver.execute(["reader"])
    assert (
        "'both' is a function in module 'auth_both' marked @exclude"
        in str(raised.value).splitlines()
    )


def _hint_driver(name: str) -> tuple[ModuleType, fnloom.Driver]:
    module = make_module(
        name,
        """
        import fnloom

        __all__ = ["needs_left_out", "needs_marked"]


        def left_out(x: int) -> int:
            return x


        @fnloom.exclude
        def marked(x: int) -> int:
            return x


        def needs_left_out(left_out: int) -> int:
            return left_out


        def needs_marked(marked: int) -> int:
            return marked
        """,
    )
    return module, fnloom.build(module)


def test_a_missing_input_named_like_a_function_left_out_of_all_says_so() -> None:
    """The error says the function exists and `__all__` left it out."""
    module, driver = _hint_driver("auth_hint_all")
    with pytest.raises(ValueError, match=r"missing inputs: 'left_out'") as raised:
        driver.execute(["needs_left_out"])
    lines = str(raised.value).splitlines()
    assert (
        f"'left_out' is a function in module {module.__name__!r} "
        "not listed in its __all__" in lines
    )


def test_a_missing_input_named_like_an_excluded_function_says_so() -> None:
    """The error says the function exists and `@exclude` marked it."""
    module, driver = _hint_driver("auth_hint_exclude")
    with pytest.raises(ValueError, match=r"missing inputs: 'marked'") as raised:
        driver.execute(["needs_marked"])
    lines = str(raised.value).splitlines()
    assert (
        f"'marked' is a function in module {module.__name__!r} "
        "marked @exclude and not listed in its __all__" in lines
    )


def test_the_hint_reaches_validate_copies_pickles_and_the_async_driver() -> None:
    """Every route to the error carries it, including a rebuilt driver."""
    module, driver = _hint_driver("auth_hint_routes")
    hint = (
        f"'left_out' is a function in module {module.__name__!r} "
        "not listed in its __all__"
    )
    assert hint in driver.validate(["needs_left_out"])
    assert hint in fnloom.Builder().with_modules(module).build_async().validate(
        ["needs_left_out"]
    )
    with pytest.raises(ValueError, match=r"missing inputs: 'left_out'") as raised:
        copy.copy(driver).execute(["needs_left_out"])
    assert hint in str(raised.value)
    with pytest.raises(ValueError, match=r"missing inputs: 'left_out'") as raised:
        pickle.loads(pickle.dumps(driver)).execute(["needs_left_out"])  # noqa: S301  # a driver this test just pickled
    assert hint in str(raised.value)


def test_no_hint_when_the_missing_input_is_not_a_left_out_function() -> None:
    """An ordinary missing input gets no extra line."""
    plain = make_module("auth_hint_plain", "def a(q: int) -> int:\n    return q\n")
    with pytest.raises(ValueError, match=r"missing inputs: 'q'") as raised:
        fnloom.build(plain).execute(["a"])
    assert "is a function in module" not in str(raised.value)


@pytest.mark.parametrize(
    "bad", ["'a'", "None", "1", "{'a'}", "['a', 1]", "('a', None)"]
)
def test_a_malformed_all_is_a_type_error(bad: str) -> None:
    """`__all__` must be a list or tuple of `str`; the error names the module."""
    module = make_module(
        "auth_bad_all", f"__all__ = {bad}\n\n\ndef a(x: int) -> int:\n    return x\n"
    )
    with pytest.raises(
        TypeError, match=r"module 'auth_bad_all': __all__ must be a list"
    ):
        fnloom.build(module)


def test_a_malformed_all_lists_every_module_sorted() -> None:
    """Every culprit is named, in one message, sorted by module."""
    zed = make_module(
        "auth_zed", "__all__ = 'a'\n\n\ndef a(x: int) -> int:\n    return x\n"
    )
    alpha = make_module(
        "auth_alpha", "__all__ = [1]\n\n\ndef a(y: int) -> int:\n    return y\n"
    )
    fine = make_module(
        "auth_fine", "__all__ = ['f']\n\n\ndef f(z: int) -> int:\n    return z\n"
    )
    with pytest.raises(TypeError) as raised:
        fnloom.Builder().with_modules(zed, fine, alpha).build()
    message = str(raised.value)
    assert message.index("'auth_alpha'") < message.index("'auth_zed'")
    assert "auth_fine" not in message


def test_an_all_only_module_with_no_listed_function_is_refused_as_empty() -> None:
    """The empty-module error gives `__all__` and `@exclude` as reasons."""
    make_module("auth_elsewhere", "def imported(x: int) -> int:\n    return x\n")
    module = make_module(
        "auth_empty",
        """
        import fnloom
        from auth_elsewhere import imported

        __all__ = ["imported"]


        def left_out(x: int) -> int:
            return x


        @fnloom.exclude
        def marked(x: int) -> int:
            return x


        def _private(x: int) -> int:
            return x
        """,
    )
    with pytest.raises(ValueError, match=r"module 'auth_empty' has no nodes") as raised:
        fnloom.build(module)
    message = str(raised.value)
    assert "'left_out' is not listed in its __all__" in message
    assert "'marked' is marked @exclude" in message
    assert "'_private' is private" in message
    assert "'imported' is defined in 'auth_elsewhere'" in message


def test_an_excluded_function_is_never_a_node_by_identity() -> None:
    """Review Focus 4: a same-named excluded function in another module is no node.

    Passing the node's own function works. Passing the excluded object is refused
    as a function no node comes from, though a node bears its name.
    """
    first = make_module(
        "auth_id_first",
        """
        def total(x: int) -> int:
            return x * 2
        """,
    )
    second = make_module(
        "auth_id_second",
        """
        import fnloom


        @fnloom.exclude
        def total(x: int) -> int:
            return x * 3


        def other(x: int) -> int:
            return x
        """,
    )
    driver = fnloom.Builder().with_modules(first, second).build()
    assert driver.node_names() == ["other", "total", "x"]
    assert driver.execute([first.total], inputs={"x": 2}) == {"total": 4}
    assert driver.execute(["total"], inputs={"x": 2}) == {"total": 4}
    with pytest.raises(
        ValueError, match=r"not the function behind node 'total'"
    ) as raised:
        driver.execute([second.total], inputs={"x": 2})
    assert "auth_id_second" in str(raised.value)
    problems = driver.validate([second.total], inputs={"x": 2})
    assert len(problems) == 1
    assert "auth_id_second" in problems[0]


_LEFT_OUT_TWICE = """
import fnloom

__all__ = ["reader{n}"]


def shared(x: int) -> int:
    return x


@fnloom.exclude
def shared_{n}(x: int) -> int:
    return x


def reader{n}(shared: int) -> int:
    return shared
"""


def test_the_hint_does_not_depend_on_module_order() -> None:
    """A name left out of two modules gives the same lines in either order."""
    first = make_module("auth_order_a", _LEFT_OUT_TWICE.format(n="a"))
    second = make_module("auth_order_b", _LEFT_OUT_TWICE.format(n="b"))
    messages = []
    for modules in ((first, second), (second, first)):
        driver = fnloom.Builder().with_modules(*modules).build()
        with pytest.raises(ValueError, match=r"missing inputs: 'shared'") as raised:
            driver.execute(["readera"])
        messages.append(str(raised.value))
    assert messages[0] == messages[1]
    lines = messages[0].splitlines()
    assert lines[1:] == [
        "'shared' is a function in module 'auth_order_a' not listed in its __all__",
        "'shared' is a function in module 'auth_order_b' not listed in its __all__",
    ]


def test_a_name_left_out_of_one_module_but_a_node_of_another_has_no_hint() -> None:
    """The hint only explains a missing name: a real node of that name is no culprit."""
    left_out = make_module(
        "auth_real_left",
        """
        import fnloom


        @fnloom.exclude
        def total(x: int) -> int:
            return x * 3


        def other(x: int) -> int:
            return x
        """,
    )
    real = make_module(
        "auth_real_node",
        """
        def total(x: int) -> int:
            return x * 2


        def reader(total: int, q: int) -> int:
            return total + q
        """,
    )
    driver = fnloom.Builder().with_modules(left_out, real).build()
    assert driver.execute(["reader"], inputs={"x": 1, "q": 1}) == {"reader": 3}
    with pytest.raises(ValueError, match=r"missing inputs: 'q'") as raised:
        driver.execute(["reader"], inputs={"x": 1})
    assert "is a function in module" not in str(raised.value)
    assert not any(
        "is a function in module" in line for line in driver.validate(["reader"])
    )


_VARIANTS = """
import fnloom

__all__ = ["price__eu", "report"]


@fnloom.config.when(region="eu")
def price__eu(x: int) -> int:
    return x


@fnloom.config.when(region="us")
def price__us(x: int) -> int:
    return x * 2


def report(price: int) -> int:
    return price
"""


def test_a_config_variant_left_out_names_its_function_in_the_hint() -> None:
    """The missing node's name `price` is explained by the variant `price__us`."""
    module = make_module("auth_variant_hint", _VARIANTS)
    only_us = make_module(
        "auth_variant_only_us",
        """
        import fnloom

        __all__ = ["report"]


        @fnloom.config.when(region="us")
        def price__us(x: int) -> int:
            return x * 2


        def report(price: int) -> int:
            return price
        """,
    )
    assert module.report is not None
    driver = (
        fnloom.Builder().with_modules(only_us).with_config({"region": "us"}).build()
    )
    with pytest.raises(ValueError, match=r"missing inputs: 'price'") as raised:
        driver.execute(["report"])
    assert (
        "'price__us' (node 'price') is a function in module 'auth_variant_only_us' "
        "not listed in its __all__"
    ) in raised.value.__str__().splitlines()


def test_an_unmatched_variant_error_mentions_variants_left_out() -> None:
    """With `__all__` listing only `price__eu` and `region=us`, `price__us` is named."""
    module = make_module("auth_variant_unmatched", _VARIANTS)
    with pytest.raises(ValueError, match=r"no variant of 'price' matches") as raised:
        fnloom.Builder().with_modules(module).with_config({"region": "us"}).build()
    message = str(raised.value)
    assert "its variants need region == 'eu' (price__eu)" in message
    assert (
        "'price__us' (node 'price') is a function in module "
        "'auth_variant_unmatched' not listed in its __all__"
    ) in message
    # With the matching region the variant is selected and nothing is said.
    driver = fnloom.Builder().with_modules(module).with_config({"region": "eu"}).build()
    assert driver.execute(["report"], inputs={"x": 4}) == {"report": 4}


def test_an_unmatched_variant_error_names_excluded_variants_too() -> None:
    """`@exclude` on a variant is a reason as `__all__` is."""
    module = make_module(
        "auth_variant_excluded",
        """
        import fnloom


        @fnloom.config.when(region="eu")
        def price__eu(x: int) -> int:
            return x


        @fnloom.exclude
        @fnloom.config.when(region="us")
        def price__us(x: int) -> int:
            return x * 2


        def report(price: int) -> int:
            return price
        """,
    )
    with pytest.raises(ValueError, match=r"no variant of 'price' matches") as raised:
        fnloom.Builder().with_modules(module).with_config({"region": "us"}).build()
    assert (
        "'price__us' (node 'price') is a function in module "
        "'auth_variant_excluded' marked @exclude"
    ) in str(raised.value)


@pytest.mark.parametrize("bad", [None, int, len, 3])
def test_exclude_refuses_anything_but_a_function(bad: object) -> None:
    """`@exclude(None)`, a class and a built-in all get the same message."""
    with pytest.raises(TypeError, match=r"exclude takes a function or nothing, got"):
        fnloom.exclude(cast("Any", bad))


def test_exclude_with_a_class_says_a_class() -> None:
    """The message names the kind of thing it was given."""
    with pytest.raises(TypeError, match=r"got a class"):
        fnloom.exclude(cast("Any", int))
    with pytest.raises(TypeError, match=r"got NoneType"):
        fnloom.exclude(cast("Any", None))


def test_a_function_both_excluded_and_unlisted_gives_both_reasons_when_empty() -> None:
    """The empty-module error lists both reasons for one function."""
    module = make_module(
        "auth_both_reasons",
        """
        import fnloom

        __all__ = []


        @fnloom.exclude
        def both(x: int) -> int:
            return x
        """,
    )
    with pytest.raises(
        ValueError, match=r"module 'auth_both_reasons' has no nodes"
    ) as raised:
        fnloom.build(module)
    assert "'both' is marked @exclude and not listed in its __all__" in str(
        raised.value
    )


# Graphs from functions (Q72).

_SOURCES = """
import fnloom


def base(x: int) -> int:
    return x + 1


@fnloom.tag(owner="data")
def doubled(base: int) -> int:
    \"\"\"Twice the base.\"\"\"
    return base * 2


@fnloom.parameterize(low={"limit": fnloom.value(1)}, high={"limit": fnloom.value(9)})
def above(base: int, limit: int) -> bool:
    return base > limit


@fnloom.config.when(region="eu")
def price__eu(base: int) -> float:
    return base * 1.2


@fnloom.config.when(region="us")
def price__us(base: int) -> float:
    return base * 1.0


def _private(base: int) -> int:
    return -base
"""


def test_with_functions_builds_without_a_module() -> None:
    """Functions alone build a graph; nothing is copied or registered in sys.modules."""
    module = make_module("auth_functions_plain", _SOURCES)
    before = dict(sys.modules)
    driver = fnloom.Builder().with_functions(module.base, module.doubled).build()
    assert driver.node_names() == ["base", "doubled", "x"]
    assert driver.execute(["doubled"], inputs={"x": 2}) == {"doubled": 6}
    # No module made, none registered, and nothing listed as a module.
    assert sys.modules == before
    assert driver.modules == ()
    # The node is the function itself, in its own module.
    view = driver.nodes["doubled"]
    assert view.origin is module.doubled
    assert view.module == "auth_functions_plain"
    assert view.doc == "Twice the base."
    # A function given on purpose is a node even when private, unlike in a module.
    private = fnloom.Builder().with_functions(module.base, module._private).build()  # noqa: SLF001  # a private function, given on purpose
    assert private.execute(["_private"], inputs={"x": 1}) == {"_private": -2}
    # Cumulative and ordered, each function once; a local function works too.

    def tripled(doubled: int) -> int:
        return doubled * 3

    builder = fnloom.Builder().with_functions(module.base, module.base)
    built = builder.with_functions(module.doubled).with_functions(tripled).build()
    assert built.execute(["tripled"], inputs={"x": 0}) == {"tripled": 6}
    assert built.nodes["tripled"].module == __name__
    # A function in `final_vars` means its node (Q30).
    assert built.execute([tripled], inputs={"x": 0}) == {"tripled": 6}


def test_decorated_functions_keep_their_decorations() -> None:
    """Tags, rows, config variants, extracted fields and async work without a module."""
    module = make_module("auth_functions_decorated", _SOURCES)
    driver = (
        fnloom.Builder()
        .with_config({"region": "eu"})
        .with_functions(
            module.base,
            module.doubled,
            module.above,
            module.price__eu,
            module.price__us,
        )
        .build()
    )
    assert driver.node_names() == ["base", "doubled", "high", "low", "price", "x"]
    assert dict(driver.nodes["doubled"].tags) == {"owner": "data"}
    assert driver.execute(["low", "high", "price"], inputs={"x": 2}) == {
        "low": True,
        "high": False,
        "price": pytest.approx(3.6),
    }
    us = (
        fnloom.Builder()
        .with_config({"region": "us"})
        .with_functions(module.base, module.price__eu, module.price__us)
        .build()
    )
    assert us.execute(["price"], inputs={"x": 2}) == {"price": 3.0}
    # A variant given alone, that the config does not select, gives no node.

    def reader(price: float) -> float:
        return price

    unselected = (
        fnloom.Builder()
        .with_config({"region": "us"})
        .with_functions(module.base, module.price__eu, reader)
    )
    with pytest.raises(ValueError, match="no variant of 'price' matches"):
        unselected.build()

    class Pair(TypedDict):
        left: int
        right: int

    @fnloom.extract_fields()
    def split(x: int) -> Pair:
        return {"left": x, "right": -x}

    fields = fnloom.Builder().with_functions(split).build()
    assert fields.execute(["left", "right"], inputs={"x": 4}) == {
        "left": 4,
        "right": -4,
    }

    async def fetched(x: int) -> int:
        await asyncio.sleep(0)
        return x * 10

    with pytest.raises(ValueError, match="build it with build_async"):
        fnloom.Builder().with_functions(fetched).build()
    driven = fnloom.Builder().with_functions(fetched, module.base).build_async()
    assert driven.modules == ()
    result = asyncio.run(driven.execute(["fetched", "base"], inputs={"x": 1}))
    assert result == {"fetched": 10, "base": 2}


def test_node_names_and_binds_a_callable() -> None:
    """`fnloom.node` names one node and binds it, as a `@parameterize` row does."""
    module = make_module("auth_functions_node", _SOURCES)

    def scaled(value: int, factor: int = 2) -> int:
        return value * factor

    spec = fnloom.node(
        scaled,
        name="big",
        bind={"value": fnloom.source("x"), "factor": fnloom.value(10)},
    )
    assert isinstance(spec, fnloom.NodeSpec)
    assert spec.fn is scaled
    assert spec.name == "big"
    assert dict(spec.bind) == {"value": fnloom.source("x"), "factor": fnloom.value(10)}
    with pytest.raises(TypeError):
        spec.bind["value"] = fnloom.value(1)  # ty: ignore[invalid-assignment]  # read-only on purpose
    small = fnloom.node(scaled, name="small", bind={"value": fnloom.source("base")})
    driver = fnloom.Builder().with_functions(module.base, spec, small, scaled).build()
    assert driver.execute(["big", "small"], inputs={"x": 3}) == {"big": 30, "small": 8}
    # Unbound `value` of the plain `scaled` is an input; its `factor` keeps its default.
    assert driver.execute(["scaled"], inputs={"x": 0, "value": 5}) == {"scaled": 10}
    assert driver.nodes["big"].origin is scaled
    # Without a name, the function's own; the function is never changed.
    assert fnloom.node(scaled).name == "scaled"
    assert not hasattr(scaled, "__fnloom__")
    # Its decorations apply: `doubled`'s tag.
    tagged = fnloom.Builder().with_functions(
        module.base, fnloom.node(module.doubled, name="twice")
    )
    assert dict(tagged.build().nodes["twice"].tags) == {"owner": "data"}
    # The checks `@parameterize` makes, every culprit in one message, sorted.
    with pytest.raises(
        TypeError,
        match=(
            r"^expected source\(\.\.\.\), value\(\.\.\.\) or group\(\.\.\.\) for: "
            r"parameter 'factor' \(int\), parameter 'value' \(str\)$"
        ),
    ):
        fnloom.node(scaled, bind=cast("Any", {"value": "x", "factor": 3}))
    with pytest.raises(
        ValueError,
        match=(
            r"^the function does not have the parameters named by: "
            r"parameter 'nope', parameter 'zz'$"
        ),
    ):
        fnloom.node(scaled, bind={"zz": fnloom.value(1), "nope": fnloom.source("a")})
    with pytest.raises(TypeError, match=r"^bind must be a mapping, got list$"):
        fnloom.node(scaled, bind=cast("Any", [("value", fnloom.value(1))]))
    with pytest.raises(TypeError, match=r"^node name must be a str, got int$"):
        fnloom.node(scaled, name=cast("Any", 3))
    # A node name is an identifier: a dotted name is a namespace's (Q72).
    for bad in ("", "a b", "ns.p", "1st"):
        with pytest.raises(
            ValueError,
            match=rf"^node name {re.escape(repr(bad))} is not an identifier$",
        ):
            fnloom.node(scaled, name=bad)
    # One node: a parameterized function or a config variant is given as it is.
    with pytest.raises(ValueError, match=r"^node\(\) makes one node, but function"):
        fnloom.node(module.above, name="x")
    with pytest.raises(ValueError, match=r"^node\(\) names its node, but function"):
        fnloom.node(module.price__eu)
    # A partial and a callable instance are refused, with what to write instead.
    with pytest.raises(TypeError, match=r"functools\.partial .* bind=") as partial:
        fnloom.node(cast("Any", functools.partial(scaled, factor=3)), name="p")
    assert "auth_functions_node" not in str(partial.value)

    class Scaler:
        def __call__(self, value: int) -> int:
            return value

    with pytest.raises(
        TypeError, match=r"^a callable .*Scaler is not a function: wrap it in a def$"
    ):
        fnloom.node(cast("Any", Scaler()), name="s")
    with pytest.raises(TypeError, match=r"^class .* is not a function"):
        fnloom.node(cast("Any", Scaler), name="s")
    with pytest.raises(TypeError, match=r"given to with_functions, not to node\(\)"):
        fnloom.node(cast("Any", spec))


def test_a_lambda_needs_a_name() -> None:
    """A lambda is refused without `name=`, and named with it."""
    with pytest.raises(
        ValueError,
        match=r"^function '.*<lambda>' has no name a node can take: give it one with",
    ):
        fnloom.Builder().with_functions(lambda x: x)
    with pytest.raises(ValueError, match=r"has no name a node can take"):
        fnloom.node(lambda x: x)
    square = cast("Any", lambda x: x * x)
    square.__annotations__ = {"x": int, "return": int}
    driver = fnloom.Builder().with_functions(fnloom.node(square, name="square")).build()
    assert driver.execute(["square"], inputs={"x": 3}) == {"square": 9}

    def plain(x: int) -> int:
        return x

    # Every refused item in one message, sorted; a TypeError when one is no function.
    with pytest.raises(ValueError, match=r"has no name") as named:
        fnloom.Builder().with_functions(plain, lambda: 1, fnloom.exclude(lambda: 2))
    assert len(str(named.value).split("; ")) == len(["no name", "marked @exclude"])
    assert "marked @exclude" in str(named.value)
    with pytest.raises(TypeError) as mixed:
        fnloom.Builder().with_functions(cast("Any", 3), lambda: 1, cast("Any", sys))
    lines = str(mixed.value).split("; ")
    assert lines == sorted(lines)
    assert len(lines) == len(["3", "the lambda", "sys"])
    assert "expected a function or fnloom.node(...), got int" in lines
    assert "module 'sys' is not a function: give it to with_modules" in lines


def test_an_excluded_function_given_explicitly_is_refused() -> None:
    """`@exclude` on a function given to `with_functions` is an error, not a no-op."""

    @fnloom.exclude
    def helper(x: int) -> int:
        return x

    with pytest.raises(ValueError, match=r"^function '.*helper' is marked @exclude"):
        fnloom.Builder().with_functions(helper)
    with pytest.raises(ValueError, match=r"marked @exclude"):
        fnloom.node(helper, name="h")
    # A module's `__all__` does not govern a function given alone.
    module = make_module(
        "auth_functions_unlisted",
        """
        __all__ = ["listed"]


        def listed(x: int) -> int:
            return x


        def unlisted(x: int) -> int:
            return x + 1
        """,
    )
    driver = fnloom.Builder().with_modules(module).with_functions(module.unlisted)
    assert driver.build().node_names() == ["listed", "unlisted", "x"]


def test_functions_and_modules_mix_and_clash_by_name() -> None:
    """Modules and functions mix; one node name twice is the usual duplicate error."""
    module = make_module("auth_functions_mix", _SOURCES)
    other = make_module(
        "auth_functions_other",
        """
        def doubled(x: int) -> int:
            return x * 2


        def tripled(doubled: int) -> int:
            return doubled * 3
        """,
    )
    mixed = fnloom.Builder().with_modules(other).with_functions(module.base).build()
    assert mixed.modules == (other,)
    assert mixed.execute(["tripled", "base"], inputs={"x": 1}) == {
        "tripled": 6,
        "base": 2,
    }
    # Same name, two definitions, in two modules.
    with pytest.raises(
        ValueError,
        match=(
            r"^node 'doubled' is defined in both 'auth_functions_other' and "
            r"'auth_functions_mix' \(given to with_functions\)$"
        ),
    ):
        fnloom.Builder().with_modules(other).with_functions(module.doubled).build()
    # The same function, in its module and given again, counts once.
    again = fnloom.Builder().with_modules(other).with_functions(other.tripled).build()
    assert again.node_names() == ["doubled", "tripled", "x"]
    assert again.nodes["tripled"].origin is other.tripled
    # So does a variant, and a parameterized function, given before their module.
    variants = (
        fnloom.Builder()
        .with_config({"region": "eu"})
        .with_functions(module.price__eu, module.above)
        .with_modules(module)
        .build()
    )
    assert variants.node_names() == [
        "base",
        "doubled",
        "high",
        "low",
        "price",
        "x",
    ]
    assert variants.execute(["price"], inputs={"x": 1}) == {"price": 2.4}
    # Another function of the same name, in the same module, still clashes.
    impostor = make_module(
        "auth_functions_other",
        """
        def tripled(doubled: int) -> int:
            return doubled * 4
        """,
    )
    with pytest.raises(
        ValueError,
        match=(
            r"^node 'tripled' is defined twice in 'auth_functions_other': by function "
            r"'tripled' and by function 'tripled' given to with_functions$"
        ),
    ):
        fnloom.Builder().with_modules(other).with_functions(impostor.tripled).build()
    # Two specs of one name, every clash listed, sorted.
    with pytest.raises(ValueError, match=r"^node 'b2' is defined twice") as twice:
        (
            fnloom.Builder()
            .with_functions(
                module.base,
                fnloom.node(module.base, name="b2"),
                fnloom.node(module.doubled, name="b2"),
                fnloom.node(module.doubled, name="base"),
            )
            .build()
        )
    assert str(twice.value) == (
        "node 'b2' is defined twice in 'auth_functions_mix': by function 'base' "
        "given to with_functions and by function 'doubled' given to with_functions; "
        "node 'base' is defined twice in 'auth_functions_mix': by function 'base' "
        "given to with_functions and by function 'doubled' given to with_functions"
    )
    # One spec given twice counts once.
    spec = fnloom.node(module.doubled, name="d")
    once = fnloom.Builder().with_functions(module.base, spec, spec).build()
    assert once.node_names() == ["base", "d", "x"]


def test_cache_keys_use_the_function_module() -> None:
    """A function's cache key is its own: the same whether given alone or in its module.

    A copy, as `create_temporary_module` makes, belongs to another module and
    misses.
    """
    module = make_module(
        "auth_functions_cache",
        """
        def base(x: int) -> int:
            return x + 1
        """,
    )
    store = fnloom.MemoryCache()
    fnloom.Builder().with_modules(module).with_cache(store).build().execute(
        ["base"], inputs={"x": 1}
    )
    alone = fnloom.Builder().with_functions(module.base).with_cache(store).build()
    statuses = alone.explain_cache(["base"], inputs={"x": 1})
    assert statuses["base"].status == "hit"
    original = module.base
    copied = type(original)(
        original.__code__,
        original.__globals__,
        original.__name__,
        original.__defaults__,
        original.__closure__,
    )
    copied.__annotations__ = dict(original.__annotations__)
    copied.__module__ = "temporary_module_copy"
    elsewhere = fnloom.Builder().with_functions(copied).with_cache(store).build()
    assert elsewhere.nodes["base"].module == "temporary_module_copy"
    assert elsewhere.explain_cache(["base"], inputs={"x": 1})["base"].status == "miss"


def test_a_node_spec_made_directly_is_checked_as_node_checks_it() -> None:
    """The `NodeSpec` constructor makes every check `node()` does, and copies `bind`."""
    module = make_module("auth_functions_spec", _SOURCES)

    def scaled(value: int, factor: int = 2) -> int:
        return value * factor

    with pytest.raises(
        TypeError,
        match=(
            r"^expected source\(\.\.\.\), value\(\.\.\.\) or group\(\.\.\.\) for: "
            r"parameter 'value' \(int\)$"
        ),
    ):
        fnloom.NodeSpec(scaled, "s", cast("Any", {"value": 3}))
    with pytest.raises(
        ValueError,
        match=r"^the function does not have the parameters named by: parameter 'zz'$",
    ):
        fnloom.NodeSpec(scaled, "s", {"zz": fnloom.value(1)})
    with pytest.raises(ValueError, match=r"^node name '' is not an identifier$"):
        fnloom.NodeSpec(scaled, "", {})
    with pytest.raises(ValueError, match=r"has no name a node can take"):
        fnloom.NodeSpec(cast("Any", lambda x: x), "<lambda>")
    with pytest.raises(ValueError, match=r"is parameterized"):
        fnloom.NodeSpec(module.above, "a")
    # `bind` is copied: changing the given mapping later changes nothing.
    given: dict[str, fnloom.Source | fnloom.Value] = {"factor": fnloom.value(3)}
    spec = fnloom.NodeSpec(scaled, "s", given)
    builder = fnloom.Builder().with_functions(spec)
    given["value"] = fnloom.source("nope")
    assert dict(spec.bind) == {"factor": fnloom.value(3)}
    driver = builder.build()
    assert driver.node_names() == ["s", "value"]
    assert driver.execute(["s"], inputs={"value": 2}) == {"s": 6}
    assert fnloom.NodeSpec(scaled, "plain").bind == {}
