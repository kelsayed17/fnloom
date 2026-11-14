# Changelog

All notable changes to fnloom are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project will adhere to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) from its first release. Before 1.0, a minor release may
change the public API; such changes are listed under **Changed** and **Removed** with what to write instead.

## [Unreleased]

### Added

- Typed absence (Q135). A node annotated `-> T | fnloom.Absent` may return `fnloom.ABSENT`, a final singleton
  distinct from `None`. Each parameter reading a node that may be absent decides by its own signature, at build:
  one admitting `Absent` receives `ABSENT`, one with a default takes its default, and any other skips its node,
  which is absent too; a requested output that is absent is `ABSENT` in the result. `NodeView.may_be_absent` and
  `Param.admits_absent` say so before anything runs. Returning `ABSENT` without admitting it is a `TypeError`
  naming the node. Edge checks read `T | Absent` as `T`. Two parameters of one node reading one source that may
  be absent must agree on admitting `Absent`, or the build fails naming the node, the source and both. `ABSENT`
  pickles and copies to itself, and `bool(ABSENT)` is a `TypeError`: compare with `is ABSENT`. A node annotated
  `-> Absent` alone feeds any parameter, which it never gives a value. `build_async()` follows the same rules, for an
  `async def` node and a sync one, and a node that returns `ABSENT` there without admitting it is the same
  `TypeError`.
- Absence at the edges of a run (Q135; amends Q19, Q84, Q95). An `ABSENT` input is a `TypeError` naming it, typed
  or `Any`, with or without type checks: leave the input out instead. An override may be `ABSENT` for a node that may
  be absent, forcing its consumers' skip, and is a `TypeError` for any other node. `RunEnd.skipped` lists each skipped
  node as a `(node, branch)` pair, in the order skipped, `branch` being `()` outside a block; a skipped node fires no
  node event, so `PrintNodes` prints nothing for it. Like a value check's failure, an `ABSENT` input or override fires
  `before_run` with no nodes, then `after_run` with `ok=False`. A result builder gets an absent output as `ABSENT`,
  and `frame()` refuses one with a `ValueError` naming each. `@extract_fields` and `@extract_columns` take a producer annotated
  `R | Absent`, and every field is absent when it declines. `@check_output*` and `with_output_type_checks()` don't
  run on `ABSENT` (`NodeEnd.checks` is `()`). A graph with a node that may be absent exports `fnloom.export/2`, each
  node with `may_be_absent`; any other graph still exports `fnloom.export/1`, byte for byte, and `fnloom diff` reads
  both. Drawings mark such a node `may be absent`, drawn dashed and named in the legend.
- `ObserverBase.skipped(node, *, branch)`, the binding's forward of the core's `Observer::skipped`. An observer whose
  class overrides neither `pre_node` nor `post_node` is sent no node events, so a run makes no keyword arguments for
  it.
- Absence on the async driver. `build_async()` builds a node that declines, and its runs follow the same rules as
  `build()`'s: a skipped node runs nothing, fires no node event, stores nothing in the cache and is listed in
  `RunEnd.skipped`. The return check runs on `async def` nodes and on sync nodes, in a thread or inline, refusing
  `ABSENT` only from a node whose annotation doesn't admit it.
- Rust core: absence. `Dependency::Admits { optional }` is a third way to read an input, `NodeSpec.declines` marks a
  node whose value may be absent, and `Node.may_be_absent` is known at build. `Evaluate::is_absent` (default
  `false`) recognises an absent value, and `Observer::skipped(node, branch)` reports a node skipped because a
  `Required` argument is absent. `Admits` passes the absent value, `Optional` omits it, and `Required` skips the
  consumer, which is absent too. `Plan::needs_rules` says whether a plan follows these rules; one that doesn't runs
  the plain loop unchanged. `BuildError::ConflictingInput` refuses a spec listing one input twice with different
  dependencies, which no one dependency describes; listed twice alike, it is one edge, as before.
- Rust core: blocks, at build. `NodeSpec.expands` makes a node an expand (`NodeKind::Expand(payload)`, whose
  payload `Node::payload` returns as a function's), and `InputSpec.collect: Option<Collect>` makes a parameter
  collect a block, keeping the present branches (`Collect::Present`) or every branch (`Collect::All`). A block holds
  every node reading its expand along a path that crosses no collect of it; `Node.block` names a node's innermost
  expand and `Graph::block_path` all of them, outermost first. Blocks nest, any number of collects may read one
  block (Q123), and a node may collect one block and expand another (Q136). The build refuses a collect of a node in
  no block (`BuildError::CollectOutsideBlock`) or of an expand (`CollectOfExpand`), a node reading blocks of two
  unrelated expands (`UnrelatedBlocks`), and a node collecting a block it is inside, by also reading it directly
  (Q124) or because the block reads its own collect back through nodes outside it (`CollectsOwnBlock`, with the
  path). Collecting one input in two modes is a `ConflictingInput`. A collect passes on absence only from an absent
  expand, and an expand's items are never absent.
- Rust core: blocks, at prepare and run. `Graph::prepare` compiles each block the request reaches into a `SubPlan`
  (`Plan::blocks`), named for its expand by `Plan::block_of`, and refuses a requested output inside a block
  (`PlanError::OutputsInBlocks`, naming the nodes that collect it) or an override of a block node
  (`OverridesInBlocks`). An override of an expand supplies its items, and one of a collect cuts its block. A value
  read only by a block is computed before its expand, whatever its name. `Plan::run` runs each block once per item,
  in item order, branch after branch, with nested blocks on an explicit stack (Q122). An evaluator opts in through
  `Evaluate::blocks`, returning a `Blocks` that iterates expands (`Item::Next`, `Done`, `Declined`) and gathers
  collects; a plan with an expand run without one is a `RunError::BrokenPlan`. An empty expand gives empty collects,
  an expand requested alone returns its items, an absent expand gives its collects the three absence rules as a
  whole, and a failing branch finishes every open iterator, innermost first, and reports its branch
  (`RunError::Node.branch` and `.block`). `Evaluate::evaluate_in`, `Evaluate::lookup` (a value known without
  computing it, asked only downstream of an expand, in reverse need order within a branch), and
  `Observer::{pre_node_in, post_node_in, post_expand, hit_in}` carry the branch path; their defaults change nothing.
  `Plan::block_of(expand)` names an expand's sub-plan, `Plan::is_late(node)` says whether a step is downstream of an
  expand, and `Plan::has_expands` whether a plan has an expand step. An iterator that declines for an expand that
  can't be absent is a `RunError::BrokenPlan`. A plan without an expand runs the plain loop unchanged.
- Caching declared absences. A node returning `fnloom.ABSENT` is stored under its key in every store, and a hit
  replays it, so each node requiring it is skipped again without running; a skipped node is never stored.
  `DiskCache` writes `ABSENT` as an `absent` flag in the entry's header with no payload, in every format, so the
  `json` format no longer warns and recomputes, and no format's value can pass for it; every store gives back the
  singleton. `fnloom.fingerprint(ABSENT)` is a fixed digest, distinct from `None`'s. A parameter with a default
  reading an `ABSENT` override, or a `recompute` node's `ABSENT`, is keyed as an optional input not supplied, the
  call being the same. `explain_cache` reports a node a run would skip as `CacheOutcome("skipped", "reads absent
  'x'")`, a new `status`, instead of a `miss` forever. fnloom 0.4.0 and older, reading the same `DiskCache`
  directory, find an absence entry damaged: they remove it, with a `CacheWarning`.
- Fan-out and fan-in, with no flag (Q118 to Q124, Q127, Q136 to Q138). `fnloom.Parallelizable[T]` marks an expand:
  a node returning or yielding items of type `T`, read lazily, each running the expand's block, the nodes reading
  it, once, in item order. A parameter annotated `fnloom.Collect[T]` receives a block node's value from every item,
  as a `list` in item order; `Collect[T | Absent]` keeps the absent ones as `ABSENT`. Blocks nest, a block may be
  collected any number of times, and a node may collect one block and expand another. An empty expand gives `[]`,
  and an expand requested as an output gives the `list` of its items. Each item is checked by the input rules as a
  `T` (off with `without_input_type_checks()`), a misfit being a `TypeError` naming the expand and the index, and
  an item can't be `ABSENT`. Edges are checked as `T` into the block and as each block node's type into the
  collect. For a checker, `Parallelizable[T]` is `Iterable[T]` and `Collect[T]` is `list[T]`. `NodeView.block`
  names a node's innermost expand, `NodeView.kind` is `"input"`, `"function"`, `"expand"` or `"collect"`, and
  `Param.collect` is `"present"`, `"all"` or `None`. A generator function is a node when annotated
  `-> Parallelizable[...]`. The build refuses a collect outside a block or of an expand, a node reading two
  unrelated blocks or both a block node and its collect (Q124), `@extract_fields` or `@extract_columns` on an expand,
  and `Parallelizable[T | Absent]`. `build_async()` refuses a graph with an expand, until Plan 11 (Q132).
- Overrides and outputs around blocks (Q125, Q126). An override of an expand supplies its items, any iterable, read
  lazily, and one of a collect cuts its block. A block node requested as an output is a `ValueError` naming the
  nodes that collect it, or the `Collect[...]` annotation that would; an override of one is a `ValueError` saying
  to override the expand with the items instead. `validate` lists both, with the request's other problems. Lineage
  and `inputs()` read through blocks unchanged.
- Hooks and wrappers per branch (Q128, Q130; amends Q84, Q111). A block node fires `before_node` and `after_node`
  once per item, with `NodeStart.branch` and `NodeEnd.branch` giving its branch path (`()` outside blocks), and
  `RunStart.nodes` lists it once. `Hook.after_expand(ExpandEnd)`, a sixth method, hears each expand's item count
  once its items are exhausted. `fnloom.current_branch()` gives the branch inside a node, its checks and its
  wrappers, which run once per item. `PrintNodes` times each branch apart and prints it, as
  `node 'details' (branch (3,)) ran in 1.2 ms`. A failing branch raises the node's own exception, noted
  `while computing 'details' for branch (2,) of 'picked'`, after closing the expand's generator; nothing of the
  branches before it is returned. `_core.ObserverBase` gains `post_expand(node, *, branch, items)`, and its
  `pre_node` and `post_node` get `branch=` for a block node.
- Drawing and export of blocks (Q131; amends Q95). DOT and Mermaid draw each block as a cluster titled `for each
  item of <expand>`, nested as blocks nest, with the edges into and out of it labelled `expand` and `collect`; the
  builtin SVG engine writes the title on each block node instead, and labels the edges the same. `DrawnNode.block`
  (left out of equality, like `view`) names a node's block, and `Drawing.blocks` how the drawn blocks nest. A graph
  with a block exports `fnloom.export/2`: each node says `block`, its `kind` may be `"expand"` or `"collect"`, each
  parameter says `collect`, `graph_version` hashes both, and `diff` compares them, reading a `/1` document's nodes as
  in no block and collecting nothing.
- Caching blocks (Q129). `with_cache()` caches a graph with blocks. A block node's key is its ordinary key, with the
  item's fingerprint as the version of its expand and no branch index, so a moved or repeated item hits and an
  appended one computes one branch; a hit at a block's exit skips its upstream in that branch, and fires
  `before_node` and `after_node` with `cache` a hit and its `branch`. An expand is stored as the `list` of its
  items and a hit replays them without running it; a `recompute` or `disable` expand stays lazy and stores
  nothing. A collect's version hashes its branches' keys in branch order, so reordered items miss it. An item with
  no fingerprint is keyed by its index only when its expand's items are stored or replayed, else its branch is
  uncacheable. `explain_cache` reports a block node `unknown` and counts an outer expand's branches, from its
  stored items, in its reason. `_core.ObserverBase` gains `hit_in(node, *, branch, value)`.
- Blocks across namespaces, `bind=` and decorators. Blocks are named by their namespaced expand, so two copies of a
  module have independent blocks. A namespace's `bind=` to a node outside every block is shared by its branches. One
  to a block node feeds a `Collect` parameter, and read any other way is a `ValueError` at build saying to bind its
  collect instead. Every decorator applies to a block node once per item, `@parameterize` on an expand makes one
  block per row, and `replace=` may replace an expand.
- Absence in blocks. `Collect[T]` over a block node that may be absent gathers the present values in item order, and
  `Collect[T | Absent]` keeps a place per item. An expand annotated `-> Parallelizable[T] | Absent` declines as a
  whole, a generator too by returning `ABSENT` after yielding; its branches' results are dropped, and each collect
  of its block gets `ABSENT` (`Collect[T] | Absent`), its default, or is skipped. A declined branch is cached and
  replayed, so its block consumers skip again without running. A defaulted `Collect` parameter over an expand that
  may decline is no longer refused as a shadowed default.

### Changed

- A bounded `DiskCache` prunes when it opens, then when its own writes take it past `max_bytes` or add a tenth of it,
  down to nine tenths, rather than to the bound after every 100 writes: a full store prunes about once per tenth of
  its bound written. A store pickled to a worker process no longer prunes as it loads, but at its first write. With
  several processes writing, the directory can pass `max_bytes` by up to a tenth of it per other process until the
  next prune.
- A default on a `Collect` parameter whose expand can't decline is refused as a shadowed default, since it is never
  used; one reading a block node that may be absent was accepted before.
- A parameter with a default that reads a node that may be absent, and doesn't admit `Absent`, is no longer a
  shadowed default when every parameter of its node reading that node has a default: its default is used when the
  node declines. One that shares the source with a parameter without a default is still refused, since its node is
  then skipped instead (Q76, Q135).
- The native `_core.Graph` takes each node as `(name, [(source, kind, None)], tags, fn, declines)`, where `kind` is
  `"required"`, `"optional"`, `"admits"` or `"admits_optional"`; it was `(name, [(source, required)], tags,
  fn)`. `_scan.core_inputs` returns the same triples. A sixth element, the item iterator factory of an expand or
  `None`, follows `declines`, and the third of each input is its collect mode, `"present"`, `"all"` or `None`.
  `_core.Graph.blocks()` gives each block node's block path, `_core.Plan.blocks()` each block's steps, and
  `_core.Plan.steps()` lists a block's steps after its expand's.
- `CacheOutcome.status` is widened: its `Literal` gains `"skipped"`, so an exhaustive `match` on it needs an arm.
- The native `_core.KeyPlan` takes each node entry with a sixth element, the sources the node reads by an
  `optional` edge. Its marker for an optional input not supplied is now named `NOT_SUPPLIED` in the binding; its
  bytes, and so every cache key, are unchanged.
- Rust core: `NodeSpec` has a new field, `declines`; a spec built with struct syntax sets it, `false` for a node
  that never declines. `Dependency` has a third variant, `Admits`, and `BuildError` a new one, `ConflictingInput`,
  so a `match` on either needs an arm for it.
- Rust core: `NodeSpec` has a new field, `expands`, and `InputSpec` and `Edge` one, `collect`; `Node` has `block`.
  A spec built with struct syntax sets them, `false` and `None` for a graph without blocks. `NodeKind` has a third
  variant, `Expand`, and `BuildError` four more, so a `match` on either needs arms for them.
- Rust core: `RunError::Node` has two new fields, `branch` and `block`; a pattern naming every field needs them, or
  `..`. `PlanError` has two more variants, `OutputsInBlocks` and
  `OverridesInBlocks`.
- A run with node hooks is about a third faster: `before_node` and `after_node` cost about 1.7 µs per node rather
  than 2.6 (a 10,000-node chain). The events are the same `NodeStart` and `NodeEnd` objects.

### Fixed

- A frozen pydantic model holding a `set` or `frozenset` (in a field, a nested model, a container such as
  `dict[str, frozenset[str]]`, an extra field or a private attribute) is fingerprinted the same on every
  `PYTHONHASHSEED`: each set's members are sorted, as for a code version. Such a model used to key `DiskCache`
  differently in each process, so it missed (never a stale hit). Its cache key changes once, even under a fixed
  `PYTHONHASHSEED`; a model holding no set keeps its key.
- A nullable pandas column or index (`Int64`, `Float64` and other extension dtypes) and any polars series holding a
  null fingerprint their non-missing values in their own dtype, beside the missing mask; a nested polars series (list,
  array or struct) fingerprints its items as exact Python values. They went through a float NaN, so `[2**53, NA]` and
  `[2**53 + 1, NA]` keyed the cache the same and could serve each other's result, and a pandas `Float64` NaN keyed the
  same as NA. Their cache keys change once; numpy-backed columns keep theirs.
- `@check_output(contains=...)` and `does_not_contain=` build on a node returning a `str` subclass, a `NewType` over
  `str`, a `Literal` of strings, an `Annotated[str, ...]`, or a union with one of them such as `Name | None`. The
  first three, and such unions, were refused as not `str`, and `Annotated` raised an unhelpful `TypeError` about set
  elements.
- A failed node leads the `ExceptionGroup` with `node '<name>' failed and hooks raised` when only run hooks
  (`before_run`, `after_run`) watch and one raises, as `build_async()` and a driver with node hooks say. The sync
  driver said `the run failed and hooks raised`.
- `@check_output(range=...)`, `allow_nans=` and `max_fraction_nans=` refuse, at build, a node whose values can only be
  text or `None`: a union such as `str | None` or `Name | None`, a `NewType` over `str`, or a `Literal` of strings. Such
  a node built, then failed every run (Q101). A union with a numeric arm, such as `int | str`, still builds.
- `max_fraction_nans=` given text, such as `"0.5"`, or anything `float()` can't read, is refused with the `ValueError`
  naming the argument that an out-of-range number gets, rather than a bare comparison `TypeError`. A `Decimal` or
  `Fraction` is still read as its float.
- An annotation that raises when evaluated, other than for an unknown name, such as `-> "int | 3"`, fails the build
  with a note naming its function. The error named nothing.
- A `Validator` whose attributes hold a `set`, and an `extract_fields`/`extract_columns` `fill_with=` holding one,
  key the cache the same on every `PYTHONHASHSEED`, as a `value()` binding does. They keyed `DiskCache` differently in
  each process, so the node missed every time (never a stale hit). Their keys change, so an existing disk cache
  misses once for them; any other check or fill keeps its key.
- A check function whose code version can't be read, such as one whose default's registered fingerprint raises, makes
  its node uncacheable with a `CacheWarning` saying why, as a node's own function does. The cached build raised
  instead, though the same build without a cache ran. `export()` of such a node warns the same way and leaves its
  `version` out, saying why in `unversioned`; it raised.
- A node's code version counts the classes of everything its defaults' fingerprints read: state set on a dataclass
  outside its fields, and a `__fnloom_fingerprint__` or registered form. A class reached only that way was left out,
  so editing its methods kept the version and the cache could return a stale result. Such a node's version changes,
  so an existing disk cache misses once for it.
- The fingerprint of a `value()` binding holding a set counts a dataclass's state set beyond its fields, as the plain
  fingerprint does: two such values differing only there keyed the cache alike, so one could be served the other's
  result. Their keys change, so an existing disk cache misses once for them. A `defaultdict`, whose `default_factory`
  is state, is refused whether or not it holds a set, and a dataclass with an unset `init=False` field holding a set is
  refused as `UnfingerprintableError` rather than a raw `AttributeError`.
- `fnloom diff` of an export whose node has a `kind` that is not a `str`, or `params` that are not a list of entries,
  exits 2, a bad file (Q98), naming the side. It raised `AttributeError` and exited 1, which says the graphs changed.
- `to_dot()`, `to_mermaid()`, `render(...)` to `.dot` or `.mmd`, and `fnloom draw` replace a lone surrogate in a label
  with U+FFFD, as `to_svg()` does. They raised `UnicodeEncodeError`. The preview's Graphviz input gets U+FFFD too,
  rather than `?`.

## [0.4.0] - 2026-11-05

### Added

- `fnloom.Style(fill=, stroke=, font_color=, stroke_width=, dashed=, shape=, dot=, mermaid=)` and
  `Drawing.styled(graph=, node=, edge=, style=)`, returning a new drawing that DOT, Mermaid and SVG all draw alike.
  `style(view, drawn)` is called once per node with its `NodeView`. A colour is `#rrggbb` or a CSS name from
  `Style.COLOURS`; anything else is a `ValueError` naming the attribute, and the node when a style function made it.
  Passthrough keys are identifiers and values `str`: quoted and escaped in DOT, and in Mermaid only ASCII letters,
  digits, space and `# . _ ! : + * / ' % -` (not `%%`). `stroke_width` is from 0.01 to 1000. Attributes are written
  in a fixed order. Every `draw*` method takes `show_types`, `show_tags`, `show_schema` (a `TypedDict`, dataclass or
  `NamedTuple` return type's fields) and `hide_inputs` (keeping an input the call names: an output, a node drawn
  from, a path's end), and `fnloom draw` takes `--no-types`, `--show-tag`, `--show-schema` and `--hide-inputs`.
  `DrawnNode` gains `tags`, `schema`, `style` and `view` (the `NodeView`, left out of equality, `repr` and pickling),
  and `Drawing` gains `graph_style`, `edge_style` and `show_types` (Q139).
- `pipe_input(..., namespace=)`, `pipe_output(..., namespace=, on_output=)` and `step(...).named(name, namespace=)`,
  with Hamilton's names: `...` (the default) keeps `<node>.<label>`, `None` names a step `<label>`, and a dotted path
  `<path>.<label>`, within a module's namespace. A step's own namespace wins over the decorator's, and a step's own
  `.on_output` over `on_output=`. A namespace that is no dotted identifier path is refused when decorating, and a
  name two nodes would have fails the build naming what made each. `apply_to(...).named(name, namespace=)` does the
  same for `@mutate` (Q140).
- `@tag` takes a `list` or `tuple` of strings as a value, kept as a tuple in written order. An empty sequence, a
  repeated item, a non-`str` item, or an unordered collection (`set`, `frozenset`, `dict`) is refused when decorating,
  naming the tag. `Driver.nodes.tagged(**query)` returns the views whose tags match: a `str` matches an equal value or
  a tuple holding it, a sequence matches any item, `fnloom.ANY` matches a node with the tag. `fnloom draw` and `fnloom
  export` take a repeatable `--tag key=value`, one per key (a key given twice is a usage error). A `str` subclass
  value or item is kept as a plain `str`, and `fnloom.ANY` survives `copy`, `deepcopy` and `pickle`. The export writes
  a tuple as a JSON list, and `diff` compares it in order (Q36, Q141).
- `fnloom.PrintNodes(verbosity=1, *, print_fn=print, nodes=None, width=50)`, a hook that prints a line per node: its
  time, `loaded from cache` for a hit, the error for a failure, and at `verbosity=2` the sorted inputs and the result,
  cut to `width`. `nodes` is names (an unknown one fails the build) or a function of the `NodeView` (Q142).
- `Builder.with_cache(store, *, recompute=(), disable=(), default=(), default_behavior="default", ignore=None)`: each
  list names nodes given that behavior, or is `True` for the default. A list naming a node wins over its
  `@cache(behavior=...)`, which wins over `default_behavior`; `True` counts as `default_behavior`, not as a name, so
  a node's own `@cache` still wins over it. A name in two lists, two `True` lists, or `True` against another
  `default_behavior` is a `ValueError`; a name that is no node, or an `ignore` parameter the consumer lacks, fails
  the build with a `did you mean`. `ignore` maps a consumer to parameters left out of its key. Carried by pickling,
  `copy`, `rebuild()` and `build_async` (Q143).
- `fnloom.frame(library="pandas", *, flatten=True, separator=".", join="strict")`, a result builder for
  `with_result_builder` making one pandas or polars frame: a frame output flattened into `<output>.<column>` columns in
  request then column order, a series one column, scalars broadcast, all scalars one row. `join="strict"` refuses
  outputs on different pandas indexes or of different polars lengths with a `ValueError` naming them; `join="outer"`
  is Hamilton's `PandasDataFrameResult` frame, without its log; polars refuses it. Two columns of one name are a
  `ValueError` naming both. The library is imported by `frame`, never by `import fnloom`. `frame` returns a
  `fnloom.FrameBuilder` (Q144).
  `with_result_builder(pandas.DataFrame)` is unchanged (Q89).
- `Hook.after_build(event: BuildEnd)`, with `fnloom.BuildEnd(nodes, config, modules)`: called once per hook from
  `build()`, `build_async()` and `rebuild()`. Every hook sees the event, a raise refuses the build, and several are
  one `ExceptionGroup`. This is Hamilton's `GraphConstructionHook` and `StaticValidator` (Q145, Q117).
- `Drawing.to_svg(*, orient="LR", legend=True)`, an SVG laid out in pure Python, and `render(path, engine="dot" |
  "builtin")`: `"dot"` stays the default, and `"builtin"` writes `to_svg()` with nothing installed. `fnloom draw`
  takes `--engine builtin` (Q146).
- `@cache(fields={...})` and `fnloom.FieldCache(behavior="default", format=None)`: a field or column of
  `@extract_fields` or `@extract_columns` with a policy of its own. `"default"` stores and serves the field apart from
  its producer, keyed by the producer's key and the field, so a `disable` producer can have a stored field and does
  not run when it hits; `"recompute"` runs the producer again for the field; `"disable"` makes the field's readers
  uncacheable. A field the function doesn't extract is a `ValueError` at build, and `fields=` on a function that
  extracts nothing a `TypeError` (Q147).
- `@config.matches(predicate, *, name=None)`: a variant kept when `predicate`, called with the config keys its
  parameters name, returns `True`. A defaulted parameter is an optional key, a missing required key never matches,
  and a result that is no `bool` is a `TypeError` at build. It stacks with `config.when*` and falls back to
  `@config.default`. The predicate's code version is in the selected variant's cache key and export `version`, and
  the export shows the condition as `matches(<module.qualname>)`. A public module-level predicate, which would be a
  node, fails the build (Q148).
- `Builder.without_edge_type_checks(*consumers)` turns edge type checks off for every edge, or only for the edges into
  the nodes named; an unknown name is a `ValueError` at build. Input checks stay on. The export's new `edge_checks`
  field records it, and it is in `graph_version` (Q149).
- `with_modules(..., namespace="eu", config={...})` gives a namespace a config of its own, overlaid on the builder's
  for its `@config` variants, `@resolve`s, `config.matches` predicates and steps, as `@subdag(config=)` does.
  `Driver.config` stays the builder's own; the export's new `namespace_config` field lists each overlay, which is in
  `graph_version` (Q112 amended: `config=` per copy was not planned).
- `with_modules(module, replace=("total",))` replaces the node `total` that earlier modules define, for every reader.
  A name no earlier module, or not this one, defines under the config is a `ValueError` at build. `NodeView.replaced`
  and the export entry's `replaced` name the module replaced. A `@mutate` on the replaced function fails the build,
  and `@config` variants on both sides are each selected first (Q74 amended, Q150).

### Changed

- **Breaking:** `NodeView.tags` is now `Mapping[str, str | tuple[str, ...]]`: code that reads a tag as a `str` needs
  an `isinstance(value, str)` check (or `tags.get("k")` handled as either) to type-check. `Driver.nodes` is typed
  `NodeViews`, which has `tagged` (Q36, Q141). A node's `display_name` tag given as a tuple is drawn joined by `, `.
- On a `@pipe_output` function without extracted fields, `.on_output(...)` (and `@mutate`'s
  `apply_to(...).on_output(...)`) picks the `@parameterize` rows it names, where it applied to every row; naming no
  node of the function is a `ValueError` at build, where it was ignored (Q106, Q140). The error for two nodes of one
  name now names what made each.
- A step named with `.named(...)` counts toward the numbers of its function's unnamed steps, as in Hamilton:
  `step(_inc).named("first"), step(_inc)` makes `<node>.with_inc_1` for the second step, where it was
  `<node>.with_inc` (Q104, Q140).
- `explain_cache` (and a hook's cache outcome) names where a policy came from: `"policy recompute"` is now
  `"policy recompute (decorator)"`, `"policy disable"` is `"policy disable (decorator)"`, and `(driver)` or
  `(default)` mark a behavior from a `with_cache` list or `default_behavior`. A `default` node that a list or
  `@cache(behavior="default")` made so has the reason `"policy default (driver)"` or `"policy default (decorator)"`
  where it had `None` (Q143).
- `@cache(...)` without `behavior` no longer sets one: `CachePolicy.behavior` is `None`, and the driver's
  `default_behavior` applies, as in Hamilton. With `with_cache`'s default, nothing changes (Q143).
- Without Graphviz, a drawing's `_repr_svg_` (Jupyter's preview) is the builtin SVG, where it was `None`. The
  `FileNotFoundError` for a missing `dot` gives the platform's install command (`brew install graphviz`, `apt-get
  install graphviz`, `winget install graphviz`), and names `.mmd`, `.dot` and, for an SVG, `engine="builtin"` as the
  ways that need nothing (Q146).

### Fixed

- A code version no longer depends on `PYTHONHASHSEED` when a function reads a `set` or `frozenset`, as a default
  or a global: its members are versioned sorted. Such a node's version, and its cache key, change once.
- A `value()` binding, default or global holding a set inside a subclass of `set`, `frozenset`, `list`, `tuple` or
  `dict` with attributes or slots of its own, or inside another mapping, is no longer versioned by its members alone,
  so a change to that state can't hit a stale result: it is fingerprinted whole, or refused.

## [0.3.1] - 2026-11-05

### Fixed

- `register_fingerprint` now applies to pydantic models. A model class could not be code-versioned (pydantic's
  generated class attributes, such as `__class_vars__` and `model_config`, were refused as mutable), so its values
  were refused even when registered, and a node reading one was `uncacheable`. A model class is now versioned by its
  body and by what pydantic takes out of it: fields' resolved types (with the classes, aliases and `Annotated`
  validators they name), defaults and `Field(...)` settings, private attributes, validators' settings and
  `model_config`.
- A frozen pydantic model (`ConfigDict(frozen=True)`) is fingerprinted without a registration, by its class, its
  fields in order, the names of the fields set, its extra fields and its private attributes. A model that is not
  frozen still needs one. fnloom never imports pydantic.
- A node reading a `typing.NewType` global is cacheable: the `NewType` is versioned by its name and its supertype
  (a class, a generic alias or another `NewType`), where it used to make the node `uncacheable`.

## [0.3.0] - 2026-11-03

### Added

- Every row of Coming from Hamilton's comparison tables cites a register row, and each cited row has a differential
  test against Hamilton 1.90.0 (`deliberate`, or the new `keeps` marker for kept behaviour). Two docs tests enforce
  both. New register rows record Hamilton's side of features fnloom doesn't have yet or keeps as they are (Q139,
  Q140, Q142, Q143, Q145, Q147, Q148, Q151 to Q159). The `@pipe` row now says what Hamilton does: `pipe` is a
  `pipe_input` that silently drops `collapse`, not a deprecated alias.
- `Builder.with_modules(*modules, namespace=, bind={...})` feeds a namespace inputs of its own, as `@subdag`'s
  `inputs=` does. Each key is a name the namespace reads that none of its modules defines, bound to `source("x")`, read
  outside the namespace, or `value(v)`. A key no node reads, or one the namespace defines, is a `ValueError` at build.
  Bindings are in `Driver.nodes`, the export, `graph_version` and the cache key (a `value()` holding a set is keyed the
  same on every hash seed), and survive pickling, `copy` and `rebuild()` (Q112).
  A `value()` holding a set now has a different cache key and export version than before, so such cache entries miss
  once and `diff` against an older export shows them changed.
- Decisions, each with a register row and a runnable recipe in Coming from Hamilton: `@does`, `@schema.output`,
  `@dataloader`, `@datasaver`, `@load_from`, `@save_to`, materializers and `StaticValidator` are not planned (Q113 to
  Q117); `@parameterized_subdag` and `config=` per `@subdag` copy are not planned either (Q112).
- Output checks. `@fnloom.check_output(range=, values_in=, data_type=, allow_none=, contains=, does_not_contain=,
  allow_nans=, max_fraction_nans=, importance="fail", field=None)` and `@fnloom.check_output_custom(*checks)` check a
  node's value inside the node, adding no node. A custom check is a function, which may read the node's `inputs`, or a
  `fnloom.Validator`. A failed `fail` check raises `fnloom.ValidationError`, a `ValueError`, with one line per check;
  `warn` logs a WARNING on the `fnloom` logger. Every result is on `NodeEnd.checks` as a `fnloom.CheckResult`.
  Checks read any type, numpy and pandas through their own methods, never consume a one-shot iterator, stack in any
  order, and are part of the cache key (Q99 to Q103).
- Steps. `fnloom.step(fn, **bindings)` with `.named`, `.when*` and `.on_output`; `@pipe_input` and `@pipe_output` lower
  each step to an ordinary node named `<node>.with_<step>`, and `@pipe_output` runs the function as `<node>.raw`.
  `@mutate(*targets)` and `apply_to(fn, ...)` add a step to another function's `@pipe_output`, across modules too. A
  defaulted step parameter is never a graph input, names never change with the config, and misuse Hamilton ignores is
  refused (Q104 to Q108).
- `@fnloom.inject(**bindings)`, one `@parameterize` row that keeps the function's name, and
  `@fnloom.resolve(decorate_with)`, which applies the decorators `decorate_with` returns for the config, needing no
  power-user mode (Q109, Q110).
- `Builder.with_node_wrappers(*wrappers)`: an ordered chain of `wrapper(node, call, inputs)` around every computed
  node that runs a function of yours, any number per driver; hooks fire outside it, a wrapped node's checks run inside
  it, and a cache hit skips it (Q111).
- Hooks. Subclass `fnloom.Hook`, override any of `before_run`, `before_node`, `after_node` and `after_run`, and give
  instances to `Builder.with_hooks(*hooks)`, which is cumulative. Each method gets one frozen event, `fnloom.RunStart`,
  `NodeStart`, `NodeEnd` or `RunEnd`, with a `run_id` shared by a run's events, the node's `NodeView`, its inputs by
  parameter name as a read-only mapping, and its result or the very exception it raised. Every computed node a run
  evaluates reports, extracted fields included; inputs and overrides don't. Hooks are called in the order given, and
  only the methods a hook overrides. `with_hooks` refuses an object that is no `Hook`, a method that can't take one
  positional event or is an `async def` (`TypeError`), and a hook given twice (`ValueError`). `Driver.hooks` lists
  them, and pickling, `copy`, `rebuild()` and `build_async` carry them (Q84). A `Driver` made from a native graph
  takes `hooks=` only with `nodes=`, its node views, and refuses it otherwise (`TypeError`).
- A hook that raises: every hook sees the event first, `after_run(ok=False)` still fires, and one error is raised as
  itself, several as one `ExceptionGroup` with a failing node's own exception first. Hamilton skips the later hooks
  and keeps a node's error only in `__context__` (Q23).
- `AsyncDriver` calls the same hooks, on the event loop's thread, as each node starts and finishes, with real values;
  concurrent nodes report in the order they start and finish. A raising hook fails the run as a failing node does
  (Q86).
- On a cached driver, every node event carries the node's `fnloom.CacheOutcome` as `cache`. A hit gets
  `before_node` and `after_node` with `inputs=None` and the loaded value, hits first, in plan order; a node only hits
  need reports nothing (Q85).
- `fnloom.LogFailures(logger=None, *, level=logging.ERROR)`, a ready-made hook that logs each node that raised, with
  its inputs, each `repr` cut to 50 characters, and the exception as `exc_info`, on the `fnloom` logger by default.
  A huge input's `repr` is bounded before the cut, and one whose `repr` raises shows as `<unrepresentable TypeName>`,
  so the node's own exception is still what `execute` raises. fnloom still logs nothing unless you add it (Q88).
- `Builder.with_result_builder(build)` makes `execute`, `PreparedPlan.run` and the async equivalents return
  `build(outputs)`, where `outputs` is the raw dict in request order. `Builder`, `Driver`, `AsyncDriver`,
  `PreparedPlan` and `AsyncPreparedPlan` take a type parameter, defaulting to `dict[str, object]`, so a type checker
  sees the built type. A second `with_result_builder` is a `ValueError` naming both (Q19, Q24). There is no pandas or
  polars builder: pass `pandas.DataFrame` or `polars.DataFrame`, or the strict `frame` recipe in the docs, which
  refuses outputs that would outer-join (Q89).
- `Driver.raw_execute(final_vars, *, inputs=None, overrides=None)`, `PreparedPlan.run_raw` and their async
  equivalents return the raw dict, skipping the result builder and firing every hook (Q19).
- `Builder.with_output_type_checks()` checks each node's result against its return annotation by the input checks'
  rules. A mismatch fails the node with `TypeError: x: returned str, expected int`; a node returning `Any` or `object`
  costs nothing. Off by default, as in Hamilton (Q87).
- `Builder.with_value_check(check)`, cumulative, adds to the default value check: `check(declared, value)` is asked
  only about a value the default refuses, and admits it by returning a true value. It applies to inputs, overrides,
  extracted fields, output checks and `validate`, never to edges. `fnloom.ValueCheck` is its type. Hamilton's
  `do_validate_input` replaces the default instead (Q21).
- `Driver.rebuild()` and `AsyncDriver.rebuild()` build a new driver from the same builder, with its modules, functions,
  namespaces, config, cache store and every setting, and `sync_nodes` for the async driver. After `importlib.reload`,
  the new driver runs the edited code and, with a shared store, misses on the edited node; the old driver keeps its
  code, as Hamilton's does. A driver made from a native graph raises `TypeError` (Q83).
- One `Driver` shared between threads is a documented guarantee: `execute`, `prepare`, `validate`, `explain_cache`,
  inspection and `PreparedPlan.run` are safe to call at once, on the default and free-threaded builds, and a stress
  test of 16 threads over one cached driver pins it. A user's own mutable values, and nodes that mutate their inputs,
  are not covered.
- A function in `final_vars` means the node it makes in this driver, matched by identity with `NodeView.origin`, never
  by name, in `execute`, `prepare`, `validate` and `explain_cache`, on both drivers; the result is keyed by name. An
  `@extract_*` function means its producer. A function that makes several nodes, such as a `@parameterize` function,
  is a `TypeError` listing them, unless one is named like the function; a function no node comes from is a
  `ValueError` naming it and why. Hamilton maps every function to its `__name__` (Q30).
- `@parameterize` sugar. A row may be `(bindings, doc)`, the doc used as written; otherwise the function's docstring is
  a template filled per row (`{output_name}`, each bound parameter), and a placeholder the row lacks is a `ValueError`
  when decorating, where Hamilton prints the bare key. `fnloom.group(*deps)` and `group(**deps)` bind a parameter to a
  list or dict built from `source()` and `value()` items. `parameterize_sources(**rows)` and
  `parameterize_values(parameter, assigned_output)` are the shorthands. Misuse of any is refused when decorating, each
  offender listed once (Q82).
- `Builder.with_functions(*items)` builds from functions, plain or decorated, without a module: nothing is copied and
  nothing is registered in `sys.modules`. `fnloom.node(fn, *, name=None, bind=None)` makes one node from a function,
  named and bound with `source()` and `value()` without decorating it, and returns a `fnloom.NodeSpec`. A lambda needs
  `name=`. Builders and drivers of functions pickle them by module and qualified name (Q72).
- `Builder.with_modules(*modules, namespace="ns")` names each node of those modules `ns.<name>`, so one module can be
  used under several namespaces. Inside a namespace, a parameter reads the namespace's own node when one is defined,
  and otherwise the plain name; inputs are never namespaced. `NodeView.namespace` says which namespace a node is in.
  Names still clash as before: there is no `allow_module_overrides` (Q74).
- `Driver.inputs()` and `outputs()`, `Driver.modules` and `Driver.what_is_the_path_between(a, b)`, on `AsyncDriver` too,
  with `fnloom.InputView`. The path includes both ends and is in topological order, ties broken by name, on every hash
  seed; its error names every unknown node (Q70). An unsupplied input requested as an output stays a `ValueError`
  (Q78).
- `Driver.validate(final_vars, *, inputs=None, overrides=None)`, on `AsyncDriver` too (not a coroutine), returns every
  problem `execute` would raise for a request, one sorted line each, and runs nothing: unknown outputs, inputs and
  overrides, missing inputs (naming only the consumers the request needs), input and override types, and outputs no
  config variant gave, and functions in `final_vars` no node comes from. Every unknown name in an error now ends in
  `(did you mean 'x'?)` when a node is close to it; in `validate`, a missing input also names the closest supplied
  name that is no node of the graph (Q71).
- `Builder.with_strict_overrides()` refuses an override of an input that isn't also supplied, as Hamilton does, and
  `Builder.with_declared_inputs(*names)` fails the build for any input of the graph not declared and any declared name
  that is no input, each naming the closest node (Q75). Both are off by default. `Builder.without_override_type_checks()`
  skips only the override type checks, keeping input checks on, for overriding a node with a value of another type
  such as `nan` (Q22). All three survive pickling, copying and `build_async`.
- `Builder.with_deep_type_checks()` checks every item of a `list`, `set`, `tuple[T, ...]` or other standard
  collection, every key and value of a mapping, and nested collections' items, iteratively; an iterator, or a value of
  any class but a standard collection's, is never read in full. `Builder.with_numeric_widening()` lets an `int` (not a
  `bool`) or a numpy integer or floating scalar fit `float`, and a numpy integer scalar fit `int`, without importing
  numpy. Both apply to inputs, overrides, extracted fields and
  `validate`, not to edges, and survive pickling, copying and `build_async`. `fnloom.value_matches` takes `deep=` and
  `widen=` to answer the same way.
- `fnloom.value_matches(declared, value)` and `fnloom.type_matches(produced, expected)`, the checks `execute` runs on
  each input, override and extracted field and `build` runs on each edge, are public. Their docstrings state the
  rules, as Hamilton's `htypes.check_input_type` is public too.
- An input or override naming a parameter that only `value()` bindings supply is refused as an unknown name, as any
  input nothing reads is (Q4, Q5); Hamilton drops it, even a wrong-typed one. When another node reads the name, the
  caller's value feeds that node alone and the `value()` rows keep their constant (Q77). Documented and tested; no
  behaviour changed.
- `@fnloom.exclude`, bare or called, keeps a public function out of the graph and returns it unchanged. Hamilton's
  `@hamilton_exclude` works bare only. An excluded function's annotations are never read, so it needs none (Q73).
- CodSpeed benchmarks of the Python run path in `benchmarks/` (`uv run pytest benchmarks`): prepared runs on every
  generated shape, repeated `Driver.execute`, cached runs, the async driver, extraction, config and `Builder.build`.
  CI runs them once on every pull request, and measures them under CodSpeed when `CODSPEED_ENABLED` is set.
- Drawing. `draw()`, `draw_upstream_of(*names)`, `draw_downstream_of(*names)`, `draw_path_between(upstream,
  downstream, *, context=True)` and `draw_execution(final_vars, *, inputs=None, overrides=None, validate=True)` on both
  drivers return a `fnloom.Drawing`: its `nodes`, each a `fnloom.DrawnNode` with its title, whole type, `@config.when`
  conditions and marks (`output`, `override`, `path`, `context`), in topological order, and its `edges`. Each input is
  one node; config is never drawn. `to_dot()` and `to_mermaid()` write the text with nothing installed, the same under
  every `PYTHONHASHSEED`. `render(path)` writes `.dot`, `.gv` and `.mmd` as text and any other format through
  Graphviz's `dot` executable, with a `FileNotFoundError` saying how to install it when it is missing. In Jupyter a
  drawing shows as an SVG, or as its DOT text. Hamilton's drawings need the graphviz package and, without it, return
  `None` and log a traceback (Q90 to Q94).
- `Driver.export(final_vars=None, *, inputs=None, overrides=None, validate=True)` and `AsyncDriver.export` describe the
  graph, or the run `execute` would make, as a JSON-ready `fnloom.export/1` document: each node's type, bindings,
  tags, conditions, source and code version, inputs as inputs, and a `graph_version` that changes with any name,
  wiring, type or code. No value is exported (Q95, Q96).
- The `fnloom` command, also `python -m fnloom`: `draw`, `export`, `validate`, and `diff OLD.json NEW.json` of two
  exports, with `--config`, `--input`, `--override`, `--outputs` and a JSON or TOML `--context`. Modules are dotted
  names or `.py` paths, which may import their siblings; `MODULE@NS` namespaces one. stdout carries only the result,
  an error goes to stderr with status 1, and `diff` exits 0, 1 or 2. It needs no dependency (Q97, Q98).

### Changed

- **Breaking:** a module that defines `__all__` now makes nodes of only the functions it names. Hamilton ignores
  `__all__`, and so did fnloom, so a function the module left out stops being a node, and anything reading its name
  now sees an input. A function listed by its attribute name, `price__eu` for a `@config` variant, is the one
  considered. To keep a function as a node, add its name to `__all__`, or remove `__all__`. An `__all__` that is not a
  list or tuple of `str` is a `TypeError` at build. A missing input named like a function `__all__` or `@exclude` left
  out now says so, and so does the error for a module with no nodes (Q73).
- **Breaking:** the build refuses a parameter with a default that reads a computed node, and an input that two
  consumers give different defaults. Hamilton lets the node replace the default, and each consumer keep its own. One
  `ValueError` lists every case, sorted, naming the parameter, the node or the other consumers, and the opt-out. To
  keep the behaviour, write `@fnloom.defaults(shadowed=("cost",))` on the consumer, and
  `@fnloom.defaults(differing=("shared",))` on each consumer of the input but one; or rename the node, or make the
  defaults equal (Q76). `@fnloom.defaults` is new, and records names only.
- **Breaking:** `None` passes an input, override or extracted-field check only when the type admits it: `Optional[T]`,
  `T | None`, `object`, `Any`, `None`, `Literal[None]`, or a `TypeVar` that is free or whose bound or a constraint
  admits it. Hamilton passes `None` for every type, and so did fnloom, so `None` for `x: int` is now a `ValueError`
  before anything runs: `x: expected int, got NoneType`. Annotate the parameter `x: int | None` (a default of `None`
  does not make it optional), or stop passing `None` (Q79).
- **Breaking:** a fixed-length tuple type, `tuple[int, str]` or `tuple[()]`, now checks a value's length and each item
  with the same rules, nested tuples included. Before, any tuple fitted; Hamilton refuses every one. `tuple[int, ...]`
  and bare `tuple` are unchanged (Q80).
- **Breaking:** a `@parameterize` function's docstring is now a template, filled per row with `str.format_map`. In 0.2.0
  each row used it as written, so a docstring with braces, such as `"""Returns {"count": n}."""`, now raises a
  `ValueError` naming the row when the module is imported. Escape literal braces as `{{` and `}}`, or give the row its
  doc as `(bindings, doc)`, which is used as written. Hamilton fails on the same docstring when decorating (Q82).
- An annotation naming something that does not exist at run time, such as a name imported only under
  `TYPE_CHECKING`, no longer fails the build with `NameError`. It becomes an opaque type, `unresolved 'Decimal'`, with
  its text as written; an alias over such a name and a `TypedDict` field type do too. An edge matches it only from or
  to the same text or `Any`, and fails the build, naming the text, otherwise. A value checked against it raises a
  `TypeError` saying to import it at run time. Code that caught the `NameError` at build should instead import the
  names its annotations use, or catch the `TypeError` where a value is checked (Q81).

## [0.2.0] - 2026-10-28

### Added

- `@cache(format=...)` picks how `DiskCache` writes a node's result: a `fnloom.Format` (a name with a `dumps` and
  `loads` pair) or the name of one the store knows. `"pickle"` and `"json"` are built in, and
  `DiskCache(formats=...)` adds more. A name the store does not know is refused when the driver is built (Q55). The
  `json` format refuses any value that would not load back as the same types, such as a tuple, so a hit never
  returns a different value (Q54).
- `len(MemoryCache)`, the number of entries it holds.
- `Builder.with_config(...)` and `Driver.config`, a read-only, shallow copy. `@config.when`, `when_not`, `when_in` and
  `when_not_in` pick a function's variant at build, and `@config.default` marks the variant used when no other matches.
  `name=` names the node on all four forms. Config shapes the graph only: a key is never a node, an input or a value
  (Q56). A key the config doesn't have never matches, and a node something needs that no variant gives fails the build,
  naming what each variant needs; so does a row or extracted field only an unselected variant gives (Q57). Two matching
  variants fail the build, naming both, and so does a config-decorated function defined twice in its module, naming the
  lines of each (Q58). Misuse, such as `@config.when` without parentheses or `when_in(mode="ab")`, is refused when
  decorating (Q59). The user's function is never changed, and `NodeView.config` holds the conditions that picked a node,
  each a `fnloom.Condition` (Q60).
- `@extract_fields` splits a node's mapping into one node per field. Each field's value is checked against its
  declared type on every run, even with input checks off, and declared types are checked at build against a
  `TypedDict` or `dict[str, T]` return (Q61). A `Mapping[str, T]` return works. `fill_with` never changes the
  producer's value, and `None` is a fill (Q62). With a cache, a field or column is never stored or served from the
  store: it reruns from its cached producer whenever it's needed, its check with it, and its key covers its name,
  declared type and fill.
- `@extract_columns` splits any value with `__getitem__`, such as a pandas or polars frame or a mapping, into one node
  per column, with no registry or extension to load (Q64). A missing column raises `ValueError` when run, naming the
  columns the producer did return. `@tag` on an `@extract_*` function tags the producer and every field or column
  (Q63).
- `Builder.build_async()` and `AsyncDriver`, for graphs with `async def` nodes. The build is synchronous (Q12), and
  `execute` and `AsyncPreparedPlan.run` are coroutines that return the sync driver's dict, in request order (Q11). Sync
  nodes run in a worker thread, or on the loop with `build_async(sync_nodes="inline")` (Q65). A failing node cancels its
  running siblings, each once, waits for them and raises its own exception, noted with its name (Q66). Requests are
  checked as the sync driver checks them (Q67), and an `async def` `@parameterize` row is awaited (Q69).
- Caching on the async driver: `with_cache(...).build_async()` uses the sync driver's keys, stores, policies and
  `explain_cache`. A node is stored as it succeeds; one that fails, or that a failure cancels, stores nothing (Q68).

### Changed

- A `TypedDict`-typed input or override accepts any `dict`, its keys unchecked, as the shallow container checks do.
  Before, every value failed the check, so such an input could never be supplied (Q61).
- `build()` still refuses a graph with an `async def` node, and its `ValueError` now names `build_async()` (Q32).
- A cold cached run is 2.7 to 2.9 times faster with `MemoryCache`, and a warm one 2.1 to 3.3 times faster: a plain
  `MemoryCache` keeps its entries in the native extension, which walks it and stores each result with no call into
  Python per node (issue #10). A subclass of `MemoryCache` is used through its methods, like any other store.
  The caching design's cold-run target is restated as at most 0.25 µs of overhead per node, which it meets.
- Cache keys are BLAKE3 keyed by each node's static part (schema `fnloom-cache-2`), so a node reading one value
  hashes one block. Entries written by 0.1.0 are misses, and are replaced as nodes run.

## [0.1.1] - 2026-10-28

### Added

- A `Driver` pickles, and copies with `copy.deepcopy`, so it can go to a process pool. It is pickled as the
  `Builder` that built it: its modules by importable name, whether it checks input types, and its store. Loading it
  builds it again. A `PreparedPlan` pickles as its driver and its request, and loading it prepares the request again.
  A `Builder` pickles too. A `DiskCache` reopens its directory when loaded, and a `MemoryCache` arrives empty with the
  same bounds. Pickling raises `TypeError` for a module that isn't importable by its name, and for a driver made from
  a native graph. See [Pickling and process pools](https://kelsayed17.github.io/fnloom/running/#pickling-and-process-pools).

### Changed

- `build`, `Builder.with_modules` and `Builder` raise `TypeError: expected a module, got function 'f'` for anything
  that isn't a module, instead of building nothing from it (Q52).
- A module that contributes no node fails the build with a `ValueError`. It names the module and says why each of its
  functions was skipped: defined in another module (a re-export) or private. Before, such a module was silently empty
  and its nodes showed up as unknown outputs at `execute` (Q53).
- The missing-input error names, for each input, the nodes that need it, and says when you asked for it as an output:
  `missing inputs: 'quantity' (needed by 'total')`. In the Rust core, `PlanError::MissingInputs` holds a
  `MissingInput` per input, with its consumers.

### Fixed

- `inspect.signature` works on every public callable. Before, 12 of them (`build`, `Builder`, `Driver`, `PreparedPlan`,
  `NodeView`, `DiskCache` and methods such as `Driver.execute`) raised `NameError`, because their annotations named
  types imported only for type checkers.

## [0.1.0] - 2026-10-27

### Added

- The Rust core, `fnloom-core`: a graph built from node specs, with cycle and duplicate detection; upstream,
  downstream and between queries; plans compiled once from outputs, input names and override names; and an executor
  with `Evaluate` and `Observer` seams. No walk recurses over graph depth, and execution order is topological with ties
  broken by node name. `Plan::run_by_id` runs a plan reading its values by node id and returns the outputs by id.
- The Python front end: `fnloom.build(*modules)` and `Builder().with_modules(...).build()` return a `Driver` whose
  nodes are a module's public functions, wired by parameter name. `Driver.execute`, `Driver.prepare` and
  `PreparedPlan.run`; `node_names`, `what_is_upstream_of` and `what_is_downstream_of`.
- The native binding `fnloom._core`, with a typed stub. A node's own exception propagates as itself, with a note
  naming the node; a Rust panic becomes a Python exception.
- Differential tests against Apache Hamilton 1.90.0, and the register of deliberate differences (Q1–Q51).
- Decorators `fnloom.parameterize`, `source`, `value` and `tag`, with the `Source` and `Value` types they build. A
  decorator returns the function it was given, typed as itself. Tag values are strings only.
- `Driver.nodes`, a mapping from node name to a `NodeView` (the node's type, parameters, tags and origin), and the
  `Param` type its parameters are described with.
- Type checks. Each edge is checked at build, always, and a mismatch raises one `ValueError` listing every one. Each
  input and override is checked on every run, and a failing run raises one `ValueError` listing every failure.
  `Builder.without_input_type_checks()` turns the per-run checks off.
- Wall-clock benchmarks against Hamilton, with a `rows` shape (one function with `n` `@parameterize` rows) and fnloom
  measured with input checks on and off, and a CI gate on the core's instruction counts.
- A release workflow: a `vX.Y.Z` tag builds CPython 3.14 and 3.14t wheels for manylinux, musllinux, macOS and
  Windows, and the sdist, smoke-tests each, and publishes them to PyPI through trusted publishing. CI's checks now run
  on Windows too.
- A documentation site, built with Zensical and published to GitHub Pages.
- Result caching. `Builder.with_cache(store)` caches every node of a driver, keyed by the node's code version (read
  once, at build) and the fingerprints of its inputs and overrides, chained from node to node; a run loads only the
  stored results nearest its outputs. Stores `fnloom.MemoryCache` (least recently used eviction) and
  `fnloom.DiskCache` (one file per key, atomic writes, version headers, pluggable serializer), the `CacheStore`
  protocol, `MISSING` and `CacheWarning`: the cache never fails a run. The `@cache` decorator with
  `behavior="recompute"` or `"disable"`, `ignore=` per consuming parameter and `version=`, recorded as a
  `CachePolicy`. `fnloom.fingerprint`, `register_fingerprint` and the `__fnloom_fingerprint__` protocol, with
  `UnfingerprintableError`. `Driver.explain_cache`, which reports each node's `CacheOutcome` and runs nothing. A guide,
  [Caching](https://kelsayed17.github.io/fnloom/caching/), and cached-run benchmarks (`benches/cache.py`).
- Markdown lint with rumdl, run by `scripts/check.sh`, so every page renders as written on the docs site.

### Changed

- Building a graph does less Python work per parameter and per node, so every benchmark shape builds faster than in
  Hamilton (`fan_in` with 10,000 parameters: 17.5 against 32.6 ms). An input's `NodeView` is made when `Driver.nodes`
  first reads it.
- Each run costs less: input checks against a plain class are one `isinstance` call, a run makes no name strings, a
  `@parameterize` row is called without a Python wrapper when each source feeds one parameter, and `Driver.execute`
  skips the plan cache's key and lock when a request repeats the last one. A 10-node chain runs in about 1.9 µs, a
  10-input fan-in in 1.8 µs (13.1 before).
- Annotations are resolved at build with `typing.get_type_hints(include_extras=True)`, so an annotation that cannot be
  resolved is now a `NameError` (or, for a missing attribute such as `decimal.Missing`, an `AttributeError`) at build,
  with a note naming every failing node and parameter. Undecorated functions whose string annotations resolve behave
  as before; one that names a class imported only under `TYPE_CHECKING` now fails the build.
- `fnloom-core`: `NodeKind::Function` carries the node's payload, replacing the `Node::payload` field with the
  `Node::payload()` accessor, and `Evaluate::evaluate` receives the payload as an argument, so an evaluator never
  meets a function node without one.
- The README and docs are rewritten to be easier to read, with a graph diagram, a comparison with Apache Hamilton and
  pages that read well on a phone. Apache Hamilton is named as an incubating project.
- The package version comes from the Cargo workspace (`dynamic = ["version"]`), so the wheel and
  `fnloom._core.core_version()` always agree, and `fnloom.__version__` reports it.
- `scripts/check.sh` runs every Cargo step with `--locked`, and CI pins the instruction-count image by digest.

### Fixed

- `DiskCache` no longer warns on Windows that every cache directory is writable by other users. It judged access by
  POSIX mode bits, which Windows reports as `0o777` for every directory; it now checks them on POSIX only.
- The cache returned a stale result after an edit it should have seen. A module constant read through another name
  (a default, a helper handed the module, a class attribute), a function's own attribute, a name read as a string
  (`globals()["RATE"]`, `getattr(config, "RATE")`), a name a module's `__getattr__` serves, and a method of an input
  value's own class now all count in a code version or a fingerprint. Fingerprinting an input of a user class costs
  about 1 µs more per value.
- A pandas index's frequency and a frame's or series' `attrs` are part of their fingerprints.
- A `set` or `frozenset` is fingerprinted in iteration order, and a numpy array by its strides as well as its
  bytes, so a node that reads a set's order or an array's layout never gets another order's or layout's result. A
  set of strings now fingerprints differently in each process unless `PYTHONHASHSEED` is fixed, so with `DiskCache`
  it misses across processes; pass a sorted tuple to share results.
- Packages installed outside the interpreter's own `site-packages` (an overlay or user environment, a `PYTHONPATH`
  entry) are libraries, versioned by name, not user code whose globals could make a node uncacheable.
- `DiskCache` makes its directories private to their owner, warns when it opens a directory others can write, warns
  about what `clear()` cannot remove, never removes a good entry another process wrote since it read a bad one, and
  refuses a path with a NUL byte. `with_cache` refuses an object that is not a store.
- A refusal of a cyclic or deeply nested value shortens its path, and `inspect.signature(fnloom.register_fingerprint)`
  works.
- An edge check now strips `Annotated` and PEP 695 aliases before it looks for `Any` or a `TypeVar`, so a node
  returning `Annotated[Any, ...]` or an alias of `Any` feeds any parameter, as `Any` does (Q41, Q44).
- An input check against a recursive alias such as `type Tree = list[Tree]` refuses a string instead of raising
  `RecursionError`.
- The type stub `fnloom/_core.pyi` matches the extension: `Graph`, `KeyPlan` and `ObserverBase` construct through `__new__`,
  and `Plan.run`'s `observers` defaults to `()` in its runtime signature too.

[Unreleased]: https://github.com/kelsayed17/fnloom/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/kelsayed17/fnloom/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/kelsayed17/fnloom/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/kelsayed17/fnloom/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/kelsayed17/fnloom/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/kelsayed17/fnloom/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/kelsayed17/fnloom/releases/tag/v0.1.0
