"""Namespaces: one module's nodes under a dotted prefix (Q74)."""

import asyncio
import copy
import pickle
import re

import pytest

import fnloom
from helpers import make_module

_PRICING = """
def base(x: int) -> int:
    return x + 1


def total(base: int) -> int:
    return base * 10
"""


def test_a_namespace_prefixes_every_node() -> None:
    """Each node is `ns.<name>`, its view says so, and it reads its own neighbours."""
    module = make_module("ns_prefix", _PRICING)
    driver = fnloom.Builder().with_modules(module, namespace="eu").build()
    assert driver.node_names() == ["eu.base", "eu.total", "x"]
    assert driver.nodes["eu.total"].namespace == "eu"
    assert driver.nodes["eu.total"].dependencies == ("eu.base",)
    # An input is never namespaced.
    assert driver.nodes["x"].namespace is None
    assert driver.execute(["eu.total"], inputs={"x": 1}) == {"eu.total": 20}
    # A dotted path works the same, and a module given plainly has no namespace.
    nested = fnloom.Builder().with_modules(module, namespace="a.b").build()
    assert nested.node_names() == ["a.b.base", "a.b.total", "x"]
    assert fnloom.build(module).nodes["total"].namespace is None


def test_parameters_resolve_inside_then_outside() -> None:
    """A parameter reads its namespace's node, else the outer node or an input."""
    inner = make_module(
        "ns_resolve_inner",
        """
        def rate(x: int) -> int:
            return x * 2


        def charge(rate: int, fee: int, y: int) -> int:
            return rate + fee + y
        """,
    )
    outer = make_module(
        "ns_resolve_outer",
        """
        def fee() -> int:
            return 100
        """,
    )
    driver = (
        fnloom.Builder().with_modules(outer).with_modules(inner, namespace="ns").build()
    )
    # `rate` is the namespace's own; `fee` an outer node; `y` and `x` inputs.
    assert driver.nodes["ns.charge"].dependencies == ("fee", "ns.rate", "y")
    assert driver.nodes["ns.rate"].dependencies == ("x",)
    assert sorted(driver.inputs()) == ["x", "y"]
    assert driver.execute(["ns.charge"], inputs={"x": 1, "y": 3}) == {"ns.charge": 105}
    # The same holds on the async driver.
    built = (
        fnloom.Builder()
        .with_modules(outer)
        .with_modules(inner, namespace="ns")
        .build_async()
    )
    assert asyncio.run(built.execute(["ns.charge"], inputs={"x": 1, "y": 3})) == {
        "ns.charge": 105
    }


def test_namespaces_inside_a_namespace_cover_every_module_given_under_it() -> None:
    """Two modules under one namespace see each other's nodes, in any call."""
    first = make_module(
        "ns_cover_first",
        """
        def rate(x: int) -> int:
            return x * 2
        """,
    )
    second = make_module(
        "ns_cover_second",
        """
        def charge(rate: int) -> int:
            return rate + 1
        """,
    )
    together = fnloom.Builder().with_modules(first, second, namespace="ns").build()
    apart = (
        fnloom.Builder()
        .with_modules(second, namespace="ns")
        .with_modules(first, namespace="ns")
        .build()
    )
    for driver in (together, apart):
        assert driver.nodes["ns.charge"].dependencies == ("ns.rate",)
        assert driver.execute(["ns.charge"], inputs={"x": 2}) == {"ns.charge": 5}


def test_one_module_under_two_namespaces() -> None:
    """Each namespace gets its own nodes from the same functions."""
    module = make_module("ns_reuse", _PRICING)
    driver = (
        fnloom.Builder()
        .with_modules(module, namespace="eu")
        .with_modules(module, namespace="us")
        .build()
    )
    assert driver.node_names() == ["eu.base", "eu.total", "us.base", "us.total", "x"]
    assert driver.nodes["eu.total"].origin is driver.nodes["us.total"].origin
    assert driver.nodes["us.total"].dependencies == ("us.base",)
    assert driver.execute(["eu.total", "us.total"], inputs={"x": 1}) == {
        "eu.total": 20,
        "us.total": 20,
    }
    # The module is listed once.
    assert driver.modules == (module,)
    # One function now makes two nodes, so `final_vars` refuses it, naming both.
    with pytest.raises(
        TypeError,
        match=re.escape(
            "function ns_reuse.total makes 2 nodes: pass 'eu.total', 'us.total' instead"
        ),
    ):
        driver.execute([module.total], inputs={"x": 1})
    # Given plainly as well, it makes three: still refused, never the plain one.
    plain_too = (
        fnloom.Builder()
        .with_modules(module)
        .with_modules(module, namespace="eu")
        .build()
    )
    with pytest.raises(
        TypeError, match=re.escape("makes 2 nodes: pass 'eu.total', 'total'")
    ):
        plain_too.execute([module.total], inputs={"x": 1})
    # The same module under the same namespace twice counts once.
    again = (
        fnloom.Builder()
        .with_modules(module, namespace="eu")
        .with_modules(module, module, namespace="eu")
        .build()
    )
    assert again.node_names() == ["eu.base", "eu.total", "x"]


def test_identity_dedup_holds_within_one_namespace_only() -> None:
    """A function given to `with_functions` is not merged with a namespaced copy."""
    module = make_module("ns_dedup", _PRICING)
    driver = (
        fnloom.Builder()
        .with_modules(module, namespace="eu")
        .with_functions(module.base)
        .build()
    )
    assert driver.node_names() == ["base", "eu.base", "eu.total", "x"]
    assert driver.execute(["base", "eu.total"], inputs={"x": 1}) == {
        "base": 2,
        "eu.total": 20,
    }


def test_a_namespace_cannot_read_another() -> None:
    """A parameter named like another namespace's node reads the plain name."""
    producer = make_module(
        "ns_other_producer",
        """
        def rate(x: int) -> int:
            return x * 2
        """,
    )
    reader = make_module(
        "ns_other_reader",
        """
        def charge(rate: int) -> int:
            return rate + 1
        """,
    )
    driver = (
        fnloom.Builder()
        .with_modules(producer, namespace="a")
        .with_modules(reader, namespace="b")
        .build()
    )
    assert driver.nodes["b.charge"].dependencies == ("rate",)
    assert sorted(driver.inputs()) == ["rate", "x"]
    with pytest.raises(ValueError, match=re.escape("missing inputs: 'rate'")):
        driver.execute(["b.charge"])
    assert driver.execute(["b.charge"], inputs={"rate": 7}) == {"b.charge": 8}


def test_inner_name_beats_outer() -> None:
    """Review Focus 1: the namespace's node shadows an outer one, inside only."""
    outer = make_module(
        "ns_shadow_outer",
        """
        def rate() -> int:
            return 1


        def outer_charge(rate: int) -> int:
            return rate * 100
        """,
    )
    inner = make_module(
        "ns_shadow_inner",
        """
        def rate() -> int:
            return 2


        def charge(rate: int) -> int:
            return rate * 10
        """,
    )
    driver = (
        fnloom.Builder().with_modules(outer).with_modules(inner, namespace="ns").build()
    )
    assert driver.nodes["ns.charge"].dependencies == ("ns.rate",)
    assert driver.nodes["outer_charge"].dependencies == ("rate",)
    assert driver.execute(["ns.charge", "rate", "outer_charge", "ns.rate"]) == {
        "ns.charge": 20,
        "rate": 1,
        "outer_charge": 100,
        "ns.rate": 2,
    }
    assert driver.what_is_the_path_between("ns.rate", "ns.charge") == [
        "ns.rate",
        "ns.charge",
    ]
    # The outer node is not on the namespace's path.
    assert driver.what_is_the_path_between("rate", "ns.charge") == []


_DECORATED = """
import fnloom
from typing import TypedDict


class Split(TypedDict):
    left: int
    right: int


@fnloom.config.when(region="eu")
def price__eu(x: int) -> int:
    return x + 1


@fnloom.config.when(region="us")
def price__us(x: int) -> int:
    return x + 2


@fnloom.parameterize(
    low={"limit": fnloom.value(1)},
    high={"limit": fnloom.source("price")},
)
def above(x: int, limit: int) -> bool:
    return x > limit


@fnloom.extract_fields({"left": int, "right": int})
def halves(x: int) -> Split:
    return {"left": x // 2, "right": x - x // 2}


def joined(left: int, right: int, low: bool) -> str:
    return f"{left}/{right}/{low}"
"""


def test_namespaced_config_extract_and_parameterize() -> None:
    """Variants, rows and fields are all prefixed, and read their namespace."""
    module = make_module("ns_decorated", _DECORATED)
    driver = (
        fnloom.Builder()
        .with_modules(module, namespace="ns")
        .with_config({"region": "eu"})
        .build()
    )
    assert driver.node_names() == [
        "ns.halves",
        "ns.high",
        "ns.joined",
        "ns.left",
        "ns.low",
        "ns.price",
        "ns.right",
        "x",
    ]
    assert "ns.above" not in driver.nodes
    assert driver.nodes["ns.high"].dependencies == ("ns.price", "x")
    assert driver.nodes["ns.left"].dependencies == ("ns.halves",)
    assert driver.nodes["ns.joined"].dependencies == ("ns.left", "ns.low", "ns.right")
    assert driver.nodes["ns.price"].config != ()
    assert driver.execute(["ns.joined", "ns.high"], inputs={"x": 5}) == {
        "ns.joined": "2/3/True",
        "ns.high": False,
    }
    # No variant matches: the reason names the namespaced node.
    with pytest.raises(
        ValueError,
        match=re.escape("no variant of 'ns.price' matches the config {'region': 'jp'}"),
    ):
        (
            fnloom.Builder()
            .with_modules(module, namespace="ns")
            .with_config({"region": "jp"})
            .build()
        )
    # Under two namespaces, each selects its own variant, independently.
    two = (
        fnloom.Builder()
        .with_modules(module, namespace="a")
        .with_modules(module, namespace="b")
        .with_config({"region": "us"})
        .build()
    )
    assert two.execute(["a.price", "b.high"], inputs={"x": 1}) == {
        "a.price": 3,
        "b.high": False,
    }


def test_namespaced_unmatched_variant_read_by_a_node() -> None:
    """A node no variant gave, read inside its namespace, names the namespaced node."""
    module = make_module(
        "ns_unmatched",
        """
        import fnloom


        @fnloom.config.when(region="eu")
        def price__eu(x: int) -> int:
            return x


        def total(price: int) -> int:
            return price
        """,
    )
    with pytest.raises(ValueError, match=re.escape("no variant of 'ns.price'")):
        (
            fnloom.Builder()
            .with_modules(module, namespace="ns")
            .with_config({"region": "us"})
            .build()
        )
    # An outer node of that name does not stand in for it.
    outer = make_module("ns_unmatched_outer", "def price() -> int:\n    return 1\n")
    with pytest.raises(ValueError, match=re.escape("no variant of 'ns.price'")):
        (
            fnloom.Builder()
            .with_modules(outer)
            .with_modules(module, namespace="ns")
            .with_config({"region": "us"})
            .build()
        )
    # Requested as an output, it is the driver's unmatched error, namespaced.
    lone = make_module(
        "ns_unmatched_lone",
        """
        import fnloom


        @fnloom.config.when(region="eu")
        def price__eu(x: int) -> int:
            return x


        def other(x: int) -> int:
            return x
        """,
    )
    driver = (
        fnloom.Builder()
        .with_modules(lone, namespace="ns")
        .with_config({"region": "us"})
        .build()
    )
    with pytest.raises(ValueError, match=re.escape("no variant of 'ns.price'")):
        driver.execute(["ns.price"], inputs={"x": 1})
    assert driver.validate(["ns.price"], inputs={"x": 1}) == [
        (
            "no variant of 'ns.price' matches the config {'region': 'us'}; its "
            "variants need region == 'eu' (price__eu); add @config.default or a "
            "variant for it"
        )
    ]


def test_defaults_resolve_per_namespace() -> None:
    """A shadowed default names the namespaced node; differing defaults compare."""
    module = make_module(
        "ns_defaults",
        """
        def fee() -> int:
            return 1


        def total(fee: int = 5) -> int:
            return fee
        """,
    )
    with pytest.raises(
        ValueError,
        match=re.escape(
            "shadowed default: parameter 'fee' of 'ns.total' has a default, but it "
            "reads the node 'ns.fee'"
        ),
    ):
        fnloom.Builder().with_modules(module, namespace="ns").build()
    # Outside the namespace, a node named `fee` does not shadow a reader inside.
    reader = make_module(
        "ns_defaults_reader",
        """
        def total(fee: int = 5) -> int:
            return fee
        """,
    )
    outer = make_module(
        "ns_defaults_outer",
        """
        def other(fee: int = 7) -> int:
            return fee
        """,
    )
    with pytest.raises(
        ValueError,
        match=re.escape(
            "differing defaults: input 'fee' has different defaults in "
            "'ns.total' (parameter 'fee') = 5, 'other' (parameter 'fee') = 7"
        ),
    ):
        fnloom.Builder().with_modules(outer).with_modules(
            reader, namespace="ns"
        ).build()


def test_left_out_hints_name_the_namespace() -> None:
    """A missing input named like a function the namespace left out says so."""
    module = make_module(
        "ns_left_out",
        """
        import fnloom


        @fnloom.exclude
        def rate(x: int) -> int:
            return x


        def charge(rate: int) -> int:
            return rate
        """,
    )
    driver = fnloom.Builder().with_modules(module, namespace="ns").build()
    lines = driver.validate(["ns.charge"])
    assert lines == [
        "'rate' (node 'ns.rate') is a function in module 'ns_left_out' marked @exclude",
        "missing inputs: 'rate' (needed by 'ns.charge')",
    ]
    with pytest.raises(ValueError, match=re.escape("(node 'ns.rate') is a function")):
        driver.execute(["ns.charge"])


def test_inspection_validate_and_suggestions_use_full_names() -> None:
    """`inputs`, `outputs`, the path and suggestions all speak full names."""
    module = make_module("ns_inspect", _PRICING)
    driver = fnloom.Builder().with_modules(module, namespace="eu").build()
    assert driver.outputs() == ["eu.total"]
    assert list(driver.inputs()) == ["x"]
    assert driver.inputs()["x"].consumers == ("eu.base",)
    assert driver.what_is_the_path_between("x", "eu.total") == [
        "x",
        "eu.base",
        "eu.total",
    ]
    assert driver.validate(["eu.totl"], inputs={"x": 1}) == [
        "unknown outputs: 'eu.totl' (did you mean 'eu.total'?)"
    ]
    assert driver.validate(["eu.total"]) == [
        "missing inputs: 'x' (needed by 'eu.base')"
    ]


def test_bad_namespace_is_refused() -> None:
    """A namespace is a non-empty dotted identifier path; anything else is refused."""
    module = make_module("ns_bad", _PRICING)
    for bad in ("", "a.", ".a", "a..b", "1a", "a-b", "a b", "a.1"):
        with pytest.raises(
            ValueError,
            match=re.escape(
                f"namespace {bad!r} is not a dotted identifier path, such as 'a' or "
                "'a.b'"
            ),
        ):
            fnloom.Builder().with_modules(module, namespace=bad)
    with pytest.raises(TypeError, match="namespace must be a str or None, got int"):
        fnloom.Builder().with_modules(module, namespace=3)  # ty: ignore[invalid-argument-type]  # refused on purpose


def test_a_dotted_row_name_clashes_with_a_namespaced_node() -> None:
    """An un-namespaced row named `ns.x` is a clash naming both sources."""
    rows = make_module(
        "ns_dotted_rows",
        """
        import fnloom


        @fnloom.parameterize(**{"ns.base": {"y": fnloom.value(1)}})
        def made(y: int) -> int:
            return y
        """,
    )
    module = make_module("ns_dotted_module", _PRICING)
    with pytest.raises(
        ValueError,
        match=re.escape(
            "node 'ns.base' is defined twice: by a row of function 'made' in module "
            "'ns_dotted_rows', and by function 'base' in module 'ns_dotted_module' "
            "under namespace 'ns'"
        ),
    ):
        fnloom.Builder().with_modules(rows).with_modules(module, namespace="ns").build()
    # Two modules under one namespace clash as plain modules do, namespace named.
    twin = make_module("ns_dotted_twin", _PRICING)
    with pytest.raises(
        ValueError,
        match=re.escape(
            "node 'ns.base' is defined twice: by function 'base' in module "
            "'ns_dotted_module' under namespace 'ns', and by function 'base' in "
            "module 'ns_dotted_twin' under namespace 'ns'"
        ),
    ):
        fnloom.Builder().with_modules(module, twin, namespace="ns").build()


def test_namespaces_survive_pickling_and_copying() -> None:
    """A pickled or copied builder and driver keep each module's namespace."""
    module = make_module("ns_pickled", _PRICING)
    builder = (
        fnloom.Builder()
        .with_modules(module)
        .with_modules(module, namespace="eu")
        .with_declared_inputs("x")
    )
    loaded = pickle.loads(pickle.dumps(builder))  # noqa: S301  # a builder this test just pickled
    assert loaded.build().node_names() == ["base", "eu.base", "eu.total", "total", "x"]
    driver = builder.build()
    again = pickle.loads(pickle.dumps(driver))  # noqa: S301  # a driver this test just pickled
    assert again.execute(["eu.total", "total"], inputs={"x": 1}) == {
        "eu.total": 20,
        "total": 20,
    }
    assert copy.copy(driver).node_names() == driver.node_names()
    built = builder.build_async()
    restored = pickle.loads(pickle.dumps(built))  # noqa: S301  # a driver this test just pickled
    assert asyncio.run(restored.execute(["eu.total"], inputs={"x": 1})) == {
        "eu.total": 20
    }


def test_namespaced_nodes_cache_under_their_full_names() -> None:
    """Two namespaces of one module are two cache entries, keyed by full name."""
    module = make_module("ns_cached", _PRICING)
    store = fnloom.MemoryCache()
    driver = (
        fnloom.Builder()
        .with_modules(module, namespace="eu")
        .with_modules(module, namespace="us")
        .with_cache(store)
        .build()
    )
    request = ["eu.total", "us.total"]
    computed = ["eu.base", "eu.total", "us.base", "us.total"]
    assert {
        name: outcome.status
        for name, outcome in driver.explain_cache(request, inputs={"x": 1}).items()
    } == dict.fromkeys(computed, "miss")
    first = driver.execute(request, inputs={"x": 1})
    assert first == {"eu.total": 20, "us.total": 20}
    # An entry per node: each namespace's are stored under their own names.
    assert len(store) == len(computed)
    assert {
        name: outcome.status
        for name, outcome in driver.explain_cache(request, inputs={"x": 1}).items()
    } == dict.fromkeys(computed, "hit")
    assert driver.execute(request, inputs={"x": 1}) == first


def test_extract_columns_and_all_hints_under_a_namespace() -> None:
    """Columns are prefixed; a function `__all__` leaves out hints with its node."""
    module = make_module(
        "ns_columns",
        """
        import fnloom

        __all__ = ["table", "width"]


        @fnloom.extract_columns("left", "right")
        def table(x: int) -> dict:
            return {"left": x, "right": x + 1}


        def hidden(x: int) -> int:
            return x


        def width(left: int, right: int, hidden: int) -> int:
            return right - left + hidden
        """,
    )
    driver = fnloom.Builder().with_modules(module, namespace="t").build()
    assert driver.nodes["t.width"].dependencies == ("hidden", "t.left", "t.right")
    assert driver.execute(["t.width"], inputs={"x": 1, "hidden": 10}) == {"t.width": 11}
    assert driver.validate(["t.width"], inputs={"x": 1}) == [
        (
            "'hidden' (node 't.hidden') is a function in module 'ns_columns' not "
            "listed in its __all__"
        ),
        "missing inputs: 'hidden' (needed by 't.width')",
    ]


def test_builder_namespaces_hold_one_per_module() -> None:
    """`Builder(namespaces=...)` needs one per module; `with_functions` takes none."""
    module = make_module("ns_lengths", _PRICING)
    with pytest.raises(
        ValueError,
        match=re.escape("namespaces must hold one per module: 1 module, 2 namespaces"),
    ):
        fnloom.Builder((module,), namespaces=("a", "b"))
    with pytest.raises(TypeError) as every:
        fnloom.Builder((module,), namespaces=("a-b", 5, "ok"))  # ty: ignore[invalid-argument-type]  # the refusal is the point
    assert str(every.value) == (
        "namespace 'a-b' is not a dotted identifier path, such as 'a' or 'a.b'; "
        "namespace must be a str or None, got int; "
        "namespaces must hold one per module: 1 module, 3 namespaces"
    )
    with pytest.raises(ValueError, match="is not a dotted identifier path"):
        fnloom.Builder((module,), namespaces=("a-b",))
    assert fnloom.Builder((module,), namespaces=("a",)).build().node_names() == [
        "a.base",
        "a.total",
        "x",
    ]
    with pytest.raises(TypeError, match="namespace"):
        fnloom.Builder().with_functions(module.base, namespace="a")  # ty: ignore[unknown-argument]  # refused on purpose
