"""R11 rework regression tests: P0-R11-01 (runs.task raw-text residual
falsely reported clean), P0-R11-02 (needle_hashes still enumerable
offline from the live DB), P2-R11-03 (same-scope historical audit
metadata causes short-value false positives).

Fix rules (verification report §3):
  1. Task text is part of the residual loop: a runs / idempotency row
     whose task still holds the needle verbatim is flagged on the
     remaining entry (task_text + task_locations), and an entry so
     flagged never resolves in the live DB. Disposition: keep + honest
     PARTIAL (runs.task is the run's operation history, not the erased
     record's own data; clearing it would falsify audit history) —
     the remedy is export to a fresh DB (runs are not exported).
     Encoded task text is still NOT scanned (retained §8 limitation).
  2. A COMPLETE erase persists NO needle hashes (the hashes would be
     the only enumerable material for the payload in the live DB — the
     scope key sits in the same DB's scope_secrets table, so HMAC is
     no protection there, R10 principle). PARTIAL erasures still
     persist them (a live copy exists, so the hashes add no new leak
     surface). Non-digest compensation, computed from the live needles
     at erase time: the export-exclusion map (record ids + field
     names, persisted in the erase event's metadata) and the blocked
     repair-reason labels (live-DB-only erase_flags table — NOT in the
     event, since a blocked label is a fixed vocabulary word and
     persisting it in an event would write the payload itself into
     history when the payload is literally that label), plus the
     key_redacted flag (erase-event key + residual-report redaction).
     "Move the key out of the DB" was evaluated and rejected:
     local-first single-machine SQLite, no external key management,
     and any disk file under the same OS user is inside the same trust
     boundary.
  3. Structured audit-field matching: audit-structure dict KEY names
     are never substring-matched (they are fixed schema words); a
     controlled label (reason, erase status, table, source type, kind,
     state, run status, guarantee, blocked-repair-reasons,
     export-excluded field names) counts only on whole-value equality;
     an opaque digest field (needle_hashes, request_digest,
     content_digest) never matches on substring; a finish receipt's
     committed_at is a legal-timestamp audit position like created_at;
     the tombstone marker {"_erased": true} is structural, never a
     copy; numbers / booleans / null in audit structure never match.
     User-data key names still match (P1-R4-06 honest limit, kept):
     a 1-char payload colliding with a user-data key name still
     reports PARTIAL, fail-closed.

Setup recipes are GPT's R11 repro chains (r11-verification).
"""
import base64
import hashlib
import hmac
import json
import re
from pathlib import Path

import pytest

from turnkeep.storage import open_db, _json, _now, insert_record
from turnkeep.scopes import register_scope
from turnkeep.memory import remember
from turnkeep.runs import begin
from turnkeep.commits import finish
from turnkeep.errors import TurnkeepError
from turnkeep.erasure import (
    erase_record, residual_report,
)
from turnkeep.portability import export_scope

SRC = [{"id": "u:1", "type": "user_statement"}]
PIN = "4827"
HIGH = "9f3a7c2e5b1d8f4a6c0e2b7d9f1a3c5e7"  # high-entropy payload
LEGAL_CREATED = "2026-11-11T00:00:00+00:00"


def _db(tmp_path, name="t", scope="s"):
    db = open_db(tmp_path / f"{name}.sqlite3")
    register_scope(db, scope)
    return db


def _rev(db, rid):
    return db.execute(
        "SELECT revision FROM memories WHERE scope_id='s' AND id=?", (rid,)).fetchone()[0]


def _erase_meta(db, n=1):
    rows = db.execute(
        "SELECT metadata_json FROM events WHERE scope_id='s' AND event_type='erase' "
        "ORDER BY scope_epoch").fetchall()
    return json.loads(rows[-n][0])


def _all_erase_metas(db):
    return [json.loads(m[0]) for m in db.execute(
        "SELECT metadata_json FROM events WHERE scope_id='s' AND event_type='erase' "
        "ORDER BY scope_epoch").fetchall()]


def _export_rep(db, tmp_path, scope="s", name="exp"):
    out = tmp_path / name
    rep = export_scope(db, scope, out)
    return (rep, (out / "records.jsonl").read_text(encoding="utf-8"),
            (out / "events.jsonl").read_text(encoding="utf-8"))


_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?([+-]\d{2}:\d{2}|Z)?$")


def _recs_hold_pin(recs, pin=PIN):
    """True when `pin` occurs in a parsed records.jsonl line outside
    an ISO-8601 timestamp value. A raw-text `pin not in recs` check
    is flaky: a microsecond timestamp can contain a 4-digit PIN by
    coincidence (the known R7 flake, ~0.26% per timestamp)."""
    def walk(obj):
        if isinstance(obj, str):
            return pin in obj and not _TS_RE.match(obj)
        if isinstance(obj, dict):
            return any(walk(v) for v in obj.values())
        if isinstance(obj, list):
            return any(walk(v) for v in obj)
        return False
    return any(walk(json.loads(l)) for l in recs.splitlines() if l.strip())


def _enumerate_pin(db, scope="s"):
    """Offline enumeration of a 4-digit PIN from the live DB's stored
    material (GPT's A2 chain): EVERY key the DB holds (the legacy
    scope key + every per-record verification key) against the
    persisted needle hashes. Returns the recovered value, or None.
    P0-R12-01: after a terminal COMPLETE erase the per-record key is
    deleted, so no key in the DB verifies the historical hashes and
    this returns None even though the hashes are still in the
    append-only event — while the erase is PARTIAL the key is alive
    and the PIN is recovered."""
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


# ---------------------------------------------------------------- P0-R11-01
# Raw task-text residual loop.

def test_raw_task_begin_before_remember(tmp_path):
    # GPT's A1 chain, begin BEFORE the record exists, with idempotency.
    db = _db(tmp_path)
    begin(db, "s", "revoke key 4827 now", idempotency_key="begin-key-001")
    rec = remember(db, "s", "pin", PIN, source_refs=SRC, idempotency_key="a1-1")
    out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
    assert out["status"] == "PARTIAL"
    assert set(out["remaining"]) == {"runs", "idempotency"}
    meta = _erase_meta(db)
    assert meta["erase_status"] == "PARTIAL"
    # Both entries are flagged with the task-text location (never the
    # text itself), and the report names the column.
    flagged = [e for e in meta["remaining"] if e.get("task_text")]
    assert len(flagged) == 2
    locs = sorted(l for e in flagged for l in e["task_locations"])
    assert locs == ["idempotency.response_json.task", "runs.task"]
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is False
    residuals = rep["controlled"]["controlled_residuals"]
    assert len(residuals) == 2
    assert all(r.get("task_text") for r in residuals)
    assert sorted(l for r in residuals for l in r["task_locations"]) == locs
    assert all(r["record_id"] == rec["id"] for r in residuals)
    # A repair does not resolve it: the task text is never scrubbed.
    # The repair carries the first erase's task-text entries forward
    # (its own needles are receipt-derived and empty, so its scan
    # cannot see the task text — but it cannot clear it either, so it
    # reports PARTIAL, fail-closed, and the residual report keeps
    # naming it; the remedy stays export to a fresh DB).
    out2 = erase_record(db, "s", rec["id"], _rev(db, rec["id"]), "repair")
    assert out2["status"] == "PARTIAL"
    assert set(out2["remaining"]) == {"runs", "idempotency"}
    meta2 = _erase_meta(db)
    assert meta2["erase_status"] == "PARTIAL"
    assert meta2.get("repair") is True
    assert all(e.get("task_text") for e in meta2["remaining"])
    rep2 = residual_report(db, "s")
    assert rep2["controlled"]["live_tables_clean"] is False
    assert any(r.get("task_text") for r in rep2["controlled"]["controlled_residuals"])
    db.close()


def test_raw_task_begin_after_remember(tmp_path):
    # GPT's A1 chain, begin AFTER the record exists (the begin receipt
    # then also holds a dump of the record): same verdict.
    db = _db(tmp_path)
    rec = remember(db, "s", "pin", PIN, source_refs=SRC, idempotency_key="a1-1")
    begin(db, "s", "revoke key 4827 now", idempotency_key="begin-key-002")
    out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
    assert out["status"] == "PARTIAL"
    assert set(out["remaining"]) == {"runs", "idempotency"}
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is False
    assert any(r.get("task_text") for r in rep["controlled"]["controlled_residuals"])
    db.close()


def test_raw_task_no_idempotency(tmp_path):
    # Without an idempotency key there is no idempotency row — only the
    # runs row remains, and the report still says non-clean.
    db = _db(tmp_path)
    begin(db, "s", "revoke key 4827 now")  # no idempotency key
    rec = remember(db, "s", "pin", PIN, source_refs=SRC, idempotency_key="a1-1")
    out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
    assert out["status"] == "PARTIAL"
    assert set(out["remaining"]) == {"runs"}
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is False
    residuals = rep["controlled"]["controlled_residuals"]
    assert len(residuals) == 1
    assert residuals[0]["table"] == "runs"
    assert residuals[0].get("task_text") is True
    assert residuals[0]["task_locations"] == ["runs.task"]
    db.close()


def test_raw_task_finish_path(tmp_path):
    # begin -> finish writes a receipt (receipt_json non-NULL) before
    # the erase: the flag still comes from runs.task (a finish receipt
    # carries no top-level "task" field), and the report is non-clean.
    db = _db(tmp_path)
    run = begin(db, "s", "revoke key 4827 now", idempotency_key="a1f-1")
    finish(db, "s", run["run_id"], {"key": "out", "value": "result note"})
    rec = remember(db, "s", "pin", PIN, source_refs=SRC, idempotency_key="a1f-2")
    out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
    assert out["status"] == "PARTIAL"
    assert "runs" in out["remaining"]
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is False
    runs_res = [r for r in rep["controlled"]["controlled_residuals"]
                if r["table"] == "runs"]
    assert runs_res and runs_res[0].get("task_text") is True
    assert "runs.task" in runs_res[0]["task_locations"]
    db.close()


def test_encoded_task_still_retained(tmp_path):
    # §8 retained: an encoding in free task text has no value boundary,
    # so it is NOT scanned for — the erase is COMPLETE and clean.
    db = _db(tmp_path)
    b64 = base64.b64encode(PIN.encode()).decode()
    begin(db, "s", f"revoke key {b64} now", idempotency_key="a1e-1")
    rec = remember(db, "s", "pin", PIN, source_refs=SRC, idempotency_key="a1e-2")
    out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
    assert out["status"] == "COMPLETE"
    assert out["remaining"] == {}
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is True
    # Reversed form, same rule.
    db2 = _db(tmp_path, name="t2")
    begin(db2, "s", f"revoke key {PIN[::-1]} now", idempotency_key="a1e-3")
    rec2 = remember(db2, "s", "pin", PIN, source_refs=SRC, idempotency_key="a1e-4")
    out2 = erase_record(db2, "s", rec2["id"], rec2["revision"], "rotation")
    assert out2["status"] == "COMPLETE"
    assert residual_report(db2, "s")["controlled"]["live_tables_clean"] is True
    db.close()
    db2.close()


# ---------------------------------------------------------------- P0-R11-02
# needle_hashes must not stay enumerable in the live DB.

def test_complete_erase_persists_no_needle_hashes(tmp_path):
    # GPT's A2 chain: remember -> erase -> enumerate 0000-9999 from
    # the live DB's stored material. Nothing is recoverable, and the
    # COMPLETE event persists no needle hashes at all.
    db = _db(tmp_path)
    rec = remember(db, "s", "pin", PIN, source_refs=SRC, idempotency_key="a2-1")
    out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
    assert out["status"] == "COMPLETE"
    meta = _erase_meta(db)
    assert meta["needle_hashes"] == []
    # The export-exclusion map IS persisted in the event metadata
    # (record ids + field names only, nothing enumerable). The blocked
    # repair-reason labels are NOT in the event — a blocked label is a
    # fixed vocabulary word, and persisting it in an event would write
    # the payload itself into history when the payload is literally
    # that label — so they live in the live-DB-only erase_flags table.
    assert "blocked_repair_reasons" not in meta
    assert "export_excluded_identifiers" in meta
    row = db.execute(
        "SELECT blocked_repair_reasons FROM erase_flags "
        "WHERE scope_id=? AND record_id=?", ("s", rec["id"])).fetchone()
    assert row is not None and json.loads(row[0]) == []
    assert _enumerate_pin(db) is None
    # P0-R12-01: the per-record verification key is deleted at
    # terminal COMPLETE, so the historical hashes (still in the
    # append-only event) verify against nothing — and a fresh DB
    # never creates a scope key at all (the per-record key is the
    # only key, created lazily at the first erase of that record).
    assert db.execute(
        "SELECT COUNT(*) FROM record_erase_keys "
        "WHERE scope_id='s' AND record_id=?", (rec["id"],)).fetchone()[0] == 0
    assert db.execute(
        "SELECT COUNT(*) FROM scope_secrets WHERE scope_id='s'").fetchone()[0] == 0
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is True
    db.close()


def test_partial_erase_still_persists_hashes(tmp_path):
    # A PARTIAL erase leaves a live copy, so the hashes add no new
    # leak surface — and the repair / residual paths still need them.
    db = _db(tmp_path)
    r1 = remember(db, "s", "a", PIN, source_refs=SRC, idempotency_key="a2p-1")
    r2 = remember(db, "s", "b", PIN, source_refs=SRC, idempotency_key="a2p-2")
    out = erase_record(db, "s", r1["id"], r1["revision"], "rotation")
    assert out["status"] == "PARTIAL"
    meta = _erase_meta(db)
    assert meta["needle_hashes"], "a PARTIAL erase must persist needle hashes"
    assert len(meta["needle_hashes"]) == len(set(meta["needle_hashes"]))
    db.close()


def test_high_entropy_complete_erase_no_hashes(tmp_path):
    db = _db(tmp_path)
    rec = remember(db, "s", "k", HIGH, source_refs=SRC, idempotency_key="a2h-1")
    out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
    assert out["status"] == "COMPLETE"
    assert _erase_meta(db)["needle_hashes"] == []
    db.close()


def test_repair_reason_blocked_by_flags(tmp_path):
    # A payload literally "repair", first-erased COMPLETE with reason
    # "rotation": the repair must refuse reason "repair" — the flags
    # (not hashes, which a COMPLETE erase no longer persists) carry it.
    db = _db(tmp_path)
    rec = remember(db, "s", "k", "repair", source_refs=SRC, idempotency_key="a2r-1")
    out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
    assert out["status"] == "COMPLETE"
    meta = _erase_meta(db)
    assert meta["needle_hashes"] == []
    # The blocked label is in the live-DB-only erase_flags table, not
    # the event metadata (a blocked label is a fixed vocabulary word;
    # persisting it in an event would write the payload itself into
    # history when the payload is literally that label).
    assert "blocked_repair_reasons" not in meta
    row = db.execute(
        "SELECT blocked_repair_reasons FROM erase_flags "
        "WHERE scope_id=? AND record_id=?", ("s", rec["id"])).fetchone()
    assert row is not None and json.loads(row[0]) == ["repair"]
    with pytest.raises(TurnkeepError) as ei:
        erase_record(db, "s", rec["id"], _rev(db, rec["id"]), "repair")
    assert ei.value.code == "INVALID_INPUT"
    # "rotation" was not blocked: the repair with it succeeds.
    out2 = erase_record(db, "s", rec["id"], _rev(db, rec["id"]), "rotation")
    assert out2["status"] == "COMPLETE"
    db.close()


def test_repair_key_redaction_via_flags(tmp_path):
    # A key that CONTAINS the payload as a substring (len >= 4) is
    # redacted by _carries_needle at first-erase time, while the erase
    # itself is COMPLETE (the scan's identifier rule is whole-value
    # only, so the key is not a residual). The repair must redact it
    # too — via the persisted key_redacted flag, not hashes.
    db = _db(tmp_path)
    rec = remember(db, "s", "api-4827-key", PIN, source_refs=SRC,
                   idempotency_key="a2k-1")
    out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
    assert out["status"] == "COMPLETE"
    meta = _erase_meta(db)
    assert meta["needle_hashes"] == []
    assert meta.get("key_redacted") is True
    assert "key" not in meta
    out2 = erase_record(db, "s", rec["id"], _rev(db, rec["id"]), "repair")
    assert out2["status"] == "COMPLETE"
    meta2 = _erase_meta(db)
    assert meta2.get("key_redacted") is True
    assert "key" not in meta2
    assert PIN not in _json(meta2)
    # The residual report redacts the key too (flags fallback).
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is True
    erased = rep["controlled"]["erased_records"]
    assert any(e.get("id") == rec["id"] and e.get("key_redacted") for e in erased)
    assert PIN not in _json(rep)
    db.close()


def test_idempotency_key_residual_still_detected(tmp_path):
    # P0-R10-02c under the new scheme: another record's idempotency
    # row whose KEY is the payload still refuses PARTIAL and stays
    # non-clean (the first erase is PARTIAL, so hashes persist and the
    # judgement works exactly as before).
    db = _db(tmp_path)
    other = remember(db, "s", "other", "other note", source_refs=SRC,
                     idempotency_key=PIN)
    rec = remember(db, "s", "pin", PIN, source_refs=SRC, idempotency_key="a2c-1")
    out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
    assert out["status"] == "PARTIAL"
    assert "idempotency" in out["remaining"]
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is False
    assert any(r["table"] == "idempotency"
               for r in rep["controlled"]["controlled_residuals"])
    db.close()


def test_live_source_link_residual_still_detected(tmp_path):
    # P0-R10-02d under the new scheme: a live record citing a source
    # whose id IS the payload still refuses PARTIAL, stays non-clean,
    # and is excluded from export with its benign key visible.
    db = _db(tmp_path)
    rb = remember(db, "s", "b", "other note",
                  source_refs=[{"type": "user_statement", "id": PIN}])
    ra = remember(db, "s", "pin", PIN, source_refs=SRC)
    out = erase_record(db, "s", ra["id"], ra["revision"], "rotation")
    assert out["status"] == "PARTIAL"
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is False
    assert any(r["table"] == "sources"
               for r in rep["controlled"]["controlled_residuals"])
    rep_exp, recs, _ = _export_rep(db, tmp_path)
    assert "other note" not in recs and not _recs_hold_pin(recs)
    assert any(e.get("key") == "b" for e in rep_exp["records_excluded_secrets"])
    db.close()


def test_cross_record_key_export_excluded_after_complete(tmp_path):
    # A live record B whose KEY is A's erased payload: erasing A is
    # COMPLETE (keys are not scanned as columns), but export must
    # still exclude B — via the export-excluded-identifiers map the
    # COMPLETE erase persisted, since there are no hashes to check.
    # B is planted by direct write (no remember event, no receipt):
    # a remember-created B would leave its key in the remember event's
    # metadata / the receipt dump, and the scan would (correctly)
    # refuse PARTIAL instead.
    db = _db(tmp_path)
    insert_record(db, "s", PIN, _json("other note"), "fact",
                  [{"id": "u:1", "type": "user_statement"}], [], {},
                  state="active", cause_event_id="legacy-import",
                  record_id="rb")
    ra = remember(db, "s", "pin", PIN, source_refs=SRC, idempotency_key="a2x-2")
    out = erase_record(db, "s", ra["id"], ra["revision"], "rotation")
    assert out["status"] == "COMPLETE"
    meta = _erase_meta(db)
    assert meta["needle_hashes"] == []
    assert meta["export_excluded_identifiers"].get("rb") == ["key"]
    rep_exp, recs, _ = _export_rep(db, tmp_path)
    assert "other note" not in recs and not _recs_hold_pin(recs)
    entry = [e for e in rep_exp["records_excluded_secrets"]
             if e.get("key_redacted")]
    assert entry, "B's key is payload material and must be redacted"
    db.close()


def test_tombstone_export_excluded_after_complete(tmp_path):
    # R8 tombstone case under the new scheme: a record planted by
    # direct write (no remember event, no receipt) with key == value
    # erases COMPLETE — and the export must still exclude the
    # tombstone row, via the map, with the key redacted.
    db = _db(tmp_path)
    insert_record(db, "s", PIN, _json(PIN), "fact",
                  [{"id": PIN, "type": "user_statement"}], [], {},
                  state="active", cause_event_id="legacy-import",
                  record_id="r1")
    rev = db.execute(
        "SELECT revision FROM memories WHERE scope_id='s' AND id='r1'").fetchone()[0]
    out = erase_record(db, "s", "r1", rev, "rotation")
    assert out["status"] == "COMPLETE"
    meta = _erase_meta(db)
    assert meta["needle_hashes"] == []
    assert meta["export_excluded_identifiers"].get("r1") == ["key"]
    rep_exp, recs, _ = _export_rep(db, tmp_path)
    assert not _recs_hold_pin(recs)
    assert "r1" not in recs
    assert any(e.get("key_redacted") for e in rep_exp["records_excluded_secrets"])
    db.close()


# ---------------------------------------------------------------- P2-R11-03
# Structured audit-field matching (same-scope accumulation).

def test_chain_a_short_value_after_prior_erase(tmp_path):
    # GPT's A chain, exact repro: erase "first value", then "r" — both
    # COMPLETE, report clean.
    db = _db(tmp_path)
    r1 = remember(db, "s", "first", "first value", source_refs=SRC,
                  idempotency_key="a3a-1")
    o1 = erase_record(db, "s", r1["id"], r1["revision"], "rotation")
    assert o1["status"] == "COMPLETE"
    r2 = remember(db, "s", "target", "r", source_refs=SRC, idempotency_key="a3a-2")
    o2 = erase_record(db, "s", r2["id"], r2["revision"], "rotation")
    assert o2["status"] == "COMPLETE", f"remaining={o2.get('remaining')}"
    assert o2["remaining"] == {}
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is True
    db.close()


def test_chain_b_deterministic_hmac_202(tmp_path):
    # GPT's B chain, deterministic construction: find a payload whose
    # HMAC (under this record's per-record key, P0-R12-01) contains
    # "202", persist that hash with a PARTIAL erase (two records share
    # the payload, so the first erase stays PARTIAL), then erase
    # "202" — COMPLETE. The digest field must never be
    # substring-matched. (As written before the R13 rework this test
    # was vacuous: the seed erase was COMPLETE, so no "202" hash was
    # ever persisted — the rule it claimed to exercise never ran.)
    from turnkeep.erasure import _record_erase_key
    from turnkeep.storage import _id
    db = _db(tmp_path)
    rid = _id()
    key = _record_erase_key(db, "s", rid)
    target = None
    for i in range(2000):
        cand = f"ordinary value {i}"
        if "202" in hmac.new(key, cand.encode(), hashlib.sha256).hexdigest():
            target = cand
            break
    assert target is not None, "no HMAC containing '202' in 2000 candidates"
    r1 = remember(db, "s", "k1", target, source_refs=SRC,
                  idempotency_key="a3b-1", record_id=rid)
    r2 = remember(db, "s", "k2", target, source_refs=SRC, idempotency_key="a3b-2")
    o1 = erase_record(db, "s", r1["id"], r1["revision"], "rotation")
    assert o1["status"] == "PARTIAL", f"remaining={o1.get('remaining')}"
    # The PARTIAL event persisted a hash containing "202".
    assert any("202" in h for h in _erase_meta(db).get("needle_hashes", []))
    r3 = remember(db, "s", "k202", "202", source_refs=SRC, idempotency_key="a3b-3")
    o2 = erase_record(db, "s", r3["id"], r3["revision"], "rotation")
    assert o2["status"] == "COMPLETE", f"remaining={o2.get('remaining')}"
    assert o2["remaining"] == {}
    db.close()


def test_same_scope_consecutive_erases_then_202(tmp_path):
    # Same-scope accumulation loop (the R8 200x loop's same-scope
    # version): erase ordinary records consecutively in one scope,
    # then erase "202" — COMPLETE.
    db = _db(tmp_path)
    for i in range(30):
        r = remember(db, "s", f"k{i}", f"ordinary value {i}", source_refs=SRC,
                     idempotency_key=f"a3c-{i}")
        o = erase_record(db, "s", r["id"], r["revision"], "rotation")
        assert o["status"] == "COMPLETE"
    r = remember(db, "s", "k202", "202", source_refs=SRC, idempotency_key="a3c-202")
    o = erase_record(db, "s", r["id"], r["revision"], "rotation")
    assert o["status"] == "COMPLETE", f"remaining={o.get('remaining')}"
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is True
    db.close()


def test_one_char_payload_e_after_prior_erase(tmp_path):
    # Audit key-name collisions are fixed: "e" occurs in structural
    # key names ("key", "erased", "remaining", ...) and in the
    # tombstone marker, none of which is a copy of the payload.
    db = _db(tmp_path)
    r1 = remember(db, "s", "first", "first value", source_refs=SRC,
                  idempotency_key="a3d-1")
    assert erase_record(db, "s", r1["id"], r1["revision"], "rotation")["status"] == "COMPLETE"
    r2 = remember(db, "s", "target", "e", source_refs=SRC, idempotency_key="a3d-2")
    o2 = erase_record(db, "s", r2["id"], r2["revision"], "rotation")
    assert o2["status"] == "COMPLETE", f"remaining={o2.get('remaining')}"
    db.close()


def test_user_data_key_name_collision_still_partial(tmp_path):
    # P1-R4-06, kept: a 1-char payload colliding with a USER-DATA key
    # name still reports PARTIAL, fail-closed — the "value" subtree is
    # user data, and its key names are matched there. (Audit-structure
    # key names are the ones fixed above; the two are documented
    # separately in KNOWN_LIMITATIONS §8/§24.)
    db = _db(tmp_path)
    rb = remember(db, "s", "b", {"key": "note"}, source_refs=SRC,
                  idempotency_key="a3e-1")
    ra = remember(db, "s", "a", "e", source_refs=SRC, idempotency_key="a3e-2")
    out = erase_record(db, "s", ra["id"], ra["revision"], "rotation")
    assert out["status"] == "PARTIAL"
    assert "memories" in out["remaining"]
    db.close()


def test_reason_label_whole_value(tmp_path):
    # A reason label counts only on whole-value equality: erasing a
    # record with payload "rota" must not be refused by an earlier
    # erase event's reason "rotation".
    db = _db(tmp_path)
    r1 = remember(db, "s", "first", "first value", source_refs=SRC,
                  idempotency_key="a3f-1")
    assert erase_record(db, "s", r1["id"], r1["revision"], "rotation")["status"] == "COMPLETE"
    r2 = remember(db, "s", "target", "rota", source_refs=SRC, idempotency_key="a3f-2")
    o2 = erase_record(db, "s", r2["id"], r2["revision"], "cleanup")
    assert o2["status"] == "COMPLETE", f"remaining={o2.get('remaining')}"
    db.close()


def test_finish_receipt_committed_at_no_false_positive(tmp_path):
    # A finish receipt's committed_at is a legal timestamp at an audit
    # position: erasing "202" must not be refused by its own commit time.
    db = _db(tmp_path)
    run = begin(db, "s", "do the thing", idempotency_key="a3g-1")
    finish(db, "s", run["run_id"], {"key": "out", "value": "result note"})
    r = remember(db, "s", "k202", "202", source_refs=SRC, idempotency_key="a3g-2")
    o = erase_record(db, "s", r["id"], r["revision"], "rotation")
    assert o["status"] == "COMPLETE", f"remaining={o.get('remaining')}"
    db.close()


def test_202_2026_fresh_db_complete(tmp_path):
    # E-loop retained: fresh-DB "202" / "2026" still COMPLETE.
    for i, val in enumerate(["202", "2026"]):
        db = _db(tmp_path, name=f"e{i}")
        r = remember(db, "s", "k", val, source_refs=SRC,
                     idempotency_key=f"a3h-{i}")
        o = erase_record(db, "s", r["id"], r["revision"], "rotation")
        assert o["status"] == "COMPLETE", f"{val}: remaining={o.get('remaining')}"
        db.close()
    # And a small random loop on fresh DBs (the R8 200x loop's shape,
    # shortened: the same-scope loop above is the accumulation check).
    for i in range(10):
        db = _db(tmp_path, name=f"r{i}")
        r = remember(db, "s", "k", "202", source_refs=SRC,
                     idempotency_key=f"a3h-r{i}")
        o = erase_record(db, "s", r["id"], r["revision"], "rotation")
        assert o["status"] == "COMPLETE"
        db.close()
