"""Run ``omnigent open`` while mirroring its session title into Herdr.

The supervisor is intended to be the foreground command in one managed Herdr
pane.  Its TUI child inherits the pane's terminal, process group, environment,
and stdio.  A daemon thread performs only quiet management calls: it polls the
exact Omnigent session and renames the exact Herdr workspace when the display
label changes.
"""

from __future__ import annotations

import argparse
import json
import signal
import subprocess
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from urllib.parse import quote

# Keep a linked source plugin runnable without an editable install.
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


DEFAULT_POLL_INTERVAL = 3.0
DEFAULT_REQUEST_TIMEOUT = 5.0
DEFAULT_RENAME_TIMEOUT = 5.0
DEFAULT_MAX_RETRY_INTERVAL = 30.0
MAX_WORKSPACE_LABEL_CHARS = 80


class SupervisorError(RuntimeError):
    """The foreground TUI could not be started."""


class RetryableTitleSyncError(RuntimeError):
    """A title-sync operation should be retried after backoff."""


class SessionMissing(RuntimeError):
    """The exact Omnigent session no longer exists."""


class WorkspaceMissing(RuntimeError):
    """The exact Herdr workspace no longer exists."""


class WatchStopReason(str, Enum):
    STOPPED = "stopped"
    SESSION_MISSING = "session_missing"
    WORKSPACE_MISSING = "workspace_missing"


@dataclass(frozen=True)
class SupervisorConfig:
    session_id: str
    workspace_id: str
    base_url: str
    herdr_executable: str
    omnigent_executable: str
    initial_label: str | None = None
    poll_interval: float = DEFAULT_POLL_INTERVAL
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT
    rename_timeout: float = DEFAULT_RENAME_TIMEOUT
    max_retry_interval: float = DEFAULT_MAX_RETRY_INTERVAL

    def __post_init__(self) -> None:
        for name in (
            "session_id",
            "workspace_id",
            "base_url",
            "herdr_executable",
            "omnigent_executable",
        ):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be empty")
        for name in (
            "poll_interval",
            "request_timeout",
            "rename_timeout",
            "max_retry_interval",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")

    @property
    def open_command(self) -> list[str]:
        return [
            self.omnigent_executable,
            "open",
            self.session_id,
            "--server",
            self.base_url,
        ]


def _clean_label(value: str | None, *, fallback: str) -> str:
    raw = value or fallback
    cleaned = " ".join("".join(ch for ch in raw if ch.isprintable()).split())
    return (cleaned or fallback)[:MAX_WORKSPACE_LABEL_CHARS]


def fetch_session_label(base_url: str, session_id: str, timeout_s: float) -> str | None:
    """Read the display label without runner liveness or project lookups."""
    try:
        from omnigent.cli import _host_error_text, _host_http_json
    except (ImportError, OSError) as exc:
        raise RetryableTitleSyncError(f"could not load Omnigent HTTP client: {exc}") from exc

    result = _host_http_json(
        base_url=base_url,
        method="GET",
        path=f"/v1/sessions/{quote(session_id, safe='')}",
        params={"include_items": "false", "include_liveness": "false"},
        timeout_s=timeout_s,
    )
    if result.status_code in {404, 410}:
        raise SessionMissing(session_id)
    if result.status_code != 200:
        raise RetryableTitleSyncError(
            f"session lookup failed ({result.status_code}): {_host_error_text(result.body)}"
        )
    if not isinstance(result.body, dict):
        raise RetryableTitleSyncError("session lookup returned a non-object response")

    returned_id = result.body.get("id")
    if returned_id is not None and returned_id != session_id:
        raise RetryableTitleSyncError("session lookup returned a different session")
    title = result.body.get("title")
    if title is not None and not isinstance(title, str):
        raise RetryableTitleSyncError("session lookup returned a malformed title")
    if title is None or not title.strip():
        return None
    return _clean_label(title, fallback=session_id)


def _herdr_error_code(*streams: str) -> str | None:
    for stream in streams:
        for line in reversed(stream.splitlines()):
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(value, dict):
                continue
            error = value.get("error")
            if not isinstance(error, dict):
                continue
            code = error.get("code")
            if isinstance(code, str):
                return code
    return None


def rename_workspace(
    herdr_executable: str,
    workspace_id: str,
    label: str,
    timeout_s: float,
) -> None:
    """Rename one workspace through argv while inheriting its Herdr socket."""
    command = [
        str(Path(herdr_executable).expanduser()),
        "workspace",
        "rename",
        workspace_id,
        label,
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RetryableTitleSyncError(f"could not rename Herdr workspace: {exc}") from exc
    if completed.returncode == 0:
        return

    code = _herdr_error_code(completed.stderr, completed.stdout)
    if code == "workspace_not_found":
        raise WorkspaceMissing(workspace_id)
    detail = (completed.stderr or completed.stdout).strip()
    raise RetryableTitleSyncError(
        f"Herdr workspace rename failed ({completed.returncode}): {detail[-400:]}"
    )


TitleFetcher = Callable[[str, str, float], str | None]
WorkspaceRenamer = Callable[[str, str, str, float], None]


def _retry_delay(config: SupervisorConfig, failures: int) -> float:
    exponent = min(max(0, failures - 1), 10)
    return min(config.max_retry_interval, config.poll_interval * (2**exponent))


def watch_title_changes(
    config: SupervisorConfig,
    stop_event: threading.Event,
    *,
    fetch_label: TitleFetcher = fetch_session_label,
    rename_label: WorkspaceRenamer = rename_workspace,
) -> WatchStopReason:
    """Mirror every later label change until the pane's TUI exits."""
    last_applied = (
        _clean_label(config.initial_label, fallback=config.session_id)
        if config.initial_label is not None
        else None
    )
    failures = 0
    while not stop_event.is_set():
        try:
            desired = fetch_label(config.base_url, config.session_id, config.request_timeout)
        except SessionMissing:
            return WatchStopReason.SESSION_MISSING
        except RetryableTitleSyncError:
            failures += 1
            stop_event.wait(_retry_delay(config, failures))
            continue

        if stop_event.is_set():
            break
        if desired is None:
            failures = 0
            stop_event.wait(config.poll_interval)
            continue
        if desired != last_applied:
            try:
                rename_label(
                    config.herdr_executable,
                    config.workspace_id,
                    desired,
                    config.rename_timeout,
                )
            except WorkspaceMissing:
                return WatchStopReason.WORKSPACE_MISSING
            except RetryableTitleSyncError:
                failures += 1
                stop_event.wait(_retry_delay(config, failures))
                continue
            last_applied = desired

        failures = 0
        stop_event.wait(config.poll_interval)
    return WatchStopReason.STOPPED


Watcher = Callable[[SupervisorConfig, threading.Event], WatchStopReason]
PopenFactory = Callable[[Sequence[str]], subprocess.Popen[bytes]]


def _interrupt_child(child: subprocess.Popen[bytes]) -> None:
    if child.poll() is not None:
        return
    try:
        child.send_signal(signal.SIGINT)
        child.wait(timeout=2)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        child.terminate()
        child.wait(timeout=2)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        child.kill()
        child.wait()
    except OSError:
        pass


def _shell_exit_code(returncode: int) -> int:
    return 128 + abs(returncode) if returncode < 0 else returncode


def supervise(
    config: SupervisorConfig,
    *,
    watcher: Watcher = watch_title_changes,
    popen_factory: PopenFactory = subprocess.Popen,
) -> int:
    """Run the native TUI in the foreground and bound its watcher to it."""
    try:
        # No stdio, env, or process-group override: the TUI owns the pane PTY.
        child = popen_factory(config.open_command)
    except OSError as exc:
        raise SupervisorError(f"could not run {config.omnigent_executable!r}: {exc}") from exc

    stop_event = threading.Event()
    worker = threading.Thread(
        target=watcher,
        args=(config, stop_event),
        name=f"omnigent-herdr-title-{config.session_id[:24]}",
        daemon=True,
    )
    worker.start()
    try:
        return _shell_exit_code(child.wait())
    except KeyboardInterrupt:
        _interrupt_child(child)
        return 130
    finally:
        stop_event.set()
        worker.join(timeout=0.5)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True, help="exact Omnigent session id")
    parser.add_argument("--workspace", required=True, help="exact Herdr workspace id")
    parser.add_argument("--server", required=True, help="Omnigent server URL")
    parser.add_argument("--herdr", required=True, help="path to the Herdr executable")
    parser.add_argument("--omnigent", required=True, help="path to the Omnigent executable")
    parser.add_argument(
        "--initial-label",
        help="label already applied when this pane was launched",
    )
    parser.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL)
    parser.add_argument("--request-timeout", type=float, default=DEFAULT_REQUEST_TIMEOUT)
    parser.add_argument("--rename-timeout", type=float, default=DEFAULT_RENAME_TIMEOUT)
    parser.add_argument(
        "--max-retry-interval",
        type=float,
        default=DEFAULT_MAX_RETRY_INTERVAL,
    )
    return parser


def config_from_args(args: argparse.Namespace) -> SupervisorConfig:
    return SupervisorConfig(
        session_id=args.session,
        workspace_id=args.workspace,
        base_url=args.server.rstrip("/"),
        herdr_executable=str(Path(args.herdr).expanduser()),
        omnigent_executable=str(Path(args.omnigent).expanduser()),
        initial_label=args.initial_label,
        poll_interval=args.poll_interval,
        request_timeout=args.request_timeout,
        rename_timeout=args.rename_timeout,
        max_retry_interval=args.max_retry_interval,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = config_from_args(args)
    except ValueError as exc:
        parser.error(str(exc))
    try:
        return supervise(config)
    except SupervisorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 127


if __name__ == "__main__":
    raise SystemExit(main())
