"""Import-graph guards for the runner: tool dispatch stays out of it.

``runner/tool_dispatch.py`` costs ~220ms to import — it reaches
``omnigent.tools`` -> ``omnigent.tools.local`` -> ``omnigent_client`` — and the
runner only needs it once a turn actually dispatches a tool. It used to arrive
unconditionally anyway: ``runner/proxy_mcp_manager.py`` imported one float from
it (``MCP_PROXY_CALL_TIMEOUT_S``) and ``runner/app.py`` imports
``proxy_mcp_manager`` eagerly. That put it on the cold-zygote start that
``POST /v1/sessions`` awaits, and on the whole per-session import cost of any
runner the zygote did not fork. The constants now live in the leaf module
``runner/tool_timeouts.py``.

Every assertion here runs in a fresh subprocess, the same way
``tests/runner/test_identity.py`` guards ``runner.identity`` against FastAPI:
the shared pytest session has long since imported ``tool_dispatch`` for the
dispatch tests themselves, so only a clean interpreter can tell whether the
runner graph pulls it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import omnigent

_OMNIGENT_ROOT = str(Path(omnigent.__file__).resolve().parent)

# Reports the loaded tree plus the modules the dispatch graph would bring in.
# The tree line is checked against this process's own ``omnigent``, so a probe
# that resolved a different checkout fails loudly instead of silently
# "passing".
_PREAMBLE = """
import sys

import omnigent
print("TREE", omnigent.__file__)


def dispatch_loaded():
    # ``omnigent.tools`` itself resolves its submodules lazily (PEP 562), so
    # only the heavy ones below matter.
    roots = (
        "omnigent.runner.tool_dispatch",
        "omnigent.tools.manager",
        "omnigent.tools.local",
        "omnigent_client",
    )
    return sorted(
        m for m in sys.modules if any(m == r or m.startswith(r + ".") for r in roots)
    )
"""


def _probe(body: str, timeout: float = 300.0) -> list[str]:
    """Run *body* in a fresh interpreter that resolves this checkout.

    :param body: Probe source appended to the shared preamble.
    :param timeout: Seconds to allow the probe.
    :returns: The probe's stdout lines.
    """
    # Hand the child the same import roots as this process so it resolves
    # ``omnigent`` to the code under test (worktree or installed package) — a
    # bare ``-c`` subprocess otherwise misses pytest's rootdir sys.path entries.
    child_env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}
    result = subprocess.run(
        [sys.executable, "-c", _PREAMBLE + body],
        env=child_env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    assert result.returncode == 0, f"probe failed:\n{result.stdout}\n{result.stderr}"
    lines = result.stdout.strip().splitlines()
    tree = next(line for line in lines if line.startswith("TREE "))
    assert tree.removeprefix("TREE ").startswith(_OMNIGENT_ROOT), (
        f"probe resolved a different omnigent than the code under test: {tree}"
    )
    return lines


def test_runner_app_import_does_not_pull_tool_dispatch() -> None:
    """``omnigent.runner.app`` must import with no dispatch graph resident."""
    lines = _probe(
        """
import omnigent.runner.app  # noqa: F401
print("DISPATCH", dispatch_loaded())
"""
    )
    assert "DISPATCH []" in lines, (
        f"omnigent.runner.app pulled tool_dispatch into its import graph: {lines}"
    )


def test_zygote_pre_import_excludes_tool_dispatch() -> None:
    """The zygote's boot graph skips dispatch, so forked runners start lighter.

    Covers the wider graph the zygote imports (``_entry`` and the native
    harness modules as well as ``app``): a deferral made only in ``app.py``
    would be silently undone by any of the others importing dispatch eagerly.
    """
    lines = _probe(
        """
from omnigent.runner._zygote import _import_runner_graph

_import_runner_graph()
print("DISPATCH", dispatch_loaded())
"""
    )
    assert "DISPATCH []" in lines, f"the zygote pre-imported tool_dispatch: {lines}"


def test_proxy_mcp_manager_import_does_not_pull_tool_dispatch() -> None:
    """The proxy manager needs one timeout constant, not the dispatch module."""
    lines = _probe(
        """
from omnigent.runner.proxy_mcp_manager import ProxyMcpManager  # noqa: F401
print("DISPATCH", dispatch_loaded())
"""
    )
    assert "DISPATCH []" in lines, f"proxy_mcp_manager pulled tool_dispatch: {lines}"


def test_tool_dispatch_still_re_exports_the_timeout_constants() -> None:
    """Moving the constants must not break callers importing them from before."""
    from omnigent.runner import tool_dispatch, tool_timeouts

    assert tool_dispatch.MCP_PROXY_CALL_TIMEOUT_S == tool_timeouts.MCP_PROXY_CALL_TIMEOUT_S
    assert tool_dispatch.MCP_PROXY_FORWARD_TIMEOUT_S == tool_timeouts.MCP_PROXY_FORWARD_TIMEOUT_S
    assert tool_dispatch._RUNNER_EXECUTION_TIMEOUT_S == tool_timeouts.RUNNER_EXECUTION_TIMEOUT_S
