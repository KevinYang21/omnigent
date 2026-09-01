"""Shared fixtures for the onboarding test suite."""

from __future__ import annotations

import contextlib

import pytest

from omnigent.onboarding import harness_install


@pytest.fixture(autouse=True)
def _clear_probe_caches() -> None:
    """Isolate the CLI probe caches — in-process and on-disk — between tests.

    ``harness_cli_logged_in`` / ``_harness_cli_version_string`` cache
    verdicts process-wide, and a logged-in verdict is also mirrored to
    ``<data-dir>/cli_login_probe_cache.json`` so a cold host daemon skips the
    probe; a positive left by one test's fake probe would leak into the next
    test's counting assertions.
    """
    harness_install._LOGIN_PROBE_CACHE.clear()
    harness_install._VERSION_PROBE_CACHE.clear()
    harness_install._PROBE_GATES.clear()
    with contextlib.suppress(OSError):
        harness_install._persisted_login_cache_path().unlink()
