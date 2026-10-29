"""Node wrappers: an ordered chain around each computed node's call (Q111, Q24).

A wrapper is a callable `wrapper(node, call, inputs)`: `node` is the node's
`NodeView`, `call()` runs the rest of the chain and then the node, its checks
included, and `inputs` are the node's arguments by parameter name, read-only.
What the wrapper returns is the node's value. The first wrapper given is the
outermost. Wrappers are composed at build, so a driver with none runs its nodes
as they are.
"""

import annotationlib
import inspect
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from types import MappingProxyType
from typing import TYPE_CHECKING

from fnloom import _async
from fnloom._scan import by_parameter

if TYPE_CHECKING:
    from fnloom._scan import NodeDef
    from fnloom._view import NodeView


# Annotations as written: a name imported only under TYPE_CHECKING is no error.
_FORWARD = annotationlib.Format.FORWARDREF

type NodeWrapper = Callable[
    [NodeView, Callable[[], object], Mapping[str, object]], object
]


def checked(wrappers: Sequence[object]) -> tuple[NodeWrapper, ...]:
    """`wrappers`, each refused unless it can wrap a node: `TypeError` naming each."""
    problems: list[str] = []
    for index, wrapper in enumerate(wrappers):
        name = getattr(wrapper, "__qualname__", None) or type(wrapper).__name__
        where = f"node wrapper {index} ({name})"
        if not callable(wrapper):
            problems.append(f"{where} is not callable")
            continue
        method = getattr(wrapper, "__call__", None)  # noqa: B004  # its async-ness
        if inspect.iscoroutinefunction(wrapper) or inspect.iscoroutinefunction(method):
            problems.append(f"{where} is an async def; wrappers run synchronously")
            continue
        try:
            inspect.signature(wrapper, annotation_format=_FORWARD).bind(
                None, None, None
            )
        except TypeError:
            problems.append(f"{where} can't take (node, call, inputs)")
        except ValueError:
            pass
    if problems:
        raise TypeError("; ".join(problems))
    return tuple(wrappers)  # ty: ignore[invalid-return-type]  # checked callables


def refuse_async(nodes: Sequence[NodeDef]) -> None:
    """`TypeError` naming each `async def` node, which wrappers can't wrap yet."""
    if found := sorted(node.name for node in nodes if _async.awaits(node)):
        listed = ", ".join(repr(n) for n in found)
        msg = f"node wrappers wrap sync nodes only; these are async def: {listed}"
        raise TypeError(msg)


def wrapped(
    nodes: Sequence[NodeDef],
    views: Mapping[str, NodeView],
    wrappers: tuple[NodeWrapper, ...],
) -> list[NodeDef]:
    """`nodes`, each one's payload run through `wrappers`, the first outermost."""
    if not wrappers:
        return list(nodes)
    return [
        replace(node, payload=_chained(node, views[node.name], wrappers))
        for node in nodes
    ]


def _chained(
    node: NodeDef, view: NodeView, wrappers: tuple[NodeWrapper, ...]
) -> Callable[..., object]:
    payload = node.payload
    keyed = by_parameter(node.params)
    last = len(wrappers)

    def call(**kwargs: object) -> object:
        inputs = MappingProxyType(keyed(kwargs) if keyed else dict(kwargs))

        def at(index: int) -> object:
            if index == last:
                return payload(**kwargs)
            return wrappers[index](view, lambda: at(index + 1), inputs)

        return at(0)

    return call
