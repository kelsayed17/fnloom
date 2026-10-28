"""Authoring: `@exclude` and `__all__` decide which functions are nodes (Q73)."""

import copy
import pickle
from typing import TYPE_CHECKING, Any, cast

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
