"""E2E regression test: a passive viewer's sidebar must hold its order.

With several sessions in the sidebar, a background session receiving
activity (any server-side ``updated_at`` bump — an agent turn, a rename
from another client) makes its row jump to the top of the list, even
while the user is quietly reading a *different* session with the pointer
nowhere near the list. With 3 running sessions this repeats on every
turn, so the sidebar constantly re-orders itself.

The sidebar sorts by ``updated_at`` desc on every render
(``web/src/shell/sidebarNav.ts`` ``sortByUpdatedAtDesc``). Two freezes
exist today — the active chat's own key (``ActiveChatOverride``) and a
pointer-inside / rename-edit freeze (``frozenKeys`` in ``Sidebar.tsx``)
— but neither covers the reported case: the user is viewing session A
with the pointer outside the list while background sessions B/C keep
bumping.

This test drives that exact journey and asserts the stable behavior
(the delivered background bump must not re-order rows under a passive
viewer):

1. Create three runner-bound sessions and title them so the sidebar
   order is A (top), B, C (bottom).
2. Open session A in the browser; never move the pointer into the list.
3. A third client bumps session C's ``updated_at`` via
   ``PATCH /v1/sessions/{id}`` (the same server-side bump an agent turn
   produces), and the push stream delivers the change to the sidebar.
4. Once the change is visibly delivered (C's row shows its new title),
   assert the row order is still A, B, C — i.e. the delivered update did
   not re-order rows under a passive viewer.

Selectors: the sidebar renders each session as ``<a href="/c/{id}">``
whose text is the session title (``web/src/shell/Sidebar.tsx``). The
default pytest-playwright viewport (1280×720) is desktop, so the sidebar
is visible without a toggle. Structure follows
``test_session_updates_stream.py`` in this directory.
"""

from __future__ import annotations

import time
import uuid

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import _create_runner_bound_session, _server_state


def _set_title(base_url: str, session_id: str, title: str) -> None:
    """Set a session's title, bumping its server-side ``updated_at``.

    ``PATCH /v1/sessions/{id}`` bumps ``updated_at`` exactly like other
    background activity does, and the session-updates stream pushes the
    change to every open sidebar — which is what triggers the re-sort
    under test.

    :param base_url: Spawned server base URL.
    :param session_id: The session to retitle.
    :param title: New title (unique per run so row-text waits can't match
        stale rows).
    """
    resp = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"title": title},
        timeout=10.0,
    )
    resp.raise_for_status()


def _sidebar_order(page: Page, session_ids: list[str]) -> list[str]:
    """Return the top-to-bottom sidebar order of the given session ids.

    Reads all sidebar ``<a href="/c/{id}">`` anchors in DOM order and
    filters to ``session_ids``, so unrelated rows (other tests' sessions
    on the shared server) can't perturb the result.

    :param page: Playwright page with the sidebar visible.
    :param session_ids: The session ids to track.
    :returns: The tracked ids in on-screen order (missing ids omitted).
    """
    id_set = set(session_ids)
    order: list[str] = []
    for anchor in page.locator("a[href^='/c/']").all():
        href = anchor.get_attribute("href") or ""
        sid = href.removeprefix("/c/")
        if sid in id_set and sid not in order:
            order.append(sid)
    return order


@pytest.mark.compat_smoke
def test_background_updated_at_bump_does_not_reorder_sidebar(
    page: Page,
    seeded_session_pair: tuple[str, str, str],
) -> None:
    """A background session's ``updated_at`` bump must not re-order the sidebar.

    The user is reading session A (pointer outside the list) when
    bottom-ranked session C receives background activity. Without the
    open-conversation order hold, the delivered ``updated_at`` bump
    immediately re-sorts the list and C jumps from #3 to #1; with three
    running sessions this repeats on every turn. The test asserts the
    order stays A, B, C after the bump is visibly delivered.

    :param page: Playwright page (records video under ``pytest --video``).
    :param seeded_session_pair: ``(base_url, session_a, session_b)`` —
        two runner-bound sessions; a third is created inline.
    """
    base_url, session_a, session_b = seeded_session_pair
    runner_id = str(_server_state["runner_id"])
    session_c = _create_runner_bound_session(base_url, runner_id)
    try:
        run_tag = uuid.uuid4().hex[:8]
        # Title in C → B → A order so updated_at ranks A newest: the
        # sidebar shows A (top), B, C (bottom). `updated_at` has 1-second
        # granularity, so space the bumps out or all three tie and the
        # starting order is arbitrary.
        _set_title(base_url, session_c, f"order-C-{run_tag}")
        time.sleep(1.1)
        _set_title(base_url, session_b, f"order-B-{run_tag}")
        time.sleep(1.1)
        _set_title(base_url, session_a, f"order-A-{run_tag}")

        page.set_viewport_size({"width": 1280, "height": 720})
        page.goto(f"{base_url}/c/{session_a}")
        # Park the pointer in the main content area, well right of the
        # sidebar, so the pre-existing pointer-inside freeze cannot mask
        # what this test exercises (the open-conversation hold).
        page.mouse.move(900, 400)

        # All three rows rendered ⇒ the list loaded and (via
        # SessionUpdatesProvider) the rows are in the WS watch-set.
        for sid in (session_a, session_b, session_c):
            expect(page.locator(f'a[href="/c/{sid}"]')).to_be_visible(timeout=20_000)
        expected = [session_a, session_b, session_c]
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if _sidebar_order(page, expected) == expected:
                break
            time.sleep(0.25)
        assert _sidebar_order(page, expected) == expected, (
            "precondition: sidebar must start ordered A, B, C"
        )

        # Background activity on the bottom session: a third client bumps
        # C's updated_at. The pointer is parked outside the list, so the
        # pointer-inside freeze does not apply — exactly the reported
        # situation (a passive reader with the mouse in the content area).
        bumped_title = f"order-C-bumped-{run_tag}"
        _set_title(base_url, session_c, bumped_title)

        # Wait until the change is visibly delivered to the sidebar (C's
        # row shows its new title), so the order assertion below runs
        # after — not racing — the push-stream merge that triggers the
        # buggy re-sort.
        expect(page.locator(f'a[href="/c/{session_c}"]')).to_contain_text(
            bumped_title, timeout=20_000
        )

        # KEY ASSERTION: a passive viewer's sidebar must not re-order.
        # Without the open-conversation hold, C has already jumped to #1.
        assert _sidebar_order(page, expected) == expected, (
            "background updated_at bump re-ordered the sidebar "
            "while the user was viewing another session"
        )
    finally:
        httpx.delete(f"{base_url}/v1/sessions/{session_c}", timeout=10.0)
