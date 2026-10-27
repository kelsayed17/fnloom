# Benchmarks

fnloom against Hamilton 1.90.0 on generated graphs, by wall clock. The design requires fnloom to be faster on build
and on prepared execution; a row where it is reliably slower is a bug.

## Results

Milliseconds, Hamilton / fnloom with input type checks on / fnloom with them off; lower is better. Hamilton has no
prepared plans and no way to switch its input checks off, so it is measured as it ships. The last column is fnloom's
alone (checks on / off). A cell marked † is one where fnloom with checks on was slower than Hamilton in that single
sample.

| shape | n | build | first execute | steady execute | prepare |
| --- | ---: | --- | --- | --- | ---: |
| chain | 10 | 0.342 / 0.219 / 0.068 | 0.073 / 0.036 / 0.010 | 0.035 / 0.002 / 0.002 | 0.016 / 0.004 |
| chain | 100 | 1.904 / 1.986 / 0.531 † | 0.326 / 0.054 / 0.025 | 0.295 / 0.015 / 0.014 | 0.023 / 0.009 |
| chain | 1,000 | 18.5 / 7.367 / 5.518 | `RecursionError` / 0.326 / 0.215 | `RecursionError` / 0.159 / 0.139 | 0.176 / 0.060 |
| chain | 10,000 | 207.5 / 63.4 / 58.7 | `RecursionError` / 3.254 / 2.035 | `RecursionError` / 1.339 / 1.345 | 1.786 / 0.492 |
| fan_in | 10 | 0.152 / 0.052 / 0.025 | 0.067 / 0.037 / 0.012 | 0.034 / 0.002 / 0.001 | 0.015 / 0.006 |
| fan_in | 100 | 0.323 / 0.156 / 0.144 | 0.295 / 0.099 / 0.041 | 0.266 / 0.015 / 0.011 | 0.066 / 0.026 |
| fan_in | 1,000 | 3.024 / 1.467 / 1.245 | 2.777 / 0.932 / 0.593 | 2.733 / 0.312 / 0.269 | 0.597 / 0.308 |
| fan_in | 10,000 | 66.1 / 17.3 / 16.4 | 42.2 / 26.4 / 20.1 | 43.4 / 17.2 / 17.2 | 7.550 / 6.642 |
| layered | 10 | 0.323 / 0.120 / 0.163 | 0.068 / 0.047 / 0.024 | 0.039 / 0.003 / 0.002 | 0.015 / 0.007 |
| layered | 100 | 2.201 / 0.748 / 0.628 | 0.434 / 0.067 / 0.032 | 0.397 / 0.019 / 0.020 | 0.037 / 0.018 |
| layered | 1,000 | 21.6 / 7.219 / 7.502 | 4.757 / 0.374 / 0.269 | 4.353 / 0.209 / 0.184 | 0.270 / 0.088 |
| layered | 10,000 | 206.6 / 132.1 / 106.9 | `RecursionError` / 4.944 / 2.850 | `RecursionError` / 1.880 / 1.858 | 2.363 / 0.835 |
| rows | 10 | 0.265 / 0.123 / 0.060 | 0.078 / 0.037 / 0.007 | 0.043 / 0.002 / 0.002 | 0.010 / 0.004 |
| rows | 100 | 1.813 / 0.520 / 0.501 | 0.351 / 0.045 / 0.022 | 0.319 / 0.013 / 0.013 | 0.024 / 0.008 |
| rows | 1,000 | 63.2 / 5.566 / 7.593 | `RecursionError` / 0.301 / 0.207 | `RecursionError` / 0.137 / 0.139 | 0.171 / 0.187 |
| rows | 10,000 | 5,285.0 / 128.1 / 60.9 | `RecursionError` / 3.325 / 1.965 | `RecursionError` / 1.346 / 1.344 | 3.589 / 0.594 |

Measured on an Apple M2 Pro with 16 GB, macOS 27.0, CPython 3.14.9, `apache-hamilton==1.90.0`, and a release build
of the extension, on 2026-10-27.

**Steady execution**, the cost of running a prepared plan again, is where fnloom differs most: with checks on it is
2.5 to 24 times faster than Hamilton's `execute` wherever both finish, because the plan is compiled once and each run
is a loop over its steps in Rust. The low end is `fan_in`, 2.5 times at 10,000 parameters, where one Python call with
10,000 keyword arguments is most of the cost for both.

**The cost of input checks** is the gap between the two fnloom figures, and it is now within noise: steady execution,
checks on against off, differs by at most 0.05 ms on every graph. A check against a plain class, such as `int`, is
compiled once per plan to one `isinstance` call; only a generic, a union and the like take the general check.

### Where Hamilton raises

Hamilton builds the 1,000- and 10,000-node chains, the 10,000-node layered graph and the 1,000- and 10,000-row `rows`
graphs, then raises `RecursionError` on the first `execute`: its graph walk recurses once per level of depth. fnloom
runs all 16 cases, with checks on and off, and every answer it gives is checked against the shape's closed form.

### Build: re-measured

The design calls a row where fnloom is reliably slower a bug. The table's build column is one sample per cell, with
Hamilton running first in the same process, so it cannot settle this. The build it marks †, and the `fan_in` builds
that were reliably slower before ([#5](https://github.com/kelsayed17/fnloom/issues/5),
[#6](https://github.com/kelsayed17/fnloom/issues/6), [#7](https://github.com/kelsayed17/fnloom/issues/7)), were
re-measured with one fresh process per sample, nine samples per runtime and case, the two runtimes alternating which
goes first, on a release build, timing only the build call (medians, ms, Hamilton / fnloom):

| shape | n | Hamilton / fnloom | verdict |
| --- | ---: | --- | --- |
| chain | 10 | 0.368 / 0.136 | fnloom faster |
| chain | 100 | 2.009 / 0.724 | fnloom faster |
| layered | 1,000 | 21.991 / 8.608 | fnloom faster |
| fan_in | 100 | 0.428 / 0.222 | fnloom faster (was 0.419 / 0.542) |
| fan_in | 1,000 | 4.271 / 1.581 | fnloom faster (was 4.290 / 4.491) |
| fan_in | 10,000 | 32.6 / 17.5 | fnloom faster (was 33.0 / 49.8) |

No build is reliably slower. The checks-on and checks-off builds do the same build work, since input checks run per
run, so their differences in the table (for example rows 10,000, 128.1 against 60.9 ms) are noise.

A build was slow from per-parameter and per-node Python work, not from the Rust core, which is under a fifth of it.
For an ordinary function the scan reads its parameters off its code object and its kind off its code flags, keeping
`inspect` for wrapped functions, a `__signature__` and the unsupported kinds; resolving skips
`typing.get_type_hints` when every annotation is already a class, keeps every parameter and node it would make
again unchanged, and skips plain classes when it looks for type aliases; an input read by one parameter is typed
without comparing consumers; an undocumented module-level function skips the search for an inherited docstring; and
an input's `NodeView` is made when `Driver.nodes` first reads it, while the input checks read each input's type
without making its view.

### Per run

A prepared plan run once per incoming record (a market bar, say) pays its per-run overhead every time. Microseconds
per `PreparedPlan.run` on the same machine, median of nine batches, before and after the per-run work, checks on:

| shape | n | before | after | of which the native run |
| --- | ---: | ---: | ---: | ---: |
| chain | 10 | 3.46 | 1.92 | 1.49 |
| fan_in | 10 | 13.13 | 1.84 | 1.05 |
| layered | 100 | 22.23 | 19.13 | 18.77 |
| fan_in | 100 | 133.31 | 15.50 | 10.33 |
| rows | 100 | 36.86 | 13.51 | 13.17 |

What made the difference: input checks compiled to `isinstance` for plain classes; inputs read and outputs keyed by
each node's interned name, so a run makes no name strings (`Plan::run_by_id` in the core); a `@parameterize` row
whose sources each feed one parameter called directly, passing each source under its parameter's name, with its
`value()` constants bound by `functools.partial`, instead of through a Python wrapper; and `Driver.execute` skipping
the plan cache's key and lock when a request repeats the last one, the common case in a loop. One node call costs about
130 ns in the native run, against about 30 ns for a bare Python call; a run's fixed cost is about 0.3 µs native and
0.7 µs through `PreparedPlan.run`, and `Driver.execute` adds about 0.3 µs more.

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
