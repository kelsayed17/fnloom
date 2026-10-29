"""Fingerprints that hold in every process, for a value holding a set.

A set's fingerprint follows the hash seed: its members are walked in the order the
seed gives. `stable_fingerprint` versions such a value from a canonical form
instead, so a `value()` binding holding one keys the cache, and versions an export,
the same on every `PYTHONHASHSEED`.
"""

import dataclasses
import hashlib
import json
from collections.abc import Mapping

from fnloom._codeversion import class_version
from fnloom._fingerprint import fingerprint

# How deep a canonical form, or an export's `stable_repr`, looks into a value: past
# it, a value has no form, rather than exhausting the stack.
DEPTH = 100

# What a canonical form's hash is framed with, so it can't pass for a fingerprint.
_FORMAT = "fnloom.canonical/1"


class UnwritableError(Exception):
    """A value too deep, or holding itself, for a canonical form."""

    def __init__(self) -> None:
        super().__init__("a value holding itself, or nested past 100 levels")


def stable_fingerprint(value: object) -> bytes:
    """`value`'s 32-byte fingerprint, the same on every hash seed.

    Its `fingerprint` when it holds no set. Otherwise a hash of its canonical
    form: each part with no set in it by its own fingerprint, each set as its
    members' forms sorted, and each container by its type and parts, a
    dataclass by its class version and every field. Nothing is read from text,
    so a change a `repr` elides is still seen. Raises as `fingerprint` does,
    and `UnwritableError` for a value holding itself or nested past 100 levels.
    """
    if not _holds_a_set(value):
        return fingerprint(value)
    text = json.dumps([_FORMAT, _canonical(value, frozenset(), 0)])
    return hashlib.sha256(text.encode()).digest()


def parts(value: object) -> list[object]:
    """The parts `value` is written and versioned from, or `[]` for a whole value.

    A dataclass instance's parts are all its fields, `repr=False` ones included:
    they hold state as much as the others do.
    """
    if isinstance(value, (set, frozenset, list, tuple)):
        return list(value)
    if isinstance(value, Mapping):
        return [part for pair in value.items() for part in pair]
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return [getattr(value, f.name) for f in dataclasses.fields(value)]
    return []


def _holds_a_set(value: object) -> bool:
    """Whether `value` is or holds a set, anywhere `parts` reaches."""
    waiting, seen = [value], set[int]()
    while waiting:
        current = waiting.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, (set, frozenset)):
            return True
        waiting.extend(parts(current))
    return False


def _canonical(value: object, seen: frozenset[int], depth: int) -> object:
    """`value` as JSON-ready parts, the same in every process; raises if it can't be."""
    if depth > DEPTH or id(value) in seen:
        raise UnwritableError
    if not _holds_a_set(value):
        return ["fingerprint", fingerprint(value).hex()]
    inner = seen | {id(value)}
    found = [_canonical(part, inner, depth + 1) for part in parts(value)]
    kind = _type_version(type(value))
    if isinstance(value, (set, frozenset)):
        return ["set", kind, sorted(json.dumps(part) for part in found)]
    return ["parts", kind, found]


def _type_version(cls: type) -> str:
    """A builtin type by name; any other by its class version, as the cache has it."""
    if cls.__module__ == "builtins":
        return cls.__qualname__
    return class_version(cls).hex()
