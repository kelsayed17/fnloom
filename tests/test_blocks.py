"""Fan-out and fan-in: `Parallelizable`, `Collect`, and the blocks between them."""

from typing import TYPE_CHECKING, Any, cast, override

import pytest

import fnloom
from fnloom import _core
from helpers import make_module
from type_snippets import blocks as snippet

if TYPE_CHECKING:
    from collections.abc import Callable

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


def test_a_nested_block_output_names_its_collectors_inside_blocks() -> None:
    """A node collected only inside blocks names those collectors, not the fix."""
    module = make_module(
        "blocks_nested_output",
        """
        from fnloom import Collect, Parallelizable

        def outer() -> Parallelizable[int]:
            yield 1

        def inner(outer: int) -> Parallelizable[int]:
            yield outer

        def leaf(inner: int) -> int:
            return inner

        def per_outer(leaf: Collect[int]) -> int:
            return sum(leaf)

        def total(per_outer: Collect[int]) -> int:
            return sum(per_outer)
        """,
    )
    with pytest.raises(ValueError, match="not an output") as info:
        fnloom.build(module).execute(["leaf"])
    assert str(info.value) == (
        "'leaf' is computed once per item of 'inner', so it is not an output; "
        "it is collected only inside blocks, by 'per_outer'"
    )


MISMATCHED = """
from fnloom import Collect, Parallelizable


def picked() -> Parallelizable[str]:
    yield "a"


def details(picked: int) -> str:
    return repr(picked)


def summary(details: Collect[int]) -> list[int]:
    return list(details)
"""


def test_block_edge_mismatches_say_what_is_compared() -> None:
    """Each block edge's refusal names the item or element type it compared."""
    with pytest.raises(ValueError, match="requires") as info:
        fnloom.build(make_module("blocks_mismatched", MISMATCHED))
    assert str(info.value).splitlines() == [
        "details.picked requires int but picked yields items of type str",
        "summary.details collects elements of type int but details returns str",
    ]


@pytest.mark.parametrize(
    ("loose", "refused"),
    [
        (("details", "summary"), []),
        (("details",), ["summary.details"]),
        (("summary",), ["details.picked"]),
        (("picked",), ["details.picked", "summary.details"]),
    ],
)
def test_without_edge_type_checks_opts_out_block_edges_per_consumer(
    loose: tuple[str, ...], refused: list[str]
) -> None:
    """Naming a consumer opts out its block edge; naming another node still refuses."""
    module = make_module(f"blocks_loose_{'_'.join(loose)}", MISMATCHED)
    builder = fnloom.Builder().with_modules(module).without_edge_type_checks(*loose)
    if not refused:
        builder.build()
        return
    with pytest.raises(ValueError, match=r"requires|collects") as info:
        builder.build()
    assert [line.split()[0] for line in str(info.value).splitlines()] == refused


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


# Hooks, `current_branch()`, wrappers and the failing branch (task 10).


class Recorder(fnloom.Hook):
    """Records the run's nodes, each node event and each expand's end."""

    def __init__(self) -> None:
        """Records nothing yet."""
        self.events: list[tuple[str, str, tuple[int, ...]]] = []
        self.expands: list[tuple[str, tuple[int, ...], int]] = []
        self.nodes: tuple[str, ...] = ()

    @override
    def before_run(self, event: fnloom.RunStart) -> None:
        self.nodes = event.nodes

    @override
    def before_node(self, event: fnloom.NodeStart) -> None:
        self.events.append(("start", event.node.name, event.branch))

    @override
    def after_node(self, event: fnloom.NodeEnd) -> None:
        self.events.append(("end", event.node.name, event.branch))

    @override
    def after_expand(self, event: fnloom.ExpandEnd) -> None:
        self.expands.append((event.node.name, event.branch, event.items))


def _hooked(name: str, source: str, *hooks: fnloom.Hook) -> fnloom.Driver:
    module = make_module(name, source)
    return fnloom.Builder().with_modules(module).with_hooks(*hooks).build()


def test_node_events_carry_branch_in_reference_order() -> None:
    """Each branch runs to its end before the next; `RunStart.nodes` lists each once."""
    recorder = Recorder()
    built = _hooked("blocks_events", NESTED, recorder)
    assert built.execute(["everything"]) == {"everything": [[10], [20, 21]]}
    assert recorder.nodes == ("outer", "inner", "leaf", "per_outer", "everything")
    starts = [
        (name, branch) for kind, name, branch in recorder.events if kind == "start"
    ]
    assert starts == [
        ("outer", ()),
        ("inner", (0,)),
        ("leaf", (0, 0)),
        ("per_outer", (0,)),
        ("inner", (1,)),
        ("leaf", (1, 0)),
        ("leaf", (1, 1)),
        ("per_outer", (1,)),
        ("everything", ()),
    ]
    ends = [(name, branch) for kind, name, branch in recorder.events if kind == "end"]
    assert ends == starts


def test_after_expand_reports_the_item_count_per_branch() -> None:
    """Each expand's end fires once its items are exhausted, in its own branch."""
    recorder = Recorder()
    _hooked("blocks_expand_ends", NESTED, recorder).execute(["everything"])
    assert recorder.expands == [
        ("inner", (0,), 1),
        ("inner", (1,), 2),
        ("outer", (), 2),
    ]


def test_hook_with_after_expand_only_is_accepted_and_checked() -> None:
    """A hook with only `after_expand` runs; one that can't take the event is refused.

    It is refused when attached, as every hook method is.
    """

    class Counts(fnloom.Hook):
        def __init__(self) -> None:
            self.items: list[int] = []

        @override
        def after_expand(self, event: fnloom.ExpandEnd) -> None:
            self.items.append(event.items)

    counts = Counts()
    built = _hooked("blocks_expand_only", PICKED, counts)
    built.execute(["summary"], inputs={"n": 3, "rate": 1})
    built.execute(["summary"], inputs={"rate": 1}, overrides={"picked": [4]})
    assert counts.items == [3, 1]

    class Broken(fnloom.Hook):
        @override
        def after_expand(self) -> None:  # ty: ignore[invalid-method-override]
            pass

    with pytest.raises(TypeError, match=r"Broken\.after_expand must take the event"):
        fnloom.Builder().with_hooks(Broken())


CHECKED = """
from fnloom import Collect, Parallelizable, check_output_custom, current_branch

seen = []


def _branch_is_seen(value: int) -> bool:
    seen.append(("check", current_branch()))
    return True


def picked(n: int) -> Parallelizable[int]:
    seen.append(("picked", current_branch()))
    yield from range(n)


@check_output_custom(_branch_is_seen)
def details(picked: int) -> int:
    seen.append(("node", current_branch()))
    return picked


def summary(details: Collect[int]) -> list[int]:
    seen.append(("summary", current_branch()))
    return details
"""


def test_current_branch_inside_a_node_a_check_and_a_wrapper() -> None:
    """The node, its checks and its wrappers read its branch; `()` outside blocks."""
    module = make_module("blocks_current", CHECKED)
    wrapped: list[tuple[str, tuple[int, ...]]] = []

    def wrapper(
        node: fnloom.NodeView, call: Callable[[], object], inputs: object
    ) -> object:
        del inputs
        wrapped.append((node.name, fnloom.current_branch()))
        return call()

    built = fnloom.Builder().with_modules(module).with_node_wrappers(wrapper).build()
    assert built.execute(["summary"], inputs={"n": 2}) == {"summary": [0, 1]}
    assert module.seen == [
        ("picked", ()),
        ("node", (0,)),
        ("check", (0,)),
        ("node", (1,)),
        ("check", (1,)),
        ("summary", ()),
    ]
    assert wrapped == [
        ("picked", ()),
        ("details", (0,)),
        ("details", (1,)),
        ("summary", ()),
    ]
    assert fnloom.current_branch() == ()


def test_a_wrapper_is_called_once_per_branch() -> None:
    """A block node's wrapper runs for each item, and may replace its value."""
    calls: list[str] = []

    def doubled(
        node: fnloom.NodeView, call: Callable[[], object], inputs: object
    ) -> object:
        del inputs
        calls.append(node.name)
        value = call()
        return value * 2 if node.name == "details" else value  # ty: ignore[unsupported-operator]

    module = make_module("blocks_wrapped", PICKED)
    built = fnloom.Builder().with_modules(module).with_node_wrappers(doubled).build()
    assert built.execute(["summary"], inputs={"n": 3, "rate": 1}) == {
        "summary": [0, 2, 4]
    }
    assert calls == ["picked", "details", "details", "details", "summary"]


FAILING = """
from fnloom import Collect, Parallelizable

closed = []
ran = []


def picked(n: int) -> Parallelizable[int]:
    try:
        yield from range(n)
    finally:
        closed.append(True)


def details(picked: int) -> int:
    ran.append(picked)
    if picked == 2:
        raise KeyError(picked)
    return picked


def summary(details: Collect[int]) -> list[int]:
    return details
"""


def test_a_failing_branch_closes_the_generator_once() -> None:
    """Review Focus 1: branch 2 fails, and the generator's `finally` runs once.

    Nothing of branches 0 and 1 is returned, and the error is the node's own, noted.
    """
    module = make_module("blocks_failing", FAILING)
    recorder = Recorder()
    built = fnloom.Builder().with_modules(module).with_hooks(recorder).build()
    with pytest.raises(KeyError) as info:
        built.execute(["summary"], inputs={"n": 5})
    assert info.value.args == (2,)
    assert info.value.__notes__ == [
        "while computing 'details' for branch (2,) of 'picked'"
    ]
    assert module.closed == [True]
    assert module.ran == [0, 1, 2]
    assert ("end", "details", (2,)) in recorder.events
    assert recorder.expands == []
    assert not any(name == "summary" for _, name, _ in recorder.events)


def test_a_failing_nested_branch_names_its_whole_path() -> None:
    """The note gives the full branch path and the innermost expand."""
    module = make_module(
        "blocks_failing_nested",
        NESTED.replace("return outer * 10 + inner", "return 1 // (outer - 2)"),
    )
    with pytest.raises(ZeroDivisionError) as info:
        fnloom.build(module).execute(["everything"])
    assert info.value.__notes__ == [
        "while computing 'leaf' for branch (1, 0) of 'inner'"
    ]


def test_print_nodes_times_each_branch_apart() -> None:
    """`PrintNodes` keys its times by branch, and names a block node's branch."""
    lines: list[str] = []
    built = _hooked("blocks_print", PICKED, fnloom.PrintNodes(print_fn=lines.append))
    built.execute(["summary"], inputs={"n": 2, "rate": 1})
    assert [line.split(" ran in ")[0] for line in lines] == [
        "node 'picked'",
        "node 'details' (branch (0,))",
        "node 'details' (branch (1,))",
        "node 'summary'",
    ]


def test_a_nested_generator_expand_reads_its_branch_while_yielding() -> None:
    """A generator's body runs as its items are read, still in its own branch."""
    module = make_module(
        "blocks_nested_body",
        """
        from fnloom import Collect, Parallelizable, current_branch

        seen = []

        def outer() -> Parallelizable[int]:
            seen.append(("outer", current_branch()))
            yield from [1, 2]

        def inner(outer: int) -> Parallelizable[int]:
            seen.append(("inner", current_branch()))
            yield from range(outer)
            seen.append(("inner done", current_branch()))

        def leaf(inner: int) -> int:
            return inner

        def per_outer(leaf: Collect[int]) -> int:
            return len(leaf)

        def total(per_outer: Collect[int]) -> int:
            return sum(per_outer)
        """,
    )
    assert fnloom.build(module).execute(["total"]) == {"total": 3}
    assert module.seen == [
        ("outer", ()),
        ("inner", (0,)),
        ("inner done", (0,)),
        ("inner", (1,)),
        ("inner done", (1,)),
    ]
    assert fnloom.current_branch() == ()


# Decorators on expand nodes (task 14).


def test_replace_may_replace_an_expand() -> None:
    """The replacing expand gives the items its block runs over (Plan 12 PR F)."""
    module = make_module("blocks_replaced", PICKED)
    evens = make_module(
        "blocks_replacing",
        """
        from fnloom import Parallelizable

        def picked(n: int) -> Parallelizable[int]:
            return [i * 2 for i in range(n)]
        """,
    )
    driver = (
        fnloom.Builder()
        .with_modules(module)
        .with_modules(evens, replace=("picked",))
        .build()
    )
    assert driver.nodes["picked"].kind == "expand"
    assert driver.nodes["picked"].replaced == "blocks_replaced"
    assert driver.execute(["summary"], inputs={"n": 3, "rate": 10}) == {
        "summary": [0, 20, 40]
    }


def test_parameterize_on_an_expand_makes_one_block_per_row() -> None:
    """Each row is an expand of its own, so a node reading two rows is refused."""
    rows = """
from fnloom import Collect, Parallelizable, parameterize, value

@parameterize(small={"n": value(2)}, large={"n": value(3)})
def picked(n: int) -> Parallelizable[int]:
    yield from range(n)

def small_doubled(small: int) -> int:
    return small * 2

def large_doubled(large: int) -> int:
    return large * 2

def sums(small_doubled: Collect[int], large_doubled: Collect[int]) -> list[int]:
    return [sum(small_doubled), sum(large_doubled)]
"""
    driver = fnloom.build(make_module("blocks_parameterized", rows))
    shown = {name: (view.kind, view.block) for name, view in driver.nodes.items()}
    assert shown == {
        "large": ("expand", None),
        "large_doubled": ("function", "large"),
        "small": ("expand", None),
        "small_doubled": ("function", "small"),
        "sums": ("collect", None),
    }
    assert driver.execute(["sums"]) == {"sums": [2, 6]}
    paired = rows + "\ndef both(small: int, large: int) -> int:\n    return small\n"
    with pytest.raises(ValueError, match="unrelated") as info:
        fnloom.build(make_module("blocks_parameterized_paired", paired))
    assert str(info.value) == (
        "'both' reads blocks of 'large' and 'small', which are unrelated; collect "
        "both in one node to pair them"
    )


# Absence in blocks (task 15).

DECLINING = """
from fnloom import ABSENT, Absent, Collect, Parallelizable


def picked(n: int) -> Parallelizable[int]:
    yield from range(n)


def odd(picked: int) -> int | Absent:
    return picked if picked % 2 else ABSENT


def tenfold(odd: int) -> int:
    return odd * 10


def present(tenfold: Collect[int]) -> list[int]:
    return list(tenfold)


def placed(tenfold: Collect[int | Absent]) -> list[object]:
    return list(tenfold)
"""


class Skips(fnloom.Hook):
    """The nodes each run skipped, with their branches."""

    def __init__(self) -> None:
        """Starts with none."""
        self.skipped: tuple[tuple[str, tuple[int, ...]], ...] = ()

    @override
    def after_run(self, event: fnloom.RunEnd) -> None:
        self.skipped = event.skipped


def test_collect_drops_declined_branches_in_order() -> None:
    """`Collect[T]` gathers the branches that gave a value, in item order."""
    skips = Skips()
    module = make_module("blocks_declining_present", DECLINING)
    driver = fnloom.Builder().with_modules(module).with_hooks(skips).build()
    assert driver.nodes["tenfold"].may_be_absent
    assert not driver.nodes["present"].may_be_absent
    assert driver.execute(["present"], inputs={"n": 5}) == {"present": [10, 30]}
    assert skips.skipped == (("tenfold", (0,)), ("tenfold", (2,)), ("tenfold", (4,)))


def test_collect_with_absent_keeps_places() -> None:
    """`Collect[T | Absent]` has an entry per item, `ABSENT` where a branch declined."""
    driver = fnloom.build(make_module("blocks_declining_placed", DECLINING))
    assert driver.execute(["placed"], inputs={"n": 4}) == {
        "placed": [fnloom.ABSENT, 10, fnloom.ABSENT, 30]
    }


MAYBE_EMPTY = """
from fnloom import ABSENT, Absent, Collect, Parallelizable


def picked(items: list[int] | None) -> Parallelizable[int] | Absent:
    return ABSENT if items is None else items


def doubled(picked: int) -> int:
    return picked * 2


def given(doubled: Collect[int] | Absent) -> list[int] | str:
    return "absent" if doubled is ABSENT else doubled


def defaulted(doubled: Collect[int] = [-1]) -> list[int]:
    return doubled


def plain(doubled: Collect[int]) -> list[int]:
    return doubled
"""


def test_an_absent_expand_is_not_an_empty_one() -> None:
    """An empty expand collects `[]`; an absent one, `ABSENT`, a default or a skip."""
    skips = Skips()
    module = make_module("blocks_absent_expand", MAYBE_EMPTY)
    driver = fnloom.Builder().with_modules(module).with_hooks(skips).build()
    shown = {n: driver.nodes[n].may_be_absent for n in ("given", "defaulted", "plain")}
    assert shown == {"given": False, "defaulted": False, "plain": True}
    outputs = ["given", "defaulted", "plain"]
    assert driver.execute(outputs, inputs={"items": []}) == {
        "given": [],
        "defaulted": [],
        "plain": [],
    }
    assert skips.skipped == ()
    assert driver.execute(outputs, inputs={"items": None}) == {
        "given": "absent",
        "defaulted": [-1],
        "plain": fnloom.ABSENT,
    }
    assert skips.skipped == (("plain", ()),)


MIDWAY = """
from fnloom import ABSENT, Absent, Collect, Parallelizable

seen = []


def picked() -> Parallelizable[str]{admits}:
    yield "a"
    yield "b"
    return ABSENT


def shout(picked: str) -> str:
    seen.append(picked)
    return picked.upper()


def shouted(shout: Collect[str] | Absent) -> str:
    return "declined" if shout is ABSENT else ",".join(shout)
"""


def test_items_already_pulled_are_dropped_when_the_expand_declines_midway() -> None:
    """A generator returning `ABSENT` declines as a whole, after its branches ran."""
    module = make_module("blocks_midway", MIDWAY.format(admits=" | Absent"))
    assert fnloom.build(module).execute(["shouted"]) == {"shouted": "declined"}
    assert module.seen == ["a", "b"]
    refused = make_module("blocks_midway_refused", MIDWAY.format(admits=""))
    with pytest.raises(TypeError, match="ABSENT") as info:
        fnloom.build(refused).execute(["shouted"])
    assert str(info.value) == (
        "node 'picked' returned fnloom.ABSENT, but its return annotation does not "
        "admit fnloom.Absent"
    )


EDGE = """
from fnloom import ABSENT, Absent, Collect, Parallelizable


def picked() -> Parallelizable[int]:
    yield from range(3)


def odd(picked: int) -> int | Absent:
    return picked if picked % 2 else ABSENT


def present(odd: Collect[{element}]) -> list[object]:
    return list(odd)
"""


def test_edge_check_of_t_or_absent_into_collect_t() -> None:
    """`T | Absent` feeds `Collect[T]`, its absences dropped; `Absent` isn't `T`."""
    fits = make_module("blocks_edge_fits", EDGE.format(element="int"))
    assert fnloom.build(fits).execute(["present"]) == {"present": [1]}
    misfit = make_module("blocks_edge_misfit", EDGE.format(element="str"))
    with pytest.raises(ValueError, match="collects elements") as info:
        fnloom.build(misfit)
    assert str(info.value) == (
        "present.odd collects elements of type str but odd returns int | fnloom.Absent"
    )


def test_a_collect_default_is_shadowed_unless_the_expand_may_decline() -> None:
    """A collect is absent only with its expand: else its default is never used."""
    shadowed = EDGE.format(element="int").replace(
        "Collect[int])", "Collect[int] = [-1])"
    )
    with pytest.raises(ValueError, match="shadowed default") as info:
        fnloom.build(make_module("blocks_collect_shadowed", shadowed))
    assert str(info.value).startswith(
        "shadowed default: parameter 'odd' of 'present' has a default, but it reads "
        "the node 'odd'"
    )
