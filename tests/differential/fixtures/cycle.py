"""Two nodes that depend on each other."""


def p(q: int) -> int:
    """Reads `q`."""
    return q


def q(p: int) -> int:
    """Reads `p`."""
    return p
