from __future__ import annotations

import json
import shlex
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from session_space_sync import (
    LAUNCHER_VERSION,
    TOKEN_LAUNCHER,
    Binding,
    HerdrClient,
    OmnigentClient,
    ReconcileAction,
    SessionSummary,
    StateStore,
    SyncError,
    WorkspaceSummary,
    apply_plan,
    catalog_records,
    expected_tokens,
    hydrate_adoption_panes,
    loaded_session_ids,
    main,
    merge_session_catalogs,
    origin_fingerprint,
    plan_reconciliation,
    reopen_bindings,
    resolve_existing_host_id,
    select_projection_sessions,
    session_from_picker_record,
    sync_loaded_workspace_titles,
    synchronize_once,
)

ORIGIN = origin_fingerprint("https://omnigent.example")


def session(
    session_id: str = "conv_one",
    *,
    title: str = "First task",
    status: str = "running",
    online: bool = True,
    pinned: bool = False,
    project_id: str | None = "project_one",
    project_name: str | None = "Project One",
    pending_elicitations: int = 0,
    host_resumable: bool | None = None,
) -> SessionSummary:
    return SessionSummary(
        session_id=session_id,
        title=title,
        agent_name="Codex",
        status=status,  # type: ignore[arg-type]
        pending_elicitations=pending_elicitations,
        workspace="/remote/project",
        updated_at=1_725_000_000,
        runner_id=f"runner_{session_id}",
        host_id="host_one",
        runner_online=online,
        host_online=online,
        host_resumable=host_resumable,
        pinned=pinned,
        project_id=project_id,
        project_name=project_name,
    )


def workspace(
    item: SessionSummary,
    *,
    workspace_id: str = "w1",
    pane_id: str | None = "w1:p1",
    label: str | None = None,
    status: str = "working",
    tokens: dict[str, str] | None = None,
    focused: bool = False,
) -> WorkspaceSummary:
    return WorkspaceSummary(
        workspace_id=workspace_id,
        label=label if label is not None else item.label,
        agent_status=status,
        tokens=expected_tokens(item, ORIGIN) if tokens is None else tokens,
        focused=focused,
        candidate_pane_id=pane_id,
    )


class ReconcilePlanTest(unittest.TestCase):
    def test_new_sessions_create_distinct_workspaces_even_with_same_title(self) -> None:
        sessions = [session("conv_one"), session("conv_two")]

        actions = plan_reconciliation(sessions, [], {}, origin=ORIGIN)

        self.assertEqual(
            [(action.kind, action.session_id) for action in actions],
            [("create", "conv_one"), ("create", "conv_two")],
        )

    def test_second_identical_snapshot_is_a_noop(self) -> None:
        item = session()
        existing = workspace(item)
        bindings = {item.session_id: Binding("w1", "w1:p1")}

        actions = plan_reconciliation([item], [existing], bindings, origin=ORIGIN)

        self.assertEqual(actions, [])

    def test_rename_updates_in_place(self) -> None:
        item = session(title="Renamed upstream")
        existing = workspace(item, label="Old name")
        bindings = {item.session_id: Binding("w1", "w1:p1")}

        actions = plan_reconciliation([item], [existing], bindings, origin=ORIGIN)

        self.assertEqual([action.kind for action in actions], ["rename"])
        self.assertEqual(actions[0].workspace_id, "w1")

    def test_missing_workspace_marks_local_close_without_recreating(self) -> None:
        item = session()
        bindings = {item.session_id: Binding("w9", "w9:p1")}

        actions = plan_reconciliation([item], [], bindings, origin=ORIGIN)

        self.assertEqual([action.kind for action in actions], ["mark_closed"])

    def test_locally_closed_binding_stays_closed(self) -> None:
        item = session()
        bindings = {item.session_id: Binding("w9", "w9:p1", locally_closed=True)}

        actions = plan_reconciliation([item], [], bindings, origin=ORIGIN)

        self.assertEqual(actions, [])

    def test_explicit_reopen_drops_a_stale_binding_then_create_is_planned(self) -> None:
        item = session()
        bindings = {item.session_id: Binding("w9", "w9:p1", locally_closed=True)}

        changed = reopen_bindings({item.session_id}, bindings, [])
        actions = plan_reconciliation([item], [], bindings, origin=ORIGIN)

        self.assertTrue(changed)
        self.assertNotIn(item.session_id, bindings)
        self.assertEqual([action.kind for action in actions], ["create"])

    def test_metadata_token_allows_safe_adoption_after_state_loss(self) -> None:
        item = session()
        existing = workspace(item)

        actions = plan_reconciliation([item], [existing], {}, origin=ORIGIN)

        self.assertEqual([action.kind for action in actions], ["adopt"])
        self.assertEqual(actions[0].workspace_id, "w1")
        self.assertEqual(actions[0].pane_id, "w1:p1")
        self.assertEqual(actions[0].launcher_version, LAUNCHER_VERSION)

    def test_foreign_origin_is_not_adopted(self) -> None:
        item = session()
        tokens = expected_tokens(item, "someone-else")
        existing = workspace(item, tokens=tokens)

        actions = plan_reconciliation([item], [existing], {}, origin=ORIGIN)

        self.assertEqual([action.kind for action in actions], ["create"])

    def test_duplicate_token_bindings_fail_closed(self) -> None:
        item = session()
        first = workspace(item, workspace_id="w1")
        second = workspace(item, workspace_id="w2")

        with self.assertRaisesRegex(SyncError, "duplicate managed"):
            plan_reconciliation([item], [first, second], {}, origin=ORIGIN)

    def test_unrelated_duplicate_does_not_block_selection_only_plan(self) -> None:
        selected = session("selected")
        unrelated = session("unrelated")
        duplicate_one = workspace(unrelated, workspace_id="w8")
        duplicate_two = workspace(unrelated, workspace_id="w9")

        actions = plan_reconciliation(
            [selected],
            [duplicate_one, duplicate_two],
            {},
            origin=ORIGIN,
        )

        self.assertEqual(
            [(action.kind, action.session_id) for action in actions],
            [("create", selected.session_id)],
        )

    def test_waiting_session_updates_metadata_and_native_agent_state(self) -> None:
        item = session(status="waiting")
        existing = workspace(
            item,
            status="working",
            tokens={**expected_tokens(item, ORIGIN), "omnigent_status": "running"},
        )
        bindings = {item.session_id: Binding("w1", "w1:p1")}

        actions = plan_reconciliation([item], [existing], bindings, origin=ORIGIN)

        self.assertEqual([action.kind for action in actions], ["metadata", "agent_status"])

    def test_legacy_tui_is_not_sent_a_shell_upgrade_command(self) -> None:
        item = session()
        old_tokens = expected_tokens(item, ORIGIN)
        old_tokens.pop(TOKEN_LAUNCHER)
        existing = workspace(item, tokens=old_tokens)
        bindings = {
            item.session_id: Binding(
                "w1",
                "w1:p1",
                launcher_started=True,
                launcher_version=None,
            )
        }

        actions = plan_reconciliation([item], [existing], bindings, origin=ORIGIN)

        self.assertEqual(actions, [])

    def test_legacy_metadata_refresh_does_not_claim_open_launcher(self) -> None:
        item = session()
        old_tokens = expected_tokens(item, ORIGIN)
        old_tokens.pop(TOKEN_LAUNCHER)
        old_tokens["omnigent_status"] = "idle"
        existing = workspace(item, tokens=old_tokens)
        bindings = {
            item.session_id: Binding(
                "w1",
                "w1:p1",
                launcher_started=True,
                launcher_version=None,
            )
        }

        actions = plan_reconciliation([item], [existing], bindings, origin=ORIGIN)

        self.assertEqual([action.kind for action in actions], ["metadata"])
        self.assertFalse(actions[0].include_launcher_token)

    def test_current_state_avoids_duplicate_launch_during_metadata_retry(self) -> None:
        item = session()
        old_tokens = expected_tokens(item, ORIGIN)
        old_tokens.pop(TOKEN_LAUNCHER)
        existing = workspace(item, tokens=old_tokens)
        bindings = {
            item.session_id: Binding(
                "w1",
                "w1:p1",
                launcher_version=LAUNCHER_VERSION,
            )
        }

        actions = plan_reconciliation([item], [existing], bindings, origin=ORIGIN)

        self.assertEqual([action.kind for action in actions], ["metadata"])

    def test_previous_open_launcher_is_not_overwritten_or_retokened(self) -> None:
        item = session()
        old_tokens = expected_tokens(item, ORIGIN)
        old_tokens[TOKEN_LAUNCHER] = "open-v1"
        existing = workspace(item, tokens=old_tokens)
        bindings = {
            item.session_id: Binding(
                "w1",
                "w1:p1",
                launcher_started=True,
                launcher_version="open-v1",
            )
        }

        actions = plan_reconciliation([item], [existing], bindings, origin=ORIGIN)

        self.assertEqual(actions, [])

    def test_restored_workspace_refreshes_metadata_and_relaunches_open(self) -> None:
        item = session()
        restored = workspace(item, status="unknown", tokens={})
        bindings = {
            item.session_id: Binding(
                "w1",
                "w1:p1",
                launcher_started=True,
                launcher_version=LAUNCHER_VERSION,
            )
        }

        actions = plan_reconciliation([item], [restored], bindings, origin=ORIGIN)

        self.assertEqual(
            [action.kind for action in actions],
            ["metadata", "launch_open"],
        )

    def test_explicit_open_focuses_an_already_loaded_space(self) -> None:
        item = session(online=False)
        existing = workspace(item, focused=False)
        bindings = {
            item.session_id: Binding(
                "w1",
                "w1:p1",
                launcher_version=LAUNCHER_VERSION,
            )
        }

        actions = plan_reconciliation(
            [item],
            [existing],
            bindings,
            origin=ORIGIN,
            activate={item.session_id},
        )

        self.assertEqual([action.kind for action in actions], ["focus"])


class WorkingSetTest(unittest.TestCase):
    def test_default_selects_active_online_and_loaded_but_not_idle_or_offline(self) -> None:
        active = session("active", online=True)
        idle_online = session("idle_online", status="idle", online=True)
        loaded = session("loaded", online=False)
        token_loaded = session("token_loaded", online=False)
        unseen = session("unseen", online=False)
        bindings = {loaded.session_id: Binding("w1", "w1:p1")}
        spaces = [workspace(token_loaded, workspace_id="w2")]

        selected = select_projection_sessions(
            [active, idle_online, loaded, token_loaded, unseen],
            bindings,
            spaces,
            origin=ORIGIN,
            open_session=None,
        )

        self.assertEqual(
            [item.session_id for item in selected],
            ["active", "loaded", "token_loaded"],
        )

    def test_pending_attention_is_active_and_auto_loaded_when_online(self) -> None:
        attention = session(
            "attention",
            status="idle",
            online=True,
            pending_elicitations=1,
        )

        selected = select_projection_sessions(
            [attention],
            {},
            [],
            origin=ORIGIN,
            open_session=None,
        )

        self.assertEqual([item.session_id for item in selected], ["attention"])

    def test_explicit_picker_selection_adds_one_unseen_offline_session(self) -> None:
        online = session("online", online=True)
        selected_offline = session("selected", online=False)
        other_offline = session("other", online=False)

        selected = select_projection_sessions(
            [online, selected_offline, other_offline],
            {},
            [],
            origin=ORIGIN,
            open_session=selected_offline.session_id,
        )

        self.assertEqual(
            [item.session_id for item in selected],
            ["online", "selected"],
        )

    def test_catalog_record_exposes_picker_ranking_fields(self) -> None:
        item = session(
            "pinned",
            online=False,
            pinned=True,
            project_id="proj_123",
            host_resumable=False,
        )

        record = item.catalog_record()

        self.assertEqual(record["runner_online"], False)
        self.assertEqual(record["active"], True)
        self.assertEqual(record["pinned"], True)
        self.assertEqual(record["project_id"], "proj_123")
        self.assertEqual(record["project"], "Project One")
        self.assertEqual(record["updated_at"], 1_725_000_000)
        self.assertEqual(record["runner_id"], "runner_pinned")
        self.assertEqual(record["host_id"], "host_one")
        self.assertEqual(record["recovery_hint"], "reconnect")

    def test_recovery_hint_is_unknown_when_list_omits_host_resumability(self) -> None:
        item = session("offline", online=False, host_resumable=None)

        self.assertEqual(item.recovery_hint, "unknown")

    def test_picker_catalog_merges_pins_and_marks_only_real_workspaces_loaded(self) -> None:
        recent = session("recent")
        pinned = session("pinned", pinned=True)
        duplicate = session("recent", pinned=True)
        catalog = merge_session_catalogs([recent], [duplicate, pinned])
        spaces = [workspace(recent, workspace_id="w1")]
        bindings = {
            recent.session_id: Binding("w1", "w1:p1"),
            pinned.session_id: Binding("missing", "missing:p1"),
        }

        loaded = loaded_session_ids(spaces, bindings, origin=ORIGIN)
        records = catalog_records(catalog, loaded=loaded)

        self.assertEqual([item.session_id for item in catalog], ["recent", "pinned"])
        self.assertEqual([record["loaded"] for record in records], [True, False])

    def test_selection_only_hydrates_only_the_requested_workspace(self) -> None:
        selected = session("selected")
        unrelated = session("unrelated")
        selected_space = workspace(selected, workspace_id="w1")
        unrelated_space = workspace(unrelated, workspace_id="w2", pane_id="w2:p1")
        herdr = MagicMock(spec=HerdrClient)
        herdr.candidate_pane.return_value = "w1:p1"

        hydrated = hydrate_adoption_panes(
            herdr,
            [selected_space, unrelated_space],
            origin=ORIGIN,
            session_ids={selected.session_id},
        )

        herdr.candidate_pane.assert_called_once_with("w1", selected.session_id)
        self.assertEqual(hydrated[0].candidate_pane_id, "w1:p1")
        self.assertEqual(hydrated[1].candidate_pane_id, "w2:p1")


class PickerSnapshotTest(unittest.TestCase):
    def test_snapshot_preserves_open_fields_and_maps_attention(self) -> None:
        value = {
            "id": "conv_one",
            "title": "Prompt label",
            "agent": "Codex",
            "status": "idle",
            "attention": True,
            "runner_online": False,
            "pinned": True,
            "project": "Omnigent",
            "workspace": "/repos/omnigent",
            "updated_at": 123,
        }

        parsed = session_from_picker_record(value, expected_id="conv_one")

        self.assertEqual(parsed.session_id, "conv_one")
        self.assertEqual(parsed.title, "Prompt label")
        self.assertEqual(parsed.workspace, "/repos/omnigent")
        self.assertEqual(parsed.pending_elicitations, 1)
        self.assertTrue(parsed.pinned)

    def test_snapshot_rejects_malformed_or_mismatched_identity(self) -> None:
        for value, message in [
            ([], "JSON object"),
            ({"id": "other", "status": "idle"}, "does not match"),
            ({"id": "conv_one", "status": "paused"}, "unknown status"),
        ]:
            with self.subTest(value=value), self.assertRaisesRegex(SyncError, message):
                session_from_picker_record(value, expected_id="conv_one")


class LoadedTitleSyncTest(unittest.TestCase):
    def test_renames_only_the_exact_managed_workspace(self) -> None:
        item = session("managed", title="Generated title")
        managed = workspace(item, workspace_id="w1", label="Provisional title")
        ordinary = WorkspaceSummary("w2", "My shell", "idle", {})
        herdr = MagicMock(spec=HerdrClient)

        renamed = sync_loaded_workspace_titles(
            [item],
            [managed, ordinary],
            {item.session_id: Binding("w1", "w1:p1")},
            origin=ORIGIN,
            herdr=herdr,
        )

        self.assertEqual(renamed, 1)
        herdr.rename_workspace.assert_called_once_with("w1", "Generated title")

    def test_missing_title_and_ambiguous_bindings_never_rename(self) -> None:
        untitled = replace(session("untitled"), title=None)
        first = workspace(untitled, workspace_id="w1", label="Prompt label")
        ambiguous = session("ambiguous", title="Generated")
        second = workspace(
            ambiguous,
            workspace_id="w2",
            label="Prompt label",
        )
        third = workspace(ambiguous, workspace_id="w3", label="Prompt label")
        stale_binding = session("stale-binding", title="Must not rename")
        ordinary = WorkspaceSummary("w4", "My shell", "idle", {})
        herdr = MagicMock(spec=HerdrClient)

        renamed = sync_loaded_workspace_titles(
            [untitled, ambiguous, stale_binding],
            [first, second, third, ordinary],
            {stale_binding.session_id: Binding("w4", "w4:p1")},
            origin=ORIGIN,
            herdr=herdr,
        )

        self.assertEqual(renamed, 0)
        herdr.rename_workspace.assert_not_called()


class SynchronizeOnceTest(unittest.TestCase):
    def test_picker_open_reconciles_only_the_selected_session(self) -> None:
        selected = session("selected", status="idle", online=False)
        omnigent = MagicMock(spec=OmnigentClient)
        omnigent.base_url = "https://omnigent.example"
        omnigent.get_session.return_value = selected
        herdr = MagicMock(spec=HerdrClient)
        herdr.list_workspaces.return_value = []

        with (
            tempfile.TemporaryDirectory() as directory,
            patch("session_space_sync.plan_reconciliation", return_value=[]) as planner,
            redirect_stdout(StringIO()),
        ):
            synchronize_once(
                omnigent=omnigent,
                herdr=herdr,
                state_store=StateStore(Path(directory) / "state.json"),
                origin=ORIGIN,
                reopen=set(),
                open_session=selected.session_id,
                fallback_cwd=Path(directory),
                omnigent_executable="/opt/omnigent",
                dry_run=False,
            )

        planned_sessions = planner.call_args.args[0]
        self.assertEqual(
            [item.session_id for item in planned_sessions],
            [selected.session_id],
        )
        omnigent.get_session.assert_called_once_with(selected.session_id)
        omnigent.list_recent_sessions.assert_not_called()

    def test_picker_snapshot_skips_redundant_session_lookup(self) -> None:
        selected = session("selected", title="Fresh picker title", status="idle", online=False)
        omnigent = MagicMock(spec=OmnigentClient)
        omnigent.base_url = "https://omnigent.example"
        herdr = MagicMock(spec=HerdrClient)
        herdr.list_workspaces.return_value = []

        with (
            tempfile.TemporaryDirectory() as directory,
            patch("session_space_sync.plan_reconciliation", return_value=[]) as planner,
            redirect_stdout(StringIO()),
        ):
            synchronize_once(
                omnigent=omnigent,
                herdr=herdr,
                state_store=StateStore(Path(directory) / "state.json"),
                origin=ORIGIN,
                reopen=set(),
                open_session=selected.session_id,
                open_session_record=selected,
                fallback_cwd=Path(directory),
                omnigent_executable="/opt/omnigent",
                dry_run=False,
            )

        omnigent.get_session.assert_not_called()
        self.assertEqual(planner.call_args.args[0], [selected])

    def test_mismatched_picker_snapshot_fails_closed(self) -> None:
        with self.assertRaisesRegex(SyncError, "does not match"):
            synchronize_once(
                omnigent=MagicMock(spec=OmnigentClient),
                herdr=MagicMock(spec=HerdrClient),
                state_store=MagicMock(spec=StateStore),
                origin=ORIGIN,
                reopen=set(),
                open_session="wanted",
                open_session_record=session("different"),
                fallback_cwd=Path("/tmp"),
                omnigent_executable="/opt/omnigent",
                dry_run=False,
            )


class StateStoreTest(unittest.TestCase):
    def test_round_trip_preserves_binding_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            store = StateStore(path)
            original = {
                "conv_one": Binding(
                    "w1",
                    "w1:p1",
                    locally_closed=True,
                    launcher_started=False,
                    launcher_version=LAUNCHER_VERSION,
                )
            }

            store.save(ORIGIN, original)
            loaded = StateStore(path).bindings(ORIGIN)

            self.assertEqual(loaded, original)

    def test_pre_open_attach_started_field_is_migrated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "origins": {
                            ORIGIN: {
                                "bindings": {
                                    "conv_one": {
                                        "workspace_id": "w1",
                                        "pane_id": "w1:p1",
                                        "locally_closed": False,
                                        "attach_started": True,
                                    }
                                }
                            }
                        },
                    }
                )
            )

            loaded = StateStore(path).bindings(ORIGIN)

            self.assertEqual(
                loaded["conv_one"],
                Binding("w1", "w1:p1", launcher_started=True),
            )

    def test_invalid_state_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps({"version": 99, "origins": {}}))

            with self.assertRaisesRegex(SyncError, "unsupported state"):
                StateStore(path)

    def test_lock_reloads_state_before_updating_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            first = StateStore(path)
            stale = StateStore(path)

            with first.locked():
                first.save("origin-a", {"conv_a": Binding("w1", "w1:p1")})
            with stale.locked():
                stale.save("origin-b", {"conv_b": Binding("w2", "w2:p1")})

            final = StateStore(path)
            self.assertEqual(final.bindings("origin-a")["conv_a"].workspace_id, "w1")
            self.assertEqual(final.bindings("origin-b")["conv_b"].workspace_id, "w2")


class ApplyPlanTest(unittest.TestCase):
    def test_picker_selection_creates_focused_space_and_runs_open(self) -> None:
        item = session()
        herdr = MagicMock(spec=HerdrClient)
        herdr.create_workspace.return_value = Binding(
            "w1",
            "w1:p1",
            launcher_started=False,
        )
        bindings: dict[str, Binding] = {}

        with tempfile.TemporaryDirectory() as directory, patch("builtins.print"):
            store = StateStore(Path(directory) / "state.json")
            apply_plan(
                [ReconcileAction("create", item.session_id)],
                sessions=[item],
                bindings=bindings,
                origin=ORIGIN,
                state_store=store,
                herdr=herdr,
                base_url="https://omnigent.example",
                fallback_cwd=Path("/tmp"),
                omnigent_executable="/opt/omnigent",
                activate={item.session_id},
                dry_run=False,
            )

        self.assertTrue(herdr.create_workspace.call_args.kwargs["focus"])
        herdr.launch_open.assert_called_once_with(
            "w1:p1",
            "w1",
            item.session_id,
            base_url="https://omnigent.example",
            omnigent_executable="/opt/omnigent",
            initial_label=item.label,
        )
        herdr.report_agent.assert_called_once()
        reported_tokens = herdr.report_metadata.call_args.args[1]
        self.assertEqual(
            reported_tokens[TOKEN_LAUNCHER],
            LAUNCHER_VERSION,
        )
        self.assertTrue(bindings[item.session_id].launcher_started)
        self.assertEqual(
            bindings[item.session_id].launcher_version,
            LAUNCHER_VERSION,
        )

    def test_adopting_legacy_tui_records_it_as_already_started(self) -> None:
        item = session()
        herdr = MagicMock(spec=HerdrClient)
        bindings: dict[str, Binding] = {}

        with tempfile.TemporaryDirectory() as directory, patch("builtins.print"):
            apply_plan(
                [
                    ReconcileAction(
                        "adopt",
                        item.session_id,
                        workspace_id="w1",
                        pane_id="w1:p1",
                        launcher_version=None,
                    )
                ],
                sessions=[item],
                bindings=bindings,
                origin=ORIGIN,
                state_store=StateStore(Path(directory) / "state.json"),
                herdr=herdr,
                base_url="https://omnigent.example",
                fallback_cwd=Path(directory),
                omnigent_executable="/opt/omnigent",
                activate=set(),
                dry_run=False,
            )

        self.assertTrue(bindings[item.session_id].launcher_started)
        herdr.launch_open.assert_not_called()

    def test_legacy_metadata_apply_omits_launcher_token(self) -> None:
        item = session()
        herdr = MagicMock(spec=HerdrClient)
        bindings = {
            item.session_id: Binding(
                "w1",
                "w1:p1",
                launcher_started=True,
                launcher_version=None,
            )
        }

        with tempfile.TemporaryDirectory() as directory, patch("builtins.print"):
            apply_plan(
                [
                    ReconcileAction(
                        "metadata",
                        item.session_id,
                        workspace_id="w1",
                        pane_id="w1:p1",
                        include_launcher_token=False,
                    )
                ],
                sessions=[item],
                bindings=bindings,
                origin=ORIGIN,
                state_store=StateStore(Path(directory) / "state.json"),
                herdr=herdr,
                base_url="https://omnigent.example",
                fallback_cwd=Path(directory),
                omnigent_executable="/opt/omnigent",
                activate=set(),
                dry_run=False,
            )

        tokens = herdr.report_metadata.call_args.args[1]
        self.assertNotIn(TOKEN_LAUNCHER, tokens)


class HerdrCliContractTest(unittest.TestCase):
    @patch("session_space_sync.subprocess.run")
    def test_create_extracts_workspace_and_root_pane_from_single_response(
        self, run: object
    ) -> None:
        payload = {
            "id": "cli:workspace:create",
            "result": {
                "type": "workspace_created",
                "workspace": {"workspace_id": "w7"},
                "root_pane": {"pane_id": "w7:p1"},
                "tab": {"tab_id": "w7:t1"},
            },
        }
        run.return_value = subprocess.CompletedProcess(  # type: ignore[attr-defined]
            args=[], returncode=0, stdout=json.dumps(payload), stderr=""
        )
        client = HerdrClient("/opt/herdr", "omnigent-spike")

        binding = client.create_workspace(
            session(),
            base_url="https://omnigent.example",
            fallback_cwd=Path("/tmp"),
        )

        self.assertEqual(
            binding,
            Binding("w7", "w7:p1", launcher_started=False),
        )
        command = run.call_args.args[0]  # type: ignore[attr-defined]
        self.assertEqual(command[:3], ["/opt/herdr", "--session", "omnigent-spike"])
        self.assertIn("workspace", command)
        self.assertIn("OMNIGENT_SESSION_ID=conv_one", command)
        self.assertIn("--no-focus", command)

    @patch("session_space_sync.subprocess.run")
    def test_successful_no_output_mutation_is_accepted(self, run: object) -> None:
        run.return_value = subprocess.CompletedProcess(  # type: ignore[attr-defined]
            args=[], returncode=0, stdout="", stderr=""
        )
        client = HerdrClient("herdr")

        client.report_metadata("w1", {"omnigent_session": "conv_one"})

        command = run.call_args.args[0]  # type: ignore[attr-defined]
        self.assertEqual(command[:4], ["herdr", "workspace", "report-metadata", "w1"])

    @patch("session_space_sync.subprocess.run")
    def test_open_command_shell_quotes_each_dynamic_value(self, run: object) -> None:
        run.return_value = subprocess.CompletedProcess(  # type: ignore[attr-defined]
            args=[], returncode=0, stdout="", stderr=""
        )
        client = HerdrClient("herdr")

        client.launch_open(
            "w1:p1",
            "w harmless; printf BAD",
            "conv harmless; printf BAD",
            base_url="https://example.invalid/path with space",
            omnigent_executable="/opt/Omnigent Bin/omnigent",
            initial_label="Title harmless; printf BAD",
        )

        command = run.call_args.args[0]  # type: ignore[attr-defined]
        self.assertEqual(command[:4], ["herdr", "pane", "run", "w1:p1"])
        self.assertEqual(
            shlex.split(command[4]),
            [
                sys.executable,
                str(Path(__file__).with_name("session_open_supervisor.py").resolve()),
                "--session",
                "conv harmless; printf BAD",
                "--workspace",
                "w harmless; printf BAD",
                "--server",
                "https://example.invalid/path with space",
                "--herdr",
                "herdr",
                "--omnigent",
                "/opt/Omnigent Bin/omnigent",
                "--initial-label",
                "Title harmless; printf BAD",
            ],
        )


class OmnigentSessionListTest(unittest.TestCase):
    @patch("omnigent.cli._host_http_json")
    def test_stored_sessions_include_confirmed_liveness_and_picker_metadata(
        self, request: object
    ) -> None:
        def row(
            session_id: str,
            runner_id: str | None,
            online: bool | None,
        ) -> dict[str, object]:
            return {
                "id": session_id,
                "title": session_id,
                "agent_name": "Codex",
                "status": "idle",
                "pending_elicitations_count": 0,
                "runner_id": runner_id,
                "runner_online": online,
                "host_id": "host_one",
                "host_online": True,
                "updated_at": 1_725_000_000,
                "labels": {"omnigent.pinned": ""} if session_id == "online" else {},
                "project_id": "proj_123",
                "workspace": "/repos/omnigent",
            }

        request.side_effect = [  # type: ignore[attr-defined]
            SimpleNamespace(
                status_code=200,
                body={
                    "data": [
                        row("online", "runner_online", True),
                        row("offline", "runner_offline", False),
                        row("unknown", "runner_unknown", None),
                        row("runnerless", None, None),
                    ],
                    "has_more": False,
                    "last_id": "runnerless",
                },
            ),
            SimpleNamespace(status_code=200, body={"online": True}),
            SimpleNamespace(
                status_code=200,
                body={
                    "object": "list",
                    "data": [{"id": "proj_123", "name": "Omnigent Product", "config": {}}],
                },
            ),
        ]

        sessions = OmnigentClient("http://127.0.0.1:55777", 20).list_recent_sessions()

        self.assertEqual(
            [item.session_id for item in sessions],
            ["online", "offline", "unknown", "runnerless"],
        )
        self.assertEqual(
            [item.runner_online for item in sessions],
            [True, False, True, False],
        )
        self.assertTrue(sessions[0].pinned)
        self.assertEqual(sessions[0].project_id, "proj_123")
        self.assertEqual(sessions[0].project_label, "Omnigent Product")
        paths = [call.kwargs["path"] for call in request.call_args_list]  # type: ignore[attr-defined]
        self.assertEqual(
            paths,
            [
                "/v1/sessions",
                "/v1/runners/runner_unknown/status",
                "/v1/projects",
            ],
        )
        params = request.call_args_list[0].kwargs["params"]  # type: ignore[attr-defined]
        self.assertEqual(params["kind"], "default")
        self.assertEqual(params["include_archived"], "false")

    @patch("omnigent.cli._host_http_json")
    def test_server_side_search_and_pin_filter_are_forwarded(self, request: object) -> None:
        request.return_value = SimpleNamespace(  # type: ignore[attr-defined]
            status_code=200,
            body={"data": [], "has_more": False, "last_id": None},
        )

        OmnigentClient("https://omnigent.example", 20).list_recent_sessions(
            search_query="deployment failure",
            pinned=True,
        )

        params = request.call_args.kwargs["params"]  # type: ignore[attr-defined]
        self.assertEqual(params["search_query"], "deployment failure")
        self.assertEqual(params["pinned"], "true")

    @patch("omnigent.cli._host_http_json")
    def test_fast_catalog_skips_per_runner_liveness_requests(self, request: object) -> None:
        request.return_value = SimpleNamespace(  # type: ignore[attr-defined]
            status_code=200,
            body={
                "data": [
                    {
                        "id": "stored",
                        "status": "idle",
                        "runner_id": "runner_one",
                    }
                ],
                "has_more": False,
                "last_id": "stored",
            },
        )

        sessions = OmnigentClient("https://omnigent.example", 20).list_recent_sessions(
            resolve_liveness=False
        )

        self.assertFalse(sessions[0].runner_online)
        self.assertEqual(request.call_count, 1)  # type: ignore[attr-defined]
        self.assertEqual(  # type: ignore[attr-defined]
            request.call_args.kwargs["path"],
            "/v1/sessions",
        )

    @patch("omnigent.cli._host_http_json")
    def test_project_name_prefers_first_class_then_legacy_without_workspace_guess(
        self, request: object
    ) -> None:
        def row(
            session_id: str,
            *,
            project_id: str | None,
            legacy_project: str | None,
        ) -> dict[str, object]:
            return {
                "id": session_id,
                "status": "idle",
                "runner_online": False,
                "project_id": project_id,
                "labels": {"omni_project": legacy_project} if legacy_project else {},
                "workspace": f"/repos/{session_id}-workspace",
            }

        request.side_effect = [  # type: ignore[attr-defined]
            SimpleNamespace(
                status_code=200,
                body={
                    "data": [
                        row(
                            "first_class",
                            project_id="proj_one",
                            legacy_project="Stale Legacy Name",
                        ),
                        row("legacy", project_id=None, legacy_project="Legacy Project"),
                        row("unfiled", project_id=None, legacy_project=None),
                    ],
                    "has_more": False,
                    "last_id": "unfiled",
                },
            ),
            SimpleNamespace(
                status_code=200,
                body={
                    "object": "list",
                    "data": [{"id": "proj_one", "name": "First-class Project"}],
                },
            ),
        ]

        sessions = OmnigentClient("https://omnigent.example", 20).list_recent_sessions()

        self.assertEqual(
            [item.project_label for item in sessions],
            ["First-class Project", "Legacy Project", None],
        )
        self.assertNotEqual(sessions[2].project_label, "unfiled-workspace")

    @patch("omnigent.cli._host_http_json")
    def test_project_catalog_failure_keeps_sessions_and_legacy_fallback(
        self, request: object
    ) -> None:
        request.side_effect = [  # type: ignore[attr-defined]
            SimpleNamespace(
                status_code=200,
                body={
                    "data": [
                        {
                            "id": "stored",
                            "status": "idle",
                            "runner_online": False,
                            "project_id": "proj_unavailable",
                            "labels": {"omni_project": "Legacy Fallback"},
                        }
                    ],
                    "has_more": False,
                    "last_id": "stored",
                },
            ),
            SimpleNamespace(
                status_code=503,
                body={"error": {"message": "project store unavailable"}},
            ),
        ]
        errors = StringIO()

        with redirect_stderr(errors):
            sessions = OmnigentClient("https://omnigent.example", 20).list_recent_sessions()

        self.assertEqual(sessions[0].project_label, "Legacy Fallback")
        self.assertIn("project catalog warning", errors.getvalue())

    @patch("omnigent.cli._host_http_json")
    def test_explicit_lookup_quotes_id_and_exposes_full_recovery_state(
        self, request: object
    ) -> None:
        request.return_value = SimpleNamespace(  # type: ignore[attr-defined]
            status_code=200,
            body={
                "id": "conv/older ?",
                "status": "idle",
                "runner_id": "runner_one",
                "host_id": "host_one",
                "runner_online": False,
                "host_online": False,
                "host_resumable": True,
            },
        )

        item = OmnigentClient("https://omnigent.example", 20).get_session("conv/older ?")

        self.assertEqual(
            request.call_args.kwargs["path"],  # type: ignore[attr-defined]
            "/v1/sessions/conv%2Folder%20%3F",
        )
        self.assertEqual(item.runner_id, "runner_one")
        self.assertEqual(item.host_id, "host_one")
        self.assertTrue(item.host_resumable)
        self.assertEqual(item.recovery_hint, "resume")


class OmnigentSessionCreateTest(unittest.TestCase):
    @patch(
        "omnigent.host.identity.load_host_identity_if_present",
        return_value=SimpleNamespace(host_id="host_from_effective_config"),
    )
    @patch(
        "omnigent.cli._effective_global_config_path",
        return_value=Path("/custom/omnigent/config.yaml"),
    )
    def test_host_identity_uses_validated_read_only_effective_config_loader(
        self,
        effective_path: object,
        load_identity: object,
    ) -> None:
        self.assertEqual(resolve_existing_host_id(), "host_from_effective_config")
        effective_path.assert_called_once_with()  # type: ignore[attr-defined]
        load_identity.assert_called_once_with(  # type: ignore[attr-defined]
            Path("/custom/omnigent/config.yaml")
        )

    @patch("omnigent.cli._host_http_json")
    def test_create_agent_catalog_is_paginated_and_native_only(self, request: object) -> None:
        request.side_effect = [  # type: ignore[attr-defined]
            SimpleNamespace(
                status_code=200,
                body={
                    "data": [
                        {
                            "id": "agent_codex",
                            "name": "codex-native-ui",
                            "harness": "codex-native",
                            "builtin": True,
                        },
                        {
                            "id": "agent_custom_native",
                            "name": "custom-codex-template",
                            "harness": "codex-native",
                            "builtin": True,
                        },
                        {
                            "id": "agent_operator_impostor",
                            "name": "codex-native-ui",
                            "harness": "codex-native",
                            "builtin": False,
                        },
                    ],
                    "has_more": True,
                    "last_id": "agent_operator_impostor",
                },
            ),
            SimpleNamespace(
                status_code=200,
                body={
                    "data": [
                        {
                            "id": "agent_claude",
                            "name": "claude-native-ui",
                            "harness": "claude-native",
                            "builtin": True,
                        }
                    ],
                    "has_more": False,
                    "last_id": "agent_claude",
                },
            ),
        ]

        agents = OmnigentClient("https://omnigent.example", 20).list_create_agents()

        self.assertEqual(
            agents,
            [
                {
                    "id": "agent_codex",
                    "name": "codex-native-ui",
                    "display_name": "Codex",
                    "harness": "codex-native",
                },
                {
                    "id": "agent_claude",
                    "name": "claude-native-ui",
                    "display_name": "Claude",
                    "harness": "claude-native",
                },
            ],
        )
        calls = request.call_args_list  # type: ignore[attr-defined]
        self.assertEqual(calls[0].kwargs["params"], {"limit": 1000, "order": "asc"})
        self.assertEqual(calls[1].kwargs["params"]["after"], "agent_operator_impostor")

    @patch("omnigent.cli._host_http_json")
    def test_agent_pagination_rejects_repeated_cursor(self, request: object) -> None:
        page = SimpleNamespace(
            status_code=200,
            body={"data": [], "has_more": True, "last_id": "same"},
        )
        request.side_effect = [page, page]  # type: ignore[attr-defined]

        with self.assertRaisesRegex(SyncError, "invalid/repeated cursor"):
            OmnigentClient("https://omnigent.example", 20).list_create_agents()

    @patch("session_space_sync.resolve_existing_host_id", return_value="host_local")
    @patch("omnigent.cli._host_http_json")
    def test_create_routes_exact_payload_once_with_long_timeout(
        self,
        request: object,
        _load_host_id: object,
    ) -> None:
        request.side_effect = [  # type: ignore[attr-defined]
            SimpleNamespace(
                status_code=200,
                body={
                    "data": [
                        {
                            "id": "agent_codex",
                            "name": "codex-native-ui",
                            "harness": "codex-native",
                            "builtin": True,
                        }
                    ],
                    "has_more": False,
                    "last_id": "agent_codex",
                },
            ),
            SimpleNamespace(
                status_code=200,
                body={
                    "hosts": [
                        {"host_id": "host_other", "status": "online"},
                        # Missing readiness is intentionally tolerated for an
                        # older server/host report.
                        {"host_id": "host_local", "status": "online"},
                    ]
                },
            ),
            SimpleNamespace(
                status_code=201,
                body={
                    "id": "created_one",
                    "title": None,
                    "agent_name": "Codex",
                    "status": "idle",
                    "pending_elicitations_count": 0,
                    "workspace": "/tmp/project",
                    "host_id": "host_local",
                    "runner_online": False,
                    "labels": {
                        "omnigent.ui": "terminal",
                        "omnigent.wrapper": "codex-native-ui",
                    },
                },
            ),
        ]

        with tempfile.TemporaryDirectory() as directory:
            item = OmnigentClient("https://omnigent.example", 20).create_session(
                "agent_codex", workspace=Path(directory)
            )
            expected_workspace = str(Path(directory).resolve())

        self.assertEqual(item.session_id, "created_one")
        calls = request.call_args_list  # type: ignore[attr-defined]
        self.assertEqual(
            [call.kwargs["path"] for call in calls],
            [
                "/v1/agents",
                "/v1/hosts",
                "/v1/sessions",
            ],
        )
        create_call = calls[2]
        self.assertEqual(create_call.kwargs["method"], "POST")
        self.assertEqual(create_call.kwargs["host_id"], "host_local")
        self.assertEqual(create_call.kwargs["timeout_s"], 120.0)
        self.assertEqual(
            create_call.kwargs["json_body"],
            {
                "agent_id": "agent_codex",
                "host_id": "host_local",
                "workspace": expected_workspace,
                "labels": {
                    "omnigent.ui": "terminal",
                    "omnigent.wrapper": "codex-native-ui",
                },
            },
        )

    @patch("session_space_sync.resolve_existing_host_id", return_value="host_local")
    @patch("omnigent.cli._host_http_json")
    def test_create_never_falls_back_to_another_online_host(
        self,
        request: object,
        _load_host_id: object,
    ) -> None:
        request.side_effect = [  # type: ignore[attr-defined]
            self._agent_page(),
            SimpleNamespace(
                status_code=200,
                body={
                    "hosts": [
                        {"host_id": "host_local", "status": "offline"},
                        {"host_id": "host_other", "status": "online"},
                    ]
                },
            ),
        ]

        with (
            tempfile.TemporaryDirectory() as directory,
            self.assertRaisesRegex(SyncError, "host_local.*not online"),
        ):
            OmnigentClient("https://omnigent.example", 20).create_session(
                "agent_codex", workspace=Path(directory)
            )

        self.assertEqual(request.call_count, 2)  # type: ignore[attr-defined]

    @patch("session_space_sync.resolve_existing_host_id", return_value="host_local")
    @patch("omnigent.cli._host_http_json")
    def test_create_rejects_explicitly_unavailable_host_harness(
        self,
        request: object,
        _load_host_id: object,
    ) -> None:
        for unavailable in (False, "needs-auth", "future-unavailable-reason"):
            with self.subTest(unavailable=unavailable):
                request.reset_mock()  # type: ignore[attr-defined]
                request.side_effect = [  # type: ignore[attr-defined]
                    self._agent_page(),
                    SimpleNamespace(
                        status_code=200,
                        body={
                            "hosts": [
                                {
                                    "host_id": "host_local",
                                    "status": "online",
                                    "configured_harnesses": {"codex-native": unavailable},
                                }
                            ]
                        },
                    ),
                ]
                with (
                    tempfile.TemporaryDirectory() as directory,
                    self.assertRaisesRegex(SyncError, "cannot launch"),
                ):
                    OmnigentClient("https://omnigent.example", 20).create_session(
                        "agent_codex", workspace=Path(directory)
                    )
                self.assertEqual(request.call_count, 2)  # type: ignore[attr-defined]

    @patch("session_space_sync.resolve_existing_host_id", return_value="host_local")
    @patch("omnigent.cli._host_http_json")
    def test_transport_failure_reports_unknown_outcome_without_retry(
        self,
        request: object,
        _load_host_id: object,
    ) -> None:
        request.side_effect = [  # type: ignore[attr-defined]
            self._agent_page(),
            self._online_host_page(),
            SimpleNamespace(status_code=0, body="ReadTimeout: timed out"),
        ]

        with (
            tempfile.TemporaryDirectory() as directory,
            self.assertRaisesRegex(
                SyncError,
                "outcome is unknown.*Refresh/search.*do not retry",
            ),
        ):
            OmnigentClient("https://omnigent.example", 20).create_session(
                "agent_codex", workspace=Path(directory)
            )

        self.assertEqual(request.call_count, 3)  # type: ignore[attr-defined]

    @patch("session_space_sync.resolve_existing_host_id", return_value="host_local")
    @patch("omnigent.cli._host_http_json")
    def test_create_rejects_error_and_malformed_success_responses(
        self,
        request: object,
        _load_host_id: object,
    ) -> None:
        cases = [
            (
                SimpleNamespace(status_code=503, body={"detail": "busy"}),
                r"creation failed \(503\): busy",
            ),
            (
                SimpleNamespace(status_code=201, body="not json"),
                "malformed response",
            ),
            (
                SimpleNamespace(status_code=201, body={"status": "idle"}),
                "no valid session id",
            ),
        ]
        for post_response, message in cases:
            with self.subTest(message=message):
                request.reset_mock()  # type: ignore[attr-defined]
                request.side_effect = [  # type: ignore[attr-defined]
                    self._agent_page(),
                    self._online_host_page(),
                    post_response,
                ]
                with (
                    tempfile.TemporaryDirectory() as directory,
                    self.assertRaisesRegex(SyncError, message),
                ):
                    OmnigentClient("https://omnigent.example", 20).create_session(
                        "agent_codex", workspace=Path(directory)
                    )
                self.assertEqual(request.call_count, 3)  # type: ignore[attr-defined]

    @staticmethod
    def _agent_page() -> SimpleNamespace:
        return SimpleNamespace(
            status_code=200,
            body={
                "data": [
                    {
                        "id": "agent_codex",
                        "name": "codex-native-ui",
                        "harness": "codex-native",
                        "builtin": True,
                    }
                ],
                "has_more": False,
                "last_id": "agent_codex",
            },
        )

    @staticmethod
    def _online_host_page() -> SimpleNamespace:
        return SimpleNamespace(
            status_code=200,
            body={"hosts": [{"host_id": "host_local", "status": "online"}]},
        )


class OmnigentSessionMessageTest(unittest.TestCase):
    @patch("session_space_sync.resolve_existing_host_id", return_value="host_local")
    @patch("omnigent.cli._host_http_json")
    def test_send_routes_exact_message_payload_once(
        self,
        request: object,
        _load_host_id: object,
    ) -> None:
        response = {
            "queued": True,
            "pending_id": "pending_one",
        }
        request.return_value = SimpleNamespace(  # type: ignore[attr-defined]
            status_code=202,
            body=response,
        )
        prompt = "Investigate the session bridge\nKeep this second line."

        result = OmnigentClient("https://omnigent.example", 20).send_message(
            "conv/one ?",
            prompt,
        )

        self.assertEqual(result, response)
        request.assert_called_once_with(  # type: ignore[attr-defined]
            base_url="https://omnigent.example",
            method="POST",
            path="/v1/sessions/conv%2Fone%20%3F/events",
            json_body={
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": prompt}],
                },
            },
            timeout_s=120.0,
            host_id="host_local",
        )

    @patch("omnigent.cli._host_http_json")
    def test_send_rejects_blank_prompt_before_network(self, request: object) -> None:
        with self.assertRaisesRegex(SyncError, "stdin.*non-whitespace"):
            OmnigentClient("https://omnigent.example", 20).send_message(
                "conv_one",
                " \n\t ",
            )

        request.assert_not_called()  # type: ignore[attr-defined]

    @patch("session_space_sync.resolve_existing_host_id", return_value="host_local")
    @patch("omnigent.cli._host_http_json")
    def test_send_transport_failure_reports_ambiguous_outcome_without_retry(
        self,
        request: object,
        _load_host_id: object,
    ) -> None:
        request.return_value = SimpleNamespace(  # type: ignore[attr-defined]
            status_code=0,
            body="ReadTimeout: timed out",
        )

        with self.assertRaisesRegex(
            SyncError,
            "outcome is unknown.*may already have received.*do not retry automatically",
        ):
            OmnigentClient("https://omnigent.example", 20).send_message(
                "conv_one",
                "Start the task",
            )

        self.assertEqual(request.call_count, 1)  # type: ignore[attr-defined]

    @patch("session_space_sync.resolve_existing_host_id", return_value="host_local")
    @patch("omnigent.cli._host_http_json")
    def test_send_requires_valid_202_queued_response(
        self,
        request: object,
        _load_host_id: object,
    ) -> None:
        cases = [
            (
                SimpleNamespace(status_code=200, body={"queued": True}),
                r"failed \(200\)",
            ),
            (
                SimpleNamespace(status_code=202, body="not json"),
                "malformed response",
            ),
            (
                SimpleNamespace(
                    status_code=202,
                    body={"queued": False, "denied": True, "reason": "blocked"},
                ),
                "not queued",
            ),
            (
                SimpleNamespace(status_code=202, body={}),
                "not queued",
            ),
        ]
        for response, message in cases:
            with self.subTest(message=message):
                request.reset_mock()  # type: ignore[attr-defined]
                request.return_value = response  # type: ignore[attr-defined]
                with self.assertRaisesRegex(SyncError, message):
                    OmnigentClient("https://omnigent.example", 20).send_message(
                        "conv_one",
                        "Start the task",
                    )
                self.assertEqual(request.call_count, 1)  # type: ignore[attr-defined]


class MainTest(unittest.TestCase):
    def test_list_sessions_outputs_catalog_without_constructing_herdr_or_state(self) -> None:
        output = StringIO()
        listed = session("stored", status="idle", online=False)
        with (
            patch(
                "session_space_sync.resolve_server_url",
                return_value="https://omnigent.example",
            ),
            patch("session_space_sync.OmnigentClient") as omnigent_type,
            patch("session_space_sync.HerdrClient") as herdr_type,
            patch("session_space_sync.StateStore") as state_type,
            redirect_stdout(output),
        ):
            omnigent_type.return_value.list_recent_sessions.return_value = [listed]

            exit_code = main(["--server", "https://omnigent.example", "--list-sessions"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(output.getvalue())[0]["id"], "stored")
        herdr_type.assert_not_called()
        state_type.assert_not_called()

    def test_list_create_agents_outputs_json_without_herdr_or_state(self) -> None:
        output = StringIO()
        agents = [
            {
                "id": "agent_codex",
                "name": "codex-native-ui",
                "display_name": "Codex",
                "harness": "codex-native",
            }
        ]
        with (
            patch(
                "session_space_sync.resolve_server_url",
                return_value="https://omnigent.example",
            ),
            patch("session_space_sync.OmnigentClient") as omnigent_type,
            patch("session_space_sync.HerdrClient") as herdr_type,
            patch("session_space_sync.StateStore") as state_type,
            redirect_stdout(output),
        ):
            omnigent_type.return_value.list_create_agents.return_value = agents

            exit_code = main(["--server", "https://omnigent.example", "--list-create-agents"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(output.getvalue()), agents)
        herdr_type.assert_not_called()
        state_type.assert_not_called()

    def test_create_session_outputs_unloaded_catalog_record_only(self) -> None:
        output = StringIO()
        created = session("created_one", status="idle", online=False)
        with tempfile.TemporaryDirectory() as directory:
            workspace_path = Path(directory).resolve()
            with (
                patch(
                    "session_space_sync.resolve_server_url",
                    return_value="https://omnigent.example",
                ),
                patch("session_space_sync.OmnigentClient") as omnigent_type,
                patch("session_space_sync.HerdrClient") as herdr_type,
                patch("session_space_sync.StateStore") as state_type,
                redirect_stdout(output),
            ):
                omnigent_type.return_value.create_session.return_value = created

                exit_code = main(
                    [
                        "--server",
                        "https://omnigent.example",
                        "--create-session",
                        "agent_codex",
                        "--cwd",
                        str(workspace_path),
                    ]
                )

        self.assertEqual(exit_code, 0)
        record = json.loads(output.getvalue())
        self.assertEqual(record["id"], "created_one")
        self.assertFalse(record["loaded"])
        omnigent_type.return_value.create_session.assert_called_once_with(
            "agent_codex",
            workspace=workspace_path,
        )
        herdr_type.assert_not_called()
        state_type.assert_not_called()

    def test_create_session_rejects_relative_or_missing_cwd_before_network(self) -> None:
        for cwd, message in [
            (Path("relative"), "absolute path"),
            (Path("/definitely/missing/omnigent-spike"), "does not exist"),
        ]:
            with self.subTest(cwd=cwd):
                errors = StringIO()
                with (
                    patch("session_space_sync.resolve_server_url") as resolve,
                    redirect_stderr(errors),
                ):
                    exit_code = main(["--create-session", "agent_codex", "--cwd", str(cwd)])
                self.assertEqual(exit_code, 1)
                self.assertIn(message, errors.getvalue())
                resolve.assert_not_called()

    def test_send_message_reads_prompt_from_stdin_without_herdr_or_state(self) -> None:
        output = StringIO()
        prompt = "Investigate title propagation\nwithout putting this in argv."
        response = {"queued": True, "pending_id": "pending_one"}
        with (
            patch("session_space_sync.sys.stdin", StringIO(prompt)),
            patch(
                "session_space_sync.resolve_server_url",
                return_value="https://omnigent.example",
            ),
            patch("session_space_sync.OmnigentClient") as omnigent_type,
            patch("session_space_sync.HerdrClient") as herdr_type,
            patch("session_space_sync.StateStore") as state_type,
            redirect_stdout(output),
        ):
            omnigent_type.return_value.send_message.return_value = response

            exit_code = main(
                [
                    "--server",
                    "https://omnigent.example",
                    "--send-message",
                    "conv_one",
                ]
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(output.getvalue()), response)
        omnigent_type.return_value.send_message.assert_called_once_with("conv_one", prompt)
        herdr_type.assert_not_called()
        state_type.assert_not_called()

    def test_send_message_rejects_blank_stdin_before_network(self) -> None:
        errors = StringIO()
        with (
            patch("session_space_sync.sys.stdin", StringIO(" \n\t")),
            patch("session_space_sync.resolve_server_url") as resolve,
            redirect_stderr(errors),
        ):
            exit_code = main(["--send-message", "conv_one"])

        self.assertEqual(exit_code, 1)
        self.assertIn("stdin", errors.getvalue())
        self.assertIn("non-whitespace", errors.getvalue())
        resolve.assert_not_called()

    def test_open_session_accepts_fresh_picker_snapshot_from_stdin(self) -> None:
        snapshot = {
            "id": "conv_one",
            "title": "Fresh title",
            "agent": "Codex",
            "status": "idle",
            "attention": False,
            "runner_online": False,
            "pinned": False,
            "project": None,
            "workspace": "/repos/omnigent",
            "updated_at": 123,
        }
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("session_space_sync.sys.stdin", StringIO(json.dumps(snapshot))),
            patch(
                "session_space_sync.resolve_server_url",
                return_value="https://omnigent.example",
            ),
            patch("session_space_sync.synchronize_once", return_value=0) as sync,
            redirect_stdout(StringIO()),
        ):
            exit_code = main(
                [
                    "--server",
                    "https://omnigent.example",
                    "--open-session",
                    "conv_one",
                    "--open-session-record-stdin",
                    "--state-file",
                    str(Path(directory) / "state.json"),
                ]
            )

        self.assertEqual(exit_code, 0)
        passed = sync.call_args.kwargs["open_session_record"]
        self.assertEqual(passed.session_id, "conv_one")
        self.assertEqual(passed.title, "Fresh title")

    def test_open_session_snapshot_id_mismatch_fails_before_network(self) -> None:
        errors = StringIO()
        snapshot = {"id": "another", "status": "idle"}
        with (
            patch("session_space_sync.sys.stdin", StringIO(json.dumps(snapshot))),
            patch("session_space_sync.resolve_server_url") as resolve,
            redirect_stderr(errors),
        ):
            exit_code = main(
                [
                    "--open-session",
                    "conv_one",
                    "--open-session-record-stdin",
                ]
            )

        self.assertEqual(exit_code, 1)
        self.assertIn("does not match", errors.getvalue())
        resolve.assert_not_called()

    def test_create_and_open_modes_are_mutually_exclusive(self) -> None:
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit) as raised:
            main(
                [
                    "--create-session",
                    "agent_codex",
                    "--open-session",
                    "created_one",
                ]
            )

        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
