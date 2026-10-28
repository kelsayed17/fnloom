"""Decorators that attach metadata to a node function and return the function itself."""

import annotationlib
import inspect
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, overload

from fnloom._stores import Format

if TYPE_CHECKING:
    from fnloom._config import ConfigPolicy
    from fnloom._extract import Extraction

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


@dataclass(frozen=True, slots=True)
class DefaultsPolicy:
    """What `@defaults` recorded: the parameters whose default the build may not refuse.

    Attributes:
        shadowed: Parameters with a default that read a node's name, where the
            node is meant to win.
        differing: Parameters whose default is meant to differ from another
            consumer's default for the same input.
    """

    shadowed: frozenset[str]
    differing: frozenset[str]


@dataclass(frozen=True)
class Decorations:
    """What the decorators recorded on one function.

    Its rows, tags, cache policy, config conditions, extraction, defaults, and whether
    `@exclude` marked it.
    """

    rows: Mapping[str, Mapping[str, Source | Value]] | None
    tags: Mapping[str, str]
    cache: CachePolicy | None = None
    config: ConfigPolicy | None = None
    extract: Extraction | None = None
    defaults: DefaultsPolicy | None = None
    excluded: bool = False


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


def too_many_rows(rows: Mapping[str, object], decorator: str) -> str:
    """Why `decorator`, an `extract_*`, refuses these rows: it lowers one node."""
    listed = ", ".join(repr(name) for name in sorted(rows))
    return f"{decorator} needs one node, but parameterize makes {len(rows)}: {listed}"


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
        if current.extract is not None and len(frozen) > 1:
            decorator = f"extract_{current.extract.kind}"
            raise ValueError(too_many_rows(frozen, decorator))
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


def _names(given: Iterable[str], argument: str) -> frozenset[str]:
    """`given` as a set of names; `TypeError` for one bare `str` or a non-`str` item."""
    if isinstance(given, str):
        msg = f"defaults {argument} must be an iterable of parameter names, not one str"
        raise TypeError(msg)
    names: list[object] = list(given)
    not_str = sorted({type(n).__name__ for n in names if not isinstance(n, str)})
    if not_str:
        msg = f"defaults {argument} names must be str, got: {', '.join(not_str)}"
        raise TypeError(msg)
    return frozenset(str(n) for n in names)


def defaults[F: Callable[..., object]](
    *, shadowed: Iterable[str] = (), differing: Iterable[str] = ()
) -> Callable[[F], F]:
    """Records which defaults of the function the build may not refuse.

    The build refuses a parameter with a default that reads a node's name, because
    the node replaces the default (`shadowed`), and an input that two consumers
    give different defaults (`differing`). Naming a parameter here opts it out.
    Applying `defaults` again adds its names to the first call's.
    """
    policy = DefaultsPolicy(
        _names(shadowed, "shadowed"), _names(differing, "differing")
    )

    def apply(fn: F) -> F:
        current = decorations(fn)
        # Names only: annotations are evaluated at build, not when decorating.
        forward = annotationlib.Format.FORWARDREF
        parameters = inspect.signature(fn, annotation_format=forward).parameters
        optional = {
            name
            for name, parameter in parameters.items()
            if parameter.default is not inspect.Parameter.empty
        }
        unknown = [
            f"{argument}: {', '.join(repr(n) for n in sorted(names - optional))}"
            for argument, names in (
                ("shadowed", policy.shadowed),
                ("differing", policy.differing),
            )
            if names - optional
        ]
        if unknown:
            msg = (
                "defaults names a parameter that is not one with a default, "
                f"in {'; '.join(unknown)}"
            )
            raise ValueError(msg)
        merged = policy
        if current.defaults is not None:
            merged = DefaultsPolicy(
                current.defaults.shadowed | policy.shadowed,
                current.defaults.differing | policy.differing,
            )
        setattr(fn, _ATTRIBUTE, replace(current, defaults=merged))
        return fn

    return apply


def _mark_excluded[F: Callable[..., object]](fn: F) -> F:
    """Records on `fn` that it is not a node; returns `fn` itself."""
    setattr(fn, _ATTRIBUTE, replace(decorations(fn), excluded=True))
    return fn


@overload
def exclude[F: Callable[..., object]](fn: F, /) -> F: ...


@overload
def exclude[F: Callable[..., object]]() -> Callable[[F], F]: ...


def exclude[F: Callable[..., object]](fn: F | None = None, /) -> F | Callable[[F], F]:
    """Keeps a public function out of the graph: written bare or called, `@exclude()`.

    Only records the mark: the function is returned unchanged, still callable, and
    stacks with the other decorators in any order. The scan skips it, so its
    annotations are never read. Hamilton's `hamilton_exclude` works bare only (Q73).
    """
    # Typed as `object`: a caller may pass anything, whatever the hint says.
    given: object = fn
    if given is None:
        return _mark_excluded
    if not callable(given):
        msg = f"exclude takes a function or nothing, got {type(given).__name__}"
        raise TypeError(msg)
    return _mark_excluded(fn)  # ty: ignore[invalid-argument-type]  # narrowed through `given`
