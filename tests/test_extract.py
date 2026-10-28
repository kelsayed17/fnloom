"""Tests for `@extract_fields`: the producer, its field nodes and their checks."""

import itertools
from typing import TYPE_CHECKING, Any, TypedDict

import pytest

import fnloom
from fnloom import (
    Builder,
    _types,
    config,
    extract_fields,
    parameterize,
    source,
    tag,
    value,
)
from fnloom._decorators import decorations
from fnloom._extract import MISSING_FILL, Extraction, _Extractor
from helpers import make_module

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from types import ModuleType


class Point(TypedDict):
    """A TypedDict for the tests below."""

    x: int
    y: str


def test_mapping_form_makes_typed_fields_and_keeps_the_producer() -> None:
    """Each field is a node of its declared type; the producer keeps its name."""
    module = make_module(
        "extract_mapping",
        """
        from fnloom import extract_fields

        @extract_fields({"a": int, "b": str})
        def p(n: int) -> dict:
            '''The producer.'''
            return {"a": n, "b": str(n)}

        def total(a: int) -> int:
            return a + 1
        """,
    )
    assert decorations(module.p).extract == Extraction(
        "fields", (("a", int, None), ("b", str, None)), MISSING_FILL
    )
    driver = fnloom.build(module)
    assert driver.node_names() == ["a", "b", "n", "p", "total"]
    assert driver.nodes["a"].type is int
    assert driver.nodes["b"].type is str
    assert driver.nodes["p"].type is dict
    assert driver.nodes["a"].dependencies == ("p",)
    assert driver.nodes["a"].origin is module.p
    assert driver.nodes["a"].doc == "The producer."
    assert driver.execute(["a", "b", "p", "total"], inputs={"n": 3}) == {
        "a": 3,
        "b": "3",
        "p": {"a": 3, "b": "3"},
        "total": 4,
    }


@pytest.mark.parametrize(
    ("returns", "expected"),
    [
        ("Point", {"x": int, "y": str}),
        ("dict[str, int]", {"x": int, "y": int}),
        ("Mapping[str, int]", {"x": int, "y": int}),
    ],
)
def test_names_take_types_from_a_typed_dict_or_dict_value_type(
    returns: str, expected: dict[str, type]
) -> None:
    """Names alone take each type from the `TypedDict` or the mapping's value type."""
    module = make_module(
        "extract_names",
        f"""
        from collections.abc import Mapping
        from typing import TypedDict
        from fnloom import extract_fields

        class Point(TypedDict):
            x: int
            y: str

        @extract_fields(["x"], "y")
        def p() -> {returns}:
            return {{"x": 1, "y": "a" if {returns!r} == "Point" else 2}}
        """,
    )
    driver = fnloom.build(module)
    assert {name: driver.nodes[name].type for name in ("x", "y")} == expected


def test_no_arguments_extracts_every_typed_dict_field() -> None:
    """With no fields named, every field of the `TypedDict` return is a node."""
    module = make_module(
        "extract_all",
        """
        from typing import NotRequired, TypedDict
        from fnloom import extract_fields

        class Shape(TypedDict):
            width: int
            label: NotRequired[str]

        @extract_fields()
        def shape() -> Shape:
            return {"width": 2, "label": "box"}
        """,
    )
    driver = fnloom.build(module)
    assert driver.node_names() == ["label", "shape", "width"]
    assert driver.nodes["label"].type is str
    assert driver.execute(["width", "label"]) == {"width": 2, "label": "box"}


def test_declared_types_must_fit_the_return_type() -> None:
    """A declared type the return's value type cannot feed fails the build."""
    module = make_module(
        "extract_fit",
        """
        from fnloom import extract_fields

        @extract_fields({"b": int | None, "a": str, "c": object})
        def p() -> dict[str, int]:
            return {"a": 1, "b": 2, "c": 3}
        """,
    )
    with pytest.raises(
        ValueError,
        match=r"^'p': field 'a' is declared str, but the return type "
        r"dict\[str, int\] gives int$",
    ):
        fnloom.build(module)


def test_declared_types_must_fit_a_typed_dict_and_all_are_listed() -> None:
    """Every field that does not fit is listed, sorted, in one message."""
    module = make_module(
        "extract_fit_td",
        """
        from typing import TypedDict
        from fnloom import extract_fields

        class Point(TypedDict):
            x: int
            y: str

        @extract_fields({"y": int, "x": str})
        def p() -> Point:
            return {"x": 1, "y": "a"}
        """,
    )
    with pytest.raises(ValueError, match=r"field 'x' is declared str.*field 'y'"):
        fnloom.build(module)


def test_mixing_a_mapping_and_names_is_refused() -> None:
    """A mapping with extra names is refused when decorating, naming the names."""
    with pytest.raises(
        ValueError,
        match=r"a mapping of fields to types or field names, not both.*'b', 'c'",
    ):
        extract_fields({"a": int}, "c", "b")


def test_a_missing_typed_dict_field_is_a_value_error() -> None:
    """A name the `TypedDict` lacks is a `ValueError` when decorating, all listed."""

    def p() -> Point:
        return {"x": 1, "y": "a"}

    with pytest.raises(
        ValueError,
        match=r"^'p' returns test_extract\.Point, which has no fields 'w', 'zz'$",
    ):
        extract_fields("x", "zz", "w")(p)


def test_a_missing_typed_dict_field_under_a_forward_reference_fails_the_build() -> None:
    """A return not yet defined when decorating is checked at build instead."""
    module = make_module(
        "extract_forward",
        """
        from typing import TypedDict
        from fnloom import extract_fields

        @extract_fields("zz")
        def p() -> Later:
            return {"x": 1}

        class Later(TypedDict):
            x: int
        """,
    )
    with pytest.raises(
        ValueError,
        match=r"^'p' returns extract_forward\.Later, which has no field 'zz'$",
    ):
        fnloom.build(module)


def test_a_field_of_the_wrong_type_fails_the_run() -> None:
    """The node, field and both types are named, also with input checks off."""
    module = make_module(
        "extract_wrong",
        """
        from fnloom import extract_fields

        @extract_fields({"a": int, "b": int})
        def p(n: int) -> dict:
            return {"a": "not-int", "b": n}
        """,
    )
    for builder in (Builder(), Builder().without_input_type_checks()):
        driver = builder.with_modules(module).build()
        assert driver.execute(["b"], inputs={"n": 1}) == {"b": 1}
        with pytest.raises(
            ValueError,
            match=r"^'p' returned field 'a' as str, but it is declared int\n",
        ) as raised:
            driver.execute(["a"], inputs={"n": 1})
        assert raised.value.__notes__ == ["in node 'a'"]


def test_a_missing_field_names_what_was_returned() -> None:
    """A missing field names the fields the producer did return, sorted."""
    module = make_module(
        "extract_missing",
        """
        from fnloom import extract_fields

        @extract_fields({"a": int, "zz": int})
        def p() -> dict:
            return {"c": 3, "a": 1, "b": 2}
        """,
    )
    driver = fnloom.build(module)
    assert driver.execute(["a"]) == {"a": 1}
    with pytest.raises(
        ValueError, match=r"^'p' returned no field 'zz'; it returned 'a', 'b', 'c'\n"
    ) as raised:
        driver.execute(["zz"])
    assert raised.value.__notes__ == ["in node 'zz'"]


def test_a_producer_returning_no_mapping_is_named() -> None:
    """A value that cannot hold fields is a `TypeError` naming its type."""
    module = make_module(
        "extract_not_mapping",
        """
        from fnloom import extract_fields

        @extract_fields({"a": int})
        def p() -> dict:
            return None  # type: ignore[return-value]
        """,
    )
    with pytest.raises(
        TypeError, match=r"^'p' returned NoneType, which has no field 'a'\n"
    ):
        fnloom.build(module).execute(["a"])


def test_extra_keys_stay_on_the_producer() -> None:
    """Keys no field names are not nodes, and the producer keeps them."""
    module = make_module(
        "extract_extra",
        """
        from fnloom import extract_fields

        @extract_fields("a")
        def p() -> dict[str, int]:
            return {"a": 1, "extra": 2}
        """,
    )
    driver = fnloom.build(module)
    assert driver.node_names() == ["a", "p"]
    assert driver.execute(["a", "p"]) == {"a": 1, "p": {"a": 1, "extra": 2}}


def test_fill_with_fills_without_mutating() -> None:
    """The fill reaches the field only; the returned module-level dict is unchanged."""
    module = make_module(
        "extract_fill",
        """
        from fnloom import extract_fields

        SHARED: dict[str, int] = {"a": 1}

        @extract_fields({"a": int, "b": int}, fill_with=-1)
        def p() -> dict:
            return SHARED
        """,
    )
    driver = fnloom.build(module)
    assert driver.execute(["a", "b", "p"]) == {"a": 1, "b": -1, "p": {"a": 1}}
    assert module.SHARED == {"a": 1}


def test_none_is_a_fill_value() -> None:
    """`fill_with=None` fills with `None`; leaving it out means no fill."""
    module = make_module(
        "extract_fill_none",
        """
        from fnloom import extract_fields

        @extract_fields({"a": int, "b": int}, fill_with=None)
        def p() -> dict:
            return {"a": 1}
        """,
    )
    extraction = decorations(module.p).extract
    assert extraction is not None
    assert extraction.fill is None
    assert fnloom.build(module).execute(["b"]) == {"b": None}


def test_a_fill_is_checked_like_a_field() -> None:
    """A fill of the wrong type fails the run as a returned value would."""
    module = make_module(
        "extract_fill_wrong",
        """
        from fnloom import extract_fields

        @extract_fields({"b": int}, fill_with="none")
        def p() -> dict:
            return {}
        """,
    )
    with pytest.raises(ValueError, match=r"field 'b' as str, but it is declared int"):
        fnloom.build(module).execute(["b"])


_TAGGED = """
from fnloom import extract_fields, tag

{first}
{second}
def p() -> dict:
    return {{"a": 1}}
"""


@pytest.mark.parametrize("order", [0, 1])
def test_tags_land_on_producer_and_fields(order: int) -> None:
    """`@tag` tags the producer and every field, in either order (Q63)."""
    pair = ['@tag(owner="z")', '@extract_fields({"a": int})']
    if order:
        pair.reverse()
    module = make_module(
        f"extract_tags_{order}", _TAGGED.format(first=pair[0], second=pair[1])
    )
    driver = fnloom.build(module)
    assert driver.nodes["p"].tags == {"owner": "z"}
    assert driver.nodes["a"].tags == {"owner": "z"}


def test_one_parameterize_row_works_and_two_are_refused() -> None:
    """One row's node is the producer; two rows are refused in either order."""
    module = make_module(
        "extract_row",
        """
        from fnloom import extract_fields, parameterize, value

        @extract_fields({"a": int})
        @parameterize(pp={"n": value(4)})
        def p(n: int) -> dict:
            return {"a": n}
        """,
    )
    driver = fnloom.build(module)
    assert driver.node_names() == ["a", "pp"]
    assert driver.nodes["a"].dependencies == ("pp",)
    assert driver.execute(["a"]) == {"a": 4}

    def q(n: int) -> dict[str, int]:
        return {"a": n}

    rows = parameterize(r1={"n": value(1)}, r2={"n": source("m")})
    with pytest.raises(
        ValueError, match=r"needs one node, but parameterize makes 2: 'r1', 'r2'"
    ):
        extract_fields({"a": int})(rows(q))

    def r(n: int) -> dict[str, int]:
        return {"a": n}

    extract_fields({"a": int})(r)
    with pytest.raises(
        ValueError, match=r"needs one node, but parameterize makes 2: 'r1', 'r2'"
    ):
        rows(r)


_CONFIGURED = """
from fnloom import config, extract_fields

{first}
{second}
def p__a() -> dict:
    return {{"a": 1}}

{third}
{fourth}
def p__b() -> dict:
    return {{"a": 2, "b": 3}}
"""


@pytest.mark.parametrize("order", [0, 1])
def test_composes_with_config_in_either_order(order: int) -> None:
    """Only the selected variant is lowered, whichever decorator is outermost."""
    a_pair = ['@config.when(mode="a")', '@extract_fields({"a": int})']
    b_pair = ['@config.when(mode="b")', '@extract_fields({"a": int, "b": int})']
    if order:
        a_pair.reverse()
        b_pair.reverse()
    module = make_module(
        f"extract_config_{order}",
        _CONFIGURED.format(
            first=a_pair[0], second=a_pair[1], third=b_pair[0], fourth=b_pair[1]
        ),
    )
    a = Builder().with_config({"mode": "a"}).with_modules(module).build()
    b = Builder().with_config({"mode": "b"}).with_modules(module).build()
    assert a.node_names() == ["a", "p"]
    assert b.node_names() == ["a", "b", "p"]
    assert a.execute(["a"]) == {"a": 1}
    assert b.execute(["a", "b"]) == {"a": 2, "b": 3}
    assert [c.key for c in b.nodes["b"].config] == ["mode"]


class _Duck:
    """Neither a dict nor a `Mapping`, for the type checks below."""


_SAMPLES: list[object] = [
    {"x": 1, "y": "a"},
    {},
    {"x": "wrong"},
    1,
    "a",
    None,
    1.5,
    [1],
    _Duck(),
]


def _input_module(declared: object) -> ModuleType:
    """A module whose one node reads input `v`, declared as `declared`."""
    module = make_module(
        "extract_input_check",
        """
        def out(v: Declared) -> int:
            return 1
        """.replace("Declared", "declared_type"),
    )
    setattr(module, "declared_type", declared)  # noqa: B010  # a module attribute ty cannot see
    return module


def _accepts(call: Callable[[], object]) -> bool:
    """Whether `call` returns, rather than raising a type-check `ValueError`."""
    try:
        call()
    except ValueError:
        return False
    return True


@pytest.mark.parametrize("declared", [Point, int | None, Any])
def test_field_checks_match_input_checks(declared: object) -> None:
    """A field accepts exactly what the input check accepts (Review Focus 3)."""
    extractor = _Extractor("p", "f", declared, MISSING_FILL)
    driver = fnloom.build(_input_module(declared))
    for sample in _SAMPLES:
        as_input = _accepts(lambda s=sample: driver.execute(["out"], inputs={"v": s}))
        as_field = _accepts(lambda s=sample: extractor(p={"f": s}))
        assert as_field == as_input == _types.value_matches(declared, sample), sample


def test_a_typed_dict_takes_any_dict() -> None:
    """A `TypedDict` type is checked by its kind: any `dict`, its keys unread (Q61)."""
    assert _types.value_matches(Point, {"x": 1, "y": "a"})
    assert _types.value_matches(Point, {"other": 1})
    assert not _types.value_matches(Point, [("x", 1)])


def test_decoration_refusals() -> None:
    """Each malformed argument is refused when decorating, every culprit listed."""
    with pytest.raises(ValueError, match="an empty mapping"):
        extract_fields({})
    with pytest.raises(ValueError, match="at least one field name"):
        extract_fields([])
    with pytest.raises(
        TypeError, match=r"a type for each field: 'a' \(5\), 'b' \('int'\)"
    ):
        extract_fields({"b": "int", "a": 5})
    with pytest.raises(TypeError, match=r"field names must be str, got: 1 \(int\)"):
        extract_fields({1: int})  # ty: ignore[invalid-argument-type]  # the misuse under test
    with pytest.raises(TypeError, match=r"field names must be str, got: 2 \(int\)"):
        extract_fields(["a", 2])  # ty: ignore[invalid-argument-type]  # the misuse under test
    with pytest.raises(TypeError, match=r"takes a mapping, field names or nothing"):
        extract_fields(5)  # ty: ignore[invalid-argument-type]  # the misuse under test

    def p() -> dict:  # ty: ignore[missing-type-argument]  # a bare dict under test
        return {}

    def q() -> dict[str, int]:
        return {}

    def r() -> list[int]:
        return []

    def s():  # noqa: ANN202  # the missing annotation under test
        return {}

    with pytest.raises(ValueError, match=r"^'p': field names take their types"):
        extract_fields("a")(p)
    with pytest.raises(ValueError, match=r"^'q': extract_fields with no fields"):
        extract_fields()(q)
    with pytest.raises(
        ValueError, match=r"^'r': extract_fields needs a dict, Mapping or TypedDict"
    ):
        extract_fields({"a": int})(r)
    with pytest.raises(ValueError, match=r"^'s': extract_fields needs a return"):
        extract_fields({"a": int})(s)
    extract_fields({"a": int})(p)
    with pytest.raises(ValueError, match="apply extract_fields once"):
        extract_fields({"a": int})(p)


def test_names_are_deduplicated_and_returned_function_is_itself() -> None:
    """A repeated name is one field, as in Hamilton; the function is returned."""

    def p() -> dict[str, int]:
        return {"a": 1}

    assert extract_fields("a", "a")(p) is p
    extraction = decorations(p).extract
    assert extraction is not None
    assert extraction.items == (("a", None, None),)


def test_a_field_named_like_another_node_is_a_duplicate() -> None:
    """A field clashing with its producer or another function fails the build."""
    module = make_module(
        "extract_clash",
        """
        from fnloom import extract_fields

        @extract_fields({"p": int, "other": int})
        def p() -> dict:
            return {"p": 1}

        def other() -> int:
            return 1
        """,
    )
    with pytest.raises(
        ValueError, match=r"node 'other' is defined twice.*a field of function 'p'"
    ):
        fnloom.build(module)


@pytest.mark.parametrize("order", list(itertools.permutations(range(3))))
def test_decorator_order_never_matters(order: tuple[int, ...]) -> None:
    """`extract_fields`, `tag` and `config.when` record the same in any order."""

    def p() -> dict[str, int]:
        return {"a": 1}

    decorators = [
        extract_fields({"a": int}, fill_with=0),
        tag(owner="z"),
        config.when(mode="a"),
    ]
    for index in order:
        decorators[index](p)
    recorded = decorations(p)
    assert recorded.extract == Extraction("fields", (("a", int, None),), 0)
    assert recorded.tags == {"owner": "z"}
    assert recorded.config is not None


def test_mapping_type_is_kept_as_written() -> None:
    """A `Mapping[str, T]` return is accepted (Q61)."""

    def p() -> Mapping[str, int]:
        return {"a": 1}

    assert extract_fields("a")(p) is p


def test_every_field_of_a_forward_referenced_typed_dict() -> None:
    """A `TypedDict` defined after its function gives its fields at build."""
    module = make_module(
        "extract_forward_all",
        """
        from typing import TypedDict
        from fnloom import extract_fields

        @extract_fields()
        def p() -> Later:
            return {"x": 1, "y": "a"}

        class Later(TypedDict):
            x: int
            y: str
        """,
    )
    driver = fnloom.build(module)
    assert driver.node_names() == ["p", "x", "y"]
    assert driver.nodes["y"].type is str
    assert driver.execute(["x", "y"]) == {"x": 1, "y": "a"}


def test_a_clash_found_only_at_build_is_refused() -> None:
    """A forward-referenced field clashing with a function fails the build."""
    module = make_module(
        "extract_forward_clash",
        """
        from typing import TypedDict
        from fnloom import extract_fields

        @extract_fields()
        def p() -> Later:
            return {"x": 1}

        def x() -> int:
            return 2

        class Later(TypedDict):
            x: int
        """,
    )
    with pytest.raises(ValueError, match=r"node 'x' is defined twice"):
        fnloom.build(module)


def test_an_unresolved_typed_dict_field_names_the_producer() -> None:
    """A `TypedDict` field type that does not resolve fails the build, noted."""
    module = make_module(
        "extract_unresolved",
        """
        from typing import TypedDict
        from fnloom import extract_fields

        class Broken(TypedDict):
            x: Missing

        @extract_fields("x")
        def p() -> Broken:
            return {"x": 1}
        """,
    )
    with pytest.raises(NameError, match="Missing") as raised:
        fnloom.build(module)
    assert raised.value.__notes__ == [
        "resolving the fields of extract_unresolved.Broken, returned by 'p'"
    ]
