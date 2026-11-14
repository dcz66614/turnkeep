#!/usr/bin/env python3
"""R10 rework repro (ships in the RC as evidence for final-review R11).

Run from a source checkout, an extracted RC, or an installed wheel:
    python scripts/repro_r11.py
All checks run against throwaway SQLite files and assert the FIXED
behaviour for the R10 final-review findings (verified in
~/workspace/turnkeep-gpt-R10-verification-2026-11-12.md):

  A1  P0-R10-01: an idempotency request_digest (plain SHA-256 of the
      request incl. the raw value) no longer survives erasure — it is
      neutralized to the non-hex marker "erased" (NEUTRALIZED_DIGEST),
      so offline enumeration finds nothing; HMAC is NOT used (the HMAC
      key lives in the same DB — repro A1b.2). All six operations using
      request_digest are covered (remember / finish / begin / correct /
      retract / resolve_conflict); a replay after erasure raises
      IDEMPOTENCY_MISMATCH. sources.content_digest is neutralized the
      same way (set NULL).
  A2  P0-R10-02: the residual scan covers four identifier columns on
      whole-value equality only — events.affected_ids_json,
      events.actor_binding, idempotency.idempotency_key, sources.id.
      Append-only events refuse PARTIAL and name the event; a live
      record citing a source whose id IS the payload stays (R8 design
      decision) and is reported PARTIAL; cross-record residuals do NOT
      resolve by erasing the other record (the key/link is the OTHER
      payload) — the remedy is export to a fresh DB.
  A3  P1-R10-03: erase_record()'s return dict no longer re-materialises
      the payload key when key == value — the key is omitted and
      flagged "key_redacted" on all three return paths (step-1 PARTIAL
      refusal, terminal return, repair return); benign keys stay
      visible; the CLI's stdout is clean.
  D   Retained (no code action): a reversible encoding in runs.task
      free text is NOT scanned for (docs/KNOWN_LIMITATIONS.md §8) —
      it stays in the live DB, the residual still says clean, and it
      is NOT in the export.
  E   "202" / "2026" still erase COMPLETE (whole-value decoded
      equality did not bring back the P2-R8-03 false-positive class).
  F   R9/R8 spot checks: the previous rounds' fixes still hold in
      their original scope (encoded key PARTIAL, export redaction,
      key == payload PARTIAL).
"""
import atexit
import base64
import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import turnkeep  # noqa: F401  (proves the import resolves: source, RC or wheel)
from turnkeep.storage import open_db, _json, _now, _digest
from turnkeep.scopes import register_scope
from turnkeep.memory import remember, list_conflicts
from turnkeep.corrections import correct, retract, resolve_conflict
from turnkeep.runs import begin
from turnkeep.commits import finish
from turnkeep.retention import set_expiry
from turnkeep.erasure import erase_record, residual_report, TOMBSTONE
from turnkeep.idempotency import request_digest, NEUTRALIZED_DIGEST
from turnkeep.portability import export_scope
from turnkeep.errors import TurnkeepError

SRC = [{"id": "u:1", "type": "user_statement"}]
PIN = "4827"
HIGH = "9f3a7c2e5b1d8f4a6c0e2b7d9f1a3c5e7"  # high-entropy payload
LEGAL_TS = "2026-11-12T22:00:00+00:00"
TMPROOT = Path(tempfile.mkdtemp(prefix="turnkeep-repro-r11-"))
CHECKS = []


def _cleanup():
    # Temp-dir hygiene (P0-R13-01 item 5): earlier runs left one ~13M
    # TMPROOT per run without cleanup, which filled /tmp during
    # verification runs. Remove it on success; on failure keep it and
    # print the path, so the failing DBs can be inspected.
    fails = [n for n, ok in CHECKS if not ok]
    if not fails:
        shutil.rmtree(TMPROOT, ignore_errors=True)
    else:
        print(f"\n(keeping TMPROOT for inspection: {TMPROOT})")


atexit.register(_cleanup)


def check(name, ok, extra=""):
    CHECKS.append((name, ok))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{extra}]" if extra else ""))


def _db(name, scope="s"):
    db = open_db(TMPROOT / f"{name}.sqlite3")
    register_scope(db, scope)
    return db


def _req_dict(key, value, sources=None):
    return {"key": key, "value": value, "kind": "fact",
            "sources": sources if sources is not None else SRC,
            "parent_ids": [], "expected_revision": None, "replaces_ids": []}


def _export_rep(db, name):
    out = TMPROOT / f"exp-{name}"
    rep = export_scope(db, "s", out)
    return (rep, (out / "records.jsonl").read_text(encoding="utf-8"),
            (out / "events.jsonl").read_text(encoding="utf-8"))


def _rev(db, rid):
    return db.execute("SELECT revision FROM memories WHERE scope_id='s' AND id=?",
                      (rid,)).fetchone()[0]


def _latest_erase_meta(db):
    meta = db.execute(
        "SELECT metadata_json FROM events WHERE scope_id='s' AND event_type='erase'"
        " ORDER BY scope_epoch DESC LIMIT 1").fetchone()[0]
    return json.loads(meta)


def _idem_row(db, key):
    return db.execute(
        "SELECT idempotency_key, request_digest, response_json FROM idempotency "
        "WHERE scope_id='s' AND idempotency_key=?", (key,)).fetchone()


def _enumerate_pin(digest):
    return [f"{i:04d}" for i in range(10000)
            if request_digest("remember", "s", "local",
                              _req_dict("pin", f"{i:04d}")) == digest]


print(f"turnkeep: {turnkeep.__file__}")

# ---------------------------------------------------------------- A1: digest
print("\n== A1: request_digest neutralization (P0-R10-01) ==")
db = _db("a1-chain")
rec = remember(db, "s", "pin", PIN, source_refs=SRC, idempotency_key="idem-4827")
before = _idem_row(db, "idem-4827")[1]
check("A1.1 stored digest before erase == plain sha256 of the request",
      before == request_digest("remember", "s", "local", _req_dict("pin", PIN)))
out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
rep = residual_report(db, "s")
check("A1.2 erase COMPLETE, residual clean",
      out["status"] == "COMPLETE" and rep["controlled"]["live_tables_clean"] is True,
      f"status={out['status']}")
check("A1.3 neutralization counts: 1 digest, 0 rows deleted, 0 source digests",
      out.get("neutralized_idempotency") == 1
      and out.get("deleted_idempotency") == 0
      and out.get("neutralized_source_digests") == 0, f"out={out}")
row = _idem_row(db, "idem-4827")
check("A1.4 stored digest after erase == marker 'erased', not hex",
      row is not None and row[1] == NEUTRALIZED_DIGEST == "erased"
      and row[1] != before, f"digest={row[1] if row else None}")
check("A1.5 offline enumeration 0000-9999 finds nothing",
      _enumerate_pin(row[1]) == [])
_, recs, evs = _export_rep(db, "a1-chain")
check("A1.6 export clean of the PIN (records + events)",
      PIN not in recs and PIN not in evs)
db.close()

db = _db("a1-replay")
rec = remember(db, "s", "pin", PIN, source_refs=SRC, idempotency_key="mem-live")
replay = remember(db, "s", "pin", PIN, source_refs=SRC, idempotency_key="mem-live")
check("A1-replay.1 replay while live returns the original record",
      replay["id"] == rec["id"])
erase_record(db, "s", rec["id"], rec["revision"], "rotation")
try:
    remember(db, "s", "pin", PIN, source_refs=SRC, idempotency_key="mem-live")
    check("A1-replay.2 replay after erasure raises IDEMPOTENCY_MISMATCH", False, "replayed")
except TurnkeepError as e:
    check("A1-replay.2 replay after erasure raises IDEMPOTENCY_MISMATCH",
          e.code == "IDEMPOTENCY_MISMATCH", e.code)
db.close()

db = _db("a1-correct")
old = remember(db, "s", "kA", "old note", source_refs=SRC)
new = correct(db, "s", old["id"], old["revision"], PIN, source_refs=SRC,
              idempotency_key="corr-1")
out = erase_record(db, "s", new["id"], new["revision"], "rotation")
check("A1-correct.1 correct-row digest neutralized (erase COMPLETE)",
      out["status"] == "COMPLETE" and out.get("neutralized_idempotency") == 1
      and _idem_row(db, "corr-1")[1] == NEUTRALIZED_DIGEST)
try:
    correct(db, "s", old["id"], old["revision"], PIN, source_refs=SRC,
            idempotency_key="corr-1")
    check("A1-correct.2 replay after erasure raises IDEMPOTENCY_MISMATCH", False, "replayed")
except TurnkeepError as e:
    check("A1-correct.2 replay after erasure raises IDEMPOTENCY_MISMATCH",
          e.code == "IDEMPOTENCY_MISMATCH", e.code)
db.close()

db = _db("a1-retract")
rec = remember(db, "s", "kA", PIN, source_refs=SRC)
rev = rec["revision"]
retract(db, "s", rec["id"], rev, "withdrawn", idempotency_key="ret-1")
out = erase_record(db, "s", rec["id"], _rev(db, rec["id"]), "rotation")
check("A1-retract.1 retract-row digest neutralized (request has no value, row still owned)",
      out["status"] == "COMPLETE" and out.get("neutralized_idempotency") == 1
      and _idem_row(db, "ret-1")[1] == NEUTRALIZED_DIGEST)
try:
    retract(db, "s", rec["id"], rev, "withdrawn", idempotency_key="ret-1")
    check("A1-retract.2 replay after erasure raises IDEMPOTENCY_MISMATCH", False, "replayed")
except TurnkeepError as e:
    check("A1-retract.2 replay after erasure raises IDEMPOTENCY_MISMATCH",
          e.code == "IDEMPOTENCY_MISMATCH", e.code)
db.close()

db = _db("a1-resolve")
remember(db, "s", "ck", PIN, source_refs=SRC)
remember(db, "s", "ck", "other note", source_refs=SRC)
group = list_conflicts(db, "s", "ck")
cands = [{"id": r["id"], "revision": r["revision"]} for r in group]
res = resolve_conflict(db, "s", "ck", cands, PIN, source_refs=SRC,
                       idempotency_key="res-1")
out = erase_record(db, "s", res["id"], res["revision"], "rotation")
check("A1-resolve.1 resolve-row digest neutralized (erase PARTIAL: another live copy)",
      out["status"] == "PARTIAL" and out.get("neutralized_idempotency") == 1
      and _idem_row(db, "res-1")[1] == NEUTRALIZED_DIGEST, f"status={out['status']}")
try:
    resolve_conflict(db, "s", "ck", cands, PIN, source_refs=SRC, idempotency_key="res-1")
    check("A1-resolve.2 replay after erasure raises IDEMPOTENCY_MISMATCH", False, "replayed")
except TurnkeepError as e:
    check("A1-resolve.2 replay after erasure raises IDEMPOTENCY_MISMATCH",
          e.code == "IDEMPOTENCY_MISMATCH", e.code)
db.close()

db = _db("a1-begin")
rec = remember(db, "s", "kA", PIN, source_refs=SRC)
run = begin(db, "s", task="check kA", idempotency_key="run-1")
check("A1-begin.0 begin receipt holds the record's dump",
      any(r["id"] == rec["id"] for r in run["records"]))
out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
check("A1-begin.1 begin-row digest neutralized (request has no value, receipt owns it)",
      out["status"] == "COMPLETE" and out.get("neutralized_idempotency") == 1
      and _idem_row(db, "run-1")[1] == NEUTRALIZED_DIGEST)
try:
    begin(db, "s", task="check kA", idempotency_key="run-1")
    check("A1-begin.2 replay after erasure raises IDEMPOTENCY_MISMATCH", False, "replayed")
except TurnkeepError as e:
    check("A1-begin.2 replay after erasure raises IDEMPOTENCY_MISMATCH",
          e.code == "IDEMPOTENCY_MISMATCH", e.code)
db.close()

db = _db("a1-finish")
remember(db, "s", "req", "3.11", kind="requirement", source_refs=SRC)
run = begin(db, "s", "work")
receipt = finish(db, "s", run["run_id"], {"key": "out", "value": PIN},
                 idempotency_key="fin-1")
out = erase_record(db, "s", receipt["record_id"], 1, "rotation")
check("A1-finish.1 finish-row digest neutralized (erase COMPLETE)",
      out["status"] == "COMPLETE" and out.get("neutralized_idempotency") == 2
      and _idem_row(db, "fin-1")[1] == NEUTRALIZED_DIGEST,
      f"neutralized={out.get('neutralized_idempotency')}")
row = db.execute(
    "SELECT request_digest, receipt_json FROM runs WHERE scope_id='s' AND id=?",
    (run["run_id"],)).fetchone()
check("A1-finish.2 runs.request_digest neutralized, receipt field removed",
      row[0] == NEUTRALIZED_DIGEST and "request_digest" not in json.loads(row[1]))
try:
    finish(db, "s", run["run_id"], {"key": "out", "value": PIN}, idempotency_key="fin-1")
    check("A1-finish.3 replay after erasure raises IDEMPOTENCY_MISMATCH", False, "replayed")
except TurnkeepError as e:
    check("A1-finish.3 replay after erasure raises IDEMPOTENCY_MISMATCH",
          e.code == "IDEMPOTENCY_MISMATCH", e.code)
db.close()

db = _db("a1-live")
ra = remember(db, "s", "pin", PIN, source_refs=SRC, idempotency_key="mem-a")
rb = remember(db, "s", "kB", "other note", source_refs=SRC, idempotency_key="mem-b")
erase_record(db, "s", ra["id"], ra["revision"], "rotation")
check("A1-live.1 live record's digest unchanged",
      _idem_row(db, "mem-b")[1] == request_digest(
          "remember", "s", "local", _req_dict("kB", "other note")))
replay = remember(db, "s", "kB", "other note", source_refs=SRC, idempotency_key="mem-b")
check("A1-live.2 live record's replay still works", replay["id"] == rb["id"])
db.close()

db = _db("a1-high")
rec = remember(db, "s", "kH", HIGH, source_refs=SRC, idempotency_key="hi-1")
out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
rep = residual_report(db, "s")
check("A1-high: high-entropy payload digest neutralized, COMPLETE, clean",
      out["status"] == "COMPLETE"
      and _idem_row(db, "hi-1")[1] == NEUTRALIZED_DIGEST
      and rep["controlled"]["live_tables_clean"] is True)
db.close()

db = _db("a1b-live-link")
rb = remember(db, "s", "b", "other note",
              source_refs=[{"type": "user_statement", "id": PIN}])
ra = remember(db, "s", "pin", PIN, source_refs=SRC)
out = erase_record(db, "s", ra["id"], ra["revision"], "rotation")
check("A1b.1 live-link source digest neutralized (erase PARTIAL via 2d)",
      out["status"] == "PARTIAL" and out.get("neutralized_source_digests") == 1,
      f"status={out['status']}")
row = db.execute(
    "SELECT id, content_digest FROM sources WHERE scope_id='s' AND id=?", (PIN,)).fetchone()
check("A1b.2 sources row survives, content_digest NULL",
      row is not None and row[1] is None)
link = db.execute(
    "SELECT COUNT(*) FROM memory_sources WHERE scope_id='s' AND memory_id=? AND source_id=?",
    (rb["id"], PIN)).fetchone()[0]
check("A1b.3 live link still cites the source id", link == 1)
rc = remember(db, "s", "c", "third note",
              source_refs=[{"type": "user_statement", "id": PIN}])
row = db.execute(
    "SELECT content_digest FROM sources WHERE scope_id='s' AND id=?", (PIN,)).fetchone()
check("A1b.4 later remember citing the same source id works, digest stays NULL",
      rc["id"] != rb["id"] and row[0] is None)
db.close()

db = _db("a1b-orphan")
rec = remember(db, "s", "pin", PIN,
               source_refs=[{"type": "user_statement", "id": PIN}])
out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
row = db.execute(
    "SELECT COUNT(*) FROM sources WHERE scope_id='s' AND id=?", (PIN,)).fetchone()[0]
check("A1b-orphan: orphaned sources row deleted with its digest (COMPLETE)",
      out["status"] == "COMPLETE" and out.get("neutralized_source_digests") == 0
      and row == 0)
db.close()

# A1 extras: the HMAC negative, pre-erase digest shape, marker shape,
# and the neutralization counts in the erase event metadata.
print("\n== A1 extras: HMAC negative, digest shape, event metadata ==")
db = _db("a1-hmac")
rec = remember(db, "s", "pin", PIN, source_refs=SRC, idempotency_key="idem-4827")
remember(db, "s", "pin2", PIN, source_refs=SRC, idempotency_key="idem-4827b")
erase_record(db, "s", rec["id"], rec["revision"], "rotation")
# P0-R12-01: the erase is PARTIAL (a second record still holds the
# PIN), so the record's verification key is alive in the live-DB-only
# record_erase_keys table. A terminal COMPLETE erase deletes it —
# that is the R12 fix — but while PARTIAL the key is in the same DB
# file as the hashes it verifies.
scope_key = bytes.fromhex(db.execute(
    "SELECT secret_hex FROM record_erase_keys "
    "WHERE scope_id='s' AND record_id=?", (rec["id"],)).fetchone()[0])
check("A1-hmac.1 per-record verification key lives in the same DB file (32 bytes)",
      scope_key is not None and len(scope_key) == 32)


def _hmac_digest(value):
    payload = {"v": 2, "op": "remember", "scope": "s", "principal": "local",
               "request": _req_dict("pin", value)}
    return hmac.new(scope_key, _json(payload).encode(), hashlib.sha256).hexdigest()


target_hmac = _hmac_digest(PIN)
found_hmac = [f"{i:04d}" for i in range(10000) if _hmac_digest(f"{i:04d}") == target_hmac]
check("A1-hmac.2 HMAC-with-DB-key digest is enumerable the same way (HMAC does NOT fix it)",
      found_hmac == [PIN], f"found={found_hmac}")
meta = _latest_erase_meta(db)
check("A1-meta: erase event metadata carries the neutralization counts",
      meta.get("neutralized_idempotency") == 1
      and meta.get("deleted_idempotency") == 0
      and meta.get("neutralized_source_digests") == 0, f"meta keys={sorted(meta)}")
db.close()

check("A1-marker: NEUTRALIZED_DIGEST is a non-hex marker, never a computed digest",
      NEUTRALIZED_DIGEST == "erased"
      and re.fullmatch(r"[0-9a-f]{64}", NEUTRALIZED_DIGEST) is None)

db = _db("a1-pre")
r1 = remember(db, "s", "k1", "pre note", source_refs=SRC, idempotency_key="pre-mem")
r2 = correct(db, "s", r1["id"], r1["revision"], "pre note 2", source_refs=SRC,
             idempotency_key="pre-corr")
retract(db, "s", r2["id"], r2["revision"], "withdrawn", idempotency_key="pre-ret")
remember(db, "s", "ck", "a", source_refs=SRC)
remember(db, "s", "ck", "b", source_refs=SRC)
group = list_conflicts(db, "s", "ck")
cands = [{"id": r["id"], "revision": r["revision"]} for r in group]
resolve_conflict(db, "s", "ck", cands, "a", source_refs=SRC, idempotency_key="pre-res")
run = begin(db, "s", task="pre task", idempotency_key="pre-run")
finish(db, "s", run["run_id"], {"key": "pre-out", "value": "pre done"},
       idempotency_key="pre-fin")
digests = [row[0] for row in db.execute(
    "SELECT request_digest FROM idempotency WHERE scope_id='s'").fetchall()]
check("A1-pre.1 all six stored digests are 64-char hex before erasure",
      len(digests) == 6
      and all(re.fullmatch(r"[0-9a-f]{64}", d) for d in digests), f"n={len(digests)}")
check("A1-pre.2 none is the marker before erasure",
      all(d != NEUTRALIZED_DIGEST for d in digests))
db.close()

# ---------------------------------------------------------------- A2: surfaces
print("\n== A2: four residual-scan surfaces (P0-R10-02) ==")
db = _db("a2-1")
rb = remember(db, "s", "other", "some note", source_refs=SRC, record_id=PIN)
ra = remember(db, "s", "pin", PIN, source_refs=SRC)
out = erase_record(db, "s", ra["id"], ra["revision"], "rotation")
meta = _latest_erase_meta(db)
remember_ev = db.execute(
    "SELECT id FROM events WHERE scope_id='s' AND event_type='remember'"
    " AND affected_ids_json LIKE '%4827%'").fetchone()[0]
check("A2-1.1 erase PARTIAL, append-only refusal",
      out["status"] == "PARTIAL" and "append-only" in out["reason"], f"status={out['status']}")
check("A2-1.2 refusal names the event holding the payload",
      meta["remaining"] == [{"table": "events", "record_id": ra["id"],
                            "event_id": remember_ev, "key": "pin"}])
rep = residual_report(db, "s")
check("A2-1.3 residual NOT clean, names the erased record",
      rep["controlled"]["live_tables_clean"] is False
      and any(r["record_id"] == ra["id"]
              for r in rep["controlled"]["controlled_residuals"]))
_, recs, evs = _export_rep(db, "a2-1")
check("A2-1.4 events.jsonl still holds the affected id (history); records.jsonl clean",
      f'"{PIN}"' in evs and PIN not in recs)
out2 = erase_record(db, "s", ra["id"], ra["revision"], "rotation")
check("A2-1.5 repeat erase still PARTIAL, same event named",
      out2["status"] == "PARTIAL"
      and _latest_erase_meta(db)["remaining"][0]["event_id"] == remember_ev)
db.close()

db = _db("a2-2")
rec = remember(db, "s", "pin", PIN, source_refs=SRC, actor_binding=PIN)
out = erase_record(db, "s", rec["id"], rec["revision"], "rotation", actor_binding="local")
meta = _latest_erase_meta(db)
remember_ev = db.execute(
    "SELECT id FROM events WHERE scope_id='s' AND event_type='remember'"
    " AND actor_binding=?", (PIN,)).fetchone()[0]
check("A2-2.1 erase PARTIAL, refusal names the event",
      out["status"] == "PARTIAL"
      and meta["remaining"] == [{"table": "events", "record_id": rec["id"],
                                "event_id": remember_ev, "key": "pin"}])
rep = residual_report(db, "s")
check("A2-2.2 residual NOT clean",
      rep["controlled"]["live_tables_clean"] is False)
_, recs, evs = _export_rep(db, "a2-2")
check("A2-2.3 events.jsonl does NOT carry actor_binding (not exported)",
      PIN not in evs)
db.close()

db = _db("a2-2-sub")
rec = remember(db, "s", "k1", "note1", source_refs=SRC, actor_binding="x4827y")
out = erase_record(db, "s", rec["id"], rec["revision"], "rotation", actor_binding="local")
rep = residual_report(db, "s")
check("A2-2-sub: binding merely containing the needle is NOT a copy (COMPLETE, clean)",
      out["status"] == "COMPLETE" and rep["controlled"]["live_tables_clean"] is True)
db.close()

db = _db("a2-3")
rb = remember(db, "s", "b", "other note", source_refs=SRC, idempotency_key=PIN)
ra = remember(db, "s", "pin", PIN, source_refs=SRC, idempotency_key="a-key")
out = erase_record(db, "s", ra["id"], ra["revision"], "rotation")
meta = _latest_erase_meta(db)
check("A2-3.1 erase PARTIAL, names B (other record's idempotency key)",
      out["status"] == "PARTIAL"
      and meta["remaining"] == [{"table": "idempotency",
                                 "rowid": meta["remaining"][0]["rowid"],
                                 "record_id": rb["id"], "key": "b"}])
rep = residual_report(db, "s")
check("A2-3.2 residual NOT clean, names B + idempotency table",
      rep["controlled"]["live_tables_clean"] is False
      and any(r["record_id"] == rb["id"] and r["table"] == "idempotency"
              for r in rep["controlled"]["controlled_residuals"]))
check("A2-3.3 the key row still exists (other record's row is kept)",
      _idem_row(db, PIN) is not None and _idem_row(db, PIN)[0] == PIN)
outb = erase_record(db, "s", rb["id"], rb["revision"], "rotation")
check("A2-3.4 erasing B is COMPLETE, but the key row survives",
      outb["status"] == "COMPLETE" and _idem_row(db, PIN) is not None)
rep = residual_report(db, "s")
check("A2-3.5 residual persists after B's erasure (key is the OTHER payload)",
      rep["controlled"]["live_tables_clean"] is False
      and any(r["table"] == "idempotency"
              for r in rep["controlled"]["controlled_residuals"]))
db.close()

db = _db("a2-3-own")
rec = remember(db, "s", "pin", PIN, source_refs=SRC, idempotency_key=PIN)
out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
rep = residual_report(db, "s")
check("A2-3-own.1 own row deleted outright (erase COMPLETE, clean)",
      out["status"] == "COMPLETE" and out.get("deleted_idempotency") == 1
      and _idem_row(db, PIN) is None
      and rep["controlled"]["live_tables_clean"] is True)
try:
    remember(db, "s", "pin", PIN, source_refs=SRC, idempotency_key=PIN)
    check("A2-3-own.2 replay is not recognised (row gone) — key-history guard refuses",
          False, "replayed")
except TurnkeepError as e:
    check("A2-3-own.2 replay is not recognised (row gone) — key-history guard refuses",
          e.code == "REPLACEMENT_REQUIRED", e.code)
db.close()

db = _db("a2-4")
rb = remember(db, "s", "b", "other note",
              source_refs=[{"type": "user_statement", "id": PIN}])
ra = remember(db, "s", "pin", PIN, source_refs=SRC)
out = erase_record(db, "s", ra["id"], ra["revision"], "rotation")
meta = _latest_erase_meta(db)
check("A2-4.1 erase PARTIAL, names B (live record citing the source id)",
      out["status"] == "PARTIAL"
      and meta["remaining"] == [{"table": "sources",
                                 "rowid": meta["remaining"][0]["rowid"],
                                 "record_id": rb["id"], "key": "b"}])
rep = residual_report(db, "s")
check("A2-4.2 residual NOT clean, names B + sources table",
      rep["controlled"]["live_tables_clean"] is False
      and any(r["record_id"] == rb["id"] and r["table"] == "sources"
              for r in rep["controlled"]["controlled_residuals"]))
rep_exp, recs, _ = _export_rep(db, "a2-4")
check("A2-4.3 export excludes B (retention behaviour unchanged)",
      "other note" not in recs and PIN not in recs)
check("A2-4.4 export report names B (benign key visible)",
      any(e.get("key") == "b" for e in rep_exp["records_excluded_secrets"]))
link = db.execute(
    "SELECT COUNT(*) FROM memory_sources WHERE scope_id='s' AND memory_id=? AND source_id=?",
    (rb["id"], PIN)).fetchone()[0]
check("A2-4.5 the link is retained (R8 design decision)", link == 1)
outb = erase_record(db, "s", rb["id"], rb["revision"], "rotation")
link = db.execute(
    "SELECT COUNT(*) FROM memory_sources WHERE scope_id='s' AND memory_id=? AND source_id=?",
    (rb["id"], PIN)).fetchone()[0]
check("A2-4.6 erasing B is COMPLETE, the link survives",
      outb["status"] == "COMPLETE" and link == 1)
rep = residual_report(db, "s")
check("A2-4.7 residual persists after B's erasure (link id is the OTHER payload)",
      rep["controlled"]["live_tables_clean"] is False
      and any(r["table"] == "sources"
              for r in rep["controlled"]["controlled_residuals"]))
db.close()

db = _db("a2-orphan")
rec = remember(db, "s", "pin", PIN, source_refs=SRC)
digest = _digest({"id": PIN, "type": "user_statement"})
db.execute(
    "INSERT INTO sources(id, scope_id, source_type, source_ref, content_digest, observed_at)"
    " VALUES (?,?,?,?,?,?)",
    (PIN, "s", "user_statement", PIN, digest, _now()))
db.commit()
out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
meta = _latest_erase_meta(db)
check("A2-orphan.1 orphan sources row: PARTIAL, flagged source_orphan",
      out["status"] == "PARTIAL"
      and meta["remaining"] == [{"table": "sources",
                                 "rowid": meta["remaining"][0]["rowid"],
                                 "source_orphan": True}])
row = db.execute(
    "SELECT content_digest FROM sources WHERE scope_id='s' AND id=?", (PIN,)).fetchone()
check("A2-orphan.2 orphan row's digest neutralized, row survives",
      row is not None and row[0] is None)
rep = residual_report(db, "s")
check("A2-orphan.3 residual NOT clean", rep["controlled"]["live_tables_clean"] is False)
out2 = erase_record(db, "s", rec["id"], _rev(db, rec["id"]), "repair")
rep = residual_report(db, "s")
check("A2-orphan.4 repair scan is blind (needles gone) yet the report stays NOT clean",
      out2["status"] == "COMPLETE" and rep["controlled"]["live_tables_clean"] is False)
db.close()


# A2 extras: the four identifier columns under ENCODING (decode-aware
# whole-value equality, R9 rule applied to the new columns), substring
# controls for the two key/id columns, and two resolution details.
print("\n== A2 extras: encoded identifiers, substring controls ==")
ENC_PIN = base64.b64encode(PIN.encode()).decode()  # "NDgyNw=="
db = _db("a2-1-enc")
rb = remember(db, "s", "other", "some note", source_refs=SRC, record_id=ENC_PIN)
ra = remember(db, "s", "pin", PIN, source_refs=SRC)
out = erase_record(db, "s", ra["id"], ra["revision"], "rotation")
meta = _latest_erase_meta(db)
check("A2-1-enc.1 affected id that DECODES to the payload refuses PARTIAL",
      out["status"] == "PARTIAL", f"status={out['status']}")
check("A2-1-enc.2 refusal names the event (decode-aware identifier rule)",
      meta["remaining"][0].get("table") == "events"
      and "event_id" in meta["remaining"][0])
rep = residual_report(db, "s")
check("A2-1-enc.3 residual NOT clean",
      rep["controlled"]["live_tables_clean"] is False)
db.close()

db = _db("a2-2-enc")
rec = remember(db, "s", "pin", PIN, source_refs=SRC, actor_binding=ENC_PIN)
out = erase_record(db, "s", rec["id"], rec["revision"], "rotation", actor_binding="local")
meta = _latest_erase_meta(db)
check("A2-2-enc.1 actor binding that DECODES to the payload refuses PARTIAL",
      out["status"] == "PARTIAL", f"status={out['status']}")
check("A2-2-enc.2 refusal names the event",
      meta["remaining"][0].get("table") == "events"
      and "event_id" in meta["remaining"][0])
rep = residual_report(db, "s")
check("A2-2-enc.3 residual NOT clean",
      rep["controlled"]["live_tables_clean"] is False)
db.close()

db = _db("a2-3-enc")
rb = remember(db, "s", "b", "other note", source_refs=SRC, idempotency_key=ENC_PIN)
ra = remember(db, "s", "pin", PIN, source_refs=SRC, idempotency_key="a-key")
out = erase_record(db, "s", ra["id"], ra["revision"], "rotation")
meta = _latest_erase_meta(db)
check("A2-3-enc.1 idempotency key that DECODES to the payload refuses PARTIAL",
      out["status"] == "PARTIAL", f"status={out['status']}")
check("A2-3-enc.2 residual names B",
      any(r["record_id"] == rb["id"] and r["table"] == "idempotency"
          for r in residual_report(db, "s")["controlled"]["controlled_residuals"]))
check("A2-3-enc.3 the encoded key row is kept (other record's row)",
      _idem_row(db, ENC_PIN) is not None)
db.close()

db = _db("a2-4-enc")
rb = remember(db, "s", "b", "other note",
              source_refs=[{"type": "user_statement", "id": ENC_PIN}])
ra = remember(db, "s", "pin", PIN, source_refs=SRC)
out = erase_record(db, "s", ra["id"], ra["revision"], "rotation")
check("A2-4-enc.1 source id that DECODES to the payload refuses PARTIAL",
      out["status"] == "PARTIAL", f"status={out['status']}")
rep = residual_report(db, "s")
check("A2-4-enc.2 residual names B + sources table",
      rep["controlled"]["live_tables_clean"] is False
      and any(r["record_id"] == rb["id"] and r["table"] == "sources"
              for r in rep["controlled"]["controlled_residuals"]))
_, recs, _ = _export_rep(db, "a2-4-enc")
check("A2-4-enc.3 export still excludes B (decoded source id)",
      "other note" not in recs and PIN not in recs and ENC_PIN not in recs)
db.close()

db = _db("a2-sub-key")
rb = remember(db, "s", "b", "other note", source_refs=SRC, idempotency_key="x4827y")
ra = remember(db, "s", "pin", PIN, source_refs=SRC, idempotency_key="a-key")
out = erase_record(db, "s", ra["id"], ra["revision"], "rotation")
rep = residual_report(db, "s")
check("A2-sub-key: idempotency key merely containing the needle is NOT a copy",
      out["status"] == "COMPLETE" and rep["controlled"]["live_tables_clean"] is True)
db.close()

db = _db("a2-sub-src")
rb = remember(db, "s", "b", "other note",
              source_refs=[{"type": "user_statement", "id": "x4827y"}])
ra = remember(db, "s", "pin", PIN, source_refs=SRC)
out = erase_record(db, "s", ra["id"], ra["revision"], "rotation")
rep = residual_report(db, "s")
check("A2-sub-src: source id merely containing the needle is NOT a copy",
      out["status"] == "COMPLETE" and rep["controlled"]["live_tables_clean"] is True)
db.close()

db = _db("a2-1-report")
rb = remember(db, "s", "other", "some note", source_refs=SRC, record_id=PIN)
ra = remember(db, "s", "pin", PIN, source_refs=SRC)
erase_record(db, "s", ra["id"], ra["revision"], "rotation")
rep_exp, _, _ = _export_rep(db, "a2-1-report")
check("A2-1.6 export report names B (benign key 'other' visible)",
      any(e.get("key") == "other" for e in rep_exp["records_excluded_secrets"]))
db.close()

db = _db("a2-3-digest")
rb = remember(db, "s", "b", "other note", source_refs=SRC, idempotency_key=PIN)
ra = remember(db, "s", "pin", PIN, source_refs=SRC, idempotency_key="a-key")
erase_record(db, "s", ra["id"], ra["revision"], "rotation")
erase_record(db, "s", rb["id"], rb["revision"], "rotation")
row = _idem_row(db, PIN)
check("A2-3.6 after B's erasure its row digest is the marker, but its KEY still PIN",
      row is not None and row[1] == NEUTRALIZED_DIGEST and row[0] == PIN)
db.close()

# ---------------------------------------------------------------- A3: return redaction
print("\n== A3: erase return-dict redaction (P1-R10-03) ==")
db = _db("a3-chain")
rec = remember(db, "s", PIN, PIN, source_refs=SRC)
out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
check("A3.1 first erase PARTIAL (remember event metadata.key == payload)",
      out["status"] == "PARTIAL", f"status={out['status']}")
check("A3.2 return dict omits the key, flags key_redacted",
      "key" not in out and out.get("key_redacted") is True)
check("A3.3 return dict JSON carries no raw payload",
      PIN not in _json(out))
meta = _latest_erase_meta(db)
check("A3.4 erase event metadata still redacts (R8 fix holds)",
      meta.get("key_redacted") is True and "key" not in meta)
rep = residual_report(db, "s")
check("A3.5 residual_report does not re-materialise the key",
      PIN not in _json(rep["controlled"]))
out2 = erase_record(db, "s", rec["id"], rec["revision"], "repair")
check("A3.6 repeat erase return also redacted",
      out2["status"] == "PARTIAL"
      and "key" not in out2 and out2.get("key_redacted") is True
      and PIN not in _json(out2))
db.close()

db = _db("a3-repair")
rec = remember(db, "s", PIN, PIN, source_refs=SRC, idempotency_key="rep-mem")
out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
check("A3-repair.0 first erase PARTIAL (step-1 refusal, record stays live)",
      out["status"] == "PARTIAL")
db.execute(
    "UPDATE memories SET value_json=?, state='erased', revision=revision+1 "
    "WHERE scope_id='s' AND id=?", (_json(TOMBSTONE), rec["id"]))
db.commit()
out2 = erase_record(db, "s", rec["id"], _rev(db, rec["id"]), "repair")
check("A3-repair.1 repair-path return redacted (needles from unscrubbed receipts)",
      out2["status"] == "PARTIAL" and out2.get("repair") is True
      and "key" not in out2 and out2.get("key_redacted") is True
      and PIN not in _json(out2), f"out2={out2}")
db.close()

db = _db("a3-benign")
rec = remember(db, "s", "kBenign", PIN, source_refs=SRC)
out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
check("A3-benign.1 benign key visible in the first-erase return",
      out["status"] == "COMPLETE" and out.get("key") == "kBenign"
      and "key_redacted" not in out)
out2 = erase_record(db, "s", rec["id"], _rev(db, rec["id"]), "repair")
check("A3-benign.2 benign key visible in the repair return",
      out2["status"] == "COMPLETE" and out2.get("key") == "kBenign"
      and "key_redacted" not in out2)
db.close()

db = _db("a3-cli")
rec = remember(db, "s", PIN, PIN, source_refs=SRC)
db.close()
env = dict(os.environ)
env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
proc = subprocess.run(
    [sys.executable, "-m", "turnkeep", "--db", str(TMPROOT / "a3-cli.sqlite3"),
     "erase", "--scope", "s", "--id", rec["id"],
     "--expected-revision", str(rec["revision"]), "--reason", "rotation"],
    capture_output=True, text=True, env=env, timeout=60,
    cwd=str(Path(__file__).resolve().parents[1]))
check("A3-cli.1 CLI erase exits 0, stdout carries no raw payload key",
      proc.returncode == 0 and PIN not in proc.stdout, f"rc={proc.returncode}")
check("A3-cli.2 CLI stdout flags key_redacted", "key_redacted" in proc.stdout)
db = _db("a3-audit")
rec = remember(db, "s", "kLive", "live note", source_refs=SRC)
new = correct(db, "s", rec["id"], rec["revision"], "new note", source_refs=SRC)
ret = retract(db, "s", new["id"], new["revision"], "withdrawn")
run = begin(db, "s", "work")
receipt = finish(db, "s", run["run_id"], {"key": "out", "value": "done"})
check("A3-audit.1 correct/retract return the LIVE record's key (no same-class echo)",
      new["key"] == "kLive" and new["state"] == "active"
      and ret["key"] == "kLive" and ret["state"] == "retracted")
check("A3-audit.2 finish receipt has no 'key' field",
      "key" not in receipt and receipt["record_id"] is not None)
db.close()

# A3 extras: the step-1 return's raw remaining rowids carry no payload,
# and the residuals CLI (which prints the report) is clean too.
db = _db("a3-extra")
rec = remember(db, "s", PIN, PIN, source_refs=SRC)
out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
check("A3.7 step-1 return's raw remaining rowids carry no payload",
      PIN not in _json(out.get("remaining", {})))
db.close()
env = dict(os.environ)
env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
proc = subprocess.run(
    [sys.executable, "-m", "turnkeep", "--db", str(TMPROOT / "a3-cli.sqlite3"),
     "residuals", "--scope", "s"],
    capture_output=True, text=True, env=env, timeout=60,
    cwd=str(Path(__file__).resolve().parents[1]))
check("A3-resid-cli: residuals CLI stdout carries no raw payload key",
      proc.returncode == 0 and PIN not in proc.stdout, f"rc={proc.returncode}")

# ---------------------------------------------------------------- D: runs.task retained
print("\n== D: runs.task encoding channel (retained, §8) ==")
db = _db("d-runs")
import base64 as _b64
enc_b64 = _b64.b64encode(PIN.encode()).decode()
rec = remember(db, "s", "k1", PIN, source_refs=SRC, idempotency_key="d-1")
begin(db, "s", f"revoke {enc_b64} now", idempotency_key="d-run")
out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
rep = residual_report(db, "s")
task = db.execute("SELECT task FROM runs WHERE scope_id='s'").fetchone()[0]
_, recs, evs = _export_rep(db, "d-runs")
check("D.1 erase COMPLETE, residual clean, encoding stays in live runs.task",
      out["status"] == "COMPLETE"
      and rep["controlled"]["live_tables_clean"] is True
      and enc_b64 in task)
check("D.2 encoding NOT in the export (runs are not exported)",
      enc_b64 not in recs and enc_b64 not in evs)
db.close()

# ---------------------------------------------------------------- E: "202" / "2026"
print('\n== E: "202" / "2026" still COMPLETE ==')
for val, expiry in (("202", LEGAL_TS), ("2026", "2027-01-01T00:00:00+00:00")):
    db = _db(f"e-{val}")
    rec = remember(db, "s", "k1", val, source_refs=SRC, idempotency_key=f"e-{val}",
                   record_id="a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6")
    set_expiry(db, "s", rec["id"], expiry, rec["revision"])
    begin(db, "s", task="check k1", idempotency_key="run1")
    out = erase_record(db, "s", rec["id"], _rev(db, rec["id"]), "rotation")
    check(f"E-{val}: erase COMPLETE (pinned id)", out["status"] == "COMPLETE",
          f"status={out['status']}")
    db.close()
fails = 0
for i in range(25):
    db = _db(f"e-loop-{i}")
    rec = remember(db, "s", "k1", "202", source_refs=SRC, idempotency_key=f"e-loop-{i}")
    set_expiry(db, "s", rec["id"], LEGAL_TS, rec["revision"])
    begin(db, "s", task="check k1", idempotency_key="run1")
    out = erase_record(db, "s", rec["id"], _rev(db, rec["id"]), "rotation")
    if out["status"] != "COMPLETE":
        fails += 1
    db.close()
check("E-loop: 25x random-id '202' erases COMPLETE", fails == 0, f"fails={fails}")
fails = 0
for i in range(10):
    db = _db(f"e-loop26-{i}")
    rec = remember(db, "s", "k1", "2026", source_refs=SRC, idempotency_key=f"e26-{i}")
    set_expiry(db, "s", rec["id"], "2027-01-01T00:00:00+00:00", rec["revision"])
    begin(db, "s", task="check k1", idempotency_key="run1")
    out = erase_record(db, "s", rec["id"], _rev(db, rec["id"]), "rotation")
    if out["status"] != "COMPLETE":
        fails += 1
    db.close()
check("E-loop26: 10x random-id '2026' erases COMPLETE", fails == 0, f"fails={fails}")

# ---------------------------------------------------------------- F: R9/R8 spot checks
print("\n== F: R9/R8 fixes still hold ==")
import codecs as _codecs
import urllib.parse as _url
encs = {
    "b64": _b64.b64encode(b"SYNTHETIC-PAYLOAD-9f3a7c2e").decode(),
    "urlsafe": _b64.urlsafe_b64encode(b"SYNTHETIC-PAYLOAD-9f3a7c2e").decode(),
    "percent": _url.quote("SYNTHETIC-PAYLOAD-9f3a7c2e"),
    "reversed": "SYNTHETIC-PAYLOAD-9f3a7c2e"[::-1],
    "hex": b"SYNTHETIC-PAYLOAD-9f3a7c2e".hex(),
    "rot13": _codecs.encode("SYNTHETIC-PAYLOAD-9f3a7c2e", "rot_13"),
}
PAYLOAD9 = "SYNTHETIC-PAYLOAD-9f3a7c2e"
db = _db("f-r9")
rec = remember(db, "s", encs["b64"], PAYLOAD9, source_refs=SRC, idempotency_key="f-r9")
out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
rep = residual_report(db, "s")
check("F-R9.1 encoded key via remember() still refuses PARTIAL",
      out["status"] == "PARTIAL" and rep["controlled"]["live_tables_clean"] is False)
rep2, recs, evs = _export_rep(db, "f-r9")
check("F-R9.2 export report still redacts the encoded key",
      len(rep2["records_excluded_secrets"]) == 1
      and "key" not in rep2["records_excluded_secrets"][0]
      and rep2["records_excluded_secrets"][0].get("key_redacted") is True
      and encs["b64"] not in _json(rep2))
db.close()
db = _db("f-r8")
rec = remember(db, "s", PAYLOAD9, PAYLOAD9, source_refs=SRC, idempotency_key="f-r8")
out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
rep = residual_report(db, "s")
check("F-R8: key == payload still erases PARTIAL (R8 verbatim control)",
      out["status"] == "PARTIAL" and rep["controlled"]["live_tables_clean"] is False)
db.close()
db = _db("f-r9-a3")
ra = remember(db, "s", "kA", PAYLOAD9, source_refs=SRC, idempotency_key="f-a3a")
rb = remember(db, "s", "kB", encs["b64"], source_refs=SRC, idempotency_key="f-a3b")
out = erase_record(db, "s", ra["id"], ra["revision"], "rotation")
rep = residual_report(db, "s")
check("F-R9-A3.1 cross-record encoded copy still refuses PARTIAL, names B",
      out["status"] == "PARTIAL"
      and any(r["record_id"] == rb["id"]
              for r in rep["controlled"]["controlled_residuals"]))
outb = erase_record(db, "s", rb["id"], rb["revision"], "rotation")
outa = erase_record(db, "s", ra["id"], _rev(db, ra["id"]), "repair")
rep3 = residual_report(db, "s")
_, recs2, evs2 = _export_rep(db, "f-r9-a3")
check("F-R9-A3.2 erase B + repair A resolves (COMPLETE, clean, export clean)",
      outb["status"] == "COMPLETE" and outa["status"] == "COMPLETE"
      and rep3["controlled"]["live_tables_clean"] is True
      and encs["b64"] not in recs2 and encs["b64"] not in evs2
      and PAYLOAD9 not in recs2 and PAYLOAD9 not in evs2)
db.close()

fails = [n for n, ok in CHECKS if not ok]
print(f"\n{len(CHECKS) - len(fails)}/{len(CHECKS)} checks passed; FAILs: {fails}")
sys.exit(1 if fails else 0)
