from __future__ import annotations

import json
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from session_picker import (
    AgentRecord,
    CatalogCache,
    CatalogWorker,
    NewSessionState,
    PersistentAgentCache,
    PersistentCatalogCache,
    PickerConfig,
    PickerError,
    PickerState,
    SessionRecord,
    SessionSpaceBridge,
    _agent_row_text,
    _context_cwd,
    _initial_prompt_label,
    _merge_created_records,
    _normalize_query,
    _relative_age,
    _row_text,
    run_picker,
)


def record(
    session_id: str,
    *,
    pinned: bool = False,
    loaded: bool = False,
    active: bool = False,
    attention: bool = False,
    online: bool = False,
    updated_at: int = 100,
) -> SessionRecord:
    return SessionRecord(
        session_id=session_id,
        title=f"Title {session_id}",
        agent="Codex",
        status="running" if active else "idle",
        active=active,
        attention=attention,
        runner_online=online,
        recovery_hint="attach" if online else "reconnect",
        pinned=pinned,
        project="Omnigent",
        workspace="/repos/omnigent",
        updated_at=updated_at,
        loaded=loaded,
    )


def agent(
    agent_id: str,
    *,
    name: str | None = None,
    display_name: str | None = None,
    harness: str | None = "claude-sdk",
) -> AgentRecord:
    resolved_name = name or agent_id
    return AgentRecord(
        agent_id=agent_id,
        name=resolved_name,
        display_name=display_name or resolved_name,
        harness=harness,
    )


class AgentRecordTest(unittest.TestCase):
    def test_json_parser_sanitizes_fields_and_defaults_display_name(self) -> None:
        parsed = AgentRecord.from_json(
            {
                "id": "ag_codex",
                "name": "codex-native-ui\n",
                "display_name": "",
                "harness": "codex-native\x00",
            }
        )

        self.assertEqual(parsed.agent_id, "ag_codex")
        self.assertEqual(parsed.name, "codex-native-ui")
        self.assertEqual(parsed.display_name, "codex-native-ui")
        self.assertEqual(parsed.harness, "codex-native")
        self.assertTrue(parsed.preferred_codex)

    def test_invalid_agent_record_fails_loudly(self) -> None:
        with self.assertRaisesRegex(PickerError, "non-object"):
            AgentRecord.from_json("agent")
        with self.assertRaisesRegex(PickerError, "valid id"):
            AgentRecord.from_json({"name": "Codex"})
        with self.assertRaisesRegex(PickerError, "valid name"):
            AgentRecord.from_json({"id": "ag_codex"})

    def test_initial_prompt_label_uses_first_nonblank_line_without_changing_prompt(self) -> None:
        prompt = "\n  Diagnose the deploy  \nkeep this detail\n"

        self.assertEqual(_initial_prompt_label(prompt), "Diagnose the deploy")
        self.assertEqual(prompt, "\n  Diagnose the deploy  \nkeep this detail\n")


class SessionRecordTest(unittest.TestCase):
    def test_json_parser_sanitizes_display_text(self) -> None:
        parsed = SessionRecord.from_json(
            {
                "id": "conv_one",
                "title": "line one\nline two\x00",
                "agent": "Codex",
                "status": "idle",
                "active": False,
                "attention": False,
                "runner_online": False,
                "recovery_hint": "resume",
                "pinned": True,
                "project": "Product",
                "workspace": "/repo",
                "updated_at": 123,
                "loaded": False,
            }
        )

        self.assertEqual(parsed.title, "line one line two")
        self.assertTrue(parsed.pinned)

    def test_invalid_record_fails_with_actionable_error(self) -> None:
        with self.assertRaisesRegex(PickerError, "unknown status"):
            SessionRecord.from_json({"id": "conv_one", "status": "paused"})


class PickerStateTest(unittest.TestCase):
    def test_default_rank_is_deterministic_and_prioritizes_user_intent(self) -> None:
        state = PickerState()
        state.set_records(
            [
                record("recent", updated_at=500),
                record("loaded", loaded=True, updated_at=200),
                record("pinned", pinned=True, updated_at=100),
                record("attention", attention=True, active=True, updated_at=300),
            ]
        )

        self.assertEqual(
            [item.session_id for item in state.records],
            ["pinned", "loaded", "attention", "recent"],
        )

    def test_scope_cycle_and_selection_clamp(self) -> None:
        state = PickerState()
        state.set_records(
            [
                record("plain"),
                record("active", active=True),
                record("loaded", loaded=True),
                record("pinned", pinned=True),
            ]
        )
        state.cycle_scope()

        self.assertEqual(state.scope, "active")
        self.assertEqual(
            {item.session_id for item in state.visible_records},
            {"active", "loaded"},
        )
        state.move(99)
        self.assertEqual(state.selected_index, 1)
        state.cycle_scope()
        self.assertEqual(state.scope, "pinned")
        self.assertEqual(state.selected.session_id, "pinned")

    def test_refresh_preserves_selected_session_by_id(self) -> None:
        state = PickerState()
        state.set_records([record("one"), record("two", updated_at=200)])
        state.selected_index = 1
        selected = state.selected.session_id

        state.set_records([record("new", pinned=True), *state.records])

        self.assertEqual(state.selected.session_id, selected)

    def test_loading_or_error_never_allows_a_stale_selection_to_open(self) -> None:
        state = PickerState()
        state.set_records([record("old")])
        self.assertTrue(state.can_open)

        state.loading = True
        self.assertFalse(state.can_open)
        state.loading = False
        state.error = "search failed"
        self.assertFalse(state.can_open)


class NewSessionStateTest(unittest.TestCase):
    def load_agents(
        self,
        state: NewSessionState,
        agents: list[AgentRecord],
    ) -> None:
        generation = state.start_agent_load()
        assert generation is not None
        self.assertTrue(state.finish_agent_load(generation, agents, None))

    def test_chooser_prefers_codex_filters_and_restores_browse_context(self) -> None:
        state = NewSessionState()
        self.assertTrue(state.enter("deploy failure", "conv_selected"))
        self.load_agents(
            state,
            [
                agent("ag_claude", display_name="Claude", harness="claude-native"),
                agent(
                    "ag_codex",
                    name="codex-native-ui",
                    display_name="Codex",
                    harness="codex-native",
                ),
                agent("ag_polly", display_name="Polly"),
            ],
        )

        self.assertEqual(state.selected_agent, state.agents[1])
        state.set_query("cla native")
        self.assertEqual([item.agent_id for item in state.visible_agents], ["ag_claude"])
        self.assertEqual(state.selected_agent.agent_id, "ag_claude")

        self.assertEqual(state.leave(), ("deploy failure", "conv_selected"))
        self.assertEqual(state.phase, "browse")

    def test_chooser_uses_first_agent_when_codex_is_absent(self) -> None:
        state = NewSessionState()
        state.enter("", None)
        self.load_agents(state, [agent("ag_polly"), agent("ag_claude")])

        self.assertEqual(state.selected_agent.agent_id, "ag_polly")

    def test_stale_agent_catalog_result_is_ignored_after_back(self) -> None:
        state = NewSessionState()
        state.enter("", None)
        generation = state.start_agent_load()
        assert generation is not None
        self.assertEqual(state.leave(), ("", None))

        self.assertFalse(state.finish_agent_load(generation, [agent("ag_late")], None))
        self.assertEqual(state.phase, "browse")

    def test_create_submit_is_single_shot_and_failure_requires_leaving(self) -> None:
        state = NewSessionState()
        state.enter("", None)
        self.load_agents(state, [agent("ag_codex")])

        self.assertEqual(state.begin_compose().agent_id, "ag_codex")
        state.set_prompt("Investigate the deployment")
        submission = state.begin_create()
        assert submission is not None
        self.assertEqual(submission[0].agent_id, "ag_codex")
        self.assertEqual(submission[1], "Investigate the deployment")
        self.assertIsNone(state.begin_create())
        self.assertTrue(state.mark_create_failed("request timed out"))
        self.assertIsNone(state.begin_create())
        self.assertEqual(state.phase, "create_failed")
        self.assertEqual(state.leave(), ("", None))

    def test_created_session_open_failure_retries_open_without_create(self) -> None:
        state = NewSessionState()
        state.enter("", None)
        self.load_agents(state, [agent("ag_codex")])
        state.begin_compose()
        state.set_prompt("Fix the issue")
        state.begin_create()
        created = record("conv_created")

        self.assertTrue(state.mark_created(created))
        self.assertEqual(state.phase, "sending")
        self.assertEqual(state.mark_message_sent(), created)
        self.assertEqual(state.phase, "opening")
        self.assertTrue(state.mark_open_failed("Herdr unavailable"))
        self.assertIsNone(state.begin_create())
        self.assertEqual(state.begin_open_retry(), created)
        self.assertIsNone(state.begin_open_retry())
        self.assertEqual(state.phase, "opening")

    def test_composer_requires_prompt_and_preserves_draft_when_returning_to_agents(self) -> None:
        state = NewSessionState()
        state.enter("", None)
        self.load_agents(state, [agent("ag_codex"), agent("ag_claude")])

        self.assertEqual(state.begin_compose().agent_id, "ag_codex")
        self.assertIsNone(state.begin_create())
        state.set_prompt("Line one\nLine two")
        self.assertTrue(state.return_to_agent_choice())
        self.assertEqual(state.prompt, "Line one\nLine two")
        state.move(1)
        self.assertEqual(state.begin_compose().agent_id, "ag_claude")

    def test_ambiguous_send_can_only_open_the_created_session(self) -> None:
        state = NewSessionState()
        state.enter("", None)
        self.load_agents(state, [agent("ag_codex")])
        state.begin_compose()
        state.set_prompt("Ship it")
        state.begin_create()
        created = record("conv_created")
        state.mark_created(created)

        self.assertTrue(state.mark_send_unknown("timed out"))
        self.assertIsNone(state.begin_create())
        self.assertEqual(state.begin_open_after_unknown_send(), created)
        self.assertIsNone(state.begin_open_after_unknown_send())

    def test_cached_agents_remain_selectable_during_refresh(self) -> None:
        state = NewSessionState()
        state.enter("", None)

        self.assertTrue(state.seed_agents([agent("ag_codex")]))
        generation = state.start_agent_load()

        self.assertIsNotNone(generation)
        self.assertEqual(state.phase, "choose_agent")
        self.assertTrue(state.agent_loading)
        self.assertEqual(state.begin_compose().agent_id, "ag_codex")
        self.assertFalse(state.agent_loading)


class PickerConfigTest(unittest.TestCase):
    def test_context_cwd_prefers_the_focused_pane(self) -> None:
        context = json.dumps(
            {
                "focused_pane_cwd": "/repos/focused-pane",
                "workspace_cwd": "/repos/workspace-root",
            }
        )

        self.assertEqual(_context_cwd(context), "/repos/focused-pane")

    def test_plugin_state_is_scoped_to_current_herdr_socket(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env = {
                "HERDR_PLUGIN_CONFIG_DIR": str(Path(directory) / "config"),
                "HERDR_PLUGIN_STATE_DIR": str(Path(directory) / "state"),
                "HERDR_SOCKET_PATH": "/tmp/herdr-a.sock",
                "HERDR_BIN_PATH": "/opt/herdr",
            }
            with patch.object(
                PickerConfig,
                "_resolve_auto_server_url",
                return_value="http://omnigent.example:55777",
            ):
                first = PickerConfig.load(env)
                env["HERDR_SOCKET_PATH"] = "/tmp/herdr-b.sock"
                second = PickerConfig.load(env)

        self.assertNotEqual(first.state_file, second.state_file)
        self.assertEqual(first.state_file.parent.name, "state")
        self.assertEqual(first.herdr_executable, "/opt/herdr")

    def test_config_reads_server_and_recovery_capable_executable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_dir = Path(directory) / "config"
            config_dir.mkdir()
            (config_dir / "config.json").write_text(
                json.dumps(
                    {
                        "server": "http://127.0.0.1:55777",
                        "omnigent": "/opt/omnigent-open",
                        "max_sessions": 321,
                        "search_debounce_ms": 25,
                    }
                )
            )

            config = PickerConfig.load(
                {
                    "HERDR_PLUGIN_CONFIG_DIR": str(config_dir),
                    "HERDR_PLUGIN_STATE_DIR": str(Path(directory) / "state"),
                }
            )

        self.assertEqual(config.server, "http://127.0.0.1:55777")
        self.assertEqual(config.omnigent_executable, "/opt/omnigent-open")
        self.assertEqual(config.max_sessions, 321)
        self.assertEqual(config.debounce_seconds, 0.025)

    def test_catalog_cache_is_scoped_by_herdr_socket_and_omnigent_server(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_dir = Path(directory) / "config"
            config_dir.mkdir()
            env = {
                "HERDR_PLUGIN_CONFIG_DIR": str(config_dir),
                "HERDR_PLUGIN_STATE_DIR": str(Path(directory) / "state"),
                "HERDR_SOCKET_PATH": "/tmp/herdr-a.sock",
            }

            def load(server: str, socket: str = "/tmp/herdr-a.sock") -> PickerConfig:
                (config_dir / "config.json").write_text(json.dumps({"server": server}))
                env["HERDR_SOCKET_PATH"] = socket
                return PickerConfig.load(env)

            first = load("HTTP://OMNIGENT.EXAMPLE:55777/")
            equivalent = load("http://omnigent.example:55777")
            other_server = load("http://omnigent.example:55778")
            other_socket = load(
                "http://omnigent.example:55777",
                socket="/tmp/herdr-b.sock",
            )

        self.assertEqual(first.catalog_cache_file, equivalent.catalog_cache_file)
        self.assertNotEqual(first.catalog_cache_file, other_server.catalog_cache_file)
        self.assertNotEqual(first.catalog_cache_file, other_socket.catalog_cache_file)
        self.assertEqual(first.agent_cache_file, equivalent.agent_cache_file)
        self.assertNotEqual(first.agent_cache_file, other_server.agent_cache_file)
        self.assertNotEqual(first.agent_cache_file, other_socket.agent_cache_file)
        self.assertEqual(first.catalog_cache_file.parent.name, "catalog-cache")

    def test_catalog_cache_uses_the_auto_resolved_omnigent_server(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_dir = Path(directory) / "config"
            config_dir.mkdir()
            env = {
                "HERDR_PLUGIN_CONFIG_DIR": str(config_dir),
                "HERDR_PLUGIN_STATE_DIR": str(Path(directory) / "state"),
                "HERDR_SOCKET_PATH": "/tmp/herdr-a.sock",
            }

            with patch(
                "omnigent.config.load_effective_config",
                return_value={"server": "http://omnigent.example:55777"},
            ):
                first = PickerConfig.load(env)
            with patch(
                "omnigent.config.load_effective_config",
                return_value={"server": "http://omnigent.example:55778"},
            ):
                second = PickerConfig.load(env)

        self.assertIsNone(first.server)
        self.assertIsNone(second.server)
        self.assertNotEqual(first.catalog_cache_file, second.catalog_cache_file)
        self.assertNotEqual(first.agent_cache_file, second.agent_cache_file)

    def test_explicit_server_does_not_need_auto_resolution_for_cache_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_dir = Path(directory) / "config"
            config_dir.mkdir()
            (config_dir / "config.json").write_text(
                json.dumps({"server": "http://omnigent.example:55777"})
            )
            env = {
                "HERDR_PLUGIN_CONFIG_DIR": str(config_dir),
                "HERDR_PLUGIN_STATE_DIR": str(Path(directory) / "state"),
                "HERDR_SOCKET_PATH": "/tmp/herdr-a.sock",
            }

            with patch.object(PickerConfig, "_resolve_auto_server_url") as resolve_auto:
                PickerConfig.load(env)

        resolve_auto.assert_not_called()


class SessionSpaceBridgeTest(unittest.TestCase):
    def config(self, directory: str) -> PickerConfig:
        return PickerConfig(
            server="https://omnigent.example",
            omnigent_executable="/opt/Omnigent Bin/omnigent",
            max_sessions=200,
            debounce_seconds=0.3,
            state_file=Path(directory) / "state.json",
            catalog_cache_file=Path(directory) / "catalog.json",
            agent_cache_file=Path(directory) / "agents.json",
            catalog_cache_scope="test",
            fallback_cwd=Path(directory),
            herdr_executable="/opt/herdr",
        )

    @patch("session_picker.subprocess.run")
    def test_catalog_uses_server_search_and_loaded_annotation(self, run: object) -> None:
        run.return_value = subprocess.CompletedProcess(  # type: ignore[attr-defined]
            args=[],
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "id": "conv_one",
                        "title": "Deployment",
                        "status": "idle",
                        "active": False,
                        "attention": False,
                        "runner_online": False,
                        "recovery_hint": "resume",
                        "pinned": False,
                        "loaded": True,
                    }
                ]
            ),
            stderr="",
        )
        with tempfile.TemporaryDirectory() as directory:
            bridge = SessionSpaceBridge(self.config(directory))
            rows = bridge.catalog("  deployment   failure ")

        command = run.call_args.args[0]  # type: ignore[attr-defined]
        self.assertIn("--include-pinned", command)
        self.assertIn("--include-loaded", command)
        self.assertIn("--skip-liveness", command)
        query_index = command.index("--search-query")
        self.assertEqual(command[query_index + 1], "deployment failure")
        self.assertTrue(rows[0].loaded)

    @patch("session_picker.subprocess.run")
    def test_open_passes_fresh_record_via_stdin_without_display_data_in_argv(
        self, run: object
    ) -> None:
        run.return_value = subprocess.CompletedProcess(  # type: ignore[attr-defined]
            args=[], returncode=0, stdout="", stderr=""
        )
        selected = replace(
            record("conv harmless; printf BAD"),
            title="Private title",
            workspace="/private/workspace",
        )
        with tempfile.TemporaryDirectory() as directory:
            bridge = SessionSpaceBridge(self.config(directory))
            bridge.open(selected)

        command = run.call_args.args[0]  # type: ignore[attr-defined]
        open_index = command.index("--open-session")
        self.assertEqual(command[open_index + 1], "conv harmless; printf BAD")
        self.assertIn("--open-session-record-stdin", command)
        self.assertNotIn("Private title", command)
        self.assertNotIn("/private/workspace", command)
        snapshot = json.loads(run.call_args.kwargs["input"])  # type: ignore[attr-defined]
        self.assertEqual(snapshot["title"], "Private title")
        self.assertEqual(snapshot["workspace"], "/private/workspace")
        self.assertNotIn("search_snippet", snapshot)

    @patch("session_picker.subprocess.run")
    def test_initial_message_is_unchanged_on_stdin_and_waits_for_cold_host(
        self, run: object
    ) -> None:
        run.return_value = subprocess.CompletedProcess(  # type: ignore[attr-defined]
            args=[], returncode=0, stdout="{}", stderr=""
        )
        prompt = "First line\n\nSecond line $()"
        with tempfile.TemporaryDirectory() as directory:
            bridge = SessionSpaceBridge(self.config(directory))
            bridge.send_initial_message("conv_one", prompt)

        command = run.call_args.args[0]  # type: ignore[attr-defined]
        self.assertEqual(command[-2:], ["--send-message", "conv_one"])
        self.assertNotIn(prompt, command)
        self.assertEqual(run.call_args.kwargs["input"], prompt)  # type: ignore[attr-defined]
        self.assertEqual(run.call_args.kwargs["timeout"], 150)  # type: ignore[attr-defined]

    @patch("session_picker.subprocess.run")
    def test_list_create_agents_uses_bridge_contract_and_30_second_timeout(
        self,
        run: object,
    ) -> None:
        run.return_value = subprocess.CompletedProcess(  # type: ignore[attr-defined]
            args=[],
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "id": "ag_codex",
                        "name": "codex-native-ui",
                        "display_name": "Codex",
                        "harness": "codex-native",
                    }
                ]
            ),
            stderr="",
        )
        with tempfile.TemporaryDirectory() as directory:
            bridge = SessionSpaceBridge(self.config(directory))
            agents = bridge.list_create_agents()

        command = run.call_args.args[0]  # type: ignore[attr-defined]
        self.assertEqual(command[-1], "--list-create-agents")
        self.assertEqual(run.call_args.kwargs["timeout"], 30)  # type: ignore[attr-defined]
        self.assertEqual(agents[0].display_name, "Codex")

    @patch("session_picker.subprocess.run")
    def test_create_passes_agent_as_one_argv_element_and_uses_long_timeout(
        self,
        run: object,
    ) -> None:
        expected = record("conv_created")
        run.return_value = subprocess.CompletedProcess(  # type: ignore[attr-defined]
            args=[],
            returncode=0,
            stdout=json.dumps(expected.to_json()),
            stderr="",
        )
        with tempfile.TemporaryDirectory() as directory:
            bridge = SessionSpaceBridge(self.config(directory))
            created = bridge.create("ag harmless; printf BAD")

        command = run.call_args.args[0]  # type: ignore[attr-defined]
        create_index = command.index("--create-session")
        self.assertEqual(command[create_index + 1], "ag harmless; printf BAD")
        self.assertEqual(run.call_args.kwargs["timeout"], 150)  # type: ignore[attr-defined]
        self.assertEqual(created, expected)

    @patch("session_picker.subprocess.run")
    def test_create_rejects_invalid_or_non_object_json(self, run: object) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge = SessionSpaceBridge(self.config(directory))
            run.return_value = subprocess.CompletedProcess(  # type: ignore[attr-defined]
                args=[], returncode=0, stdout="not-json", stderr=""
            )
            with self.assertRaisesRegex(PickerError, "invalid created session"):
                bridge.create("ag_codex")

            run.return_value = subprocess.CompletedProcess(  # type: ignore[attr-defined]
                args=[], returncode=0, stdout="[]", stderr=""
            )
            with self.assertRaisesRegex(PickerError, "non-object created session"):
                bridge.create("ag_codex")


class PickerInteractionTest(unittest.TestCase):
    def test_multiline_initial_message_is_sent_once_despite_repeated_enter(self) -> None:
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput

        class Bridge:
            def __init__(self) -> None:
                self.catalog_started = threading.Event()
                self.agents_listed = threading.Event()
                self.create_started = threading.Event()
                self.release_create = threading.Event()
                self.opened = threading.Event()
                self.create_calls: list[str] = []
                self.messages: list[tuple[str, str]] = []
                self.opened_records: list[SessionRecord] = []

            def catalog(self, _query: str) -> list[SessionRecord]:
                self.catalog_started.set()
                return []

            def list_create_agents(self) -> list[AgentRecord]:
                self.agents_listed.set()
                return [agent("ag_codex", display_name="Codex", harness="codex-native")]

            def create(self, agent_id: str) -> SessionRecord:
                self.create_calls.append(agent_id)
                self.create_started.set()
                self.release_create.wait(2)
                return replace(record("conv_created"), title=None)

            def send_initial_message(self, session_id: str, prompt: str) -> None:
                self.messages.append((session_id, prompt))

            def open(self, selected: SessionRecord) -> None:
                self.opened_records.append(selected)
                self.opened.set()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = PickerConfig(
                server="https://omnigent.example",
                omnigent_executable="/opt/omnigent",
                max_sessions=200,
                debounce_seconds=0,
                state_file=root / "state.json",
                catalog_cache_file=root / "catalog.json",
                agent_cache_file=root / "agents.json",
                catalog_cache_scope="test",
                fallback_cwd=root,
                herdr_executable="/opt/herdr",
            )
            cached_agent = agent(
                "ag_codex",
                display_name="Codex",
                harness="codex-native",
            )
            PersistentAgentCache(config.agent_cache_file, scope="test").store([cached_agent])
            bridge = Bridge()
            results: list[str | None] = []

            def run() -> None:
                results.append(
                    run_picker(
                        bridge,  # type: ignore[arg-type]
                        config,
                        input_stream=pipe,
                        output_stream=DummyOutput(),
                    )
                )

            with create_pipe_input() as pipe:
                thread = threading.Thread(target=run)
                thread.start()
                try:
                    self.assertTrue(bridge.catalog_started.wait(2))
                    time.sleep(0.1)
                    pipe.send_text("\x0e")
                    self.assertTrue(
                        bridge.agents_listed.wait(2),
                        f"picker results={results!r} alive={thread.is_alive()}",
                    )
                    time.sleep(0.05)
                    pipe.send_text("\r")
                    time.sleep(0.05)
                    pipe.send_text("First line\nSecond line\r")
                    self.assertTrue(bridge.create_started.wait(2))
                    pipe.send_text("\r\r")
                    bridge.release_create.set()
                    self.assertTrue(bridge.opened.wait(2))
                    thread.join(2)
                finally:
                    bridge.release_create.set()
                    if thread.is_alive():
                        pipe.send_text("\x03")
                        thread.join(2)

        self.assertFalse(thread.is_alive())
        self.assertEqual(results, ["conv_created"])
        self.assertEqual(bridge.create_calls, ["ag_codex"])
        self.assertEqual(bridge.messages, [("conv_created", "First line\nSecond line")])
        self.assertEqual(len(bridge.opened_records), 1)
        self.assertEqual(bridge.opened_records[0].title, "First line")


class CatalogWorkerTest(unittest.TestCase):
    def test_bursty_requests_are_coalesced_before_catalog_call(self) -> None:
        called: list[str] = []
        delivered = threading.Event()

        def catalog(query: str) -> list[SessionRecord]:
            called.append(query)
            return []

        worker = CatalogWorker(catalog, lambda _result: delivered.set())
        try:
            worker.request("d", 0.05)
            worker.request("de", 0.05)
            worker.request("deploy", 0.01)
            self.assertTrue(delivered.wait(1))
        finally:
            worker.stop()

        self.assertEqual(called, ["deploy"])

    def test_result_identifies_normalized_query_and_request_generation(self) -> None:
        delivered: list[object] = []
        ready = threading.Event()
        expected = [record("match")]

        def deliver(result: object) -> None:
            delivered.append(result)
            ready.set()

        worker = CatalogWorker(lambda _query: expected, deliver)
        try:
            generation = worker.request("  DePloY   NOW ", 0)
            self.assertTrue(ready.wait(1))
        finally:
            worker.stop()

        self.assertEqual(delivered, [(generation, "deploy now", expected, None)])


class CatalogCacheTest(unittest.TestCase):
    def test_query_normalization_is_case_insensitive_and_collapses_whitespace(self) -> None:
        self.assertEqual(_normalize_query("  DePloY\t FAILURE\n"), "deploy failure")

    def test_exact_cached_result_is_reused_before_the_base_catalog(self) -> None:
        base_match = replace(record("base"), title="Deploy from base catalog")
        server_match = replace(record("server"), title="Server-ranked deployment")
        cache = CatalogCache()
        cache.store("", [base_match])
        cache.store("  DEPLOY  ", [server_match])

        cached = cache.lookup("deploy")

        self.assertEqual([item.session_id for item in cached or []], ["server"])

    def test_uncached_query_filters_base_catalog_across_searchable_fields(self) -> None:
        cache = CatalogCache()
        cache.store(
            "",
            [
                replace(record("by-title"), title="Needle in title"),
                replace(record("needle-session-id"), title="By identifier"),
                replace(record("by-agent"), title="By agent", agent="Needle Agent"),
                replace(record("by-project"), title="By project", project="Needle Project"),
                replace(
                    record("by-workspace"),
                    title="By workspace",
                    workspace="/repos/Needle/service",
                ),
                replace(
                    record("by-snippet"),
                    title="By snippet",
                    search_snippet="Conversation mentions NEEDLE here",
                ),
                replace(record("miss"), title="Unrelated session"),
            ],
        )

        cached = cache.lookup("  nEeDlE ")

        self.assertEqual(
            {item.session_id for item in cached or []},
            {
                "by-title",
                "needle-session-id",
                "by-agent",
                "by-project",
                "by-workspace",
                "by-snippet",
            },
        )

    def test_local_filter_matches_normalized_tokens_across_multiple_fields(self) -> None:
        cache = CatalogCache()
        cache.store(
            "",
            [
                replace(
                    record("match"),
                    title="Deploy the service",
                    project="Failure response",
                ),
                replace(record("partial"), title="Deploy only"),
            ],
        )

        cached = cache.lookup(" FAILURE   deploy ")

        self.assertEqual([item.session_id for item in cached or []], ["match"])

    def test_cached_lists_are_isolated_from_callers(self) -> None:
        original = [record("one")]
        cache = CatalogCache()
        cache.store("deploy", original)
        original.append(record("added-after-store"))

        first_lookup = cache.lookup("DEPLOY")
        assert first_lookup is not None
        first_lookup.append(record("added-after-lookup"))

        second_lookup = cache.lookup(" deploy ")
        self.assertEqual(
            [item.session_id for item in second_lookup or []],
            ["one"],
        )

    def test_known_empty_local_result_is_distinct_from_cache_miss(self) -> None:
        cache = CatalogCache()
        self.assertIsNone(cache.lookup("missing"))

        cache.store("", [record("one")])

        self.assertEqual(cache.lookup("missing"), [])

    def test_lru_evicts_least_recently_used_exact_query(self) -> None:
        cache = CatalogCache(max_entries=2)
        cache.store("one", [record("one")])
        cache.store("two", [record("two")])
        self.assertEqual(cache.lookup("one"), [record("one")])

        cache.store("three", [record("three")])

        self.assertIsNone(cache.lookup("two"))
        self.assertEqual(cache.lookup("one"), [record("one")])
        self.assertEqual(cache.lookup("three"), [record("three")])

    def test_upsert_updates_base_and_matching_exact_caches(self) -> None:
        cache = CatalogCache()
        old = replace(record("created"), title="Old title")
        cache.store("", [record("existing"), old])
        cache.store("deploy", [old])
        cache.store("unrelated", [record("unrelated")])
        created = replace(record("created"), title="Deploy service")

        cache.upsert(created)

        self.assertEqual(cache.base_records(), [created, record("existing")])
        self.assertEqual(cache.lookup("deploy"), [created])
        self.assertEqual(cache.lookup("unrelated"), [record("unrelated")])

    def test_upsert_initializes_an_empty_cache(self) -> None:
        cache = CatalogCache()
        created = record("created")

        cache.upsert(created)

        self.assertEqual(cache.lookup(""), [created])
        self.assertEqual(cache.base_records(), [created])

    def test_upsert_keeps_base_after_empty_exact_entry_was_lru_evicted(self) -> None:
        existing = record("existing")
        created = record("created")
        cache = CatalogCache(max_entries=2)
        cache.store("", [existing])
        cache.store("first", [])
        cache.store("second", [])

        cache.upsert(created)

        self.assertEqual(cache.base_records(), [created, existing])
        self.assertEqual(cache.lookup(""), [created, existing])

    def test_server_metadata_replaces_provisional_title_and_preserves_loaded(self) -> None:
        existing = record("existing")
        server_created = replace(record("created"), title="Generated title")
        local_created = replace(record("created", loaded=True), title="Provisional title")

        merged = _merge_created_records(
            [existing, server_created],
            [local_created],
            query="",
        )

        self.assertEqual(merged, [existing, replace(server_created, loaded=True)])

    def test_local_created_record_only_joins_matching_query_results(self) -> None:
        server_match = replace(record("server"), title="Server-ranked incident")
        local_created = replace(record("created"), title="Deploy service")

        merged = _merge_created_records(
            [server_match],
            [local_created],
            query="incident",
        )

        self.assertEqual(merged, [server_match])


class PersistentAgentCacheTest(unittest.TestCase):
    def test_round_trip_preserves_small_server_scoped_agent_catalog(self) -> None:
        expected = [
            agent(
                "ag_codex",
                name="codex-native-ui",
                display_name="Codex",
                harness="codex-native",
            ),
            agent("ag_claude", display_name="Claude", harness="claude-native"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agents.json"
            PersistentAgentCache(path, scope="server-a").store(expected)

            restored = PersistentAgentCache(path, scope="server-a").load()
            wrong_scope = PersistentAgentCache(path, scope="server-b").load()

        self.assertEqual(restored, expected)
        self.assertIsNone(wrong_scope)

    def test_corrupt_agent_cache_fails_open(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agents.json"
            path.write_text("not-json")

            self.assertIsNone(PersistentAgentCache(path, scope="server-a").load())


class PersistentCatalogCacheTest(unittest.TestCase):
    def test_round_trip_reopens_the_base_catalog(self) -> None:
        expected = [
            replace(
                record(
                    "one",
                    pinned=True,
                    loaded=True,
                    active=True,
                    attention=True,
                    online=True,
                    updated_at=1234,
                ),
                title="Deployment session",
                agent="Claude",
                project="Release",
                workspace=None,
            ),
            replace(
                record("two", updated_at=0),
                title=None,
                agent=None,
                project=None,
                workspace=None,
            ),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            PersistentCatalogCache(path).store(expected)

            restored = PersistentCatalogCache(path).load()

        self.assertEqual(restored, expected)

    def test_corrupt_document_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            path.write_text("{ definitely not json")

            restored = PersistentCatalogCache(path).load()

        self.assertIsNone(restored)

    def test_invalid_utf8_document_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            path.write_bytes(b"\xff\xfe\x00")

            restored = PersistentCatalogCache(path).load()

        self.assertIsNone(restored)

    def test_version_mismatch_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            cache = PersistentCatalogCache(path)
            cache.store([record("one")])
            document = json.loads(path.read_text())
            document["version"] = 999
            path.write_text(json.dumps(document))

            restored = cache.load()

        self.assertIsNone(restored)

    def test_scope_mismatch_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            PersistentCatalogCache(path, scope="server-a").store([record("one")])

            restored = PersistentCatalogCache(path, scope="server-b").load()

        self.assertIsNone(restored)

    def test_expired_snapshot_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            cache = PersistentCatalogCache(path)
            with patch("session_picker.time.time", return_value=1_000_000):
                cache.store([record("one")])

            with patch(
                "session_picker.time.time",
                return_value=1_000_000 + 8 * 86_400,
            ):
                restored = cache.load()

        self.assertIsNone(restored)

    def test_failed_atomic_replace_preserves_previous_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            cache = PersistentCatalogCache(path)
            cache.store([record("old")])

            with patch("session_picker.os.replace", side_effect=OSError("disk failure")):
                cache.store([record("new")])

            restored = PersistentCatalogCache(path).load()
            remaining_files = list(path.parent.iterdir())

        self.assertEqual(restored, [replace(record("old"), workspace=None)])
        self.assertEqual(remaining_files, [path])

    def test_sensitive_search_snippets_and_workspace_are_not_persisted(self) -> None:
        sensitive = replace(
            record("one"),
            workspace="/private/customer/repository",
            search_snippet="private conversation text",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            cache = PersistentCatalogCache(path)
            cache.store([sensitive])

            restored = cache.load()
            serialized = path.read_text()

        assert restored is not None
        self.assertIsNone(restored[0].search_snippet)
        self.assertIsNone(restored[0].workspace)
        self.assertNotIn("private conversation text", serialized)
        self.assertNotIn("/private/customer/repository", serialized)

    def test_snapshot_uses_private_directory_and_file_modes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache_dir = Path(directory) / "catalog-cache"
            path = cache_dir / "catalog.json"

            PersistentCatalogCache(path).store([record("one")])

            directory_mode = stat.S_IMODE(cache_dir.stat().st_mode)
            file_mode = stat.S_IMODE(path.stat().st_mode)

        self.assertEqual(directory_mode, 0o700)
        self.assertEqual(file_mode, 0o600)


class RenderingTest(unittest.TestCase):
    def test_row_keeps_action_and_identity_markers_at_narrow_width(self) -> None:
        rendered = _row_text(
            record("one", pinned=True, loaded=True, active=True),
            selected=True,
            width=48,
        )

        self.assertEqual(len(rendered), 48)
        self.assertIn("› ★ ● OPEN", rendered)

    def test_agent_row_keeps_name_and_harness_at_narrow_width(self) -> None:
        rendered = _agent_row_text(
            agent(
                "ag_codex",
                name="codex-native-ui",
                display_name="Codex",
                harness="codex-native",
            ),
            selected=True,
            width=32,
        )

        self.assertEqual(len(rendered), 32)
        self.assertIn("›  Codex", rendered)
        self.assertIn("codex-native", rendered)

    def test_relative_age_boundaries(self) -> None:
        self.assertEqual(_relative_age(999, now=1000), "now")
        self.assertEqual(_relative_age(880, now=1000), "2m")
        self.assertEqual(_relative_age(100, now=7300), "2h")


if __name__ == "__main__":
    unittest.main()
