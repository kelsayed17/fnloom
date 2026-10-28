"""Pickling and copying a `Driver`, a `PreparedPlan`, a `Builder` and the stores."""

import copy
import pickle
import sys
import types
from concurrent.futures import ProcessPoolExecutor
from typing import TYPE_CHECKING, Any, cast

import pytest

import fnloom
import picklable_config_nodes
import picklable_functions
import picklable_nodes
from fnloom import Builder, DiskCache, Driver, MemoryCache, PreparedPlan, build

if TYPE_CHECKING:
    from pathlib import Path

INPUTS = {"a": 3, "b": 1}
EXPECTED = {"total": 7, "doubled": 6}


def _round_trip[T](value: T) -> T:
    return cast("T", pickle.loads(pickle.dumps(value)))  # noqa: S301  # our own pickle


@pytest.mark.parametrize("copier", [_round_trip, copy.deepcopy])
def test_a_driver_is_rebuilt_from_its_modules(copier: object) -> None:
    """A pickled or copied driver is a new driver over the same modules."""
    driver = build(picklable_nodes)
    again = copier(driver)  # ty: ignore[call-non-callable]  # each copier is callable
    assert isinstance(again, Driver)
    assert again is not driver
    assert again.node_names() == driver.node_names()
    assert again.execute(["total", "doubled"], inputs=INPUTS) == EXPECTED


def test_a_pickled_driver_keeps_the_type_check_opt_out() -> None:
    """A driver built without input checks still skips them after loading."""
    checked = _round_trip(build(picklable_nodes))
    unchecked = _round_trip(
        Builder().with_modules(picklable_nodes).without_input_type_checks().build()
    )
    inputs = {"a": 3.5, "b": 1}
    with pytest.raises(ValueError, match="a: expected int, got float"):
        checked.execute(["total"], inputs=inputs)
    assert unchecked.execute(["total"], inputs=inputs) == {"total": 8.0}


@pytest.mark.parametrize("copier", [_round_trip, copy.deepcopy, copy.copy])
def test_a_pickled_driver_keeps_its_config(copier: object) -> None:
    """The config travels with the driver, so the same variants are selected again."""
    driver = (
        Builder()
        .with_config({"mode": "b"})
        .with_modules(picklable_config_nodes)
        .build()
    )
    again = copier(driver)  # ty: ignore[call-non-callable]  # each copier is callable
    assert isinstance(again, Driver)
    assert dict(again.config) == {"mode": "b"}
    assert again.execute(["scale"], inputs={"a": 2}) == {"scale": 20}
    builder = _round_trip(Builder().with_config({"mode": "a"}))
    assert dict(builder.with_modules(picklable_config_nodes).build().config) == {
        "mode": "a"
    }


def test_a_pickled_driver_reopens_its_disk_cache(tmp_path: Path) -> None:
    """A disk cache reopens its directory: the loaded driver hits what was stored."""
    store = DiskCache(tmp_path / "cache", max_bytes=10_000_000)
    driver = Builder().with_modules(picklable_nodes).with_cache(store).build()
    driver.execute(["total"], inputs=INPUTS)
    again = _round_trip(driver)
    explained = again.explain_cache(["total"], inputs=INPUTS)
    assert {name: outcome.status for name, outcome in explained.items()} == {
        "doubled": "hit",
        "total": "hit",
    }


def test_a_memory_cache_comes_back_empty_with_its_bounds() -> None:
    """A memory cache's entries stay in its process; its bounds travel."""
    store = MemoryCache(max_entries=7, max_bytes=1_000)
    driver = Builder().with_modules(picklable_nodes).with_cache(store).build()
    driver.execute(["total"], inputs=INPUTS)
    for copied in (_round_trip(store), copy.deepcopy(store)):
        assert isinstance(copied, MemoryCache)
        assert copied is not store
        # Neither the entries nor the bounds have a public reader.
        assert not copied._entries  # noqa: SLF001  # see above
        assert (copied._max_entries, copied._max_bytes) == (7, 1_000)  # noqa: SLF001  # see above
    again = _round_trip(driver)
    explained = again.explain_cache(["total"], inputs=INPUTS)
    assert {outcome.status for outcome in explained.values()} == {"miss"}


def test_a_prepared_plan_is_prepared_again_on_its_rebuilt_driver() -> None:
    """A plan loads as the same request prepared on a rebuilt driver."""
    plan = build(picklable_nodes).prepare(["total", "doubled"], inputs=INPUTS)
    again = _round_trip(plan)
    assert isinstance(again, PreparedPlan)
    assert again.run(inputs=INPUTS) == EXPECTED
    with pytest.raises(ValueError, match="inputs"):
        again.run(inputs={"a": 3})


def test_a_plan_pickled_with_its_driver_shares_the_rebuilt_driver() -> None:
    """Pickled together, a driver and its plans load as one driver and its plans."""
    driver = build(picklable_nodes)
    first = driver.prepare(["total"], inputs=INPUTS)
    second = driver.prepare(["doubled"], inputs={"a": 0})
    loaded_driver, loaded_first, loaded_second = _round_trip((driver, first, second))
    # The rebuilt driver's own plan cache answers both requests with the loaded plans.
    assert loaded_driver.prepare(["total"], inputs=INPUTS) is loaded_first
    assert loaded_driver.prepare(["doubled"], inputs={"a": 0}) is loaded_second


def test_a_builder_pickles_as_module_names() -> None:
    """A builder loads with the same modules, imported again."""
    builder = Builder().with_modules(picklable_nodes).without_input_type_checks()
    again = _round_trip(builder)
    assert again.build().execute(["total"], inputs=INPUTS) == {"total": 7}


def test_a_module_not_importable_by_its_name_is_refused() -> None:
    """A hand-made module, not in `sys.modules`, cannot be loaded again."""
    handmade = types.ModuleType("pickling_handmade")
    exec("def f(a: int) -> int:\n    return a\n", handmade.__dict__)  # noqa: S102  # building a fixture module
    driver = build(handmade)
    with pytest.raises(TypeError, match="module 'pickling_handmade' is not importable"):
        pickle.dumps(driver)
    with pytest.raises(TypeError, match="module 'pickling_handmade' is not importable"):
        copy.deepcopy(driver)


def test_a_module_shadowed_in_sys_modules_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A module whose name now imports another module is refused, not swapped."""
    driver = build(picklable_nodes)
    monkeypatch.setitem(
        sys.modules, "picklable_nodes", types.ModuleType("picklable_nodes")
    )
    with pytest.raises(TypeError, match="module 'picklable_nodes' is not importable"):
        pickle.dumps(driver)


def test_a_shallow_copy_shares_the_graph_without_rebuilding() -> None:
    """`copy.copy` makes a new driver over the same graph, even one made directly."""
    driver = build(picklable_nodes)
    copied = copy.copy(driver)
    assert copied is not driver
    assert copied._graph is driver._graph  # noqa: SLF001  # the graph has no public reader
    assert copied.execute(["total"], inputs=INPUTS) == {"total": 7}
    assert _round_trip(copied).execute(["total"], inputs=INPUTS) == {"total": 7}
    direct = Driver(fnloom._core.Graph([], {}))  # noqa: SLF001  # a native graph on purpose
    assert copy.copy(direct).node_names() == []
    plan = direct.prepare([])
    assert copy.copy(plan) is plan


def test_a_driver_made_from_a_native_graph_is_refused() -> None:
    """A driver made without a builder has no modules to rebuild from."""
    driver = Driver(fnloom._core.Graph([], {}))  # noqa: SLF001  # a native graph on purpose
    with pytest.raises(TypeError, match="made from a native graph"):
        pickle.dumps(driver)
    plan = driver.prepare([])
    with pytest.raises(TypeError, match="made from a native graph"):
        pickle.dumps(plan)


def _execute(driver: Driver, inputs: dict[str, int]) -> dict[str, object]:
    return driver.execute(["total", "doubled"], inputs=inputs)


def _run(plan: PreparedPlan, inputs: dict[str, int]) -> dict[str, object]:
    return plan.run(inputs=inputs)


def test_drivers_and_plans_run_in_a_process_pool(tmp_path: Path) -> None:
    """Workers rebuild a driver, or a plan, and share its disk cache with the parent."""
    store = DiskCache(tmp_path / "cache")
    driver = Builder().with_modules(picklable_nodes).with_cache(store).build()
    plan = driver.prepare(["total", "doubled"], inputs=INPUTS)
    many = [{"a": n, "b": 1} for n in range(4)]
    with ProcessPoolExecutor(max_workers=2) as pool:
        by_driver = list(pool.map(_execute, [driver] * len(many), many))
        by_plan = list(pool.map(_run, [plan] * len(many), many))
    expected = [{"total": 2 * n + 1, "doubled": 2 * n} for n in range(4)]
    assert by_driver == expected
    assert by_plan == expected
    # The workers stored their results in the shared directory.
    explained = driver.explain_cache(["total"], inputs={"a": 3, "b": 1})
    assert {outcome.status for outcome in explained.values()} == {"hit"}


def _as_text(value: object) -> bytes:
    return str(value).encode()


def _from_text(data: bytes) -> object:
    return data.decode()


def test_a_pickled_disk_cache_keeps_its_formats(tmp_path: Path) -> None:
    """A `DiskCache`'s formats come back with it, and still read its entries."""
    text = fnloom.Format("text", _as_text, _from_text)
    store = DiskCache(tmp_path, formats=[text])
    store.put(b"\x01" * 32, "hello", format=text)
    again = _round_trip(store)
    assert again.formats == store.formats
    assert again.get(b"\x01" * 32, format=again.formats["text"]) == "hello"


def test_settings_survive_pickling() -> None:
    """Strict overrides, declared inputs and the override opt-out are carried."""
    builder = (
        Builder()
        .with_modules(picklable_nodes)
        .with_strict_overrides()
        .with_declared_inputs("a")
        .with_declared_inputs("b")
    )
    for again in (_round_trip(builder), copy.deepcopy(builder)):
        driver = again.build()
        with pytest.raises(ValueError, match="override of input 'a' refused"):
            driver.execute(["total"], inputs={"b": 1}, overrides={"a": 1})
        undeclared = again.with_declared_inputs("zzz")
        with pytest.raises(ValueError, match="declared name 'zzz'"):
            undeclared.build()
    # The built drivers carry them too, through a driver's own pickling and copy.
    for driver in (_round_trip(builder.build()), copy.deepcopy(builder.build())):
        with pytest.raises(ValueError, match="override of input 'a' refused"):
            driver.execute(["total"], inputs={"b": 1}, overrides={"a": 1})
    async_again = _round_trip(builder.build_async())
    with pytest.raises(ValueError, match="refused"):
        async_again.prepare(["total"], inputs={"b": 1}, overrides={"a": 1})
    assert copy.copy(builder.build()).validate(
        ["total"], inputs={"b": 1}, overrides={"a": 1}
    )


def test_override_opt_out_survives_pickling() -> None:
    """`without_override_type_checks` is kept by a pickled builder and driver."""
    builder = Builder().with_modules(picklable_nodes).without_override_type_checks()
    for again in (_round_trip(builder), copy.deepcopy(builder)):
        driver = again.build()
        assert driver.execute(["total"], inputs=INPUTS, overrides={"doubled": 2.5})
        with pytest.raises(ValueError, match="a: expected int, got str"):
            driver.execute(["total"], inputs={"a": "s", "b": 1})
    loaded = _round_trip(builder.build())
    assert loaded.execute(["total"], inputs=INPUTS, overrides={"doubled": 2.5})
    plain = _round_trip(Builder().with_modules(picklable_nodes).build())
    with pytest.raises(ValueError, match="doubled: expected"):
        plain.execute(["total"], inputs=INPUTS, overrides={"doubled": 2.5})


def _function_builder() -> Builder:
    """A builder of functions alone, plain, decorated, a static method and specs."""
    return (
        Builder()
        .with_functions(picklable_functions.doubled, picklable_functions.total)
        .with_functions(
            picklable_functions.Holder.offset,
            fnloom.node(
                picklable_functions.scaled,
                name="tenfold",
                bind={"value": fnloom.source("total"), "factor": fnloom.value(10)},
            ),
        )
        .with_strict_overrides()
    )


def test_pickling_by_qualified_name() -> None:
    """A builder of functions pickles them by module and qualified name, not code."""
    builder = _function_builder()
    data = pickle.dumps(builder)
    # References, not code: the functions' names are in the pickle, their code is not.
    assert b"picklable_functions" in data
    assert picklable_functions.scaled.__code__.co_code not in data
    for again in (_round_trip(builder), copy.deepcopy(builder)):
        driver = again.build()
        assert driver.modules == ()
        assert driver.nodes["tenfold"].origin is picklable_functions.scaled
        assert driver.nodes["offset"].origin is picklable_functions.Holder.offset
        assert dict(driver.nodes["total"].tags) == {"kind": "sum"}
        assert driver.execute(["tenfold", "offset"], inputs=INPUTS) == {
            "tenfold": 70,
            "offset": 8,
        }
        # The other settings came along.
        with pytest.raises(ValueError, match="override of input 'a' refused"):
            driver.execute(["total"], inputs={"b": 1}, overrides={"a": 1})
    for driver in (
        _round_trip(builder.build()),
        copy.deepcopy(builder.build()),
        copy.copy(builder.build()),
    ):
        assert driver.execute(["tenfold"], inputs=INPUTS) == {"tenfold": 70}
    async_again = _round_trip(builder.build_async())
    assert async_again.node_names() == builder.build().node_names()
    # A spec pickles alone, as its parts.
    spec = _round_trip(fnloom.node(picklable_functions.doubled, name="d"))
    assert spec.fn is picklable_functions.doubled
    assert spec.name == "d"
    assert dict(spec.bind) == {}


def test_a_local_function_is_refused_by_name() -> None:
    """A function not importable by its qualified name is a `TypeError` naming it."""

    def local(a: int) -> int:
        return a

    square = cast("Any", lambda a: a * a)
    square.__annotations__ = {"a": int, "return": int}
    builder = (
        Builder()
        .with_functions(picklable_functions.doubled, local)
        .with_functions(fnloom.node(square, name="square"))
    )
    driver = builder.build()
    expected = (
        "cannot pickle: "
        "function 'test_pickling.test_a_local_function_is_refused_by_name.<locals>"
        ".<lambda>' is not importable by its qualified name, so it cannot be loaded "
        "again; "
        "function 'test_pickling.test_a_local_function_is_refused_by_name.<locals>"
        ".local' is not importable by its qualified name, so it cannot be loaded again"
    )
    for target in (builder, driver):
        with pytest.raises(TypeError) as raised:
            pickle.dumps(target)
        assert str(raised.value) == expected
        with pytest.raises(TypeError, match="is not importable by its qualified name"):
            copy.deepcopy(target)
    with pytest.raises(TypeError, match=r"^cannot pickle: function .*\.local' is not"):
        pickle.dumps(fnloom.node(local, name="x"))
    # A module and a function together: both named, sorted.
    handmade = types.ModuleType("pickling_handmade_functions")
    exec("def f(a: int) -> int:\n    return a\n", handmade.__dict__)  # noqa: S102  # building a fixture module
    with pytest.raises(TypeError) as both:
        pickle.dumps(Builder().with_modules(handmade).with_functions(local))
    assert str(both.value).startswith("cannot pickle: function ")
    assert "; module 'pickling_handmade_functions' is not importable" in str(both.value)


def test_a_function_shadowed_in_its_module_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A function whose qualified name now finds another object is refused."""
    builder = Builder().with_functions(picklable_functions.doubled)
    monkeypatch.setattr(picklable_functions, "doubled", lambda a: a)
    with pytest.raises(
        TypeError, match=r"'picklable_functions\.doubled' is not importable"
    ):
        pickle.dumps(builder)


def _execute_tenfold(driver: Driver, inputs: dict[str, int]) -> dict[str, object]:
    return driver.execute(["tenfold"], inputs=inputs)


def test_a_driver_of_functions_runs_in_a_process_pool() -> None:
    """Workers import the functions again by name and rebuild the driver."""
    driver = _function_builder().build()
    many = [{"a": n, "b": 1} for n in range(3)]
    with ProcessPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(_execute_tenfold, [driver] * len(many), many))
    assert results == [{"tenfold": (2 * n + 1) * 10} for n in range(3)]
