#!/usr/bin/env python3
"""R11 rework repro (ships in the RC as evidence for final-review R12).

Run from a source checkout, an extracted RC, or an installed wheel:
    python scripts/repro_r12.py
All checks run against throwaway SQLite files and assert the FIXED
behaviour for the R11 final-review findings (verified in
~/workspace/turnkeep-gpt-R11-verification-2026-11-12.md):

  A  P0-R11-01: the residual report's loop is closed over raw task
     text. A runs.task / runs.receipt_json.task /
     idempotency.response_json.task that still holds the needle
     verbatim is flagged on the remaining entry (task_text +
     task_locations), and an entry so flagged never resolves in the
     live DB. Disposition: keep + honest PARTIAL (runs.task is the
     run's operation history, not the erased record's own data) — the
     remedy is export to a fresh DB (runs are not exported). Covers
     GPT's A1 chain (raw task; begin before/after remember; with and
     without idempotency), the finish path, repair-after-task (report
     still not clean), and the retained §8 encoding limitation (an
     encoded task still erases COMPLETE).
  B  P0-R11-02: needle_hashes are no longer enumerable from the live
     DB. A COMPLETE erase persists NO needle hashes (the hashes would
     be the only enumerable material for the payload — the scope key
     sits in the same DB's scope_secrets table, so HMAC is no
     protection there, R10 principle). A PARTIAL erase still persists
     them (a live copy exists, so the hashes add no new leak
     surface). Non-digest compensation, computed from the live needles
     at erase time: the export-exclusion map (record ids + field
     names, in the erase event metadata) and the blocked repair-reason
     labels (live-DB-only erase_flags table — NOT in the event, since
     a blocked label is a fixed vocabulary word and persisting it in
     an event would write the payload itself into history when the
     payload is literally that label). "Move the key out of the DB"
     was evaluated and rejected: local-first single-machine SQLite,
     no external key management, and any disk file under the same OS
     user is inside the same trust boundary. Covers GPT's A2 chain
     (remember -> erase -> enumerate 0000-9999 finds nothing),
     low- and high-entropy payloads, the repair-path chain effects
     (reason vetting, identifier cleanup, digest neutralization),
     and P0-R10-02c/d (idempotency-key and source-link residuals
     still detected).
  C  P2-R11-03: structured audit-field matching. Audit-structure dict
     KEY names are never substring-matched (fixed schema words);
     controlled labels (reason, erase status, table, source type,
     kind, state, run status, guarantee) count only on whole-value
     equality; opaque digest fields (needle_hashes, request_digest,
     content_digest) never match on substring; a finish receipt's
     committed_at is a legal-timestamp audit position; the tombstone
     marker {"_erased": true} is structural, never a copy; audit
     numbers / booleans / null never match. Covers GPT's A chain
     (erase "first value", then "r" -> COMPLETE), the B chain (a
     PARTIAL erase whose persisted HMAC contains "202", then erase
     "202" -> COMPLETE — deterministic construction, not a fixed
     threshold: a single HMAC contains "202" with ~1.4% probability),
     a same-scope 30-erasure loop then "202" -> COMPLETE, 1-char "e"
     -> COMPLETE, and the retained P1-R4-06 honest limit (a 1-char
     payload colliding with a USER-DATA key name still reports
     PARTIAL, fail-closed).
  D  "202" / "2026" still erase COMPLETE (E-loop retained).
  E  R11 original-scope spot checks: the corrected P0-R11-01 rowids
     (remaining runs:[1], idempotency:[2]), the R10 digest
     neutralization still holds, and the R9/R8 spot checks.

Corrections to GPT's R11 report that this repro pins down:
  - P0-R11-01 remaining rowids measured runs:[1], idempotency:[2]
    (GPT wrote runs:[1] + idempotency:[1]; the remember's own
    idempotency row occupies rowid 1).
  - Chain A hit surface is broader than GPT listed: metadata KEY
    names, source_refs "type" enum values, the tombstone marker in
    receipt value subtrees, and booleans/numbers are hit sources too.
  - Chain B is probabilistic (single HMAC containing "202" ~1.4%,
    5-6 erasures ~14-15%), not a fixed threshold.
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
from turnkeep.storage import open_db, _json, insert_record, _id
from turnkeep.scopes import register_scope
from turnkeep.memory import remember
from turnkeep.runs import begin
from turnkeep.commits import finish
from turnkeep.retention import set_expiry
from turnkeep.erasure import (
    erase_record, residual_report, _record_erase_key, _stored_needle_hashes,
)
from turnkeep.idempotency import request_digest, NEUTRALIZED_DIGEST
from turnkeep.portability import export_scope
from turnkeep.errors import TurnkeepError

SRC = [{"id": "u:1", "type": "user_statement"}]
PIN = "4827"
HIGH = "9f3a7c2e5b1d8f4a6c0e2b7d9f1a3c5e7"  # high-entropy payload
LEGAL_TS = "2026-11-12T22:00:00+00:00"
TMPROOT = Path(tempfile.mkdtemp(prefix="turnkeep-repro-r12-"))
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


def _rev(db, rid):
    return db.execute("SELECT revision FROM memories WHERE scope_id='s' AND id=?",
                      (rid,)).fetchone()[0]


def _all_erase_metas(db):
    return [json.loads(m[0]) for m in db.execute(
        "SELECT metadata_json FROM events WHERE scope_id='s' AND event_type='erase' "
        "ORDER BY scope_epoch").fetchall()]


def _export_rep(db, name):
    out = TMPROOT / f"exp-{name}"
    rep = export_scope(db, "s", out)
    return (rep, (out / "records.jsonl").read_text(encoding="utf-8"),
            (out / "events.jsonl").read_text(encoding="utf-8"))


def _enumerate_pin(db, scope="s"):
    """Offline enumeration of a 4-digit PIN from the live DB's stored
    material (GPT's A2 chain): the scope HMAC key + the persisted
    needle hashes. Returns the recovered value, or None."""
    key_row = db.execute(
        "SELECT secret_hex FROM scope_secrets WHERE scope_id=?", (scope,)).fetchone()
    hashes = set()
    for meta in _all_erase_metas(db):
        hashes.update(h for h in meta.get("needle_hashes", []) or []
                      if isinstance(h, str))
    if key_row is None or not hashes:
        return None
    key = bytes.fromhex(key_row[0])
    for i in range(10000):
        cand = f"{i:04d}"
        if hmac.new(key, cand.encode(), hashlib.sha256).hexdigest() in hashes:
            return cand
        if hashlib.sha256(cand.encode()).hexdigest() in hashes:
            return cand
    return None


_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?([+-]\d{2}:\d{2}|Z)?$")


def _holds_pin(obj, pin=PIN):
    """True when `pin` occurs in any string value of a parsed export
    line that is NOT an ISO-8601 timestamp. A raw-text `pin not in
    events.jsonl` check is flaky: a microsecond timestamp can contain
    a 4-digit PIN by coincidence (the known R7 flake, ~0.26% per
    timestamp), so the check is structural — the PIN must not appear
    as payload material, while timestamps are exempt."""
    if isinstance(obj, str):
        return pin in obj and not _TS_RE.match(obj)
    if isinstance(obj, dict):
        return any(_holds_pin(v, pin) for v in obj.values())
    if isinstance(obj, list):
        return any(_holds_pin(v, pin) for v in obj)
    return False


def _export_holds_pin(recs, evs, pin=PIN):
    return (any(_holds_pin(json.loads(l), pin) for l in recs.splitlines() if l.strip())
            or any(_holds_pin(json.loads(l), pin) for l in evs.splitlines() if l.strip()))


def _find_payload_with_hash_substring(db, scope, substring, record_id, prefix="probe"):
    """Deterministically find a payload whose per-record-key HMAC
    contains `substring` (chain B construction): iterate candidate
    payloads until one hits. A single HMAC contains "202" with ~1.4%
    probability, so this finds one within a few dozen tries. The key
    is the record's own verification key (P0-R12-01), pre-created for
    the record id that will hold the payload — a fresh DB has no
    scope key to read."""
    key = _record_erase_key(db, scope, record_id)
    for i in range(10000):
        cand = f"{prefix}-{i}"
        if substring in hmac.new(key, cand.encode(), hashlib.sha256).hexdigest():
            return cand
    raise AssertionError(f"no payload found with HMAC containing {substring!r}")


print(f"turnkeep: {turnkeep.__file__}")

# ---------------------------------------------------------------- A: task text
print("\n== A: raw task residual loop (P0-R11-01) ==")
db = _db("a1-after")
rec = remember(db, "s", "pin", PIN, source_refs=SRC, idempotency_key="a1-1")
begin(db, "s", "revoke key 4827 now", idempotency_key="begin-key-001")
out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
rep = residual_report(db, "s")
check("A1.1 GPT A1 begin-after-remember: erase PARTIAL, report not clean",
      out["status"] == "PARTIAL" and rep["controlled"]["live_tables_clean"] is False
      and rep["controlled"]["controlled_residuals"])
check("A1.2 remaining entry flags the task text with its locations",
      any(e.get("task_text") and "runs.task" in e.get("task_locations", [])
          for e in rep["controlled"]["controlled_residuals"]))
db.close()

db = _db("a1-before")
begin(db, "s", "revoke key 4827 now", idempotency_key="begin-key-002")
rec = remember(db, "s", "pin", PIN, source_refs=SRC, idempotency_key="a1-2")
out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
rep = residual_report(db, "s")
check("A1.3 GPT A1 begin-before-remember: erase PARTIAL, report not clean",
      out["status"] == "PARTIAL" and rep["controlled"]["live_tables_clean"] is False)
db.close()

db = _db("a1-noidem")
begin(db, "s", "revoke key 4827 now")  # no idempotency key
rec = remember(db, "s", "pin", PIN, source_refs=SRC, idempotency_key="a1-3")
out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
rep = residual_report(db, "s")
check("A1.4 GPT A1 no-idempotency begin: erase PARTIAL, report not clean",
      out["status"] == "PARTIAL" and rep["controlled"]["live_tables_clean"] is False)
db.close()

db = _db("a1-finish")
rec = remember(db, "s", "pin", PIN, source_refs=SRC, idempotency_key="a1-4")
run = begin(db, "s", "revoke key 4827 now", idempotency_key="a1f-1")
finish(db, "s", run["run_id"], {"key": "out", "value": "result note"})
out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
rep = residual_report(db, "s")
check("A1.5 finish path: erase PARTIAL, report not clean (runs.task survives finish)",
      out["status"] == "PARTIAL" and rep["controlled"]["live_tables_clean"] is False)
db.close()

db = _db("a1-repair")
rec = remember(db, "s", "pin", PIN, source_refs=SRC, idempotency_key="a1-5")
begin(db, "s", "revoke key 4827 now", idempotency_key="a1r-1")
out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
assert out["status"] == "PARTIAL"
out2 = erase_record(db, "s", rec["id"], _rev(db, rec["id"]), "repair")
rep = residual_report(db, "s")
check("A1.6 repair with task still present: still PARTIAL, report still not clean",
      out2["status"] == "PARTIAL" and rep["controlled"]["live_tables_clean"] is False)
db.close()

db = _db("a1-encoded")
b64 = base64.b64encode(PIN.encode()).decode()
begin(db, "s", f"revoke key {b64} now", idempotency_key="a1e-1")
rec = remember(db, "s", "pin", PIN, source_refs=SRC, idempotency_key="a1-6")
out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
rep = residual_report(db, "s")
check("A1.7 encoded task retained (§8): erase COMPLETE, report clean",
      out["status"] == "COMPLETE" and rep["controlled"]["live_tables_clean"] is True)
db.close()

# ---------------------------------------------------------------- B: hashes
print("\n== B: needle_hashes non-enumerable (P0-R11-02) ==")
db = _db("a2-chain")
rec = remember(db, "s", "pin", PIN, source_refs=SRC, idempotency_key="a2-1")
out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
meta = _all_erase_metas(db)[0]
check("B1.1 GPT A2: COMPLETE erase persists no needle hashes",
      out["status"] == "COMPLETE" and meta["needle_hashes"] == [])
check("B1.2 GPT A2: offline enumeration 0000-9999 finds nothing",
      _enumerate_pin(db) is None)
rep = residual_report(db, "s")
check("B1.3 GPT A2: residual report clean, export has no hashes",
      rep["controlled"]["live_tables_clean"] is True)
_, recs, evs = _export_rep(db, "a2-chain")
check("B1.4 export carries no needle hashes and no PIN",
      not _export_holds_pin(recs, evs)
      and all(not json.loads(l)["metadata"].get("needle_hashes")
              for l in evs.splitlines() if l.strip()))
db.close()

db = _db("a2-partial")
r1 = remember(db, "s", "a", PIN, source_refs=SRC, idempotency_key="a2p-1")
remember(db, "s", "b", PIN, source_refs=SRC, idempotency_key="a2p-2")
out = erase_record(db, "s", r1["id"], r1["revision"], "rotation")
meta = _all_erase_metas(db)[0]
check("B2 PARTIAL erase still persists hashes (live copy exists)",
      out["status"] == "PARTIAL" and meta["needle_hashes"]
      and len(meta["needle_hashes"]) == len(set(meta["needle_hashes"])))
db.close()

db = _db("a2-high")
rec = remember(db, "s", "k", HIGH, source_refs=SRC, idempotency_key="a2h-1")
out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
check("B3 high-entropy COMPLETE erase persists no hashes",
      out["status"] == "COMPLETE" and _all_erase_metas(db)[0]["needle_hashes"] == [])
db.close()

db = _db("a2-repair-reason")
rec = remember(db, "s", "k", "repair", source_refs=SRC, idempotency_key="a2r-1")
out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
assert out["status"] == "COMPLETE"
blocked = None
try:
    erase_record(db, "s", rec["id"], _rev(db, rec["id"]), "repair")
except TurnkeepError as e:
    blocked = e.code
out2 = erase_record(db, "s", rec["id"], _rev(db, rec["id"]), "rotation")
check("B4 repair reason vetting via flags: 'repair' refused, 'rotation' succeeds",
      blocked == "INVALID_INPUT" and out2["status"] == "COMPLETE")
db.close()

db = _db("a2-repair-key")
rec = remember(db, "s", "api-4827-key", PIN, source_refs=SRC, idempotency_key="a2k-1")
out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
assert out["status"] == "COMPLETE"
out2 = erase_record(db, "s", rec["id"], _rev(db, rec["id"]), "repair")
meta2 = _all_erase_metas(db)[-1]
check("B5 repair key redaction via flags (key contains the payload)",
      out2["status"] == "COMPLETE" and meta2.get("key_redacted") is True)
db.close()

db = _db("a2-r10-02c")
# P0-R10-02c: ANOTHER record's idempotency key is this record's
# payload. (The erased record's OWN idempotency row whose key IS its
# payload is deleted outright by the erase — it is the erased
# operation's own derived copy, and its key cannot be scrubbed — so
# the same-record construction erases COMPLETE by design.)
rb = remember(db, "s", "b", "other note", source_refs=SRC,
              idempotency_key="SYNTHETIC-PAYLOAD-9f3a7c2e")
ra = remember(db, "s", "pin", "SYNTHETIC-PAYLOAD-9f3a7c2e", source_refs=SRC,
              idempotency_key="a-key")
out = erase_record(db, "s", ra["id"], ra["revision"], "rotation")
rep = residual_report(db, "s")
check("B6.1 P0-R10-02c: another record's idempotency key == payload still detected (PARTIAL)",
      out["status"] == "PARTIAL" and rep["controlled"]["live_tables_clean"] is False
      and any(r["record_id"] == rb["id"] and r["table"] == "idempotency"
              for r in rep["controlled"]["controlled_residuals"]))
db.close()

db = _db("a2-r10-02d")
rec = remember(db, "s", "k1", "SYNTHETIC-PAYLOAD-9f3a7c2e", source_refs=SRC,
               idempotency_key="a2d-1")
# A second record citing a source whose id IS the payload (planted
# directly, so no remember event carries the key).
insert_record(db, "s", "k2", _json({"note": "other"}), "fact",
              [{"id": "SYNTHETIC-PAYLOAD-9f3a7c2e", "type": "user_statement"}],
              [], {}, state="active", cause_event_id="legacy-import", record_id="r2")
out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
rep = residual_report(db, "s")
check("B6.2 P0-R10-02d: source link id == payload still detected (PARTIAL)",
      out["status"] == "PARTIAL" and rep["controlled"]["live_tables_clean"] is False)
db.close()

db = _db("a2-export-map")
rec = remember(db, "s", "pin", PIN, source_refs=SRC, idempotency_key="a2x-1")
# Record rB's KEY is the payload (planted directly, so no remember
# event carries it).
insert_record(db, "s", PIN, _json({"note": "other"}), "fact", [], [], {},
              state="active", cause_event_id="legacy-import", record_id="rB")
out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
assert out["status"] == "COMPLETE"
_, recs, evs = _export_rep(db, "a2-export-map")
check("B7 cross-record key excluded from export after COMPLETE (map, not hashes)",
      not _export_holds_pin(recs, evs))
db.close()

# ---------------------------------------------------------------- C: short values
print("\n== C: short-value false positives (P2-R11-03) ==")
db = _db("a-chain")
r1 = remember(db, "s", "k1", "first value", source_refs=SRC, idempotency_key="c1-1")
out1 = erase_record(db, "s", r1["id"], r1["revision"], "rotation")
r2 = remember(db, "s", "k2", "r", source_refs=SRC, idempotency_key="c1-2")
out2 = erase_record(db, "s", r2["id"], r2["revision"], "rotation")
check("C1 GPT chain A: erase 'first value' then 'r' -> COMPLETE",
      out1["status"] == "COMPLETE" and out2["status"] == "COMPLETE")
db.close()

db = _db("b-chain")
# Deterministic chain B: a payload whose persisted HMAC contains "202",
# erased PARTIAL (so the hash is persisted), then erase "202" -> COMPLETE.
rid = _id()
probe = _find_payload_with_hash_substring(db, "s", "202", rid)
pa = remember(db, "s", "ka", probe, source_refs=SRC, idempotency_key="cb-a",
              record_id=rid)
remember(db, "s", "kb", probe, source_refs=SRC, idempotency_key="cb-b")
outa = erase_record(db, "s", pa["id"], pa["revision"], "rotation")
assert outa["status"] == "PARTIAL"
persisted = [h for m in _all_erase_metas(db) for h in m.get("needle_hashes", [])]
assert any("202" in h for h in persisted), "construction must persist a '202' HMAC"
rc = remember(db, "s", "kc", "202", source_refs=SRC, idempotency_key="cb-c")
outc = erase_record(db, "s", rc["id"], rc["revision"], "rotation")
check("C2 GPT chain B (deterministic): persisted HMAC contains '202', erase '202' COMPLETE",
      outc["status"] == "COMPLETE")
db.close()

db = _db("c-loop")
fails = 0
for i in range(30):
    payload = f"loop-payload-{i:02d}-abcdef"
    ra = remember(db, "s", f"la{i}", payload, source_refs=SRC, idempotency_key=f"cl-a{i}")
    remember(db, "s", f"lb{i}", payload, source_refs=SRC, idempotency_key=f"cl-b{i}")
    outa = erase_record(db, "s", ra["id"], ra["revision"], "rotation")
    if outa["status"] != "PARTIAL":
        fails += 1
r202 = remember(db, "s", "k202", "202", source_refs=SRC, idempotency_key="cl-202")
out202 = erase_record(db, "s", r202["id"], r202["revision"], "rotation")
check("C3 same-scope 30-erasure loop, then '202' -> COMPLETE",
      fails == 0 and out202["status"] == "COMPLETE")
db.close()

db = _db("c-e")
rec = remember(db, "s", "k", "e", source_refs=SRC, idempotency_key="ce-1")
out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
check("C4 1-char payload 'e' -> COMPLETE (audit key names no longer collide)",
      out["status"] == "COMPLETE")
db.close()

db = _db("c-userdata-key")
remember(db, "s", "b", {"key": "note"}, source_refs=SRC, idempotency_key="cu-1")
ra = remember(db, "s", "a", "e", source_refs=SRC, idempotency_key="cu-2")
out = erase_record(db, "s", ra["id"], ra["revision"], "rotation")
check("C5 P1-R4-06 retained: 1-char payload colliding with a USER-DATA key name still PARTIAL",
      out["status"] == "PARTIAL" and "memories" in out["remaining"])
db.close()

db = _db("c-reason-label")
rec = remember(db, "s", "k", "repair", source_refs=SRC, idempotency_key="cr-1")
out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
check("C6 controlled reason label 'repair' as payload, erased with 'rotation' -> COMPLETE",
      out["status"] == "COMPLETE")
db.close()

db = _db("c-committed")
rec = remember(db, "s", "k1", "202", source_refs=SRC, idempotency_key="cc-1")
set_expiry(db, "s", rec["id"], LEGAL_TS, rec["revision"])
run = begin(db, "s", "check k1", idempotency_key="cc-run1")
finish(db, "s", run["run_id"], {"key": "out", "value": "result note"})
out = erase_record(db, "s", rec["id"], _rev(db, rec["id"]), "rotation")
check("C7 finish receipt committed_at (legal timestamp) does not block '202' -> COMPLETE",
      out["status"] == "COMPLETE")
db.close()

# ---------------------------------------------------------------- D: E-loop
print("\n== D: '202' / '2026' still COMPLETE (E-loop) ==")
fails = 0
for i in range(10):
    db = _db(f"d-loop202-{i}")
    rec = remember(db, "s", "k1", "202", source_refs=SRC, idempotency_key=f"d202-{i}")
    set_expiry(db, "s", rec["id"], LEGAL_TS, rec["revision"])
    begin(db, "s", "check k1", idempotency_key="run1")
    out = erase_record(db, "s", rec["id"], _rev(db, rec["id"]), "rotation")
    if out["status"] != "COMPLETE":
        fails += 1
    db.close()
check("D1 E-loop: 10x '202' erases COMPLETE", fails == 0, f"fails={fails}")
fails = 0
for i in range(10):
    db = _db(f"d-loop26-{i}")
    rec = remember(db, "s", "k1", "2026", source_refs=SRC, idempotency_key=f"d26-{i}")
    set_expiry(db, "s", rec["id"], "2027-01-01T00:00:00+00:00", rec["revision"])
    begin(db, "s", "check k1", idempotency_key="run1")
    out = erase_record(db, "s", rec["id"], _rev(db, rec["id"]), "rotation")
    if out["status"] != "COMPLETE":
        fails += 1
    db.close()
check("D2 E-loop: 10x '2026' erases COMPLETE", fails == 0, f"fails={fails}")

# ---------------------------------------------------------------- E: spot checks
print("\n== E: R11 / R10 original-scope spot checks ==")
db = _db("e-rowids")
rec = remember(db, "s", "pin", PIN, source_refs=SRC, idempotency_key="e-1")
begin(db, "s", "revoke key 4827 now", idempotency_key="begin-key-001")
out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
# Corrected P0-R11-01 rowids: runs:[1], idempotency:[2] (the remember's
# own idempotency row occupies rowid 1 — GPT wrote idempotency:[1]).
remaining = out.get("remaining", {})
check("E1 corrected P0-R11-01 rowids: remaining runs:[1], idempotency:[2]",
      remaining.get("runs") == [1] and remaining.get("idempotency") == [2],
      f"remaining={remaining}")
db.close()

db = _db("e-r10-digest")
rec = remember(db, "s", "pin", PIN, source_refs=SRC, idempotency_key="e10-1")
erase_record(db, "s", rec["id"], rec["revision"], "rotation")
row = db.execute(
    "SELECT request_digest FROM idempotency WHERE scope_id='s' AND idempotency_key='e10-1'"
).fetchone()
check("E2 R10 spot: request_digest neutralized to the non-hex marker after erasure",
      row is not None and row[0] == NEUTRALIZED_DIGEST)
db.close()

db = _db("e-r9")
encs = base64.b64encode(b"SYNTHETIC-PAYLOAD-9f3a7c2e").decode()
rec = remember(db, "s", encs, "SYNTHETIC-PAYLOAD-9f3a7c2e", source_refs=SRC,
               idempotency_key="e-r9")
out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
rep = residual_report(db, "s")
check("E3 R9 spot: encoded key via remember() still refuses PARTIAL",
      out["status"] == "PARTIAL" and rep["controlled"]["live_tables_clean"] is False)
db.close()

db = _db("e-r8")
rec = remember(db, "s", "SYNTHETIC-PAYLOAD-9f3a7c2e", "SYNTHETIC-PAYLOAD-9f3a7c2e",
               source_refs=SRC, idempotency_key="e-r8")
out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
rep = residual_report(db, "s")
check("E4 R8 spot: key == payload still erases PARTIAL",
      out["status"] == "PARTIAL" and rep["controlled"]["live_tables_clean"] is False)
db.close()

fails = [n for n, ok in CHECKS if not ok]
print(f"\n{len(CHECKS) - len(fails)}/{len(CHECKS)} checks passed; FAILs: {fails}")
sys.exit(1 if fails else 0)
