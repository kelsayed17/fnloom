"""Scans modules for node definitions: public functions defined in the module itself."""

import annotationlib
import inspect
from dataclasses import dataclass
from typing import TYPE_CHECKING

from fnloom._decorators import Source, Value, decorations
from fnloom._view import Param

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from types import ModuleType

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
    `origin`'s parameters. `returns` is the return annotation as written.
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
        binding = row.get(p.name, Source(p.name))
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
    if all(p.binding == Source(p.name) for p in params):
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
    rows = recorded.rows
    if rows is None:
        # A plain function is one row, of its own name, binding nothing.
        unbound: dict[str, Source | Value] = {}
        rows = {name: unbound}
    nodes: list[NodeDef] = []
    for node_name, row in rows.items():
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


def collect(modules: Sequence[ModuleType]) -> list[NodeDef]:
    """Returns the nodes of `modules`; `ValueError` on a malformed or duplicate one."""
    seen: dict[str, NodeDef] = {}
    # Modules hash by identity, so this drops repeats of one module, keeping the first.
    for module in dict.fromkeys(modules):
        for node in _scan_module(module):
            if node.name in seen:
                msg = (
                    f"node {node.name!r} is defined in both "
                    f"{seen[node.name].module!r} and {node.module!r}"
                )
                raise ValueError(msg)
            seen[node.name] = node
    return list(seen.values())
