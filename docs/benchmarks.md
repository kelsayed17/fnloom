# Benchmarks

fnloom against Apache Hamilton 1.90.0 on generated graphs, by wall clock. fnloom is meant to be faster at building a graph
and at running a prepared plan, so a row where it's reliably slower counts as a bug.

## At a glance

<div class="grid cards" markdown>

-   :lucide-repeat:{ .lg .fl-icon } **2.5× to 24× faster repeat runs**

    Running a prepared plan again on the plain graphs, wherever both finish. A 1,000-node layered graph: 4.35 ms
    in Hamilton, 0.21 ms in fnloom. With `@extract_fields`, where Hamilton's cost grows faster than the graph, up to
    998×: 10,000 extracted fields take 6.4 s against 6.4 ms.

-   :lucide-hammer:{ .lg .fl-icon } **Every graph builds faster**

    At least 1.35× faster on every graph, the least on 10 extracted fields, and 41× on 10,000 `@parameterize` rows
    (5.3 s against 0.13 s).

-   :lucide-infinity:{ .lg .fl-icon } **Runs what Hamilton can't**

    Hamilton raises `RecursionError` on 5 of the 20 graphs. fnloom runs all of them, and checks every answer.

-   :lucide-gauge:{ .lg .fl-icon } **Microseconds per run**

    A 10-node chain runs in about 1.9 µs per `PreparedPlan.run`, input type checks included.

</div>

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
| extract | 10 | 0.430 / 0.318 / 0.143 | 0.130 / 0.046 / 0.019 | 0.075 / 0.009 / 0.006 | 0.022 / 0.007 |
| extract | 100 | 2.679 / 1.310 / 1.232 | 1.228 / 0.137 / 0.106 | 1.176 / 0.061 / 0.060 | 0.060 / 0.036 |
| extract | 1,000 | 65.3 / 12.7 / 11.4 | 68.4 / 1.115 / 0.919 | 65.8 / 0.602 / 0.597 | 0.480 / 0.350 |
| extract | 10,000 | 4,520.7 / 208.3 / 179.0 | 6,161.3 / 12.9 / 10.3 | 6,372.9 / 6.385 / 6.304 | 5.770 / 3.424 |

Measured on an Apple M2 Pro with 16 GB, macOS 27.0, CPython 3.14.9, `apache-hamilton==1.90.0`, and a release build
of the extension, on 2026-10-27; the `extract` rows on 2026-10-28.

**`extract`** is one `@extract_fields` producer returning a dict of `n` int fields, each read by its own consumer,
with every consumer requested. fnloom checks each field against its type on every run, with input checks on or off
(Q61), and still runs 8.6 to 998 times faster than Hamilton's `execute`, which checks none. Hamilton's cost grows
faster than `n`: 65.8 ms at 1,000 fields, and 6.4 s at 10,000.

**Steady execution**, the cost of running a prepared plan again, is where fnloom differs most. With checks on, it's
2.5 to 24 times faster than Hamilton's `execute` on the first four shapes wherever both finish, and 8.6 to 998 times
on `extract`, because the plan is compiled once and each run is a loop over its steps in Rust. The low end is
`fan_in` at 10,000 parameters (2.5×), where one Python call with 10,000 keyword arguments is most of the cost for
both.

**Input checks are close to free.** The gap between the two fnloom figures is within noise: steady execution with
checks on and off differs by at most 0.05 ms on every graph. A check against a plain class such as `int` is compiled
once per plan into one `isinstance` call. Only generics, unions and the like take the general path.

### Where Hamilton raises

Hamilton builds the 1,000- and 10,000-node chains, the 10,000-node layered graph and the 1,000- and 10,000-row `rows`
graphs, then raises `RecursionError` on the first `execute`: its graph walk recurses once per level of depth. fnloom
runs all 20 cases, with checks on and off, and every answer it gives is checked against the shape's closed form.

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

### Building from functions

`Builder.with_functions` builds the same graph as `with_modules` from the module's functions given one by one (Q72).
`benches/builds.py` times both, and Hamilton's nearest way, `ad_hoc_utils.create_temporary_module` and a build of it, on
a release build, median of nine builds each, interleaved in one process (ms):

| shape | n | `with_modules` | `with_functions` | Hamilton, temporary module |
| --- | ---: | ---: | ---: | ---: |
| chain | 1,000 | 5.428 | 5.914 | 16.2 |
| rows | 1,000 | 4.614 | 4.651 | 50.9 |

Measured on the machine above, on 2026-10-28. Giving the functions costs under a tenth more than scanning the module:
each function is checked once, when it is given, and then scanned as a module's function is.

### Building with namespaces

`benches/builds.py` also builds the 1,000-node `layered` graph two ways: as one module, and as ten modules of 100 nodes,
each under its own namespace, `ns0` to `ns9`, all reading the one input `seed` (Q74). Same machine and release build,
median of nine builds each, interleaved in one process (ms):

| shape | n | one module | 10 namespaces |
| --- | ---: | ---: | ---: |
| layered | 1,000 | 6.326 | 6.798 |

Measured on 2026-10-28. Namespaces cost under a tenth more: every parameter of a namespaced node reads a node of its
namespace under a dotted name, so the core passes it under the parameter's own keyword, one rename per edge. The
namespace's names are found once, before any node is made, so each parameter is bound in the same pass as without one.

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

## Caching

Cached runs against uncached ones and against Hamilton 1.90.0's `with_cache`, on a 200-node `chain` and a 1,000-node
`layered` graph of trivial nodes, the worst case for a cache: a node costs less to run than to look up. Milliseconds,
cold / warm, medians over seven fresh processes, each the median of three cold runs (a new driver with an empty
store) and the 21 warm runs after them. Uncached, cold is a first run and warm a steady one. fnloom runs a prepared
plan; Hamilton, which has none, runs `execute`. So fnloom's cold and warm times leave out `prepare`, made once before
the timed runs, while Hamilton's include the planning each `execute` does. Hamilton's memory store is its
`InMemoryMetadataStore` with its `InMemoryResultStore`, and its disk store the default `with_cache(path=...)`.

| shape | n | runtime | uncached | memory | disk |
| --- | ---: | --- | --- | --- | --- |
| chain | 200 | fnloom | 0.031 / 0.028 | 0.070 / 0.019 | 42.5 / 0.075 |
| chain | 200 | fnloom, async | 0.128 / 0.117 | 0.492 / 0.027 | 43.9 / 0.075 |
| chain | 200 | Hamilton | 0.564 / 0.506 | 14.8 / 13.8 | 93.4 / 21.8 |
| layered | 1,000 | fnloom | 0.203 / 0.191 | 0.376 / 0.082 | 192.8 / 0.485 |
| layered | 1,000 | fnloom, async | 0.745 / 0.725 | 2.789 / 0.117 | 200.9 / 0.484 |
| layered | 1,000 | Hamilton | 3.612 / 3.541 | 95.3 / 88.0 | 495.8 / 127.3 |

Measured on the machine and build above, on 2026-10-28. A warm fnloom run is 260 to 1,070 times faster than
Hamilton's on the same kind of store, and 7 to 43 times faster than Hamilton's uncached run. A warm fnloom run on
`MemoryCache` is faster than an uncached fnloom run: it loads the outputs and runs nothing. Hamilton's warm runs cost
more than its uncached ones, 25 to 27 times as much in memory, as the caching design found (Q16).

**fnloom, async** is `AsyncDriver` with `sync_nodes="inline"`, timed on the loop; Hamilton's async driver has no row,
since it fails to build with a cache (Q68). Its warm runs load the outputs and run nothing, as the sync driver's do.
Its cold runs with `MemoryCache` cost 3.7 to 3.8 times its uncached ones, against 1.9 to 2.3 times on the sync
driver. Two things make that up. The async walk is Python, so it looks up and stores each node through Python calls
instead of the native walk a plain `MemoryCache` gets on `Driver`. And a cached async plan builds its `Schedule`
lazily, on its first run, so the cold figure counts it, where the uncached figure leaves it to `prepare`. With
`DiskCache`, the files cost the same on either driver.

The targets of the caching design (§8), with fnloom's figures:

| target | chain 200 | layered 1,000 | verdict |
| --- | --- | --- | --- |
| A memory hit costs at most 1 µs of overhead per node (warm run / n) | 0.10 µs | 0.08 µs | met |
| A warm run costs at most about twice an uncached one (warm / uncached steady) | 0.67× | 0.43× | met |
| A cold run costs at most 0.25 µs of overhead per node, with `MemoryCache` ((cold − uncached first) / n) | 0.19 µs (2.24×) | 0.17 µs (1.86×) | met |
| A requested output that hits loads one entry | 1 entry | 10 entries, one per output | met |

**The cold target is now per node.** It was "at most 1.5 times an uncached run", and a cold run cost 5.99× and 5.45×
on the same day before [#10](https://github.com/kelsayed17/fnloom/issues/10). A cold run now pays about 0.19 µs per
node over an uncached one, where an uncached trivial node costs about 0.16 µs. Most of that is the key, one BLAKE3
compression per 64 bytes read, about 75 ns, which alone is half of a trivial node, so no ratio near 1.5 is reachable
on trivial nodes without a weaker key. The rest is a lookup and a store in the native memory store. The cost is per
node, not per byte, so the design now bounds it per node: at most 0.25 µs.

What closed most of the gap:

- **The walk and the memory store are native.** A plain `MemoryCache` keeps its entries in the extension. The walk
  back from the outputs looks keys up there, and the run stores each result as its node succeeds, with no call into
  Python per node. A subclass of `MemoryCache`, or any other store, takes the Python path.
- **A key is one BLAKE3 block where it can be.** A key is BLAKE3 keyed by the node's static part, over the 32-byte
  versions of what the node reads. The parameter names and `value()` constants that used to be framed into every key
  are hashed once, at build, into the static part, so a node reading one value hashes 32 bytes.

## Async

`AsyncDriver` against Hamilton 1.90.0's `async_driver.Builder`, whose result is a dict, on the `chain` and `layered`
graphs of trivial nodes, once with every node an `async def` and once with every node a plain function. fnloom runs
in both `sync_nodes` modes. Milliseconds, first / steady: a first run on a new driver, then the median of the 21 runs
after it, each row the median over five fresh processes. fnloom runs a prepared plan, so its first run leaves out
`prepare` and the schedule it builds from the plan's steps, both made once before the timed runs. Hamilton, which has
no prepared plans, runs `execute`, planning included.

| shape | n | nodes | Hamilton | fnloom, `thread` | fnloom, `inline` |
| --- | ---: | --- | --- | --- | --- |
| chain | 100 | async | 5.584 / 5.160 | 0.102 / 0.078 | 0.106 / 0.077 |
| chain | 100 | sync | 5.606 / 5.145 | 3.897 / 3.406 | 0.083 / 0.057 |
| chain | 1,000 | async | 54.4 / 53.1 | 0.828 / 0.785 | 0.837 / 0.783 |
| chain | 1,000 | sync | 54.5 / 52.9 | 34.6 / 33.6 | 0.650 / 0.585 |
| layered | 100 | async | 2.246 / 2.103 | 0.114 / 0.088 | 0.114 / 0.088 |
| layered | 100 | sync | 2.229 / 1.977 | 3.114 / 2.343 | 0.098 / 0.068 |
| layered | 1,000 | async | 22.0 / 21.5 | 0.970 / 0.911 | 0.960 / 0.907 |
| layered | 1,000 | sync | 22.6 / 21.8 | 24.1 / 23.0 | 0.754 / 0.700 |

Measured on the machine above, with CPython 3.14.8 and a release build, on 2026-10-28. Wherever both run nodes on the
loop, which means every `async` row and the `inline` column, fnloom is 20 to 84 times faster than Hamilton on a first
run and 24 to 90 times faster on a steady one. The smallest gain is on the 100-node layered graph of `async def`
nodes, 19.7× on the first run in either column. The generated `async def` nodes never suspend. fnloom starts each
node's task eagerly, so a node that doesn't suspend finishes with no turn of the loop, while Hamilton schedules a task
for every node. Without a custom task factory on the loop, fnloom makes each task directly instead of through
`loop.create_task`, which took about a tenth off the 1,000-node `async def` rows (steady, 0.882 to 0.785 ms on the
chain and 1.013 to 0.911 ms on the layered graph, against the previous run). A node that awaits real I/O pays for its
suspensions on either.

**`thread` is slower than Hamilton on the layered graph of sync nodes, by design.** Each sync node makes a round trip
to a worker thread: `loop.run_in_executor` queues the call to the loop's default executor, a worker thread wakes to
run it, and the loop wakes again for the result. That costs about 34 µs per node on the chain, where the nodes run
one after another, and about 23 µs on the layered graph, where ten at a time are in flight. A trivial node costs well
under 1 µs, so the round trip is the whole cost. On the chain, `thread` is still 1.4 to 1.6 times faster than
Hamilton. These are the rows where it's slower:

| row | Hamilton, first / steady | fnloom `thread`, first / steady | slower by, first / steady | fnloom `inline` |
| --- | --- | --- | --- | --- |
| layered, 100, sync | 2.229 / 1.977 | 3.114 / 2.343 | 40% / 19% | 0.098 / 0.068 |
| layered, 1,000, sync | 22.6 / 21.8 | 24.1 / 23.0 | 7% / 6% | 0.754 / 0.700 |

Hamilton runs sync nodes on the loop and stops it while each one runs. The thread is what keeps the loop free (Q65),
so the `thread` column isn't a like-for-like comparison. For a graph of cheap sync nodes, `sync_nodes="inline"` is
the remedy, and it's the fastest column. A dedicated executor and dropping the per-call context copy were tried. Each
saved 1 to 2% of the round trip, within noise, so neither was kept.

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
uv run python -m benches.run extract > extract.jsonl 2> bench.stderr   # only the shapes named
```

It prints 60 JSON lines to stdout (`shape`, `n`, `runtime`, `input_checks`, `build_ms`,
`first_execute_ms`, `steady_execute_ms`, `prepare_ms`, `error`, `error_phase`).
Hamilton prints a help banner to stderr whenever it raises, hence the second redirect.
Each runtime's answer is checked against the shape's closed form, and a
mismatch aborts the run. Generated modules are dropped from `sys.modules` after each case.

The cached runs have their own script, which starts a fresh process per sample:

```bash
uv run python -m benches.cache > cache.jsonl
```

It prints 18 JSON lines (`shape`, `n`, `runtime`, `store`, `cold_ms`, `warm_ms`, `warm_loads`, `processes`, `error`),
one per shape, runtime (`fnloom`, `fnloom-async` or `hamilton`) and store (`none`, `memory` or `disk`). `warm_loads` is
how many entries a warm `fnloom` run loads, counted on a separate, untimed driver. The graphs are written to files and
imported, since Hamilton's cache versions a function by its source, and every answer is checked against the shape's
closed form.

The async runs have their own script too, also a fresh process per sample:

```bash
uv run python -m benches.async_ > async.jsonl
```

It prints 24 JSON lines (`shape`, `n`, `nodes`, `runtime`, `first_ms`, `run_ms`, `processes`, `error`), one per
shape, kind of node (`async` or `sync`) and runtime (`hamilton`, `fnloom-thread` or `fnloom-inline`). Every answer
is checked against the shape's closed form.

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
- `extract`: one `@extract_fields` producer returning a dict of `n` int fields, each read by its own consumer.

## The instruction-count gate

Wall-clock time is too noisy on shared CI runners to gate on, so CI gates the Rust core instead: it counts the
instructions `Graph::build`, `Graph::prepare` and `Plan::run` execute under callgrind, on chain, fan-in and layered
graphs of 1,000 and 10,000 nodes (`crates/fnloom-core/benches/core.rs`). A rise of more than 2% over the committed
`crates/fnloom-core/benches/baseline.json` fails the `instruction-counts` job, which runs `scripts/bench_gate.py`.

Counts depend on the CPU architecture, the toolchain and the C library, so the baseline is generated in the job's own
environment: the `rust:1.99.1` image on aarch64 Linux. A change that costs instructions on purpose regenerates it with
`--write-baseline` and says why in its commit message.
