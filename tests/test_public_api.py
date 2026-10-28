"""The public names: every callable among them has a signature that resolves."""

import inspect

import pytest

import fnloom


def _public_callables() -> list[tuple[str, object]]:
    """Every callable in `fnloom.__all__`, and each public method of a class there.

    An exception class is left out, with its methods: CPython gives no signature for
    one that does not define `__init__`, as for the built-in exceptions.
    """
    found: list[tuple[str, object]] = []
    for name in fnloom.__all__:
        value = getattr(fnloom, name)
        if isinstance(value, type) and issubclass(value, BaseException):
            continue
        if callable(value):
            found.append((name, value))
        if isinstance(value, type):
            found.extend(
                (f"{name}.{member}", attribute)
                for member, attribute in vars(value).items()
                if not member.startswith("_") and callable(attribute)
            )
    return found


@pytest.mark.parametrize(
    ("name", "value"), _public_callables(), ids=[n for n, _ in _public_callables()]
)
def test_signature_resolves(name: str, value: object) -> None:
    """`inspect.signature` evaluates every annotation without a `NameError`."""
    signature = inspect.signature(value, eval_str=True)  # ty: ignore[invalid-argument-type]
    assert name
    for parameter in signature.parameters.values():
        assert not isinstance(parameter.annotation, str)
    assert not isinstance(signature.return_annotation, str)


def test_the_regressed_callables_are_covered() -> None:
    """The callables 0.1.0 failed on are among those tested."""
    names = {name for name, _ in _public_callables()}
    assert names >= {
        "build",
        "Builder",
        "Builder.with_modules",
        "Builder.with_cache",
        "Driver",
        "Driver.execute",
        "Driver.prepare",
        "Driver.explain_cache",
        "PreparedPlan",
        "PreparedPlan.run",
        "NodeView",
        "DiskCache",
    }
