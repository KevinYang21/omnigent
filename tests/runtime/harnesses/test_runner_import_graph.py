"""Guard: the harness subprocess entrypoint stays free of the server router.

``omnigent/runtime/_globals.py`` built a default
:class:`~omnigent.runtime.caps.RuntimeCaps` at module scope, and that default's
``routing_settings`` is supplied by ``omnigent.server.smart_routing`` — so every
process that touched ``omnigent.runtime``, harness children included, loaded the
server routing module and the graph behind it just to fill a field it never
read. ``_globals.__getattr__`` defers the construction to first read; these
tests fail if the edge grows back, and pin the behaviour the deferral has to
preserve.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

_FORBIDDEN = "omnigent.server.smart_routing"

_PROBE = (
    "import sys\n"
    "import {module}\n"
    "loaded = sorted(m for m in sys.modules if m.startswith('omnigent.server'))\n"
    "assert {forbidden!r} not in loaded, (\n"
    "    'a server module is back in the client import graph; '\n"
    "    'loaded omnigent.server modules: ' + repr(loaded)\n"
    ")\n"
)


def _assert_no_server_router(module: str) -> None:
    """Import *module* in a fresh interpreter and fail if it loads the router.

    A subprocess, not ``sys.modules`` surgery: the pytest session has long since
    imported the server for other tests, so only a clean interpreter shows what
    a real harness child pays for.
    """
    # Hand the child the same import roots as this process so it resolves
    # ``omnigent`` to the code under test (worktree or installed package).
    child_env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}

    result = subprocess.run(
        [sys.executable, "-c", _PROBE.format(module=module, forbidden=_FORBIDDEN)],
        env=child_env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, (
        f"importing {module} pulled in {_FORBIDDEN} (the lazy default caps "
        f"regressed). stderr:\n{result.stderr}"
    )


def test_harness_runner_import_stays_free_of_smart_routing() -> None:
    """The harness subprocess entrypoint must import no server routing module."""
    _assert_no_server_router("omnigent.runtime.harnesses._runner")


def test_runtime_import_stays_free_of_smart_routing() -> None:
    """The same edge one level up: importing the runtime stays client-only."""
    _assert_no_server_router("omnigent.runtime")


def test_get_caps_still_returns_default_routing_settings() -> None:
    """Deferring the construction must not change what callers read."""
    from omnigent.runtime import get_caps
    from omnigent.runtime.caps import RuntimeCaps
    from omnigent.server.smart_routing import RoutingSettings

    caps = get_caps()
    assert isinstance(caps, RuntimeCaps)
    assert caps.routing_settings == RoutingSettings()


def test_caps_is_readable_as_a_module_attribute() -> None:
    """``from omnigent.runtime._globals import _caps`` is a live call site."""
    from omnigent.runtime import get_caps
    from omnigent.runtime._globals import _caps

    assert _caps is get_caps()


def test_unknown_globals_attribute_still_raises_attribute_error() -> None:
    """The module ``__getattr__`` must not swallow typos."""
    from omnigent.runtime import _globals

    with pytest.raises(AttributeError, match="no_such_global"):
        _globals.no_such_global  # noqa: B018
