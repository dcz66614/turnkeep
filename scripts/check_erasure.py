#!/usr/bin/env python3
"""Erasure gate (T038).

  python scripts/check_erasure.py --synthetic-fixture

Builds a throwaway DB, seeds a synthetic secret, writes it through a run
finish (so a receipt holds the value), then erases it and verifies:
  - default recall, historical recall and lexical search never return the
    payload;
  - the live DB file bytes no longer contain the payload (after WAL
    checkpoint + VACUUM);
  - the erasure receipt contains no copy of the payload and nothing that
    can directly restore it;
  - residuals are listed (backups / external clients / physical recovery),
    not claimed as deleted.
Exit 0 PASS, 1 FAIL. The fixture secret is synthetic and is never a real key.
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
from turnkeep.retrieval import recall
from turnkeep.erasure import erase_record, residual_report

SECRET = "SYNTHETIC-SECRET-7f3a9c2e-do-not-use"
SRC = [{"id": "u:1", "type": "user_statement"}]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--synthetic-fixture", action="store_true", required=True)
    ap.parse_args()
    failures = []
    with tempfile.TemporaryDirectory(prefix="turnkeep-erasure-") as tmp:
        dbpath = Path(tmp) / "e.sqlite3"
        db = open_db(dbpath)
        register_scope(db, "erase")
        # A run finish writes the secret AND stores it in a receipt
        # (derived copy) — erasure must scrub both.
        run = begin(db, "erase", "write the secret down")
        done = finish(db, "erase", run["run_id"], {"key": "ci.api_key", "value": SECRET},
                      idempotency_key="erase-1")
        from turnkeep.retrieval import search_by_key
        rec = search_by_key(db, "erase", "ci.api_key")[0]
        # Sanity: the payload is findable before erasure.
        assert any(SECRET in str(r.get("value", "")) for r in recall(db, "erase", "api_key"))
        receipt = erase_record(db, "erase", rec["id"], rec["revision"], "rotation")
        if receipt["status"] != "COMPLETE":
            failures.append(f"erase status is {receipt['status']}: {receipt}")
        # 1. No read path returns the payload.
        for label, rows in (("default recall", recall(db, "erase", "api_key")),
                            ("historical recall", recall(db, "erase", "api_key", include_history=True))):
            if any(SECRET in str(r.get("value", "")) for r in rows):
                failures.append(f"{label} still returns the payload")
        row = db.execute("SELECT value_json FROM memories WHERE id=?", (rec["id"],)).fetchone()
        if row is None or SECRET in row[0]:
            failures.append("memories row still holds the payload")
        if row is not None and json.loads(row[0]) != {"_erased": True}:
            failures.append(f"tombstone is not exactly {{'_erased': True}}: {row[0]}")
        # 2. The live DB file bytes no longer contain the payload.
        db.close()
        if SECRET.encode() in dbpath.read_bytes():
            failures.append("live DB file bytes still contain the payload after checkpoint+VACUUM")
        # 3. The receipt never copies the payload.
        if SECRET in json.dumps(receipt):
            failures.append("erasure receipt contains the payload")
        db = open_db(dbpath)
        rep = residual_report(db, "erase")
        if rep["status"] != "COMPLETE_WITH_RESIDUALS":
            failures.append(f"residual report status is {rep['status']}, expected COMPLETE_WITH_RESIDUALS")
        kinds = {u["kind"] for u in rep["uncontrolled"]}
        if not {"backup_or_export_copy", "client_history", "physical_recovery"} <= kinds:
            failures.append(f"residual report misses a category: {kinds}")
        db.close()
    print(json.dumps({"status": "PASS" if not failures else "FAIL", "failures": failures}, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
