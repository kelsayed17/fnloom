"""A diamond: `root` feeds `left` and `right`, which `join` combines."""


def root(a: int) -> int:
    """Adds one to the input."""
    return a + 1


def left(root: int) -> int:
    """Doubles `root`."""
    return root * 2


def right(root: int) -> int:
    """Negates `root`."""
    return -root


def join(left: int, right: int) -> int:
    """Sums the two branches."""
    return left + right
