"""Regression: claude-sdk must not receive a ``.py`` executable on native Windows.

On native Windows with the default ``windows_jobobject`` sandbox backend,
``prepare_claude_cli_path()`` wraps the Claude CLI via ``create_exec_launcher()``,
which returns a tempfile ``omnigent-sandbox-*.py`` path. The Claude Agent SDK
uses that string as the CLI executable (``SubprocessCLITransport`` execs it as
``argv[0]``), and Windows ``CreateProcess`` cannot execute a Python source
file — every claude-sdk session dies before connect with
``OSError: [WinError 193] %1 is not a valid Win32 application``.

User journey: on native Windows, configure a claude-sdk agent with the default
os_env sandbox → create a session and send the first message → the session
fails before connect with WinError 193.

The invariant guarded here: for the ``windows_jobobject`` backend, the CLI path
``prepare_claude_cli_path()`` hands the SDK must be in a form Windows
``CreateProcess`` can execute (the raw CLI itself, or a real executable shim) —
never a bare ``.py`` script. The backend enforces nothing at launcher run time
anyway (its ``activate()`` is a documented no-op; containment is applied by the
parent via ``post_spawn``), so the script launcher adds no sandboxing on
Windows — only the pre-connect crash.

The cross-platform test drives the real ``prepare_claude_cli_path()`` resolve
path using the ``windows_jobobject`` module's own ``os_name`` test seam, so it
runs (and fails on the bug) on POSIX CI too. The ``windows_only`` test
additionally proves the exec form for real by spawning the prepared path.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
from unittest.mock import patch

import pytest

from omnigent.inner import windows_jobobject_sandbox as windows_jobobject
from omnigent.inner.claude_sdk_executor import prepare_claude_cli_path
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec

# Exec forms Windows can start directly: PE images via CreateProcess, plus the
# batch forms CreateProcess special-cases through cmd.exe. A bare ``.py``
# source file is none of these — passing one as argv[0] raises WinError 193.
_WINDOWS_EXECUTABLE_SUFFIXES = {".exe", ".com", ".bat", ".cmd"}


def _windows_jobobject_spec(cwd: pathlib.Path) -> OSEnvSpec:
    """The agent-facing os_env spec a native-Windows user gets by default."""
    return OSEnvSpec(
        type="caller_process",
        cwd=str(cwd),
        sandbox=OSEnvSandboxSpec(
            type="windows_jobobject",
            write_paths=["."],
            allow_network=True,
        ),
    )


def _prepare_cli_for_windows_backend(
    real_cli_path: str, cwd: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[str | None, bool]:
    """Run the real ``prepare_claude_cli_path`` resolve path for the Windows backend.

    On Windows this is the untouched production path. On POSIX the
    ``windows_jobobject`` module's ``os_name`` seam (an indirection the module
    itself documents as "so tests can monkeypatch the platform check") lets the
    backend resolve, exercising the same code that runs on native Windows —
    nothing inside ``prepare_claude_cli_path`` or the sandbox module is mocked.
    """
    spec = _windows_jobobject_spec(cwd)
    # An ambient sandbox bypass would short-circuit prepare_claude_cli_path
    # before the backend branch under test, masking the regression.
    monkeypatch.delenv("OMNIGENT_CLAUDE_SDK_NO_SANDBOX", raising=False)
    if sys.platform == "win32":
        prepared = prepare_claude_cli_path(real_cli_path, spec)
    else:
        with patch.object(windows_jobobject, "os_name", lambda: "nt"):
            prepared = prepare_claude_cli_path(real_cli_path, spec)
    return prepared.cli_path, prepared.enable_native_tools


def test_prepare_claude_cli_path_never_returns_dot_py_for_windows_jobobject(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI path handed to the Claude Agent SDK must be Windows-executable.

    Fails on the bug: ``prepare_claude_cli_path`` returns an
    ``omnigent-sandbox-*.py`` launcher, which ``CreateProcess`` rejects with
    WinError 193, killing every claude-sdk session before connect.
    """
    real_cli = sys.executable  # stands in for the installed Claude CLI binary
    cli_path, _native_tools = _prepare_cli_for_windows_backend(real_cli, tmp_path, monkeypatch)

    assert cli_path is not None
    suffix = pathlib.Path(cli_path).suffix.lower()
    assert suffix != ".py", (
        f"prepare_claude_cli_path handed the Claude Agent SDK {cli_path!r} under "
        "the windows_jobobject backend. Windows CreateProcess cannot execute a "
        ".py source file, so the SDK's SubprocessCLITransport fails before "
        "connect with 'OSError: [WinError 193] %1 is not a valid Win32 "
        "application'. Return the raw CLI or a Windows-executable shim instead."
    )
    # The path must be the raw CLI itself or an exec form Windows can start.
    assert cli_path == real_cli or suffix in _WINDOWS_EXECUTABLE_SUFFIXES, (
        f"prepare_claude_cli_path returned {cli_path!r} (suffix {suffix!r}) for "
        "the windows_jobobject backend; Windows CreateProcess can only start "
        f"{sorted(_WINDOWS_EXECUTABLE_SUFFIXES)} images (or the CLI binary "
        "itself)."
    )


@pytest.mark.windows_only
def test_prepared_cli_path_is_spawnable_on_native_windows(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spawning the prepared CLI path must not raise WinError 193.

    Mirrors what the Claude Agent SDK does with ``options.cli_path``: use it as
    ``argv[0]`` of a subprocess. On the bug this raises
    ``OSError: [WinError 193] %1 is not a valid Win32 application`` because the
    prepared path is a Python source file.
    """
    real_cli = sys.executable  # a known-good Windows .exe standing in for claude
    cli_path, _native_tools = _prepare_cli_for_windows_backend(real_cli, tmp_path, monkeypatch)
    assert cli_path is not None

    try:
        completed = subprocess.run(
            [cli_path, "--version"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except OSError as exc:
        pytest.fail(
            f"CreateProcess rejected the prepared Claude CLI path {cli_path!r}: "
            f"{exc}. This is the pre-connect crash — the Claude Agent "
            "SDK execs this exact path as argv[0]."
        )
    assert completed.returncode == 0, (
        f"Prepared CLI path {cli_path!r} spawned but exited "
        f"rc={completed.returncode}; stderr: {completed.stderr[-500:]}"
    )
