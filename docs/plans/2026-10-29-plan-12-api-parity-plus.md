# fnloom Plan 12: every row better than Hamilton

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan PR by PR, with
> superpowers:test-driven-development inside each task and an independent reviewer subagent per PR. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Status:** draft for the maintainer's approval, 2026-10-29. Nothing is built until it is approved.

**Numbering:** Plan 10 (typed absence and sequential fan-out) and Plan 11 (the scheduler, `with_threads`, `run_many`,
`absent_on`) are taken by the dynamic execution design (#30), which also takes register rows Q118 to Q138. This plan
is Plan 12, and its rows start at Q139. It touches none of #30's files except the register and the roadmap, where it
appends rows only.

**Goal:** every row of [Coming from Hamilton](../from-hamilton.md)'s three tables (Decorators, What changes in your
code, Caching) is strictly better than Hamilton 1.90.0. Today 4 rows are worse and 7 are only sideways. Each item
below gives fnloom everything Hamilton's version did, and keeps fnloom's guarantees: determinism (same text and
order on every hash seed), named errors (every refusal names the node, key or argument), and no silent wrong results.

**Architecture:** all of it is Python except item 3, where tag values reach the Rust core. No new runtime
dependency. Every new setting on `Builder` is carried by pickling, `copy`, `rebuild()` and `build_async`, as
Plans 5 to 9 settled. Anything that changes what a node computes enters its cache key and the export.

**Tech Stack:** as Plans 1–9: Python 3.14, Rust core via maturin, ty strict, ruff ALL, rumdl, pytest, and
`apache-hamilton==1.90.0` as the oracle. Build with `maturin develop --features test-hooks`; test with
`UV_NO_SYNC=1`; gate with `scripts/check.sh`.

## Global Constraints

- Pre-1.0: an API may break where an item says so, with a CHANGELOG `[Unreleased]` entry under **Changed**.
- Every deliberate difference from Hamilton is a register row with a `deliberate("Qn")` test.
- Every kept behaviour has a differential test in `tests/differential/` running both libraries.
- No output may depend on `PYTHONHASHSEED`, dict insertion of a set, or the machine (Graphviz present or not).
- fnloom is generic: examples use neutral domains (sales, taxes, regions).

## Decisions, one per item

Each item says what Hamilton does, what fnloom builds, and why the result is strictly better. Change any before the
build starts; the recommended option is the one written, and alternatives are noted where they are real.

### 1. Drawing style (worse today) → Q139

Hamilton: `display_*(graphviz_kwargs=, custom_style_function=, show_schema=, hide_inputs=, deduplicate_inputs=)`,
DOT only, with the style function given a `HamiltonNode` and its node class.

fnloom builds:

- `Drawing.styled(*, graph=None, node=None, edge=None, style=None) -> Drawing`, returning a new frozen drawing.
  `graph`, `node` and `edge` are `fnloom.Style` objects applied to every graph, node and edge; `style` is a callable
  `style(view: NodeView, drawn: DrawnNode) -> Style | None`, called once per node in drawing order.
- `fnloom.Style(*, fill=None, stroke=None, font_color=None, stroke_width=None, dashed=None, shape=None, dot=None,
  mermaid=None)`: a frozen, neutral vocabulary that both writers map (`fill` → DOT `fillcolor` / Mermaid `fill`, and so
  on), plus `dot={...}` and `mermaid={...}` passthroughs that only their writer reads. A colour is a `#rrggbb` string
  or a CSS/X11 name from a fixed list; anything else is a `ValueError` naming the node and the attribute, so a typo
  can't silently draw black. Passthrough keys must be identifiers and values `str`; they are quoted by the writer.
- Precedence, later winning: kind and mark styles (today's), then `node`, then `style(...)`. Edges: `edge`, then the
  path's red.
- Display options on every `draw*` method and the CLI: `show_types=True`, `show_tags=()` (tag names whose values are
  printed under the type, a tuple joined by commas in written order), `show_schema=False` (the fields of a return
  type that is a `TypedDict`, dataclass or `NamedTuple`, one `name: type` line each, in declaration order), and
  `hide_inputs=False`.
- `DrawnNode` gains `view: NodeView | None` (excluded from equality and `repr`) so a style function can be re-run.
- Determinism: attributes are written in a fixed order (vocabulary order, then passthrough keys sorted).

Better than Hamilton: one style call drives DOT, Mermaid and SVG alike; the style function gets the full `NodeView`
(tags, config, namespace, params) rather than a node and a class name; bad colours fail loudly; `show_schema` reads
real types rather than an unchecked string tag (Q114). Not carried over: `deduplicate_inputs`, since fnloom already
draws each input once.

### 2. Step namespaces and `on_output` (worse today) → Q140

Hamilton: `pipe_input(..., namespace=...)`, `pipe_output(..., namespace=..., on_output=...)` and
`step(...).named(name, namespace=...)`. `namespace=...` (the default) prefixes `<fn>.`, `None` prefixes nothing, a
`str` prefixes `<str>.`.

fnloom builds the same three arguments with the same meanings, `<fn>.with_<step>` staying the default. Additions:

- A prefix-free or shared-namespace step whose name another node has is a `ValueError` at build naming both, as
  any duplicate is (Hamilton, probed in the PR, is expected to fail later or overwrite).
- `pipe_output(..., on_output=("a", "b"))` applies to every step; a step's own `.on_output(...)` wins, as the more
  specific. `on_output` naming a field the function doesn't extract is a `ValueError` at build naming it.
- A namespace must be a dotted identifier path (checked by `checked_namespace`, Q74), refused when decorating.
- The name is stable across config, as today (Q104).

### 3. `@tag` sequence values (worse today) → Q36 amended, Q141

Hamilton: a value is a `str` or a `list[str]`; `list_available_variables(tag_filter=...)` matches a list value if any
item matches.

fnloom builds:

- `@tag(owner=("ann", "bo"))` or a `list`: stored as a `tuple[str, ...]` in written order. An empty sequence,
  repeated items, a non-`str` item, or an unordered collection (`set`, `frozenset`, `dict`) is a `TypeError` or
  `ValueError` when decorating, naming the tag. `NodeView.tags` becomes `Mapping[str, str | tuple[str, ...]]`
  (**Changed**, typed).
- The Rust core's tag value becomes an enum of one string or several; the core's tag map stays a `BTreeMap`.
- Filtering, which fnloom doesn't have at all today: `NodeViews.tagged(**query) -> tuple[NodeView, ...]`, in the
  views' order. A query value is a `str` (matches an equal value or a tuple holding it), a sequence (matches any of
  them) or `fnloom.ANY` (the tag is present). Unknown tag names match nothing; a malformed query is a `TypeError`.
  The CLI's `draw` and `export` take `--tag key=value` (repeatable, AND-ed).
- Export writes a tuple as a JSON list; `diff` compares it; `graph_version` hashes it in order. Drawing shows it
  via `show_tags` (item 1).

### 4. A built-in printing hook (worse today) → Q142

Hamilton: `lifecycle.PrintLn(verbosity=1|2, print_fn=print, node_filter=...)`.

fnloom builds `fnloom.PrintNodes(verbosity=1, *, print_fn=print, nodes=None, width=50)`, a `Hook`:

- verbosity 1: `node 'total' ran in 1.2 ms`, `node 'total' failed in 0.4 ms: ValueError(...)`, or
  `node 'total' loaded from cache` (a hit, which Hamilton reports as a run, Q85).
- verbosity 2 adds `inputs: a=1, b=2` (sorted) and `result: ...`, each cut to `width` characters with `LogFailures`'s
  bounded repr (`_cut`), shared rather than copied.
- `nodes` is a sequence of names (unknown names are a `ValueError` at the first run, naming them) or a callable
  `(NodeView) -> bool`, which sees tags, namespace and config, where Hamilton's sees a name and a tag dict.
- Times are taken in `before_node`/`after_node`, keyed by `(run_id, node)`, so it stays correct on Plan 11's threads.
- Any other `verbosity` is a `ValueError`; `width < 10` is a `ValueError`.

### 5. Driver-level cache behaviour per node (sideways) → Q143

Hamilton: `with_cache(path, default=, recompute=, ignore=, disable=, default_behavior=)`, each a list of names or
`True`.

fnloom builds `with_cache(store, *, recompute=(), disable=(), default=(), default_behavior="default")`:

- Each list holds node names, or is `True` for every node. A name in two lists, or a name that is no node at build,
  is a `ValueError` naming it with a `did you mean` (Q71). `default_behavior` takes `"default"`, `"recompute"` or
  `"disable"`.
- Precedence, most specific first: a driver list naming the node; the node's `@cache(behavior=...)`; then
  `default_behavior`. `True` counts as `default_behavior`, not as a name. The precedence is documented and tested
  in both directions.
- `ignore` stays per consumer parameter (Q50): `ignore=` on `with_cache` takes `{"consumer": ("param",)}`, so it
  can't hide a producer from every reader.
- `explain_cache` gives the outcome's source (`"driver"`, `"decorator"` or `"default"`) in its reason.
- Hamilton's precedence between its lists and the decorator is probed in the PR; a difference is a row in Q143.

### 6. A flattening frame builder (sideways) → Q144

Hamilton: `PandasDataFrameResult` flattens a frame output into `output.column` columns, aligns indexes with a
logged warning, and makes a one-row frame from all scalars.

fnloom builds `fnloom.frame(library="pandas" | "polars", *, flatten=True, separator=".", join="strict")`, a
result builder for `with_result_builder`. The library is imported when the builder is made, never by fnloom itself:

- A frame output becomes `<output><separator><column>` columns, in request order then column order; a series is
  one column; scalars are broadcast; all scalars make one row, as Hamilton does.
- `join="strict"` (default): pandas indexes must be equal, polars lengths equal, else a `ValueError` naming the
  outputs and their lengths or index differences. `join="outer"` aligns pandas indexes as Hamilton does, without a
  log; polars refuses `"outer"` with a `ValueError`, since it has no index.
- A flattened name colliding with another output is a `ValueError` naming both.
- `with_result_builder(pandas.DataFrame)` keeps working unchanged (Q89).

### 7. A build-time hook (sideways) → Q145

Hamilton: `GraphConstructionHook.run_after_graph_construction(graph=, config=)`.

fnloom builds `Hook.after_build(self, event: BuildEnd)`, with `fnloom.BuildEnd(nodes: NodeViews, config:
Mapping[str, object], modules: tuple[ModuleType, ...])`. It fires from `build()`, `build_async()` and `rebuild()`,
once per hook in order. Every hook sees the event; their errors are raised together as one `ExceptionGroup` and
the build fails (Q23's rule). A hook that is a `before_*` / `after_*` subclass only is unaffected.

Better than Hamilton: frozen views with config and tags rather than a mutable graph; and it can refuse a build,
which also gives Hamilton's `StaticValidator` a home. Q117 changes from DROP to FIX: a raising `after_build` is the
fnloom `StaticValidator`, carried through `rebuild()`, which was Q117's condition to reconsider.

### 8. `render("x.svg")` without Graphviz (sideways) → Q146

Recommended: **build a dependency-free SVG writer, gated by a spike.** `Drawing.to_svg(*, orient="LR", legend=True)`
in pure Python: longest-path layers, barycentre ordering (a fixed number of sweeps, ties by name), straight edges
with arrowheads, nodes sized from text with a fixed character-width table. `render("x.svg", engine="dot" |
"builtin")`: `"dot"` stays the default, so a file never silently changes with the machine; `"builtin"` needs
nothing. `_repr_svg_` uses `dot` when present and `builtin` otherwise, since a notebook preview is not a file.

The quality bar, checked by the spike on the docs' example graphs and a 200-node generated graph: no overlapping
nodes or labels, every edge pointing in the orient direction, and edge crossings no more than 1.5× `dot`'s. If the
spike misses it, the fallback is documented in the PR and here: no builtin engine, and the missing-`dot` error
gives the install command for the platform (`brew install graphviz`, `apt-get install graphviz`, `winget install
graphviz`), plus `render("x.mmd")` and `render("x.dot")` as the dependency-free choices.

Either way the missing-`dot` message improves. The plan reports which was chosen when the PR opens.

### 9. Per-field cache control (sideways) → Q147

Hamilton: `@cache(..., target_="a")` applies the behaviour to the named output node of a multi-output function.

fnloom today stores only the producer; a field is keyed but always recomputed from it. fnloom builds
`@cache(..., fields={"a": "disable", "frame": fnloom.FieldCache(behavior="default", format="json")})`:

- A field with a policy is stored and served on its own, keyed by the producer's key and the field name. That lets
  a producer be `disable` (its whole value can't be pickled) while one field is stored, which Hamilton can't do
  because there the producer node runs first.
- `"recompute"` on a field makes a reader of it rerun the producer; `"disable"` gives readers of that field the
  producer-disable semantics (Q50).
- A field name the function doesn't extract is a `ValueError` at build naming it; `fields=` on a function with no
  extraction is a `TypeError` at build.
- `explain_cache` reports fields as their own entries.

### 10. A config predicate (sideways) → Q148

Hamilton: `@config(lambda cfg: ..., target_name=, config_used=)`: a callable given the whole config dict, whose keys
are unknown unless `config_used` lists them.

fnloom builds `@config.matches(predicate, *, name=None)`. The predicate's parameters are config keys, as
`@resolve`'s are (Q110): a defaulted one is optional, a missing required key never matches (Q57). It must return a
`bool`; anything else is a `TypeError` at build naming the function. It combines with `when*` conditions (all must
hold) and with `@config.default`, and two matching variants fail as in Q58. Its code version (Q16, Q48) enters the
cache key of the variant it selects, and the export shows the condition as `matches(<qualname>)` with that version,
so `diff` sees an edited predicate. `NodeView.config` holds it as a `Condition` of kind `"matches"`.

### 11. Three smaller ones

- **Loose edges → Q149.** `Builder.without_edge_type_checks(*consumers)`: with no argument, every edge; with node
  names, only the edges into those nodes. An unknown name is a `ValueError` at build. The setting is in the export
  as `edge_checks`, and in `graph_version`. Input checks are separate (`without_input_type_checks`,
  Q10). Better than Hamilton's all-or-nothing adapter, which also drops input checks.
- **`config=` per namespace → Q112 amended.** `with_modules(..., namespace="eu", config={"region": "eu"})`. The
  namespace's nodes see the driver's config overlaid with these keys, for `@config` selection, `@resolve` and
  `@config.matches`. Keys are `str`; `config=` without `namespace=` is a `TypeError`; a second `config=` for the same
  namespace that differs is a `ValueError`. `Driver.config` stays the driver's own; the export records each
  namespace's overlay, and it enters `graph_version`. Q112's "DROP: `config=` per copy" becomes FIX.
- **Explicit replacement → Q74 amended, Q150.** `with_modules(module, replace=("total",))`: the module's `total`
  replaces the node `total` that modules given earlier define, for every reader. Under `namespace=`, names are
  the namespace's. A name no earlier module defines, or one this module doesn't define, is a `ValueError` at build
  naming it. Without `replace=`, a name defined twice still fails (Q74). The export records `replaced` with the
  winning function's module, and `NodeView.replaced` names the module replaced. Better than
  `allow_module_overrides()`: one named node, not every collision, and a typo fails.

## Register rows

| Row | Disposition | One line |
| --- | --- | --- |
| Q139 | FIX | Drawing style through `Style`, DOT and Mermaid alike, validated colours, deterministic attribute order |
| Q140 | KEEP / FIX | Step `namespace=` and `on_output=`: Hamilton's names; collisions and bad fields fail at build |
| Q36 | amended: REFUSE → KEEP / FIX | Sequence values kept, as tuples; unordered collections and empty or repeated items refused |
| Q141 | FIX | `NodeViews.tagged(...)`: Hamilton's `tag_filter` matching, in view order |
| Q142 | KEEP / FIX | `PrintNodes`: `PrintLn`'s two levels; cache hits reported as hits; bounded reprs; view filter |
| Q143 | KEEP / FIX | Driver-level per-node behaviour, unknown names refused, precedence documented |
| Q144 | KEEP / FIX | `frame(...)` flattening; strict alignment by default, no log-only warnings |
| Q145 | FIX | `after_build` on frozen views; can refuse a build |
| Q117 | amended: DROP → FIX | `after_build` is the `StaticValidator` |
| Q146 | FIX | Builtin SVG engine (or the documented fallback); never chosen silently for a file |
| Q147 | FIX | Per-field cache policy; a field stored apart from a disabled producer |
| Q148 | FIX | `config.matches`: keys known from the signature, versioned, exported |
| Q149 | FIX | Edge checks off per consumer or for all, recorded in the export |
| Q112 | amended: DROP → FIX for `config=` | Per-namespace config overlay |
| Q74, Q150 | FIX | `replace=`: one named replacement, typos fail |

## Differential tests against Hamilton 1.90.0

Each in `tests/differential/`, comparing values and names, never text formats:

- Item 2: the node names of `pipe_input`/`pipe_output` under `namespace=...`, `None`, `"ns"`, and `.named(n,
  namespace=)`; each step's value; `on_output` picking fields.
- Item 3: `tag_filter` results (as sorted names) for `str`, list and `None` queries over list-valued tags, against
  `tagged(...)`.
- Item 4: the set of nodes `PrintLn` reports, and which ones report inputs and results at verbosity 2 (output
  compared as node-name lists, not text).
- Item 5: the outcome of each node for each combination of a list and `@cache(behavior=)`.
- Item 6: `PandasDataFrameResult` against `frame("pandas", join="outer")` on scalars, series and frames, compared
  with `DataFrame.equals`.
- Item 7: the node names and config a `GraphConstructionHook` sees against `BuildEnd`.
- Item 9: which `target_` outputs Hamilton recomputes against fnloom's field outcomes.
- Item 10: which variant `@config(lambda cfg: ...)` picks against `@config.matches` over a config grid.
- Item 11: a namespace's `config=` against `@subdag(config=)` values; `replace=` against
  `allow_module_overrides()` values when exactly one name collides.
- Items 1 and 8 have no Hamilton value to compare (both only style text); they get golden-text tests run under
  three `PYTHONHASHSEED` values.

## PR breakdown

Each PR: TDD, the register rows, the `from-hamilton.md` rows rewritten to say how fnloom is now better, a CHANGELOG
`[Unreleased]` entry, `scripts/check.sh` green, an independent reviewer subagent, then the maintainer's go-ahead to
squash-merge. Branches merge `main` in; nothing is rebased or force-pushed.

1. **PR A: tags (item 3).** Rust tag enum, `@tag` sequences, `NodeViews.tagged`, export/diff/`graph_version`, CLI
   `--tag`. First, because items 1 and 4 read tags.
2. **PR B: drawing (items 1 and 8).** `Style`, `styled`, display options, then the SVG spike; the spike's result
   decides the second half of the PR. If the spike fails the bar, PR B ships the fallback and says so.
3. **PR C: hooks (items 4 and 7).** `PrintNodes`, `after_build`/`BuildEnd`, Q117 amended.
4. **PR D: steps (item 2).**
5. **PR E: caching (items 5 and 9).** Driver-level lists and per-field policies share `_caching.py` and
   `explain_cache`.
6. **PR F: config and modules (items 10 and 11).** `config.matches`, namespace `config=`, `replace=`, loose edges;
   all resolved in the scan.
7. **PR G: results (item 6).** `frame(...)`.

A to G are independent except A before B and C. PRs D to G can be reviewed in any order.

## Review Focus

1. A `Style` colour or passthrough value containing `"` or `\n`: must be refused or escaped, never break the DOT or
   Mermaid text (PR B test `test_style_values_are_escaped`).
2. A tag tuple drawn, exported and re-imported by `diff` on two hash seeds: identical text (PR A
   `test_tuple_tags_are_hash_seed_stable`).
3. `PrintNodes` on the async driver and on a cache hit: one line per node, hits saying so (PR C
   `test_print_nodes_async_and_hits`).
4. `with_cache(recompute=True)` combined with `@cache(behavior="disable")`: the decorator's disable must win over
   `True`, since `True` is not a name (PR E `test_true_list_does_not_beat_decorator`).
5. `replace=` naming a node that a `@config` variant defines only under another config: a `ValueError` at build
   under that config, not a silent no-op (PR F `test_replace_needs_the_node_under_this_config`).

## Open questions for the maintainer

1. **Item 8:** build the builtin SVG engine behind the spike as written, or skip straight to the excellent error?
2. **Item 3:** `NodeView.tags` becomes `Mapping[str, str | tuple[str, ...]]`, which breaks type-checked code that
   reads a tag as `str`. Accept the break, or add `NodeView.tag_values` as the new field and keep `tags` `str`-only
   (refusing to build a view of a tuple tag)? Recommended: accept the break.
3. **Release:** ship these in 0.3.0 with Plan 10, or as 0.4.0 after it? Recommended: whichever lands first; no
   dependency either way.
