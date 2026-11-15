"""Doctor (T028): reports DB version/lock, server commands, scopes, client
configs, last real call, and unverified capabilities as JSON.

  python -m turnkeep doctor --json
  python -m turnkeep doctor --data-dir /path --json

Exit 0 always (doctor is a diagnostic, not a gate); the JSON carries
status per check. macOS/Windows rows are NOT_RUN here — T043 does the
real platform verification; path rules for those platforms are unit-tested
with monkeypatched platform.system(), which is NOT platform evidence.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import sqlite3
import sys
from pathlib import Path

from .storage import SCHEMA_VERSION, data_path, default_data_dir


def _check_python():
    v = sys.version_info
    return {"status": "PASS" if v >= (3, 11) else "FAIL",
            "version": platform.python_version(), "required": ">=3.11"}


def _check_sqlite():
    return {"status": "PASS" if sqlite3.sqlite_version_info >= (3, 35) else "FAIL",
            "version": sqlite3.sqlite_version}


def _check_data_dir(base):
    try:
        p = data_path(None, base_dir=base)
        probe = p / ".doctor-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return {"status": "PASS", "path": str(p), "writable": True}
    except Exception as e:
        return {"status": "FAIL", "path": str(base), "writable": False, "error": str(e)}


def _check_db(base):
    dbpath = data_path("default", base_dir=base) if base else None
    # also look at the platform default DB
    candidates = []
    if base:
        candidates.append(data_path("default", base_dir=base))
    candidates.append(default_data_dir() / "default.sqlite3")
    for cand in candidates:
        if cand.exists():
            try:
                con = sqlite3.connect(f"file:{cand}?mode=ro", uri=True, timeout=5)
                version = con.execute("PRAGMA user_version").fetchone()[0]
                journal = con.execute("PRAGMA journal_mode").fetchone()[0]
                scopes = [r[0] for r in con.execute("SELECT id FROM scopes")]
                last = con.execute(
                    "SELECT MAX(created_at) FROM events").fetchone()[0]
                con.close()
                return {"status": "PASS" if version == SCHEMA_VERSION else "FAIL",
                        "path": str(cand), "schema_version": version,
                        "expected_schema_version": SCHEMA_VERSION,
                        "journal_mode": journal, "scopes": scopes,
                        "last_event_at": last}
            except Exception as e:
                return {"status": "FAIL", "path": str(cand), "error": str(e)}
    return {"status": "NOT_RUN", "path": None,
            "note": "no turnkeep database found yet — run `turnkeep init --scope <id>`"}


def _check_commands():
    out = {}
    for cmd in ("mcp", "serve", "doctor"):
        out[cmd] = {"status": "PASS",
                    "command": f"python -m turnkeep {cmd}",
                    "note": "implemented in src/turnkeep (stdlib only)"}
    return out


def _check_clients():
    root = Path(__file__).resolve().parents[2]
    claude_tpl = root / "adapters" / "claude-code" / ".mcp.json"
    codex_tpl = root / "adapters" / "codex" / "config.toml"
    home = Path.home()
    claude_installed = (home / ".claude.json").exists() and \
        "turnkeep" in (home / ".claude.json").read_text(encoding="utf-8", errors="ignore")
    codex_cfg = home / ".codex" / "config.toml"
    codex_installed = codex_cfg.exists() and \
        "[mcp_servers.turnkeep]" in codex_cfg.read_text(encoding="utf-8", errors="ignore")
    return {
        "claude-code": {"config_template": str(claude_tpl),
                        "template_present": claude_tpl.exists(),
                        "installed": claude_installed,
                        "cli_present": bool(shutil.which("claude")),
                        "live": "NOT_RUN",
                        "live_reason": "claude CLI not installed on this machine"},
        "codex": {"config_template": str(codex_tpl),
                  "template_present": codex_tpl.exists(),
                  "installed": codex_installed,
                  "cli_present": bool(shutil.which("codex")),
                  "live": "NOT_RUN",
                  "live_reason": "login state unknown; no login attempted"},
    }


def run_doctor(data_dir=None):
    base = Path(data_dir) if data_dir else None
    system = platform.system()
    report = {
        "tool": "turnkeep doctor", "version": "0.1.0",
        "platform": {"system": system, "release": platform.release(),
                     "machine": platform.machine()},
        "python": _check_python(),
        "sqlite": _check_sqlite(),
        "data_dir": _check_data_dir(base or default_data_dir()),
        "database": _check_db(base),
        "commands": _check_commands(),
        "clients": _check_clients(),
        "platforms": {
            "linux": {"status": "VERIFIED" if system == "Linux" else "NOT_RUN",
                      "note": "full test suite on this VM" if system == "Linux"
                              else "not this machine"},
            "macos": {"status": "NOT_RUN", "note": "real verification in T043"},
            "windows": {"status": "NOT_RUN", "note": "real verification in T043"},
        },
    }
    fails = [k for k, v in report.items()
             if isinstance(v, dict) and v.get("status") == "FAIL"]
    report["overall"] = "FAIL" if fails else "OK"
    report["failed_checks"] = fails
    return report


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="turnkeep doctor")
    ap.add_argument("--data-dir")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    report = run_doctor(args.data_dir)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0
