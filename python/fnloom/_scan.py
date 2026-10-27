"""Scans modules for node definitions: public functions defined in the module itself."""

import annotationlib
import inspect
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from types import ModuleType

_UNSUPPORTED_KINDS = {
    inspect.Parameter.POSITIONAL_ONLY: "positional-only parameter",
    inspect.Parameter.VAR_POSITIONAL: "*args parameter",
    inspect.Parameter.VAR_KEYWORD: "**kwargs parameter",
}


@dataclass(frozen=True)
class ParamDef:
    """One node parameter: the upstream name it reads, and whether it is required."""

    name: str
    required: bool


@dataclass(frozen=True)
class NodeDef:
    """A node found in a module: name, parameters, tags, function and module."""

    name: str
    params: tuple[ParamDef, ...]
    tags: Mapping[str, str]
    fn: Callable[..., object]
    module: str


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


def _param(fn_name: str, param: inspect.Parameter, annotated: set[str]) -> ParamDef:
    if param.kind in _UNSUPPORTED_KINDS:
        kind = _UNSUPPORTED_KINDS[param.kind]
        msg = f"{fn_name!r}: {kind} {param.name!r} is not supported"
        raise ValueError(msg)
    if param.name not in annotated:
        msg = f"{fn_name!r}: parameter {param.name!r} has no type annotation"
        raise ValueError(msg)
    return ParamDef(param.name, param.default is inspect.Parameter.empty)


def _node_def(module: ModuleType, name: str, fn: Callable[..., object]) -> NodeDef:
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
    params = tuple(
        _param(name, p, annotated)
        for p in inspect.signature(
            fn, annotation_format=annotationlib.Format.FORWARDREF
        ).parameters.values()
    )
    return NodeDef(name, params, {}, fn, module.__name__)


def _scan_module(module: ModuleType) -> list[NodeDef]:
    return [
        _node_def(module, name, value)
        for name, value in vars(module).items()
        if _is_node(module, name, value)
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
