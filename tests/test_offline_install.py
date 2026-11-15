"""R2 (A5 / P1-f) offline-install regression: a fresh install must work
with NO network. The old scripts/install.sh ran `pip install -e .`,
which fails offline because pip's isolated build env must download
setuptools>=68 (pyproject.toml build-system requires). The fix installs
from the shipped wheel (release/artifacts/turnkeep-0.1.0-py3-none-any.whl)
when offline. This test reproduces the failure mode exactly: a copy of
the checkout in a temp dir, fresh venv, PIP_NO_INDEX=1, then
`turnkeep doctor` must pass."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WHEEL_REL = Path("release/artifacts/turnkeep-0.1.0-py3-none-any.whl")

# Directories/files that are not part of an installed checkout (or are
# deliberately excluded from the release zip) — skip them so the copy
# stays small and simulates an extracted zip.
_SKIP_DIRS = {".venv", ".git", "__pycache__", ".pytest_cache",
              "node_modules", "ui/dist", "state", "evidence"}
_SKIP_FILES = {"TURNKEEP_RC.zip"}


def _ignore(path, names):
    ignored = []
    for name in names:
        p = Path(path) / name
        if p.is_dir() and name in _SKIP_DIRS:
            ignored.append(name)
        elif p.is_file() and name in _SKIP_FILES:
            ignored.append(name)
        elif name.endswith((".db", ".sqlite3", ".db-wal", ".db-shm")):
            ignored.append(name)
    return ignored


def _usable_bash():
    """True when a real bash exists. On Windows without WSL, "bash"
    resolves to C:\\Windows\\System32\\bash.exe — the WSL launcher —
    which fails with "Windows Subsystem for Linux has no installed
    distributions."; install.sh cannot run there, so the test must
    skip instead of failing on the launcher's error."""
    bash = shutil.which("bash")
    if not bash:
        return False
    try:
        r = subprocess.run([bash, "--version"], capture_output=True,
                           text=True, encoding="utf-8", timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def test_offline_install_from_shipped_wheel(tmp_path):
    if not _usable_bash():
        pytest.skip("no usable bash (no WSL distribution on Windows)")
    if not (ROOT / WHEEL_REL).is_file():
        pytest.skip(f"shipped wheel missing: {WHEEL_REL}")
    copy = tmp_path / "checkout"
    shutil.copytree(ROOT, copy, ignore=_ignore)
    assert (copy / WHEEL_REL).is_file()
    assert (copy / "scripts" / "install.sh").is_file()

    env = dict(os.environ)
    env["PIP_NO_INDEX"] = "1"  # no network, no index: the R2 repro condition
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env["TURNKEEP_DATA_DIR"] = str(tmp_path / "data")
    env["PYTHON"] = sys.executable

    r = subprocess.run(["bash", "scripts/install.sh"], cwd=str(copy),
                       capture_output=True, text=True, timeout=600, env=env, encoding="utf-8")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "turnkeep installed as" in r.stdout

    venv_bin = copy / ".venv" / "bin"
    assert (venv_bin / "turnkeep").is_file()
    ver = subprocess.run([str(venv_bin / "turnkeep"), "--version"],
                         capture_output=True, text=True, timeout=60, env=env, encoding="utf-8")
    assert ver.returncode == 0, ver.stdout + ver.stderr
    assert "0.1.0" in ver.stdout + ver.stderr

    doctor = subprocess.run([str(venv_bin / "turnkeep"), "doctor", "--json"],
                            capture_output=True, text=True, timeout=60, env=env, encoding="utf-8")
    assert doctor.returncode == 0, doctor.stdout + doctor.stderr
    report = json.loads(doctor.stdout)
    assert report["python"]["status"] == "PASS"
    assert report["sqlite"]["status"] == "PASS"
