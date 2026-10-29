"""Exports: a graph, or one run of it, as a JSON-ready document, and their diff.

A document says what each node is: its type, what each parameter reads, its tags,
config conditions and source, and its code version (Q95, Q96). It never holds a
value: a request's inputs and overrides are listed by name only, config values and
`value()` bindings as `repr`.
"""

import hashlib
import inspect
import json
from typing import TYPE_CHECKING, Any

from fnloom._codeversion import UncacheableError, code_version
from fnloom._decorators import Source, Value
from fnloom._drawing import condition_text
from fnloom._fingerprint import fingerprint
from fnloom._types import Unresolved

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from fnloom._drawing import Drawing
    from fnloom._view import InputView, NodeView, Param

FORMAT = "fnloom.export/1"

# What `diff` compares, in the order it lists them.
_COMPARED = ("kind", "type", "params", "config", "tags", "version")


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
        "config": {key: repr(config[key]) for key in sorted(config)},
        "request": None if request is None else dict(request),
        "nodes": nodes,
    }


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
    return entry


def _param(param: Param) -> dict[str, Any]:
    binding = param.binding
    return {
        "name": param.name,
        "slot": param.slot,
        "reads": binding.name if isinstance(binding, Source) else None,
        "value": repr(binding.value) if isinstance(binding, Value) else None,
        "required": param.required,
        "type": type_text(param.annotation),
    }


def _source(fn: Callable[..., object]) -> str | None:
    try:
        return inspect.getsource(fn)
    except OSError, TypeError:
        return None


def _version(fn: Callable[..., object]) -> tuple[str | None, str | None]:
    """`fn`'s code version in hex, or `None` and why it has none (Q96)."""
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

    `None` when a node is unversioned, or a `value()` it is bound to has no
    fingerprint: a change there could not be seen (Q96).
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
    the fields that differ, of `kind`, `type`, `params`, `config`, `tags` and
    `version`. `unversioned` names each node in both whose change can't be seen
    because a side has no version and nothing else differs. `ValueError` for a
    document of another format.
    """
    for side, document in (("old", old), ("new", new)):
        if document.get("format") != FORMAT:
            msg = f"{side}: not an fnloom export ({FORMAT})"
            raise ValueError(msg)
    before = {node["name"]: node for node in old["nodes"]}
    after = {node["name"]: node for node in new["nodes"]}
    changed: dict[str, list[str]] = {}
    unversioned: list[str] = []
    for name in sorted(before.keys() & after.keys()):
        a, b = before[name], after[name]
        fields = [f for f in _COMPARED if a.get(f) != b.get(f)]
        functions = a["kind"] == b["kind"] == "function"
        blind = functions and (a["version"] is None or b["version"] is None)
        if blind and fields in ([], ["version"]):
            unversioned.append(name)
        elif fields:
            changed[name] = fields
    return {
        "added": sorted(after.keys() - before.keys()),
        "removed": sorted(before.keys() - after.keys()),
        "changed": changed,
        "unversioned": unversioned,
    }
