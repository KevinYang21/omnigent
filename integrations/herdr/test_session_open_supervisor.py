from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from session_open_supervisor import (
    RetryableTitleSyncError,
    SessionMissing,
    SupervisorConfig,
    SupervisorError,
    WatchStopReason,
    WorkspaceMissing,
    fetch_session_label,
    main,
    rename_workspace,
    supervise,
    watch_title_changes,
)


def config(**overrides: object) -> SupervisorConfig:
    values: dict[str, object] = {
        "session_id": "conv_one",
        "workspace_id": "w7",
        "base_url": "https://omnigent.example",
        "herdr_executable": "/opt/bin/herdr",
        "omnigent_executable": "/opt/bin/omnigent",
        "initial_label": "Codex",
        "poll_interval": 0.01,
        "request_timeout": 2.0,
        "rename_timeout": 3.0,
        "max_retry_interval": 0.08,
    }
    values.update(overrides)
    return SupervisorConfig(**values)  # type: ignore[arg-type]


class ScriptedEvent:
    def __init__(self, *, stop_after_waits: int) -> None:
        self.stop_after_waits = stop_after_waits
        self.waits: list[float] = []
        self._set = False

    def is_set(self) -> bool:
        return self._set

    def set(self) -> None:
        self._set = True

    def wait(self, timeout: float) -> bool:
        self.waits.append(timeout)
        if len(self.waits) >= self.stop_after_waits:
            self._set = True
        return self._set


class FetchSessionLabelTest(unittest.TestCase):
    @patch("omnigent.cli._host_http_json")
    def test_fetches_only_the_exact_lightweight_session_snapshot(self, request: MagicMock) -> None:
        request.return_value = SimpleNamespace(
            status_code=200,
            body={"id": "conv/a", "title": "  Ship\n safely  ", "agent_name": "Codex"},
        )

        label = fetch_session_label("https://example.test", "conv/a", 1.5)

        self.assertEqual(label, "Ship safely")
        request.assert_called_once_with(
            base_url="https://example.test",
            method="GET",
            path="/v1/sessions/conv%2Fa",
            params={"include_items": "false", "include_liveness": "false"},
            timeout_s=1.5,
        )

    @patch("omnigent.cli._host_http_json")
    def test_missing_title_preserves_the_existing_provisional_label(
        self, request: MagicMock
    ) -> None:
        request.side_effect = [
            SimpleNamespace(
                status_code=200,
                body={"id": "conv_one", "title": None, "agent_name": "Claude"},
            ),
            SimpleNamespace(
                status_code=200,
                body={"id": "conv_one", "title": None, "agent_name": None},
            ),
        ]

        self.assertIsNone(fetch_session_label("http://local", "conv_one", 1))
        self.assertIsNone(fetch_session_label("http://local", "conv_one", 1))

    @patch("omnigent.cli._host_http_json")
    def test_missing_session_is_terminal(self, request: MagicMock) -> None:
        request.return_value = SimpleNamespace(status_code=404, body={"detail": "gone"})

        with self.assertRaises(SessionMissing):
            fetch_session_label("http://local", "conv_one", 1)

    @patch("omnigent.cli._host_http_json")
    def test_transport_and_malformed_responses_are_retryable(self, request: MagicMock) -> None:
        request.side_effect = [
            SimpleNamespace(status_code=0, body="offline"),
            SimpleNamespace(status_code=200, body={"id": "another", "title": "Wrong"}),
            SimpleNamespace(status_code=200, body={"id": "conv_one", "title": 42}),
        ]

        for _ in range(3):
            with self.assertRaises(RetryableTitleSyncError):
                fetch_session_label("http://local", "conv_one", 1)


class RenameWorkspaceTest(unittest.TestCase):
    @patch("session_open_supervisor.subprocess.run")
    def test_uses_exact_argv_and_inherits_the_pane_socket(self, run: MagicMock) -> None:
        run.return_value = SimpleNamespace(returncode=0, stdout="{}", stderr="")

        with patch.dict(os.environ, {"HERDR_SOCKET_PATH": "/tmp/exact-herdr.sock"}):
            rename_workspace("/opt/bin/herdr", "w9", "New title", 4.0)

        args, kwargs = run.call_args
        self.assertEqual(
            args[0],
            ["/opt/bin/herdr", "workspace", "rename", "w9", "New title"],
        )
        self.assertNotIn("env", kwargs)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["timeout"], 4.0)

    @patch("session_open_supervisor.subprocess.run")
    def test_workspace_not_found_is_terminal(self, run: MagicMock) -> None:
        run.return_value = SimpleNamespace(
            returncode=1,
            stdout="",
            stderr=json.dumps({"error": {"code": "workspace_not_found", "message": "gone"}}),
        )

        with self.assertRaises(WorkspaceMissing):
            rename_workspace("herdr", "w9", "Title", 1)

    @patch("session_open_supervisor.subprocess.run")
    def test_other_rename_failures_are_retryable(self, run: MagicMock) -> None:
        run.return_value = SimpleNamespace(
            returncode=1,
            stdout="",
            stderr=json.dumps({"error": {"code": "server_not_running", "message": "handoff"}}),
        )

        with self.assertRaises(RetryableTitleSyncError):
            rename_workspace("herdr", "w9", "Title", 1)


class WatchTitleChangesTest(unittest.TestCase):
    def test_no_server_title_does_not_replace_the_provisional_label(self) -> None:
        event = ScriptedEvent(stop_after_waits=2)
        labels = iter([None, "Generated title"])
        renames: list[str] = []

        watch_title_changes(
            config(initial_label="Provisional prompt"),
            event,  # type: ignore[arg-type]
            fetch_label=lambda _server, _session, _timeout: next(labels),
            rename_label=lambda _herdr, _workspace, label, _timeout: renames.append(label),
        )

        self.assertEqual(renames, ["Generated title"])

    def test_tracks_seed_semantic_and_later_manual_titles(self) -> None:
        event = ScriptedEvent(stop_after_waits=5)
        labels = iter(["Codex", "Seed title", "Semantic title", "Semantic title", "Manual title"])
        renames: list[tuple[str, str]] = []

        reason = watch_title_changes(
            config(),
            event,  # type: ignore[arg-type]
            fetch_label=lambda _server, _session, _timeout: next(labels),
            rename_label=lambda _herdr, workspace, label, _timeout: renames.append(
                (workspace, label)
            ),
        )

        self.assertEqual(reason, WatchStopReason.STOPPED)
        self.assertEqual(
            renames,
            [
                ("w7", "Seed title"),
                ("w7", "Semantic title"),
                ("w7", "Manual title"),
            ],
        )

    def test_transient_fetch_failures_back_off_and_recover(self) -> None:
        event = ScriptedEvent(stop_after_waits=3)
        outcomes: list[object] = [
            RetryableTitleSyncError("one"),
            RetryableTitleSyncError("two"),
            "Recovered title",
        ]
        renames: list[str] = []

        def fetch(_server: str, _session: str, _timeout: float) -> str:
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return str(outcome)

        reason = watch_title_changes(
            config(),
            event,  # type: ignore[arg-type]
            fetch_label=fetch,
            rename_label=lambda _herdr, _workspace, label, _timeout: renames.append(label),
        )

        self.assertEqual(reason, WatchStopReason.STOPPED)
        self.assertEqual(renames, ["Recovered title"])
        self.assertEqual(event.waits, [0.01, 0.02, 0.01])

    def test_transient_rename_retries_the_unapplied_title(self) -> None:
        event = ScriptedEvent(stop_after_waits=2)
        attempts: list[str] = []

        def rename(_herdr: str, _workspace: str, label: str, _timeout: float) -> None:
            attempts.append(label)
            if len(attempts) == 1:
                raise RetryableTitleSyncError("handoff")

        reason = watch_title_changes(
            config(),
            event,  # type: ignore[arg-type]
            fetch_label=lambda _server, _session, _timeout: "New title",
            rename_label=rename,
        )

        self.assertEqual(reason, WatchStopReason.STOPPED)
        self.assertEqual(attempts, ["New title", "New title"])

    def test_stops_when_session_disappears(self) -> None:
        event = ScriptedEvent(stop_after_waits=1)

        def missing(_server: str, _session: str, _timeout: float) -> str:
            raise SessionMissing("conv_one")

        reason = watch_title_changes(
            config(),
            event,  # type: ignore[arg-type]
            fetch_label=missing,
        )

        self.assertEqual(reason, WatchStopReason.SESSION_MISSING)
        self.assertEqual(event.waits, [])

    def test_stops_when_workspace_disappears(self) -> None:
        event = ScriptedEvent(stop_after_waits=1)

        def missing(_herdr: str, workspace: str, _label: str, _timeout: float) -> None:
            raise WorkspaceMissing(workspace)

        reason = watch_title_changes(
            config(),
            event,  # type: ignore[arg-type]
            fetch_label=lambda _server, _session, _timeout: "New title",
            rename_label=missing,
        )

        self.assertEqual(reason, WatchStopReason.WORKSPACE_MISSING)
        self.assertEqual(event.waits, [])

    def test_tui_exit_during_fetch_prevents_a_trailing_rename(self) -> None:
        event = threading.Event()
        rename = MagicMock()

        def fetch(_server: str, _session: str, _timeout: float) -> str:
            event.set()
            return "Too late"

        reason = watch_title_changes(
            config(),
            event,
            fetch_label=fetch,
            rename_label=rename,
        )

        self.assertEqual(reason, WatchStopReason.STOPPED)
        rename.assert_not_called()


class SupervisorLifecycleTest(unittest.TestCase):
    def test_tui_inherits_pty_and_its_exit_stops_the_worker(self) -> None:
        started = threading.Event()
        stopped = threading.Event()
        child = MagicMock()

        def child_wait(*_args: object, **_kwargs: object) -> int:
            self.assertTrue(started.wait(1))
            return 7

        child.wait.side_effect = child_wait
        popen_calls: list[list[str]] = []

        def popen(command: object) -> MagicMock:
            popen_calls.append(list(command))  # type: ignore[arg-type]
            return child

        def watcher(_config: SupervisorConfig, stop_event: threading.Event) -> WatchStopReason:
            started.set()
            stop_event.wait(1)
            if stop_event.is_set():
                stopped.set()
            return WatchStopReason.STOPPED

        result = supervise(config(), watcher=watcher, popen_factory=popen)

        self.assertEqual(result, 7)
        self.assertEqual(
            popen_calls,
            [["/opt/bin/omnigent", "open", "conv_one", "--server", "https://omnigent.example"]],
        )
        self.assertTrue(stopped.wait(1))

    def test_signal_exit_is_translated_to_shell_status(self) -> None:
        child = MagicMock()
        child.wait.return_value = -signal.SIGTERM

        result = supervise(
            config(),
            watcher=lambda _config, _stop: WatchStopReason.STOPPED,
            popen_factory=lambda _command: child,
        )

        self.assertEqual(result, 128 + signal.SIGTERM)

    def test_spawn_failure_is_reported_without_starting_a_watcher(self) -> None:
        watcher = MagicMock()

        with self.assertRaises(SupervisorError):
            supervise(
                config(),
                watcher=watcher,
                popen_factory=lambda _command: (_ for _ in ()).throw(OSError("missing")),
            )

        watcher.assert_not_called()

    @patch("session_open_supervisor.supervise", return_value=9)
    def test_cli_builds_an_explicit_bridge_contract(self, run: MagicMock) -> None:
        result = main(
            [
                "--session",
                "conv_two",
                "--workspace",
                "w8",
                "--server",
                "https://example.test/",
                "--herdr",
                "/bin/herdr",
                "--omnigent",
                "/bin/omnigent",
                "--initial-label",
                "Codex",
            ]
        )

        self.assertEqual(result, 9)
        passed = run.call_args.args[0]
        self.assertEqual(passed.session_id, "conv_two")
        self.assertEqual(passed.workspace_id, "w8")
        self.assertEqual(passed.base_url, "https://example.test")
        self.assertEqual(passed.initial_label, "Codex")


if __name__ == "__main__":
    unittest.main()
