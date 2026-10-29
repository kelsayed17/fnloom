# fnloom Plan 7: drawing, export and the CLI

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Status:** done, 2026-10-28, on `claude/plan-7-viz-cli`, merge pending. [As built](#as-built) records where the build
went beyond the plan or narrowed it.

**Goal:** close the roadmap's step 6. That means:

- **Drawing:** DOT text for the whole graph, upstream of nodes, downstream of nodes, the path between two nodes and
  an execution, on both drivers, with no dependency. Mermaid text too, which the roadmap's register row promised.
- **Rendering:** an image when Graphviz is installed, with an error that says how to get it when it isn't.
- **Export:** a run's graph as JSON, as Hamilton's `export_execution` gives, and a versioned format a tool can read.
- **CLI:** a `fnloom` command over `Builder` arguments: config, inputs and overrides included.
- **Ledger:** the drawing, export and CLI rows of the roadmap. None may still say **same** or **worse** when this plan
  is done, and "Graph visualisation" leaves the "Not there yet" list in [Coming from Hamilton](../from-hamilton.md).

**Architecture:** all of it is Python, off the run path. A new `_drawing.py` turns a set of node names plus marks
(output, override, path, context) into a `Drawing`: nodes in topological order, edges sorted, each node with its label
text. `Drawing` writes DOT or Mermaid text itself, and `render` pipes the DOT to Graphviz's `dot` executable. The
drivers choose the node sets with the graph queries they already have (`upstream_of`, `downstream_of`, `between`,
`prepare`). A new `_export.py` builds the JSON document from `Driver.nodes`, the request and Plan 4's code versions. The
CLI (`_cli.py`, `fnloom.__main__`) uses `argparse`, loads modules by dotted name or file path, and calls the public
API only.

**Tech Stack:** as Plans 1–6. Python 3.14 for all tasks, ty strict, ruff ALL, rumdl, pytest, and
`apache-hamilton==1.90.0` as the oracle. No Rust change. No new runtime or development dependency: the Graphviz
binary is optional at run time and in CI, and tests that render skip without it.

**Spec:**

- `docs/design/2026-10-26-fnloom-design.md` §2 (native API, no runtime dependency) and §5 (v1.x order).
- `docs/plans/2026-10-28-roadmap-ahead-of-hamilton.md`: the drawing, `export_execution` and CLI rows, and the two
  register-row lines "Visualisation without graphviz returns `None`" and "The CLI can't build or diff a graph that
  needs config or overrides".
- The oracle's behaviour, probed on 2026-10-28, is catalogued in the session notes as
  `hamilton-plan7-drawing-export-cli.md` and cited below as "H7.3" and so on. Tests re-probe whatever they rely on.

## fnloom today, probed against the same points

| Point | Hamilton 1.90.0 | fnloom 0.2.0 + Plan 6 |
|---|---|---|
| Drawing a graph | five methods returning a `graphviz.Digraph`, or `None` without the package (H7.1, H7.9) | none; `what_is_upstream_of`, `what_is_downstream_of` and `what_is_the_path_between` list names |
| Order of the DOT text | changes with `PYTHONHASHSEED` (H7.3) | n/a |
| Type labels | origin name only: `list`, `dict`, `Union` (H7.4) | n/a; errors print `inspect.formatannotation` |
| Inputs and config | inputs as per-consumer boxes, config keys as nodes in every drawing (H7.2, H7.5) | n/a; config is never a node (Q56) |
| Exporting a run | JSON of `HamiltonNode.as_dict()`; inputs carry a consumer's source and version (H7.10) | none |
| Versions | source-text hashes; graph version ignores names and edges (H7.11) | code versions for caching (Plan 4), not exposed |
| CLI | `build`, `diff`, `validate`, `version`, `view`, `ui`; files only; no config without a context file (H7.12–H7.14) | none |

## Decisions for review

Each decision becomes a register row, Q90 on, in the task that builds it. Change any before the build starts.

1. **Drawing methods (Q90).** On `Driver` and `AsyncDriver`, each returning a `fnloom.Drawing`:
    - `draw() -> Drawing`: every node and input.
    - `draw_upstream_of(*names: str) -> Drawing`: the named nodes and everything they depend on.
    - `draw_downstream_of(*names: str) -> Drawing`: the named nodes and everything that depends on them, plus each
      drawn node's other direct dependencies, marked `context` (KEEP: Hamilton adds the parents too, H7.1, but draws
      them like any node; fnloom draws them faded, so the reader sees what is downstream and what is only feeding it).
    - `draw_path_between(upstream: str, downstream: str, *, context: bool = True) -> Drawing`: the nodes on a path,
      marked `path`; with `context`, each path node's other direct dependencies, marked `context`. `context=False` is
      Hamilton's `strict_path_visualization=True` (H7.1).
    - `draw_execution(outputs, *, inputs=None, overrides=None, validate=True) -> Drawing`: see decision 4.
    - Names are checked as Q70's queries check them: every unknown name in one sorted `ValueError`, a non-`str` a
      `TypeError`. Hamilton names only the first unknown path endpoint (H7.5). `draw_upstream_of()` with no names is
      a `TypeError` ("give at least one node"), where Hamilton draws an empty graph.
    - No path between the endpoints is a `ValueError`, `no path from 'c' to 'a'` (KEEP, H7.5).
    - The names are fnloom's own, not Hamilton's `display_*`/`visualize_*`: design §2 keeps no compatibility layer,
      and the `from-hamilton` guide maps each.
2. **What a `Drawing` is (Q90).** A frozen, slotted dataclass exported from `fnloom`:
    - `nodes: tuple[DrawnNode, ...]` in topological order, ties broken by name, so the text never depends on
      `PYTHONHASHSEED` (Hamilton's does, H7.3). `edges: tuple[tuple[str, str], ...]`, `(dependency, node)`, in the
      order of their nodes, then of their dependencies.
    - `DrawnNode` (frozen, slotted, exported): `name`, `kind: Literal["function", "input"]`, `type: str` (the label
      text of decision 3), `condition: str | None` (decision 3), `marks: frozenset[Literal["output", "override",
      "path", "context"]]`.
    - `to_dot(*, orient: Literal["LR", "TB", "RL", "BT"] = "LR", legend: bool = True) -> str` and
      `to_mermaid(*, orient=...) -> str`. A bad `orient` is a `ValueError` (Hamilton writes it as given, H7.5).
      `str(drawing)` is `to_dot()`.
    - `render(path: str | os.PathLike, *, format: str | None = None, orient=..., legend=...) -> pathlib.Path`
      writes an image through Graphviz's `dot` (decision 6). `format` defaults to the path's suffix; a `.dot` path
      writes the DOT text itself without Graphviz, never Graphviz's laid-out DOT (H7.7).
    - `_repr_svg_` renders inline in Jupyter when `dot` is on `PATH`, and returns `None` otherwise, so the notebook
      falls back to the DOT text: a notebook never raises for a missing tool.
    - A `Drawing` is plain data: it pickles and compares by value, and a test can assert on `nodes` without parsing
      text.
3. **Labels (Q91).**
    - A function node's label is its name over its return annotation, written by `inspect.formatannotation`:
      `dict[str, list[int]]`, `list[int] | None`. Hamilton writes `dict` and `Union` (H7.4). A label over 80
      characters is cut to 79 and `…` (KEEP the limit, H7.4).
    - A `@config.when*` node shows its conditions under the name, from `NodeView.config`: `mode == 'fast'`. Hamilton
      shows only the key, `e: mode` (H7.2).
    - A `display_name` tag replaces the name in the label, as Hamilton's does (KEEP, H7.2, Hamilton #1413). The node's
      id in the text stays its name.
    - Names and labels are always quoted and escaped, so a namespaced name `ns.a` (Q74) or a label with `<`, `"` or
      `|` is valid DOT and Mermaid.
4. **Execution drawings (Q92).** `draw_execution(outputs, *, inputs=None, overrides=None, validate=True)`:
    - `outputs`, `inputs` and `overrides` take what `execute` takes, functions in `outputs` included (Q30). Hamilton's
      `export_execution` refuses functions while its `visualize_execution` accepts them (H7.10).
    - It draws exactly what `execute` would compute: the nodes and inputs the plan reads, the outputs marked
      `output`, every override marked `override`, and nothing only an override needed (KEEP, H7.5).
    - An input requested as an output is drawn and marked `output`. Hamilton leaves it unmarked inside a box (H7.5).
    - With `validate=True`, it refuses what `Driver.validate` refuses, all problems in one `ValueError` (Q71), so a
      drawing never shows a run that can't happen. `validate=False` draws from the names alone, without checking
      values; it is Hamilton's `bypass_validation` (KEEP, H7.6). Request-shape errors (unknown names) are raised
      either way.
5. **Inputs and config in drawings (Q93).**
    - Every input is one node, drawn once with a dashed outline, with an edge to each consumer. Hamilton draws a box
      per consumer and duplicates an input read twice; `deduplicate_inputs` only merges identical sets (H7.2). An input
      as a node makes `hide_inputs`/`deduplicate_inputs` unnecessary; `draw*(..., inputs=False)` is not added, since
      a reader can filter `Drawing.nodes`.
    - Config is never drawn as a node, since it is never one (Q56). Hamilton draws every config key in every drawing,
      used or not (H7.5). The conditions a node was chosen by are on its label (decision 3).
    - An input every consumer has a default for is drawn like a required one, with its type only;
      `Driver.inputs()` already says which inputs are required.
6. **No graphviz dependency (Q94).**
    - DOT and Mermaid text need nothing. Rendering runs the `dot` executable from Graphviz (`shutil.which("dot")`)
      with `subprocess`, so there is no Python extra to install: Hamilton's `graphviz` package only runs that same
      executable.
    - Without `dot`, `render` raises `FileNotFoundError: rendering needs Graphviz's 'dot' on PATH; install Graphviz
      (https://graphviz.org/download/), or write the DOT text with drawing.to_dot()`. A `dot` failure raises
      `RuntimeError` with its stderr. Hamilton returns `None`, logs a traceback at ERROR and names `sf-hamilton`
      (H7.9). The roadmap promised an `ImportError` naming an extra; with no extra, the error names the tool.
7. **Export (Q95).** `Driver.export(outputs=None, *, inputs=None, overrides=None, validate=True) -> dict[str,
   object]`, and the same on `AsyncDriver`: a JSON-ready document. `json.dumps` of it is deterministic with
   `sort_keys=True`.
    - **What it describes.** With no `outputs`, the whole graph. With `outputs`, the run `execute` would make, chosen
      and validated as in decision 4. Hamilton's `export_execution` describes a planned run too: nothing is executed
      and no values are exported (H7.10). A record of an executed run, with timings and outcomes, is a `Hook`'s job
      and is not built here (decision 11).
    - **Document:** `{"format": "fnloom.export/1", "fnloom": __version__, "graph_version": str | None,
      "config": {key: repr(value)}, "request": {"outputs": [...], "inputs": [...], "overrides": [...]} | None,
      "nodes": [...]}`. Config values are written as `repr`, since they need not be JSON (Hamilton omits config, so
      its export can't say which `@config.when` variant was chosen, H7.10). `request` lists names only, never values.
    - **A node:** `{"name", "kind": "function" | "input", "type": formatannotation text, "params": [{"name",
      "reads": node name | null, "value": repr | null, "required"}], "dependencies": [sorted names], "tags",
      "doc", "module", "function": qualified name | null, "namespace", "config": [condition text], "source": text |
      null, "version": hex | null, "marks": [...]}`, in topological order, ties broken by name. `marks` uses decision
      2's names.
    - **Inputs are inputs.** An input has `kind: "input"`, its narrowest type, `required` and its consumers, and no
      source or version. Hamilton exports an input with its first consumer's source and version (H7.10).
    - Every input the request reads is exported, supplied or not; Hamilton drops an unsupplied defaulted input
      (H7.10). Overrides are marked `override` (Hamilton exports `b` with no mark, H7.10).
    - `source` is `inspect.getsource` of the function, `null` when Python can't find it.
8. **Versions (Q96).**
    - A node's `version` is its Plan 4 code version in hex: it covers the code, defaults, closures and the globals the
      function reads, not just its text (Hamilton hashes the source text, H7.11). When a function has no code version
      (`UncacheableError`, such as reading a mutable global), `version` is `null` and the node gets `"unversioned":
      reason`.
    - `graph_version` hashes, for every node in order: its name, kind, type text, params with their bindings, config
      conditions and version. So renaming a node, rewiring an edge or changing a type changes it; Hamilton's graph
      version hashes only the sorted node versions and misses all three (H7.11). It is `null` when any node is
      unversioned, since a change inside that node can't be seen.
    - `Driver.version` is not added: `export()["graph_version"]` and the CLI give it.
9. **The CLI (Q97).** A `fnloom` console script and `python -m fnloom`, with stdlib `argparse` (Hamilton's needs
   `typer`, a dependency fnloom doesn't add).
    - **Loading modules.** Each positional is a dotted module name (`pkg.flows`) imported normally, or a `.py` path:
      its directory goes first on `sys.path` and it is imported under its stem, so a sibling import works. Hamilton
      compiles the file's text alone, and `from m1 import a` in a sibling fails (H7.12). `--namespace NS` applies to
      the module before it (Q74).
    - **Builder arguments.** `--config KEY=VALUE` (repeatable; `VALUE` is JSON, or a plain string when it isn't JSON),
      `--input KEY=VALUE`, `--override KEY=VALUE`, `--outputs a,b`, and `--context FILE` (`.json` or `.toml`, with
      the tables `config`, `inputs`, `overrides` and the list `outputs`). A flag wins over the same key in a context
      file. Hamilton takes config only from a context file, and without one silently drops every `@config.when` node
      (H7.12). The CLI builds exactly as `Builder` does: a `@config.when` node whose key is missing is left out,
      as it is in Python, and `--config` is how to choose it.
    - **Commands:**
        - `fnloom draw MODULE... [--upstream N...] [--downstream N...] [--path A B] [--execution] [--format
          dot|mermaid|svg|png|pdf] [--orient LR] [--no-legend] [-o PATH]`: DOT on stdout by default; an image format
          needs `-o`. `--execution` draws `--outputs` with the request's inputs and overrides.
        - `fnloom export MODULE... [--outputs ...] [-o PATH]`: decision 7's JSON, indented, keys sorted.
        - `fnloom validate MODULE... --outputs ...`: runs `Driver.validate`; prints nothing and exits 0 when the
          request is valid, else prints every problem and exits 1. Hamilton prints the context back (H7.12).
        - `fnloom diff OLD.json NEW.json`: compares two exports (decision 10).
    - No `run` command (decision 11), and no `ui` (Hamilton's starts a web server, design §2 non-goal).
10. **CLI output and `diff` (Q98).**
    - stdout carries only the result: DOT, Mermaid, or JSON. Every error is one line on stderr, `fnloom: TypeName:
      message` (several lines when the message has several), and exit status 1; a usage error is argparse's, status
      2. A build or validation error is fnloom's own exception message, never a Python `repr` of a dict. Hamilton
      logs a dict repr to stderr, and with `--json-out` prints nothing on stdout on failure (H7.13).
    - `diff` reads two exports and prints `{"added": [...], "removed": [...], "changed": {name: [fields]}}`, the
      fields being any of `kind`, `type`, `params`, `config`, `tags`, `version`, plus `"unversioned": [...]` for a
      node whose change can't be seen. Exit status 0 when nothing changed, 1 when something did, 2 on a bad file, so a
      CI step can gate on it. Inputs are compared as inputs: an input never shows as changed because a consumer
      did. Hamilton reports input `z` as edited when its consumer changes, and `diff --view` always fails in 1.90.0
      (H7.14).
    - `diff` compares exports, not git revisions: `fnloom export` on each side, then `diff`. Hamilton's reads the old
      files with `git show` and can't import their siblings (H7.12, H7.14); exporting on a checkout imports
      normally.
11. **Out of scope, each with a ledger line:**
    - `custom_style_function` and `graphviz_kwargs`: the DOT text is the styling surface; a reader can edit or
      post-process it, or build their own from `Drawing.nodes`.
    - `show_schema`: `@schema` is a Plan 9 decorator.
    - `keep_dot`: `render` writes the image; `to_dot()` is the text, written wherever the caller wants.
    - A `run` command, and an export of an executed run: values aren't JSON in general, and a `Hook` sees every value
      and outcome (Plan 6). The hooks guide gets a short recipe that records a run as JSON lines.
    - `ui` (a server) and Hamilton's dynamic-execution drawing (`Parallelizable` edges; Plan 10).

## Global Constraints

- **Earlier plans' constraints:** every Global Constraint of Plans 1–6 applies unchanged.
    - Python 3.14 and 3.14t.
    - ty strict, ruff ALL, Clippy pedantic, rumdl, and the crate's MSRV.
    - One commit per task, with `scripts/check.sh` green on every commit.
    - Author email `4119211+kelsayed17@users.noreply.github.com`, with the `Co-Authored-By:` trailer.
    - The Python extension is built with `maturin develop --features test-hooks`; pytest runs with `UV_NO_SYNC=1`.
- **No consumer content, no copying, no new dependency:** nothing consumer-specific (design §8). No Hamilton code is
  copied. No new runtime or development dependency; Graphviz is an optional executable.
- **Register rows come first:** every difference from Hamilton 1.90.0 gets its register row before the
  `deliberate("Qn")` test that cites it is collected. New rows continue from Q90. Every kept behaviour gets a
  differential test in `tests/differential/test_semantics.py`. Hamilton's drawings need the `graphviz` package,
  which the development environment doesn't have, so differential tests read Hamilton's node sets through
  `graph.get_upstream_nodes`, `get_downstream_nodes` and `_get_nodes_between`, and its export through
  `export_execution`, which needs no graphviz.
- **Off the run path:** nothing in this plan changes `execute`, `prepare` or the core. `benches.run` stays within
  noise; no new benchmark row.
- **Determinism:** every drawing, export and CLI output is byte-identical across `PYTHONHASHSEED` values, tested on
  three seeds in a subprocess.
- **Tests that render** skip when `dot` is not on `PATH`, with the reason shown by `-ra`; CI's Ubuntu image has no
  Graphviz, so the error path is what CI pins, and the render path runs where Graphviz is installed.

## Review Focus

Each line has its test in the owning task.

1. **Hash-seed independence.** `draw().to_dot()`, `to_mermaid()` and `json.dumps(export(), sort_keys=True)` of the
   probe graph are identical under `PYTHONHASHSEED` 0, 1 and 2. (Tasks 1, 4)
2. **An execution drawing with an override.** `draw_execution(["c"], inputs={"x": 1}, overrides={"b": 3})` draws
   `b` (override) and `c` (output) only, and an input requested as an output is marked `output`. (Task 2)
3. **Escaping.** A namespaced node `ns.a`, and a `display_name` of `a "b" <c>|d`, give DOT that `dot -Tsvg` accepts
   and Mermaid whose ids are valid. (Tasks 1, 3)
4. **Unversioned nodes.** A node reading a mutable global exports `version: null` with its reason, and
   `graph_version` is `null`; `diff` lists it under `unversioned`, never as unchanged. (Task 4)
5. **CLI sibling import and config.** A `.py` module importing its sibling builds; `--config mode=fast` selects the
   `@config.when` variant; an error exits 1 with one stderr line and an empty stdout. (Task 5)

## File Structure

```text
python/fnloom/
  _drawing.py    NEW  Drawing, DrawnNode, node sets and marks, DOT and Mermaid writers, render
  _export.py     NEW  the export document, versions, diff of two documents
  _cli.py        NEW  argparse, module loading, context files, the four commands
  __main__.py    NEW  `python -m fnloom`
  driver.py      draw, draw_upstream_of, draw_downstream_of, draw_path_between, draw_execution, export (both drivers)
  __init__.py    + Drawing, DrawnNode
pyproject.toml   [project.scripts] fnloom = "fnloom._cli:main"
tests/
  test_drawing.py  test_export.py  test_cli.py   NEW
  differential/test_semantics.py, differential/test_deliberate_differences.py
docs/drawing.md (NEW, in the site nav: drawing, export and the CLI), running.md, from-hamilton.md,
quirk-register.md, index.md, README.md, CHANGELOG.md, hooks.md (the run-record recipe), the roadmap's ledger
```

---

### Task 1: `Drawing` and the four graph drawings

**Files:** `python/fnloom/_drawing.py` (new), `driver.py`, `__init__.py`, `tests/test_drawing.py` (new),
`docs/quirk-register.md` (Q90, Q91, Q93), differential tests.

- [ ] Register rows Q90 (the methods and `Drawing`), Q91 (labels) and Q93 (inputs and config).
- [ ] Failing tests: node order and edge order of `draw()` on the probe graph; each method's node set and marks
      against decision 1; label text for generics, unions, `NewType`, `Annotated`, a forward reference, an 80+
      character type, a `@config.when` node and a `display_name`; quoting of `ns.a`; unknown and non-`str` names;
      no path; `orient` checked; hash-seed independence in a subprocess (Review Focus 1).
- [ ] `DrawnNode`, `Drawing` and `Drawing.to_dot` (DOT text: a `digraph` with quoted ids, HTML-free record labels, a
      dashed style for inputs, a fill per mark, a legend subgraph listing only the kinds and marks drawn).
- [ ] `draw`, `draw_upstream_of`, `draw_downstream_of`, `draw_path_between` on `Driver`, delegating on `AsyncDriver`.
- [ ] KEEP differential tests (node sets of upstream, downstream with parents, path with and without context, against
      Hamilton's graph queries) and `deliberate("Q90")`, `("Q91")`, `("Q93")` tests.
- [ ] `scripts/check.sh` green; commit.

### Task 2: Execution drawings

**Files:** `_drawing.py`, `driver.py`, `tests/test_drawing.py`, register (Q92), differential tests.

- [ ] Register row Q92.
- [ ] Failing tests: Review Focus 2; functions in `outputs`; `validate=True` refusing a missing input and a wrong type
      with `validate`'s message; `validate=False` drawing anyway; unknown names refused either way; the async driver
      gives the same drawing.
- [ ] `draw_execution` on both drivers, choosing nodes as `prepare` does.
- [ ] KEEP differential test (node set and override handling against `get_upstream_nodes(final_vars, inputs,
      overrides)`) and `deliberate("Q92")`.
- [ ] Check green; commit.

### Task 3: Mermaid and rendering

**Files:** `_drawing.py`, `tests/test_drawing.py`, register (Q94).

- [ ] Register row Q94.
- [ ] Failing tests: `to_mermaid` text for the probe graph (a `flowchart LR`, quoted labels, a class per mark);
      `render` to `.svg` and `.png` (skip without `dot`); `.dot` writes `to_dot()` with no Graphviz; no `dot` on
      `PATH` (monkeypatched `shutil.which`) raises Q94's `FileNotFoundError`; a failing `dot` raises `RuntimeError`
      with stderr; `_repr_svg_` returns `None` without `dot`; Review Focus 3 (escaping through `dot -Tsvg`).
- [ ] `to_mermaid`, `render`, `_repr_svg_`.
- [ ] `deliberate("Q94")`: Hamilton, without the graphviz package, returns `None`; fnloom's text needs nothing.
- [ ] Check green; commit.

### Task 4: Export and versions

**Files:** `python/fnloom/_export.py` (new), `driver.py`, `tests/test_export.py` (new), register (Q95, Q96),
differential tests.

- [ ] Register rows Q95 and Q96.
- [ ] Failing tests: the whole-graph document's shape and `format`; a request's document (marks, unsupplied
      defaulted input present, overrides marked, only names in `request`); config as `repr`; input nodes have no
      source or version; versions equal Plan 4's code versions; a mutable-global node is unversioned (Review Focus
      4); `graph_version` changes on a rename, a rewired edge and a changed type, and is stable across seeds;
      `diff_exports` on added, removed, changed and unversioned nodes; functions in `outputs`; async parity.
- [ ] `Driver.export`, `AsyncDriver.export`, the `diff` function in `_export.py` (private; the CLI calls it).
- [ ] KEEP differential test (the exported node set of a request equals Hamilton's computed nodes plus inputs) and
      `deliberate("Q95")`, `("Q96")`.
- [ ] Check green; commit.

### Task 5: The CLI

**Files:** `python/fnloom/_cli.py`, `__main__.py` (new), `pyproject.toml`, `tests/test_cli.py` (new), register (Q97,
Q98).

- [ ] Register rows Q97 and Q98.
- [ ] Failing tests, each running `main(argv)` in process with `capsys`, and one through `python -m fnloom` in a
      subprocess: dotted-name and file loading; a sibling import (Review Focus 5); `--namespace`; `--config`,
      `--input`, `--override` parsing (JSON and plain strings); a JSON and a TOML context, flags winning; each
      command's stdout; `-o` for text and images; errors on stderr with exit 1 and empty stdout; argparse usage exit
      2; `diff` exit statuses 0, 1 and 2.
- [ ] `_cli.main`, the console script, `__main__`.
- [ ] `deliberate("Q97")` (sibling import and config without a context file, against Hamilton's `commands.build`) and
      `deliberate("Q98")` (Hamilton's diff reporting an input as edited, against fnloom's).
- [ ] Check green; commit.

### Task 6: Docs and the ledger

**Files:** `docs/drawing.md` (new, in `zensical.toml`'s nav), `running.md`, `from-hamilton.md` (the method map and
"Not there yet"), `hooks.md` (the run-record recipe, tested), `index.md`, `README.md`, `CHANGELOG.md`, the roadmap
(rows and step 6 marked done), this plan's As built.

- [ ] Every code block in the new guide runs in a test, as the earlier guides' blocks do.
- [ ] Check green; commit.

## As built

- **`DrawnNode.title`** is a field decision 2 didn't list: what the label shows for the node, its `display_name` tag
  or its name, so a reader of `Drawing.nodes` sees the label without reading tags.
- **Legend ids step aside.** A node may be named like a legend entry (`@extract_columns("legend: function")`), so an
  entry's id gets underscores until no drawn node has it.
- **`draw_execution(validate=False)`** supplies only the inputs the run would miss, as `validate` finds them, not every
  input some consumer requires: an input another node requires but this run skips is not drawn, and an optional input
  requested as an output is drawn rather than refused. An override `with_strict_overrides()` refuses is still refused,
  since that refusal is by names alone. A cached driver draws every node the plan has; hits are known only at run
  time.
- **Validate's request checks are shared.** `Driver._shape` holds the checks of a request's names that `validate`
  ran inline, and `draw_execution` uses it; `validate` behaves as before.
- **`render`** reads the suffix in any case, writes `.gv` as DOT text like `.dot` (both are Graphviz's suffixes; never
  its laid-out DOT), writes `.mmd` as Mermaid, and raises `FileNotFoundError` naming the folder when the path's folder
  doesn't exist, before running `dot`. `_repr_pretty_` gives IPython the DOT text when there is no SVG; without it
  IPython would show the dataclass's `repr`. Mermaid escapes `&` and backticks too, and marks an override by its
  outline, as DOT does, not by a fill.
- **Decision 7 was wrong about defaulted inputs.** It said Hamilton drops an unsupplied defaulted input from an export
  while fnloom keeps it. In a validated run neither exports it, since the run never reads it. With `validate=False`,
  fnloom exports the inputs the run would miss, supplied or not. Q95 doesn't claim the difference.
- **Hamilton's export also crashes** on a function Python has no source for (one made by `exec`): `OSError`. fnloom
  exports `source: null`. Q95 records it.
- **Export text is the same in every process.** A config value's or `value()` binding's text is `stable_repr`: sets
  sorted, and the address in a default `repr` dropped. A `value()` holding a set is hashed into `graph_version` by that
  sorted text, since a set of strings' fingerprint follows the hash seed; one holding an object known only by its
  address makes `graph_version` `null`. A function reading a global `frozenset` of strings keeps the result cache's
  limit (its code version follows the seed); the guide says so.
- **Maintainer's review of the PR.** Five fixes, each with a regression test:
    - **`diff` sees a value its text hides.** Each `value()` parameter exports a `value_version`: its fingerprint, or a
      hash of its sorted text when it holds a set. `diff` compares it with the rest of `params`, so an array changed
      past its `repr`'s elision shows as changed. A value with no version makes its node `unversioned`. `diff` also
      reports `graph_version_changed`, and warns when the graph versions differ though no entry does; either makes the
      CLI's status 1.
    - **`stable_repr` walks every container it can see into:** set and mapping subclasses, `OrderedDict`, named tuples,
      list and tuple subclasses, and dataclass instances. A set inside any of them is sorted, in the text and in the
      `value_version`, under every hash seed.
    - **Run flags without a run are usage errors.** `--outputs`, `--input` and `--override` on `export` without
      `--outputs`, or on `draw` without `--execution`, exit 2 rather than being dropped.
    - **Formats in any case.** `render` lowercases `format` as well as the suffix; the CLI prints `mmd` and `gv` to
      stdout like `mermaid` and `dot`.
    - **`main` returns a status when a loaded module calls `sys.exit`**, and a refused file leaves `sys.path` alone. A
      `.py` file loaded by an earlier `main` call in the process is used again, not reloaded.
- **Pinned versions.** A `@cache(version="v1")` function exports a version hashed from the pin and `"pinned": "v1"`,
  as the result cache takes the pin; without this, a pinned function reading a mutable global would export as
  unversioned.
- **Each parameter exports its `type` and `slot`**, so a `group(...)` parameter's items are told apart.
- **`graph_version` leaves out tags and docs**, which don't change what a node computes; `diff` still reports a tag
  change. It also leaves out an `@extract_columns` `fill_with`, which the result cache keys on: a known gap, since
  `NodeView` doesn't carry the extractor.
- **`diff`** compares nodes only, not the documents' `config` or `request`; it compares `marks` too, so a run's outputs
  changing shows. It refuses a document that isn't an export's shape, or names a node twice, with a `ValueError`,
  which the CLI turns into status 2. At the maintainer's request, its result has `warnings`, one for each document
  with no `graph_version`, naming the unversioned nodes that hide their changes; the CLI prints each on stderr as
  well. A node it can't see into makes the status 1.
- **`KEY=VALUE` is JSON first**, so `--config n=7` is the int 7 and `--config n=007` the string `'007'`; the help and
  the guide say so, at the maintainer's request.
- **The CLI's namespace is `MODULE@NS`**, not a `--namespace` flag after the module: argparse can't attach a flag to a
  positional. `@` starts a namespace only when a dotted name follows it, so a folder named `a@b` stays a folder.
- **The CLI loads a `.py` path under its stem** with `spec_from_file_location`, and refuses one whose stem is already
  imported from another file (two files named `flow.py`), or that names a standard library module (`json.py`,
  `statistics.py`), as a usage error, rather than silently using one module for another. An `@` in a file's own name
  (`my@flow.py`) is part of the path.
- **`main` always returns a status**, `--help` and usage errors included, so an in-process caller never sees
  `SystemExit`; the console script exits with it. A closed pipe (`fnloom draw ... | head`) ends quietly with status 1,
  and Ctrl-C with 130. Without `--format`, `draw -o FILE` writes the format the suffix names.
  `--no-context` without `--path`, `--no-validate` without `--execution`, and `outputs` in a context file that aren't
  names are usage errors.
- **`validate` prints its problems on stdout**, one per line: they are the command's result. Errors are on stderr.
- **Differential tests drive Hamilton's own drawing methods** through a stand-in `graphviz` module
  (`FAKE_GRAPHVIZ` in `tests/differential/harness.py`), since the development environment has no graphviz package.
  The CLI rows test Hamilton's `hamilton.cli.commands` and `logic`, which need no `typer`; Hamilton's stdout and
  `--json-out` behaviour (H7.13) is from the catalogue's probes.
- **Rendering is tested on CI** with a stand-in `dot` executable on `PATH`; the tests that run the real Graphviz skip
  where it isn't installed.
- **DOT ids escape backslashes**, so a node named with a backslash shows it doubled in Graphviz's own output. Names
  with backslashes are rare, and the drawing itself is right.
- **Register rows:** Q90 (drawing methods and `Drawing`), Q91 (labels), Q92 (execution drawings), Q93 (inputs and
  config), Q94 (no dependency, `render`), Q95 (export), Q96 (versions), Q97 (the CLI) and Q98 (CLI output and `diff`).
