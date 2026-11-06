"""Pydantic models as cache inputs: registered, or frozen and read by their fields.

fnloom never imports pydantic; these tests do, up front, as the shared fixture
forgets the modules a test imports first.
"""

import subprocess
import sys
import textwrap
from typing import TYPE_CHECKING

import pytest

from fnloom import UnfingerprintableError, fingerprint, register_fingerprint
from fnloom._codeversion import UncacheableError, class_version, code_version
from helpers import make_module
from test_hooks import Recorder
from test_hooks_cache import HIT, MISS, Cached, Kind, StoreKind

if TYPE_CHECKING:
    from pathlib import Path
    from types import ModuleType

pydantic = pytest.importorskip("pydantic")
# What the fixture modules import, loaded up front: a model class made after a
# test forgot `pydantic.main` would be another `BaseModel`'s.
_ = (pydantic.BaseModel, pydantic.ConfigDict, pydantic.Field, pydantic.PrivateAttr)
_ = (pydantic.field_validator, pydantic.AfterValidator, pydantic.RootModel)

MODELS = """
from pydantic import BaseModel, ConfigDict, Field


class Plain(BaseModel):
    x: int


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)
    x: int
    y: str = "a"


class Twin(BaseModel):
    model_config = ConfigDict(frozen=True)
    x: int
    y: str = "a"


class Holder(BaseModel):
    model_config = ConfigDict(frozen=True)
    inner: Frozen
    items: tuple[int, ...] = ()


class Open(BaseModel):
    model_config = ConfigDict(frozen=True, extra="allow")
    x: int
"""


def _models() -> ModuleType:
    return make_module("pydantic_models", MODELS)


def test_a_registration_applies_to_a_pydantic_model() -> None:
    """A registered model fingerprints by its form, frozen or not, subclasses too."""
    models = _models()
    with pytest.raises(UnfingerprintableError, match="Plain"):
        fingerprint(models.Plain(x=1))
    register_fingerprint(models.Plain, lambda m: m.model_dump_json())
    assert fingerprint(models.Plain(x=1)) == fingerprint(models.Plain(x=1))
    assert fingerprint(models.Plain(x=1)) != fingerprint(models.Plain(x=2))

    register_fingerprint(models.Frozen, lambda m: m.x)
    assert fingerprint(models.Frozen(x=1, y="a")) == fingerprint(models.Frozen(x=1))
    assert fingerprint(models.Frozen(x=1)) != fingerprint(models.Frozen(x=2))

    sub = make_module(
        "pydantic_sub",
        "from pydantic_models import Plain\nclass Sub(Plain):\n    pass\n",
    )
    assert fingerprint(sub.Sub(x=1)) != fingerprint(sub.Sub(x=2))
    assert fingerprint(sub.Sub(x=1)) != fingerprint(models.Plain(x=1))


def test_a_frozen_model_fingerprints_by_its_class_and_fields() -> None:
    """Equal fields under one class match; any other field, class or state differs."""
    models = _models()
    one = fingerprint(models.Frozen(x=1, y="a"))
    assert fingerprint(models.Frozen(x=1, y="a")) == one
    assert fingerprint(models.Frozen(x=2, y="a")) != one
    assert fingerprint(models.Frozen(x=1, y="b")) != one
    assert fingerprint(models.Twin(x=1, y="a")) != one
    # The fields set apart from their defaults show in `model_dump(exclude_unset=True)`.
    assert fingerprint(models.Frozen(x=1)) != one

    holder = fingerprint(models.Holder(inner=models.Frozen(x=1), items=(1, 2)))
    assert holder == fingerprint(models.Holder(inner=models.Frozen(x=1), items=(1, 2)))
    assert holder != fingerprint(models.Holder(inner=models.Frozen(x=2), items=(1, 2)))
    assert holder != fingerprint(models.Holder(inner=models.Frozen(x=1), items=(2, 1)))

    extra = fingerprint(models.Open(x=1, z=1))
    assert extra == fingerprint(models.Open(x=1, z=1))
    assert extra != fingerprint(models.Open(x=1, z=2))
    assert extra != fingerprint(models.Open(x=1))


def test_a_mutable_model_is_refused_unless_registered() -> None:
    """Without `frozen=True`, a model has no fingerprint of its own."""
    models = _models()
    with pytest.raises(UnfingerprintableError) as caught:
        fingerprint(models.Plain(x=1))
    assert caught.value.type_name == "Plain"
    # A frozen model holding a mutable one is refused at that field.
    holder = make_module(
        "pydantic_holder",
        """
        from pydantic import BaseModel, ConfigDict
        from pydantic_models import Plain

        class Holder(BaseModel):
            model_config = ConfigDict(frozen=True)
            plain: Plain
        """,
    )
    with pytest.raises(UnfingerprintableError) as caught:
        fingerprint(holder.Holder(plain=models.Plain(x=1)))
    assert caught.value.type_name == "Plain"


def test_a_model_built_without_a_field_is_refused() -> None:
    """`model_construct` may leave a field unset, which nothing could fingerprint."""
    models = _models()
    with pytest.raises(UnfingerprintableError):
        fingerprint(models.Frozen.model_construct(y="a"))


def test_fnloom_never_imports_pydantic() -> None:
    """Fingerprinting and code versions leave pydantic unimported."""
    code = textwrap.dedent(
        """
        import sys
        from dataclasses import dataclass
        import fnloom
        from fnloom._codeversion import code_version

        @dataclass(frozen=True)
        class Point:
            x: int

        fnloom.fingerprint((1, Point(2)))
        code_version(lambda: Point(1))
        print("pydantic" in sys.modules)
        """
    )
    result = subprocess.run(  # noqa: S603  # runs this interpreter on fixed code
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "False"


MODEL_TEMPLATE = """
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator


class Model(BaseModel):
    model_config = ConfigDict(frozen={frozen})
    x: int = Field({default}, ge={ge})
    _secret: int = PrivateAttr(default={private})

    def double(self):
        return self.x * {factor}

    @field_validator("x", mode="{mode}")
    @classmethod
    def check(cls, x):
        return x + {shift}


def f():
    return Model().double()
"""

_BASE = {
    "frozen": "True",
    "default": 1,
    "ge": 0,
    "private": 0,
    "factor": 2,
    "shift": 0,
    "mode": "after",
}


def _model_version(**edits: object) -> bytes:
    module = make_module("pydantic_versioned", MODEL_TEMPLATE.format(**_BASE | edits))
    return code_version(module.f)


@pytest.mark.parametrize(
    "edit",
    [
        {"frozen": "False"},
        {"default": 2},
        {"ge": 1},
        {"private": 1},
        {"factor": 3},
        {"shift": 1},
        {"mode": "before"},
    ],
    ids=[
        "config",
        "default",
        "constraint",
        "private default",
        "method",
        "validator",
        "validator mode",
    ],
)
def test_a_model_class_counts_its_body_fields_and_config(
    edit: dict[str, object],
) -> None:
    """A reader of a model class is versioned by everything its body declares."""
    one = _model_version()
    assert len(one) == 32  # noqa: PLR2004  # the digest size
    assert _model_version() == one
    assert _model_version(**edit) != one


@pytest.mark.parametrize(
    "template",
    [
        """
        class Inner(BaseModel):
            y: int

            @field_validator("y")
            @classmethod
            def scale(cls, y):
                return y * {v}

        class Model(BaseModel):
            inner: Inner
        """,
        """
        class Mode(enum.Enum):
            A = {v}

        class Model(BaseModel):
            mode: Mode
        """,
        """
        Alias = {t}

        class Model(BaseModel):
            x: Alias
        """,
        """
        class Generic_(BaseModel, Generic[T]):
            x: T

        Model = Generic_[{t}]
        """,
        """
        class Model(RootModel[{t}]):
            pass
        """,
        """
        class Model(BaseModel):
            x: Annotated[int, AfterValidator(lambda x: x * {v})]
        """,
        """
        class Model(BaseModel):
            x: int

            @classmethod
            def __pydantic_init_subclass__(cls, **kwargs):
                return {v}
        """,
    ],
    ids=["nested model", "enum", "alias", "generic", "root model", "annotated", "hook"],
)
def test_a_model_class_counts_the_types_it_validates_by(template: str) -> None:
    """A field's type is what pydantic validates by: the classes it names count."""
    header = """
        import enum
        from typing import Annotated, Generic, TypeVar
        from pydantic import AfterValidator, BaseModel, RootModel, field_validator

        T = TypeVar("T")
        """

    def version(v: int, t: str) -> bytes:
        body = textwrap.dedent(header) + textwrap.dedent(template.format(v=v, t=t))
        module = make_module("pydantic_typed", body + "\ndef f():\n    return Model\n")
        return code_version(module.f)

    one = version(1, "int")
    assert version(1, "int") == one
    assert version(2, "str") != one


@pytest.mark.parametrize(
    "body",
    [
        "class Model(BaseModel):\n    later: 'Later'\n",
        (
            "class Model(BaseModel):\n"
            "    model_config = ConfigDict(defer_build=True)\n"
            "    x: int\n"
        ),
    ],
    ids=["forward reference", "defer_build"],
)
def test_a_model_not_built_yet_is_refused_until_rebuilt(body: str) -> None:
    """An unbuilt model's types are unknown; the reason says what to do."""
    source = "from pydantic import BaseModel, ConfigDict\n"
    source += body + "class Later(BaseModel):\n    y: int\n"
    module = make_module("pydantic_unbuilt", source + "def f():\n    return Model\n")
    with pytest.raises(UncacheableError) as caught:
        code_version(module.f)
    assert caught.value.reason == (
        "model 'Model' is not built yet (defer_build or an unresolved forward"
        " reference); call Model.model_rebuild() first"
    )
    module.Model.model_rebuild()
    assert len(code_version(module.f)) == 32  # noqa: PLR2004  # the digest size


def test_a_hash_of_your_own_counts() -> None:
    """Only the `__hash__` pydantic makes is left out; one the body defines counts."""
    source = """
        from pydantic import BaseModel, ConfigDict

        class Model(BaseModel):
            model_config = ConfigDict(frozen=True)
            x: int

            def __hash__(self):
                return {v}

        def f():
            return hash(Model(x=1))
        """
    one = code_version(make_module("pydantic_hash", source.format(v=1)).f)
    assert code_version(make_module("pydantic_hash", source.format(v=2)).f) != one


def test_a_model_class_has_a_version() -> None:
    """A model's class version, which its values carry, is not refused."""
    models = _models()
    assert class_version(models.Frozen) == class_version(models.Frozen)
    assert class_version(models.Frozen) != class_version(models.Holder)


def test_model_config_values_follow_the_rules_for_defaults() -> None:
    """Config data counts by fingerprint; what cannot be fingerprinted is refused."""
    source = """
        from pydantic import BaseModel, ConfigDict

        SCHEMA = {schema}

        class Model(BaseModel):
            model_config = ConfigDict(json_schema_extra=SCHEMA)
            x: int

        def f():
            return Model(x=1)
        """
    one = code_version(
        make_module("pydantic_config", source.format(schema="{'t': 1}")).f
    )
    two = code_version(
        make_module("pydantic_config", source.format(schema="{'t': 2}")).f
    )
    assert one != two
    refused = make_module("pydantic_config", source.format(schema="{'t': object()}"))
    with pytest.raises(UncacheableError, match="model_config"):
        code_version(refused.f)


GRAPH = """
from typing import NewType

import fnloom
from pydantic import BaseModel, ConfigDict

Sym = NewType("Sym", str)


class Request(BaseModel):
    name: str


fnloom.register_fingerprint(Request, lambda r: r.model_dump_json())


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True)
    scale: int


def params(request: Request, settings: Settings) -> str:
    return Sym(request.name * settings.scale)
"""


@pytest.mark.parametrize("kind", ["sync", "async"])
@pytest.mark.parametrize("store", ["memory", "disk"])
def test_a_node_reading_models_and_a_newtype_hits(
    kind: Kind, store: StoreKind, tmp_path: Path
) -> None:
    """A registered model, a frozen one and a `NewType` leave a node cacheable."""
    hook = Recorder()
    cached = Cached(GRAPH, kind, store, tmp_path, hook)
    module = cached.module

    def inputs(name: str = "ab", scale: int = 2) -> dict[str, object]:
        return {
            "request": module.Request(name=name),
            "settings": module.Settings(scale=scale),
        }

    assert cached.explain(["params"], inputs=inputs()) == {"params": MISS}
    assert cached.execute(["params"], inputs=inputs()) == {"params": "abab"}
    assert cached.explain(["params"], inputs=inputs()) == {"params": HIT}
    hook.events.clear()
    assert cached.execute(["params"], inputs=inputs()) == {"params": "abab"}
    assert [e.cache for e in hook.events if hasattr(e, "cache")] == [HIT, HIT]
    assert cached.explain(["params"], inputs=inputs(name="b")) == {"params": MISS}
    assert cached.explain(["params"], inputs=inputs(scale=3)) == {"params": MISS}
