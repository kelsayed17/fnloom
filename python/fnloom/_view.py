"""Read-only descriptions of a built graph's nodes."""

import inspect

# `Callable` is imported for real: `NodeView`'s signature annotates with it, and
# `inspect.signature` resolves that at run time.
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, NamedTuple, override

from fnloom._config import Condition
from fnloom._decorators import Source, Value
from fnloom._types import type_matches

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

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
    written with `@tag`, read-only. `config` holds the conditions of the
    `@config.when*` variant that is the node, sorted by key; it is empty for an
    input, a plain function and a `@config.default`.
    """

    name: str
    type: object
    is_input: bool
    origin: Callable[..., object] | None
    module: str | None
    params: tuple[Param, ...]
    tags: Mapping[str, str]
    doc: str | None
    config: tuple[Condition, ...] = ()

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
    # Plain (node, param, annotation) tuples: a `_Consumer` is made only for an
    # input read more than once, the only kind whose consumers are compared.
    consumers: dict[str, list[tuple[str, str, object]]] = {}
    for node in nodes:
        for p in node.params:
            if isinstance(p.binding, Source) and p.binding.name not in defined:
                consumer = (node.name, p.name, p.annotation)
                consumers.setdefault(p.binding.name, []).append(consumer)
    types: dict[str, object] = {}
    conflicts: list[str] = []
    for name in sorted(consumers):
        read = consumers[name]
        if len(read) == 1:
            types[name] = read[0][2]
            continue
        found = sorted((_Consumer(*c) for c in read), key=lambda c: (c.node, c.param))
        narrowest = _narrowest([c.annotation for c in found])
        if narrowest is _INCOMPATIBLE:
            conflicts.append(_describe(name, found))
        else:
            types[name] = narrowest
    if conflicts:
        msg = "inputs have conflicting types: " + "; ".join(conflicts)
        raise ValueError(msg)
    return types


_NO_TAGS = MappingProxyType[str, str]({})


class NodeViews(Mapping[str, NodeView]):
    """Every node of a built graph by name, inputs included: read-only, sorted.

    A function node's view is made at build. An input's view is made when it is
    first read and then kept, so a graph with many inputs pays only for the views
    that are read.
    """

    def __init__(
        self, functions: Mapping[str, NodeView], inputs: Mapping[str, object]
    ) -> None:
        """Holds the function nodes' views, and each input's type by name."""
        self._functions = functions
        self._inputs = inputs
        self._made: dict[str, NodeView] = {}
        self._names: list[str] | None = None

    def type_of(self, name: str) -> object:
        """The type of node or input `name`, without making an input's view."""
        if (view := self._functions.get(name)) is not None:
            return view.type
        return self._inputs[name]

    @property
    def functions(self) -> Mapping[str, NodeView]:
        """The function nodes' views alone, by name, in no set order."""
        return self._functions

    @override
    def __getitem__(self, name: str) -> NodeView:
        """The view of node or input `name`; `KeyError` when there is none."""
        if (view := self._functions.get(name)) is not None:
            return view
        if (view := self._made.get(name)) is not None:
            return view
        type_ = self._inputs[name]
        view = NodeView(name, type_, True, None, None, (), _NO_TAGS, None)  # noqa: FBT003  # every field, in order
        # One view per input, even when two threads read it at once.
        return self._made.setdefault(name, view)

    @override
    def __contains__(self, name: object) -> bool:
        """Whether `name` is a node or input of the graph."""
        return name in self._functions or name in self._inputs

    @override
    def __iter__(self) -> Iterator[str]:
        """The names of every node and input, sorted."""
        if self._names is None:
            self._names = sorted([*self._functions, *self._inputs])
        return iter(self._names)

    @override
    def __len__(self) -> int:
        """How many nodes and inputs the graph has."""
        return len(self._functions) + len(self._inputs)

    @override
    def __repr__(self) -> str:
        """The views as a dict, sorted by name."""
        return f"{type(self).__name__}({dict(self)!r})"


def _read_only(tags: Mapping[str, str]) -> Mapping[str, str]:
    """`tags` read-only: as they are when the decorators already made them so."""
    if isinstance(tags, MappingProxyType):
        return tags
    return MappingProxyType(dict(tags))


def _doc(fn: Callable[..., object]) -> str | None:
    """`inspect.getdoc(fn)`, without its search for a docstring a class could give.

    An undocumented function defined at module level belongs to no class, so it
    has no docstring to inherit: the common case, and the costly search skipped.
    """
    if fn.__doc__ is None and "." not in getattr(fn, "__qualname__", "."):
        return None
    return inspect.getdoc(fn)


def node_views(nodes: Sequence[NodeDef]) -> NodeViews:
    """The views of `nodes` and of the inputs they read, read-only, sorted by name.

    `nodes` carry resolved annotations. `ValueError` when two of an input's
    consumers declare types that match neither way.
    """
    # Positional, every field in order: a view is made per node, so it is kept cheap.
    functions = {
        node.name: NodeView(
            node.name,
            node.returns,
            False,  # noqa: FBT003  # `is_input`, in field order
            node.origin,
            node.module,
            node.params,
            _read_only(node.tags),
            _doc(node.origin) if node.doc is None else node.doc,
            node.config,
        )
        for node in nodes
    }
    return NodeViews(functions, _input_types(nodes))
