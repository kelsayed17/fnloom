"""`with_modules(module, replace=(...))`: one named node replaced (Q74, Q150)."""

import copy
import pickle
from typing import TYPE_CHECKING

import pytest

from fnloom import Builder
from helpers import make_module

if TYPE_CHECKING:
    from types import ModuleType

SALES = """
def price() -> float:
    return 10.0

def total(price: float, quantity: int) -> float:
    return price * quantity

def report(total: float) -> str:
    return f"{total:.1f}"
"""

DISCOUNTED = """
def total(price: float, quantity: int) -> float:
    return price * quantity * 0.9
"""


def test_the_named_node_is_replaced_for_every_reader() -> None:
    """The later module's `total` stands for the earlier one; `report` reads it."""
    sales = make_module("replace_sales", SALES)
    discounted = make_module("replace_discounted", DISCOUNTED)
    driver = (
        Builder()
        .with_modules(sales)
        .with_modules(discounted, replace=("total",))
        .build()
    )
    assert driver.execute(["total", "report"], inputs={"quantity": 2}) == {
        "total": 18.0,
        "report": "18.0",
    }
    view = driver.nodes["total"]
    assert view.module == "replace_discounted"
    assert view.replaced == "replace_sales"
    assert driver.nodes["report"].replaced is None
    assert driver.node_names() == ["price", "quantity", "report", "total"]


def test_without_replace_a_name_defined_twice_still_fails() -> None:
    """Q74 stands: no `replace=`, no override."""
    sales = make_module("replace_sales_twice", SALES)
    discounted = make_module("replace_discounted_twice", DISCOUNTED)
    with pytest.raises(ValueError, match="node 'total' is defined in both"):
        Builder().with_modules(sales, discounted).build()


def test_only_the_named_node_is_replaced() -> None:
    """Another name both modules define is still the duplicate error."""
    sales = make_module("replace_sales_other", SALES)
    both = make_module(
        "replace_both",
        DISCOUNTED + "\ndef price() -> float:\n    return 5.0\n",
    )
    with pytest.raises(ValueError, match="node 'price' is defined in both"):
        Builder().with_modules(sales).with_modules(both, replace=("total",)).build()
    driver = (
        Builder()
        .with_modules(sales)
        .with_modules(both, replace=("total", "price"))
        .build()
    )
    assert driver.execute(["total"], inputs={"quantity": 2}) == {"total": 9.0}


def test_a_name_no_earlier_module_defines_is_refused() -> None:
    """A typo, or a name only this module defines, fails the build, named."""
    sales = make_module("replace_sales_typo", SALES)
    discounted = make_module("replace_discounted_typo", DISCOUNTED)
    extra = make_module(
        "replace_extra", "def bonus(total: float) -> float:\n    return total\n"
    )
    with pytest.raises(
        ValueError,
        match="replace names 'totl', which module 'replace_discounted_typo' "
        "doesn't define, under this config",
    ):
        Builder().with_modules(sales).with_modules(
            discounted, replace=("totl",)
        ).build()
    with pytest.raises(
        ValueError,
        match="replace names 'bonus', which no module given before "
        "'replace_extra' defines, under this config",
    ):
        Builder().with_modules(sales).with_modules(extra, replace=("bonus",)).build()


def test_a_later_module_does_not_replace() -> None:
    """Only modules given before are replaced; a later one clashes."""
    sales = make_module("replace_sales_order", SALES)
    discounted = make_module("replace_discounted_order", DISCOUNTED)
    with pytest.raises(ValueError, match="replace names 'total', which no module"):
        Builder().with_modules(discounted, replace=("total",)).with_modules(
            sales
        ).build()


def test_replace_needs_the_node_under_this_config() -> None:
    """A name only a variant gives under another config is refused, not a no-op.

    Review Focus 5: under `mode="b"` the earlier module defines no `total`, so
    `replace=("total",)` replaces nothing and is a `ValueError` at build.
    """
    variants = make_module(
        "replace_variants",
        """
from fnloom import config

@config.when(mode="a")
def total__a(price: float) -> float:
    return price
""",
    )
    discounted = make_module("replace_discounted_config", DISCOUNTED)
    builder = (
        Builder().with_modules(variants).with_modules(discounted, replace=("total",))
    )
    replaced = builder.with_config({"mode": "a"}).build()
    assert replaced.nodes["total"].replaced == "replace_variants"
    assert replaced.execute(["total"], inputs={"price": 2.0, "quantity": 3}) == {
        "total": pytest.approx(5.4)
    }
    with pytest.raises(
        ValueError,
        match="replace names 'total', which no module given before "
        "'replace_discounted_config' defines, under this config",
    ):
        builder.with_config({"mode": "b"}).build()
    # And the replacing module's own variant, absent under the config, is refused.
    own = make_module(
        "replace_own_variant",
        """
from fnloom import config

@config.when(mode="a")
def total__a(price: float) -> float:
    return price * 2
""",
    )
    sales = make_module("replace_sales_config", SALES)
    with pytest.raises(
        ValueError,
        match="replace names 'total', which module 'replace_own_variant' "
        "doesn't define, under this config",
    ):
        Builder().with_modules(sales).with_modules(own, replace=("total",)).with_config(
            {"mode": "b"}
        ).build()


def test_names_are_the_namespace_s() -> None:
    """Under `namespace=`, `replace=` names the namespace's nodes."""
    sales = make_module("replace_sales_ns", SALES)
    discounted = make_module("replace_discounted_ns", DISCOUNTED)
    driver = (
        Builder()
        .with_modules(sales, namespace="eu")
        .with_modules(sales, namespace="us")
        .with_modules(discounted, namespace="eu", replace=("total",))
        .build()
    )
    assert driver.execute(["eu.report", "us.report"], inputs={"quantity": 2}) == {
        "eu.report": "18.0",
        "us.report": "20.0",
    }
    assert driver.nodes["eu.total"].replaced == "replace_sales_ns"
    assert driver.nodes["us.total"].replaced is None
    # A plain-named module under no namespace doesn't replace a namespace's node.
    with pytest.raises(ValueError, match="replace names 'total', which no module"):
        Builder().with_modules(sales, namespace="eu").with_modules(
            discounted, replace=("total",)
        ).build()


def test_a_field_or_a_node_with_fields_is_refused() -> None:
    """A replacement takes one node: not an extracted field, nor its producer."""
    fields = make_module(
        "replace_fields",
        """
from fnloom import extract_fields

@extract_fields({"net": float, "gross": float})
def amounts() -> dict:
    return {"net": 1.0, "gross": 2.0}
""",
    )
    net = make_module("replace_net", "def net() -> float:\n    return 5.0\n")
    with pytest.raises(ValueError, match="replace names 'net', but it is a field"):
        Builder().with_modules(fields).with_modules(net, replace=("net",)).build()
    producer = make_module(
        "replace_producer", "def amounts() -> dict:\n    return {}\n"
    )
    with pytest.raises(ValueError, match=r"replace names 'amounts', but .* extracts"):
        Builder().with_modules(fields).with_modules(
            producer, replace=("amounts",)
        ).build()


@pytest.mark.parametrize(
    ("replace", "error", "match"),
    [
        ("total", TypeError, r"replace takes a tuple of node names, got str 'total'"),
        ((1,), TypeError, "replace names must be str, got: int"),
        (("",), ValueError, "replace names must not be empty"),
    ],
)
def test_a_bad_replace_is_refused(
    replace: object, error: type[Exception], match: str
) -> None:
    """`replace=` is a sequence of names."""
    discounted = make_module("replace_bad", DISCOUNTED)
    with pytest.raises(error, match=match):
        Builder().with_modules(discounted, replace=replace)  # ty: ignore[invalid-argument-type]  # refused is the point


def test_replace_takes_one_module() -> None:
    """Which module replaces must be clear: one per call."""
    a = make_module("replace_one_a", DISCOUNTED)
    b = make_module("replace_one_b", "def other() -> int:\n    return 1\n")
    with pytest.raises(TypeError, match=r"replace takes one module.*got 2 modules"):
        Builder().with_modules(a, b, replace=("total",))


def test_the_export_records_the_replacement() -> None:
    """The entry names the winning module and the module it replaced."""
    sales = make_module("replace_sales_export", SALES)
    discounted = make_module("replace_discounted_export", DISCOUNTED)
    document = (
        Builder()
        .with_modules(sales)
        .with_modules(discounted, replace=("total",))
        .build()
        .export()
    )
    total = next(n for n in document["nodes"] if n["name"] == "total")
    assert total["module"] == "replace_discounted_export"
    assert total["replaced"] == "replace_sales_export"
    report = next(n for n in document["nodes"] if n["name"] == "report")
    assert report["replaced"] is None


def _computed(module: ModuleType) -> set[str]:
    """The names of the nodes `module` computes, built alone."""
    views = Builder().with_modules(module).build().nodes
    return {name for name, view in views.items() if not view.is_input}


def test_pickling_copy_and_rebuild_carry_replace() -> None:
    """`replace=` survives pickling, `copy`, `rebuild()` and `build_async`."""
    import picklable_functions  # noqa: PLC0415  # importable by name, for pickling
    import picklable_nodes  # noqa: PLC0415  # importable by name, for pickling

    shared = sorted(_computed(picklable_nodes) & _computed(picklable_functions))
    if not shared:
        pytest.skip("the picklable modules share no node name")
    builder = (
        Builder()
        .with_modules(picklable_nodes)
        .with_modules(picklable_functions, replace=shared)
    )
    driver = builder.build()
    for again in (
        pickle.loads(pickle.dumps(driver)),  # noqa: S301  # our own bytes
        copy.copy(driver),
        driver.rebuild(),
        pickle.loads(pickle.dumps(builder)).build(),  # noqa: S301  # our own bytes
    ):
        assert again.nodes[shared[0]].replaced == "picklable_nodes"
    assert builder.build_async().nodes[shared[0]].replaced == "picklable_nodes"
