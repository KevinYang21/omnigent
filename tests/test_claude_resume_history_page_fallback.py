"""Tests for the claude resume history fetch's page-size fallback.

Deployed conversation stores have failed ``GET /v1/sessions/<id>/items``
reads at large page sizes while serving smaller pages of the same history
fine (200 at ``limit<=400``, 500 at ``limit>=500``). The resume fetch must
not abort — and thereby silently launch a blank Claude session — when the
history is fully recoverable at a smaller page size.
"""

from __future__ import annotations

import json

import click
import httpx
import pytest

from omnigent import claude_native

_SESSION = "conv_large_history"


def _item(i: int) -> dict[str, object]:
    """A minimal flat API message item with a stable id."""
    return {"id": f"item_{i:04d}", "type": "message", "data": {"role": "user"}}


class _PagedItemsServer:
    """Mock /items endpoint that 500s above a page-size threshold.

    Mirrors the deployed failure signature: pages requested with a limit
    above *fail_above* return the production ``internal_error`` body; pages
    at or below it serve real slices of *total* items with correct
    ``has_more`` / ``last_id`` continuation.
    """

    def __init__(self, total: int, fail_above: int) -> None:
        self.total = total
        self.fail_above = fail_above
        self.requests: list[tuple[int, str | None]] = []
        self.items = [_item(i) for i in range(total)]

    def handler(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        limit = int(params["limit"])
        after = params.get("after")
        self.requests.append((limit, after))
        if limit > self.fail_above:
            return httpx.Response(
                500,
                json={
                    "error": {"code": "internal_error", "message": "An internal error occurred."}
                },
            )
        start = 0
        if after is not None:
            ids = [it["id"] for it in self.items]
            start = ids.index(after) + 1
        page = self.items[start : start + limit]
        has_more = start + limit < self.total
        payload: dict[str, object] = {"data": page, "has_more": has_more}
        if has_more and page:
            payload["last_id"] = page[-1]["id"]
        return httpx.Response(200, json=payload)


async def _fetch(server: _PagedItemsServer) -> list[dict[str, object]]:
    transport = httpx.MockTransport(server.handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://example.com") as client:
        return await claude_native._fetch_all_session_items_for_claude_resume(client, _SESSION)


@pytest.mark.asyncio
async def test_large_page_500_falls_back_to_smaller_pages() -> None:
    """A 500 on the big page degrades the page size instead of aborting.

    The same history serves fine at ``limit<=400``, so the fetch must
    recover every item (in order) by re-requesting the failed window at a
    smaller size and paginating from there.
    """
    server = _PagedItemsServer(total=600, fail_above=400)

    items = await _fetch(server)

    assert [it["id"] for it in items] == [f"item_{i:04d}" for i in range(600)]
    # First request tried the big page, the retry re-requested the SAME
    # window (no cursor) at the degraded size, and pagination continued at
    # that size — never re-attempting the failing large limit.
    assert server.requests[0] == (1000, None)
    assert server.requests[1] == (400, None)
    assert all(limit <= 400 for limit, _ in server.requests[1:])


@pytest.mark.asyncio
async def test_all_page_sizes_failing_still_raises() -> None:
    """When every fallback size also 5xxes, the failure surfaces."""
    server = _PagedItemsServer(total=600, fail_above=0)

    with pytest.raises(click.ClickException, match="Failed to fetch history"):
        await _fetch(server)

    # Every configured rung was attempted before giving up.
    assert [limit for limit, _ in server.requests] == list(
        claude_native._CLAUDE_RESUME_PAGE_LIMITS
    )


@pytest.mark.asyncio
async def test_client_error_does_not_retry_smaller_pages() -> None:
    """A 4xx is the caller's bug, not a page-size problem — no fallback."""

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(404, json={"error": {"message": "no such session"}})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://example.com") as client:
        with pytest.raises(click.ClickException, match="Failed to fetch history"):
            await claude_native._fetch_all_session_items_for_claude_resume(client, _SESSION)

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_healthy_server_pages_at_full_size() -> None:
    """With no failures the fetch keeps the original large page size."""
    server = _PagedItemsServer(total=1500, fail_above=1000)

    items = await _fetch(server)

    assert len(items) == 1500
    assert json.dumps(items[0]) == json.dumps(_item(0))
    assert [limit for limit, _ in server.requests] == [1000, 1000]
