"""Tests for `@extract_fields` and `@extract_columns`: producers, nodes, checks."""

import itertools
import sys
from types import ModuleType
from typing import TYPE_CHECKING, Any, TypedDict, override

import pytest

import fnloom
from fnloom import (
    Builder,
    CacheOutcome,
    DiskCache,
    MemoryCache,
    _types,
    config,
    extract_columns,
    extract_fields,
    parameterize,
    source,
    tag,
    value,
)
from fnloom._decorators import decorations
from fnloom._extract import MISSING_FILL, Extraction, _Extractor, column_type
from fnloom._scan import collect, resolve
from helpers import make_module

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path


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


def test_every_producer_with_an_unresolved_typed_dict_is_named() -> None:
    """Two producers whose `TypedDict` fields do not resolve: one error, both named."""
    module = make_module(
        "extract_unresolved_two",
        """
        from typing import TypedDict
        from fnloom import extract_fields

        class Broken(TypedDict):
            x: Missing

        class Other(TypedDict):
            y: Absent

        @extract_fields("y")
        def q() -> Other:
            return {"y": 1}

        @extract_fields("x")
        def p() -> Broken:
            return {"x": 1}
        """,
    )
    with pytest.raises(NameError, match="Missing") as raised:
        fnloom.build(module)
    assert raised.value.__notes__ == [
        (
            "resolving the fields of extract_unresolved_two.Broken, returned by 'p'; "
            "extract_unresolved_two.Other, returned by 'q'"
        )
    ]


class _CountingCache(MemoryCache):
    """A memory store that counts each `put`, so the native walk is not used."""

    def __init__(self) -> None:
        """Starts empty, with no puts."""
        super().__init__()
        self.puts = 0

    @override
    def put(self, key: bytes, value: object) -> None:
        self.puts += 1
        super().put(key, value)


# The calls are recorded in a global, so each node pins its code version.
_CACHED = """
from fnloom import cache, extract_fields

CALLS: list[str] = []

@cache(version="1")
@extract_fields({{"mean": {declared}}}, fill_with={fill})
def averages(x: int) -> dict:
    CALLS.append("averages")
    return {{}}

@cache(version="1")
def doubled(mean: object) -> object:
    CALLS.append("doubled")
    return 2 * mean
"""


def _cached(name: str, fill: str = "0.0", declared: str = "float") -> ModuleType:
    return make_module(name, _CACHED.format(fill=fill, declared=declared))


def test_a_rebuild_with_another_extraction_is_not_served_stale(tmp_path: Path) -> None:
    """A new fill shows, and a new declared type is checked, with a `DiskCache`."""
    store = DiskCache(tmp_path / "cache")
    for fill in ("0.0", "1.0"):
        module = _cached("extract_cached", fill=fill)
        driver = Builder().with_modules(module).with_cache(store).build()
        assert driver.execute(["mean", "doubled"], inputs={"x": 1}) == {
            "mean": float(fill),
            "doubled": 2 * float(fill),
        }
    module = _cached("extract_cached", declared="str")
    driver = Builder().with_modules(module).with_cache(store).build()
    with pytest.raises(ValueError, match=r"'mean' as float, but it is declared str"):
        driver.execute(["mean"], inputs={"x": 1})


@pytest.mark.parametrize("kind", ["counting", "native"])
def test_a_field_is_keyed_never_stored_and_its_consumer_hits(kind: str) -> None:
    """The consumer of a field hits on a warm run; the field is never stored."""
    store = _CountingCache() if kind == "counting" else MemoryCache()
    module = _cached(f"extract_keyed_{kind}")
    driver = Builder().with_modules(module).with_cache(store).build()
    assert driver.execute(["doubled"], inputs={"x": 1}) == {"doubled": 0.0}
    assert module.CALLS == ["averages", "doubled"]
    assert driver.execute(["doubled"], inputs={"x": 1}) == {"doubled": 0.0}
    assert module.CALLS == ["averages", "doubled"]
    if isinstance(store, _CountingCache):
        # One put for each node that ran, the producer and the consumer; never
        # one for the field.
        assert store.puts == len(module.CALLS)
    module.CALLS.clear()
    # A request for the field runs it, from the stored producer, every time.
    assert driver.execute(["mean"], inputs={"x": 1}) == {"mean": 0.0}
    assert module.CALLS == []
    assert driver.explain_cache(["mean", "doubled"], inputs={"x": 1}) == {
        "averages": CacheOutcome("hit", None),
        "doubled": CacheOutcome("hit", None),
        "mean": CacheOutcome(
            "miss", "extracted field: always recomputed from its producer"
        ),
    }


def test_an_unfingerprintable_fill_makes_the_field_uncacheable() -> None:
    """A fill with no fingerprint leaves the field, and what reads it, uncacheable."""
    module = _cached("extract_unkeyed", fill="object()")
    driver = Builder().with_modules(module).with_cache(MemoryCache()).build()
    outcomes = driver.explain_cache(["doubled"], inputs={"x": 1})
    assert outcomes["mean"].status == "uncacheable"
    assert outcomes["mean"].reason is not None
    assert outcomes["mean"].reason.startswith("fill_with of field 'mean'")
    columns = make_module(
        "columns_unkeyed",
        """
        from fnloom import extract_columns

        @extract_columns("a", fill_with=object())
        def p() -> dict:
            return {}
        """,
    )
    driver = Builder().with_modules(columns).with_cache(MemoryCache()).build()
    reason = driver.explain_cache(["a"])["a"].reason
    assert reason is not None
    assert reason.startswith("fill_with of column 'a'")
    assert outcomes["doubled"] == CacheOutcome(
        "uncacheable", "reads uncacheable 'mean'"
    )


def test_the_producer_format_does_not_apply_to_fields(tmp_path: Path) -> None:
    """A producer's `@cache(format=...)` stores the producer only."""
    module = make_module(
        "extract_format",
        """
        from fnloom import cache, extract_fields

        @cache(format="json")
        @extract_fields({"a": int})
        def p() -> dict:
            return {"a": 1}
        """,
    )
    store = DiskCache(tmp_path / "cache")
    driver = Builder().with_modules(module).with_cache(store).build()
    assert driver.execute(["a", "p"]) == {"a": 1, "p": {"a": 1}}
    assert driver.execute(["a", "p"]) == {"a": 1, "p": {"a": 1}}


# `@extract_columns`: the columns of whatever the producer returns.


@pytest.fixture(scope="module")
def pd() -> ModuleType:
    """pandas, imported once, before a test's modules are forgotten.

    NumPy, under it, cannot be imported a second time in one process.
    """
    module = pytest.importorskip("pandas")
    assert isinstance(module, ModuleType)
    return module


@pytest.fixture(scope="module")
def pl() -> ModuleType:
    """polars, imported once, as `pd` is."""
    module = pytest.importorskip("polars")
    assert isinstance(module, ModuleType)
    return module


def _values(result: Mapping[str, object]) -> dict[str, Any]:
    """A run's result, its values typed `Any` to read frame attributes off them."""
    return dict(result)


def test_columns_split_a_mapping() -> None:
    """A mapping's columns are nodes typed `Any`; the producer keeps its name."""
    module = make_module(
        "columns_mapping",
        """
        from fnloom import extract_columns

        @extract_columns("a", "b")
        def p(n: int) -> dict:
            '''The producer.'''
            return {"a": [n], "b": [n + 1], "extra": []}

        def total(a: list[int], b: list[int]) -> int:
            return sum(a) + sum(b)
        """,
    )
    assert decorations(module.p).extract == Extraction(
        "columns", (("a", None, None), ("b", None, None)), MISSING_FILL
    )
    driver = fnloom.build(module)
    assert driver.node_names() == ["a", "b", "n", "p", "total"]
    assert driver.nodes["a"].type is Any
    assert driver.nodes["a"].dependencies == ("p",)
    assert driver.nodes["a"].doc == "The producer."
    assert isinstance(_extract_payload(module, "a"), _Extractor)
    assert driver.execute(["a", "b", "p", "total"], inputs={"n": 1}) == {
        "a": [1],
        "b": [2],
        "p": {"a": [1], "b": [2], "extra": []},
        "total": 3,
    }


def _extract_payload(module: ModuleType, name: str) -> object:
    """The payload of node `name` of `module`, resolved as a build resolves it."""
    nodes = {node.name: node for node in resolve(collect([module]))}
    return nodes[name].payload


def test_columns_split_a_pandas_frame_into_series(pd: ModuleType) -> None:
    """A `pandas.DataFrame` producer gives `pandas.Series` columns, checked."""
    module = make_module(
        "columns_pandas",
        """
        import pandas as pd

        from fnloom import extract_columns

        @extract_columns("a", "b")
        def frame(n: int) -> pd.DataFrame:
            return pd.DataFrame({"a": [n, n], "b": ["x", "y"], "c": [0, 0]})
        """,
    )
    driver = fnloom.build(module)
    assert driver.nodes["a"].type is pd.Series
    assert driver.nodes["b"].type is pd.Series
    result = _values(driver.execute(["a", "b", "frame"], inputs={"n": 3}))
    assert result["a"].tolist() == [3, 3]
    assert result["b"].tolist() == ["x", "y"]
    assert list(result["frame"].columns) == ["a", "b", "c"]
    unchecked = Builder().with_modules(module).without_input_type_checks().build()
    assert _values(unchecked.execute(["a"], inputs={"n": 1}))["a"].tolist() == [1, 1]


def test_column_type_reads_the_frame_type_by_name(
    pd: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only pandas and polars frames give a Series; anything else gives `Any`."""
    assert column_type(dict) is Any
    assert column_type(list[int]) is Any
    assert column_type("pd.DataFrame") is Any
    fake = type("DataFrame", (), {"__module__": "polars.dataframe.frame"})
    # A polars-like frame with no polars imported gives `Any`, not an import.
    monkeypatch.delitem(sys.modules, "polars", raising=False)
    assert column_type(fake) is Any
    assert "polars" not in sys.modules
    # With a stand-in `polars` imported, its `Series` is the column type.
    series = type("Series", (), {})
    monkeypatch.setitem(sys.modules, "polars", ModuleType("polars"))
    monkeypatch.setattr(sys.modules["polars"], "Series", series, raising=False)
    assert column_type(fake) is series
    assert column_type(pd.DataFrame) is pd.Series
    assert column_type(pd.Series) is Any


def test_a_list_argument_is_refused_with_the_star_hint() -> None:
    """A list is refused with Hamilton's advice; so are no columns and non-names."""
    with pytest.raises(
        TypeError, match=r"got a list: put `\*` in front of it to expand it"
    ):
        extract_columns(["a", "b"])  # ty: ignore[invalid-argument-type]  # the misuse under test
    with pytest.raises(ValueError, match="extract_columns got no columns"):
        extract_columns()
    with pytest.raises(
        TypeError,
        match=r"column names or \(name, doc\) pairs, got: \('a',\), 1",
    ):
        extract_columns("a", 1, ("a",))  # ty: ignore[invalid-argument-type]  # the misuse under test
    with pytest.raises(ValueError, match=r"more than once: 'a'$"):
        extract_columns("a", ("a", "doc"))
    # Never de-duplicated: every repeat is listed, sorted (Q64).
    with pytest.raises(ValueError, match=r"more than once: 'a', 'b'$"):
        extract_columns("b", "a", "b", "a", "c")

    def p() -> dict[str, int]:
        return {}

    assert extract_columns("a")(p) is p
    extraction = decorations(p).extract
    assert extraction is not None
    assert extraction.items == (("a", None, None),)
    with pytest.raises(ValueError, match="apply extract_columns once"):
        extract_columns("b")(p)
    with pytest.raises(ValueError, match="extract_fields and extract_columns"):
        extract_fields("b")(p)


def test_a_missing_column_names_the_columns_returned() -> None:
    """A missing column is a `ValueError` naming the producer and its columns."""
    module = make_module(
        "columns_missing",
        """
        from fnloom import extract_columns

        @extract_columns("zz")
        def p() -> dict:
            return {"b": 1, "a": 2}
        """,
    )
    with pytest.raises(
        ValueError, match=r"^'p' returned no column 'zz'; it returned 'a', 'b'\n"
    ):
        fnloom.build(module).execute(["zz"])


@pytest.mark.usefixtures("pd")
def test_a_missing_pandas_column_names_the_frame_columns() -> None:
    """A frame's columns are read from `.columns`."""
    module = make_module(
        "columns_missing_pandas",
        """
        import pandas as pd

        from fnloom import extract_columns

        @extract_columns("zz")
        def frame() -> pd.DataFrame:
            return pd.DataFrame({"b": [1], "a": [2]})
        """,
    )
    with pytest.raises(
        ValueError, match=r"^'frame' returned no column 'zz'; it returned 'a', 'b'\n"
    ):
        fnloom.build(module).execute(["zz"])


def test_fill_with_builds_a_full_column_without_mutating(pd: ModuleType) -> None:
    """pandas: a full Series on the frame's index; the frame keeps its columns."""
    module = make_module(
        "columns_fill_pandas",
        """
        import pandas as pd

        from fnloom import extract_columns

        @extract_columns("a", "zz", fill_with=0)
        def frame() -> pd.DataFrame:
            return pd.DataFrame({"a": [1, 2]}, index=[10, 20])
        """,
    )
    result = _values(fnloom.build(module).execute(["zz", "frame"]))
    filled = result["zz"]
    assert isinstance(filled, pd.Series)
    assert filled.name == "zz"
    assert filled.tolist() == [0, 0]
    assert filled.index.tolist() == [10, 20]
    assert list(result["frame"].columns) == ["a"]


def test_fill_with_builds_a_full_polars_column(pl: ModuleType) -> None:
    """polars: a full Series named by the column, as long as the frame."""
    module = make_module(
        "columns_fill_polars",
        """
        import polars as pl

        from fnloom import extract_columns

        @extract_columns("a", "zz", fill_with=0)
        def frame() -> pl.DataFrame:
            return pl.DataFrame({"a": [1, 2]})
        """,
    )
    driver = fnloom.build(module)
    assert driver.nodes["zz"].type is pl.Series
    result = _values(driver.execute(["a", "zz", "frame"]))
    assert result["a"].to_list() == [1, 2]
    assert result["zz"].name == "zz"
    assert result["zz"].to_list() == [0, 0]
    assert result["frame"].columns == ["a"]


def test_fill_with_on_a_mapping_is_the_value() -> None:
    """A mapping's missing column is the fill itself, `None` included."""
    module = make_module(
        "columns_fill_mapping",
        """
        from fnloom import extract_columns

        SHARED: dict[str, int] = {"a": 1}

        @extract_columns("a", "b", fill_with=None)
        def p() -> dict:
            return SHARED
        """,
    )
    assert fnloom.build(module).execute(["a", "b", "p"]) == {
        "a": 1,
        "b": None,
        "p": {"a": 1},
    }
    assert module.SHARED == {"a": 1}


def test_fill_with_on_another_frame_type_names_it() -> None:
    """A fill for a value that is no mapping or known frame is a `TypeError`."""
    module = make_module(
        "columns_fill_other",
        """
        from fnloom import extract_columns

        class Table:
            def __contains__(self, key: object) -> bool:
                return key == "a"

            def __getitem__(self, key: str) -> list[int]:
                return [1]

        @extract_columns("a", "zz", fill_with=0)
        def p() -> Table:
            return Table()
        """,
    )
    driver = fnloom.build(module)
    assert driver.execute(["a"]) == {"a": [1]}
    with pytest.raises(
        TypeError,
        match=(
            r"^'p' returned no column 'zz', and fill_with fills only a mapping, a "
            r"pandas DataFrame or a polars DataFrame, not columns_fill_other\.Table\n"
        ),
    ):
        driver.execute(["zz"])


def test_tuple_doc_form_sets_the_doc_and_fills_by_name() -> None:
    """`(name, doc)` names the node `name`, with that doc, and fills by `name`."""
    module = make_module(
        "columns_tuple",
        """
        from fnloom import extract_columns

        @extract_columns("a", ("b", "The b column."), fill_with=7)
        def p() -> dict:
            '''The producer.'''
            return {"a": 1}
        """,
    )
    assert decorations(module.p).extract == Extraction(
        "columns", (("a", None, None), ("b", None, "The b column.")), 7
    )
    driver = fnloom.build(module)
    assert driver.nodes["a"].doc == "The producer."
    assert driver.nodes["b"].doc == "The b column."
    assert driver.execute(["b", "p"]) == {"b": 7, "p": {"a": 1}}


@pytest.mark.usefixtures("pd")
def test_a_duplicated_pandas_column_fails_the_series_check() -> None:
    """A name the frame holds twice reads a DataFrame, which is no Series."""
    module = make_module(
        "columns_duplicated",
        """
        import pandas as pd

        from fnloom import extract_columns

        @extract_columns("a")
        def frame() -> pd.DataFrame:
            return pd.DataFrame([[1, 2]], columns=["a", "a"])
        """,
    )
    with pytest.raises(
        ValueError,
        match=(
            r"^'frame' returned column 'a' as pandas\.DataFrame, "
            r"but it is declared pandas\.Series\n"
        ),
    ):
        fnloom.build(module).execute(["a"])


def test_columns_compose_with_tag_config_and_one_row() -> None:
    """`@tag` lands everywhere, a variant extracts, one row names the producer."""
    module = make_module(
        "columns_compose",
        """
        from fnloom import config, extract_columns, parameterize, tag, value

        @tag(owner="z")
        @extract_columns("a")
        @config.when(mode="x")
        def p__x() -> dict:
            return {"a": 1}

        @extract_columns("b")
        @parameterize(q={"n": value(2)})
        def r(n: int) -> dict:
            return {"b": n}
        """,
    )
    driver = Builder().with_modules(module).with_config({"mode": "x"}).build()
    assert driver.node_names() == ["a", "b", "p", "q"]
    assert driver.nodes["p"].tags == {"owner": "z"}
    assert driver.nodes["a"].tags == {"owner": "z"}
    assert driver.execute(["a", "b"]) == {"a": 1, "b": 2}

    def rows(n: int) -> dict[str, int]:
        return {"a": n}

    two = parameterize(s={"n": value(1)}, t={"n": value(2)})(rows)
    with pytest.raises(
        ValueError, match=r"extract_columns needs one node, but parameterize makes 2"
    ):
        extract_columns("a")(two)

    def later(n: int) -> dict[str, int]:
        return {"a": n}

    with pytest.raises(ValueError, match=r"extract_columns needs one node"):
        parameterize(s={"n": value(1)}, t={"n": value(2)})(extract_columns("a")(later))


def test_a_column_named_like_its_producer_is_a_duplicate() -> None:
    """A column clashing with its producer fails the build, naming the column."""
    module = make_module(
        "columns_clash",
        """
        from fnloom import extract_columns

        @extract_columns("p")
        def p() -> dict:
            return {"p": 1}
        """,
    )
    with pytest.raises(ValueError, match="a column of function 'p'"):
        fnloom.build(module)


_COLUMNS_CACHED = """
{imports}
from fnloom import cache, extract_columns

CALLS: list[str] = []

@cache(version="1")
@extract_columns("a", fill_with={fill})
def frame(x: int) -> {returns}:
    CALLS.append("frame")
    return {{"b": [x]}}

@cache(version="1")
def doubled(a: object) -> object:
    CALLS.append("doubled")
    return [a, a]
"""


def test_a_changed_column_fill_is_not_served_stale(tmp_path: Path) -> None:
    """A rebuild with a new fill gives the new fill, through a stored consumer."""
    store = DiskCache(tmp_path / "cache")
    for fill in ("0", "1"):
        module = make_module(
            "columns_cached",
            _COLUMNS_CACHED.format(imports="", fill=fill, returns="dict"),
        )
        driver = Builder().with_modules(module).with_cache(store).build()
        assert driver.execute(["doubled"], inputs={"x": 1}) == {
            "doubled": [int(fill), int(fill)]
        }


@pytest.mark.usefixtures("pd")
def test_a_changed_column_type_is_not_served_stale(tmp_path: Path) -> None:
    """A producer now typed a DataFrame checks its column, though its key holds."""
    store = DiskCache(tmp_path / "cache")
    module = make_module(
        "columns_cached_type",
        _COLUMNS_CACHED.format(imports="", fill="0", returns="dict"),
    )
    driver = Builder().with_modules(module).with_cache(store).build()
    assert driver.execute(["doubled"], inputs={"x": 1}) == {"doubled": [0, 0]}
    # The pinned version keeps the producer's key: it hits, returning the dict.
    module = make_module(
        "columns_cached_type",
        _COLUMNS_CACHED.format(
            imports="import pandas as pd", fill="0", returns="pd.DataFrame"
        ),
    )
    driver = Builder().with_modules(module).with_cache(store).build()
    with pytest.raises(ValueError, match=r"'a' as int, but it is declared pandas"):
        driver.execute(["doubled"], inputs={"x": 1})
    assert module.CALLS == []


def test_fill_with_none_on_a_pandas_frame_stays_none(pd: ModuleType) -> None:
    """`fill_with=None` fills a pandas column with `None`, not NaN (Q62)."""
    module = make_module(
        "columns_fill_none_pandas",
        """
        import pandas as pd

        from fnloom import extract_columns

        @extract_columns("zz", fill_with=None)
        def frame() -> pd.DataFrame:
            return pd.DataFrame({"a": [1, 2]})
        """,
    )
    filled = _values(fnloom.build(module).execute(["zz"]))["zz"]
    assert isinstance(filled, pd.Series)
    assert filled.dtype == object
    assert filled.tolist() == [None, None]


_SWITCHED = """
from typing import Any

from fnloom import cache, extract_columns, extract_fields

CALLS: list[str] = []

@cache(version="1")
@{decorator}
def p(x: int) -> dict:
    CALLS.append("p")
    return {{"a": x}}

@cache(version="1")
def doubled(a: object) -> object:
    CALLS.append("doubled")
    return [a, a]
"""


def test_switching_the_extract_decorator_misses(tmp_path: Path) -> None:
    """A field and a column of one name, type and fill never share a key."""
    store = DiskCache(tmp_path / "cache")
    for decorator, expected in (
        ('extract_fields({"a": Any})', ["p", "doubled"]),
        ('extract_fields({"a": Any})', []),
        ('extract_columns("a")', ["doubled"]),
    ):
        module = make_module("columns_switched", _SWITCHED.format(decorator=decorator))
        driver = Builder().with_modules(module).with_cache(store).build()
        assert driver.execute(["doubled"], inputs={"x": 1}) == {"doubled": [1, 1]}
        ran = module.CALLS
        assert ran == expected, decorator
