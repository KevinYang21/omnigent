"""A resolve that lands on a live parked Future must also tombstone the verdict.

A proxy can sever a harness long-poll client-side while holding the backend
connection open: the harness abandons the chunk, but the server never observes
a disconnect, so the chunk's waiter stays registered as a zombie. A resolve
that finds that zombie sets its Future — written to a connection nobody reads —
and, before this fix, wrote NO pre-resolved tombstone (only the nothing-parked
branch did). The harness's next re-park of the same stable id then found
nothing and re-asked the gate; the operator's answer was lost.

These tests pin the resolve-side half of the fix: resolving a live registered
Future ALSO leaves a verdict-carrying tombstone for the same id, so a re-park
can adopt the verdict. The tombstone is session-scoped and TTL-pruned, so a
verdict that WAS delivered leaves only a harmless entry that ages out.
"""

from __future__ import annotations

import asyncio

import pytest

from omnigent.server.routes import sessions as S
from omnigent.server.schemas import ElicitationResult


@pytest.mark.asyncio
async def test_resolve_of_live_future_also_tombstones_the_verdict():
    sid = "conv_zombie_waiter"
    eid = "elicit_codex_33333333333333333333333333333333"
    future: asyncio.Future[ElicitationResult] = asyncio.get_running_loop().create_future()
    S._harness_elicitation_registry[eid] = future
    S._harness_elicitation_owners[eid] = sid
    S._harness_pre_resolved_elicitations.pop(eid, None)
    try:
        await S._resolve_elicitation(
            sid, {"elicitation_id": eid, "action": "accept", "content": {"ok": "go"}}, None
        )
        # The Future still gets the verdict (the delivered-poll path).
        assert future.done() and future.result().action == "accept"
        # And the tombstone carries the same verdict, so a re-park of the
        # stable id adopts it when the waiter turns out to be a zombie.
        tomb = S._harness_pre_resolved_elicitations.get(eid)
        assert tomb is not None, (
            "resolving a live parked Future must also tombstone the verdict; "
            "a zombie waiter otherwise swallows it and the gate re-asks"
        )
        assert tomb.session_id == sid
        assert tomb.result is not None and tomb.result.action == "accept"
        assert tomb.result.content == {"ok": "go"}
    finally:
        S._harness_elicitation_registry.pop(eid, None)
        S._harness_elicitation_owners.pop(eid, None)
        S._harness_pre_resolved_elicitations.pop(eid, None)


@pytest.mark.asyncio
async def test_resolve_from_wrong_session_tombstones_nothing():
    # The ownership guard must cover the tombstone too: a foreign session's
    # resolve may neither settle the Future nor plant a verdict for the id.
    sid = "conv_owner_session"
    eid = "elicit_codex_44444444444444444444444444444444"
    future: asyncio.Future[ElicitationResult] = asyncio.get_running_loop().create_future()
    S._harness_elicitation_registry[eid] = future
    S._harness_elicitation_owners[eid] = sid
    S._harness_pre_resolved_elicitations.pop(eid, None)
    try:
        await S._resolve_elicitation(
            "conv_intruder", {"elicitation_id": eid, "action": "accept"}, None
        )
        assert not future.done()
        assert S._harness_pre_resolved_elicitations.get(eid) is None
    finally:
        S._harness_elicitation_registry.pop(eid, None)
        S._harness_elicitation_owners.pop(eid, None)
        S._harness_pre_resolved_elicitations.pop(eid, None)


@pytest.mark.asyncio
async def test_resolve_with_invalid_payload_tombstones_nothing():
    # A malformed verdict must not settle the Future or leave a tombstone a
    # re-park would then try to honor.
    sid = "conv_bad_payload"
    eid = "elicit_codex_55555555555555555555555555555555"
    future: asyncio.Future[ElicitationResult] = asyncio.get_running_loop().create_future()
    S._harness_elicitation_registry[eid] = future
    S._harness_elicitation_owners[eid] = sid
    S._harness_pre_resolved_elicitations.pop(eid, None)
    try:
        await S._resolve_elicitation(
            sid, {"elicitation_id": eid, "action": "not-a-real-action"}, None
        )
        assert not future.done()
        assert S._harness_pre_resolved_elicitations.get(eid) is None
    finally:
        S._harness_elicitation_registry.pop(eid, None)
        S._harness_elicitation_owners.pop(eid, None)
        S._harness_pre_resolved_elicitations.pop(eid, None)
