"""E2E regression guard: fresh composer must list host-discovered skills.

Bug: the web composer's slash-command menu is missing host-discovered
skills on a fresh session. The menu is built from the session snapshot's
``skills`` field (``buildSlashCommandMap`` in ``web/src/pages/ChatPage.tsx``),
which the server populates from ``_fetch_runner_skills``
(``omnigent/server/routes/_sessions/orchestration.py``). That helper returns
``[]`` when no runner is bound (``runner_client is None``), and a runner binds
only once the first turn is dispatched. So on the landing / freshly created
session, a skill sitting in the workspace's ``.claude/skills/`` — the kind of
skill a user reaches for with ``/`` — is absent from the menu until they have
already sent a message.

User journey reproduced here:

1. A host skill exists under the session workspace's ``.claude/skills/``.
2. Start a fresh session in the web UI (no message sent, so no runner bound).
3. Open the composer's ``/`` menu before sending anything.
4. The host-discovered skill should be listed — it is the whole point of the
   ``/`` menu — but on the buggy build it is missing (only built-ins/bundled
   commands appear).

This test asserts the **expected** behavior (the host skill IS offered in the
fresh composer), so it fails on a build with the bug and is the fail->pass
target for the fix. ``/help`` (an unconditional built-in) is asserted first as
a control: it proves the ``/`` menu itself rendered, so the host skill's
absence is specifically the missing-skills bug and not a menu that never
opened.

Selectors mirror the component: rows are
``data-testid="slash-menu-item-<name-sans-slash>"`` (see
``web/src/components/SlashCommandMenu.tsx`` and
``tests/e2e_ui/chat/test_slash_command_menu_matching.py``).
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import _build_hello_world_bundle

# The host-library skill seeded under the workspace's ``.claude/skills/``.
# Name matches ``SkillSpec.name`` (``[a-z0-9-]+``); the composer row testid is
# ``slash-menu-item-<name>``.
_HOST_SKILL = "host-lib-skill"


def _seed_host_skill(workspace: Path, name: str) -> None:
    """Write a discoverable host skill into ``<workspace>/.claude/skills/``.

    ``discover_host_skills`` (walked by the runner's
    ``_resolve_session_skills``) picks up any ``<dir>/SKILL.md`` under a
    ``.claude/skills/`` directory on the session workspace — exactly the
    "skill in your own library" that must be offered in the menu.

    :param workspace: The session workspace directory.
    :param name: The skill name (its frontmatter ``name`` and dir).
    """
    skill_dir = workspace / ".claude" / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: A skill from the user's host library.\n---\n\n"
        "Say HOST-LIB.\n"
    )


def test_fresh_composer_lists_host_discovered_skill(
    page: Page,
    live_server: str,
    tmp_path: Path,
) -> None:
    """A fresh session's ``/`` menu must offer a host-discovered skill.

    Creates a session bound to a workspace that has a host skill under
    ``.claude/skills/`` but deliberately does **not** bind a runner (the
    fresh, pre-first-turn state the user lands in). Navigates to the session
    and opens the composer's ``/`` menu.

    * ``/help`` (an unconditional built-in) must appear — proves the menu
      rendered on a runnerless session.
    * The host skill must appear — this is the expected behavior the fix
      restores. On a buggy build it is absent, so this assertion fails until
      host-discovered skills are surfaced independent of runner binding.

    :param page: Playwright page (fresh context per test).
    :param live_server: Base URL of the spawned server serving the SPA.
    :param tmp_path: Per-test temp dir used for the session workspace.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _seed_host_skill(workspace, _HOST_SKILL)

    # Create a fresh session bound to the seeded workspace. No PATCH to
    # ``runner_id`` — this mirrors the landing/new-chat state before the first
    # turn dispatches (and thus before any runner binds).
    bundle = _build_hello_world_bundle()
    create_resp = httpx.post(
        f"{live_server}/v1/sessions",
        data={"metadata": json.dumps({"workspace": str(workspace)})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        timeout=30.0,
    )
    create_resp.raise_for_status()
    session_id = create_resp.json()["session_id"]

    try:
        page.goto(f"{live_server}/c/{session_id}")

        composer = page.get_by_label("Message the agent")
        expect(composer).to_be_visible(timeout=30_000)

        # Open the slash-command menu.
        composer.fill("/")

        # Control: the menu opened (an always-present built-in is listed).
        expect(page.get_by_test_id("slash-menu-item-help")).to_be_visible(timeout=15_000)

        # The bug: the host-discovered skill is expected here but missing on
        # the buggy build. Fails until the fix surfaces host skills before a
        # runner binds; passes after.
        expect(page.get_by_test_id(f"slash-menu-item-{_HOST_SKILL}")).to_be_visible(timeout=20_000)
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
