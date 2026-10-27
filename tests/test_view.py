"""Tests for `Driver.nodes`: the typed, read-only view of a built graph."""

import importlib.util
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import fnloom
from fnloom import NodeView, Param, Source, Value
from helpers import make_module

if TYPE_CHECKING:
    from types import ModuleType

FIXTURES = Path(__file__).parent / "differential" / "fixtures"
HEADER = "from fnloom import parameterize, source, tag, value\n"


def _fixture(name: str) -> ModuleType:
    """Loads `differential/fixtures/<name>.py` as a module of its own name."""
    unique = f"view_fixture_{name}"
    spec = importlib.util.spec_from_file_location(unique, FIXTURES / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[unique] = module
    spec.loader.exec_module(module)
    return module


def test_function_node_view_fields() -> None:
    """A function node shows its return type, function, module, params and docstring."""
    module = make_module(
        "view_fields",
        HEADER
        + '''
def scaled(a: int, factor: float = 2.0) -> float:
    """Multiplies `a` by `factor`."""
    return a * factor
''',
    )
    view = fnloom.build(module).nodes["scaled"]
    assert isinstance(view, NodeView)
    assert view.name == "scaled"
    assert view.type is float
    assert view.is_input is False
    assert view.origin is module.scaled
    assert view.module == "view_fields"
    assert view.params == (
        Param("a", Source("a"), required=True, annotation=int),
        Param("factor", Source("factor"), required=False, annotation=float),
    )
    assert view.doc == "Multiplies `a` by `factor`."
    assert dict(view.tags) == {}


def test_input_node_view_fields() -> None:
    """An input has no function, module or params; its type is its consumer's."""
    module = make_module("view_input", "def f(a: list[int]) -> int:\n    return 1\n")
    view = fnloom.build(module).nodes["a"]
    assert view.name == "a"
    assert view.is_input is True
    assert view.origin is None
    assert view.module is None
    assert view.params == ()
    assert view.dependencies == ()
    assert view.type == list[int]
    assert view.doc is None
    assert dict(view.tags) == {}


def test_dependencies_are_distinct_sorted_sources() -> None:
    """Two parameters reading one source count once; the names come sorted."""
    module = make_module(
        "view_deps",
        HEADER
        + """
@parameterize(out={"left": source("b"), "right": source("b")})
def f(zeta: int, left: int, right: int, alpha: int) -> int:
    return zeta + left + right + alpha
""",
    )
    assert fnloom.build(module).nodes["out"].dependencies == ("alpha", "b", "zeta")


def test_value_bound_param_is_not_a_dependency() -> None:
    """A `value()` parameter is a param of the view, but no dependency."""
    module = make_module(
        "view_value",
        HEADER
        + """
@parameterize(out={"weight": value(3)})
def f(a: int, weight: int) -> int:
    return a * weight
""",
    )
    view = fnloom.build(module).nodes["out"]
    assert view.dependencies == ("a",)
    assert view.params[1] == Param("weight", Value(3), required=True, annotation=int)


def test_tags_are_readable_on_every_row_of_a_tagged_parameterized_function() -> None:
    """Each row carries the function's tags, read-only."""
    module = make_module(
        "view_tags",
        HEADER
        + """
@parameterize(low={"x": source("a")}, high={"x": source("b")})
@tag(owner="limits", stage="raw")
def bound(x: int) -> int:
    return x
""",
    )
    nodes = fnloom.build(module).nodes
    for name in ("low", "high"):
        tags = nodes[name].tags
        assert dict(tags) == {"owner": "limits", "stage": "raw"}
        with pytest.raises(TypeError):
            tags["owner"] = "other"  # ty: ignore[invalid-assignment]  # the refusal is the point


def test_nodes_mapping_is_read_only_and_sorted() -> None:
    """`nodes` iterates in name order, inputs included, and cannot be changed."""
    module = make_module(
        "view_sorted",
        "def z(b: int) -> int:\n    return b\ndef a(z: int) -> int:\n    return z\n",
    )
    nodes = fnloom.build(module).nodes
    assert list(nodes) == ["a", "b", "z"]
    assert [view.name for view in nodes.values()] == ["a", "b", "z"]
    with pytest.raises(TypeError):
        nodes["a"] = nodes["z"]  # ty: ignore[invalid-assignment]  # the refusal is the point


def test_future_annotations_resolve_like_plain_ones() -> None:
    """String annotations resolve to the same values as plain ones, node by node.

    Each module defines its own `Report` class, so values are compared by their
    repr with the module's name taken out.
    """
    modules = _fixture("typed"), _fixture("typed_future")
    seen: list[dict[str, list[str]]] = []
    for module in modules:
        nodes = fnloom.build(module).nodes
        seen.append(
            {
                name: [
                    repr(value).replace(module.__name__, "<module>")
                    for value in (view.type, *(p.annotation for p in view.params))
                ]
                for name, view in nodes.items()
            }
        )
        assert nodes["history"].type == list[module.Report]
        assert nodes["log"].type is type(None)
    assert seen[1] == seen[0]
    assert seen[0]["window"] == [repr(Sequence[str]), "tuple[str, ...]"]


def test_forward_reference_to_a_later_class_resolves() -> None:
    """An annotation naming a class defined further down resolves to that class."""
    module = make_module(
        "view_forward",
        """
def make(a: int) -> Later:
    return Later()

def use(make: "Later") -> int:
    return 1

class Later:
    pass
""",
    )
    nodes = fnloom.build(module).nodes
    assert nodes["make"].type is module.Later
    assert nodes["use"].params[0].annotation is module.Later


def test_unresolvable_annotation_raises_name_error_with_a_note_naming_the_node() -> None:  # noqa: E501  # fmt: skip
    """A name imported only for type checkers fails the build, naming node and param."""
    module = make_module(
        "view_unresolvable",
        """
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from decimal import Decimal

def ok(a: int) -> int:
    return a

def price(ok: int, rate: Decimal) -> int:
    return ok
""",
    )
    with pytest.raises(NameError, match="Decimal") as raised:
        fnloom.build(module)
    assert raised.value.__notes__ == [
        "resolving the annotation of parameter 'rate' of node 'price'"
    ]


def test_unresolvable_return_annotation_note_names_the_return() -> None:
    """For a return annotation the note says so; future-style strings fail alike."""
    module = make_module(
        "view_unresolvable_return",
        """
from __future__ import annotations

def price(a: int) -> Missing:
    return a
""",
    )
    with pytest.raises(NameError, match="Missing") as raised:
        fnloom.build(module)
    assert raised.value.__notes__ == ["resolving the return annotation of node 'price'"]


def test_input_with_conflicting_consumer_types_is_a_value_error_naming_both_consumers() -> None:  # noqa: E501  # fmt: skip
    """Consumers that disagree on an input's type fail the build, listing each one."""
    module = make_module(
        "view_conflict",
        """
def f(a: int, b: int) -> int:
    return a

def g(a: str, b: int) -> int:
    return 1

def h(c: str) -> int:
    return 1

def k(c: bytes) -> int:
    return 1
""",
    )
    message = (
        "inputs have conflicting types: "
        "'a' is int in 'f', str in 'g'; 'c' is str in 'h', bytes in 'k'"
    )
    with pytest.raises(ValueError, match=f"^{message}$"):
        fnloom.build(module)


def test_input_takes_the_narrower_of_two_compatible_consumer_types() -> None:
    """`Base` and `Child` consumers give `Child`, whichever comes first."""
    body = """
class Base:
    pass

class Child(Base):
    pass

def {first}(a: Base) -> int:
    return 1

def {second}(a: Child) -> int:
    return 1
"""
    for name, first, second in (
        ("view_narrow", "f", "g"),
        ("view_narrow_rev", "g", "f"),
    ):
        module = make_module(name, body.format(first=first, second=second))
        assert fnloom.build(module).nodes["a"].type is module.Child, name


def test_input_takes_the_narrower_of_an_optional_and_a_plain_type() -> None:
    """`int | None` and `int` consumers give `int`."""
    module = make_module(
        "view_narrow_optional",
        "def f(a: int | None) -> int:\n    return 1\n"
        "def g(a: int) -> int:\n    return 1\n",
    )
    assert fnloom.build(module).nodes["a"].type is int


def test_every_unresolvable_annotation_is_listed_sorted_in_one_note() -> None:
    """Failures in several functions come in one note, by node name; rows all named."""
    module = make_module(
        "view_unresolvable_many",
        HEADER
        + """
def zeta(a: Missing) -> int:
    return 1

@parameterize(low={"x": source("a")}, high={"x": source("b")})
def bound(x: int) -> Absent:
    return x

def alpha(a: int, b: list[Gone]) -> int:
    return a
""",
    )
    with pytest.raises(NameError, match="Gone") as raised:
        fnloom.build(module)
    note = (
        "resolving the annotations of: parameter 'b' of node 'alpha'; "
        "the return of nodes 'high', 'low' (rows of function 'bound'); "
        "parameter 'a' of node 'zeta'"
    )
    assert raised.value.__notes__ == [note]


def test_unresolvable_annotation_of_a_parameterized_function_names_its_rows() -> None:
    """A single failing annotation of a parameterized function names every row."""
    module = make_module(
        "view_unresolvable_rows",
        HEADER
        + """
@parameterize(low={"x": source("a")}, high={"x": source("b")})
def bound(x: Missing) -> int:
    return x
""",
    )
    with pytest.raises(NameError, match="Missing") as raised:
        fnloom.build(module)
    note = (
        "resolving the annotation of parameter 'x' of nodes 'high', 'low' "
        "(rows of function 'bound')"
    )
    assert raised.value.__notes__ == [note]


def test_missing_module_attribute_raises_attribute_error_with_a_note_naming_the_node() -> None:  # noqa: E501  # fmt: skip
    """`decimal.Missing` fails the build with its `AttributeError`, with the note."""
    module = make_module(
        "view_missing_attribute",
        """
from __future__ import annotations

import decimal

def price(a: int, rate: decimal.Missing) -> int:
    return a
""",
    )
    with pytest.raises(AttributeError, match="Missing") as raised:
        fnloom.build(module)
    assert raised.value.__notes__ == [
        "resolving the annotation of parameter 'rate' of node 'price'"
    ]


def test_alias_over_an_unresolvable_name_fails_the_build_with_note() -> None:
    """An alias over a type-checking-only import fails the build like it (Q44)."""
    module = make_module(
        "view_unresolvable_alias",
        """
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from decimal import Decimal

type Money = Decimal
type Prices = list[Money]

def ok(a: int) -> int:
    return a

def price(ok: int, rate: Prices) -> int:
    return ok
""",
    )
    with pytest.raises(NameError, match="Decimal") as raised:
        fnloom.build(module)
    assert raised.value.__notes__ == [
        "resolving the annotation of parameter 'rate' of node 'price'"
    ]
