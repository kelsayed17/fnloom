"""Read-only descriptions of a built graph's nodes."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fnloom._decorators import Source, Value


@dataclass(frozen=True)
class Param:
    """One parameter of a node's function, and what it is bound to.

    `name` is the function's own parameter. A plain parameter is bound to
    `Source(name)`. `required` is False when the parameter has a default and is
    bound to a source; a `Value`-bound parameter is always supplied, so required.
    """

    name: str
    binding: Source | Value
    required: bool
    annotation: object
