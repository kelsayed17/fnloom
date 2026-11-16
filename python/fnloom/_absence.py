"""Typed absence: a node that may decline, and how each consumer reads it (Q135).

A node whose return annotation admits `fnloom.Absent` may return `fnloom.ABSENT`.
Each parameter reading a node that may be absent decides by its own signature:
annotated to admit `Absent`, it receives `ABSENT`; with a default, it is left out
and the default applies; otherwise its node is skipped, and absent too. A node
`@absent_on` or `Builder.with_absent_on` covers is absent when its call raises
one of the errors listed (Q133).
"""

import functools
import operator
import types
import typing
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal, Union

from fnloom._core import ABSENT, Absent
from fnloom._decorators import Source, checked_errors, decorations
from fnloom._tags import TagValue, checked_query, tag_matches
from fnloom._types import Unresolved, stripped

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from collections.abc import Set as AbstractSet

    from fnloom._scan import NodeDef
    from fnloom._tags import _Any
    from fnloom._view import Param

__all__ = [
    "ABSENT",
    "Absent",
    "AbsentOn",
    "Kind",
    "admits_absent",
    "applied",
    "checked_absent_on",
    "covered",
    "declines",
    "given_problems",
    "may_be_absent",
    "merged",
    "needs_input",
    "own",
    "refuse_given",
    "refuse_mixed",
    "refuse_unadmitted",
    "without_absent",
]

type Kind = Literal["required", "optional", "admits", "admits_optional"]

# One `with_absent_on` call: its errors, and its tag query, `None` for every node.
type AbsentOn = tuple[
    tuple[type[Exception], ...],
    tuple[tuple[str, str | tuple[str, ...] | _Any], ...] | None,
]

# The kinds whose consumer needs an input supplied: they have no default.
_NEEDS_INPUT: frozenset[str] = frozenset({"required", "admits"})


def _members(annotation: object) -> list[object]:
    """`annotation`'s top-level parts: a union's members, through aliases, flattened.

    `Annotated` metadata and PEP 695 aliases are read through, at every level of
    the union; a generic's arguments are never read. Iterative, so an alias naming
    a union of aliases is read without recursion; one met twice is read once.
    """
    found: list[object] = []
    seen: list[object] = []
    work = [annotation]
    while work:
        current = stripped(work.pop())
        if typing.get_origin(current) in {Union, types.UnionType}:
            for member in reversed(typing.get_args(current)):
                if member not in seen:
                    seen.append(member)
                    work.append(member)
        else:
            found.append(current)
    return found


def admits_absent(annotation: object) -> bool:
    """Whether `annotation` admits `Absent` at its top level.

    True for `Absent` itself, or a union holding it, seen through `Annotated` and
    PEP 695 aliases. A generic's arguments are not read: `list[int | Absent]`
    admits a list, not an absence.
    """
    if isinstance(annotation, type):
        # The common case, and no union: a class is `Absent` or not.
        return annotation is Absent
    return any(member is Absent for member in _members(annotation))


def without_absent(annotation: object) -> object:
    """`annotation` with `Absent` taken out of its top level, for an edge check.

    `float | Absent` is `float`, and `float | int | Absent` is `float | int`. An
    annotation that does not admit `Absent` is returned as it is. `Absent` alone
    leaves nothing, and is `Any`, no constraint: such a node never gives a value,
    so a consumer that does not admit `Absent` is skipped, never fed.
    """
    if not admits_absent(annotation):
        return annotation
    rest = [member for member in _members(annotation) if member is not Absent]
    if not rest:
        return Any
    if len(rest) == 1:
        return rest[0]
    return functools.reduce(operator.or_, rest)


def declines(node: NodeDef) -> bool:
    """Whether `node` may return `ABSENT`: its return annotation admits `Absent`."""
    return admits_absent(node.returns)


# The attribute that marks an exception fnloom raised for a check of its own.
_OWN = "__fnloom_own__"


def own[E: BaseException](error: E) -> E:
    """`error`, marked as raised by a check of fnloom's, which no `absent_on` matches.

    An item check, an output check or a field extraction that fails is a fault the
    absence rules can't stand in for, so it fails the run whatever is listed (Q133).
    """
    setattr(error, _OWN, True)
    return error


def checked_absent_on(
    errors: tuple[object, ...], tags: Mapping[str, object] | None
) -> AbsentOn:
    """One `with_absent_on` call, checked: its errors as `absent_on`'s, its tag query.

    `tags` is a query in `NodeViews.tagged`'s form (Q141), each value checked as
    `tagged` checks it; `None` covers every node. `TypeError` naming the culprit.
    """
    checked = checked_errors(errors, "with_absent_on")
    if tags is None:
        return checked, None
    if not isinstance(tags, Mapping):
        msg = f"with_absent_on tags must be a mapping of tag queries, not {tags!r}"
        raise TypeError(msg)
    query = tuple(
        (key, checked_query(key, value))
        for key, value in sorted(tags.items(), key=lambda item: str(item[0]))
    )
    return checked, query


def covered(
    nodes: Sequence[NodeDef],
    rules: Sequence[AbsentOn],
    others: AbstractSet[str],
    tags: Mapping[str, Mapping[str, TagValue]] | None = None,
) -> dict[str, tuple[type[Exception], ...]]:
    """The errors each node is absent on, by node: its `@absent_on` and `rules`' union.

    A node in `others`, such as an extracted field or a `@pipe_output` relay, takes
    no `@absent_on` of its function: the node calling the function does, and what
    reads an absent node is absent too. A rule covers each node its tag query
    matches, those included, reading a node's tags from `tags` when it is there:
    a `@pipe_output` function's `.raw` node, which keeps none of its own.
    """
    found: dict[str, tuple[type[Exception], ...]] = {}
    given = tags or {}
    for node in nodes:
        errors = () if node.name in others else decorations(node.origin).absent_on
        held = given.get(node.name, node.tags)
        for listed, query in rules:
            if query is None or all(
                tag_matches(held, key, value) for key, value in query
            ):
                errors = (*errors, *listed)
        if errors:
            found[node.name] = tuple(dict.fromkeys(errors))
    return found


def refuse_unadmitted(nodes: Sequence[NodeDef], others: AbstractSet[str]) -> None:
    """`ValueError` for each `@absent_on` node whose return annotation lacks `Absent`.

    The type then says what can happen (O11). Checked at build, once annotations
    resolve: an annotation that did not (Q81) is refused too, since whether it
    admits `Absent` can't be told. A node covered only by `with_absent_on` needs
    no such annotation, and nor does a node in `others`, as for `covered`. Every
    culprit is listed, sorted.
    """
    lines: list[str] = []
    for node in nodes:
        if node.name in others or not decorations(node.origin).absent_on:
            continue
        if admits_absent(node.returns):
            continue
        if isinstance(node.returns, Unresolved):
            lines.append(
                f"node {node.name!r} is @absent_on, but its return annotation "
                f"{node.returns.name!r} did not resolve, so fnloom can't tell whether "
                "it admits fnloom.Absent; import what it names at run time"
            )
        else:
            lines.append(
                f"node {node.name!r} is @absent_on, so it may be absent, but its "
                "return annotation does not admit fnloom.Absent; add `| Absent`"
            )
    if lines:
        raise ValueError("\n".join(sorted(lines)))


def merged(params: Sequence[Param]) -> Kind:
    """The one kind of a source read by every parameter of `params`.

    When each of them admits `Absent`, it is `admits`, or `admits_optional` when
    each also has a default. Otherwise it is `required` when any lacks a default,
    and `optional` when all have one. Parameters that disagree on admitting
    `Absent` can only share a source that can't be absent: `refuse_mixed`.
    """
    if all(param.admits_absent for param in params):
        return (
            "admits" if any(param.required for param in params) else "admits_optional"
        )
    return "required" if any(param.required for param in params) else "optional"


def needs_input(kind: Kind) -> bool:
    """Whether a consumer reading an input by `kind` needs it supplied."""
    return kind in _NEEDS_INPUT


def _sources(params: Iterable[Param]) -> dict[str, list[Param]]:
    """The parameters bound to each source, by source, in first-appearance order."""
    by_source: dict[str, list[Param]] = {}
    for param in params:
        if isinstance(param.binding, Source):
            by_source.setdefault(param.binding.name, []).append(param)
    return by_source


def may_be_absent(
    nodes: Sequence[NodeDef], errors: AbstractSet[str] = frozenset()
) -> frozenset[str]:
    """The nodes whose value may be absent: they decline, or read one by `required`.

    A node in `errors`, absent on an error, counts as one that declines. The same
    set the core computes at build (`Graph.may_be_absent`), known here before the
    graph is, for the views. A reachability, found with a worklist.
    """
    declining = [node.name for node in nodes if declines(node) or node.name in errors]
    if not declining:
        return frozenset[str]()
    defined = {node.name for node in nodes}
    required_by: dict[str, list[str]] = {}
    for node in nodes:
        for source, params in _sources(node.params).items():
            if source in defined and merged(params) == "required":
                required_by.setdefault(source, []).append(node.name)
    found = set(declining)
    work = list(declining)
    while work:
        for consumer in required_by.get(work.pop(), ()):
            if consumer not in found:
                found.add(consumer)
                work.append(consumer)
    return frozenset(found)


def _shown(params: Iterable[Param]) -> str:
    """The parameters' names, quoted and joined, a group item with its slot."""
    return ", ".join(
        repr(param.name) if param.slot is None else f"'{param.name}[{param.slot!r}]'"
        for param in params
    )


def refuse_mixed(nodes: Sequence[NodeDef], absent: AbstractSet[str]) -> None:
    """`ValueError` for each source that may be absent and that parameters disagree on.

    One source read by several parameters of a node has one kind (`merged`); when
    some admit `Absent` and others don't, no kind gives each what it says, unless
    the source can't be absent. Every culprit is listed, sorted.
    """
    if not absent:
        return
    lines: list[str] = []
    for node in nodes:
        for source, params in _sources(node.params).items():
            if source not in absent or len(params) < 2:  # noqa: PLR2004  # a pair can disagree
                continue
            admitting = [param for param in params if param.admits_absent]
            others = [param for param in params if not param.admits_absent]
            if admitting and others:
                lines.append(
                    f"mixed absence handling: {node.name!r} reads {source!r}, which "
                    "may be absent, through parameters that admit fnloom.Absent "
                    f"({_shown(admitting)}) and parameters that do not "
                    f"({_shown(others)}); annotate them alike, or read it once"
                )
    if lines:
        raise ValueError("; ".join(sorted(lines)))


def applied(
    kwargs: dict[str, object],
    values: Mapping[str, object],
    watched: Sequence[tuple[str, str, Kind]],
) -> bool:
    """Applies the three rules to a call's `kwargs`; `False` when the node is skipped.

    `watched` are the node's sources that may be absent, each with the keyword
    it is passed under and the kind it is read by, as the async driver's walk
    reads them from `values`. A source that is `ABSENT` is kept for `admits` and
    `admits_optional`, left out for `optional`, so the default applies, and
    skips the node for `required`, as the core's run does (Q135).
    """
    for source, keyword, kind in watched:
        if values[source] is not ABSENT:
            continue
        if kind == "required":
            return False
        if kind == "optional":
            del kwargs[keyword]
    return True


def refuse_given(
    inputs: Mapping[str, object],
    overrides: Mapping[str, object],
    may_be_absent: AbstractSet[str],
) -> None:
    """`TypeError` for each `ABSENT` input, and each `ABSENT` override not allowed.

    Every culprit is listed, sorted; see `given_problems`.
    """
    if lines := given_problems(inputs, overrides, may_be_absent):
        raise TypeError("; ".join(lines))


def given_problems(
    inputs: Mapping[str, object],
    overrides: Mapping[str, object],
    may_be_absent: AbstractSet[str],
) -> list[str]:
    """A line for each `ABSENT` input, and each `ABSENT` override not allowed, sorted.

    An input is never absent: leaving it out is how a run says so. An override
    may be `ABSENT` for a node in `may_be_absent`, which forces its skip, and for
    no other. Checked whatever the type checks say, an `Any`-typed node too.
    """
    lines = [
        f"input {name!r} is fnloom.ABSENT, which an input can't be; leave it out "
        "instead"
        for name, given in inputs.items()
        if given is ABSENT
    ]
    lines += [
        f"override {name!r} is fnloom.ABSENT, but node {name!r} can't be absent: "
        "its return annotation does not admit fnloom.Absent, and it reads no node "
        "that may be; override it with a value"
        for name, given in overrides.items()
        if given is ABSENT and name not in may_be_absent
    ]
    return sorted(lines)
