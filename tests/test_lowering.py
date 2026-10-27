"""Tests for lowering `@parameterize` rows into nodes: names, edges and payloads."""

import functools

import pytest

import fnloom
from fnloom import Source, Value
from fnloom._scan import collect, core_inputs
from helpers import make_module

HEADER = "from fnloom import parameterize, source, value\n"


def test_each_row_is_a_node_and_the_function_is_not() -> None:
    """Two rows give two nodes named by the row keys, and no node for the function."""
    module = make_module(
        "lower_rows",
        HEADER
        + """
@parameterize(low={"x": source("a")}, high={"x": source("b")})
def pick(x: int) -> int:
    return x
""",
    )
    assert sorted(n.name for n in collect([module])) == ["high", "low"]
    assert fnloom.build(module).node_names() == ["a", "b", "high", "low"]


def test_source_renames_an_edge() -> None:
    """`source("a")` makes the parameter read node `a`, under its own name."""
    module = make_module(
        "lower_source",
        HEADER
        + """
@parameterize(out={"x": source("a")})
def f(x: int) -> int:
    return x + 1
""",
    )
    (node,) = collect([module])
    assert node.params[0].name == "x"
    assert node.params[0].binding == Source("a")
    assert core_inputs(node.params) == [("a", True)]
    assert fnloom.build(module).execute(["out"], inputs={"a": 1}) == {"out": 2}


def test_value_binds_a_constant_and_is_not_an_edge() -> None:
    """`value(10)` passes 10 and adds no edge."""
    module = make_module(
        "lower_value",
        HEADER
        + """
@parameterize(out={"k": value(10)})
def f(a: int, k: int) -> int:
    return a * k
""",
    )
    (node,) = collect([module])
    assert core_inputs(node.params) == [("a", True)]
    assert node.params[1].binding == Value(10)
    assert node.params[1].required
    driver = fnloom.build(module)
    assert driver.node_names() == ["a", "out"]
    assert driver.execute(["out"], inputs={"a": 3}) == {"out": 30}


def test_unmentioned_parameter_stays_an_edge_of_its_own_name() -> None:
    """A parameter the row leaves out reads the node of its own name."""
    module = make_module(
        "lower_unmentioned",
        HEADER
        + """
@parameterize(out={"x": source("a")})
def f(x: int, y: int) -> int:
    return x - y
""",
    )
    (node,) = collect([module])
    assert node.params[1].binding == Source("y")
    assert core_inputs(node.params) == [("a", True), ("y", True)]
    assert fnloom.build(module).execute(["out"], inputs={"a": 5, "y": 2}) == {"out": 3}


def test_two_parameters_from_one_source_get_one_core_input_and_the_same_value() -> None:
    """Two parameters bound to one source are one core input, read twice."""
    module = make_module(
        "lower_shared",
        HEADER
        + """
@parameterize(out={"x": source("a"), "y": source("a")})
def f(x: list[int], y: list[int]) -> bool:
    return x is y
""",
    )
    (node,) = collect([module])
    assert core_inputs(node.params) == [("a", True)]
    assert fnloom.build(module).execute(["out"], inputs={"a": [1]}) == {"out": True}


def test_core_input_is_required_if_any_parameter_bound_to_it_is() -> None:
    """One required parameter makes the shared core input required."""
    module = make_module(
        "lower_shared_optional",
        HEADER
        + """
@parameterize(out={"x": source("a"), "y": source("a")})
def f(x: int = 1, y: int = 2, *, z: int) -> int:
    return x + y + z
@parameterize(both={"x": source("b"), "z": source("b")})
def g(x: int = 1, *, z: int) -> int:
    return x + z
""",
    )
    nodes = {n.name: n for n in collect([module])}
    assert core_inputs(nodes["out"].params) == [("a", False), ("z", True)]
    assert core_inputs(nodes["both"].params) == [("b", True)]


def test_defaulted_parameter_bound_by_source_is_optional_and_omitted_when_unsupplied() -> None:  # noqa: E501  # fmt: skip
    """An unsupplied optional source omits the keyword: the default applies."""
    module = make_module(
        "lower_default",
        HEADER
        + """
SEEN = []

@parameterize(out={"factor": source("scale")})
def f(a: int, factor: int | None = 3) -> int:
    SEEN.append(factor)
    return a * (factor if factor is not None else -1)
""",
    )
    (node,) = collect([module])
    assert core_inputs(node.params) == [("a", True), ("scale", False)]
    driver = fnloom.build(module)
    assert driver.execute(["out"], inputs={"a": 2}) == {"out": 6}
    assert driver.execute(["out"], inputs={"a": 2, "scale": 5}) == {"out": 10}
    assert vars(module)["SEEN"] == [3, 5]


def test_row_can_source_another_row() -> None:
    """Rows of one function chain: a1 -> a2 -> a3."""
    module = make_module(
        "lower_chain",
        HEADER
        + """
@parameterize(
    a1={"prev": source("start")},
    a2={"prev": source("a1")},
    a3={"prev": source("a2")},
)
def step(prev: int) -> int:
    return prev * 2
""",
    )
    driver = fnloom.build(module)
    assert driver.what_is_upstream_of("a3") == ["a1", "a2", "a3", "start"]
    assert driver.execute(["a3"], inputs={"start": 1}) == {"a3": 8}


def test_undecorated_function_payload_is_the_function_itself() -> None:
    """A plain function, or a row binding each parameter to itself, is called as is."""
    module = make_module(
        "lower_plain",
        HEADER
        + """
def plain(a: int) -> int:
    return a

@parameterize(same={"a": source("a")})
def identity(a: int) -> int:
    return a
""",
    )
    nodes = {n.name: n for n in collect([module])}
    for node in nodes.values():
        assert node.payload is node.origin
    assert nodes["plain"].origin is vars(module)["plain"]
    assert nodes["same"].origin is vars(module)["identity"]
    assert nodes["plain"].module == "lower_plain"
    assert nodes["plain"].params[0].binding == Source("a")


def test_row_payloads_call_the_function_directly_when_each_source_is_one_param() -> None:  # noqa: E501  # fmt: skip
    """Renamed sources pass by keyword, values by `partial`; a shared source wraps."""
    module = make_module(
        "lower_payloads",
        HEADER
        + """
@parameterize(
    renamed={"x": source("a"), "y": source("b")},
    constant={"x": source("a"), "y": value(10)},
    shared={"x": source("a"), "y": source("a")},
)
def f(x: int, y: int) -> int:
    return x * 100 + y
""",
    )
    nodes = {n.name: n for n in collect([module])}
    origin = vars(module)["f"]
    assert nodes["renamed"].payload is origin
    assert dict(nodes["renamed"].renames) == {"a": "x", "b": "y"}
    constant = nodes["constant"].payload
    assert isinstance(constant, functools.partial)
    assert constant.func is origin
    assert constant.keywords == {"y": 10}
    assert dict(nodes["constant"].renames) == {"a": "x"}
    assert nodes["shared"].payload is not origin
    assert dict(nodes["shared"].renames) == {}
    driver = fnloom.build(module)
    assert driver.execute(
        ["renamed", "constant", "shared"], inputs={"a": 1, "b": 2}
    ) == {
        "renamed": 102,
        "constant": 110,
        "shared": 101,
    }


def test_row_named_like_the_function_is_allowed() -> None:
    """A row may take the function's own name."""
    module = make_module(
        "lower_own_name",
        HEADER
        + """
@parameterize(f={"x": source("a")}, g={"x": source("b")})
def f(x: int) -> int:
    return x
""",
    )
    driver = fnloom.build(module)
    assert driver.node_names() == ["a", "b", "f", "g"]
    assert driver.execute(["f", "g"], inputs={"a": 1, "b": 2}) == {"f": 1, "g": 2}


def test_row_name_clashing_with_another_node_is_a_value_error_naming_both_modules() -> None:  # noqa: E501  # fmt: skip
    """A row named like a node of another module is a duplicate, naming both."""
    first = make_module("lower_first", "def shared(a: int) -> int:\n    return a\n")
    second = make_module(
        "lower_second",
        HEADER
        + """
@parameterize(shared={"x": source("a")})
def f(x: int) -> int:
    return x
""",
    )
    with pytest.raises(ValueError, match=r"'shared'.*lower_first.*lower_second"):
        fnloom.build(first, second)


def test_private_decorated_function_yields_no_nodes() -> None:
    """An underscore function makes no nodes, rows or not."""
    module = make_module(
        "lower_private",
        HEADER
        + """
@parameterize(out={"x": source("a")})
def _f(x: int) -> int:
    return x
""",
    )
    assert collect([module]) == []


def test_decorated_function_imported_elsewhere_is_one_node_set() -> None:
    """The defining module owns the rows; a module importing it adds none."""
    defining = make_module(
        "lower_defining",
        HEADER
        + """
@parameterize(out={"x": source("a")})
def f(x: int) -> int:
    return x
""",
    )
    importing = make_module(
        "lower_importing",
        "from lower_defining import f\ndef other(out: int) -> int:\n    return out\n",
    )
    nodes = collect([defining, importing])
    assert sorted(n.name for n in nodes) == ["other", "out"]
    driver = fnloom.build(importing, defining)
    assert driver.execute(["other"], inputs={"a": 4}) == {"other": 4}
    assert driver.nodes["out"].module == "lower_defining"


def test_node_exception_from_a_row_carries_the_row_name_note() -> None:
    """A row's exception keeps its class and is noted with the row's name."""
    module = make_module(
        "lower_boom",
        HEADER
        + """
@parameterize(boom={"x": source("a")})
def f(x: int) -> int:
    return 1 // x
""",
    )
    with pytest.raises(ZeroDivisionError) as info:
        fnloom.build(module).execute(["boom"], inputs={"a": 0})
    assert info.value.__notes__ == ["in node 'boom'"]


def test_value_is_passed_uncopied() -> None:
    """A `value()` object is passed itself on every call."""
    bound: list[int] = []
    module = make_module(
        "lower_identity",
        HEADER
        + """
def _make(bound):
    @parameterize(out={"acc": value(bound)})
    def f(a: int, acc: list[int]) -> int:
        acc.append(a)
        return id(acc)
    return f
""",
    )
    vars(module)["f"] = vars(module)["_make"](bound)
    driver = fnloom.build(module)
    for a in (1, 2):
        assert driver.execute(["out"], inputs={"a": a}) == {"out": id(bound)}
    assert bound == [1, 2]


def test_row_clashing_with_a_function_of_the_same_module_names_both_definitions() -> None:  # noqa: E501  # fmt: skip
    """In one module, the error names the row's function and the clashing function."""
    module = make_module(
        "lower_same",
        HEADER
        + """
@parameterize(g={"x": source("a")})
def f(x: int) -> int:
    return x

def g(a: int) -> int:
    return a
""",
    )
    message = "node 'g' is defined twice in 'lower_same': by a row of function 'f' and by function 'g'"  # noqa: E501
    with pytest.raises(ValueError, match=f"^{message}$"):
        collect([module])


def test_rows_of_two_functions_clashing_name_both_functions() -> None:
    """Two parameterized functions of one module declaring one row name clash."""
    module = make_module(
        "lower_rows_clash",
        HEADER
        + """
@parameterize(out={"x": source("a")})
def f(x: int) -> int:
    return x

@parameterize(out={"y": source("a")})
def g(y: int) -> int:
    return y
""",
    )
    message = "node 'out' is defined twice in 'lower_rows_clash': by a row of function 'f' and by a row of function 'g'"  # noqa: E501
    with pytest.raises(ValueError, match=f"^{message}$"):
        collect([module])
