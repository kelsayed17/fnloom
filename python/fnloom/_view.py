"""Read-only descriptions of a built graph's nodes."""

import inspect
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, NamedTuple

from fnloom._decorators import Source, Value
from fnloom._types import type_matches

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from fnloom._scan import NodeDef


@dataclass(frozen=True)
class Param:
    """One parameter of a node's function, and what it is bound to.

    `name` is the function's own parameter. A plain parameter is bound to
    `Source(name)`. `required` is False when the parameter has a default and is
    bound to a source; a `Value`-bound parameter is always supplied, so required.
    """

    name: str
    binding: Source | Value
    required: bool
    annotation: object


@dataclass(frozen=True)
class NodeView:
    """One node of a built graph: a function's node or an input.

    `type` is a function node's return annotation, or an input's type: the
    narrowest annotation of the parameters that read it. `origin`, `module` and
    `doc` are the function's, and `None` for an input; `params` follow the
    function's declaration order and are empty for an input. `tags` are the ones
    written with `@tag`, read-only.
    """

    name: str
    type: object
    is_input: bool
    origin: Callable[..., object] | None
    module: str | None
    params: tuple[Param, ...]
    tags: Mapping[str, str]
    doc: str | None

    @property
    def dependencies(self) -> tuple[str, ...]:
        """The distinct nodes and inputs this node reads, sorted."""
        return tuple(
            sorted(
                {p.binding.name for p in self.params if isinstance(p.binding, Source)}
            )
        )


class _Consumer(NamedTuple):
    """A parameter reading an input: its node, its own name and its annotation."""

    node: str
    param: str
    annotation: object


def _describe(input_name: str, consumers: list[_Consumer]) -> str:
    uses = ", ".join(
        f"{inspect.formatannotation(c.annotation)} in {c.node!r}"
        + ("" if c.param == input_name else f" (parameter {c.param!r})")
        for c in consumers
    )
    return f"{input_name!r} is {uses}"


_INCOMPATIBLE = object()


def _narrowest(annotations: list[object]) -> object:
    """The narrowest of `annotations` by `type_matches`, or `_INCOMPATIBLE`.

    Each annotation in turn is either fed by the narrowest so far (it is wider,
    and dropped) or feeds it (it is narrower, and replaces it); when both hold,
    the earlier stays, as Hamilton keeps the type it saw first.
    """
    narrowest = annotations[0]
    for annotation in annotations[1:]:
        if type_matches(narrowest, annotation):
            continue
        if not type_matches(annotation, narrowest):
            return _INCOMPATIBLE
        narrowest = annotation
    return narrowest


def _input_types(nodes: Sequence[NodeDef]) -> dict[str, object]:
    """Each input's type, the narrowest its consumers declare.

    `ValueError` listing every input with two consumers whose types do not match
    either way, naming all of that input's consumers.
    """
    defined = {node.name for node in nodes}
    consumers: dict[str, list[_Consumer]] = {}
    for node in nodes:
        for p in node.params:
            if isinstance(p.binding, Source) and p.binding.name not in defined:
                consumer = _Consumer(node.name, p.name, p.annotation)
                consumers.setdefault(p.binding.name, []).append(consumer)
    types: dict[str, object] = {}
    conflicts: list[str] = []
    for name in sorted(consumers):
        found = sorted(consumers[name], key=lambda c: (c.node, c.param))
        narrowest = _narrowest([c.annotation for c in found])
        if narrowest is _INCOMPATIBLE:
            conflicts.append(_describe(name, found))
        else:
            types[name] = narrowest
    if conflicts:
        msg = "inputs have conflicting types: " + "; ".join(conflicts)
        raise ValueError(msg)
    return types


def node_views(nodes: Sequence[NodeDef]) -> Mapping[str, NodeView]:
    """The views of `nodes` and of the inputs they read, read-only, sorted by name.

    `nodes` carry resolved annotations. `ValueError` when two of an input's
    consumers declare types that match neither way.
    """
    no_tags = MappingProxyType[str, str]({})
    views = {
        name: NodeView(
            name=name,
            type=type_,
            is_input=True,
            origin=None,
            module=None,
            params=(),
            tags=no_tags,
            doc=None,
        )
        for name, type_ in _input_types(nodes).items()
    }
    for node in nodes:
        views[node.name] = NodeView(
            name=node.name,
            type=node.returns,
            is_input=False,
            origin=node.origin,
            module=node.module,
            params=node.params,
            tags=MappingProxyType(dict(node.tags)),
            doc=inspect.getdoc(node.origin),
        )
    return MappingProxyType(dict(sorted(views.items())))
