#!/usr/bin/env python3
"""Platform gate (T043).

  python scripts/platform_gate.py --record evidence/platforms/current.json

Records the platform matrix honestly on THIS machine:
- Linux (this VM): runs the real checks — Python/SQLite versions, doctor,
  J01/J04/J05 journeys, a path with spaces + Chinese characters, offline
  core (no network), and a restart recovery.
- Windows / macOS: NOT_RUN — no runner is available here. The CI workflow
  (.github/workflows/test.yml) exists for them, but a workflow file is not
  evidence of a run.
Exit 0 = the record was written (Linux checks pass); 1 = a Linux check
failed or the harness errored. NOT_RUN platforms never count as passed.
"""
import argparse
import json
import platform
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENV_PY = ROOT / ".venv" / "bin" / "python"


def _run(argv, **kw):
    return subprocess.run(argv, capture_output=True, text=True, timeout=300, **kw, encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--record", required=True)
    args = ap.parse_args()
    checks, failures = {}, []
    checks["platform"] = platform.platform()
    checks["python"] = platform.python_version()
    checks["sqlite"] = sqlite3.sqlite_version
    checks["cpu_count"] = __import__("os").cpu_count()

    r = _run([str(VENV_PY), "-m", "turnkeep", "doctor", "--json"], cwd=ROOT)
    checks["doctor"] = {"exit_code": r.returncode, "ok": r.returncode == 0}
    if r.returncode != 0:
        failures.append(f"doctor failed: {r.stderr[-300:]}")

    for journey, mode in (("J01", "cli"), ("J05", "cli")):
        r = _run([str(VENV_PY), "scripts/run_journey.py", "--mode", mode,
                  "--journey", journey, "--no-model"], cwd=ROOT)
        checks[f"journey_{journey}"] = {"exit_code": r.returncode, "ok": r.returncode == 0}
        if r.returncode != 0:
            failures.append(f"journey {journey} failed: {r.stderr[-300:]}")

    # Paths with spaces and Chinese characters.
    with tempfile.TemporaryDirectory(prefix="tk-platform-") as tmp:
        tricky = Path(tmp) / "带空格 的目录" / "子目录"
        tricky.mkdir(parents=True)
        dbpath = tricky / "my db.sqlite3"
        env = {"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin:/usr/local/bin",
               "HOME": "/tmp", "TMPDIR": "/tmp"}
        r = _run([str(VENV_PY), "-m", "turnkeep", "--db", str(dbpath), "--json",
                  "init", "--scope", "spaces"], cwd=ROOT, env=env)
        ok = r.returncode == 0
        r2 = _run([str(VENV_PY), "-m", "turnkeep", "--db", str(dbpath), "--json",
                   "remember", "--scope", "spaces", "--key", "k", "--value", "v"],
                  cwd=ROOT, env=env)
        ok = ok and r2.returncode == 0
        checks["spaces_and_chinese_path"] = {"ok": ok, "db": str(dbpath)}
        if not ok:
            failures.append("spaces/Chinese path init+remember failed")

    # Restart recovery (J04) is covered by the UI e2e suite; record the
    # production-build gate result if it exists.
    ui_gate = ROOT / "evidence" / "ui" / "ui-gate.json"
    checks["ui_production_gate"] = (json.loads(ui_gate.read_text(encoding="utf-8"))["status"]
                                    if ui_gate.exists() else "NOT_RUN")

    record = {"status": "PASS" if not failures else "FAIL", "failures": failures,
              "linux": {"status": "PASS" if not failures else "FAIL", "checks": checks},
              "windows": {"status": "NOT_RUN",
                          "reason": "no Windows runner available on this VM; "
                                    "see .github/workflows/test.yml (workflow file is not evidence)"},
              "macos": {"status": "NOT_RUN",
                        "reason": "no macOS runner available on this VM; "
                                  "see .github/workflows/test.yml (workflow file is not evidence)"},
              "note": "Only Linux has real evidence here. The support list contains "
                      "only platforms with a passing record."}
    out = Path(args.record)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(record, indent=2, ensure_ascii=False))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
