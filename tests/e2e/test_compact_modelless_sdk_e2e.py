"""E2E: manual ``/compact`` on a model-less SDK-harness session.

Drives the user journey where ``/compact`` used to error with a
model-requirement message instead of compacting:

1. Start an ``openai-agents`` agent whose spec pins **no** model
   (neither ``executor.model`` nor ``llm.model``).
2. Build some context (run one turn).
3. Press ``/compact`` — the same ``{"type": "compact"}`` control event the
   web composer and the REPL's ``/compact`` command post.

Expected (post-fix): the control is handled — the harness's context is
compacted in place (mirroring the claude-sdk handler from OMNI-743) and a
``compaction`` item lands in the transcript.

The failure mode this guards against: the runner has no ``/compact``
handler for SDK-style harnesses (``openai-agents`` / ``pi`` / ``goose`` /
``qwen`` / ``copilot`` — see the ``body_type == "compact"`` dispatch in
``omnigent/runner/app.py``, which only handles ``*-native`` harnesses), so
the control 204-falls-through to the server's AP-side
``_run_compact_locked``, which used to reject a model-less spec with the
"does not declare an LLM model" error (the clarified wording of the older
"Compaction requires a configured LLM model"). The user got an error
instead of a compaction.

If a future change instead *gates* the ``/compact`` control per harness
capability, update this test deliberately to assert that contract — today
it asserts the primary direction: the control works.

Usage::

    pytest tests/e2e/test_compact_modelless_sdk_e2e.py -v
"""

from __future__ import annotations

import io
import json
import tarfile
import time
import uuid

import httpx
import yaml

from tests.e2e.conftest import (
    OMNIGENT_INTERNAL_WS_ORIGIN,
    configure_mock_llm,
    create_runner_bound_session,
    poll_session_until_terminal,
    reset_mock_llm,
    send_user_message_to_session,
    set_fallback_mock_llm,
)

# Unique content token so the seed turn draws from this test's own
# match-routed mock queue no matter what model string a model-less
# openai-agents harness ends up sending (see configure_mock_llm docs).
_SEED_TOKEN = "modelless-compact-context-seed"

# The model-requirement rejection this bug surfaced to the user. Two
# spellings: the original message and the clarified wording that later
# replaced it — the failure is the same either way.
_MODEL_REQUIRED_MARKERS = (
    "Compaction requires a configured LLM model",
    "does not declare an LLM model",
)


def _register_modelless_agent(client: httpx.Client) -> str:
    """Register an ``openai-agents`` agent that pins **no** model.

    ``register_inline_agent`` always bakes a model into the executor, which
    would defeat the scenario under test, so this builds the bundle by hand:
    a single ``<name>.yaml`` whose executor carries only the harness. The
    ``*.yaml`` arcname routes it through the compat translator (same as the
    other inline-agent helpers), which does not inject a default model.

    :param client: HTTP client pointed at the live server.
    :returns: The registered agent name.
    """
    name = f"modelless-sdk-{uuid.uuid4().hex[:6]}"
    config = {
        "name": name,
        "prompt": "You are a terse test agent. Answer in one short sentence.",
        # Deliberately: no ``model`` (and no top-level ``llm``) — the exact
        # model-less spec shape from the bug report.
        "executor": {"harness": "openai-agents"},
    }
    with io.BytesIO() as buf:
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            yaml_bytes = yaml.dump(config).encode()
            info = tarfile.TarInfo(f"{name}.yaml")
            info.size = len(yaml_bytes)
            tar.addfile(info, io.BytesIO(yaml_bytes))
        bundle = buf.getvalue()

    resp = client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
    )
    if resp.status_code not in (200, 201, 409):
        raise AssertionError(
            f"model-less agent registration failed: {resp.status_code} {resp.text}"
        )
    return name


def test_compact_on_modelless_sdk_session_compacts_instead_of_erroring(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """``/compact`` on a model-less openai-agents session must not error.

    **What breaks if wrong:** the runner has no compact handler for SDK
    harnesses and 204s, the server falls through to AP-side compaction, and
    the model-less spec is rejected with the "requires a configured LLM
    model" / "does not declare an LLM model" error — the user's ``/compact``
    fails and nothing is compacted.
    """
    reset_mock_llm(mock_llm_server_url)
    # Seed turn reply, routed by content token so the (unknown) model string
    # a model-less harness sends cannot miss the queue.
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "OK, context noted."}],
        match=_SEED_TOKEN,
    )
    # A post-fix in-place compaction summarizes through the harness's LLM
    # path; give any such summarization request a fallback answer so the
    # fixed behavior can complete against the mock.
    set_fallback_mock_llm(
        mock_llm_server_url,
        "default",
        "Summary: the user built context in a short conversation.",
    )

    agent_name = _register_modelless_agent(http_client)
    session_id = create_runner_bound_session(
        http_client, agent_name=agent_name, runner_id=live_runner_id
    )

    # Step 2 of the reported journey: build some context. The turn's own
    # outcome is deliberately not asserted — the bug fires on the compact
    # control regardless, and a turn-level environment quirk must not mask
    # the compact assertion below.
    response_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=f"Remember that my project codename is mango-omni. {_SEED_TOKEN}",
    )
    poll_session_until_terminal(http_client, session_id=session_id, response_id=response_id)

    # Step 3: press /compact — the exact control event the web composer's
    # builtin and the REPL's /compact post.
    resp = http_client.post(
        f"/v1/sessions/{session_id}/events",
        json={"type": "compact", "data": {}},
    )

    # The bug: 400 invalid_input carrying the model-requirement error.
    assert resp.status_code < 400, (
        f"/compact on a model-less openai-agents session failed with "
        f"HTTP {resp.status_code}: {resp.text} — manual /compact errors "
        f"instead of compacting for model-less SDK harnesses"
    )
    for marker in _MODEL_REQUIRED_MARKERS:
        assert marker not in resp.text, (
            f"/compact surfaced the raw model-requirement error ({marker!r}) "
            f"instead of compacting: {resp.text}"
        )

    # And it must actually compact: a compaction item lands in the
    # transcript (both the runner-side in-place path and the AP-side path
    # persist one when they succeed).
    deadline = time.monotonic() + 60
    kinds: list[str] = []
    while time.monotonic() < deadline:
        snapshot = http_client.get(f"/v1/sessions/{session_id}")
        snapshot.raise_for_status()
        kinds = [item.get("type") for item in snapshot.json().get("items", [])]
        if "compaction" in kinds:
            break
        time.sleep(2)
    assert "compaction" in kinds, (
        f"/compact was accepted but no compaction item was persisted within "
        f"60s; transcript item kinds: {kinds} — the harness context was not "
        f"compacted"
    )
