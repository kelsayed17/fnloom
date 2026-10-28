"""Nodes in a module importable by name, so a pickled driver can be rebuilt from it.

The pickling tests send drivers built from this module to worker processes, which
import it again by name.
"""


def doubled(a: int) -> int:
    """Twice the input."""
    return a * 2


def total(doubled: int, b: int) -> int:
    """The doubled input plus `b`."""
    return doubled + b
