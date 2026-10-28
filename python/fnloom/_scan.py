"""Scans for node definitions: a module's public functions, and lone functions."""

import annotationlib
import functools
import inspect
import operator
import typing
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import TYPE_CHECKING, NamedTuple

from fnloom import _config, _extract
from fnloom._config import (
    Condition,
    Found,
    Selection,
    redefinitions,
    resolve_variants,
)
from fnloom._decorators import (
    _ATTRIBUTE,
    Binding,
    Group,
    Source,
    Value,
    decorations,
    group_element,
)
from fnloom._functions import FunctionSource, NodeSpec
from fnloom._namespaces import qualified
from fnloom._types import SHALLOW, Rules, Unresolved, opaque, resolved_hints
from fnloom._view import Param

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from collections.abc import Set as AbstractSet
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
    keyword per core input: `origin` itself, `origin` with each `Value` bound, or a
    call that maps those keywords onto `origin`'s parameters. `renames` maps a core
    input to the parameter keyword the core passes it under, when the two differ.
    `returns` is the return annotation: as written after `collect`, its value after
    `resolve`, as are the params' annotations. `config` holds the conditions that
    selected the function, empty for a plain one or a `@config.default`. `doc` is
    the node's own docstring, as an `extract_columns` `(name, doc)` or a
    `@parameterize` row sets it, or `None` for `origin`'s, unless `documented`: the
    doc is then `doc`, even when that is `None`. `namespace` is the one its module
    was given under, or `None`; `name` is then `namespace.<name>` (Q74).
    """

    name: str
    params: tuple[Param, ...]
    tags: Mapping[str, str]
    payload: Callable[..., object]
    origin: Callable[..., object]
    module: str
    returns: object
    renames: Mapping[str, str] = field(default_factory=dict)
    config: tuple[Condition, ...] = ()
    doc: str | None = None
    namespace: str | None = None
    documented: bool = False


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


# Code flags of a body that does not run to a value when called.
_LAZY = inspect.CO_COROUTINE | inspect.CO_GENERATOR | inspect.CO_ASYNC_GENERATOR


def _unsupported_function_kind(fn: Callable[..., object]) -> str | None:
    """Names the kind of `fn` when calling it would not run its body to a value."""
    code = getattr(fn, "__code__", None)
    if (
        code is not None
        and not code.co_flags & _LAZY
        and not hasattr(fn, "_is_coroutine_marker")
    ):
        # An ordinary function, read off its flags: the checks below cost far more.
        return None
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


# A parameter as the scan reads it: its name, whether it has a default, and its
# annotation as written.
type _Parameter = tuple[str, bool, object]

# Code flags that mark a `*args` or `**kwargs` parameter.
_VARIADIC = inspect.CO_VARARGS | inspect.CO_VARKEYWORDS


def _plain_parameters(
    fn: Callable[..., object], annotations: Mapping[str, object]
) -> list[_Parameter] | None:
    """`fn`'s parameters read off its code object, or `None` when that would not do.

    A function whose signature is not its code's, one that is wrapped or carries a
    `__signature__`, or one with a positional-only, `*args` or `**kwargs`
    parameter, or an unannotated one, is left to `inspect.signature`, which also
    reports the first unsupported parameter. Reading the code object directly is
    much cheaper for a function with many parameters.
    """
    if _resigned(fn):
        return None
    code = getattr(fn, "__code__", None)
    if code is None or code.co_posonlyargcount or code.co_flags & _VARIADIC:
        return None
    positional = code.co_argcount
    names = code.co_varnames[: positional + code.co_kwonlyargcount]
    first_default = positional - len(getattr(fn, "__defaults__", None) or ())
    keyword_defaults = getattr(fn, "__kwdefaults__", None) or {}
    parameters: list[_Parameter] = []
    for index, name in enumerate(names):
        if name not in annotations:
            return None
        if index < positional:
            has_default = index >= first_default
        else:
            has_default = name in keyword_defaults
        parameters.append((name, has_default, annotations[name]))
    return parameters


def _signature_parameters(
    name: str, fn: Callable[..., object], annotations: Mapping[str, object]
) -> list[_Parameter]:
    """`fn`'s parameters by `inspect.signature`; `ValueError` on the first bad one."""
    annotated = set(annotations)
    signature = inspect.signature(fn, annotation_format=annotationlib.Format.FORWARDREF)
    parameters: list[_Parameter] = []
    for p in signature.parameters.values():
        _check_param(name, p, annotated)
        parameters.append(
            (p.name, p.default is not inspect.Parameter.empty, p.annotation)
        )
    return parameters


class Within(NamedTuple):
    """A namespace, and its nodes' full names by their names in it (Q74)."""

    namespace: str
    names: Mapping[str, str]


def _bind(
    parameters: Sequence[_Parameter],
    row: Mapping[str, Binding],
    within: Within | None = None,
) -> tuple[Param, ...]:
    """Binds each parameter to the row's source or value, or to its own name.

    `within` a namespace, a source the namespace defines reads its node there. A
    group becomes one `Param` per item, typed by the group's element.
    """
    params: list[Param] = []
    for name, has_default, annotation in parameters:
        # A `Source` is made only for a parameter the row does not bind.
        binding = row[name] if name in row else Source(name)
        if isinstance(binding, Group):
            element = group_element(annotation, is_list=binding.is_list)
            params.extend(
                Param(name, _within(item, within), True, element, slot)  # noqa: FBT003  # `required`, in field order
                for slot, item in binding.slots
            )
            continue
        binding = _within(binding, within)
        required = not has_default or isinstance(binding, Value)
        params.append(Param(name, binding, required, annotation))
    return tuple(params)


def _within(binding: Source | Value, within: Within | None) -> Source | Value:
    """`binding`, reading the namespace's node when `within` defines its source."""
    if (
        within is not None
        and isinstance(binding, Source)
        and binding.name in within.names
    ):
        return Source(within.names[binding.name])
    return binding


def _bind_within(
    parameters: Sequence[_Parameter], within: Within
) -> tuple[tuple[Param, ...], dict[str, str]]:
    """A plain function's parameters `within` a namespace, and the renames.

    Each reads the namespace's node of its name when the namespace defines one,
    under the parameter's keyword, and its own name otherwise: `_bind` and
    `_payload` for a function binding nothing, in one cheaper pass.
    """
    names = within.names
    params: list[Param] = []
    renames: dict[str, str] = {}
    for name, has_default, annotation in parameters:
        read = names.get(name, name)
        if read is not name:
            renames[read] = name
        params.append(Param(name, Source(read), not has_default, annotation))
    return tuple(params), renames


def _payload(
    origin: Callable[..., object], params: tuple[Param, ...]
) -> tuple[Callable[..., object], dict[str, str]]:
    """What the core calls for a row, and the keyword renames it calls it with.

    When each source feeds one parameter, the core passes each source under its
    parameter's name, so the payload is `origin` itself, or `origin` with each
    `Value` bound by `functools.partial`: no Python call in between. When one source
    feeds several parameters, or a parameter is a `group(...)`, the payload is a call
    that maps the core's keywords onto them, building each group's list or dict anew.
    Either way a parameter whose source the core did not pass is left out, so its
    default applies, and each `Value` is passed as the object itself, the same on
    every call.
    """
    plain = [p for p in params if p.slot is None]
    sourced = tuple(
        (p.name, p.binding.name) for p in plain if isinstance(p.binding, Source)
    )
    constants = {p.name: p.binding.value for p in plain if isinstance(p.binding, Value)}
    if len(plain) == len(params) and len({source for _, source in sourced}) == len(
        sourced
    ):
        renames = {source: param for param, source in sourced if source != param}
        if constants:
            return functools.partial(origin, **constants), renames
        return origin, renames
    groups = _groups(params)

    def call(**supplied: object) -> object:
        keywords = {
            param: supplied[source] for param, source in sourced if source in supplied
        }
        keywords.update(constants)
        for param, is_list, items in groups:
            built = [
                (slot, supplied[b.name] if isinstance(b, Source) else b.value)
                for slot, b in items
            ]
            keywords[param] = [v for _, v in built] if is_list else dict(built)
        return origin(**keywords)

    return call, {}


def _groups(
    params: tuple[Param, ...],
) -> list[tuple[str, bool, list[tuple[int | str, Source | Value]]]]:
    """Each group parameter's name, whether it is a list, and its items in order."""
    found: dict[str, list[tuple[int | str, Source | Value]]] = {}
    for p in params:
        if p.slot is not None:
            found.setdefault(p.name, []).append((p.slot, p.binding))
    return [
        (name, isinstance(items[0][0], int), items) for name, items in found.items()
    ]


def _check_kind(name: str, fn: Callable[..., object], *, awaits: bool) -> None:
    """`ValueError` when calling `fn` would not run its body to a value (Q32).

    With `awaits`, an `async def` is accepted: the async driver awaits it.
    """
    kind = _unsupported_function_kind(fn)
    if kind is None or (awaits and kind == "async function"):
        return
    if kind == "async function":
        msg = (
            f"{name!r}: async function is not supported by build(), which calls "
            "nodes synchronously; build it with build_async()"
        )
    elif awaits:
        msg = (
            f"{name!r}: {kind} is not supported; calling it returns a generator, "
            "not the node's value"
        )
    else:
        msg = f"{name!r}: {kind} is not supported; nodes are called synchronously"
    raise ValueError(msg)


def _node_defs(  # noqa: PLR0913  # keyword-only options, one per kind of source
    module: str,
    name: str,
    fn: Callable[..., object],
    *,
    awaits: bool,
    bind: Mapping[str, Binding] | None = None,
    within: Within | None = None,
) -> list[NodeDef]:
    """One node for a plain function; one per row for a parameterized one.

    `module` is the name of the module the nodes belong to. `bind`, from a
    `fnloom.node` spec, makes `fn` one row named `name`; `node` has refused a
    parameterized function or a variant. With `awaits`, as `build_async` scans,
    an `async def` is a node too (Q32). `within` a namespace, each node is named
    under it, and a parameter or source reads the namespace's node when the
    namespace defines its name, and the name as written otherwise (Q74): the
    core passes the namespaced node under the parameter's keyword (`renames`).
    """
    _check_kind(name, fn, awaits=awaits)
    annotations = annotationlib.get_annotations(
        fn, format=annotationlib.Format.FORWARDREF
    )
    if "return" not in annotations:
        msg = f"{name!r}: return has no type annotation"
        raise ValueError(msg)
    parameters = _plain_parameters(fn, annotations)
    if parameters is None:
        parameters = _signature_parameters(name, fn, annotations)
    recorded = decorations(fn)
    rows = recorded.rows if bind is None else {name: bind}
    conditions: tuple[Condition, ...] = ()
    if recorded.config is not None:
        # A variant is named by `name=`, or by its function's name cut at `__`.
        name = _config.node_name(fn, recorded.config.name)
        conditions = recorded.config.conditions
    if rows is None:
        # A plain function is one node, of its own name, binding nothing, so each
        # parameter reads its own name, or its namespace's node of that name, and
        # the payload is the function itself.
        returns = annotations["return"]
        tags = recorded.tags
        if within is not None:
            params, renames = _bind_within(parameters, within)
            node = NodeDef(
                within.names.get(name) or qualified(within.namespace, name),
                params,
                tags,
                fn,
                fn,
                module,
                returns,
                renames,
                conditions,
                None,
                within.namespace,
            )
        elif not conditions:
            params = _bind(parameters, {})
            # Positional and without `config`: the common case, kept cheapest.
            node = NodeDef(name, params, tags, fn, fn, module, returns)
        else:
            params = _bind(parameters, {})
            node = NodeDef(name, params, tags, fn, fn, module, returns, {}, conditions)
        if recorded.extract is None:
            return [node]
        # Lowered after the variant is selected. These field nodes are placeholders,
        # used only to find duplicate names; `resolve` lowers them again, typed
        # from the resolved return, with their checks.
        return _extract.lower(node, recorded.extract, resolved=False)
    nodes: list[NodeDef] = []
    namespace = None if within is None else within.namespace
    docs = recorded.docs if bind is None else {}
    for node_name, row in rows.items():
        params = _bind(parameters, row, within)
        payload, renames = _payload(fn, params)
        nodes.append(
            NodeDef(
                qualified(namespace, node_name),
                params,
                recorded.tags,
                payload,
                fn,
                module,
                annotations["return"],
                renames,
                conditions,
                docs.get(node_name),
                namespace,
                node_name in docs,
            )
        )
    if recorded.extract is not None:
        # At most one row: `@parameterize` and `@extract_fields` refuse more. The
        # field nodes are placeholders for duplicate detection, as above.
        return [
            lowered
            for node in nodes
            for lowered in _extract.lower(node, recorded.extract, resolved=False)
        ]
    return nodes


# Why a function that would be a node is not: the phrase the errors use.
_EXCLUDED = "marked @exclude"
_UNLISTED = "not listed in its __all__"


def _listed(module: ModuleType) -> frozenset[str] | None:
    """The names in `module.__all__`, or `None` when it defines none (Q73).

    `TypeError` when it is not a list or tuple of `str`; `_functions` has checked
    every module before this is called on one.
    """
    if "__all__" not in vars(module):
        return None
    given: object = vars(module)["__all__"]
    if isinstance(given, list | tuple):
        return frozenset(str(n) for n in given if isinstance(n, str))
    return frozenset[str]()


def _malformed_all(module: ModuleType) -> str | None:
    """What is wrong with `module.__all__`, or `None` when it is fine or absent."""
    if "__all__" not in vars(module):
        return None
    given: object = vars(module)["__all__"]
    if not isinstance(given, list | tuple):
        return (
            f"module {module.__name__!r}: __all__ must be a list or tuple of str, "
            f"got {type(given).__name__}"
        )
    not_str = sorted({type(n).__name__ for n in given if not isinstance(n, str)})
    if not_str:
        return (
            f"module {module.__name__!r}: __all__ must be a list or tuple of str, "
            f"got an item of {', '.join(not_str)}"
        )
    return None


def _leaving_out(listed: frozenset[str] | None, name: str, value: object) -> str | None:
    """Why `value`, a function a module defines publicly, is no node, or `None`.

    `listed` is the module's `__all__`, or `None` when it has none.

    Both reasons are given when both hold. A function no decorator touched has no
    record in its `__dict__`, so it costs a dict lookup, the common case.
    """
    reasons: list[str] = []
    if _ATTRIBUTE in getattr(value, "__dict__", ()) and decorations(value).excluded:  # ty: ignore[invalid-argument-type]  # `value` is a function here
        reasons.append(_EXCLUDED)
    if listed is not None and name not in listed:
        reasons.append(_UNLISTED)
    return " and ".join(reasons) or None


class LeftFunction(NamedTuple):
    """A function `@exclude` or `__all__` left out of a module."""

    # The name it is listed under in `__all__`: its attribute name.
    name: str
    # The node name it would have made: its own name, or its variant's node.
    node: str
    why: str
    # Whether it is a `@config` variant.
    variant: bool


class ModuleFunctions(NamedTuple):
    """What one module holds: its node functions, and each function left out."""

    functions: list[Found]
    configured: bool
    # Each function `@exclude` or `__all__` left out.
    left_out: list[LeftFunction]


def _found(module: ModuleType) -> ModuleFunctions:
    """The module's node functions, in definition order, with their attribute names.

    And whether any is config-decorated: a function no decorator touched has no
    record in its `__dict__`, so it costs a dict lookup, the common case. And
    each function that `@exclude` or `__all__` left out, as (the name it is
    listed under in `__all__`, the node name it would have made, why).
    """
    functions: list[Found] = []
    left_out: list[LeftFunction] = []
    configured = False
    listed = _listed(module)
    for name, value in vars(module).items():
        if not _is_node(module, name, value):
            continue
        if (why := _leaving_out(listed, name, value)) is not None:
            variant = decorations(value).config is not None
            left_out.append(LeftFunction(name, _node_name(value), why, variant))
            continue
        functions.append((module.__name__, name, value))
        if not configured and _ATTRIBUTE in value.__dict__:
            configured = decorations(value).config is not None
    return ModuleFunctions(functions, configured, left_out)


def _node_name(fn: Callable[..., object]) -> str:
    """The name `fn` would give its node: its own, cut at `__` for a config variant."""
    full = str(getattr(fn, "__name__", ""))
    recorded = decorations(fn)
    if recorded.config is None:
        return full
    try:
        return _config.node_name(fn, recorded.config.name)
    except ValueError:
        return full


def _skipped(module: ModuleType) -> str:
    """Why `module` has no node: each function it holds, and why it is not one."""
    listed = _listed(module)
    reasons: list[str] = []
    for name, value in vars(module).items():
        if not inspect.isfunction(value):
            continue
        if value.__module__ != module.__name__:
            reasons.append(f"{name!r} is defined in {value.__module__!r}")
        elif name.startswith("_"):
            reasons.append(f"{name!r} is private")
        elif (why := _leaving_out(listed, name, value)) is not None:
            reasons.append(f"{name!r} is {why}")
    if not reasons:
        return f"module {module.__name__!r} has no nodes: it defines no functions"
    return f"module {module.__name__!r} has no nodes: {', '.join(sorted(reasons))}"


def _definition(node: NodeDef, given: AbstractSet[int]) -> str:
    """How `node` was written: as a function, or as a row of a parameterized one.

    `given` holds the ids of the nodes made from functions given to
    `with_functions`, which say so.
    """
    function = getattr(node.origin, "__name__", node.name)
    suffix = " given to with_functions" if id(node) in given else ""
    if (payload := _extract.extractor(node)) is not None:
        return f"a {payload.noun} of function {function!r}{suffix}"
    if decorations(node.origin).rows is None:
        return f"function {function!r}{suffix}"
    return f"a row of function {function!r}{suffix}"


def _source(node: NodeDef, given: AbstractSet[int]) -> str:
    """Where `node` came from, for a clash under a namespace: what, which module."""
    where = f"by {_definition(node, set())} in module {node.module!r}"
    if node.namespace is not None:
        where += f" under namespace {node.namespace!r}"
    if id(node) in given:
        where += " (given to with_functions)"
    return where


def _duplicate(
    first: NodeDef, second: NodeDef, given: AbstractSet[int] = frozenset()
) -> str:
    if first.namespace is not None or second.namespace is not None:
        # A dotted row name can meet a namespaced node: name both sources in full.
        return (
            f"node {first.name!r} is defined twice: {_source(first, given)}, "
            f"and {_source(second, given)}"
        )
    if first.module != second.module:
        marks = [
            " (given to with_functions)" if id(node) in given else ""
            for node in (first, second)
        ]
        return (
            f"node {first.name!r} is defined in both "
            f"{first.module!r}{marks[0]} and {second.module!r}{marks[1]}"
        )
    return (
        f"node {first.name!r} is defined twice in {first.module!r}: "
        f"by {_definition(first, given)} and by {_definition(second, given)}"
    )


# A function left out of a module, by name: the lines saying why, one per module.
type LeftOut = dict[str, list[str]]


class Gathered(NamedTuple):
    """Every module's node functions, and what the scan left out."""

    # Each namespace's node functions (`None` for none), in the order first given.
    functions: dict[str | None, list[Found]]
    configured: list[ModuleType]
    # The namespaces holding a config-decorated function.
    selecting: set[str | None]
    left_out: LeftOut
    # The lines for `@config` variants left out, by the node they would give.
    variants: LeftOut


def _record(
    module: ModuleType,
    namespace: str | None,
    left: list[LeftFunction],
    found: tuple[LeftOut, LeftOut],
) -> None:
    """Adds a line for each function of `module` that was left out, by name (Q73).

    `found` is the lines by name, and the lines for variants by node. A line is
    kept under the plain name, which a missing input has, since an input is never
    namespaced; under a namespace it names the node it would have been (Q74).
    """
    left_out, variants = found
    for function in left:
        shown = repr(function.name)
        node = qualified(namespace, function.node)
        if node != function.name:
            shown += f" (node {node!r})"
        line = f"{shown} is a function in module {module.__name__!r} {function.why}"
        for key in dict.fromkeys((function.name, function.node)):
            left_out.setdefault(key, []).append(line)
        if function.variant:
            variants.setdefault(node, []).append(line)


def _functions(
    modules: Sequence[ModuleType], namespaces: Sequence[str | None] = ()
) -> Gathered:
    """Every module's node functions, config-decorated modules, and what was left out.

    `namespaces`, when given, holds the namespace of each of `modules`, in order.
    A function `@exclude` or `__all__` left out is kept by name, with why (Q73).
    `TypeError` naming every module whose `__all__` is malformed, sorted, and then
    `ValueError` naming every module with no node function (Q53).
    """
    # Modules hash by identity, so this drops repeats of one module, keeping the first.
    unique = list(dict.fromkeys(modules))
    if malformed := sorted(
        line for module in unique if (line := _malformed_all(module)) is not None
    ):
        raise TypeError("; ".join(malformed))
    configured: list[ModuleType] = []
    empty: list[str] = []
    scanned: dict[int, ModuleFunctions] = {}
    for module in unique:
        gathered = _found(module)
        scanned[id(module)] = gathered
        if not gathered.functions:
            empty.append(_skipped(module))
        elif gathered.configured:
            configured.append(module)
    if empty:
        raise ValueError("; ".join(empty))
    under = namespaces or (None,) * len(modules)
    found: dict[str | None, list[Found]] = {}
    selecting: set[str | None] = set()
    left_out: LeftOut = {}
    variants: LeftOut = {}
    # A module given twice under one namespace counts once there.
    for module, namespace in dict.fromkeys(zip(modules, under, strict=True)):
        gathered = scanned[id(module)]
        if namespace is None or not gathered.configured:
            found.setdefault(namespace, []).extend(gathered.functions)
        else:
            # Each its own tuple, so a selection keeps this namespace's variants
            # apart from another's: it keeps the tuples it selects, by identity.
            found.setdefault(namespace, []).extend(
                (name, attribute, fn) for name, attribute, fn in gathered.functions
            )
        if gathered.configured:
            selecting.add(namespace)
        _record(module, namespace, gathered.left_out, (left_out, variants))
    # Sorted, so the lines do not depend on the order the modules were given in.
    for lines in (*left_out.values(), *variants.values()):
        lines.sort()
    return Gathered(found, configured, selecting, left_out, variants)


# No config: what a build without `with_config` selects with. Read, never written.
_NO_CONFIG: Mapping[str, object] = MappingProxyType({})


class Scanned(NamedTuple):
    """What a scan found: the nodes, and why each node no variant gave is absent.

    `left_out` maps each function `@exclude` or `__all__` left out to a line saying
    so, per module, for the missing-input hint (Q73).
    """

    nodes: list[NodeDef]
    unmatched: dict[str, str]
    left_out: LeftOut


def collect(
    modules: Sequence[ModuleType],
    config: Mapping[str, object] = _NO_CONFIG,
    *,
    awaits: bool = False,
    functions: Sequence[FunctionSource] = (),
    namespaces: Sequence[str | None] = (),
) -> list[NodeDef]:
    """Returns the nodes of `modules`; `ValueError` on a malformed or duplicate one.

    Every duplicate name is listed, sorted by name, in one `ValueError`. A module
    with no node is refused too, before duplicates, every such module in one
    `ValueError` naming each function it holds and why that is not a node (Q53). A
    function `@exclude` marks, or a module's `__all__` does not name, is not a node
    (Q73); an `__all__` that is not a list or tuple of `str` is a `TypeError`
    naming every such module.

    `config` selects among `@config.when*` variants (see `scan`), and `awaits`
    accepts `async def` nodes, as `build_async` does. `functions` are the node
    sources given to `with_functions`, after the modules' (Q72). `namespaces`
    holds each module's namespace, in order (see `scan`).

    Annotations are read without being evaluated; `resolve` evaluates them.
    """
    return scan(
        modules, config, awaits=awaits, functions=functions, namespaces=namespaces
    ).nodes


def _given(
    functions: Sequence[FunctionSource], found: Sequence[Found]
) -> list[Found | NodeSpec]:
    """The node sources given to `with_functions`, each once, in the order given.

    A function is found under its own name, in its own module; a spec is kept
    as it is. Functions and specs hash by identity, so a repeat is dropped, and
    so is a function a module given to the same builder already has: one
    function object counts once, wherever it was found. `found` are the
    functions of the modules given without a namespace, since the dedup holds
    within one namespace only (Q74).
    """
    if not functions:
        return []
    known = {id(fn) for _, _, fn in found}
    return [
        item
        if isinstance(item, NodeSpec)
        else (str(item.__module__), str(getattr(item, "__name__", "")), item)
        for item in dict.fromkeys(functions)
        if isinstance(item, NodeSpec) or id(item) not in known
    ]


# A node source with the namespace it was given under.
type _Entry = tuple[str | None, Found | NodeSpec]


def scan(
    modules: Sequence[ModuleType],
    config: Mapping[str, object] = _NO_CONFIG,
    *,
    awaits: bool = False,
    functions: Sequence[FunctionSource] = (),
    namespaces: Sequence[str | None] = (),
) -> Scanned:
    """`collect`, with the reason each node that no variant gave is absent.

    Variants are selected first, one per node (`_config.select`), so a variant
    the config does not select is never lowered. A module none of whose
    variants is selected is not empty. Without a config-decorated function
    there is nothing to select, and selection is skipped entirely. A function
    given to `with_functions` takes part in selection as a module's does; a
    `fnloom.node` spec is never a variant.

    `namespaces`, when given, holds the namespace of each of `modules` (Q74).
    Each node of a namespaced module is named `namespace.<name>`, and reads the
    namespace's nodes first, by the names every module given under it defines,
    found before any node is made. A function found under several namespaces
    makes nodes in each, and the identity dedup of functions holds within one
    namespace only.
    """
    gathered = _functions(modules, namespaces)
    groups = gathered.functions
    given = _given(functions, groups.get(None, ()))
    if given:
        groups.setdefault(None, []).extend(given)  # ty: ignore[invalid-argument-type]  # specs join the plain group
    entries: list[_Entry] = [
        (namespace, entry) for namespace, group in groups.items() for entry in group
    ]
    selecting = gathered.selecting
    if any(
        not isinstance(e, NodeSpec) and decorations(e[2]).config is not None
        for e in given
    ):
        selecting = {*selecting, None}
    # Each namespace's names, from every function given under it, unselected
    # variants too: reading a name no variant gave is the unmatched error.
    names = {
        namespace: _names_in(namespace, group)
        for namespace, group in groups.items()
        if namespace is not None
    }
    unmatched: dict[str, str] = {}
    if selecting:
        entries, selection = _selected(
            entries, gathered.configured, config, namespaced=bool(names)
        )
        unmatched = selection.unmatched
    seen = _made(entries, given, names, awaits=awaits)
    # A node a plain function or another variant defines is not missing.
    missing = {n: why for n, why in unmatched.items() if n not in seen}
    for name in missing.keys() & gathered.variants.keys():
        # A variant the scan left out is a reason too: say so beside the others.
        missing[name] = "; ".join([missing[name], *gathered.variants[name]])
    return Scanned(list(seen.values()), missing, gathered.left_out)


def _selected(
    entries: list[_Entry],
    configured: Sequence[ModuleType],
    config: Mapping[str, object],
    *,
    namespaced: bool,
) -> tuple[list[_Entry], Selection]:
    """`entries` with only the variants `config` selects, and the selection.

    `ValueError` first for a config-decorated function a module defines twice.
    With `namespaced`, each namespace selects its own variants (Q74).
    """
    if redefined := sorted(line for m in configured for line in redefinitions(m)):
        raise ValueError("; ".join(redefined))
    functions = [(n, e) for n, e in entries if not isinstance(e, NodeSpec)]
    selection = resolve_variants(
        [entry for _, entry in functions],
        config,
        [namespace for namespace, _ in functions] if namespaced else (),
    )
    chosen = {id(e) for e in selection.functions}
    kept = [(n, e) for n, e in entries if isinstance(e, NodeSpec) or id(e) in chosen]
    return kept, selection


def _names_in(namespace: str, group: Sequence[Found | NodeSpec]) -> Within:
    """The names the functions of `group` give their nodes in `namespace`.

    A plain function's attribute name, a variant's node name, every row, and
    every field an `@extract_*` names or a `TypedDict` return gives, each with
    its full name. One string per name, shared by every node that reads it.
    """
    names: set[str] = set()
    for entry in group:
        if isinstance(entry, NodeSpec):
            names.add(entry.name)
            continue
        _, attribute, fn = entry
        if _ATTRIBUTE not in getattr(fn, "__dict__", ()):
            names.add(attribute)
            continue
        recorded = decorations(fn)
        if recorded.rows is not None:
            names.update(recorded.rows)
        elif recorded.config is not None:
            names.add(_node_name(fn))
        else:
            names.add(attribute)
        if recorded.extract is not None:
            names.update(_extract.field_names(fn, recorded.extract))
    return Within(namespace, {name: f"{namespace}.{name}" for name in names})


def _made(
    entries: list[_Entry],
    given: Sequence[Found | NodeSpec],
    names: Mapping[str, Within],
    *,
    awaits: bool,
) -> dict[str, NodeDef]:
    """The nodes `entries` make, by name; `ValueError` naming every clash, sorted.

    `given` are the sources given to `with_functions`, which a clash line names;
    `names` holds each namespace's names, which its nodes read first.
    """
    # The ids of the given sources, and of the nodes they make, for the clash lines.
    sourced = {id(e) for e in given}
    from_given: set[int] = set()
    seen: dict[str, NodeDef] = {}
    clashes: list[tuple[str, str]] = []
    for namespace, entry in entries:
        if isinstance(entry, NodeSpec):
            fn = entry.fn
            made = _node_defs(
                str(fn.__module__),
                entry.name,
                fn,
                awaits=awaits,
                bind=entry.bind or None,
            )
        else:
            within = None if namespace is None else names[namespace]
            made = _node_defs(*entry, awaits=awaits, within=within)
        if sourced and id(entry) in sourced:
            from_given.update(id(node) for node in made)
        for node in made:
            if node.name in seen:
                clash = _duplicate(seen[node.name], node, from_given)
                clashes.append((node.name, clash))
            else:
                seen[node.name] = node
    if clashes:
        # Sorted by node name; a name clashing more than once keeps scan order.
        clashes.sort(key=lambda clash: clash[0])
        raise ValueError("; ".join(message for _, message in clashes))
    return seen


def _all_classes(node: NodeDef) -> bool:
    """Whether every annotation of `node`'s function, as written, is already a class.

    `typing.get_type_hints` returns a class as it is, so such a function needs no
    evaluating: the common case, and the costly call skipped. A generic such as
    `list[int]` is not a class, nor is `None`, which evaluates to `NoneType`.
    """
    return isinstance(node.returns, type) and all(
        isinstance(p.annotation, type) for p in node.params
    )


def _resigned(fn: Callable[..., object]) -> bool:
    """Whether `fn`'s parameters were read from another signature than its own.

    For a wrapped function or one with a `__signature__`, the annotations read by
    the scan need not be the ones `typing.get_type_hints` evaluates, so it is called.
    """
    return hasattr(fn, "__wrapped__") or hasattr(fn, "__signature__")


def resolve(nodes: Sequence[NodeDef], rules: Rules = SHALLOW) -> list[NodeDef]:
    """Returns `nodes` with their annotations evaluated to values, as Hamilton does.

    `typing.get_type_hints` evaluates them: string annotations, and forward
    references to names defined anywhere in the module, also nested ones such as
    `list["Later"]`, resolve, and a `None` annotation becomes `NoneType`. An
    annotation naming something that does not exist at run time, such as a name
    imported only under `TYPE_CHECKING` or an alias over one, becomes
    `Unresolved`, with its text as written, where Hamilton raises `NameError`
    (Q81). A PEP 695 alias's value, and a `TypeVar`'s bound and constraints, are
    read here too, so a run never evaluates one (Q44). `rules` are the value
    checks' rules, which an extracted field's check follows.
    """
    origins: dict[int, Callable[..., object]] = {}
    nodes_of: dict[int, NodeDef] = {}
    # A field node is lowered again from its resolved producer, by `_lowered`.
    own = [node for node in nodes if not _extract.is_generated(node)]
    for node in own:
        origins.setdefault(id(node.origin), node.origin)
        nodes_of.setdefault(id(node.origin), node)
    values: dict[int, dict[str, typing.Any]] = {}
    for key, origin in origins.items():
        if _all_classes(nodes_of[key]) and not _resigned(origin):
            continue
        values[key] = resolved_hints(origin)
    resolved: list[NodeDef] = []
    for node in own:
        if (hints := values.get(id(node.origin))) is None:
            # Every annotation was already a class, which evaluates to itself.
            resolved.append(node)
            continue
        # A parameter whose annotation was already a value, such as `int`, is kept.
        params = tuple(
            p if hints[p.name] is p.annotation else _resolved_param(p, hints[p.name])
            for p in node.params
        )
        returns = hints["return"]
        if returns is node.returns and all(
            map(operator.is_, params, node.params, strict=True)
        ):
            resolved.append(node)
        else:
            resolved.append(replace(node, params=params, returns=returns))
    return _lowered(resolved, rules)


def _resolved_param(p: Param, hint: object) -> Param:
    """`p` with its annotation resolved to `hint`: a group item's is the element."""
    if p.slot is not None:
        # A group whose element did not resolve is `Unresolved` as a whole; its
        # element is read off the annotation as written (Q81).
        hint = (
            opaque(p.annotation)
            if isinstance(hint, Unresolved)
            else group_element(hint, is_list=isinstance(p.slot, int))
        )
    return Param(p.name, p.binding, p.required, hint, p.slot)


def _lowered(nodes: list[NodeDef], rules: Rules) -> list[NodeDef]:
    """`nodes`, resolved, with each producer's field nodes lowered from its return.

    `ValueError` listing, sorted, every declared field type a return cannot feed,
    and then every field name that another node already has.
    """
    if all(decorations(node.origin).extract is None for node in nodes):
        return nodes
    lowered: list[NodeDef] = []
    problems: list[str] = []
    for node in nodes:
        extraction = decorations(node.origin).extract
        if extraction is None:
            lowered.append(node)
            continue
        try:
            lowered.extend(_extract.lower(node, extraction, resolved=True, rules=rules))
        except ValueError as error:
            problems.append(str(error))
    if problems:
        raise ValueError("; ".join(sorted(problems)))
    # A field a forward-referenced `TypedDict` gives is first known here.
    _refuse_duplicates(lowered)
    return lowered


def _refuse_duplicates(nodes: list[NodeDef]) -> None:
    """`ValueError` naming every node name defined twice in `nodes`, sorted."""
    seen: dict[str, NodeDef] = {}
    clashes: list[str] = []
    for node in nodes:
        if node.name in seen:
            clashes.append(_duplicate(seen[node.name], node))
        else:
            seen[node.name] = node
    if clashes:
        raise ValueError("; ".join(sorted(clashes)))
