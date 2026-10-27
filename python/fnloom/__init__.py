"""fnloom: plain Python functions as nodes in a dataflow graph, run by a Rust core."""

from importlib.metadata import version as _version

from fnloom._decorators import Source, Value, parameterize, source, tag, value
from fnloom._view import NodeView, Param
from fnloom.driver import Builder, Driver, PreparedPlan, build

__version__ = _version("fnloom")

__all__ = [
    "Builder",
    "Driver",
    "NodeView",
    "Param",
    "PreparedPlan",
    "Source",
    "Value",
    "build",
    "parameterize",
    "source",
    "tag",
    "value",
]
