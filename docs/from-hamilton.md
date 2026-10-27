# Coming from Hamilton

fnloom keeps [Apache Hamilton](https://github.com/apache/hamilton)'s model, so a module of Hamilton functions is
already a module of fnloom nodes. It is not a drop-in replacement: there is no `hamilton` module, and existing driver
code changes in small, mechanical ways. Hamilton 1.90.0 is fnloom's test oracle, and every place they differ on
purpose is a row in [the register of differences](quirk-register.md), with a test that asserts both behaviours.

## What carries over

- **Name-wiring.** A function's name is its node and its parameter names are its edges; a parameter that names no
  function is an input, and one with a default is optional.
- **Modules.** Public functions defined in a module are its nodes; private and imported functions are not.
- **The builder.** `fnloom.Builder().with_modules(...).build()` mirrors `driver.Builder().with_modules(...).build()`,
  and `fnloom.build(*modules)` is the short form.
- **`execute`.** `execute(final_vars, inputs=..., overrides=...)` returns a dict of the requested outputs, in the
  order requested.

## Decorators

The decorators fnloom has so far take the same arguments as Hamilton's; only the import changes.

<div class="fl-compare" markdown>

| Hamilton 1.90.0 | fnloom |
| --- | --- |
| `from hamilton.function_modifiers import parameterize, source, value` | `from fnloom import parameterize, source, value` |
| `@parameterize(row={"param": source("node")})` | The same |
| `@parameterize(row={"param": value(v)})` | The same; `v` is passed itself, uncopied, as in Hamilton |
| `@tag(owner="data")` | `from fnloom import tag`, the same; values are `str` |
| A row as `(bindings, docstring)`, `group(...)`, `parameterize_sources`, `parameterize_values` | Not yet; a row is a mapping |

</div>

## What changes in your code

<div class="fl-compare" markdown>

| | Hamilton 1.90.0 | fnloom |
| --- | --- | --- |
| `inputs` and `overrides` | `overrides` is the second positional parameter | Keyword-only (Q31) |
| A misspelled input or override | Ignored | `ValueError` naming it (Q4, Q5) |
| `final_vars="total"` | One output per character | `TypeError` (Q6) |
| A function in `final_vars` | Its node | `TypeError` for now; names only (Q30) |
| Overriding an input | Only when it is also supplied | Any node; it beats or replaces the input (Q7) |
| `what_is_upstream_of` | `HamiltonNode` objects | Sorted names, the named nodes included (Q28) |
| Reusing a request | Re-planned on every call | `prepare(...)` returns a plan to run again (Q3) |

</div>

## What changes in behaviour

<div class="fl-compare" markdown>

| | Hamilton 1.90.0 | fnloom |
| --- | --- | --- |
| A chain of about 1,000 nodes | `RecursionError` | Runs; nothing recurses over depth (Q25) |
| A dependency cycle | `RecursionError` when executed | `ValueError` naming it, at build (Q27) |
| Execution order | Depends on the order of outputs and parameters | Topological, ties broken by name (Q26) |
| `*args`, `**kwargs`, positional-only | Treated as inputs | `ValueError` at build (Q29) |
| `async def` and generator functions | The coroutine or generator is the value | `ValueError` at build (Q32) |
| A row naming no parameter of its function | `InvalidDecoratorException` | `ValueError`, both when decorating (Q33) |
| A row's defaulted parameter bound to an unsupplied `source()` | `KeyError` when run | The default applies (Q35) |

</div>

## Not there yet

Plan 1 is the core and the native path. These Hamilton features come in later plans, in roughly this order:

1. More decorators and configuration: `@extract_columns`, `@extract_fields` and `@config.when`, with type checks on
   edges and inputs. `@parameterize`, `source`, `value` and `@tag` are here.
2. Adapters and lifecycle hooks, result builders, and async execution.
3. Graph visualisation.
4. Dynamic execution, caching, materializers, `@subdag`, `@check_output` and `@pipe`.

There will be no compatibility layer: code moves to fnloom's API rather than running unchanged.
