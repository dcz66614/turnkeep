#!/usr/bin/env python3
"""R8 final-review repro: the R7 rework, re-runnable by the reviewer.

  python scripts/repro_r8.py            # from a source checkout
  python -m turnkeep ...                # (installed wheel: same checks,
                                        #  import falls back to src/ below)

Runs against throwaway SQLite files in a TemporaryDirectory — no
network, no existing database, no home-directory paths. Every check
asserts CORRECT behaviour after the R7 rework; a FAIL line means the
defect reproduces. Prints PASS/FAIL per check plus a total; exits 1
if any check fails.

Coverage (P0-R7-01 / P1-R7-02 / P2-R7-03, plus the R6 spot checks):
  A1  import created_at / updated_at / valid_until carrying the secret
      verbatim or in Base64 / percent / reversed / hex / rot13 form
      is INVALID_INPUT (R6 already rejected valid_until; created_at
      and updated_at had no validation at all).
  A2  a legacy record already holding an illegal timestamp field
      erases COMPLETE with the field cleared — in the DB and in the
      export — instead of COMPLETE while the recoverable text stays.
  A3  value "202" / "2026" records with a real expiry still erase
      COMPLETE (the erase scan no longer matches a needle inside a
      legal timestamp held under a timestamp field name).
  A4  sweep_expired() compares timezone-aware UTC instants (GPT's two
      cases + a +00:00 control).
  A5  _check_valid_until(123) / import valid_until=123 /
      set_expiry(123) are INVALID_INPUT, never a raw TypeError.
  A6  export strips an illegal timestamp field (null, reported) and
      excludes a record whose key IS its payload.
  A7  end-to-end chain: legacy bad fields -> first erase -> repair
      erase -> residual report -> export; nothing recoverable.
  A8  R6 spot checks still hold (glued / Base64 erase reasons
      rejected; clean label erases COMPLETE).
  D   runs.task encoding channel (RETAINED, no code fix): the encoding
      stays in the live DB's runs.task, NOT in the export.
"""
import base64
import codecs
import hashlib
import json
import sys
import tempfile
from pathlib import Path

try:
    import turnkeep  # noqa: F401  (installed wheel)
except ImportError:  # source checkout: the RC root holds src/
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    import turnkeep  # noqa: F401

from turnkeep.storage import open_db, _json, _now
from turnkeep.scopes import register_scope
from turnkeep.memory import remember
from turnkeep.corrections import retract
from turnkeep.retention import set_expiry, sweep_expired, _check_valid_until
from turnkeep.errors import TurnkeepError
from turnkeep.erasure import erase_record, residual_report
from turnkeep.portability import export_scope, import_package
from turnkeep.runs import begin

SRC = [{"id": "u:1", "type": "user_statement"}]
SECRET = "SYNTHETIC-SECRET-9d4c1a7e-r8-do-not-use"
LEGAL_TS = "2026-11-12T22:00:00+00:00"
LEGAL_CREATED = "2026-11-11T00:00:00+00:00"
TMPROOT = Path(tempfile.mkdtemp(prefix="turnkeep-repro-r8-"))
CHECKS = []


def check(name, ok, extra=""):
    CHECKS.append((name, ok))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{extra}]" if extra else ""))


def _db(name, scope="s"):
    db = open_db(TMPROOT / f"{name}.sqlite3")
    register_scope(db, scope)
    return db


def _b64(s):
    return base64.b64encode(s.encode()).decode()


def _encodings():
    return [
        ("base64", _b64(SECRET)),
        ("percent", "".join(f"%{b:02x}" for b in SECRET.encode())),
        ("reversed", SECRET[::-1]),
        ("hex", SECRET.encode().hex()),
        ("rot13", codecs.encode(SECRET, "rot_13")),
    ]


def _mk_package(records, scope="s"):
    d = TMPROOT / f"pkg-{len(list(TMPROOT.glob('pkg-*')))}"
    d.mkdir()
    rec_text = "".join(_json(r) + "\n" for r in records)
    (d / "records.jsonl").write_text(rec_text, encoding="utf-8")
    (d / "events.jsonl").write_text("", encoding="utf-8")
    files = {"records.jsonl": hashlib.sha256(rec_text.encode()).hexdigest(),
             "events.jsonl": hashlib.sha256(b"").hexdigest()}
    manifest = {"format": "turnkeep-export", "schema_version": 1,
                "scope_id": scope, "exported_at": _now(), "tool": "repro-r8",
                "record_count": len(records), "event_count": 0, "files": files}
    (d / "manifest.json").write_text(_json(manifest) + "\n", encoding="utf-8")
    return d


def _rec(rid, key, value, **kw):
    r = {"id": rid, "key": key, "value": value, "kind": "fact",
         "state": "active", "revision": 1,
         "created_at": LEGAL_CREATED, "updated_at": LEGAL_CREATED,
         "valid_until": None, "cause_event_id": "x",
         "source_refs": SRC, "parent_ids": [], "supersedes_ids": []}
    r.update(kw)
    return r


def _export_text(db, name, scope="s"):
    out = TMPROOT / name
    export_scope(db, scope, out)
    return ((out / "records.jsonl").read_text(encoding="utf-8"),
            (out / "events.jsonl").read_text(encoding="utf-8"))


def _legacy(db, field, value, record_id=None, scope="s"):
    rid = record_id or db.execute(
        "SELECT id FROM memories WHERE scope_id=?", (scope,)).fetchone()[0]
    db.execute(f"UPDATE memories SET {field}=? WHERE scope_id=? AND id=?",
               (value, scope, rid))
    db.commit()
    return rid


# ---------------- A1: import timestamp-field validation ----------------
for field in ("created_at", "updated_at", "valid_until"):
    for label, enc in [("verbatim", "x" + SECRET + "y")] + _encodings():
        db = _db(f"a1-{field}-{label}")
        pkg = _mk_package([_rec("o1", "k1", {"api_key": SECRET}, **{field: enc})])
        try:
            import_package(db, pkg, target_scope="s")
            check(f"A1 import {field}={label} rejected", False, "accepted")
        except TurnkeepError as e:
            check(f"A1 import {field}={label} rejected",
                  e.code == "INVALID_INPUT", f"code={e.code}")
        db.close()

# ---------------- A2: legacy illegal timestamp erasure ----------------
for field in ("valid_until", "created_at"):
    db = _db(f"a2-{field}")
    rec = remember(db, "s", "k1", {"api_key": SECRET}, source_refs=SRC,
                   idempotency_key="k1")
    _legacy(db, field, "x" + SECRET + "y")
    out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
    row = db.execute("SELECT valid_until, created_at FROM memories "
                     "WHERE scope_id='s' AND id=?", (rec["id"],)).fetchone()
    col = 0 if field == "valid_until" else 1
    check(f"A2 legacy {field} erase COMPLETE",
          out["status"] == "COMPLETE" and out["erased"] is True,
          f"status={out['status']}")
    check(f"A2 legacy {field} cleared in DB",
          row[col] is None or (field == "created_at" and SECRET not in row[col]),
          f"value={row[col]}")
    recs, evs = _export_text(db, f"a2-{field}-exp")
    check(f"A2 legacy {field} absent from export",
          SECRET not in recs and SECRET not in evs)
    db.close()

# ---------------- A3: "202" / "2026" erase COMPLETE ----------------
db = _db("a3-202")
rec = remember(db, "s", "k1", "202", source_refs=SRC, idempotency_key="k1")
set_expiry(db, "s", rec["id"], LEGAL_TS, rec["revision"])
begin(db, "s", task="check k1", idempotency_key="run1")
rev = db.execute("SELECT revision FROM memories WHERE scope_id='s' AND id=?",
                 (rec["id"],)).fetchone()[0]
out = erase_record(db, "s", rec["id"], rev, "rotation")
check("A3 value '202' + expiry + receipt erases COMPLETE",
      out["status"] == "COMPLETE" and out["erased"] is True,
      f"status={out['status']}")
db.close()

db = _db("a3-2026")
rec = remember(db, "s", "k1", "2026", source_refs=SRC, idempotency_key="k1")
_legacy(db, "valid_until", LEGAL_TS)
out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
check("A3 value '2026' + legacy expiry erases COMPLETE",
      out["status"] == "COMPLETE" and out["erased"] is True,
      f"status={out['status']}")
db.close()

# ---------------- A4: sweep timezone-aware ----------------
db = _db("a4")
now = "2026-11-12T20:30:00+00:00"
r1 = remember(db, "s", "exp", "v1", source_refs=SRC, idempotency_key="e1")
r2 = remember(db, "s", "notexp", "v2", source_refs=SRC, idempotency_key="e2")
r3 = remember(db, "s", "expz", "v3", source_refs=SRC, idempotency_key="e3")
set_expiry(db, "s", r1["id"], "2026-11-12T22:00:00+02:00", r1["revision"])
set_expiry(db, "s", r2["id"], "2026-11-12T19:00:00-02:00", r2["revision"])
set_expiry(db, "s", r3["id"], "2026-11-12T20:00:00+00:00", r3["revision"])
res = sweep_expired(db, "s", now=now)
check("A4 +02:00 expiry (UTC 20:00) swept",
      r1["id"] in res["expired"], f"expired={res['expired']}")
check("A4 -02:00 expiry (UTC 21:00) NOT swept",
      r2["id"] not in res["expired"], f"expired={res['expired']}")
check("A4 +00:00 control (UTC 20:00) swept",
      r3["id"] in res["expired"], f"expired={res['expired']}")
db.close()

# ---------------- A5: type checks ----------------
try:
    _check_valid_until(123)
    check("A5 _check_valid_until(123) INVALID_INPUT", False, "accepted")
except TurnkeepError as e:
    check("A5 _check_valid_until(123) INVALID_INPUT", e.code == "INVALID_INPUT")
except TypeError as e:
    check("A5 _check_valid_until(123) INVALID_INPUT", False, f"raw TypeError: {e}")

db = _db("a5-import")
pkg = _mk_package([_rec("o1", "k1", "v", valid_until=123)])
try:
    import_package(db, pkg, target_scope="s")
    check("A5 import valid_until=123 INVALID_INPUT", False, "accepted")
except TurnkeepError as e:
    check("A5 import valid_until=123 INVALID_INPUT", e.code == "INVALID_INPUT")
except TypeError as e:
    check("A5 import valid_until=123 INVALID_INPUT", False, f"raw TypeError: {e}")
db.close()

db = _db("a5-expiry")
rec = remember(db, "s", "k1", "v", source_refs=SRC, idempotency_key="k1")
try:
    set_expiry(db, "s", rec["id"], 123, rec["revision"])
    check("A5 set_expiry(123) INVALID_INPUT", False, "accepted")
except TurnkeepError as e:
    check("A5 set_expiry(123) INVALID_INPUT", e.code == "INVALID_INPUT")
db.close()

# ---------------- A6: export stripping / exclusion ----------------
db = _db("a6-strip")
rec = remember(db, "s", "k1", "plain note", source_refs=SRC, idempotency_key="k1")
_legacy(db, "created_at", "x" + SECRET + "y")
out_dir = TMPROOT / "a6-exp"
rep = export_scope(db, "s", out_dir)
recs = (out_dir / "records.jsonl").read_text(encoding="utf-8")
exported = [json.loads(line) for line in recs.splitlines()]
check("A6 export strips illegal created_at",
      len(exported) == 1 and exported[0]["created_at"] is None
      and SECRET not in recs,
      f"stripped={rep.get('stripped_illegal_timestamps')}")
db.close()

db = _db("a6-key")
payload = "plain-note-7f3a9c2e"
remember(db, "s", payload, payload, source_refs=SRC, idempotency_key="k1")
out_dir = TMPROOT / "a6-key-exp"
rep = export_scope(db, "s", out_dir)
recs = (out_dir / "records.jsonl").read_text(encoding="utf-8")
check("A6 export excludes record whose key IS its payload",
      payload not in recs
      and len(rep["records_excluded_secrets"]) == 1
      and "key" not in rep["records_excluded_secrets"][0]
      and rep["records_excluded_secrets"][0].get("key_redacted") is True
      and payload not in _json(rep))
db.close()

# ---------------- A7: end-to-end chain ----------------
db = _db("a7")
enc = _b64(SECRET)
rec = remember(db, "s", "k1", {"api_key": SECRET}, source_refs=SRC,
               idempotency_key="r8-chain-key-1")
_legacy(db, "created_at", enc)
_legacy(db, "valid_until", "x" + SECRET + "y")
out1 = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
check("A7 chain first erase COMPLETE",
      out1["status"] == "COMPLETE" and out1["erased"] is True,
      f"status={out1['status']}")
out2 = erase_record(db, "s", rec["id"], out1["revision"], "repair")
check("A7 chain repair erase COMPLETE, no revision bump",
      out2["status"] == "COMPLETE" and out2.get("repair") is True
      and out2["revision"] == out1["revision"],
      f"status={out2['status']}")
rep = residual_report(db, "s")
check("A7 chain residual report clean",
      rep["controlled"]["live_tables_clean"] is True
      and rep["controlled"]["controlled_residuals"] == [])
recs, evs = _export_text(db, "a7-exp")
check("A7 chain export holds no recoverable secret",
      SECRET not in recs and SECRET not in evs
      and enc not in recs and enc not in evs)
db.close()

# ---------------- A8: R6 spot checks still hold ----------------
db = _db("a8")
rec = remember(db, "s", "k", SECRET, source_refs=SRC, idempotency_key="k1")
try:
    erase_record(db, "s", rec["id"], rec["revision"], "rotatedabcagain")
    check("A8 glued reason rejected", False, "accepted")
except TurnkeepError as e:
    check("A8 glued reason rejected", e.code == "INVALID_INPUT")
try:
    erase_record(db, "s", rec["id"], rec["revision"], "rotated " + _b64(SECRET))
    check("A8 Base64 reason rejected", False, "accepted")
except TurnkeepError as e:
    check("A8 Base64 reason rejected", e.code == "INVALID_INPUT")
out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
check("A8 clean label erases COMPLETE",
      out["status"] == "COMPLETE", f"status={out['status']}")
db.close()

# ---------------- D: runs.task encoding (RETAINED, no code fix) ----------------
db = _db("d-task")
rec = remember(db, "s", "k1", {"api_key": SECRET}, source_refs=SRC,
               idempotency_key="k1")
begin(db, "s", task="revoke " + _b64(SECRET) + " now", idempotency_key="run1")
out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
task_row = db.execute("SELECT task FROM runs WHERE scope_id=?", ("s",)).fetchone()
recs, evs = _export_text(db, "d-exp")
check("D erase COMPLETE despite Base64 in runs.task",
      out["status"] == "COMPLETE", f"status={out['status']}")
check("D encoding remains in live DB runs.task (retained issue)",
      task_row and _b64(SECRET) in task_row[0])
check("D encoding NOT in the export (runs are not exported)",
      _b64(SECRET) not in recs and _b64(SECRET) not in evs)
db.close()

print()
failed = [n for n, ok in CHECKS if not ok]
print(f"{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed "
      f"(turnkeep: {turnkeep.__file__})")
if failed:
    print("FAILED:", failed)
    sys.exit(1)
