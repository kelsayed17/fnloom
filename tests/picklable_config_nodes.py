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


def _is_eu(region: str) -> bool:
    """Whether the config is for the EU."""
    return region == "eu"


@config.matches(_is_eu)
def matched_rate__eu() -> float:
    """The EU rate."""
    return 0.2


@config.default
def matched_rate__other() -> float:
    """Every other rate."""
    return 0.1
