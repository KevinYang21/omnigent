"""The resolve-URL path (UI Approve) must wake a parked harness elicitation via
its ``resolved_elsewhere`` event, not only its Future.

Before the fix, ``_resolve_elicitation`` set the Future but never signalled
``_harness_parked_elicitations``, so an ASK-gated tool call whose long-poll had
severed/re-parked never woke on UI Approve → indefinite hang. Only the
``/events`` path signalled it. This locks the resolve-URL path in.
"""

from __future__ import annotations

import asyncio

import pytest

from omnigent.server.routes import sessions as S


@pytest.mark.asyncio
async def test_resolve_elicitation_signals_parked_harness_elicitation():
    sid = "conv_resolveurl_test"
    eid = "elicit_evaluate_deadbeefdeadbeefdeadbeefdeadbeef"
    parked = S._ParkedHarnessElicitation(
        session_id=sid,
        tool_name="mcp_example__apply_change",
        tool_input={},
        resolved_elsewhere=asyncio.Event(),
    )
    S._harness_parked_elicitations[eid] = parked
    S._harness_elicitation_owners[eid] = sid
    try:
        assert not parked.resolved_elsewhere.is_set()
        # runner_router=None → the runner forward is skipped; we only assert the
        # server-side parked-elicitation wake.
        await S._resolve_elicitation(sid, {"elicitation_id": eid, "action": "accept"}, None)
        assert parked.resolved_elsewhere.is_set(), (
            "resolve-URL must signal the parked harness elicitation (resolved_elsewhere), "
            "otherwise an ASK-gated tool hangs on UI Approve"
        )
    finally:
        S._harness_parked_elicitations.pop(eid, None)
        S._harness_elicitation_owners.pop(eid, None)


@pytest.mark.asyncio
async def test_not_parked_resolve_keeps_verdict_tombstone():
    # Regression (#62 review): when nothing is parked (severed long-poll, before
    # the retry re-parks), resolve-URL must store a pre-resolved tombstone
    # carrying the ACTUAL verdict, so the re-park returns it. The resolved_elsewhere
    # wake must NOT clobber it with a verdict-less tombstone.
    sid = "conv_tombstone"
    eid = "elicit_evaluate_22222222222222222222222222222222"
    S._harness_parked_elicitations.pop(eid, None)  # ensure NOT parked
    S._harness_pre_resolved_elicitations.pop(eid, None)
    try:
        await S._resolve_elicitation(sid, {"elicitation_id": eid, "action": "accept"}, None)
        tomb = S._harness_pre_resolved_elicitations.get(eid)
        assert tomb is not None, "a not-parked resolve must leave a pre-resolved tombstone"
        assert tomb.result is not None, "the tombstone must carry the verdict (not clobbered)"
    finally:
        S._harness_pre_resolved_elicitations.pop(eid, None)


@pytest.mark.asyncio
async def test_resolve_elicitation_wrong_session_does_not_wake():
    # Ownership guard: a resolve for a DIFFERENT session must not wake this park.
    sid = "conv_owner"
    eid = "elicit_evaluate_11111111111111111111111111111111"
    parked = S._ParkedHarnessElicitation(
        session_id=sid, tool_name="t", tool_input={}, resolved_elsewhere=asyncio.Event()
    )
    S._harness_parked_elicitations[eid] = parked
    S._harness_elicitation_owners[eid] = sid
    try:
        await S._resolve_elicitation(
            "conv_other", {"elicitation_id": eid, "action": "accept"}, None
        )
        assert not parked.resolved_elsewhere.is_set()
    finally:
        S._harness_parked_elicitations.pop(eid, None)
        S._harness_elicitation_owners.pop(eid, None)


@pytest.mark.asyncio
async def test_resolve_with_registered_future_also_tombstones_hook_id():
    # Zombie-waiter safety net: a proxy can sever a hook long-poll client-side
    # without the server observing a disconnect, so the resolve finds the
    # abandoned chunk's Future still registered, sets the verdict on it - and
    # nobody reads it. For a deterministic hook id (the only kind a retry
    # re-parks), the resolve must ALSO tombstone the verdict so the re-park
    # still receives it instead of re-asking the gate.
    sid = "conv_zombie_waiter"
    eid = "elicit_codex_33333333333333333333333333333333"
    future: asyncio.Future = asyncio.get_running_loop().create_future()
    S._harness_elicitation_registry[eid] = future
    S._harness_elicitation_owners[eid] = sid
    try:
        await S._resolve_elicitation(sid, {"elicitation_id": eid, "action": "accept"}, None)
        assert future.done() and future.result().action == "accept"
        tomb = S._harness_pre_resolved_elicitations.get(eid)
        assert tomb is not None, (
            "a resolve landing on a (possibly zombie) registered future must "
            "also tombstone the verdict for a re-park of the same hook id"
        )
        assert tomb.result is not None and tomb.result.action == "accept"
        assert tomb.session_id == sid
    finally:
        S._harness_elicitation_registry.pop(eid, None)
        S._harness_elicitation_owners.pop(eid, None)
        S._harness_pre_resolved_elicitations.pop(eid, None)


@pytest.mark.asyncio
async def test_resolve_with_registered_future_random_id_does_not_tombstone():
    # A randomly minted id (no hook retry will ever re-park it) must not
    # accumulate tombstones: the delivered Future is the whole story there.
    sid = "conv_random_id"
    eid = "elicit_9c2f0e7a4b1d4c6f8e2a5b7c9d1e3f50"  # no harness namespace
    future: asyncio.Future = asyncio.get_running_loop().create_future()
    S._harness_elicitation_registry[eid] = future
    S._harness_elicitation_owners[eid] = sid
    try:
        await S._resolve_elicitation(sid, {"elicitation_id": eid, "action": "accept"}, None)
        assert future.done()
        assert eid not in S._harness_pre_resolved_elicitations
    finally:
        S._harness_elicitation_registry.pop(eid, None)
        S._harness_elicitation_owners.pop(eid, None)
        S._harness_pre_resolved_elicitations.pop(eid, None)


@pytest.mark.asyncio
async def test_resolve_wrong_session_does_not_settle_or_tombstone():
    # Ownership guard extends to the zombie-waiter tombstone: a resolve from a
    # different session must neither settle the future nor leave a tombstone.
    sid = "conv_owner_zombie"
    eid = "elicit_codex_44444444444444444444444444444444"
    future: asyncio.Future = asyncio.get_running_loop().create_future()
    S._harness_elicitation_registry[eid] = future
    S._harness_elicitation_owners[eid] = sid
    try:
        await S._resolve_elicitation(
            "conv_other", {"elicitation_id": eid, "action": "accept"}, None
        )
        assert not future.done()
        assert eid not in S._harness_pre_resolved_elicitations
    finally:
        S._harness_elicitation_registry.pop(eid, None)
        S._harness_elicitation_owners.pop(eid, None)
        S._harness_pre_resolved_elicitations.pop(eid, None)
