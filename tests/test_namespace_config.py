"""`with_modules(..., namespace=, config=)`: a namespace's own config (Q112)."""

import asyncio
import copy
import pickle
from types import MappingProxyType

import pytest

import fnloom
from fnloom import Builder
from helpers import make_module

REGIONAL = """
from fnloom import config, resolve, tag, pipe_output, step

def _is_eu(region: str) -> bool:
    return region == "eu"

@config.matches(_is_eu)
def rate__eu() -> float:
    return 0.2

@config.when(region="us")
def rate__us() -> float:
    return 0.1

@config.default
def rate__other() -> float:
    return 0.0

@resolve(lambda region: tag(region=region))
def tax(rate: float, sales: float) -> float:
    return rate * sales

def _double(x: float) -> float:
    return x * 2

@pipe_output(step(_double).when(region="eu"))
def report(tax: float) -> float:
    return tax
"""


def _regional() -> object:
    return make_module("namespace_config_regional", REGIONAL)


def _two_regions(cfg: dict[str, object] | None = None) -> Builder:
    module = _regional()
    builder = Builder()
    if cfg is not None:
        builder = builder.with_config(cfg)
    return builder.with_modules(
        module,  # ty: ignore[invalid-argument-type]  # a module from make_module
        namespace="eu",
        config={"region": "eu"},
    ).with_modules(
        module,  # ty: ignore[invalid-argument-type]  # a module from make_module
        namespace="us",
        config={"region": "us"},
    )


def test_each_namespace_selects_by_its_own_config() -> None:
    """Variants, `config.matches`, `@resolve` and steps all see the overlay."""
    driver = _two_regions().build()
    assert driver.execute(
        ["eu.rate", "us.rate", "eu.report", "us.report"], inputs={"sales": 10.0}
    ) == {"eu.rate": 0.2, "us.rate": 0.1, "eu.report": 4.0, "us.report": 1.0}
    assert driver.nodes["eu.tax"].tags == {"region": "eu"}
    assert driver.nodes["us.tax"].tags == {"region": "us"}
    assert [c.kind for c in driver.nodes["eu.rate"].config] == ["matches"]
    assert [c.kind for c in driver.nodes["us.rate"].config] == ["when"]
    # The driver's config is the builder's own, never an overlay.
    assert driver.config == {}


def test_the_overlay_wins_over_the_builder_config() -> None:
    """The namespace's keys override the builder's; its other keys stay seen."""
    module = make_module(
        "namespace_config_overlay",
        """
from fnloom import config

@config.when(region="eu", mode="fast")
def rate__eu_fast() -> float:
    return 0.3

@config.default
def rate__other() -> float:
    return 0.0
""",
    )
    driver = (
        Builder()
        .with_config({"region": "us", "mode": "fast"})
        .with_modules(module)
        .with_modules(module, namespace="eu", config={"region": "eu"})
        .build()
    )
    assert driver.execute(["rate", "eu.rate"]) == {"rate": 0.0, "eu.rate": 0.3}
    assert driver.config == {"region": "us", "mode": "fast"}


def test_a_namespace_without_config_sees_the_builder_config() -> None:
    """Only a namespace given `config=` is overlaid."""
    module = _regional()
    driver = (
        Builder()
        .with_config({"region": "us"})
        .with_modules(module, namespace="eu", config={"region": "eu"})  # ty: ignore[invalid-argument-type]  # a module from make_module
        .with_modules(module, namespace="other")  # ty: ignore[invalid-argument-type]  # a module from make_module
        .build()
    )
    assert driver.execute(["eu.rate", "other.rate"]) == {
        "eu.rate": 0.2,
        "other.rate": 0.1,
    }


def test_the_overlay_holds_for_the_whole_namespace() -> None:
    """A later `with_modules` under the namespace, without `config=`, sees it too."""
    rates = make_module(
        "namespace_config_rates",
        """
from fnloom import config

@config.when(region="eu")
def rate__eu() -> float:
    return 0.2

@config.default
def rate__other() -> float:
    return 0.0
""",
    )
    totals = make_module(
        "namespace_config_totals",
        """
from fnloom import config

@config.when(region="eu")
def label__eu() -> str:
    return "EU"

@config.default
def label__other() -> str:
    return "?"
""",
    )
    driver = (
        Builder()
        .with_modules(rates, namespace="eu", config={"region": "eu"})
        .with_modules(totals, namespace="eu")
        .build()
    )
    assert driver.execute(["eu.rate", "eu.label"]) == {
        "eu.rate": 0.2,
        "eu.label": "EU",
    }


def test_config_needs_a_namespace() -> None:
    """`config=` without `namespace=` is a `TypeError`, pointing at `with_config`."""
    with pytest.raises(TypeError, match=r"config needs a namespace.*with_config"):
        Builder().with_modules(_regional(), config={"region": "eu"})  # ty: ignore[invalid-argument-type]  # a module from make_module


def test_config_keys_must_be_str() -> None:
    """A key that is no `str` is a `TypeError`, as `with_config`'s are."""
    with pytest.raises(TypeError, match="config keys under namespace 'eu' must be str"):
        Builder().with_modules(_regional(), namespace="eu", config={1: "eu"})  # ty: ignore[invalid-argument-type]  # refused is the point
    with pytest.raises(TypeError, match="takes a mapping, got list"):
        Builder().with_modules(_regional(), namespace="eu", config=["region"])  # ty: ignore[invalid-argument-type]  # refused is the point


def test_a_second_differing_config_is_refused() -> None:
    """The same `config=` again is fine; a different one is a `ValueError`."""
    module = _regional()
    builder = Builder().with_modules(module, namespace="eu", config={"region": "eu"})  # ty: ignore[invalid-argument-type]  # a module from make_module
    same = builder.with_modules(module, namespace="eu", config={"region": "eu"})  # ty: ignore[invalid-argument-type]  # a module from make_module
    assert same.build().execute(["eu.rate"]) == {"eu.rate": 0.2}
    with pytest.raises(
        ValueError,
        match=r"namespace 'eu' already has config \{'region': 'eu'\}, "
        r"so config \{'region': 'us'\} is refused",
    ):
        builder.with_modules(module, namespace="eu", config={"region": "us"})  # ty: ignore[invalid-argument-type]  # a module from make_module


def test_the_export_records_each_overlay_and_graph_version_sees_it() -> None:
    """`namespace_config` lists each overlay, and it moves `graph_version`."""
    first = _two_regions().build().export()
    assert first["namespace_config"] == {
        "eu": {"region": "'eu'"},
        "us": {"region": "'us'"},
    }
    assert first["config"] == {}
    module = make_module(
        "namespace_config_versions",
        "def total(sales: float) -> float:\n    return sales\n",
    )
    plain = Builder().with_modules(module, namespace="eu").build().export()
    overlaid = (
        Builder()
        .with_modules(module, namespace="eu", config={"region": "eu"})
        .build()
        .export()
    )
    assert plain["namespace_config"] == {}
    assert plain["nodes"] == overlaid["nodes"]
    assert plain["graph_version"] != overlaid["graph_version"]


def test_pickling_copy_and_rebuild_carry_the_overlay() -> None:
    """The overlay survives pickling, `copy` and `rebuild()`, and `build_async`."""
    import picklable_config_nodes  # noqa: PLC0415  # importable by name, for pickling

    builder = Builder().with_modules(
        picklable_config_nodes, namespace="eu", config={"region": "eu", "mode": "b"}
    )
    driver = builder.build()
    expected = {"eu.matched_rate": 0.2, "eu.scale": 10}
    request = ["eu.matched_rate", "eu.scale"]
    assert driver.execute(request, inputs={"a": 1}) == expected
    loaded = pickle.loads(pickle.dumps(driver))  # noqa: S301  # our own bytes
    assert loaded.execute(request, inputs={"a": 1}) == expected
    assert copy.copy(driver).execute(request, inputs={"a": 1}) == expected
    assert driver.rebuild().execute(request, inputs={"a": 1}) == expected
    reloaded = pickle.loads(pickle.dumps(builder))  # noqa: S301  # our own bytes
    assert reloaded.build().execute(request, inputs={"a": 1}) == expected
    assert isinstance(builder._overlays, MappingProxyType)  # noqa: SLF001  # read-only, as bind= is
    assert (
        builder.with_result_builder(dict).build().execute(request, inputs={"a": 1})
        == expected
    )


def test_build_async_sees_the_overlay() -> None:
    """The async driver selects by each namespace's config too."""
    driver = _two_regions().build_async()
    assert asyncio.run(driver.execute(["eu.rate", "us.rate"])) == {
        "eu.rate": 0.2,
        "us.rate": 0.1,
    }


def test_a_missing_variant_names_the_namespace_config() -> None:
    """A node no variant matches shows the config its namespace selected by."""
    module = make_module(
        "namespace_config_unmatched",
        """
from fnloom import config

@config.when(region="eu")
def rate__eu() -> float:
    return 0.2

def tax(rate: float) -> float:
    return rate
""",
    )
    with pytest.raises(
        ValueError,
        match=r"no variant of 'us\.rate' matches the config \{'region': 'us'\}",
    ):
        Builder().with_modules(module, namespace="us", config={"region": "us"}).build()
    # The same module under a matching overlay builds.
    assert fnloom.Builder().with_modules(
        module, namespace="eu", config={"region": "eu"}
    ).build().execute(["eu.tax"]) == {"eu.tax": 0.2}
