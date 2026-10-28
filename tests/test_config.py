"""Tests for the config decorators: conditions are recorded, nothing is resolved."""

from typing import TYPE_CHECKING

import pytest

from fnloom import cache, config, parameterize, source, tag
from fnloom._config import Condition, ConfigPolicy
from fnloom._decorators import decorations

if TYPE_CHECKING:
    from collections.abc import Callable


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
