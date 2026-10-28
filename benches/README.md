# Benchmarks

Wall-clock timings of fnloom against Apache Hamilton 1.90.0 on generated graphs. The results, how to run them and what each
column means are on the [benchmarks page](../docs/benchmarks.md)
([on the docs site](https://kelsayed17.github.io/fnloom/benchmarks/)).

```bash
uv run maturin develop --release
uv run python -m benches.run > bench.jsonl 2> bench.stderr
uv run python -m benches.cache > cache.jsonl   # cached runs, one fresh process per sample
uv run python -m benches.async_ > async.jsonl  # async runs, one fresh process per sample
uv run python -m benches.builds > builds.jsonl  # builds from modules and from functions
```
