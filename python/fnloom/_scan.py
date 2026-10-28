"""Scans modules for node definitions: public functions defined in the module itself."""

import annotationlib
import functools
import inspect
import operator
import typing
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import TYPE_CHECKING, NamedTuple

from fnloom import _config, _extract
from fnloom._config import Condition, Found, redefinitions, resolve_variants
from fnloom._decorators import _ATTRIBUTE, Source, Value, decorations
from fnloom._types import read_aliases
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
    keyword per core input: `origin` itself, `origin` with each `Value` bound, or a
    call that maps those keywords onto `origin`'s parameters. `renames` maps a core
    input to the parameter keyword the core passes it under, when the two differ.
    `returns` is the return annotation: as written after `collect`, its value after
    `resolve`, as are the params' annotations. `config` holds the conditions that
    selected the function, empty for a plain one or a `@config.default`. `doc` is
    the node's own docstring, as an `extract_columns` `(name, doc)` sets it, or
    `None` for `origin`'s.
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


def _bind(
    parameters: Sequence[_Parameter], row: Mapping[str, Source | Value]
) -> tuple[Param, ...]:
    """Binds each parameter to the row's source or value, or to its own name."""
    params: list[Param] = []
    for name, has_default, annotation in parameters:
        # A `Source` is made only for a parameter the row does not bind.
        binding = row[name] if name in row else Source(name)
        required = not has_default or isinstance(binding, Value)
        params.append(Param(name, binding, required, annotation))
    return tuple(params)


def _payload(
    origin: Callable[..., object], params: tuple[Param, ...]
) -> tuple[Callable[..., object], dict[str, str]]:
    """What the core calls for a row, and the keyword renames it calls it with.

    When each source feeds one parameter, the core passes each source under its
    parameter's name, so the payload is `origin` itself, or `origin` with each
    `Value` bound by `functools.partial`: no Python call in between. When one source
    feeds several parameters, the payload is a call that maps the core's keywords
    onto them. Either way a parameter whose source the core did not pass is left
    out, so its default applies, and each `Value` is passed as the object itself,
    the same on every call.
    """
    sourced = tuple(
        (p.name, p.binding.name) for p in params if isinstance(p.binding, Source)
    )
    constants = {
        p.name: p.binding.value for p in params if isinstance(p.binding, Value)
    }
    if len({source for _, source in sourced}) == len(sourced):
        renames = {source: param for param, source in sourced if source != param}
        if constants:
            return functools.partial(origin, **constants), renames
        return origin, renames

    def call(**supplied: object) -> object:
        keywords = {
            param: supplied[source] for param, source in sourced if source in supplied
        }
        keywords.update(constants)
        return origin(**keywords)

    return call, {}


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


def _node_defs(
    module: ModuleType, name: str, fn: Callable[..., object], *, awaits: bool
) -> list[NodeDef]:
    """One node for a plain function; one per row for a parameterized one.

    With `awaits`, as `build_async` scans, an `async def` is a node too (Q32).
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
    conditions: tuple[Condition, ...] = ()
    if recorded.config is not None:
        # A variant is named by `name=`, or by its function's name cut at `__`.
        name = _config.node_name(fn, recorded.config.name)
        conditions = recorded.config.conditions
    if recorded.rows is None:
        # A plain function is one node, of its own name, binding nothing, so each
        # parameter reads its own name and the payload is the function itself.
        params = _bind(parameters, {})
        returns = annotations["return"]
        tags, module_name = recorded.tags, module.__name__
        if not conditions:
            # Positional and without `config`: the common case, kept cheapest.
            node = NodeDef(name, params, tags, fn, fn, module_name, returns)
        else:
            node = NodeDef(
                name, params, tags, fn, fn, module_name, returns, {}, conditions
            )
        if recorded.extract is None:
            return [node]
        # Lowered after the variant is selected. These field nodes are placeholders,
        # used only to find duplicate names; `resolve` lowers them again, typed
        # from the resolved return, with their checks.
        return _extract.lower(node, recorded.extract, resolved=False)
    nodes: list[NodeDef] = []
    for node_name, row in recorded.rows.items():
        params = _bind(parameters, row)
        payload, renames = _payload(fn, params)
        nodes.append(
            NodeDef(
                node_name,
                params,
                recorded.tags,
                payload,
                fn,
                module.__name__,
                annotations["return"],
                renames,
                conditions,
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
        functions.append((module, name, value))
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


def _definition(node: NodeDef) -> str:
    """How `node` was written: as a function, or as a row of a parameterized one."""
    function = getattr(node.origin, "__name__", node.name)
    if (payload := _extract.extractor(node)) is not None:
        return f"a {payload.noun} of function {function!r}"
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


# A function left out of a module, by name: the lines saying why, one per module.
type LeftOut = dict[str, list[str]]


class Gathered(NamedTuple):
    """Every module's node functions, and what the scan left out."""

    functions: list[Found]
    configured: list[ModuleType]
    left_out: LeftOut
    # The lines for `@config` variants left out, by the node they would give.
    variants: LeftOut


def _record(
    module: ModuleType, left: list[LeftFunction], left_out: LeftOut, variants: LeftOut
) -> None:
    """Adds a line for each function of `module` that was left out, by name (Q73)."""
    for function in left:
        shown = repr(function.name)
        if function.node != function.name:
            shown += f" (node {function.node!r})"
        line = f"{shown} is a function in module {module.__name__!r} {function.why}"
        for key in dict.fromkeys((function.name, function.node)):
            left_out.setdefault(key, []).append(line)
        if function.variant:
            variants.setdefault(function.node, []).append(line)


def _functions(modules: Sequence[ModuleType]) -> Gathered:
    """Every module's node functions, config-decorated modules, and what was left out.

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
    found: list[Found] = []
    configured: list[ModuleType] = []
    empty: list[str] = []
    left_out: LeftOut = {}
    variants: LeftOut = {}
    for module in unique:
        gathered = _found(module)
        if not gathered.functions:
            empty.append(_skipped(module))
        elif gathered.configured:
            configured.append(module)
        found.extend(gathered.functions)
        _record(module, gathered.left_out, left_out, variants)
    if empty:
        raise ValueError("; ".join(empty))
    # Sorted, so the lines do not depend on the order the modules were given in.
    for lines in (*left_out.values(), *variants.values()):
        lines.sort()
    return Gathered(found, configured, left_out, variants)


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
) -> list[NodeDef]:
    """Returns the nodes of `modules`; `ValueError` on a malformed or duplicate one.

    Every duplicate name is listed, sorted by name, in one `ValueError`. A module
    with no node is refused too, before duplicates, every such module in one
    `ValueError` naming each function it holds and why that is not a node (Q53). A
    function `@exclude` marks, or a module's `__all__` does not name, is not a node
    (Q73); an `__all__` that is not a list or tuple of `str` is a `TypeError`
    naming every such module.

    `config` selects among `@config.when*` variants (see `scan`), and `awaits`
    accepts `async def` nodes, as `build_async` does.

    Annotations are read without being evaluated; `resolve` evaluates them.
    """
    return scan(modules, config, awaits=awaits).nodes


def scan(
    modules: Sequence[ModuleType],
    config: Mapping[str, object] = _NO_CONFIG,
    *,
    awaits: bool = False,
) -> Scanned:
    """`collect`, with the reason each node that no variant gave is absent.

    Variants are selected first, one per node (`_config.select`), so a variant
    the config does not select is never lowered. A module none of whose
    variants is selected is not empty. Without a config-decorated function
    there is nothing to select, and selection is skipped entirely.
    """
    found, configured, left_out, variants = _functions(modules)
    unmatched: dict[str, str] = {}
    if configured:
        if redefined := sorted(line for m in configured for line in redefinitions(m)):
            raise ValueError("; ".join(redefined))
        selection = resolve_variants(found, config)
        found, unmatched = selection.functions, selection.unmatched
    seen: dict[str, NodeDef] = {}
    clashes: list[tuple[str, str]] = []
    for module, name, fn in found:
        for node in _node_defs(module, name, fn, awaits=awaits):
            if node.name in seen:
                clashes.append((node.name, _duplicate(seen[node.name], node)))
            else:
                seen[node.name] = node
    if clashes:
        # Sorted by node name; a name clashing more than once keeps scan order.
        clashes.sort(key=lambda clash: clash[0])
        raise ValueError("; ".join(message for _, message in clashes))
    # A node a plain function or another variant defines is not missing.
    missing = {n: why for n, why in unmatched.items() if n not in seen}
    for name in missing.keys() & variants.keys():
        # A variant the scan left out is a reason too: say so beside the others.
        missing[name] = "; ".join([missing[name], *variants[name]])
    return Scanned(list(seen.values()), missing, left_out)


def _owner(origin: Callable[..., object], names: list[str]) -> str:
    """Names the nodes of `origin`: its node, or every row of a parameterized one."""
    if decorations(origin).rows is None:
        return f"node {names[0]!r}"
    rows = ", ".join(repr(name) for name in sorted(names))
    function = getattr(origin, "__name__", names[0])
    return f"nodes {rows} (rows of function {function!r})"


def _culprit(key: str) -> str:
    """Names the annotation `key` of a function in an unresolved-annotation note."""
    return "the return" if key == "return" else f"parameter {key!r}"


def _unresolved(origin: Callable[..., object]) -> list[str]:
    """The annotations of `origin` that do not evaluate, in declaration order."""
    written = annotationlib.get_annotations(origin, format=annotationlib.Format.STRING)
    culprits: list[str] = []
    for key, text in written.items():
        try:
            typing.evaluate_forward_ref(annotationlib.ForwardRef(text, owner=origin))
        except NameError, AttributeError:
            culprits.append(_culprit(key))
    return culprits or ["an annotation"]


def _unread_aliases(hints: dict[str, object]) -> list[tuple[str, _Unresolved]]:
    """The annotations among `hints` naming an alias whose value does not evaluate.

    Each is (culprit, the error its value raised), in declaration order (Q44).
    """
    culprits: list[tuple[str, _Unresolved]] = []
    for key, hint in hints.items():
        try:
            read_aliases(hint)
        except (NameError, AttributeError) as error:
            culprits.append((_culprit(key), error))
    return culprits


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


def resolve(nodes: Sequence[NodeDef]) -> list[NodeDef]:
    """Returns `nodes` with their annotations evaluated to values, as Hamilton does.

    `typing.get_type_hints` evaluates them: string annotations, and forward
    references to names defined anywhere in the module, also nested ones such as
    `list["Later"]`, resolve, and a `None` annotation becomes `NoneType`. When
    names do not resolve, the `NameError` (or, for a missing attribute such as
    `decimal.Missing`, the `AttributeError`) of the first failing node by name is
    raised, as Hamilton raises it, with one note naming every failing node and
    annotation, sorted by node. A PEP 695 alias's value is evaluated here too, so
    an alias over a name that does not resolve fails the same way (Q44).
    """
    origins: dict[int, tuple[Callable[..., object], list[str]]] = {}
    nodes_of: dict[int, NodeDef] = {}
    # A field node is lowered again from its resolved producer, by `_lowered`.
    own = [node for node in nodes if not _extract.is_generated(node)]
    for node in own:
        origins.setdefault(id(node.origin), (node.origin, []))[1].append(node.name)
        nodes_of.setdefault(id(node.origin), node)
    values: dict[int, dict[str, typing.Any]] = {}
    # Each failure: (first node name, error, origin, node names, culprits).
    errors: list[
        tuple[str, _Unresolved, Callable[..., object], list[str], list[str]]
    ] = []
    for key, (origin, names) in origins.items():
        if _all_classes(nodes_of[key]) and not _resigned(origin):
            continue
        try:
            hints = typing.get_type_hints(origin, include_extras=True)
        except (NameError, AttributeError) as error:
            errors.append((min(names), error, origin, names, _unresolved(origin)))
            continue
        if unread := _unread_aliases(hints):
            culprits = [culprit for culprit, _ in unread]
            errors.append((min(names), unread[0][1], origin, names, culprits))
        else:
            values[key] = hints
    if errors:
        errors.sort(key=lambda failure: failure[0])
        failures = [(_owner(o, names), culprits) for _, _, o, names, culprits in errors]
        error = errors[0][1]
        error.add_note(_unresolved_note(failures))
        raise error
    resolved: list[NodeDef] = []
    for node in own:
        if (hints := values.get(id(node.origin))) is None:
            # Every annotation was already a class, which evaluates to itself.
            resolved.append(node)
            continue
        # A parameter whose annotation was already a value, such as `int`, is kept.
        params = tuple(
            p
            if hints[p.name] is p.annotation
            else Param(p.name, p.binding, p.required, hints[p.name])
            for p in node.params
        )
        returns = hints["return"]
        if returns is node.returns and all(
            map(operator.is_, params, node.params, strict=True)
        ):
            resolved.append(node)
        else:
            resolved.append(replace(node, params=params, returns=returns))
    return _lowered(resolved)


def _lowered(nodes: list[NodeDef]) -> list[NodeDef]:
    """`nodes`, resolved, with each producer's field nodes lowered from its return.

    `ValueError` listing, sorted, every declared field type a return cannot feed,
    and then every field name that another node already has.
    """
    if all(decorations(node.origin).extract is None for node in nodes):
        return nodes
    lowered: list[NodeDef] = []
    problems: list[str] = []
    # Each producer whose `TypedDict` field types do not resolve, with the error.
    unresolved: list[tuple[str, _Unresolved, str]] = []
    for node in nodes:
        extraction = decorations(node.origin).extract
        if extraction is None:
            lowered.append(node)
            continue
        try:
            lowered.extend(_extract.lower(node, extraction, resolved=True))
        except ValueError as error:
            problems.append(str(error))
        except (NameError, AttributeError) as error:
            unresolved.append((node.name, error, _extract.returned_type(node)))
    if unresolved:
        _raise_unresolved(unresolved)
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


def _raise_unresolved(unresolved: list[tuple[str, _Unresolved, str]]) -> None:
    """Raises the first failing producer's error, by name, as `resolve` does.

    With one note naming every producer whose `TypedDict` fields do not resolve;
    each failure is (producer name, its error, its return type as shown).
    """
    unresolved.sort(key=lambda failure: failure[0])
    listed = "; ".join(
        f"{shown}, returned by {name!r}" for name, _, shown in unresolved
    )
    error = unresolved[0][1]
    error.add_note(f"resolving the fields of {listed}")
    raise error
