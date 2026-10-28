"""Config variants in a module importable by name, for pickling a configured driver."""

from fnloom import config


@config.when(mode="a")
def scale__a(a: int) -> int:
    """The input, unchanged."""
    return a


@config.when(mode="b")
def scale__b(a: int) -> int:
    """Ten times the input."""
    return a * 10
