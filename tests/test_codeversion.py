"""Tests for code versions: what a function's result depends on, hashed at build."""

import dis
import importlib.metadata
import json
import os
import subprocess
import sys
import textwrap
import types
from typing import TYPE_CHECKING

import pytest

import fnloom
from fnloom import _codeversion
from fnloom._codeversion import (
    UncacheableError,
    code_version,
    distribution_version,
    is_user_module,
)
from helpers import make_module

if TYPE_CHECKING:
    from pathlib import Path

DIGEST_SIZE = 32


def _version(source: str, fn: str = "f", module: str = "cv_main") -> bytes:
    """The code version of function `fn` in a module built from `source`."""
    return code_version(getattr(make_module(module, source), fn))


def test_editing_the_body_changes_it() -> None:
    """A different body is a different version; the same body, the same version."""
    one = _version("def f(x):\n    return x + 1\n")
    assert len(one) == DIGEST_SIZE
    assert _version("def f(x):\n    return x + 1\n") == one
    assert _version("def f(x):\n    return x + 2\n") != one
    assert _version("def f(x):\n    return x - 1\n") != one
    assert _version("def f(y):\n    return y + 1\n") != one


def test_formatting_comments_and_docstrings_do_not() -> None:
    """Layout, comments, line numbers and docstrings leave the version alone."""
    plain = _version("def f(x):\n    return x + 1\n")
    edited = """
        # A comment above.


        def f(x):
            '''A docstring, which does not change what f returns.'''
            # A comment inside.
            return (
                x+1  # and one at the end
            )
        """
    assert _version(textwrap.dedent(edited)) == plain
    assert _version("\n\n\ndef f(x):\n    '''Other.'''\n    return x + 1\n") == plain


def test_a_helper_it_calls_counts() -> None:
    """Editing a helper, in the same or another user module, changes the caller."""
    make_module("cv_other", "def f(x):\n    return x * 2\n")
    main = """
        import cv_other

        def helper(x):
            return x + {step}

        def out(x):
            return cv_other.f(helper(x))
        """
    before = _version(textwrap.dedent(main.format(step=1)), "out")
    assert _version(textwrap.dedent(main.format(step=1)), "out") == before
    assert _version(textwrap.dedent(main.format(step=2)), "out") != before

    make_module("cv_other", "def f(x):\n    return x * 3\n")
    assert _version(textwrap.dedent(main.format(step=1)), "out") != before


def test_mutual_recursion_terminates() -> None:
    """Helpers that call each other are walked once each, and still count."""
    source = """
        def even(n):
            return n == 0 or odd(n - 1)

        def odd(n):
            return n != 0 and even(n - {step})

        def out(n):
            return even(n)
        """
    before = _version(textwrap.dedent(source.format(step=1)), "out")
    assert _version(textwrap.dedent(source.format(step=1)), "out") == before
    assert _version(textwrap.dedent(source.format(step=2)), "out") != before


@pytest.mark.parametrize(
    "template",
    [
        "K = {v}\ndef f(x):\n    return x + K\n",
        "def f(x: int = {v}) -> int:\n    return x\n",
        "def f(*, x: int = {v}) -> int:\n    return x\n",
        (
            "def make(k):\n    def f(x):\n        return x + k\n    return f\n"
            "f = make({v})\n"
        ),
        "K = ({v}, 'a', None)\ndef f(x):\n    return K\n",
    ],
    ids=["global", "default", "keyword-only default", "closure", "tuple global"],
)
def test_constants_and_defaults_count(template: str) -> None:
    """A constant global, a default or a closure cell's value is part of it."""
    one = _version(template.format(v=1))
    assert _version(template.format(v=1)) == one
    assert _version(template.format(v=2)) != one


def test_library_versions_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """The standard library counts by Python's version, a distribution by its own."""
    stdlib = "import json\ndef f(x):\n    return json.dumps(x)\n"
    before = _version(stdlib)
    monkeypatch.setattr(sys, "version", sys.version + " (patched)")
    assert _version(stdlib) != before

    installed = "import pytest\ndef f(x):\n    return pytest.approx(x)\n"
    monkeypatch.setattr(_codeversion, "distribution_version", lambda _: "1.0")
    one = _version(installed)
    assert _version(installed) == one
    monkeypatch.setattr(_codeversion, "distribution_version", lambda _: "2.0")
    assert _version(installed) != one


def test_distribution_version() -> None:
    """A top-level module maps to its distribution's version; the stdlib to none."""
    assert distribution_version("pytest") == importlib.metadata.version("pytest")
    assert distribution_version("_pytest.python") == importlib.metadata.version(
        "pytest"
    )
    assert distribution_version("json") is None
    assert distribution_version("no_such_module_anywhere") is None


def test_is_user_module(tmp_path: Path) -> None:
    """User code is neither the standard library, nor installed, nor fnloom."""
    assert is_user_module(make_module("cv_user", "X = 1\n"))
    in_project = types.ModuleType("cv_project")
    in_project.__file__ = str(tmp_path / "cv_project.py")
    assert is_user_module(in_project)
    assert not is_user_module(json)
    assert not is_user_module(sys)
    assert not is_user_module(pytest)
    assert not is_user_module(fnloom)
    assert not is_user_module(_codeversion)


@pytest.mark.parametrize(
    ("value", "kind"),
    [
        ("[1, 2]", "a list"),
        ("{'a': 1}", "a dict"),
        ("{1, 2}", "a set"),
        ("object()", "an object"),
        ("(1, [2])", "a tuple"),
    ],
)
def test_mutable_globals_are_refused(value: str, kind: str) -> None:
    """A global that may change between runs makes the function uncacheable."""
    source = f"RATES = {value}\ndef price(x):\n    return RATES\n"
    with pytest.raises(UncacheableError) as caught:
        _version(source, "price")
    assert caught.value.reason == (
        f"global 'RATES' of 'price' is {kind}, which may change;"
        " give @cache(version=...)"
    )
    assert str(caught.value) == caught.value.reason


def test_a_mutable_global_read_by_a_helper_is_refused() -> None:
    """The reason names the function that reads the global, wherever it is."""
    source = (
        "RATES = {}\ndef helper():\n    return RATES\ndef f():\n    return helper()\n"
    )
    with pytest.raises(UncacheableError, match="global 'RATES' of 'helper'"):
        _version(source)


def test_an_unfingerprintable_default_is_refused() -> None:
    """A default that cannot be fingerprinted makes the function uncacheable."""
    with pytest.raises(UncacheableError, match="default 'x' of 'f'"):
        _version("def f(x=object()):\n    return x\n")


def test_immutable_globals_are_accepted() -> None:
    """Enum members, frozensets and nested tuples of plain values are constants."""
    source = """
        import enum

        class Mode(enum.Enum):
            FAST = 1
            SLOW = 2

        K = frozenset({{1, 2}})
        P = (1, ("a", b"b", 2.5, None, True))
        M = Mode.{mode}

        def f(x):
            return (x, K, P, M)
        """
    fast = _version(textwrap.dedent(source.format(mode="FAST")))
    assert _version(textwrap.dedent(source.format(mode="SLOW"))) != fast


@pytest.mark.parametrize(
    "template",
    [
        "def f(x):\n    return (lambda y: y + {v})(x)\n",
        "def f(xs):\n    return [i * {v} for i in xs]\n",
        "def f(xs):\n    return sum(i * {v} for i in xs)\n",
        "def f(x):\n    def g(y):\n        return y + {v}\n    return g(x)\n",
    ],
    ids=["lambda", "list comprehension", "generator", "nested def"],
)
def test_nested_code_counts(template: str) -> None:
    """Editing a lambda, comprehension or nested function changes it."""
    assert _version(template.format(v=1)) != _version(template.format(v=2))


def test_a_user_class_counts_through_its_methods() -> None:
    """Editing a method of a class the function uses changes the function."""
    source = """
        class Model:
            def __init__(self, k):
                self.k = k

            def predict(self, x):
                return x * self.k + {v}

            @staticmethod
            def make():
                return Model(2)

        def f(x):
            return Model.make().predict(x)
        """
    one = _version(textwrap.dedent(source.format(v=1)))
    assert _version(textwrap.dedent(source.format(v=1))) == one
    assert _version(textwrap.dedent(source.format(v=2))) != one


def test_imports_inside_the_body_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """A module imported in the body counts as a global of that name would."""
    make_module("cv_other", "def g(x):\n    return x + 1\n")
    source = "def f(x):\n    import cv_other\n    return cv_other.g(x)\n"
    before = _version(source)
    make_module("cv_other", "def g(x):\n    return x + 2\n")
    assert _version(source) != before

    stdlib = "def f(x):\n    import json\n    return json.dumps(x)\n"
    before = _version(stdlib)
    monkeypatch.setattr(sys, "version", sys.version + " (patched)")
    assert _version(stdlib) != before


def test_importing_an_unknown_module_in_the_body_is_refused() -> None:
    """A user module not yet imported has no code to read, so it is refused."""
    source = "def f(x):\n    import cv_not_yet\n    return cv_not_yet.g(x)\n"
    with pytest.raises(UncacheableError, match="'f' imports 'cv_not_yet'"):
        _version(source)


def test_relative_imports_inside_the_body_count() -> None:
    """A relative import in the body resolves against the function's package."""
    make_module("cv_pkg", "")
    make_module("cv_pkg.helpers", "def g(x):\n    return x + 1\n")
    source = (
        "__package__ = 'cv_pkg'\n"
        "def f(x):\n    from .helpers import g\n    return g(x)\n"
    )
    before = _version(source, module="cv_pkg.main")
    make_module("cv_pkg.helpers", "def g(x):\n    return x + 2\n")
    assert _version(source, module="cv_pkg.main") != before


def test_library_classes_and_sentinels_are_accepted() -> None:
    """Dataclasses, named tuples and enums, whose generated code reads sentinels."""
    source = """
        import dataclasses
        import enum
        import typing

        @dataclasses.dataclass
        class Point:
            xs: list = dataclasses.field(default_factory=list)
            y: int = 3

            def total(self):
                return sum(self.xs) + self.y + {v}

        class Pair(typing.NamedTuple):
            a: int
            b: str = "x"

        class Mode(enum.Enum):
            FAST = 1

        def f():
            return Point().total(), Pair(1), Mode.FAST
        """
    one = _version(textwrap.dedent(source.format(v=1)))
    assert _version(textwrap.dedent(source.format(v=2))) != one


def test_only_functions_have_code_versions() -> None:
    """A callable that is not a plain function is a type error, not a refusal."""
    with pytest.raises(TypeError, match="needs a function, not a builtin"):
        code_version(len)


# Builds a module from fixed source, with sets, helpers and a library call.
_SEEDED = """
import json, sys, textwrap, types
from fnloom._codeversion import code_version
source = textwrap.dedent('''
    import json
    NAMES = frozenset({"alpha", "beta", "gamma", "delta"})
    def helper(x):
        return x in {"a", "b", "c", "d"}
    def f(x):
        return [helper(x), x in NAMES, json.dumps(x)]
''')
module = types.ModuleType("cv_seeded")
sys.modules["cv_seeded"] = module
exec(compile(source, "cv_seeded", "exec"), module.__dict__)
print(code_version(module.f).hex())
"""


def _version_in_subprocess(seed: str) -> str:
    """The hex code version of `_SEEDED`'s `f`, taken by a fresh interpreter."""
    result = subprocess.run(  # noqa: S603  # runs this interpreter on fixed code
        [sys.executable, "-c", _SEEDED],
        capture_output=True,
        check=True,
        text=True,
        env={**os.environ, "PYTHONHASHSEED": seed},
    )
    return result.stdout.strip()


def test_stable_across_processes() -> None:
    """The same source gives the same version under different hash seeds."""
    first = _version_in_subprocess("0")
    assert len(first) == 2 * DIGEST_SIZE
    assert _version_in_subprocess("1") == first
    assert _version_in_subprocess("12345") == first


def test_a_library_decorator_counts_the_function_it_wraps() -> None:
    """A user function behind `contextmanager` still counts for its readers."""
    source = """
        import contextlib

        @contextlib.contextmanager
        def h():
            yield {v}

        def f():
            with h() as v:
                return v
        """
    one = _version(textwrap.dedent(source.format(v=1)))
    assert _version(textwrap.dedent(source.format(v=2))) != one


def test_a_library_decorator_with_state_is_refused() -> None:
    """`singledispatch` keeps a registry that may change, so its reader is refused."""
    source = """
        import functools

        @functools.singledispatch
        def h(x):
            return 1

        def f():
            return h(1)
        """
    with pytest.raises(UncacheableError, match="closure variable"):
        _version(textwrap.dedent(source))


@pytest.mark.parametrize(
    ("attribute", "kind"),
    [
        ("RATES = {'a': 1}", "a dict"),
        ("K = decimal.Decimal('1.1')", "a Decimal"),
        ("g = functools.partialmethod(lambda self, k: k, 1)", "a partialmethod"),
    ],
)
def test_class_attributes_follow_the_global_rules(attribute: str, kind: str) -> None:
    """A class attribute that may change makes a reader of the class uncacheable."""
    source = f"""
        import decimal
        import functools

        class Model:
            {attribute}

        def f():
            return Model()
        """
    name = attribute.partition(" ")[0]
    with pytest.raises(UncacheableError) as caught:
        _version(textwrap.dedent(source))
    assert caught.value.reason.startswith(f"attribute {name!r} of 'Model' is {kind},")


@pytest.mark.parametrize(
    "template",
    [
        "class Model:\n    K = {v}\n",
        "class Model:\n    __match_args__ = ('a', 'b' * {v})\n",
        "class Model:\n    def __add__(self, other):\n        return {v}\n",
        "class Base:\n    K = {v}\nclass Model(Base):\n    pass\n",
        (
            "class Meta(type):\n    def __call__(cls):\n        return {v}\n"
            "class Model(metaclass=Meta):\n    pass\n"
        ),
    ],
    ids=["constant", "dunder data", "dunder method", "base", "metaclass"],
)
def test_a_class_counts_its_attributes_bases_and_metaclass(template: str) -> None:
    """Editing any entry of a class body, a base or the metaclass changes readers."""
    reader = "def f():\n    return Model()\n"
    one = _version(template.format(v=1) + reader)
    assert _version(template.format(v=1) + reader) == one
    assert _version(template.format(v=2) + reader) != one


def test_an_enum_member_counts_its_class() -> None:
    """A member read by name brings its class's methods with it."""
    source = """
        import enum

        class C(enum.Enum):
            A = 1

            def lab(self):
                return {v}

        D = C.A

        def f():
            return D.lab()
        """
    one = _version(textwrap.dedent(source.format(v=1)))
    assert _version(textwrap.dedent(source.format(v=2))) != one


def _package(tmp_path: Path, name: str) -> None:
    """A user package `name` whose `helpers` submodule exists but is not imported."""
    (tmp_path / "helpers.py").write_text("def g():\n    return 1\n")
    make_module(name, f"__path__ = [{str(tmp_path)!r}]\n")


@pytest.mark.parametrize(
    "source",
    [
        (
            "__package__ = 'cv_upkg'\ndef f():\n    from . import helpers\n"
            "    return helpers.g()\n"
        ),
        "import cv_upkg\ndef f():\n    return cv_upkg.helpers.g()\n",
    ],
    ids=["relative import", "attribute"],
)
def test_a_submodule_not_yet_imported_is_refused(tmp_path: Path, source: str) -> None:
    """A submodule that exists but is not loaded has no code to read at build."""
    _package(tmp_path, "cv_upkg")
    with pytest.raises(UncacheableError, match=r"submodule 'cv_upkg\.helpers'"):
        _version(source, module="cv_upkg.main")


def test_a_package_global_naming_an_unloaded_submodule_is_refused(
    tmp_path: Path,
) -> None:
    """A package's own function reading its submodule by bare name is refused too."""
    _package(tmp_path, "cv_upkg")
    package = sys.modules["cv_upkg"]
    source = "def f():\n    return helpers.g()\n"
    exec(compile(source, "cv_upkg", "exec"), vars(package))  # noqa: S102  # a fixture function
    with pytest.raises(UncacheableError, match=r"submodule 'cv_upkg\.helpers'"):
        code_version(package.f)


def test_a_loaded_submodule_counts() -> None:
    """A submodule imported but not set on its package is read like an attribute."""
    make_module("cv_upkg", "")
    source = "import cv_upkg\ndef f():\n    return cv_upkg.helpers.g()\n"
    make_module("cv_upkg.helpers", "def g():\n    return 1\n")
    one = _version(source, module="cv_main")
    make_module("cv_upkg.helpers", "def g():\n    return 2\n")
    assert _version(source, module="cv_main") != one


def test_defaults_follow_the_global_rules() -> None:
    """A library function as a default counts by name; a user one by its code."""
    assert len(_version("import json\ndef f(x, key=json.dumps):\n    return x\n")) == (
        DIGEST_SIZE
    )
    template = "def h():\n    return {v}\ndef f(key=h):\n    return key()\n"
    assert _version(template.format(v=1)) != _version(template.format(v=2))


def test_an_unknown_import_shape_is_refused() -> None:
    """An `IMPORT_NAME` not preceded by its level and fromlist has no known level."""

    def imports() -> None:
        import json  # noqa: F401, PLC0415  # the bytecode under test

    def reads(x: int) -> int:
        return x

    instructions = list(dis.get_instructions(imports))
    index = next(i for i, op in enumerate(instructions) if op.opname == "IMPORT_NAME")
    assert _codeversion._import_level(instructions, index) == 0  # noqa: SLF001
    load = next(op for op in dis.get_instructions(reads) if "LOAD_FAST" in op.opname)
    instructions[index - 2] = load
    assert _codeversion._import_level(instructions, index) is None  # noqa: SLF001
    assert _codeversion._import_level(instructions[index:], 0) is None  # noqa: SLF001


_REFUSABLE_ENUM = """
import enum

TABLE = {{}}

class C(enum.Enum):
    A = 1

    def lab(self):
        return TABLE

def f({params}):
    return m
"""


@pytest.mark.parametrize("params", ["m=C.A", "*, m=(C.A,)", "m=[C.A]"])
def test_a_refusal_reached_through_a_default_stands(params: str) -> None:
    """A default's enum class that reads a mutable global refuses the function."""
    with pytest.raises(UncacheableError, match=r"global 'TABLE' of 'C\.lab'"):
        _version(_REFUSABLE_ENUM.format(params=params))


@pytest.mark.parametrize("params", ["m=C.A", "*, m=(C.A,)", "m=[C.A]"])
def test_a_default_enum_member_counts_its_class(params: str) -> None:
    """Editing a method of a default's enum class changes the function."""
    template = (
        "import enum\nclass C(enum.Enum):\n    A = 1\n"
        "    def lab(self):\n        return {v}\n"
        f"def f({params}):\n    return m\n"
    )
    assert _version(template.format(v=1)) != _version(template.format(v=2))


def test_a_library_function_counts_its_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A library wrapper keeping the user's function in a default counts it."""
    make_module(
        "cv_lib",
        "def wrap(fn):\n    def inner(x, _fn=fn):\n        return _fn(x)\n"
        "    return inner\n",
    )
    real = _codeversion.is_user_module
    monkeypatch.setattr(
        _codeversion, "is_user_module", lambda m: m.__name__ != "cv_lib" and real(m)
    )
    template = (
        "import cv_lib\ndef g(x):\n    return x + {v}\nh = cv_lib.wrap(g)\n"
        "def f(x):\n    return h(x)\n"
    )
    assert _version(template.format(v=1)) != _version(template.format(v=2))


@pytest.mark.parametrize(
    "template",
    [
        "@dataclasses.dataclass\nclass Model:\n    x: {t}\n",
        "class Model(typing.TypedDict):\n    x: {t}\n",
        "class Model:\n    x: {t}\n",
        (
            "@dataclasses.dataclass\nclass Model:\n"
            "    x: int = dataclasses.field(default=0, metadata={{'unit': '{t}'}})\n"
        ),
        "class Model:\n    __annotations__ = {{'x': '{t}'}}\n",
    ],
    ids=["dataclass", "typed dict", "plain class", "field metadata", "hand-written"],
)
def test_annotations_and_field_metadata_count(template: str) -> None:
    """A field's type or metadata, which reflection reads, is part of the class."""
    header = "import dataclasses\nimport typing\n"
    reader = "def f():\n    return Model\n"
    one = _version(header + template.format(t="int") + reader)
    assert _version(header + template.format(t="float") + reader) != one


def test_unfingerprintable_field_metadata_is_refused() -> None:
    """Metadata that cannot be fingerprinted makes a reader of the class refused."""
    source = """
        import dataclasses

        @dataclasses.dataclass
        class Model:
            x: int = dataclasses.field(default=0, metadata={"k": object()})

        def f():
            return Model
        """
    with pytest.raises(UncacheableError, match="metadata of field 'x' of 'Model'"):
        _version(textwrap.dedent(source))


@pytest.mark.parametrize(
    "name",
    ["_field_defaults", "_member_map_", "__dataclass_fields__", "_abc_impl"],
)
def test_generated_names_on_other_classes_are_attributes(name: str) -> None:
    """A name a library generates is an ordinary attribute on any other class."""
    source = f"class Model:\n    {name} = {{'a': 1}}\ndef f():\n    return Model\n"
    with pytest.raises(UncacheableError, match=f"attribute '{name}' of 'Model'"):
        _version(source)


# A user enum class `C` and a record type `P` holding its member, as a default.
_RECORD_DEFAULT = """
import collections
import dataclasses
import enum
{table}
class C(enum.Enum):
    A = 1

    def lab(self):
        return {body}

{record}

def f(m=P(C.A)):
    return m
"""

_RECORDS = {
    "namedtuple": "P = collections.namedtuple('P', 'm')",
    "frozen dataclass": "@dataclasses.dataclass(frozen=True)\nclass P:\n    m: object",
}


@pytest.mark.parametrize("record", _RECORDS.values(), ids=_RECORDS.keys())
def test_classes_inside_a_default_record_count(record: str) -> None:
    """An enum member inside a named tuple or dataclass default brings its class."""

    def version(body: str) -> bytes:
        return _version(_RECORD_DEFAULT.format(table="", body=body, record=record))

    assert version("1") != version("2")


@pytest.mark.parametrize("record", _RECORDS.values(), ids=_RECORDS.keys())
def test_a_refusal_inside_a_default_record_stands(record: str) -> None:
    """A refusable class reached inside a default record refuses the function."""
    source = _RECORD_DEFAULT.format(table="TABLE = {}", body="TABLE", record=record)
    with pytest.raises(UncacheableError, match=r"global 'TABLE' of 'C\.lab'"):
        _version(source)


def test_a_default_record_counts_its_own_class() -> None:
    """The methods of a user dataclass used as a default count."""
    template = """
        import dataclasses

        @dataclasses.dataclass(frozen=True)
        class P:
            m: int = 0

            def lab(self):
                return {v}

        def f(p=P()):
            return p.lab()
        """
    one = _version(textwrap.dedent(template.format(v=1)))
    assert _version(textwrap.dedent(template.format(v=2))) != one


def test_a_typed_dict_counts_the_typed_dicts_it_extends() -> None:
    """A typed dict's bases are typed dicts merged in, not its `__bases__`."""
    template = """
        import typing

        class B(typing.TypedDict):
            y: {t}

        class M(B):
            x: int

        def f():
            return typing.get_type_hints(M)
        """
    one = _version(textwrap.dedent(template.format(t="int")))
    assert _version(textwrap.dedent(template.format(t="str"))) != one


@pytest.mark.parametrize(
    "base",
    [
        "T = typing.TypeVar('T')\nclass B(typing.TypedDict, typing.Generic[T]):",
        "class B[T](typing.TypedDict):",
    ],
    ids=["Generic", "type parameters"],
)
def test_a_typed_dict_counts_the_generic_typed_dicts_it_extends(base: str) -> None:
    """A generic typed dict extended as `B[int]` counts as `B`."""
    template = (
        f"import typing\n{base}\n    y: {{t}}\n"
        "class M(B[int]):\n    x: int\n"
        "def f():\n    return typing.get_type_hints(M)\n"
    )
    one = _version(template.format(t="int"))
    assert _version(template.format(t="str")) != one


def test_a_typing_extensions_typed_dict_counts_its_bases() -> None:
    """A typed dict made by `typing_extensions` is known as one too."""
    pytest.importorskip("typing_extensions")
    template = (
        "import typing, typing_extensions\n"
        "class B(typing_extensions.TypedDict):\n    y: {t}\n"
        "class M(B):\n    x: int\n"
        "def f():\n    return typing.get_type_hints(M)\n"
    )
    one = _version(template.format(t="int"))
    assert _version(template.format(t="str")) != one


_UNSET_FIELD = """
import dataclasses

@dataclasses.dataclass(frozen=True)
class P:
    a: int
    b: int = dataclasses.field(init=False)

{use}
"""


@pytest.mark.parametrize(
    "use",
    ["def f(p=P(1)):\n    return p.a", "K = P(1)\ndef f():\n    return K.a"],
    ids=["default", "global"],
)
def test_a_record_with_an_unset_field_is_refused(use: str) -> None:
    """A dataclass instance whose `init=False` field was never set is refused."""
    with pytest.raises(UncacheableError):
        _version(_UNSET_FIELD.format(use=use))


@pytest.mark.parametrize(
    "flag", ["hash=True", "compare=False", "repr=False", "init=False", "kw_only=True"]
)
def test_field_flags_count(flag: str) -> None:
    """A dataclass field's flags, read by reflection, are part of the class."""
    template = (
        "import dataclasses\n@dataclasses.dataclass\nclass Model:\n"
        "    x: int = dataclasses.field(default=0{flag})\n"
        "def f():\n    return dataclasses.fields(Model)\n"
    )
    assert _version(template.format(flag="")) != _version(
        template.format(flag=", " + flag)
    )


@pytest.mark.parametrize(
    ("body", "name"),
    [
        ("_is_protocol = True\n    __protocol_attrs__ = {'a'}", "__protocol_attrs__"),
        ("_fields = ('a',)\n    _field_defaults = {'a': 1}", "_field_defaults"),
    ],
    ids=["fake protocol", "fake named tuple"],
)
def test_hand_written_kind_markers_do_not_hide_attributes(body: str, name: str) -> None:
    """A class that only looks like a protocol or named tuple gets the normal rules."""
    source = f"class Model(tuple):\n    {body}\ndef f():\n    return Model\n"
    with pytest.raises(UncacheableError, match=f"attribute '{name}' of 'Model'"):
        _version(source)
