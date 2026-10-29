"""Node wrappers: the chain's order, calls, checks, hooks, caching and carrying."""

import asyncio
import copy
import pickle
import threading
from typing import TYPE_CHECKING, cast, override

import pytest

import fnloom
import picklable_nodes
from helpers import make_module

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

CHECKED = """
from fnloom import check_output

@check_output(range=(0, 50))
def a(x: int) -> int:
    return x

def b(a: int) -> int:
    return a + 1
"""


def _builder(source: str = CHECKED, name: str = "wrapped") -> fnloom.Builder:
    return fnloom.Builder().with_modules(make_module(name, source))


def _passing(
    log: list[str], label: str
) -> Callable[[fnloom.NodeView, Callable[[], object], Mapping[str, object]], object]:
    def wrapper(
        node: fnloom.NodeView, call: Callable[[], object], inputs: Mapping[str, object]
    ) -> object:
        log.append(f"{label} in {node.name} {dict(inputs)}")
        value = call()
        log.append(f"{label} out {node.name}")
        return value

    return wrapper


def test_the_chain_runs_first_outermost_inside_the_hooks() -> None:
    """Hooks, wrapper 1, wrapper 2, the function and checks, then back (RF 5)."""
    log: list[str] = []

    class Logged(fnloom.Hook):
        @override
        def before_node(self, event: fnloom.NodeStart) -> None:
            log.append(f"before {event.node.name}")

        @override
        def after_node(self, event: fnloom.NodeEnd) -> None:
            log.append(f"after {event.node.name} {[c.name for c in event.checks]}")

    driver = (
        _builder()
        .with_hooks(Logged())
        .with_node_wrappers(_passing(log, "w1"), _passing(log, "w2"))
        .build()
    )
    assert driver.execute(["a"], inputs={"x": 1}) == {"a": 1}
    assert log == [
        "before a",
        "w1 in a {'x': 1}",
        "w2 in a {'x': 1}",
        "w2 out a",
        "w1 out a",
        "after a ['range']",
    ]


def test_cumulative() -> None:
    """A second call adds after the first."""
    log: list[str] = []
    builder = _builder().with_node_wrappers(_passing(log, "w1"))
    builder.with_node_wrappers(_passing(log, "w2")).build().execute(
        ["a"], inputs={"x": 1}
    )
    assert [entry.split()[0] for entry in log] == ["w1", "w2", "w2", "w1"]


def test_a_wrapper_may_call_the_node_any_number_of_times() -> None:
    """Twice runs the function twice; never returns the wrapper's value (KEEP)."""
    calls: list[int] = []
    module = make_module("counted", "def a(x: int) -> int:\n    return x\n")
    original = module.a

    def a(x: int) -> int:
        calls.append(x)
        return cast("int", original(x))

    def twice(node: object, call: Callable[[], object], inputs: object) -> object:
        del node, inputs
        call()
        return call()

    driver = fnloom.Builder().with_functions(a).with_node_wrappers(twice).build()
    assert driver.execute(["a"], inputs={"x": 3}) == {"a": 3}
    assert calls == [3, 3]

    def never(node: object, call: Callable[[], object], inputs: object) -> object:
        del node, call, inputs
        return 42

    skipped = _builder(name="skipped").with_node_wrappers(never).build()
    assert skipped.execute(["b"], inputs={"x": 1}) == {"b": 42}


def test_a_wrapper_sees_a_failed_check_and_may_raise() -> None:
    """Checks run inside the chain; a wrapper's own error fails the node."""
    seen: list[BaseException] = []

    def catching(node: object, call: Callable[[], object], inputs: object) -> object:
        del node, inputs
        try:
            return call()
        except fnloom.ValidationError as error:
            seen.append(error)
            raise

    driver = _builder().with_node_wrappers(catching).build()
    with pytest.raises(fnloom.ValidationError):
        driver.execute(["a"], inputs={"x": 99})
    assert len(seen) == 1

    def refusing(node: object, call: Callable[[], object], inputs: object) -> object:
        del node, call, inputs
        msg = "refused"
        raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="refused"):
        _builder(name="refusing").with_node_wrappers(refusing).build().execute(
            ["a"], inputs={"x": 1}
        )


def test_a_wrappers_value_is_type_checked_with_output_checks() -> None:
    """With `with_output_type_checks`, what the chain returns is checked."""

    def text(node: object, call: Callable[[], object], inputs: object) -> object:
        del node, call, inputs
        return "not an int"

    driver = (
        _builder(name="typed")
        .with_output_type_checks()
        .with_node_wrappers(text)
        .build()
    )
    with pytest.raises(TypeError, match="returned str, expected int"):
        driver.execute(["b"], inputs={"x": 1})


def test_wrappers_are_execution_only_for_the_cache() -> None:
    """A hit skips the chain; its value is stored and served to any driver."""
    store = fnloom.MemoryCache()
    module = make_module("cached_wrapped", "def a(x: int) -> int:\n    return x\n")
    ran: list[str] = []

    def never(node: object, call: Callable[[], object], inputs: object) -> object:
        del node, call, inputs
        ran.append("never")
        return 42

    wrapped = fnloom.Builder().with_modules(module).with_cache(store)
    assert wrapped.with_node_wrappers(never).build().execute(
        ["a"], inputs={"x": 1}
    ) == {"a": 42}
    assert wrapped.with_node_wrappers(never).build().execute(
        ["a"], inputs={"x": 1}
    ) == {"a": 42}
    assert ran == ["never"]
    plain = wrapped.build()
    assert plain.explain_cache(["a"], inputs={"x": 1})["a"].status == "hit"
    assert plain.execute(["a"], inputs={"x": 1}) == {"a": 42}


@pytest.mark.parametrize(
    ("wrapper", "match"),
    [
        (3, "is not callable"),
        (lambda node: node, r"can't take \(node, call, inputs\)"),
    ],
)
def test_wrappers_are_checked_when_attached(wrapper: object, match: str) -> None:
    """A TypeError from `with_node_wrappers`, naming the wrapper."""
    with pytest.raises(TypeError, match=match):
        fnloom.Builder().with_node_wrappers(wrapper)  # ty: ignore[invalid-argument-type]


def test_an_async_wrapper_is_refused() -> None:
    """Wrappers run synchronously."""

    async def later(node: object, call: object, inputs: object) -> object:
        del node, inputs
        return call

    with pytest.raises(TypeError, match="async def"):
        fnloom.Builder().with_node_wrappers(later)


def test_the_async_driver_wraps_sync_nodes_and_refuses_async_ones() -> None:
    """Sync nodes, in the executor; an async def node is refused at build."""
    log: list[str] = []
    driver = _builder(name="async_wrapped").with_node_wrappers(_passing(log, "w"))
    built = driver.build_async()
    assert asyncio.run(built.execute(["b"], inputs={"x": 1})) == {"b": 2}
    assert log[0] == "w in a {'x': 1}"
    async_source = "async def c(x: int) -> int:\n    return x\n"
    with pytest.raises(TypeError, match="async def: 'c'"):
        _builder(async_source, "async_node_wrapped").with_node_wrappers(
            _passing(log, "w")
        ).build_async()


def _module_wrapper(node: object, call: Callable[[], object], inputs: object) -> object:
    """A wrapper importable by name, so a driver using it pickles."""
    del node, inputs
    return call()


def test_wrappers_are_carried() -> None:
    """By pickling, `copy`, `rebuild()` and `build_async`."""
    builder = (
        fnloom.Builder()
        .with_modules(picklable_nodes)
        .with_node_wrappers(_module_wrapper)
    )
    driver = builder.build()
    for carried in (
        pickle.loads(pickle.dumps(driver)),  # noqa: S301  # our own bytes
        copy.copy(driver),
        copy.deepcopy(driver),
        driver.rebuild(),
    ):
        assert carried.execute(["total"], inputs={"a": 1, "b": 2}) == {"total": 4}
    assert builder._wrappers == (_module_wrapper,)  # noqa: SLF001  # the builder's own setting
    assert pickle.loads(pickle.dumps(builder))._wrappers == (_module_wrapper,)  # noqa: S301, SLF001  # our own bytes, the setting


def test_wrappers_share_a_driver_across_threads() -> None:
    """Each thread's runs go through the chain, with its own inputs."""
    seen: list[object] = []
    lock = threading.Lock()

    def recording(
        node: object, call: Callable[[], object], inputs: Mapping[str, object]
    ) -> object:
        del node
        with lock:
            seen.append(inputs.get("x"))
        return call()

    driver = _builder(name="threaded").with_node_wrappers(recording).build()
    results: list[object] = []

    def run(x: int) -> None:
        results.append(driver.execute(["a"], inputs={"x": x})["a"])

    threads = [threading.Thread(target=run, args=(x,)) for x in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == list(range(16))  # ty: ignore[invalid-argument-type]
    assert sorted(seen) == list(range(16))  # ty: ignore[invalid-argument-type]


FIELDS = """
from fnloom import check_output, extract_fields, pipe_output, step

def _inc(v: int) -> int:
    return v + 1

@check_output(range=(0, 100), field="p")
@extract_fields({"p": int, "q": int})
def pair(x: int) -> dict:
    return {"p": x, "q": x}

def total(p: int, q: int) -> int:
    return p + q

@pipe_output(step(_inc))
def piped(x: int) -> int:
    return x
"""


def test_fields_stay_fields_to_the_cache_with_wrappers_and_checks() -> None:
    """A field, checked or not, is never stored, wrappers or none."""
    for index, wrapped in enumerate((False, True)):
        builder = _builder(FIELDS, f"fields_cached_{index}").with_cache(
            fnloom.MemoryCache()
        )
        if wrapped:
            builder = builder.with_node_wrappers(_module_wrapper)
        driver = builder.build()
        driver.execute(["total"], inputs={"x": 1})
        outcomes = driver.explain_cache(["total"], inputs={"x": 1})
        assert outcomes["p"].status == "miss", (wrapped, outcomes["p"])
        assert outcomes["q"].status == "miss"
        assert outcomes["total"].status == "hit"


def test_wrappers_see_functions_steps_and_raw_nodes_only() -> None:
    """Not extracted fields, nor a `@pipe_output` node passing its value on."""
    seen: list[str] = []

    def recording(
        node: fnloom.NodeView, call: Callable[[], object], inputs: object
    ) -> object:
        del inputs
        seen.append(node.name)
        return call()

    driver = _builder(FIELDS, "seen_wrapped").with_node_wrappers(recording).build()
    driver.execute(["total", "piped"], inputs={"x": 1})
    assert sorted(seen) == ["pair", "piped.raw", "piped.with_inc", "total"]


def test_inputs_hold_what_the_node_reads() -> None:
    """By parameter name; `value()` bindings and unused defaults are left out."""
    source = """
from fnloom import inject, value

@inject(k=value(3))
def scaled(x: int, k: int, extra: int = 0) -> int:
    return x * k + extra
"""
    seen: list[dict[str, object]] = []

    def recording(
        node: object, call: Callable[[], object], inputs: Mapping[str, object]
    ) -> object:
        del node
        seen.append(dict(inputs))
        return call()

    driver = _builder(source, "inputs_wrapped").with_node_wrappers(recording).build()
    driver.execute(["scaled"], inputs={"x": 2})
    assert seen == [{"x": 2}]


def _doubling(node: object, call: Callable[[], object], inputs: object) -> object:
    """Changes the value, so a driver that dropped its wrappers would show it."""
    del node, inputs
    value = call()
    return value * 2 if isinstance(value, int) else value


def test_wrappers_that_change_values_are_carried() -> None:
    """Pickled, copied and rebuilt drivers still run them."""
    driver = (
        fnloom.Builder()
        .with_modules(picklable_nodes)
        .with_node_wrappers(_doubling)
        .build()
    )
    expected = {"total": ((1 * 2) * 2 + 2) * 2}
    for carried in (
        driver,
        pickle.loads(pickle.dumps(driver)),  # noqa: S301  # our own bytes
        copy.copy(driver),
        copy.deepcopy(driver),
        driver.rebuild(),
    ):
        assert carried.execute(["total"], inputs={"a": 1, "b": 2}) == expected
