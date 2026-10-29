"""Output checks: the built-ins, custom checks, failing and warning, and caching."""

import asyncio
import logging
from typing import TYPE_CHECKING, cast, override

import pytest

import fnloom
from fnloom import CheckResult, ValidationError, Validator, check_output
from helpers import make_module

if TYPE_CHECKING:
    from collections.abc import Mapping


class Recorder(fnloom.Hook):
    """Keeps each node's `NodeEnd`."""

    def __init__(self) -> None:
        """Starts with no events."""
        self.ends: dict[str, fnloom.NodeEnd] = {}

    @override
    def after_node(self, event: fnloom.NodeEnd) -> None:
        """Keeps the event by node."""
        self.ends[event.node.name] = event


def _driver(source: str, name: str = "checked", **build: object) -> fnloom.Driver:
    builder = fnloom.Builder().with_modules(make_module(name, source))
    for method, value in build.items():
        builder = getattr(builder, method)(value)
    return cast("fnloom.Driver", builder.build())


RANGED = """
from fnloom import check_output

@check_output(range=(0, 10))
def a(x: int) -> int:
    return x
"""


def test_a_passing_value_is_returned_as_it_is() -> None:
    """No node is added, and the value passes through."""
    driver = _driver(RANGED)
    assert driver.node_names() == ["a", "x"]
    assert driver.execute(["a"], inputs={"x": 3}) == {"a": 3}


def test_a_failing_fail_check_is_a_value_error() -> None:
    """One readable line per failed check; the results on the error (Q100)."""
    driver = _driver(RANGED)
    with pytest.raises(ValidationError) as caught:
        driver.execute(["a"], inputs={"x": 12})
    error = caught.value
    assert isinstance(error, ValueError)
    assert str(error) == "a: check range failed: 12 is not in [0, 10]"
    assert error.node == "a"
    (result,) = error.results
    assert result == CheckResult(
        "range",
        "fail",
        passes=False,
        message="12 is not in [0, 10]",
        diagnostics=result.diagnostics,
    )
    assert dict(result.diagnostics) == {"range": (0, 10)}


WARN_AND_FAIL = """
from fnloom import check_output

@check_output(range=(0, 10))
@check_output(values_in=[1, 2], importance="warn")
def a(x: int) -> int:
    return x
"""


def test_a_warn_and_a_fail_check_on_one_node(caplog: pytest.LogCaptureFixture) -> None:
    """One WARNING, the error names only the fail check, the event has both (RF 1)."""
    recorder = Recorder()
    driver = _driver(WARN_AND_FAIL, with_hooks=recorder)
    with caplog.at_level(logging.DEBUG), pytest.raises(ValidationError) as caught:
        driver.execute(["a"], inputs={"x": 12})
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert [r.name for r in warnings] == ["fnloom"]
    assert (
        warnings[0].getMessage() == "a: check values_in failed: 12 is not one of [1, 2]"
    )
    assert [r.name for r in caught.value.results] == ["range"]
    end = recorder.ends["a"]
    assert not end.ok
    assert end.error is caught.value
    assert [(c.name, c.passes) for c in end.checks] == [
        ("range", False),
        ("values_in", False),
    ]


def test_a_warn_failure_logs_once_per_run_and_the_run_goes_on(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Through `logging` only, once per failed check per run (amended Q100)."""
    source = """
from fnloom import check_output

@check_output(range=(0, 1), importance="warn")
def a(x: int) -> int:
    return x

def b(a: int) -> int:
    return a + 1
"""
    driver = _driver(source)
    with caplog.at_level(logging.DEBUG):
        assert driver.execute(["b"], inputs={"x": 5}) == {"b": 6}
        driver.execute(["b"], inputs={"x": 5})
    messages = [r.getMessage() for r in caplog.records if r.name == "fnloom"]
    assert messages == ["a: check range failed: 5 is not in [0, 1]"] * 2


def test_checks_run_in_the_order_written_top_first() -> None:
    """Stacked decorators and several keywords, top decorator first."""
    source = """
from fnloom import check_output, check_output_custom

def _first(v: int) -> bool:
    return True

@check_output_custom(_first)
@check_output(range=(0, 9), allow_none=False)
def a(x: int) -> int:
    return x
"""
    recorder = Recorder()
    _driver(source, with_hooks=recorder).execute(["a"], inputs={"x": 1})
    names = [c.name for c in recorder.ends["a"].checks]
    assert names == ["_first", "range", "allow_none"]


# ---- the built-ins over values of every shape --------------------------------------


@pytest.mark.parametrize(
    ("checks", "value", "passes"),
    [
        ({"range": (0, 10)}, 10, True),
        ({"range": (0, 10)}, 10.5, False),
        ({"range": (0, 10)}, [1, 2, 3], True),
        ({"range": (0, 10)}, [1, 20], False),
        ({"range": (0, 10)}, (), True),
        ({"values_in": [1, 2]}, 2, True),
        ({"values_in": [1, 2]}, [1, 1, 2], True),
        ({"values_in": [1, 2]}, [1, 3], False),
        ({"values_in": ["ab"]}, "ab", True),
        ({"data_type": list[int]}, [1, 2], True),
        ({"data_type": list[int]}, 3, False),
        ({"data_type": int | None}, None, True),
        ({"allow_none": False}, None, False),
        ({"allow_none": False}, 0, True),
        ({"contains": "lo"}, "hello", True),
        ({"contains": ["he", "zz"]}, "hello", False),
        ({"does_not_contain": "zz"}, "hello", True),
        ({"allow_nans": False}, float("nan"), False),
        ({"allow_nans": False}, [1.0, float("nan")], False),
        ({"allow_nans": False}, [1.0, 2.0], True),
        ({"max_fraction_nans": 0.5}, [1.0, float("nan")], True),
        ({"max_fraction_nans": 0.4}, [1.0, float("nan")], False),
    ],
)
def test_builtin_checks(checks: dict[str, object], value: object, passes: bool) -> None:  # noqa: FBT001  # the case's answer
    """Each built-in on scalars and on plain collections."""
    module = make_module(
        "builtin_checked",
        "from typing import Any\ndef a(v: Any) -> Any:\n    return v\n",
    )
    check_output(**checks)(module.a)  # ty: ignore[invalid-argument-type]
    driver = fnloom.build(module)
    if passes:
        assert driver.execute(["a"], inputs={"v": value}) == {"a": value}
    else:
        with pytest.raises(ValidationError):
            driver.execute(["a"], inputs={"v": value})


def test_a_mapping_and_a_str_are_one_value() -> None:
    """Never iterated as items (amended Q101)."""
    module = make_module(
        "whole_checked",
        "from typing import Any\ndef a(v: Any) -> Any:\n    return v\n",
    )
    check_output(values_in=["ab", {"k": 1}])(module.a)
    driver = fnloom.build(module)
    assert driver.execute(["a"], inputs={"v": "ab"}) == {"a": "ab"}
    with pytest.raises(ValidationError):
        driver.execute(["a"], inputs={"v": {"k": 2}})


def test_a_one_shot_iterator_is_never_consumed() -> None:
    """Refused at build when annotated; a TypeError at run otherwise (amended Q101)."""
    annotated = """
from collections.abc import Iterator
from fnloom import check_output

@check_output(range=(0, 9))
def a(x: int) -> Iterator[int]:
    return iter([x])
"""
    with pytest.raises(TypeError, match="a: check range would consume a one-shot"):
        _driver(annotated, "one_shot_annotated")
    hidden = annotated.replace("-> Iterator[int]", "-> object").replace(
        "from collections.abc import Iterator\n", ""
    )
    driver = _driver(hidden, "one_shot_hidden")
    with pytest.raises(TypeError, match="check range would consume a one-shot"):
        driver.execute(["a"], inputs={"x": 1})


def test_numpy_and_pandas_are_checked_vectorised() -> None:
    """Through their own min, max, isin and NaN tests, without changing the value."""
    numpy = pytest.importorskip("numpy")
    pandas = pytest.importorskip("pandas")
    module = make_module(
        "vector_checked",
        "from typing import Any\ndef a(v: Any) -> Any:\n    return v\n",
    )
    check_output(range=(0, 10), values_in=[0, 1, 2, 3])(module.a)
    driver = fnloom.build(module)
    frame = pandas.DataFrame({"p": [1, 2], "q": [3, 0]})
    for value in (numpy.array([1, 2, 3]), pandas.Series([1.0, 2.0]), frame):
        (got,) = driver.execute(["a"], inputs={"v": value}).values()
        assert got is value
    for bad in (numpy.array([1, 20]), pandas.Series([1, 7])):
        with pytest.raises(ValidationError):
            driver.execute(["a"], inputs={"v": bad})
    nans = make_module(
        "nan_checked",
        "from typing import Any\ndef a(v: Any) -> Any:\n    return v\n",
    )
    check_output(max_fraction_nans=0.5)(nans.a)
    counted = fnloom.build(nans)
    assert counted.execute(["a"], inputs={"v": numpy.array([1.0, numpy.nan])})
    with pytest.raises(ValidationError, match=r"0\.667 of the values are NaN"):
        counted.execute(["a"], inputs={"v": pandas.Series([numpy.nan, numpy.nan, 1.0])})


def test_a_check_that_cannot_read_the_annotation_fails_the_build() -> None:
    """A TypeError naming the node, the check and the type (Q101)."""
    source = """
from fnloom import check_output

@check_output(range=(0, 9))
def a(x: int) -> str:
    return str(x)

@check_output(contains="z")
def b(x: int) -> int:
    return x
"""
    with pytest.raises(TypeError) as caught:
        _driver(source, "unreadable")
    assert str(caught.value) == (
        "a: check range does not apply to str\n"
        "b: check contains applies to str, not int"
    )


@pytest.mark.parametrize(
    ("arguments", "error", "match"),
    [
        ({}, TypeError, "at least one check"),
        ({"range": (3, 1)}, ValueError, "low is above high"),
        ({"range": 3}, TypeError, r"\(low, high\) pair"),
        ({"max_fraction_nans": 2}, ValueError, r"in \[0, 1\]"),
        ({"values_in": "ab"}, TypeError, "collection of values"),
        ({"contains": [1]}, TypeError, "str or str values"),
        ({"importance": "loud", "range": (0, 1)}, ValueError, "'fail' or 'warn'"),
        ({"field": 3, "range": (0, 1)}, TypeError, "field must be"),
    ],
)
def test_bad_arguments_are_refused_when_decorating(
    arguments: dict[str, object], error: type[Exception], match: str
) -> None:
    """Before any build."""
    with pytest.raises(error, match=match):
        check_output(**arguments)  # ty: ignore[invalid-argument-type]


# ---- custom checks ------------------------------------------------------------------


CUSTOM = """
from fnloom import CheckResult, Validator, check_output_custom

def _small(v: int) -> bool:
    return v < 5

def _named(v: int) -> str | None:
    return None if v != 3 else "three is unlucky"

def _uses_inputs(v: int, inputs) -> bool:
    return v == inputs["x"] * 2

class Even(Validator):
    name = "even"

    def validate(self, value, inputs):
        return CheckResult("even", self.importance, value % 2 == 0, "odd")

@check_output_custom(_small, _named, _uses_inputs, Even(), importance="fail")
def a(x: int) -> int:
    return x * 2
"""


def test_custom_checks() -> None:
    """Functions return bool, str or None; one reads inputs; a Validator (Q102)."""
    recorder = Recorder()
    driver = _driver(CUSTOM, "custom", with_hooks=recorder)
    assert driver.execute(["a"], inputs={"x": 2}) == {"a": 4}
    assert [c.name for c in recorder.ends["a"].checks] == [
        "_small",
        "_named",
        "_uses_inputs",
        "even",
    ]
    with pytest.raises(ValidationError) as caught:
        driver.execute(["a"], inputs={"x": 3})
    assert str(caught.value) == "a: check _small failed: it returned False"


def test_a_validator_refuses_an_annotation_at_build() -> None:
    """`applies_to` is called at build (Q102); Hamilton never calls it."""

    class OnlyStr(Validator):
        @override
        def applies_to(self, declared: object) -> bool:
            return declared is str

        @override
        def validate(self, value: object, inputs: Mapping[str, object]) -> bool:
            del value, inputs
            return True

    module = make_module("refused_validator", "def a(x: int) -> int:\n    return x\n")
    fnloom.check_output_custom(OnlyStr())(module.a)
    with pytest.raises(TypeError, match="a: check OnlyStr does not apply to int"):
        fnloom.build(module)


def test_async_checks_and_bad_returns_are_refused() -> None:
    """An async def is refused when decorating; a wrong return at run (Q102)."""

    async def _later(v: int) -> bool:
        del v
        return True

    with pytest.raises(TypeError, match="async def"):
        fnloom.check_output_custom(_later)

    class Late(Validator):
        @override
        async def validate(self, value: object, inputs: Mapping[str, object]) -> bool:
            del value, inputs
            return True

    with pytest.raises(TypeError, match="async validate"):
        fnloom.check_output_custom(Late())
    module = make_module("bad_return", "def a(x: int) -> int:\n    return x\n")
    fnloom.check_output_custom(lambda _: 3)(module.a)
    with pytest.raises(TypeError, match="must return a bool"):
        fnloom.build(module).execute(["a"], inputs={"x": 1})


# ---- fields, overrides, the async driver, caching -----------------------------------


FIELDS = """
from fnloom import check_output, extract_fields

@check_output(range=(0, 5), field="p")
@extract_fields({"p": int, "q": int})
def d(x: int) -> dict:
    return {"p": x, "q": 100}
"""


def test_a_field_is_checked_rather_than_the_whole_value() -> None:
    """`field=` puts the check on that field's node (Q103)."""
    driver = _driver(FIELDS, "fields")
    assert driver.execute(["q"], inputs={"x": 1}) == {"q": 100}
    with pytest.raises(ValidationError, match="p: check range failed"):
        driver.execute(["p"], inputs={"x": 9})
    unknown = FIELDS.replace('field="p"', 'field="zz"')
    with pytest.raises(ValueError, match="names field 'zz'"):
        _driver(unknown, "fields_unknown")


def test_decorator_order_does_not_matter() -> None:
    """Above or below `@extract_fields` and `@tag`, the same checks (Q103)."""
    above = FIELDS
    below = """
from fnloom import check_output, extract_fields

@extract_fields({"p": int, "q": int})
@check_output(range=(0, 5), field="p")
def d(x: int) -> dict:
    return {"p": x, "q": 100}
"""
    for index, source in enumerate((above, below)):
        driver = _driver(source, f"order_{index}")
        with pytest.raises(ValidationError, match="p: check range failed"):
            driver.execute(["p"], inputs={"x": 9})


def test_rows_and_variants_are_each_checked() -> None:
    """Every `@parameterize` row and `@config.when` variant (KEEP)."""
    source = """
from fnloom import check_output, config, parameterize, value

@check_output(range=(0, 5))
@parameterize(small={"k": value(1)}, big={"k": value(10)})
def scaled(x: int, k: int) -> int:
    return x * k

@check_output(range=(0, 5))
@config.when(mode="a")
def chosen__a(x: int) -> int:
    return x * 10
"""
    driver = (
        fnloom.Builder()
        .with_modules(make_module("rows_checked", source))
        .with_config({"mode": "a"})
        .build()
    )
    assert driver.execute(["small"], inputs={"x": 1}) == {"small": 1}
    with pytest.raises(ValidationError, match="big: check range"):
        driver.execute(["big"], inputs={"x": 1})
    with pytest.raises(ValidationError, match="chosen: check range"):
        driver.execute(["chosen"], inputs={"x": 1})


def test_an_override_is_not_checked() -> None:
    """It doesn't run (KEEP, H8.7)."""
    driver = _driver(RANGED + "\ndef b(a: int) -> int:\n    return a\n", "override")
    assert driver.execute(["b"], overrides={"a": 99}) == {"b": 99}


def test_the_async_driver_checks_sync_and_async_nodes() -> None:
    """The same checks, the same error, on both kinds of node."""
    source = (
        RANGED
        + """
@check_output(range=(0, 10))
async def b(x: int) -> int:
    return x
"""
    )
    driver = (
        fnloom.Builder()
        .with_modules(make_module("async_checked", source))
        .build_async()
    )
    assert asyncio.run(driver.execute(["a", "b"], inputs={"x": 1})) == {"a": 1, "b": 1}
    for name in ("a", "b"):
        with pytest.raises(ValidationError, match=f"{name}: check range"):
            asyncio.run(driver.execute([name], inputs={"x": 11}))


def test_cache_hits_are_not_checked_and_failures_are_not_stored(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A warned value is stored, its warning not logged again (Q103)."""
    source = """
from fnloom import check_output

@check_output(range=(0, 1), importance="warn")
def a(x: int) -> int:
    return x

@check_output(range=(0, 1))
def b(x: int) -> int:
    return x
"""
    store = fnloom.MemoryCache()
    recorder = Recorder()
    module = make_module("cached_checks", source)
    driver = (
        fnloom.Builder()
        .with_modules(module)
        .with_cache(store)
        .with_hooks(recorder)
        .build()
    )
    with caplog.at_level(logging.DEBUG):
        driver.execute(["a"], inputs={"x": 5})
        driver.execute(["a"], inputs={"x": 5})
    assert [r.getMessage() for r in caplog.records if r.name == "fnloom"] == [
        "a: check range failed: 5 is not in [0, 1]"
    ]
    assert recorder.ends["a"].checks == ()
    for _ in range(2):
        with pytest.raises(ValidationError):
            driver.execute(["b"], inputs={"x": 5})
    assert driver.explain_cache(["b"], inputs={"x": 5})["b"].status == "miss"


def test_checks_are_part_of_the_cache_key() -> None:
    """A stricter check added later misses, never returning a value unchecked."""
    loose = "def a(x: int) -> int:\n    return x\n"
    strict = "from fnloom import check_output\n\n@check_output(range=(0, 1))\n" + loose
    store = fnloom.MemoryCache()

    def built(source: str) -> fnloom.Driver:
        module = make_module("keyed_checks", source)
        return fnloom.Builder().with_modules(module).with_cache(store).build()

    assert built(loose).execute(["a"], inputs={"x": 5}) == {"a": 5}
    assert built(loose).explain_cache(["a"], inputs={"x": 5})["a"].status == "hit"
    stricter = built(strict)
    assert stricter.explain_cache(["a"], inputs={"x": 5})["a"].status == "miss"
    with pytest.raises(ValidationError):
        stricter.execute(["a"], inputs={"x": 5})
    looser = built(strict.replace("(0, 1)", "(0, 9)"))
    assert looser.explain_cache(["a"], inputs={"x": 5})["a"].status == "miss"


def test_a_check_without_a_version_makes_the_node_uncacheable() -> None:
    """Saying why, rather than keying it wrong."""
    source = """
from fnloom import Validator, check_output_custom

class Holds(Validator):
    def __init__(self) -> None:
        super().__init__()
        self.thing = object()

    def validate(self, value, inputs):
        return True

@check_output_custom(Holds())
def a(x: int) -> int:
    return x
"""
    module = make_module("unversioned_check", source)
    driver = (
        fnloom.Builder().with_modules(module).with_cache(fnloom.MemoryCache()).build()
    )
    outcome = driver.explain_cache(["a"], inputs={"x": 1})["a"]
    assert outcome.status == "uncacheable"
    assert outcome.reason is not None
    assert "check Holds" in outcome.reason


def test_a_failing_node_is_never_checked() -> None:
    """Its own error, unwrapped (KEEP, H8.9)."""
    called: list[int] = []

    def _never(value: int) -> bool:
        called.append(value)
        return True

    module = make_module(
        "failing_checked",
        "def a(x: int) -> int:\n    raise RuntimeError('node failed')\n",
    )
    fnloom.check_output_custom(_never)(module.a)
    with pytest.raises(RuntimeError, match="node failed"):
        fnloom.build(module).execute(["a"], inputs={"x": 1})
    assert called == []


def test_node_end_checks_default_is_empty() -> None:
    """An unchecked node's event has no results."""
    recorder = Recorder()
    _driver(
        "def a(x: int) -> int:\n    return x\n", "plain", with_hooks=recorder
    ).execute(["a"], inputs={"x": 1})
    assert recorder.ends["a"].checks == ()
