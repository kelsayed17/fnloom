"""Injection: `@inject` binds parameters, `@resolve` decorates from config (Q109, Q110).

`@inject` is one `@parameterize` row that keeps the function's name. `@resolve`
calls its `decorate_with` with the build's config, and applies the decorators it
returns to a copy of the function, made for that build: the function itself is
never changed, so builds with other configs see their own decorators.
"""

import annotationlib
import inspect
import threading
import types
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from types import MappingProxyType

from fnloom._decorators import (
    _ATTRIBUTE,
    Binding,
    _check_rows,
    decorations,
    group_check,
)

# Annotations as written: a name imported only under TYPE_CHECKING is no error.
_FORWARD = annotationlib.Format.FORWARDREF

# The attribute a resolved copy names its function by, so a request, a view and
# a hook see the function as written.
ORIGINAL = "__fnloom_original__"


def inject[F: Callable[..., object]](**bindings: Binding) -> Callable[[F], F]:
    """Binds parameters to `source(...)`, `value(...)` or `group(...)` (Q109).

    The node keeps the function's name, and its docstring is templated with the
    bindings as a `@parameterize` row's is. A bare value is a `TypeError`, as
    with `@parameterize`, and so is `@inject` with `@parameterize` on one
    function, in either order.
    """
    if not bindings:
        msg = "inject needs at least one binding"
        raise TypeError(msg)
    frozen = MappingProxyType(dict(bindings))

    def apply(fn: F) -> F:
        current = decorations(fn)
        if current.rows is not None:
            msg = "inject and parameterize can't share a function: use one"
            raise TypeError(msg)
        if current.injected is not None:
            msg = "function already has @inject; give all its bindings to one"
            raise ValueError(msg)
        params = set(inspect.signature(fn, annotation_format=_FORWARD).parameters)
        _check_rows({"inject": frozen}, params)
        group_check(fn, {"inject": frozen})
        setattr(fn, _ATTRIBUTE, replace(current, injected=frozen))
        return fn

    return apply


def resolve[F: Callable[..., object]](
    decorate_with: Callable[..., object],
) -> Callable[[F], F]:
    """Decorates the function with what `decorate_with` returns for the config (Q110).

    `decorate_with`'s parameters name config keys; one with a default is an
    optional key. It returns a decorator, or a sequence of them, the first
    outermost, applied as if written on the function. A missing required key is
    a `ValueError` at build, naming the function and the key, and a value that
    is no decorator a `TypeError` there. `*args`, `**kwargs` and positional-only
    parameters are refused here. No power-user mode is needed. `@resolve`s
    stack.
    """
    if not callable(decorate_with):
        given = type(decorate_with).__name__
        msg = f"resolve takes a function of config keys, got {given}"
        raise TypeError(msg)
    params = inspect.signature(
        decorate_with, annotation_format=_FORWARD
    ).parameters.values()
    unnamed = [
        p.name
        for p in params
        if p.kind in {p.VAR_POSITIONAL, p.VAR_KEYWORD, p.POSITIONAL_ONLY}
    ]
    if unnamed:
        listed = ", ".join(unnamed)
        msg = f"resolve: decorate_with must name each config key, not {listed}"
        raise TypeError(msg)

    def apply(fn: F) -> F:
        current = decorations(fn)
        resolvers = (*current.resolvers, decorate_with)
        setattr(fn, _ATTRIBUTE, replace(current, resolvers=resolvers))
        return fn

    return apply


def _copied(fn: types.FunctionType) -> types.FunctionType:
    """A new function running `fn`'s code, with its names, defaults and attributes."""
    copy = types.FunctionType(
        fn.__code__, fn.__globals__, fn.__name__, fn.__defaults__, fn.__closure__
    )
    copy.__kwdefaults__ = fn.__kwdefaults__
    copy.__qualname__ = fn.__qualname__
    copy.__module__ = fn.__module__
    copy.__doc__ = fn.__doc__
    copy.__type_params__ = fn.__type_params__
    annotate: object = getattr(fn, "__annotate__", None)
    if annotate is not None:
        setattr(copy, "__annotate__", annotate)  # noqa: B010  # typeshed narrows it
    else:
        copy.__annotations__ = dict(fn.__annotations__)
    copy.__dict__.update(fn.__dict__)
    return copy


# What a resolver may not apply: decorators that change which functions are
# nodes, or what the config selects.
_FORBIDDEN = (
    ("config", "@config.when*"),
    ("excluded", "@exclude"),
    ("mutates", "@mutate"),
    ("resolvers", "@resolve"),
)

# One resolved copy at a time, re-entrant: a resolver may build a driver itself.
_LOCK = threading.RLock()


def resolved(
    fn: types.FunctionType, config: Mapping[str, object]
) -> types.FunctionType:
    """A copy of `fn`, decorated as its `@resolve`s say for `config`.

    `fn` itself when it has none. The copy names `fn` under `ORIGINAL`.
    """
    resolvers = decorations(fn).resolvers
    if not resolvers:
        return fn
    where = f"resolve on {fn.__qualname__}"
    copy = _copied(fn)
    with _LOCK:
        setattr(copy, _ATTRIBUTE, replace(decorations(fn), resolvers=()))
        for resolver in resolvers:
            for decorator in reversed(_decorators(where, resolver, config)):
                before = decorations(copy)
                try:
                    applied = decorator(copy)
                except Exception as error:
                    error.add_note(f"in {where}")
                    raise
                if applied is not copy:
                    msg = f"{where}: a decorator returned another object"
                    raise TypeError(msg)
                after = decorations(copy)
                refused = [
                    name
                    for field, name in _FORBIDDEN
                    if getattr(before, field) != getattr(after, field)
                ]
                if refused:
                    msg = f"{where}: it can't apply {', '.join(refused)}"
                    raise TypeError(msg)
    setattr(copy, ORIGINAL, fn)
    return copy


def _decorators(
    where: str, resolver: Callable[..., object], config: Mapping[str, object]
) -> Sequence[Callable[..., object]]:
    """What `resolver` returns for `config`, as decorators to apply, first outermost."""
    params = inspect.signature(resolver, annotation_format=_FORWARD).parameters.values()
    missing = sorted(
        p.name for p in params if p.default is p.empty and p.name not in config
    )
    if missing:
        listed = ", ".join(repr(k) for k in missing)
        msg = f"{where}: the config has no {listed}"
        raise ValueError(msg)
    try:
        made = resolver(**{p.name: config[p.name] for p in params if p.name in config})
    except Exception as error:
        error.add_note(f"in {where}")
        raise
    found: list[object] = list(made) if isinstance(made, (list, tuple)) else [made]
    if wrong := [type(d).__name__ for d in found if not callable(d)]:
        listed = ", ".join(wrong)
        msg = f"{where}: decorate_with must return decorators, got {listed}"
        raise TypeError(msg)
    decorators: list[Callable[..., object]] = [d for d in found if callable(d)]
    return decorators


def original(fn: Callable[..., object]) -> Callable[..., object]:
    """The function as written, for a resolved copy; `fn` itself otherwise."""
    found: object = getattr(fn, ORIGINAL, None)
    if isinstance(found, types.FunctionType):
        return found
    return fn
