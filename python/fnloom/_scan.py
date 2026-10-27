"""Scans modules for node definitions: public functions defined in the module itself."""

import annotationlib
import inspect
import typing
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from fnloom._decorators import Source, Value, decorations
from fnloom._view import Param

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from types import ModuleType

# What evaluating an annotation raises when a name in it does not resolve.
type _Unresolved = NameError | AttributeError

_UNSUPPORTED_KINDS = {
    inspect.Parameter.POSITIONAL_ONLY: "positional-only parameter",
    inspect.Parameter.VAR_POSITIONAL: "*args parameter",
    inspect.Parameter.VAR_KEYWORD: "**kwargs parameter",
}


@dataclass(frozen=True)
class NodeDef:
    """A node found in a module.

    `origin` is the function as written; `payload` is what the core calls with one
    keyword per core input: `origin` itself, or a call that maps those keywords onto
    `origin`'s parameters. `returns` is the return annotation: as written after
    `collect`, its value after `resolve`, as are the params' annotations.
    """

    name: str
    params: tuple[Param, ...]
    tags: Mapping[str, str]
    payload: Callable[..., object]
    origin: Callable[..., object]
    module: str
    returns: object


def core_inputs(params: tuple[Param, ...]) -> list[tuple[str, bool]]:
    """The distinct sources `params` read, first-appearance order, with requiredness.

    A source is required when any parameter bound to it is.
    """
    required: dict[str, bool] = {}
    for param in params:
        if isinstance(param.binding, Source):
            name = param.binding.name
            required[name] = required.get(name, False) or param.required
    return list(required.items())


def _is_node(module: ModuleType, name: str, value: object) -> bool:
    return (
        inspect.isfunction(value)
        and value.__module__ == module.__name__
        and not name.startswith("_")
    )


def _unsupported_function_kind(fn: Callable[..., object]) -> str | None:
    """Names the kind of `fn` when calling it would not run its body to a value."""
    if inspect.iscoroutinefunction(fn):
        return "async function"
    if inspect.isasyncgenfunction(fn):
        return "async generator function"
    if inspect.isgeneratorfunction(fn):
        return "generator function"
    return None


def _check_param(fn_name: str, param: inspect.Parameter, annotated: set[str]) -> None:
    if param.kind in _UNSUPPORTED_KINDS:
        kind = _UNSUPPORTED_KINDS[param.kind]
        msg = f"{fn_name!r}: {kind} {param.name!r} is not supported"
        raise ValueError(msg)
    if param.name not in annotated:
        msg = f"{fn_name!r}: parameter {param.name!r} has no type annotation"
        raise ValueError(msg)


def _bind(
    parameters: Sequence[inspect.Parameter], row: Mapping[str, Source | Value]
) -> tuple[Param, ...]:
    """Binds each parameter to the row's source or value, or to its own name."""
    params: list[Param] = []
    for p in parameters:
        # A `Source` is made only for a parameter the row does not bind.
        binding = row[p.name] if p.name in row else Source(p.name)
        required = isinstance(binding, Value) or p.default is inspect.Parameter.empty
        params.append(Param(p.name, binding, required, p.annotation))
    return tuple(params)


def _payload(
    origin: Callable[..., object], params: tuple[Param, ...]
) -> Callable[..., object]:
    """`origin` when each parameter reads its own name, else a call that maps keywords.

    A parameter whose source the core did not pass is left out, so its default
    applies. Each `Value` is passed as the object itself, the same on every call.
    """
    if all(type(p.binding) is Source and p.binding.name == p.name for p in params):
        return origin
    sourced = tuple(
        (p.name, p.binding.name) for p in params if isinstance(p.binding, Source)
    )
    constants = {
        p.name: p.binding.value for p in params if isinstance(p.binding, Value)
    }

    def call(**supplied: object) -> object:
        keywords = {
            param: supplied[source] for param, source in sourced if source in supplied
        }
        keywords.update(constants)
        return origin(**keywords)

    return call


def _node_defs(
    module: ModuleType, name: str, fn: Callable[..., object]
) -> list[NodeDef]:
    """One node for a plain function; one per row for a parameterized one."""
    if (kind := _unsupported_function_kind(fn)) is not None:
        msg = f"{name!r}: {kind} is not supported; nodes are called synchronously"
        raise ValueError(msg)
    annotations = annotationlib.get_annotations(
        fn, format=annotationlib.Format.FORWARDREF
    )
    if "return" not in annotations:
        msg = f"{name!r}: return has no type annotation"
        raise ValueError(msg)
    annotated = set(annotations)
    parameters = tuple(
        inspect.signature(
            fn, annotation_format=annotationlib.Format.FORWARDREF
        ).parameters.values()
    )
    for p in parameters:
        _check_param(name, p, annotated)
    recorded = decorations(fn)
    if recorded.rows is None:
        # A plain function is one node, of its own name, binding nothing, so each
        # parameter reads its own name and the payload is the function itself.
        params = _bind(parameters, {})
        returns = annotations["return"]
        return [NodeDef(name, params, recorded.tags, fn, fn, module.__name__, returns)]
    nodes: list[NodeDef] = []
    for node_name, row in recorded.rows.items():
        params = _bind(parameters, row)
        nodes.append(
            NodeDef(
                node_name,
                params,
                recorded.tags,
                _payload(fn, params),
                fn,
                module.__name__,
                annotations["return"],
            )
        )
    return nodes


def _scan_module(module: ModuleType) -> list[NodeDef]:
    return [
        node
        for name, value in vars(module).items()
        if _is_node(module, name, value)
        for node in _node_defs(module, name, value)
    ]


def _definition(node: NodeDef) -> str:
    """How `node` was written: as a function, or as a row of a parameterized one."""
    function = getattr(node.origin, "__name__", node.name)
    if decorations(node.origin).rows is None:
        return f"function {function!r}"
    return f"a row of function {function!r}"


def _duplicate(first: NodeDef, second: NodeDef) -> str:
    if first.module != second.module:
        return (
            f"node {first.name!r} is defined in both "
            f"{first.module!r} and {second.module!r}"
        )
    return (
        f"node {first.name!r} is defined twice in {first.module!r}: "
        f"by {_definition(first)} and by {_definition(second)}"
    )


def collect(modules: Sequence[ModuleType]) -> list[NodeDef]:
    """Returns the nodes of `modules`; `ValueError` on a malformed or duplicate one.

    Every duplicate name is listed, sorted by name, in one `ValueError`.

    Annotations are read without being evaluated; `resolve` evaluates them.
    """
    seen: dict[str, NodeDef] = {}
    clashes: list[tuple[str, str]] = []
    # Modules hash by identity, so this drops repeats of one module, keeping the first.
    for module in dict.fromkeys(modules):
        for node in _scan_module(module):
            if node.name in seen:
                clashes.append((node.name, _duplicate(seen[node.name], node)))
            else:
                seen[node.name] = node
    if clashes:
        # Sorted by node name; a name clashing more than once keeps scan order.
        clashes.sort(key=lambda clash: clash[0])
        raise ValueError("; ".join(message for _, message in clashes))
    return list(seen.values())


def _owner(origin: Callable[..., object], names: list[str]) -> str:
    """Names the nodes of `origin`: its node, or every row of a parameterized one."""
    if decorations(origin).rows is None:
        return f"node {names[0]!r}"
    rows = ", ".join(repr(name) for name in sorted(names))
    function = getattr(origin, "__name__", names[0])
    return f"nodes {rows} (rows of function {function!r})"


def _unresolved(origin: Callable[..., object]) -> list[str]:
    """The annotations of `origin` that do not evaluate, in declaration order."""
    written = annotationlib.get_annotations(origin, format=annotationlib.Format.STRING)
    culprits: list[str] = []
    for key, text in written.items():
        try:
            typing.evaluate_forward_ref(annotationlib.ForwardRef(text, owner=origin))
        except NameError, AttributeError:
            culprits.append("the return" if key == "return" else f"parameter {key!r}")
    return culprits or ["an annotation"]


def _unresolved_note(failures: list[tuple[str, list[str]]]) -> str:
    """One note naming every failing annotation; `failures` are (owner, culprits)."""
    items = [(culprit, owner) for owner, culprits in failures for culprit in culprits]
    if len(items) == 1:
        culprit, owner = items[0]
        if culprit == "the return":
            return f"resolving the return annotation of {owner}"
        return f"resolving the annotation of {culprit} of {owner}"
    listed = "; ".join(f"{culprit} of {owner}" for culprit, owner in items)
    return f"resolving the annotations of: {listed}"


def resolve(nodes: Sequence[NodeDef]) -> list[NodeDef]:
    """Returns `nodes` with their annotations evaluated to values, as Hamilton does.

    `typing.get_type_hints` evaluates them: string annotations, and forward
    references to names defined anywhere in the module, also nested ones such as
    `list["Later"]`, resolve, and a `None` annotation becomes `NoneType`. When
    names do not resolve, the `NameError` (or, for a missing attribute such as
    `decimal.Missing`, the `AttributeError`) of the first failing node by name is
    raised, as Hamilton raises it, with one note naming every failing node and
    annotation, sorted by node.
    """
    origins: dict[int, tuple[Callable[..., object], list[str]]] = {}
    for node in nodes:
        origins.setdefault(id(node.origin), (node.origin, []))[1].append(node.name)
    values: dict[int, dict[str, typing.Any]] = {}
    errors: list[tuple[str, _Unresolved, Callable[..., object], list[str]]] = []
    for key, (origin, names) in origins.items():
        try:
            values[key] = typing.get_type_hints(origin, include_extras=True)
        except (NameError, AttributeError) as error:
            errors.append((min(names), error, origin, names))
    if errors:
        errors.sort(key=lambda failure: failure[0])
        failures = [(_owner(o, names), _unresolved(o)) for _, _, o, names in errors]
        error = errors[0][1]
        error.add_note(_unresolved_note(failures))
        raise error
    resolved: list[NodeDef] = []
    for node in nodes:
        hints = values[id(node.origin)]
        params = tuple(
            Param(p.name, p.binding, p.required, hints[p.name]) for p in node.params
        )
        resolved.append(replace(node, params=params, returns=hints["return"]))
    return resolved
