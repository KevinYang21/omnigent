"""E2E regression tests: a native sub-agent's terminal status must not be a
guess made at interrupt-delivery time.

User journey reproduced here, through the runner's real HTTP boundary — the
exact ``POST /v1/sessions/{id}/events`` path the web UI's Stop button, the
hook-deny sites in ``routes_hooks.py``, and the native forwarders all use:

1. an orchestrator dispatches a native CLI sub-agent (cursor-native here);
2. the user declines a tool call (or clicks Stop) while the sub-agent is
   mid-task — the server posts ``{"type": "interrupt"}`` to the child;
3. the sub-agent SURVIVES the Escape (it is only a keystroke into its tmux
   pane; nothing confirms the agent stopped) and finishes its work;
4. the forwarder reports the finished turn — ``external_session_status: idle``
   with the genuine result;
5. the parent orchestrator is told the sub-agent was ``cancelled`` and the
   genuine result is silently discarded.

Nothing in the interrupt path is stubbed: a REAL tmux pane is advertised in
the child's cursor-native bridge dir, so ``inject_interrupt`` delivers a real
``Escape`` via ``tmux send-keys``, and the pane's process provably keeps
running (the interrupt never verifies the agent stopped — the defect's
premise).

Both tests assert the *correct* post-fix behavior, so they FAIL on the buggy
build (the reproduction) and PASS once the interrupt path stops delivering an
optimistic terminal ``cancelled`` before the sub-agent's outcome is known
:

* ``test_interrupt_must_not_deliver_terminal_cancelled_before_outcome_known``
  — the interrupt only *requests* a stop; declaring the dispatch terminally
  ``cancelled`` at Escape-delivery time is a guess
  (``NativeInterruptRunner._uniform_interrupt`` ->
  ``_wake_parent_after_native_interrupt``).
* ``test_survived_interrupt_delivers_genuine_result_to_parent`` — when the
  agent survives and finishes, its genuine result must reach the parent; today
  the premature ``cancelled`` was already drained-delivered and a delivered
  status cannot be corrected, so the result is dropped.

The third facet — the terminal ``idle`` edge itself conflating "finished" with
"stopped early" (an aborted turn delivered as ``completed``) — cannot be
pinned at this boundary until the disambiguated edge vocabulary exists: with
today's contract both outcomes arrive as the same ``idle`` event, so any
assertion would hard-code a fix design the issue deliberately leaves open.
Its guard must land with the forwarder-side fix.

Run::

    .venv/bin/python -m pytest tests/e2e/test_native_subagent_interrupt_not_terminal.py -v
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent import cursor_native_bridge
from omnigent.runner import app as runner_app
from omnigent.runner import create_runner_app
from omnigent.spec.types import AgentSpec, ExecutorSpec
from tests.runner.conftest import (
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
)
from tests.runner.helpers import NullServerClient

PARENT_SESSION_ID = "conv_parent_orchestrator"
CHILD_SESSION_ID = "conv_child_citation_checker"
GENUINE_RESULT = "VERDICT: 3 citations, all check out."
# A line the pane's resident "agent" prints so liveness is observable.
PANE_BANNER = "[cursor-agent] Checking citations 1/3..."


@pytest.fixture(autouse=True)
def _require_tmux() -> None:
    """The real-Escape delivery needs the tmux binary the product itself uses."""
    if shutil.which("tmux") is None:
        pytest.skip("tmux not installed; the real interrupt keystroke cannot be delivered")


@pytest.fixture(autouse=True)
def _clean_subagent_registry() -> Iterator[None]:
    """Snapshot and restore the process-wide sub-agent / inbox maps.

    The sub-agent work registry and inbox queues live in module-level dicts on
    ``omnigent.runner.app`` that otherwise leak across tests. Clear them before
    the test and restore the originals after.
    """
    saved = (
        dict(runner_app._subagent_work_by_child),
        {k: set(v) for k, v in runner_app._subagent_work_by_parent.items()},
        dict(runner_app._session_inboxes_ref),
        set(runner_app._drained_delivered_subagent_children),
    )
    runner_app._subagent_work_by_child.clear()
    runner_app._subagent_work_by_parent.clear()
    runner_app._session_inboxes_ref.clear()
    runner_app._drained_delivered_subagent_children.clear()
    try:
        yield
    finally:
        runner_app._subagent_work_by_child.clear()
        runner_app._subagent_work_by_child.update(saved[0])
        runner_app._subagent_work_by_parent.clear()
        runner_app._subagent_work_by_parent.update(saved[1])
        runner_app._session_inboxes_ref.clear()
        runner_app._session_inboxes_ref.update(saved[2])
        runner_app._drained_delivered_subagent_children.clear()
        runner_app._drained_delivered_subagent_children.update(saved[3])


@pytest.fixture
def _isolated_bridge_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Path:
    """Point the cursor-native bridge root at a private temp dir.

    The interrupt handler resolves ``bridge_dir_for_session_id`` through the
    module global, so both the test's ``write_tmux_target`` and the product's
    ``inject_interrupt`` see the same isolated directory.
    """
    root = tmp_path / "cursor-native-bridge"
    monkeypatch.setattr(cursor_native_bridge, "_BRIDGE_ROOT", root)
    return root


@pytest.fixture
def _live_tmux_pane() -> Iterator[tuple[Path, str]]:
    """A real tmux pane whose resident process survives an Escape keystroke.

    Models the cursor-agent TUI mid-task: ``inject_interrupt`` sends Escape
    into this pane exactly as in production, and — as in the reported bug —
    nothing about that delivery confirms the agent stopped. Yields
    ``(socket_path, tmux_target)``.
    """
    sock = Path(tempfile.mkdtemp(prefix="repro-interrupt-tmux-")) / "tmux.sock"
    target = "repro-interrupt"
    subprocess.run(
        [
            "tmux",
            "-S",
            str(sock),
            "new-session",
            "-d",
            "-s",
            target,
            f"sh -c 'echo \"{PANE_BANNER}\"; sleep 600'",
        ],
        check=True,
        timeout=15,
    )
    try:
        yield sock, target
    finally:
        subprocess.run(["tmux", "-S", str(sock), "kill-server"], check=False, timeout=15)


def _pane_contents(sock: Path, target: str) -> str:
    """Capture the pane text (liveness evidence for the surviving agent)."""
    proc = subprocess.run(
        ["tmux", "-S", str(sock), "capture-pane", "-p", "-t", target],
        capture_output=True,
        text=True,
        timeout=15,
    )
    return proc.stdout if proc.returncode == 0 else ""


@asynccontextmanager
async def _dispatched_native_subagent(
    monkeypatch: pytest.MonkeyPatch,
    bridge_root: Path,
    pane: tuple[Path, str],
) -> AsyncIterator[tuple[httpx.AsyncClient, asyncio.Queue[Any]]]:
    """Stand up the runner app with a dispatched cursor-native sub-agent.

    Creates parent + child sessions over the real HTTP API, registers the
    dispatch the way ``sys_session_send`` does, and advertises the live tmux
    pane in the child's bridge dir the way the runner's cursor terminal
    launch does (``write_tmux_target``). Yields the ASGI client and the
    parent's inbox queue.
    """
    del bridge_root  # patched by the fixture; bridge dirs derive from it

    async def _no_op_auto_create(*args: Any, **kwargs: Any) -> None:
        # Stands in for the cursor terminal auto-create path, which needs a
        # real terminal registry + cursor-agent binary; the pane under test is
        # advertised explicitly below, exactly as the launch path would.
        del args, kwargs

    monkeypatch.setattr(
        "omnigent.runner.native.orchestration._auto_create_cursor_terminal",
        _no_op_auto_create,
    )

    spec = AgentSpec(
        spec_version=1,
        name="citation-checker",
        executor=ExecutorSpec(type="omnigent", config={"harness": "cursor-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    async with _runner_client(app) as client:
        for sid in (PARENT_SESSION_ID, CHILD_SESSION_ID):
            resp = await client.post(
                "/v1/sessions", json={"session_id": sid, "agent_id": "ag"}
            )
            assert resp.status_code == 201, resp.text
        runner_app.register_subagent_work(
            parent_session_id=PARENT_SESSION_ID,
            child_session_id=CHILD_SESSION_ID,
            agent="citation-checker",
            title="verify citations",
        )
        inbox: asyncio.Queue[Any] = asyncio.Queue()
        runner_app._session_inboxes_ref[PARENT_SESSION_ID] = inbox
        sock, target = pane
        cursor_native_bridge.write_tmux_target(
            cursor_native_bridge.bridge_dir_for_session_id(CHILD_SESSION_ID),
            socket_path=sock,
            tmux_target=target,
        )
        yield client, inbox


def _drain(inbox: asyncio.Queue[Any]) -> list[dict[str, Any]]:
    """Drain and return every payload currently in the parent inbox."""
    items: list[dict[str, Any]] = []
    while not inbox.empty():
        items.append(inbox.get_nowait())
    return items


async def test_interrupt_must_not_deliver_terminal_cancelled_before_outcome_known(
    monkeypatch: pytest.MonkeyPatch,
    _isolated_bridge_root: Path,
    _live_tmux_pane: tuple[Path, str],
) -> None:
    """Declining a tool / pressing Stop must not report the sub-agent dead.

    ``inject_interrupt`` only sends Escape into the tmux pane; it never
    confirms the agent stopped (contrast ``stop_session``'s ``kill_session``).
    The parent must therefore not receive a terminal ``cancelled`` verdict at
    Escape-delivery time — the sub-agent's outcome is not yet known, and the
    pane's agent here is provably still working.
    """
    async with _dispatched_native_subagent(
        monkeypatch, _isolated_bridge_root, _live_tmux_pane
    ) as (client, inbox):
        resp = await client.post(
            f"/v1/sessions/{CHILD_SESSION_ID}/events", json={"type": "interrupt"}
        )
        assert resp.status_code == 204, (
            f"native interrupt must return 204; got {resp.status_code}: {resp.text}"
        )

        sock, target = _live_tmux_pane
        assert PANE_BANNER in _pane_contents(sock, target), (
            "test-harness invariant: the pane's agent should have survived the "
            "Escape keystroke (it is a plain sh sleep), but its banner is gone"
        )

        premature = _drain(inbox)
        assert not premature, (
            "The interrupt only DELIVERED an Escape keystroke — the sub-agent "
            "provably kept working — yet the runner already declared the "
            f"dispatch terminal to the parent: {premature!r}. "
            "``_wake_parent_after_native_interrupt`` guesses ``cancelled`` "
            "before the outcome is known, which is optimistic-"
            "interrupt defect."
        )


async def test_survived_interrupt_delivers_genuine_result_to_parent(
    monkeypatch: pytest.MonkeyPatch,
    _isolated_bridge_root: Path,
    _live_tmux_pane: tuple[Path, str],
) -> None:
    """A sub-agent that survives the Escape and finishes must not lose its result.

    The forwarder's ``external_session_status: idle`` edge carries the genuine
    result after the interrupted-but-surviving turn completes. Today the
    premature ``cancelled`` was already delivered and drained, a delivered
    status cannot be corrected, and the 204-acked result vanishes — the parent
    orchestrator believes the work was cancelled and never sees the verdict.
    """
    async with _dispatched_native_subagent(
        monkeypatch, _isolated_bridge_root, _live_tmux_pane
    ) as (client, inbox):
        resp = await client.post(
            f"/v1/sessions/{CHILD_SESSION_ID}/events", json={"type": "interrupt"}
        )
        assert resp.status_code == 204, resp.text

        # The agent survived Escape and finished its task; the forwarder
        # reports the completed turn with the genuine result.
        resp = await client.post(
            f"/v1/sessions/{CHILD_SESSION_ID}/events",
            json={
                "type": "external_session_status",
                "data": {"status": "idle", "output": GENUINE_RESULT},
            },
        )
        assert resp.status_code in (204, 503), resp.text

        delivered = _drain(inbox)
        with_result = [m for m in delivered if m.get("output") == GENUINE_RESULT]
        assert with_result, (
            "The sub-agent survived the interrupt and finished its work, and "
            "the forwarder reported the genuine result "
            f"({GENUINE_RESULT!r}) — but the parent inbox received "
            f"{[(m.get('status'), m.get('output')) for m in delivered]!r} "
            "instead. The optimistic ``cancelled`` delivered at interrupt "
            "time drained the work entry, and a delivered status cannot be "
            "corrected, so the genuine result was silently discarded."
        )
        assert all(m.get("status") != "cancelled" for m in with_result), (
            "The genuine result reached the parent but was labelled "
            "``cancelled`` — the terminal status must reflect the sub-agent's "
            f"actual outcome. Got: {with_result!r}"
        )
