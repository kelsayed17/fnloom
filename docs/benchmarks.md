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
| chain | 10 | 0.417 / 0.430 / 0.127 † | 0.093 / 0.050 / 0.013 | 0.038 / 0.005 / 0.003 | 0.015 / 0.006 |
| chain | 100 | 2.041 / 2.902 / 1.038 † | 0.365 / 0.071 / 0.087 | 0.306 / 0.018 / 0.017 | 0.029 / 0.016 |
| chain | 1,000 | 19.9 / 13.2 / 13.6 | `RecursionError` / 0.387 / 0.287 | `RecursionError` / 0.164 / 0.137 | 0.168 / 0.079 |
| chain | 10,000 | 208.7 / 109.7 / 135.5 | `RecursionError` / 3.281 / 2.133 | `RecursionError` / 1.338 / 1.332 | 1.845 / 0.692 |
| fan_in | 10 | 0.178 / 0.112 / 0.054 | 0.070 / 0.348 / 0.018 | 0.037 / 0.015 / 0.003 | 0.017 / 0.208 |
| fan_in | 100 | 0.343 / 0.227 / 0.178 | 0.298 / 0.229 / 0.062 | 0.265 / 0.133 / 0.029 | 0.066 / 0.028 |
| fan_in | 1,000 | 2.941 / 1.895 / 1.642 | 2.749 / 2.174 / 0.826 | 2.729 / 1.540 / 0.482 | 0.574 / 0.307 |
| fan_in | 10,000 | 67.7 / 22.7 / 18.0 | 42.4 / 39.3 / 22.6 | 41.8 / 31.6 / 18.9 | 6.952 / 3.571 |
| layered | 10 | 0.292 / 0.146 / 0.109 | 0.065 / 0.042 / 0.011 | 0.037 / 0.005 / 0.003 | 0.012 / 0.005 |
| layered | 100 | 2.099 / 1.119 / 1.106 | 0.412 / 0.062 / 0.044 | 0.385 / 0.021 / 0.020 | 0.037 / 0.019 |
| layered | 1,000 | 19.8 / 33.4 / 11.5 † | 3.955 / 0.414 / 0.267 | 3.857 / 0.182 / 0.180 | 0.171 / 0.074 |
| layered | 10,000 | 210.6 / 186.1 / 123.8 | `RecursionError` / 4.344 / 2.816 | `RecursionError` / 1.850 / 1.821 | 1.974 / 0.764 |
| rows | 10 | 0.233 / 0.120 / 0.077 | 0.067 / 0.044 / 0.011 | 0.038 / 0.006 / 0.005 | 0.011 / 0.004 |
| rows | 100 | 1.813 / 0.658 / 0.609 | 0.348 / 0.076 / 0.052 | 0.315 / 0.036 / 0.034 | 0.027 / 0.013 |
| rows | 1,000 | 60.7 / 7.688 / 7.112 | `RecursionError` / 0.520 / 0.430 | `RecursionError` / 0.343 / 0.368 | 0.157 / 0.059 |
| rows | 10,000 | 5,167.8 / 132.9 / 90.1 | `RecursionError` / 5.735 / 4.345 | `RecursionError` / 3.491 / 3.468 | 2.479 / 0.633 |

Measured on an Apple M2 Pro with 16 GB, macOS 27.0, CPython 3.14.9, `apache-hamilton==1.90.0`, and a release build
of the extension, on 2026-10-27.

**Steady execution**, the cost of running a prepared plan again, is where fnloom differs most: with checks on it is
1.3 to 21 times faster than Hamilton's `execute` wherever both finish, because the plan is compiled once and each run
is a loop over its steps in Rust. The low end is `fan_in`, 1.3 times at 10,000 parameters, where one Python call with
10,000 keyword arguments is most of the cost for both.

**The cost of input checks** is the gap between the two fnloom figures. Per run it is small next to the call itself
(steady execution, checks on against off, differs by at most 0.03 ms on every `chain`, `layered` and `rows` graph, and
by 0.1 to 12.7 ms on the `fan_in` graphs of 100 parameters or more, where every one of the `n` inputs is checked). On
`fan_in` that is more than half of the steady time at 1,000 parameters (1.540 against 0.482 ms).

### Where Hamilton raises

Hamilton builds the 1,000- and 10,000-node chains, the 10,000-node layered graph and the 1,000- and 10,000-row `rows`
graphs, then raises `RecursionError` on the first `execute`: its graph walk recurses once per level of depth. fnloom
runs all 16 cases, with checks on and off, and every answer it gives is checked against the shape's closed form.

### Build: re-measured

The design calls a row where fnloom is reliably slower a bug. The table's build column is one sample per cell, with
Hamilton running first in the same process, so it cannot settle this. The builds it marks †, and the `fan_in` builds
that were reliably slower before the speedups below ([#5](https://github.com/kelsayed17/fnloom/issues/5),
[#6](https://github.com/kelsayed17/fnloom/issues/6), [#7](https://github.com/kelsayed17/fnloom/issues/7)), were
re-measured with one fresh process per sample, nine samples per runtime and case, the two runtimes alternating which
goes first, on a release build, timing only the build call (medians, ms, Hamilton / fnloom):

| shape | n | Hamilton / fnloom | verdict |
| --- | ---: | --- | --- |
| chain | 10 | 0.362 / 0.201 | fnloom faster |
| chain | 100 | 2.034 / 1.208 | fnloom faster |
| layered | 1,000 | 21.871 / 14.044 | fnloom faster |
| fan_in | 100 | 0.407 / 0.278 | fnloom faster (was 0.419 / 0.542) |
| fan_in | 1,000 | 4.508 / 1.982 | fnloom faster (was 4.290 / 4.491) |
| fan_in | 10,000 | 33.3 / 21.1 | fnloom faster (was 33.0 / 49.8) |

No build is reliably slower. The 10-parameter `fan_in` first execute, re-measured the same way, was not slower either
(0.077 against 0.068 ms). The checks-on and checks-off builds do the same build work, since input checks run per
run, so their differences in the table (for example layered 1,000, 33.4 against 11.5 ms) are noise.

The `fan_in` builds were slow from per-parameter Python work, not from the Rust core, which is under a fifth of the
build. They are now faster from four changes: an ordinary function's parameters are read off its code object, with
`inspect.signature` kept for wrapped functions, a `__signature__` and the unsupported kinds; resolving annotations
keeps every parameter whose annotation was already a value, such as `int`, instead of making it again, and skips
plain classes when it looks for type aliases; an input read by one parameter is typed without comparing consumers;
and an input's `NodeView` is made when `Driver.nodes` first reads it, not at build, while the input checks read
each input's type without making its view.

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
