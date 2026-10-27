"""Decorators that attach metadata to a node function and return the function itself."""

import annotationlib
import inspect
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType

_ATTRIBUTE = "__fnloom__"


@dataclass(frozen=True, slots=True)
class Source:
    """A row binding: read the parameter from the upstream node or input `name`."""

    name: str


@dataclass(frozen=True, slots=True)
class Value:
    """A row binding: pass `value` itself, the same object on every call."""

    value: object


@dataclass(frozen=True)
class Decorations:
    """What the decorators recorded on one function: parameterize rows and tags."""

    rows: Mapping[str, Mapping[str, Source | Value]] | None
    tags: Mapping[str, str]


def source(name: str) -> Source:
    """Binds a row's parameter to the upstream node or input `name`."""
    return Source(name)


def value(v: object) -> Value:
    """Binds a row's parameter to the constant `v`."""
    return Value(v)


def decorations(fn: Callable[..., object]) -> Decorations:
    """Returns what the decorators recorded on `fn`; empty for a plain function."""
    recorded = getattr(fn, _ATTRIBUTE, None)
    if isinstance(recorded, Decorations):
        return recorded
    return Decorations(None, {})


def _check_rows(
    rows: Mapping[str, Mapping[str, Source | Value]], params: set[str]
) -> None:
    """Raises one error listing every culprit, sorted.

    Wrong-type bindings (TypeError) take precedence over unknown parameters
    (ValueError): fix the kind of binding first, then the names.
    """
    wrong = sorted(
        (row, param, type(binding).__name__)
        for row, bindings in rows.items()
        for param, binding in bindings.items()
        if not isinstance(binding, Source | Value)
    )
    if wrong:
        listed = ", ".join(f"row {r!r} parameter {p!r} ({t})" for r, p, t in wrong)
        msg = f"expected source(...) or value(...) for: {listed}"
        raise TypeError(msg)
    unknown = sorted(
        (row, param)
        for row, bindings in rows.items()
        for param in bindings
        if param not in params
    )
    if unknown:
        listed = ", ".join(f"row {r!r} parameter {p!r}" for r, p in unknown)
        msg = f"the function does not have the parameters named by: {listed}"
        raise ValueError(msg)


def parameterize[F: Callable[..., object]](
    **rows: Mapping[str, Source | Value],
) -> Callable[[F], F]:
    """Declares one node per row, binding named parameters to a source or value."""
    if not rows:
        msg = "parameterize needs at least one row"
        raise ValueError(msg)
    for name, bindings in rows.items():
        if not isinstance(bindings, Mapping):
            msg = f"row {name!r}: expected a mapping, got {type(bindings).__name__}"
            raise TypeError(msg)
    frozen = {name: MappingProxyType(dict(b)) for name, b in rows.items()}

    def apply(fn: F) -> F:
        current = decorations(fn)
        if current.rows is not None:
            msg = "function is already parameterized; apply parameterize once"
            raise ValueError(msg)
        # Names only: annotations are evaluated at build, not when decorating.
        forward = annotationlib.Format.FORWARDREF
        params = set(inspect.signature(fn, annotation_format=forward).parameters)
        _check_rows(frozen, params)
        setattr(fn, _ATTRIBUTE, replace(current, rows=MappingProxyType(frozen)))
        return fn

    return apply


def tag[F: Callable[..., object]](**tags: str) -> Callable[[F], F]:
    """Attaches string tags to the node function."""
    for key, tag_value in tags.items():
        if not isinstance(tag_value, str):
            msg = f"tag {key!r}: value must be str, got {type(tag_value).__name__}"
            raise TypeError(msg)

    def apply(fn: F) -> F:
        current = decorations(fn)
        repeated = sorted(set(current.tags) & set(tags))
        if repeated:
            names = ", ".join(repr(k) for k in repeated)
            msg = f"tag {names} already set on this function"
            raise ValueError(msg)
        merged = MappingProxyType({**current.tags, **tags})
        setattr(fn, _ATTRIBUTE, replace(current, tags=merged))
        return fn

    return apply
