"""The Security Scan workflow's OSV advisory step must be able to export uv.lock.

The ``OSV advisory scan (uv.lock)`` step in
``.github/workflows/security-scan.yml`` exports the lockfile to a
requirements file and feeds it to ``pip-audit``. If the export itself cannot
resolve (e.g. ``--all-extras`` collides with extras declared mutually
exclusive under ``[tool.uv] conflicts``), the step exits before any advisory
is evaluated and every PR that touches ``uv.lock`` fails the Security Gate.

These tests execute the step's real ``run`` body against the repository's
real ``pyproject.toml``/``uv.lock``, with ``pip-audit`` stubbed out (no
network), and assert:

1. the step body exits 0 — the export resolves; and
2. the union of requirements handed to ``pip-audit`` still covers both sides
   of the declared extra conflict (``google-antigravity`` and ``cwsandbox``),
   so a fix cannot silently drop a conflicting extra from the audit.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import tomllib
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github/workflows/security-scan.yml"
OSV_STEP_NAME = "OSV advisory scan (uv.lock)"

FAKE_UVX = """\
#!/usr/bin/env python3
\"\"\"pip-audit stand-in: record each --requirement file, always succeed.\"\"\"
import os
import shutil
import sys

capture = os.environ["OSV_AUDIT_CAPTURE"]
os.makedirs(capture, exist_ok=True)
call = len([f for f in os.listdir(capture) if f.endswith(".args")])
args = sys.argv[1:]
with open(os.path.join(capture, f"call-{call}.args"), "w") as f:
    f.write("\\n".join(args))
for i, arg in enumerate(args):
    if arg in ("--requirement", "-r") and i + 1 < len(args):
        shutil.copy(args[i + 1], os.path.join(capture, f"req-{call}-{i}.txt"))
sys.exit(0)
"""


def _workflow_text() -> str:
    """Return the workflow source, falling back to HEAD when the working
    tree copy is absent (some sandboxes strip ``.github/``)."""
    if WORKFLOW.is_file():
        return WORKFLOW.read_text()
    proc = subprocess.run(
        ["git", "show", "HEAD:.github/workflows/security-scan.yml"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        pytest.skip("security-scan.yml not available on disk or at HEAD")
    return proc.stdout


def _osv_step_body() -> str:
    """Extract the OSV step's ``run`` body from the workflow."""
    workflow = yaml.safe_load(_workflow_text())
    for job in workflow["jobs"].values():
        for step in job.get("steps", []):
            if step.get("name") == OSV_STEP_NAME:
                return step["run"]
    pytest.fail(f"step {OSV_STEP_NAME!r} not found in security-scan.yml")


def _run_osv_step(tmp_path: Path) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Execute the step body as the workflow would, with pip-audit stubbed.

    :param tmp_path: Pytest tmp dir for the fake GITHUB_WORKSPACE, the stub
        ``uvx`` binary, and the audit capture dir.
    :returns: The finished process and the capture dir holding the
        requirement files the stub ``pip-audit`` received.
    """
    if shutil.which("uv") is None:
        pytest.skip("uv not on PATH")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    # The step only proceeds when uv.lock is listed in the PR changeset.
    (workspace / "changed.txt").write_text("uv.lock\n")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    uvx = fake_bin / "uvx"
    uvx.write_text(FAKE_UVX)
    uvx.chmod(uvx.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    capture = tmp_path / "audit-capture"

    env = os.environ.copy()
    env["GITHUB_WORKSPACE"] = str(workspace)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    env["OSV_AUDIT_CAPTURE"] = str(capture)

    # GitHub runs `run:` blocks with `bash -e`; working-directory is the PR
    # head checkout, which for this test is the repo root itself.
    proc = subprocess.run(
        ["bash", "-e", "-c", _osv_step_body()],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    return proc, capture


def test_osv_step_export_resolves(tmp_path: Path) -> None:
    """The step body exits 0 on the current lockfile.

    Guards against the export dying before pip-audit ever runs (e.g.
    ``--all-extras`` tripping over extras declared conflicting in
    ``[tool.uv] conflicts``), which fails the Security Gate on every PR
    that touches uv.lock.
    """
    proc, _ = _run_osv_step(tmp_path)
    assert proc.returncode == 0, (
        f"OSV step body exited {proc.returncode} before auditing anything:\n"
        f"stderr:\n{proc.stderr}\nstdout:\n{proc.stdout}"
    )


def test_osv_step_audits_both_sides_of_extra_conflict(tmp_path: Path) -> None:
    """Every conflicting extra's pins still reach pip-audit.

    ``antigravity`` conflicts with ``cwsandbox`` (among others), so no single
    export can carry both — but the audit as a whole must. Asserts the union
    of requirement files handed to pip-audit pins both packages, so a fix
    can't restore green by silently dropping one extra from the scan.
    """
    proc, capture = _run_osv_step(tmp_path)
    assert proc.returncode == 0, (
        f"OSV step body exited {proc.returncode}; pip-audit never ran:\nstderr:\n{proc.stderr}"
    )

    req_files = sorted(capture.glob("req-*.txt")) if capture.is_dir() else []
    assert req_files, "pip-audit was never invoked with a --requirement file"
    audited = "\n".join(f.read_text() for f in req_files)
    for pkg in ("google-antigravity==", "cwsandbox=="):
        assert pkg in audited, (
            f"{pkg.rstrip('=')} pins never reached pip-audit; the audit "
            f"silently dropped one side of the declared extra conflict"
        )
    # No editable locals may leak through (pip-audit can't hash them).
    assert "\n-e " not in f"\n{audited}", "editable requirement leaked into audit"


def _audited_pins(capture: Path) -> set[tuple[str, str]]:
    """Normalized ``(name, version)`` pins across all captured req files."""
    pins: set[tuple[str, str]] = set()
    for req in sorted(capture.glob("req-*.txt")):
        for line in req.read_text().splitlines():
            match = re.match(r"^([A-Za-z0-9_.\-]+)==([^ \\;]+)", line.strip())
            if match:
                pins.add((match.group(1).lower().replace("_", "-"), match.group(2)))
    return pins


def test_osv_step_audits_every_locked_registry_pin(tmp_path: Path) -> None:
    """The audit's union covers every registry ``(package, version)`` in uv.lock.

    Extras declared mutually exclusive force the scan to export multiple
    resolution sets; this pins the invariant that no locked registry package
    slips through the gaps between those sets — including packages whose
    pinned version differs per set — even as extras and conflicts evolve.
    """
    proc, capture = _run_osv_step(tmp_path)
    assert proc.returncode == 0, (
        f"OSV step body exited {proc.returncode}; pip-audit never ran:\nstderr:\n{proc.stderr}"
    )

    audited = _audited_pins(capture)
    with (REPO_ROOT / "uv.lock").open("rb") as fh:
        lock = tomllib.load(fh)
    locked = {
        (pkg["name"].lower().replace("_", "-"), pkg["version"])
        for pkg in lock.get("package", [])
        # Non-registry packages (workspace members, editable locals) are
        # intentionally excluded from the audit: OSV has no advisories for
        # local source and pip-audit cannot hash them.
        if "registry" in pkg.get("source", {})
    }
    missing = locked - audited
    assert not missing, (
        f"{len(missing)} locked registry pins never reached pip-audit: {sorted(missing)[:10]}"
    )
