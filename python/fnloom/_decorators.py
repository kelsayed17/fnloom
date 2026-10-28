"""Decorators that attach metadata to a node function and return the function itself."""

import annotationlib
import inspect
import types
import typing
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Literal, overload

from fnloom._stores import Format

if TYPE_CHECKING:
    from fnloom._config import ConfigPolicy
    from fnloom._extract import Extraction

_ATTRIBUTE = "__fnloom__"

# How a node uses the result cache: see `cache`.
type CacheBehavior = Literal["default", "recompute", "disable"]


@dataclass(frozen=True, slots=True)
class Source:
    """A row binding: read the parameter from the upstream node or input `name`."""

    name: str


@dataclass(frozen=True, slots=True)
class Value:
    """A row binding: pass `value` itself, the same object on every call."""

    value: object


@dataclass(frozen=True, slots=True)
class Group:
    """A row binding: assemble a list or a dict from several sources and values.

    Made by `group`. Exactly one of `positional` (a list-valued parameter) and
    `named` (a dict-valued one, key and item in the order written) is non-empty.
    """

    positional: tuple[Source | Value, ...] = ()
    named: tuple[tuple[str, Source | Value], ...] = ()

    @property
    def is_list(self) -> bool:
        """Whether the parameter is a list: the group was written positionally."""
        return bool(self.positional)

    @property
    def slots(self) -> tuple[tuple[int | str, Source | Value], ...]:
        """Each item with its place in the result: an index, or a key."""
        if self.positional:
            return tuple(enumerate(self.positional))
        return self.named


# What a row may bind a parameter to.
type Binding = Source | Value | Group


@dataclass(frozen=True, slots=True)
class CachePolicy:
    """What `@cache` recorded: how the node uses the result cache.

    Attributes:
        behavior: `"default"`, `"recompute"` or `"disable"`.
        ignore: Parameters left out of this node's cache key.
        version: The user's own code version, or None for the automatic one.
        format: How a `DiskCache` stores the result: a `Format`, a format's name,
            or None for the store's default. Only a `default` node is stored, so
            on another behavior it is checked at build and otherwise unused.
    """

    behavior: CacheBehavior
    ignore: frozenset[str]
    version: str | None
    format: Format | str | None = None


@dataclass(frozen=True, slots=True)
class DefaultsPolicy:
    """What `@defaults` recorded: the parameters whose default the build may not refuse.

    Attributes:
        shadowed: Parameters with a default that read a node's name, where the
            node is meant to win.
        differing: Parameters whose default is meant to differ from another
            consumer's default for the same input.
    """

    shadowed: frozenset[str]
    differing: frozenset[str]


@dataclass(frozen=True)
class Decorations:
    """What the decorators recorded on one function.

    Its rows, tags, cache policy, config conditions, extraction, defaults, and whether
    `@exclude` marked it.
    """

    rows: Mapping[str, Mapping[str, Binding]] | None
    tags: Mapping[str, str]
    cache: CachePolicy | None = None
    config: ConfigPolicy | None = None
    extract: Extraction | None = None
    defaults: DefaultsPolicy | None = None
    excluded: bool = False
    docs: Mapping[str, str | None] = MappingProxyType[str, str | None]({})


def source(name: str) -> Source:
    """Binds a row's parameter to the upstream node or input `name`."""
    return Source(name)


def value(v: object) -> Value:
    """Binds a row's parameter to the constant `v`."""
    return Value(v)


def decorations(fn: Callable[..., object]) -> Decorations:
    """Returns what the decorators recorded on `fn`; empty for a plain function."""
    recorded = getattr(fn, _ATTRIBUTE, None)
    if isinstance(recorded, Decorations):
        return recorded
    return _UNDECORATED


# What a plain function records: no rows, tags or cache policy. One object, shared.
_UNDECORATED = Decorations(None, MappingProxyType[str, str]({}))


def too_many_rows(rows: Mapping[str, object], decorator: str) -> str:
    """Why `decorator`, an `extract_*`, refuses these rows: it lowers one node."""
    listed = ", ".join(repr(name) for name in sorted(rows))
    return f"{decorator} needs one node, but parameterize makes {len(rows)}: {listed}"


def group(*positional: Source | Value, **named: Source | Value) -> Group:
    """Binds a row's parameter to a list or a dict assembled from sources and values.

    `group(source("a"), value(3))` is the list `[a, 3]`, for a parameter annotated
    `list[X]`; `group(k=source("a"))` is the dict `{"k": a}`, for `dict[str, X]`.
    Each call builds the list or dict fresh; a `value(...)` item is the same
    object every time. Each `source` is an ordinary input, typed `X`.

    Raises:
        TypeError: An item is not `source(...)` or `value(...)`: a nested `group`
            or a bare `str`, say. Every offender is listed once.
        ValueError: There are no items, or both positional and keyword ones.
    """
    # Typed as `object`: a caller may pass anything, whatever the hint says.
    items: list[tuple[str, object]] = [
        *((f"item {i}", item) for i, item in enumerate(positional)),
        *((f"item {key!r}", item) for key, item in named.items()),
    ]
    wrong = [
        f"{where} ({type(item).__name__})"
        for where, item in items
        if not isinstance(item, Source | Value)
    ]
    if wrong:
        msg = f"group() takes source(...) or value(...), not: {', '.join(wrong)}"
        raise TypeError(msg)
    if not items:
        msg = "group() needs at least one source(...) or value(...)"
        raise ValueError(msg)
    if positional and named:
        msg = (
            "group() takes positional items for a list or keywords for a dict, not both"
        )
        raise ValueError(msg)
    return Group(positional, tuple(named.items()))


def _group_shape(annotation: object) -> tuple[bool, object] | None:
    """`(is_list, element)` for `list[X]`, `dict[str, X]` or an Optional of one.

    `None` for any other annotation, a bare `list` and a quoted one included.
    """
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if origin is typing.Union or origin is types.UnionType:
        rest = [a for a in args if a is not type(None)]
        if len(rest) != 1 or len(args) != 2:  # noqa: PLR2004  # X and None
            return None
        return _group_shape(rest[0])
    if origin is list and len(args) == 1:
        return True, args[0]
    if origin is dict and len(args) == 2 and args[0] is str:  # noqa: PLR2004  # key, value
        return False, args[1]
    return None


def group_element(annotation: object, *, is_list: bool) -> object:
    """The element type of a group parameter's annotation; the annotation if not one."""
    shape = _group_shape(annotation)
    if shape is None or shape[0] != is_list:
        return annotation
    return shape[1]


def _group_problems(
    fn: Callable[..., object], rows: Mapping[str, Mapping[str, object]]
) -> list[str]:
    """One line per group parameter that is not annotated as its group needs.

    Each parameter and group kind is listed once, however many rows bind it.
    """
    forward = annotationlib.Format.FORWARDREF
    annotations = annotationlib.get_annotations(fn, format=forward)
    wanted = {
        (param, binding.is_list)
        for bindings in rows.values()
        for param, binding in bindings.items()
        if isinstance(binding, Group)
    }
    lines: list[str] = []
    for param, is_list in sorted(wanted):
        annotation = annotations.get(param)
        shape = _group_shape(annotation)
        if shape is not None and shape[0] == is_list:
            continue
        needs = "list[X]" if is_list else "dict[str, X]"
        found = (
            "no annotation"
            if param not in annotations
            else inspect.formatannotation(annotation)
        )
        unquoted = ", written unquoted" if isinstance(annotation, str) else ""
        lines.append(f"{param!r} ({found}) needs {needs} for its group{unquoted}")
    return lines


def group_check(
    fn: Callable[..., object], rows: Mapping[str, Mapping[str, object]]
) -> None:
    """`TypeError` naming every group parameter `fn` annotates wrongly."""
    lines = _group_problems(fn, rows)
    if lines:
        msg = f"group() parameters must be annotated as their group: {'; '.join(lines)}"
        raise TypeError(msg)


def _check_rows(rows: Mapping[str, Mapping[str, Binding]], params: set[str]) -> None:
    """Raises one error listing every culprit, sorted.

    Wrong-type bindings (TypeError) take precedence over unknown parameters
    (ValueError): fix the kind of binding first, then the names.
    """
    wrong = sorted(
        (row, param, type(binding).__name__)
        for row, bindings in rows.items()
        for param, binding in bindings.items()
        if not isinstance(binding, Source | Value | Group)
    )
    if wrong:
        listed = ", ".join(f"row {r!r} parameter {p!r} ({t})" for r, p, t in wrong)
        msg = f"expected source(...), value(...) or group(...) for: {listed}"
        raise TypeError(msg)
    unknown = sorted(
        (row, param)
        for row, bindings in rows.items()
        for param in bindings
        if param not in params
    )
    if unknown:
        listed = ", ".join(f"row {r!r} parameter {p!r}" for r, p in unknown)
        msg = f"the function does not have the parameters named by: {listed}"
        raise ValueError(msg)


# The placeholder every row's docstring template has besides its parameters.
_OUTPUT_NAME = "output_name"


def _group_text(binding: Group) -> str:
    """A group as a docstring shows it: its items joined, a source by its name."""

    def item(bound: Source | Value) -> str:
        return bound.name if isinstance(bound, Source) else str(bound.value)

    if binding.is_list:
        return ", ".join(item(b) for b in binding.positional)
    return ", ".join(f"{key}={item(b)}" for key, b in binding.named)


class _Fields(dict[str, object]):
    """The fields of one row's docstring; remembers every one it did not have."""

    def __init__(self, fields: Mapping[str, object]) -> None:
        super().__init__(fields)
        self.missing: set[str] = set()

    def __missing__(self, key: str) -> str:
        self.missing.add(key)
        return ""


def _templated(
    fn: Callable[..., object],
    rows: Mapping[str, Mapping[str, Binding]],
    given: set[str],
) -> dict[str, str | None]:
    """Each row's docstring: the function's, templated, unless the row gave one.

    A row's fields are `output_name`, its name, and each parameter it binds: a
    source's name, a value as the value itself (so a format spec works on it), a
    group's items joined. `ValueError` listing every row and placeholder the
    docstring uses but the row does not have, and every malformed template.
    """
    template = inspect.cleandoc(fn.__doc__) if isinstance(fn.__doc__, str) else None
    docs: dict[str, str | None] = {}
    problems: list[str] = []
    for row, bindings in rows.items():
        if row in given or template is None:
            docs[row] = None
            continue
        fields = _Fields(
            {
                _OUTPUT_NAME: row,
                **{
                    param: (
                        binding.name
                        if isinstance(binding, Source)
                        else _group_text(binding)
                        if isinstance(binding, Group)
                        else binding.value
                    )
                    for param, binding in bindings.items()
                },
            }
        )
        try:
            docs[row] = template.format_map(fields)
        except (ValueError, AttributeError, IndexError, TypeError) as error:
            problems.append(f"row {row!r}: {error}")
        problems.extend(
            f"row {row!r}: placeholder {{{key}}} is not {_OUTPUT_NAME!r} or a "
            "parameter the row binds"
            for key in sorted(fields.missing)
        )
    if problems:
        shown = getattr(fn, "__qualname__", fn)
        msg = (
            f"the docstring of {shown!r} is a template for each row's doc: "
            f"{'; '.join(sorted(problems))}; write a literal brace as {{{{ or }}}}, or "
            "give the row its own doc with (bindings, doc)"
        )
        raise ValueError(msg)
    return docs


def _split_rows(
    rows: Mapping[str, object],
) -> tuple[dict[str, Mapping[str, Binding]], dict[str, str | None]]:
    """The rows' bindings and the docs the rows gave, or `TypeError` for every bad row.

    A row is a mapping, or `(bindings, doc)` with `doc` a `str` or `None`.
    """
    bindings: dict[str, Mapping[str, Binding]] = {}
    docs: dict[str, str | None] = {}
    problems: list[str] = []
    for name, row in rows.items():
        mapping = row
        if isinstance(row, tuple):
            if len(row) != 2:  # noqa: PLR2004  # (bindings, doc)
                problems.append(
                    f"row {name!r}: a (bindings, doc) tuple has 2 items, got {len(row)}"
                )
                continue
            mapping, doc = row
            if doc is not None and not isinstance(doc, str):
                problems.append(
                    f"row {name!r}: doc must be a str or None, got {type(doc).__name__}"
                )
                continue
            docs[name] = doc
        if not isinstance(mapping, Mapping):
            problems.append(
                f"row {name!r}: expected a mapping, got {type(mapping).__name__}"
            )
            continue
        bindings[name] = MappingProxyType(dict(mapping))
    if problems:
        raise TypeError("; ".join(sorted(problems)))
    return bindings, docs


def parameterize[F: Callable[..., object]](
    **rows: Mapping[str, Binding] | tuple[Mapping[str, Binding], str | None],
) -> Callable[[F], F]:
    """Declares one node per row, binding named parameters to a source or value.

    A row is its bindings, or `(bindings, doc)`: the doc is then the node's, used
    as written. Otherwise the node's doc is the function's docstring, templated with
    `str.format_map`: `{output_name}` is the row's name and each bound parameter
    its source's name or its value; a `group(...)` is its items joined. A
    placeholder the row does not have raises `ValueError` here, listing all.
    A binding may be `group(...)`, for a list or dict parameter.
    """
    if not rows:
        msg = "parameterize needs at least one row"
        raise ValueError(msg)
    # Typed as `object`: a caller may pass anything, whatever the hint says.
    given_rows: dict[str, object] = dict(rows)
    frozen, given_docs = _split_rows(given_rows)

    def apply(fn: F) -> F:
        current = decorations(fn)
        if current.rows is not None:
            msg = "function is already parameterized; apply parameterize once"
            raise ValueError(msg)
        if current.extract is not None and len(frozen) > 1:
            decorator = f"extract_{current.extract.kind}"
            raise ValueError(too_many_rows(frozen, decorator))
        # Names only: annotations are evaluated at build, not when decorating.
        forward = annotationlib.Format.FORWARDREF
        params = set(inspect.signature(fn, annotation_format=forward).parameters)
        _check_rows(frozen, params)
        group_check(fn, frozen)
        docs = _templated(fn, frozen, set(given_docs))
        docs.update(given_docs)
        recorded = replace(
            current, rows=MappingProxyType(frozen), docs=MappingProxyType(docs)
        )
        setattr(fn, _ATTRIBUTE, recorded)
        return fn

    return apply


def parameterize_sources[F: Callable[..., object]](
    **rows: Mapping[str, str],
) -> Callable[[F], F]:
    """`parameterize`, each row mapping parameters to the names of sources.

    `parameterize_sources(r=dict(p="x"))` is `parameterize(r=dict(p=source("x")))`.
    No row may be empty, and a source is a `str`: the error lists each offender.
    """
    if not rows:
        msg = "parameterize_sources needs at least one row"
        raise ValueError(msg)
    # Typed as `object`: a caller may pass anything, whatever the hint says.
    given: dict[str, object] = dict(rows)
    type_lines: list[str] = []
    value_lines: list[str] = []
    bound: dict[str, Mapping[str, Binding]] = {}
    for name, row in given.items():
        if not isinstance(row, Mapping):
            type_lines.append(
                f"row {name!r}: expected a mapping of parameter to source name, "
                f"got {type(row).__name__}"
            )
            continue
        if not row:
            value_lines.append(f"row {name!r}: has no parameter to bind")
            continue
        type_lines.extend(
            f"row {name!r} parameter {param!r}: a source is a str, got "
            f"{type(named).__name__}"
            for param, named in sorted(row.items())
            if not isinstance(named, str)
        )
        bound[name] = {param: Source(named) for param, named in row.items()}
    if type_lines:
        raise TypeError("; ".join(sorted(type_lines + value_lines)))
    if value_lines:
        raise ValueError("; ".join(sorted(value_lines)))
    return parameterize(**bound)


def parameterize_values[F: Callable[..., object]](
    parameter: str, assigned_output: Mapping[tuple[str, str | None], object]
) -> Callable[[F], F]:
    """`parameterize` over one parameter: each `(name, doc)` key gives a node its value.

    `parameterize_values("p", {("a", "doc"): 1})` is
    `parameterize(a=({"p": value(1)}, "doc"))`: the doc is the node's as written,
    and may be `None`. `assigned_output` may not be empty, a key is a `(str, str |
    None)` tuple, and a name may be given once; the error lists each offender.
    """
    if not isinstance(parameter, str):
        msg = f"parameter must be a str, got {type(parameter).__name__}"
        raise TypeError(msg)
    # Typed as `object`: a caller may pass anything, whatever the hint says.
    given: object = assigned_output
    if not isinstance(given, Mapping):
        msg = f"assigned_output must be a mapping, got {type(given).__name__}"
        raise TypeError(msg)
    if not given:
        msg = "assigned_output is empty: it needs a (name, doc) key for each node"
        raise ValueError(msg)
    problems: list[str] = []
    rows: dict[str, tuple[Mapping[str, Binding], str | None]] = {}
    repeated: set[str] = set()
    items: dict[object, object] = dict(given)
    for key, assigned in items.items():
        if not isinstance(key, tuple) or len(key) != 2:  # noqa: PLR2004  # (name, doc)
            problems.append(f"key {key!r} must be a (name, doc) tuple")
            continue
        name, doc = key
        if not isinstance(name, str) or (doc is not None and not isinstance(doc, str)):
            problems.append(f"key {key!r} must be (str, str | None)")
            continue
        if name in rows:
            repeated.add(name)
        rows[name] = ({parameter: Value(assigned)}, doc)
    if problems:
        raise TypeError("; ".join(sorted(problems)))
    if repeated:
        listed = ", ".join(repr(n) for n in sorted(repeated))
        msg = f"assigned_output names a node more than once: {listed}"
        raise ValueError(msg)
    return parameterize(**rows)


def tag[F: Callable[..., object]](**tags: str) -> Callable[[F], F]:
    """Attaches string tags to the node function."""
    # Typed as `object`: a caller may pass anything, whatever the hint says.
    given_tags: dict[str, object] = dict(tags)
    not_str = [
        f"tag {key!r}: value must be str, got {type(tag_value).__name__}"
        for key, tag_value in sorted(given_tags.items())
        if not isinstance(tag_value, str)
    ]
    if not_str:
        raise TypeError("; ".join(not_str))

    def apply(fn: F) -> F:
        current = decorations(fn)
        repeated = sorted(set(current.tags) & set(tags))
        if repeated:
            names = ", ".join(repr(k) for k in repeated)
            msg = f"tag {names} already set on this function"
            raise ValueError(msg)
        merged = MappingProxyType({**current.tags, **tags})
        setattr(fn, _ATTRIBUTE, replace(current, tags=merged))
        return fn

    return apply


# The values of `CacheBehavior`.
_BEHAVIORS = ("default", "recompute", "disable")


def _check_format(format_: object) -> None:
    """Refuses a format that is neither a `Format` nor a non-empty name."""
    if format_ is None or isinstance(format_, Format):
        return
    if not isinstance(format_, str) or not format_:
        msg = f"cache format must be a Format or its name, got {format_!r}"
        raise TypeError(msg)


def cache[F: Callable[..., object]](
    *,
    behavior: CacheBehavior = "default",
    ignore: Iterable[str] = (),
    version: str | None = None,
    format: Format | str | None = None,  # noqa: A002  # Hamilton's name for it
) -> Callable[[F], F]:
    """Records the node's cache policy; the build reads it, nothing runs here."""
    # Typed as `object`: a caller may pass anything, whatever the hint says.
    given_behavior: object = behavior
    given_version: object = version
    given_format: object = format
    if given_behavior not in _BEHAVIORS:
        allowed = ", ".join(repr(b) for b in _BEHAVIORS)
        msg = f"cache behavior {given_behavior!r} is not one of {allowed}"
        raise ValueError(msg)
    if given_version is not None and (
        not isinstance(given_version, str) or not given_version
    ):
        msg = f"cache version must be a non-empty str, got {given_version!r}"
        raise TypeError(msg)
    if isinstance(ignore, str):
        msg = "cache ignore must be an iterable of parameter names, not one str"
        raise TypeError(msg)
    names: list[object] = list(ignore)
    not_str = sorted(type(n).__name__ for n in names if not isinstance(n, str))
    if not_str:
        listed = ", ".join(not_str)
        msg = f"cache ignore names must be str, got: {listed}"
        raise TypeError(msg)
    _check_format(given_format)
    policy = CachePolicy(behavior, frozenset(str(n) for n in names), version, format)

    def apply(fn: F) -> F:
        current = decorations(fn)
        if current.cache is not None:
            msg = "function already has a cache policy; apply cache once"
            raise ValueError(msg)
        # Names only: annotations are evaluated at build, not when decorating.
        forward = annotationlib.Format.FORWARDREF
        params = set(inspect.signature(fn, annotation_format=forward).parameters)
        unknown = sorted(policy.ignore - params)
        if unknown:
            listed = ", ".join(repr(n) for n in unknown)
            msg = f"the function does not have the parameters named by ignore: {listed}"
            raise ValueError(msg)
        setattr(fn, _ATTRIBUTE, replace(current, cache=policy))
        return fn

    return apply


def _names(given: Iterable[str], argument: str) -> frozenset[str]:
    """`given` as a set of names; `TypeError` for one bare `str` or a non-`str` item."""
    if isinstance(given, str):
        msg = f"defaults {argument} must be an iterable of parameter names, not one str"
        raise TypeError(msg)
    names: list[object] = list(given)
    not_str = sorted({type(n).__name__ for n in names if not isinstance(n, str)})
    if not_str:
        msg = f"defaults {argument} names must be str, got: {', '.join(not_str)}"
        raise TypeError(msg)
    return frozenset(str(n) for n in names)


def defaults[F: Callable[..., object]](
    *, shadowed: Iterable[str] = (), differing: Iterable[str] = ()
) -> Callable[[F], F]:
    """Records which defaults of the function the build may not refuse.

    The build refuses a parameter with a default that reads a node's name, because
    the node replaces the default (`shadowed`), and an input that two consumers
    give different defaults (`differing`). Naming a parameter here opts it out.
    Applying `defaults` again adds its names to the first call's.
    """
    policy = DefaultsPolicy(
        _names(shadowed, "shadowed"), _names(differing, "differing")
    )

    def apply(fn: F) -> F:
        current = decorations(fn)
        # Names only: annotations are evaluated at build, not when decorating.
        forward = annotationlib.Format.FORWARDREF
        parameters = inspect.signature(fn, annotation_format=forward).parameters
        optional = {
            name
            for name, parameter in parameters.items()
            if parameter.default is not inspect.Parameter.empty
        }
        unknown = [
            f"{argument}: {', '.join(repr(n) for n in sorted(names - optional))}"
            for argument, names in (
                ("shadowed", policy.shadowed),
                ("differing", policy.differing),
            )
            if names - optional
        ]
        if unknown:
            msg = (
                "defaults names a parameter that is not one with a default, "
                f"in {'; '.join(unknown)}"
            )
            raise ValueError(msg)
        merged = policy
        if current.defaults is not None:
            merged = DefaultsPolicy(
                current.defaults.shadowed | policy.shadowed,
                current.defaults.differing | policy.differing,
            )
        setattr(fn, _ATTRIBUTE, replace(current, defaults=merged))
        return fn

    return apply


def _mark_excluded[F: Callable[..., object]](fn: F) -> F:
    """Records on `fn` that it is not a node; returns `fn` itself."""
    # Typed as `object`: a caller may pass anything, whatever the hint says.
    given: object = fn
    if not callable(given) or isinstance(given, type):
        msg = f"exclude takes a function or nothing, got {_shown(given)}"
        raise TypeError(msg)
    try:
        setattr(fn, _ATTRIBUTE, replace(decorations(fn), excluded=True))
    except AttributeError, TypeError:
        # A callable that takes no attributes, such as a built-in function.
        msg = f"exclude takes a function or nothing, got {_shown(given)}"
        raise TypeError(msg) from None
    return fn


def _shown(given: object) -> str:
    """What the exclude error calls `given`: its kind, or `a class` for a class."""
    return "a class" if isinstance(given, type) else type(given).__name__


# What `exclude()` was called with when nothing was: `None` is a value to refuse.
_NOTHING: Final = object()


@overload
def exclude[F: Callable[..., object]](fn: F, /) -> F: ...


@overload
def exclude[F: Callable[..., object]]() -> Callable[[F], F]: ...


def exclude[F: Callable[..., object]](fn: F = _NOTHING, /) -> F | Callable[[F], F]:  # ty: ignore[invalid-parameter-default]  # a sentinel for "called with nothing"
    """Keeps a public function out of the graph: written bare or called, `@exclude()`.

    Only records the mark: the function is returned unchanged, still callable, and
    stacks with the other decorators in either order. The scan skips it, so its
    annotations are never read. Anything but a function, `None` and a class
    included, is a `TypeError`. Hamilton's `hamilton_exclude` works bare only (Q73).
    """
    if fn is _NOTHING:
        return _mark_excluded
    return _mark_excluded(fn)
