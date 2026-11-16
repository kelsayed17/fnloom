"""Fan-out and fan-in: `Parallelizable[T]` expands a block, `Collect[T]` gathers it.

A node annotated `-> Parallelizable[T]` is an expand: its value, any iterable, is read
item by item, and every node reading it, directly or through other such nodes, runs
once per item, in item order. That set of nodes is the expand's block. A parameter
annotated `Collect[T]` reads a block node's value from every branch, as a `list`, in
branch order. The core finds the blocks and runs them; this module reads the
annotations, checks the items and holds the branch a node runs in.
"""

import contextlib
import inspect
import types
import typing
from collections.abc import AsyncIterable, AsyncIterator, Callable, Iterable, Iterator
from contextvars import ContextVar
from typing import TYPE_CHECKING, Annotated, Final, Literal, Union, override

from fnloom._absence import ABSENT, admits_absent, own, without_absent
from fnloom._types import Rules, ValueCheck, admitting, checks_of

if TYPE_CHECKING:
    from collections.abc import Sequence

    from fnloom._caching import LateKeys
    from fnloom._scan import NodeDef

__all__ = [
    "BRANCH",
    "AsyncItems",
    "Collect",
    "Items",
    "Parallelizable",
    "collect_mode",
    "current_branch",
    "expanded_type",
    "expands",
    "item_check",
    "items",
    "refuse",
]


class _Marker:
    """The metadata that tells `Parallelizable` and `Collect` from their plain types."""

    __slots__ = ("_name",)

    def __init__(self, name: str) -> None:
        self._name = name

    @override
    def __repr__(self) -> str:
        return f"fnloom.{self._name}"


_EXPAND: Final = _Marker("expand")
_COLLECT: Final = _Marker("collect")

# PEP 695 aliases, so a checker reads `Iterable[T]` and `list[T]`: an expand may be a
# generator, and `len()` and indexing type-check on what a collect receives.
type Parallelizable[T] = Annotated[Iterable[T], _EXPAND]
type Collect[T] = Annotated[list[T], _COLLECT]

BRANCH: ContextVar[tuple[int, ...]] = ContextVar("fnloom.branch", default=())


def current_branch() -> tuple[int, ...]:
    """The branch the running node is in: one item index per block, outermost first.

    `()` outside every block. Readable from a node, its output checks and its
    wrappers; the core sets it around each call of a block node.
    """
    return BRANCH.get()


def _alias_argument(annotation: object, alias: object) -> object | None:
    """`T` of `alias[T]` at the top level of `annotation`, a union's members included.

    shortcut: a PEP 695 alias naming a union holding `alias[T]` is not read through;
    read it when someone needs `type Maybe = Parallelizable[T] | Absent`.
    """
    if isinstance(annotation, type):
        # The common case: a plain class is neither.
        return None
    # Metadata never changes an answer (Q41), at the top or in a union's member.
    annotation = _unannotated(annotation)
    parts = (
        typing.get_args(annotation)
        if typing.get_origin(annotation) in {Union, types.UnionType}
        else (annotation,)
    )
    for part in map(_unannotated, parts):
        if part is alias:
            return typing.Any
        if typing.get_origin(part) is alias:
            return typing.get_args(part)[0]
    return None


def _unannotated(annotation: object) -> object:
    """`annotation` without `Annotated` metadata; a PEP 695 alias left as written."""
    while typing.get_origin(annotation) is Annotated:
        annotation = typing.get_args(annotation)[0]
    return annotation


def expanded_type(annotation: object) -> object | None:
    """The item type `T` of an expand's return annotation, or `None` for no expand.

    `Parallelizable[T] | Absent` is an expand that may decline as a whole.
    """
    return _alias_argument(annotation, Parallelizable)


def expands(annotation: object) -> bool:
    """Whether a node returning `annotation` is an expand."""
    return expanded_type(annotation) is not None


def collect_mode(
    annotation: object,
) -> tuple[Literal["present", "all"], object] | None:
    """How a parameter annotated `annotation` collects, and the element type, or `None`.

    `Collect[T]` keeps the present branches, `("present", T)`; `Collect[T | Absent]`
    keeps every branch, absent ones as `ABSENT`, `("all", T)` with `Absent` taken out.
    `Collect[T] | Absent` collects too: it also admits a whole absent block.
    """
    element = _alias_argument(annotation, Collect)
    if element is None:
        return None
    if admits_absent(element):
        return "all", without_absent(element)
    return "present", element


def refuse(nodes: Sequence[NodeDef], *, awaits: bool) -> None:
    """`ValueError` for each node that can't be what its annotation says, sorted.

    A generator function that is no expand stays refused (Q32), with the message
    the scan gave before an expand could be one, and so does an async generator
    function that is no expand; an expand whose items may be absent is refused.
    The scan refuses an async generator expand on `build` (Q32).
    """
    lines: list[str] = []
    for node in nodes:
        item = expanded_type(node.returns)
        if item is None:
            # Read off the code's flags, as the scan does: far cheaper per node.
            code = getattr(node.origin, "__code__", None)
            flags = 0 if code is None else code.co_flags
            if flags & (inspect.CO_GENERATOR | inspect.CO_ASYNC_GENERATOR):
                kind = (
                    "async generator"
                    if flags & inspect.CO_ASYNC_GENERATOR
                    else "generator"
                )
                tail = (
                    "calling it returns a generator, not the node's value"
                    if awaits
                    else "nodes are called synchronously"
                )
                lines.append(f"{node.name!r}: {kind} function is not supported; {tail}")
            continue
        if admits_absent(item):
            shown = inspect.formatannotation(item)
            present = inspect.formatannotation(without_absent(item))
            lines.append(
                f"{node.name!r} yields items of type {shown}, but an item can't be "
                f"absent; write Parallelizable[{present}] | Absent to let the "
                "expand decline as a whole"
            )
    if lines:
        raise ValueError("; ".join(sorted(lines)))


def item_check(
    declared: object, rules: Rules, value_checks: tuple[ValueCheck, ...]
) -> Callable[[object], str | None]:
    """Why an item does not fit `declared` by the input rules, or `None` when it does.

    The check is made once, here, as a plan's input checks are.
    """
    ((classes, matches),) = checks_of((declared,), rules)
    classes, matches = admitting(declared, (classes, matches), value_checks)
    shown = inspect.formatannotation(declared)

    def problem(item: object) -> str | None:
        if (classes is not None and isinstance(item, classes)) or matches(item):
            return None
        return f"expected {shown}, got {inspect.formatannotation(type(item))}"

    return problem


class Items(Iterator[object]):
    """The item iterator of one run of expand `node`, which the core pulls from.

    Each item is checked by `check`, when given, as an input of the item type is
    (Q127), and an item that is `ABSENT` is refused: an item is never absent. With
    `record`, the items are kept, since the expand is a requested output and its
    value is then the `list` of them (Q121); otherwise none is held.
    """

    __slots__ = ("_branch", "_check", "_index", "_items", "_iterator", "_late", "_node")

    def __init__(  # noqa: PLR0913, PLR0917  # the factory's two, then the binding's four
        self,
        node: str,
        check: Callable[[object], str | None] | None,
        iterable: Iterable[object],
        record: bool,  # noqa: FBT001  # the binding passes it by position
        branch: tuple[int, ...] = (),
        late: LateKeys | None = None,
    ) -> None:
        """Iterates `iterable`, the items of `node`, which runs in `branch`.

        `late`, a cached run's late keys, hears of each item and of the end, with
        `branch`, so branches read at once each key their own items.
        """
        self._node = node
        self._branch = branch
        self._late = late
        self._check = check
        self._iterator = iter(iterable)
        self._index = 0
        self._items: list[object] | None = [] if record else None

    @override
    def __next__(self) -> object:
        """The next item, checked; `StopIteration` as the iterable raised it.

        An iterator that raised anything else is closed at once, and only once: the
        run's end may not close it, when the error makes the expand absent (Q133).
        """
        try:
            item = self._in_branch(next, self._iterator)
        except StopIteration:
            raise
        except Exception:
            # What closing raises is dropped, as for a stopped run's iterator.
            with contextlib.suppress(Exception):
                self.close()
            self._iterator = iter(())
            raise
        return self._kept(item)

    def _kept(self, item: object) -> object:
        """`item`, the next one read, once checked, recorded and reported."""
        index = self._index
        self._index += 1
        if item is ABSENT:
            msg = (
                f"item {index} of {self._node!r} is fnloom.ABSENT, which an item "
                "can't be"
            )
            raise own(TypeError(msg))
        if self._check is not None and (problem := self._check(item)) is not None:
            msg = f"item {index} of {self._node!r}: {problem}"
            raise own(TypeError(msg))
        if self._items is not None:
            self._items.append(item)
        if self._late is not None:
            self._late.pulled(self._node, self._branch, item)
        return item

    def close(self) -> None:
        """Closes the iterable's iterator if it can be: a generator's `finally` runs."""
        close = getattr(self._iterator, "close", None)
        if callable(close):
            self._in_branch(close)

    def _in_branch[**A, T](
        self, call: Callable[A, T], *args: A.args, **kwargs: A.kwargs
    ) -> T:
        """`call(...)` with `current_branch()` set to the expand's branch.

        A generator's body runs as its items are read, so in its expand's branch.
        """
        if not self._branch:
            return call(*args, **kwargs)
        token = BRANCH.set(self._branch)
        try:
            return call(*args, **kwargs)
        finally:
            BRANCH.reset(token)

    def finish(self, exhausted: bool) -> list[object] | None:  # noqa: FBT001  # the binding passes it by position
        """Ends the iteration, closing it unless `exhausted`; the items, if recorded."""
        if not exhausted:
            self.close()
        elif self._late is not None:
            self._late.finished(self._node, self._branch, self._items)
        return self._items


async def _nothing() -> AsyncIterator[object]:
    """An async iterator with no items: what a closed `AsyncItems` reads."""
    return
    yield


class AsyncItems(Items):
    """`Items` of an async generator expand, read with `__anext__` by the async driver.

    `current_branch()` is the expand's in each read, since the driver awaits every
    read of one iterator in one context, in the expand's branch.
    """

    __slots__ = ("_aiterator",)

    def __init__(  # noqa: PLR0913, PLR0917  # as `Items`
        self,
        node: str,
        check: Callable[[object], str | None] | None,
        iterable: AsyncIterable[object],
        record: bool,  # noqa: FBT001  # the binding passes it by position
        branch: tuple[int, ...] = (),
        late: LateKeys | None = None,
    ) -> None:
        """Iterates `iterable`, as `Items` does."""
        super().__init__(node, check, (), record, branch, late)
        self._aiterator = aiter(iterable)

    @override
    def __next__(self) -> object:
        """`TypeError`: async items are read only by the async driver."""
        msg = (
            f"the items of {self._node!r} are an async iterable, which only "
            "build_async() reads"
        )
        raise TypeError(msg)

    async def __anext__(self) -> object:
        """The next item, checked; `StopAsyncIteration` at the end.

        An iterator that raised anything else is closed at once, and only once, as
        `Items.__next__` closes one.
        """
        try:
            item = await anext(self._aiterator)
        except StopAsyncIteration:
            raise
        except Exception:
            with contextlib.suppress(Exception):
                await self.afinish(False)  # noqa: FBT003  # `afinish`'s flag, by position
            self._aiterator = _nothing()
            raise
        return self._kept(item)

    async def afinish(self, exhausted: bool) -> list[object] | None:  # noqa: FBT001  # as `finish`
        """Ends the iteration, closing it unless `exhausted`; the items, if recorded.

        An async generator is closed with `aclose()`, so its `finally` runs.
        """
        if not exhausted:
            close = getattr(self._aiterator, "aclose", None)
            if callable(close):
                await close()
            return self._items
        return self.finish(exhausted)


def items(  # noqa: PLR0913, PLR0917  # the factory's two, then the binding's four
    node: str,
    check: Callable[[object], str | None] | None,
    iterable: Iterable[object] | AsyncIterable[object],
    record: bool,  # noqa: FBT001  # the binding passes it by position
    branch: tuple[int, ...] = (),
    late: LateKeys | None = None,
) -> Items:
    """The item iterator of one run of expand `node`, `Items` or `AsyncItems`.

    `AsyncItems` over an async iterable that is not also a sync one, which only the
    async driver reads; an object that is both is read as `Items`, on either driver.
    """
    if isinstance(iterable, AsyncIterable) and not isinstance(iterable, Iterable):
        return AsyncItems(node, check, iterable, record, branch, late)
    return Items(node, check, iterable, record, branch, late)
