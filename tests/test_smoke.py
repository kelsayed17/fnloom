"""Smoke test: the native extension builds, imports and answers."""

import fnloom._core as core


def test_core_version_is_a_string() -> None:
    """The extension reports a non-empty version string."""
    assert isinstance(core.core_version(), str)
    assert core.core_version()
