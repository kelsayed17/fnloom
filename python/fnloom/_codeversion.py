"""Code versions: a hash of everything a function's result depends on, read at build.

A code version hashes a function's code object (bytecode, constants and names, without
line numbers, file names or docstrings), its defaults, its closure cells and every
global name its code reads, resolved by the rules of the caching design (§4): user
code by its own code version, recursively; libraries by name and version; immutable
values by fingerprint. Anything else may change between runs unseen, so the function
is refused with an `UncacheableError` that says why.

Every part is tagged and framed by `_core.hash_parts`, and nothing hashed depends on
the process: no `id()`, no `hash()` of a string, no file path or line number.
"""

import abc
import builtins
import collections
import dataclasses
import dis
import enum
import functools
import importlib.metadata
import importlib.util
import inspect
import sys
import sysconfig
import types
import typing
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fnloom import _core
from fnloom._core import UnfingerprintableError
from fnloom._fingerprint import classes_versioned_by_caller, fingerprint

if TYPE_CHECKING:
    from collections.abc import Callable, Container, Iterator, Mapping, Sequence

_hash = _core.hash_parts

# The advice every refusal ends with.
_ADVICE = "give @cache(version=...)"

# Values that cannot change in place, by exact type; a subclass may add state.
_IMMUTABLE = frozenset({int, str, bytes, float, complex, bool, type(None)})

# The descriptors written in C that a class body may hold: slots, and a builtin type's
# methods reused by a subclass (an `IntEnum`'s `__format__`, say).
_C_DESCRIPTORS = (
    types.MemberDescriptorType,
    types.GetSetDescriptorType,
    types.WrapperDescriptorType,
    types.MethodDescriptorType,
    types.ClassMethodDescriptorType,
)

# Values the global rules read as code, walked or versioned by name.
_CODE = (types.FunctionType, type, types.ModuleType, types.BuiltinFunctionType)

# What `getattr` gives for a dataclass field never set.
_UNSET = object()

# A named tuple's field accessor, which holds the field's index and docstring.
_TUPLEGETTER: type = type(collections.namedtuple("_Probe", "field").field)  # noqa: PYI024  # probing the accessor type

# Closure cells of library wrappers that hold state never changing a result, by the
# wrapper's module and code name. `reprlib.recursive_repr` (on every dataclass's
# `__repr__`) keeps the objects being printed, to cut recursion; it is empty between
# calls.
_TRANSIENT_CELLS: dict[tuple[str, str], frozenset[str]] = {
    (
        "reprlib",
        "recursive_repr.<locals>.decorating_function.<locals>.wrapper",
    ): frozenset({"repr_running"}),
}

# Class body entries that Python generates on every class, left out of its version:
# the namespace, docstring, position and class cells, which never change a result;
# the evaluated-annotations cache, which `__annotate__` (counted) is the source of;
# and typing's type parameters and bases as written, `__bases__` being counted. A
# docstring or generic argument read by reflection at run time is a known limit.
_GENERATED = frozenset(
    {
        "__dict__",
        "__weakref__",
        "__doc__",
        "__module__",
        "__qualname__",
        "__firstlineno__",
        "__static_attributes__",
        "__classcell__",
        "__classdictcell__",
        "__annotations_cache__",
        "__type_params__",
        "__parameters__",
        "__orig_bases__",
    }
)

# Entries the standard library generates on its own kinds of class, left out only
# when the class is of that kind; elsewhere they are ordinary attributes.
_ANNOTATIONS = frozenset({"__annotate__", "__annotate_func__", "__annotations__"})
# abc: the abstract-method cache. typing: a protocol's member sets, from its body.
_ABC = frozenset({"_abc_impl"})
_PROTOCOL = frozenset({"__protocol_attrs__", "__non_callable_proto_members__"})
# dataclasses: the fields and flags the generated methods are made from; each
# field's metadata, which no generated method holds, counts on its own.
_DATACLASS = frozenset({"__dataclass_fields__", "__dataclass_params__"})
# collections.namedtuple: the defaults, which the generated `__new__` holds.
_NAMEDTUPLE = frozenset({"_field_defaults"})
# enum: lookup tables built from the members, which count one by one.
_ENUM = frozenset(
    {
        "_member_names_",
        "_member_map_",
        "_value2member_map_",
        "_hashable_values_",
        "_unhashable_values_",
        "_unhashable_values_map_",
    }
)


def _generated(cls: type) -> frozenset[str]:
    """The names in `cls`'s body that its kind of class generates."""
    names = _GENERATED | _ANNOTATIONS
    if isinstance(cls, abc.ABCMeta):
        names |= _ABC
    if _is_protocol(cls):
        names |= _PROTOCOL
    if _is_dataclass(cls):
        names |= _DATACLASS
    if _is_namedtuple(cls):
        names |= _NAMEDTUPLE
    if isinstance(cls, enum.EnumType):
        names |= _ENUM
    return names


def _is_namedtuple(cls: type) -> bool:
    """Whether `cls` itself was made by `collections.namedtuple`.

    Its tuple of `_fields` and the `_make` that namedtuple defines mark it; a
    `typing.NamedTuple` is made the same way.
    """
    entries = vars(cls)
    make = getattr(entries.get("_make"), "__func__", None)
    return bool(
        issubclass(cls, tuple)
        and isinstance(entries.get("_fields"), tuple)
        and isinstance(make, types.FunctionType)
        and make.__globals__.get("__name__") == "collections"
    )


def _is_protocol(cls: type) -> bool:
    """Whether `cls` is a protocol class: one deriving from `typing.Protocol`."""
    return typing.Protocol in cls.__mro__ and bool(getattr(cls, "_is_protocol", False))


def _is_dataclass(cls: type) -> bool:
    """Whether `cls` itself was made by `@dataclass`, its fields all `Field`s."""
    fields = vars(cls).get("__dataclass_fields__")
    return (
        dataclasses.is_dataclass(cls)
        and isinstance(fields, dict)
        and all(isinstance(field, dataclasses.Field) for field in fields.values())
    )


class UncacheableError(Exception):
    """A function whose result may depend on something its code version cannot see.

    Attributes:
        reason: What was refused, naming the function and the name it reads.
    """

    def __init__(self, reason: str) -> None:
        """Records `reason`, which is also the message."""
        super().__init__(reason)
        self.reason = reason


def code_version(fn: Callable[..., object]) -> bytes:
    """The 32-byte code version of `fn`.

    Raises:
        UncacheableError: `fn`, or user code it reads, depends on a value that may
            change between runs, such as a mutable global.
        TypeError: `fn` is not a plain Python function.
    """
    if not isinstance(fn, types.FunctionType):
        msg = f"a code version needs a function, not {_article(type(fn).__qualname__)}"
        raise TypeError(msg)
    return _settled(lambda walk: walk.function(fn))


def class_version(cls: type) -> bytes:
    """The 32-byte version of a user class: its body, bases and the code they reach.

    Raises:
        UncacheableError: the class, or code it reaches, holds state that may change.
    """
    name = cls.__qualname__
    return _settled(lambda walk: walk.value(cls, _Where("class", name, name)))


def _settled(read: Callable[[_Walk], bytes]) -> bytes:
    """What `read` gives from a walk whose names have settled.

    A module or function attribute may be read by any code the walk reaches, a helper
    that was handed the module say, so each is versioned by every name read anywhere.
    Walking with more names can reach more code, and so more names: walk again until
    the names settle.
    """
    reads = frozenset[str]()
    with classes_versioned_by_caller():
        while True:
            walk = _Walk(reads)
            digest = read(walk)
            if walk.seen <= reads:
                return digest
            reads |= walk.seen


def is_user_module(module: types.ModuleType) -> bool:
    """Whether `module` is the user's own code, walked rather than versioned by name.

    User code is neither built in, nor frozen, nor the standard library, nor installed:
    a module without a file (a notebook's, or one built in the process) or with a
    file outside every library path. fnloom itself counts as a library.
    """
    name = module.__name__
    if name in sys.builtin_module_names or _root(name) == "fnloom":
        return False
    # From the namespace, not `getattr`: a module's `__getattr__` would be asked.
    attributes = vars(module)
    spec = attributes.get("__spec__")
    if getattr(spec, "origin", None) in {"built-in", "frozen"}:
        return False
    file = attributes.get("__file__")
    if not isinstance(file, str):
        return True
    path = Path(file).resolve()
    return not any(path.is_relative_to(library) for library in _library_paths())


def distribution_version(module_name: str) -> str | None:
    """The version of the distribution that installs `module_name`'s top-level module.

    `None` for a module no distribution installs, such as the standard library's.
    Several distributions sharing a top-level name (a namespace package) give each
    one's name and version.
    """
    names = sorted(set(_distributions().get(_root(module_name), ())))
    versions = [
        f"{name}=={version}"
        for name in names
        if (version := _installed_version(name)) is not None
    ]
    if not versions:
        return None
    if len(versions) == 1:
        return versions[0].partition("==")[2]
    return ",".join(versions)


@functools.cache
def _distributions() -> Mapping[str, list[str]]:
    """Top-level module names to the distributions providing them, read once."""
    return importlib.metadata.packages_distributions()


@functools.cache
def _installed_version(distribution: str) -> str | None:
    """A distribution's version, read once per process; `None` if not installed."""
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return None


def _library_paths() -> tuple[Path, ...]:
    """The standard library's and the installed packages' directories, resolved.

    Besides the interpreter's own, every `site-packages` or `dist-packages` directory
    on `sys.path`: an overlay or user environment, or a `PYTHONPATH` entry, installs
    there too. A project installed in editable mode keeps its files in the project,
    so it is still the user's code, walked rather than versioned by name.
    """
    return _library_paths_for(tuple(sys.path))


@functools.cache
def _library_paths_for(entries: tuple[str, ...]) -> tuple[Path, ...]:
    """`_library_paths` for this `sys.path`."""
    paths = sysconfig.get_paths()
    keys = ("stdlib", "platstdlib", "purelib", "platlib")
    found = [Path(paths[key]) for key in keys if key in paths]
    found += [Path(e) for e in entries if Path(e).name in _INSTALL_DIRECTORIES]
    return tuple(dict.fromkeys(path.resolve() for path in found))


# The directory names installers put packages in.
_INSTALL_DIRECTORIES = frozenset({"site-packages", "dist-packages"})


def _root(module_name: str) -> str:
    """The top-level package of a dotted module name."""
    return module_name.partition(".")[0]


def _qualified(value: object) -> str:
    """`module.qualname` of a function or class, for cycle placeholders."""
    module = getattr(value, "__module__", None) or ""
    return f"{module}.{getattr(value, '__qualname__', '')}"


def _article(type_name: str) -> str:
    """`type_name` after its indefinite article: "a dict", "an object"."""
    return f"{'an' if type_name[:1].lower() in 'aeiou' else 'a'} {type_name}"


def _library(module_name: str, qualname: str) -> bytes:
    """A library object's part: its qualified name and its provider's version.

    The standard library and the builtins go by Python's version; fnloom, when no
    distribution names it, by its core's.
    """
    version = distribution_version(module_name)
    if version is None:
        version = (
            _core.core_version() if _root(module_name) == "fnloom" else sys.version
        )
    parts = [b"library", module_name.encode(), qualname.encode(), version.encode()]
    return _hash(parts)


def _is_immutable(value: object) -> bool:
    """A plain immutable value, an enum member, or a tuple or frozenset of these."""
    if type(value) in _IMMUTABLE or isinstance(value, enum.Enum):
        return True
    if type(value) in {tuple, frozenset}:
        return all(_is_immutable(item) for item in value)  # ty: ignore[not-iterable]
    return False


def _fingerprint(value: object, what: str) -> bytes:
    """`value`'s fingerprint, or an `UncacheableError` saying `what` was refused."""
    try:
        return fingerprint(value)
    except UnfingerprintableError as error:
        at = f" at {error.path}" if error.path else ""
        held = _article(error.type_name)
        msg = f"{what} cannot be fingerprinted ({held}{at}); {_ADVICE}"
        raise UncacheableError(msg) from error


def _instance_types(value: object, seen: set[int] | None = None) -> Iterator[type]:
    """The classes of the instances inside a value, as the fingerprint walks it.

    Built-in containers, named tuples, dataclass instances and enum members are
    looked into; every instance that is not of a built-in type gives its class,
    whose code (methods, a member's enum class) may run on the value.
    """
    seen = set() if seen is None else seen
    if id(value) in seen:
        return
    seen.add(id(value))
    if type(value).__module__ != "builtins":
        yield type(value)
    for item in _inner(value):
        yield from _instance_types(item, seen)


def _inner(value: object) -> Iterator[object]:
    """The values the fingerprint looks into inside `value`, if any."""
    if isinstance(value, enum.Enum):
        yield value.value
    elif isinstance(value, tuple | list | set | frozenset):
        yield from value
    elif type(value) is dict:
        for key, item in value.items():
            yield key
            yield item
    elif dataclasses.is_dataclass(value) and not isinstance(value, type):
        for field in dataclasses.fields(value):
            # An unset field (`init=False`, no default) has nothing to walk; the
            # fingerprint refuses such an instance.
            item = getattr(value, field.name, _UNSET)
            if item is not _UNSET:
                yield item


def _is_typeddict(cls: type) -> bool:
    """Whether `cls` is a typed dict, of `typing` or of `typing_extensions`.

    `typing_extensions` has its own on some Pythons; it is asked only if it is
    already imported, as a class made from it must have imported it.
    """
    if typing.is_typeddict(cls):
        return True
    extensions = sys.modules.get("typing_extensions")
    return extensions is not None and bool(extensions.is_typeddict(cls))


def _c_descriptor(descriptor: Any) -> bytes:  # noqa: ANN401  # one of _C_DESCRIPTORS
    """A C descriptor: a library type's by name and version, a user slot by name."""
    owner = descriptor.__objclass__
    name = f"{owner.__qualname__}.{descriptor.__name__}"
    module = sys.modules.get(owner.__module__)
    if module is not None and not is_user_module(module):
        return _library(owner.__module__, name)
    return _hash([b"slot", type(descriptor).__name__.encode(), name.encode()])


def _annotations(cls: type) -> bytes:
    """A class's annotations, which dataclasses and typed dicts read as fields.

    Lazy annotations count by their `__annotate__` code, never evaluated; eager
    ones (`from __future__ import annotations` keeps strings) by fingerprint.
    """
    owner = cls.__qualname__
    entries = vars(cls)
    annotate = entries.get("__annotate_func__", entries.get("__annotate__"))
    parts = [b"annotations"]
    if isinstance(annotate, types.FunctionType):
        parts.append(_annotate_code(annotate))
    elif annotate is not None:
        raise _Where("attribute", "__annotate__", owner).refuse(annotate)
    if "__annotations__" in entries:
        what = f"attribute '__annotations__' of {owner!r}"
        parts.append(_fingerprint(entries["__annotations__"], what))
    return _hash(parts)


def _annotate_code(
    annotate: types.FunctionType, seen: frozenset[int] = frozenset()
) -> bytes:
    """An `__annotate__` function's code, and that of the ones it wraps.

    A library may replace a class's own `__annotate__` with one that merges it with
    the bases' (`TypedDict` does), keeping the class's in a closure cell.
    """
    seen |= {id(annotate)}
    parts = [b"annotate", _code(annotate.__code__)]
    for cell in annotate.__closure__ or ():
        try:
            inner = cell.cell_contents
        except ValueError:  # not yet assigned
            continue
        if isinstance(inner, types.FunctionType) and id(inner) not in seen:
            parts.append(_annotate_code(inner, seen))
    return _hash(parts)


def _field_metadata(cls: type) -> bytes:
    """Each dataclass field's metadata and flags, read by reflection, by fingerprint."""
    fields = vars(cls)["__dataclass_fields__"]
    parts = [b"field-metadata"]
    for name, field in sorted(fields.items()):
        what = f"the metadata of field {name!r} of {cls.__qualname__!r}"
        flags = (field.hash, field.compare, field.repr, field.init, field.kw_only)
        form = (dict(field.metadata), *flags)
        parts.append(_hash([name.encode(), _fingerprint(form, what)]))
    return _hash(parts)


def _unbound(fn: types.BuiltinFunctionType) -> bool:
    """Whether a builtin is a plain function or a type's, not bound to an object."""
    return fn.__self__ is None or isinstance(fn.__self__, types.ModuleType | type)


def _sentinel(value: object) -> bytes | None:
    """A library object that its type's module names, as that library's part.

    Such an object is a marker its library compares by identity, not a value.
    """
    module = sys.modules.get(type(value).__module__)
    if module is None or is_user_module(module) or _is_immutable(value):
        return None
    for name, candidate in sorted(vars(module).items()):
        if candidate is value:
            return _library(module.__name__, name)
    return None


class _Where:
    """Where a value was read: its kind, its name and the function that reads it."""

    def __init__(self, kind: str, name: str, owner: str) -> None:
        self.kind = kind
        self.name = name
        self.owner = owner

    def refuse(self, value: object) -> UncacheableError:
        """The refusal of `value`, which may change between runs."""
        held = _article(type(value).__qualname__)
        return UncacheableError(
            f"{self.kind} {self.name!r} of {self.owner!r} is {held},"
            f" which may change; {_ADVICE}"
        )

    def within(self, attribute: str) -> _Where:
        """The same place, one attribute further in."""
        return _Where(self.kind, f"{self.name}.{attribute}", self.owner)


class _Walk:
    """One `code_version` call: versions memoised per object, cycles cut.

    An object whose version is in progress stands for itself by a placeholder of its
    qualified name, so mutual recursion terminates and still hashes the same.
    """

    def __init__(self, reads: frozenset[str] = frozenset()) -> None:
        """Starts a walk that versions attributes by `reads` and the reader's names."""
        # Keyed by `id`; each entry keeps its object alive for the walk.
        self._done: dict[int, tuple[object, bytes]] = {}
        self._busy: set[int] = set()
        # The names any code of the previous walk read, and those this walk reads.
        self._reads = reads
        self.seen: set[str] = set()

    def value(  # noqa: PLR0911  # one return per rule
        self, value: object, where: _Where, names: Sequence[str] = ()
    ) -> bytes:
        """The part for a value read by name, by the rules of spec §4.

        `names` are the names the reading code uses, which select the attributes of a
        user module it reads. A library object that its type's module names (a
        sentinel such as `dataclasses.MISSING`) counts as `library.name` would.
        """
        match value:
            case types.FunctionType():
                module = sys.modules.get(value.__globals__.get("__name__", ""))
                if module is not None and not is_user_module(module):
                    return self._library_function(value, module, where, names)
                return _hash([b"user-function", self.function(value)])
            case type():
                module = sys.modules.get(value.__module__)
                if module is not None and not is_user_module(module):
                    return _library(value.__module__, value.__qualname__)
                return _hash([b"user-class", self._class(value)])
            case types.ModuleType():
                if not is_user_module(value):
                    return _library(value.__name__, "")
                return self._module(value, where, names)
            case types.BuiltinFunctionType() if _unbound(value):
                return _library(value.__module__ or "builtins", value.__qualname__)
            case _:
                return self._object(value, where)

    def _object(self, value: object, where: _Where) -> bytes:
        """A value that is not code: a constant, a C descriptor or a sentinel.

        Anything else may change between runs, so it is refused.
        """
        if _is_immutable(value):
            return self._constant(value, where)
        if isinstance(value, _C_DESCRIPTORS):
            return _c_descriptor(value)
        if type(value) is _TUPLEGETTER:
            index, doc = value.__reduce__()[1]
            field = [
                b"tuple-field",
                str(index).encode(),
                _fingerprint(doc, "a named tuple field"),
            ]
            return _hash(field)
        sentinel = _sentinel(value)
        if sentinel is None:
            raise where.refuse(value)
        return _hash([b"sentinel", sentinel])

    def _constant(self, value: object, where: _Where) -> bytes:
        """An immutable value's fingerprint, and the classes of its enum members.

        A member's methods are code its reader may call, so a user enum class counts
        as it would read by name.
        """
        what = f"{where.kind} {where.name!r} of {where.owner!r}"
        # Classes first: a refused class says why, where its value's fingerprint
        # would only say that it cannot be taken.
        classes = {self.value(cls, where) for cls in _instance_types(value)}
        data = _fingerprint(value, what)
        return _hash([b"constant", data, *sorted(classes)])

    def _library_function(
        self,
        fn: types.FunctionType,
        module: types.ModuleType,
        where: _Where,
        names: Sequence[str],
    ) -> bytes:
        """A library function: its name and version, and the code it wraps.

        A library decorator (`contextlib.contextmanager`, `functools.singledispatch`)
        returns its own function, holding the user's in `__wrapped__` or a closure
        cell, so both are read by the global rules: user code is walked and state
        that may change (a dispatch registry) is refused.
        """
        # By its code's name: `functools.wraps` gives the wrapper the user's.
        qualname = fn.__code__.co_qualname

        def compute() -> bytes:
            parts = [b"library-function", _library(module.__name__, qualname)]
            wrapped = vars(fn).get("__wrapped__")
            if wrapped is not None:
                inner = where.within("__wrapped__")
                parts.append(_hash([b"wrapped", self.value(wrapped, inner, names)]))
            parts.append(self._defaults(fn, where.name))
            transient = _TRANSIENT_CELLS.get((module.__name__, qualname), ())
            parts.append(self._closure(fn, where.name, names, transient))
            return _hash(parts)

        return self._memo(fn, compute)

    def _memo(self, value: object, compute: Callable[[], bytes]) -> bytes:
        """`compute()` once per object, or a placeholder while it is in progress."""
        key = id(value)
        done = self._done.get(key)
        if done is not None:
            return done[1]
        if key in self._busy:
            return _hash([b"cycle", _qualified(value).encode()])
        self._busy.add(key)
        try:
            digest = compute()
        finally:
            self._busy.discard(key)
        self._done[key] = (value, digest)
        return digest

    def function(self, fn: types.FunctionType) -> bytes:
        """A user function's version: code, defaults, closure and globals read."""

        def compute() -> bytes:
            owner = fn.__qualname__
            names = _names(fn.__code__)
            self.seen.update(names)
            return _hash(
                [
                    b"function",
                    _code(fn.__code__),
                    self._defaults(fn, owner),
                    self._closure(fn, owner, names),
                    self._globals(fn, owner, names),
                    self._attributes(fn, owner, names),
                ]
            )

        return self._memo(fn, compute)

    def _attributes(
        self, fn: types.FunctionType, owner: str, names: Sequence[str]
    ) -> bytes:
        """The function's own attributes that code reads, by the global rules.

        Dunder attributes are Python's and fnloom's metadata (`__wrapped__`, the
        decorators' record), never state a result depends on.
        """
        parts = [b"attributes"]
        read = self._reads.union(names)
        for name, value in sorted(vars(fn).items()):
            if name not in read or (name.startswith("__") and name.endswith("__")):
                continue
            digest = self.value(value, _Where("attribute", name, owner), names)
            parts.append(_hash([name.encode(), digest]))
        return _hash(parts)

    def _defaults(self, fn: types.FunctionType, owner: str) -> bytes:
        """Defaults and keyword-only defaults, each with its parameter's name."""
        code = fn.__code__
        defaults = fn.__defaults__ or ()
        positional = code.co_varnames[: code.co_argcount]
        named = zip(
            positional[len(positional) - len(defaults) :], defaults, strict=True
        )
        keyword = sorted((fn.__kwdefaults__ or {}).items())
        parts = [b"defaults"]
        for kind, pairs in ((b"positional", named), (b"keyword-only", keyword)):
            parts.append(kind)
            for name, value in pairs:
                digest = self._default(value, _Where("default", name, owner))
                parts.append(_hash([name.encode(), digest]))
        return _hash(parts)

    def _default(self, value: object, where: _Where) -> bytes:
        """A default's part: code and constants by the global rules, data by value.

        Code (a function, class or module) and immutable values follow the global
        rules, and any refusal from the code they reach stands. Other data (a list,
        say) is fingerprinted, as spec §4 counts defaults, with the classes of the
        enum members inside it; data that cannot be fingerprinted goes to the global
        rules, which know sentinels and refuse the rest.
        """
        if _is_immutable(value) or isinstance(value, _CODE):
            return _hash([b"read", self.value(value, where)])
        # Classes first: a refused class says why, where its value's fingerprint
        # would only say that it cannot be taken.
        classes = {self.value(cls, where) for cls in _instance_types(value)}
        try:
            data = fingerprint(value)
        except UnfingerprintableError:
            return _hash([b"read", self.value(value, where)])
        return _hash([b"value", data, *sorted(classes)])

    def _closure(
        self,
        fn: types.FunctionType,
        owner: str,
        names: Sequence[str],
        transient: Container[str] = (),
    ) -> bytes:
        """Closure cells, each read like a global of its variable's name.

        Cells named in `transient` hold state that never changes a result.
        """
        parts = [b"closure"]
        cells = fn.__closure__ or ()
        for name, cell in zip(fn.__code__.co_freevars, cells, strict=True):
            if name in transient:
                continue
            try:
                contents = cell.cell_contents
            except ValueError:  # not yet assigned
                digest = _hash([b"empty"])
            else:
                where = _Where("closure variable", name, owner)
                digest = self.value(contents, where, names)
            parts.append(_hash([name.encode(), digest]))
        return _hash(parts)

    def _globals(
        self, fn: types.FunctionType, owner: str, names: Sequence[str]
    ) -> bytes:
        """Each name the code reads, resolved in its globals, its builtins or imports.

        A name found in neither is an attribute name, or fails at run time anyway.
        """
        scope = fn.__globals__
        builtin_scope = scope.get("__builtins__", builtins.__dict__)
        if isinstance(builtin_scope, types.ModuleType):
            builtin_scope = vars(builtin_scope)
        home = sys.modules.get(scope.get("__name__", ""))
        parts = [b"globals"]
        for name in names:
            where = _Where("global", name, owner)
            if name in scope:
                digest = _hash([b"global", self.value(scope[name], where, names)])
            elif name in builtin_scope:
                digest = _hash([b"builtin", _library("builtins", name)])
            elif (
                home is not None
                and (sub := self._submodule(home, name, where, names)) is not None
            ):
                digest = _hash([b"submodule", sub])
            else:
                continue
            parts.append(_hash([name.encode(), digest]))
        for name in _imports(fn.__code__, scope.get("__package__"), owner):
            digest = self._imported(name, owner, names)
            parts.append(_hash([b"import", name.encode(), digest]))
        return _hash(parts)

    def _imported(self, name: str, owner: str, names: Sequence[str]) -> bytes:
        """A module imported in the body: as a global, or by name if not loaded yet.

        A library not yet imported is known by its name alone; user code not yet
        imported has no code to read, so it is refused.
        """
        module = sys.modules.get(name)
        if module is not None:
            return self.value(module, _Where("import", name, owner), names)
        known = distribution_version(name) is not None
        if known or _root(name) in sys.stdlib_module_names:
            return _library(name, "")
        msg = (
            f"{owner!r} imports {name!r}, which is not imported yet,"
            f" so its code cannot be read; {_ADVICE}"
        )
        raise UncacheableError(msg)

    def _module(
        self, module: types.ModuleType, where: _Where, names: Sequence[str]
    ) -> bytes:
        """A user module: the parts of its attributes whose names the code reads."""

        def compute() -> bytes:
            parts = [b"user-module", module.__name__.encode()]
            attributes = vars(module)
            for name in sorted(self._reads.union(names)):
                inner = where.within(name)
                if name in attributes:
                    digest = self.value(attributes[name], inner, names)
                elif (sub := self._submodule(module, name, inner, names)) is not None:
                    digest = _hash([b"submodule", sub])
                elif "__getattr__" in attributes:
                    # A name the module lacks is its `__getattr__`'s to answer.
                    hook = self.value(attributes["__getattr__"], inner, names)
                    digest = _hash([b"getattr-hook", hook])
                else:
                    continue
                parts.append(_hash([name.encode(), digest]))
            return _hash(parts)

        # Not memoised: what is read of a module depends on the reading code. The
        # busy set alone cuts a module that reaches itself.
        key = id(module)
        if key in self._busy:
            return _hash([b"cycle", module.__name__.encode()])
        self._busy.add(key)
        try:
            return compute()
        finally:
            self._busy.discard(key)

    def _submodule(
        self,
        module: types.ModuleType,
        name: str,
        where: _Where,
        names: Sequence[str],
    ) -> bytes | None:
        """A user package's submodule `name` that is not an attribute, or `None`.

        A submodule already imported is read as an attribute would be; one that
        exists but is not imported yet has no code to read, so it is refused.
        """
        full = f"{module.__name__}.{name}"
        loaded = sys.modules.get(full)
        if loaded is not None:
            return self.value(loaded, where, names)
        # Not `hasattr`: a module's `__getattr__` would be asked, and may raise.
        if "__path__" not in vars(module):
            return None
        try:
            spec = importlib.util.find_spec(full)
        except ImportError, ValueError:
            spec = None
        if spec is None:
            return None
        msg = (
            f"{where.kind} {where.name!r} of {where.owner!r} is the submodule {full!r},"
            f" which is not imported yet, so its code cannot be read; {_ADVICE}"
        )
        raise UncacheableError(msg)

    def _class(self, cls: type) -> bytes:
        """A user class: its metaclass, its bases and every entry of its body.

        Entries follow the global rules, so a mutable class attribute is refused.
        Only the entries Python and the standard library generate from the body,
        which the body's own entries already count, are left out (`_GENERATED`).
        """

        def compute() -> bytes:
            owner = cls.__qualname__
            meta = self.value(type(cls), _Where("metaclass", owner, owner))
            bases = [
                self.value(base, _Where("base", owner, owner)) for base in cls.__bases__
            ]
            if _is_typeddict(cls):
                # Its `__bases__` is `(dict,)`: the typed dicts it extends, whose
                # keys it merges, are only in `__orig_bases__`, a generic one as
                # its alias (`B[int]`), which counts as its origin class.
                where = _Where("base", owner, owner)
                origins = (
                    typing.get_origin(b) or b
                    for b in vars(cls).get("__orig_bases__", ())
                )
                extended = [b for b in origins if isinstance(b, type)]
                bases.extend(self.value(base, where) for base in extended)
            parts = [b"class", meta, _hash([b"bases", *bases]), _annotations(cls)]
            if _is_dataclass(cls):
                parts.append(_field_metadata(cls))
            skipped = _generated(cls)
            for name, member in sorted(vars(cls).items()):
                if name in skipped:
                    continue
                digest = self._member(member, _Where("attribute", name, owner))
                parts.append(_hash([name.encode(), digest]))
            return _hash(parts)

        return self._memo(cls, compute)

    def _member(self, member: object, where: _Where) -> bytes:
        """A class body entry's part: functions unwrapped from their descriptors."""
        match member:
            case staticmethod() | classmethod():
                targets: list[object] = [member.__func__]
            case property():
                targets = [member.fget, member.fset, member.fdel]
            case functools.cached_property():
                targets = [member.func]
            case _:
                return self.value(member, where)
        none = _hash([b"none"])
        parts = [none if t is None else self.value(t, where) for t in targets]
        return _hash([b"descriptor", type(member).__name__.encode(), *parts])


def _codes(code: types.CodeType) -> Iterator[types.CodeType]:
    """`code` and the code objects nested in it, depth first, without annotations.

    Annotations never change what a function returns, and their names (`Optional`,
    say) would be read as globals.
    """
    yield code
    for const in code.co_consts:
        if isinstance(const, types.CodeType) and const.co_name != "__annotate__":
            yield from _codes(const)


def _names(code: types.CodeType) -> list[str]:
    """Every name `code` and its nested code read, sorted.

    A string constant that is an identifier counts too: `globals()["RATE"]` and
    `getattr(module, "RATE")` read `RATE` by it. A name built at run time cannot be
    seen, a known limit.
    """
    names: set[str] = set()
    for nested in _codes(code):
        names.update(nested.co_names)
        names.update(
            c for c in nested.co_consts if isinstance(c, str) and c.isidentifier()
        )
    return sorted(names)


def _imports(code: types.CodeType, package: object, owner: str) -> list[str]:
    """The absolute names of the modules `code` and its nested code import, sorted.

    In 3.14, an `IMPORT_NAME` follows its level and its fromlist, so the level is
    two instructions back, `EXTENDED_ARG` prefixes aside.
    """
    found: set[str] = set()
    for nested in _codes(code):
        instructions = [
            instruction
            for instruction in dis.get_instructions(nested)
            if instruction.opname != "EXTENDED_ARG"
        ]
        for index, instruction in enumerate(instructions):
            if instruction.opname != "IMPORT_NAME":
                continue
            name = instruction.argval
            level = _import_level(instructions, index)
            if level is None:
                msg = (
                    f"{owner!r} imports {name!r} in a way this Python's bytecode does"
                    f" not show, so its code cannot be read; {_ADVICE}"
                )
                raise UncacheableError(msg)
            if level > 0:
                if not isinstance(package, str) or not package:
                    msg = (
                        f"{owner!r} imports {name!r} relatively, outside a package,"
                        f" so its code cannot be read; {_ADVICE}"
                    )
                    raise UncacheableError(msg)
                name = importlib.util.resolve_name("." * level + name, package)
            found.add(name)
    return sorted(found)


def _import_level(instructions: list[dis.Instruction], index: int) -> int | None:
    """The level of the `IMPORT_NAME` at `index`, or `None` if the shape is unknown.

    In 3.14 the level is loaded two instructions back and the fromlist one back.
    """
    if index < 2:  # noqa: PLR2004  # the level and the fromlist
        return None
    level, fromlist = instructions[index - 2], instructions[index - 1]
    if level.opname not in _LOADS or fromlist.opname not in _LOADS:
        return None
    return level.argval if isinstance(level.argval, int) else None


# The instructions that load an import's level and fromlist.
_LOADS = frozenset({"LOAD_SMALL_INT", "LOAD_CONST"})


def _code(code: types.CodeType) -> bytes:
    """A code object's hash: instructions, the constants they load, and names.

    Constants are hashed where an instruction loads them, never by index: the
    compiler keeps unused constants (the docstring, a folded literal) in `co_consts`
    or not depending on the rest of the body, which moves the indices. The docstring
    flag is masked out; line tables and file names are never read.
    """
    return _hash(
        [
            b"code",
            _ints(
                code.co_flags & ~inspect.CO_HAS_DOCSTRING,
                code.co_argcount,
                code.co_posonlyargcount,
                code.co_kwonlyargcount,
            ),
            _instructions(code),
            code.co_exceptiontable,
            _strs(code.co_names),
            _strs(code.co_varnames),
            _strs(code.co_freevars),
            _strs(code.co_cellvars),
        ]
    )


def _instructions(code: types.CodeType) -> bytes:
    """The instructions: each opcode with its argument, or the constant it loads.

    `EXTENDED_ARG` prefixes are folded into the argument that follows them.
    """
    parts = [b"instructions"]
    for instruction in dis.get_instructions(code):
        if instruction.opname == "EXTENDED_ARG":
            continue
        opcode = instruction.opcode.to_bytes(2, "little")
        arg = instruction.arg or 0
        if instruction.opcode in dis.hasconst:
            parts.append(_hash([b"load", opcode, _const(code.co_consts[arg])]))
        else:
            parts.append(_hash([b"op", opcode, arg.to_bytes(4, "little")]))
    return _hash(parts)


def _const(const: object) -> bytes:  # noqa: PLR0911  # one return per kind
    """A code constant's hash: nested code recursed, other values fingerprinted.

    The compiler's constants that are not values (`...`, a slice) are tagged here,
    as are tuples and frozensets, which may hold them.
    """
    match const:
        case types.CodeType() if const.co_name == "__annotate__":
            return _hash([b"annotations"])
        case types.CodeType():
            return _code(const)
        case tuple():
            return _hash([b"tuple", *(_const(item) for item in const)])
        case frozenset():
            return _hash([b"frozenset", *sorted(_const(item) for item in const)])
        case slice():
            return _hash(
                [b"slice", _const(const.start), _const(const.stop), _const(const.step)]
            )
        case _ if const is Ellipsis:
            return _hash([b"ellipsis"])
        case _:
            return _hash([b"value", fingerprint(const)])


def _ints(*values: int) -> bytes:
    """Integers framed as decimal parts."""
    return _hash([str(value).encode() for value in values])


def _strs(values: Sequence[str]) -> bytes:
    """Strings framed as UTF-8 parts, in order."""
    return _hash([value.encode() for value in values])
