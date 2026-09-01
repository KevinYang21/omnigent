"""Timeout constants shared by runner tool dispatch and the MCP proxy.

A dependency-free leaf module so a caller that only needs one of these
numbers does not pull ``omnigent.runner.tool_dispatch`` — and the
``omnigent.tools`` / ``omnigent_client`` graph behind it — into its import
graph. ``runner/proxy_mcp_manager.py`` is imported eagerly by
``runner/app.py``, so that cost would land on every runner start and on the
runner zygote's pre-import set.

``tool_dispatch`` re-exports these names, so existing imports from there
keep resolving.
"""

from __future__ import annotations

# Wall-clock budget the runner allows a single agent execution.
RUNNER_EXECUTION_TIMEOUT_S = 7200.0

# Read timeouts for the two MCP-proxy hops that carry a tool call back to the
# runner (runner → Omnigent server → runner). ``sys_os_shell`` accepts
# caller-provided timeouts, so these must sit above the runner's execution
# timeout rather than only above the 120-second shell default. Keep the outer
# hop larger so the AP→runner leg fails first with the more specific error when
# the proxy wedges.
MCP_PROXY_FORWARD_TIMEOUT_S = RUNNER_EXECUTION_TIMEOUT_S + 30.0
MCP_PROXY_CALL_TIMEOUT_S = RUNNER_EXECUTION_TIMEOUT_S + 60.0
