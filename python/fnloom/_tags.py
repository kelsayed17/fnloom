"""Tag values: a string or a tuple of them, and the query that matches them."""

from collections.abc import Mapping, Sequence
from typing import Final, final, override

type TagValue = str | tuple[str, ...]


@final
class _Any:
    """The query value that matches any node carrying the tag.

    One object: `copy`, `deepcopy` and `pickle` give back `ANY` itself, so a
    copied query still matches by identity.
    """

    __slots__ = ()

    @override
    def __repr__(self) -> str:
        return "fnloom.ANY"

    @override
    def __reduce__(self) -> str:
        return "ANY"


ANY: Final = _Any()
"""A `tagged(...)` query value: the tag is present, whatever its value."""

type Query = str | Sequence[str] | _Any


def checked_value(key: str, value: object) -> TagValue:
    """`value` as a tag value: a `str`, or a non-empty tuple of distinct `str`.

    A `list` or `tuple` is kept in written order. A `str` subclass, alone or as
    an item, is kept as a plain `str`, so views and exports hold one type.
    `TypeError` for anything else, an unordered collection included;
    `ValueError` for an empty sequence or a repeated item. Both name the tag
    `key`.
    """
    if isinstance(value, str):
        return str.__str__(value)
    if not isinstance(value, list | tuple):
        got = type(value).__name__
        msg = f"tag {key!r}: value must be str or a list or tuple of str, got {got}"
        raise TypeError(msg)
    items: list[object] = list(value)
    wrong = [type(item).__name__ for item in items if not isinstance(item, str)]
    if wrong:
        msg = f"tag {key!r}: every item must be str, got {wrong[0]}"
        raise TypeError(msg)
    if not items:
        msg = f"tag {key!r}: a sequence value must not be empty"
        raise ValueError(msg)
    texts = [str.__str__(item) for item in items if isinstance(item, str)]
    repeated = sorted({item for item in texts if texts.count(item) > 1})
    if repeated:
        msg = f"tag {key!r}: repeated items {', '.join(map(repr, repeated))}"
        raise ValueError(msg)
    return tuple(texts)


def checked_query(key: str, query: object) -> str | tuple[str, ...] | _Any:
    """`query` as one tag's query, or `TypeError` naming the tag `key`."""
    if query is ANY or isinstance(query, str):
        return query
    if isinstance(query, list | tuple) and query:
        items: list[object] = list(query)
        if all(isinstance(item, str) for item in items):
            return tuple(item for item in items if isinstance(item, str))
    msg = (
        f"tagged: {key!r} must be a str, a non-empty list or tuple of str, or "
        f"fnloom.ANY, got {query!r}"
    )
    raise TypeError(msg)


def tag_matches(
    tags: Mapping[str, TagValue], key: str, query: str | tuple[str, ...] | _Any
) -> bool:
    """Whether `tags` holds `key` with a value `query` matches.

    A string query matches an equal value, or a tuple holding it. A tuple query
    matches when any of its items does. `ANY` matches whatever the value is.
    """
    if (value := tags.get(key)) is None:
        return False
    if isinstance(query, _Any):
        return True
    held = (value,) if isinstance(value, str) else value
    wanted = (query,) if isinstance(query, str) else query
    return any(item in held for item in wanted)
