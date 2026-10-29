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
import threading
import typing
import warnings
from collections import abc
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, NamedTuple, Protocol

from fnloom import _core, _extract
from fnloom._codeversion import UncacheableError, class_version, code_version
from fnloom._decorators import CacheBehavior, CachePolicy, Source, decorations
from fnloom._fingerprint import fingerprint, forget_class_versions
from fnloom._scan import core_inputs
from fnloom._stores import MISSING, CacheStore, CacheWarning, Format, MemoryCache
from fnloom._types import written

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Mapping, Sequence

    from fnloom._checks import Check
    from fnloom._hooks import Dispatch
    from fnloom._scan import NodeDef

# The key schema version, the first part of every static key part.
_SCHEMA = b"fnloom-cache-2"

# How many core plans a prepared plan keeps, one per hit set; the least recently
# used one goes first.
HIT_PLAN_CACHE_SIZE = 64

# The policy of a node without `@cache`.
_DEFAULT_POLICY = CachePolicy("default", frozenset(), None)

# A key parameter: its name, the node or input it reads (`None` for a constant), and
# a constant's version (`None` for a read, or a constant with no fingerprint).
type KeyParam = tuple[str, str | None, bytes | None]


class CacheOutcome(NamedTuple):
    """What a cached run would do with one computed node, from `explain_cache`.

    Attributes:
        status: `"hit"`: the store holds the node's key. `"miss"`: it does not, so
            the node runs and is stored; a `recompute` node is always one, with the
            reason `"policy recompute"`, and is never stored. `"uncacheable"`: the
            node has no key, so it always runs and is never stored. An
            `@extract_*` field or column node is always a miss, with the reason
            `"extracted field: runs whenever needed; never served from the store"`:
            it is keyed but never stored, so its check runs whenever it runs.
            `"unknown"`: the key depends on the result of a `recompute` node,
            which only a run knows.
        reason: Why a node is uncacheable or unknown, or is a `recompute` miss;
            `None` for a plain hit or miss.
    """

    status: Literal["hit", "miss", "uncacheable", "unknown"]
    reason: str | None


# A run's keys on the binding's walk, which keeps them itself.
_EMPTY_KEYS: dict[str, bytes | None] = {}
_NO_KEYS: Mapping[str, bytes | None] = MappingProxyType(_EMPTY_KEYS)

# The outcome of a `recompute` node with a key, or one that only a run would know.
_RECOMPUTE = CacheOutcome("miss", "policy recompute")

# The outcomes of a `default` node the store holds, and of one it does not.
_HIT = CacheOutcome("hit", None)
_MISS = CacheOutcome("miss", None)

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
            extracted field, which is keyed but never served from the store.
    """

    static: bytes | None
    reason: str | None
    behavior: CacheBehavior
    params: tuple[KeyParam, ...]
    stored: bool = True


def node_keys(nodes: Sequence[NodeDef]) -> dict[str, NodeKey]:
    """The `NodeKey` of every node, by name; made once, at build.

    A function's code version is computed once, however many rows it has.
    """
    # By the origin's id; `nodes` keeps every origin alive meanwhile.
    versions: dict[int, bytes | UncacheableError] = {}
    keys: dict[str, NodeKey] = {}
    for node in nodes:
        policy = decorations(node.origin).cache or _DEFAULT_POLICY
        if _extract.is_generated(node):
            policy = _field_policy(policy)
        code: bytes | UncacheableError
        if policy.behavior == "disable":
            # Never keyed, so its code is never read.
            code = b""
        elif policy.version is not None:
            code = policy.version.encode()
        elif (code_or_none := versions.get(id(node.origin))) is not None:
            code = code_or_none
        else:
            try:
                code = code_version(node.origin)
            except UncacheableError as error:
                code = error
            except Exception as error:  # noqa: BLE001  # a failing version: uncacheable
                # Such as a default's fingerprint function raising: the build goes
                # on, as it does without a cache, and the node always runs.
                code = UncacheableError(
                    f"cannot read the code version of {_qualname(node)!r}: {error!r}"
                )
                # Up: `node_keys`, `Cache.of`, `Builder.build`, its caller.
                _warn_build(code.reason, stacklevel=5)
            versions[id(node.origin)] = code
        keys[node.name] = _node_key(node, policy, code)
    return keys


def _field_policy(producer: CachePolicy) -> CachePolicy:
    """An extracted field's policy: keyed like its producer, never stored.

    `disable` carries over; `recompute`, `ignore` and the format do not, since a
    field runs whenever it is needed and is never stored. A pinned `version`
    carries over.
    """
    behavior: CacheBehavior = "disable" if producer.behavior == "disable" else "default"
    return CachePolicy(behavior, frozenset(), producer.version)


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
        # Up: `_extraction_parts`, `_node_key`, `node_keys`, `Cache.of`,
        # `Builder.build`, its caller.
        _warn_build(reason, stacklevel=7)
        return parts, reason


def _node_key(
    node: NodeDef, policy: CachePolicy, code: bytes | UncacheableError
) -> NodeKey:
    """`node`'s key parts, given its policy and its code version or why it has none."""
    params: list[KeyParam] = []
    reason: str | None = None
    for param in node.params:
        if param.name in policy.ignore:
            continue
        # A group item is keyed by its place too, `xs[0]` or `xs['k']`: no plain
        # parameter has such a name, so a move between places or into a group misses.
        key_name = param.name if param.slot is None else f"{param.name}[{param.slot!r}]"
        if isinstance(param.binding, Source):
            params.append((key_name, param.binding.name, None))
            continue
        try:
            version = fingerprint(param.binding.value)
        except _core.UnfingerprintableError as error:
            version = None
            reason = reason or f"value() of parameter {param.name!r}: {error}"
        except Exception as error:  # noqa: BLE001  # a failing fingerprint: uncacheable
            version = None
            failed = f"cannot fingerprint value() of parameter {param.name!r}"
            reason = reason or f"{failed} of node {node.name!r}: {error!r}"
            # Up: `_node_key`, `node_keys`, `Cache.of`, `Builder.build`, its caller.
            _warn_build(reason, stacklevel=6)
        params.append((key_name, None, version))
    extraction, unkeyed = _extraction_parts(node)
    checked, unchecked = check_parts(node.checks)
    reason = reason or unkeyed or unchecked
    static = None
    if policy.behavior == "disable":
        reason = "policy disable"
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
                policy.behavior.encode(),
                *framed,
                *extraction,
                *checked,
            ]
        )
    stored = not extraction
    return NodeKey(static, reason, policy.behavior, tuple(params), stored)


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


def _formats(store: CacheStore, nodes: Sequence[NodeDef]) -> dict[str, Format]:
    """Each node's format, resolved against `store`, every unknown one listed at once.

    A `MemoryCache` holds values, never bytes, so it takes any format and ignores it.
    Another store takes formats when it has a `formats` mapping, as `DiskCache` does.
    """
    formats_of: object = getattr(store, "formats", None)
    known = formats_of if isinstance(formats_of, abc.Mapping) else None
    formats: dict[str, Format] = {}
    problems: list[str] = []
    for node in nodes:
        # A field is never stored, so its producer's format does not apply.
        policy = None if _extract.is_generated(node) else decorations(node.origin).cache
        wanted = None if policy is None else policy.format
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


def _warn_build(reason: str, *, stacklevel: int) -> None:
    """Reports, at the `build` call, a node made uncacheable by a failure."""
    warnings.warn(reason, CacheWarning, stacklevel=stacklevel)


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
    def of(cls, store: CacheStore, nodes: Sequence[NodeDef]) -> Cache:
        """The cache over `store` for the built `nodes`.

        Raises:
            ValueError: A node's `@cache(format=...)` names a format the store does
                not know, or the store takes no formats.
        """
        # Classes may have been edited since the last build: read them again.
        forget_class_versions()
        sources = {n.name: tuple(s for s, _ in core_inputs(n.params)) for n in nodes}
        return cls(store, node_keys(nodes), sources, _formats(store, nodes))


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
    ) -> None:
        """Holds the request's plan, compiled with no hits, and its key plan."""
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
        keys = [cache.keys[name] for name in computed]
        self._key_plan = _core.KeyPlan(
            [
                (
                    name,
                    k.static,
                    [source for _, source, _ in k.params if source is not None],
                    k.behavior == "default" and k.stored,
                    list(cache.sources[name]),
                )
                for name, k in zip(computed, keys, strict=True)
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
        # Looked up and stored: a `default` node that is no extracted field.
        self._stored = {
            name: k.behavior == "default" and k.stored
            for name, k in self._node_keys.items()
        }
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
                cache, graph, nested, first, input_names, override_names
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
        reported = dict.fromkeys(loaded, _HIT)
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
            elif not k.stored:
                outcome = _EXTRACTED
            elif k.behavior == "default":
                found = (
                    warned is not None and self._get(key, name, warned) is not MISSING
                )
                outcome = _HIT if found else _MISS
            else:
                outcome = _RECOMPUTE
            # A `recompute` node always runs, so it is a miss whether or not its
            # key is known, unless it has none to be stored under.
            if k.behavior == "recompute" and outcome.status == "unknown":
                outcome = _RECOMPUTE
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
