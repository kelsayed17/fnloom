"""Tests for the decorators: metadata is recorded, the function comes back as is."""

from typing import assert_type

import pytest

import fnloom
from fnloom._decorators import (
    CachePolicy,
    Decorations,
    cache,
    decorations,
    parameterize,
    source,
    tag,
    value,
)


def f(x: int) -> int:
    """A plain function no test decorates."""
    return x


def _rowed_source(x: int) -> int:
    return x


rowed = parameterize(a={"x": value(1)})(_rowed_source)
assert_type(rowed(1), int)


def _fresh(x: int) -> int:
    return x


def test_parameterize_returns_the_same_function() -> None:
    """A decorator hands back the object it was given."""

    def g(x: int) -> int:
        return x

    assert parameterize(a={"x": value(1)})(g) is g


def test_tag_returns_the_same_function() -> None:
    """`tag` hands back the object it was given."""

    def g(x: int) -> int:
        return x

    assert tag(owner="a")(g) is g


def test_decorators_keep_the_signature_for_type_checkers() -> None:
    """The module-level `assert_type` above is checked by ty; the call still works."""
    assert rowed(2) == 2  # noqa: PLR2004


def test_rows_and_tags_are_recorded_in_either_order() -> None:
    """Stacking order of `tag` and `parameterize` does not change the metadata."""

    @tag(owner="a")
    @parameterize(r={"x": source("up")})
    def over(x: int) -> int:
        return x

    @parameterize(r={"x": source("up")})
    @tag(owner="a")
    def under(x: int) -> int:
        return x

    assert decorations(over) == decorations(under)
    assert decorations(over) == Decorations({"r": {"x": source("up")}}, {"owner": "a"})


def test_plain_function_has_empty_decorations() -> None:
    """An undecorated function reports no rows and no tags."""
    assert decorations(f) == Decorations(None, {})


def test_empty_parameterize_is_a_value_error() -> None:
    """`parameterize()` with no rows is refused."""
    with pytest.raises(ValueError, match="at least one row"):
        parameterize()


def test_row_value_of_wrong_type_is_a_type_error() -> None:
    """A row value must be a `Source` or a `Value`."""
    with pytest.raises(TypeError, match="'x'"):
        parameterize(a={"x": 1})(_fresh)  # ty: ignore[invalid-argument-type]


def test_row_naming_an_unknown_parameter_is_a_value_error() -> None:
    """The message names the row and the parameter the function lacks."""
    with pytest.raises(ValueError, match=r"row 'a'.*'nope'"):
        parameterize(a={"nope": value(1)})(_fresh)


def test_parameterize_twice_is_a_value_error() -> None:
    """One function takes one `parameterize`."""

    def g(x: int) -> int:
        return x

    parameterize(a={"x": value(1)})(g)
    with pytest.raises(ValueError, match="already parameterized"):
        parameterize(b={"x": value(2)})(g)


def test_non_str_tag_is_a_type_error() -> None:
    """Tag values are strings."""
    with pytest.raises(TypeError, match="'owner'"):
        tag(owner=1)  # ty: ignore[invalid-argument-type]


def test_repeated_tag_key_is_a_value_error() -> None:
    """The same tag key twice on one function is refused."""

    def g(x: int) -> int:
        return x

    tag(owner="a")(g)
    with pytest.raises(ValueError, match="'owner'"):
        tag(owner="b")(g)


def test_public_names_are_exported() -> None:
    """The six decorator names are public."""
    for name in ("parameterize", "source", "value", "tag", "Source", "Value"):
        assert hasattr(fnloom, name)
        assert name in fnloom.__all__


def test_every_unknown_parameter_is_listed_sorted_in_one_message() -> None:
    """Two bad parameters in one row and one in another all appear, sorted."""
    with pytest.raises(ValueError, match="parameters named by") as excinfo:
        parameterize(
            z={"x": value(1), "y2": value(1), "y1": value(1)},
            a={"nope": value(1)},
        )(_fresh)
    message = str(excinfo.value)
    positions = [
        message.index(f"row {r!r} parameter {p!r}")
        for r, p in [("a", "nope"), ("z", "y1"), ("z", "y2")]
    ]
    assert positions == sorted(positions)
    assert "'x'" not in message


def test_every_wrong_binding_is_listed_sorted_in_one_message() -> None:
    """Wrong-type bindings across rows are all reported, sorted, before names."""
    with pytest.raises(TypeError, match="source") as excinfo:
        parameterize(
            z={"x": 1, "nope": 2},  # ty: ignore[invalid-argument-type]
            a={"x": "s"},  # ty: ignore[invalid-argument-type]
        )(_fresh)
    message = str(excinfo.value)
    positions = [
        message.index(f"row {r!r} parameter {p!r}")
        for r, p in [("a", "x"), ("z", "nope"), ("z", "x")]
    ]
    assert positions == sorted(positions)


def test_row_that_is_not_a_mapping_is_a_type_error() -> None:
    """A non-mapping row names the row."""
    with pytest.raises(TypeError, match="row 'a'"):
        parameterize(a=1)  # ty: ignore[invalid-argument-type]


def test_parameterize_does_not_evaluate_annotations() -> None:
    """An unresolvable annotation is the build's error, not the decorator's."""

    def f(x: Missing) -> Missing:  # noqa: F821  # ty: ignore[unresolved-reference]  # the unresolvable name is the point
        return x

    assert parameterize(out={"x": source("a")})(f) is f


def test_every_row_that_is_not_a_mapping_is_listed_sorted_in_one_message() -> None:
    """Two non-mapping rows are both named, sorted, in one `TypeError`."""
    with pytest.raises(TypeError) as excinfo:
        parameterize(z=1, a="s", ok={"x": value(1)})  # ty: ignore[invalid-argument-type]
    assert str(excinfo.value) == (
        "row 'a': expected a mapping, got str; row 'z': expected a mapping, got int"
    )


def test_every_non_str_tag_is_listed_sorted_in_one_message() -> None:
    """Two non-str tag values are both named, sorted, in one `TypeError`."""
    with pytest.raises(TypeError) as excinfo:
        tag(zone=1, area=["a"], ok="s")  # ty: ignore[invalid-argument-type]
    assert str(excinfo.value) == (
        "tag 'area': value must be str, got list; "
        "tag 'zone': value must be str, got int"
    )


def test_cache_records_a_policy_and_returns_the_function() -> None:
    """`cache` stores a frozen policy and hands back the same function."""

    def g(x: int) -> int:
        return x

    assert cache(behavior="recompute")(g) is g
    assert decorations(g).cache == CachePolicy("recompute", frozenset(), None)
    assert decorations(f).cache is None


def test_cache_records_ignore_and_version() -> None:
    """`ignore` becomes a frozenset and `version` is kept as given."""

    def g(x: int, y: int) -> int:
        return x + y

    cache(ignore=("y", "x"), version="2")(g)
    assert decorations(g).cache == CachePolicy("default", frozenset("xy"), "2")


def test_cache_composes_with_parameterize_and_tag_in_either_order() -> None:
    """Stacking order does not change the recorded metadata."""

    @cache(behavior="disable")
    @tag(owner="a")
    @parameterize(r={"x": source("up")})
    def over(x: int) -> int:
        return x

    @parameterize(r={"x": source("up")})
    @tag(owner="a")
    @cache(behavior="disable")
    def under(x: int) -> int:
        return x

    assert decorations(over) == decorations(under)
    assert decorations(over).cache == CachePolicy("disable", frozenset(), None)
    assert decorations(over).tags == {"owner": "a"}


def test_cache_errors() -> None:
    """Each bad argument names every culprit, sorted."""
    with pytest.raises(ValueError, match="'default', 'recompute', 'disable'"):
        cache(behavior="never")  # ty: ignore[invalid-argument-type]

    def g(x: int) -> int:
        return x

    with pytest.raises(ValueError, match="'a', 'b'"):
        cache(ignore=("b", "x", "a"))(g)
    assert decorations(g).cache is None

    cache()(g)
    with pytest.raises(ValueError, match="apply cache once"):
        cache(behavior="disable")(g)

    for bad in ("", 2):
        with pytest.raises(TypeError, match="version"):
            cache(version=bad)  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="int, int"):
        cache(ignore=(1, 2))  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match="not one str"):
        cache(ignore="x")


def test_cache_does_not_evaluate_annotations() -> None:
    """An annotation naming something undefined is fine at decoration time."""

    @cache(ignore=("x",))
    def g(x: Undefined) -> Undefined:  # noqa: F821  # ty: ignore[unresolved-reference]
        return x

    assert decorations(g).cache is not None


def test_cache_names_are_exported() -> None:
    """`cache` and `CachePolicy` are public."""
    for name in ("cache", "CachePolicy"):
        assert hasattr(fnloom, name)
        assert name in fnloom.__all__
