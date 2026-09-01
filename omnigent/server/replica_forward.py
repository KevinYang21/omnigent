"""Server-side forwarding of mis-routed session requests to the right replica.

On a multi-replica deployment every replica shares one DB, but each host's
WebSocket tunnel (and its runners' tunnels) registers on exactly ONE replica's
in-memory registries. A session request that lands on a replica without the
tunnel is a *wrong-replica routing miss*. Historically the server answered
``400 wrong_replica`` and delegated recovery entirely to the client — which
only the slice-key-aware Databricks-workspace web client implements, and even
there only when the keyless route happens to hit the right replica. Every
other client (plain SPA, SSE reconnect loop, third-party API callers) stranded
on the raw error.

This module closes that gap server-side: the host row (written by the tunnel
handshake) records the **advertise URL** of the replica holding the tunnel,
and a replica that detects a miss replays the original request against that
URL and mirrors the response back to the caller. The client never sees the
mis-route. Forwarding is best-effort — when no peer URL is known, the URL
names this replica itself, the request was already forwarded once (loop
guard), or the peer is unreachable, callers fall back to the pre-existing
``wrong_replica`` error so behavior degrades to exactly what it was before.
"""

from __future__ import annotations

import logging
import os
import socket
from typing import TYPE_CHECKING, Final
from urllib.parse import urlsplit

import httpx
from fastapi import Request, Response
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

if TYPE_CHECKING:
    from collections.abc import Mapping

_logger = logging.getLogger(__name__)

#: Marks a request that was already forwarded once by a peer replica. A
#: forwarded request that still misses (stale ``replica_url``) must surface
#: the miss rather than bounce between replicas forever.
REPLICA_FORWARD_HEADER: Final[str] = "X-Omnigent-Replica-Forward"

#: Env override naming this replica's peer-reachable base URL, e.g.
#: ``http://10.0.3.7:6767``. Takes precedence over the bind-derived default.
REPLICA_ADVERTISE_URL_ENV: Final[str] = "OMNIGENT_REPLICA_ADVERTISE_URL"

# Hop-by-hop / transport headers that must not be replayed on the forwarded
# request (httpx recomputes host/length; connection control is per-hop).
_HOP_HEADERS: Final[frozenset[str]] = frozenset(
    {
        "host",
        "content-length",
        "connection",
        "keep-alive",
        "transfer-encoding",
        "upgrade",
        "te",
        "trailer",
        "proxy-authorization",
        "proxy-authenticate",
        # Not hop-by-hop, but must not be replayed: the peer would gzip its
        # answer and the relay mirrors bytes without re-declaring the
        # content-encoding (the raw SSE relay in particular would hand the
        # caller compressed bytes it can't parse). Dropping it makes the
        # peer answer identity-encoded.
        "accept-encoding",
    }
)

#: Budget for a forwarded event POST. Generous because a send can park on a
#: managed-launch rendezvous on the peer before it is acknowledged.
_FORWARD_EVENT_TIMEOUT_S: Final[float] = 120.0

#: Connect budget for the forwarded SSE stream open (the stream itself is
#: unbounded once established).
_FORWARD_STREAM_CONNECT_TIMEOUT_S: Final[float] = 30.0


def _primary_local_ipv4() -> str | None:
    """Best-effort primary outbound IPv4 address of this machine.

    Uses the connected-UDP-socket trick: no packet is sent; the kernel just
    picks the source address it would route from. In a container/pod this is
    the pod IP — exactly what a peer replica in the same cluster network can
    reach. Returns ``None`` when no route exists (isolated host).

    :returns: A dotted-quad address, e.g. ``"10.0.3.7"``, or ``None``.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            # RFC 5737 TEST-NET-1 — any non-local target works; nothing is sent.
            probe.connect(("192.0.2.1", 1))
            address = probe.getsockname()[0]
    except OSError:
        return None
    return address if address and not address.startswith("127.") else None


def resolve_replica_advertise_url(
    configured_host: str | None,
    port: int | None,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """Resolve the URL peer replicas can reach THIS replica at.

    The :data:`REPLICA_ADVERTISE_URL_ENV` env var wins when set. Otherwise a
    concrete bind address derives ``http://<host>:<port>``, and a wildcard
    bind (the container/deploy default) falls back to the machine's primary
    outbound IPv4 — in a pod that's the pod IP, which peer replicas on the
    same cluster network can reach. When nothing resolves, this replica is
    not peer-addressable and mis-routed requests fall back to the plain
    ``wrong_replica`` error.

    :param configured_host: The bind host, e.g. ``"127.0.0.1"`` or
        ``"0.0.0.0"``. ``None`` when unknown.
    :param port: The bind port, e.g. ``6767``. ``None`` when unknown.
    :param environ: Process environment; defaults to ``os.environ``.
    :returns: The advertise URL without a trailing slash, or ``None``.
    """
    env = environ if environ is not None else os.environ
    override = (env.get(REPLICA_ADVERTISE_URL_ENV) or "").strip()
    if override:
        return override.rstrip("/")
    if not configured_host or port is None:
        return None
    if configured_host in ("0.0.0.0", "::", "[::]"):
        derived = _primary_local_ipv4()
        return f"http://{derived}:{port}" if derived else None
    host = configured_host
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"  # bare IPv6 literal needs brackets in a URL
    return f"http://{host}:{port}"


def _is_plain_base_url(url: str) -> bool:
    """Whether *url* is a bare ``http(s)://host[:port]`` base URL.

    The advertise URL is written only by the server itself (tunnel
    handshake / env var), but the forward replays caller credentials, so
    defense-in-depth rejects anything that isn't a plain origin — no
    userinfo, path, query, or fragment a poisoned row could smuggle in.

    :param url: Candidate peer URL, e.g. ``"http://10.0.3.7:6767"``.
    :returns: ``True`` when the URL is a forwardable plain origin.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return (
        parts.scheme in ("http", "https")
        and bool(parts.hostname)
        and parts.username is None
        and parts.password is None
        and parts.path in ("", "/")
        and not parts.query
        and not parts.fragment
    )


def _forwardable_peer_url(request: Request, replica_url: str | None) -> str | None:
    """Validate a peer replica URL for forwarding, applying the loop guard.

    :param request: The incoming (mis-routed) request.
    :param replica_url: The tunnel-holding replica's advertise URL from the
        host row, or ``None``/empty when unknown.
    :returns: The normalized peer URL, or ``None`` when forwarding must not
        happen (no URL, a malformed/non-origin URL, already forwarded once,
        or the URL names this replica itself).
    """
    if not replica_url:
        return None
    if request.headers.get(REPLICA_FORWARD_HEADER):
        # Already forwarded once — a second hop means the host row is stale
        # or two replicas disagree; surface the miss instead of looping.
        return None
    if not _is_plain_base_url(replica_url):
        _logger.warning("Refusing cross-replica forward to malformed URL %r", replica_url)
        return None
    peer = replica_url.rstrip("/")
    own = getattr(request.app.state, "replica_advertise_url", None)
    if own and peer == own.rstrip("/"):
        # The row names this replica but the registry has no tunnel — a
        # stale row (e.g. the host dropped moments ago). Nothing to
        # forward to; fall back.
        return None
    return peer


def _forward_headers(request: Request) -> dict[str, str]:
    """Build the header set for the forwarded request.

    Replays the caller's headers (auth identity included — both replicas
    share one auth configuration) minus hop-by-hop ones, and stamps the
    loop-guard marker.

    :param request: The incoming request whose headers to replay.
    :returns: Headers for the peer request.
    """
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_HEADERS}
    headers[REPLICA_FORWARD_HEADER] = "1"
    # Explicit identity (httpx would otherwise inject its own gzip default):
    # the relay mirrors the peer's bytes verbatim, so they must be uncompressed.
    headers["accept-encoding"] = "identity"
    return headers


async def forward_event_to_replica(
    request: Request,
    replica_url: str | None,
    session_id: str,
) -> Response | None:
    """Replay a mis-routed session event POST against the tunnel's replica.

    :param request: The incoming ``POST /v1/sessions/{id}/events`` request.
        Its body is re-read from the cached buffer (FastAPI already consumed
        it to parse the payload), so the replay carries identical bytes.
    :param replica_url: The peer's advertise URL from the host row.
    :param session_id: Session/conversation identifier, e.g. ``"conv_abc"``.
    :returns: A response mirroring the peer's answer, or ``None`` when
        forwarding is unavailable or the peer is unreachable — the caller
        then falls back to raising ``wrong_replica`` exactly as before.
    """
    peer = _forwardable_peer_url(request, replica_url)
    if peer is None:
        return None
    body = await request.body()
    url = f"{peer}/v1/sessions/{session_id}/events"
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=_FORWARD_EVENT_TIMEOUT_S) as client:
            resp = await client.post(url, content=body, headers=_forward_headers(request))
    except httpx.HTTPError as exc:
        _logger.warning(
            "Cross-replica event forward for session %s to %s failed: %s",
            session_id,
            peer,
            exc,
            extra={"session_id": session_id},
        )
        return None
    _logger.info(
        "Forwarded mis-routed event for session %s to replica %s (%d)",
        session_id,
        peer,
        resp.status_code,
        extra={"session_id": session_id},
    )
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type"),
    )


async def forward_stream_to_replica(
    request: Request,
    replica_url: str | None,
    session_id: str,
) -> Response | None:
    """Proxy a mis-routed session SSE stream from the tunnel's replica.

    Opens the same ``GET /v1/sessions/{id}/stream`` (query string preserved)
    against the peer and relays the byte stream, so the caller's live tail
    works no matter which replica its connection landed on.

    :param request: The incoming stream request.
    :param replica_url: The peer's advertise URL from the host row.
    :param session_id: Session/conversation identifier.
    :returns: A streaming response relaying the peer's SSE bytes, a plain
        response mirroring a peer error, or ``None`` when forwarding is
        unavailable or the peer is unreachable (caller falls back).
    """
    peer = _forwardable_peer_url(request, replica_url)
    if peer is None:
        return None
    url = f"{peer}/v1/sessions/{session_id}/stream"
    if request.url.query:
        url = f"{url}?{request.url.query}"
    # The stream is long-lived: no read timeout once connected. The client
    # is closed by the response's background task (or on open failure below).
    client = httpx.AsyncClient(
        trust_env=False,
        timeout=httpx.Timeout(_FORWARD_STREAM_CONNECT_TIMEOUT_S, read=None),
    )
    try:
        peer_request = client.build_request("GET", url, headers=_forward_headers(request))
        resp = await client.send(peer_request, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        _logger.warning(
            "Cross-replica stream forward for session %s to %s failed: %s",
            session_id,
            peer,
            exc,
            extra={"session_id": session_id},
        )
        return None
    if resp.status_code != 200:
        # Mirror the peer's structured error (e.g. 404, or wrong_replica from
        # a stale row — the loop guard stopped a second hop there).
        content = await resp.aread()
        await resp.aclose()
        await client.aclose()
        return Response(
            content=content,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type"),
        )
    _logger.info(
        "Proxying mis-routed stream for session %s from replica %s",
        session_id,
        peer,
        extra={"session_id": session_id},
    )

    async def _close() -> None:
        """Release the proxied stream and its client when the caller goes."""
        await resp.aclose()
        await client.aclose()

    return StreamingResponse(
        resp.aiter_raw(),
        media_type=resp.headers.get("content-type", "text/event-stream"),
        background=BackgroundTask(_close),
        # Same anti-buffering posture as the direct stream route: without
        # these an nginx-style intermediary buffers the proxied SSE bytes,
        # delaying heartbeats/deltas past client timeouts — the very freeze
        # this forward exists to prevent.
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
