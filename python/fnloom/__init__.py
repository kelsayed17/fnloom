"""fnloom: plain Python functions as nodes in a dataflow graph, run by a Rust core."""

from importlib.metadata import version as _version

from fnloom._caching import CacheOutcome
from fnloom._checks import (
    CheckResult,
    ValidationError,
    Validator,
    check_output,
    check_output_custom,
)
from fnloom._config import Condition, config
from fnloom._core import UnfingerprintableError
from fnloom._decorators import (
    CachePolicy,
    Group,
    Source,
    Value,
    cache,
    defaults,
    exclude,
    group,
    parameterize,
    parameterize_sources,
    parameterize_values,
    source,
    tag,
    value,
)
from fnloom._drawing import Drawing, DrawnNode
from fnloom._extract import extract_columns, extract_fields
from fnloom._fingerprint import fingerprint, register_fingerprint
from fnloom._functions import NodeSpec, node
from fnloom._hooks import Hook, LogFailures, NodeEnd, NodeStart, RunEnd, RunStart
from fnloom._steps import Step, pipe_input, pipe_output, step
from fnloom._stores import (
    MISSING,
    CacheStore,
    CacheWarning,
    DiskCache,
    Format,
    MemoryCache,
)
from fnloom._types import ValueCheck, type_matches, value_matches
from fnloom._view import InputView, NodeView, Param
from fnloom.driver import (
    AsyncDriver,
    AsyncPreparedPlan,
    Builder,
    Driver,
    PreparedPlan,
    build,
)

__version__ = _version("fnloom")

__all__ = [
    "MISSING",
    "AsyncDriver",
    "AsyncPreparedPlan",
    "Builder",
    "CacheOutcome",
    "CachePolicy",
    "CacheStore",
    "CacheWarning",
    "CheckResult",
    "Condition",
    "DiskCache",
    "Drawing",
    "DrawnNode",
    "Driver",
    "Format",
    "Group",
    "Hook",
    "InputView",
    "LogFailures",
    "MemoryCache",
    "NodeEnd",
    "NodeSpec",
    "NodeStart",
    "NodeView",
    "Param",
    "PreparedPlan",
    "RunEnd",
    "RunStart",
    "Source",
    "Step",
    "UnfingerprintableError",
    "ValidationError",
    "Validator",
    "Value",
    "ValueCheck",
    "build",
    "cache",
    "check_output",
    "check_output_custom",
    "config",
    "defaults",
    "exclude",
    "extract_columns",
    "extract_fields",
    "fingerprint",
    "group",
    "node",
    "parameterize",
    "parameterize_sources",
    "parameterize_values",
    "pipe_input",
    "pipe_output",
    "register_fingerprint",
    "source",
    "step",
    "tag",
    "type_matches",
    "value",
    "value_matches",
]
