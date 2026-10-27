"""Parameterized rows, decorated with Hamilton's decorators; `rows_f` mirrors it."""

from hamilton.function_modifiers import parameterize, source, value

BOUND: list[int] = []


@parameterize(sum_once={"weight": value(1)}, sum_twice={"weight": value(2)})
def weighted_sum(numbers: list[int], weight: int) -> int:
    """Adds the numbers and multiplies by the row's weight."""
    return sum(numbers) * weight


@parameterize(first={"prev": source("start")}, second={"prev": source("first")})
def doubled(prev: int) -> int:
    """Twice the previous row's value."""
    return prev * 2


@parameterize(shifted_total={"base": source("sum_once"), "offset": source("bump")})
def shifted(base: int, offset: int = 100) -> int:
    """The base plus an offset that defaults when `bump` is not supplied."""
    return base + offset


@parameterize(squared={"left": source("start"), "right": source("start")})
def product(left: int, right: int) -> int:
    """Both parameters read `start`."""
    return left * right


@parameterize(bound_seen={"acc": value(BOUND)})
def remember(start: int, acc: list[int]) -> bool:
    """Appends `start` to the bound list; true when that list is `BOUND` itself."""
    acc.append(start)
    return acc is BOUND
