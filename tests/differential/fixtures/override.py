"""A chain whose first node fails, so an override on `mid` proves upstream is cut."""


def base(a: int) -> int:
    """Always fails; it must not run when `mid` is overridden."""
    msg = f"base must not run (a={a})"
    raise RuntimeError(msg)


def mid(base: int) -> int:
    """Adds one to `base`."""
    return base + 1


def top(mid: int) -> int:
    """Doubles `mid`."""
    return mid * 2
