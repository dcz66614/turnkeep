"""Release boundary tests (T045).

Covers the T045 acceptance invariants without reaching outside the
repository: licence/SBOM presence and lock agreement, the real
release-checks scan passing, loopback-only serving, brief redaction,
and secret exclusion on the import path.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent


def _parse_py_lock(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z0-9_.\-]+)==(.+)$", line)
        if m:
            out[m.group(1).lower()] = m.group(2)
    return out


def _load_sbom() -> dict:
    return json.loads((ROOT / "release" / "SBOM.json").read_text(encoding="utf-8"))


def test_license_notice_security_exist():
    lic = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "MIT License" in lic
    assert "Permission is hereby granted" in lic
    assert (ROOT / "NOTICE").exists()
    sec = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    assert "single" in sec.lower() and "trusted" in sec.lower()
    assert "retract" in sec.lower()
    assert "127.0.0.1" in sec


def test_sbom_matches_python_locks():
    sbom = _load_sbom()
    by_key = {(c["ecosystem"], c["name"].lower()): str(c["version"])
              for c in sbom["components"]}
    assert sbom["license_of_own_code"] == "MIT"
    for lock in ("requirements/base.lock", "requirements/mcp.lock"):
        for name, version in _parse_py_lock(ROOT / lock).items():
            assert by_key.get(("pypi", name)) == version, f"{lock}: {name}"


def test_sbom_matches_node_lock():
    sbom = _load_sbom()
    by_key = {(c["ecosystem"], c["name"].lower()): str(c["version"])
              for c in sbom["components"]}
    pl = json.loads((ROOT / "ui" / "package-lock.json").read_text(encoding="utf-8"))
    count = 0
    for key, val in pl["packages"].items():
        if key == "":
            continue
        name = key.replace("node_modules/", "")
        assert by_key.get(("npm", name.lower())) == str(val["version"]), name
        count += 1
    assert count >= 70  # 4 direct + 66 transitive, per ui/package-lock.json
    direct = {c["name"] for c in sbom["components"]
              if c["ecosystem"] == "npm" and c["direct"]}
    assert {"@playwright/test", "@types/node", "typescript", "vite"} <= direct


def test_sbom_has_no_copyleft_and_no_handwritten_versions():
    sbom = _load_sbom()
    assert sbom["counts"]["total"] == len(sbom["components"])
    for comp in sbom["components"]:
        lic = comp["license"]
        assert "GPL" not in lic and "AGPL" not in lic, comp
        assert comp["version"] not in (None, "", "UNKNOWN"), comp
    assert len(sbom["components"]) >= 100


def test_release_checks_pass():
    proc = subprocess.run(
        [sys.executable, "scripts/release_checks.py",
         "--profile", "license-and-secrets"],
        cwd=ROOT, capture_output=True, text=True, timeout=300, encoding="utf-8")
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-1000:]
    report = json.loads(proc.stdout)
    assert report["verdict"] == "PASS"
    assert report["summary"]["fail"] == 0
    assert report["files_scanned"] > 100
    assert report["limitations"], "scan must state its limitations"


def test_scan_secrets_windows_backslash_paths_find_no_hard():
    # R6 (CI #5): on Windows, str(Path) yields backslash rel paths,
    # but the allowlist suffixes and the top-dir split assumed forward
    # slashes, so every personal-path hit in evidence/state/tests/
    # fixtures became FAIL instead of INFO (23,619 hard findings) and
    # path-based allowlist entries stopped matching. Simulating the
    # Windows rel form must give the same result as the native run.
    spec = importlib.util.spec_from_file_location(
        "release_checks", ROOT / "scripts" / "release_checks.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    findings, whitelisted = [], []
    mod.scan_secrets(findings, whitelisted)
    hard_native = [f for f in findings
                   if f.get("severity") in ("HIGH", "FAIL")]
    assert hard_native == []
    orig_iter = mod._iter_files

    def win_iter():
        for path, rel in orig_iter():
            yield path, rel.replace("/", "\\")

    mod._iter_files = win_iter
    try:
        findings_w, whitelisted_w = [], []
        mod.scan_secrets(findings_w, whitelisted_w)
    finally:
        mod._iter_files = orig_iter
    hard_win = [f for f in findings_w
                if f.get("severity") in ("HIGH", "FAIL")]
    assert hard_win == []
    assert len(whitelisted_w) == len(whitelisted)


def test_loopback_only():
    app = (ROOT / "src" / "turnkeep" / "web" / "app.py").read_text(encoding="utf-8")
    assert '("127.0.0.1"' in app
    assert not re.search(r'\(\s*["\']0\.0\.0\.0["\']', app)
    cli = (ROOT / "src" / "turnkeep" / "cli.py").read_text(encoding="utf-8")
    assert "http://127.0.0.1:" in cli


def test_brief_redaction_hides_fake_secret():
    from turnkeep.briefs import redact

    out = redact("api_key: sk-live-123456 and some normal text")
    assert "sk-live-123456" not in out
    assert "[REDACTED]" in out
    assert "some normal text" in out


def test_import_path_excludes_secret_like_keys():
    from turnkeep.portability import _SECRET_LIKE
    from turnkeep.importers.markdown import _SECRET_LIKE as _MD_SECRET_LIKE

    for keys in (_SECRET_LIKE, _MD_SECRET_LIKE):
        for k in ("api_key", "password", "token", "secret"):
            assert k in keys
    port = (ROOT / "src" / "turnkeep" / "portability.py").read_text(encoding="utf-8")
    assert "imported_text" in port


def test_security_md_states_release_boundaries():
    sec = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    # Perf FAIL + platform NOT_RUN are release boundaries, not vulns.
    assert "7,760" in sec and "12,288" in sec
    assert "NOT_RUN" in sec
    assert "production-safe" in sec or "production safety" in sec
