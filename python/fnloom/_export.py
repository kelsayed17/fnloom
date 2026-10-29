"""Exports: a graph, or one run of it, as a JSON-ready document, and their diff.

A document says what each node is: its type, what each parameter reads, its tags,
config conditions and source, and its code version (Q95, Q96). It never holds a
value: a request's inputs and overrides are listed by name only, config values and
`value()` bindings as `repr`.
"""

import dataclasses
import hashlib
import inspect
import json
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from fnloom._caching import check_parts
from fnloom._canonical import DEPTH, UnwritableError, stable_fingerprint
from fnloom._codeversion import UncacheableError, code_version
from fnloom._decorators import Source, Value, decorations
from fnloom._drawing import condition_text
from fnloom._types import Unresolved

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from fnloom._checks import Check
    from fnloom._drawing import Drawing
    from fnloom._view import InputView, NodeView, Param

FORMAT = "fnloom.export/1"

# What `diff` compares, in the order it lists them.
_COMPARED = ("kind", "type", "params", "config", "checks", "tags", "version", "marks")


def export(  # noqa: PLR0913  # one argument per part of the document
    drawing: Drawing,
    views: Mapping[str, NodeView],
    *,
    inputs: Mapping[str, InputView],
    config: Mapping[str, object],
    request: Mapping[str, list[str]] | None,
    version: str,
) -> dict[str, Any]:
    """The document for `drawing`'s nodes, in its order, with its marks.

    `request` is the outputs, input names and override names of a run, or `None`
    for the whole graph. `version` is fnloom's.
    """
    versions: dict[int, tuple[str | None, str | None]] = {}
    nodes = [
        _node(
            drawn.name,
            sorted(drawn.marks),
            views[drawn.name],
            inputs,
            versions,
            built_from=getattr(views, "built_from", {}).get(drawn.name),
            checks=getattr(views, "checks", {}).get(drawn.name, ()),
        )
        for drawn in drawing.nodes
    ]
    return {
        "format": FORMAT,
        "fnloom": version,
        "graph_version": _graph_version(nodes, views),
        "config": {key: stable_repr(config[key]) for key in sorted(config)},
        "request": None if request is None else dict(request),
        "nodes": nodes,
    }


# A default `repr`'s address, which changes with every process.
_ADDRESS = re.compile(r" at 0x[0-9a-fA-F]+")


def stable_repr(value: object) -> str:
    """`repr(value)`, the same in every process: sets sorted, addresses dropped.

    A set's members come in the order the hash seed gives, and an object's
    default `repr` names its address; neither says anything about the value.
    Sets, mappings, lists, tuples, named tuples and dataclass instances are
    written part by part, so a set inside any of them is sorted too; anything
    else is its own `repr`, its addresses dropped. A container holding itself
    shows `...` there, as `repr` does. A value nested past 100 levels, or whose
    own `__iter__`, `__repr__` or the like raises, is `<unrepresentable T>`.
    """
    try:
        return _written(value, frozenset(), 0)
    except Exception:  # noqa: BLE001  # the value's own code raised, or too deep
        return f"<unrepresentable {type(value).__qualname__}>"


def _written(value: object, seen: frozenset[int], depth: int) -> str:  # noqa: PLR0911  # one return per kind
    if depth > DEPTH:
        raise UnwritableError
    if id(value) in seen:
        return "..."
    inner = seen | {id(value)}
    name = type(value).__name__

    def each(items: Iterable[object]) -> list[str]:
        return [_written(item, inner, depth + 1) for item in items]

    if isinstance(value, (set, frozenset)):
        return _written_set(value, sorted(each(value)))
    if isinstance(value, Mapping):
        pairs = ", ".join(
            f"{_written(k, inner, depth + 1)}: {_written(v, inner, depth + 1)}"
            for k, v in value.items()
        )
        return f"{{{pairs}}}" if type(value) is dict else f"{name}({{{pairs}}})"
    if isinstance(value, tuple) and hasattr(value, "_fields"):
        fields = getattr(value, "_fields")  # noqa: B009  # a named tuple's
        named = ", ".join(
            f"{field}={text}" for field, text in zip(fields, each(value), strict=True)
        )
        return f"{name}({named})"
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        shown = [f for f in dataclasses.fields(value) if f.repr]
        named = ", ".join(
            f"{f.name}={_written(getattr(value, f.name), inner, depth + 1)}"
            for f in shown
        )
        return f"{name}({named})"
    if isinstance(value, list):
        listed = f"[{', '.join(each(value))}]"
        return listed if type(value) is list else f"{name}({listed})"
    if isinstance(value, tuple):
        items = each(value)
        listed = f"({items[0]},)" if len(items) == 1 else f"({', '.join(items)})"
        return listed if type(value) is tuple else f"{name}({listed})"
    return _ADDRESS.sub("", repr(value))


def _written_set(value: set[object] | frozenset[object], members: list[str]) -> str:
    """A set as `repr` writes it, its members already written and sorted."""
    listed = ", ".join(members)
    if type(value) is set:
        return f"{{{listed}}}" if value else "set()"
    name = type(value).__name__
    return f"{name}({{{listed}}})" if value else f"{name}()"


def value_version(value: object) -> str | None:
    """A `value()` binding's version: what `graph_version` and `diff` compare.

    Its `stable_fingerprint` in hex, as the cache keys it. `None` when it has
    none: such a value's node is unversioned, never unchanged.
    """
    try:
        return stable_fingerprint(value).hex()
    except Exception:  # noqa: BLE001  # no fingerprint, too deep, or the value raised
        return None


def type_text(annotation: object) -> str:
    """`annotation` as text, whole: an unresolved name as written (Q81)."""
    if isinstance(annotation, Unresolved):
        return annotation.name
    return inspect.formatannotation(annotation)


def _node(  # noqa: PLR0913  # the node's parts, and its build's
    name: str,
    marks: list[str],
    view: NodeView,
    inputs: Mapping[str, InputView],
    versions: dict[int, tuple[str | None, str | None]],
    *,
    built_from: Callable[..., object] | None = None,
    checks: Sequence[Check] = (),
) -> dict[str, Any]:
    """One node's entry; an input's has no source or version (Q95).

    `built_from` is the function the node was built from when it isn't the
    view's `origin`, a `@resolve` copy: its decorations, such as a pinned
    version, are the ones that apply. `checks` are the node's output checks:
    listed, and part of its `version`, as the result cache keys them.
    """
    entry: dict[str, Any] = {
        "name": name,
        "kind": "input" if view.is_input else "function",
        "type": type_text(view.type),
        "params": [_param(p) for p in view.params],
        "dependencies": list(view.dependencies),
        "tags": dict(view.tags),
        "doc": view.doc,
        "module": view.module,
        "function": None,
        "namespace": view.namespace,
        "config": [condition_text([c]) for c in view.config],
        "source": None,
        "version": None,
        "marks": marks,
        "checks": [_check(c) for c in checks],
    }
    if view.is_input:
        found = inputs[name]
        entry["required"] = found.required
        entry["consumers"] = list(found.consumers)
        return entry
    origin = view.origin
    if origin is not None:
        qualname = getattr(origin, "__qualname__", type(origin).__qualname__)
        entry["function"] = f"{origin.__module__}.{qualname}"
        entry["source"] = _source(origin)
        built = origin if built_from is None else built_from
        key = id(built)
        if key not in versions:
            versions[key] = _version(built)
        entry["version"], unversioned = versions[key]
        if checks and entry["version"] is not None:
            parts, unchecked = check_parts(checks)
            if unchecked is not None:
                entry["version"], unversioned = None, unchecked
            else:
                both = hashlib.sha256(bytes.fromhex(entry["version"]))
                for part in parts:
                    both.update(len(part).to_bytes(8, "big") + part)
                entry["version"] = both.hexdigest()
        if unversioned is not None:
            entry["unversioned"] = unversioned
        if (
            policy := decorations(built).cache
        ) is not None and policy.version is not None:
            entry["pinned"] = policy.version
    return entry


def _check(check: Check) -> dict[str, Any]:
    """One output check as the export lists it: what it is, never what it read."""
    kind, *rest = check.identity
    if kind == "builtin":
        given = stable_repr(rest[1])
    elif kind == "function":
        function = rest[0]
        module = getattr(function, "__module__", "")
        given = f"{module}.{getattr(function, '__qualname__', '')}"
    else:
        cls = type(rest[0])
        given = f"{cls.__module__}.{cls.__qualname__}"
    return {
        "name": check.name,
        "kind": kind,
        "importance": check.importance,
        "field": check.field,
        "given": given,
    }


def _param(param: Param) -> dict[str, Any]:
    binding = param.binding
    return {
        "name": param.name,
        "slot": param.slot,
        "reads": binding.name if isinstance(binding, Source) else None,
        "value": stable_repr(binding.value) if isinstance(binding, Value) else None,
        "value_version": (
            value_version(binding.value) if isinstance(binding, Value) else None
        ),
        "required": param.required,
        "type": type_text(param.annotation),
    }


def _source(fn: Callable[..., object]) -> str | None:
    try:
        return inspect.getsource(fn)
    except OSError, TypeError:
        return None


def _version(fn: Callable[..., object]) -> tuple[str | None, str | None]:
    """`fn`'s code version in hex, or `None` and why it has none (Q96).

    A version pinned with `@cache(version=...)` stands for the code, as the
    result cache takes it: the hex is a hash of the pin.
    """
    policy = decorations(fn).cache
    if policy is not None and policy.version is not None:
        pinned = json.dumps([FORMAT, "pinned", policy.version])
        return hashlib.sha256(pinned.encode()).hexdigest(), None
    try:
        return code_version(fn).hex(), None
    except UncacheableError as error:
        return None, error.reason
    except Exception as error:  # noqa: BLE001  # any failure: unversioned, said why
        return None, f"cannot read the code version: {error!r}"


def _graph_version(
    nodes: Sequence[Mapping[str, Any]], views: Mapping[str, NodeView]
) -> str | None:
    """A hash of every node's name, kind, type, params, conditions and version.

    Tags and docs are left out: they don't change what a node computes. A
    `value()` is hashed by its `value_version`. `None` when a node is
    unversioned, or a `value()` it is bound to has no fingerprint: a change there
    could not be seen (Q96).
    """
    parts: list[object] = []
    for node in nodes:
        if node["kind"] == "function" and node["version"] is None:
            return None
        bound: list[object] = []
        params = views[node["name"]].params
        for param, entry in zip(params, node["params"], strict=True):
            binding = param.binding
            if isinstance(binding, Source):
                bound.append([param.name, param.slot, binding.name, param.required])
                continue
            value = entry["value_version"]
            if value is None:
                return None
            bound.append([param.name, param.slot, None, value])
        named = [node[key] for key in ("name", "kind", "type")]
        parts.append(
            [*named, bound, node["config"], node.get("checks", []), node["version"]]
        )
    text = json.dumps([FORMAT, parts], sort_keys=True)
    return hashlib.sha256(text.encode()).hexdigest()


def diff(old: Mapping[str, Any], new: Mapping[str, Any]) -> dict[str, Any]:
    """What changed between two documents, node by node.

    `added` and `removed` are names, sorted; `changed` maps each node in both to
    the fields that differ, of `kind`, `type`, `params`, `config`, `checks`,
    `tags`, `version` and `marks` (a run's output or override). `unversioned` names
    each function node in both that differs in nothing else, its version aside,
    and that a side has no version for, or a `value()` without a
    `value_version`: a change there can't be seen.
    `graph_version_changed` is whether both documents have a `graph_version`
    and they differ. `warnings` says, for each document with no
    `graph_version`, which nodes leave it blind (Q96), and when the graph
    versions differ though no node entry does. The documents' own `config`
    and `request` are not compared, only their nodes. `ValueError` for a
    document of another format, or one naming a node twice.
    """
    for side, document in (("old", old), ("new", new)):
        if not isinstance(document, dict) or document.get("format") != FORMAT:
            msg = f"{side}: not an fnloom export ({FORMAT})"
            raise ValueError(msg)
        nodes = document.get("nodes")
        if not isinstance(nodes, list) or not all(
            isinstance(node, dict) and isinstance(node.get("name"), str)
            for node in nodes
        ):
            msg = f"{side}: an fnloom export's nodes must be a list of named entries"
            raise ValueError(msg)
        names = [node["name"] for node in nodes]
        if twice := sorted({n for n in names if names.count(n) > 1}):
            msg = f"{side}: nodes named twice: {', '.join(map(repr, twice))}"
            raise ValueError(msg)
    before = {node["name"]: node for node in old["nodes"]}
    after = {node["name"]: node for node in new["nodes"]}
    changed: dict[str, list[str]] = {}
    unversioned: list[str] = []
    for name in sorted(before.keys() & after.keys()):
        a, b = before[name], after[name]
        fields = [f for f in _COMPARED if a.get(f) != b.get(f)]
        blind = _is_blind(a) or _is_blind(b)
        if blind and fields in ([], ["version"]):
            unversioned.append(name)
        elif fields:
            changed[name] = fields
    blind = [
        f"{side} has no graph_version: a change inside an unversioned node "
        f"({', '.join(_unversioned(document))}) can't be seen"
        for side, document in (("old", old), ("new", new))
        if document.get("graph_version") is None
    ]
    versions = (old.get("graph_version"), new.get("graph_version"))
    moved = None not in versions and versions[0] != versions[1]
    added = sorted(after.keys() - before.keys())
    removed = sorted(before.keys() - after.keys())
    if moved and not (added or removed or changed):
        blind.append(
            "the graph_versions differ, though no node's exported entry does: "
            "a change the entries don't show"
        )
    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "unversioned": unversioned,
        "graph_version_changed": moved,
        "warnings": blind,
    }


def _is_blind(node: Mapping[str, Any]) -> bool:
    """Whether a change in `node` could hide: no code version, or no value version."""
    if node.get("kind") != "function":
        return False
    if node.get("version") is None:
        return True
    return any(
        param.get("value") is not None and param.get("value_version") is None
        for param in node.get("params") or ()
    )


def _unversioned(document: Mapping[str, Any]) -> list[str]:
    """The nodes of `document` that a change could hide in, by name."""
    return [str(node["name"]) for node in document["nodes"] if _is_blind(node)]
