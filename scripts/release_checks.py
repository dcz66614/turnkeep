#!/usr/bin/env python3
"""Turnkeep release checks (T045): licence + SBOM + secrets + boundary.

Profile: ``license-and-secrets`` (the only supported profile).

This script REALLY scans: it walks the working tree, applies secret
patterns, verifies release/SBOM.json against requirements/*.lock and
ui/package-lock.json, and checks the loopback / redaction / import
boundaries in source. It prints a JSON report on stdout and exits 0
only when there are no FAIL findings. A PASS means "this scan found
nothing at this commit" — it is NOT a claim of production safety;
see SECURITY.md for the limitations.
"""

from __future__ import annotations

import argparse
import importlib
import json
import re
import sys
from pathlib import Path

TOOL = "release_checks"
TOOL_VERSION = "1.0.0 (T045)"
PROFILE = "license-and-secrets"

ROOT = Path(__file__).resolve().parent.parent

EXCLUDE_DIRS = {
    ".venv", ".git", "__pycache__", ".tmp", ".pytest_cache",
    "node_modules", "dist",
}
# Files under these top-level dirs are working records, not shipped
# content. Personal-path hits there are INFO, not FAIL.
WORKING_RECORD_DIRS = {"evidence", "state"}

SECRET_PATTERNS = [
    ("sk-live/sk-proj key", re.compile(r"sk-(?:live|proj)-[A-Za-z0-9]{6,}")),
    ("github token", re.compile(r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}")),
    ("github fine-grained token", re.compile(r"github_pat_[A-Za-z0-9_]{20,}")),
    ("aws access key id", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("slack token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("private key block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |)PRIVATE KEY-----")),
    ("api_key assignment", re.compile(r"api[_-]?key\s*=\s*['\"][^'\"\s]{8,}['\"]", re.I)),
    ("password assignment", re.compile(r"password\s*=\s*['\"][^'\"\s]{4,}['\"]", re.I)),
    ("bearer token", re.compile(r"[Bb]earer\s+[A-Za-z0-9\-._~+/=]{20,}")),
]

PERSONAL_PATH_PATTERNS = [
    ("unix home path", re.compile(r"/home/[A-Za-z0-9_.-]+")),
    ("mac home path", re.compile(r"/Users/[A-Za-z0-9_.-]+")),
    ("windows user path", re.compile(r"C:\\Users\\[A-Za-z0-9_.-]+", re.I)),
    ("github username", re.compile(r"dcz66614")),
]

# (path suffix, matched-text substring, reason) — fake/test secrets only.
ALLOWLIST = [
    ("scripts/check_docs.py", "dcz66614", "scanner's own leak-pattern list"),
    ("scripts/release_checks.py", "dcz66614", "scanner's own leak-pattern list"),
    ("scripts/check_docs.py", "AKIA", "scanner's own leak-pattern list"),
    ("tests/core/test_briefs.py", "sk-live-123456", "fake secret in redaction test"),
    ("tests/data/test_import_injection.py", "sk-live-999", "fake secret in injection fixture test"),
    ("fixtures/imports/markdown-sample/injection.md", "sk-live-999", "malicious-import fixture, fake secret"),
    ("fixtures/imports/markdown-sample/injection.md", "hunter2", "malicious-import fixture, fake password"),
    ("scripts/check_erasure.py", "SYNTHETIC-SECRET", "synthetic erasure fixture, not a real secret"),
    ("tests/privacy/test_erasure.py", "SYNTHETIC-SECRET", "synthetic erasure fixture, not a real secret"),
    ("tests/storage/test_restore_erasure.py", "SYNTHETIC-SECRET", "synthetic erasure fixture, not a real secret"),
]

FAKE_MARKERS = ("<redacted>", "SYNTHETIC", "sk-live-123456", "sk-live-999",
                "example", "placeholder", "fake")


def _is_allowlisted(rel: str, matched: str) -> str | None:
    for suffix, needle, reason in ALLOWLIST:
        if rel.endswith(suffix) and needle in matched:
            return reason
    if any(m in matched for m in FAKE_MARKERS):
        return "fake/placeholder marker in matched text"
    return None


def _iter_files():
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT)
        if any(part in EXCLUDE_DIRS for part in rel.parts):
            continue
        if rel.suffix in {".pyc", ".pyo"}:
            continue
        yield path, str(rel)


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="strict")
    except (UnicodeDecodeError, OSError):
        return None


def scan_secrets(findings, whitelisted):
    scanned = 0
    for path, rel in _iter_files():
        # str(Path) uses backslashes on Windows, but the allowlist
        # suffixes and the top-dir split below assume forward slashes;
        # without this, every personal-path hit in evidence/state/
        # tests/fixtures becomes FAIL instead of INFO on Windows
        # (CI #5: 23,619 hard findings) and path-based allowlist
        # entries stop matching.
        rel = rel.replace("\\", "/")
        text = _read_text(path)
        if text is None:
            continue  # binary / unreadable: recorded as a limitation
        scanned += 1
        for line_no, line in enumerate(text.splitlines(), 1):
            for label, pat in SECRET_PATTERNS:
                for m in pat.finditer(line):
                    matched = m.group(0)
                    reason = _is_allowlisted(rel, matched)
                    entry = {"file": rel, "line": line_no, "kind": label,
                             "match": matched[:40]}
                    if reason:
                        entry["reason"] = reason
                        whitelisted.append(entry)
                    else:
                        entry["severity"] = "HIGH"
                        findings.append(entry)
            for label, pat in PERSONAL_PATH_PATTERNS:
                for m in pat.finditer(line):
                    matched = m.group(0)
                    reason = _is_allowlisted(rel, matched)
                    if reason:
                        whitelisted.append({"file": rel, "line": line_no,
                                            "kind": label, "match": matched[:40],
                                            "reason": reason})
                        continue
                    top = rel.split("/")[0]
                    if top in WORKING_RECORD_DIRS:
                        findings.append({"file": rel, "line": line_no, "kind": label,
                                         "match": matched[:40], "severity": "INFO",
                                         "note": "working record (evidence/state); must be "
                                                 "excluded from the shipped package (T046)"})
                    elif top in ("tests", "fixtures"):
                        findings.append({"file": rel, "line": line_no, "kind": label,
                                         "match": matched[:40], "severity": "INFO",
                                         "note": "test/fixture path: synthetic fixture value or "
                                                 "hardcoded test-runner cwd for this VM; not "
                                                 "shipped user data, but non-portable — see "
                                                 "SECURITY.md risk table"})
                    else:
                        findings.append({"file": rel, "line": line_no, "kind": label,
                                         "match": matched[:40], "severity": "FAIL",
                                         "note": "personal path/identifier outside working records"})
    return scanned


def _parse_py_lock(path: Path) -> dict:
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z0-9_.\-]+)==(.+)$", line)
        if m:
            out[m.group(1).lower()] = m.group(2)
    return out


def check_sbom(findings):
    sbom_path = ROOT / "release" / "SBOM.json"
    if not sbom_path.exists():
        findings.append({"kind": "sbom-missing", "severity": "FAIL",
                         "file": "release/SBOM.json"})
        return
    try:
        sbom = json.loads(sbom_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        findings.append({"kind": "sbom-invalid-json", "severity": "FAIL",
                         "file": "release/SBOM.json", "note": str(exc)})
        return
    by_key = {}
    for comp in sbom.get("components", []):
        by_key[(comp.get("ecosystem"), comp.get("name", "").lower())] = comp.get("version")
    for lock in ("requirements/base.lock", "requirements/mcp.lock"):
        for name, version in _parse_py_lock(ROOT / lock).items():
            got = by_key.get(("pypi", name))
            if got is None:
                findings.append({"kind": "sbom-missing-dep", "severity": "FAIL",
                                 "file": lock, "note": f"{name}=={version} not in SBOM"})
            elif str(got) != version:
                findings.append({"kind": "sbom-version-mismatch", "severity": "FAIL",
                                 "file": lock,
                                 "note": f"{name}: lock {version} vs SBOM {got}"})
    pl_path = ROOT / "ui" / "package-lock.json"
    if pl_path.exists():
        pl = json.loads(pl_path.read_text(encoding="utf-8"))
        for key, val in pl.get("packages", {}).items():
            if key == "":
                continue
            name = key.replace("node_modules/", "")
            got = by_key.get(("npm", name.lower()))
            if got is None:
                findings.append({"kind": "sbom-missing-dep", "severity": "FAIL",
                                 "file": "ui/package-lock.json",
                                 "note": f"npm {name}@{val.get('version')} not in SBOM"})
            elif str(got) != str(val.get("version")):
                findings.append({"kind": "sbom-version-mismatch", "severity": "FAIL",
                                 "file": "ui/package-lock.json",
                                 "note": f"npm {name}: lock {val.get('version')} vs SBOM {got}"})


def check_files(findings):
    lic = ROOT / "LICENSE"
    if not lic.exists() or "MIT License" not in lic.read_text(encoding="utf-8", errors="ignore"):
        findings.append({"kind": "license", "severity": "FAIL",
                         "file": "LICENSE", "note": "MIT licence text missing"})
    for name in ("NOTICE", "SECURITY.md", "release/SBOM.json"):
        if not (ROOT / name).exists():
            findings.append({"kind": "missing-file", "severity": "FAIL", "file": name})


def check_boundaries(findings):
    # All reads below are UTF-8 (the default locale encoding on Windows
    # is cp1252, which cannot decode the non-ASCII bytes in these files).
    app = ROOT / "src" / "turnkeep" / "web" / "app.py"
    text = app.read_text(encoding="utf-8") if app.exists() else ""
    if '("127.0.0.1"' not in text:
        findings.append({"kind": "loopback-bind", "severity": "FAIL",
                         "file": "src/turnkeep/web/app.py",
                         "note": "no 127.0.0.1 bind found"})
    if re.search(r'\(\s*["\']0\.0\.0\.0["\']', text):
        findings.append({"kind": "wildcard-bind", "severity": "FAIL",
                         "file": "src/turnkeep/web/app.py",
                         "note": "0.0.0.0 bind tuple present"})
    cli = ROOT / "src" / "turnkeep" / "cli.py"
    if cli.exists() and "http://127.0.0.1:" not in cli.read_text(encoding="utf-8"):
        findings.append({"kind": "loopback-url", "severity": "FAIL",
                         "file": "src/turnkeep/cli.py",
                         "note": "serve URL is not loopback"})
    # Functional redaction check (imports the real function).
    sys.path.insert(0, str(ROOT / "src"))
    try:
        briefs = importlib.import_module("turnkeep.briefs")
        out = briefs.redact("api_key: sk-live-123456 and ok")
        if "sk-live-123456" in out or "[REDACTED]" not in out:
            findings.append({"kind": "redaction", "severity": "FAIL",
                             "file": "src/turnkeep/briefs.py",
                             "note": "redact() did not hide the test secret"})
    except Exception as exc:  # noqa: BLE001 — report, don't crash
        findings.append({"kind": "redaction", "severity": "FAIL",
                         "file": "src/turnkeep/briefs.py", "note": f"import/call failed: {exc}"})
    finally:
        sys.path.pop(0)
    for rel in ("src/turnkeep/portability.py", "src/turnkeep/importers/markdown.py"):
        p = ROOT / rel
        t = p.read_text(encoding="utf-8") if p.exists() else ""
        if not all(k in t for k in ("api_key", "password", "token", "secret")):
            findings.append({"kind": "secret-exclusion", "severity": "FAIL",
                             "file": rel, "note": "_SECRET_LIKE keys incomplete"})
    port = ROOT / "src" / "turnkeep" / "portability.py"
    if port.exists() and "imported_text" not in port.read_text(encoding="utf-8"):
        findings.append({"kind": "import-trust", "severity": "FAIL",
                         "file": "src/turnkeep/portability.py",
                         "note": "imported_text source kind not referenced"})


def main(argv=None) -> int:
    # The JSON report can contain non-ASCII matched text; force UTF-8
    # stdout so printing it does not fail on Windows (cp1252 default).
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001 - stdout may be replaced/closed in tests
        pass
    ap = argparse.ArgumentParser(description="Turnkeep release checks (T045)")
    ap.add_argument("--profile", required=True)
    ap.add_argument("--output", default=None, help="write JSON report to this path")
    args = ap.parse_args(argv)
    if args.profile != PROFILE:
        print(json.dumps({"tool": TOOL, "tool_version": TOOL_VERSION,
                          "profile": args.profile, "verdict": "FAIL",
                          "error": f"unsupported profile; only {PROFILE!r}"},
                         ensure_ascii=False), file=sys.stderr)
        return 2
    findings: list[dict] = []
    whitelisted: list[dict] = []
    scanned = scan_secrets(findings, whitelisted)
    check_files(findings)
    check_sbom(findings)
    check_boundaries(findings)
    hard = [f for f in findings if f.get("severity") in ("HIGH", "FAIL")]
    verdict = "FAIL" if hard else "PASS"
    report = {
        "tool": TOOL,
        "tool_version": TOOL_VERSION,
        "python": sys.version.split()[0],
        "profile": PROFILE,
        "verdict": verdict,
        "files_scanned": scanned,
        "findings": findings,
        "whitelisted_findings": whitelisted,
        "summary": {
            "fail": len(hard),
            "info": len([f for f in findings if f.get("severity") == "INFO"]),
            "whitelisted": len(whitelisted),
        },
        "limitations": [
            "Pattern scan only: secrets in unrecognised forms, binary files, or user data written later into a database are not detected.",
            "Unreadable/binary files are skipped, not inspected.",
            "No dependency vulnerability scan (pip-audit / npm audit) is performed by this script; see SECURITY.md.",
            "A PASS here is not a claim of production safety; see SECURITY.md.",
        ],
    }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
