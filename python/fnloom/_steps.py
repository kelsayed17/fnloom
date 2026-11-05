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
from types import EllipsisType, MappingProxyType
from typing import TYPE_CHECKING

from fnloom._config import Condition, ConfigPolicy, build_conditions, matches
from fnloom._decorators import _ATTRIBUTE, RESOLVING, Source, Value, decorations
from fnloom._extract import extractor
from fnloom._fingerprint import fingerprint
from fnloom._namespaces import checked_namespace, qualified
from fnloom._scan import NodeDef, Within, _payload
from fnloom._types import opaque, resolved_hints, type_matches
from fnloom._view import Param

if TYPE_CHECKING:
    from types import ModuleType

    from fnloom._config import ConditionKind


# Annotations as written: a name imported only under TYPE_CHECKING is no error.
_FORWARD = annotationlib.Format.FORWARDREF

_NO_TAGS = MappingProxyType[str, str]({})

# A step's namespace: `...` for its node's own name, `None` for none, or a path.
type StepNamespace = str | EllipsisType | None


@dataclass(frozen=True, slots=True)
class Step:
    """One step: a function and its bindings, as `step(...)` made it.

    `bindings` bind its parameters after the first, by name. `label` replaces the
    `with_<function>` part of its node's name, from `.named`. `namespace` is what
    goes before it: `...` for the node's own name, `None` for nothing, or a
    dotted path (Q140). `conditions` are the config conditions it runs under,
    all of which must hold; `fields` the extracted fields it applies to, from
    `.on_output`, or `None` for all.
    """

    fn: Callable[..., object]
    bindings: Mapping[str, Source | Value]
    label: str | None = None
    conditions: tuple[Condition, ...] = ()
    fields: tuple[str, ...] | None = None
    namespace: StepNamespace = ...

    def named(self, name: str, namespace: StepNamespace = ...) -> Step:
        """This step, its node named `<node>.<name>` rather than `<node>.with_<fn>`.

        `namespace` replaces the `<node>` part (Q140): `None` drops it, so the
        node is `<name>`, and a dotted path names it `<namespace>.<name>`. `...`,
        the default, keeps the step's namespace as it is, by default `<node>`.
        Under a module's namespace, the step's stays within it.
        """
        if not isinstance(name, str) or not name.isidentifier():
            msg = f"a step's name must be an identifier, got {name!r}"
            raise ValueError(msg)
        if namespace is ...:
            return replace(self, label=name)
        return replace(self, label=name, namespace=checked_namespace(namespace))

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
        return replace(self, fields=_checked_fields(fields))


def _checked_fields(fields: Sequence[object]) -> tuple[str, ...]:
    """`fields` as a tuple of distinct names; `TypeError` for none or a non-`str`."""
    if not fields or not all(isinstance(f, str) for f in fields):
        msg = "on_output takes one or more field names"
        raise TypeError(msg)
    twice = sorted({f for f in fields if fields.count(f) > 1}, key=str)
    if twice:
        msg = f"on_output names {', '.join(repr(f) for f in twice)} twice"
        raise ValueError(msg)
    return tuple(str(f) for f in fields)


def _step_namespace(namespace: object) -> StepNamespace:
    """A decorator's `namespace=`: `...`, or what `checked_namespace` allows."""
    return ... if namespace is ... else checked_namespace(namespace)


def _in_namespace(
    steps: tuple[Step, ...], namespace: StepNamespace
) -> tuple[Step, ...]:
    """`steps`, each without a namespace of its own given `namespace` (Q140)."""
    if namespace is ...:
        return steps
    return tuple(
        replace(s, namespace=namespace) if s.namespace is ... else s for s in steps
    )


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
    *steps: Step, namespace: StepNamespace = ..., on_input: str | None = None
) -> Callable[[F], F]:
    """Runs `steps` on a parameter's value before the function reads it (Q104).

    The steps feed the parameter `on_input`, by default the first one: the first
    step takes what the parameter would have read, and the function reads the
    last step. `namespace` names the steps as `Step.named`'s does, for each step
    not given one of its own (Q140). `TypeError` for no steps, a function with no
    parameters, or an `on_input` naming none of its parameters; a namespace that
    is no dotted identifier path is refused here.
    """
    recorded = _in_namespace(_steps(steps, "pipe_input"), _step_namespace(namespace))
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


def pipe_output[F: Callable[..., object]](
    *steps: Step,
    namespace: StepNamespace = ...,
    on_output: str | Sequence[str] | None = None,
) -> Callable[[F], F]:
    """Runs `steps` on the function's value: the node named after it is the last's.

    The function itself runs as `<node>.raw`. On a function with extracted
    fields, the steps apply to each field, or to those `on_output` names; a
    step's own `.on_output` wins over `on_output`, as the more specific. On a
    function without fields, `on_output` may name only the node itself.
    `namespace` names the steps as `Step.named`'s does, for each step not given
    one of its own; `.raw` and the node keep their names (Q140). `TypeError` for
    no steps or a malformed `on_output`; a namespace that is no dotted
    identifier path is refused here, and a field the function doesn't extract
    at build.
    """
    recorded = _in_namespace(_steps(steps, "pipe_output"), _step_namespace(namespace))
    if on_output is not None:
        given = (on_output,) if isinstance(on_output, str) else on_output
        if not isinstance(given, (list, tuple)):
            msg = f"on_output takes a field name or a list of them, got {on_output!r}"
            raise TypeError(msg)
        fields = _checked_fields(given)
        recorded = tuple(
            replace(s, fields=fields) if s.fields is None else s for s in recorded
        )

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


def _step_name(owner: NodeDef, s: Step, label: str) -> str:
    """`s`'s node's name: its label under its namespace, within the module's (Q140)."""
    if s.namespace is ...:
        return f"{owner.name}.{label}"
    return qualified(owner.namespace, qualified(s.namespace, label))


def _step_node(
    s: Step, name: str, reads: str, owner: NodeDef, within: Within | None
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
        if isinstance(binding, Source) and within is not None:
            # The namespace's node of that name, or what its `bind=` says.
            read = within.read(binding.name)
            binding = binding if read is None else read
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
    nodes: Sequence[NodeDef],
    config: Mapping[str, object],
    bound: Mapping[str, Mapping[str, Source | Value]] | None = None,
    used: dict[str, set[str]] | None = None,
) -> tuple[list[NodeDef], frozenset[str]]:
    """`nodes` with each `@pipe_input` and `@pipe_output` lowered to step nodes.

    Also the names of the nodes made, which no function stands for in a request.
    `ValueError` for an `.on_output` field the function doesn't extract, a fed
    parameter bound to a `value()`, and two nodes of one name. `bound` and
    `used` are each namespace's `bind=` and the bound names read, as `scan`
    takes them (Q112).
    """
    out: list[NodeDef] = []
    # What made each node in `out`, for a name made twice (Q140).
    owners: list[str] = []
    made: set[str] = set()
    problems: list[str] = []
    by_origin: dict[int, set[str]] = {}
    # The nodes a function without fields makes, by name: a row each (Q140).
    rows: dict[int, set[str]] = {}
    for node in nodes:
        if (extracting := extractor(node)) is not None:
            by_origin.setdefault(id(node.origin), set()).add(extracting.field)
        else:
            rows.setdefault(id(node.origin), set()).add(_bare(node))
    within = _within(nodes, bound or {}, used)
    for node in nodes:
        recorded = decorations(node.origin)
        current = node
        extra: list[NodeDef] = []
        names = None if node.namespace is None else within.get(node.namespace)
        if recorded.pipe_input is not None and extractor(node) is None:
            current, extra = _fed(node, recorded.pipe_input, config, names, problems)
        steps_out = (*recorded.pipe_output, *(s for _, s in recorded.mutations))
        if steps_out:
            fields = by_origin.get(id(node.origin))
            extracting = extractor(node)
            own_target = fields is None and extracting is None
            field_target = extracting is not None
            if own_target or field_target:
                target = _bare(node) if extracting is None else extracting.field
                current, piped = _piped(current, steps_out, config, target, names)
                extra += piped
                if field_target and piped:
                    # A field's final node stands for no function in a request.
                    made.add(current.name)
            if not field_target:
                known = fields if fields is not None else rows[id(node.origin)]
                problems.extend(
                    _unknown_fields(node, steps_out, known, extracts=fields is not None)
                )
        made.update(n.name for n in extra)
        out.append(current)
        owners.append(f"the {_kind(node)} {node.name!r}")
        out.extend(extra)
        owners.extend(f"a step of {node.name!r}" for _ in extra)
    problems.extend(_shadowed(out, made, bound or {}))
    if problems:
        raise ValueError("\n".join(sorted(set(problems))))
    _refuse_twice(out, owners)
    return out, frozenset(made)


def _shadowed(
    nodes: Sequence[NodeDef],
    made: set[str],
    bound: Mapping[str, Mapping[str, Source | Value]],
) -> list[str]:
    """A line for each node reading outside its namespace a name a step has in it.

    Names were resolved before the steps were made, so with `namespace=None` or
    a `str`, a step can take a name a node of its module namespace reads from
    outside: refused rather than read from the wrong node (Q74, Q140). A name
    the namespace's `bind=` sends outside on purpose is left alone.
    """
    found: list[str] = []
    for node in nodes:
        if node.namespace is None:
            continue
        sent = {
            b.name
            for b in bound.get(node.namespace, {}).values()
            if isinstance(b, Source)
        }
        for param in node.params:
            binding = param.binding
            if not isinstance(binding, Source) or binding.name in sent:
                continue
            inside = qualified(node.namespace, binding.name)
            if inside in made and inside != binding.name:
                found.append(
                    f"{node.name} reads {binding.name!r} from outside namespace "
                    f"{node.namespace!r}, but a step there is named {inside!r}; "
                    "name the step otherwise, or read it as a source"
                )
    return found


def _unknown_fields(
    node: NodeDef, steps: Sequence[Step], known: set[str], *, extracts: bool
) -> list[str]:
    """A line for each `on_output` name that is none of `known`.

    `known` is the fields the function extracts, or, without fields, the nodes
    it makes: itself, or each `@parameterize` row, as in Hamilton. Any other
    name is refused, not ignored (Q106, Q140).
    """
    unknown = sorted({f for s in steps for f in s.fields or ()} - known)
    if extracts:
        return [
            f"{node.name}: on_output names {f!r}, which it doesn't extract"
            for f in unknown
        ]
    listed = ", ".join(repr(n) for n in sorted(known))
    return [
        f"{node.name}: on_output names {f!r}, but it extracts no fields; name "
        f"{listed} or leave it out"
        for f in unknown
    ]


def _bare(node: NodeDef) -> str:
    """`node`'s name within its module's namespace."""
    if node.namespace is None:
        return node.name
    return node.name.removeprefix(f"{node.namespace}.")


def _kind(node: NodeDef) -> str:
    """What `node` is, for an error naming it: an extracted field or a function."""
    return "field" if extractor(node) is not None else "function"


def _within(
    nodes: Sequence[NodeDef],
    bound: Mapping[str, Mapping[str, Source | Value]],
    used: dict[str, set[str]] | None,
) -> dict[str, Within]:
    """Each namespace's nodes' full names, by their names in it (Q74).

    A step's `source("x")` within a namespace reads the namespace's `x` when it
    has one, as a parameter there does, and otherwise its `bind=` of `x` (Q112).
    """
    found: dict[str, dict[str, str]] = {}
    for node in nodes:
        if node.namespace is not None:
            bare = node.name.removeprefix(f"{node.namespace}.")
            found.setdefault(node.namespace, {})[bare] = node.name
    return {
        namespace: Within(
            namespace,
            names,
            bound.get(namespace, MappingProxyType({})),
            None if used is None else used.setdefault(namespace, set()),
        )
        for namespace, names in found.items()
    }


def _fed(
    node: NodeDef,
    policy: PipeInput,
    config: Mapping[str, object],
    names: Within | None,
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
        made_node = _step_node(s, _step_name(node, s, label), reads, node, names)
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
    target: str,
    names: Within | None,
) -> tuple[NodeDef, list[NodeDef]]:
    """`node`'s final node reading its last `@pipe_output` step, and the others.

    `target` is what `on_output` names `node` by: its field, or its own name.
    """
    name = node.name
    mine = [s for s in steps if s.fields is None or target in s.fields]
    if not mine:
        return node, []
    # `.raw` and the node's pass-on are made even when the config keeps no
    # step, so a name never depends on the config (Q104).
    kept = _kept(mine, config)
    raw = replace(node, name=f"{name}.raw", tags=_NO_TAGS, checks=())
    made = [raw]
    reads = raw.name
    for s, label in kept:
        made_node = _step_node(s, _step_name(node, s, label), reads, node, names)
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


def _refuse_twice(nodes: Sequence[NodeDef], owners: Sequence[str]) -> None:
    """`ValueError` for a name two nodes have, naming what made each (Q140)."""
    by_name: dict[str, list[str]] = {}
    for node, owner in zip(nodes, owners, strict=True):
        by_name.setdefault(node.name, []).append(owner)
    twice = sorted(
        f"{name!r} is made twice: by {' and by '.join(sorted(found))}"
        for name, found in by_name.items()
        if len(found) > 1
    )
    if twice:
        listed = "\n".join(twice)
        msg = (
            f"{listed}\ngive one step a name with .named(...), or a namespace of its "
            "own"
        )
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
    namespace: StepNamespace = ...

    def named(self, name: str, namespace: StepNamespace = ...) -> Target:
        """The step's node named `<node>.<name>`, or `namespace`'s; see `Step.named`."""
        named = replace(Step(_passed, {}), namespace=self.namespace).named(
            name, namespace
        )
        return replace(self, label=named.label, namespace=named.namespace)

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


def _same(one: Step, other: Step) -> bool:
    """Whether two steps are one: the same function, bound alike, never by `==`.

    A `value()` matches by identity or by fingerprint, as the cache would key
    it, so an array or anything whose `==` is no `bool` is never compared.
    """
    if (
        one.fn is not other.fn
        or one.label != other.label
        or one.namespace != other.namespace
        or one.conditions != other.conditions
        or one.fields != other.fields
        or one.bindings.keys() != other.bindings.keys()
    ):
        return False
    return all(
        same_binding(one.bindings[name], other.bindings[name]) for name in one.bindings
    )


def same_binding(one: Source | Value, other: Source | Value) -> bool:
    """Whether two bindings are one: a source by name, a value by fingerprint."""
    if one is other:
        return True
    if isinstance(one, Source) and isinstance(other, Source):
        return one.name == other.name
    if not (isinstance(one, Value) and isinstance(other, Value)):
        return False
    if one.value is other.value:
        return True
    try:
        return fingerprint(one.value) == fingerprint(other.value)
    except Exception:  # noqa: BLE001  # no fingerprint: not known to be the same
        return False


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
        if (where := RESOLVING.get()) is not None:
            # Refused before it writes onto a target, however a resolver came
            # to apply it: a refused build leaves every function as it was.
            msg = f"{where}: it can't apply @mutate"
            raise TypeError(msg)
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
                namespace=target.namespace,
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
            if any(_same(step_, made) for _, step_ in mutations):
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
