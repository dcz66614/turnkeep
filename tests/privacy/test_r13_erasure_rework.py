"""R13 rework regression tests: P0-R12-01 (resolved PARTIAL erasures
must leave no enumerable historical hashes — per-record verification
keys), P1-R12-02 (decode-equality detection for fixed audit labels),
erase_flags 2-bit residual (retained, documented).

Fix rules (verification report §3):
  1. Per-record verification keys: every record has its own 32-byte
     key in the live-DB-only record_erase_keys table (schema v5),
     created lazily at the record's first erase, never exported.
     Needle hashes are HMAC-SHA256 under that record's key. When an
     erase (first or repair) reaches terminal COMPLETE, the record's
     key is deleted in the same transaction (after the event is
     published and every hash verification that erase needed), so the
     historical hashes in append-only history verify against nothing.
     A fresh DB never creates a scope key. Verification tries
     [per-record key, legacy scope key] so pre-fix DBs' scope-keyed
     hashes still verify; a deleted per-record key verifies as NO
     match, never an error.
  2. Decode-equality for controlled labels: a label that the needle
     decodes to IS a copy (the payload is an encoding of the label).
     The controlled-label branch of the walk counts whole-value
     equality OR decode equality (>= 3-char floor); the candidate
     filter admits rows holding a decoded form; the reason write gate
     rejects a reason that IS a decoded form of a needle; the
     erase_status label the erase is about to write downgrades
     COMPLETE -> PARTIAL with a synthetic status_label entry (never
     resolves); the record's OWN kind is scanned (kind_label entry,
     never resolves) and the export excludes a tombstone row whose
     kind encodes to an erased payload.
  3. erase_flags 2-bit residual RETAINED: the blocked repair-reason
     labels still persist in the live-DB-only table; the security
     promise is corrected (residual_report's live_tables_clean_scope
     states it does not cover the 2-bit residual).
"""
import base64
import hashlib
import hmac
import json
import re

import pytest

from turnkeep.storage import open_db, _json, _id
from turnkeep.scopes import register_scope
from turnkeep.memory import remember
from turnkeep.runs import begin
from turnkeep.errors import TurnkeepError
from turnkeep.erasure import (
    erase_record, residual_report, _record_erase_key,
    _record_verification_keys, _stored_needle_hashes,
)
from turnkeep.portability import export_scope

SRC = [{"id": "u:1", "type": "user_statement"}]
PIN = "4827"
HIGH = "9f3a7c2e5b1d8f4a6c0e2b7d9f1a3c5e7"  # high-entropy payload


def _db(tmp_path, name="t", scope="s"):
    db = open_db(tmp_path / f"{name}.sqlite3")
    register_scope(db, scope)
    return db


def _erase_meta(db, n=1):
    rows = db.execute(
        "SELECT metadata_json FROM events WHERE scope_id='s' AND event_type='erase' "
        "ORDER BY scope_epoch").fetchall()
    return json.loads(rows[-n][0])


def _all_erase_metas(db):
    return [json.loads(m[0]) for m in db.execute(
        "SELECT metadata_json FROM events WHERE scope_id='s' AND event_type='erase' "
        "ORDER BY scope_epoch").fetchall()]


def _enumerate_pin(db, scope="s"):
    """Offline enumeration of a 4-digit PIN from the live DB's stored
    material: EVERY key the DB holds (legacy scope key + every
    per-record verification key) against the persisted needle hashes.
    Returns the recovered value, or None."""
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
    """True when a specific payload is enumerable from the live DB's
    stored material (any key the DB holds against the persisted
    needle hashes). Used when another record's payload is still
    enumerable by design (PARTIAL), so a whole-DB enumeration would
    find that one instead."""
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
    return db.execute(
        "SELECT record_id FROM record_erase_keys WHERE scope_id=?", (scope,)).fetchall()


def _rev(b64):
    return base64.b64encode(b64).decode()


def _hex(b):
    return b.hex() if isinstance(b, bytes) else b.encode().hex()


def _rot13(s):
    return s.translate(str.maketrans(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ",
        "nopqrstuvwxyzabcdefghijklmNOPQRSTUVWXYZABCDEFGHIJKLM"))


# ---------------------------------------------------------------- P0-R12-01
# Resolved PARTIAL erasures leave no enumerable historical hashes.

def test_a1_chain_repair_complete_not_enumerable(tmp_path):
    # GPT's A1 chain: two records, same low-entropy value; erase A
    # PARTIAL, erase B COMPLETE, repair A COMPLETE; enumerate
    # 0000-9999 — finds nothing. The first PARTIAL event's hashes
    # are still in append-only history, but both records' keys are
    # retired, so they verify against nothing.
    db = _db(tmp_path)
    ra = remember(db, "s", "pin", PIN, source_refs=SRC, idempotency_key="r13-a1-1")
    rb = remember(db, "s", "code", PIN, source_refs=SRC, idempotency_key="r13-a1-2")
    o1 = erase_record(db, "s", ra["id"], ra["revision"], "rotation")
    assert o1["status"] == "PARTIAL"
    # While PARTIAL the PIN IS enumerable (the key is alive) — the
    # fix retires the key only at terminal COMPLETE.
    assert _enumerate_pin(db) == PIN
    o2 = erase_record(db, "s", rb["id"], rb["revision"], "rotation")
    assert o2["status"] == "COMPLETE"
    # B's key is retired at its COMPLETE, but A's hashes are still
    # verifiable under A's key (A is still PARTIAL) — so the PIN is
    # still enumerable until A is repaired.
    assert _enumerate_pin(db) == PIN
    assert [r[0] for r in _key_rows(db)] == [ra["id"]]
    o3 = erase_record(db, "s", ra["id"], o1["revision"], "repair")
    assert o3["status"] == "COMPLETE"
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is True
    assert rep["controlled"]["controlled_residuals"] == []
    # Both keys retired; the historical hashes verify against nothing.
    assert _key_rows(db) == []
    assert _enumerate_pin(db) is None
    # The first PARTIAL event's hashes are still in history (append-only).
    assert any(m.get("needle_hashes") for m in _all_erase_metas(db))
    db.close()


def test_cross_payload_complete_retires_own_key(tmp_path):
    # A reaches COMPLETE while B is still PARTIAL for a DIFFERENT
    # payload: A's hashes must retire (its key deleted) even though
    # B's key is alive. Per-record keys, not a shared scope key.
    db = _db(tmp_path)
    ra = remember(db, "s", "pin", PIN, source_refs=SRC, idempotency_key="r13-x-1")
    begin(db, "s", "revoke key 9999 now", idempotency_key="r13-x-begin")
    rb = remember(db, "s", "code", "9999", source_refs=SRC, idempotency_key="r13-x-2")
    o1 = erase_record(db, "s", ra["id"], ra["revision"], "rotation")
    assert o1["status"] == "COMPLETE"
    o2 = erase_record(db, "s", rb["id"], rb["revision"], "rotation")
    assert o2["status"] == "PARTIAL"  # the run task still holds 9999
    assert [r[0] for r in _key_rows(db)] == [rb["id"]]
    # A's payload is not enumerable (its key is gone); B's is
    # (its key is alive, by design — B is PARTIAL).
    assert _is_enumerable(db, PIN) is False
    assert _is_enumerable(db, "9999") is True
    db.close()


def test_single_record_partial_to_repair(tmp_path):
    # Single-record PARTIAL -> repair chain with no second record:
    # the repair still works (its reason vetting + identifier cleanup
    # run on the stored hashes while the key is alive), and the key
    # retires at the repair's COMPLETE.
    db = _db(tmp_path)
    rec = remember(db, "s", "pin", PIN, source_refs=SRC, idempotency_key="r13-s-1")
    begin(db, "s", "revoke key 4827 now", idempotency_key="r13-s-begin")
    o1 = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
    assert o1["status"] == "PARTIAL"
    assert _enumerate_pin(db) == PIN
    # The run task still holds the payload, so the repair stays PARTIAL
    # and the key stays alive (the residual is honest, not washed).
    o2 = erase_record(db, "s", rec["id"], o1["revision"], "repair")
    assert o2["status"] == "PARTIAL"
    assert _enumerate_pin(db) == PIN
    assert [r[0] for r in _key_rows(db)] == [rec["id"]]
    db.close()


def test_repair_reason_vetting_under_new_scheme(tmp_path):
    # Repair reason vetting still works under per-record keys: a
    # repair with a reason that IS the payload is rejected, and a
    # blocked label is recorded in the flags (not the event).
    db = _db(tmp_path)
    ra = remember(db, "s", "pin", PIN, source_refs=SRC, idempotency_key="r13-v-1")
    rb = remember(db, "s", "code", PIN, source_refs=SRC, idempotency_key="r13-v-2")
    o1 = erase_record(db, "s", ra["id"], ra["revision"], "rotation")
    assert o1["status"] == "PARTIAL"
    # A repair whose reason is the payload itself is refused.
    with pytest.raises(TurnkeepError) as ei:
        erase_record(db, "s", ra["id"], o1["revision"], PIN)
    assert ei.value.code == "INVALID_INPUT"
    # A repair with the blocked label "repair" as the reason is
    # accepted (the label is a fixed vocabulary word) and the flag
    # records it in the live-DB-only table.
    o2 = erase_record(db, "s", rb["id"], rb["revision"], "rotation")
    assert o2["status"] == "COMPLETE"
    o3 = erase_record(db, "s", ra["id"], o1["revision"], "repair")
    assert o3["status"] == "COMPLETE"
    assert _enumerate_pin(db) is None
    db.close()


def test_r10_02c_idempotency_key_under_new_scheme(tmp_path):
    # P0-R10-02c under per-record keys: B's operation used the payload
    # as its idempotency key. Erasing A refuses (PARTIAL) and names B;
    # erasing B afterwards does NOT resolve it — the key is A's
    # payload, not B's, so B's erasure leaves the key in place. (This
    # is the regression for the key-attribution fix: the residual must
    # be verified under A's key, not B's — B's key is deleted at its
    # COMPLETE, and using it would silently resolve the residual.)
    db = _db(tmp_path)
    rb = remember(db, "s", "b", "other note", source_refs=SRC, idempotency_key=PIN)
    ra = remember(db, "s", "pin", PIN, source_refs=SRC, idempotency_key="r13-c-1")
    out = erase_record(db, "s", ra["id"], ra["revision"], "rotation")
    assert out["status"] == "PARTIAL"
    outb = erase_record(db, "s", rb["id"], rb["revision"], "rotation")
    assert outb["status"] == "COMPLETE"
    row = db.execute(
        "SELECT idempotency_key FROM idempotency WHERE scope_id='s' AND idempotency_key=?",
        (PIN,)).fetchone()
    assert row is not None
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is False
    assert any(r["table"] == "idempotency"
               for r in rep["controlled"]["controlled_residuals"])
    db.close()


def test_r10_02d_live_source_link_under_new_scheme(tmp_path):
    # P0-R10-02d under per-record keys: live record B cites a source
    # whose id IS A's payload. Erasing A refuses (PARTIAL) naming B;
    # erasing B afterwards does NOT resolve it — the link id is A's
    # payload, not B's.
    db = _db(tmp_path)
    rb = remember(db, "s", "b", "other note",
                  source_refs=[{"id": PIN, "type": "user_statement"}],
                  idempotency_key="r13-d-1")
    ra = remember(db, "s", "pin", PIN, source_refs=SRC, idempotency_key="r13-d-2")
    out = erase_record(db, "s", ra["id"], ra["revision"], "rotation")
    assert out["status"] == "PARTIAL"
    outb = erase_record(db, "s", rb["id"], rb["revision"], "rotation")
    assert outb["status"] == "COMPLETE"
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is False
    assert any(r["table"] == "sources"
               for r in rep["controlled"]["controlled_residuals"])
    db.close()


def test_202_2026_still_complete(tmp_path):
    # "202" / "2026" still COMPLETE (digest fields are never
    # substring-matched; the per-record key change does not alter it).
    db = _db(tmp_path)
    for i in range(10):
        r = remember(db, "s", f"k{i}", "202", source_refs=SRC,
                     idempotency_key=f"r13-e-{i}")
        o = erase_record(db, "s", r["id"], r["revision"], "rotation")
        assert o["status"] == "COMPLETE", f"remaining={o.get('remaining')}"
    for i in range(10):
        r = remember(db, "s", f"y{i}", "2026", source_refs=SRC,
                     idempotency_key=f"r13-e-y{i}")
        o = erase_record(db, "s", r["id"], r["revision"], "rotation")
        assert o["status"] == "COMPLETE", f"remaining={o.get('remaining')}"
    db.close()


def test_high_entropy_control(tmp_path):
    # High-entropy payload: COMPLETE, no hashes, no key rows left.
    db = _db(tmp_path)
    rec = remember(db, "s", "k", HIGH, source_refs=SRC, idempotency_key="r13-h-1")
    out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
    assert out["status"] == "COMPLETE"
    assert _erase_meta(db).get("needle_hashes") == []
    assert _key_rows(db) == []
    db.close()


# ---------------------------------------------------------------- P1-R12-02
# Decode-equality detection for fixed audit labels.

def test_erase_status_label_chains(tmp_path):
    # GPT's erase_status chains: a payload that is an encoding of
    # "COMPLETE" (Base64 / reversed / hex / rot13) must erase PARTIAL
    # — the event's erase_status label IS a decoded form of the
    # payload — and the residual names the status label.
    for i, payload in enumerate((_rev(b"COMPLETE"), "ETELPMOC",
                                 _hex(b"COMPLETE"), _rot13("COMPLETE"))):
        db = _db(tmp_path, name=f"st-{i}")
        rec = remember(db, "s", "pin", payload, source_refs=SRC,
                       idempotency_key=f"r13-st-{i}")
        out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
        assert out["status"] == "PARTIAL", f"payload={payload!r}"
        meta = _erase_meta(db)
        assert meta["erase_status"] == "PARTIAL"
        rep = residual_report(db, "s")
        assert rep["controlled"]["live_tables_clean"] is False
        assert any(e.get("status_label")
                   and "events.erase_status" in e.get("status_locations", [])
                   for e in rep["controlled"]["controlled_residuals"])
        db.close()


def test_kind_label_chains(tmp_path):
    # GPT's kind chains: a payload that is an encoding of "fact"
    # (Base64 / reversed / hex / rot13) must erase PARTIAL — the
    # record's own kind IS a decoded form of the payload — and the
    # residual names the kind label. The export must exclude the
    # tombstone row (the fresh-DB remedy removes it).
    for i, payload in enumerate((_rev(b"fact"), "tcaf", _hex(b"fact"), _rot13("fact"))):
        db = _db(tmp_path, name=f"kd-{i}")
        rec = remember(db, "s", "pin", payload, source_refs=SRC, kind="fact",
                       idempotency_key=f"r13-kd-{i}")
        out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
        assert out["status"] == "PARTIAL", f"payload={payload!r}"
        rep = residual_report(db, "s")
        assert rep["controlled"]["live_tables_clean"] is False
        assert any(e.get("kind_label")
                   and "memories.kind" in e.get("kind_locations", [])
                   for e in rep["controlled"]["controlled_residuals"])
        out_dir = tmp_path / f"kd-exp-{i}"
        export_scope(db, "s", out_dir)
        recs = (out_dir / "records.jsonl").read_text(encoding="utf-8")
        assert rec["id"] not in recs
        assert payload not in recs
        db.close()


def test_partial_label_chain_needs_live_copy(tmp_path):
    # PARTIAL label chain: a payload encoding "PARTIAL" erases
    # PARTIAL only when the erase is already PARTIAL (a live copy
    # exists) — the status label the erase then writes IS "PARTIAL",
    # which decodes to the payload, so a status_label entry is
    # recorded and the residual never resolves. With no live copy the
    # erase writes "COMPLETE" (not a decoded form of the payload) and
    # is COMPLETE — the label only exists once the erase is PARTIAL.
    db = _db(tmp_path)
    payload = _rev(b"PARTIAL")
    ra = remember(db, "s", "pin", payload, source_refs=SRC, idempotency_key="r13-pl-1")
    remember(db, "s", "code", payload, source_refs=SRC, idempotency_key="r13-pl-2")
    out = erase_record(db, "s", ra["id"], ra["revision"], "rotation")
    assert out["status"] == "PARTIAL"
    rep = residual_report(db, "s")
    assert any(e.get("status_label")
               and "events.erase_status" in e.get("status_locations", [])
               for e in rep["controlled"]["controlled_residuals"])
    db.close()


def test_all_kind_labels(tmp_path):
    # Every kind label's encoded form is detected: for each kind, a
    # record of that kind whose payload is Base64(kind) erases
    # PARTIAL (the record's own kind IS a decoded form of the
    # payload). The kind is the label the erase carries, so this is
    # the kind field's encoding regression.
    for i, kind in enumerate(["requirement", "fact", "decision",
                              "attempt", "procedure", "handoff"]):
        db = _db(tmp_path, name=f"kl-{i}")
        rec = remember(db, "s", "pin", _rev(kind.encode()), source_refs=SRC,
                       kind=kind, idempotency_key=f"r13-kl-{i}")
        out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
        assert out["status"] == "PARTIAL", f"kind={kind!r}"
        rep = residual_report(db, "s")
        assert any(e.get("kind_label")
                   for e in rep["controlled"]["controlled_residuals"])
        db.close()


def test_all_erase_reason_labels(tmp_path):
    # Every erase-reason label's encoded form is rejected at the
    # write gate: a payload encoding an erase reason (rotation /
    # cleanup / repair) erased with that reason is INVALID_INPUT, not
    # written into the event log. (The "rotation label chain" is
    # handled here, not by PARTIAL — the gate refuses to write the
    # label at all.)
    for i, reason in enumerate(["rotation", "cleanup", "repair"]):
        db = _db(tmp_path, name=f"rl-{i}")
        rec = remember(db, "s", "pin", _rev(reason.encode()), source_refs=SRC,
                       idempotency_key=f"r13-rl-{i}")
        with pytest.raises(TurnkeepError) as ei:
            erase_record(db, "s", rec["id"], rec["revision"], reason)
        assert ei.value.code == "INVALID_INPUT", f"reason={reason!r}"
        db.close()


def test_short_payload_boundary_still_complete(tmp_path):
    # "r" and Base64("r") == "cg==" still COMPLETE — no label decodes
    # from a 1-char payload (the >= 3-char floor), so the "r" false
    # positive is not reintroduced.
    for payload in ("r", "cg=="):
        db = _db(tmp_path, name=f"bd-{payload}")
        rec = remember(db, "s", "pin", payload, source_refs=SRC,
                       idempotency_key=f"r13-bd-{payload}")
        out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
        assert out["status"] == "COMPLETE", f"payload={payload!r}"
        rep = residual_report(db, "s")
        assert rep["controlled"]["live_tables_clean"] is True
        db.close()


def test_reason_is_decoded_form_rejected(tmp_path):
    # A reason that IS a decoded form of the needle is rejected at
    # the write gate: payload Base64("rotation") + reason "rotation"
    # is INVALID_INPUT, not written into the event log.
    db = _db(tmp_path)
    rec = remember(db, "s", "pin", _rev(b"rotation"), source_refs=SRC,
                   idempotency_key="r13-rs-1")
    with pytest.raises(TurnkeepError) as ei:
        erase_record(db, "s", rec["id"], rec["revision"], "rotation")
    assert ei.value.code == "INVALID_INPUT"
    db.close()


def test_repair_rederives_kind_and_status(tmp_path):
    # The repair path re-derives kind/status matches from the stored
    # hashes: a kind-label PARTIAL first erase stays PARTIAL after
    # repair (the label cannot be rewritten), and the key stays alive
    # while the record is PARTIAL.
    db = _db(tmp_path)
    rec = remember(db, "s", "pin", _rev(b"fact"), source_refs=SRC, kind="fact",
                   idempotency_key="r13-rr-1")
    o1 = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
    assert o1["status"] == "PARTIAL"
    o2 = erase_record(db, "s", rec["id"], o1["revision"], "repair")
    assert o2["status"] == "PARTIAL"
    rep = residual_report(db, "s")
    assert any(e.get("kind_label")
               for e in rep["controlled"]["controlled_residuals"])
    assert [r[0] for r in _key_rows(db)] == [rec["id"]]
    db.close()


# ---------------------------------------------------------------- erase_flags
# 2-bit residual — RETAINED, documented.

def test_erase_flags_two_bit_retained(tmp_path):
    # Payload "repair" with reason "rotation": COMPLETE, the blocked
    # repair-reason label is recorded in the live-DB-only erase_flags
    # table, and the report is clean — with the scope text stating
    # it does not cover the disclosed 2-bit residual.
    db = _db(tmp_path)
    rec = remember(db, "s", "pin", "repair", source_refs=SRC,
                   idempotency_key="r13-ef-1")
    out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
    assert out["status"] == "COMPLETE"
    row = db.execute(
        "SELECT blocked_repair_reasons FROM erase_flags "
        "WHERE scope_id=? AND record_id=?", ("s", rec["id"])).fetchone()
    assert row is not None and json.loads(row[0]) == ["repair"]
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is True
    assert "2-bit" in rep["controlled"]["live_tables_clean_scope"]
    db.close()


# ---------------------------------------------------------------- CLI / export

def test_cli_export_clean_after_label_partial(tmp_path):
    # CLI/export spot check: after a kind-label PARTIAL erase via the
    # CLI, the CLI's JSON output reports PARTIAL without echoing the
    # payload, and the export excludes the tombstone row and carries
    # no payload material (the encoded payload or the label it
    # decodes to).
    import os
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    env = {"PYTHONPATH": str(root / "src"), "PATH": "/usr/bin:/bin:/usr/local/bin",
           "HOME": "/tmp", "TMPDIR": "/tmp"}
    dbpath = tmp_path / "ce.sqlite3"
    payload = _rev(b"fact")
    r = subprocess.run(
        [sys.executable, "-m", "turnkeep", "--db", str(dbpath), "--json",
         "init", "--scope", "s"],
        capture_output=True, text=True, timeout=60, env=env, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    r = subprocess.run(
        [sys.executable, "-m", "turnkeep", "--db", str(dbpath), "--json",
         "remember", "--scope", "s", "--key", "pin", "--value", payload,
         "--kind", "fact", "--source", "u:1:user_statement"],
        capture_output=True, text=True, timeout=60, env=env, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    rec = json.loads(r.stdout)
    r = subprocess.run(
        [sys.executable, "-m", "turnkeep", "--db", str(dbpath), "--json",
         "erase", "--scope", "s", "--id", rec["id"],
         "--expected-revision", str(rec["revision"]), "--reason", "rotation"],
        capture_output=True, text=True, timeout=60, env=env, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["status"] == "PARTIAL"
    assert payload not in r.stdout
    db = open_db(dbpath)
    out_dir = tmp_path / "ce-exp"
    rep = export_scope(db, "s", out_dir)
    assert rep["records_exported"] == 0
    text = (out_dir / "records.jsonl").read_text(encoding="utf-8")
    assert payload not in text
    db.close()
