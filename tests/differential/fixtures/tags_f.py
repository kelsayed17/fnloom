"""Tagged nodes, decorated with fnloom's decorators; it mirrors `tags_h`."""

from fnloom import parameterize, source, tag


@tag(owner="pricing", stage="raw")
def price(cents: int) -> float:
    """The price in whole units."""
    return cents / 100


@parameterize(low={"amount": source("price")}, high={"amount": source("ceiling")})
@tag(owner="limits")
def bound(amount: float) -> float:
    """One bound per row, each tagged like the function."""
    return amount


def untagged(low: float, high: float) -> float:
    """A node without tags of its own."""
    return high - low
