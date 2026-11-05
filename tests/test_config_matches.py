"""`@config.matches`: a variant chosen by a predicate of config keys (Q148)."""

import pickle
from typing import TYPE_CHECKING

import pytest

import fnloom
from fnloom import Builder, Condition, MemoryCache, _export, config
from fnloom._config import ConfigPolicy, predicate_name
from fnloom._decorators import decorations
from helpers import make_module

if TYPE_CHECKING:
    from collections.abc import Mapping
    from types import ModuleType

TAXES = """
from fnloom import config

def _is_eu(region: str, year: int = 2020) -> bool:
    return region == "eu" and year >= 2020

@config.matches(_is_eu)
def rate__eu() -> float:
    return 0.2

@config.default
def rate__other() -> float:
    return 0.1

def tax(rate: float, sales: float) -> float:
    return rate * sales
"""


def _taxes(name: str = "matches_taxes") -> ModuleType:
    return make_module(name, TAXES)


def _rate(module: ModuleType, cfg: Mapping[str, object]) -> object:
    return Builder().with_modules(module).with_config(cfg).build().execute(["rate"])


def test_matches_records_one_condition_and_returns_the_function() -> None:
    """The decorator returns its function and records a `matches` condition."""

    def is_big(size: int) -> bool:
        return size > 1

    def f__big() -> int:
        return 1

    assert config.matches(is_big)(f__big) is f__big
    assert decorations(f__big).config == ConfigPolicy(
        (Condition("matches", predicate_name(is_big), (is_big,)),),
        default=False,
        name=None,
    )
    assert predicate_name(is_big).endswith(
        "test_matches_records_one_condition_and_returns_the_function.<locals>.is_big"
    )


@pytest.mark.parametrize(
    ("cfg", "expected"),
    [
        ({"region": "eu"}, 0.2),
        ({"region": "eu", "year": 2021}, 0.2),
        ({"region": "eu", "year": 2010}, 0.1),
        ({"region": "us"}, 0.1),
        # A missing required key never matches (Q57); the default is used.
        ({}, 0.1),
        ({"year": 2021}, 0.1),
    ],
)
def test_parameters_are_config_keys(cfg: dict[str, object], expected: float) -> None:
    """A required parameter is a key the config must have; a defaulted one isn't."""
    assert _rate(_taxes(), cfg) == {"rate": expected}


def test_a_result_that_is_no_bool_is_a_type_error_at_build() -> None:
    """A truthy non-bool is refused, naming the predicate and the variant."""
    module = make_module(
        "matches_not_bool",
        """
from fnloom import config

def _has_region(region: str) -> object:
    return region

@config.matches(_has_region)
def rate__any() -> float:
    return 0.2
""",
    )
    with pytest.raises(
        TypeError,
        match=r"config\.matches\(matches_not_bool\._has_region\) on rate__any "
        r"must return a bool, got str",
    ):
        Builder().with_modules(module).with_config({"region": "eu"}).build()
    # A missing key never calls the predicate, so nothing is refused.
    assert Builder().with_modules(module).build().node_names() == []


def test_an_error_the_predicate_raises_names_it() -> None:
    """The predicate's own error is raised, with a note naming it."""
    module = make_module(
        "matches_raises",
        """
from fnloom import config

def _broken(region: str) -> bool:
    raise KeyError(region)

@config.matches(_broken)
def rate__any() -> float:
    return 0.2
""",
    )
    with pytest.raises(KeyError) as raised:
        Builder().with_modules(module).with_config({"region": "eu"}).build()
    assert raised.value.__notes__ == [
        "in config.matches(matches_raises._broken) on rate__any"
    ]


def test_it_combines_with_when() -> None:
    """Every condition must hold: the predicate and a `when`."""
    module = make_module(
        "matches_and_when",
        """
from fnloom import config

def _is_eu(region: str) -> bool:
    return region == "eu"

@config.matches(_is_eu)
@config.when(mode="fast")
def rate__eu_fast() -> float:
    return 0.3

@config.default
def rate__other() -> float:
    return 0.1
""",
    )
    for cfg, expected in (
        ({"region": "eu", "mode": "fast"}, 0.3),
        ({"region": "eu", "mode": "slow"}, 0.1),
        ({"region": "us", "mode": "fast"}, 0.1),
    ):
        assert _rate(module, cfg) == {"rate": expected}
    driver = (
        Builder()
        .with_modules(module)
        .with_config({"region": "eu", "mode": "fast"})
        .build()
    )
    assert [c.kind for c in driver.nodes["rate"].config] == ["matches", "when"]


def test_two_matching_variants_fail_as_q58() -> None:
    """A predicate and a `when` both matching is the usual two-variant error."""
    module = make_module(
        "matches_twice",
        """
from fnloom import config

def _is_eu(region: str) -> bool:
    return region == "eu"

@config.matches(_is_eu)
def rate__by_predicate() -> float:
    return 0.2

@config.when(region="eu")
def rate__by_when() -> float:
    return 0.3
""",
    )
    with pytest.raises(
        ValueError,
        match="node 'rate' has more than one variant matching the config "
        r"\{'region': 'eu'\}: 'rate__by_predicate', 'rate__by_when'",
    ):
        Builder().with_modules(module).with_config({"region": "eu"}).build()


def test_no_variant_names_the_predicate() -> None:
    """A node no variant matches says which predicate its variant needs."""
    module = make_module(
        "matches_unmatched",
        """
from fnloom import config

def _is_eu(region: str) -> bool:
    return region == "eu"

@config.matches(_is_eu)
def rate__eu() -> float:
    return 0.2

def tax(rate: float) -> float:
    return rate
""",
    )
    with pytest.raises(
        ValueError,
        match=r"its variants need matches\(matches_unmatched\._is_eu\) \(rate__eu\)",
    ):
        Builder().with_modules(module).with_config({"region": "us"}).build()


def test_default_cannot_be_on_the_same_function() -> None:
    """`@config.default` and `config.matches` on one function are refused."""

    def anything() -> bool:
        return True

    def f() -> int:
        return 1

    with pytest.raises(ValueError, match=r"config.matches on one function"):
        config.default(config.matches(anything)(f))


def test_name_sets_the_node() -> None:
    """`name=` names the node, as on the `when` forms."""

    def always() -> bool:
        return True

    def anything() -> int:
        return 1

    config.matches(always, name="rate")(anything)
    driver = fnloom.Builder().with_functions(anything).build()
    assert driver.node_names() == ["rate"]


@pytest.mark.parametrize(
    ("predicate", "error", "match"),
    [
        (3, TypeError, "config.matches takes a function of config keys, got int"),
        (lambda *keys: True, TypeError, "must name each config key, not keys"),  # noqa: ARG005  # the signature is the point
        (lambda **keys: True, TypeError, "must name each config key, not keys"),  # noqa: ARG005  # the signature is the point
        (lambda a, /: True, TypeError, "must name each config key, not a"),  # noqa: ARG005  # the signature is the point
    ],
)
def test_a_bad_predicate_is_refused_when_decorating(
    predicate: object, error: type[Exception], match: str
) -> None:
    """Only a function naming its config keys is a predicate."""
    with pytest.raises(error, match=match):
        config.matches(predicate)  # ty: ignore[invalid-argument-type]  # refused is the point


def test_a_bad_name_is_refused_when_decorating() -> None:
    """`name=` must be a non-empty `str`."""
    with pytest.raises(TypeError, match=r"config.matches name must be a str"):
        config.matches(lambda: True, name=3)  # ty: ignore[invalid-argument-type]  # refused is the point
    with pytest.raises(ValueError, match=r"config.matches name must not be empty"):
        config.matches(lambda: True, name="")


def test_the_export_shows_the_predicate_and_its_version() -> None:
    """`matches(<qualname>)`, and an edited predicate changes the variant's version."""
    module = _taxes("matches_export_a")
    edited = make_module(
        "matches_export_a",
        TAXES.replace("year >= 2020", "year >= 2019"),
    )
    cfg = {"region": "eu"}
    before = Builder().with_modules(module).with_config(cfg).build().export()
    after = Builder().with_modules(edited).with_config(cfg).build().export()
    rate = next(n for n in before["nodes"] if n["name"] == "rate")
    assert rate["config"] == ["matches(matches_export_a._is_eu)"]
    changed = _export.diff(before, after)
    assert changed["changed"] == {"rate": ["version"]}
    assert changed["graph_version_changed"]
    # The default variant reads no predicate: an edit to it changes nothing there.
    us = {"region": "us"}
    plain_before = Builder().with_modules(module).with_config(us).build().export()
    plain_after = Builder().with_modules(edited).with_config(us).build().export()
    assert _export.diff(plain_before, plain_after)["changed"] == {}


def test_an_edited_predicate_misses_in_the_cache() -> None:
    """The predicate's code version is in the selected variant's key."""
    store = MemoryCache()
    cfg = {"region": "eu"}
    module = _taxes("matches_cache")
    first = Builder().with_modules(module).with_config(cfg).with_cache(store).build()
    assert first.execute(["rate"]) == {"rate": 0.2}
    assert first.explain_cache(["rate"])["rate"].status == "hit"
    edited = make_module("matches_cache", TAXES.replace("year >= 2020", "year > 0"))
    second = Builder().with_modules(edited).with_config(cfg).with_cache(store).build()
    assert second.explain_cache(["rate"])["rate"].status == "miss"


def test_an_unversioned_predicate_makes_the_variant_uncacheable() -> None:
    """A predicate reading a mutable global has no version: its variant isn't cached."""
    module = make_module(
        "matches_unversioned",
        """
from fnloom import config

REGIONS = ["eu"]

def _listed(region: str) -> bool:
    return region in REGIONS

@config.matches(_listed)
def rate__listed() -> float:
    return 0.2
""",
    )
    driver = (
        Builder()
        .with_modules(module)
        .with_config({"region": "eu"})
        .with_cache(MemoryCache())
        .build()
    )
    outcome = driver.explain_cache(["rate"])["rate"]
    assert outcome.status == "uncacheable"
    assert "config.matches predicate 'matches_unversioned._listed'" in str(
        outcome.reason
    )
    rate = next(n for n in driver.export()["nodes"] if n["name"] == "rate")
    assert rate["version"] is None
    assert "config.matches predicate" in rate["unversioned"]


def test_a_builder_with_a_predicate_pickles(tmp_path: object) -> None:  # noqa: ARG001  # pytest's fixture, unused
    """A pickled driver is built again and selects the same variant."""
    import picklable_config_nodes  # noqa: PLC0415  # importable by name, for pickling

    driver = (
        Builder()
        .with_modules(picklable_config_nodes)
        .with_config({"region": "eu"})
        .build()
    )
    loaded = pickle.loads(pickle.dumps(driver))  # noqa: S301  # our own bytes
    assert loaded.execute(["matched_rate"]) == driver.execute(["matched_rate"])
    assert loaded.execute(["matched_rate"]) == {"matched_rate": 0.2}


def test_a_public_predicate_is_refused_as_a_node() -> None:
    """A module-level public predicate would be a node: the build says so."""
    module = make_module(
        "matches_public",
        """
from fnloom import config, exclude

def is_eu(region: str) -> bool:
    return region == "eu"

@config.matches(is_eu)
def rate__eu() -> float:
    return 0.2
""",
    )
    with pytest.raises(
        ValueError,
        match=r"'is_eu' is a config.matches predicate and a node; prefix it with "
        "`_` or `@exclude` it",
    ):
        Builder().with_modules(module).with_config({"region": "eu"}).build()
    excluded = make_module(
        "matches_excluded",
        """
from fnloom import config, exclude

@exclude
def is_eu(region: str) -> bool:
    return region == "eu"

@config.matches(is_eu)
def rate__eu() -> float:
    return 0.2
""",
    )
    driver = Builder().with_modules(excluded).with_config({"region": "eu"}).build()
    assert driver.execute(["rate"]) == {"rate": 0.2}
    # A public lambda is named by the attribute that holds it, not `<lambda>`.
    held = make_module(
        "matches_public_lambda",
        """
from fnloom import config

is_us = lambda region: region == "us"  # noqa: E731

@config.matches(is_us)
def rate__us() -> float:
    return 0.1
""",
    )
    with pytest.raises(ValueError, match=r"^'is_us' is a config\.matches predicate"):
        Builder().with_modules(held).with_config({"region": "us"}).build()


def test_a_predicate_runs_only_when_the_when_conditions_hold() -> None:
    """A variant a `when` already rules out never calls its predicate."""
    module = make_module(
        "matches_after_when",
        """
from fnloom import config

def _big(limit: int) -> bool:
    return limit > 10

@config.when(engine="spark")
@config.matches(_big)
def rows__spark() -> int:
    return 1

@config.default
def rows__other() -> int:
    return 2
""",
    )
    driver = (
        Builder()
        .with_modules(module)
        .with_config({"engine": "pandas", "limit": "n/a"})
        .build()
    )
    assert driver.execute(["rows"]) == {"rows": 2}


_FIELDS = """
from fnloom import cache, config, extract_fields

def _is_eu(region: str) -> bool:
    return {test}

@config.matches(_is_eu)
@cache(behavior="disable", fields={{"net": "default"}})
@extract_fields({{"net": float}})
def amounts__eu() -> dict:
    return {{"net": 1.0}}
"""


def test_a_stored_field_s_key_has_the_predicate_s_version() -> None:
    """A field stored apart from a `disable` producer misses on an edited predicate."""
    store = MemoryCache()
    cfg = {"region": "eu"}
    first = make_module("matches_fields", _FIELDS.format(test='region == "eu"'))
    driver = Builder().with_modules(first).with_config(cfg).with_cache(store).build()
    assert driver.execute(["net"]) == {"net": 1.0}
    assert driver.explain_cache(["net"])["net"].status == "hit"
    edited = make_module("matches_fields", _FIELDS.format(test='region in ("eu",)'))
    again = Builder().with_modules(edited).with_config(cfg).with_cache(store).build()
    assert again.explain_cache(["net"])["net"].status == "miss"
