"""Tests for `fnloom._scan.collect`: which functions are nodes, which are bad."""

import pytest

from fnloom import Source
from fnloom._scan import collect
from fnloom._view import Param
from helpers import make_module


def test_underscore_and_imported_functions_are_not_nodes() -> None:
    """Only public functions defined in the module itself are nodes."""
    make_module("scan_other", "def imported(a: int) -> int:\n    return a\n")
    module = make_module(
        "scan_main",
        """
        from scan_other import imported

        def public(a: int) -> int:
            return a

        def _private(a: int) -> int:
            return a

        value = 3
        """,
    )
    assert [n.name for n in collect([module])] == ["public"]


@pytest.mark.parametrize(
    ("source", "needle"),
    [
        ("def f(a) -> int:\n    return 1\n", "'a'"),
        ("def f(a: int):\n    return 1\n", "return"),
    ],
)
def test_missing_annotation_raises_value_error(source: str, needle: str) -> None:
    """A missing parameter or return annotation names the function and the slot."""
    module = make_module("scan_bad", source)
    with pytest.raises(ValueError, match=needle) as excinfo:
        collect([module])
    assert "f" in str(excinfo.value)


@pytest.mark.parametrize(
    ("source", "needle"),
    [
        ("def f(*args: int) -> int:\n    return 1\n", "args"),
        ("def f(**kwargs: int) -> int:\n    return 1\n", "kwargs"),
        ("def f(a: int, /) -> int:\n    return 1\n", "positional-only"),
    ],
)
def test_unsupported_parameter_kinds_raise_value_error(
    source: str, needle: str
) -> None:
    """Nodes are called by keyword, so variadic and positional-only are errors."""
    module = make_module("scan_kinds", source)
    with pytest.raises(ValueError, match=needle):
        collect([module])


@pytest.mark.parametrize(
    ("source", "needle"),
    [
        ("async def f(a: int) -> int:\n    return a\n", "async function"),
        ("def f(a: int) -> int:\n    yield a\n", "generator function"),
        ("async def f(a: int) -> int:\n    yield a\n", "async generator function"),
    ],
)
def test_functions_that_do_not_return_their_value_raise_value_error(
    source: str, needle: str
) -> None:
    """Calling these returns a coroutine or generator, not the node's value."""
    module = make_module("scan_lazy", source)
    with pytest.raises(ValueError, match=f"'f': {needle}"):
        collect([module])


def test_default_parameter_is_optional() -> None:
    """A parameter with a default is not required."""
    module = make_module(
        "scan_default", "def f(a: int, b: int = 2) -> int:\n    return a + b\n"
    )
    (node,) = collect([module])
    assert node.params == (
        Param("a", Source("a"), required=True, annotation=int),
        Param("b", Source("b"), required=False, annotation=int),
    )
    assert node.module == "scan_default"
    assert node.returns is int


def test_unresolvable_annotations_are_accepted() -> None:
    """Annotations are never evaluated: undefined and TYPE_CHECKING-only names pass."""
    module = make_module(
        "scan_forward",
        """
        from typing import TYPE_CHECKING

        if TYPE_CHECKING:
            from decimal import Decimal

        def f(a: Missing) -> Missing:
            return a

        def g(a: Decimal, b: Decimal = 1) -> Decimal:
            return a
        """,
    )
    nodes = {n.name: n for n in collect([module])}
    assert sorted(nodes) == ["f", "g"]
    assert [(p.name, p.required) for p in nodes["g"].params] == [
        ("a", True),
        ("b", False),
    ]


def test_duplicate_name_across_modules_names_both_modules() -> None:
    """The same node name in two modules is an error naming both."""
    first = make_module("scan_first", "def f(a: int) -> int:\n    return a\n")
    second = make_module("scan_second", "def f(a: int) -> int:\n    return a\n")
    with pytest.raises(ValueError, match=r"'f'.*scan_first.*scan_second"):
        collect([first, second])


def test_same_module_passed_twice_is_deduplicated() -> None:
    """Passing one module object repeatedly yields each node once."""
    module = make_module("scan_twice", "def f(a: int) -> int:\n    return a\n")
    assert [n.name for n in collect([module, module])] == ["f"]


def test_repeated_module_keeps_first_occurrence_order() -> None:
    """`[a, b, a]` scans `a` then `b`, each once."""
    a = make_module("scan_a", "def fa(x: int) -> int:\n    return x\n")
    b = make_module("scan_b", "def fb(x: int) -> int:\n    return x\n")
    assert [n.name for n in collect([a, b, a])] == ["fa", "fb"]


def test_duplicate_error_lists_modules_in_scan_order() -> None:
    """The error names the module scanned first, then the one that clashes."""
    a = make_module("scan_one", "def f(x: int) -> int:\n    return x\n")
    b = make_module("scan_two", "def f(x: int) -> int:\n    return x\n")
    with pytest.raises(ValueError, match=r"'scan_one' and 'scan_two'"):
        collect([a, b, a])
