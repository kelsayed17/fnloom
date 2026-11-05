"""Cached runs: each node's static key part, made at build, and runs that skip hits.

A node's key hashes its static part (identity, code version and behavior) and the
version of each value it reads: an input's or override's fingerprint, a computed
node's key, or a `recompute` node's result's fingerprint. `CacheRun` first runs the
plan's `recompute` nodes, in a nested run, then computes every key, walks back from
the outputs to the first nodes
the store holds, and runs the plan with those as overrides, so the core prunes their
upstream. Each `default` node that runs is stored under its key as it succeeds;
nothing else is ever looked up, so nothing else is stored. A field or column
node of `@extract_fields` or `@extract_columns` is keyed, so what reads it chains
from its key, but is never looked up or stored: it runs whenever it is needed,
and its check with it, and is never served from the store. `explain`
reports what a run would do with each computed node, running nothing.

The cache never fails a run: a store that raises is a miss or a skipped write,
reported with one `CacheWarning` per node per run. A node's own exception fails the
run as without the cache, and nothing is stored for that node.
"""

import functools
import os
import threading
import typing
import warnings
from collections import abc
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, NamedTuple, Protocol

from fnloom import _core, _extract
from fnloom._canonical import stable_fingerprint
from fnloom._codeversion import UncacheableError, class_version, code_version
from fnloom._decorators import (
    _BEHAVIORS,
    CacheBehavior,
    CachePolicy,
    FieldCache,
    Source,
    decorations,
)
from fnloom._fingerprint import fingerprint, forget_class_versions
from fnloom._requests import suggest
from fnloom._scan import core_inputs
from fnloom._stores import MISSING, CacheStore, CacheWarning, Format, MemoryCache
from fnloom._types import written

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Mapping, Sequence

    from fnloom._checks import Check
    from fnloom._hooks import Dispatch
    from fnloom._scan import NodeDef

# fnloom's own files, which a build warning points past.
_PACKAGE = f"{Path(__file__).parent}{os.sep}"

# The key schema version, the first part of every static key part.
_SCHEMA = b"fnloom-cache-2"

# How many core plans a prepared plan keeps, one per hit set; the least recently
# used one goes first.
HIT_PLAN_CACHE_SIZE = 64

# The policy of a node without `@cache`: the driver's default behavior applies.
_DEFAULT_POLICY = CachePolicy(None, frozenset(), None)

# Where a node's behavior came from: a `with_cache` list naming it, its `@cache`
# (`fields=` for a field), or the driver's `default_behavior` (Q143).
type PolicySource = Literal["driver", "decorator", "default"]

# A key parameter: its name, the node or input it reads (`None` for a constant), and
# a constant's version (`None` for a read, or a constant with no fingerprint).
type KeyParam = tuple[str, str | None, bytes | None]

# The `with_cache` lists, in the order a refusal names them.
_LISTS: tuple[CacheBehavior, ...] = ("default", "recompute", "disable")


@dataclass(frozen=True, slots=True)
class CacheSettings:
    """What `Builder.with_cache` was given beside its store (Q143).

    Attributes:
        named: The behavior of each node a list names, sorted by name.
        default_behavior: The behavior of a node no list names and whose `@cache`
            gives none; a list given as `True` sets it.
        ignore: Each consumer's parameters left out of its key, by consumer,
            sorted; added to its `@cache(ignore=...)`.
    """

    named: tuple[tuple[str, CacheBehavior], ...] = ()
    default_behavior: CacheBehavior = "default"
    ignore: tuple[tuple[str, frozenset[str]], ...] = ()

    @classmethod
    def checked(
        cls,
        lists: Mapping[CacheBehavior, object],
        default_behavior: object,
        ignore: object,
    ) -> CacheSettings:
        """The settings from `with_cache`'s arguments; refuses malformed ones.

        `lists` maps `"default"`, `"recompute"` and `"disable"` to what each was
        given: `True`, for every node no other list names, or an iterable of node
        names. `TypeError` for a bare `str`, `False`, a non-`str` name or a
        malformed `ignore`; `ValueError` for a name in two lists, two lists given
        `True`, a `True` list against another `default_behavior`, or an unknown
        behavior.
        """
        if default_behavior not in _BEHAVIORS:
            allowed = ", ".join(repr(b) for b in _BEHAVIORS)
            msg = (
                f"with_cache default_behavior {default_behavior!r} is not one of "
                f"{allowed}"
            )
            raise ValueError(msg)
        behavior = typing.cast("CacheBehavior", default_behavior)
        everything = [name for name in _LISTS if lists[name] is True]
        if len(everything) > 1:
            listed = " and ".join(everything)
            msg = f"with_cache: only one list may be True, but {listed} are"
            raise ValueError(msg)
        if everything:
            (whole,) = everything
            if behavior not in {"default", whole}:
                msg = (
                    f"with_cache: {whole}=True sets the default behavior, but "
                    f"default_behavior is {behavior!r}"
                )
                raise ValueError(msg)
            behavior = whole
        named: dict[str, list[CacheBehavior]] = {}
        for list_name in _LISTS:
            given = lists[list_name]
            if given is True:
                continue
            for name in dict.fromkeys(_names_of(given, f"with_cache {list_name}")):
                named.setdefault(name, []).append(list_name)
        if twice := sorted(n for n, where in named.items() if len(where) > 1):
            listed = "; ".join(
                f"{name!r} is in {' and '.join(dict.fromkeys(named[name]))}"
                for name in twice
            )
            msg = f"with_cache: a node may be in one list only: {listed}"
            raise ValueError(msg)
        return cls(
            tuple(sorted((n, where[0]) for n, where in named.items())),
            behavior,
            _checked_ignore(ignore),
        )

    def problems(self, nodes: Sequence[NodeDef]) -> list[str]:
        """Why each name these settings give is wrong for the built `nodes`.

        A listed name that is no node, an `ignore` consumer that is no node or is
        an extracted field, and a parameter the consumer does not have; each with
        a `did you mean` when a name is close (Q71).
        """
        by_name = {node.name: node for node in nodes}
        problems: list[str] = [
            f"with_cache {behavior} names {name!r}, which is no node"
            f"{suggest(name, by_name)}"
            for name, behavior in self.named
            if name not in by_name
        ]
        for consumer, params in self.ignore:
            node = by_name.get(consumer)
            if node is None:
                problems.append(
                    f"with_cache ignore names {consumer!r}, which is no node"
                    f"{suggest(consumer, by_name)}"
                )
                continue
            if _extract.is_generated(node):
                problems.append(
                    f"with_cache ignore names {consumer!r}, an extracted field, "
                    "which reads only its producer"
                )
                continue
            known = [param.name for param in node.params]
            problems.extend(
                f"with_cache ignore[{consumer!r}] names {param!r}, which is no "
                f"parameter of that node{suggest(param, known)}"
                for param in sorted(params)
                if param not in known
            )
        return problems


def _names_of(given: object, argument: str) -> list[str]:
    """`given` as a list of names; `TypeError` for `False`, one `str` or a non-`str`."""
    if isinstance(given, str) or not isinstance(given, abc.Iterable):
        msg = f"{argument} must be True or an iterable of node names, not {given!r}"
        raise TypeError(msg)
    names: list[object] = list(typing.cast("abc.Iterable[object]", given))
    if not_str := sorted({type(n).__name__ for n in names if not isinstance(n, str)}):
        msg = f"{argument} names must be str, got: {', '.join(not_str)}"
        raise TypeError(msg)
    return [str(n) for n in names]


def _checked_ignore(ignore: object) -> tuple[tuple[str, frozenset[str]], ...]:
    """`with_cache`'s `ignore`, each consumer's parameters, sorted by consumer."""
    if ignore is None:
        return ()
    if not isinstance(ignore, abc.Mapping):
        msg = (
            "with_cache ignore must map each consumer to the parameters it "
            f"ignores, not {ignore!r}"
        )
        raise TypeError(msg)
    checked: list[tuple[str, frozenset[str]]] = []
    for consumer, params in typing.cast("Mapping[object, object]", ignore).items():
        if not isinstance(consumer, str):
            msg = f"with_cache ignore consumers must be str, got {consumer!r}"
            raise TypeError(msg)
        names = _names_of(params, f"with_cache ignore[{consumer!r}]")
        if names:
            checked.append((consumer, frozenset(names)))
    return tuple(sorted(checked, key=lambda pair: pair[0]))


# The settings of `with_cache(store)` alone.
NO_SETTINGS = CacheSettings()


@dataclass(frozen=True, slots=True)
class _Resolved:
    """A node's cache policy once the driver's lists and default are applied.

    Attributes:
        policy: Its policy, the behavior settled and the driver's `ignore` added.
        source: Where the behavior came from.
        field: For an extracted field that a list or `fields=` gives a policy of
            its own, that policy; `None` for a field that follows its producer,
            and for every other node.
    """

    policy: CachePolicy
    source: PolicySource
    field: FieldCache | None = None

    @property
    def behavior(self) -> CacheBehavior:
        """The settled behavior."""
        behavior = self.policy.behavior
        assert behavior is not None  # noqa: S101  # settled by `_resolved`
        return behavior


def _resolved(
    nodes: Sequence[NodeDef], settings: CacheSettings
) -> dict[str, _Resolved]:
    """Each node's settled policy, by name.

    Most specific first: a driver list naming the node, then its `@cache`
    behavior (for a field, its `fields=` entry), then `default_behavior`. A list
    given `True` counts as `default_behavior`, not as a name. A field with no
    policy of its own follows its producer: `disable` with it, else keyed and
    never stored.
    """
    named = dict(settings.named)
    ignored = dict(settings.ignore)
    resolved: dict[str, _Resolved] = {}
    fields: list[NodeDef] = []
    for node in nodes:
        if _extract.is_generated(node):
            fields.append(node)
            continue
        policy = decorations(node.origin).cache or _DEFAULT_POLICY
        source: PolicySource
        if (behavior := named.get(node.name)) is not None:
            source = "driver"
        elif policy.behavior is not None:
            behavior, source = policy.behavior, "decorator"
        else:
            behavior, source = settings.default_behavior, "default"
        settled = replace(
            policy,
            behavior=behavior,
            ignore=policy.ignore | ignored.get(node.name, frozenset()),
        )
        resolved[node.name] = _Resolved(settled, source)
    for node in fields:
        resolved[node.name] = _field_resolved(node, resolved, named)
    return resolved


def _field_resolved(
    node: NodeDef, resolved: Mapping[str, _Resolved], named: Mapping[str, CacheBehavior]
) -> _Resolved:
    """An extracted field's settled policy; see `_resolved`."""
    extracting = _extract.extractor(node)
    producer = None if extracting is None else resolved.get(extracting.producer)
    if extracting is None or producer is None:
        # Never so in a build: a field without its producer follows a plain one.
        return _Resolved(CachePolicy("default", frozenset(), None), "default")
    own = dict(producer.policy.fields).get(extracting.field)
    version = producer.policy.version
    if (behavior := named.get(node.name)) is not None:
        field = FieldCache(behavior, None if own is None else own.format)
        policy = CachePolicy(behavior, frozenset(), version, field.format)
        return _Resolved(policy, "driver", field)
    if own is not None:
        policy = CachePolicy(own.behavior, frozenset(), version, own.format)
        return _Resolved(policy, "decorator", own)
    follows: CacheBehavior = "disable" if producer.behavior == "disable" else "default"
    return _Resolved(CachePolicy(follows, frozenset(), version), producer.source)


def refuse_bad_fields(nodes: Sequence[NodeDef]) -> None:
    """Refuses each `@cache(fields=...)` that does not fit its function (Q147).

    At build, cache or not: a `TypeError` for a function with `fields=` that
    extracts nothing, else a `ValueError` for a field it does not extract, with
    a `did you mean`; each lists every such problem. A `@pipe_output` step or
    relay node lowered from an extracting function shares its origin, so only
    the producer is checked, and a function counts as extracting by its origin.
    """
    extracted: dict[str, list[str]] = {}
    extracting_origins: set[int] = set()
    for node in nodes:
        if (extracting := _extract.extractor(node)) is not None:
            extracted.setdefault(extracting.producer, []).append(extracting.field)
            extracting_origins.add(id(node.origin))
    types: list[str] = []
    values: list[str] = []
    for node in nodes:
        if _extract.is_generated(node):
            continue
        policy = decorations(node.origin).cache
        if policy is None or not policy.fields:
            continue
        known = extracted.get(node.name)
        if known is None and id(node.origin) in extracting_origins:
            # A node lowered from the producer's function, not the producer.
            continue
        if known is None:
            types.append(
                f"node {node.name!r}: @cache(fields=...) needs @extract_fields or "
                "@extract_columns, but the function extracts nothing"
            )
            continue
        values.extend(
            f"node {node.name!r}: @cache(fields=...) names {name!r}, which the "
            f"function does not extract{suggest(name, known)}"
            for name, _ in policy.fields
            if name not in known
        )
    if types:
        raise TypeError("; ".join(types))
    if values:
        raise ValueError("; ".join(values))


class CacheOutcome(NamedTuple):
    """What a cached run would do with one computed node, from `explain_cache`.

    Attributes:
        status: `"hit"`: the store holds the node's key. `"miss"`: it does not, so
            the node runs and is stored; a `recompute` node is always one, with a
            reason such as `"policy recompute (decorator)"`, and is never stored.
            `"uncacheable"`: the node has no key, so it always runs and is never
            stored. An `@extract_*` field or column node with no policy of its
            own is always a miss, with the reason
            `"extracted field: runs whenever needed; never served from the store"`:
            it is keyed but never stored, so its check runs whenever it runs.
            `"unknown"`: the key depends on the result of a `recompute` node,
            which only a run knows.
        reason: Why a node is uncacheable or unknown, or is a `recompute` miss;
            `None` for a plain hit or miss. A reason from a policy names where
            the behavior came from (Q143): `(driver)`, a `with_cache` list naming
            the node; `(decorator)`, its `@cache` or its producer's `fields=`;
            `(default)`, `with_cache`'s `default_behavior`. A `default` node's
            hit or miss has the reason `"policy default (driver)"` or
            `"policy default (decorator)"` when a list or `@cache` gave it that
            behavior, and `None` when the default did.
    """

    status: Literal["hit", "miss", "uncacheable", "unknown"]
    reason: str | None


# A run's keys on the binding's walk, which keeps them itself.
_EMPTY_KEYS: dict[str, bytes | None] = {}
_NO_KEYS: Mapping[str, bytes | None] = MappingProxyType(_EMPTY_KEYS)

# The outcome of an extracted field with a key: run when needed, never stored.
_EXTRACTED = CacheOutcome(
    "miss", "extracted field: runs whenever needed; never served from the store"
)


@dataclass(frozen=True, slots=True)
class NodeKey:
    """What a node's key is made of, apart from the versions of what it reads.

    Attributes:
        static: The hash of the key schema, the node's name, module, qualified name,
            code version and behavior, and each key parameter's name and, for a
            constant, its version; `None` when the node cannot be cached.
        reason: Why `static` is `None`, else `None`.
        behavior: The node's cache behavior.
        params: The key parameters, in declaration order, the ignored ones left out.
        stored: Whether a `default` node is looked up and stored; False for an
            extracted field, which is keyed but never served from the store,
            unless a list or `fields=` gives it a policy of its own.
        source: Where the behavior came from, which a policy's reason names.
        fresh: For a `recompute` field, its producer: a plan computing the
            field never looks the producer up.
        via: For a field keyed through its `disable` producer's sources, that
            producer; `alternate` keys it in a plan that does not compute it.
        alternate: The field's key read through the producer itself.
    """

    static: bytes | None
    reason: str | None
    behavior: CacheBehavior
    params: tuple[KeyParam, ...]
    stored: bool = True
    source: PolicySource = "default"
    fresh: str | None = None
    via: str | None = None
    alternate: NodeKey | None = None

    def outcome(self, *, found: bool) -> CacheOutcome:
        """A keyed node's outcome: a hit when `found`, else a miss, with its source.

        A `default` node's reason names where its behavior came from, unless that
        is the driver's `default_behavior` (`None`, a plain hit or miss).
        """
        if self.behavior != "default":
            return CacheOutcome("miss", f"policy {self.behavior} ({self.source})")
        if not self.stored:
            return _EXTRACTED
        reason = None if self.source == "default" else f"policy default ({self.source})"
        return CacheOutcome("hit" if found else "miss", reason)


def node_keys(
    nodes: Sequence[NodeDef], settings: CacheSettings = NO_SETTINGS
) -> dict[str, NodeKey]:
    """The `NodeKey` of every node, by name; made once, at build.

    Each node's behavior is settled first (`_resolved`). A function's code
    version is computed once, however many rows it has.
    """
    return _keyed(nodes, _resolved(nodes, settings))


def _keyed(
    nodes: Sequence[NodeDef], resolved: Mapping[str, _Resolved]
) -> dict[str, NodeKey]:
    """`node_keys`, given each node's settled policy."""
    by_name = {node.name: node for node in nodes}
    # By the origin's id; `nodes` keeps every origin alive meanwhile.
    versions: dict[int, bytes | UncacheableError] = {}
    keys: dict[str, NodeKey] = {}
    for node in nodes:
        settled = resolved[node.name]
        if settled.field is not None:
            keys[node.name] = _field_key(node, settled, resolved, by_name, versions)
            continue
        # A `disable` node is never keyed, so its code is never read.
        code = (
            b""
            if settled.behavior == "disable"
            else _code(node, settled.policy, versions)
        )
        keys[node.name] = _node_key(node, settled, code)
    return keys


def _code(
    node: NodeDef, policy: CachePolicy, versions: dict[int, bytes | UncacheableError]
) -> bytes | UncacheableError:
    """`node`'s code version, its pin, or why it has none; once per function."""
    if policy.version is not None:
        return policy.version.encode()
    if (code := versions.get(id(node.origin))) is not None:
        return code
    try:
        code = code_version(node.origin)
    except UncacheableError as error:
        code = error
    except Exception as error:  # noqa: BLE001  # a failing version: uncacheable
        # Such as a default's fingerprint function raising: the build goes on,
        # as it does without a cache, and the node always runs.
        code = UncacheableError(
            f"cannot read the code version of {_qualname(node)!r}: {error!r}"
        )
        _warn_build(code.reason)
    versions[id(node.origin)] = code
    return code


def _field_key(
    node: NodeDef,
    settled: _Resolved,
    resolved: Mapping[str, _Resolved],
    by_name: Mapping[str, NodeDef],
    versions: dict[int, bytes | UncacheableError],
) -> NodeKey:
    """The key of a field with a policy of its own (Q147).

    `disable`: none. `recompute`: keyed like any field, never stored, and a plan
    that computes it never looks its producer up (`fresh`), so the producer runs
    again. `default`: keyed by the producer's key and the field, and stored. When
    the producer is `disable`, it has no key, so the field is keyed by what the
    producer's key would be, from its code and what it reads: a field can then be
    stored apart from a producer that never is. In a plan where that producer is
    an override, the field is keyed by the override instead (`alternate`).
    """
    extracting = _extract.extractor(node)
    producer = None if extracting is None else by_name.get(extracting.producer)
    code = (
        b"" if settled.behavior == "disable" else _code(node, settled.policy, versions)
    )
    own = _node_key(node, settled, code, stored=settled.behavior == "default")
    if producer is None or own.static is None:
        return own
    if settled.behavior == "recompute":
        return replace(own, fresh=producer.name)
    made = resolved[producer.name]
    if settled.behavior != "default" or made.behavior != "disable":
        return own
    as_default = replace(made, policy=replace(made.policy, behavior="default"))
    # The producer was keyed already, so a failing constant warned once.
    shadow = _node_key(
        producer,
        as_default,
        _code(producer, made.policy, versions),
        stored=True,
        warn=False,
    )
    if shadow.static is None:
        reason = f"producer {producer.name!r}: {shadow.reason}"
        return replace(
            own, static=None, reason=reason, via=producer.name, alternate=own
        )
    static = _core.hash_parts(
        [
            _SCHEMA,
            b"field of a disable producer",
            shadow.static,
            own.static,
        ]
    )
    return replace(
        own, static=static, params=shadow.params, via=producer.name, alternate=own
    )


def _extraction_parts(node: NodeDef) -> tuple[list[bytes], str | None]:
    """An extracted field's or column's own key parts, or why it has no key.

    Which decorator made it, its field or column, its declared type's `repr` and
    its fill's fingerprint, or a marker for no fill: a rebuild changing any of
    them changes the key of what reads it.
    """
    payload = _extract.extractor(node)
    if payload is None:
        return [], None
    noun = payload.noun
    parts = [
        b"extract",
        f"{noun}s".encode(),
        payload.field.encode(),
        repr(payload.declared).encode(),
    ]
    if payload.fill is _extract.MISSING_FILL:
        return [*parts, b"no fill", b""], None
    try:
        return [*parts, b"fill", fingerprint(payload.fill)], None
    except _core.UnfingerprintableError as error:
        return parts, f"fill_with of {noun} {payload.field!r}: {error}"
    except Exception as error:  # noqa: BLE001  # a failing fingerprint: uncacheable
        reason = (
            f"cannot fingerprint fill_with of {noun} {payload.field!r} of node "
            f"{node.name!r}: {error!r}"
        )
        _warn_build(reason)
        return parts, reason


def _key_params(
    node: NodeDef, ignore: frozenset[str], *, warn: bool
) -> tuple[list[KeyParam], str | None]:
    """`node`'s key parameters, the ignored ones left out, and why one has no version.

    A constant is versioned by its stable fingerprint; one that fails warns when
    `warn` is set.
    """
    params: list[KeyParam] = []
    reason: str | None = None
    for param in node.params:
        if param.name in ignore:
            continue
        # A group item is keyed by its place too, `xs[0]` or `xs['k']`: no plain
        # parameter has such a name, so a move between places or into a group misses.
        key_name = param.name if param.slot is None else f"{param.name}[{param.slot!r}]"
        if isinstance(param.binding, Source):
            params.append((key_name, param.binding.name, None))
            continue
        try:
            # The same on every hash seed, so a set constant hits in another process.
            version = stable_fingerprint(param.binding.value)
        except _core.UnfingerprintableError as error:
            version = None
            reason = reason or f"value() of parameter {param.name!r}: {error}"
        except Exception as error:  # noqa: BLE001  # a failing fingerprint: uncacheable
            version = None
            failed = f"cannot fingerprint value() of parameter {param.name!r}"
            reason = reason or f"{failed} of node {node.name!r}: {error!r}"
            if warn:
                _warn_build(reason)
        params.append((key_name, None, version))
    return params, reason


def _node_key(
    node: NodeDef,
    settled: _Resolved,
    code: bytes | UncacheableError,
    *,
    stored: bool | None = None,
    warn: bool = True,
) -> NodeKey:
    """`node`'s key parts, given its policy and its code version or why it has none.

    `stored` says whether a `default` node is looked up and stored; by default,
    unless it is an extracted field. Without `warn`, a failing fingerprint is
    not reported, as when the node was keyed, and reported, already.
    """
    policy = settled.policy
    behavior = settled.behavior
    params, reason = _key_params(node, policy.ignore, warn=warn)
    extraction, unkeyed = _extraction_parts(node)
    checked, unchecked = check_parts(node.checks)
    reason = reason or unkeyed or unchecked
    static = None
    if behavior == "disable":
        reason = f"policy disable ({settled.source})"
    elif isinstance(code, UncacheableError):
        reason = code.reason
    elif reason is None:
        # A read is framed as its name and an empty part, a constant as its name and
        # its version (never empty), so neither can pass for the other.
        framed = [
            part
            for name, _, version in params
            for part in (name.encode(), version or b"")
        ]
        static = _core.hash_parts(
            [
                _SCHEMA,
                node.name.encode(),
                node.module.encode(),
                _qualname(node).encode(),
                code,
                behavior.encode(),
                *framed,
                *extraction,
                *checked,
            ]
        )
    if stored is None:
        stored = not extraction
    return NodeKey(
        static, reason, behavior, tuple(params), stored, source=settled.source
    )


def check_parts(checks: Sequence[Check]) -> tuple[list[bytes], str | None]:
    """A node's output checks as key parts, or why one has no version (Plan 8).

    Each check is keyed by its name, importance and field, and by what it runs:
    a built-in's argument by fingerprint (an annotation by its text), a check
    function by its code version, a `Validator` by its class version and its
    attributes' fingerprint. So adding, removing or changing a check misses, and
    a value stored before a stricter check is never returned unchecked.
    """
    parts: list[bytes] = []
    for check in checks:
        kind, *rest = check.identity
        try:
            if kind == "builtin":
                version = _argument_version(rest[1])
            elif kind == "function" and callable(function := rest[0]):
                version = code_version(function)
            else:
                validator = rest[0]
                version = class_version(type(validator)) + fingerprint(
                    dict(sorted(vars(validator).items()))
                )
        except (UncacheableError, _core.UnfingerprintableError, TypeError) as error:
            reason = error.reason if isinstance(error, UncacheableError) else error
            return [], f"check {check.name}: {reason}"
        parts += [
            b"check",
            check.name.encode(),
            check.importance.encode(),
            (check.field or "").encode(),
            version,
        ]
    return parts, None


def _argument_version(argument: object) -> bytes:
    """A built-in check's argument by fingerprint; an annotation by its text."""
    try:
        return fingerprint(argument)
    except _core.UnfingerprintableError:
        if isinstance(argument, type) or typing.get_origin(argument) is not None:
            return _core.hash_parts([b"annotation", written(argument).encode()])
        if argument is typing.Any:
            return _core.hash_parts([b"annotation", b"Any"])
        raise


def _formats(
    store: CacheStore, nodes: Sequence[NodeDef], resolved: Mapping[str, _Resolved]
) -> dict[str, Format]:
    """Each node's format, resolved against `store`, every unknown one listed at once.

    A `MemoryCache` holds values, never bytes, so it takes any format and ignores it.
    Another store takes formats when it has a `formats` mapping, as `DiskCache` does.
    A field's format is its own `FieldCache`'s, never its producer's.
    """
    formats_of: object = getattr(store, "formats", None)
    known = formats_of if isinstance(formats_of, abc.Mapping) else None
    formats: dict[str, Format] = {}
    problems: list[str] = []
    for node in nodes:
        settled = resolved[node.name]
        if settled.field is not None:
            wanted = settled.field.format
        elif _extract.is_generated(node):
            # Never stored, so its producer's format does not apply.
            wanted = None
        else:
            wanted = settled.policy.format
        if wanted is None or isinstance(store, MemoryCache):
            continue
        name = wanted.name if isinstance(wanted, Format) else wanted
        if known is None:
            problems.append(
                f"node {node.name!r} names cache format {name!r}, but "
                f"{type(store).__qualname__} takes no formats"
            )
        elif isinstance(wanted, Format):
            if known.get(name, wanted) != wanted:
                problems.append(
                    f"node {node.name!r} names a cache format {name!r} unlike the "
                    "store's format of that name"
                )
            formats[node.name] = wanted
        elif (found := known.get(name)) is None:
            listed = ", ".join(repr(n) for n in sorted(known))
            problems.append(
                f"node {node.name!r} names cache format {name!r}, which the store "
                f"does not know (it knows {listed})"
            )
        else:
            formats[node.name] = found
    if problems:
        raise ValueError("; ".join(sorted(problems)))
    return formats


def _qualname(node: NodeDef) -> str:
    """The qualified name of `node`'s function."""
    qualname = getattr(node.origin, "__qualname__", None)
    return qualname if isinstance(qualname, str) else node.name


def _warn_build(reason: str) -> None:
    """Reports, at the `build` call, a node made uncacheable by a failure.

    At the first frame outside fnloom, however deep the build reached it.
    """
    warnings.warn(reason, CacheWarning, skip_file_prefixes=(_PACKAGE,))


@dataclass(frozen=True, slots=True)
class Cache:
    """A driver's cache: its store, and each node's key parts and sources.

    Attributes:
        store: Where results are kept.
        keys: Each node's `NodeKey`, by name.
        sources: The distinct nodes and inputs each node reads, by name.
        formats: The `Format` of each node that names one, by name.
    """

    store: CacheStore
    keys: Mapping[str, NodeKey]
    sources: Mapping[str, tuple[str, ...]]
    formats: Mapping[str, Format]

    @classmethod
    def of(
        cls,
        store: CacheStore,
        nodes: Sequence[NodeDef],
        settings: CacheSettings = NO_SETTINGS,
    ) -> Cache:
        """The cache over `store` for the built `nodes`, with `with_cache`'s lists.

        Raises:
            ValueError: A list or `ignore` names no node, or a parameter the
                consumer does not have; or a node's `@cache(format=...)` names a
                format the store does not know, or the store takes no formats.
        """
        if problems := settings.problems(nodes):
            raise ValueError("; ".join(problems))
        # Classes may have been edited since the last build: read them again.
        forget_class_versions()
        sources = {n.name: tuple(s for s, _ in core_inputs(n.params)) for n in nodes}
        resolved = _resolved(nodes, settings)
        return cls(
            store,
            _keyed(nodes, resolved),
            sources,
            _formats(store, nodes, resolved),
        )


@dataclass(frozen=True, slots=True)
class Begun:
    """A cached run, begun: what `CacheRun.begin` found, for the run and `finish`.

    Attributes:
        plan: The core plan to run: the request's, with the hits as overrides.
        overrides: The run's overrides and the hits, by name.
        hits: The values loaded from the store or computed by the pre-run, by name.
        keys: Each computed node's key, by name; empty on the binding's walk,
            which keeps them in `pending`.
        pending: On the binding's walk, the keys it stores as the core run calls
            it; `None` when `finish` stores each value instead.
        warned: The nodes this run has already warned about.
        values: Receives every value the run computes, for an enclosing run;
            `None` for none.
        outcomes: For a reported run only, the cache outcome of each node it
            reports, by name; `None` otherwise.
        loaded: For a reported run, the hits the store held, in plan order: not
            the values an inner run computed or loaded, which it reported.
        computed: For a reported run, the nodes `plan` computes, in its order.
    """

    plan: _core.Plan
    overrides: dict[str, object]
    hits: dict[str, object]
    keys: Mapping[str, bytes | None]
    pending: _core.Keys | None
    warned: set[str]
    values: dict[str, object] | None
    outcomes: dict[str, CacheOutcome] | None = None
    loaded: tuple[str, ...] = ()
    computed: tuple[str, ...] = ()


class _Preparer(Protocol):
    """The part of `_core.Graph` a cached run uses."""

    def prepare(
        self, outputs: list[str], inputs: list[str], overrides: list[str]
    ) -> _core.Plan:
        """Compiles a plan for the outputs, given the input and override names."""
        ...


def _warn_once(warned: set[str], name: str, message: str) -> None:
    """Warns `message` unless `name` already warned in this run.

    Keyed by name alone: once per node per run, so after a failed load a failed
    store of the same node in the same run is not reported again.
    """
    if name in warned:
        return
    warned.add(name)
    # The caller's frame: the depth to user code varies, and a store from
    # `on_value` is called from the native run.
    warnings.warn(message, CacheWarning, stacklevel=2)


class CacheRun:
    """The cached runs of one prepared plan; safe to share between threads.

    Made once per prepared plan: the key plan over its computed nodes, which of
    them run first because they are `recompute` nodes something reads, and the
    core plans compiled so far, one per hit set.
    """

    def __init__(  # noqa: PLR0913, PLR0917  # the prepared plan's request, and where it runs
        self,
        cache: Cache,
        graph: _Preparer,
        plan: _core.Plan,
        outputs: Sequence[str],
        input_names: frozenset[str],
        override_names: frozenset[str],
        fresh: Mapping[str, str] | None = None,
    ) -> None:
        """Holds the request's plan, compiled with no hits, and its key plan.

        `fresh` holds the producers an enclosing run's `recompute` fields need
        fresh, so a nested run computing one never looks it up either.
        """
        self._store = cache.store
        self._formats = cache.formats
        self._graph = graph
        self._plan = plan
        self._outputs = list(outputs)
        self._inputs = sorted(input_names)
        self._overrides = override_names
        steps = plan.steps()
        actions = dict(steps)
        computed = [name for name, action in steps if action == "compute"]
        keys = [_planned(cache.keys[name], actions) for name in computed]
        # The producer of each `recompute` field this plan, or an enclosing one,
        # computes: never looked up here, so the field reads a value the producer
        # just made, even when a nested run computes the producer (Q147).
        self._fresh: dict[str, str] = {
            producer: field
            for producer, field in (fresh or {}).items()
            if actions.get(producer) == "compute"
        }
        for name, k in sorted(zip(computed, keys, strict=True)):
            if k.fresh is not None and actions.get(k.fresh) == "compute":
                self._fresh.setdefault(k.fresh, name)
        # Looked up and stored: a `default` node that is no extracted field, or a
        # field with a policy of its own, unless a `recompute` field needs it fresh.
        stored = [
            k.behavior == "default" and k.stored and name not in self._fresh
            for name, k in zip(computed, keys, strict=True)
        ]
        self._key_plan = _core.KeyPlan(
            [
                (
                    name,
                    k.static,
                    [source for _, source, _ in k.params if source is not None],
                    looked_up,
                    list(cache.sources[name]),
                )
                for name, k, looked_up in zip(computed, keys, stored, strict=True)
            ],
            self._outputs,
        )
        # A plain `MemoryCache` is walked and written in the binding, with no call
        # into Python per node; a subclass may override its methods, so it is not.
        store = cache.store
        self._native = store.native if type(store) is MemoryCache else None
        # In plan order.
        self._node_keys = dict(zip(computed, keys, strict=True))
        self._behaviors = {name: k.behavior for name, k in self._node_keys.items()}
        self._stored = dict(zip(computed, stored, strict=True))
        self._sources = {name: cache.sources[name] for name in computed}
        # Every input and override a key reads gets a version, so a version that
        # is missing only ever means an optional input not supplied.
        read = {source for k in keys for _, source, _ in k.params if source is not None}
        self._read_inputs = sorted(n for n in read if actions.get(n) == "use_input")
        self._read_overrides = sorted(
            n for n in read if actions.get(n) == "use_override"
        )
        # A `recompute` node that another node reads runs first, so what reads it
        # is keyed by its result. Of those, the ones read by another of them run
        # first in turn: each nesting has fewer, so it ends.
        # Only a key parameter needs the version: a parameter that ignores the
        # node reads it as it runs.
        consumed = {source for k in keys for _, source, _ in k.params}
        self._first = tuple(
            name
            for name in computed
            if self._behaviors[name] == "recompute" and name in consumed
        )
        self._nested = None
        self._levels: tuple[CacheRun, ...] = (self,)
        if self._first:
            first = list(self._first)
            nested = graph.prepare(first, self._inputs, sorted(override_names))
            self._nested = CacheRun(
                cache, graph, nested, first, input_names, override_names, self._fresh
            )
            self._levels = (self, *self._nested._levels)  # noqa: SLF001  # the same class
        # Insertion order is recency order: a hit moves its plan to the end.
        self._plans: dict[frozenset[str], _core.Plan] = {}
        self._plans_lock = threading.Lock()

    @property
    def nested(self) -> CacheRun | None:
        """The run of the `recompute` nodes something reads, which goes first."""
        return self._nested

    def run(
        self,
        inputs: dict[str, object],
        overrides: dict[str, object],
        observers: Sequence[_core.ObserverBase] = (),
        /,
    ) -> dict[str, object]:
        """Runs the plan, already checked, through the store; the outputs, sorted.

        `observers` see the nodes the final core run computes: not a hit, nor a
        node of the nested run.
        """
        return self._finished(self.begin(inputs, overrides), inputs, observers)

    def _execute(
        self,
        inputs: dict[str, object],
        overrides: dict[str, object],
        versions: dict[str, bytes | None],
        warned: set[str],
        values: dict[str, object],
    ) -> dict[str, object]:
        """A nested run; `values` receives every value it computed or loaded."""
        begun = self.begin(
            inputs, overrides, versions=versions, warned=warned, values=values
        )
        return self._finished(begun, inputs)

    def _finished(
        self,
        begun: Begun,
        inputs: dict[str, object],
        observers: Sequence[_core.ObserverBase] = (),
        on_value: Callable[[str, object], None] | _core.Keys | None = None,
    ) -> dict[str, object]:
        """Runs a begun run's plan, storing each value as its node succeeds.

        `on_value`, when given, takes each value instead: by default the binding's
        keys store it, or else `finish` does.
        """
        if on_value is None:
            on_value = begun.pending
        result = begun.plan.run(
            inputs,
            begun.overrides,
            observers,
            on_value=functools.partial(self.finish, begun)
            if on_value is None
            else on_value,
        )
        if begun.values is not None:
            begun.values.update(begun.hits)
            begun.values.update(result)
        return result

    def begin(  # noqa: PLR0913  # the request, and what an enclosing run already has
        self,
        inputs: dict[str, object],
        overrides: dict[str, object],
        *,
        known: dict[str, object] | None = None,
        versions: dict[str, bytes | None] | None = None,
        warned: set[str] | None = None,
        values: dict[str, object] | None = None,
        native: bool = True,
        reasons: dict[str, str] | None = None,
    ) -> Begun:
        """Everything a cached run does before its core run: versions, keys, hits.

        `known` holds the values of the nested run, when the caller ran it; when
        it is `None`, the nested run, if any, runs here, synchronously. `versions`
        are the inputs' and overrides' when the caller already has them, and
        `warned` the nodes it already warned about. `values`, when given, receives
        every value the run computes or loads. With `native`, a plain
        `MemoryCache` is walked and written in the binding; without, every store
        call goes through `finish`.

        `reasons`, when given, holds why each input and override with no version
        has none, from `versions`, and makes the run a reported one: it is walked
        in Python, so its keys are known, and `Begun` carries the outcome of each
        node it reports, its hits and the nodes it computes. It receives why each
        `recompute` node run first has no version, if so.
        """
        warned = set() if warned is None else warned
        if versions is None:
            versions = self._versions(inputs, overrides, warned)
        if known is None:
            known = {}
            if self._nested is not None:
                self._nested._execute(inputs, overrides, dict(versions), warned, known)  # noqa: SLF001  # the same class
        if reasons is None:
            for name in self._first:
                versions[name] = _version(known[name], name, warned)
        else:
            versions.update(self._first_versions(known, warned, reasons))
        keys: Mapping[str, bytes | None]
        pending: _core.Keys | None = None
        if (
            native
            and self._native is not None
            and not known
            and values is None
            and reasons is None
        ):
            pending, hits = self._key_plan.walk(versions, self._native)
            keys = _NO_KEYS
        else:
            keys = self._key_plan.keys(versions)
            hits = self._frontier(keys, known, warned)
        if reasons is not None:
            return self._reported_begun(
                overrides, hits, keys, warned, values, reasons, known
            )
        if not hits:
            return Begun(self._plan, overrides, hits, keys, pending, warned, values)
        plan = self._plan_for(frozenset(hits))
        return Begun(plan, {**overrides, **hits}, hits, keys, pending, warned, values)

    def _first_versions(
        self, known: Mapping[str, object], warned: set[str], reasons: dict[str, str]
    ) -> dict[str, bytes | None]:
        """The version of each `recompute` node run first, from its result.

        `reasons` receives why each one with none has none, as for an input.
        """
        versions: dict[str, bytes | None] = {}
        for name in self._first:
            version = _version_or_reason(known[name], name, warned)
            if isinstance(version, bytes):
                versions[name] = version
            else:
                versions[name] = None
                reasons[name] = version
        return versions

    def _reported_begun(  # noqa: PLR0913, PLR0917  # what `begin` found
        self,
        overrides: dict[str, object],
        hits: dict[str, object],
        keys: Mapping[str, bytes | None],
        warned: set[str],
        values: dict[str, object] | None,
        reasons: Mapping[str, str],
        known: Mapping[str, object],
    ) -> Begun:
        """A reported run's `Begun`: its hits, the nodes it computes, their outcomes.

        A value in `known`, which an inner run computed or loaded and reported, is
        no hit of this one's.
        """
        plan = self._plan_for(frozenset(hits)) if hits else self._plan
        loaded = tuple(n for n in self._node_keys if n in hits and n not in known)
        computed = tuple(n for n, action in plan.steps() if action == "compute")
        outcomes = self._outcomes(keys, reasons, None)
        reported = {name: self._node_keys[name].outcome(found=True) for name in loaded}
        reported.update((name, outcomes[name]) for name in computed)
        return Begun(
            plan,
            {**overrides, **hits},
            hits,
            keys,
            None,
            warned,
            values,
            reported,
            loaded,
            computed,
        )

    def versions(
        self, inputs: dict[str, object], overrides: dict[str, object], warned: set[str]
    ) -> dict[str, bytes | None]:
        """The fingerprint of every input and override a key reads; see `begin`."""
        return self._versions(inputs, overrides, warned)

    def reported_levels(
        self,
        inputs: dict[str, object],
        overrides: dict[str, object],
        dispatch: Dispatch,
        start: Callable[[tuple[str, ...]], None],
    ) -> Generator[tuple[CacheRun, Begun], dict[str, object]]:
        """A reported run's levels, innermost first, for either driver to run.

        The one copy of what both drivers do around their runs, so they fire the
        same events. The innermost level looks its hits up first; then `start`
        fires `before_run` with the nodes the run reports, or with none, and the
        error raised, when the lookups raise. Each level then reports its hits to
        `dispatch` and is yielded with its `Begun`, for the caller to run its plan
        and send back every value the run computed or loaded, the next level's
        `known`.
        """
        levels = self._levels
        warned: set[str] = set()
        reasons: dict[str, str] = {}
        known: dict[str, object] = {}
        try:
            versions = self._versions(inputs, overrides, warned, reasons)
            begun = self._begin_reported(
                levels[-1], inputs, overrides, known, versions, warned, reasons
            )
        except BaseException:
            start(())
            raise
        start(self._reported(begun, levels[-1]))
        for at, level in enumerate(reversed(levels)):
            if at:
                begun = self._begin_reported(
                    level, inputs, overrides, known, versions, warned, reasons
                )
            dispatch.loaded(begun)
            known = yield level, begun

    def _begin_reported(  # noqa: PLR0913, PLR0917  # the request, and what the run has so far
        self,
        level: CacheRun,
        inputs: dict[str, object],
        overrides: dict[str, object],
        known: dict[str, object],
        versions: dict[str, bytes | None],
        warned: set[str],
        reasons: dict[str, str],
    ) -> Begun:
        """`level.begin` for a reported run whose outermost level is this one.

        `known` holds the values of the level run before it; an inner level gets
        copies of `versions` and `reasons`, and collects its values for the next.
        """
        outer = level is self
        return level.begin(
            inputs,
            overrides,
            known=known,
            versions=versions if outer else dict(versions),
            warned=warned,
            values=None if outer else {},
            native=False,
            reasons=reasons if outer else dict(reasons),
        )

    def _reported(self, begun: Begun, level: CacheRun) -> tuple[str, ...]:
        """The nodes a reported run reports, in order, once `level` has begun.

        When `level` is this run, exactly its hits and then the nodes it computes.
        When it is an inner run, which runs first because its `recompute` nodes key
        what reads them, the hits of the outer levels are known only once it ran:
        its own nodes, then every other node the plan could report, in plan order.
        """
        listed = begun.loaded + begun.computed
        if level is self:
            return listed
        seen = set(listed)
        return listed + tuple(n for n in self._node_keys if n not in seen)

    def hooked(
        self,
        inputs: dict[str, object],
        overrides: dict[str, object],
        dispatch: Dispatch,
        start: Callable[[tuple[str, ...]], None],
    ) -> dict[str, object]:
        """`run`, reporting every node to `dispatch`, a run's own dispatcher.

        Runs `reported_levels`: each level's nodes report through `dispatch`'s
        observers, and a node whose `after_node` raised is not stored (`kept`).
        """
        levels = self.reported_levels(inputs, overrides, dispatch, start)
        level, begun = next(levels)
        while True:
            result = level._finished(  # noqa: SLF001  # the same class
                begun,
                inputs,
                dispatch.observers(),
                functools.partial(level.kept, begun, dispatch),
            )
            try:
                level, begun = levels.send(
                    result if begun.values is None else begun.values
                )
            except StopIteration:
                return result

    def kept(self, begun: Begun, dispatch: Dispatch, name: str, value: object) -> None:
        """`finish`, unless `dispatch`'s hooks refused the value (R8).

        The core calls `on_value` after every observer, even one that raised: a
        node whose `after_node` raised is discarded, so it is not stored.
        """
        if not dispatch.refused:
            self.finish(begun, name, value)

    def finish(self, begun: Begun, name: str, value: object) -> None:
        """Stores node `name`'s `value`, which it just computed, under its key.

        Only a `default` node is ever looked up, so only one is stored: a
        `disable` node has no key, and a `recompute` node or an extracted field
        always runs. A store that raises skips the write, and warns.

        For a run begun without `pending` only: the binding's walk stores
        through its own keys. A name the run has no key for is a `KeyError`, so
        a run and a plan that disagree never skip a store silently.
        """
        if begun.values is not None:
            begun.values[name] = value
        key = begun.keys[name]
        if key is None or not self._stored[name]:
            return
        try:
            if (format_ := self._formats.get(name)) is not None:
                self._store.put(key, value, format=format_)  # ty: ignore[unknown-argument]  # as in `_get`
            else:
                self._store.put(key, value)
        except Exception as error:  # noqa: BLE001  # a store error skips the write
            _warn_once(
                begun.warned,
                name,
                f"cannot store node {name!r} in the cache: {error!r}",
            )

    def explain(
        self, inputs: dict[str, object], overrides: dict[str, object]
    ) -> dict[str, CacheOutcome]:
        """Each computed node's outcome, by name, in name order; runs nothing.

        The keys are a run's, except that no `recompute` node runs first: what
        reads one has no known key, and is `unknown`. Every default node with a key
        is looked up on its own, not only the ones a run's walk would reach, so a
        node upstream of a hit reports its own entry although a run would neither
        load nor run it.
        """
        warned: set[str] = set()
        reasons: dict[str, str] = {}
        versions = self._versions(inputs, overrides, warned, reasons)
        # No result, so no version: every key reading one is `None`.
        versions.update(dict.fromkeys(self._first))
        keys = self._key_plan.keys(versions)
        return dict(sorted(self._outcomes(keys, reasons, warned).items()))

    def _outcomes(
        self,
        keys: Mapping[str, bytes | None],
        reasons: Mapping[str, str],
        warned: set[str] | None,
    ) -> dict[str, CacheOutcome]:
        """Each computed node's outcome, by name, in plan order.

        With `warned`, `explain`'s: each `default` node with a key is looked up,
        and what reads a `recompute` node run first is `unknown`. Without, a run's,
        once its walk found the hits: such a node is a miss, the caller marking
        the hits, and a `recompute` node run first is read like an input.
        """
        outcomes: dict[str, CacheOutcome] = {}
        # Plan order, so each node's sources have their outcomes already.
        for name, k in self._node_keys.items():
            key = keys[name]
            if key is None:
                outcome = self._keyless(k, reasons, outcomes, ran=warned is None)
            elif name in self._fresh:
                field = self._fresh[name]
                outcome = CacheOutcome(
                    "miss", f"runs again for recompute field {field!r}"
                )
            else:
                found = (
                    warned is not None
                    and self._stored[name]
                    and self._get(key, name, warned) is not MISSING
                )
                outcome = k.outcome(found=found)
            # A `recompute` node always runs, so it is a miss whether or not its
            # key is known, unless it has none to be stored under.
            if k.behavior == "recompute" and outcome.status == "unknown":
                outcome = k.outcome(found=False)
            outcomes[name] = outcome
        return outcomes

    def _keyless(
        self,
        k: NodeKey,
        reasons: Mapping[str, str],
        outcomes: Mapping[str, CacheOutcome],
        *,
        ran: bool,
    ) -> CacheOutcome:
        """Why a node has no key: its own reason, else the first source it reads.

        An uncacheable source outweighs one with no known version yet, since the
        node is uncacheable whatever that version turns out to be. When the
        `recompute` nodes run first `ran`, each has its version, or its reason.
        """
        if k.static is None:
            return CacheOutcome("uncacheable", k.reason)
        unknown: CacheOutcome | None = None
        for _, source, _ in k.params:
            if source is None:
                continue
            if source in self._first and not ran:
                found = CacheOutcome(
                    "unknown", f"downstream of recompute node {source!r}"
                )
            elif source in reasons:
                return CacheOutcome("uncacheable", reasons[source])
            elif (upstream := outcomes.get(source)) is None:
                continue
            elif upstream.status == "uncacheable":
                return CacheOutcome("uncacheable", f"reads uncacheable {source!r}")
            elif upstream.status == "unknown":
                found = upstream
            else:
                continue
            unknown = unknown or found
        # A source with no version is one of the above; never guess a hit.
        return unknown or CacheOutcome("unknown", "no key")

    def _versions(
        self,
        inputs: dict[str, object],
        overrides: dict[str, object],
        warned: set[str],
        reasons: dict[str, str] | None = None,
    ) -> dict[str, bytes | None]:
        """The fingerprint of every input and override a key reads; `None` if none.

        `reasons`, when given, receives why each one with no fingerprint has none.
        """
        versions: dict[str, bytes | None] = {}
        for values, names in (
            (inputs, self._read_inputs),
            (overrides, self._read_overrides),
        ):
            for name in names:
                version = _version_or_reason(values[name], name, warned)
                if isinstance(version, bytes):
                    versions[name] = version
                else:
                    versions[name] = None
                    if reasons is not None:
                        reasons[name] = version
        return versions

    def _frontier(
        self,
        keys: Mapping[str, bytes | None],
        known: Mapping[str, object],
        warned: set[str],
    ) -> dict[str, object]:
        """The hits, by name, that stop the walk back from the outputs.

        A node already computed by the nested run is one, and so is a `default`
        node with a key the store holds. Every other computed node the walk reaches
        must run, and the walk goes on into what it reads.
        """
        hits: dict[str, object] = {}
        seen: set[str] = set()
        stack = list(self._outputs)
        while stack:
            name = stack.pop()
            if name in seen:
                continue
            seen.add(name)
            sources = self._sources.get(name)
            if sources is None:
                # An input or an override: nothing to look up.
                continue
            if name in known:
                hits[name] = known[name]
                continue
            key = keys[name]
            if key is not None and self._stored[name]:
                value = self._get(key, name, warned)
                if value is not MISSING:
                    hits[name] = value
                    continue
            stack.extend(sources)
        return hits

    def _plan_for(self, hits: frozenset[str]) -> _core.Plan:
        """The core plan with `hits` as overrides, compiled on first use."""
        with self._plans_lock:
            plan = self._plans.pop(hits, None)
            if plan is None:
                overrides = sorted(self._overrides | hits)
                plan = self._graph.prepare(self._outputs, self._inputs, overrides)
            self._plans[hits] = plan
            if len(self._plans) > HIT_PLAN_CACHE_SIZE:
                del self._plans[next(iter(self._plans))]
        return plan

    def _get(self, key: bytes, name: str, warned: set[str]) -> object:
        """The store's value for `key`; `MISSING` when the store raises."""
        try:
            if (format_ := self._formats.get(name)) is not None:
                return self._store.get(key, format=format_)  # ty: ignore[unknown-argument]  # checked at build: the store takes formats
            return self._store.get(key)
        except Exception as error:  # noqa: BLE001  # a store error is a miss
            _warn_once(
                warned, name, f"cannot load node {name!r} from the cache: {error!r}"
            )
            return MISSING


def _planned(key: NodeKey, actions: Mapping[str, str]) -> NodeKey:
    """`key` as a plan with these `actions` reads it.

    A field keyed through its `disable` producer's sources is keyed by the
    producer instead when the plan does not compute the producer, as when it is
    an override: then the producer's sources are not in the plan.
    """
    if (
        key.via is not None
        and key.alternate is not None
        and actions.get(key.via) != "compute"
    ):
        return key.alternate
    return key


def _version(value: object, name: str, warned: set[str]) -> bytes | None:
    """`value`'s fingerprint, or `None` when it has none."""
    version = _version_or_reason(value, name, warned)
    return version if isinstance(version, bytes) else None


def _version_or_reason(value: object, name: str, warned: set[str]) -> bytes | str:
    """`value`'s fingerprint, or why it has none, such as `items[0]: Model`.

    A value with no fingerprint is expected, and quiet: what reads it runs. A
    fingerprint function that fails is reported.
    """
    try:
        return fingerprint(value)
    except _core.UnfingerprintableError as error:
        return f"{name}{error.path}: {error.type_name}"
    except Exception as error:  # noqa: BLE001  # a failing fingerprint is no version
        reason = f"cannot fingerprint {name!r} for the cache: {error!r}"
        _warn_once(warned, name, reason)
        return reason
