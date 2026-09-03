"""Sidebar must not show a perpetual spinner while paging through shared sessions.

User journey (from the bug report):

1. On a multi-user server, many sessions are shared with the viewer while the
   viewer owns only a few recent sessions of their own.
2. The viewer opens the app. The sidebar's default "My sessions" slice shows
   their own rows quickly (they are the newest sessions, so they land on the
   first page of the unified owned+shared ``updated_at``-desc list).
3. The sidebar keeps paginating in the background through the many shared
   sessions (which the "My sessions" slice filters out client-side), and the
   "Loading…" spinner at the bottom of the list stays visible the whole time —
   with enough shared sessions it never seems to disappear.

Expected behavior: once the viewer's own rows are on screen, the sidebar must
not keep showing a loading spinner for the background pagination walk.

This test seeds many sharer-owned sessions granted to the admin viewer plus a
few admin-owned sessions created last (newest → first page), injects realistic
per-page latency on the paginated ``after=`` fetches (the deployment where the
bug was filed pays a visible round-trip per page), and asserts the spinner is
never shown once the viewer's rows are visible. On the buggy build the spinner
stays up for the entire multi-page background walk, failing the assertion.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from playwright.sync_api import Browser, Route, expect

from tests.e2e_ui.collaboration._multi_user_server import (
    ADMIN_EMAIL,
    MultiUserServer,
    spawn_multi_user_server,
)
from tests.e2e_ui.conftest import _build_hello_world_bundle

# Identity that owns the "many shared sessions" and grants them to the admin.
_SHARER_EMAIL = "sharer-bulk@ui.test"
# Read grant level (mirrors omnigent/server/auth.py).
_LEVEL_READ = 1

# Enough shared sessions for a multi-page background walk (30/page → 4+ pages
# beyond the first), so the sentinel keeps auto-fetching while we observe.
_SHARED_SESSION_COUNT = 130
_OWN_SESSION_TITLES = (
    "viewer own session A",
    "viewer own session B",
    "viewer own session C",
)
# Injected latency per paginated (``after=``) list fetch. Local SQLite answers
# in milliseconds, which would let the walk finish before anyone could see it;
# the reported deployment pays a visible round-trip per page, and this delay
# reproduces that pacing so the spinner window is observable.
_PAGE_FETCH_DELAY_S = 1.5
# Let a correct build hide any transitional spinner after rows appear.
_GRACE_MS = 600
# Sampling window: well inside the injected 4-page × 1.5 s busy window.
_SAMPLES = 12
_SAMPLE_INTERVAL_MS = 250


@dataclass
class _SeededServer:
    """The multi-user server with the bug's session population seeded."""

    server: MultiUserServer
    accessible_count: int


@pytest.fixture(scope="module")
def multi_user_server(
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[MultiUserServer]:
    """A dedicated NON-single-user server (real owner/shared split)."""
    server_tmp = tmp_path_factory.mktemp("e2e_ui_sidebar_shared_pagination")
    yield from spawn_multi_user_server(mock_llm_server_url, server_tmp)


def _create_session(client: httpx.Client, bundle: bytes) -> str:
    resp = client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
    )
    resp.raise_for_status()
    return resp.json()["session_id"]


def _count_accessible(base_url: str) -> int:
    """Walk the admin's paginated session list and count accessible rows."""
    count = 0
    after: str | None = None
    with httpx.Client(
        base_url=base_url,
        headers={"X-Forwarded-Email": ADMIN_EMAIL},
        timeout=30.0,
    ) as client:
        while True:
            params: dict[str, str] = {"order": "desc", "sort_by": "updated_at", "limit": "30"}
            if after:
                params["after"] = after
            resp = client.get("/v1/sessions", params=params)
            resp.raise_for_status()
            page = resp.json()
            count += len(page["data"])
            if not page.get("has_more") or not page.get("last_id"):
                return count
            after = page["last_id"]


@pytest.fixture(scope="module")
def seeded(multi_user_server: MultiUserServer) -> _SeededServer:
    """Seed the bug's population: many shared sessions, few (newest) own ones.

    The sharer's sessions are created FIRST and granted to the admin, then the
    admin's own sessions are created LAST — so on the ``updated_at``-desc list
    the viewer's own rows sit on the first page and the shared bulk fills the
    later pages the sidebar walks in the background.
    """
    base = multi_user_server.base_url
    bundle = _build_hello_world_bundle()
    with httpx.Client(
        base_url=base,
        headers={"X-Forwarded-Email": _SHARER_EMAIL},
        timeout=30.0,
    ) as sharer:
        for _ in range(_SHARED_SESSION_COUNT):
            sid = _create_session(sharer, bundle)
            sharer.put(
                f"/v1/sessions/{sid}/permissions",
                json={"user_id": ADMIN_EMAIL, "level": _LEVEL_READ},
            ).raise_for_status()
    # Ensure the admin's own sessions get strictly newer timestamps.
    time.sleep(1.0)
    with httpx.Client(
        base_url=base,
        headers={"X-Forwarded-Email": ADMIN_EMAIL},
        timeout=30.0,
    ) as admin:
        for title in _OWN_SESSION_TITLES:
            sid = _create_session(admin, bundle)
            admin.patch(f"/v1/sessions/{sid}", json={"title": title}).raise_for_status()
            time.sleep(0.05)
    accessible = _count_accessible(base)
    # Seeding sanity: the grants really made the shared bulk visible to the
    # admin, so the sidebar has a genuine multi-page walk ahead of it.
    assert accessible >= _SHARED_SESSION_COUNT + len(_OWN_SESSION_TITLES), (
        f"expected the admin to see the seeded population, got {accessible} rows"
    )
    return _SeededServer(server=multi_user_server, accessible_count=accessible)


def test_sidebar_settles_while_shared_sessions_paginate(
    browser: Browser,
    seeded: _SeededServer,
) -> None:
    server = seeded.server
    # The suite's video autouse fixture patches only the async Browser API;
    # this test drives the sync API, so honor the record dir directly.
    context_kwargs: dict = {"extra_http_headers": {"X-Forwarded-Email": ADMIN_EMAIL}}
    record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
    if record_dir:
        context_kwargs["record_video_dir"] = record_dir
    context = browser.new_context(**context_kwargs)
    try:
        page = context.new_page()

        def _pace_paginated_fetch(route: Route) -> None:
            request = route.request
            parsed = urlparse(request.url)
            is_paginated_list = (
                request.method == "GET"
                and parsed.path == "/v1/sessions"
                and "after" in parse_qs(parsed.query)
            )
            if is_paginated_list:
                time.sleep(_PAGE_FETCH_DELAY_S)
            route.continue_()

        page.route("**/v1/sessions*", _pace_paginated_fetch)
        page.goto(server.public_url)

        # The viewer's own (newest) sessions render on the default "My
        # sessions" slice from the first page.
        sidebar = page.locator('[data-testid="sidebar-conversation-list"]')
        for title in _OWN_SESSION_TITLES:
            expect(sidebar.get_by_text(title)).to_be_visible(timeout=30_000)

        # Grace: a correct build may show a transitional spinner while the
        # first page settles; give it a beat before judging.
        page.wait_for_timeout(_GRACE_MS)

        # With the viewer's rows visible, the sidebar must not keep showing
        # the pagination spinner while it walks the shared bulk in the
        # background. Sample across the injected multi-page busy window; on
        # the buggy build the sentinel's "Loading…" spinner is up throughout.
        spinner = sidebar.get_by_text("Loading…")
        spinner_sightings = 0
        for _ in range(_SAMPLES):
            if spinner.count() > 0 and spinner.first.is_visible():
                spinner_sightings += 1
            page.wait_for_timeout(_SAMPLE_INTERVAL_MS)
        assert spinner_sightings == 0, (
            "sidebar kept showing the loading spinner during background "
            f"pagination of shared sessions ({spinner_sightings}/{_SAMPLES} "
            f"samples saw it) while the viewer's own sessions were already "
            f"visible ({seeded.accessible_count} accessible sessions seeded)"
        )
    finally:
        context.close()
