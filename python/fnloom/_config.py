"""What `@config.when*` and `@config.default` record, and how the build resolves it."""

import ast
import inspect
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

from fnloom import _extract
from fnloom._decorators import _ATTRIBUTE, decorations

if TYPE_CHECKING:
    from types import ModuleType

type ConditionKind = Literal["when", "when_not", "when_in", "when_not_in"]


@dataclass(frozen=True, slots=True)
class Condition:
    """One condition on a config key.

    Attributes:
        kind: `"when"` (equals), `"when_not"` (differs), `"when_in"` or
            `"when_not_in"` (member of `values`).
        key: The config key.
        values: What the key is compared with. `when` and `when_not` hold one.
    """

    kind: ConditionKind
    key: str
    values: tuple[object, ...]


@dataclass(frozen=True, slots=True)
class ConfigPolicy:
    """What the config decorators recorded on one function.

    Attributes:
        conditions: Every condition must hold (stacked decorators AND), sorted
            by key and kind.
        default: True for the variant used when no other variant matches.
        name: The node name given by `name=`, or None to cut it from the
            function's name.
    """

    conditions: tuple[Condition, ...]
    default: bool
    name: str | None


def _sort_key(condition: Condition) -> tuple[str, str, str]:
    # The repr breaks a tie between equal keys and kinds, so order never matters.
    return (condition.key, condition.kind, repr(condition.values))


def build_conditions(
    kind: ConditionKind,
    conditions: object,
    keys: Mapping[str, object],
    name: object,
) -> ConfigPolicy:
    """Checks the arguments of one `config.when*` call and returns its policy."""
    if callable(conditions):
        msg = f"use @config.{kind}(...), with parentheses"
        raise TypeError(msg)
    if conditions is not None and not isinstance(conditions, Mapping):
        msg = (
            f"config.{kind} takes conditions as a mapping or keywords, "
            f"got {type(conditions).__name__}"
        )
        raise TypeError(msg)
    given: dict[str, object] = dict(conditions) if conditions is not None else {}
    not_str = sorted(
        type(k).__name__ for k in list[object](given) if not isinstance(k, str)
    )
    if not_str:
        msg = f"config.{kind} keys must be str, got: {', '.join(not_str)}"
        raise TypeError(msg)
    twice = sorted(set(given) & set(keys))
    if twice:
        listed = ", ".join(repr(k) for k in twice)
        msg = f"config.{kind}: {listed} is given both in the mapping and as a keyword"
        raise ValueError(msg)
    given.update(keys)
    if not given:
        msg = f"config.{kind} needs at least one condition"
        raise ValueError(msg)
    if name is not None and not isinstance(name, str):
        msg = f"config.{kind} name must be a str, got {type(name).__name__}"
        raise TypeError(msg)
    if name == "":
        msg = f"config.{kind} name must not be empty"
        raise ValueError(msg)
    bad = sorted(
        (key, type(value).__name__)
        for key, value in given.items()
        if kind in {"when_in", "when_not_in"} and not _is_collection(value)
    )
    if bad:
        listed = ", ".join(f"{k!r} ({t})" for k, t in bad)
        msg = f"config.{kind}: these keys need a collection of values: {listed}"
        raise TypeError(msg)
    built = tuple(
        sorted(
            (
                Condition(kind, key, _values(kind, value))
                for key, value in given.items()
            ),
            key=_sort_key,
        )
    )
    return ConfigPolicy(built, default=False, name=name)


def _is_collection(value: object) -> bool:
    return isinstance(value, Iterable) and not isinstance(value, str | bytes)


def _values(kind: ConditionKind, value: object) -> tuple[object, ...]:
    if kind in {"when", "when_not"}:
        return (value,)
    items = tuple[object, ...](value)  # ty: ignore[invalid-argument-type]  # checked by _is_collection
    if isinstance(value, set | frozenset):
        # Hash order varies between runs: store the values in a fixed order.
        try:
            return tuple(sorted(items))  # ty: ignore[invalid-argument-type]  # a TypeError falls back to repr
        except TypeError:
            return tuple(sorted(items, key=repr))
    return items


def node_name(fn: Callable[..., object], name: str | None) -> str:
    """Returns the node name: `name`, or the function's name cut at the last `__`.

    Raises:
        ValueError: The function's name ends in `__`, or the name is empty.
    """
    if name is not None:
        return name
    full = str(getattr(fn, "__name__", ""))
    if full.endswith("__"):
        msg = f"function {full!r} ends in '__': there is no node name to cut"
        raise ValueError(msg)
    cut: str = full.rpartition("__")[0] if "__" in full else full
    if not cut:
        msg = f"function {full!r} has an empty node name"
        raise ValueError(msg)
    return cut


def merge(current: ConfigPolicy | None, new: ConfigPolicy) -> ConfigPolicy:
    """Combines the policy already on a function with a new one.

    Raises:
        ValueError: `default` meets a condition, or two different names.
    """
    if current is None:
        return new
    if current.default != new.default and (current.conditions or new.conditions):
        msg = "@config.default cannot be combined with a config.when* on one function"
        raise ValueError(msg)
    if current.name is not None and new.name is not None and current.name != new.name:
        msg = f"node name set twice: {current.name!r} and {new.name!r}"
        raise ValueError(msg)
    conditions = tuple(sorted((*current.conditions, *new.conditions), key=_sort_key))
    return ConfigPolicy(
        conditions, new.default, new.name if current.name is None else current.name
    )


class config:  # noqa: N801  # Hamilton's name
    """The config decorators: they record conditions, the build resolves them."""

    @staticmethod
    def when[F: Callable[..., object]](
        conditions: Mapping[str, object] | None = None,
        /,
        *,
        name: str | None = None,
        **keys: object,
    ) -> Callable[[F], F]:
        """Keeps the node when every key equals its value."""
        return _config_decorator("when", conditions, keys, name)

    @staticmethod
    def when_not[F: Callable[..., object]](
        conditions: Mapping[str, object] | None = None,
        /,
        *,
        name: str | None = None,
        **keys: object,
    ) -> Callable[[F], F]:
        """Keeps the node when every key differs from its value."""
        return _config_decorator("when_not", conditions, keys, name)

    @staticmethod
    def when_in[F: Callable[..., object]](
        conditions: Mapping[str, object] | None = None,
        /,
        *,
        name: str | None = None,
        **keys: object,
    ) -> Callable[[F], F]:
        """Keeps the node when every key is one of its values."""
        return _config_decorator("when_in", conditions, keys, name)

    @staticmethod
    def when_not_in[F: Callable[..., object]](
        conditions: Mapping[str, object] | None = None,
        /,
        *,
        name: str | None = None,
        **keys: object,
    ) -> Callable[[F], F]:
        """Keeps the node when every key is none of its values."""
        return _config_decorator("when_not_in", conditions, keys, name)

    @staticmethod
    def default[F: Callable[..., object]](fn: F) -> F:
        """Marks the variant used when no other variant of the node matches."""
        if not callable(fn):
            msg = "@config.default is used bare, without parentheses"
            raise TypeError(msg)
        node_name(fn, None)
        return _record(fn, ConfigPolicy((), default=True, name=None))


def _record[F: Callable[..., object]](fn: F, policy: ConfigPolicy) -> F:
    current = decorations(fn)
    setattr(fn, _ATTRIBUTE, replace(current, config=merge(current.config, policy)))
    return fn


def _config_decorator[F: Callable[..., object]](
    kind: ConditionKind,
    conditions: object,
    keys: Mapping[str, object],
    name: str | None,
) -> Callable[[F], F]:
    policy = build_conditions(kind, conditions, keys, name)

    def apply(fn: F) -> F:
        node_name(fn, policy.name)
        return _record(fn, policy)

    return apply


# A function found in a module or given to `with_functions`: its module's name, the
# name it was found under (its attribute name, or its own name), and the function.
type Found = tuple[str, str, Callable[..., object]]


@dataclass(frozen=True, slots=True)
class Variant:
    """A config-decorated function, as one variant of the node it names.

    Attributes:
        function: The function as written, never changed.
        policy: What its config decorators recorded.
        node: The node name: `name=`, or the function's name cut at the last `__`.
        produces: The names of the nodes it makes: its rows, or `node` itself.
    """

    function: Callable[..., object]
    policy: ConfigPolicy
    node: str
    produces: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Selection:
    """What the config selected among the found functions.

    Attributes:
        functions: The functions to lower, in the order found: every plain
            function, and the one variant selected for each node.
        unmatched: For each node name no variant produced, why: see `unmatched`.
    """

    functions: list[Found]
    unmatched: dict[str, str]


# A comparison that returned no bool: the config key, and the result's type by
# qualified name.
type _Unequal = tuple[str, str]

# The names NumPy gives its boolean scalar type (`bool` since 2.0, `bool_` before).
_NUMPY_BOOLS = frozenset({"bool", "bool_"})


def _is_bool(result: object) -> bool:
    """Whether `result` is a `bool`, or NumPy's boolean scalar, read without NumPy."""
    kind = type(result)
    return kind is bool or (
        kind.__module__ == "numpy" and kind.__name__ in _NUMPY_BOOLS
    )


def _equal(key: str, have: object, want: object, bad: set[_Unequal]) -> bool:
    """`have == want` as a `bool`; a result of any other type goes into `bad`."""
    result = have == want
    if _is_bool(result):
        return bool(result)
    kind = type(result)
    bad.add((key, f"{kind.__module__}.{kind.__qualname__}"))
    return False


def _holds(
    condition: Condition, config: Mapping[str, object], bad: set[_Unequal]
) -> bool:
    if condition.key not in config:
        return False
    have = config[condition.key]
    hits = [_equal(condition.key, have, want, bad) for want in condition.values]
    if condition.kind in {"when", "when_in"}:
        return any(hits)
    return not any(hits)


def _unequal_error(bad: set[_Unequal]) -> TypeError:
    """One error naming every key whose `==` returned no bool, sorted."""
    listed = ", ".join(f"{key!r} ({kind})" for key, kind in sorted(bad))
    return TypeError(f"== on these config keys did not return a bool: {listed}")


def _matches(
    policy: ConfigPolicy, config: Mapping[str, object], bad: set[_Unequal]
) -> bool:
    # Every condition is evaluated, so every bad comparison is found.
    held = [_holds(condition, config, bad) for condition in policy.conditions]
    return all(held)


def matches(policy: ConfigPolicy, config: Mapping[str, object]) -> bool:
    """Whether every condition of `policy` holds for `config`.

    A condition on a key `config` does not have is false, whichever its kind.
    An `==` must return a `bool`, or NumPy's boolean scalar, which counts as one.

    Raises:
        TypeError: An `==` returned anything else, such as an array; one
            message names every such key and the type it returned, sorted.
    """
    bad: set[_Unequal] = set()
    held = _matches(policy, config, bad)
    if bad:
        raise _unequal_error(bad)
    return held


def _variant(fn: Callable[..., object], policy: ConfigPolicy) -> Variant:
    node = node_name(fn, policy.name)
    recorded = decorations(fn)
    produces = (node,) if recorded.rows is None else tuple(recorded.rows)
    if recorded.extract is not None:
        produces = (*produces, *_extract.field_names(fn, recorded.extract))
    return Variant(fn, policy, node, produces)


def _labels(variants: Sequence[Variant]) -> dict[int, str]:
    """Names each variant by its function, with its module when they span several."""
    modules = {str(getattr(v.function, "__module__", "")) for v in variants}
    return {
        id(v.function): (
            str(getattr(v.function, "__name__", v.node))
            if len(modules) == 1
            else f"{v.function.__module__}.{getattr(v.function, '__name__', v.node)}"
        )
        for v in variants
    }


def _shown(config: Mapping[str, object]) -> str:
    """The config as a dict literal, its keys sorted."""
    return "{" + ", ".join(f"{k!r}: {config[k]!r}" for k in sorted(config)) + "}"


_OPERATORS: dict[ConditionKind, str] = {
    "when": "==",
    "when_not": "!=",
    "when_in": "in",
    "when_not_in": "not in",
}


def _need(condition: Condition) -> str:
    if condition.kind in {"when", "when_not"}:
        shown = repr(condition.values[0])
    else:
        shown = repr(list(condition.values))
    return f"{condition.key} {_OPERATORS[condition.kind]} {shown}"


def unmatched(
    name: str, config: Mapping[str, object], variants: Sequence[Variant]
) -> str:
    """Why node `name` has no variant: what `config` holds and what each variant needs.

    The variants are listed sorted by function name, each condition in its
    recorded order.
    """
    labels = _labels(variants)
    needs = ", ".join(
        f"{' and '.join(_need(c) for c in v.policy.conditions)} ({label})"
        for label, v in sorted(
            ((labels[id(v.function)], v) for v in variants), key=lambda pair: pair[0]
        )
    )
    return (
        f"no variant of {name!r} matches the config {_shown(config)}; "
        f"its variants need {needs}; add @config.default or a variant for it"
    )


def _not_given(
    picked: Variant, variants: Sequence[Variant], config: Mapping[str, object]
) -> dict[str, str]:
    """Why each name another variant gives, but `picked` does not, is absent."""
    labels = _labels(variants)
    givers: dict[str, list[str]] = {}
    for variant in variants:
        for produced in variant.produces:
            if produced not in picked.produces:
                givers.setdefault(produced, []).append(labels[id(variant.function)])
    return {
        produced: (
            f"no selected variant gives {produced!r}: the config {_shown(config)} "
            f"selects {labels[id(picked.function)]} for {picked.node!r}, and only "
            f"{', '.join(sorted(set(givers[produced])))} "
            f"{'gives' if len(set(givers[produced])) == 1 else 'give'} {produced!r}"
        )
        for produced in sorted(givers)
    }


def _pick(
    node: str,
    variants: list[Variant],
    config: Mapping[str, object],
    bad: set[_Unequal],
) -> tuple[Variant | None, str | None]:
    """The variant selected for `node`, or `None`; and a problem to refuse, if any."""
    labels = _labels(variants)
    defaults = sorted(labels[id(v.function)] for v in variants if v.policy.default)
    if len(defaults) > 1:
        listed = ", ".join(repr(label) for label in defaults)
        return None, f"node {node!r} has more than one @config.default: {listed}"
    hits = [
        v for v in variants if not v.policy.default and _matches(v.policy, config, bad)
    ]
    if len(hits) > 1:
        culprits = sorted(labels[id(v.function)] for v in hits)
        listed = ", ".join(repr(label) for label in culprits)
        return None, (
            f"node {node!r} has more than one variant matching the config "
            f"{_shown(config)}: {listed}"
        )
    if hits:
        return hits[0], None
    return next((v for v in variants if v.policy.default), None), None


def _groups(functions: Sequence[Found]) -> dict[str, list[Variant]]:
    """The config-decorated functions as variants, by the node they name."""
    groups: dict[str, list[Variant]] = {}
    seen: set[int] = set()
    for _, _, fn in functions:
        policy = decorations(fn).config
        # A function bound under two names, or a module given twice, is one variant.
        if policy is None or id(fn) in seen:
            continue
        seen.add(id(fn))
        variant = _variant(fn, policy)
        groups.setdefault(variant.node, []).append(variant)
    return groups


def resolve_variants(
    functions: Sequence[Found], config: Mapping[str, object]
) -> Selection:
    """Selects one variant per node; see `select`. Also says why a node has none."""
    groups = _groups(functions)
    chosen: set[int] = set()
    problems: list[tuple[str, str]] = []
    missing: dict[str, str] = {}
    bad: set[_Unequal] = set()
    for node in sorted(groups):
        variants = groups[node]
        picked, problem = _pick(node, variants, config, bad)
        if problem is not None:
            problems.append((node, problem))
        elif picked is not None:
            chosen.add(id(picked.function))
            missing.update(_not_given(picked, variants, config))
        else:
            reason = unmatched(node, config, variants)
            for produced in sorted({p for v in variants for p in v.produces}):
                missing[produced] = reason
    # A bad comparison leaves the selection unknown, so it is raised first.
    if bad:
        raise _unequal_error(bad)
    if problems:
        raise ValueError("; ".join(problem for _, problem in problems))
    selected: list[Found] = []
    for found in functions:
        fn = found[2]
        if decorations(fn).config is None:
            selected.append(found)
        elif id(fn) in chosen:
            chosen.discard(id(fn))
            selected.append(found)
    return Selection(selected, missing)


def select(functions: Sequence[Found], config: Mapping[str, object]) -> list[Found]:
    """The functions to lower: every plain one, and one variant per node.

    A node's variant is the one whose conditions all hold for `config`, or else
    its `@config.default`; a node with neither has no function. A function
    bound under two names counts once, under the first.

    Raises:
        ValueError: Two variants of one node match, or one node has two
            defaults; every such node in one message, sorted.
        TypeError: An `==` did not return a `bool` (see `matches`); every
            such key in one message.
    """
    return resolve_variants(functions, config).functions


def redefinitions(module: ModuleType) -> list[str]:
    """A line per config-decorated function `module` defines twice at top level.

    Python keeps only the last definition, so the earlier ones would be lost
    without a word. The module's source is read with `ast`; a module without
    source, such as one built in memory, has nothing to check.
    """
    try:
        source = inspect.getsource(module)
        tree = ast.parse(source)
    except OSError, TypeError, SyntaxError:
        # No source, or source edited since import that no longer parses.
        return []
    lines: dict[str, list[int]] = {}
    for statement in tree.body:
        if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef):
            lines.setdefault(statement.name, []).append(statement.lineno)
    found: list[str] = []
    for name in sorted(lines):
        if len(lines[name]) < 2:  # noqa: PLR2004  # twice is a redefinition
            continue
        current = getattr(module, name, None)
        if not callable(current) or decorations(current).config is None:
            continue
        listed = ", ".join(str(line) for line in lines[name])
        found.append(
            f"function {name!r} is defined more than once in module "
            f"{module.__name__!r} (lines {listed}); Python keeps only the last"
        )
    return found
