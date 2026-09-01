"""Tests for the server request audit-logging helpers.

Covers the two pieces the ``_record_server_metrics`` middleware and the
exception handlers rely on: resolving the operation + session id from the
matched route before routing, and emitting a gated, table-only audit row.
"""

from __future__ import annotations

import logging
import threading

import pytest
from fastapi import FastAPI
from starlette.requests import Request

from omnigent import debug_logging as dl
from omnigent.server.app import _resolve_audit_route


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/v1/sessions/{session_id}")
    def get_session(session_id: str) -> dict[str, str]:  # pragma: no cover - not called
        return {}

    @app.get("/v1/sessions")
    def list_sessions() -> list[str]:  # pragma: no cover - not called
        return []

    @app.get("/v1/hosts")
    def list_hosts() -> list[str]:  # pragma: no cover - not called
        return []

    return app


def _request(app: FastAPI, method: str, path: str) -> Request:
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": [],
        "app": app,
        "router": app.router,
    }
    return Request(scope)


def test_resolve_audit_route_session_scoped_extracts_id() -> None:
    app = _app()
    operation, template, session_id = _resolve_audit_route(
        _request(app, "GET", "/v1/sessions/abc123")
    )
    assert operation == "get_session"
    assert template == "/v1/sessions/{session_id}"
    assert session_id == "abc123"


def test_resolve_audit_route_list_has_no_session_id() -> None:
    # The list route must NOT capture a spurious session id (the loose path
    # regex would treat a literal next segment as one; the route param does not).
    app = _app()
    operation, _template, session_id = _resolve_audit_route(_request(app, "GET", "/v1/sessions"))
    assert operation == "list_sessions"
    assert session_id is None


def test_resolve_audit_route_non_session_route() -> None:
    app = _app()
    operation, _template, session_id = _resolve_audit_route(_request(app, "GET", "/v1/hosts"))
    assert operation == "list_hosts"
    assert session_id is None


def test_resolve_audit_route_unmatched() -> None:
    app = _app()
    assert _resolve_audit_route(_request(app, "GET", "/nope")) == (
        "unmatched",
        "<unmatched>",
        None,
    )


def test_emit_audit_event_is_noop_when_sink_disabled() -> None:
    # Gated on the debug sink: with no sink, building/emitting is skipped
    # entirely, so nothing reaches the audit logger.
    from omnigent.server.app import _emit_audit_event

    captured: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    handler = _Capture()
    audit_logger = dl.audit_event_logger()
    audit_logger.addHandler(handler)
    try:
        assert not dl.debug_sink_enabled()
        _emit_audit_event(
            "get_session", "start", session_id="s1", route="/v1/sessions/{session_id}"
        )
        assert captured == []
    finally:
        audit_logger.removeHandler(handler)


def test_emit_audit_event_ships_operation_and_phase(monkeypatch: pytest.MonkeyPatch) -> None:
    from omnigent.server.app import _emit_audit_event

    monkeypatch.setattr(dl.DebugLogHandler, "_FLUSH_WAIT", 0.01)
    monkeypatch.setattr(dl, "_active_sink", None)
    batches: list[list[dl.DebugLogRow]] = []
    delivered = threading.Event()

    def send(batch: list[dl.DebugLogRow]) -> None:
        batches.append(batch)
        delivered.set()

    dl.attach_debug_log_sink([], source="server", level=logging.INFO, send=send)
    sink = dl._active_sink
    assert sink is not None
    try:
        _emit_audit_event(
            "get_session",
            "ok",
            session_id="conv_1",
            route="/v1/sessions/{session_id}",
            method="GET",
            status="200",
        )
        assert delivered.wait(timeout=1.0)
        row = batches[0][0]
        assert row["event_name"] == "get_session"
        assert row["session_id"] == "conv_1"
        assert row["attributes"]["phase"] == "ok"
        assert row["attributes"]["route"] == "/v1/sessions/{session_id}"
        assert row["attributes"]["status"] == "200"
    finally:
        dl.audit_event_logger().removeHandler(sink)
        dl.sse_event_logger().removeHandler(sink)
        sink.close()
