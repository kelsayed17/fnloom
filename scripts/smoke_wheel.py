"""Smoke-tests an installed fnloom wheel: it imports, and its versions agree.

The release workflow (`.github/workflows/release.yml`) runs this against each wheel it
builds, installed into a clean environment and run with `python -I`, so `fnloom` comes
from the wheel and not from the source tree. It checks that the wheel's metadata
version, `fnloom.__version__` and the compiled core's `core_version()` are one version,
and that on a free-threaded interpreter importing fnloom leaves the GIL off.

Exit status: 0 when every check passes, 1 with a message otherwise.
"""

import sys
import sysconfig
from importlib.metadata import version

import fnloom
from fnloom import _core


def main() -> None:
    """Runs the checks and exits non-zero at the first that fails."""
    core = _core.core_version()
    metadata = version("fnloom")
    if metadata != core:
        sys.exit(f"the wheel's metadata is {metadata!r}, core_version() {core!r}")
    # `__version__` reads the same metadata; the default covers a tree without it.
    exported = getattr(fnloom, "__version__", core)
    if exported != core:
        sys.exit(f"fnloom.__version__ is {exported!r}, but core_version() is {core!r}")
    free_threaded = bool(sysconfig.get_config_var("Py_GIL_DISABLED"))
    if free_threaded and sys._is_gil_enabled():  # noqa: SLF001  # the only GIL probe
        sys.exit("importing fnloom turned the GIL back on")
    build = "free-threaded" if free_threaded else "with the GIL"
    python = sys.version.split()[0]
    sys.stdout.write(f"fnloom {core} imports on Python {python} ({build})\n")


if __name__ == "__main__":
    main()
