"""Nodes absent on errors, in a module importable by name: a pickle rebuilds them."""

from fnloom import Absent, Collect, Parallelizable, absent_on, tag


def item(n: int) -> Parallelizable[int]:
    """Each of `range(n)`."""
    yield from range(n)


@tag(io="net")
def fetched(item: int) -> int:
    """The item, unless it is 1, which times out."""
    if item == 1:
        raise TimeoutError(item)
    return item * 10


@absent_on(KeyError)
def parsed(fetched: int) -> int | Absent:
    """The fetched value, unless it is 20, which has no key."""
    if fetched == 20:  # noqa: PLR2004  # the item that fails
        raise KeyError(fetched)
    return fetched + 1


def summary(parsed: Collect[int]) -> list[int]:
    """The parsed values that are present."""
    return list(parsed)
