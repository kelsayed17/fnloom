"""Output type checks (Q87) and custom value checks (Q21), on both drivers."""

import asyncio
import copy
import pickle
import typing
from typing import TYPE_CHECKING, Any, Literal, cast

import pytest

import fnloom
from fnloom import NodeEnd, RunEnd
from fnloom._types import Unresolved
from helpers import make_module
from test_hooks import Recorder

if TYPE_CHECKING:
    from collections.abc import Mapping
    from types import ModuleType

# Each way a node runs: the sync driver, and the async one with each `sync_nodes`.
DRIVERS = ["sync", "async-thread", "async-inline"]
ASYNC_DRIVERS = ["async-thread", "async-inline"]

SHAPES = """
import collections.abc
from collections.abc import Callable, Iterator
from typing import Any, NewType

UserId = NewType("UserId", int)


def wrong() -> int:
    return "s"


def anything() -> Any:
    return object()


def plain_object() -> object:
    return object()


def pair() -> tuple[int, str]:
    return (1, "a")


def short_pair() -> tuple[int, str]:
    return (1,)


def empty() -> tuple[()]:
    return ()


def numbers() -> tuple[int, ...]:
    return (1, 2, 3)


def call() -> Callable[[int], int]:
    return abs


def frozen() -> frozenset[str]:
    return frozenset({"a"})


def kind() -> type[int]:
    return bool


def items() -> Iterator[int]:
    return iter([1])


def user() -> UserId:
    return UserId(3)


def mixed() -> list[int]:
    return [1, "x"]


def counts() -> dict[str, int]:
    return {"a": 1}


def int_for_float() -> float:
    return 1


def optional_none() -> int | None:
    return None


def none_for_int() -> int:
    return None


def fails() -> int:
    raise ValueError("original failure")


def a(x: int) -> int:
    return x + 1


def b(a: int) -> int:
    return a * 2
"""


class CheckError(Exception):
    """What a raising custom check raises."""


def digits(declared: object, value: object) -> bool:
    """A custom check: a `str` of digits stands for an `int`."""
    return declared is int and isinstance(value, str) and value.isdigit()


def refuses(declared: object, value: object) -> bool:  # noqa: ARG001  # a check's signature
    """A custom check that admits nothing."""
    return False


class Counted:
    """A custom check that keeps every `(declared, value)` it is called with."""

    def __init__(self, admits: bool = False) -> None:  # noqa: FBT001, FBT002  # its one switch
        """Admits every value when `admits`, else none."""
        self.admits = admits
        self.calls: list[tuple[object, object]] = []

    def __call__(self, declared: object, value: object) -> bool:
        """Keeps the call, then answers."""
        self.calls.append((declared, value))
        return self.admits


def _module(source: str = SHAPES) -> ModuleType:
    return make_module("output_checks_graph", source)


def _run(
    builder: fnloom.Builder,
    how: str,
    final_vars: list[str],
    *,
    inputs: Mapping[str, object] | None = None,
    overrides: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Runs the request on the driver `how` names."""
    if how == "sync":
        return builder.build().execute(final_vars, inputs=inputs, overrides=overrides)
    sync_nodes = cast("Literal['thread', 'inline']", how.removeprefix("async-"))
    driver = builder.build_async(sync_nodes=sync_nodes)
    return asyncio.run(driver.execute(final_vars, inputs=inputs, overrides=overrides))


def _checked(module: ModuleType | None = None) -> fnloom.Builder:
    return fnloom.Builder().with_modules(module or _module()).with_output_type_checks()


@pytest.mark.parametrize("how", DRIVERS)
def test_a_wrong_result_fails_its_node_with_a_type_error(how: str) -> None:
    """The mismatch is the node's own failure, with its note; off by default."""
    module = _module()
    plain = fnloom.Builder().with_modules(module)
    assert _run(plain, how, ["wrong"]) == {"wrong": "s"}
    with pytest.raises(
        TypeError, match=r"^wrong: returned str, expected int(?:\n|$)"
    ) as info:
        _run(_checked(module), how, ["wrong"])
    assert info.value.__notes__ == ["in node 'wrong'"]
    assert _run(_checked(module), how, ["a", "b"], inputs={"x": 1}) == {"a": 2, "b": 4}


def test_any_and_object_returns_get_no_check() -> None:
    """Compiled away at build: the payload is the function itself."""
    module = _module()
    driver = _checked(module).build_async()
    nodes = driver._nodes  # noqa: SLF001  # what the core calls
    assert nodes["anything"].payload is module.anything
    assert nodes["plain_object"].payload is module.plain_object
    assert nodes["wrong"].payload is not module.wrong
    unchecked = fnloom.Builder().with_modules(module).build_async()
    assert unchecked._nodes["wrong"].payload is module.wrong  # noqa: SLF001  # what the core calls
    assert set(_run(_checked(module), "sync", ["anything", "plain_object"])) == {
        "anything",
        "plain_object",
    }


@pytest.mark.parametrize("how", DRIVERS)
def test_fixed_tuples_generics_and_newtypes_check_correctly(how: str) -> None:
    """Every form Hamilton's checker crashes on or misreads answers by the rules."""
    builder = _checked()
    passing = [
        "pair",
        "empty",
        "numbers",
        "call",
        "frozen",
        "kind",
        "items",
        "user",
        "counts",
        "mixed",  # shallow: a `list` is checked by its class
    ]
    got = _run(builder, how, passing)
    assert got["pair"] == (1, "a")
    assert got["user"] == 3  # noqa: PLR2004  # the NewType's value
    with pytest.raises(
        TypeError,
        match=r"^short_pair: returned tuple, expected tuple\[int, str\] "
        r"\(its length does not fit\)(?:\n|$)",
    ):
        _run(builder, how, ["short_pair"])
    with pytest.raises(
        TypeError,
        match=r"^mixed: returned list, expected list\[int\] "
        r"\(an item does not fit\)(?:\n|$)",
    ):
        _run(builder.with_deep_type_checks(), how, ["mixed"])


@pytest.mark.parametrize("how", DRIVERS)
def test_widening_and_none_follow_the_driver_rules(how: str) -> None:
    """Review Focus 5: `int` for `float` only with widening; `None` by Q79."""
    builder = _checked()
    with pytest.raises(
        TypeError, match=r"^int_for_float: returned int, expected float(?:\n|$)"
    ):
        _run(builder, how, ["int_for_float"])
    assert _run(builder.with_numeric_widening(), how, ["int_for_float"]) == {
        "int_for_float": 1
    }
    assert _run(builder, how, ["optional_none"]) == {"optional_none": None}
    with pytest.raises(
        TypeError, match=r"^none_for_int: returned NoneType, expected int(?:\n|$)"
    ):
        _run(builder, how, ["none_for_int"])


@pytest.mark.parametrize("how", DRIVERS)
def test_a_raising_node_keeps_its_own_error(how: str) -> None:
    """A node's failure is never checked as a `None` result (H6.17)."""
    with pytest.raises(ValueError, match=r"^original failure(?:\n|$)") as info:
        _run(_checked(), how, ["fails"])
    assert info.value.__notes__ == ["in node 'fails'"]


@pytest.mark.parametrize("how", DRIVERS)
def test_after_node_sees_the_mismatch_as_the_failure(how: str) -> None:
    """`after_node` gets `ok=False` and the very `TypeError`; `after_run` too."""
    hook = Recorder()
    with pytest.raises(TypeError) as info:
        _run(_checked().with_hooks(hook), how, ["wrong"])
    ends = [e for e in hook.events if isinstance(e, NodeEnd)]
    assert [(e.node.name, e.ok, e.result) for e in ends] == [("wrong", False, None)]
    assert ends[0].error is info.value
    run_end = hook.events[-1]
    assert isinstance(run_end, RunEnd)
    assert not run_end.ok
    assert run_end.error is info.value


ASYNC_NODES = """
async def wrong() -> int:
    return "s"


async def right() -> int:
    return 1


async def fails() -> int:
    raise ValueError("original failure")


async def anything() -> object:
    return "s"
"""


@pytest.mark.parametrize("how", ASYNC_DRIVERS)
def test_an_async_node_is_checked_on_its_awaited_result(how: str) -> None:
    """The coroutine's value is what is checked, never the coroutine."""
    module = _module(ASYNC_NODES)
    builder = _checked(module)
    assert _run(builder, how, ["right", "anything"]) == {"right": 1, "anything": "s"}
    with pytest.raises(
        TypeError, match=r"^wrong: returned str, expected int(?:\n|$)"
    ) as info:
        _run(builder, how, ["wrong"])
    assert info.value.__notes__ == ["in node 'wrong'"]
    with pytest.raises(ValueError, match=r"^original failure(?:\n|$)"):
        _run(builder, how, ["fails"])
    nodes = builder.build_async()._nodes  # noqa: SLF001  # what the core calls
    assert nodes["anything"].payload is module.anything


ROWS_AND_FIELDS = """
from typing import TypedDict

from fnloom import extract_fields, parameterize, source, value


@parameterize(
    good={"v": source("x"), "w": source("x")},
    bad={"v": value("s"), "w": source("x")},
)
def row(v: object, w: int) -> int:
    return v


def twice(x: int, y: int = 0) -> int:
    return x + y


class Parts(TypedDict):
    p: int
    q: str


@extract_fields()
def parts(x: int) -> Parts:
    return {"p": x, "q": "s"}


class Other(TypedDict):
    r: int


@extract_fields()
def broken(x: int) -> Other:
    return []
"""


@pytest.mark.parametrize("how", DRIVERS)
def test_rows_groups_and_producers_are_checked_and_hooks_keep_their_keys(
    how: str,
) -> None:
    """A row's wrapper, a producer and `by_parameter`'s re-keying all still work."""
    module = _module(ROWS_AND_FIELDS)
    hook = Recorder()
    builder = _checked(module).with_hooks(hook)
    got = _run(builder, how, ["good", "twice", "p", "q"], inputs={"x": 2})
    assert got == {"good": 2, "twice": 2, "p": 2, "q": "s"}
    starts = {
        e.node.name: dict(e.inputs or {})
        for e in hook.events
        if isinstance(e, fnloom.NodeStart)
    }
    assert starts["good"] == {"v": 2, "w": 2}
    assert starts["twice"] == {"x": 2}
    with pytest.raises(TypeError, match=r"^bad: returned str, expected int(?:\n|$)"):
        _run(builder, how, ["bad"], inputs={"x": 2})
    with pytest.raises(
        TypeError,
        match=r"^broken: returned list, expected output_checks_graph\.Other(?:\n|$)",
    ) as info:
        _run(builder, how, ["broken"], inputs={"x": 2})
    assert info.value.__notes__ == ["in node 'broken'"]


@pytest.mark.parametrize("how", DRIVERS)
def test_a_custom_check_admits_what_the_default_refuses_and_is_not_called_otherwise(
    how: str,
) -> None:
    """Tried only after the compiled default refuses; any `True` admits."""
    module = _module()
    counted = Counted()
    builder = (
        fnloom.Builder()
        .with_modules(module)
        .with_output_type_checks()
        .with_value_check(counted)
    )
    assert _run(builder, how, ["a", "b"], inputs={"x": 1}) == {"a": 2, "b": 4}
    assert counted.calls == []
    with pytest.raises(TypeError, match=r"^wrong: returned str, expected int(?:\n|$)"):
        _run(builder, how, ["wrong"])
    assert counted.calls == [(int, "s")]
    admitted = builder.with_value_check(digits)
    with pytest.raises(TypeError, match=r"^wrong: returned str, expected int(?:\n|$)"):
        _run(admitted, how, ["wrong"])
    assert _run(builder.with_value_check(Counted(admits=True)), how, ["wrong"]) == {
        "wrong": "s"
    }


@pytest.mark.parametrize("how", DRIVERS)
def test_custom_checks_apply_to_inputs_overrides_fields_outputs_and_validate(
    how: str,
) -> None:
    """Every value check consults them; an edge check does not."""
    module = _module(
        """
from typing import TypedDict

from fnloom import extract_fields


class Parts(TypedDict):
    p: int


def a(x: int) -> int:
    return x


def b(a: int) -> int:
    return a


@extract_fields()
def parts(x: int) -> Parts:
    return {"p": str(x)}


def out(x: int) -> int:
    return str(x)
"""
    )
    plain = fnloom.Builder().with_modules(module)
    checked = plain.with_value_check(digits)
    request: list[str] = ["b"]
    with pytest.raises(ValueError, match=r"^x: expected int, got str(?:\n|$)"):
        _run(plain, how, request, inputs={"x": "7"})
    assert _run(checked, how, request, inputs={"x": "7"}) == {"b": "7"}
    with pytest.raises(ValueError, match=r"^a: expected int, got str(?:\n|$)"):
        _run(plain, how, request, overrides={"a": "7"})
    assert _run(checked, how, request, overrides={"a": "7"}) == {"b": "7"}
    with pytest.raises(ValueError, match=r"returned field 'p' as str"):
        _run(plain, how, ["p"], inputs={"x": 7})
    assert _run(checked, how, ["p"], inputs={"x": 7}) == {"p": "7"}
    with pytest.raises(TypeError, match=r"^out: returned str, expected int(?:\n|$)"):
        _run(plain.with_output_type_checks(), how, ["out"], inputs={"x": 7})
    assert _run(checked.with_output_type_checks(), how, ["out"], inputs={"x": 7}) == {
        "out": "7"
    }
    assert plain.build().validate(request, inputs={"x": "7"}) == [
        "x: expected int, got str"
    ]
    assert checked.build().validate(request, inputs={"x": "7"}) == []
    assert checked.build().validate(request, inputs={"x": "seven"}) == [
        "x: expected int, got str"
    ]
    # Edges compare types, never values: a custom check cannot change one.
    edge = make_module(
        "output_checks_edge",
        """
def p() -> int:
    return 1


def q(p: str) -> str:
    return p
""",
    )
    with pytest.raises(ValueError, match=r"q\.p requires str but p returns int"):
        fnloom.Builder().with_modules(edge).with_value_check(
            Counted(admits=True)
        ).build()


def test_custom_checks_are_cumulative_and_get_the_declared_type() -> None:
    """Each check given is kept, in order, and gets `(declared, value)`."""
    first, second = Counted(), Counted(admits=True)
    module = _module()
    driver = (
        fnloom.Builder()
        .with_modules(module)
        .with_value_check(first)
        .with_value_check(second)
        .build()
    )
    assert driver.execute(["a"], inputs={"x": 1.5}) == {"a": 2.5}
    assert first.calls == second.calls == [(int, 1.5)]
    only_first = fnloom.Builder().with_modules(module).with_value_check(refuses)
    with pytest.raises(ValueError, match=r"^x: expected int, got float(?:\n|$)"):
        only_first.build().execute(["a"], inputs={"x": 1.5})


def test_a_check_that_is_not_callable_is_refused() -> None:
    """`TypeError` when given, naming what was given."""
    with pytest.raises(
        TypeError, match=r"^a value check must be callable, not 3(?:\n|$)"
    ):
        fnloom.Builder().with_value_check(cast("Any", 3))


def _raises(declared: object, value: object) -> bool:
    msg = f"cannot judge {value!r} for {declared!r}"
    raise CheckError(msg)


@pytest.mark.parametrize("how", DRIVERS)
def test_a_raising_custom_check_propagates_unwrapped(how: str) -> None:
    """Its exception is raised as it is: from a run's checks, or as the node's."""
    module = _module()
    builder = fnloom.Builder().with_modules(module).with_value_check(_raises)
    with pytest.raises(
        CheckError, match=r"^cannot judge 's' for <class 'int'>(?:\n|$)"
    ):
        _run(builder, how, ["a"], inputs={"x": "s"})
    with pytest.raises(
        CheckError, match=r"^cannot judge 's' for <class 'int'>(?:\n|$)"
    ):
        builder.build().validate(["a"], inputs={"x": "s"})
    with pytest.raises(CheckError) as info:
        _run(builder.with_output_type_checks(), how, ["wrong"])
    assert info.value.__notes__ == ["in node 'wrong'"]
    assert _run(builder, how, ["a"], inputs={"x": 1}) == {"a": 2}


UNRESOLVED = """
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from decimal import Decimal


def price() -> "Decimal":
    return 1
"""


def test_an_unresolved_return_is_q81s_type_error_unless_a_check_admits_it() -> None:
    """Checking against an annotation that did not resolve says to import it."""
    module = _module(UNRESOLVED)
    with pytest.raises(
        TypeError,
        match=r"^price: cannot check a value against unresolved 'Decimal': .*"
        r"import what it names at run time",
    ):
        _checked(module).build().execute(["price"])
    counted = Counted(admits=True)
    admitted = _checked(module).with_value_check(counted).build()
    assert admitted.execute(["price"]) == {"price": 1}
    (declared, _), *_ = counted.calls
    assert isinstance(declared, Unresolved)


def _carry(value: object) -> object:
    return pickle.loads(pickle.dumps(value))  # noqa: S301  # our own pickle


CARRIED = """
def a(x: int) -> int:
    return x


def wrong(x: int) -> int:
    return f"#{x}"
"""


def test_both_settings_survive_pickling_copy_rebuild_and_build_async() -> None:
    """Every way of carrying a driver keeps both settings."""
    builder = (
        fnloom.Builder()
        .with_modules(make_module("output_checks_carried", CARRIED))
        .with_output_type_checks()
        .with_value_check(digits)
    )
    driver = builder.build()
    sync: list[Any] = [
        driver,
        copy.copy(driver),
        copy.deepcopy(driver),
        driver.rebuild(),
        _carry(driver),
        cast("fnloom.Builder", _carry(builder)).build(),
    ]
    for each in sync:
        assert each.execute(["a"], inputs={"x": "7"}) == {"a": "7"}
        with pytest.raises(
            TypeError, match=r"^wrong: returned str, expected int(?:\n|$)"
        ):
            each.execute(["wrong"], inputs={"x": 7})
    async_drivers: list[Any] = [
        builder.build_async(),
        builder.build_async().rebuild(),
        copy.copy(builder.build_async()),
        _carry(builder.build_async()),
    ]
    for each_async in async_drivers:
        assert asyncio.run(each_async.execute(["a"], inputs={"x": "7"})) == {"a": "7"}
        with pytest.raises(
            TypeError, match=r"^wrong: returned str, expected int(?:\n|$)"
        ):
            asyncio.run(each_async.execute(["wrong"], inputs={"x": 7}))
    with pytest.raises((pickle.PicklingError, AttributeError)):
        pickle.dumps(builder.with_value_check(lambda _d, _v: True).build())


def test_the_check_types_are_typed() -> None:
    """`with_value_check` takes a `(declared, value) -> bool` callable."""
    typing.assert_type(
        fnloom.Builder().with_value_check(digits).with_output_type_checks(),
        fnloom.Builder,
    )
