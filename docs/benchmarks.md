# Benchmarks

fnloom against Hamilton 1.90.0 on generated graphs, by wall clock. The design requires fnloom to be faster on build
and on prepared execution; a row where it is reliably slower is a bug.

## Results

Milliseconds, Hamilton / fnloom; lower is better. Hamilton has no prepared plans, so the last column is fnloom's
alone.

| shape | n | build | first execute | steady execute | prepare |
| --- | ---: | --- | --- | --- | ---: |
| chain | 10 | 0.762 / 0.309 | 0.107 / 0.045 | 0.040 / 0.004 | 0.013 |
| chain | 100 | 2.159 / 0.908 | 0.358 / 0.110 | 0.322 / 0.017 | 0.056 |
| chain | 1,000 | 21.5 / 10.1 | `RecursionError` / 0.715 | `RecursionError` / 0.165 | 0.494 |
| chain | 10,000 | 249.2 / 109.1 | `RecursionError` / 7.146 | `RecursionError` / 1.688 | 6.499 |
| fan_in | 10 | 0.202 / 0.166 | 0.072 / 0.062 | 0.038 / 0.003 | 0.015 |
| fan_in | 100 | 0.358 / 0.225 | 0.316 / 0.168 | 0.295 / 0.031 | 0.076 |
| fan_in | 1,000 | 3.638 / 2.455 | 3.803 / 1.614 | 3.386 / 0.638 | 0.902 |
| fan_in | 10,000 | 32.8 / 86.8 | 54.2 / 32.7 | 52.1 / 21.3 | 14.8 |
| layered | 10 | 0.361 / 0.153 | 0.084 / 0.066 | 0.042 / 0.004 | 0.013 |
| layered | 100 | 2.518 / 1.101 | 0.499 / 0.128 | 0.411 / 0.023 | 0.070 |
| layered | 1,000 | 28.8 / 13.1 | 6.349 / 1.138 | 5.185 / 0.227 | 1.583 |
| layered | 10,000 | 393.2 / 225.9 | `RecursionError` / 10.6 | `RecursionError` / 3.517 | 8.436 |

Measured on an Apple M2 Pro with 16 GB, macOS 27.0, CPython 3.14.9, `apache-hamilton==1.90.0`, and a release build
of the extension, on 2026-10-27.

**Steady execution**, the cost of running a prepared plan again, is where fnloom differs most: 5 to 23 times faster
than Hamilton's `execute` wherever both finish, because the plan is compiled once and each run is a loop over its
steps in Rust. The exception is the 10,000-parameter `fan_in` at 2.4 times, where one Python call with 10,000 keyword
arguments is most of the cost for both.

### Where Hamilton raises

Hamilton builds the 1,000- and 10,000-node chains and the 10,000-node layered graph, then raises `RecursionError` on
the first `execute`: its graph walk recurses once per level of depth. fnloom runs all 24 cases, and every answer it
gives is checked against the shape's closed form.

### The one slower row

In this run, building the 10,000-parameter `fan_in` took fnloom 86.8 ms against Hamilton's 32.8 ms. Each build is a
single sample, taken right after Hamilton's runs in the same process. Measured alone, in five fresh processes, fnloom
built it in 20 to 42 ms and Hamilton in 32 to 45 ms, fnloom faster every time. Treat the row as noise until a quieter
run says otherwise.

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

It prints 24 JSON lines to stdout (`shape`, `n`, `runtime`, `build_ms`, `first_execute_ms`, `steady_execute_ms`,
`prepare_ms`, `error`, `error_phase`). Hamilton prints a help banner to stderr whenever it raises, hence the second
redirect. Each runtime's answer is checked against the shape's closed form, and a mismatch aborts the run. Generated
modules are dropped from `sys.modules` after each case.

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
- Argument copies (`[*outputs]`, `dict(inputs)`) are made once, outside the timed regions.

### Shapes

- `chain`: `n` nodes in a line, each adding one to the one before; one input, `seed`.
- `fan_in`: one function with `n` parameters, all inputs, summed.
- `layered`: `n` nodes in rows of ten, each reading two nodes of the row above; one input, `seed`.

## The instruction-count gate

Wall-clock time is too noisy on shared CI runners to gate on, so CI gates the Rust core instead: it counts the
instructions `Graph::build`, `Graph::prepare` and `Plan::run` execute under callgrind, on chain, fan-in and layered
graphs of 1,000 and 10,000 nodes (`crates/fnloom-core/benches/core.rs`). A rise of more than 2% over the committed
`crates/fnloom-core/benches/baseline.json` fails the `instruction-counts` job, which runs `scripts/bench_gate.py`.

Counts depend on the CPU architecture, the toolchain and the C library, so the baseline is generated in the job's own
environment: the `rust:1.99.1` image on aarch64 Linux. A change that costs instructions on purpose regenerates it with
`--write-baseline` and says why in its commit message.
