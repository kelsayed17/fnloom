"""Smoke test: the native extension builds, imports and answers."""

import fnloom
import fnloom._core as core


def test_core_version_is_a_string() -> None:
    """The extension reports a non-empty version string."""
    assert isinstance(core.core_version(), str)
    assert core.core_version()


def test_package_and_core_versions_agree() -> None:
    """The installed package's version is the native core's, read from one manifest."""
    assert fnloom.__version__ == core.core_version()
