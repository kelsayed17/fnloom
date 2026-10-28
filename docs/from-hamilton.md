# Coming from Hamilton

Good news: fnloom uses [Apache Hamilton (incubating)](https://github.com/apache/hamilton)'s model, so **a module of Hamilton
functions is already a module of fnloom nodes.** Your functions don't change. Your driver code changes in a few small,
mechanical ways.

fnloom isn't a drop-in replacement, though. There's no `hamilton` module to import and no compatibility layer.
Hamilton 1.90.0 is fnloom's test oracle: every place the two differ on purpose is a row in
[the register of differences](quirk-register.md), and most rows have a test that checks both sides.

## What you get by switching

- **Deep graphs just work.** Hamilton's graph walk recurses, so a chain of about 1,000 nodes raises `RecursionError`.
  fnloom has no depth limit.
- **Faster repeat runs.** `prepare` compiles a request once, and each run after that is a loop in Rust: up to 24×
  faster than Hamilton's `execute` on the [benchmarks](benchmarks.md).
- **Typos are errors.** A misspelled input or override raises instead of being silently ignored.
- **Stable order.** Nodes run in the same order however you list outputs, so logs and side effects are reproducible.
- **Real types.** fnloom ships `py.typed`, and its decorators keep your function's signature for type checkers.
- **A lighter install.** No runtime dependencies, where Hamilton pulls in numpy, pandas and more.

## What carries over

- **Name-wiring.** A function's name is its node and its parameter names are its edges. A parameter that names no
  function is an input, and one with a default is optional.
- **Modules.** The public functions defined in a module are its nodes. Private and imported functions aren't.
- **The builder.** `fnloom.Builder().with_modules(...).build()` mirrors `driver.Builder().with_modules(...).build()`.
  `fnloom.build(*modules)` is the short form.
- **`execute`.** `execute(final_vars, inputs=..., overrides=...)` returns a dict of the requested outputs, in the
  order you asked for them.

## Decorators

Most decorators fnloom has so far take the same arguments as Hamilton's, so usually only the import changes.
`@cache` is the exception, below.

<div class="fl-compare" markdown>

| Hamilton 1.90.0 | fnloom |
| --- | --- |
| `from hamilton.function_modifiers import parameterize, source, value` | `from fnloom import parameterize, source, value` |
| `@parameterize(row={"param": source("node")})` | The same |
| `@parameterize(row={"param": value(v)})` | The same. `v` is passed as is, never copied, as in Hamilton |
| `@tag(owner="data")` | The same, imported from `fnloom`. A value must be a `str`, not a list (Q36) |
| `@cache(behavior="recompute")`, `@cache(behavior="disable")` | The same, imported from `fnloom` ([Caching](#caching)) |
| `@cache(behavior="ignore")` on a producer | `@cache(ignore=("name",))` on the consumer instead (Q50) |
| `@cache(format=...)` | The same, imported from `fnloom`: a format's name, or a `fnloom.Format` ([Caching](caching.md#stores)) |
| `target_=...` | Not there. fnloom adds `version=` to pin a code version |
| `@config.when`, `when_not`, `when_in`, `when_not_in` | The same, imported from `fnloom`, with the same `foo__variant` naming ([Config](writing-nodes.md#config)) |
| `@config.when(name="x")` | The same on `when`, `when_not` and `when_in`. On `when_not_in`, Hamilton reads `name` as a config key, while fnloom reads it as the node name, as on the other forms (Q59) |
| `@config.default` | Not in Hamilton: the variant used when no other matches |
| `@config(lambda cfg: ...)` | Not there |
| A row as `(bindings, docstring)`, `group(...)`, `parameterize_sources`, `parameterize_values` | Not yet. A row is a mapping |

</div>

## What changes in your code

<div class="fl-compare" markdown>

| | Hamilton 1.90.0 | fnloom |
| --- | --- | --- |
| `inputs` and `overrides` | `overrides` is the second positional parameter | Keyword-only (Q31) |
| A misspelled input or override | Ignored | `ValueError` naming it (Q4, Q5) |
| `final_vars="total"` | One output per character | `TypeError` (Q6) |
| A function in `final_vars` | Its node | `TypeError` for now: pass names (Q30) |
| Overriding an input | Only when it's also supplied | Any node. It beats or replaces the input (Q7) |
| `what_is_upstream_of` | `HamiltonNode` objects | Sorted names, including the ones you named (Q28) |
| Reusing a request | Re-planned on every call | `prepare(...)` returns a plan to run again (Q3) |

</div>

## What changes in behaviour

<div class="fl-compare" markdown>

| | Hamilton 1.90.0 | fnloom |
| --- | --- | --- |
| A chain of about 1,000 nodes | `RecursionError` | Runs. Nothing recurses over depth (Q25) |
| A dependency cycle | `RecursionError` when executed | `ValueError` naming it, at build (Q27) |
| Execution order | Depends on the order of outputs and parameters | Topological, ties broken by name (Q26) |
| `*args`, `**kwargs`, positional-only | Treated as inputs | `ValueError` at build (Q29) |
| `async def` and generator functions | The coroutine or generator is the value | `ValueError` at build (Q32) |
| A function passed where a module goes | An empty graph | `TypeError` (Q52) |
| A module with no nodes, such as a package that only re-exports | Builds; its nodes are unknown at `execute` | `ValueError` at build, saying why each function was skipped (Q53) |
| A row naming no parameter of its function | `InvalidDecoratorException` | `ValueError`, when decorating (Q33) |
| A row's defaulted parameter bound to an unsupplied `source()` | `KeyError` when run | The default applies (Q35) |
| A config key | A node, passed to parameters of its name | Never a node or a value; a parameter of that name is an input (Q56) |
| A config key the config doesn't have | Reads as `None`, so `when_not` matches | Never matches (Q57) |
| A needed node no config variant matches | An input, failing at `execute` | `ValueError` at build, naming what each variant needs (Q57) |
| Two matching variants, or a variant defined twice | Names the wrong function; keeps the last silently | `ValueError` at build naming both, or the lines (Q58) |
| `@config.when` without parentheses, `when_in(key="ab")` | The node vanishes; substring matching | Refused when decorating (Q59) |
| The function's `__name__`, and its tags | Renamed in place; a `hamilton.config` tag | Untouched; `NodeView.config` holds the conditions (Q60) |

</div>

## Caching

fnloom's cache keeps Hamilton's names for what they mean, and differs wherever Hamilton's cache can return a wrong
value (Q46 to Q51 in [the register](quirk-register.md)). The guide is [Caching](caching.md).

<div class="fl-compare" markdown>

| | Hamilton 1.90.0 | fnloom |
| --- | --- | --- |
| Turning it on | `Builder().with_cache(path=...)` | `Builder().with_cache(fnloom.DiskCache(path))`, or `fnloom.MemoryCache()` |
| Per-node behavior | `@cache(behavior=...)`, or lists of names given to `with_cache` | `@cache(behavior=...)` on the function only |
| `ignore` | `@cache(behavior="ignore")` on the producer hides it from every consumer | `@cache(ignore=("name",))` on a consumer, per parameter; the producer is `disable` (Q50) |
| A code version | The function's source, hashed on every run | Its code, helpers, constants and libraries, read once at build (Q16, Q48) |
| A helper or global the node reads | Not seen | Seen; a mutable global makes the node uncacheable unless it gives `@cache(version=...)` |
| A value that cannot be hashed, nested in a list | A constant hash | The nodes reading it run, and `explain_cache` says why (Q47) |
| `@cache(format=...)` | Formats from a registry of data savers; an unknown name fails every run; `json` turns a tuple into a list on a hit | `"pickle"`, `"json"` or a `fnloom.Format`, checked at build (Q55); `json` stores only what loads back exactly (Q54) |
| A corrupt stored entry | Fails the run | A miss, with a `CacheWarning` (Q51) |
| What a run did with each node | A log that reports a hit on every lookup | `driver.explain_cache(...)` |

</div>

## Not there yet

fnloom is pre-alpha. The core, the native path, `@parameterize`, `@tag`, `@config`, graph inspection, type checks
and result caching are done. These Hamilton features come next, roughly in this order:

1. **More decorators:** `@extract_columns` and `@extract_fields`.
2. **Adapters and lifecycle hooks**, result builders and async execution.
3. **Graph visualisation.**
4. **Dynamic execution**, materializers, `@subdag`, `@check_output` and `@pipe`.

If your pipeline depends on one of these, it's worth waiting for it before you switch.
