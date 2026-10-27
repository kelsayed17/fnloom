"""A linear chain: input `a` feeds `b`, which feeds `c`."""


def b(a: int) -> int:
    """Adds one to the input."""
    return a + 1


def c(b: int) -> int:
    """Doubles `b`."""
    return b * 2
