# Changelog

All notable changes to fnloom are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project will adhere to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) from its first release. Before 1.0, a minor release may
change the public API; such changes are listed under **Changed** and **Removed** with what to write instead.

## [Unreleased]

### Added

- The Rust core, `fnloom-core`: a graph built from node specs, with cycle and duplicate detection; upstream,
  downstream and between queries; plans compiled once from outputs, input names and override names; and an executor
  with `Evaluate` and `Observer` seams. No walk recurses over graph depth, and execution order is topological with ties
  broken by node name.
- The Python front end: `fnloom.build(*modules)` and `Builder().with_modules(...).build()` return a `Driver` whose
  nodes are a module's public functions, wired by parameter name. `Driver.execute`, `Driver.prepare` and
  `PreparedPlan.run`; `node_names`, `what_is_upstream_of` and `what_is_downstream_of`.
- The native binding `fnloom._core`, with a typed stub. A node's own exception propagates as itself, with a note
  naming the node; a Rust panic becomes a Python exception.
- Differential tests against Apache Hamilton 1.90.0, and the register of deliberate differences (Q1–Q43).
- Decorators `fnloom.parameterize`, `source`, `value` and `tag`, with the `Source` and `Value` types they build. A
  decorator returns the function it was given, typed as itself. Tag values are strings only.
- `Driver.nodes`, a mapping from node name to a `NodeView` (the node's type, parameters, tags and origin), and the
  `Param` type its parameters are described with.
- Type checks. Each edge is checked at build, always, and a mismatch raises one `ValueError` listing every one. Each
  input and override is checked on every run, and a failing run raises one `ValueError` listing every failure.
  `Builder.without_input_type_checks()` turns the per-run checks off.
- Wall-clock benchmarks against Hamilton, with a `rows` shape (one function with `n` `@parameterize` rows) and fnloom
  measured with input checks on and off, and a CI gate on the core's instruction counts.
- A documentation site, built with Zensical and published to GitHub Pages.

### Changed

- Annotations are resolved at build with `typing.get_type_hints(include_extras=True)`, so an annotation that cannot be
  resolved is now a `NameError` at build, with a note naming every failing node and parameter. Undecorated functions
  with string annotations behave as before.
- `fnloom-core`: `NodeKind::Function` carries the node's payload, replacing the `Node::payload` field with the
  `Node::payload()` accessor, and `Evaluate::evaluate` receives the payload as an argument, so an evaluator never
  meets a function node without one.

[Unreleased]: https://github.com/kelsayed17/fnloom/commits/main
