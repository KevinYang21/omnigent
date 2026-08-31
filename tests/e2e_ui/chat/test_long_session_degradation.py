"""E2E regression tests for the stuck send-latch fix (OMNI-5653).

Exercises the two observable symptoms reported in the ticket:

Facet 1 — Chat submission completes and persists on a >100-item session
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Before the fix, a session with ``has_more=True`` (more than
``INITIAL_WINDOW_ITEMS = 100`` items) could leave the local ``status``
stuck at ``"streaming"`` if the final ``session_status: idle`` SSE edge
was lost before ``postEvent``'s ``finally`` ran (e.g. a stream drop).
``reconnectStatusPatch`` now unconditionally clears ``status`` to
``"idle"`` on reconnect when the server snapshot confirms idle, unblocking
the composer and queue.

Facet 2 — Working indicator clears after a turn on a >100-item session
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Before the fix, ``sendLatchIsStranded`` required
``cachedConversationStatus() === "idle"``.  On sessions scrolled off
loaded sidebar pages (common with 100+ items) that function returned
``undefined``, so the latch never cleared within
``SEND_CHAIN_MAX_WAIT_MS``.  The fix adds a ``sessionStatus`` fallback so
the latch times out correctly when the sidebar cache misses.

Both tests use the ``long_seeded_session`` fixture which seeds all 110
items in a single bulk write **before** the runner binds — see the fixture
docstring for why that ordering matters.
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


def test_send_succeeds_after_100_seeded_turns(
    page: Page,
    long_seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """Composer enabled, reply appears, message persists on a >100-item session.

    The session has 110 committed items so the SPA loads with
    ``has_more=True``.  The test verifies that the composer is not disabled
    (no stuck send-latch), a new message completes normally, and the reply
    survives a full page reload.

    Fails on old code when the stream drops between the server's final
    ``session_status: idle`` edge and the client — the local ``status``
    stays ``"streaming"`` and the composer stays blocked.
    """
    base_url, session_id = long_seeded_session

    reply_text = f"{_REPLY_MARKER}-{uuid.uuid4().hex[:8]}"
    send_prompt = f"long-session-send-{uuid.uuid4().hex[:8]}"

    configure_mock_llm(
        mock_llm_server_url,
        [{"text": reply_text}],
        key="long-session-send",
        match=send_prompt,
    )

    page.goto(f"{base_url}/c/{session_id}")

    # Composer must be enabled on load — a stuck "streaming" latch from a
    # prior dropped stream edge would leave it disabled.
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible(timeout=20_000)
    expect(composer).to_be_enabled(timeout=20_000)

    _send(page, send_prompt)

    expect(page.locator(_USER).last).to_be_visible(timeout=15_000)
    expect(page.locator(_ASSISTANT, has_text=reply_text).first).to_be_visible(timeout=60_000)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=30_000)

    # Message must persist — not lost due to a stuck latch race.
    page.reload()
    expect(page.locator(_ASSISTANT, has_text=reply_text).first).to_be_visible(timeout=30_000)


def test_working_indicator_clears_after_turn_on_long_session(
    page: Page,
    long_seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """Working indicator clears and a follow-up can be sent on a >100-item session.

    The session has 110 committed items so the SPA loads with
    ``has_more=True``.  The test verifies that the "Working…" indicator
    disappears after the assistant reply arrives (not stuck indefinitely),
    and that the composer is re-enabled for a follow-up turn.

    Fails on old code when ``cachedConversationStatus()`` returns
    ``undefined`` (session scrolled off the sidebar) because
    ``sendLatchIsStranded()`` could never return ``true`` within
    ``SEND_CHAIN_MAX_WAIT_MS``, leaving the indicator on-screen and the
    composer disabled until a manual reload.
    """
    base_url, session_id = long_seeded_session

    reply_text = f"{_REPLY_MARKER}-stuck-{uuid.uuid4().hex[:8]}"
    send_prompt = f"long-session-status-{uuid.uuid4().hex[:8]}"

    configure_mock_llm(
        mock_llm_server_url,
        [{"text": reply_text}],
        key="long-session-status",
        match=send_prompt,
    )

    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible(timeout=20_000)
    expect(composer).to_be_enabled(timeout=20_000)

    _send(page, send_prompt)

    expect(page.locator(_ASSISTANT, has_text=reply_text).first).to_be_visible(timeout=60_000)

    # Indicator must clear on its own — no reload.
    expect(page.locator(_WORKING)).to_have_count(0, timeout=30_000)

    # Composer re-enabled for a follow-up.
    expect(composer).to_be_enabled(timeout=10_000)

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
