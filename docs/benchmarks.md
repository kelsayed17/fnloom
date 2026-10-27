# Benchmarks

fnloom against Hamilton 1.90.0 on generated graphs, by wall clock. The design requires fnloom to be faster on build
and on prepared execution; a row where it is reliably slower is a bug.

## Results

Milliseconds, Hamilton / fnloom with input type checks on / fnloom with them off; lower is better. Hamilton has no
prepared plans and no way to switch its input checks off, so it is measured as it ships. The last column is fnloom's
alone (checks on / off). A cell marked † is a build where fnloom with checks on was slower than Hamilton in
that single sample.

| shape | n | build | first execute | steady execute | prepare |
| --- | ---: | --- | --- | --- | ---: |
| chain | 10 | 0.515 / 0.408 / 0.220 | 0.109 / 0.040 / 0.014 | 0.049 / 0.005 / 0.003 | 0.019 / 0.012 |
| chain | 100 | 2.239 / 3.664 / 1.722 † | 0.388 / 0.066 / 0.041 | 0.349 / 0.019 / 0.018 | 0.030 / 0.017 |
| chain | 1,000 | 17.7 / 16.5 / 15.0 | `RecursionError` / 0.341 / 0.219 | `RecursionError` / 0.148 / 0.137 | 0.151 / 0.064 |
| chain | 10,000 | 214.9 / 167.6 / 198.3 | `RecursionError` / 4.091 / 2.446 | `RecursionError` / 1.586 / 1.464 | 1.857 / 0.694 |
| fan_in | 10 | 0.176 / 0.144 / 0.082 | 0.070 / 0.073 / 0.011 | 0.036 / 0.011 / 0.003 | 0.020 / 0.006 |
| fan_in | 100 | 0.347 / 0.592 / 0.533 † | 0.317 / 0.212 / 0.075 | 0.283 / 0.108 / 0.029 | 0.068 / 0.031 |
| fan_in | 1,000 | 3.600 / 5.491 / 5.545 † | 3.132 / 1.849 / 0.846 | 2.898 / 1.237 / 0.493 | 0.496 / 0.339 |
| fan_in | 10,000 | 32.4 / 57.4 / 71.4 † | 45.2 / 33.0 / 22.6 | 42.0 / 26.3 / 18.8 | 7.142 / 3.811 |
| layered | 10 | 0.292 / 0.226 / 0.168 | 0.064 / 0.049 / 0.012 | 0.037 / 0.004 / 0.004 | 0.017 / 0.005 |
| layered | 100 | 2.081 / 1.816 / 1.759 | 0.427 / 0.066 / 0.044 | 0.385 / 0.021 / 0.020 | 0.034 / 0.026 |
| layered | 1,000 | 19.9 / 20.1 / 18.4 † | 3.956 / 0.375 / 0.263 | 3.886 / 0.183 / 0.182 | 0.163 / 0.074 |
| layered | 10,000 | 241.8 / 268.1 / 222.9 † | `RecursionError` / 4.277 / 2.807 | `RecursionError` / 1.857 / 1.823 | 2.028 / 0.759 |
| rows | 10 | 0.260 / 0.167 / 0.108 | 0.076 / 0.056 / 0.012 | 0.040 / 0.006 / 0.004 | 0.013 / 0.004 |
| rows | 100 | 1.843 / 0.825 / 0.801 | 0.350 / 0.075 / 0.053 | 0.314 / 0.036 / 0.034 | 0.028 / 0.013 |
| rows | 1,000 | 61.4 / 9.485 / 8.165 | `RecursionError` / 0.596 / 0.423 | `RecursionError` / 0.348 / 0.341 | 0.157 / 0.063 |
| rows | 10,000 | 5,145.4 / 143.6 / 105.3 | `RecursionError` / 6.059 / 4.288 | `RecursionError` / 3.703 / 3.554 | 1.709 / 0.513 |

Measured on an Apple M2 Pro with 16 GB, macOS 27.0, CPython 3.14.9, `apache-hamilton==1.90.0`, and a release build
of the extension, on 2026-10-27.

**Steady execution**, the cost of running a prepared plan again, is where fnloom differs most: with checks on it is
1.6 to 21 times faster than Hamilton's `execute` wherever both finish, because the plan is compiled once and each run
is a loop over its steps in Rust. The low end is `fan_in`, 1.6 times at 10,000 parameters, where one Python call with
10,000 keyword arguments is most of the cost for both.

**The cost of input checks** is the gap between the two fnloom figures. Per run it is small next to the call itself
(steady execution, checks on against off, differs by at most 0.15 ms on every `chain`, `layered` and `rows` graph, and
by 0.08 to 7.5 ms on the `fan_in` graphs of 100 parameters or more, where every one of the `n` inputs is checked). On
`fan_in` that is more than half of the steady time at 1,000 parameters (1.237 against 0.493 ms).

### Where Hamilton raises

Hamilton builds the 1,000- and 10,000-node chains, the 10,000-node layered graph and the 1,000- and 10,000-row `rows`
graphs, then raises `RecursionError` on the first `execute`: its graph walk recurses once per level of depth. fnloom
runs all 16 cases, with checks on and off, and every answer it gives is checked against the shape's closed form.

### Build: the rows where fnloom with checks on is slower

The design calls a row where fnloom is reliably slower a bug. The table's build column is one sample per cell, with
Hamilton running first in the same process, so it cannot settle this: it marks six builds †, re-measured below, and
three of them are reliably slower. They were re-measured with one fresh process per sample, nine samples per runtime
and case, the two runtimes alternating which goes first, on a release build, timing only the build call (medians, ms,
Hamilton / fnloom). The three `fan_in` rows were measured again after the scan speedups of the whole-branch review
(no `dataclasses.replace` per parameter, no identity check of an undecorated function's bindings, no default `Source`
made for a parameter its row binds, and a `NamedTuple` for each input's consumers); those are the post-fix figures
below, in place of the earlier 0.402 / 0.633, 3.078 / 5.344 and 31.8 / 56.5. The main table's `fan_in` build cells
are still the earlier single samples.

| shape | n | Hamilton / fnloom | verdict |
| --- | ---: | --- | --- |
| chain | 100 | 2.040 / 1.693 | fnloom faster |
| layered | 1,000 | 21.974 / 20.300 | fnloom faster |
| layered | 10,000 | 215.8 / 206.0 | fnloom faster |
| fan_in | 100 | 0.419 / 0.542 (post-fix) | **slower, issue to open** |
| fan_in | 1,000 | 4.290 / 4.491 (post-fix) | **slower, issue to open** |
| fan_in | 10,000 | 33.0 / 49.8 (post-fix) | **slower, issue to open** |

Only `fan_in` builds are reliably slower, by 1.05 to 1.5 times after the fixes (1.6 to 1.8 before; Hamilton's own
1,000-parameter median moved from 3.078 to 4.290 ms between the two runs, so that row's narrower gap is partly
noise), and each is an issue to open (none opened yet). The other † cells in the table, and the 10-parameter `fan_in` first execute (0.073
against 0.070 ms), were not reliably slower. The checks-on and checks-off builds do the same build work, since input
checks run per run, so their differences in the table (for example chain 100, 3.664 against 1.722 ms) are noise.

The remaining cost is per-parameter Python work in the scan and the node views, not type resolution. A `cProfile` of
one post-fix fnloom build of `fan_in` with 10,000 parameters (0.107 s under the profiler, which inflates it) splits
the build into the scan, 45% (`collect`, 0.048 s, of which one `inspect.signature` call is 21% of the build and binding
each parameter, `_bind`, 20%); resolving the annotations, 16% (`resolve`, 0.017 s, of which `typing.get_type_hints`
is 7%); the node views, 31% (`node_views`, 0.033 s, of which working out the input types is 17%); making the native
graph, 6% (`_graph_of`); and the edge checks, 2%.

## Why fan-in is fast

A node with `n` parameters is called with `n` keyword arguments. CPython binds a keyword to a parameter by identity
with the interned parameter name, and when no name matches by identity it compares the keyword with `==` against
every parameter, so fresh name strings make a call cost `n²` string comparisons. fnloom interns every node's name once,
when the graph is built, and passes those exact strings
(`tests/test_binding.py::test_keyword_names_reach_the_function_interned`). What remains is CPython's own identity
scan, `n` pointer comparisons per keyword, which Hamilton's calls pay too.

## Run it

```bash
uv run maturin develop --release   # time an optimised extension, not the debug build
uv run python -m benches.run > bench.jsonl 2> bench.stderr
```

It prints 48 JSON lines to stdout (`shape`, `n`, `runtime`, `input_checks`, `build_ms`,
`first_execute_ms`, `steady_execute_ms`, `prepare_ms`, `error`, `error_phase`).
Hamilton prints a help banner to stderr whenever it raises, hence the second redirect.
Each runtime's answer is checked against the shape's closed form, and a
mismatch aborts the run. Generated modules are dropped from `sys.modules` after each case.

### What is timed

All times use `time.perf_counter_ns`, in milliseconds. Graph generation is not timed.

- `build_ms` (one sample): module to ready driver (`fnloom.build`; `hamilton.driver.Builder().build()`).
- `first_execute_ms` (one sample): the first `execute` call, which for fnloom includes compiling the plan.
- `steady_execute_ms`: the median of 7 runs after one warm run. fnloom uses a prepared plan's `run`; Hamilton has no
  prepared plans, so it uses `execute`.
- `prepare_ms` (one sample): a cold `Driver.prepare` for the same request, on a newly built driver so no plan is
  cached. Always null for Hamilton.
- A runtime that raises records the exception class in `error` and the phase in `error_phase` (`build`,
  `first_execute`, `steady_execute` or `prepare`). Timings of earlier phases are kept; the rest are null.
- `input_checks`: Hamilton is always `true`, as it ships. fnloom is measured twice per graph, with the checks on and
  with `Builder.without_input_type_checks()`.
- Argument copies (`[*outputs]`, `dict(inputs)`) are made once, outside the timed regions.

### Shapes

- `chain`: `n` nodes in a line, each adding one to the one before; one input, `seed`.
- `fan_in`: one function with `n` parameters, all inputs, summed.
- `layered`: `n` nodes in rows of ten, each reading two nodes of the row above; one input, `seed`.
- `rows`: one function with `n` `@parameterize` rows, each sourcing the row before it, the first sourcing the input
  `seed`. The module is generated in Hamilton's spelling for Hamilton and in fnloom's for fnloom.

## The instruction-count gate

Wall-clock time is too noisy on shared CI runners to gate on, so CI gates the Rust core instead: it counts the
instructions `Graph::build`, `Graph::prepare` and `Plan::run` execute under callgrind, on chain, fan-in and layered
graphs of 1,000 and 10,000 nodes (`crates/fnloom-core/benches/core.rs`). A rise of more than 2% over the committed
`crates/fnloom-core/benches/baseline.json` fails the `instruction-counts` job, which runs `scripts/bench_gate.py`.

Counts depend on the CPU architecture, the toolchain and the C library, so the baseline is generated in the job's own
environment: the `rust:1.99.1` image on aarch64 Linux. A change that costs instructions on purpose regenerates it with
`--write-baseline` and says why in its commit message.
