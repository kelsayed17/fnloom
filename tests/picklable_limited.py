"""Limited nodes in a module importable by name, so a pickled driver can be rebuilt."""

from fnloom import limit


@limit(2, group="db")
def loaded(a: int) -> int:
    """The input, read under a limit."""
    return a + 1


@limit(group="db")
def saved(loaded: int) -> int:
    """The loaded value, written under the same group."""
    return loaded * 2
