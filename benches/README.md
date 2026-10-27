# Benchmarks

fnloom against Hamilton 1.90.0 on generated graphs. The design requires fnloom to be faster on
build and on prepared execution; a row where it is slower is a bug.

## Run it

```sh
uv run maturin develop --release   # time an optimised extension, not the debug build
uv run python -m benches.run > bench.jsonl 2> bench.stderr
```

It prints 24 JSON lines to stdout (`shape`, `n`, `runtime`, `build_ms`, `first_execute_ms`,
`steady_execute_ms`, `prepare_ms`, `error`, `error_phase`). Hamilton prints a help banner to stderr whenever it raises, hence the
second redirect. Each runtime's answer is checked against the shape's closed form, and a mismatch aborts the run.
Generated modules are dropped from `sys.modules` after each case.

## What is timed

All times use `time.perf_counter_ns`, in milliseconds.

- `build_ms` (a single sample): module to ready driver (`fnloom.build`; `hamilton.driver.Builder().build()`).
- `first_execute_ms` (a single sample): the first `execute` call, which for fnloom includes compiling the plan.
- `steady_execute_ms`: the median of 7 runs after one warm run. fnloom uses a prepared plan's
  `run`; Hamilton has no prepared plans, so it uses `execute`.
- `prepare_ms` (a single sample): a cold `Driver.prepare` for the same request, on a newly built
  driver so no plan is cached. Always null for Hamilton, which has no prepared plans.
- A runtime that raises records the exception class in `error` and the phase in `error_phase`
  (`build`, `first_execute`, `steady_execute` or `prepare`). Timings of phases before it are kept; the rest are null.
- Argument copies (`[*outputs]`, `dict(inputs)`) are made once, outside the timed regions.

Graph generation is not timed.

## Shapes

- `chain`: `n` nodes in a line, each adding one to the one before; one input, `seed`.
- `fan_in`: one function with `n` parameters, all inputs, summed.
- `layered`: `n` nodes in rows of ten, each reading two nodes of the row above; one input, `seed`.

## Machine

Apple M2 Pro, 16 GB, macOS (Darwin 27.0.0), CPython 3.14.9, `apache-hamilton==1.90.0`.
The numbers below were measured with a release extension (`maturin develop --release`).

## Medians

Milliseconds, Hamilton (H) / fnloom (F). Hamilton has no prepared plans, so prepare is fnloom's only.

| shape | n | build (H / F) | first execute (H / F) | steady execute (H / F) | prepare (F) |
|---|---:|---|---|---|---|
| chain | 10 | 0.982 / 0.658 | 0.144 / 0.184 | 0.053 / 0.007 | 0.184 |
| chain | 100 | 2.835 / 2.053 | 0.484 / 0.440 | 0.396 / 0.032 | 0.363 |
| chain | 1000 | 23.4 / 8.299 | RecursionError / 1.753 | RecursionError / 0.260 | 1.083 |
| chain | 10000 | 228.4 / 70.7 | RecursionError / 9.349 | RecursionError / 2.565 | 4.851 |
| fan_in | 10 | 0.154 / 0.055 | 0.068 / 0.046 | 0.037 / 0.003 | 0.011 |
| fan_in | 100 | 0.339 / 0.188 | 0.345 / 0.194 | 0.278 / 0.035 | 0.069 |
| fan_in | 1000 | 3.259 / 2.817 | 3.297 / 3.412 | 3.612 / 0.571 | 0.788 |
| fan_in | 10000 | 33.1 / 63.6 | 48.5 / 30.6 | 47.4 / 21.2 | 8.262 |
| layered | 10 | 0.307 / 0.097 | 0.067 / 0.039 | 0.037 / 0.004 | 0.012 |
| layered | 100 | 2.059 / 0.890 | 0.433 / 0.140 | 0.401 / 0.034 | 0.073 |
| layered | 1000 | 21.9 / 7.460 | 4.114 / 1.215 | 4.089 / 0.339 | 0.695 |
| layered | 10000 | 224.5 / 127.7 | RecursionError / 12.7 | RecursionError / 3.725 | 7.479 |

## Rows where fnloom is slower than Hamilton

Three rows of this run, all single samples. None repeats: five runs of each case alone, each in a
fresh process, gave fnloom the lower time every time. Treat them as noise.

- chain, n=10, first execute: fnloom 0.184 ms, Hamilton 0.144 ms (1.28x). Repeats: fnloom 0.037 to
  0.049 ms, Hamilton 0.195 to 0.226 ms.
- fan_in, n=1000, first execute: fnloom 3.412 ms, Hamilton 3.297 ms (1.03x). Repeats: fnloom 1.563
  to 2.556 ms, Hamilton 2.994 to 3.271 ms.
- fan_in, n=10000, build: fnloom 63.6 ms, Hamilton 33.1 ms (1.92x). Repeats: fnloom 15.4 to 18.2 ms,
  Hamilton 32.7 to 35.7 ms.

## The `fan_in` fix

`fan_in` used to grow with the square of `n`. In the first measurement, steady execute took 5.1 ms
at n=1000 and 496 ms at n=10000, against Hamilton's 3.5 ms and 51 ms in the same run. The binding
built each keyword argument's name as a new `str`. CPython binds a keyword to a parameter by
identity with the interned parameter name, and when no name matches by identity it compares the
keyword with `==` against every parameter, so each of the `n` keywords cost `n` string
comparisons. The binding now passes interned names
(`tests/test_binding.py::test_keyword_names_reach_the_function_interned`), and the table above
has 0.571 ms and 21.2 ms, against Hamilton's 3.612 ms and 47.4 ms. What remains is CPython's own
identity scan, still `n` pointer comparisons per keyword, which Hamilton's calls pay too.

## Hamilton errors

Hamilton builds these graphs but raises `RecursionError` on the first `execute`, so its
`first_execute_ms` and `steady_execute_ms` are empty and the build time it did measure is kept.
The JSON rows name the phase in `error_phase`.

- chain, n=1000: `RecursionError` in the first execute phase (build finished in 23.4 ms)
- chain, n=10000: `RecursionError` in the first execute phase (build finished in 228.4 ms)
- layered, n=10000: `RecursionError` in the first execute phase (build finished in 224.5 ms)

fnloom completes all 24 rows, and every answer it gives, including on the rows above, is checked
against the shape's closed form.
