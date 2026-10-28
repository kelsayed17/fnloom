"""`@defaults` and the build's refusal of shadowed and differing defaults (Q76, Q77)."""

import inspect
import re
from typing import override

import pytest

import fnloom
from fnloom import _decorators
from helpers import make_module

_SHADOWED = """
import fnloom


def cost() -> int:
    return 100


def uses_cost(cost: int = 3) -> int:
    return cost
"""

_DIFFERING = """
import fnloom


def opt1(shared: int = 1) -> int:
    return shared


def opt2(shared: int = 2) -> int:
    return shared
"""


def _error(source: str, name: str) -> str:
    """The message of the `ValueError` building `source` raises."""
    with pytest.raises(ValueError, match="default") as raised:
        fnloom.build(make_module(name, source))
    return str(raised.value)


def test_a_node_shadowing_a_default_fails_the_build() -> None:
    """The node would replace the default; the error names both and the opt-out."""
    message = _error(_SHADOWED, "defaults_shadowed")
    assert message == (
        "shadowed default: parameter 'cost' of 'uses_cost' has a default, but it "
        "reads the node 'cost', which replaces it; rename one, or let the node win "
        "with @fnloom.defaults(shadowed=('cost',)) on 'uses_cost'"
    )


def test_a_source_binding_that_reads_a_node_is_shadowed_too() -> None:
    """A row's `source("x")` on a defaulted parameter reads `x` like a plain name."""
    message = _error(
        """
        import fnloom


        def total() -> int:
            return 7


        @fnloom.parameterize(row={"amount": fnloom.source("total")})
        def spend(amount: int = 1) -> int:
            return amount
        """,
        "defaults_source_row",
    )
    assert "parameter 'amount' of 'row'" in message
    assert "reads the node 'total'" in message
    assert "shadowed=('amount',)" in message


def test_a_value_binding_has_no_edge_so_nothing_is_shadowed() -> None:
    """`value()` passes a constant; a node of the parameter's name is not read."""
    module = make_module(
        "defaults_value_row",
        """
        import fnloom


        def amount() -> int:
            return 100


        @fnloom.parameterize(row={"amount": fnloom.value(5)})
        def spend(amount: int = 1) -> int:
            return amount
        """,
    )
    assert fnloom.build(module).execute(["row"]) == {"row": 5}


def test_a_field_node_can_shadow_a_default() -> None:
    """An `extract_*` field is a computed node like any other."""
    message = _error(
        """
        import fnloom


        @fnloom.extract_fields({"mean": float})
        def stats() -> dict[str, float]:
            return {"mean": 1.0}


        def report(mean: float = 0.0) -> float:
            return mean
        """,
        "defaults_field",
    )
    assert "parameter 'mean' of 'report' has a default" in message
    assert "reads the node 'mean'" in message


def test_only_the_selected_config_variants_count() -> None:
    """A consumer variant the config does not select is no node: it refuses nothing."""
    source = """
    import fnloom


    def cost() -> int:
        return 100


    @fnloom.config.when(mode="a")
    def uses__a(cost: int = 3) -> int:
        return cost


    @fnloom.config.when(mode="b")
    def uses__b(other: int = 4) -> int:
        return other


    @fnloom.config.when(mode="a")
    def reads__a(shared: int = 1) -> int:
        return shared


    @fnloom.config.when(mode="b")
    def reads__b(shared: int = 2) -> int:
        return shared


    def reads_always(shared: int = 1) -> int:
        return shared
    """
    module = make_module("defaults_config", source)
    quiet = fnloom.Builder().with_modules(module).with_config({"mode": "b"})
    with pytest.raises(
        ValueError, match="differing defaults: input 'shared'"
    ) as raised:
        quiet.build()
    # Variant `b` of `reads` (default 2) differs from `reads_always`; `uses__a`, which
    # would shadow `cost`, is not selected, so it is absent.
    assert "'reads' (parameter 'shared') = 2" in str(raised.value)
    assert "= 1, 'reads_always'" not in str(raised.value)
    assert "shadowed default" not in str(raised.value)
    loud = fnloom.Builder().with_modules(module).with_config({"mode": "a"})
    # The selected variants agree on `shared` and `uses__a` shadows `cost`.
    with pytest.raises(ValueError, match="parameter 'cost' of 'uses'") as raised:
        loud.build()
    assert "differing" not in str(raised.value)


def test_differing_defaults_fail_the_build() -> None:
    """Two consumers of one input with different defaults, each named."""
    message = _error(_DIFFERING, "defaults_differing")
    assert message == (
        "differing defaults: input 'shared' has different defaults in "
        "'opt1' (parameter 'shared') = 1, 'opt2' (parameter 'shared') = 2; make "
        "them equal, or write @fnloom.defaults(differing=(<parameter>,)) on each of "
        "these consumers but one"
    )


def test_a_required_consumer_is_not_a_conflicting_default() -> None:
    """A consumer without a default has none to differ from."""
    module = make_module(
        "defaults_required_mix",
        _DIFFERING.replace("def opt2(shared: int = 2)", "def opt2(shared: int)"),
    )
    driver = fnloom.build(module)
    assert driver.execute(["opt1", "opt2"], inputs={"shared": 9}) == {
        "opt1": 9,
        "opt2": 9,
    }


def test_equal_defaults_are_fine() -> None:
    """Defaults that compare equal agree, whatever their type or identity."""
    module = make_module(
        "defaults_equal",
        """
        def a(shared: list[int] = [1, 2]) -> int:
            return len(shared)


        def b(shared: list[int] = [1, 2]) -> int:
            return len(shared) + 1


        def c(other: int = 1) -> int:
            return other


        def d(other: int = 1) -> int:
            return other
        """,
    )
    driver = fnloom.build(module)
    assert driver.execute(["a", "b", "c", "d"]) == {"a": 2, "b": 3, "c": 1, "d": 1}


class _NoEquality:
    """Compares by identity, as an object without `__eq__` does."""


class _ArrayLike:
    """Compares like a numpy array: elementwise, with a result that is no `bool`."""

    @override
    def __eq__(self, other: object) -> list[bool]:  # ty: ignore[invalid-method-override]  # an array's result
        return [True]

    __hash__ = None  # as an array


class _Refuses:
    """Cannot be compared at all."""

    @override
    def __eq__(self, other: object) -> bool:
        msg = "ambiguous"
        raise ValueError(msg)

    __hash__ = None  # as an array


@pytest.mark.parametrize("make", [_NoEquality, _ArrayLike, _Refuses])
def test_an_unclear_comparison_counts_as_differing(make: type) -> None:
    """Two defaults that cannot be told equal are refused, not trusted."""
    source = (
        f"from test_defaults import {make.__name__} as make\n\n\n"
        "def a(shared: object = make()) -> int:\n    return 1\n\n\n"
        "def b(shared: object = make()) -> int:\n    return 2\n"
    )
    module = make_module("defaults_unclear", source)
    with pytest.raises(ValueError, match="differing defaults: input 'shared'"):
        fnloom.build(module)


def _two_consumers(name: str, default: str, other: str) -> fnloom.Driver:
    """Builds two consumers of `shared`, whose defaults are the expressions given."""
    source = (
        "import math\n\n\n"
        f"def a(shared: object = {default}) -> int:\n    return 1\n\n\n"
        f"def b(shared: object = {other}) -> int:\n    return 2\n"
    )
    return fnloom.build(make_module(name, source))


def test_separate_nans_are_equal_defaults() -> None:
    """`nan != nan`, but two NaN defaults are the same default."""
    driver = _two_consumers("defaults_nan", "float('nan')", "math.nan")
    assert driver.execute(["a", "b"]) == {"a": 1, "b": 2}


def test_nan_against_a_number_differs() -> None:
    """A NaN is not equal to a number, so the defaults differ."""
    with pytest.raises(ValueError, match="differing defaults: input 'shared'"):
        _two_consumers("defaults_nan_number", "float('nan')", "1.0")


def test_equal_numpy_scalars_are_equal_defaults() -> None:
    """`==` on two numpy scalars gives numpy's bool, which counts as a `bool`."""
    pytest.importorskip("numpy")
    source = (
        "import numpy as np\n\n\n"
        "def a(shared: object = np.float64(1.0)) -> int:\n    return 1\n\n\n"
        "def b(shared: object = np.float64(1.0)) -> int:\n    return 2\n"
    )
    assert fnloom.build(make_module("defaults_np_equal", source)).execute(
        ["a", "b"]
    ) == {"a": 1, "b": 2}
    different = source.replace(
        "np.float64(1.0)) -> int:\n    return 2",
        "np.float64(2.0)) -> int:\n    return 2",
    )
    with pytest.raises(ValueError, match="differing defaults: input 'shared'"):
        fnloom.build(make_module("defaults_np_differ", different))
    arrays = source.replace("np.float64(1.0)", "np.zeros(2)")
    with pytest.raises(ValueError, match="differing defaults: input 'shared'"):
        fnloom.build(make_module("defaults_np_arrays", arrays))


def test_one_default_object_shared_is_the_same() -> None:
    """The same object is equal to itself, even where `==` would be unclear."""
    module = make_module(
        "defaults_same_object",
        """
        SHARED = object()


        def a(shared: object = SHARED) -> int:
            return 1


        def b(shared: object = SHARED) -> int:
            return 2
        """,
    )
    assert fnloom.build(module).execute(["a", "b"]) == {"a": 1, "b": 2}


def test_every_case_is_listed_sorted_in_one_error() -> None:
    """Both checks, several cases each, one `ValueError`, in name order."""
    source = (
        _SHADOWED.replace("uses_cost", "zz_uses_cost")
        + "\n\ndef aa_uses_cost(cost: int = 4) -> int:\n    return cost\n"
        + _DIFFERING.split("import fnloom\n", 1)[1]
    )
    lines = _error(source, "defaults_many").splitlines()
    assert lines == sorted(lines)
    assert [line.split(":")[0] for line in lines] == [
        "differing defaults",
        "shadowed default",
        "shadowed default",
    ]
    assert "'aa_uses_cost'" in lines[1]
    assert "'zz_uses_cost'" in lines[2]


def test_opt_outs_restore_hamilton_behaviour() -> None:
    """`shadowed` lets the node win; `differing` keeps each consumer's default."""
    module = make_module(
        "defaults_opt_out",
        """
        import fnloom


        def cost() -> int:
            return 100


        @fnloom.defaults(shadowed=("cost",))
        def uses_cost(cost: int = 3) -> int:
            return cost


        @fnloom.defaults(differing=("shared",))
        def opt1(shared: int = 1) -> int:
            return shared


        @fnloom.defaults(differing=("shared",))
        def opt2(shared: int = 2) -> int:
            return shared
        """,
    )
    driver = fnloom.build(module)
    assert driver.execute(["uses_cost", "opt1", "opt2"]) == {
        "uses_cost": 100,
        "opt1": 1,
        "opt2": 2,
    }
    # A supplied input reaches both; the node still wins over a supplied `cost`.
    result = driver.execute(
        ["uses_cost", "opt1", "opt2"], inputs={"shared": 9}, overrides={}
    )
    assert result == {"uses_cost": 100, "opt1": 9, "opt2": 9}


def test_differing_may_be_left_off_one_consumer() -> None:
    """The opted-out consumers leave the comparison, so one may keep it off."""
    third = "\n\ndef opt3(shared: int = 3) -> int:\n    return shared\n"
    source = _DIFFERING.replace(
        "def opt2", '@fnloom.defaults(differing=("shared",))\ndef opt2'
    )
    # Two consumers, one opted out: the other stands alone.
    module = make_module("defaults_all_but_one", source)
    assert fnloom.build(module).execute(["opt1", "opt2"]) == {"opt1": 1, "opt2": 2}
    # Three, one opted out: the remaining two still differ.
    module = make_module("defaults_one_of_three", source + third)
    message = ""
    try:
        fnloom.build(module)
    except ValueError as error:
        message = str(error)
    assert "'opt1' (parameter 'shared') = 1, 'opt3' (parameter 'shared') = 3" in message
    assert "'opt2'" not in message
    # Opting out the third too settles it.
    both = source + third.replace(
        "def opt3", '@fnloom.defaults(differing=("shared",))\ndef opt3'
    )
    module = make_module("defaults_two_of_three", both)
    assert fnloom.build(module).execute(["opt1", "opt3"]) == {"opt1": 1, "opt3": 3}


def test_a_parameterized_rows_input_is_a_consumer() -> None:
    """A row's `source("x")` on a defaulted parameter reads input `x`."""
    message = _error(
        """
        import fnloom


        @fnloom.parameterize(row={"limit": fnloom.source("ceiling")})
        def capped(limit: int = 10) -> int:
            return limit


        def other(ceiling: int = 20) -> int:
            return ceiling
        """,
        "defaults_row_input",
    )
    assert "input 'ceiling'" in message
    assert "'other' (parameter 'ceiling') = 20" in message
    assert "'row' (parameter 'limit') = 10" in message


def test_rows_of_one_function_with_one_default_agree() -> None:
    """Rows of a function share its default, so they never differ from each other."""
    module = make_module(
        "defaults_rows_agree",
        """
        import fnloom


        @fnloom.parameterize(
            a={"limit": fnloom.source("ceiling")}, b={"limit": fnloom.source("ceiling")}
        )
        def capped(limit: int = 10) -> int:
            return limit
        """,
    )
    assert fnloom.build(module).execute(["a", "b"]) == {"a": 10, "b": 10}


def test_the_checks_run_once_at_build(monkeypatch: pytest.MonkeyPatch) -> None:
    """Executing and validating never look at the defaults again."""
    calls: list[int] = []
    real = fnloom.driver.default_problems

    def counting(nodes: object) -> list[str]:
        calls.append(1)
        return real(nodes)  # ty: ignore[invalid-argument-type]  # the builder's nodes

    monkeypatch.setattr(fnloom.driver, "default_problems", counting)
    module = make_module("defaults_once", _DIFFERING.replace("= 2", "= 1"))
    driver = fnloom.build(module)
    for _ in range(3):
        driver.execute(["opt1"])
        driver.validate(["opt2"])
    assert len(calls) == 1


def test_build_async_refuses_too() -> None:
    """The async builder runs the same checks."""
    module = make_module("defaults_async", _SHADOWED)
    with pytest.raises(ValueError, match="shadowed default"):
        fnloom.Builder().with_modules(module).build_async()


# --- the decorator ---------------------------------------------------------------


def test_defaults_returns_the_function_itself() -> None:
    """Like every decorator: the same object, still callable and still typed."""

    def f(a: int = 1) -> int:
        return a

    decorated = fnloom.defaults(shadowed=["a"])(f)
    assert decorated is f
    assert f() == 1
    assert _decorators.decorations(f).defaults == _decorators.DefaultsPolicy(
        frozenset({"a"}), frozenset()
    )
    assert inspect.signature(f).parameters["a"].default == 1


def test_stacking_order_does_not_matter() -> None:
    """`@defaults` records only, so it composes with the others in any order."""

    @fnloom.defaults(shadowed=("a",))
    @fnloom.tag(kind="x")
    @fnloom.parameterize(row={"a": fnloom.source("b")})
    def above(a: int = 1) -> int:
        return a

    @fnloom.parameterize(row={"a": fnloom.source("b")})
    @fnloom.tag(kind="x")
    @fnloom.defaults(shadowed=("a",))
    def below(a: int = 1) -> int:
        return a

    first, second = (_decorators.decorations(f) for f in (above, below))
    assert first.defaults == second.defaults
    assert first.rows == second.rows
    assert first.tags == second.tags


def test_applying_defaults_twice_merges_the_names() -> None:
    """The second call adds to the first, so order and splitting do not matter."""

    @fnloom.defaults(shadowed=("a",))
    @fnloom.defaults(shadowed=("b",), differing=("a",))
    def f(a: int = 1, b: int = 2) -> int:
        return a + b

    assert _decorators.decorations(f).defaults == _decorators.DefaultsPolicy(
        frozenset({"a", "b"}), frozenset({"a"})
    )


def test_a_name_that_is_no_defaulted_parameter_is_refused() -> None:
    """Every culprit of both arguments, sorted, in one error."""
    with pytest.raises(ValueError, match="not one with a default") as raised:

        @fnloom.defaults(shadowed=("zeta", "needed", "a"), differing=("typo",))
        def f(needed: int, a: int = 1) -> int:
            return needed + a

    assert str(raised.value) == (
        "defaults names a parameter that is not one with a default, in "
        "shadowed: 'needed', 'zeta'; differing: 'typo'"
    )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"shadowed": "a"}, "not one str"),
        ({"differing": [1, None]}, "differing names must be str, got: NoneType, int"),
    ],
)
def test_names_must_be_an_iterable_of_str(
    kwargs: dict[str, object], message: str
) -> None:
    """A bare `str` would split into letters, and an item must be a `str`."""
    with pytest.raises(TypeError, match=re.escape(message)):
        fnloom.defaults(**kwargs)  # ty: ignore[invalid-argument-type]  # the misuse under test


# --- value()-bound names (Q77) ---------------------------------------------------

_VALUE_BOUND = """
import fnloom


@fnloom.parameterize(r1={"p": fnloom.value(1), "s": fnloom.source("src")})
def f(p: int, s: int) -> int:
    return p + s
"""


def test_a_value_bound_name_is_refused_unless_read_elsewhere() -> None:
    """Nothing reads `p`: refused (Q4). Another node does: it feeds that node alone."""
    alone = fnloom.build(make_module("defaults_value_alone", _VALUE_BOUND))
    with pytest.raises(ValueError, match="'p'"):
        alone.execute(["r1"], inputs={"src": 1, "p": 99})
    with pytest.raises(ValueError, match="'p'"):
        alone.execute(["r1"], inputs={"src": 1}, overrides={"p": 99})
    assert alone.execute(["r1"], inputs={"src": 1}) == {"r1": 2}

    shared = make_module(
        "defaults_value_read",
        _VALUE_BOUND + "\n\ndef g(p: int) -> int:\n    return p * 100\n",
    )
    driver = fnloom.build(shared)
    assert driver.execute(["r1", "g"], inputs={"src": 1, "p": 99}) == {
        "r1": 2,
        "g": 9900,
    }
    assert driver.inputs()["p"].consumers == ("g",)
    assert driver.execute(["r1"], inputs={"src": 1, "p": 99}) == {"r1": 2}
