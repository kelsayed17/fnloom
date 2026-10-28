"""The build's checks of parameter defaults: shadowed and differing ones (Q76)."""

import annotationlib
import inspect
from typing import TYPE_CHECKING

from fnloom._decorators import Source, decorations

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from fnloom._scan import NodeDef

# Longest repr of a default in a message.
_SHOWN = 40


def default_problems(nodes: Sequence[NodeDef]) -> list[str]:
    """A line for each shadowed default and each input with differing defaults.

    A parameter with a default that reads a node's name never uses the default,
    since the node is computed (`shadowed`); an input that two or more consumers
    give different defaults is a different value for each (`differing`).
    `@defaults` on the consumer opts a parameter out of either. Sorted.

    A consumer that opts out of `differing` is left out of the comparison, so
    opting out every consumer but one settles it: the rest then agree.
    """
    computed = {node.name for node in nodes}
    shadowed: list[str] = []
    # Per input: the consumers (node, parameter, its default) that did not opt out.
    readers: dict[str, list[tuple[str, str, object]]] = {}
    defaults_of: dict[int, dict[str, object]] = {}
    for node in nodes:
        recorded = decorations(node.origin).defaults
        for param in node.params:
            if param.required or not isinstance(param.binding, Source):
                continue
            read = param.binding.name
            if read in computed:
                if recorded is None or param.name not in recorded.shadowed:
                    shadowed.append(_shadowed(node.name, param.name, read))
            elif recorded is None or param.name not in recorded.differing:
                found = defaults_of.get(id(node.origin))
                if found is None:
                    found = defaults_of[id(node.origin)] = _defaults(node.origin)
                readers.setdefault(read, []).append(
                    (node.name, param.name, found[param.name])
                )
    lines = shadowed
    for name, group in readers.items():
        line = _differing(name, group)
        if line is not None:
            lines.append(line)
    return sorted(lines)


def _shadowed(consumer: str, parameter: str, node: str) -> str:
    """The line for `parameter` of `consumer`, whose default `node` replaces."""
    return (
        f"shadowed default: parameter {parameter!r} of {consumer!r} has a default, "
        f"but it reads the node {node!r}, which replaces it; rename one, or let the "
        f"node win with @fnloom.defaults(shadowed=({parameter!r},)) on {consumer!r}"
    )


def _differing(name: str, group: list[tuple[str, str, object]]) -> str | None:
    """The line for input `name` when its consumers' `group` of defaults disagree."""
    first = group[0][2]
    if all(_same(first, default) for _, _, default in group[1:]):
        return None
    group.sort(key=lambda reader: (reader[0], reader[1]))
    consumers = ", ".join(
        f"{node!r} (parameter {parameter!r}) = {_shown(default)}"
        for node, parameter, default in group
    )
    return (
        f"differing defaults: input {name!r} has different defaults in {consumers}; "
        "make them equal, or write @fnloom.defaults(differing=(<parameter>,)) on each "
        "of these consumers but one"
    )


def _same(left: object, right: object) -> bool:
    """Whether two defaults are the same; an unclear comparison says no.

    The same object is equal to itself, even a numpy array. Otherwise `==` must
    return a `bool` and not raise: an array's elementwise result, or an object
    that cannot be compared, is not a clear yes.
    """
    if left is right:
        return True
    try:
        result = left == right
    except Exception:  # noqa: BLE001  # any failure of a user's `__eq__` is "unclear"
        return False
    return result is True


def _shown(default: object) -> str:
    """`repr(default)` cut short; its type's name when its `repr` raises."""
    try:
        text = repr(default)
    except Exception:  # noqa: BLE001  # any failure of a user's `__repr__`
        return f"<{type(default).__name__}>"
    return text if len(text) <= _SHOWN else text[: _SHOWN - 3] + "..."


def _defaults(fn: Callable[..., object]) -> dict[str, object]:
    """The default of each of `fn`'s parameters that has one."""
    signature = inspect.signature(fn, annotation_format=annotationlib.Format.FORWARDREF)
    return {
        name: parameter.default
        for name, parameter in signature.parameters.items()
        if parameter.default is not inspect.Parameter.empty
    }
