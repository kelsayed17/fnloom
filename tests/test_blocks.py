"""Fan-out and fan-in: `Parallelizable`, `Collect`, and the blocks between them."""

from typing import Any, cast, override

import pytest

import fnloom
from fnloom import _core
from helpers import make_module
from type_snippets import blocks as snippet

PICKED = """
from fnloom import Collect, Parallelizable


def picked(n: int) -> Parallelizable[int]:
    yield from range(n)


def details(picked: int, rate: int) -> int:
    return picked * rate


def summary(details: Collect[int]) -> list[int]:
    return list(details)
"""


def test_aliases_type_check_as_iterable_and_list() -> None:
    """The snippet `ty check` reads runs: a generator expand, `len()` and `[0]`."""
    built = fnloom.Builder().with_modules(snippet).build()
    assert built.execute(["total"], inputs={"universe": "a,bb,ccc"}) == {"total": 4.0}


def test_parallelizable_of_t_or_absent_is_refused() -> None:
    """An item can't be absent: `Parallelizable[T | Absent]` is refused at build."""
    module = make_module(
        "blocks_absent_items",
        """
        from fnloom import Absent, Parallelizable

        def picked() -> Parallelizable[int | Absent]:
            yield 1
        """,
    )
    with pytest.raises(ValueError, match="an item can't be absent") as info:
        fnloom.build(module)
    assert str(info.value) == (
        "'picked' yields items of type int | fnloom.Absent, but an item can't be "
        "absent; write Parallelizable[int] | Absent to let the expand decline as a "
        "whole"
    )


NESTED = """
from fnloom import Collect, Parallelizable


def outer() -> Parallelizable[int]:
    yield from [1, 2]


def inner(outer: int) -> Parallelizable[int]:
    yield from range(outer)


def leaf(inner: int, outer: int) -> int:
    return outer * 10 + inner


def per_outer(leaf: Collect[int]) -> list[int]:
    return list(leaf)


def everything(per_outer: Collect[list[int]]) -> list[list[int]]:
    return list(per_outer)
"""


def test_views_report_block_and_kind() -> None:
    """`NodeView.block` names the innermost expand, `kind` the node's part."""
    built = fnloom.build(make_module("blocks_views", NESTED))
    shown = {name: (view.kind, view.block) for name, view in built.nodes.items()}
    assert shown == {
        "everything": ("collect", None),
        "inner": ("expand", "outer"),
        "leaf": ("function", "inner"),
        "outer": ("expand", None),
        "per_outer": ("collect", "outer"),
    }
    assert [p.collect for p in built.nodes["everything"].params] == ["present"]
    assert [p.collect for p in built.nodes["leaf"].params] == [None, None]
    assert built.execute(["everything"]) == {"everything": [[10], [20, 21]]}


def test_a_collect_of_t_or_absent_collects_every_branch() -> None:
    """`Collect[T | Absent]` collects `"all"`, and `Collect[T]` `"present"`."""
    module = make_module(
        "blocks_collect_all",
        """
        from fnloom import ABSENT, Absent, Collect, Parallelizable

        def picked() -> Parallelizable[int]:
            yield from range(4)

        def odd(picked: int) -> int | Absent:
            return picked if picked % 2 else ABSENT

        def every(odd: Collect[int | Absent]) -> list[object]:
            return list(odd)

        def present(odd: Collect[int]) -> list[int]:
            return list(odd)
        """,
    )
    built = fnloom.build(module)
    assert [p.collect for p in built.nodes["every"].params] == ["all"]
    assert built.execute(["every", "present"]) == {
        "every": [fnloom.ABSENT, 1, fnloom.ABSENT, 3],
        "present": [1, 3],
    }
    assert not built.nodes["present"].may_be_absent


@pytest.mark.parametrize("future", ["", "from __future__ import annotations\n"])
@pytest.mark.parametrize(
    "decorator", ['extract_fields({"a": int})', 'extract_columns("a")']
)
def test_extract_on_an_expand_is_refused(future: str, decorator: str) -> None:
    """`@extract_*` can't split an expand's items, string annotation or not."""
    source = f"""{future}
from fnloom import Parallelizable, extract_columns, extract_fields

@{decorator}
def picked() -> Parallelizable[dict]:
    yield {{"a": 1}}
"""
    with pytest.raises(ValueError, match="'picked' expands, so @extract_fields"):
        fnloom.build(make_module(f"blocks_extract_{bool(future)}", source))


def test_build_async_refuses_blocks() -> None:
    """Blocks run on the async driver from Plan 11 (Q132)."""
    module = make_module("blocks_async", PICKED)
    with pytest.raises(TypeError) as info:
        fnloom.Builder().with_modules(module).build_async()
    assert str(info.value) == (
        "blocks run on the async driver from Plan 11; 'picked' expands"
    )


def test_with_cache_refuses_blocks_until_they_are_cached() -> None:
    """A cached driver can't key a branch yet, so it refuses a graph with blocks."""
    module = make_module("blocks_cache", PICKED)
    builder = fnloom.Builder().with_modules(module).with_cache(fnloom.MemoryCache())
    with pytest.raises(ValueError, match="does not cache blocks yet; 'picked' expands"):
        builder.build()


STR_ITEMS = """
from fnloom import Collect, Parallelizable


def picked() -> Parallelizable[int]:
    yield 1
    yield "x"


def details(picked: int) -> str:
    return repr(picked)


def summary(details: Collect[str]) -> list[str]:
    return list(details)
"""


def test_items_are_checked_by_default() -> None:
    """An item of the wrong type is a `TypeError` naming the expand and the index."""
    built = fnloom.build(make_module("blocks_checked", STR_ITEMS))
    with pytest.raises(TypeError) as info:
        built.execute(["summary"])
    assert str(info.value) == "item 1 of 'picked': expected int, got str"
    assert info.value.__notes__ == ["in node 'picked'"]


def test_items_are_unchecked_without_input_type_checks() -> None:
    """`without_input_type_checks()` leaves items unchecked, as inputs."""
    module = make_module("blocks_unchecked", STR_ITEMS)
    built = fnloom.Builder().with_modules(module).without_input_type_checks().build()
    assert built.execute(["summary"]) == {"summary": ["1", "'x'"]}


def test_an_absent_item_is_refused() -> None:
    """An item is never absent, checked or not."""
    module = make_module(
        "blocks_absent_item",
        """
        from fnloom import ABSENT, Collect, Parallelizable

        def picked() -> Parallelizable[object]:
            yield ABSENT

        def details(picked: object) -> object:
            return picked

        def summary(details: Collect[object]) -> list[object]:
            return list(details)
        """,
    )
    built = fnloom.Builder().with_modules(module).without_input_type_checks().build()
    with pytest.raises(TypeError, match=r"item 0 of 'picked' is fnloom\.ABSENT"):
        built.execute(["summary"])


@pytest.mark.parametrize("future", ["", "from __future__ import annotations\n"])
def test_a_generator_not_annotated_parallelizable_stays_refused(future: str) -> None:
    """A generator is a node only as an expand, whether its annotation is a string."""
    module = make_module(
        f"blocks_generator_{bool(future)}",
        f"{future}def f(a: int) -> int:\n    yield a\n",
    )
    with pytest.raises(ValueError, match="generator function") as info:
        fnloom.build(module)
    assert str(info.value) == (
        "'f': generator function is not supported; nodes are called synchronously"
    )


def test_a_string_annotated_expand_is_found_after_resolving() -> None:
    """Under `from __future__ import annotations`, an expand is read once resolved."""
    module = make_module(
        "blocks_future", "from __future__ import annotations\n" + PICKED
    )
    built = fnloom.build(module)
    assert built.execute(["summary"], inputs={"n": 3, "rate": 2}) == {
        "summary": [0, 2, 4]
    }


def test_an_expand_returning_a_non_iterable_fails_as_itself() -> None:
    """The expand's own `TypeError`, noted with its name."""
    module = make_module(
        "blocks_not_iterable",
        """
        from fnloom import Collect, Parallelizable

        def picked() -> Parallelizable[int]:
            return 3

        def details(picked: int) -> int:
            return picked

        def summary(details: Collect[int]) -> list[int]:
            return list(details)
        """,
    )
    with pytest.raises(TypeError, match="not iterable") as info:
        fnloom.build(module).execute(["summary"])
    assert info.value.__notes__ == ["in node 'picked'"]


def test_the_native_graph_and_plan_list_their_blocks() -> None:
    """`Graph.blocks()` gives each block node's path; `Plan.steps()` lists them too."""
    built = fnloom.build(make_module("blocks_native", NESTED))
    graph = cast("_core.Graph", built._graph)  # noqa: SLF001  # the native graph on purpose
    assert graph.blocks() == {
        "inner": ["outer"],
        "leaf": ["outer", "inner"],
        "per_outer": ["outer"],
    }
    plan = graph.prepare(["everything"], [], [])
    assert [name for name, _ in plan.steps()] == [
        "outer",
        "inner",
        "leaf",
        "per_outer",
        "everything",
    ]
    assert plan.blocks() == {
        "inner": [("leaf", "compute")],
        "outer": [("inner", "compute"), ("per_outer", "compute")],
    }


def test_a_collect_mode_the_core_does_not_know_is_refused() -> None:
    """The node tuple's collect mode is `"present"`, `"all"` or `None`."""
    node: Any = ("b", [("a", "required", "some")], {}, int, False, None)
    with pytest.raises(ValueError, match="which is no collect mode"):
        _core.Graph([node])


# Overrides, outputs, lineage and validate around blocks (task 9).


def test_an_expand_override_with_a_generator_is_consumed_lazily() -> None:
    """An override supplies the items; a generator is read one branch at a time."""
    built = fnloom.build(make_module("blocks_lazy_override", PICKED))
    seen: list[str] = []

    def items() -> object:
        for item in (5, 6):
            seen.append(f"yield {item}")
            yield item

    class Order(fnloom.Hook):
        @override
        def before_node(self, event: fnloom.NodeStart) -> None:
            seen.append(event.node.name)

    hooked = (
        fnloom.Builder()
        .with_modules(make_module("blocks_lazy_hooked", PICKED))
        .with_hooks(Order())
        .build()
    )
    result = hooked.execute(
        ["summary"], inputs={"rate": 10}, overrides={"picked": items()}
    )
    assert result == {"summary": [50, 60]}
    assert seen == ["yield 5", "details", "yield 6", "details", "summary"]
    assert built.execute(
        ["summary"], inputs={"rate": 2}, overrides={"picked": [1, 2]}
    ) == {"summary": [2, 4]}


def test_a_collect_override_cuts_the_block() -> None:
    """Overriding a collect runs nothing upstream of it."""
    module = make_module("blocks_cut", PICKED)
    built = fnloom.build(module)
    assert built.execute(["summary"], overrides={"summary": [9]}) == {"summary": [9]}


def test_a_function_in_final_vars_that_is_a_block_node_is_refused() -> None:
    """A block node's function stands for it in a request, and is refused (Q30)."""
    module = make_module("blocks_function_output", PICKED)
    built = fnloom.build(module)
    with pytest.raises(ValueError, match="not an output") as info:
        built.execute([module.details], inputs={"n": 1, "rate": 1})
    assert str(info.value) == (
        "'details' is computed once per item of 'picked', so it is not an output; "
        "request a node that collects it: 'summary'"
    )


def test_an_uncollected_block_output_says_how_to_collect_it() -> None:
    """With no collect, the refusal names the `Collect` annotation to write."""
    module = make_module(
        "blocks_uncollected",
        """
        from fnloom import Parallelizable

        class Detail:
            pass

        def picked() -> Parallelizable[int]:
            yield 1

        def details(picked: int) -> Detail:
            return Detail()
        """,
    )
    with pytest.raises(ValueError, match="not an output") as info:
        fnloom.build(module).execute(["details"])
    assert str(info.value) == (
        "'details' is computed once per item of 'picked', so it is not an output; "
        "collect it with a parameter annotated Collect[Detail]"
    )


def test_lineage_and_inputs_treat_expand_and_collect_as_ordinary() -> None:
    """Lineage and `inputs()` read through blocks, with the shapes they always had."""
    built = fnloom.build(make_module("blocks_lineage", PICKED))
    upstream = built.what_is_upstream_of("summary")
    assert upstream == ["details", "n", "picked", "rate", "summary"]
    assert built.what_is_downstream_of("picked") == ["details", "picked", "summary"]
    assert built.what_is_the_path_between("picked", "summary") == [
        "picked",
        "details",
        "summary",
    ]
    assert sorted(built.inputs()) == ["n", "rate"]
    assert built.inputs()["rate"].consumers == ("details",)


def test_validate_lists_block_problems() -> None:
    """`validate` lists a block output and a block override with every other line."""
    built = fnloom.build(make_module("blocks_validate", PICKED))
    assert built.validate(
        ["details", "summary"], inputs={"n": 1}, overrides={"details": 3}
    ) == [
        (
            "'details' can't be overridden: it has one value per item of 'picked'; "
            "override 'picked' with the items, or a node outside the block"
        ),
        (
            "'details' is computed once per item of 'picked', so it is not an output; "
            "request a node that collects it: 'summary'"
        ),
        "missing inputs: 'rate' (needed by 'details')",
    ]
    with pytest.raises(ValueError, match="not an output") as info:
        built.execute(["details"], inputs={"n": 1, "rate": 1}, overrides={"details": 3})
    assert str(info.value).splitlines() == [
        (
            "'details' is computed once per item of 'picked', so it is not an output; "
            "request a node that collects it: 'summary'"
        ),
        (
            "'details' can't be overridden: it has one value per item of 'picked'; "
            "override 'picked' with the items, or a node outside the block"
        ),
    ]
