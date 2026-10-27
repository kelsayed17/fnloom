"""A node with a required input that the tests never supply."""


def out(needed: int) -> int:
    """Adds one to the required input."""
    return needed + 1
