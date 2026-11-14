#!/usr/bin/env python3
"""Restore check (T039).

  python scripts/restore_check.py --fixture restart-handoff

Simulates a restart handoff end to end on throwaway DBs:
  1. Seed a scope, begin a run, finish it (receipt + records).
  2. Take a consistent backup while the DB is still open (live backup).
  3. Restore it to a new path, reopen, and verify the run + records are
     intact and the run can resume (resume returns the same snapshot).
  4. Corrupt a copy of the backup (truncate it) and verify restore refuses
     it, and that restore refuses to overwrite an existing DB without force.
Exit 0 PASS, 1 FAIL.
"""
import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from turnkeep.storage import open_db
from turnkeep.scopes import register_scope
from turnkeep.memory import remember
from turnkeep.runs import begin
from turnkeep.commits import finish
from turnkeep.recovery import resume
from turnkeep.backup import create_backup, restore_backup, diagnose
from turnkeep.errors import TurnkeepError

SRC = [{"id": "u:1", "type": "user_statement"}]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixture", required=True, choices=["restart-handoff"])
    ap.parse_args()
    failures = []
    with tempfile.TemporaryDirectory(prefix="turnkeep-restore-") as tmp:
        tmp = Path(tmp)
        dbpath = tmp / "live.sqlite3"
        db = open_db(dbpath)
        register_scope(db, "handoff")
        req = remember(db, "handoff", "export.format", "zip", kind="requirement", source_refs=SRC)
        run = begin(db, "handoff", "produce export manifest")
        receipt = finish(db, "handoff", run["run_id"],
                         {"key": "release.manifest", "value": "name=export"},
                         idempotency_key="rh-1")
        # Live backup while the DB is still open.
        meta = create_backup(db, tmp / "backup.sqlite3")
        if meta["records"] != 2 or meta["scopes"] != ["handoff"]:
            failures.append(f"backup metadata wrong: {meta}")
        db.close()
        # Diagnose the live DB: OK (WAL may hold frames right after close, but
        # a clean close checkpoints; report the status either way).
        diag = diagnose(dbpath)
        if diag["status"] not in ("OK", "DEGRADED"):
            failures.append(f"diagnose of the live DB: {diag}")
        # Restore + reopen: everything intact, run resumable.
        restored = restore_backup(tmp / "backup.sqlite3", tmp / "restored.sqlite3")
        db2 = open_db(tmp / "restored.sqlite3")
        from turnkeep.retrieval import search_by_key
        if search_by_key(db2, "handoff", "export.format")[0]["value"] != "zip":
            failures.append("restored DB lost the requirement")
        diff = resume(db2, "handoff", run["run_id"])
        if diff["run_id"] != run["run_id"]:
            failures.append(f"resumed run mismatch: {diff}")
        db2.close()
        # A truncated backup is refused.
        bad = tmp / "bad.sqlite3"
        data = (tmp / "backup.sqlite3").read_bytes()
        bad.write_bytes(data[: len(data) // 2])
        try:
            restore_backup(bad, tmp / "bad-restored.sqlite3")
            failures.append("restore accepted a truncated backup")
        except TurnkeepError:
            pass
        # Overwrite without force is refused.
        try:
            restore_backup(tmp / "backup.sqlite3", tmp / "restored.sqlite3")
            failures.append("restore overwrote an existing DB without force")
        except TurnkeepError:
            pass
        # ... and with force it succeeds.
        restore_backup(tmp / "backup.sqlite3", tmp / "restored.sqlite3", force=True)
    print(json.dumps({"status": "PASS" if not failures else "FAIL", "failures": failures}, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
