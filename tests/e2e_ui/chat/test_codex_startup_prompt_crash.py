"""E2E: a Codex CLI that prompts for input on startup must not kill the session.

Reproduces the startup-prompt crash: when
the codex CLI shows an interactive startup prompt (e.g. "a new model is
available — press 1 to try it, 2 to keep your selection", or "press 1 to
update"), the runner-owned TUI pane has nobody to answer it, so the TUI never
creates its app-server thread. ``wait_for_thread_started`` times out (30s), a
``startup_error`` breadcrumb is recorded, and the user's web message fails with
"Codex native thread never started" — the sub-agent session is unusable.

The user journey:

1. start a codex-native session (a polly codex sub-task uses the same launch)
2. the codex CLI blocks on a version-advertisement prompt at startup
   (visible in the session's Terminal view)
3. the user asks their question from the chat composer
4. BUG: the turn fails with an error pill instead of an answer

The startup prompt is injected deterministically with a codex shim that
renders the advertisement and blocks reading stdin when launched as the TUI
(``--remote`` in argv) — exactly the behavior the reporter's older codex CLI
showed — and execs the real codex CLI for every other invocation (app-server,
version probe). Answering the prompt in the terminal (the fix path: something
supplies the keystroke, or the turn no longer depends on the blocked TUI)
un-blocks the shim into the real codex TUI, so this test passes once the
session survives a startup-prompting CLI.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import textwrap
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _HEALTH_POLL_INTERVAL_S,
    _HEALTH_TIMEOUT_S,
    _REPO_ROOT,
    _TEST_AGENT_YAML,
    _codex_cli_supports_mocked_app_server,
    _create_native_codex_session,
    _find_free_port,
    configure_mock_llm,
    reset_mock_llm,
    set_fallback_mock_llm,
)
from tests.e2e_ui.messages.test_message_render_parity import (
    _ASSISTANT,
    _ensure_chat_view,
    _send,
)
from tests.e2e_ui.messages.test_native_codex_render_parity import (
    _open_terminal_view,
    _wait_terminal_connected,
)

# Must match the provider config written below so the mock LLM's model-keyed
# fallback queue matches Codex's requests.
_CODEX_MOCK_MODEL = "gpt-4o"

# The runner gives the TUI 30s to create its thread; the executor then polls up
# to 60s more before surfacing the failure. Generous ceiling on top for the
# turn to settle either way.
_TURN_OUTCOME_TIMEOUT_MS = 150_000

# The startup advertisement the shim renders — mirrors the codex CLI's real
# "new model available" interstitial from the bug report.
_STARTUP_PROMPT_HEADLINE = "Codex 5.5 is now available!"


def _write_codex_startup_prompt_shim(
    directory: Path, codex_path: str, prompt_marker: Path
) -> Path:
    """Write a codex shim that blocks on a startup prompt when run as the TUI.

    Launched with ``--remote`` (the runner-owned TUI pane), it prints the
    version-advertisement prompt, touches *prompt_marker* (machine-checkable
    proof the CLI is sitting at the prompt — xterm renders to canvas, so the
    pane text is not queryable from Playwright), and blocks reading one line
    from stdin — exactly what the reporter's older codex CLI did — then execs
    the real codex TUI once (if ever) someone answers. Every other invocation
    (``app-server``, ``--version``) execs the real codex untouched.

    :param directory: Directory to write the shim into.
    :param codex_path: The real Codex CLI to delegate to.
    :param prompt_marker: File the shim creates when it renders the prompt.
    :returns: Path to the executable shim.
    """
    shim = directory / "codex-startup-prompt"
    shim.write_text(
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            import os
            import sys

            REAL = {codex_path!r}
            if "--remote" in sys.argv[1:]:
                sys.stdout.write(
                    "\\n  {_STARTUP_PROMPT_HEADLINE}\\n\\n"
                    "  1. Try the new model now\\n"
                    "  2. Continue with your current selection\\n\\n"
                    "  Press 1 or 2 to continue: "
                )
                sys.stdout.flush()
                with open({str(prompt_marker)!r}, "w") as marker:
                    marker.write("prompt rendered\\n")
                # Blocks until a terminal user answers — in a runner-owned
                # pane nobody does, which is the reported failure mode.
                sys.stdin.readline()
            os.execv(REAL, sys.argv)
            """
        ),
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return shim


def _write_mock_codex_provider_config(config_home: Path, mock_llm_server_url: str) -> None:
    """Write provider config routing native Codex at the mock LLM server.

    Mirrors the codex branch of ``_temp_omnigent_mock_config`` but writes into
    a private ``OMNIGENT_CONFIG_HOME`` so this test's dedicated server/runner
    pair never touches the developer's real ``~/.omnigent/config.yaml``.

    :param config_home: The private ``OMNIGENT_CONFIG_HOME`` directory.
    :param mock_llm_server_url: Mock LLM server base URL.
    """
    config_home.mkdir(parents=True, exist_ok=True)
    (config_home / "config.yaml").write_text(
        textwrap.dedent(
            f"""\
            providers:
              mock-codex:
                kind: key
                default: [openai]
                openai:
                  base_url: "{mock_llm_server_url}/v1"
                  api_key: "mock-key"
                  wire_api: responses
                  models:
                    default: {_CODEX_MOCK_MODEL}
            """
        ),
        encoding="utf-8",
    )


@dataclass(frozen=True)
class CodexStartupPromptSession:
    """Handle for a codex-native session whose TUI blocks on a startup prompt."""

    base_url: str
    session_id: str
    home_dir: Path
    prompt_marker: Path


@pytest.fixture
def codex_startup_prompt_session(
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
    request: pytest.FixtureRequest,
) -> Iterator[CodexStartupPromptSession]:
    """Spawn a dedicated server + runner whose codex TUI blocks at startup.

    A dedicated pair (rather than the shared ``live_server``) because the
    prompt-injecting codex shim and the private config/state homes must be in
    the runner's environment before it boots, without affecting other tests.
    """
    if request.config.getoption("--ui-base-url"):
        pytest.skip("codex startup-prompt e2e requires an isolated spawned server")

    codex_path = shutil.which("codex")
    if codex_path is None:
        pytest.skip("codex CLI is required for the codex startup-prompt e2e")
    if not _codex_cli_supports_mocked_app_server(codex_path):
        pytest.skip("codex CLI >= 0.139.0 is required for the codex startup-prompt e2e")

    server_tmp = tmp_path_factory.mktemp("e2e_ui_codex_startup_prompt")
    prompt_marker = server_tmp / "startup-prompt-rendered.marker"
    codex_shim = _write_codex_startup_prompt_shim(server_tmp, codex_path, prompt_marker)

    config_home = server_tmp / "config-home"
    source_codex_home = server_tmp / "source-codex-home"
    home_dir = server_tmp / "home"
    state_dir = server_tmp / "codex-native-state"
    artifact_dir = server_tmp / "artifacts"
    for path in (config_home, source_codex_home, home_dir, state_dir, artifact_dir):
        path.mkdir(parents=True, exist_ok=True)
    _write_mock_codex_provider_config(config_home, mock_llm_server_url)

    port = _find_free_port()
    log_path = server_tmp / "server.log"
    runner_log_path = server_tmp / "runner.log"
    db_path = server_tmp / "test.db"
    agent_yaml_path = server_tmp / "hello_world.yaml"
    agent_yaml_path.write_text(_TEST_AGENT_YAML, encoding="utf-8")

    import secrets as _secrets

    from omnigent.runner.identity import token_bound_runner_id

    binding_token = _secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(binding_token)
    base_url = f"http://127.0.0.1:{port}"
    shared_env = {
        **os.environ,
        "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        "OMNIGENT_CONFIG_HOME": str(config_home),
        "OMNIGENT_CODEX_NATIVE_STATE_DIR": str(state_dir),
        "CODEX_HOME": str(source_codex_home),
        "HOME": str(home_dir),
        "OMNIGENT_CODEX_PATH": str(codex_shim),
    }
    server_env = {
        **shared_env,
        "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token,
    }
    runner_env = {
        **shared_env,
        "OMNIGENT_RUNNER_ID": runner_id,
        "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
        "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
        "RUNNER_SERVER_URL": base_url,
    }

    log_handle = open(log_path, "w")  # noqa: SIM115
    runner_log_handle = open(runner_log_path, "w")  # noqa: SIM115
    proc: subprocess.Popen[bytes] | None = None
    runner_proc: subprocess.Popen[bytes] | None = None
    session_id: str | None = None
    try:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import omnigent.server.presence as _p; _p._LEAVE_GRACE_S = 1.0; "
                + "from omnigent.cli import main; main()",
                "server",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--database-uri",
                f"sqlite:///{db_path}",
                "--artifact-location",
                str(artifact_dir),
                "--agent",
                str(agent_yaml_path),
            ],
            env=server_env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
        runner_proc = subprocess.Popen(
            [sys.executable, "-m", "omnigent.runner._entry"],
            env=runner_env,
            stdout=runner_log_handle,
            stderr=subprocess.STDOUT,
        )

        deadline = time.monotonic() + _HEALTH_TIMEOUT_S
        ready = False
        last_error = "not polled yet"
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                last_error = f"process exited early with code {proc.returncode}"
                break
            if runner_proc.poll() is not None:
                last_error = f"runner exited early with code {runner_proc.returncode}"
                break
            try:
                resp = httpx.get(f"{base_url}/health", timeout=2)
                if resp.status_code == 200:
                    status_resp = httpx.get(
                        f"{base_url}/v1/runners/{runner_id}/status",
                        timeout=2,
                    )
                    if status_resp.status_code == 200 and status_resp.json()["online"] is True:
                        ready = True
                        break
                    last_error = (
                        f"runner status HTTP {status_resp.status_code}: {status_resp.text[:200]}"
                    )
                else:
                    last_error = f"health HTTP {resp.status_code}: {resp.text[:200]}"
            except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(_HEALTH_POLL_INTERVAL_S)

        if not ready:
            raise RuntimeError(
                f"codex startup-prompt e2e server did not become healthy within "
                f"{_HEALTH_TIMEOUT_S:.0f}s on {base_url} "
                f"(last_error={last_error}).\n"
                f"Server log at {log_path}:\n"
                f"{log_path.read_text()[-3000:] if log_path.exists() else ''}\n"
                f"Runner log at {runner_log_path}:\n"
                f"{runner_log_path.read_text()[-3000:] if runner_log_path.exists() else ''}"
            )

        session_id = _create_native_codex_session(base_url, runner_id)
        yield CodexStartupPromptSession(
            base_url=base_url,
            session_id=session_id,
            home_dir=home_dir,
            prompt_marker=prompt_marker,
        )
    finally:
        if session_id is not None:
            import contextlib as _contextlib

            with _contextlib.suppress(httpx.HTTPError):
                httpx.delete(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
        if runner_proc is not None and runner_proc.poll() is None:
            runner_proc.send_signal(signal.SIGTERM)
            try:
                runner_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                runner_proc.kill()
                runner_proc.wait(timeout=5)
        if proc is not None and proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        runner_log_handle.close()
        log_handle.close()


def _recorded_startup_errors(home_dir: Path) -> list[str]:
    """Collect any ``startup_error.json`` breadcrumbs the runner recorded.

    The runner writes them under the bridge root, ``$HOME/.omnigent/
    codex-native/<digest>/startup_error.json`` (the fixture gives the
    server/runner pair a private ``HOME``).

    :param home_dir: The private ``HOME`` the runner was spawned with.
    :returns: The recorded startup-error messages, if any.
    """
    messages: list[str] = []
    bridge_root = home_dir / ".omnigent" / "codex-native"
    if not bridge_root.is_dir():
        return messages
    for error_file in bridge_root.rglob("startup_error.json"):
        try:
            payload = json.loads(error_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        message = payload.get("message") if isinstance(payload, dict) else None
        messages.append(str(message) if message else error_file.read_text(encoding="utf-8"))
    return messages


@pytest.mark.timeout(360)
def test_codex_startup_prompt_does_not_kill_session(
    page: Page,
    codex_startup_prompt_session: CodexStartupPromptSession,
    mock_llm_server_url: str,
) -> None:
    """A web message still gets answered when the codex TUI prompts on startup.

    With the bug live this fails: the blocked TUI never creates its thread,
    the runner records a startup error after the 30s thread-start timeout, and
    the user's message dies with an error pill ("Codex native thread never
    started ...") instead of an assistant reply.
    """
    session = codex_startup_prompt_session
    page.goto(f"{session.base_url}/c/{session.session_id}")

    # The terminal view attaches to the codex pane, which is sitting at the
    # interactive startup prompt — the state the reporter described. The
    # shim's marker file proves the prompt rendered and the CLI is blocked
    # (xterm paints to canvas, so the pane text is not DOM-queryable).
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    marker_deadline = time.monotonic() + 60.0
    while time.monotonic() < marker_deadline:
        if session.prompt_marker.exists():
            break
        time.sleep(0.5)
    assert session.prompt_marker.exists(), (
        "The codex shim never rendered its startup prompt — the TUI pane did "
        "not launch, so this run does not reproduce the reported state."
    )

    # The user is in chat asking their question, not watching the terminal.
    _ensure_chat_view(page)

    marker = uuid.uuid4().hex[:8]
    token = f"ast-{marker}"
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": token}],
        key=marker,
        match=marker,
    )
    set_fallback_mock_llm(mock_llm_server_url, _CODEX_MOCK_MODEL, "")

    _send(page, f"Reply with exactly {token} and nothing else. usr-{marker}")

    # The sub-task must not crash: the turn completes with the assistant's
    # reply instead of failing with "Codex native thread never started".
    assistant = page.locator(_ASSISTANT, has_text=token).first
    try:
        expect(assistant).to_be_visible(timeout=_TURN_OUTCOME_TIMEOUT_MS)
    except AssertionError as exc:
        pill_texts = page.get_by_test_id("error-pill").all_inner_texts()
        startup_errors = _recorded_startup_errors(session.home_dir)
        raise AssertionError(
            "The codex sub-agent session died instead of answering while the "
            "codex CLI was blocked on its startup prompt. "
            f"Error pills in chat: {pill_texts!r}. "
            f"Recorded codex startup errors: {startup_errors!r}."
        ) from exc
    expect(page.get_by_test_id("error-pill")).to_have_count(0)
