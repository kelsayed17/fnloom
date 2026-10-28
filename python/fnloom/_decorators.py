"""Decorators that attach metadata to a node function and return the function itself."""

import annotationlib
import inspect
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal

from fnloom._stores import Format

if TYPE_CHECKING:
    from fnloom._config import ConfigPolicy

_ATTRIBUTE = "__fnloom__"

# How a node uses the result cache: see `cache`.
type CacheBehavior = Literal["default", "recompute", "disable"]


@dataclass(frozen=True, slots=True)
class Source:
    """A row binding: read the parameter from the upstream node or input `name`."""

    name: str


@dataclass(frozen=True, slots=True)
class Value:
    """A row binding: pass `value` itself, the same object on every call."""

    value: object


@dataclass(frozen=True, slots=True)
class CachePolicy:
    """What `@cache` recorded: how the node uses the result cache.

    Attributes:
        behavior: `"default"`, `"recompute"` or `"disable"`.
        ignore: Parameters left out of this node's cache key.
        version: The user's own code version, or None for the automatic one.
        format: How a `DiskCache` stores the result: a `Format`, a format's name,
            or None for the store's default. Only a `default` node is stored, so
            on another behavior it is checked at build and otherwise unused.
    """

    behavior: CacheBehavior
    ignore: frozenset[str]
    version: str | None
    format: Format | str | None = None


@dataclass(frozen=True)
class Decorations:
    """What the decorators recorded on one function: rows, tags and cache policy."""

    rows: Mapping[str, Mapping[str, Source | Value]] | None
    tags: Mapping[str, str]
    cache: CachePolicy | None = None
    config: ConfigPolicy | None = None


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
    return _UNDECORATED


# What a plain function records: no rows, tags or cache policy. One object, shared.
_UNDECORATED = Decorations(None, MappingProxyType[str, str]({}))


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
    # Typed as `object`: a caller may pass anything, whatever the hint says.
    given_rows: dict[str, object] = dict(rows)
    not_mappings = [
        f"row {name!r}: expected a mapping, got {type(bindings).__name__}"
        for name, bindings in sorted(given_rows.items())
        if not isinstance(bindings, Mapping)
    ]
    if not_mappings:
        raise TypeError("; ".join(not_mappings))
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
    # Typed as `object`: a caller may pass anything, whatever the hint says.
    given_tags: dict[str, object] = dict(tags)
    not_str = [
        f"tag {key!r}: value must be str, got {type(tag_value).__name__}"
        for key, tag_value in sorted(given_tags.items())
        if not isinstance(tag_value, str)
    ]
    if not_str:
        raise TypeError("; ".join(not_str))

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


# The values of `CacheBehavior`.
_BEHAVIORS = ("default", "recompute", "disable")


def _check_format(format_: object) -> None:
    """Refuses a format that is neither a `Format` nor a non-empty name."""
    if format_ is None or isinstance(format_, Format):
        return
    if not isinstance(format_, str) or not format_:
        msg = f"cache format must be a Format or its name, got {format_!r}"
        raise TypeError(msg)


def cache[F: Callable[..., object]](
    *,
    behavior: CacheBehavior = "default",
    ignore: Iterable[str] = (),
    version: str | None = None,
    format: Format | str | None = None,  # noqa: A002  # Hamilton's name for it
) -> Callable[[F], F]:
    """Records the node's cache policy; the build reads it, nothing runs here."""
    # Typed as `object`: a caller may pass anything, whatever the hint says.
    given_behavior: object = behavior
    given_version: object = version
    given_format: object = format
    if given_behavior not in _BEHAVIORS:
        allowed = ", ".join(repr(b) for b in _BEHAVIORS)
        msg = f"cache behavior {given_behavior!r} is not one of {allowed}"
        raise ValueError(msg)
    if given_version is not None and (
        not isinstance(given_version, str) or not given_version
    ):
        msg = f"cache version must be a non-empty str, got {given_version!r}"
        raise TypeError(msg)
    if isinstance(ignore, str):
        msg = "cache ignore must be an iterable of parameter names, not one str"
        raise TypeError(msg)
    names: list[object] = list(ignore)
    not_str = sorted(type(n).__name__ for n in names if not isinstance(n, str))
    if not_str:
        listed = ", ".join(not_str)
        msg = f"cache ignore names must be str, got: {listed}"
        raise TypeError(msg)
    _check_format(given_format)
    policy = CachePolicy(behavior, frozenset(str(n) for n in names), version, format)

    def apply(fn: F) -> F:
        current = decorations(fn)
        if current.cache is not None:
            msg = "function already has a cache policy; apply cache once"
            raise ValueError(msg)
        # Names only: annotations are evaluated at build, not when decorating.
        forward = annotationlib.Format.FORWARDREF
        params = set(inspect.signature(fn, annotation_format=forward).parameters)
        unknown = sorted(policy.ignore - params)
        if unknown:
            listed = ", ".join(repr(n) for n in unknown)
            msg = f"the function does not have the parameters named by ignore: {listed}"
            raise ValueError(msg)
        setattr(fn, _ATTRIBUTE, replace(current, cache=policy))
        return fn

    return apply
