"""Functions importable by qualified name, given to `Builder.with_functions`.

The pickling tests build drivers from these functions alone, never from this module,
and load them again, here or in worker processes.
"""

import fnloom


def doubled(a: int) -> int:
    """Twice the input."""
    return a * 2


@fnloom.tag(kind="sum")
def total(doubled: int, b: int) -> int:
    """The doubled input plus `b`."""
    return doubled + b


def scaled(value: int, factor: int) -> int:
    """`value` times `factor`."""
    return value * factor


class Holder:
    """A class whose method is importable by its qualified name too."""

    @staticmethod
    def offset(total: int) -> int:
        """`total` plus one."""
        return total + 1
