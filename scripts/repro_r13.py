"""Repro for the R13 rework (GPT R12 verdict, P0-R12-01 + P1-R12-02 +
erase_flags 2-bit retention). Runs from a source checkout, an extracted
RC, or an installed wheel against throwaway DBs.

Sections:
  A: P0-R12-01 — resolved PARTIAL erasures leave no enumerable
     historical hashes (per-record verification keys).
  B: P1-R12-02 — decode-equality detection for fixed audit labels
     (erase_status, kind, reason, PARTIAL, all kinds / erase reasons).
  C: erase_flags 2-bit residual — retained, documented.
  D: "202" / "2026" still COMPLETE + high-entropy control.
  E: R12 original-scope spot checks.

Temp-dir hygiene: the throwaway DBs live under a single TMPROOT in
/tmp (tmpfs, 512M). Earlier repro scripts left one ~13M TMPROOT per
run without cleanup, which filled /tmp during verification runs. This
script removes its TMPROOT on success (registered with atexit); on
failure it keeps it and prints the path, so the failing DBs can be
inspected.
"""
import atexit
import base64
import hashlib
import hmac
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import turnkeep  # noqa: F401  (proves the import resolves: source, RC or wheel)
from turnkeep.storage import open_db, _json, _id
from turnkeep.scopes import register_scope
from turnkeep.memory import remember
from turnkeep.runs import begin
from turnkeep.erasure import (
    erase_record, residual_report, _record_erase_key,
    _stored_needle_hashes,
)
from turnkeep.portability import export_scope
from turnkeep.errors import TurnkeepError

SRC = [{"id": "u:1", "type": "user_statement"}]
PIN = "4827"
HIGH = "9f3a7c2e5b1d8f4a6c0e2b7d9f1a3c5e7"  # high-entropy payload
TMPROOT = Path(tempfile.mkdtemp(prefix="turnkeep-repro-r13-"))
CHECKS = []


def _cleanup():
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


def _rev(db, rid):
    return db.execute("SELECT revision FROM memories WHERE scope_id='s' AND id=?",
                      (rid,)).fetchone()[0]


def _all_erase_metas(db):
    return [json.loads(m[0]) for m in db.execute(
        "SELECT metadata_json FROM events WHERE scope_id='s' AND event_type='erase' "
        "ORDER BY scope_epoch").fetchall()]


def _enumerate_pin(db, scope="s"):
    """Offline enumeration of a 4-digit PIN: EVERY key the DB holds
    (legacy scope key + every per-record verification key) against
    the persisted needle hashes. Returns the recovered value, or None."""
    keys = []
    for (hexkey,) in db.execute(
            "SELECT secret_hex FROM scope_secrets WHERE scope_id=?", (scope,)).fetchall():
        keys.append(bytes.fromhex(hexkey))
    for (hexkey,) in db.execute(
            "SELECT secret_hex FROM record_erase_keys WHERE scope_id=?", (scope,)).fetchall():
        keys.append(bytes.fromhex(hexkey))
    hashes = set()
    for meta in _all_erase_metas(db):
        hashes.update(h for h in meta.get("needle_hashes", []) or []
                      if isinstance(h, str))
    if not keys or not hashes:
        return None
    for i in range(10000):
        cand = f"{i:04d}"
        for key in keys:
            if hmac.new(key, cand.encode(), hashlib.sha256).hexdigest() in hashes:
                return cand
        if hashlib.sha256(cand.encode()).hexdigest() in hashes:
            return cand
    return None


def _is_enumerable(db, payload, scope="s"):
    keys = []
    for (hexkey,) in db.execute(
            "SELECT secret_hex FROM scope_secrets WHERE scope_id=?", (scope,)).fetchall():
        keys.append(bytes.fromhex(hexkey))
    for (hexkey,) in db.execute(
            "SELECT secret_hex FROM record_erase_keys WHERE scope_id=?", (scope,)).fetchall():
        keys.append(bytes.fromhex(hexkey))
    hashes = set()
    for meta in _all_erase_metas(db):
        hashes.update(h for h in meta.get("needle_hashes", []) or []
                      if isinstance(h, str))
    for key in keys:
        if hmac.new(key, payload.encode(), hashlib.sha256).hexdigest() in hashes:
            return True
    return hashlib.sha256(payload.encode()).hexdigest() in hashes


def _key_rows(db, scope="s"):
    return [r[0] for r in db.execute(
        "SELECT record_id FROM record_erase_keys WHERE scope_id=?", (scope,)).fetchall()]


def _b64(b):
    return base64.b64encode(b).decode()


def _hex(b):
    return b.hex() if isinstance(b, bytes) else b.encode().hex()


def _rot13(s):
    return s.translate(str.maketrans(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ",
        "nopqrstuvwxyzabcdefghijklmNOPQRSTUVWXYZABCDEFGHIJKLM"))


_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?([+-]\d{2}:\d{2}|Z)?$")


def _holds_pin(obj, pin=PIN):
    """True when `pin` occurs in any string value of a parsed export
    line that is NOT an ISO-8601 timestamp (structured check — a raw
    substring check is flaky on microsecond timestamps, R7)."""
    if isinstance(obj, str):
        return pin in obj and not _TS_RE.match(obj)
    if isinstance(obj, dict):
        return any(_holds_pin(v, pin) for v in obj.values())
    if isinstance(obj, list):
        return any(_holds_pin(v, pin) for v in obj)
    return False


def _export_rep(db, name):
    out = TMPROOT / f"exp-{name}"
    rep = export_scope(db, "s", out)
    return (rep, (out / "records.jsonl").read_text(encoding="utf-8"),
            (out / "events.jsonl").read_text(encoding="utf-8"))


print(f"turnkeep: {turnkeep.__file__}")

# ---------------------------------------------------------------- A: P0-R12-01
print("\n== A: resolved PARTIAL erasures leave no enumerable hashes (P0-R12-01) ==")
db = _db("a1-chain")
ra = remember(db, "s", "pin", PIN, source_refs=SRC, idempotency_key="a1-1")
rb = remember(db, "s", "code", PIN, source_refs=SRC, idempotency_key="a1-2")
o1 = erase_record(db, "s", ra["id"], ra["revision"], "rotation")
check("A1.1 erase A PARTIAL (B still holds the value)", o1["status"] == "PARTIAL")
check("A1.2 while PARTIAL the PIN IS enumerable (key alive)",
      _enumerate_pin(db) == PIN)
o2 = erase_record(db, "s", rb["id"], rb["revision"], "rotation")
check("A1.3 erase B COMPLETE", o2["status"] == "COMPLETE")
check("A1.4 B's key retired at its COMPLETE; A's key still alive",
      _key_rows(db) == [ra["id"]])
check("A1.5 PIN still enumerable until A is repaired",
      _enumerate_pin(db) == PIN)
o3 = erase_record(db, "s", ra["id"], o1["revision"], "repair")
check("A1.6 repair A COMPLETE", o3["status"] == "COMPLETE")
rep = residual_report(db, "s")
check("A1.7 report clean, no controlled residuals",
      rep["controlled"]["live_tables_clean"] is True
      and rep["controlled"]["controlled_residuals"] == [])
check("A1.8 both per-record keys retired", _key_rows(db) == [])
check("A1.9 enumeration 0000-9999 finds nothing",
      _enumerate_pin(db) is None)
check("A1.10 first PARTIAL event's hashes still in append-only history",
      any(m.get("needle_hashes") for m in _all_erase_metas(db)))
check("A1.11 no scope key was ever created in a fresh DB",
      db.execute("SELECT COUNT(*) FROM scope_secrets WHERE scope_id='s'").fetchone()[0] == 0)
db.close()

db = _db("a2-cross")
ra = remember(db, "s", "pin", PIN, source_refs=SRC, idempotency_key="a2-1")
begin(db, "s", "revoke key 9999 now", idempotency_key="a2-begin")
rb = remember(db, "s", "code", "9999", source_refs=SRC, idempotency_key="a2-2")
o1 = erase_record(db, "s", ra["id"], ra["revision"], "rotation")
o2 = erase_record(db, "s", rb["id"], rb["revision"], "rotation")
check("A2.1 cross-payload: A COMPLETE while B still PARTIAL (run task)",
      o1["status"] == "COMPLETE" and o2["status"] == "PARTIAL")
check("A2.2 only B's key remains", _key_rows(db) == [rb["id"]])
check("A2.3 A's payload not enumerable; B's is (PARTIAL, by design)",
      _is_enumerable(db, PIN) is False and _is_enumerable(db, "9999") is True)
db.close()

db = _db("a3-single")
rec = remember(db, "s", "pin", PIN, source_refs=SRC, idempotency_key="a3-1")
begin(db, "s", "revoke key 4827 now", idempotency_key="a3-begin")
o1 = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
check("A3.1 single-record PARTIAL (run task holds the payload)",
      o1["status"] == "PARTIAL")
o2 = erase_record(db, "s", rec["id"], o1["revision"], "repair")
check("A3.2 repair stays PARTIAL (task text never scrubbed — honest, not washed)",
      o2["status"] == "PARTIAL")
check("A3.3 key stays alive while PARTIAL",
      _key_rows(db) == [rec["id"]] and _enumerate_pin(db) == PIN)
db.close()

db = _db("a4-vetting")
ra = remember(db, "s", "pin", PIN, source_refs=SRC, idempotency_key="a4-1")
rb = remember(db, "s", "code", PIN, source_refs=SRC, idempotency_key="a4-2")
o1 = erase_record(db, "s", ra["id"], ra["revision"], "rotation")
try:
    erase_record(db, "s", ra["id"], o1["revision"], PIN)
    raised = None
except TurnkeepError as e:
    raised = e.code
check("A4.1 repair with a reason that IS the payload is refused",
      raised == "INVALID_INPUT", f"raised={raised}")
o2 = erase_record(db, "s", rb["id"], rb["revision"], "rotation")
o3 = erase_record(db, "s", ra["id"], o1["revision"], "repair")
check("A4.2 repair with reason 'repair' completes the chain",
      o2["status"] == "COMPLETE" and o3["status"] == "COMPLETE")
check("A4.3 enumeration finds nothing after repair-COMPLETE",
      _enumerate_pin(db) is None)
db.close()

db = _db("a5-02c")
rb = remember(db, "s", "b", "other note", source_refs=SRC, idempotency_key=PIN)
ra = remember(db, "s", "pin", PIN, source_refs=SRC, idempotency_key="a5-1")
out = erase_record(db, "s", ra["id"], ra["revision"], "rotation")
outb = erase_record(db, "s", rb["id"], rb["revision"], "rotation")
rep = residual_report(db, "s")
check("A5 P0-R10-02c under per-record keys: erasing B does NOT resolve "
      "the idempotency-key residual (key is A's payload, not B's)",
      out["status"] == "PARTIAL" and outb["status"] == "COMPLETE"
      and rep["controlled"]["live_tables_clean"] is False
      and any(r["table"] == "idempotency"
              for r in rep["controlled"]["controlled_residuals"]))
db.close()

db = _db("a6-02d")
rb = remember(db, "s", "b", "other note",
              source_refs=[{"id": PIN, "type": "user_statement"}],
              idempotency_key="a6-1")
ra = remember(db, "s", "pin", PIN, source_refs=SRC, idempotency_key="a6-2")
out = erase_record(db, "s", ra["id"], ra["revision"], "rotation")
outb = erase_record(db, "s", rb["id"], rb["revision"], "rotation")
rep = residual_report(db, "s")
check("A6 P0-R10-02d under per-record keys: erasing B does NOT resolve "
      "the live-source-link residual",
      out["status"] == "PARTIAL" and outb["status"] == "COMPLETE"
      and rep["controlled"]["live_tables_clean"] is False
      and any(r["table"] == "sources"
              for r in rep["controlled"]["controlled_residuals"]))
db.close()

# ---------------------------------------------------------------- B: P1-R12-02
print("\n== B: decode-equality for fixed audit labels (P1-R12-02) ==")
for i, payload in enumerate((_b64(b"COMPLETE"), "ETELPMOC",
                             _hex(b"COMPLETE"), _rot13("COMPLETE"))):
    db = _db(f"b-status-{i}")
    rec = remember(db, "s", "pin", payload, source_refs=SRC,
                   idempotency_key=f"b-st-{i}")
    out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
    rep = residual_report(db, "s")
    check(f"B1.{i+1} erase_status chain ({payload[:12]}...): PARTIAL + status_label",
          out["status"] == "PARTIAL"
          and any(e.get("status_label")
                  and "events.erase_status" in e.get("status_locations", [])
                  for e in rep["controlled"]["controlled_residuals"]))
    db.close()

for i, payload in enumerate((_b64(b"fact"), "tcaf", _hex(b"fact"), _rot13("fact"))):
    db = _db(f"b-kind-{i}")
    rec = remember(db, "s", "pin", payload, source_refs=SRC, kind="fact",
                   idempotency_key=f"b-kd-{i}")
    out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
    rep = residual_report(db, "s")
    _, recs, _ = _export_rep(db, f"b-kind-{i}")
    check(f"B2.{i+1} kind chain ({payload[:12]}...): PARTIAL + kind_label + export excludes tombstone",
          out["status"] == "PARTIAL"
          and any(e.get("kind_label")
                  and "memories.kind" in e.get("kind_locations", [])
                  for e in rep["controlled"]["controlled_residuals"])
          and rec["id"] not in recs and payload not in recs)
    db.close()

db = _db("b-partial")
payload = _b64(b"PARTIAL")
ra = remember(db, "s", "pin", payload, source_refs=SRC, idempotency_key="b-pl-1")
remember(db, "s", "code", payload, source_refs=SRC, idempotency_key="b-pl-2")
out = erase_record(db, "s", ra["id"], ra["revision"], "rotation")
rep = residual_report(db, "s")
check("B3 PARTIAL label chain (live copy): status_label recorded, never resolves",
      out["status"] == "PARTIAL"
      and any(e.get("status_label")
              for e in rep["controlled"]["controlled_residuals"]))
db.close()

for i, kind in enumerate(["requirement", "fact", "decision",
                          "attempt", "procedure", "handoff"]):
    db = _db(f"b-kl-{i}")
    rec = remember(db, "s", "pin", _b64(kind.encode()), source_refs=SRC,
                   kind=kind, idempotency_key=f"b-kl-{i}")
    out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
    check(f"B4.{i+1} kind label {kind!r}: PARTIAL", out["status"] == "PARTIAL")
    db.close()

for i, reason in enumerate(["rotation", "cleanup", "repair"]):
    db = _db(f"b-rl-{i}")
    rec = remember(db, "s", "pin", _b64(reason.encode()), source_refs=SRC,
                   idempotency_key=f"b-rl-{i}")
    try:
        erase_record(db, "s", rec["id"], rec["revision"], reason)
        raised = None
    except TurnkeepError as e:
        raised = e.code
    check(f"B5.{i+1} reason label {reason!r}: INVALID_INPUT, not written",
          raised == "INVALID_INPUT", f"raised={raised}")
    db.close()

for payload in ("r", "cg=="):
    db = _db(f"b-bound-{payload}")
    rec = remember(db, "s", "pin", payload, source_refs=SRC,
                   idempotency_key=f"b-bd-{payload}")
    out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
    rep = residual_report(db, "s")
    check(f"B6 boundary {payload!r}: still COMPLETE, report clean",
          out["status"] == "COMPLETE"
          and rep["controlled"]["live_tables_clean"] is True)
    db.close()

db = _db("b-repair-kind")
rec = remember(db, "s", "pin", _b64(b"fact"), source_refs=SRC, kind="fact",
               idempotency_key="b-rk-1")
o1 = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
o2 = erase_record(db, "s", rec["id"], o1["revision"], "repair")
rep = residual_report(db, "s")
check("B7 repair re-derives kind from stored hashes: stays PARTIAL, key alive",
      o1["status"] == "PARTIAL" and o2["status"] == "PARTIAL"
      and any(e.get("kind_label")
              for e in rep["controlled"]["controlled_residuals"])
      and _key_rows(db) == [rec["id"]])
db.close()

# ---------------------------------------------------------------- C: erase_flags
print("\n== C: erase_flags 2-bit residual — retained, documented ==")
db = _db("c-flags")
rec = remember(db, "s", "pin", "repair", source_refs=SRC, idempotency_key="c-1")
out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
row = db.execute(
    "SELECT blocked_repair_reasons FROM erase_flags "
    "WHERE scope_id=? AND record_id=?", ("s", rec["id"])).fetchone()
rep = residual_report(db, "s")
check("C1 payload 'repair': COMPLETE, blocked label in live-DB-only table",
      out["status"] == "COMPLETE"
      and row is not None and json.loads(row[0]) == ["repair"])
check("C2 report clean, and its scope text states the 2-bit residual is not covered",
      rep["controlled"]["live_tables_clean"] is True
      and "2-bit" in rep["controlled"]["live_tables_clean_scope"])
db.close()

# ---------------------------------------------------------------- D: "202"/"2026"
print("\n== D: '202' / '2026' still COMPLETE + high-entropy control ==")
db = _db("d-loop")
fails = 0
for i in range(10):
    r = remember(db, "s", f"k{i}", "202", source_refs=SRC, idempotency_key=f"d-{i}")
    if erase_record(db, "s", r["id"], r["revision"], "rotation")["status"] != "COMPLETE":
        fails += 1
for i in range(10):
    r = remember(db, "s", f"y{i}", "2026", source_refs=SRC, idempotency_key=f"d-y{i}")
    if erase_record(db, "s", r["id"], r["revision"], "rotation")["status"] != "COMPLETE":
        fails += 1
check("D1 10x '202' + 10x '2026' -> COMPLETE", fails == 0)
db.close()

db = _db("d-high")
rec = remember(db, "s", "k", HIGH, source_refs=SRC, idempotency_key="d-h-1")
out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
check("D2 high-entropy payload: COMPLETE, no hashes, no key rows",
      out["status"] == "COMPLETE"
      and _all_erase_metas(db)[0].get("needle_hashes") == []
      and _key_rows(db) == [])
db.close()

# ---------------------------------------------------------------- E: spot checks
print("\n== E: R12 original-scope spot checks ==")
db = _db("e-task")
rec = remember(db, "s", "pin", PIN, source_refs=SRC, idempotency_key="e-1")
begin(db, "s", "revoke key 4827 now", idempotency_key="e-begin")
out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
rep = residual_report(db, "s")
check("E1 R11 spot: task-text residual still PARTIAL + flagged",
      out["status"] == "PARTIAL"
      and any(e.get("task_text")
              for e in rep["controlled"]["controlled_residuals"]))
db.close()

db = _db("e-complete")
rec = remember(db, "s", "pin", PIN, source_refs=SRC, idempotency_key="e-2")
out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
_, recs, evs = _export_rep(db, "e-complete")
check("E2 R11 spot: single-record COMPLETE, no hashes, export holds no PIN",
      out["status"] == "COMPLETE"
      and _all_erase_metas(db)[0].get("needle_hashes") == []
      and not any(_holds_pin(json.loads(l)) for l in recs.splitlines() if l.strip())
      and not any(_holds_pin(json.loads(l)) for l in evs.splitlines() if l.strip()))
db.close()

db = _db("e-key-payload")
rec = remember(db, "s", PIN, PIN, source_refs=SRC, idempotency_key="e-3")
out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
check("E3 R8 spot: key == payload still erases PARTIAL", out["status"] == "PARTIAL")
db.close()

fails = [n for n, ok in CHECKS if not ok]
print(f"\n{len(CHECKS) - len(fails)}/{len(CHECKS)} checks passed; FAILs: {fails}")
sys.exit(1 if fails else 0)
