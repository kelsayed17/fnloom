"""Exports: a graph, or one run of it, as a JSON-ready document, and their diff.

A document says what each node is: its type, what each parameter reads, its tags,
config conditions and source, and its code version (Q95, Q96). It never holds a
value: a request's inputs and overrides are listed by name only, config values and
`value()` bindings as `repr`.
"""

import hashlib
import inspect
import json
import re
from typing import TYPE_CHECKING, Any

from fnloom._codeversion import UncacheableError, code_version
from fnloom._decorators import Source, Value, decorations
from fnloom._drawing import condition_text
from fnloom._fingerprint import fingerprint
from fnloom._types import Unresolved

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from fnloom._drawing import Drawing
    from fnloom._view import InputView, NodeView, Param

FORMAT = "fnloom.export/1"

# What `diff` compares, in the order it lists them.
_COMPARED = ("kind", "type", "params", "config", "tags", "version", "marks")


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
        _node(drawn.name, sorted(drawn.marks), views[drawn.name], inputs, versions)
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
    """
    if isinstance(value, (set, frozenset)):
        members = ", ".join(sorted(stable_repr(member) for member in value))
        if type(value) is set:
            return f"{{{members}}}" if value else "set()"
        return f"{type(value).__name__}({{{members}}})" if value else "frozenset()"
    if type(value) is dict:
        pairs = (f"{stable_repr(k)}: {stable_repr(v)}" for k, v in value.items())
        return f"{{{', '.join(pairs)}}}"
    if type(value) is list:
        return f"[{', '.join(stable_repr(item) for item in value)}]"
    if type(value) is tuple:
        items = [stable_repr(item) for item in value]
        return f"({items[0]},)" if len(items) == 1 else f"({', '.join(items)})"
    return _ADDRESS.sub("", repr(value))


def _holds_a_set(value: object) -> bool:
    """Whether `value` is or holds a set, in a list, tuple or dict."""
    if isinstance(value, (set, frozenset)):
        return True
    if type(value) is dict:
        return any(_holds_a_set(k) or _holds_a_set(v) for k, v in value.items())
    if type(value) is list or type(value) is tuple:
        return any(_holds_a_set(item) for item in value)
    return False


def type_text(annotation: object) -> str:
    """`annotation` as text, whole: an unresolved name as written (Q81)."""
    if isinstance(annotation, Unresolved):
        return annotation.name
    return inspect.formatannotation(annotation)


def _node(
    name: str,
    marks: list[str],
    view: NodeView,
    inputs: Mapping[str, InputView],
    versions: dict[int, tuple[str | None, str | None]],
) -> dict[str, Any]:
    """One node's entry; an input's has no source or version (Q95)."""
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
        key = id(origin)
        if key not in versions:
            versions[key] = _version(origin)
        entry["version"], unversioned = versions[key]
        if unversioned is not None:
            entry["unversioned"] = unversioned
        if (
            policy := decorations(origin).cache
        ) is not None and policy.version is not None:
            entry["pinned"] = policy.version
    return entry


def _param(param: Param) -> dict[str, Any]:
    binding = param.binding
    return {
        "name": param.name,
        "slot": param.slot,
        "reads": binding.name if isinstance(binding, Source) else None,
        "value": stable_repr(binding.value) if isinstance(binding, Value) else None,
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
    `value()` is hashed by its fingerprint, or by its sorted text when it holds a
    set, whose fingerprint follows the hash seed. `None` when a node is
    unversioned, or a `value()` it is bound to has no fingerprint: a change there
    could not be seen (Q96).
    """
    parts: list[object] = []
    for node in nodes:
        if node["kind"] == "function" and node["version"] is None:
            return None
        bound: list[object] = []
        for param in views[node["name"]].params:
            binding = param.binding
            if isinstance(binding, Source):
                bound.append([param.name, param.slot, binding.name, param.required])
                continue
            if _holds_a_set(binding.value):
                # A set's fingerprint follows the hash seed; its sorted text doesn't.
                if _ADDRESS.search(repr(binding.value)):
                    return None  # a member known only by its address
                value = stable_repr(binding.value)
            else:
                try:
                    value = fingerprint(binding.value).hex()
                except Exception:  # noqa: BLE001  # unfingerprintable: no graph version
                    return None
            bound.append([param.name, param.slot, None, value])
        named = [node[key] for key in ("name", "kind", "type")]
        parts.append([*named, bound, node["config"], node["version"]])
    text = json.dumps([FORMAT, parts], sort_keys=True)
    return hashlib.sha256(text.encode()).hexdigest()


def diff(old: Mapping[str, Any], new: Mapping[str, Any]) -> dict[str, Any]:
    """What changed between two documents, node by node.

    `added` and `removed` are names, sorted; `changed` maps each node in both to
    the fields that differ, of `kind`, `type`, `params`, `config`, `tags`,
    `version` and `marks` (a run's output or override). `unversioned` names
    each function node in both that a side has no version for and that differs
    in nothing else, its version aside: a change in its code can't be seen.
    `warnings` says, for each document with no `graph_version`, which nodes
    leave it blind (Q96). The documents' own `config` and `request` are not
    compared, only their nodes. `ValueError` for a document of another format,
    or one naming a node twice.
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
        functions = a.get("kind") == b.get("kind") == "function"
        blind = functions and (a.get("version") is None or b.get("version") is None)
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
    return {
        "added": sorted(after.keys() - before.keys()),
        "removed": sorted(before.keys() - after.keys()),
        "changed": changed,
        "unversioned": unversioned,
        "warnings": blind,
    }


def _unversioned(document: Mapping[str, Any]) -> list[str]:
    """The function nodes of `document` that have no version, or `["a value()"]`."""
    names: list[str] = [
        str(node["name"])
        for node in document["nodes"]
        if node.get("kind") == "function" and node.get("version") is None
    ]
    if not names:
        return ["a value() with no fingerprint"]
    return names
