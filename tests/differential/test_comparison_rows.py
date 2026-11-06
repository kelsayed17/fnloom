"""The claims of `docs/from-hamilton.md`'s comparison rows that no other test proves.

Each test is marked with the register row its comparison row cites; `test_docs.py`
fails when a cited row has no `deliberate` or `keeps` test.
"""

import importlib.util
import inspect
import json
import logging
import shutil
import sys
import xml.etree.ElementTree as ET
from typing import TYPE_CHECKING, Any, override

import pandas as pd
import pytest
from hamilton import base, htypes, lifecycle
from hamilton import driver as hamilton_driver
from hamilton import function_modifiers as hamilton_modifiers
from hamilton import graph as hamilton_graph
from hamilton.lifecycle import api as hamilton_api
from hamilton.lifecycle import default as hamilton_default
from harness import load_fixture

import fnloom
from fnloom import MemoryCache
from helpers import make_module

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path
    from types import ModuleType

HAMILTON_HEADER = "from hamilton.function_modifiers import *\n"
FNLOOM_HEADER = "from fnloom import *\n"

CACHED = """
@cache(behavior="recompute")
def b(a: int) -> int:
    print("b")
    return a + 1

@cache(behavior="disable")
def c(b: int) -> int:
    print("c")
    return b * 2

def d(c: int) -> int:
    print("d")
    return c + 3
"""


def _module_file(tmp_path: Path, name: str, source: str) -> ModuleType:
    """`source` written as `name.py` and imported, so Hamilton can read its code."""
    (tmp_path / f"{name}.py").write_text(source, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, tmp_path / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# ---- Decorators -------------------------------------------------------------------


@pytest.mark.keeps("Q152")
def test_q152_cache_recompute_and_disable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`recompute` and `disable` run on every run, in both libraries."""
    hamilton_module = _module_file(tmp_path, "rows_cached_h", HAMILTON_HEADER + CACHED)
    hamilton = (
        hamilton_driver.Builder()
        .with_modules(hamilton_module)
        .with_cache(path=tmp_path / "hamilton")
        .build()
    )
    ran: list[list[str]] = []
    for _ in range(2):
        assert hamilton.execute(["d"], inputs={"a": 1}) == {"d": 7}
        ran.append(capsys.readouterr().out.split())
    behaviors = hamilton.cache.resolve_behaviors(hamilton.cache.last_run_id)
    assert (behaviors["b"].name, behaviors["c"].name) == ("RECOMPUTE", "DISABLE")
    assert "b" in ran[1]
    assert "c" in ran[1]

    fnloom_module = make_module("rows_cached_f", FNLOOM_HEADER + CACHED)
    driver = (
        fnloom.Builder().with_modules(fnloom_module).with_cache(MemoryCache()).build()
    )
    ours: list[list[str]] = []
    for _ in range(2):
        assert driver.execute(["d"], inputs={"a": 1}) == {"d": 7}
        ours.append(capsys.readouterr().out.split())
    assert ours == ran
    outcomes = driver.explain_cache(["d"], inputs={"a": 1})
    assert outcomes["b"].status == "miss"
    assert outcomes["c"].status == "uncacheable"


TARGETED = """
@cache(behavior="{producer}", {target})
@extract_fields({{"totals": dict, "rate": float}})
def report(region: str) -> dict:
    print("report")
    return {{"totals": {{"north": len(region)}}, "rate": 0.2}}

def taxed(totals: dict) -> float:
    print("taxed")
    return totals["north"] * 0.2
"""


def _second_run(
    execute: Callable[..., object],
    capsys: pytest.CaptureFixture[str],
    outputs: list[str],
) -> list[str]:
    """The functions the second of two equal runs ran, sorted."""
    execute(outputs, inputs={"region": "north"})
    capsys.readouterr()
    execute(outputs, inputs={"region": "north"})
    return sorted(capsys.readouterr().out.split())


@pytest.mark.deliberate("Q147")
@pytest.mark.parametrize(
    ("producer", "field", "hamilton_ran", "fnloom_ran"),
    [
        # Hamilton reruns only the field's getter, on the stored producer: the
        # producer's code never runs again, so nothing is read afresh.
        ("default", "recompute", [], ["report"]),
        # Alike: the field is never versioned, so its reader runs.
        ("default", "disable", ["taxed"], ["taxed"]),
        # Hamilton runs a disabled producer to get any field; fnloom serves the
        # stored field alone.
        ("disable", "default", ["report"], []),
    ],
)
def test_q147_target_against_fields(  # noqa: PLR0913, PLR0917  # one row of the grid
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    producer: str,
    field: str,
    hamilton_ran: list[str],
    fnloom_ran: list[str],
) -> None:
    """Which functions run again under `target_`, and under `fields=`."""
    if producer == "default":
        targeted = TARGETED.format(producer=field, target='target_="totals"')
    else:
        targeted = TARGETED.format(producer=producer, target='target_="report"')
    name = f"rows_target_{producer}_{field}"
    hamilton_module = _module_file(tmp_path, f"{name}_h", HAMILTON_HEADER + targeted)
    hamilton = (
        hamilton_driver.Builder()
        .with_modules(hamilton_module)
        .with_cache(path=tmp_path / "hamilton")
        .build()
    )
    outputs = ["taxed"] if field == "disable" else ["totals"]
    assert _second_run(hamilton.execute, capsys, outputs) == hamilton_ran
    behaviors = hamilton.cache.resolve_behaviors(hamilton.cache.last_run_id)
    if producer == "default":
        assert behaviors["totals"].name == field.upper()
        assert behaviors["rate"].name == behaviors["report"].name == "DEFAULT"
    fields = f'fields={{"totals": "{field}"}}'
    source = TARGETED.format(producer=producer, target=fields)
    module = make_module(f"{name}_f", FNLOOM_HEADER + source)
    driver = fnloom.Builder().with_modules(module).with_cache(MemoryCache()).build()
    assert _second_run(driver.execute, capsys, outputs) == fnloom_ran
    with pytest.raises(TypeError, match="target_"):
        fnloom.cache(behavior="recompute", target_="x")  # ty: ignore[unknown-argument]  # fnloom names it fields=


@pytest.mark.keeps("Q153")
def test_q153_config_when_family_picks_the_same_variant() -> None:
    """The four `when*` forms choose alike, the variants named `foo__variant`."""
    body = """
@config.when(r="eu")
def tax__eu() -> float:
    return 0.2

@config.when_not(r="eu")
def tax__other() -> float:
    return 0.1

@config.when_in(r=["eu", "us"])
def fee__known() -> int:
    return 1

@config.when_not_in(r=["eu", "us"])
def fee__unknown() -> int:
    return 2
"""
    hamilton_module = make_module("rows_when_h", HAMILTON_HEADER + body)
    fnloom_module = make_module("rows_when_f", FNLOOM_HEADER + body)
    for region in ("eu", "us", "jp"):
        config = {"r": region}
        hamilton = (
            hamilton_driver.Builder()
            .with_modules(hamilton_module)
            .with_config(config)
            .build()
        )
        driver = (
            fnloom.Builder().with_modules(fnloom_module).with_config(config).build()
        )
        assert driver.execute(["tax", "fee"]) == hamilton.execute(["tax", "fee"])


@pytest.mark.deliberate("Q154")
def test_q154_config_default_is_fnloom_only() -> None:
    """Hamilton's `config` has no `default`; fnloom's is used when nothing matches."""
    assert not hasattr(hamilton_modifiers.config, "default")
    hamilton_module = make_module(
        "rows_default_h",
        HAMILTON_HEADER
        + """
@config.when(r="eu")
def tax__eu() -> float:
    return 0.2
""",
    )
    hamilton = (
        hamilton_driver.Builder()
        .with_modules(hamilton_module)
        .with_config({"r": "us"})
        .build()
    )
    assert "tax" not in {n.name for n in hamilton.list_available_variables()}
    module = make_module(
        "rows_default_f",
        FNLOOM_HEADER
        + """
@config.when(r="eu")
def tax__eu() -> float:
    return 0.2

@config.default
def tax__any() -> float:
    return 0.1
""",
    )
    for region, expected in (("eu", 0.2), ("us", 0.1)):
        driver = (
            fnloom.Builder().with_modules(module).with_config({"r": region}).build()
        )
        assert driver.execute(["tax"]) == {"tax": expected}


_Q148_HAMILTON = """
@config(
    lambda cfg: cfg.get("r") == "eu" and cfg.get("y", 2020) >= 2020, target_name="t"
)
def t__eu() -> int:
    return 1

@config(lambda cfg: cfg.get("r") == "us", target_name="t")
def t__us() -> int:
    return 2

@config(
    lambda cfg: cfg.get("r") != "us"
    and not (cfg.get("r") == "eu" and cfg.get("y", 2020) >= 2020),
    target_name="t",
)
def t__other() -> int:
    return 3
"""

_Q148_FNLOOM = """
@config.matches(lambda r, y=2020: r == "eu" and y >= 2020)
def t__eu() -> int:
    return 1

@config.matches(lambda r: r == "us")
def t__us() -> int:
    return 2

@config.default
def t__other() -> int:
    return 3
"""


@pytest.mark.deliberate("Q148")
def test_q148_config_predicate_picks_the_same_variant() -> None:
    """`@config(lambda cfg: ...)` and `@config.matches` pick alike over a config grid.

    Hamilton's predicate reads the whole config, so a variant for "no other
    matched" must be written out; fnloom's reads its keys from its signature, a
    missing required key never matches, and `@config.default` is the fallback.
    """
    hamilton_module = make_module("rows_q148_h", HAMILTON_HEADER + _Q148_HAMILTON)
    fnloom_module = make_module("rows_q148_f", FNLOOM_HEADER + _Q148_FNLOOM)
    grid: list[dict[str, object]] = [
        {"r": region, "y": year}
        for region in ("eu", "us", "uk")
        for year in (2010, 2020, 2030)
    ]
    grid += [{"r": "eu"}, {"r": "us"}, {"r": "uk"}]
    for cfg in grid:
        hamilton = (
            hamilton_driver.Builder()
            .with_modules(hamilton_module)
            .with_config(cfg)
            .build()
        )
        ours = fnloom.Builder().with_modules(fnloom_module).with_config(cfg).build()
        assert ours.execute(["t"]) == hamilton.execute(["t"]), cfg
    # fnloom: a predicate returning no bool is refused at build, naming it.
    truthy = make_module(
        "rows_q148_truthy",
        FNLOOM_HEADER
        + "@config.matches(lambda r: r)\ndef t__any() -> int:\n    return 1\n",
    )
    with pytest.raises(TypeError, match=r"must return a bool, got str"):
        fnloom.Builder().with_modules(truthy).with_config({"r": "eu"}).build()


@pytest.mark.deliberate("Q103")
def test_q103_a_check_below_extract_fields_fails_in_hamilton() -> None:
    """Hamilton refuses `@check_output` below `@extract_fields`; fnloom checks it."""
    body = """
@extract_fields({"x": int})
@check_output(range=(0, 10))
def p(a: int) -> dict:
    return {"x": a}
"""
    hamilton_module = make_module("rows_check_h", HAMILTON_HEADER + body)
    with pytest.raises(ValueError, match="Could not resolve validators"):
        hamilton_driver.Builder().with_modules(hamilton_module).build()
    fnloom_module = make_module(
        "rows_check_f",
        FNLOOM_HEADER
        + """
@extract_fields({"x": int})
@check_output(range=(0, 10), field="x")
def p(a: int) -> dict:
    return {"x": a}
""",
    )
    driver = fnloom.build(fnloom_module)
    assert driver.execute(["x"], inputs={"a": 5}) == {"x": 5}
    with pytest.raises(fnloom.ValidationError):
        driver.execute(["x"], inputs={"a": 50})


@pytest.mark.deliberate("Q155")
def test_q155_pipe_flow_and_collapse() -> None:
    """`pipe` drops `collapse`, `flow` is absent, `collapse=True` raises; fnloom: none.

    Hamilton's `pipe` is a `pipe_input` subclass, not a deprecated alias.
    """
    assert issubclass(hamilton_modifiers.pipe, hamilton_modifiers.pipe_input)
    hamilton_modifiers.pipe(hamilton_modifiers.step(abs), collapse=True)
    assert not hasattr(hamilton_modifiers, "flow")
    with pytest.raises(NotImplementedError, match="Collapsing"):
        hamilton_modifiers.pipe_input(hamilton_modifiers.step(abs), collapse=True)
    assert not hasattr(fnloom, "pipe")
    assert not hasattr(fnloom, "flow")
    assert "collapse" not in inspect.signature(fnloom.pipe_input).parameters


_Q140_STEPS = """
from {package} import extract_fields, pipe_input, pipe_output, step

def _inc(v: int) -> int:
    return v + 1

def _dbl(v: int) -> int:
    return v * 2

def _neg(v: int) -> int:
    return -v

@pipe_input(step(_inc), step(_dbl).named("twice"))
def by_default(a: int) -> int:
    return a

@pipe_input(step(_inc), namespace=None)
def bare(a: int) -> int:
    return a

@pipe_input(step(_inc), step(_dbl), namespace="sales")
def shared(a: int) -> int:
    return a

@pipe_input(step(_inc).named("bump", namespace="taxes"), step(_dbl).named("then"))
def renamed(a: int) -> int:
    return a

@pipe_output(step(_neg), namespace=None)
def out_bare(a: int) -> int:
    return a

@pipe_output(step(_inc), namespace="regions")
def out_shared(a: int) -> int:
    return a

@pipe_output(step(_inc), step(_dbl), on_output=("x", "y"))
@extract_fields({{"x": int, "y": int, "z": int}})
def fields(a: int) -> dict:
    return {{"x": a, "y": a * 10, "z": a * 100}}

@pipe_input(
    step(_inc).named("with_inc", namespace="counted"),
    step(_inc),
    step(_dbl).named("x2"),
    step(_inc),
)
def counting(a: int) -> int:
    return a
"""


@pytest.mark.deliberate("Q140")
def test_q140_step_namespaces_and_on_output_name_alike(tmp_path: Path) -> None:
    """Step `namespace=`, `.named(n, namespace=)`, `on_output=`: same names, values."""
    hamilton_module = _module_file(
        tmp_path,
        "rows_steps_h",
        _Q140_STEPS.format(package="hamilton.function_modifiers"),
    )
    hamilton = hamilton_driver.Builder().with_modules(hamilton_module).build()
    expected = {
        v.name for v in hamilton.list_available_variables() if not v.is_external_input
    }
    assert {
        "by_default.with_inc",
        "by_default.twice",
        "with_inc",
        "sales.with_inc",
        "sales.with_dbl",
        "taxes.bump",
        "renamed.then",
        "with_neg",
        "out_bare.raw",
        "regions.with_inc",
        "x.with_inc",
        "y.with_dbl",
        # A named step counts too: the unnamed ones after it are `_1`, `_2`.
        "counted.with_inc",
        "counting.with_inc_1",
        "counting.x2",
        "counting.with_inc_2",
    } <= expected
    assert "z.with_inc" not in expected
    fnloom_module = make_module("rows_steps_f", _Q140_STEPS.format(package="fnloom"))
    driver = fnloom.build(fnloom_module)
    assert set(driver.node_names()) - set(driver.inputs()) == expected
    outputs = sorted(expected)
    assert driver.execute(outputs, inputs={"a": 3}) == hamilton.execute(
        outputs, inputs={"a": 3}
    )


# ---- What changes in your code ------------------------------------------------------


@pytest.mark.deliberate("Q3")
def test_q3_hamilton_plans_every_execute_and_fnloom_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hamilton walks the graph again on each `execute`; a prepared plan doesn't."""
    calls: list[str] = []
    walk = hamilton_graph.FunctionGraph.get_upstream_nodes

    def counted(self: object, *args: object, **kwargs: object) -> object:
        calls.append("walk")
        return walk(self, *args, **kwargs)  # ty: ignore[invalid-argument-type]  # passed through

    monkeypatch.setattr(hamilton_graph.FunctionGraph, "get_upstream_nodes", counted)
    module = load_fixture("chain")
    hamilton = hamilton_driver.Builder().with_modules(module).build()
    calls.clear()
    for _ in range(2):
        hamilton.execute(["c"], inputs={"a": 1})
    assert calls == ["walk", "walk"]

    driver = fnloom.build(module)
    plan = driver.prepare(["c"], inputs={"a": 1})
    assert driver.prepare(["c"], inputs={"a": 2}) is plan
    assert plan.run(inputs={"a": 1}) == {"c": 4}


@pytest.mark.keeps("Q156")
def test_q156_public_type_helpers_agree() -> None:
    """`htypes.check_input_type` and `fnloom.value_matches` agree on plain types."""
    cases: list[tuple[type, object]] = [
        (int, 3),
        (int, "x"),
        (str, "x"),
        (list, [1]),
        (dict, {}),
        (float, 1.5),
    ]
    for annotation, value in cases:
        assert fnloom.value_matches(annotation, value) == htypes.check_input_type(
            annotation, value
        ), (annotation, value)


@pytest.mark.deliberate("Q89")
@pytest.mark.deliberate("Q144")
def test_q89_hamilton_logs_mixed_index_types_and_frame_refuses_them(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Hamilton only logs a warning when index types differ; `frame()` refuses."""
    x = pd.Series([1, 2], index=[0, 1])
    y = pd.Series([3, 4], index=pd.to_datetime(["2020-01-01", "2020-01-02"]))
    with caplog.at_level(logging.WARNING, logger="hamilton.base"):
        frame = base.PandasDataFrameResult().build_result(x=x, y=y)  # ty: ignore[invalid-argument-type]  # Hamilton hints dicts; it takes any output
    assert len(frame) == len(x) + len(y)  # the indexes never align
    assert any("Multiple index types" in r.getMessage() for r in caplog.records)
    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(
            ValueError,
            match=r"x and y both have 2 rows, but their labels differ at row 0",
        ):
            fnloom.frame()({"x": x, "y": y})
        # Outer: Hamilton's frame, and nothing logged.
        assert fnloom.frame(join="outer")({"x": x, "y": y}).equals(frame)
    assert not caplog.records


def _hamilton_frame(outputs: dict[str, object]) -> pd.DataFrame:
    """What Hamilton's `PandasDataFrameResult` builds from `outputs`."""
    return base.PandasDataFrameResult().build_result(**outputs)  # ty: ignore[invalid-argument-type]  # Hamilton hints dicts; it takes any output


# Each case: outputs of scalars, series and frames, in request order.
_FRAME_CASES: dict[str, dict[str, object]] = {
    "scalars": {"rate": 0.2, "region": "north", "count": 3},
    "series": {
        "prices": pd.Series([1.0, 2.0, 3.0]),
        "quantities": pd.Series([1, 2, 3]),
    },
    "series_misaligned": {
        "longer": pd.Series([1, 2, 3]),
        "shorter": pd.Series([10, 20], index=[1, 7]),
    },
    "series_and_scalar": {"prices": pd.Series([1.0, 2.0]), "rate": 0.2},
    "frames_and_series": {
        "regions": pd.DataFrame({"north": [1, 2], "south": [3, 4]}),
        "prices": pd.Series([1.5, 2.5]),
        "sales": pd.DataFrame({"units": [5, 6]}, index=[1, 2]),
        "rate": 0.2,
    },
}


@pytest.mark.keeps("Q144")
@pytest.mark.parametrize("case", list(_FRAME_CASES))
def test_q144_frame_outer_is_pandas_dataframe_result(case: str) -> None:
    """`frame("pandas", join="outer")` builds Hamilton's frame from the same outputs."""
    outputs = _FRAME_CASES[case]
    hamilton = _hamilton_frame(outputs)
    ours = fnloom.frame("pandas", join="outer")(outputs)
    assert ours.equals(hamilton)
    assert list(ours.columns) == list(hamilton.columns)


@pytest.mark.keeps("Q144")
def test_q144_frame_through_both_drivers() -> None:
    """Both drivers' frame builders give equal frames for one request."""
    module = make_module(
        "rows_q144",
        """
import pandas as pd


def regions() -> pd.DataFrame:
    return pd.DataFrame({"north": [1, 2, 3], "south": [4, 5, 6]})


def revenue() -> pd.Series:
    return pd.Series([1.0, 4.0, 9.0])


def rate() -> float:
    return 0.2
""",
    )
    request: list[Any] = ["revenue", "regions", "rate"]  # Hamilton's list is invariant
    hamilton = (
        hamilton_driver.Builder()
        .with_modules(module)
        .with_adapters(base.PandasDataFrameResult())
        .build()
        .execute(request)
    )
    for join in ("strict", "outer"):
        ours = (
            fnloom.Builder()
            .with_modules(module)
            .with_result_builder(fnloom.frame(join=join))
            .build()
            .execute(request)
        )
        assert ours.equals(hamilton)
        assert list(ours.columns) == [
            "revenue",
            "regions.north",
            "regions.south",
            "rate",
        ]


@pytest.mark.deliberate("Q144")
def test_q144_frame_differs_where_hamilton_is_silent() -> None:
    """A lone frame is flattened, and a name collision is refused in either order."""
    table = pd.DataFrame({"p": [1, 2]})
    # Hamilton returns a lone frame bare; fnloom names it as it would beside others.
    assert list(_hamilton_frame({"table": table}).columns) == ["p"]
    assert list(fnloom.frame()({"table": table}).columns) == ["table.p"]
    # Hamilton refuses a series named like a column already flattened, but a frame
    # flattened after the series silently replaces it.
    with pytest.raises(ValueError, match=r"Series table\.p already exists"):
        _hamilton_frame({"table": table, "table.p": pd.Series([9, 9])})
    replaced = _hamilton_frame({"table.p": pd.Series([9, 9]), "table": table})
    assert replaced["table.p"].tolist() == [1, 2]
    cases: list[dict[str, object]] = [
        {"table": table, "table.p": pd.Series([9, 9])},
        {"table.p": pd.Series([9, 9]), "table": table},
    ]
    for outputs in cases:
        with pytest.raises(ValueError, match=r"makes the column 'table\.p' twice"):
            fnloom.frame()(outputs)


@pytest.mark.deliberate("Q144")
def test_q144_polars_frame_and_the_polars_class() -> None:
    """Hamilton has no polars builder in base; fnloom has `frame("polars")` too."""
    pl = pytest.importorskip("polars")
    assert not hasattr(base, "PolarsDataFrameResult")
    outputs = {"regions": pl.DataFrame({"north": [1, 2]}), "rate": 0.2}
    built = fnloom.frame("polars")(outputs)
    assert built.to_dict(as_series=False) == {
        "regions.north": [1, 2],
        "rate": [0.2, 0.2],
    }
    with pytest.raises(ValueError, match=r"polars has no index to align on"):
        fnloom.frame("polars", join="outer")
    # The class itself still builds by polars' own rules (Q89).
    module = make_module(
        "rows_q144_polars",
        "import polars as pl\n"
        "def north() -> pl.Series:\n"
        "    return pl.Series([1, 2])\n",
    )
    plain = fnloom.Builder().with_modules(module).with_result_builder(pl.DataFrame)
    assert plain.build().execute(["north"]).to_dict(as_series=False) == {
        "north": [1, 2]
    }


@pytest.mark.deliberate("Q19")
def test_q19_hamilton_raw_execute_is_positional() -> None:
    """Hamilton's `raw_execute` takes overrides second and a `display_graph` flag."""
    params = inspect.signature(hamilton_driver.Driver.raw_execute).parameters
    assert list(params)[:5] == [
        "self",
        "final_vars",
        "overrides",
        "display_graph",
        "inputs",
    ]
    assert all(p.kind is p.POSITIONAL_OR_KEYWORD for p in params.values())
    fnloom_params = inspect.signature(fnloom.Driver.raw_execute).parameters
    assert "display_graph" not in fnloom_params
    assert fnloom_params["inputs"].kind is inspect.Parameter.KEYWORD_ONLY


@pytest.mark.deliberate("Q142")
def test_q142_println_and_print_nodes_report_the_same_nodes() -> None:
    """`PrintLn` and `PrintNodes` report the same nodes; verbosity 2 adds values."""
    module = load_fixture("chain")
    for verbosity in (1, 2):
        printed: list[str] = []
        println = lifecycle.PrintLn(verbosity=verbosity, print_fn=printed.append)
        hamilton = hamilton_driver.Builder().with_modules(module).with_adapters(println)
        hamilton.build().execute(["c"], inputs={"a": 1})
        executing = [line for line in printed if line.startswith("Executing node")]
        assert [line.split()[2].rstrip(".") for line in executing] == ["b", "c"]
        finished = [line for line in printed if line.startswith("Finished")]
        assert [line.split()[3] for line in finished] == ["b", "c"]
        assert all("Status: Success" in line for line in finished)
        assert any("Result:" in line for line in printed) == (verbosity > 1)
        ours: list[str] = []
        hook = fnloom.PrintNodes(verbosity, print_fn=ours.append)
        fnloom.Builder().with_modules(module).with_hooks(hook).build().execute(
            ["c"], inputs={"a": 1}
        )
        reported = [line.split("'")[1] for line in ours if line.startswith("node ")]
        assert reported == [line.split()[3] for line in finished]
        # Which nodes carry inputs and a result: Hamilton's `Inputs:` and `Result:`
        # against the indented lines after each of fnloom's node lines.
        with_inputs = [
            line.split()[2].rstrip(".") for line in executing if "Inputs:" in line
        ]
        with_result = [line.split()[3] for line in finished if "Result:" in line]
        ours_inputs: list[str] = []
        ours_result: list[str] = []
        node = ""
        for line in ours:
            if line.startswith("node "):
                node = line.split("'")[1]
            elif line.startswith("  inputs:"):
                ours_inputs.append(node)
            elif line.startswith("  result:"):
                ours_result.append(node)
        assert ours_inputs == with_inputs
        assert ours_result == with_result
    assert not hasattr(fnloom, "PrintLn")


@pytest.mark.deliberate("Q88")
def test_q88_hamilton_logs_a_support_banner(caplog: pytest.LogCaptureFixture) -> None:
    """A failing node logs Hamilton's help banner at ERROR; fnloom logs nothing."""
    body = """
def b(a: int) -> int:
    raise ValueError("boom")
"""
    hamilton_module = make_module("rows_fail_h", body)
    with caplog.at_level(logging.ERROR), pytest.raises(ValueError, match="boom"):
        hamilton_driver.Builder().with_modules(hamilton_module).build().execute(
            ["b"], inputs={"a": 1}
        )
    assert any("Need help with Hamilton?" in r.getMessage() for r in caplog.records)
    caplog.clear()
    fnloom_module = make_module("rows_fail_f", body)
    with caplog.at_level(logging.DEBUG), pytest.raises(ValueError, match="boom"):
        fnloom.build(fnloom_module).execute(["b"], inputs={"a": 1})
    assert caplog.records == []


@pytest.mark.deliberate("Q145")
def test_q145_graph_construction_hook_against_build_end() -> None:
    """Hamilton's build hook and `after_build` see the same nodes and config."""
    seen: list[tuple[list[str], dict[str, object]]] = []

    class Seen(hamilton_api.GraphConstructionHook):
        @override
        def run_after_graph_construction(self, *, graph, config, **future_kwargs):  # noqa: ANN001, ANN003, ANN202  # Hamilton's signature
            del future_kwargs
            seen.append((sorted(n.name for n in graph.nodes), dict(config)))

    module = load_fixture("chain")
    hamilton_driver.Builder().with_modules(module).with_config({"k": 1}).with_adapters(
        Seen()
    ).build()
    assert seen == [(["a", "b", "c", "k"], {"k": 1})]
    got: list[fnloom.BuildEnd] = []

    class Mine(fnloom.Hook):
        @override
        def after_build(self, event: fnloom.BuildEnd) -> None:
            got.append(event)

    fnloom.Builder().with_modules(module).with_config({"k": 1}).with_hooks(
        Mine()
    ).build()
    (event,) = got
    # Hamilton lists the config key `k` as a node; fnloom's views are the graph's.
    assert sorted(event.nodes) == ["a", "b", "c"]
    assert dict(event.config) == {"k": 1}


@pytest.mark.deliberate("Q157")
def test_q157_graceful_errors_and_task_hooks_are_hamilton_only() -> None:
    """`GracefulErrorAdapter` skips a failure's dependents; fnloom fails the run."""
    body = """
def b(a: int) -> int:
    raise ValueError("no")

def c(b: int) -> int:
    return b

def d(a: int) -> int:
    return a
"""
    hamilton_module = make_module("rows_grace_h", body)
    graceful = hamilton_default.GracefulErrorAdapter(
        error_to_catch=ValueError, sentinel_value=None
    )
    hamilton = (
        hamilton_driver.Builder().with_modules(hamilton_module).with_adapters(graceful)
    )
    assert hamilton.build().execute(["c", "d"], inputs={"a": 1}) == {"c": None, "d": 1}
    fnloom_module = make_module("rows_grace_f", body)
    with pytest.raises(ValueError, match="no"):
        fnloom.build(fnloom_module).execute(["c", "d"], inputs={"a": 1})
    assert hasattr(lifecycle, "TaskExecutionHook")
    assert not [name for name in fnloom.__all__ if "Task" in name]


@pytest.mark.deliberate("Q139")
@pytest.mark.usefixtures("fake_graphviz")
def test_q139_drawing_style() -> None:
    """Both style each function node and hide inputs; fnloom's style sees a `NodeView`.

    Hamilton calls `custom_style_function` once per function node with a
    `HamiltonNode` and a class name; fnloom's `style` sees the same nodes (and
    the inputs, drawn as nodes of their own) with their `NodeView`.
    `hide_inputs` leaves the same function nodes in both. A bad colour is a
    `ValueError` in fnloom, where Hamilton passes it to Graphviz as written.
    """
    module = load_fixture("diamond")
    hamilton = hamilton_driver.Builder().with_modules(module).build()
    styled: list[str] = []

    def custom(*, node: object, node_class: str) -> tuple[dict[str, str], None, None]:
        del node_class
        styled.append(getattr(node, "name", ""))
        return {"fillcolor": "redd"}, None, None

    digraph = hamilton.display_all_functions(
        custom_style_function=custom,
        hide_inputs=True,
        graphviz_kwargs={"graph_attr": {"bgcolor": "red"}},
    )
    assert digraph is not None
    assert digraph.kwargs["graph_attr"]["bgcolor"] == "red"
    assert all(attrs["fillcolor"] == "redd" for _, _, attrs in digraph.nodes)
    hamilton_drawn = sorted(name for name, _, _ in digraph.nodes)

    driver = fnloom.build(module)
    seen: list[str] = []

    def style(view: fnloom.NodeView, drawn: fnloom.DrawnNode) -> fnloom.Style | None:
        assert view.name == drawn.name
        if not view.is_input:
            seen.append(view.name)
        return fnloom.Style(fill="red")

    drawing = driver.draw(hide_inputs=True).styled(
        graph=fnloom.Style(fill="red"), style=style
    )
    assert sorted(seen) == sorted(styled)
    assert sorted(n.name for n in drawing.nodes) == hamilton_drawn
    with pytest.raises(ValueError, match=r"node 'a'.*fill 'redd' is not a colour"):
        driver.draw().styled(style=lambda *_: fnloom.Style(fill="redd"))


@pytest.mark.deliberate("Q146")
def test_q146_an_svg_without_graphviz(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Without Graphviz Hamilton writes nothing; fnloom's builtin engine writes the SVG.

    Only `engine=` chooses it: the default still needs `dot`, so a file never
    changes with the machine.
    """
    if importlib.util.find_spec("graphviz") is not None:
        pytest.skip("the graphviz package is installed")
    module = load_fixture("diamond")
    hamilton = hamilton_driver.Builder().with_modules(module).build()
    with caplog.at_level(logging.ERROR):
        assert hamilton.display_all_functions(str(tmp_path / "h.svg")) is None
    assert not (tmp_path / "h.svg").exists()
    monkeypatch.setattr(shutil, "which", lambda _: None)
    drawing = fnloom.build(module).draw()
    written = drawing.render(tmp_path / "f.svg", engine="builtin")
    titles = [
        t.text
        for t in ET.parse(written).iter("{http://www.w3.org/2000/svg}title")  # noqa: S314  # our own file
    ]  # our own file
    assert {"a", "root", "left", "right", "join"} <= set(titles)
    with pytest.raises(
        FileNotFoundError, match=r"brew install graphviz|apt-get|winget"
    ):
        drawing.render(tmp_path / "g.svg")


@pytest.mark.deliberate("Q95")
def test_q95_hamilton_exports_an_input_with_its_consumers_source() -> None:
    """Hamilton's export gives input `a` the source of the function reading it."""
    module = load_fixture("chain")
    hamilton = hamilton_driver.Builder().with_modules(module).build()
    nodes = json.loads(hamilton.export_execution(["c"], inputs={"a": 1}))["nodes"]
    a = next(n for n in nodes if n["name"] == "a")
    assert a["source"].startswith("def b(")
    exported = fnloom.build(module).export(["c"], inputs={"a": 1})
    ours = next(n for n in exported["nodes"] if n["name"] == "a")
    assert ours["source"] is None
    assert ours["consumers"] == ["b"]


@pytest.mark.deliberate("Q97")
def test_q97_hamiltons_cli_needs_typer_and_reads_config_from_a_context_file() -> None:
    """Hamilton's CLI needs `typer`; its build takes only a context file for config."""
    pytest.importorskip("hamilton.cli.commands")
    if importlib.util.find_spec("typer") is None:
        with pytest.raises(ModuleNotFoundError, match="typer"):
            import hamilton.cli.__main__  # noqa: F401, PLC0415
    from hamilton.cli import commands  # noqa: PLC0415

    assert list(inspect.signature(commands.build).parameters) == [
        "modules",
        "context_path",
    ]


_Q112_REGIONAL = """
@config.when(region="eu")
def rate__eu() -> float:
    return 0.2

@config.when(region="us")
def rate__us() -> float:
    return 0.1

def tax(rate: float, sales: float) -> float:
    return rate * sales
"""

_Q112_SUBDAGS = """
from hamilton.function_modifiers import subdag
import rows_q112_regional_h as regional

@subdag(regional, config={"region": "eu"})
def eu(tax: float) -> float:
    return tax

@subdag(regional, config={"region": "us"})
def us(tax: float) -> float:
    return tax
"""


@pytest.mark.deliberate("Q112")
def test_q112_namespace_config_against_subdag_config() -> None:
    """A namespace's `config=` gives the values `@subdag(config=)` gives."""
    make_module("rows_q112_regional_h", HAMILTON_HEADER + _Q112_REGIONAL)
    hamilton_module = make_module("rows_q112_subdags_h", _Q112_SUBDAGS)
    hamilton = hamilton_driver.Builder().with_modules(hamilton_module).build()
    expected = hamilton.execute(
        ["eu", "us", "eu.tax", "us.tax"], inputs={"sales": 10.0}
    )
    regional = make_module("rows_q112_regional_f", FNLOOM_HEADER + _Q112_REGIONAL)
    ours = (
        fnloom.Builder()
        .with_modules(regional, namespace="eu", config={"region": "eu"})
        .with_modules(regional, namespace="us", config={"region": "us"})
        .build()
    )
    assert ours.execute(["eu.tax", "us.tax"], inputs={"sales": 10.0}) == {
        "eu.tax": expected["eu.tax"],
        "us.tax": expected["us.tax"],
    }
    assert (expected["eu"], expected["us"]) == (2.0, 1.0)
    # The same variant is picked in each copy, by name.
    picked = {
        name: getattr(ours.nodes[name].origin, "__name__", None)
        for name in ("eu.rate", "us.rate")
    }
    assert picked == {"eu.rate": "rate__eu", "us.rate": "rate__us"}
    # fnloom: the driver's config stays its own; the overlay is in the export.
    assert ours.config == {}
    assert ours.export()["namespace_config"] == {
        "eu": {"region": "'eu'"},
        "us": {"region": "'us'"},
    }


_Q150_SALES = """
def price() -> float:
    return 10.0

def total(price: float, quantity: int) -> float:
    return price * quantity

def report(total: float) -> float:
    return total + 1
"""

_Q150_DISCOUNTED = """
def total(price: float, quantity: int) -> float:
    return price * quantity * 0.9
"""


@pytest.mark.deliberate("Q150")
def test_q150_replace_against_allow_module_overrides() -> None:
    """With one name colliding, `replace=` gives what `allow_module_overrides()` does.

    Hamilton's override is silent and covers every collision; fnloom's names
    the node, and a typo fails the build.
    """
    sales = make_module("rows_q150_sales", _Q150_SALES)
    discounted = make_module("rows_q150_discounted", _Q150_DISCOUNTED)
    hamilton = (
        hamilton_driver.Builder()
        .with_modules(sales, discounted)
        .allow_module_overrides()
        .build()
    )
    expected = hamilton.execute(["total", "report"], inputs={"quantity": 2})
    ours = (
        fnloom.Builder()
        .with_modules(sales)
        .with_modules(discounted, replace=("total",))
        .build()
    )
    assert ours.execute(["total", "report"], inputs={"quantity": 2}) == expected
    assert sorted(v.name for v in hamilton.list_available_variables()) == (
        ours.node_names()
    )
    assert ours.nodes["total"].replaced == "rows_q150_sales"
    # Hamilton accepts the override with no collision at all; fnloom refuses a typo.
    assert hamilton_driver.Builder().with_modules(
        sales
    ).allow_module_overrides().build().execute(["price"]) == {"price": 10.0}
    with pytest.raises(ValueError, match="replace names 'totl'"):
        fnloom.Builder().with_modules(sales).with_modules(
            discounted, replace=("totl",)
        ).build()


_Q149_LOOSE = """
def code() -> str:
    return "7"

def count(code: int) -> int:
    return code

def label(code: float) -> str:
    return str(code)
"""


@pytest.mark.deliberate("Q149")
def test_q149_loose_edges_against_no_edge_and_input_type_checking() -> None:
    """Edges off give Hamilton's values; off per node, the other edges stay checked."""
    module = make_module("rows_q149", _Q149_LOOSE)
    hamilton = (
        hamilton_driver.Builder()
        .with_modules(module)
        .with_adapters(lifecycle.NoEdgeAndInputTypeChecking())
        .build()
    )
    expected = hamilton.execute(["count", "label"])
    ours = fnloom.Builder().with_modules(module).without_edge_type_checks().build()
    assert ours.execute(["count", "label"]) == expected
    # Hamilton: without the adapter, the edge fails at build, as fnloom's does.
    with pytest.raises(ValueError, match="count"):
        hamilton_driver.Builder().with_modules(module).build()
    # fnloom: off for one consumer only; the other edge is still checked.
    with pytest.raises(ValueError, match=r"label\.code requires float"):
        fnloom.Builder().with_modules(module).without_edge_type_checks("count").build()
    assert ours.export()["edge_checks"] == "off"


# ---- Caching -----------------------------------------------------------------------


@pytest.mark.keeps("Q158")
def test_q158_turning_the_cache_on(tmp_path: Path) -> None:
    """A second run is served from the cache in both libraries."""
    source = """
def b(a: int) -> int:
    return a + 1

def c(b: int) -> int:
    return b * 2
"""
    module = _module_file(tmp_path, "rows_cache_on", source)
    hamilton = (
        hamilton_driver.Builder()
        .with_modules(module)
        .with_cache(path=tmp_path / "hamilton")
        .build()
    )
    for _ in range(2):
        assert hamilton.execute(["c"], inputs={"a": 1}) == {"c": 4}
    hits = [
        e
        for events in hamilton.cache.logs(
            level="debug", run_id=hamilton.cache.last_run_id
        ).values()
        for e in events
        if e.actor == "result_store" and e.msg == "hit"
    ]
    assert "c" in {e.node_name for e in hits}
    driver = (
        fnloom.Builder()
        .with_modules(module)
        .with_cache(fnloom.DiskCache(tmp_path / "f"))
    )
    built = driver.build()
    built.execute(["c"], inputs={"a": 1})
    assert built.explain_cache(["c"], inputs={"a": 1})["c"].status == "hit"


LISTED = """
def plain(a: int) -> int:
    return a + 1

@cache(behavior="default")
def kept(a: int) -> int:
    return a + 2

@cache(behavior="recompute")
def fresh(a: int) -> int:
    return a + 3

@cache(behavior="disable")
def off(a: int) -> int:
    return a + 4

@cache(format="pickle")
def stored(a: int) -> int:
    return a + 5
"""

LISTED_NODES = ["fresh", "kept", "off", "plain", "stored"]


def _hamilton_behaviors(
    module: ModuleType, path: Path, lists: Mapping[str, object]
) -> dict[str, str]:
    """Each node's behavior, as Hamilton resolves it under these lists."""
    hamilton = (
        hamilton_driver.Builder()
        .with_modules(module)
        .with_cache(path=path, **lists)  # ty: ignore[invalid-argument-type]  # the lists as given
        .build()
    )
    hamilton.execute(list(LISTED_NODES), inputs={"a": 1})
    behaviors = hamilton.cache.resolve_behaviors(hamilton.cache.last_run_id)
    return {name: behaviors[name].name.lower() for name in LISTED_NODES}


def _fnloom_behaviors(
    module: ModuleType, lists: Mapping[str, object]
) -> dict[str, str]:
    """Each node's behavior, read from the policy `explain_cache` names."""
    driver = (
        fnloom.Builder()
        .with_modules(module)
        .with_cache(MemoryCache(), **lists)  # ty: ignore[invalid-argument-type]  # the lists as given
        .build()
    )
    found: dict[str, str] = {}
    for name, outcome in driver.explain_cache(LISTED_NODES, inputs={"a": 1}).items():
        reason = outcome.reason or ""
        found[name] = next(
            (b for b in ("recompute", "disable") if f"policy {b}" in reason), "default"
        )
    return found


def _listed_grid() -> list[dict[str, object]]:
    """Every list naming each node, alone and beside a `default_behavior`."""
    grid: list[dict[str, object]] = [{}]
    for behavior in ("default", "recompute", "disable"):
        grid.append({"default_behavior": behavior})
        for name in LISTED_NODES:
            grid.append({behavior: [name]})
            grid.append({behavior: [name], "default_behavior": "recompute"})
    return grid


@pytest.mark.keeps("Q143")
def test_q143_each_list_and_decorator_resolve_alike(tmp_path: Path) -> None:
    """Each node's behavior, for each list and `@cache(behavior=)`, as in Hamilton.

    A list naming a node wins over its `@cache`, which wins over
    `default_behavior`; `@cache(format=...)` alone sets no behavior.
    """
    hamilton_module = _module_file(tmp_path, "rows_listed_h", HAMILTON_HEADER + LISTED)
    module = make_module("rows_listed_f", FNLOOM_HEADER + LISTED)
    for at, lists in enumerate(_listed_grid()):
        theirs = _hamilton_behaviors(hamilton_module, tmp_path / f"h{at}", lists)
        assert _fnloom_behaviors(module, lists) == theirs, lists


@pytest.mark.deliberate("Q143")
def test_q143_true_is_a_default_not_a_name(tmp_path: Path) -> None:
    """Hamilton expands `True` to every name, so it beats `@cache`; fnloom's doesn't.

    So `recompute=True` leaves `@cache(behavior="disable")` disabled, and
    `default=True` beside `disable=["fresh"]` is no conflict.
    """
    hamilton_module = _module_file(tmp_path, "rows_true_h", HAMILTON_HEADER + LISTED)
    module = make_module("rows_true_f", FNLOOM_HEADER + LISTED)
    every = {"recompute": True}
    assert _hamilton_behaviors(hamilton_module, tmp_path / "h", every) == (
        dict.fromkeys(LISTED_NODES, "recompute")
    )
    assert _fnloom_behaviors(module, every) == {
        "fresh": "recompute",
        "kept": "default",
        "off": "disable",
        "plain": "recompute",
        "stored": "recompute",
    }
    beside = {"default": True, "disable": ["fresh"]}
    with pytest.raises(ValueError, match="Multiple caching behaviors"):
        _hamilton_behaviors(hamilton_module, tmp_path / "h2", beside)
    assert _fnloom_behaviors(module, beside)["fresh"] == "disable"


@pytest.mark.deliberate("Q143")
def test_q143_unknown_names_are_refused(tmp_path: Path) -> None:
    """Hamilton ignores a name that is no node; fnloom refuses it at build."""
    hamilton_module = _module_file(tmp_path, "rows_typo_h", HAMILTON_HEADER + LISTED)
    typo = {"recompute": ["kep"]}
    assert _hamilton_behaviors(hamilton_module, tmp_path / "h", typo) == (
        _hamilton_behaviors(hamilton_module, tmp_path / "h2", {})
    )
    module = make_module("rows_typo_f", FNLOOM_HEADER + LISTED)
    with pytest.raises(ValueError, match="did you mean 'kept'"):
        _fnloom_behaviors(module, typo)
    twice = {"disable": ["kept"], "recompute": ["kept"]}
    with pytest.raises(ValueError, match="Multiple caching behaviors"):
        _hamilton_behaviors(hamilton_module, tmp_path / "h3", twice)
    with pytest.raises(ValueError, match="'kept' is in recompute and disable"):
        _fnloom_behaviors(module, twice)


@pytest.mark.deliberate("Q159")
def test_q159_hamiltons_log_says_hit_on_a_fresh_cache(tmp_path: Path) -> None:
    """On an empty cache Hamilton logs `get_cache_key: hit`; fnloom says `miss`."""
    source = """
def b(a: int) -> int:
    return a + 1
"""
    module = _module_file(tmp_path, "rows_cache_log", source)
    hamilton = (
        hamilton_driver.Builder()
        .with_modules(module)
        .with_cache(path=tmp_path / "hamilton")
        .build()
    )
    hamilton.execute(["b"], inputs={"a": 1})
    events = hamilton.cache.logs(level="debug", run_id=hamilton.cache.last_run_id)["b"]
    assert any(e.event_type.value == "get_cache_key" and e.msg == "hit" for e in events)
    driver = fnloom.Builder().with_modules(module).with_cache(MemoryCache()).build()
    assert driver.explain_cache(["b"], inputs={"a": 1})["b"].status == "miss"
