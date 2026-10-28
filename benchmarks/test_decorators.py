"""``@extract_fields``, ``@extract_columns`` and ``@config``: a steady run and a build.

An extraction makes one node per field, so a run over a producer of many fields
exercises the generated nodes and their per-run type checks. A config build resolves
each variant before the graph is lowered.
"""

from typing import TYPE_CHECKING

from benches.generate import make_expected, make_request, module_source

import fnloom

if TYPE_CHECKING:
    from pytest_codspeed import BenchmarkFixture

    from benchmarks.conftest import LoadSource

_COLUMNS = 100
_COLUMNS_SOURCE = f"""\
from fnloom import extract_columns


@extract_columns(*[f"c{{i}}" for i in range({_COLUMNS})])
def frame(seed: int) -> dict:
    return {{f"c{{i}}": seed + i for i in range({_COLUMNS})}}


def total(c0: int, c{_COLUMNS - 1}: int) -> int:
    return c0 + c{_COLUMNS - 1}
"""
_VARIANTS = 100


def _config_source() -> str:
    """A node with `_VARIANTS` variants, picked by `region`, and a consumer of it."""
    parts = ["from fnloom import config", ""]
    parts.extend(
        f'@config.when(region="r{i}")\n'
        f"def value__r{i}(seed: int) -> int:\n"
        f"    return seed + {i}\n"
        for i in range(_VARIANTS)
    )
    parts.append("def doubled(value: int) -> int:\n    return value * 2\n")
    return "\n".join(parts)


def test_extract_fields_run(
    benchmark: BenchmarkFixture, load_source: LoadSource
) -> None:
    """Measure a run of 100 consumers of one ``@extract_fields`` producer."""
    outputs, inputs = make_request("extract", 100)
    module = load_source("bench_extract_100", module_source("extract", 100))
    prepared = (
        fnloom.Builder().with_modules(module).build().prepare(outputs, inputs=inputs)
    )
    assert prepared.run(inputs=inputs) == make_expected("extract", 100)
    benchmark(prepared.run, inputs=inputs)


def test_extract_columns_run(
    benchmark: BenchmarkFixture, load_source: LoadSource
) -> None:
    """Measure a run that reads two of an ``@extract_columns`` producer's 100."""
    module = load_source("bench_columns_100", _COLUMNS_SOURCE)
    inputs = {"seed": 7}
    prepared = (
        fnloom.Builder().with_modules(module).build().prepare(["total"], inputs=inputs)
    )
    assert prepared.run(inputs=inputs) == {"total": 7 + 7 + _COLUMNS - 1}
    benchmark(prepared.run, inputs=inputs)


def test_config_build(benchmark: BenchmarkFixture, load_source: LoadSource) -> None:
    """Measure building a node with 100 ``@config.when`` variants, one matching."""
    module = load_source("bench_config_100", _config_source())
    builder = fnloom.Builder().with_modules(module).with_config({"region": "r50"})
    assert builder.build().execute(["doubled"], inputs={"seed": 1}) == {"doubled": 102}
    benchmark(builder.build)
