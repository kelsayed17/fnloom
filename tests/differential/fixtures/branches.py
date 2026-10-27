"""Two independent branches whose nodes record, in `CALLS`, the order they run in."""

CALLS: list[str] = []


def a(x: int) -> int:
    """First node of the long branch."""
    CALLS.append("a")
    return x + 1


def b(a: int) -> int:
    """Second node of the long branch."""
    CALLS.append("b")
    return a + 1


def c(b: int, a: int) -> int:
    """Joins the long branch; its parameters are listed out of name order."""
    CALLS.append("c")
    return a + b


def z(x: int) -> int:
    """First node of the short branch, last by name."""
    CALLS.append("z")
    return x


def y(z: int) -> int:
    """End of the short branch."""
    CALLS.append("y")
    return z
