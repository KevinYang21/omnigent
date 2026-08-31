"""E2E regression tests for the stuck send-latch fix (OMNI-5653).

Two observable symptoms:

Facet 1 — Chat submission completes and persists
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The fix in ``reconnectStatusPatch`` clears a stuck local ``status:
"streaming"`` when the server snapshot confirms idle on stream reconnect.
This test verifies the observable result: composer is enabled, a sent
message produces a reply, and the reply persists after a full reload.

Facet 2 — Working indicator clears after a turn
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The ``sendLatchIsStranded`` fallback and ``reconnectStatusPatch`` together
ensure the "Working…" indicator disappears when the server reports idle,
even when the session's sidebar row is absent from loaded sidebar pages.
This test verifies the indicator clears on its own after the assistant
reply arrives, and that a follow-up message can then be submitted.
"""

from __future__ import annotations

import uuid

from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import configure_mock_llm

# —— Selectors ——————————————————————————————————————————————————————————
_COMPOSER = "Ask the agent anything…"

_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_USER = '[data-testid="message-bubble"][data-role="user"]'
_WORKING = '[data-testid="working-indicator"]'

_REPLY_MARKER = "long-session-reply"


def _send(page: Page, text: str) -> None:
    """Fill the composer and click Send."""
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible(timeout=20_000)
    expect(composer).to_be_enabled(timeout=20_000)
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def test_send_succeeds_and_working_indicator_clears(
    page: Page,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """Composer is enabled, reply completes, and indicator clears.

    Regression guard for the stuck send-latch bugs:
    - reconnectStatusPatch now clears local status on reconnect when the
      server snapshot shows idle, so a post-send stream drop doesn't wedge
      the composer indefinitely.
    - sendLatchIsStranded falls back to sessionStatus when the sidebar
      cache doesn't hold the session's row.
    """
    base_url, session_id = seeded_session

    reply_text = f"{_REPLY_MARKER}-{uuid.uuid4().hex[:8]}"
    send_prompt = f"long-session-send-{uuid.uuid4().hex[:8]}"

    configure_mock_llm(
        mock_llm_server_url,
        [{"text": reply_text}],
        key="long-session-send",
        match=send_prompt,
    )

    page.goto(f"{base_url}/c/{session_id}")

    # Composer must be enabled — the fix ensures no stale "streaming" latch
    # blocks it after a previous turn completed with a dropped stream edge.
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible(timeout=20_000)
    expect(composer).to_be_enabled(timeout=20_000)

    _send(page, send_prompt)

    expect(page.locator(_USER).last).to_be_visible(timeout=15_000)
    expect(page.locator(_ASSISTANT, has_text=reply_text).first).to_be_visible(timeout=60_000)

    # Working indicator must clear — the fix ensures sessionStatus reaches
    # "idle" even when the SSE edge was lost before postEvent's finally ran.
    expect(page.locator(_WORKING)).to_have_count(0, timeout=30_000)

    # Sent message must persist after reload (not ephemeral).
    page.reload()
    expect(page.locator(_ASSISTANT, has_text=reply_text).first).to_be_visible(timeout=30_000)

    # Follow-up turn works — composer is re-enabled after the first turn.
    followup_reply = f"{_REPLY_MARKER}-followup-{uuid.uuid4().hex[:8]}"
    followup_prompt = f"followup-{uuid.uuid4().hex[:8]}"
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": followup_reply}],
        key="long-session-followup",
        match=followup_prompt,
    )
    _send(page, followup_prompt)
    expect(page.locator(_ASSISTANT, has_text=followup_reply).first).to_be_visible(timeout=60_000)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=30_000)
