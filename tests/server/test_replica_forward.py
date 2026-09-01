"""Tests for cross-replica forwarding of mis-routed session requests.

A session request that lands on a replica without the host's tunnel used to
strand on a terminal ``400 wrong_replica``. The server now records which
replica holds each host's tunnel (``hosts.replica_url``) and forwards the
mis-routed request there. These tests cover the advertise-URL resolution, the
forward decision (loop guard, self-URL, missing URL), and the replay itself
against a stub peer.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.requests import Request as StarletteRequest

from omnigent.server.replica_forward import (
    REPLICA_FORWARD_HEADER,
    forward_event_to_replica,
    forward_stream_to_replica,
    resolve_replica_advertise_url,
)

# ── advertise-URL resolution ──────────────────────────────────────────


def test_advertise_url_env_override_wins() -> None:
    """The env var beats the bind-derived default and drops a trailing slash."""
    url = resolve_replica_advertise_url(
        "127.0.0.1", 6767, {"OMNIGENT_REPLICA_ADVERTISE_URL": "http://10.0.3.7:6767/"}
    )
    assert url == "http://10.0.3.7:6767"


def test_advertise_url_from_concrete_bind() -> None:
    """A concrete bind address is peer-reachable as-is."""
    assert resolve_replica_advertise_url("127.0.0.1", 6767, {}) == "http://127.0.0.1:6767"


@pytest.mark.parametrize("wildcard", ["0.0.0.0", "::", "[::]"])
def test_advertise_url_wildcard_bind_derives_primary_ip(
    wildcard: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wildcard bind (the container default) advertises the primary
    outbound IP — the pod IP peers on the cluster network can reach."""
    import omnigent.server.replica_forward as rf

    monkeypatch.setattr(rf, "_primary_local_ipv4", lambda: "10.0.3.7")
    assert resolve_replica_advertise_url(wildcard, 6767, {}) == "http://10.0.3.7:6767"


@pytest.mark.parametrize("wildcard", ["0.0.0.0", "::", "[::]"])
def test_advertise_url_wildcard_bind_without_route_is_none(
    wildcard: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No derivable primary IP and no env override → not peer-addressable."""
    import omnigent.server.replica_forward as rf

    monkeypatch.setattr(rf, "_primary_local_ipv4", lambda: None)
    assert resolve_replica_advertise_url(wildcard, 6767, {}) is None


def test_advertise_url_ipv6_literal_is_bracketed() -> None:
    """A bare IPv6 bind address becomes a bracketed URL host."""
    assert resolve_replica_advertise_url("fe80::1", 6767, {}) == "http://[fe80::1]:6767"


def test_advertise_url_unknown_bind_returns_none() -> None:
    """No bind info and no env var means not peer-addressable."""
    assert resolve_replica_advertise_url(None, None, {}) is None


# ── request scaffolding ───────────────────────────────────────────────


def _make_request(
    *,
    path: str = "/v1/sessions/conv_x/events",
    method: str = "POST",
    headers: dict[str, str] | None = None,
    body: bytes = b"{}",
    own_url: str | None = "http://127.0.0.1:1111",
    query: str = "",
) -> Request:
    """Build a real starlette Request with a cached body and app state."""
    app = FastAPI()
    app.state.replica_advertise_url = own_url
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope: dict[str, Any] = {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "headers": raw_headers,
        "app": app,
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 50000),
    }
    request = StarletteRequest(scope)
    request._body = body  # pre-cache: FastAPI has already consumed the stream
    return request


# ── forward decision ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_forward_skipped_without_replica_url() -> None:
    """No recorded peer URL → no forward (caller falls back to the error)."""
    assert await forward_event_to_replica(_make_request(), None, "conv_x") is None
    assert await forward_event_to_replica(_make_request(), "", "conv_x") is None


@pytest.mark.asyncio
async def test_forward_skipped_when_already_forwarded() -> None:
    """The loop guard stops a second hop when the host row is stale."""
    request = _make_request(headers={REPLICA_FORWARD_HEADER: "1"})
    assert await forward_event_to_replica(request, "http://127.0.0.1:2222", "conv_x") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_url",
    [
        "ftp://10.0.0.1:21",
        "http://user:pass@10.0.0.1:6767",
        "http://10.0.0.1:6767/some/path",
        "http://10.0.0.1:6767?x=1",
        "not a url",
    ],
)
async def test_forward_skipped_for_non_origin_url(bad_url: str) -> None:
    """Only a plain http(s) origin is forwardable.

    Defense-in-depth: the forward replays caller credentials, so a poisoned
    row must not be able to aim them at an arbitrary target.
    """
    assert await forward_event_to_replica(_make_request(), bad_url, "conv_x") is None


@pytest.mark.asyncio
async def test_forward_skipped_when_peer_is_self() -> None:
    """A row naming this replica itself has nothing to forward to."""
    request = _make_request(own_url="http://127.0.0.1:1111")
    assert await forward_event_to_replica(request, "http://127.0.0.1:1111/", "conv_x") is None


@pytest.mark.asyncio
async def test_forward_returns_none_when_peer_unreachable() -> None:
    """A dead peer degrades to the pre-existing wrong_replica error."""
    # A loopback port nothing listens on: connect is refused immediately.
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        dead_port = sock.getsockname()[1]
    result = await forward_event_to_replica(
        _make_request(), f"http://127.0.0.1:{dead_port}", "conv_x"
    )
    assert result is None


# ── replay against a live stub peer ───────────────────────────────────


@pytest.fixture()
def peer_app() -> tuple[FastAPI, dict[str, Any]]:
    """A stub peer replica capturing what the forward delivers."""
    seen: dict[str, Any] = {}
    app = FastAPI()

    @app.post("/v1/sessions/{session_id}/events")
    async def events(request: Request, session_id: str) -> JSONResponse:
        seen["body"] = await request.body()
        seen["headers"] = dict(request.headers)
        seen["session_id"] = session_id
        return JSONResponse({"queued": True, "item_id": "it_1"}, status_code=202)

    @app.get("/v1/sessions/{session_id}/stream")
    async def stream(request: Request, session_id: str) -> StreamingResponse:
        seen["stream_headers"] = dict(request.headers)
        seen["stream_query"] = request.url.query

        async def _gen():
            yield b"data: {}\n\n"
            yield b"data: [DONE]\n\n"

        return StreamingResponse(_gen(), media_type="text/event-stream")

    return app, seen


@pytest.mark.asyncio
async def test_event_forward_replays_body_and_mirrors_response(
    peer_app: tuple[FastAPI, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The forward replays the exact body and mirrors the peer's answer."""
    app, seen = peer_app
    transport = httpx.ASGITransport(app=app)
    real_client = httpx.AsyncClient

    def _asgi_client(**kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_client(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _asgi_client)

    payload = json.dumps({"type": "message", "data": {"role": "user", "content": []}}).encode()
    request = _make_request(body=payload, headers={"x-forwarded-email": "alice@example.com"})
    response = await forward_event_to_replica(request, "http://127.0.0.1:2222", "conv_x")

    assert response is not None
    assert response.status_code == 202
    assert json.loads(bytes(response.body)) == {"queued": True, "item_id": "it_1"}
    assert seen["body"] == payload
    assert seen["session_id"] == "conv_x"
    # Identity replay + loop-guard stamp.
    assert seen["headers"]["x-forwarded-email"] == "alice@example.com"
    assert seen["headers"][REPLICA_FORWARD_HEADER.lower()] == "1"


@pytest.mark.asyncio
async def test_stream_forward_relays_sse_bytes(
    peer_app: tuple[FastAPI, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stream proxy relays the peer's SSE bytes and preserves the query."""
    app, seen = peer_app
    transport = httpx.ASGITransport(app=app)
    real_client = httpx.AsyncClient

    def _asgi_client(**kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_client(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _asgi_client)

    request = _make_request(
        path="/v1/sessions/conv_x/stream", method="GET", body=b"", query="idle=true"
    )
    response = await forward_stream_to_replica(request, "http://127.0.0.1:2222", "conv_x")

    assert isinstance(response, StreamingResponse)
    chunks = [chunk async for chunk in response.body_iterator]
    # Byte-exact relay: any duplication or reordering would corrupt the SSE
    # framing the client parses.
    assert b"".join(chunks) == b"data: {}\n\ndata: [DONE]\n\n"
    # Anti-buffering posture must survive the proxy hop (see the direct
    # stream route) or an nginx-style intermediary re-freezes the tail.
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
    assert seen["stream_query"] == "idle=true"
    assert seen["stream_headers"][REPLICA_FORWARD_HEADER.lower()] == "1"
    # Release the proxied client cleanly.
    if response.background is not None:
        await response.background()


@pytest.mark.asyncio
async def test_stream_forward_mirrors_peer_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-200 peer answer (e.g. its own miss) is mirrored, not looped."""
    app = FastAPI()

    @app.get("/v1/sessions/{session_id}/stream")
    async def stream(session_id: str) -> JSONResponse:
        return JSONResponse(
            {"error": {"code": "wrong_replica", "message": "stale row"}}, status_code=400
        )

    transport = httpx.ASGITransport(app=app)
    real_client = httpx.AsyncClient

    def _asgi_client(**kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_client(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _asgi_client)

    request = _make_request(path="/v1/sessions/conv_x/stream", method="GET", body=b"")
    response = await forward_stream_to_replica(request, "http://127.0.0.1:2222", "conv_x")
    assert response is not None
    assert response.status_code == 400
    assert json.loads(bytes(response.body))["error"]["code"] == "wrong_replica"
