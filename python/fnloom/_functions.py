"""Graphs from functions: `fnloom.node`, and the checks `Builder.with_functions` makes.

A function given without a module is used as it is: never copied, and no module is
made for it or registered in `sys.modules` (Q72). Its node's module is the
function's own `__module__`, and pickling finds it again by `__module__` and
`__qualname__`.
"""

import annotationlib
import functools
import importlib
import inspect
import sys
import types
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Final, override

from fnloom._decorators import Binding, Group, Source, Value, decorations, group_check


@dataclass(frozen=True, slots=True, eq=False)
class NodeSpec:
    """One node made from a function, named and bound without decorating it.

    Made by `fnloom.node`, or directly, given to `Builder.with_functions`. Made
    either way, it is checked as `node` describes, and `bind` is copied and kept
    read-only, so changing the mapping given has no effect. Two specs are the
    same only when they are one object. A spec pickles as its parts: the function
    by its module and qualified name, the name, and the bindings.

    Attributes:
        fn: The function, as written: never copied or changed.
        name: The node's name, an identifier.
        bind: Each bound parameter's `source(...)`, `value(...)` or `group(...)`.
    """

    fn: Callable[..., object]
    name: str
    bind: Mapping[str, Binding] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Checks the parts, as `node` describes; freezes a copy of `bind`."""
        _check_spec(self.fn, self.name, self.bind)
        object.__setattr__(self, "bind", MappingProxyType(dict(self.bind)))

    @override
    def __reduce__(
        self,
    ) -> tuple[
        Callable[..., NodeSpec],
        tuple[tuple[str, str], str, dict[str, Binding]],
    ]:
        """The function's reference, the name and the bindings; see the class."""
        if (problem := unimportable(self.fn)) is not None:
            msg = f"cannot pickle: {problem}"
            raise TypeError(msg)
        return _spec_again, (reference(self.fn), self.name, dict(self.bind))


# A node source `Builder.with_functions` keeps: a function, or a `NodeSpec`.
type FunctionSource = Callable[..., object] | NodeSpec

# What a pickled builder holds for a function source: the function's reference, or
# the spec itself, which pickles as its parts.
type FunctionReference = tuple[str, str] | NodeSpec


def _shown(fn: Callable[..., object]) -> str:
    """`fn` as an error names it: `module.qualname`."""
    module = getattr(fn, "__module__", None)
    qualname = getattr(fn, "__qualname__", None) or getattr(fn, "__name__", "?")
    return f"{module}.{qualname}" if module else str(qualname)


def _not_a_function(item: object) -> str | None:  # noqa: PLR0911  # one return per kind of item
    """Why `item` cannot make a node, when it is not a Python function, or `None`.

    Only a function defined with `def` or `lambda` is one: it has the module,
    qualified name, annotations and code a node's checks, code version and pickling
    read. A `functools.partial` is refused for `bind=`, which binds the same values
    without hiding the function; a callable instance or a built-in, for a `def`.
    """
    if isinstance(item, types.FunctionType):
        return None
    if isinstance(item, NodeSpec):
        return "a NodeSpec is given to with_functions, not to node()"
    if isinstance(item, types.ModuleType):
        return f"module {item.__name__!r} is not a function: give it to with_modules"
    if isinstance(item, functools.partial):
        return (
            f"a functools.partial of {_shown(item.func)!r} is not a function: give "
            "the function itself with bind={'parameter': value(...)}"
        )
    if isinstance(item, type):
        return f"class {item.__qualname__!r} is not a function"
    if callable(item):
        return (
            f"a callable {type(item).__qualname__} is not a function: wrap it in a def"
        )
    return f"expected a function or fnloom.node(...), got {type(item).__name__}"


def _usable_name(fn: Callable[..., object]) -> bool:
    """Whether `fn.__name__` can name a node: an identifier, so not `<lambda>`."""
    name = getattr(fn, "__name__", None)
    return isinstance(name, str) and name.isidentifier()


def _decoration_problems(fn: Callable[..., object], *, spec: bool) -> list[str]:
    """What `fn`'s decorators forbid: `@exclude`, and for a spec, several nodes.

    A spec names one node, so a `@parameterize` function, which makes a node per
    row, and a `@config` variant, which its decorator names, are given to
    `with_functions` as they are instead.
    """
    recorded = decorations(fn)
    problems: list[str] = []
    if recorded.excluded:
        problems.append(
            f"function {_shown(fn)!r} is marked @exclude: remove the mark "
            "to make it a node"
        )
    if spec and recorded.rows is not None:
        problems.append(
            f"node() makes one node, but function {_shown(fn)!r} is parameterized: "
            "give it to with_functions as it is"
        )
    if spec and recorded.config is not None:
        problems.append(
            f"node() names its node, but function {_shown(fn)!r} is a @config "
            "variant, named by its decorator: give it to with_functions as it is"
        )
    return problems


def _binding_problems(
    fn: Callable[..., object], bind: Mapping[Any, object]
) -> tuple[list[str], list[str]]:
    """What is wrong with `bind` for `fn`, as `@parameterize` checks a row.

    Returns the wrong-type lines and the unknown-parameter lines, each in one line
    listing every culprit, sorted.
    """
    wrong = sorted(
        (repr(param), type(binding).__name__)
        for param, binding in bind.items()
        if not isinstance(binding, Source | Value | Group)
    )
    type_lines: list[str] = []
    if wrong:
        listed = ", ".join(f"parameter {p} ({t})" for p, t in wrong)
        type_lines.append(
            f"expected source(...), value(...) or group(...) for: {listed}"
        )
    # Names only: annotations are evaluated at build, not here.
    forward = annotationlib.Format.FORWARDREF
    params = set(inspect.signature(fn, annotation_format=forward).parameters)
    unknown = sorted(repr(param) for param in bind if param not in params)
    value_lines: list[str] = []
    if not wrong and not unknown:
        try:
            group_check(fn, {"": bind})
        except TypeError as error:
            type_lines.append(str(error))
    if unknown:
        listed = ", ".join(f"parameter {p}" for p in unknown)
        value_lines.append(
            f"the function does not have the parameters named by: {listed}"
        )
    return type_lines, value_lines


def _raise(type_lines: Sequence[str], value_lines: Sequence[str]) -> None:
    """One error listing every line, sorted: a `TypeError` if any is a type's."""
    lines = sorted(dict.fromkeys([*type_lines, *value_lines]))
    if not lines:
        return
    if type_lines:
        raise TypeError("; ".join(lines))
    raise ValueError("; ".join(lines))


def node(
    fn: Callable[..., object],
    *,
    name: str | None = None,
    bind: Mapping[str, Binding] | None = None,
) -> NodeSpec:
    """One node from `fn`, named and bound without decorating it (Q72).

    The node is named `name`, or `fn.__name__`, which must be an identifier, so a
    dotted name, which namespaces use, is refused, and a lambda needs `name=`.
    `bind` maps parameters to `source(...)`, `value(...)` or `group(...)`, as a
    `@parameterize` row does, and is checked the same way. `fn` is not changed, so
    one function can make several nodes, each with its own spec, and its other
    decorations, such as `@tag` and `@cache`, apply.

    Give the result to `Builder.with_functions`.

    Raises:
        TypeError: `fn` is not a Python function (a `functools.partial`, a callable
            instance, a class), `name` is not a `str`, `bind` is not a mapping or
            binds something other than `source` or `value`.
        ValueError: `fn` needs a name, `name` is no identifier, `bind` names a parameter
            `fn` does not have, or `fn` is marked `@exclude`, parameterized or a
            `@config` variant. Every problem is listed in one message, sorted.
    """
    chosen = name if name is not None else getattr(fn, "__name__", "")
    return NodeSpec(fn, chosen, {} if bind is None else bind)


def _check_spec(fn: object, name: object, bind: object) -> None:
    """One error naming every problem with a spec's parts; see `node`."""
    type_lines: list[str] = []
    value_lines: list[str] = []
    if (problem := _not_a_function(fn)) is not None:
        type_lines.append(problem)
    if not isinstance(name, str):
        type_lines.append(f"node name must be a str, got {type(name).__name__}")
    if not isinstance(bind, Mapping):
        type_lines.append(f"bind must be a mapping, got {type(bind).__name__}")
    if isinstance(fn, types.FunctionType):
        if isinstance(name, str) and not name.isidentifier():
            if name == fn.__name__:
                value_lines.append(
                    f"function {_shown(fn)!r} has no name a node can take: "
                    "give it one with node(fn, name=...)"
                )
            else:
                value_lines.append(f"node name {name!r} is not an identifier")
        value_lines.extend(_decoration_problems(fn, spec=True))
        if isinstance(bind, Mapping):
            wrong, unknown = _binding_problems(fn, bind)
            type_lines.extend(wrong)
            value_lines.extend(unknown)
    _raise(type_lines, value_lines)


def checked_functions(items: Sequence[object]) -> tuple[FunctionSource, ...]:
    """`items` as node sources; one error naming every item that is not one.

    An item is a Python function, named by its `__name__`, or a `NodeSpec`. A
    lambda, or a function whose name is no identifier, needs `fnloom.node(fn,
    name=...)`, and a function marked `@exclude` is refused rather than dropped:
    it was given on purpose. The error is a `TypeError` when some item is no
    function at all, else a `ValueError`; every line is sorted, each once.
    """
    type_lines: list[str] = []
    value_lines: list[str] = []
    checked: list[FunctionSource] = []
    for item in items:
        if isinstance(item, NodeSpec):
            value_lines.extend(_decoration_problems(item.fn, spec=True))
            checked.append(item)
            continue
        if not isinstance(item, types.FunctionType):
            type_lines.append(_not_a_function(item) or f"{item!r} is not a function")
            continue
        fn = item
        if not _usable_name(fn):
            value_lines.append(
                f"function {_shown(fn)!r} has no name a node can take: "
                "give it one with fnloom.node(fn, name=...)"
            )
        value_lines.extend(_decoration_problems(fn, spec=False))
        checked.append(fn)
    _raise(type_lines, value_lines)
    return tuple(checked)


def function_of(item: FunctionSource) -> Callable[..., object]:
    """The function behind a node source."""
    return item.fn if isinstance(item, NodeSpec) else item


# What `getattr` gives for a name that is not there.
_ABSENT: Final = object()


def unimportable(fn: Callable[..., object]) -> str | None:
    """Why `fn` cannot be found again by `__module__` and `__qualname__`, or `None`.

    It is found when its module is in `sys.modules` and walking the qualified name
    from it reaches `fn` itself: so not a function defined inside another, a
    lambda, or one whose module now holds another object under its name.
    """
    module = sys.modules.get(str(getattr(fn, "__module__", None)))
    found: object = module if module is not None else _ABSENT
    for part in str(getattr(fn, "__qualname__", "")).split("."):
        if found is _ABSENT:
            break
        found = getattr(found, part, _ABSENT)
    if found is fn:
        return None
    return (
        f"function {_shown(fn)!r} is not importable by its qualified name, "
        "so it cannot be loaded again"
    )


def reference(fn: Callable[..., object]) -> tuple[str, str]:
    """`fn`'s module and qualified name, which `resolved` finds it again by."""
    return str(getattr(fn, "__module__", "")), str(getattr(fn, "__qualname__", ""))


def resolved(ref: tuple[str, str]) -> Callable[..., object]:
    """The function a `reference` names, imported again, as pickle finds one.

    Whatever the name holds when loading is used: a function rebound to another
    since pickling is the new one. `TypeError: cannot load function 'm.q': ...`
    when the module does not import, the name is not there, or it holds no
    function.
    """
    module, qualname = ref
    shown = f"{module}.{qualname}"
    try:
        found: object = importlib.import_module(module)
    except ImportError as error:
        msg = f"cannot load function {shown!r}: module {module!r} does not import"
        raise TypeError(msg) from error
    for part in qualname.split("."):
        found = getattr(found, part, _ABSENT)
        if found is _ABSENT:
            msg = f"cannot load function {shown!r}: {module!r} has no {qualname!r}"
            raise TypeError(msg)
    if not isinstance(found, types.FunctionType):
        msg = f"cannot load function {shown!r}: {_not_a_function(found)}"
        raise TypeError(msg)
    return found


def _spec_again(
    ref: tuple[str, str], name: str, bind: Mapping[str, Binding]
) -> NodeSpec:
    """A pickled `NodeSpec`, its function imported again and checked anew."""
    return node(resolved(ref), name=name, bind=bind)


def references(items: Sequence[FunctionSource]) -> tuple[FunctionReference, ...]:
    """How a pickled builder holds `items`: a function by reference, a spec as is."""
    return tuple(
        item if isinstance(item, NodeSpec) else reference(item) for item in items
    )


def from_references(refs: Sequence[FunctionReference]) -> tuple[FunctionSource, ...]:
    """The node sources a pickled builder held, each function imported again."""
    return tuple(ref if isinstance(ref, NodeSpec) else resolved(ref) for ref in refs)
