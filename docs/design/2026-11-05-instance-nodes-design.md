# fnloom — instance methods as nodes, design

**Status:** drafted 2026-11-05 for the maintainer's approval. Nothing here is built. It designs Plan 13 of the
[roadmap](../plans/2026-10-28-roadmap-ahead-of-hamilton.md) (item 12), requested in
[#51](https://github.com/kelsayed17/fnloom/issues/51). The implementation plan is
[Plan 13](../plans/2026-11-05-plan-13-instance-nodes.md), built after Plan 11. Facts about Hamilton were probed against
the `apache-hamilton 1.90.0` wheel on Python 3.14 (§11). Every decision is a call this design makes, changeable (§13);
the ones the maintainer should settle first are listed at the top of §13.

## 1. Intent

A long-lived object, such as an API client, reaches a fnloom graph today as an input node. Every driver must be handed
it in `inputs=`, every node that uses it takes it as a parameter, `inputs()` lists it, and the cache tries to
fingerprint it. Yet the app already owns the client's lifetime: it is built once at startup, a missing credential stops
startup, and the provider per capability is chosen once.

This design lets a user wrap the client in an instance and give the instance to the builder:

```python
class Sales:
    def __init__(self, client: SalesClient) -> None:
        self._client = client

    @fnloom.cache(behavior="recompute")  # it reads the outside world
    def orders(self, region: str) -> list[Order]:
        return self._client.orders(region)

    def total(self, orders: list[Order]) -> float:
        return sum(o.amount for o in orders)


driver = fnloom.Builder().with_modules(Sales(client), calendar).build()
driver.execute(["total"], inputs={"region": "eu"})
```

The instance's public methods become nodes, as a module's public functions do. The node name is the method name, the
parameters after `self` are its dependencies, and the return annotation is its type. `self` is bound once, at build. It
is never a dependency, and nothing of the instance's state becomes an input. fnloom's run model is unchanged: a node
still lives for one run, and the instance lives as long as the app keeps it. The alternative the issue set aside, nodes
whose lifetime is the scope's rather than a run's, would give fnloom a second kind of node lifetime (§12).

## 2. Goals, non-goals, anti-goals

### Goals

1. **An instance is a module of bound methods.** Everything that works on a module's functions works on an instance's
   methods, with the same names, errors and decorators: `namespace=`, `bind=`, `config=`, `replace=`, `@config.when*`,
   `@parameterize`, `@extract_*`, `@check_output*`, `@tag`, `@cache`, the steps, absence and fan-out.
2. **`self` is invisible to the graph.** It is never an edge, an input, a parameter in a view, or a value a hook sees as
   an argument.
3. **A cache hit is never wrong** (caching design goal 1). What of the instance can change a method's result is either
   in its key, declared not to matter by the user, or makes the node uncacheable with a reason.
4. **Collection is static and side-effect free.** Building reads class dictionaries. It never calls `getattr` on the
   instance, so no property, `__getattr__` or descriptor of the user's runs at build.
5. **No cost on the default path.** A graph with no instance builds and runs as today; a method node costs one bound-
   method call more than a function node, and nothing per run.

### Non-goals (this design)

- **Constructing instances.** fnloom never calls a class. The app builds the instance, with its own lifetime.
- **Scope-lifetime nodes**, or any second node lifetime (§12).
- **Making a client thread-safe.** Under Plan 11's `with_threads`, two method nodes of one instance may run at once.
  fnloom doesn't serialize them (§8.3).
- **Fingerprinting clients.** fnloom never walks into an instance's attributes to version it (§6).

### Anti-goals

The design fails, even if every test passes, if:

- building a driver from an instance runs any of the user's code other than decorators already applied, such as a
  property or a `__getattr__`;
- two instances whose state differs in a way the user has not declared irrelevant share a cache entry;
- a graph without instances changes in any way: its names, errors, export, drawing, keys or speed;
- a behaviour differs from Hamilton without a register row and a test (main design §2).

## 3. The decisions, in short

| # | Decision |
|---|---|
| D1 | `Builder.with_modules` takes instances beside modules, with every keyword it has (§4.1). |
| D2 | An instance's nodes are the public functions and staticmethods found on its class's MRO, in the class itself and in its bases of the user's own code. Classmethods, properties, other descriptors, instance attributes, private and dunder names, and anything defined on `object`, a built-in or library class are not nodes (§4.2). |
| D3 | A class-body `__all__` lists the nodes, as a module's does (Q73), and `@fnloom.exclude` works on methods (§4.2). |
| D4 | Each node's payload is the function bound to the instance once, at build, with `types.MethodType`. The first parameter is `self`, whatever it is called, and is never a dependency (§4.3). |
| D5 | Node names and collisions follow modules: two instances, or an instance and a module, defining one name fail the build unless `namespace=` or `replace=` says otherwise. One object given twice counts once, by identity (§4.4). |
| D6 | A method's code version adds the versions of the class attributes it reads through `self`, by the rule for a module read by attribute (§5). |
| D7 | The instance enters a method's key only if the method reads `self`, and then as its fingerprint, taken once at build. An instance with no fingerprint makes the methods that read `self` uncacheable, with a reason, unless the class declares one (`__fnloom_fingerprint__`, `register_fingerprint`) or the method says `@cache(ignore=("self",))` (§6). |
| D8 | Every decorator works on methods. A step or mutator that is a function of the instance's class is bound to that instance (§7). |
| D9 | `async def`, generator, `-> T \| Absent` and `-> Parallelizable[T]` methods follow the function rules exactly (§8). |
| D10 | `NodeView.module` stays the defining function's module, `NodeView.origin` is the bound method, `NodeView.instance` is new, and `Driver.instances` lists the instances. A bound method in `final_vars` means its node (§9). |
| D11 | A builder pickles each instance by value, with Python's rules; `copy` shares it; `rebuild()` reuses it and refuses a class that a reload made stale (§10). |
| D12 | A class, a built-in or standard-library instance, an instance with no nodes, a method without a positional `self`, and an instance attribute that shadows a method are refused at build, each naming the culprit (§4.5). |

## 4. Collecting an instance

### 4.1 The entry point

`with_modules(*modules, namespace=None, bind=None, config=None, replace=None)` accepts, as each item, a module (any
`types.ModuleType`, as today) or an instance. The keywords apply to every item of the call, as they do for modules.
Modules and instances keep one order, the order given, which is what `replace=` means by "given earlier" (Q150).

The parameter's annotation widens from `ModuleType` to `ModuleType | object`, so a type checker no longer flags a
function passed by mistake. The run-time refusals of §4.5 keep Q52's guarantee: no slip builds an empty graph.

`with_functions` also takes a bound method of an instance (`with_functions(sales.total)`), and `fnloom.node` takes one,
for the user who wants one method and not the whole class. It is the same node `with_modules` would make for it.

**Why `with_modules`:** it is the spelling the issue and the roadmap asked for, and an instance is, to the graph, a
module whose functions share some state. A separate `with_instances` is the alternative (§13, question 1).

**Hamilton:** `Builder().with_modules(Sales(client))` builds an empty graph without error, as does a class (§11).
Users pass the client as an input instead. **Rows:** Q160 (instances accepted), Q52 amended.

### 4.2 What is collected

The instance's class is `C = type(instance)`. Collection reads, for each public name (one that does not start with
`_`), the attribute Python's lookup would find on `C`, statically: the first class in `C.__mro__` whose `__dict__` holds
the name, as `inspect.getattr_static` finds it. That entry is a node when:

- the class holding it **counts** (below); and
- it is a plain Python function, or a `staticmethod` wrapping one.

A class counts when it is `C` itself, or a base of `C` that is the user's own code by the caching design's rule (§4: a
module neither in the standard library nor installed), or a base from the same top-level package as `C`. The last
clause keeps an app's own base classes counting when the app is installed into `site-packages`, as in a container
image. `object`, built-in classes, the standard library (`abc.ABC`, `typing.Generic`, `typing.Protocol`) and other
installed libraries (a pydantic `BaseModel`) never count, so their public methods, API rather than nodes, are never
collected. Since lookup is by MRO, a subclass's override is the node, and a base's method that the subclass overrides is
not.

**Not nodes:**

- **Classmethods.** They are bound to the class, not the instance. On the classes this feature is for they are mostly
  alternative constructors (`Sales.from_env()`), which would become nodes with no dependencies. A staticmethod or a
  module function does what a classmethod node would.
- **Properties**, `functools.cached_property` and every other descriptor. A property is read, not called with
  dependencies, and reading it at build would run user code (goal 4).
- **Instance attributes**, even callable ones (`self.fetch = client.get`). Only the class's code is collected, which is
  what code versions can read.
- **Private and dunder names.** `__call__`, `__init__` and `__repr__` are never nodes, as a module's `_helper` is not.
  A callable instance is not one node.
- Nested classes, class attributes, and anything else that is not a function.

**Staticmethods are nodes.** A staticmethod reads nothing of the instance. It is a function that lives in the class,
exactly a module function, and it is keyed as one (§6).

**Control.** A class may define `__all__` in its body, a list or tuple of `str`. The `__all__` Python's lookup finds on
`C` names the nodes, inherited ones included, by the rules Q73 gives a module's: a `@config` variant is listed by its
attribute name, a listed name that is no collectable method is ignored, and a malformed `__all__` is a `TypeError` at
build. `@fnloom.exclude` on a method keeps it out, and beats `__all__`, as in a module. Nothing new is added for
control.

**Order.** Nodes are found base-first along the reversed MRO, then in definition order within a class. Order never
changes a result: execution is name-tie-broken (Q26), and errors are sorted.

**Hamilton:** no equivalent. Hamilton's `find_functions` keeps a module's plain functions, so an instance, whose
members are bound methods, yields nothing (§11). **Rows:** Q161 (what is collected), Q73 amended (class-body
`__all__`, `@exclude` on methods).

### 4.3 Binding `self`

For each collected function `f`, the node's payload is `types.MethodType(f, instance)`, made once at build. A
staticmethod's payload is its function. Collection never calls `getattr(instance, name)`, so an instance whose class
overrides `__getattribute__` or `__getattr__` behaves the same as one that doesn't.

The node's parameters are `f`'s, minus the first positional one. That parameter is `self` by convention. fnloom reads
it by position, not by name. A method whose first parameter is not positional, or that has none, is refused (§4.5).
Every remaining parameter is read as a module function's is: name-wiring, defaults, annotations and `group(...)`.

So `self` is never a dependency, never in `inputs()`, never in a `NodeView.params`, and never among the arguments a node
wrapper (Q111) or a hook receives. Nothing of the instance's state is an input: a parameter that happens to share its
name with an instance attribute is an ordinary dependency.

Annotations are read from `f` with `annotationlib`, as for functions, so names in the class body resolve as Python 3.14
resolves them. `typing.Self` in a method node's annotation means `C`, for the edge, input and output checks.

**Hamilton:** `ad_hoc_utils.create_temporary_module(sales.orders, sales.total)` raises `ValueError: Missing type hint
for self in function orders` at build: the bound method is read as its unbound function (§11). Users write module
functions that take the client as a parameter. **Rows:** Q162 (`self` bound, never an input).

### 4.4 Names and collisions

A method node is named by its attribute name, cut at `__` for a `@config` variant, and prefixed by `namespace=`, all
as for a module function. The duplicate rules are a module's (Q74, Q150), with instances in the same list:

- Two instances that define one name, of one class or of two, fail the build, naming both. Give each its own
  `namespace=`, or let the later one `replace=` the name.
- An instance and a module, or an instance and a `with_functions` function, that define one name fail the same way.
  `replace=` works in both directions: an instance may replace a module's node, and a module an instance's.
- One object given twice counts once, as a module given twice does. Sameness is identity, never `==` or `hash`, so an
  unhashable instance (a dataclass with `eq=True`) works, and two equal but distinct instances are two instances.
- One instance given under two namespaces makes two node sets bound to the same object. That is allowed, as one
  module under two namespaces is.

Errors name an instance as `instance of 'app.sales.Sales'`. When the build holds several instances of one class, they
are told apart by namespace, else by their place in the builder (`the 2nd instance of 'app.sales.Sales'`).

`bind=` and `config=` per namespace (Q112) work unchanged, which is how two instances of one class get different
inputs or different `@config` variants:

```python
builder = (
    fnloom.Builder()
    .with_modules(Sales(eu_client), namespace="eu", config={"region": "eu"})
    .with_modules(Sales(us_client), namespace="us", config={"region": "us"})
)
```

**Hamilton:** no equivalent. **Rows:** none new; Q74 and Q150 are amended to say instances count as modules.

### 4.5 Refusals

Each refusal lists every culprit of the build, sorted, in one message, like the build's other errors.

| Given | Error |
|---|---|
| A class (`with_modules(Sales)`) | `TypeError: expected a module or an instance, got class 'Sales'; give an instance, as with_modules(Sales(...))` |
| A function, bound method or lambda | `TypeError: expected a module or an instance, got function 'f'; give functions to with_functions` |
| An instance of a built-in or standard-library class (`42`, `"x"`, a `dict`, a `Path`), or of a fnloom class (a `Driver`, a `Builder`) | `TypeError: expected a module or an instance of a class of your own, got 'int' instance` |
| An instance with no nodes | the empty-module error (Q53), naming each public attribute of `C` and why it is not a node: `'from_env' is a classmethod`, `'name' is a property`, `'model_dump' is defined on 'pydantic.main.BaseModel'`, `not listed in __all__`, `marked @exclude` |
| A method whose first parameter is not positional, or that has no parameter | `ValueError: method 'app.sales.Sales.f' takes no positional self; make it a staticmethod` |
| An instance attribute named like a collected method (`vars(instance)` holds `orders`) | `ValueError: instance of 'app.sales.Sales': its attribute 'orders' shadows the method 'orders', so the node would call what Python wouldn't` |
| A fnloom decorator applied over `@staticmethod` or `@classmethod` (`@tag(...)` above `@staticmethod`) | `TypeError` when decorating: put `@staticmethod` outermost |

The instance of a library class given directly (an `httpx.Client`) is not refused, since fnloom can't tell an
installed library from an installed app: its own class counts (§4.2), and its unannotated methods fail the build by the
function rules, naming each.

A missing input named like a method that is not a node gains a line saying why, as Q73's does for a module:
`'from_env' is a classmethod of 'app.sales.Sales', and classmethods are not nodes`.

**Hamilton:** an instance or a class builds an empty graph, with no error, so the mistake surfaces at `execute` as an
unknown output (§11). **Rows:** Q160 (its REFUSE half), Q52 and Q53 amended.

## 5. Code versions of a method

A method node's key keeps the caching design's identity part (§3): its name, and its function's `__module__` and
`__qualname__` (`app.sales.Sales.orders`), which already tells two classes' methods apart.

Its **code version** is its function's (caching design §4), plus what the method reaches through `self`. A call
`self.helper()` is an attribute read on a local, not a global, so the function's own version misses it. fnloom treats
the instance's class as it treats a user module read by attribute (`helpers.f`): for each name the method's code reads
as an attribute (its `co_names`, settled across everything it reaches, as `_codeversion` does today), the class
attribute of that name, wherever it is defined along the counted MRO, is versioned by the global rules:

- a function: its code version, recursively, so an edit to `self._parse` misses the cache;
- an immutable class attribute (`PAGE_SIZE = 100`): its fingerprint;
- a mutable class attribute (`_seen: list = []`): the method is uncacheable, unless `@cache(version=...)` vouches for
  it, as with a mutable global;
- a property: its getter's code version;
- a name the class doesn't define: nothing, since it is an instance attribute or a method of another object, which §6
  covers.

A name is versioned on every counted class of the MRO that defines it, not only the first, so `super().f()` is covered.
Editing a method that `orders` doesn't reach leaves `orders`'s key unchanged. A staticmethod is versioned as a function.

**Why not the whole class's version:** `class_version(C)` exists, and is simpler, but every edit to any method would
miss every method of the class. Reading by attribute is already how a module's code is reached, and costs the same.

**Hamilton:** `cache` versions a function by its source text (Q48), and a client used through a module global or a
parameter is not part of it. **Rows:** Q163 (with §6), Q48 amended.

## 6. The instance in the key

A method can read the instance's state through `self`: `self._client`, `self._region`, `self._threshold`. If two
instances with different state could share a key, a store shared by two drivers, or one driver rebuilt with another
instance, would serve one instance's result to the other. That is the caching design's first anti-goal. So the
instance's state is a value the method reads, like a closure cell, and the same rule applies to it as to a global:
versioned if it can be, otherwise the node can't be cached.

1. **A method that never reads `self` is keyed as a function.** At build, fnloom reads the method's bytecode. If the
   first local is never loaded, is not captured by an inner function, and the code reads none of `locals`, `vars`,
   `eval`, `exec` or a zero-argument `super` (which reads `self` implicitly), the instance can't affect the result, and
   it enters nothing. The issue's `total` is such a method, as is every staticmethod.
2. **Otherwise the instance's version is its fingerprint**, by the caching design's §5 rules, taken once at build:
    - `__fnloom_fingerprint__(self)` on the class, returning what of the state is data (`return self._region`), or `()`
      to say none of it is;
    - `fnloom.register_fingerprint(Sales, fn)`, for a class the user would rather not touch;
    - the built-in record forms: a frozen dataclass, a named tuple or a frozen pydantic model, fingerprinted by its
      fields, each of which must itself be fingerprintable.
3. **Otherwise the methods that read `self` are uncacheable**, with the reason in `explain_cache`, and their consumers
   follow the existing rule (uncacheable unless `ignore`d):
   `reads self, an instance of 'app.sales.Sales' with no fingerprint; give the class __fnloom_fingerprint__, register
   one with fnloom.register_fingerprint, or mark the method @cache(ignore=("self",))`.
4. **`@cache(ignore=("self",))`** on a method says that the instance doesn't affect its result. `ignore` takes the
   first parameter's name, whatever it is. The method's code version still covers what it reaches through `self` (§5).

fnloom never walks an instance's `__dict__` to fingerprint it, so a client is never fingerprinted. The only way it is
read is a registration the user wrote for its type. A record form (rule 2) that holds a client fails on that field and
names it (`self._client: SalesClient`), without hashing anything of it.

**Taken at build**, as a `value()` constant, a `bind=` value and a default are (caching design §4, §13). The instance is
app-scoped: state that changes from one run to the next is an input, and belongs in `inputs=`. Reading it at build
costs nothing per run. The known limit is the one mutable defaults have: an instance mutated after the build is not
seen until `rebuild()`. The guide says so beside the global-rebinding limit.

**The issue's case.** `orders` calls an API, so it reads the outside world, and is `@cache(behavior="recompute")` as
any loader is (caching design §6). Its consumers then hit whenever the orders are unchanged. `total` never reads `self`
and is cached by rule 1. The class needs no fingerprint unless a non-`recompute` method reads its state.

**Why not the issue's "likely" default**, the class's code version without the instance's state: §12.

**Hamilton:** a client passed as an input is fingerprinted by its `__dict__`. A client with no attributes gets the
constant `<unhashable>`, so two different clients share a key (F3, Q47). **Rows:** Q163.

## 7. Decorators on methods

fnloom's decorators record metadata on the function and return it. In a class body they decorate the plain function,
before it becomes a method, so collection finds their records exactly as in a module. Each decorator works on a
method node as it works on a function node:

- `@config.when*`, `@config.default`, `config.matches`: variants `orders__eu`, `orders__us`, selected by the builder's
  config or the namespace's `config=`.
- `@parameterize` and its sugars: one node per row, each bound to the instance. `value()` constants enter keys as
  today.
- `@extract_fields`, `@extract_columns`: the fields of a method node.
- `@check_output`, `@check_output_custom`, `@tag`, `@cache`, `@defaults`, `@inject`, `@resolve`, `@exclude`, and Plan
  11's `@absent_on`: unchanged.

`@staticmethod` goes outermost. A fnloom decorator given a `staticmethod` or `classmethod` object raises `TypeError` when
decorating, saying so (§4.5), rather than recording on an object collection would then unwrap.

**Steps.** A `@pipe_input` or `@pipe_output` step, `step(fn)`, is called as given, with one addition. A step function
that is a function of the instance's class, matched by identity with an entry along the counted MRO, is bound to the
instance, so `step(_clean)` may name a method defined earlier in the body that reads `self`. The match is by identity,
never by a parameter named `self`. A staticmethod given to `step` is its function, unbound.

**`@mutate`.** A mutator defined in a class body is found when the instance is collected, as a module's is, and bound
to that instance. It may target only methods of its own class (`@mutate(orders)` in the body), and applies to that
instance's nodes of them. A class mutator that targets a module function is a `ValueError` at build: with two instances
of the class it would have two `self`s. A module mutator may target a method's function, and applies to every
instance node made from it, in every namespace. Q107's rule that a mutator must come from the build holds: from a built
module, or from a built instance's class.

**Hamilton:** no equivalent. **Rows:** Q104 and Q107 amended (step binding, mutators in classes).

## 8. Kinds of methods

### 8.1 Async and generator methods

A method's kind is read from its function, by the rules for functions (Q32, Q12):

- An `async def` method is a coroutine node on `AsyncDriver`. `build()` refuses it with today's message, which says to
  use `build_async()`.
- A generator or async generator method is refused, as a generator function is, unless Plan 10 makes it an expand node
  (`-> Parallelizable[T]`). An async generator expand method needs Plan 11's async blocks (Q132).
- A sync method on `AsyncDriver` runs in a worker thread (Q65), so the instance is used from a thread other than the
  loop's. A client that must stay on the loop gets `async def` methods.

### 8.2 Absence and fan-out (Plan 10)

A method may decline (`-> list[Order] | Absent`), expand (`-> Parallelizable[str]`) or collect (`Collect[float]`), by
the dynamic execution design's rules, unchanged. `@absent_on` (Plan 11) applies to methods. The return check's
`TypeError` for a method returning `ABSENT` without admitting it names the method: `node 'orders' (method
'app.sales.Sales.orders') returned fnloom.ABSENT ...`.

Under caching, a method's branch key is its ordinary key with the item's fingerprint (dynamic execution §4.6). The
instance's version (§6) sits in the static part, like a `value()` constant, so it costs nothing per branch.

### 8.3 Threads (Plan 11)

With `with_threads(...)`, two method nodes of one instance, or one method in two branches, may run at the same time.
fnloom does not serialize calls per instance: that would cost every method a lock, and most clients are already
thread-safe or pooled. The guide says that an instance's methods must be safe to call concurrently under
`with_threads`, and shows Plan 11's `limits` on a tag the class's methods carry (`@tag(client="sales")`) for a client
that isn't. This is one reason the roadmap builds Plan 13 after Plan 11: the rule is written against the real executor.

**Hamilton:** no equivalent. **Rows:** none new; Q32, Q65 and Q132 hold as written.

## 9. Inspection, drawing and export

- **`NodeView.module`** stays a module name: the defining function's `__module__`, which is the class's module unless
  the method is inherited from a base in another module. Filters, drawing styles by module, and the export's `module`
  keep working, and a graph without instances is unchanged.
- **`NodeView.origin`** is the bound method the node calls. A staticmethod node's origin is its function.
- **`NodeView.instance`** is new: the instance, or `None` for a node that is not a method. Anything that reads a
  node's view, such as a drawing style (Plan 12) or a hook, can use it.
- **`Driver.modules`** stays modules only, typed `tuple[ModuleType, ...]`. **`Driver.instances`**, new on both drivers,
  holds the instances, each once by identity, in build order.
- **`final_vars`** (Q30): a bound method (`sales.total`, a new object at each attribute read) means the node whose
  function is its `__func__` and whose instance is its `__self__`, by identity. The plain function `Sales.total` means
  the node made from it when exactly one instance of the build provides it, and is otherwise a `TypeError` listing the
  namespaces it was found under, as a `@parameterize` function's rows are listed.
- **Drawing** draws a method node as a function node: its name, type and tags. Nothing of the instance is drawn.
- **Export**: a method node's entry gains `"class": "app.sales.Sales"`, its instance's class's qualified name, and
  nothing of the instance's state. Its `version` is the code version of §5. Adding a key is a format change, so a graph
  with method nodes exports the format number after the one Plan 10 leaves current, and every other graph exports
  that format, byte for byte. `diff` reads both. `graph_version` hashes each method node's class name, never the
  instance's fingerprint, so two drivers over equal graphs with different clients have one graph version.
- **Hooks** see method nodes as any node. `NodeStart.view.instance` gives the instance to a hook that needs it.

**Hamilton:** no equivalent. **Rows:** Q164 (`final_vars`, `NodeView.instance`, `Driver.instances`, the export's
`class`), Q30, Q70 and Q95 amended.

## 10. Pickling, copy, rebuild

A `Builder` pickles its modules by name (design §10). An instance has no importable name, so it pickles **by value**,
by Python's own rules, as a result builder, a hook and a `bind=` value already do. It takes its place among the
modules, in order, with its namespace and keywords.

- **An instance that does not pickle** (a client holding a socket or a lock) makes `pickle.dumps(driver)` raise the
  builder's existing `TypeError: cannot pickle: ...`. It names the instance and its namespace, with pickle's own error,
  sorted among the other problems. The guide shows the fix: a `__reduce__` that rebuilds the client in the loading
  process (`return (Sales.from_env, ())`). This is what makes a driver holding an instance usable from a process pool.
- **On load**, the builder is built again with the unpickled copy, so the instance's fingerprint is read again (§6). The
  loaded driver's nodes are bound to the copy, not the original.
- **`copy.copy(driver)`** shares the instance, as it shares the graph: nothing is rebuilt.
- **`rebuild()`** builds again with the same instance object, and reads its class's code and fingerprint again. After
  `importlib.reload(app.sales)`, the instance still has its old class. A rebuild that finds `C` is no longer what its
  module holds under `C.__qualname__` raises the `ValueError` Q83 raises for a stale `with_functions` function: make a
  new instance from the reloaded class, and give it to a new builder.
- **`build_async()`** on the same builder shares the instance with the sync driver.

**Hamilton:** a `Driver` doesn't pickle, and there are no instances to carry. **Rows:** Q165, Q83 amended.

## 11. Hamilton 1.90.0, as probed

Probed on Python 3.14 with a two-method class whose `orders` calls a client:

- `Builder().with_modules(Sales(client)).build()` builds an empty graph, without error. So does `with_modules(Sales)`,
  the class. `graph_utils.find_functions` keeps the members that are plain functions from the given module or its
  submodules. An instance's members are bound methods, and a class is no module, so neither yields a node.
- `ad_hoc_utils.create_temporary_module(sales.orders, sales.total)` raises `ValueError: Missing type hint for self in
  function orders. Please add one to fix.` at build.
- `hamilton.caching.fingerprinting.hash_value` gives a client with no instance attributes the constant
  `'<unhashable>'`, so two such clients share a fingerprint, and hashes a client with attributes by them.

What Hamilton users do instead:

- **The client as an input:** `execute(..., inputs={"client": client})`, with a `client` parameter on every node that
  uses it. Every driver must be handed it, `list_available_variables` lists it, and the cache fingerprints it.
- **A module global,** set at import or by an init function. It is invisible to the graph and to the cache, and every
  driver shares the one client.
- **A closure,** made per app with `create_temporary_module` over functions that close over the client.
- **`@inject` or `@resolve` from config,** with the client in the config.

## 12. Rejected alternatives

- **Nodes with the scope's lifetime** (the issue's alternative): a second kind of node lifetime, with its own rules for
  caching, hooks and pickling. Instances give the app the lifetime with no new kind.
- **The client as an input** (the status quo): every driver is handed it, every consumer takes it, `inputs()` lists
  it, and the cache must fingerprint it. That is the problem the issue describes.
- **Keying a method by its class alone, ignoring the instance** (the issue's "likely"): two drivers sharing a
  `DiskCache`, or one driver rebuilt with a client pointed at another region or account, would serve each other's
  results with no error. The caching design forbids exactly that hit. §6's rules keep the issue's case cheap: the loader
  is `recompute` anyway, a pure method doesn't read `self`, and one line (`__fnloom_fingerprint__` returning `()`) says
  that a class's state is never data.
- **Fingerprinting the instance's `__dict__` by default:** it walks into the client, which must not be fingerprinted,
  and fails on it anyway.
- **Fingerprinting the instance per run:** a Python call per instance per run, for state that, if it changes per run,
  should be an input. Build time matches `value()`, `bind=` and defaults.
- **The whole class's version for every method** (§5): every edit misses every method.
- **Classmethods as nodes:** alternative constructors would become nodes (§4.2).
- **Every base class counting:** a pydantic or `abc` base would contribute dozens of unannotated public methods.
- **Collecting with `getattr` or `inspect.getmembers`:** runs properties and `__getattr__`, which may do I/O.
- **Serializing calls per instance under threads** (§8.3): a lock per call for a guarantee most clients don't need.
- **A new collection control** (`__fnloom_nodes__`, `@fnloom.node_method`): `__all__` and `@exclude` already exist,
  and users know them from modules.

## 13. Calls made in this design (change any of them)

The maintainer's open questions, with the recommended answer first:

1. **Entry point.** `with_modules(instance)`, as the issue spells it (recommended), or a separate
   `with_instances(*instances, namespace=, bind=, config=, replace=)`. The second keeps `with_modules`'s static type
   strict, at the cost of a second method with the same keywords and a shared order for `replace=`.
2. **The instance in the key, by default.** A method that reads `self` is uncacheable unless the instance has a
   fingerprint or the method ignores `self` (recommended, §6), or it is keyed by its class alone (the issue's "likely",
   §12).
3. **When the instance is fingerprinted.** Once at build (recommended), or once per run.
4. **Static and class methods.** Staticmethods are nodes and classmethods are not (recommended), or both are, or
   neither.
5. **Inheritance.** Bases of the user's own code, or of the class's top-level package, count, and others don't
   (recommended). The alternatives: every base but `object`, or the instance's own class only.
6. **Control.** A class-body `__all__` and `@exclude` (recommended), or `@exclude` only.
7. **One method alone.** `with_functions(sales.total)` and `fnloom.node(sales.total)` are accepted (recommended), or
   methods come only from whole instances.
8. **Threads.** No per-instance serialization, documented, with tag `limits` (recommended), or an opt-in lock per
   instance.

The other calls:

9. **Binding by `types.MethodType` at build**, never `getattr`.
10. **`self` read by position**, not by name.
11. **`typing.Self` means the instance's class** in a method's annotations.
12. **Identity for sameness** of instances, everywhere: deduplication, `final_vars`, `Driver.instances`.
13. **A class's mutator targets its own class's methods only.**
14. **`NodeView.module` stays a module name**, with `NodeView.instance` beside it, rather than becoming the instance.
15. **Instances pickle by value**, with a `__reduce__` as the user's fix, rather than being refused.
16. **A stale class refuses `rebuild()`**, as a stale function does (Q83).
17. **An instance attribute shadowing a method is refused**, not resolved either way.

## 14. Register rows

The next free row is Q160: Plan 10 and Plan 11 reserve Q118 to Q138, and Plan 12 ends at Q159. If another plan takes
these numbers first, the plan renumbers them at build. Each row gets its text before its test; every row here is a
`deliberate("Qn")` test, red against Hamilton 1.90.0, since Hamilton has no equivalent to keep.

| Row | Hamilton 1.90.0 (§11) | Disposition | fnloom |
|---|---|---|---|
| Q160 | `with_modules` given an instance, or a class, builds an empty graph without error; the mistake surfaces at `execute` as an unknown output. | FIX, REFUSE | FIX: an instance's methods are nodes (§4.1). REFUSE: a class, a function, a built-in or standard-library instance, and an instance with no nodes fail the build, naming each (§4.5). |
| Q161 | No class is read for nodes: `find_functions` keeps a module's own functions only. | FIX | Public functions and staticmethods along the MRO, in the class and its bases of the user's own code or package; not classmethods, properties, descriptors, instance attributes, private or dunder names, or library bases; class-body `__all__` and `@exclude` (§4.2). |
| Q162 | A bound method in `create_temporary_module` fails the build, `Missing type hint for self`. A long-lived client is an input: every driver is handed it, every consumer takes it, and `list_available_variables` lists it. | FIX | `self` is bound at build, never a dependency, an input or a view parameter, and no instance state is an input (§4.3). |
| Q163 | A client passed as an input is fingerprinted by its `__dict__`, or as the constant `'<unhashable>'` when it has none, so two different clients can share a cache key. | FIX | A method's code version follows `self` attributes (§5). The instance enters only a method that reads `self`, as its declared fingerprint, at build; otherwise that method is uncacheable with a reason, or `@cache(ignore=("self",))`. A client is never fingerprinted (§6). |
| Q164 | No method can be a node, so there is nothing to request by bound method or to inspect. | FIX | A bound method in `final_vars` means its node, by identity; `NodeView.instance`, `Driver.instances`, the export's `class` (§9). |
| Q165 | A `Driver` doesn't pickle. | FIX | A builder pickles its instances by value; an unpicklable one is a `TypeError` naming it; `copy` shares it; `rebuild()` reuses it and refuses a stale class (§10). |

**Existing rows this design amends**, each updated in the task that changes it:

- **Q30**: bound methods in `final_vars` (§9).
- **Q48**: a method's code version follows `self` attributes (§5).
- **Q52**: `with_modules` accepts instances, and the message for a class says to give an instance (§4.5).
- **Q53**: an instance with no nodes, and why each public attribute is not one (§4.5).
- **Q70**: `Driver.instances` beside `Driver.modules` (§9).
- **Q72**: `with_functions` and `fnloom.node` take a bound method (§4.1).
- **Q73**: a class-body `__all__`; `@exclude` on methods (§4.2).
- **Q74**, **Q150**: instances count as modules for duplicates and `replace=` (§4.4).
- **Q83**: `rebuild()` refuses a stale class (§10).
- **Q95**: the export's `class` and its format number (§9).
- **Q104**, **Q107**: steps and mutators bound to the instance (§7).

## 15. Proof and benchmarks

- **Parity with modules.** One parametrized suite builds the same graph from a module and from an instance, through
  every decorator, `namespace=`, `bind=`, `config=`, `replace=`, absence and fan-out, and asserts equal results, node
  views (but `instance` and `origin`), errors and cache outcomes.
- **No user code runs at build.** An instance whose class has a raising property, a raising `__getattr__` and a raising
  `__getattribute__` builds and runs.
- **The key's equality property** (caching design §10), extended: generated classes with methods that do and don't
  read `self`, under random edits to methods, class attributes and instance state, with and without fingerprints. A
  warm run returns what an uncached run returns, and two instances with different declared fingerprints never share an
  entry.
- **Code-version sensitivity**: an edit to a method reached through `self` misses, and an edit to one it doesn't reach
  hits.
- **Pickling**: a driver over an instance with a `__reduce__` runs in a `ProcessPoolExecutor`. One without is refused
  with its name.
- **Determinism**: names, errors, export and keys don't depend on `PYTHONHASHSEED` or on instance `id`s.
- **Benchmarks** (`benches/run.py`, CodSpeed): a 200-node chain of methods against the same chain of functions. A build
  costs at most 10% more, a run is within noise plus one bound-method call per node, and a graph without instances is
  unchanged on the instruction-count gate.

## 16. Change log

- **2026-11-05:** drafted for approval.
