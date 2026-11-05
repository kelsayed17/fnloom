"""`fnloom.frame(...)`: one pandas or polars frame from a run's outputs (Q144)."""

import asyncio
import copy
import logging
import pickle
import subprocess
import sys
from typing import Any

import pytest

import fnloom
from helpers import make_module

# Typed `Any`: fnloom depends on neither, so neither is typed as a dependency.
pd: Any = pytest.importorskip("pandas")
pl: Any = pytest.importorskip("polars")

SALES = """
import pandas as pd


def prices() -> pd.Series:
    return pd.Series([1.0, 2.0, 3.0])


def quantities() -> pd.Series:
    return pd.Series([1, 2, 3])


def shorter() -> pd.Series:
    return pd.Series([10, 20])


def regions() -> pd.DataFrame:
    return pd.DataFrame({"north": [1, 2, 3], "south": [4, 5, 6]})


def rate() -> float:
    return 0.2


def revenue(prices: pd.Series, quantities: pd.Series) -> pd.Series:
    return prices * quantities
"""


def _sales() -> Any:  # noqa: ANN401  # a module made at run time
    return make_module("frames_sales", SALES)


def _run(builder: Any, outputs: list[str]) -> Any:  # noqa: ANN401  # a frame
    driver = fnloom.Builder().with_modules(_sales()).with_result_builder(builder)
    return driver.build().execute(outputs)


# pandas


def test_series_and_frames_flatten_in_request_order() -> None:
    """A frame's columns are `<output>.<column>`, in request then column order."""
    built = _run(fnloom.frame(), ["revenue", "regions", "quantities"])
    assert isinstance(built, pd.DataFrame)
    assert list(built.columns) == [
        "revenue",
        "regions.north",
        "regions.south",
        "quantities",
    ]
    assert built["regions.south"].tolist() == [4, 5, 6]
    assert built["revenue"].tolist() == [1.0, 4.0, 9.0]


def test_a_lone_frame_is_flattened_too() -> None:
    """A lone frame output is flattened too, where Hamilton returns it bare."""
    built = _run(fnloom.frame(), ["regions"])
    assert list(built.columns) == ["regions.north", "regions.south"]


def test_separator_and_flatten_false() -> None:
    """`separator` joins the names; `flatten=False` keeps the frame's own names."""
    assert list(_run(fnloom.frame(separator="__"), ["regions"]).columns) == [
        "regions__north",
        "regions__south",
    ]
    kept = _run(fnloom.frame(flatten=False), ["regions", "revenue"])
    assert list(kept.columns) == ["north", "south", "revenue"]


def test_scalars_broadcast_and_all_scalars_make_one_row() -> None:
    """A scalar fills its column; outputs that are all scalars make one row."""
    built = _run(fnloom.frame(), ["revenue", "rate"])
    assert built["rate"].tolist() == [0.2, 0.2, 0.2]
    one = fnloom.frame()({"rate": 0.2, "name": "north"})
    assert one.to_dict("records") == [{"rate": 0.2, "name": "north"}]


def test_strict_refuses_unequal_lengths_naming_them() -> None:
    """Different lengths are a `ValueError` naming each output and its rows."""
    with pytest.raises(
        ValueError,
        match=r"^frame\(join='strict'\) needs every output on one index: "
        r"prices has 3 rows, shorter has 2 rows; pass join='outer' to align them$",
    ):
        _run(fnloom.frame(), ["prices", "shorter"])


def test_strict_refuses_equal_lengths_on_other_labels() -> None:
    """Same length, other labels: the first differing row and both labels are named."""
    build = fnloom.frame()
    moved = pd.Series([1, 2, 3], index=[0, 1, 5])
    with pytest.raises(
        ValueError,
        match=r"^frame\(join='strict'\) needs every output on one index: "
        r"prices and moved both have 3 rows, but their labels differ at row 2: "
        r"2 against 5; pass join='outer' to align them$",
    ):
        build({"prices": pd.Series([1.0, 2.0, 3.0]), "moved": moved})


def test_strict_checks_a_frame_and_a_plain_list_too() -> None:
    """A frame's index and a list's length count as a series' do."""
    build = fnloom.frame()
    frame = pd.DataFrame({"p": [1, 2]}, index=[7, 8])
    with pytest.raises(ValueError, match=r"table has 2 rows, values has 3 rows"):
        build({"table": frame, "values": [1, 2, 3]})
    with pytest.raises(ValueError, match=r"labels differ at row 0: 7 against 0"):
        build({"table": frame, "plain": pd.Series([1, 2])})
    aligned = build({"table": frame, "values": [5, 6], "rate": 1})
    assert aligned.to_dict("list") == {
        "table.p": [1, 2],
        "values": [5, 6],
        "rate": [1, 1],
    }
    assert list(aligned.index) == [7, 8]


def test_outer_aligns_as_hamilton_does_without_logging(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`join="outer"` pads the shorter output with NaN, and logs nothing."""
    with caplog.at_level(logging.DEBUG):
        built = _run(fnloom.frame(join="outer"), ["prices", "shorter"])
    assert built["shorter"].isna().tolist() == [False, False, True]
    assert not caplog.records


def test_a_flattened_name_colliding_is_refused_naming_both() -> None:
    """A column named like another output, or another frame's column, is refused."""
    build = fnloom.frame()
    frame = pd.DataFrame({"p": [1, 2]})
    with pytest.raises(
        ValueError,
        match=r"^frame\(\) makes the column 'table\.p' twice: from output 'table\.p' "
        r"and from column 'p' of output 'table'$",
    ):
        build({"table.p": pd.Series([1, 2]), "table": frame})
    with pytest.raises(
        ValueError,
        match=r"^frame\(\) makes the column 'table\.p' twice: from column 'p' of "
        r"output 'table' and from output 'table\.p'$",
    ):
        build({"table": frame, "table.p": pd.Series([1, 2])})
    with pytest.raises(
        ValueError, match=r"from column 'p' of output 'a' and from output 'p'"
    ):
        fnloom.frame(flatten=False)({"a": frame, "p": 3})


def test_the_frame_class_itself_is_unchanged() -> None:
    """`with_result_builder(pandas.DataFrame)` builds by pandas' own rules (Q89)."""
    built = _run(pd.DataFrame, ["prices", "shorter"])
    assert built["shorter"].isna().tolist() == [False, False, True]


# polars


def test_polars_flattens_broadcasts_and_makes_one_row() -> None:
    """polars: frames flattened, a series one column, scalars broadcast."""
    build = fnloom.frame("polars")
    table = pl.DataFrame({"north": [1, 2], "south": [3, 4]})
    built = build(
        {"revenue": pl.Series("whatever", [1.5, 2.5]), "table": table, "rate": 0.2}
    )
    assert isinstance(built, pl.DataFrame)
    assert built.columns == ["revenue", "table.north", "table.south", "rate"]
    assert built.to_dict(as_series=False) == {
        "revenue": [1.5, 2.5],
        "table.north": [1, 2],
        "table.south": [3, 4],
        "rate": [0.2, 0.2],
    }
    assert build({"rate": 0.2, "name": "north"}).to_dicts() == [
        {"rate": 0.2, "name": "north"}
    ]
    assert build({"values": [1, 2], "more": (3, 4)}).columns == ["values", "more"]


def test_polars_refuses_unequal_lengths_and_outer() -> None:
    """Polars has no index: other lengths are refused, and so is `join="outer"`."""
    with pytest.raises(
        ValueError,
        match=r"^frame\('polars'\) needs every output of one length: "
        r"longer has 3 rows, shorter has 2 rows$",
    ):
        fnloom.frame("polars")(
            {"longer": pl.Series([1, 2, 3]), "shorter": pl.Series([1, 2])}
        )
    with pytest.raises(
        ValueError,
        match=r"^frame\('polars'\) takes join='strict' only: polars has no index "
        r"to align on$",
    ):
        fnloom.frame("polars", join="outer")


def test_polars_collision_is_refused() -> None:
    """A flattened polars name colliding with an output is refused naming both."""
    with pytest.raises(ValueError, match=r"column 't\.a' twice"):
        fnloom.frame("polars")({"t": pl.DataFrame({"a": [1]}), "t.a": [2]})


def test_polars_refuses_a_two_dimensional_value() -> None:
    """A pandas frame given to the polars builder is a `TypeError` naming it."""
    with pytest.raises(
        TypeError,
        match=r"^frame\('polars'\) can't make columns of output 'table', a DataFrame$",
    ):
        fnloom.frame("polars")({"table": pd.DataFrame({"a": [1]})})


def test_the_polars_frame_class_itself_is_unchanged() -> None:
    """`with_result_builder(polars.DataFrame)` builds by polars' own rules (Q89)."""
    module = make_module(
        "frames_polars",
        """
import polars as pl


def north() -> pl.Series:
    return pl.Series([1, 2])


def south() -> pl.Series:
    return pl.Series([3, 4])
""",
    )
    driver = fnloom.Builder().with_modules(module).with_result_builder(pl.DataFrame)
    built = driver.build().execute(["north", "south"])
    assert built.to_dict(as_series=False) == {"north": [1, 2], "south": [3, 4]}


# The builder itself


def test_bad_arguments_are_refused() -> None:
    """Each argument is checked when the builder is made, naming it."""
    with pytest.raises(
        ValueError,
        match=r"^frame\(\) library must be 'pandas' or 'polars', not 'arrow'$",
    ):
        fnloom.frame("arrow")  # ty: ignore[invalid-argument-type]  # the case under test
    with pytest.raises(
        ValueError, match=r"^frame\(\) join must be 'strict' or 'outer', not 'left'$"
    ):
        fnloom.frame(join="left")  # ty: ignore[invalid-argument-type]  # the case under test
    with pytest.raises(TypeError, match=r"^frame\(\) flatten must be a bool, not 1$"):
        fnloom.frame(flatten=1)  # ty: ignore[invalid-argument-type]  # the case under test
    with pytest.raises(
        ValueError, match=r"^frame\(\) separator must be a non-empty str, not ''$"
    ):
        fnloom.frame(separator="")
    with pytest.raises(TypeError, match=r"^frame\(\) separator must be a str, not 1$"):
        fnloom.frame(separator=1)  # ty: ignore[invalid-argument-type]  # the case under test
    with pytest.raises(TypeError, match=r"^frame\(\) takes from 0 to 1 positional"):
        fnloom.frame("pandas", False)  # ty: ignore[too-many-positional-arguments]  # noqa: FBT003  # the case under test


def test_pickles_copies_and_compares_by_its_settings() -> None:
    """The builder pickles, copies and rebuilds with the driver, settings and all."""
    build = fnloom.frame(separator="/", join="outer")
    assert pickle.loads(pickle.dumps(build)) == build  # noqa: S301  # our own bytes
    assert copy.deepcopy(build) == build
    assert build != fnloom.frame(separator="/")
    assert (
        repr(build)
        == "fnloom.frame('pandas', flatten=True, separator='/', join='outer')"
    )
    driver = fnloom.Builder().with_modules(_sales()).with_result_builder(build).build()
    assert driver.rebuild().execute(["regions"]).columns.tolist() == [
        "regions/north",
        "regions/south",
    ]


def test_a_second_result_builder_is_named() -> None:
    """Q24's refusal names the frame builder."""
    builder = fnloom.Builder().with_result_builder(fnloom.frame())
    with pytest.raises(
        ValueError,
        match=r"^a builder has one result builder: a FrameBuilder is set",
    ):
        builder.with_result_builder(len)


def test_the_async_driver_builds_a_frame() -> None:
    """`build_async` carries the builder."""
    builder = (
        fnloom.Builder().with_modules(_sales()).with_result_builder(fnloom.frame())
    )
    built = asyncio.run(builder.build_async().execute(["revenue", "rate"]))
    assert built["rate"].tolist() == [0.2, 0.2, 0.2]


def test_fnloom_imports_neither_library() -> None:
    """Importing fnloom loads neither pandas nor polars; `frame` loads only its own."""
    code = (
        "import sys, fnloom\n"
        "assert 'pandas' not in sys.modules and 'polars' not in sys.modules\n"
        "fnloom.frame('polars')\n"
        "assert 'polars' in sys.modules and 'pandas' not in sys.modules\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)  # noqa: S603  # our own interpreter


def test_a_missing_library_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    """A library that won't import is an `ImportError` naming it, from `frame`."""
    monkeypatch.setitem(sys.modules, "polars", None)
    with pytest.raises(
        ImportError, match=r"^frame\('polars'\) needs polars, which can't be imported"
    ):
        fnloom.frame("polars")


def test_strict_aligns_a_mapping_on_its_keys() -> None:
    """A dict aligns on its keys, so strict compares them as an index."""
    build = fnloom.frame()
    with pytest.raises(ValueError, match=r"labels differ at row 0: 'a' against 0"):
        build({"d": {"a": 1, "b": 2, "c": 3}, "s": pd.Series([1, 2, 3])})
    with pytest.raises(ValueError, match=r"labels differ at row 0: 'a' against 'b'"):
        build({"d": {"a": 1, "b": 2}, "e": {"b": 1, "c": 2}})
    assert build({"d": {0: 1, 1: 2}, "s": pd.Series([3, 4])}).shape == (2, 2)


def test_a_frame_without_columns_is_not_a_scalar() -> None:
    """A frame with rows but no columns keeps its length, as Hamilton's does."""
    with pytest.raises(ValueError, match=r"df has 2 rows, s has 3 rows"):
        fnloom.frame()({"df": pd.DataFrame(index=[0, 1]), "s": pd.Series([1, 2, 3])})


def test_a_frame_naming_a_column_twice_is_named() -> None:
    """Duplicate columns within one frame say so, rather than naming it twice."""
    with pytest.raises(
        ValueError,
        match=r"^frame\(\) can't make a column of output 'f': it has the column "
        r"'f\.a' twice$",
    ):
        fnloom.frame()({"f": pd.DataFrame([[1, 2]], columns=["a", "a"])})


def test_polars_scalars_keep_one_type_and_bad_values_are_named() -> None:
    """A broadcast scalar is typed as a one-row frame types it; refusals name it."""
    build = fnloom.frame("polars")
    one = build({"x": 2})
    many = build({"x": 2, "s": [1, 2]})
    assert one.schema["x"] == many.schema["x"]
    with pytest.raises(
        TypeError, match=r"^frame\('polars'\) can't make the column 't'"
    ):
        build({"t": pytest.importorskip("numpy").array(3), "s": [1, 2]})


def test_polars_one_row_refusals_name_the_column() -> None:
    """All scalars: a value polars refuses still names its column."""
    with pytest.raises(
        TypeError, match=r"^frame\('polars'\) can't make the column 't'"
    ):
        fnloom.frame("polars")({"t": pytest.importorskip("numpy").array(3)})
    assert fnloom.frame("polars")({"x": None, "y": 2}).to_dicts() == [
        {"x": None, "y": 2}
    ]


def test_strict_refuses_an_output_without_a_length() -> None:
    """A generator has no length to compare: a `TypeError` naming the output."""
    with pytest.raises(
        TypeError,
        match=r"^frame\(join='strict'\) can't compare output 'g', a generator: "
        r"it has no length; return a list or a series$",
    ):
        fnloom.frame()({"g": (i for i in range(2)), "s": pd.Series([1, 2])})


def test_pandas_refuses_a_polars_frame() -> None:
    """A polars frame given to the pandas builder is a `TypeError` naming it."""
    with pytest.raises(
        TypeError,
        match=r"^frame\('pandas'\) can't make columns of output 'table', a DataFrame$",
    ):
        fnloom.frame()({"table": pl.DataFrame({"a": [1]}), "s": pd.Series([1])})


def test_polars_refuses_a_lazy_frame() -> None:
    """A `LazyFrame` is refused, naming the output and saying to collect it."""
    with pytest.raises(
        TypeError,
        match=r"^frame\('polars'\) can't make columns of output 'lazy', a LazyFrame: "
        r"call \.collect\(\) on it first$",
    ):
        fnloom.frame("polars")({"lazy": pl.LazyFrame({"a": [1]}), "s": [1]})


def test_frame_builder_is_public() -> None:
    """`fnloom.FrameBuilder` is what `frame` returns, for annotations."""
    assert isinstance(fnloom.frame(), fnloom.FrameBuilder)
