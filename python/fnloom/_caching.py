"""Cached runs: each node's static key part, made at build, and runs that skip hits.

A node's key hashes its static part (identity, code version and behavior) and the
version of each value it reads: an input's or override's fingerprint, a computed
node's key, or a `recompute` node's result's fingerprint. `CacheRun` computes every
key of a plan before anything runs, walks back from the outputs to the first nodes
the store holds, and runs the plan with those as overrides, so the core prunes their
upstream. Each node that runs is stored under its key as it succeeds.

The cache never fails a run: a store that raises is a miss or a skipped write,
reported with one `CacheWarning` per node per run. A node's own exception fails the
run as without the cache, and nothing is stored for that node.
"""

import threading
import warnings
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from fnloom import _core
from fnloom._codeversion import UncacheableError, code_version
from fnloom._decorators import CacheBehavior, CachePolicy, Source, decorations
from fnloom._fingerprint import fingerprint
from fnloom._scan import core_inputs
from fnloom._stores import MISSING, CacheStore, CacheWarning

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from fnloom._scan import NodeDef

# The key schema version, the first part of every static key part.
_SCHEMA = b"fnloom-cache-1"

# How many core plans a prepared plan keeps, one per hit set; the least recently
# used one goes first.
HIT_PLAN_CACHE_SIZE = 64

# The policy of a node without `@cache`.
_DEFAULT_POLICY = CachePolicy("default", frozenset(), None)

# A key parameter: its name, the node or input it reads (`None` for a constant), and
# a constant's version (`None` for a read, or a constant with no fingerprint).
type KeyParam = tuple[str, str | None, bytes | None]


@dataclass(frozen=True, slots=True)
class NodeKey:
    """What a node's key is made of, apart from the versions of what it reads.

    Attributes:
        static: The hash of the key schema, the node's name, module, qualified name,
            code version and behavior; `None` when the node cannot be cached.
        reason: Why `static` is `None`, else `None`.
        behavior: The node's cache behavior.
        params: The key parameters, in declaration order, the ignored ones left out.
    """

    static: bytes | None
    reason: str | None
    behavior: CacheBehavior
    params: tuple[KeyParam, ...]


def node_keys(nodes: Sequence[NodeDef]) -> dict[str, NodeKey]:
    """The `NodeKey` of every node, by name; made once, at build.

    A function's code version is computed once, however many rows it has.
    """
    # By the origin's id; `nodes` keeps every origin alive meanwhile.
    versions: dict[int, bytes | UncacheableError] = {}
    keys: dict[str, NodeKey] = {}
    for node in nodes:
        policy = decorations(node.origin).cache or _DEFAULT_POLICY
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


def _node_key(
    node: NodeDef, policy: CachePolicy, code: bytes | UncacheableError
) -> NodeKey:
    """`node`'s key parts, given its policy and its code version or why it has none."""
    params: list[KeyParam] = []
    reason: str | None = None
    for param in node.params:
        if param.name in policy.ignore:
            continue
        if isinstance(param.binding, Source):
            params.append((param.name, param.binding.name, None))
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
        params.append((param.name, None, version))
    static = None
    if policy.behavior == "disable":
        reason = "policy disable"
    elif isinstance(code, UncacheableError):
        reason = code.reason
    elif reason is None:
        static = _core.hash_parts(
            [
                _SCHEMA,
                node.name.encode(),
                node.module.encode(),
                _qualname(node).encode(),
                code,
                policy.behavior.encode(),
            ]
        )
    return NodeKey(static, reason, policy.behavior, tuple(params))


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
    """

    store: CacheStore
    keys: Mapping[str, NodeKey]
    sources: Mapping[str, tuple[str, ...]]

    @classmethod
    def of(cls, store: CacheStore, nodes: Sequence[NodeDef]) -> Cache:
        """The cache over `store` for the built `nodes`."""
        sources = {n.name: tuple(s for s, _ in core_inputs(n.params)) for n in nodes}
        return cls(store, node_keys(nodes), sources)


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
                (name, k.static, list(k.params))
                for name, k in zip(computed, keys, strict=True)
            ]
        )
        self._behaviors = {
            name: k.behavior for name, k in zip(computed, keys, strict=True)
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
        if self._first:
            first = list(self._first)
            nested = graph.prepare(first, self._inputs, sorted(override_names))
            self._nested = CacheRun(
                cache, graph, nested, first, input_names, override_names
            )
        # Insertion order is recency order: a hit moves its plan to the end.
        self._plans: dict[frozenset[str], _core.Plan] = {}
        self._plans_lock = threading.Lock()

    def run(
        self, inputs: dict[str, object], overrides: dict[str, object]
    ) -> dict[str, object]:
        """Runs the plan, already checked, through the store; the outputs, sorted."""
        return self._execute(inputs, overrides, None, set(), None)

    def _execute(
        self,
        inputs: dict[str, object],
        overrides: dict[str, object],
        versions: dict[str, bytes | None] | None,
        warned: set[str],
        values: dict[str, object] | None,
    ) -> dict[str, object]:
        """One cached run: versions, keys, the hit frontier, then the pruned plan.

        `versions` are the inputs' and overrides' when an enclosing run already
        has them. `values`, when given, receives every value this run computed or
        loaded, for the enclosing run to use as it is.
        """
        if versions is None:
            versions = self._versions(inputs, overrides, warned)
        known: dict[str, object] = {}
        if self._nested is not None:
            self._nested._execute(inputs, overrides, dict(versions), warned, known)  # noqa: SLF001  # the same class
            for name in self._first:
                versions[name] = _version(known[name], name, warned)
        keys = self._key_plan.keys(versions)
        hits = self._frontier(keys, known, warned)
        plan = self._plan_for(frozenset(hits)) if hits else self._plan
        result = plan.run(
            inputs,
            {**overrides, **hits} if hits else overrides,
            on_value=self._on_value(keys, warned, values),
        )
        if values is not None:
            values.update(hits)
            values.update(result)
        return result

    def _versions(
        self,
        inputs: dict[str, object],
        overrides: dict[str, object],
        warned: set[str],
    ) -> dict[str, bytes | None]:
        """The fingerprint of every input and override a key reads; `None` if none."""
        versions = {
            name: _version(inputs[name], name, warned) for name in self._read_inputs
        }
        for name in self._read_overrides:
            versions[name] = _version(overrides[name], name, warned)
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
            if key is not None and self._behaviors[name] == "default":
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
            return self._store.get(key)
        except Exception as error:  # noqa: BLE001  # a store error is a miss
            _warn_once(
                warned, name, f"cannot load node {name!r} from the cache: {error!r}"
            )
            return MISSING

    def _on_value(
        self,
        keys: Mapping[str, bytes | None],
        warned: set[str],
        values: dict[str, object] | None,
    ) -> Callable[[str, object], None]:
        """What stores each computed value as its node succeeds, and records it."""
        store = self._store

        def on_value(name: str, value: object) -> None:
            if values is not None:
                values[name] = value
            # A `disable` node has no static part, so no key: it is never stored.
            key = keys[name]
            if key is None:
                return
            try:
                store.put(key, value)
            except Exception as error:  # noqa: BLE001  # a store error skips the write
                _warn_once(
                    warned, name, f"cannot store node {name!r} in the cache: {error!r}"
                )

        return on_value


def _version(value: object, name: str, warned: set[str]) -> bytes | None:
    """`value`'s fingerprint, or `None` when it has none.

    A value with no fingerprint is expected, and quiet: what reads it runs. A
    fingerprint function that fails is reported.
    """
    try:
        return fingerprint(value)
    except _core.UnfingerprintableError:
        return None
    except Exception as error:  # noqa: BLE001  # a failing fingerprint is no version
        _warn_once(
            warned, name, f"cannot fingerprint {name!r} for the cache: {error!r}"
        )
        return None
