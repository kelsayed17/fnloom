"""`@parameterize` sugar: row docs, docstring templates, `group`, shorthands (Q82)."""

import asyncio
import re
import sys
import types
from typing import TYPE_CHECKING, Any, override

import pytest

import fnloom
from fnloom import (
    MemoryCache,
    group,
    parameterize,
    parameterize_sources,
    parameterize_values,
    source,
    value,
)
from helpers import make_module

if TYPE_CHECKING:
    from types import FunctionType

    from fnloom import Driver


# Misuse the signatures forbid, passed on purpose.
loose_group: Any = group
loose_parameterize: Any = parameterize
loose_sources: Any = parameterize_sources
loose_values: Any = parameterize_values


def _docs(driver: Driver, *names: str) -> dict[str, str | None]:
    """The docs of the named nodes."""
    return {name: driver.nodes[name].doc for name in names}


def test_tuple_rows_keep_their_doc_verbatim() -> None:
    """A `(bindings, doc)` row's doc is used as written; None means no doc."""
    module = make_module(
        "sugar_tuple_rows",
        """
        from fnloom import parameterize, value

        @parameterize(
            literal=({"p": value(1)}, "kept {as} written {p} {output_name}"),
            silent=({"p": value(2)}, None),
            empty=({"p": value(3)}, ""),
            plain={"p": value(4)},
        )
        def f(p: int) -> int:
            \"\"\"Shows {output_name} with {p}.\"\"\"
            return p
        """,
    )
    driver = fnloom.build(module)
    assert _docs(driver, "literal", "silent", "empty", "plain") == {
        "literal": "kept {as} written {p} {output_name}",
        # `None` is no doc at all, not the function's.
        "silent": None,
        "empty": "",
        "plain": "Shows plain with 4.",
    }
    assert driver.execute(["literal", "silent"]) == {"literal": 1, "silent": 2}


def test_tuple_rows_are_checked_when_decorating() -> None:
    """A tuple of the wrong length, a doc that is no str, or no mapping: all listed."""
    with pytest.raises(TypeError) as raised:
        loose_parameterize(
            one=({"p": value(1)},),
            three=({"p": value(1)}, "doc", "extra"),
            number=({"p": value(1)}, 5),
            bare=(5, "doc"),
        )
    assert str(raised.value) == (
        "row 'bare': expected a mapping, got int; "
        "row 'number': doc must be a str or None, got int; "
        "row 'one': a (bindings, doc) tuple has 2 items, got 1; "
        "row 'three': a (bindings, doc) tuple has 2 items, got 3"
    )


def test_function_doc_is_templated_per_row() -> None:
    """The function's docstring is a template: output_name, sources, values, groups."""

    @parameterize(
        a={"p": source("left"), "q": value(2.5), "xs": group(source("x"), value(3))},
        b={"p": source("right"), "q": value(7), "xs": group(source("y"))},
    )
    def f(p: int, q: float, xs: list[int]) -> float:
        """{output_name} reads {p}, {q:.1f}, {q!r} and [{xs}]; {{braces}} stay."""
        return p + q + sum(xs)

    built = fnloom.build(_module_of(f))
    assert _docs(built, "a", "b") == {
        "a": "a reads left, 2.5, 2.5 and [x, 3]; {braces} stay.",
        "b": "b reads right, 7.0, 7 and [y]; {braces} stay.",
    }

    @parameterize(d={"xs": group(k=source("x"), j=value(1))})
    def g(xs: dict[str, int]) -> int:
        """Groups {xs}."""
        return len(xs)

    assert _docs(fnloom.build(_module_of(g)), "d") == {"d": "Groups k=x, j=1."}


def test_a_function_without_a_docstring_has_no_docs() -> None:
    """No docstring to template: every row's doc is None; a row's own doc applies."""

    @parameterize(a={"p": value(1)}, b=({"p": value(2)}, "own"))
    def f(p: int) -> int:
        return p

    driver = fnloom.build(_module_of(f))
    assert _docs(driver, "a", "b") == {"a": None, "b": "own"}


def test_unknown_placeholder_is_refused() -> None:
    """Every placeholder a row lacks, and every malformed template, in one message."""
    with pytest.raises(ValueError, match=r"placeholder \{ghost\}") as raised:

        @parameterize(a={"p": value(1)}, b={"p": value(2)})
        def f(p: int) -> int:
            """Row {output_name} has {p}, {ghost} and {other}."""
            return p

    lacks = "is not 'output_name' or a parameter the row binds"
    assert str(raised.value) == (
        "the docstring of 'test_unknown_placeholder_is_refused.<locals>.f' is a "
        "template for each row's doc: "
        f"row 'a': placeholder {{ghost}} {lacks}; "
        f"row 'a': placeholder {{other}} {lacks}; "
        f"row 'b': placeholder {{ghost}} {lacks}; "
        f"row 'b': placeholder {{other}} {lacks}; "
        "write a literal brace as {{ or }}, "
        "or give the row its own doc with (bindings, doc)"
    )

    def refused(doc: str) -> str:
        def f(p: int) -> int:
            return p

        f.__doc__ = doc
        with pytest.raises(ValueError, match="template") as error:
            parameterize(a={"p": value(1)})(f)
        return str(error.value)

    # A parameter the row does not bind is not a placeholder: `r` binds only `p`.
    assert "row 'a': placeholder {q} is not" in refused("Needs {q}.")
    assert "positional fields" in refused("Positional {} field.")
    assert "positional fields" in refused("Positional {0} field.")
    assert "Single '}'" in refused("A stray } brace.")
    assert "expected '}'" in refused("A stray { brace.")
    # A row that gave its own doc never reads the template.

    @parameterize(a=({"p": value(1)}, "own"))
    def ok(p: int) -> int:
        """Not a {template}."""
        return p

    assert _docs(fnloom.build(_module_of(ok)), "a") == {"a": "own"}


def _module_of(*fns: FunctionType) -> types.ModuleType:
    """A module whose public functions are `fns`, as if defined in it."""
    module = types.ModuleType("sugar_local")
    for fn in fns:
        fn.__module__ = module.__name__
        setattr(module, fn.__name__, fn)
    sys.modules[module.__name__] = module
    return module


_GROUPS = """
from fnloom import group, parameterize, source, value

SHARED = [10]


@parameterize(
    as_list={"xs": group(source("a"), value(3), source("b"))},
    as_one={"xs": group(source("a"))},
    shared={"xs": group(value(SHARED), source("a"))},
)
def collect(xs: list[int | list[int]]) -> list[object]:
    xs.append("mutated")
    return xs


@parameterize(
    as_dict={"ys": group(first=source("a"), fixed=value(3), second=source("b"))}
)
def named(ys: dict[str, int]) -> dict[str, int]:
    ys["extra"] = 0
    return ys
"""


def test_group_list_and_dict() -> None:
    """A group is the list or dict of its items, built fresh on every call."""
    module = make_module("sugar_groups", _GROUPS)
    driver = fnloom.build(module)
    inputs = {"a": 1, "b": 2}
    first = driver.execute(["as_list", "as_one", "as_dict", "shared"], inputs=inputs)
    assert first == {
        "as_list": [1, 3, 2, "mutated"],
        "as_one": [1, "mutated"],
        "as_dict": {"first": 1, "fixed": 3, "second": 2, "extra": 0},
        "shared": [[10], 1, "mutated"],
    }
    # The node mutated its list; the next call sees a fresh one, and the same `value()`.
    again = driver.execute(["as_list", "as_dict", "shared"], inputs={"a": 5, "b": 6})
    assert again["as_list"] == [5, 3, 6, "mutated"]
    assert again["as_dict"] == {"first": 5, "fixed": 3, "second": 6, "extra": 0}
    shared = again["shared"]
    assert isinstance(shared, list)
    assert shared[0] is module.SHARED
    # Each source is an ordinary input, typed by the element: `int`, not `list[int]`.
    assert driver.nodes["a"].type is int
    assert driver.nodes["b"].type is int
    assert sorted(driver.inputs()) == ["a", "b"]
    assert driver.nodes["as_list"].dependencies == ("a", "b")
    # A wrong-typed input is refused as an `int`.
    with pytest.raises(ValueError, match=r"^a: expected int, got str$"):
        driver.execute(["as_list"], inputs={"a": "x", "b": 2})
    with pytest.raises(ValueError, match="missing inputs: 'b'"):
        driver.execute(["as_list"], inputs={"a": 1})
    # The params say where each item goes.
    params = driver.nodes["as_dict"].params
    assert [(p.name, p.slot, p.annotation) for p in params] == [
        ("ys", "first", int),
        ("ys", "fixed", int),
        ("ys", "second", int),
    ]


def test_group_feeds_from_nodes_and_checks_the_element_type() -> None:
    """A group's source may be a node, and is checked against the element type."""
    good = make_module(
        "sugar_group_node",
        """
        from fnloom import group, parameterize, source

        def base(a: int) -> int:
            return a * 2

        @parameterize(total={"xs": group(source("base"), source("a"))})
        def add(xs: list[int]) -> int:
            return sum(xs)
        """,
    )
    assert fnloom.build(good).execute(["total"], inputs={"a": 4}) == {"total": 12}
    bad = make_module(
        "sugar_group_node_bad",
        """
        from fnloom import group, parameterize, source

        def base(a: int) -> str:
            return str(a)

        @parameterize(total={"xs": group(source("base"))})
        def add(xs: list[int]) -> int:
            return sum(xs)
        """,
    )
    with pytest.raises(
        ValueError, match=r"total\.xs\[0\] requires int but base returns str"
    ):
        fnloom.build(bad)


def test_group_accepts_optional_and_ignores_a_default() -> None:
    """`Optional` of the group type is fine; every item is still required."""
    module = make_module(
        "sugar_group_optional",
        """
        from typing import Optional
        from fnloom import group, parameterize, source

        @parameterize(a={"xs": group(source("p"), source("q"))})
        def f(xs: Optional[list[int]] = None) -> int:
            return sum(xs or [])

        @parameterize(b={"ys": group(k=source("p"))})
        def g(ys: dict[str, int] | None = None) -> int:
            return sum((ys or {}).values())
        """,
    )
    driver = fnloom.build(module)
    assert driver.execute(["a", "b"], inputs={"p": 1, "q": 2}) == {"a": 3, "b": 1}
    with pytest.raises(ValueError, match="missing inputs: 'q'"):
        driver.execute(["a"], inputs={"p": 1})
    assert driver.inputs()["q"].required


def test_group_misuse_is_refused_once_per_offender() -> None:
    """Each kind of group misuse is refused when decorating, each offender once."""
    with pytest.raises(ValueError, match="needs at least one"):
        group()
    with pytest.raises(ValueError, match="not both"):
        group(source("a"), k=source("b"))
    with pytest.raises(TypeError) as nested:
        loose_group(group(source("a")), "b", k=5)
    assert str(nested.value) == (
        "group() takes source(...) or value(...), not: item 0 (Group), item 1 (str), "
        "item 'k' (int)"
    )

    def wrong() -> None:
        @parameterize(
            one={"bare": group(source("a")), "ok": group(source("a"))},
            two={"bare": group(source("b")), "keyed": group(k=source("a"))},
            three={"seq": group(source("a")), "plain": group(source("c"))},
            four={"quoted": group(source("a")), "missing": group(source("d"))},
        )
        def f(  # noqa: PLR0913  # one parameter per kind of annotation
            *,
            bare: list,  # ty: ignore[missing-type-argument]  # a bare list is the misuse
            ok: list[int],
            keyed: dict[int, int],
            seq: tuple[int, ...],
            plain: int,
            quoted: "list[int]",  # noqa: UP037  # a quoted annotation is the misuse
            missing,  # noqa: ANN001  # no annotation is the misuse
        ) -> int:
            return len([bare, ok, keyed, seq, plain, quoted, missing])

    with pytest.raises(TypeError) as raised:
        wrong()
    assert str(raised.value) == (
        "group() parameters must be annotated as their group: "
        "'bare' (list) needs list[X] for its group; "
        "'keyed' (dict[int, int]) needs dict[str, X] for its group; "
        "'missing' (no annotation) needs list[X] for its group; "
        "'plain' (int) needs list[X] for its group; "
        "'quoted' ('list[int]') needs list[X] for its group, written unquoted; "
        "'seq' (tuple[int, ...]) needs list[X] for its group"
    )

    def kinds() -> None:
        @parameterize(one={"xs": group(source("a"))}, two={"xs": group(k=source("a"))})
        def f(xs: list[int]) -> int:
            return len(xs)

    with pytest.raises(
        TypeError, match=re.escape("'xs' (list[int]) needs dict[str, X]")
    ):
        kinds()
    # An unlisted parameter is the usual error, before any annotation is read.
    with pytest.raises(ValueError, match="does not have the parameters"):

        @parameterize(a={"nope": group(source("a"))})
        def g(xs: list[int]) -> int:
            return len(xs)


def test_group_in_node_binding() -> None:
    """`fnloom.node(bind=)` takes a group, checked as a row is."""

    def total(xs: list[int], scale: int) -> int:
        return sum(xs) * scale

    spec = fnloom.node(
        total, name="t", bind={"xs": group(source("a"), value(5)), "scale": value(2)}
    )
    driver = fnloom.Builder().with_functions(spec).build()
    assert driver.execute(["t"], inputs={"a": 1}) == {"t": 12}

    def untyped(xs: int) -> int:
        return xs

    with pytest.raises(TypeError, match=r"'xs' \(int\) needs list\[X\]"):
        fnloom.node(untyped, bind={"xs": group(source("a"))})


def test_parameterize_sources_and_values() -> None:
    """The shorthands build the same nodes `parameterize` does."""
    module = make_module(
        "sugar_shorthands",
        """
        from fnloom import parameterize_sources, parameterize_values

        @parameterize_sources(low=dict(p="a"), high=dict(p="b"))
        def pick(p: int, q: int = 5) -> int:
            \"\"\"{output_name} reads {p}.\"\"\"
            return p + q

        @parameterize_values("p", {("one", "the first"): 1, ("two", None): 2})
        def fixed(p: int) -> int:
            \"\"\"Never shown: {nothing}.\"\"\"
            return p * 10
        """,
    )
    driver = fnloom.build(module)
    assert driver.execute(["low", "high", "one", "two"], inputs={"a": 1, "b": 2}) == {
        "low": 6,
        "high": 7,
        "one": 10,
        "two": 20,
    }
    assert _docs(driver, "low", "high", "one", "two") == {
        "low": "low reads a.",
        "high": "high reads b.",
        "one": "the first",
        "two": None,
    }
    assert driver.execute(["low"], inputs={"a": 1, "b": 2, "q": 10}) == {"low": 11}


def test_parameterize_sources_refusals() -> None:
    """A non-str source, an empty row, and no rows: refused, each offender listed."""
    with pytest.raises(ValueError, match="needs at least one row"):
        parameterize_sources()
    with pytest.raises(TypeError) as raised:
        loose_sources(
            a={"p": 5, "q": source("x"), "r": "ok"}, b={"p": value(1)}, c=("p", "x")
        )
    assert str(raised.value) == (
        "row 'a' parameter 'p': a source is a str, got int; "
        "row 'a' parameter 'q': a source is a str, got Source; "
        "row 'b' parameter 'p': a source is a str, got Value; "
        "row 'c': expected a mapping of parameter to source name, got tuple"
    )
    with pytest.raises(ValueError, match="row 'empty': has no parameter to bind"):
        parameterize_sources(empty={})
    with pytest.raises(ValueError, match="does not have the parameters"):

        @parameterize_sources(a={"zz": "x"})
        def f(p: int) -> int:
            return p


def test_parameterize_values_refusals() -> None:
    """An empty mapping, a key that is no `(name, doc)`, a repeated name: refused."""
    with pytest.raises(ValueError, match="assigned_output is empty"):
        parameterize_values("p", {})
    with pytest.raises(TypeError) as raised:
        loose_values(
            "p",
            {
                "bare": 1,
                ("too", "many", "items"): 2,
                (1, "number"): 3,
                ("ok", 5): 4,
                ("fine", "doc"): 5,
            },
        )
    assert str(raised.value) == (
        "key 'bare' must be a (name, doc) tuple; "
        "key ('ok', 5) must be (str, str | None); "
        "key ('too', 'many', 'items') must be a (name, doc) tuple; "
        "key (1, 'number') must be (str, str | None)"
    )
    with pytest.raises(TypeError, match="assigned_output must be a mapping"):
        loose_values("p", [("a", 1)])
    with pytest.raises(ValueError, match="more than once: 'a', 'b'"):
        parameterize_values(
            "p", {("a", "x"): 1, ("a", "y"): 2, ("b", None): 3, ("b", "z"): 4}
        )
    with pytest.raises(TypeError, match="parameter must be a str"):
        loose_values(5, {("a", None): 1})


def test_the_sugar_composes_with_the_other_decorators() -> None:
    """Config, extract (one row), tags, cache, defaults and namespaces all apply."""
    module = make_module(
        "sugar_compose",
        """
        from fnloom import (
            cache, config, defaults, extract_fields, group, parameterize,
            parameterize_sources, parameterize_values, source, tag, value,
        )

        CALLS = []

        @config.when(mode="sum")
        @tag(kind="grouped")
        @cache()
        @parameterize(total=({"xs": group(source("a"), value(1))}, "A sum."))
        def agg__sum(xs: list[int], scale: int = 2) -> int:
            \"\"\"{output_name} of {xs}.\"\"\"
            CALLS.append(xs)
            return sum(xs) * scale

        @config.default
        @parameterize_values("v", {("total", "The default."): 0})
        def agg__zero(v: int) -> int:
            return v

        @extract_fields({"lo": int, "hi": int})
        @parameterize(span={"xs": group(source("a"), source("b"))})
        def pair(xs: list[int]) -> dict[str, int]:
            \"\"\"Spans {xs}.\"\"\"
            return {"lo": min(xs), "hi": max(xs)}

        @tag(owner="me")
        @parameterize_sources(shifted=dict(n="a"))
        @defaults(shadowed=())
        def shift(n: int, by: int = 1) -> int:
            return n + by
        """,
    )
    driver = fnloom.Builder().with_modules(module).with_config({"mode": "sum"}).build()
    assert driver.execute(
        ["total", "lo", "hi", "shifted"], inputs={"a": 3, "b": 9}
    ) == {
        "total": 8,
        "lo": 3,
        "hi": 9,
        "shifted": 4,
    }
    assert _docs(driver, "total", "span", "shifted") == {
        "total": "A sum.",
        "span": "Spans a, b.",
        "shifted": None,
    }
    assert driver.nodes["total"].tags == {"kind": "grouped"}
    assert driver.nodes["shifted"].tags == {"owner": "me"}
    # The other variant is a `parameterize_values` row, with its own doc.
    other = fnloom.Builder().with_modules(module).with_config({"mode": "none"}).build()
    assert other.execute(["total"]) == {"total": 0}
    assert _docs(other, "total") == {"total": "The default."}
    # Rows are checked as ever: two rows of one extract make a clear error.
    with pytest.raises(ValueError, match="needs one node"):

        @fnloom.extract_fields({"lo": int})
        @parameterize_values("v", {("a", None): 1, ("b", None): 2})
        def two(v: int) -> dict[str, int]:
            return {"lo": v}


def test_a_grouped_node_is_cached_by_its_group() -> None:
    """The cache key sees each item: a changed source or value recomputes."""
    module = make_module(
        "sugar_cached",
        """
        from fnloom import cache, group, parameterize, source, value

        CALLS = []

        @cache(version="1")
        @parameterize(
            one={"xs": group(source("a"), value(1))},
            two={"xs": group(source("a"), value(2))},
        )
        def agg(xs: list[int]) -> int:
            CALLS.append(tuple(xs))
            return sum(xs)
        """,
    )
    store = MemoryCache()
    driver = fnloom.Builder().with_cache(store).with_modules(module).build()
    assert driver.execute(["one", "two"], inputs={"a": 1}) == {"one": 2, "two": 3}
    assert driver.execute(["one", "two"], inputs={"a": 1}) == {"one": 2, "two": 3}
    assert module.CALLS == [(1, 1), (1, 2)]
    assert driver.execute(["one"], inputs={"a": 5}) == {"one": 6}
    assert module.CALLS == [(1, 1), (1, 2), (5, 1)]


def test_sugar_works_under_a_namespace() -> None:
    """A group's sources read the namespace's nodes, like any parameter."""
    module = make_module(
        "sugar_namespaced",
        """
        from fnloom import group, parameterize, source

        def base(a: int) -> int:
            return a * 10

        @parameterize(total={"xs": group(source("base"), source("a"))})
        def add(xs: list[int]) -> int:
            return sum(xs)
        """,
    )
    driver = fnloom.Builder().with_modules(module, namespace="ns").build()
    assert driver.node_names() == ["a", "ns.base", "ns.total"]
    assert driver.execute(["ns.total"], inputs={"a": 1}) == {"ns.total": 11}
    assert driver.nodes["ns.total"].dependencies == ("a", "ns.base")


def test_a_group_runs_on_the_async_driver() -> None:
    """The async driver assembles a group as the sync one does."""
    module = make_module(
        "sugar_async",
        """
        from fnloom import group, parameterize, source

        @parameterize(total={"xs": group(k=source("a"), j=source("b"))})
        def add(xs: dict[str, int]) -> int:
            return xs["k"] * 10 + xs["j"]
        """,
    )
    driver = fnloom.Builder().with_modules(module).build_async()
    assert asyncio.run(driver.execute(["total"], inputs={"a": 4, "b": 2})) == {
        "total": 42
    }


_SLOT_TOTAL = """
from fnloom import cache, group, parameterize, source, value

@cache(version="1")
@parameterize(r={{"xs": {group}}})
def total(xs: dict[str, int]) -> int:
    return xs.get("k", 0) * 100 + xs.get("j", 0)
"""
_FIVE = 5


def test_group_cache_keys_include_the_slot() -> None:
    """Renaming a dict key, or moving an item between slots, misses the cache."""
    store = MemoryCache()

    def run(group_source: str) -> object:
        module = make_module("slots_a", _SLOT_TOTAL.format(group=group_source))
        driver = fnloom.Builder().with_cache(store).with_modules(module).build()
        return driver.execute(["r"], inputs={"a": _FIVE})["r"]

    # The key renamed: same function and input, a different group.
    assert run('group(k=source("a"))') == _FIVE * 100
    assert run('group(j=source("a"))') == _FIVE
    # Values moved between keys: the function sees different dicts.
    module = make_module(
        "slots_b",
        """
        from fnloom import cache, group, parameterize, value

        @cache(version="1")
        @parameterize(
            first={"xs": group(k=value(1), j=value(2))},
            second={"xs": group(j=value(1), k=value(2))},
        )
        def total(xs: dict[str, int]) -> int:
            return xs["k"] * 100 + xs["j"]
        """,
    )
    driver = fnloom.Builder().with_cache(store).with_modules(module).build()
    assert driver.execute(["first", "second"]) == {"first": 102, "second": 201}


def test_group_slot_changes_the_key_across_builds() -> None:
    """Two builds of one function with different groups never share an entry."""
    store = MemoryCache()
    results = []
    for group_source in ['group(k=source("a"))', 'group(j=source("a"))']:
        module = make_module(
            "slots_c",
            f"""
            from fnloom import cache, group, parameterize, source

            @cache(version="1")
            @parameterize(r={{"xs": {group_source}}})
            def total(xs: dict[str, int]) -> int:
                return 100 if "k" in xs else 5
            """,
        )
        # Same module and function name in the key: only the group differs.
        driver = fnloom.Builder().with_cache(store).with_modules(module).build()
        results.append(driver.execute(["r"], inputs={"a": 5})["r"])
    assert results == [100, _FIVE]


def test_moving_a_source_between_a_parameter_and_a_group_misses() -> None:
    """`p=source("a")` and `xs=group(source("a"))` are different keys."""
    store = MemoryCache()
    results = []
    for index, binding in enumerate(
        ['{"p": source("a")}', '{"p": group(source("a"))}']
    ):
        annotation = "int" if index == 0 else "list[int]"
        module = make_module(
            "slots_d",
            f"""
            from fnloom import cache, group, parameterize, source

            @cache(version="1")
            @parameterize(r={binding})
            def total(p: {annotation}) -> int:
                return 1 if isinstance(p, int) else 2
            """,
        )
        driver = fnloom.Builder().with_cache(store).with_modules(module).build()
        results.append(driver.execute(["r"], inputs={"a": 5})["r"])
    assert results == [1, 2]


_UNRESOLVED = """
from typing import TYPE_CHECKING
from fnloom import group, parameterize, source

if TYPE_CHECKING:
    from decimal import Decimal


@parameterize(as_list={"xs": group(source("a"), source("b"))})
def listed(xs: list[Decimal]) -> int:
    return len(xs)


@parameterize(as_dict={"ys": group(k=source("c"))})
def keyed(ys: dict[str, Decimal]) -> int:
    return len(ys)


def other(a: Decimal, c: Decimal) -> int:
    return 0
"""


def test_an_unresolved_group_element_is_the_element_not_the_whole() -> None:
    """`list[Decimal]` with `Decimal` unresolved types each source `Decimal` (Q81)."""
    from fnloom._types import Unresolved  # noqa: PLC0415  # only this test needs it

    driver = fnloom.build(make_module("sugar_unresolved", _UNRESOLVED))
    for name in ("a", "b", "c"):
        assert driver.inputs()[name].type == Unresolved("Decimal"), name
    with pytest.raises(TypeError, match=r"^a: .*import what it names at run time"):
        driver.execute(["as_list"], inputs={"a": 1, "b": 2})


def test_group_element_conflicts_are_real_conflicts() -> None:
    """An unresolved element against a different consumer type is still refused."""
    module = make_module(
        "sugar_unresolved_conflict",
        _UNRESOLVED + "\n\ndef third(a: int) -> int:\n    return a\n",
    )
    with pytest.raises(ValueError, match="conflicting types"):
        fnloom.build(module)


def test_parameterize_values_takes_a_value_unchanged() -> None:
    """A `value(3)` given as the value is that constant, not a `Value` in a `Value`."""

    @parameterize_values("p", {("n", "d"): value(3), ("m", None): 4})
    def f(p: int) -> int:
        return p

    driver = fnloom.build(_module_of(f))
    assert driver.execute(["n", "m"]) == {"n": 3, "m": 4}


def test_any_failure_while_templating_names_the_row() -> None:
    """A value whose `__format__` raises anything is reported with its row."""

    class Broken:
        @override
        def __format__(self, spec: str) -> str:
            msg = f"cannot format {spec!r}"
            raise RuntimeError(msg)

    def f(p: object) -> int:
        """Shows {p}."""
        return hash(p)

    with pytest.raises(ValueError, match="row 'a': cannot format ''") as raised:
        parameterize(a={"p": value(Broken())})(f)
    assert "template" in str(raised.value)


def test_parameterize_sources_reports_mixed_key_types_clearly() -> None:
    """Parameter names of mixed types are listed, not a raw error from sorting."""
    with pytest.raises(TypeError) as raised:
        loose_sources(a={1: "x", "p": "y", 2.5: "z"})
    assert str(raised.value) == (
        "row 'a': parameter name 1 is not a str; "
        "row 'a': parameter name 2.5 is not a str"
    )
