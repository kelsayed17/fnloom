"""Normalising a request's `final_vars` to node names, and naming close names."""

import difflib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from fnloom._view import NodeViews


def output_names(
    final_vars: Sequence[str | Callable[..., object]], views: NodeViews
) -> tuple[str, ...]:
    """The node names `final_vars` asks for: a `str` is a name, a function its node.

    A function means the node it produces in this driver, matched by identity with
    the node's origin, never by name (Q30): a plain function is its node, and an
    `@extract_*` function is its producer, not its fields. When a function makes
    several nodes, the one named like it is its node, as for an alias, unless a
    namespace holds one of them (Q74); otherwise it is an error. Every culprit of
    every kind is listed once, sorted, in one message:

    - `TypeError` for a bare `str` (Q6), and when any culprit is malformed: an item
      that is neither a `str` nor a function, or a function that makes several nodes,
      such as a `@parameterize` one, which lists its rows;
    - `ValueError` otherwise: a function no node of this driver comes from, such as
      one of another module, an unselected `@config` variant, or a lambda.

    A name is not checked against the graph here: compiling the plan does.
    """
    names, absent = requested(final_vars, views)
    if absent:
        raise ValueError(ABSENT + "; ".join(absent))
    return names


# How the error for functions no node comes from starts.
ABSENT = "final_vars: "


def requested(
    final_vars: Sequence[str | Callable[..., object]], views: NodeViews
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The names `final_vars` resolves to, and each function no node comes from.

    `output_names` without its `ValueError`: the problem of each function no node
    comes from is returned, once each, sorted, for `Driver.validate` to list (Q71).
    The `TypeError`s of `output_names` are raised as it raises them.
    """
    if isinstance(final_vars, str):
        msg = "final_vars must be a sequence of names, not a single str"
        raise TypeError(msg)
    # Copied before checking, so a one-shot iterator is read once and not exhausted.
    items = tuple(final_vars)
    if all(isinstance(item, str) for item in items):
        return items, ()  # ty: ignore[invalid-return-type]  # every item was just checked
    names: list[str] = []
    # Each culprit once, whatever its kind; `malformed` makes the error a `TypeError`.
    culprits: set[str] = set()
    malformed = False
    for item in items:
        kind, text = _read(item, views)
        if kind == "name":
            names.append(text)
            continue
        culprits.add(text)
        malformed = malformed or kind != "absent"
    if malformed:
        msg = "final_vars items must be str or a function: "
        raise TypeError(msg + "; ".join(sorted(culprits)))
    return tuple(names), tuple(sorted(culprits))


def _read(item: object, views: NodeViews) -> tuple[str, str]:
    """One item as `(kind, text)`: a node name, or the kind and text of its problem."""
    if isinstance(item, str):
        return "name", item
    if not callable(item):
        return "foreign", f"{type(item).__name__} {item!r}"
    owned = views.owners.get(id(item), ())
    if len(owned) > 1:
        # An alias, `alias = base`, or a row named like the function: that one node.
        # Not when a namespace has a copy too: the plain node is then one of several.
        named = [name for name in owned if name == getattr(item, "__name__", None)]
        functions = views.functions
        if len(named) == 1 and all(functions[n].namespace is None for n in owned):
            return "name", named[0]
        return "several", _several(item, owned)
    if owned:
        return "name", owned[0]
    return "absent", f"{_described(item)} {_why_absent(item, views)}"


def _several(fn: Callable[..., object], owned: tuple[str, ...]) -> str:
    """The problem of a function that makes several nodes: which, and what to pass."""
    return (
        f"{_described(fn)} makes {len(owned)} nodes: pass "
        f"{', '.join(map(repr, owned))} instead"
    )


def _described(fn: Callable[..., object]) -> str:
    """`fn` as an error names it: its module and qualified name."""
    module = getattr(fn, "__module__", None)
    name = getattr(fn, "__qualname__", None) or repr(fn)
    return f"function {module}.{name}" if module else f"function {name}"


def _why_absent(fn: Callable[..., object], views: NodeViews) -> str:
    """Why no node of the driver comes from `fn`."""
    name = getattr(fn, "__name__", None)
    if name == "<lambda>":
        return "is a lambda, which no node comes from: pass its node's name"
    if isinstance(name, str) and name in views.functions:
        return (
            f"is not the function behind node {name!r}, which another function "
            "made: pass the name, or the function the driver was built from"
        )
    return (
        "is not the function of any node of this driver (a function of another "
        "module, an unselected @config variant, or one the driver was not built "
        "from): pass a node name"
    )


def suggest(name: str, candidates: Iterable[str]) -> str:
    """`" (did you mean 'x'?)"` for the closest candidate to `name`, or `""` (Q71).

    The closest is `difflib.get_close_matches` at cutoff 0.8; a tie goes to the
    greater name, whatever the candidates' order. A name never suggests itself.
    Only error paths call this, so a run pays nothing for it.
    """
    others = [candidate for candidate in candidates if candidate != name]
    found = difflib.get_close_matches(name, others, n=1, cutoff=0.8)
    return f" (did you mean '{found[0]}'?)" if found else ""
