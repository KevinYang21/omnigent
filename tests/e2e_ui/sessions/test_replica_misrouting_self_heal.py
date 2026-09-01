"""E2E: a session mis-routed to the wrong replica must self-heal.

On a multi-replica deployment every replica shares one DB, but each host's
WebSocket tunnel (and its runners' tunnels) registers on exactly ONE replica's
in-memory ``HostRegistry`` / ``TunnelRegistry``. When the ingress lands a
session's traffic on a replica that does not hold the tunnel, the server
answers ``400 {"code": "wrong_replica", "message": "session runner is on
another replica; retry"}`` (``routes_events.py``) — and nothing recovers:

* the send path surfaces the raw error text as a chat error pill
  (``describeSendFailure`` in ``web/src/store/chatStore.ts``), and
* the SSE stream loop treats the 400 as a generic failed open and retries the
  SAME replica with backoff forever, so the transcript freezes.

Journey (user-observable, from the report):

1. connect a host — its tunnel lands on replica B of a two-replica deployment
2. start a host-backed session and exchange a turn (works: routed to B)
3. the session's next requests land on replica A (ingress mis-route)
4. send a follow-up message
5. observe: the chat shows "session runner is on another replica; retry" and
   retrying never recovers — the follow-up reply never arrives

The test stands the real topology up (two ``omnigent server`` replicas sharing
one sqlite DB + artifact dir, a real host daemon tunneled to replica B, mock
LLM), drives turn 1 in the browser against replica B, then navigates the same
session against replica A — simulating the ingress re-route — and sends the
follow-up. It PASSES only when the follow-up turn completes (the deployment
self-healed the mis-route); on a build with the bug it FAILS with the observed
wrong-replica error pill.

Run::

    pytest tests/e2e_ui/sessions/test_replica_misrouting_self_heal.py
"""

from __future__ import annotations

import io
import json
import os
import signal
import socket
import subprocess
import sys
import tarfile
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import configure_mock_llm, set_fallback_mock_llm

_REPO_ROOT = Path(__file__).resolve().parents[3]

# Server boot budget. Two replicas migrate the same sqlite file, so they boot
# sequentially; each boot is normally a few seconds but a cold CI box is slow.
_SERVER_HEALTH_TIMEOUT_S = 120.0
# Host-online / runner-launch / turn-settle budgets.
_HOST_ONLINE_TIMEOUT_S = 45.0
_TURN_REPLY_TIMEOUT_S = 90.0
# How long the mis-routed follow-up gets to either heal or surface the bug.
_HEAL_OR_FAIL_TIMEOUT_S = 45.0

_TURN1_SENTINEL = "REPLICA_ROUTE_TURN_ONE_OK"
_TURN2_SENTINEL = "REPLICA_ROUTE_HEALED_OK"

_AGENT_YAML = """\
name: {name}
prompt: You are a terse smoke-test assistant. Follow instructions exactly.

executor:
  model: {model}
  harness: openai-agents

os_env:
  type: caller_process
  cwd: .
  sandbox:
    type: none
"""


def _free_port() -> int:
    """Grab an OS-assigned free TCP port."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _http(timeout: float = 30.0) -> httpx.Client:
    """An httpx client that ignores ambient proxy env (all targets are local)."""
    return httpx.Client(timeout=timeout, trust_env=False)


def _agent_bundle(name: str, model: str) -> bytes:
    """Gzip-tar the inline agent YAML for multipart upload."""
    yaml_text = _AGENT_YAML.format(name=name, model=model)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = yaml_text.encode()
        info = tarfile.TarInfo(name=f"{name}.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@dataclass
class _Deployment:
    """The spawned two-replica topology.

    :param url_a: Replica A base URL — the replica WITHOUT the host tunnel
        (where the mis-routed traffic lands).
    :param url_b: Replica B base URL — the replica holding the host tunnel.
    :param host_id: The connected host's id (bare 32-char hex).
    :param mock_llm_url: Mock LLM base URL for scripting turns.
    """

    url_a: str
    url_b: str
    host_id: str
    mock_llm_url: str


def _base_env() -> dict[str, str]:
    """Subprocess env: worktree imports, local-only traffic, no ambient proxy.

    ``PYTHONPATH`` carries the repo root plus the in-repo SDK packages the
    server imports (``omnigent_client``, ``omnigent_ui_sdk``) so the spawned
    processes run the branch's source. Proxy vars are stripped because every
    URL in this topology is 127.0.0.1.
    """
    env = {
        k: v
        for k, v in os.environ.items()
        if k.lower() not in ("http_proxy", "https_proxy", "all_proxy")
    }
    env["PYTHONPATH"] = os.pathsep.join(
        [
            str(_REPO_ROOT),
            str(_REPO_ROOT / "sdks" / "python-client"),
            str(_REPO_ROOT / "sdks" / "ui"),
        ]
    )
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env["no_proxy"] = "127.0.0.1,localhost"
    return env


def _wait_health(url: str, proc: subprocess.Popen[bytes], log_path: Path) -> None:
    """Poll ``/health`` until 200, failing fast if the process dies."""
    deadline = time.monotonic() + _SERVER_HEALTH_TIMEOUT_S
    with _http(timeout=2.0) as client:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                tail = log_path.read_text()[-3000:] if log_path.exists() else ""
                raise RuntimeError(f"server at {url} exited early; log tail:\n{tail}")
            try:
                if client.get(f"{url}/health").status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
    tail = log_path.read_text()[-3000:] if log_path.exists() else ""
    raise RuntimeError(
        f"server at {url} not healthy within {_SERVER_HEALTH_TIMEOUT_S:.0f}s; log tail:\n{tail}"
    )


@pytest.fixture(scope="module")
def two_replica_deployment(
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[_Deployment]:
    """Spawn two server replicas sharing one DB, plus a host tunneled to B.

    Mirrors the production multi-replica posture: one persistent store
    (sqlite file standing in for the shared DB) and one artifact dir, two
    server processes, and a real ``omnigent.host._daemon_entry`` daemon whose
    tunnel registers on replica B only. Replica A therefore sees the host as
    live in the DB but absent from its in-memory registry — the exact state a
    mis-routed request meets.

    :param built_spa: Ensures the SPA bundle is on disk so both replicas
        serve the web UI (the journey is driven in the browser).
    :param mock_llm_server_url: Session-scoped mock LLM; all agent turns are
        scripted against it.
    """
    tmp = tmp_path_factory.mktemp("replica_misroute")
    db_path = tmp / "shared.db"
    artifact_dir = tmp / "artifacts"
    artifact_dir.mkdir()
    srv_home = tmp / "srv_home"
    srv_home.mkdir()

    env = _base_env()
    env.update(
        {
            "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
            "OPENAI_API_KEY": "mock-key",
            "ANTHROPIC_API_KEY": "",
            # Isolated home so server logs/config never touch the real one.
            "HOME": str(srv_home),
        }
    )

    procs: list[subprocess.Popen[bytes]] = []
    log_handles: list[object] = []

    def _spawn(name: str, args: list[str], spawn_env: dict[str, str]) -> subprocess.Popen[bytes]:
        log_path = tmp / f"{name}.log"
        handle = open(log_path, "w")  # noqa: SIM115 — lives for the Popen's lifetime
        log_handles.append(handle)
        proc = subprocess.Popen(args, env=spawn_env, stdout=handle, stderr=subprocess.STDOUT)
        procs.append(proc)
        return proc

    try:
        # Boot the replicas sequentially: concurrent first-boot migrations on
        # one sqlite file contend on the DB lock.
        urls: list[str] = []
        for name in ("replica_a", "replica_b"):
            port = _free_port()
            url = f"http://127.0.0.1:{port}"
            proc = _spawn(
                name,
                [
                    sys.executable,
                    "-m",
                    "omnigent.cli",
                    "server",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--database-uri",
                    f"sqlite:///{db_path}",
                    "--artifact-location",
                    str(artifact_dir),
                ],
                env,
            )
            _wait_health(url, proc, tmp / f"{name}.log")
            urls.append(url)
        url_a, url_b = urls

        # Host daemon → replica B. Fresh HOME (identity/config) and a fresh,
        # EMPTY config home: an ambient harness config with env-ref'd gateway
        # credentials would fail turn setup before the journey starts.
        host_home = tmp / "host_home"
        (host_home / ".omnigent").mkdir(parents=True)
        config_home = tmp / "config_home"
        config_home.mkdir()
        host_id = uuid.uuid4().hex
        (host_home / ".omnigent" / "config.yaml").write_text(
            json.dumps({"host": {"host_id": host_id, "name": f"repro-host-{host_id[:8]}"}})
        )
        host_env = {
            **env,
            "HOME": str(host_home),
            "OMNIGENT_CONFIG_HOME": str(config_home),
        }
        _spawn(
            "host_daemon",
            [sys.executable, "-m", "omnigent.host._daemon_entry", "--server", url_b],
            host_env,
        )

        deadline = time.monotonic() + _HOST_ONLINE_TIMEOUT_S
        with _http() as client:
            while time.monotonic() < deadline:
                resp = client.get(f"{url_b}/v1/hosts")
                if resp.status_code == 200 and any(
                    h["host_id"] == host_id and h["status"] == "online"
                    for h in resp.json()["hosts"]
                ):
                    break
                time.sleep(0.5)
            else:
                tail = (tmp / "host_daemon.log").read_text()[-3000:]
                raise RuntimeError(
                    f"host never came online on replica B within "
                    f"{_HOST_ONLINE_TIMEOUT_S:.0f}s; daemon log tail:\n{tail}"
                )

        yield _Deployment(
            url_a=url_a, url_b=url_b, host_id=host_id, mock_llm_url=mock_llm_server_url
        )
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
        for proc in procs:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        for handle in log_handles:
            handle.close()  # type: ignore[attr-defined]


@pytest.fixture
def misrouted_session(two_replica_deployment: _Deployment) -> Iterator[tuple[_Deployment, str]]:
    """Create a host-backed session with its runner tunneled to replica B."""
    dep = two_replica_deployment
    name = f"replica_probe_{uuid.uuid4().hex[:8]}"
    model = f"replica-probe-{uuid.uuid4().hex[:8]}"

    # Script the two turns: turn 1 (works, via replica B) and the follow-up
    # (only ever consumed when the deployment heals the mis-route). The
    # fallback covers stray draws (e.g. background title generation).
    configure_mock_llm(
        dep.mock_llm_url,
        [{"text": _TURN1_SENTINEL}, {"text": _TURN2_SENTINEL}],
        key=model,
    )
    set_fallback_mock_llm(dep.mock_llm_url, model, _TURN2_SENTINEL)

    with _http() as client:
        create_resp = client.post(
            f"{dep.url_b}/v1/sessions",
            data={"metadata": json.dumps({})},
            files={
                "bundle": ("agent.tar.gz", _agent_bundle(name, model), "application/gzip"),
            },
        )
        create_resp.raise_for_status()
        session_id = create_resp.json()["session_id"]

        # Launch the session's runner on the host — its tunnel registers on
        # replica B (same replica as the host tunnel).
        launch_resp = client.post(
            f"{dep.url_b}/v1/hosts/{dep.host_id}/runners",
            json={"session_id": session_id, "workspace": str(_REPO_ROOT)},
            timeout=90.0,
        )
        assert launch_resp.status_code == 200, (
            f"runner launch failed: {launch_resp.status_code} {launch_resp.text[:300]}"
        )

    try:
        yield dep, session_id
    finally:
        with _http() as client:
            client.delete(f"{dep.url_b}/v1/sessions/{session_id}")


def _send_message(page: Page, text: str) -> None:
    """Fill the composer and click Send."""
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


@pytest.mark.timeout(420)
def test_misrouted_session_self_heals_instead_of_wrong_replica_error(
    page: Page,
    misrouted_session: tuple[_Deployment, str],
) -> None:
    """A mis-routed session send must recover, not strand on wrong_replica.

    On a build with the bug this fails with the observed error: the follow-up
    send against replica A dies with ``session runner is on another replica;
    retry`` rendered as a chat error pill, and the reply never arrives no
    matter how long the client retries.
    """
    dep, session_id = misrouted_session

    # ── Step 1-2: the session works when routed to replica B ──────────
    page.goto(f"{dep.url_b}/c/{session_id}")
    _send_message(page, "say the turn-one sentinel")
    expect(
        page.locator('[data-testid="message-bubble"][data-role="assistant"]').last
    ).to_contain_text(_TURN1_SENTINEL, timeout=int(_TURN_REPLY_TIMEOUT_S * 1000))

    # ── Step 3: the ingress now lands the same session on replica A ───
    # (replica A shares the DB but does NOT hold the host/runner tunnel).
    page.goto(f"{dep.url_a}/c/{session_id}")

    # ── Step 4: the user sends a follow-up ────────────────────────────
    _send_message(page, "say the follow-up sentinel")

    # ── Step 5: healed reply vs. the bug's wrong-replica strand ───────
    deadline = time.monotonic() + _HEAL_OR_FAIL_TIMEOUT_S
    wrong_replica_error: str | None = None
    reply_visible = False
    assistant_bubbles = page.locator('[data-testid="message-bubble"][data-role="assistant"]')
    error_pills = page.get_by_test_id("error-pill")
    while time.monotonic() < deadline:
        for i in range(assistant_bubbles.count()):
            if _TURN2_SENTINEL in (assistant_bubbles.nth(i).inner_text() or ""):
                reply_visible = True
                break
        if reply_visible:
            break
        for i in range(error_pills.count()):
            text = error_pills.nth(i).inner_text() or ""
            if "another replica" in text:
                wrong_replica_error = text
                break
        if wrong_replica_error is not None:
            break
        page.wait_for_timeout(500)

    # Let the user-visible failure sit on screen so a recorded journey ends
    # on exactly what the user sees.
    if wrong_replica_error is not None:
        page.wait_for_timeout(1_500)

    # THE BUG: the mis-routed send strands on the raw wrong-replica error.
    assert wrong_replica_error is None, (
        f"Follow-up send on the mis-routed replica surfaced the raw "
        f"wrong-replica error and never recovered: {wrong_replica_error!r}. "
        f"A session whose runner tunnel lives on another replica must "
        f"self-heal (server-side forward/re-address or client re-route), "
        f"not strand the user on 'session runner is on another replica; retry'."
    )

    assert reply_visible, (
        f"The follow-up turn neither completed nor failed with the "
        f"wrong-replica error within {_HEAL_OR_FAIL_TIMEOUT_S:.0f}s — the "
        f"journey never settled (topology or mock LLM mis-configured)."
    )
