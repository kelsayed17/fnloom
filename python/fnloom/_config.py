"""What `@config.when*` and `@config.default` record about the build's config."""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Literal

from fnloom._decorators import _ATTRIBUTE, decorations

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
