"""What `@config.when*` and `@config.default` record about the build's config."""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

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
    built = tuple(
        sorted(
            (
                Condition(kind, key, _values(kind, key, value))
                for key, value in given.items()
            ),
            key=_sort_key,
        )
    )
    return ConfigPolicy(built, default=False, name=name)


def _values(kind: ConditionKind, key: str, value: object) -> tuple[object, ...]:
    if kind in {"when", "when_not"}:
        return (value,)
    if isinstance(value, str | bytes):
        msg = (
            f"config.{kind}: {key!r} needs a collection of values, "
            f"got a {type(value).__name__}"
        )
        raise TypeError(msg)
    if not isinstance(value, Iterable):
        msg = f"config.{kind}: {key!r} needs a collection of values, got {value!r}"
        raise TypeError(msg)
    return tuple[object, ...](value)


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
