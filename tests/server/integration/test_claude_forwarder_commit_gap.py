"""Regression coverage for Claude transcript commit-gap retries."""

from __future__ import annotations

from dataclasses import replace

import httpx
import pytest

from omnigent.claude_native_bridge import ClaudeTranscriptItem
from omnigent.claude_native_forwarder import _post_external_conversation_item
from omnigent.runtime import pending_inputs
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio


async def test_claude_source_retry_is_idempotent_but_distinct_sources_are_preserved(
    client: httpx.AsyncClient,
) -> None:
    """A stale cursor retry dedupes by source identity, not message text."""
    agent = await create_test_agent(client)
    created = await client.post("/v1/sessions", json={"agent_id": agent["id"]})
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]

    transcript_item = ClaudeTranscriptItem(
        source_id="claude-record-uuid:0:message",
        item_type="message",
        data={
            "role": "assistant",
            "agent": "claude-code",
            "content": [{"type": "output_text", "text": "commit-gap-marker"}],
        },
        response_id="resp_claude_record_uuid",
    )

    # First forwarder process: the server commits, then the process dies before
    # transcript_forwarder.json records source_id/cursor advancement.
    await _post_external_conversation_item(
        client,
        session_id=session_id,
        item=transcript_item,
    )
    # Restarted process: stale durable state re-reads and reposts the same source.
    await _post_external_conversation_item(
        client,
        session_id=session_id,
        item=transcript_item,
    )

    response = await client.get(f"/v1/sessions/{session_id}/items")
    assert response.status_code == 200, response.text
    matching = [
        item
        for item in response.json()["data"]
        if item["type"] == "message"
        and any(block.get("text") == "commit-gap-marker" for block in item.get("content", []))
    ]
    assert len(matching) == 1, (
        "one Claude transcript source produced multiple durable items: "
        f"{[item['id'] for item in matching]}"
    )

    # Identical visible content from a distinct transcript record remains a
    # distinct conversation item; text is not the idempotency key.
    await _post_external_conversation_item(
        client,
        session_id=session_id,
        item=replace(transcript_item, source_id="claude-record-uuid-2:0:message"),
    )
    response = await client.get(f"/v1/sessions/{session_id}/items")
    assert response.status_code == 200, response.text
    matching = [
        item
        for item in response.json()["data"]
        if item["type"] == "message"
        and any(block.get("text") == "commit-gap-marker" for block in item.get("content", []))
    ]
    assert len(matching) == 2
    assert len({item["id"] for item in matching}) == 2


async def test_committed_source_retry_does_not_drain_newer_pending_input(
    client: httpx.AsyncClient,
) -> None:
    """Retrying committed A must leave the newer optimistic input B intact."""
    agent = await create_test_agent(client)
    created = await client.post("/v1/sessions", json={"agent_id": agent["id"]})
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    source_a = ClaudeTranscriptItem(
        source_id="claude-user-a:0:message",
        item_type="message",
        data={
            "role": "user",
            "content": [{"type": "input_text", "text": "message A"}],
        },
        response_id="resp_a",
    )

    await _post_external_conversation_item(client, session_id=session_id, item=source_a)
    pending_b = pending_inputs.record(
        session_id,
        [
            {"type": "input_image", "file_id": "file_b", "filename": "b.png"},
            {"type": "input_text", "text": "message B"},
        ],
        created_by="bob@example.com",
    )
    try:
        await _post_external_conversation_item(client, session_id=session_id, item=source_a)

        pending = pending_inputs.snapshot_for(session_id)
        assert [entry["pending_id"] for entry in pending] == [pending_b]
        assert pending[0]["created_by"] == "bob@example.com"
        assert pending[0]["content"][0]["file_id"] == "file_b"
        response = await client.get(f"/v1/sessions/{session_id}/items")
        matching_a = [
            item
            for item in response.json()["data"]
            if item["type"] == "message"
            and any(block.get("text") == "message A" for block in item.get("content", []))
        ]
        assert len(matching_a) == 1
        assert all(block.get("file_id") != "file_b" for block in matching_a[0]["content"])
    finally:
        pending_inputs.reset_for_tests()
