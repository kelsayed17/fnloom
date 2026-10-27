"""Tests for the decorators: metadata is recorded, the function comes back as is."""

from typing import assert_type

import pytest

import fnloom
from fnloom._decorators import (
    Decorations,
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
