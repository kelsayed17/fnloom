"""Output checks: `@check_output` and `@check_output_custom` (Q99 to Q103).

A check runs inside its node, after the node's function returns: no node is added.
A failed `fail` check is the node's own failure, a `ValidationError`; a failed
`warn` check logs one WARNING on the `fnloom` logger and the run goes on. Every
check's result reaches `NodeEnd.checks` when hooks listen.

Built-in checks read a value without consuming or changing it: a `str`, `bytes`
or mapping is one value; anything else that iterates is checked item by item,
except a one-shot iterator, which is refused; numpy and pandas values are read
through their own vectorised methods, found by duck typing.
"""

import collections.abc
import contextvars
import inspect
import logging
import math
import sys
import types
import typing
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, cast

from fnloom import _async, _types
from fnloom._decorators import _ATTRIBUTE, decorations
from fnloom._extract import extractor
from fnloom._scan import by_parameter

if TYPE_CHECKING:
    from collections.abc import Awaitable, Sequence

    from fnloom._scan import NodeDef

type Importance = Literal["fail", "warn"]

_IMPORTANCES: tuple[str, ...] = ("fail", "warn")

_LOG = logging.getLogger("fnloom")

_NO_DIAGNOSTICS: Mapping[str, object] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One check's result on one value.

    `name` is the check's: a built-in's argument, such as `range`, a function's
    name, or a `Validator`'s `name`. `importance` is `"fail"` or `"warn"`.
    `message` says why it failed, or is empty when it passed. `diagnostics` hold
    what the check found, read-only.
    """

    name: str
    importance: Importance
    passes: bool
    message: str = ""
    diagnostics: Mapping[str, object] = field(default=_NO_DIAGNOSTICS)


class ValidationError(ValueError):
    """A node's value failed one or more of its `fail` checks (Q100).

    `node` is the node's name and `results` the failed checks' results, in the
    order the checks ran. The message has one line per failed check:
    `x: check range failed: 12 is not in [0, 10]`.
    """

    def __init__(self, node: str, results: Sequence[CheckResult]) -> None:
        """Names `node` and each failed check in `results`."""
        self.node = node
        self.results = tuple(results)
        super().__init__(
            "\n".join(f"{node}: check {r.name} failed: {r.message}" for r in results)
        )


class Validator:
    """A custom check: subclass it, and give instances to `check_output_custom`.

    Override `validate(value, inputs)`, which returns `True`, `None` or a passing
    `CheckResult` when the value is good, and `False`, a message `str` or a
    failing `CheckResult` when it isn't. `inputs` are the node's arguments by
    parameter name, read-only. Override `applies_to(declared)` to refuse a
    node's return annotation at build: returning `False` is a `TypeError`
    naming the node. `name` defaults to the class's name; `importance` is
    `"fail"` unless given.
    """

    name: str = ""
    importance: Importance = "fail"

    def __init__(self, importance: Importance = "fail") -> None:
        """A check failing the node (`"fail"`) or logging a warning (`"warn"`)."""
        _checked_importance(importance)
        self.importance: Importance = importance

    def applies_to(self, declared: object) -> bool:  # noqa: ARG002  # an override's hook
        """Whether a node returning `declared` can be checked; `True` by default."""
        return True

    def validate(self, value: object, inputs: Mapping[str, object]) -> object:
        """Checks `value`; see the class for what it may return."""
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class Check:
    """One check as a decorator recorded it, run by the node it is on.

    `run(value, inputs)` gives the check's `CheckResult`. `problem(declared)` is
    why a node returning `declared` can't be checked, or `None`. `field` names
    the extracted field the check is on, or is `None` for the node's own value.
    `wants_inputs` says whether `run` reads `inputs`. `identity` is what the
    result cache keys the check by.
    """

    name: str
    importance: Importance
    run: Callable[[object, Mapping[str, object]], CheckResult]
    problem: Callable[[object], str | None]
    identity: tuple[object, ...]
    field: str | None = None
    wants_inputs: bool = False


def _checked_importance(importance: object) -> None:
    if importance not in _IMPORTANCES:
        msg = f"importance must be 'fail' or 'warn', got {importance!r}"
        raise ValueError(msg)


def _checked_field(field_name: object) -> None:
    if field_name is not None and not isinstance(field_name, str):
        msg = f"field must be a field's name, got {type(field_name).__name__}"
        raise TypeError(msg)


def _record[F: Callable[..., object]](fn: F, checks: tuple[Check, ...]) -> F:
    """Adds `checks` before those already on `fn`: the top decorator's run first."""
    current = decorations(fn)
    setattr(fn, _ATTRIBUTE, replace(current, checks=(*checks, *current.checks)))
    return fn


# ---- reading a value without consuming it --------------------------------------


class _OneShotError(TypeError):
    """A one-shot iterator, which a check would consume."""


def _whole(value: object) -> bool:
    """Whether `value` is checked as one value, never item by item."""
    return isinstance(value, (str, bytes, bytearray, Mapping)) or not isinstance(
        value, Iterable
    )


def _items(value: object) -> Iterable[object]:
    """The items of a value that iterates, never consuming a one-shot iterator."""
    iterable = cast("Iterable[object]", value)
    if iter(iterable) is iterable:
        raise _OneShotError
    return iterable


def _numpy(value: object) -> Any | None:  # noqa: ANN401  # the numpy module, if loaded
    """The numpy module, when `value` is a numpy array or scalar and numpy is loaded."""
    if type(value).__module__.split(".", 1)[0] != "numpy":
        return None
    return sys.modules.get("numpy")


def _scalar(value: object) -> bool:
    return not hasattr(value, "__len__")


def _reduced(value: object, method: str) -> object:
    """`value.<method>()`, reduced until a scalar: a frame's `min` is a Series."""
    reduced = value
    for _ in range(3):
        if _scalar(reduced):
            return reduced
        reduced = getattr(reduced, method)()
    return reduced


def _vectorised(value: object, method: str) -> bool:
    return not _whole(value) and callable(getattr(value, method, None))


def _empty(value: object) -> bool:
    size = getattr(value, "size", None)
    return isinstance(size, int) and size == 0


def _least_and_most(value: object) -> tuple[object, object]:
    """`value`'s least and greatest items, NaN counted: pandas skips it otherwise."""

    def reduced(method: str) -> object:
        found = getattr(value, method)
        try:
            first = found(skipna=False)
        except TypeError:
            first = found()
        return _reduced(first, method)

    return reduced("min"), reduced("max")


def _ranged(
    value: object, inside: Callable[[object], bool], bounds: tuple[object, object]
) -> tuple[bool, str]:
    """`range` on a numpy or pandas value, through its own `min` and `max`."""
    if _empty(value):
        return True, ""
    try:
        least, most = _least_and_most(value)
    except (TypeError, ValueError, ArithmeticError) as error:
        return False, f"its values can't be ranged: {error}"
    if inside(least) and inside(most):
        return True, ""
    low, high = bounds
    return False, f"values from {least!r} to {most!r} are not all in [{low}, {high}]"


def _range(bounds: tuple[object, object]) -> Callable[[object], tuple[bool, str]]:
    low, high = bounds

    def inside(item: object) -> bool:
        try:
            return bool(low <= item <= high)  # ty: ignore[unsupported-operator]
        except TypeError, ValueError, ArithmeticError:
            return False

    def check(value: object) -> tuple[bool, str]:
        if _vectorised(value, "min") and _vectorised(value, "max"):
            return _ranged(value, inside, bounds)
        if _whole(value):
            if inside(value):
                return True, ""
            return False, f"{value!r} is not in [{low}, {high}]"
        outside = [item for item in _items(value) if not inside(item)]
        if not outside:
            return True, ""
        return False, (
            f"{len(outside)} items are not in [{low}, {high}], such as {outside[0]!r}"
        )

    return check


def _all_in(value: object, allowed: Sequence[object]) -> tuple[bool, str] | None:
    """Whether every item of a numpy or pandas value is allowed, or `None` for others.

    The message is set only when the values can't be compared at all.
    """
    try:
        if (numpy := _numpy(value)) is not None and not _scalar(value):
            # As objects, so a mixed list isn't turned into strings.
            listed = numpy.empty(len(allowed), dtype=object)
            listed[:] = list(allowed)
            return bool(numpy.isin(value, listed).all()), ""
        if _vectorised(value, "isin"):
            found = getattr(value, "isin")(list(allowed))  # noqa: B009  # pandas' own method
            return bool(_reduced(found, "all")), ""
    except (TypeError, ValueError) as error:
        return False, f"its values can't be compared: {error}"
    return None


def _values_in(allowed: Sequence[object]) -> Callable[[object], tuple[bool, str]]:
    def member(item: object) -> bool:
        try:
            return item in allowed
        except TypeError:
            return False

    def check(value: object) -> tuple[bool, str]:
        vectorised = _all_in(value, allowed)
        if vectorised is not None:
            ok, why = vectorised
            if why:
                return False, why
        elif _whole(value):
            ok = member(value)
            if not ok:
                return False, f"{value!r} is not one of {list(allowed)!r}"
        else:
            missing = [item for item in _items(value) if not member(item)]
            if missing:
                return False, f"{missing[0]!r} is not one of {list(allowed)!r}"
            ok = True
        return (
            (True, "")
            if ok
            else (False, f"not every value is one of {list(allowed)!r}")
        )

    return check


def _nan_count(value: object) -> tuple[int, int]:
    """How many of `value`'s items are NaN, and how many items there are."""
    if (numpy := _numpy(value)) is not None and not _scalar(value):
        try:
            found = numpy.isnan(value)
        except TypeError:
            found = None
        if found is not None:
            return int(found.sum()), int(found.size)
    if _vectorised(value, "isna"):
        found = getattr(value, "isna")()  # noqa: B009  # pandas' own method
        count = cast("float", _reduced(found, "sum"))
        return int(count), int(getattr(found, "size", 0))
    if _whole(value):
        return int(_is_nan(value)), 1
    items = list(_items(value))
    return sum(_is_nan(item) for item in items), len(items)


def _is_nan(item: object) -> bool:
    if isinstance(item, complex):
        return math.isnan(item.real) or math.isnan(item.imag)
    if isinstance(item, (str, bytes, bytearray)) or not hasattr(item, "__float__"):
        return False
    try:
        return math.isnan(float(cast("float", item)))
    except TypeError, ValueError, OverflowError:
        return False


def _allow_nans(allowed: bool) -> Callable[[object], tuple[bool, str]]:  # noqa: FBT001  # the check's own flag
    def check(value: object) -> tuple[bool, str]:
        if allowed:
            return True, ""
        nans, _ = _nan_count(value)
        return (True, "") if not nans else (False, f"{nans} values are NaN")

    return check


def _max_fraction_nans(most: float) -> Callable[[object], tuple[bool, str]]:
    def check(value: object) -> tuple[bool, str]:
        nans, size = _nan_count(value)
        fraction = nans / size if size else 0.0
        if fraction <= most:
            return True, ""
        return False, f"{fraction:.3g} of the values are NaN, more than {most:.3g}"

    return check


def _data_type(declared: object) -> Callable[[object], tuple[bool, str]]:
    def check(value: object) -> tuple[bool, str]:
        if _types.value_matches(declared, value):
            return True, ""
        expected = inspect.formatannotation(declared)
        return False, f"{type(value).__name__} is not {expected}"

    return check


def _allow_none(allowed: bool) -> Callable[[object], tuple[bool, str]]:  # noqa: FBT001  # the check's own flag
    def check(value: object) -> tuple[bool, str]:
        if value is None and not allowed:
            return False, "the value is None"
        return True, ""

    return check


def _contains(
    needles: Sequence[str], *, wanted: bool
) -> Callable[[object], tuple[bool, str]]:
    def check(value: object) -> tuple[bool, str]:
        if not isinstance(value, str):
            return False, f"{type(value).__name__} is not str"
        wrong = [n for n in needles if (n in value) != wanted]
        if not wrong:
            return True, ""
        verb = "does not contain" if wanted else "contains"
        return False, f"{value!r} {verb} {wrong[0]!r}"

    return check


# ---- which annotations a built-in check can read ----------------------------------

_ONE_SHOT = (
    collections.abc.Iterator,
    collections.abc.Generator,
    collections.abc.AsyncIterator,
    collections.abc.AsyncGenerator,
)
_NOT_NUMBERS = (str, bytes, bytearray, Mapping)


def _origin(declared: object) -> object:
    stripped = _types.stripped(declared)
    return typing.get_origin(stripped) or stripped


def _one_shot(declared: object) -> bool:
    """Whether `declared`, or any arm of it if a union, is a one-shot iterator."""
    stripped = _types.stripped(declared)
    if typing.get_origin(stripped) in {typing.Union, types.UnionType}:
        return any(_one_shot(arm) for arm in typing.get_args(stripped))
    origin = typing.get_origin(stripped) or stripped
    return isinstance(origin, type) and issubclass(origin, _ONE_SHOT)


def _textual(declared: object) -> bool:
    origin = _origin(declared)
    return isinstance(origin, type) and issubclass(origin, _NOT_NUMBERS)


def _reads_items(_: str) -> Callable[[object], str | None]:
    # A one-shot annotation is refused for every check, by `refuse_unreadable`.
    return _anything


def _numeric(name: str) -> Callable[[object], str | None]:
    reads = _reads_items(name)

    def problem(declared: object) -> str | None:
        if _textual(declared):
            return (
                f"check {name} does not apply to {inspect.formatannotation(declared)}"
            )
        return reads(declared)

    return problem


def _text(name: str) -> Callable[[object], str | None]:
    def problem(declared: object) -> str | None:
        if declared in {Any, object} or _types.type_matches(str, declared):
            return None
        return f"check {name} applies to str, not {inspect.formatannotation(declared)}"

    return problem


def _anything(_: object) -> str | None:
    return None


# ---- the decorators ---------------------------------------------------------------


def _builtin(  # noqa: PLR0913  # a check's parts, each named
    name: str,
    argument: object,
    test: Callable[[object], tuple[bool, str]],
    problem: Callable[[object], str | None],
    *,
    importance: Importance,
    field_name: str | None,
) -> Check:
    def run(value: object, inputs: Mapping[str, object]) -> CheckResult:  # noqa: ARG001  # a built-in reads no inputs
        try:
            passes, message = test(value)
        except _OneShotError:
            msg = f"check {name} would consume a one-shot iterator"
            raise TypeError(msg) from None
        diagnostics = MappingProxyType({name: argument})
        return CheckResult(name, importance, passes, message, diagnostics)

    return Check(
        name=name,
        importance=importance,
        run=run,
        problem=problem,
        identity=("builtin", name, argument),
        field=field_name,
    )


def check_output[F: Callable[..., object]](  # noqa: C901, PLR0912, PLR0913, PLR0915  # one keyword per built-in check
    *,
    importance: Importance = "fail",
    field: str | None = None,
    range: tuple[object, object] | None = None,  # noqa: A002  # Hamilton's name for it
    values_in: Iterable[object] | None = None,
    data_type: object = None,
    allow_none: bool | None = None,
    contains: str | Iterable[str] | None = None,
    does_not_contain: str | Iterable[str] | None = None,
    allow_nans: bool | None = None,
    max_fraction_nans: float | None = None,
) -> Callable[[F], F]:
    """Checks the node's value with built-in checks, each a keyword (Q101).

    `range=(lo, hi)` (inclusive), `values_in=[...]`, `data_type=T` (by
    `value_matches`, so generics and unions work), `allow_none=False`,
    `contains=` and `does_not_contain=` (a `str` or several, for `str` values),
    `allow_nans=False` and `max_fraction_nans=f`. `range`, `values_in` and the
    NaN checks read a scalar, or each item of a value that iterates; a `str`,
    `bytes` or mapping is one value. `importance` is `"fail"` (the default) or
    `"warn"`; `field` checks an extracted field rather than the whole value.

    Bad arguments are refused here; a check that can't read the node's return
    annotation is a `TypeError` at build. Stacks with others, in any order.
    """
    _checked_importance(importance)
    _checked_field(field)
    checks: list[Check] = []

    def add(
        name: str,
        argument: object,
        test: Callable[[object], tuple[bool, str]],
        problem: Callable[[object], str | None],
    ) -> None:
        checks.append(
            _builtin(
                name, argument, test, problem, importance=importance, field_name=field
            )
        )

    if range is not None:
        if not isinstance(range, tuple) or len(range) != 2:  # noqa: PLR2004  # a pair
            msg = f"range must be a (low, high) pair, got {range!r}"
            raise TypeError(msg)
        low, high = range
        if any(_is_nan(bound) for bound in range):
            msg = f"range bounds must not be NaN, got {range!r}"
            raise ValueError(msg)
        try:
            backwards = bool(low > high)  # ty: ignore[unsupported-operator]
        except TypeError:
            msg = f"range bounds must compare, got {range!r}"
            raise TypeError(msg) from None
        if backwards:
            msg = f"range low is above high: {range!r}"
            raise ValueError(msg)
        add("range", range, _range(range), _numeric("range"))
    if values_in is not None:
        if isinstance(values_in, (str, bytes)) or not isinstance(values_in, Iterable):
            msg = f"values_in must be a collection of values, got {values_in!r}"
            raise TypeError(msg)
        allowed = tuple(values_in)
        add("values_in", allowed, _values_in(allowed), _reads_items("values_in"))
    if data_type is not None:
        add("data_type", data_type, _data_type(data_type), _anything)
    if allow_none is not None:
        add("allow_none", bool(allow_none), _allow_none(bool(allow_none)), _anything)
    for name, given, wanted in (
        ("contains", contains, True),
        ("does_not_contain", does_not_contain, False),
    ):
        if given is not None:
            needles = (given,) if isinstance(given, str) else tuple(given)
            if not needles or not all(isinstance(n, str) for n in needles):
                msg = f"{name} must be a str or str values, got {given!r}"
                raise TypeError(msg)
            add(name, needles, _contains(needles, wanted=wanted), _text(name))
    if allow_nans is not None:
        add(
            "allow_nans",
            bool(allow_nans),
            _allow_nans(bool(allow_nans)),
            _numeric("allow_nans"),
        )
    if max_fraction_nans is not None:
        if isinstance(max_fraction_nans, bool) or not 0 <= max_fraction_nans <= 1:
            msg = f"max_fraction_nans must be in [0, 1], got {max_fraction_nans!r}"
            raise ValueError(msg)
        add(
            "max_fraction_nans",
            float(max_fraction_nans),
            _max_fraction_nans(float(max_fraction_nans)),
            _numeric("max_fraction_nans"),
        )
    if not checks:
        msg = "check_output needs at least one check, such as range=(0, 1)"
        raise TypeError(msg)
    frozen = tuple(checks)

    def apply(fn: F) -> F:
        return _record(fn, frozen)

    return apply


def _result(name: str, importance: Importance, returned: object) -> CheckResult:
    """A custom check's return value as a `CheckResult` (Q102)."""
    if isinstance(returned, CheckResult):
        return returned
    if returned is None or returned is True:
        return CheckResult(name, importance, passes=True)
    if returned is False:
        return CheckResult(name, importance, passes=False, message="it returned False")
    if isinstance(returned, str):
        return CheckResult(name, importance, passes=False, message=returned)
    msg = (
        f"check {name} must return a bool, a message str, None or a CheckResult, "
        f"got {type(returned).__name__}"
    )
    raise TypeError(msg)


def _from_function(
    fn: Callable[..., object], importance: Importance, field_name: str | None
) -> Check:
    name = getattr(fn, "__name__", type(fn).__name__)
    if inspect.iscoroutinefunction(fn):
        msg = f"check {name} is an async def; checks run synchronously"
        raise TypeError(msg)
    try:
        params = inspect.signature(fn).parameters
    except TypeError, ValueError:
        msg = f"check {name} has no signature to call it by"
        raise TypeError(msg) from None
    wants_inputs = "inputs" in params
    positional = [
        p
        for p in params.values()
        if p.name != "inputs" and p.kind in {p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD}
    ]
    if not positional:
        msg = f"check {name} must take the value as its first parameter"
        raise TypeError(msg)

    def run(value: object, inputs: Mapping[str, object]) -> CheckResult:
        returned = fn(value, inputs=inputs) if wants_inputs else fn(value)
        return _result(name, importance, returned)

    return Check(
        name=name,
        importance=importance,
        run=run,
        problem=_anything,
        identity=("function", fn),
        field=field_name,
        wants_inputs=wants_inputs,
    )


def _from_validator(validator: Validator, field_name: str | None) -> Check:
    cls = type(validator)
    name = validator.name or cls.__name__
    if inspect.iscoroutinefunction(validator.validate):
        msg = f"check {name} has an async validate; checks run synchronously"
        raise TypeError(msg)
    _checked_importance(validator.importance)
    importance = validator.importance

    def run(value: object, inputs: Mapping[str, object]) -> CheckResult:
        return _result(name, importance, validator.validate(value, inputs))

    def problem(declared: object) -> str | None:
        if validator.applies_to(declared):
            return None
        return f"check {name} does not apply to {inspect.formatannotation(declared)}"

    return Check(
        name=name,
        importance=importance,
        run=run,
        problem=problem,
        identity=("validator", validator),
        field=field_name,
        wants_inputs=True,
    )


def check_output_custom[F: Callable[..., object]](
    *checks: Callable[..., object] | Validator,
    importance: Importance = "fail",
    field: str | None = None,
) -> Callable[[F], F]:
    """Checks the node's value with custom checks (Q102).

    A check is a function or a `Validator`. A function takes the value, and an
    `inputs` parameter if it wants the node's arguments, and returns `True`,
    `None` or a passing `CheckResult` for a good value, or `False`, a message
    `str` or a failing `CheckResult` for a bad one; it is named after itself and
    has this decorator's `importance`. A `Validator` has its own. An `async def`
    check is refused. `field` checks an extracted field. Stacks in any order.
    """
    _checked_importance(importance)
    _checked_field(field)
    if not checks:
        msg = "check_output_custom needs at least one check"
        raise TypeError(msg)
    made: list[Check] = []
    for given in checks:
        if isinstance(given, Validator):
            made.append(_from_validator(given, field))
        elif callable(given):
            made.append(_from_function(given, importance, field))
        else:
            msg = (
                f"a check must be a function or a Validator, got {type(given).__name__}"
            )
            raise TypeError(msg)
    frozen = tuple(made)

    def apply(fn: F) -> F:
        return _record(fn, frozen)

    return apply


# ---- running the checks inside a node ------------------------------------------------

# A hooked run's collector: its driver's token, and each node's check results.
type Sink = tuple[object, dict[str, tuple[CheckResult, ...]]]

# Where a hooked run collects its nodes' check results, for its `after_node`.
RESULTS: contextvars.ContextVar[Sink | None] = contextvars.ContextVar(
    "fnloom_check_results", default=None
)


def problems(node: str, declared: object, checks: Sequence[Check]) -> list[str]:
    """Why each of `checks` can't read a node returning `declared`, one line each."""
    found = (check.problem(declared) for check in checks)
    return [f"{node}: {why}" for why in found if why is not None]


def _ran(check: Check, value: object, inputs: Mapping[str, object]) -> CheckResult:
    """`check`'s result; an error it raises fails the node, noting the check."""
    try:
        return check.run(value, inputs)
    except Exception as error:
        error.add_note(f"in check {check.name}")
        raise


def verdict(
    node: str,
    checks: Sequence[Check],
    value: object,
    inputs: Callable[[], Mapping[str, object]],
    owner: object = None,
) -> None:
    """Runs `checks` on `value`, logs each `warn` failure, raises the `fail` ones.

    Every result is handed to a hooked run's `after_node`, when that run is of
    the driver `owner` stands for. `inputs` makes the node's arguments, only
    when a check reads them.
    """
    if isinstance(value, collections.abc.Iterator):
        msg = (
            f"{node}: its value is a one-shot iterator, which its checks would consume"
        )
        raise TypeError(msg)
    if value is None and any(
        c.identity == ("builtin", "allow_none", True) for c in checks
    ):
        # `allow_none=True`: None passes every check.
        checks = [c for c in checks if c.name == "allow_none"]
    given = inputs() if any(c.wants_inputs for c in checks) else _NO_DIAGNOSTICS
    results = tuple(_ran(check, value, given) for check in checks)
    if (sink := RESULTS.get()) is not None and sink[0] is owner:
        sink[1][node] = results
    failed: list[CheckResult] = []
    for result in results:
        if result.passes:
            continue
        if result.importance == "warn":
            _LOG.warning("%s: check %s failed: %s", node, result.name, result.message)
        else:
            failed.append(result)
    if failed:
        raise ValidationError(node, failed)


def attached(nodes: Sequence[NodeDef]) -> list[NodeDef]:
    """`nodes`, each with the checks its function recorded for it (Q103).

    A function's own node gets its checks with no `field`; each extracted field's
    node the checks naming it. `ValueError` for a `field` the function doesn't
    extract, naming every culprit, sorted; `refuse_unreadable` checks the
    annotations once steps are lowered.
    """
    fields: dict[int, set[str]] = {}
    for node in nodes:
        if (extracting := extractor(node)) is not None:
            fields.setdefault(id(node.origin), set()).add(extracting.field)
    out: list[NodeDef] = []
    unknown: list[str] = []
    for node in nodes:
        recorded = decorations(node.origin).checks
        if not recorded:
            out.append(node)
            continue
        extracting = extractor(node)
        own = None if extracting is None else extracting.field
        mine = tuple(c for c in recorded if c.field == own)
        if extracting is None:
            produced = fields.get(id(node.origin), set())
            unknown.extend(
                f"{node.name}: check {c.name} names field {c.field!r}, "
                "which the function doesn't extract"
                for c in recorded
                if c.field is not None and c.field not in produced
            )
        out.append(replace(node, checks=mine) if mine else node)
    if unknown:
        raise ValueError("\n".join(sorted(set(unknown))))
    return out


def refuse_unreadable(nodes: Sequence[NodeDef]) -> None:
    """`TypeError` naming each check that can't read its node's return annotation.

    Run once steps are lowered, so a `@pipe_output` node's checks are read
    against its last step's type, which is what they check.
    """
    refused = [
        line
        for node in nodes
        for line in problems(node.name, node.returns, node.checks)
    ]
    refused += [
        f"{node.name}: its checks would consume a one-shot "
        f"{inspect.formatannotation(node.returns)}"
        for node in nodes
        if node.checks and _one_shot(node.returns)
    ]
    if refused:
        raise TypeError("\n".join(sorted(refused)))


def wrapped(
    nodes: Sequence[NodeDef], *, awaits: bool, owner: object = None
) -> list[NodeDef]:
    """`nodes`, each checked one's payload running its checks after it returns.

    A node with no checks is kept as it is. With `awaits`, for the async driver,
    a sync node's awaitable result is passed on unchecked, so the driver refuses
    it with its own guided error.
    """
    return [
        replace(node, payload=_checking(node, awaits=awaits, owner=owner))
        if node.checks
        else node
        for node in nodes
    ]


def _checking(node: NodeDef, *, awaits: bool, owner: object) -> Callable[..., object]:
    name, checks, payload = node.name, node.checks, node.payload
    keyed = by_parameter(node.params)

    def arguments(kwargs: Mapping[str, object]) -> Callable[[], Mapping[str, object]]:
        return lambda: MappingProxyType(keyed(kwargs) if keyed else dict(kwargs))

    if _async.awaits(node):
        awaited = cast("Callable[..., Awaitable[object]]", payload)

        async def call_async(**kwargs: object) -> object:
            value = await awaited(**kwargs)
            verdict(name, checks, value, arguments(kwargs), owner)
            return value

        return call_async

    def call(**kwargs: object) -> object:
        value = payload(**kwargs)
        if awaits and inspect.isawaitable(value):
            return value
        verdict(name, checks, value, arguments(kwargs), owner)
        return value

    return call
