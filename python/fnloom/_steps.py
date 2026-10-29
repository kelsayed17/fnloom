"""Step injection: `step`, `@pipe_input` and `@pipe_output` (Q104 to Q106, Q108).

A step is a plain function run on a value before a node reads it (`@pipe_input`)
or after a node returns it (`@pipe_output`). Each step lowers to an ordinary
node, named `<node>.with_<step>`, so it is typed, checked, cached and hooked like
any other. A step's first parameter takes the value; the rest are bound with
`source`, `value` or a bare literal, and a defaulted one left unbound keeps its
default: it is never a graph input (Q105).
"""

import annotationlib
import inspect
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import TYPE_CHECKING

from fnloom._config import Condition, ConfigPolicy, build_conditions, matches
from fnloom._decorators import _ATTRIBUTE, MUTATES, Source, Value, decorations
from fnloom._extract import extractor
from fnloom._scan import NodeDef, _payload
from fnloom._types import opaque, resolved_hints, type_matches
from fnloom._view import Param

if TYPE_CHECKING:
    from types import ModuleType

    from fnloom._config import ConditionKind


# Annotations as written: a name imported only under TYPE_CHECKING is no error.
_FORWARD = annotationlib.Format.FORWARDREF

_NO_TAGS = MappingProxyType[str, str]({})


@dataclass(frozen=True, slots=True)
class Step:
    """One step: a function and its bindings, as `step(...)` made it.

    `bindings` bind its parameters after the first, by name. `label` replaces the
    `with_<function>` part of its node's name, from `.named`. `conditions` are
    the config conditions it runs under, all of which must hold; `fields` the
    extracted fields it applies to, from `.on_output`, or `None` for all.
    """

    fn: Callable[..., object]
    bindings: Mapping[str, Source | Value]
    label: str | None = None
    conditions: tuple[Condition, ...] = ()
    fields: tuple[str, ...] | None = None

    def named(self, name: str) -> Step:
        """This step, its node named `<node>.<name>` rather than `<node>.with_<fn>`."""
        if not isinstance(name, str) or not name.isidentifier():
            msg = f"a step's name must be an identifier, got {name!r}"
            raise ValueError(msg)
        return replace(self, label=name)

    def _gated(self, kind: ConditionKind, keys: Mapping[str, object]) -> Step:
        added = build_conditions(kind, None, keys, None).conditions
        return replace(self, conditions=(*self.conditions, *added))

    def when(self, **keys: object) -> Step:
        """This step, run only when every key equals its value, as `config.when`."""
        return self._gated("when", keys)

    def when_not(self, **keys: object) -> Step:
        """This step, run only when no key equals its value, as `config.when_not`."""
        return self._gated("when_not", keys)

    def when_in(self, **keys: object) -> Step:
        """This step, run only when each key is one of its values."""
        return self._gated("when_in", keys)

    def when_not_in(self, **keys: object) -> Step:
        """This step, run only when no key is one of its values."""
        return self._gated("when_not_in", keys)

    def on_output(self, *fields: str) -> Step:
        """This step, on these extracted fields only, for `@pipe_output`."""
        if not fields or not all(isinstance(f, str) for f in fields):
            msg = "on_output takes one or more field names"
            raise TypeError(msg)
        return replace(self, fields=tuple(fields))


def _named(fn: Callable[..., object]) -> str:
    found = getattr(fn, "__qualname__", None)
    return found if isinstance(found, str) else type(fn).__name__


def step(fn: Callable[..., object], *args: object, **bindings: object) -> Step:
    """A step running `fn` on a value, its other parameters bound (Q104).

    `fn`'s first parameter takes the value. `args` bind the parameters after it
    in order, and `bindings` by name; each is a `source("node")`, a `value(v)`, or
    a bare literal, read as `value`. A defaulted parameter left unbound keeps its
    default (Q105). `TypeError` for a function that can't be a step: an
    `async def`, no parameters, `*args` or `**kwargs`, a missing annotation, an
    unknown binding, or a required parameter left unbound.
    """
    if not callable(fn):
        msg = f"step takes a function, got {type(fn).__name__}"
        raise TypeError(msg)
    name = _named(fn)
    if inspect.iscoroutinefunction(fn):
        msg = f"step {name}: an async def can't be a step"
        raise TypeError(msg)
    try:
        signature = inspect.signature(fn, annotation_format=_FORWARD)
    except TypeError, ValueError:
        msg = f"step {name}: it has no signature to call it by"
        raise TypeError(msg) from None
    params = list(signature.parameters.values())
    if not params:
        msg = f"step {name}: it has no parameter to take the value"
        raise TypeError(msg)
    variadic = [
        p.name
        for p in params
        if p.kind in {p.VAR_POSITIONAL, p.VAR_KEYWORD}
        or (p.kind == p.POSITIONAL_ONLY and p is not params[0])
    ]
    if params[0].kind == params[0].POSITIONAL_ONLY or variadic:
        variadic = variadic or [params[0].name]
        listed = ", ".join(variadic)
        msg = (
            f"step {name}: *args, **kwargs and positional-only can't be bound: {listed}"
        )
        raise TypeError(msg)
    unannotated = [p.name for p in params if p.annotation is inspect.Parameter.empty]
    if signature.return_annotation is inspect.Signature.empty:
        unannotated.append("return")
    if unannotated:
        listed = ", ".join(unannotated)
        msg = f"step {name}: every parameter and the return need annotations: {listed}"
        raise TypeError(msg)
    return Step(fn, MappingProxyType(_bound(name, params[1:], args, bindings)))


def _bound(
    name: str,
    rest: Sequence[inspect.Parameter],
    args: Sequence[object],
    bindings: Mapping[str, object],
) -> dict[str, Source | Value]:
    """A step's bindings by parameter: each a `Source` or a `Value`; see `step`."""
    if len(args) > len(rest):
        msg = f"step {name}: {len(args)} values given for {len(rest)} parameters"
        raise TypeError(msg)
    bound: dict[str, object] = {p.name: a for p, a in zip(rest, args, strict=False)}
    twice = sorted(set(bound) & set(bindings))
    if twice:
        msg = f"step {name}: bound twice: {', '.join(twice)}"
        raise TypeError(msg)
    bound.update(bindings)
    names = {p.name for p in rest}
    if unknown := sorted(set(bound) - names):
        msg = f"step {name}: no parameter named {', '.join(unknown)}"
        raise TypeError(msg)
    missing = [
        p.name
        for p in rest
        if p.default is inspect.Parameter.empty and p.name not in bound
    ]
    if missing:
        msg = f"step {name}: unbound required parameters: {', '.join(missing)}"
        raise TypeError(msg)
    return {
        key: given if isinstance(given, (Source, Value)) else Value(given)
        for key, given in bound.items()
    }


@dataclass(frozen=True, slots=True)
class PipeInput:
    """What `@pipe_input` recorded: its steps and the parameter they feed."""

    steps: tuple[Step, ...]
    on_input: str | None


def _steps(given: Sequence[object], decorator: str) -> tuple[Step, ...]:
    if not given:
        msg = f"{decorator} needs at least one step"
        raise TypeError(msg)
    wrong = [type(s).__name__ for s in given if not isinstance(s, Step)]
    if wrong:
        msg = f"{decorator} takes step(...)s, got {', '.join(wrong)}"
        raise TypeError(msg)
    return tuple(s for s in given if isinstance(s, Step))


def pipe_input[F: Callable[..., object]](
    *steps: Step, on_input: str | None = None
) -> Callable[[F], F]:
    """Runs `steps` on a parameter's value before the function reads it (Q104).

    The steps feed the parameter `on_input`, by default the first one: the first
    step takes what the parameter would have read, and the function reads the
    last step. `TypeError` for no steps, a function with no parameters, or an
    `on_input` naming none of its parameters.
    """
    recorded = _steps(steps, "pipe_input")
    if on_input is not None and not isinstance(on_input, str):
        msg = f"on_input must be one parameter's name, got {on_input!r}"
        raise TypeError(msg)

    def apply(fn: F) -> F:
        current = decorations(fn)
        if current.pipe_input is not None:
            msg = "function already has a @pipe_input; give all its steps to one"
            raise ValueError(msg)
        params = list(inspect.signature(fn, annotation_format=_FORWARD).parameters)
        if not params:
            msg = f"pipe_input on {_named(fn)}: it has no parameter to feed"
            raise TypeError(msg)
        if on_input is not None and on_input not in params:
            msg = f"pipe_input on {_named(fn)}: no parameter named {on_input!r}"
            raise TypeError(msg)
        policy = PipeInput(recorded, on_input)
        setattr(fn, _ATTRIBUTE, replace(current, pipe_input=policy))
        return fn

    return apply


def pipe_output[F: Callable[..., object]](*steps: Step) -> Callable[[F], F]:
    """Runs `steps` on the function's value: the node named after it is the last's.

    The function itself runs as `<node>.raw`. On a function with extracted
    fields, the steps apply to each field, or to those `.on_output` names.
    `TypeError` for no steps.
    """
    recorded = _steps(steps, "pipe_output")

    def apply(fn: F) -> F:
        current = decorations(fn)
        if current.pipe_output:
            msg = "function already has a @pipe_output; give all its steps to one"
            raise ValueError(msg)
        setattr(fn, _ATTRIBUTE, replace(current, pipe_output=recorded))
        return fn

    return apply


# ---- lowering ---------------------------------------------------------------------


def _labels(steps: Sequence[Step]) -> list[str]:
    """Each step's label: `with_<fn>`, numbered past the first of a name (Q104).

    Every step written is counted, whether the config keeps it or not, so a
    name never changes with the config.
    """
    seen: dict[str, int] = {}
    labels: list[str] = []
    for s in steps:
        if s.label is not None:
            labels.append(s.label)
            continue
        fn_name = getattr(s.fn, "__name__", "step")
        base = f"with{fn_name}" if fn_name.startswith("_") else f"with_{fn_name}"
        count = seen.get(base, 0)
        seen[base] = count + 1
        labels.append(base if count == 0 else f"{base}_{count}")
    return labels


def _kept(
    steps: Sequence[Step], config: Mapping[str, object]
) -> list[tuple[Step, str]]:
    """The steps the config keeps, each with its label."""
    return [
        (s, label)
        for s, label in zip(steps, _labels(steps), strict=True)
        if not s.conditions or matches(ConfigPolicy(s.conditions, False, None), config)  # noqa: FBT003  # not a default variant
    ]


def _step_node(
    s: Step, name: str, reads: str, owner: NodeDef, names: Mapping[str, str]
) -> NodeDef:
    """`s` as a node `name`, its first parameter reading `reads`."""
    hints = resolved_hints(s.fn)
    params_in_order = list(
        inspect.signature(s.fn, annotation_format=_FORWARD).parameters
    )
    first = params_in_order[0]
    params = [Param(first, Source(reads), True, opaque(hints[first]))]  # noqa: FBT003  # `required`
    for param in params_in_order[1:]:
        if param not in s.bindings:
            continue
        binding = s.bindings[param]
        if isinstance(binding, Source):
            binding = Source(names.get(binding.name, binding.name))
        params.append(Param(param, binding, True, opaque(hints[param])))  # noqa: FBT003  # `required`
    frozen = tuple(params)
    payload, renames = _payload(s.fn, frozen)
    return NodeDef(
        name=name,
        params=frozen,
        tags=_NO_TAGS,
        payload=payload,
        origin=s.fn,
        module=getattr(s.fn, "__module__", owner.module),
        returns=opaque(hints["return"]),
        renames=renames,
        namespace=owner.namespace,
        doc=inspect.getdoc(s.fn),
        documented=True,
    )


def _passed(value: object) -> object:
    """A node's value, as the last step made it: the `@pipe_output` node's call."""
    return value


async def _passed_async(value: object) -> object:
    """`_passed` for an `async def` function's node, which the async driver awaits."""
    return value


def relays(nodes: Sequence[NodeDef]) -> frozenset[str]:
    """The `@pipe_output` nodes that only pass their last step's value on."""
    return frozenset(
        node.name for node in nodes if node.payload in {_passed, _passed_async}
    )


def lowered(
    nodes: Sequence[NodeDef], config: Mapping[str, object]
) -> tuple[list[NodeDef], frozenset[str]]:
    """`nodes` with each `@pipe_input` and `@pipe_output` lowered to step nodes.

    Also the names of the nodes made, which no function stands for in a request.
    `ValueError` for an `.on_output` field the function doesn't extract, a fed
    parameter bound to a `value()`, and two nodes of one name.
    """
    out: list[NodeDef] = []
    made: set[str] = set()
    problems: list[str] = []
    by_origin: dict[int, set[str]] = {}
    for node in nodes:
        if (extracting := extractor(node)) is not None:
            by_origin.setdefault(id(node.origin), set()).add(extracting.field)
    within = _within(nodes)
    for node in nodes:
        recorded = decorations(node.origin)
        current = node
        extra: list[NodeDef] = []
        names = {} if node.namespace is None else within.get(node.namespace, {})
        if recorded.pipe_input is not None and extractor(node) is None:
            current, extra = _fed(node, recorded.pipe_input, config, names, problems)
        steps_out = (*recorded.pipe_output, *(s for _, s in recorded.mutations))
        if steps_out:
            fields = by_origin.get(id(node.origin))
            extracting = extractor(node)
            own_target = fields is None and extracting is None
            field_target = extracting is not None
            if own_target or field_target:
                current, piped = _piped(current, steps_out, config, extracting, names)
                extra += piped
                if field_target and piped:
                    # A field's final node stands for no function in a request.
                    made.add(current.name)
            if extracting is None and fields is not None:
                unknown = sorted(
                    {f for s in steps_out for f in s.fields or () if f not in fields}
                )
                problems.extend(
                    f"{node.name}: on_output names {f!r}, which it doesn't extract"
                    for f in unknown
                )
        made.update(n.name for n in extra)
        out.append(current)
        out.extend(extra)
    if problems:
        raise ValueError("\n".join(sorted(set(problems))))
    _refuse_twice(out)
    return out, frozenset(made)


def _within(nodes: Sequence[NodeDef]) -> dict[str, dict[str, str]]:
    """Each namespace's nodes' full names, by their names in it (Q74).

    A step's `source("x")` within a namespace reads the namespace's `x` when it
    has one, as a parameter there does.
    """
    found: dict[str, dict[str, str]] = {}
    for node in nodes:
        if node.namespace is not None:
            bare = node.name.removeprefix(f"{node.namespace}.")
            found.setdefault(node.namespace, {})[bare] = node.name
    return found


def _fed(
    node: NodeDef,
    policy: PipeInput,
    config: Mapping[str, object],
    names: Mapping[str, str],
    problems: list[str],
) -> tuple[NodeDef, list[NodeDef]]:
    """`node` reading its last `@pipe_input` step, and the step nodes."""
    first = node.params[0].name if node.params else None
    target_name = policy.on_input or first
    fed = [p for p in node.params if p.name == target_name]
    if any(p.slot is not None for p in fed):
        problems.append(
            f"{node.name}: pipe_input can't feed {target_name!r}, a group(...) "
            "parameter; give it a plain parameter"
        )
        return node, []
    target = fed[0] if fed else None
    if target is None or not isinstance(target.binding, Source):
        problems.append(
            f"{node.name}: pipe_input feeds {target_name!r}, which is bound to "
            "a value(), not read from a node"
        )
        return node, []
    kept = _kept(policy.steps, config)
    if not kept:
        return node, []
    reads = target.binding.name
    made: list[NodeDef] = []
    for s, label in kept:
        made_node = _step_node(s, f"{node.name}.{label}", reads, node, names)
        made.append(made_node)
        reads = made_node.name
    head = made[0].params[0]
    if not type_matches(target.annotation, head.annotation):
        problems.append(
            f"{node.name}: parameter {target.name!r} is "
            f"{inspect.formatannotation(target.annotation)}, but its first step "
            f"{made[0].name!r} takes {inspect.formatannotation(head.annotation)}"
        )
    # Required: the chain always gives it a value, so its default never applies.
    params = tuple(
        replace(p, binding=Source(reads), required=True) if p is target else p
        for p in node.params
    )
    payload, renames = _payload(node.origin, params)
    return replace(node, params=params, payload=payload, renames=renames), made


def _piped(
    node: NodeDef,
    steps: tuple[Step, ...],
    config: Mapping[str, object],
    field: object,
    names: Mapping[str, str],
) -> tuple[NodeDef, list[NodeDef]]:
    """`node`'s final node reading its last `@pipe_output` step, and the others."""
    name = node.name
    field_name = getattr(field, "field", None)
    mine = [
        s
        for s in steps
        if s.fields is None or field_name is None or field_name in s.fields
    ]
    if not mine:
        return node, []
    # `.raw` and the node's pass-on are made even when the config keeps no
    # step, so a name never depends on the config (Q104).
    kept = _kept(mine, config)
    raw = replace(node, name=f"{name}.raw", tags=_NO_TAGS, checks=())
    made = [raw]
    reads = raw.name
    for s, label in kept:
        made_node = _step_node(s, f"{name}.{label}", reads, node, names)
        made.append(made_node)
        reads = made_node.name
    last = made[-1]
    final = replace(
        node,
        params=(Param("value", Source(reads), True, last.returns),),  # noqa: FBT003  # `required`
        payload=_passed_async if inspect.iscoroutinefunction(node.origin) else _passed,
        renames={reads: "value"},
        returns=last.returns,
    )
    return final, made


def _refuse_twice(nodes: Sequence[NodeDef]) -> None:
    seen: set[str] = set()
    twice: set[str] = set()
    for node in nodes:
        (twice if node.name in seen else seen).add(node.name)
    if twice:
        listed = ", ".join(repr(n) for n in sorted(twice))
        msg = f"two nodes are named {listed}; give one step a name with .named(...)"
        raise ValueError(msg)


# ---- @mutate ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Target:
    """A function `@mutate` adds a step to, with bindings of its own: `apply_to(...)`.

    `bindings`, when given, replace `@mutate`'s own for this target. The
    modifiers act on the step made for it, as `Step`'s do.
    """

    fn: Callable[..., object]
    bindings: Mapping[str, object] | None = None
    label: str | None = None
    conditions: tuple[Condition, ...] = ()
    fields: tuple[str, ...] | None = None

    def named(self, name: str) -> Target:
        """The step's node named `<node>.<name>`; see `Step.named`."""
        return replace(self, label=Step(_passed, {}).named(name).label)

    def _gated(self, kind: ConditionKind, keys: Mapping[str, object]) -> Target:
        added = build_conditions(kind, None, keys, None).conditions
        return replace(self, conditions=(*self.conditions, *added))

    def when(self, **keys: object) -> Target:
        """The step run only when every key equals its value; see `Step.when`."""
        return self._gated("when", keys)

    def when_not(self, **keys: object) -> Target:
        """The step run only when no key equals its value."""
        return self._gated("when_not", keys)

    def when_in(self, **keys: object) -> Target:
        """The step run only when each key is one of its values."""
        return self._gated("when_in", keys)

    def when_not_in(self, **keys: object) -> Target:
        """The step run only when no key is one of its values."""
        return self._gated("when_not_in", keys)

    def on_output(self, *fields: str) -> Target:
        """The step on these extracted fields only; see `Step.on_output`."""
        return replace(self, fields=Step(_passed, {}).on_output(*fields).fields)


def _target(given: object) -> Target:
    if isinstance(given, Target):
        found = given
    elif isinstance(given, str):
        msg = f"mutate takes the target function itself, not its name {given!r}"
        raise TypeError(msg)
    elif callable(given):
        found = Target(given)
    else:
        msg = f"mutate takes functions or apply_to(...)s, got {type(given).__name__}"
        raise TypeError(msg)
    if getattr(found.fn, "__name__", "") == "<lambda>":
        msg = "a lambda is never a node, so it can't be mutated"
        raise TypeError(msg)
    return found


def apply_to(fn: Callable[..., object], **bindings: object) -> Target:
    """`fn` as a `@mutate` target, the step's bindings given here rather than there.

    `bindings` replace `@mutate`'s own for this target, as in Hamilton.
    """
    found = _target(fn)
    return replace(found, bindings=MappingProxyType(dict(bindings)))


def _mutator_key(fn: Callable[..., object]) -> str:
    """Who a mutator is, across redefinitions: its module and qualified name."""
    return f"{getattr(fn, '__module__', '')}.{_named(fn)}"


def mutate[F: Callable[..., object]](
    *targets: Callable[..., object] | Target, **bindings: object
) -> Callable[[F], F]:
    """Adds the decorated function as a step to each target's `@pipe_output` (Q107).

    Each target is a function, or `apply_to(fn, **bindings)`. The step is bound
    with `bindings`, or with the target's own when it has some; its modifiers
    apply. The steps go after the target's own `@pipe_output` steps, in the
    order the mutators are defined. The mutator itself is no node. A target in
    another module works when both are built; one no built module defines is a
    `ValueError` at build. A redefinition of a mutator, as re-running a cell
    makes, replaces its step; mutators sharing one function's code, made in a
    loop or a factory, each add theirs.
    """
    if not targets:
        msg = "mutate needs at least one target function"
        raise TypeError(msg)
    found = [_target(t) for t in targets]

    def apply(mutator: F) -> F:
        key = _mutator_key(mutator)
        code = getattr(mutator, "__code__", None)
        cleared: set[int] = set()
        for target in found:
            given = target.bindings if target.bindings is not None else bindings
            made = step(mutator, **given)
            made = replace(
                made,
                label=target.label,
                conditions=target.conditions,
                fields=target.fields,
            )
            current = decorations(target.fn)
            mutations = current.mutations
            if id(target.fn) not in cleared:
                # A redefinition of this mutator, as re-running a notebook cell
                # makes, replaces its old steps: same name, other code. One
                # whose code is the same, as a loop or a factory makes, adds.
                mutations = tuple(
                    (k, s)
                    for k, s in mutations
                    if k != key or getattr(s.fn, "__code__", None) is code
                )
                cleared.add(id(target.fn))
            if any(step_ == made for _, step_ in mutations):
                # This very mutator, bound the same, already adds this step.
                continue
            mutations = (*mutations, (key, made))
            setattr(target.fn, _ATTRIBUTE, replace(current, mutations=mutations))
        current = decorations(mutator)
        setattr(
            mutator,
            _ATTRIBUTE,
            replace(
                current,
                excluded=True,
                mutates=(*current.mutates, *(t.fn for t in found)),
            ),
        )
        return mutator

    # Marked, so `@resolve` refuses it before it writes onto any target.
    setattr(apply, MUTATES, True)  # an attribute of our own
    return apply


def unbuilt_targets(
    modules: Sequence[ModuleType], nodes: Sequence[NodeDef]
) -> list[str]:
    """What is wrong with the build's `@mutate`s: one line each, sorted (Q107).

    A built mutator's target must be a function of the build: a node's, or one
    a built module defines, such as a `@config.when` variant the config didn't
    select. A built node's mutator must be of a built module too, or its step
    would be lost when the driver is pickled and built again elsewhere.
    """
    defined = {
        id(value)
        for module in modules
        for value in vars(module).values()
        if inspect.isfunction(value)
    }
    defined |= {id(node.origin) for node in nodes}
    built = {module.__name__ for module in modules}
    found: set[str] = set()
    for module in modules:
        for value in vars(module).values():
            if not inspect.isfunction(value) or value.__module__ != module.__name__:
                continue
            found.update(
                f"mutate on {_named(value)}: its target {_named(target)} "
                "is no function of the build"
                for target in decorations(value).mutates
                if id(target) not in defined
            )
    for node in nodes:
        for _, mutation in decorations(node.origin).mutations:
            home = getattr(mutation.fn, "__module__", None)
            if home not in built:
                found.add(
                    f"{node.name}: mutated by {_named(mutation.fn)} of module "
                    f"{home!r}, which isn't built; build it too"
                )
    return sorted(found)
