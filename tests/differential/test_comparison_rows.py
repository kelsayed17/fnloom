"""The claims of `docs/from-hamilton.md`'s comparison rows that no other test proves.

Each test is marked with the register row its comparison row cites; `test_docs.py`
fails when a cited row has no `deliberate` or `keeps` test.
"""

import importlib.util
import inspect
import json
import logging
import sys
import tempfile
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


def _double(x: int) -> int:
    return x * 2


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


@pytest.mark.deliberate("Q147")
def test_q147_target_picks_one_extracted_output() -> None:
    """Hamilton's `target_` sets one field's behaviour; fnloom's `@cache` has none."""
    hamilton_module = make_module(
        "rows_target_h",
        HAMILTON_HEADER
        + """
@cache(behavior="recompute", target_="x")
@extract_fields({"x": int, "y": int})
def p(a: int) -> dict:
    return {"x": a, "y": a}
""",
    )
    hamilton = (
        hamilton_driver.Builder()
        .with_modules(hamilton_module)
        .with_cache(path=tempfile.mkdtemp())
        .build()
    )
    hamilton.execute(["x", "y"], inputs={"a": 1})
    behaviors = hamilton.cache.resolve_behaviors(hamilton.cache.last_run_id)
    assert {n: behaviors[n].name for n in ("p", "x", "y")} == {
        "p": "DEFAULT",
        "x": "RECOMPUTE",
        "y": "DEFAULT",
    }
    with pytest.raises(TypeError, match="target_"):
        fnloom.cache(behavior="recompute", target_="x")  # ty: ignore[unknown-argument]  # refused is the point


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


@pytest.mark.deliberate("Q148")
def test_q148_config_predicate_is_hamilton_only() -> None:
    """Hamilton's `@config(lambda cfg: ...)` picks; fnloom has no such form."""
    hamilton_module = make_module(
        "rows_lambda_h",
        HAMILTON_HEADER
        + """
@config(lambda cfg: cfg.get("r") == "eu", target_name="t")
def t__eu() -> int:
    return 1
""",
    )
    hamilton = (
        hamilton_driver.Builder()
        .with_modules(hamilton_module)
        .with_config({"r": "eu"})
        .build()
    )
    assert hamilton.execute(["t"]) == {"t": 1}
    with pytest.raises(TypeError, match="config"):
        fnloom.config(lambda cfg: cfg.get("r") == "eu")  # ty: ignore[too-many-positional-arguments]  # refused is the point


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


@pytest.mark.deliberate("Q140")
def test_q140_step_namespaces_are_hamilton_only() -> None:
    """Hamilton's step `namespace=` and `on_output=` work; fnloom refuses them."""
    hamilton_module = make_module(
        "rows_steps_h",
        HAMILTON_HEADER
        + """
def _inc(x: int) -> int:
    return x + 1

@pipe_input(step(_inc), namespace=None)
def f(a: int) -> int:
    return a

@pipe_input(step(_inc), namespace="ns")
def g(a: int) -> int:
    return a

@pipe_input(step(_inc).named("bump", namespace="zz"))
def h(a: int) -> int:
    return a

@pipe_output(step(_inc), on_output="x")
@extract_fields({"x": int, "y": int})
def k(a: int) -> dict:
    return {"x": a, "y": a}
""",
    )
    hamilton = hamilton_driver.Builder().with_modules(hamilton_module).build()
    names = {n.name for n in hamilton.list_available_variables()}
    assert {"with_inc", "ns.with_inc", "zz.bump", "x.with_inc"} <= names
    assert hamilton.execute(["x", "y"], inputs={"a": 1}) == {"x": 2, "y": 1}

    with pytest.raises(TypeError, match="namespace"):
        fnloom.pipe_input(fnloom.step(_double), namespace="ns")  # ty: ignore[unknown-argument]  # refused is the point
    with pytest.raises(TypeError, match="namespace"):
        fnloom.step(_double).named("bump", namespace="zz")  # ty: ignore[unknown-argument]  # refused is the point
    with pytest.raises(TypeError, match="on_output"):
        fnloom.pipe_output(fnloom.step(_double), on_output="x")  # ty: ignore[unknown-argument]  # refused is the point


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
def test_q139_graphviz_kwargs_style_hamiltons_digraph() -> None:
    """Hamilton passes `graphviz_kwargs` to the `Digraph`; fnloom's `draw` has none."""
    module = load_fixture("chain")
    hamilton = hamilton_driver.Builder().with_modules(module).build()
    digraph = hamilton.display_all_functions(
        graphviz_kwargs={"graph_attr": {"bgcolor": "red"}}
    )
    assert digraph is not None
    assert digraph.kwargs["graph_attr"]["bgcolor"] == "red"
    driver = fnloom.build(module)
    for name in ("graphviz_kwargs", "custom_style_function", "show_schema"):
        assert name not in inspect.signature(driver.draw).parameters


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


@pytest.mark.deliberate("Q143")
def test_q143_driver_lists_beat_the_decorator_in_hamilton() -> None:
    """Hamilton's `with_cache` lists win over `@cache`; fnloom takes no lists yet."""
    hamilton_module = make_module("rows_lists_h", HAMILTON_HEADER + CACHED)
    hamilton = (
        hamilton_driver.Builder()
        .with_modules(hamilton_module)
        .with_cache(path=tempfile.mkdtemp(), disable=["b"], default=["c"])
        .build()
    )
    hamilton.execute(["d"], inputs={"a": 1})
    behaviors = hamilton.cache.resolve_behaviors(hamilton.cache.last_run_id)
    assert (behaviors["b"].name, behaviors["c"].name) == ("DISABLE", "DEFAULT")
    with pytest.raises(ValueError, match="Multiple caching behaviors"):
        hamilton_driver.Builder().with_modules(hamilton_module).with_cache(
            path=tempfile.mkdtemp(), disable=["b"], recompute=["b"]
        ).build().execute(["d"], inputs={"a": 1})
    with pytest.raises(TypeError, match="disable"):
        fnloom.Builder().with_cache(MemoryCache(), disable=["b"])  # ty: ignore[unknown-argument]  # refused is the point


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
