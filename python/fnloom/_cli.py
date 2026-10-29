"""The `fnloom` command: draw, export, validate and diff a graph (Q97, Q98).

Modules are dotted names or `.py` paths; config, inputs, overrides and outputs
come from flags or a context file. stdout carries only the result; every error is
a line on stderr, with exit status 1, or 2 for a usage error.
"""

import argparse
import importlib
import importlib.util
import json
import os
import sys
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn, cast

import fnloom
from fnloom import _export

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from types import ModuleType

# The formats `draw` writes as text itself; any other is an image by Graphviz.
_TEXT_FORMATS = {"dot": "dot", "mermaid": "mmd"}

# What a context file may hold, and the type of each.
_CONTEXT_KEYS = {"config": dict, "inputs": dict, "overrides": dict, "outputs": list}


class _UsageError(Exception):
    """A request the command line can't make: reported as argparse reports one."""


def main(argv: Sequence[str] | None = None) -> int:
    """Runs the command `argv` names (default `sys.argv[1:]`); its exit status.

    Never raises `SystemExit`: a usage error, `--help` included, is returned as
    argparse's status, so an in-process caller always gets an `int`.
    """
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exited:
        return exited.code if isinstance(exited.code, int) else 2
    command = cast("Callable[[argparse.Namespace], int]", args.command)
    try:
        return command(args)
    except _UsageError as error:
        parser.print_usage(sys.stderr)
        sys.stderr.write(f"{parser.prog}: error: {error}\n")
        return 2
    except BrokenPipeError:
        # The reader went away (`fnloom draw ... | head`): nothing to report.
        _quiet_stdout()
        return 1
    except Exception as error:  # noqa: BLE001  # every failure: one report, status 1
        _report(error)
        return 1


def run() -> NoReturn:
    """The console script: `main`, its status the process's; 130 on Ctrl-C."""
    try:
        status = main()
        # Flushed here, so a reader gone before the last write is caught below.
        sys.stdout.flush()
    except BrokenPipeError:
        _quiet_stdout()
        status = 1
    except KeyboardInterrupt:
        status = 130
    raise SystemExit(status)


def _quiet_stdout() -> None:
    """Points stdout at the null device: exiting then flushes into no closed pipe."""
    null = os.open(os.devnull, os.O_WRONLY)
    os.dup2(null, sys.stdout.fileno())
    os.close(null)


def _report(error: BaseException) -> None:
    sys.stderr.write(f"fnloom: {type(error).__name__}: {error}\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fnloom",
        description="Draw, export, validate and compare fnloom graphs.",
    )
    commands = parser.add_subparsers(required=True, metavar="COMMAND")

    draw = commands.add_parser(
        "draw", help="draw the graph, part of it, or a run as DOT, Mermaid or an image"
    )
    _graph_arguments(draw)
    scope = draw.add_mutually_exclusive_group()
    scope.add_argument(
        "--upstream", nargs="+", metavar="NODE", help="the nodes and what they read"
    )
    scope.add_argument(
        "--downstream", nargs="+", metavar="NODE", help="the nodes and what reads them"
    )
    scope.add_argument(
        "--path", nargs=2, metavar=("FROM", "TO"), help="the nodes on a path"
    )
    scope.add_argument(
        "--execution", action="store_true", help="the run --outputs would make"
    )
    draw.add_argument(
        "--no-context",
        action="store_true",
        help="with --path, leave out what the path's nodes read besides it",
    )
    draw.add_argument(
        "--format",
        type=str.lower,
        help="dot or mermaid, or an image format Graphviz makes: svg, png, pdf; "
        "by default dot to stdout, or what -o's suffix names",
    )
    draw.add_argument("--orient", default="LR", choices=("LR", "TB", "RL", "BT"))
    draw.add_argument("--no-legend", action="store_true", help="leave out the legend")
    draw.add_argument(
        "--no-validate",
        action="store_true",
        help="with --execution, draw without checking inputs",
    )
    draw.add_argument("-o", "--output", type=Path, help="write here, not to stdout")
    draw.set_defaults(command=_draw)

    export = commands.add_parser(
        "export", help="the graph, or the run --outputs would make, as JSON"
    )
    _graph_arguments(export)
    export.add_argument(
        "--no-validate", action="store_true", help="export without checking inputs"
    )
    export.add_argument("-o", "--output", type=Path, help="write here, not to stdout")
    export.set_defaults(command=_export_command)

    validate = commands.add_parser(
        "validate", help="every problem a run of --outputs would raise"
    )
    _graph_arguments(validate)
    validate.set_defaults(command=_validate)

    diff = commands.add_parser("diff", help="what changed between two exports")
    diff.add_argument("old", type=Path, help="an export, from `fnloom export`")
    diff.add_argument("new", type=Path, help="another export")
    diff.set_defaults(command=_diff)
    return parser


def _graph_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "modules",
        nargs="+",
        metavar="MODULE",
        help="a dotted module name or a .py path; MODULE@NAMESPACE names its nodes "
        "NAMESPACE.<name>",
    )
    parser.add_argument(
        "--config",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="a config entry; VALUE is read as JSON, or else as a plain string, "
        "so n=7 is the int 7 and n=007 (not JSON) the string '007'; quote a "
        "string that looks like JSON: n='\"7\"'",
    )
    parser.add_argument(
        "--input", action="append", default=[], metavar="KEY=VALUE", help="an input"
    )
    parser.add_argument(
        "--override",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="an override",
    )
    parser.add_argument(
        "--outputs",
        action="append",
        default=[],
        metavar="NAMES",
        help="the outputs of a run, comma-separated",
    )
    parser.add_argument(
        "--context",
        type=Path,
        metavar="FILE",
        help="a .json or .toml file with config, inputs, overrides and outputs; "
        "a flag wins over its entry",
    )


class _Request:
    """A graph and a request, from the command line: the driver and the run."""

    def __init__(self, args: argparse.Namespace) -> None:
        context = _context(args.context) if args.context is not None else {}
        self.config: dict[str, object] = {
            **context.get("config", {}),
            **_pairs(args.config, "--config"),
        }
        self.inputs: dict[str, object] = {
            **context.get("inputs", {}),
            **_pairs(args.input, "--input"),
        }
        self.overrides: dict[str, object] = {
            **context.get("overrides", {}),
            **_pairs(args.override, "--override"),
        }
        outputs = [n.strip() for item in args.outputs for n in item.split(",")]
        outputs = [n for n in outputs if n]
        self.outputs: list[str] = outputs or list(context.get("outputs", []))
        builder = fnloom.Builder()
        for spec in args.modules:
            module, namespace = _module(spec)
            builder = builder.with_modules(module, namespace=namespace)
        self.driver = builder.with_config(self.config).build()

    def run(self) -> dict[str, Any]:
        """The request as keyword arguments of `draw_execution` and the like."""
        if not self.outputs:
            msg = "give the outputs of a run: --outputs, or outputs in --context"
            raise _UsageError(msg)
        return {"inputs": self.inputs, "overrides": self.overrides}


def _draw(args: argparse.Namespace) -> int:
    if args.no_context and not args.path:
        msg = "--no-context goes with --path"
        raise _UsageError(msg)
    if args.no_validate and not args.execution:
        msg = "--no-validate goes with --execution"
        raise _UsageError(msg)
    request = _Request(args)
    driver = request.driver
    if args.upstream:
        drawing = driver.draw_upstream_of(*args.upstream)
    elif args.downstream:
        drawing = driver.draw_downstream_of(*args.downstream)
    elif args.path:
        drawing = driver.draw_path_between(*args.path, context=not args.no_context)
    elif args.execution:
        drawing = driver.draw_execution(
            request.outputs, **request.run(), validate=not args.no_validate
        )
    else:
        drawing = driver.draw()
    if args.output is not None:
        # No --format: the file's suffix says, as `render` reads it.
        chosen = None if args.format is None else _TEXT_FORMATS.get(args.format)
        drawing.render(
            args.output,
            format=chosen or args.format,
            orient=args.orient,
            legend=not args.no_legend,
        )
        return 0
    text_format = _TEXT_FORMATS.get(args.format or "dot")
    if text_format is None:
        msg = f"--format {args.format} is an image: give a file with -o"
        raise _UsageError(msg)
    if text_format == "mmd":
        sys.stdout.write(drawing.to_mermaid(orient=args.orient))
    else:
        sys.stdout.write(drawing.to_dot(orient=args.orient, legend=not args.no_legend))
    return 0


def _export_command(args: argparse.Namespace) -> int:
    request = _Request(args)
    if request.outputs:
        document = request.driver.export(
            request.outputs, **request.run(), validate=not args.no_validate
        )
    else:
        document = request.driver.export()
    _write(json.dumps(document, indent=2, sort_keys=True) + "\n", args.output)
    return 0


def _validate(args: argparse.Namespace) -> int:
    request = _Request(args)
    problems = request.driver.validate(request.outputs, **request.run())
    for line in problems:
        sys.stdout.write(f"{line}\n")
    return 1 if problems else 0


def _diff(args: argparse.Namespace) -> int:
    documents: list[dict[str, Any]] = []
    for path in (args.old, args.new):
        try:
            documents.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as error:  # unreadable, or not JSON
            _report(error)
            return 2
    try:
        found = _export.diff(*documents)
    except ValueError as error:
        _report(error)
        return 2
    sys.stdout.write(json.dumps(found, indent=2, sort_keys=True) + "\n")
    for warning in found["warnings"]:
        sys.stderr.write(f"fnloom: warning: {warning}\n")
    changed = found["added"] or found["removed"] or found["changed"]
    return 1 if changed or found["unversioned"] else 0


def _write(text: str, output: Path | None) -> None:
    if output is None:
        sys.stdout.write(text)
    else:
        output.write_text(text, encoding="utf-8")


def _module(spec: str) -> tuple[ModuleType, str | None]:
    """The module `spec` names, imported, and the namespace after an `@`.

    `@NS` is read as a namespace only when `NS` is a dotted name, so a folder
    named `a@b` is still a folder. A `.py` path is loaded under its stem with its
    folder first on `sys.path`, so a sibling import works (Q97); a module of that
    name already imported from another file is a usage error rather than a
    silent swap. A dotted name is imported with the current folder on
    `sys.path`, as `python -m` has it; a package's module needs its dotted name,
    for its relative imports.
    """
    target, namespace = _split_namespace(spec)
    if not target.lower().endswith(".py"):
        folder = str(Path.cwd())
        if folder not in sys.path:
            sys.path.insert(0, folder)
        return importlib.import_module(target), namespace
    return _load_file(target), namespace


def _split_namespace(spec: str) -> tuple[str, str | None]:
    """`spec` and no namespace, or what precedes `@NS` and `NS`, a dotted name."""
    head, at, tail = spec.rpartition("@")
    if not at or tail.lower().endswith(".py") or Path(spec).is_file():
        # No `@`, or one inside a file's own name, such as `my@flow.py`.
        return spec, None
    if not tail:
        msg = f"no namespace after '@' in {spec!r}"
        raise _UsageError(msg)
    if all(part.isidentifier() for part in tail.split(".")):
        return head, tail
    return spec, None


def _load_file(target: str) -> ModuleType:
    """The `.py` file `target`, loaded under its stem, its folder on `sys.path`."""
    path = Path(target).resolve()
    if not path.is_file():
        msg = f"no such module file: {target}"
        raise _UsageError(msg)
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))
    name = path.stem
    if name in sys.stdlib_module_names:
        msg = (
            f"{target}: {name!r} is a standard library module's name, which this "
            "file would shadow; rename the file"
        )
        raise _UsageError(msg)
    loaded = sys.modules.get(name)
    if loaded is not None:
        where = getattr(loaded, "__file__", None)
        if where is not None and Path(where).resolve() == path:
            return loaded
        source = f" from {where}" if where else ""
        msg = (
            f"{target}: a module named {name!r} is already imported{source}; "
            "rename the file, or give its dotted name"
        )
        raise _UsageError(msg)
    found = importlib.util.spec_from_file_location(name, path)
    if found is None or found.loader is None:
        msg = f"cannot load {target} as a module"
        raise _UsageError(msg)
    module = importlib.util.module_from_spec(found)
    sys.modules[name] = module
    try:
        found.loader.exec_module(module)
    except BaseException:
        del sys.modules[name]
        raise
    return module


def _pairs(items: Sequence[str], flag: str) -> dict[str, object]:
    """`KEY=VALUE` items as a dict; each `VALUE` JSON, or else a plain string."""
    found: dict[str, object] = {}
    for item in items:
        key, equals, text = item.partition("=")
        if not equals or not key:
            msg = f"{flag} takes KEY=VALUE, got {item!r}"
            raise _UsageError(msg)
        try:
            found[key] = json.loads(text)
        except ValueError:
            found[key] = text
    return found


def _context(path: Path) -> dict[str, Any]:
    """A context file's tables, checked: only the four keys, each of its type."""
    try:
        raw = path.read_bytes()
    except OSError as error:
        msg = f"cannot read --context {path}: {error.strerror}"
        raise _UsageError(msg) from None
    try:
        if path.suffix.lower() == ".toml":
            loaded = tomllib.loads(raw.decode())
        elif path.suffix.lower() == ".json":
            loaded = json.loads(raw)
        else:
            msg = f"--context must be a .json or .toml file, got {path.name}"
            raise _UsageError(msg)
    except ValueError as error:
        msg = f"--context {path}: {error}"
        raise _UsageError(msg) from None
    if not isinstance(loaded, dict):
        msg = f"--context {path} must hold a table, not {type(loaded).__name__}"
        raise _UsageError(msg)
    if unknown := sorted(set(loaded) - _CONTEXT_KEYS.keys()):
        msg = (
            f"--context {path}: unknown keys {', '.join(unknown)}; "
            f"expected {', '.join(_CONTEXT_KEYS)}"
        )
        raise _UsageError(msg)
    for key, kind in _CONTEXT_KEYS.items():
        if key in loaded and not isinstance(loaded[key], kind):
            name = "a table" if kind is dict else "a list"
            msg = f"--context {path}: {key} must be {name}"
            raise _UsageError(msg)
    if not all(isinstance(name, str) for name in loaded.get("outputs", [])):
        msg = f"--context {path}: outputs must be a list of names"
        raise _UsageError(msg)
    return loaded
