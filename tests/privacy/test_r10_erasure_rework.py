"""R10 rework regression tests: P0-R10-01 (idempotency request_digest
survives erasure and is enumerable offline), P0-R10-02 (residual scan
misses four identifier surfaces), P1-R10-03 (erase return dict re-leaks
the key when key == value).

Fix rules (verification report §3):
  1. Request digests are NEUTRALIZED at erase time (marker "erased",
     never HMAC — the HMAC key lives in the same DB, so a DB holder
     enumerates an HMAC just as easily, repro A1b.2). A row whose own
     idempotency_key IS the payload is DELETED instead (its key is part
     of the row's primary key and cannot be scrubbed). A replay of an
     erased operation raises IDEMPOTENCY_MISMATCH; the replay promise
     ends at erasure.
  2. The residual scan covers four identifier columns on whole-value
     equality only (verbatim or a known decoding, >= 3 chars):
     events.affected_ids_json, events.actor_binding,
     idempotency.idempotency_key, sources.id. Append-only events are
     never rewritten — the honest outcome is PARTIAL naming the event.
     A live record citing a source whose id IS the payload stays (R8
     design decision); only the clean-report claim is fixed.
  3. Erase return dicts omit a payload-carrying key and flag
     "key_redacted": True, by the same rule as the erase event's
     metadata key. Benign keys stay visible.

Setup recipes are GPT's R10 repro chains (repro_r10_verify.py).
"""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from turnkeep.storage import open_db, _json, _now, _digest
from turnkeep.scopes import register_scope
from turnkeep.memory import remember, list_conflicts
from turnkeep.corrections import correct, retract, resolve_conflict
from turnkeep.runs import begin
from turnkeep.commits import finish
from turnkeep.retention import set_expiry
from turnkeep.errors import TurnkeepError
from turnkeep.erasure import (
    erase_record, residual_report, TOMBSTONE,
)
from turnkeep.idempotency import request_digest, NEUTRALIZED_DIGEST
from turnkeep.portability import export_scope

SRC = [{"id": "u:1", "type": "user_statement"}]
PIN = "4827"
HIGH = "9f3a7c2e5b1d8f4a6c0e2b7d9f1a3c5e7"  # high-entropy payload
LEGAL_TS = "2026-11-12T22:00:00+00:00"
LEGAL_CREATED = "2026-11-11T00:00:00+00:00"
ROOT = Path(__file__).resolve().parents[2]
ENV = {"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin:/usr/local/bin",
       "HOME": "/tmp", "TMPDIR": "/tmp"}


def _db(tmp_path, name="t", scope="s"):
    db = open_db(tmp_path / f"{name}.sqlite3")
    register_scope(db, scope)
    return db


def _req_dict(key, value, sources=None):
    # The exact request remember() digests (verification repro A1.4):
    # {"key","value","kind","sources","parent_ids","expected_revision",
    # "replaces_ids"}.
    return {"key": key, "value": value, "kind": "fact",
            "sources": sources if sources is not None else SRC,
            "parent_ids": [], "expected_revision": None, "replaces_ids": []}


def _export_rep(db, tmp_path, scope="s", name="exp"):
    out = tmp_path / name
    rep = export_scope(db, scope, out)
    return (rep, (out / "records.jsonl").read_text(encoding="utf-8"),
            (out / "events.jsonl").read_text(encoding="utf-8"))


def _rev(db, rid):
    return db.execute("SELECT revision FROM memories WHERE id=?", (rid,)).fetchone()[0]


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
    # GPT's A1 chain: enumerate 0000-9999 against the stored digest.
    return [f"{i:04d}" for i in range(10000)
            if request_digest("remember", "s", "local",
                              _req_dict("pin", f"{i:04d}")) == digest]


# ---------------------------------------------------------------- P0-R10-01

def test_a1_chain_digest_enumeration_finds_nothing(tmp_path):
    # GPT's A1 chain, post-fix: remember -> erase -> the stored
    # digest is the neutralized marker, so enumeration finds nothing.
    db = _db(tmp_path)
    rec = remember(db, "s", "pin", PIN, source_refs=SRC, idempotency_key="idem-4827")
    before = _idem_row(db, "idem-4827")[1]
    assert before == request_digest("remember", "s", "local", _req_dict("pin", PIN))
    out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
    assert out["status"] == "COMPLETE"
    assert out["neutralized_idempotency"] == 1
    assert out["deleted_idempotency"] == 0
    assert out["neutralized_source_digests"] == 0
    row = _idem_row(db, "idem-4827")
    assert row is not None
    assert row[1] == NEUTRALIZED_DIGEST == "erased"
    assert row[1] != before
    assert _enumerate_pin(row[1]) == []
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is True
    _, recs, evs = _export_rep(db, tmp_path)
    assert PIN not in recs and PIN not in evs
    db.close()


def test_post_erase_replay_raises_mismatch_remember(tmp_path):
    # The replay promise ends at erasure (1c): a replay of the erased
    # operation raises IDEMPOTENCY_MISMATCH instead of returning the
    # stored response. While live, the same replay still works.
    db = _db(tmp_path)
    rec = remember(db, "s", "pin", PIN, source_refs=SRC, idempotency_key="mem-live")
    replay = remember(db, "s", "pin", PIN, source_refs=SRC, idempotency_key="mem-live")
    assert replay["id"] == rec["id"]
    out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
    assert out["status"] == "COMPLETE"
    with pytest.raises(TurnkeepError) as ei:
        remember(db, "s", "pin", PIN, source_refs=SRC, idempotency_key="mem-live")
    assert ei.value.code == "IDEMPOTENCY_MISMATCH"
    db.close()


def test_correct_digest_neutralized_and_replay_mismatch(tmp_path):
    # correct's request carries the new value, so its digest is
    # enumerable the same way; erasing the corrected record
    # neutralizes it (setup records carry no idempotency key, so the
    # count isolates the correct row).
    db = _db(tmp_path)
    old = remember(db, "s", "kA", "old note", source_refs=SRC)
    new = correct(db, "s", old["id"], old["revision"], PIN, source_refs=SRC,
                  idempotency_key="corr-1")
    out = erase_record(db, "s", new["id"], new["revision"], "rotation")
    assert out["status"] == "COMPLETE"
    assert out["neutralized_idempotency"] == 1
    assert _idem_row(db, "corr-1")[1] == NEUTRALIZED_DIGEST
    with pytest.raises(TurnkeepError) as ei:
        correct(db, "s", old["id"], old["revision"], PIN, source_refs=SRC,
                idempotency_key="corr-1")
    assert ei.value.code == "IDEMPOTENCY_MISMATCH"
    db.close()


def test_retract_digest_neutralized_and_replay_mismatch(tmp_path):
    # retract's request carries no value — but the row still belongs
    # to the erased record (uniform rule over all six operations),
    # so its digest is neutralized too.
    db = _db(tmp_path)
    rec = remember(db, "s", "kA", PIN, source_refs=SRC)
    rev = rec["revision"]
    retract(db, "s", rec["id"], rev, "withdrawn", idempotency_key="ret-1")
    out = erase_record(db, "s", rec["id"], _rev(db, rec["id"]), "rotation")
    assert out["status"] == "COMPLETE"
    assert out["neutralized_idempotency"] == 1
    assert _idem_row(db, "ret-1")[1] == NEUTRALIZED_DIGEST
    # Same request as the original retract (same expected_revision):
    # the idempotency check runs first, so the mismatch surfaces
    # before the already-retracted / already-erased state is reached.
    with pytest.raises(TurnkeepError) as ei:
        retract(db, "s", rec["id"], rev, "withdrawn", idempotency_key="ret-1")
    assert ei.value.code == "IDEMPOTENCY_MISMATCH"
    db.close()


def test_resolve_conflict_digest_neutralized_and_replay_mismatch(tmp_path):
    # The resolved record holds the payload, and a second live record
    # still holds a copy — so the erase is PARTIAL — but the digest
    # neutralization already ran (step 3, before the 3b scan).
    db = _db(tmp_path)
    remember(db, "s", "ck", PIN, source_refs=SRC)
    remember(db, "s", "ck", "other note", source_refs=SRC)
    group = list_conflicts(db, "s", "ck")
    cands = [{"id": r["id"], "revision": r["revision"]} for r in group]
    res = resolve_conflict(db, "s", "ck", cands, PIN, source_refs=SRC,
                           idempotency_key="res-1")
    out = erase_record(db, "s", res["id"], res["revision"], "rotation")
    assert out["status"] == "PARTIAL"
    assert out["neutralized_idempotency"] == 1
    assert _idem_row(db, "res-1")[1] == NEUTRALIZED_DIGEST
    with pytest.raises(TurnkeepError) as ei:
        resolve_conflict(db, "s", "ck", cands, PIN, source_refs=SRC,
                         idempotency_key="res-1")
    assert ei.value.code == "IDEMPOTENCY_MISMATCH"
    db.close()


def test_begin_digest_neutralized_and_replay_mismatch(tmp_path):
    # begin's request covers only the task — no value — but its
    # receipt holds the erased record's dump, so the row belongs to
    # the erased record and its digest is neutralized.
    db = _db(tmp_path)
    rec = remember(db, "s", "kA", PIN, source_refs=SRC)
    run = begin(db, "s", task="check kA", idempotency_key="run-1")
    assert any(r["id"] == rec["id"] for r in run["records"])
    out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
    assert out["status"] == "COMPLETE"
    assert out["neutralized_idempotency"] == 1
    assert _idem_row(db, "run-1")[1] == NEUTRALIZED_DIGEST
    with pytest.raises(TurnkeepError) as ei:
        begin(db, "s", task="check kA", idempotency_key="run-1")
    assert ei.value.code == "IDEMPOTENCY_MISMATCH"
    db.close()


def test_finish_digest_neutralized_and_replay_mismatch(tmp_path):
    # finish carries the digest three times: the idempotency row, the
    # runs row's request_digest column, and the receipt_json field.
    # All three are neutralized for the erased record's receipt.
    db = _db(tmp_path)
    remember(db, "s", "req", "3.11", kind="requirement", source_refs=SRC)
    run = begin(db, "s", "work")
    receipt = finish(db, "s", run["run_id"], {"key": "out", "value": PIN},
                     idempotency_key="fin-1")
    assert receipt["request_digest"] != NEUTRALIZED_DIGEST
    out = erase_record(db, "s", receipt["record_id"], 1, "rotation")
    assert out["status"] == "COMPLETE"
    # 1 idempotency row + 1 runs row.
    assert out["neutralized_idempotency"] == 2
    assert _idem_row(db, "fin-1")[1] == NEUTRALIZED_DIGEST
    row = db.execute(
        "SELECT request_digest, receipt_json FROM runs WHERE scope_id='s' AND id=?",
        (run["run_id"],)).fetchone()
    assert row[0] == NEUTRALIZED_DIGEST
    assert "request_digest" not in json.loads(row[1])
    with pytest.raises(TurnkeepError) as ei:
        finish(db, "s", run["run_id"], {"key": "out", "value": PIN},
               idempotency_key="fin-1")
    assert ei.value.code == "IDEMPOTENCY_MISMATCH"
    db.close()


def test_live_record_rows_untouched(tmp_path):
    # Only the erased record's rows are neutralized: a live record's
    # digest is unchanged and its replay still works.
    db = _db(tmp_path)
    ra = remember(db, "s", "pin", PIN, source_refs=SRC, idempotency_key="mem-a")
    rb = remember(db, "s", "kB", "other note", source_refs=SRC, idempotency_key="mem-b")
    out = erase_record(db, "s", ra["id"], ra["revision"], "rotation")
    assert out["status"] == "COMPLETE"
    assert _idem_row(db, "mem-a")[1] == NEUTRALIZED_DIGEST
    assert _idem_row(db, "mem-b")[1] == request_digest(
        "remember", "s", "local", _req_dict("kB", "other note"))
    replay = remember(db, "s", "kB", "other note", source_refs=SRC,
                      idempotency_key="mem-b")
    assert replay["id"] == rb["id"]
    db.close()


def test_high_entropy_payload_digest_neutralized(tmp_path):
    # Neutralization does not depend on entropy: a 32-hex-char value
    # is not enumerable in practice, but its digest is neutralized
    # all the same (the marker, not a computable digest).
    db = _db(tmp_path)
    rec = remember(db, "s", "kH", HIGH, source_refs=SRC, idempotency_key="hi-1")
    out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
    assert out["status"] == "COMPLETE"
    assert _idem_row(db, "hi-1")[1] == NEUTRALIZED_DIGEST
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is True
    db.close()


def test_content_digest_neutralized_live_link(tmp_path):
    # (1b) B still cites the source whose id IS the payload, so the
    # sources row survives (2d residual) — but its content_digest is
    # neutralized to NULL. A later remember citing the same source
    # id + type still works (upsert tolerates the NULL digest) and
    # does NOT restore it.
    db = _db(tmp_path)
    rb = remember(db, "s", "b", "other note",
                  source_refs=[{"type": "user_statement", "id": PIN}])
    ra = remember(db, "s", "pin", PIN, source_refs=SRC)
    out = erase_record(db, "s", ra["id"], ra["revision"], "rotation")
    assert out["status"] == "PARTIAL"
    assert out["neutralized_source_digests"] == 1
    row = db.execute(
        "SELECT id, content_digest FROM sources WHERE scope_id='s' AND id=?",
        (PIN,)).fetchone()
    assert row is not None and row[1] is None
    link = db.execute(
        "SELECT COUNT(*) FROM memory_sources WHERE scope_id='s' AND memory_id=? AND source_id=?",
        (rb["id"], PIN)).fetchone()[0]
    assert link == 1
    rc = remember(db, "s", "c", "third note",
                  source_refs=[{"type": "user_statement", "id": PIN}])
    assert rc["id"] != rb["id"]
    row = db.execute(
        "SELECT content_digest FROM sources WHERE scope_id='s' AND id=?",
        (PIN,)).fetchone()
    assert row[0] is None
    db.close()


def test_content_digest_orphan_row_deleted_with_it(tmp_path):
    # (1b) When the erased record's own source link is the payload,
    # the orphaned sources row is deleted by _clear_identifier_sources
    # and takes its digest with it — nothing left to neutralize.
    db = _db(tmp_path)
    rec = remember(db, "s", "pin", PIN,
                   source_refs=[{"type": "user_statement", "id": PIN}])
    out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
    assert out["status"] == "COMPLETE"
    assert out["neutralized_source_digests"] == 0
    row = db.execute(
        "SELECT COUNT(*) FROM sources WHERE scope_id='s' AND id=?", (PIN,)).fetchone()[0]
    assert row == 0
    db.close()


# ---------------------------------------------------------------- P0-R10-02

def test_affected_ids_partial_names_event(tmp_path):
    # GPT's (1) chain, post-fix: B's id IS the payload, so B's
    # remember event holds it in affected_ids_json. Erasing A
    # refuses (PARTIAL), names that event, and stays PARTIAL on
    # repeat — append-only history is never rewritten.
    db = _db(tmp_path)
    rb = remember(db, "s", "other", "some note", source_refs=SRC, record_id=PIN)
    ra = remember(db, "s", "pin", PIN, source_refs=SRC)
    out = erase_record(db, "s", ra["id"], ra["revision"], "rotation")
    assert out["status"] == "PARTIAL"
    assert "append-only" in out["reason"]
    meta = _latest_erase_meta(db)
    assert meta["erase_status"] == "PARTIAL"
    assert meta["append_only_table"] == "events"
    remember_ev = db.execute(
        "SELECT id FROM events WHERE scope_id='s' AND event_type='remember'"
        " AND affected_ids_json LIKE '%4827%'").fetchone()[0]
    # The key "pin" is benign (it is not the payload "4827"), so it
    # stays visible in the entry; only the event is newly named.
    assert meta["remaining"] == [
        {"table": "events", "record_id": ra["id"],
         "event_id": remember_ev, "key": "pin"}]
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is False
    assert any(r["record_id"] == ra["id"]
               for r in rep["controlled"]["controlled_residuals"])
    # The export still carries the payload via affected_ids (history),
    # but the record whose id IS the payload is excluded from records.
    _, recs, evs = _export_rep(db, tmp_path)
    assert f'"{PIN}"' in evs
    assert PIN not in recs
    # Repeat: still PARTIAL, same event named.
    out2 = erase_record(db, "s", ra["id"], ra["revision"], "rotation")
    assert out2["status"] == "PARTIAL"
    meta2 = _latest_erase_meta(db)
    assert meta2["remaining"][0]["event_id"] == remember_ev
    db.close()


def test_actor_binding_partial_names_event(tmp_path):
    # GPT's (2) chain, post-fix: the secret was written as the actor
    # binding of A's own remember event. PARTIAL, names the event;
    # the export does NOT carry actor_binding (live-DB-only surface).
    db = _db(tmp_path)
    rec = remember(db, "s", "pin", PIN, source_refs=SRC, actor_binding=PIN)
    out = erase_record(db, "s", rec["id"], rec["revision"], "rotation",
                       actor_binding="local")
    assert out["status"] == "PARTIAL"
    assert "append-only" in out["reason"]
    meta = _latest_erase_meta(db)
    remember_ev = db.execute(
        "SELECT id FROM events WHERE scope_id='s' AND event_type='remember'"
        " AND actor_binding=?", (PIN,)).fetchone()[0]
    # The key "pin" is benign (it is not the payload "4827"), so it
    # stays visible in the entry; only the event is newly named.
    assert meta["remaining"] == [
        {"table": "events", "record_id": rec["id"],
         "event_id": remember_ev, "key": "pin"}]
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is False
    _, recs, evs = _export_rep(db, tmp_path)
    assert PIN not in evs
    db.close()


def test_actor_binding_substring_is_not_a_copy(tmp_path):
    # Identifier semantics (2e): a binding that merely CONTAINS the
    # needle as a substring is not a copy — whole-value equality only.
    db = _db(tmp_path)
    rec = remember(db, "s", "k1", "note1", source_refs=SRC, actor_binding="x4827y")
    out = erase_record(db, "s", rec["id"], rec["revision"], "rotation",
                       actor_binding="local")
    assert out["status"] == "COMPLETE"
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is True
    db.close()


def test_idempotency_key_other_record_partial(tmp_path):
    # GPT's (3) chain, post-fix: B's operation used the payload as
    # its idempotency key. Erasing A refuses (PARTIAL) and names B;
    # erasing B afterwards does NOT resolve it — the key is A's
    # payload, not B's, so B's erasure leaves the key in place.
    db = _db(tmp_path)
    rb = remember(db, "s", "b", "other note", source_refs=SRC, idempotency_key=PIN)
    ra = remember(db, "s", "pin", PIN, source_refs=SRC, idempotency_key="a-key")
    out = erase_record(db, "s", ra["id"], ra["revision"], "rotation")
    assert out["status"] == "PARTIAL"
    meta = _latest_erase_meta(db)
    assert meta["erase_status"] == "PARTIAL"
    assert meta["remaining"] == [
        {"table": "idempotency", "rowid": meta["remaining"][0]["rowid"],
         "record_id": rb["id"], "key": "b"}]
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is False
    assert any(r["record_id"] == rb["id"] and r["table"] == "idempotency"
               for r in rep["controlled"]["controlled_residuals"])
    row = _idem_row(db, PIN)
    assert row is not None and row[0] == PIN
    # Erase B: its own erase is COMPLETE (its value is benign), but
    # A's residual stays — the key survives B's erasure.
    outb = erase_record(db, "s", rb["id"], rb["revision"], "rotation")
    assert outb["status"] == "COMPLETE"
    assert _idem_row(db, PIN) is not None
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is False
    assert any(r["table"] == "idempotency"
               for r in rep["controlled"]["controlled_residuals"])
    db.close()


def test_idempotency_key_own_row_deleted(tmp_path):
    # (2c) disposition, own-row case: the row is the erased
    # operation's own derived copy and its key (part of the row's
    # primary key) cannot be scrubbed, so the row is deleted outright
    # and the erase is COMPLETE. A replay is then NOT recognised as
    # one (the row is gone, so there is nothing to mismatch against)
    # — it is a fresh remember, and the key-history guard refuses to
    # silently reuse a key with erased history (REPLACEMENT_REQUIRED).
    db = _db(tmp_path)
    rec = remember(db, "s", "pin", PIN, source_refs=SRC, idempotency_key=PIN)
    out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
    assert out["status"] == "COMPLETE"
    assert out["deleted_idempotency"] == 1
    assert _idem_row(db, PIN) is None
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is True
    with pytest.raises(TurnkeepError) as ei:
        remember(db, "s", "pin", PIN, source_refs=SRC, idempotency_key=PIN)
    assert ei.value.code == "REPLACEMENT_REQUIRED"
    db.close()


def test_live_source_link_partial_names_record(tmp_path):
    # GPT's (4) chain, post-fix: live record B cites a source whose
    # id IS the payload. Erasing A refuses (PARTIAL) and names B; the
    # link is retained (R8 design decision) and the export already
    # excludes B. Erasing B afterwards does NOT resolve it — the link
    # id is A's payload, not B's.
    db = _db(tmp_path)
    rb = remember(db, "s", "b", "other note",
                  source_refs=[{"type": "user_statement", "id": PIN}])
    ra = remember(db, "s", "pin", PIN, source_refs=SRC)
    out = erase_record(db, "s", ra["id"], ra["revision"], "rotation")
    assert out["status"] == "PARTIAL"
    meta = _latest_erase_meta(db)
    assert meta["erase_status"] == "PARTIAL"
    assert meta["remaining"] == [
        {"table": "sources", "rowid": meta["remaining"][0]["rowid"],
         "record_id": rb["id"], "key": "b"}]
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is False
    assert any(r["record_id"] == rb["id"] and r["table"] == "sources"
               for r in rep["controlled"]["controlled_residuals"])
    rep_exp, recs, _ = _export_rep(db, tmp_path)
    assert "other note" not in recs and PIN not in recs
    assert any(e.get("key") == "b" for e in rep_exp["records_excluded_secrets"])
    # The link is retained: B still cites the source id.
    link = db.execute(
        "SELECT COUNT(*) FROM memory_sources WHERE scope_id='s' AND memory_id=? AND source_id=?",
        (rb["id"], PIN)).fetchone()[0]
    assert link == 1
    # Erase B: COMPLETE, but A's residual persists.
    outb = erase_record(db, "s", rb["id"], rb["revision"], "rotation")
    assert outb["status"] == "COMPLETE"
    link = db.execute(
        "SELECT COUNT(*) FROM memory_sources WHERE scope_id='s' AND memory_id=? AND source_id=?",
        (rb["id"], PIN)).fetchone()[0]
    assert link == 1
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is False
    assert any(r["table"] == "sources"
               for r in rep["controlled"]["controlled_residuals"])
    db.close()


def test_orphan_source_row_partial(tmp_path):
    # A sources row whose id IS the payload, cited by no live record
    # (orphan, planted by direct write — no public path creates one):
    # PARTIAL, flagged source_orphan, and it never resolves by erasing
    # a record, because no record owns it. Its digest is neutralized
    # (1b) even though the row survives.
    db = _db(tmp_path)
    rec = remember(db, "s", "pin", PIN, source_refs=SRC)
    digest = _digest({"id": PIN, "type": "user_statement"})
    db.execute(
        "INSERT INTO sources(id, scope_id, source_type, source_ref, content_digest, observed_at)"
        " VALUES (?,?,?,?,?,?)",
        (PIN, "s", "user_statement", PIN, digest, _now()))
    db.commit()
    out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
    assert out["status"] == "PARTIAL"
    assert out["neutralized_source_digests"] == 1
    meta = _latest_erase_meta(db)
    assert meta["remaining"] == [
        {"table": "sources", "rowid": meta["remaining"][0]["rowid"],
         "source_orphan": True}]
    row = db.execute(
        "SELECT content_digest FROM sources WHERE scope_id='s' AND id=?",
        (PIN,)).fetchone()
    assert row is not None and row[0] is None
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is False
    # A repair erase cannot resolve an orphan — and its own scan is
    # blind to it: the repair reconstructs needles from the receipts,
    # which the first erase already scrubbed, so the scan finds
    # nothing and the repair returns COMPLETE. The honest signal is
    # the residual report, which stays not-clean: the orphan entry
    # from the first erase never resolves (no record owns it). The
    # split is documented in KNOWN_LIMITATIONS §23.
    out2 = erase_record(db, "s", rec["id"], _rev(db, rec["id"]), "repair")
    assert out2["status"] == "COMPLETE"
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is False
    assert any(r["table"] == "sources"
               for r in rep["controlled"]["controlled_residuals"])
    db.close()


# ------------------------------------------------------- "202" must stay COMPLETE

def test_erase_short_needles_still_complete(tmp_path):
    # (2e) Whole-value decoded equality must not bring back the
    # P2-R8-03 class: "202" / "2026" erase COMPLETE, pinned ids and
    # a random loop (the R8 file keeps the 200x loops; 25x here is
    # the smoke — the new identifier columns add more chances for a
    # random id to match, so the loop also covers them).
    for val, expiry in (("202", LEGAL_TS), ("2026", "2027-01-01T00:00:00+00:00")):
        db = _db(tmp_path, name=f"short-{val}")
        rec = remember(db, "s", "k1", val, source_refs=SRC,
                       idempotency_key=f"short-{val}",
                       record_id="a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6")
        set_expiry(db, "s", rec["id"], expiry, rec["revision"])
        begin(db, "s", task="check k1", idempotency_key="run1")
        out = erase_record(db, "s", rec["id"], _rev(db, rec["id"]), "rotation")
        assert out["status"] == "COMPLETE", val
        db.close()
    fails = 0
    for i in range(25):
        db = _db(tmp_path, name=f"loop-{i}")
        rec = remember(db, "s", "k1", "202", source_refs=SRC,
                       idempotency_key=f"loop-{i}")
        set_expiry(db, "s", rec["id"], LEGAL_TS, rec["revision"])
        begin(db, "s", task="check k1", idempotency_key="run1")
        out = erase_record(db, "s", rec["id"], _rev(db, rec["id"]), "rotation")
        if out["status"] != "COMPLETE":
            fails += 1
        db.close()
    assert fails == 0


# ---------------------------------------------------------------- P1-R10-03

def test_a3_chain_return_redacted(tmp_path):
    # GPT's A3 chain, post-fix: key == value, first erase is PARTIAL
    # (the remember event's metadata.key IS the payload). The return
    # dict omits the key and flags key_redacted — on the first refusal
    # AND on the repeat (the record stays live, so the repeat takes
    # the first-erase path again, not the repair path).
    db = _db(tmp_path)
    rec = remember(db, "s", PIN, PIN, source_refs=SRC)
    out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
    assert out["status"] == "PARTIAL"
    assert "key" not in out and out.get("key_redacted") is True
    assert PIN not in _json(out)
    meta = _latest_erase_meta(db)
    assert meta.get("key_redacted") is True and "key" not in meta
    rep = residual_report(db, "s")
    assert PIN not in _json(rep["controlled"])
    out2 = erase_record(db, "s", rec["id"], rec["revision"], "repair")
    assert out2["status"] == "PARTIAL"
    assert "key" not in out2 and out2.get("key_redacted") is True
    assert PIN not in _json(out2)
    db.close()


def test_repair_return_redacts_payload_key(tmp_path):
    # The repair path's return, with key == value: reachable only
    # when the record is already tombstoned while its receipts still
    # hold the payload — here the first erase refused (step 1, the
    # remember event carries the key), then the row is tombstoned by
    # direct write (test setup: no public path tombstones a record
    # whose first erase refused). The repair reconstructs its needles
    # from the unscrubbed receipts, so the key is redacted.
    db = _db(tmp_path)
    rec = remember(db, "s", PIN, PIN, source_refs=SRC, idempotency_key="rep-mem")
    out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
    assert out["status"] == "PARTIAL"
    db.execute(
        "UPDATE memories SET value_json=?, state='erased', revision=revision+1 "
        "WHERE scope_id='s' AND id=?", (_json(TOMBSTONE), rec["id"]))
    db.commit()
    out2 = erase_record(db, "s", rec["id"], _rev(db, rec["id"]), "repair")
    assert out2["status"] == "PARTIAL"
    assert out2.get("repair") is True
    assert "key" not in out2 and out2.get("key_redacted") is True
    assert PIN not in _json(out2)
    db.close()


def test_benign_key_visible_in_returns(tmp_path):
    # The rule is conditional: a benign key stays visible in all
    # three return paths (first-erase COMPLETE, repair COMPLETE),
    # so the response still names its record.
    db = _db(tmp_path)
    rec = remember(db, "s", "kBenign", PIN, source_refs=SRC)
    out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
    assert out["status"] == "COMPLETE"
    assert out.get("key") == "kBenign" and "key_redacted" not in out
    out2 = erase_record(db, "s", rec["id"], _rev(db, rec["id"]), "repair")
    assert out2["status"] == "COMPLETE"
    assert out2.get("key") == "kBenign" and "key_redacted" not in out2
    db.close()


def test_cli_erase_stdout_has_no_payload(tmp_path):
    # The CLI prints the erase return dict directly to stdout —
    # fixing the return fixes the CLI (verification A3.6, inverted).
    dbpath = tmp_path / "cli.sqlite3"
    db = open_db(dbpath)
    register_scope(db, "s")
    rec = remember(db, "s", PIN, PIN, source_refs=SRC)
    db.close()
    proc = subprocess.run(
        [sys.executable, "-m", "turnkeep", "--db", str(dbpath),
         "erase", "--scope", "s", "--id", rec["id"],
         "--expected-revision", str(rec["revision"]), "--reason", "rotation"],
        capture_output=True, text=True, timeout=60, env=ENV)
    assert proc.returncode == 0
    assert PIN not in proc.stdout
    assert "key_redacted" in proc.stdout


def test_audit_other_returns_do_not_echo_erased_key(tmp_path):
    # (3c) audit: correct / retract return the dump of a LIVE record
    # (the key is live data, not post-erasure materialization), and
    # finish's receipt has no "key" field at all — so there is no
    # same-class echo to redact. Pinned here so a future change that
    # adds a key echo fails loudly.
    db = _db(tmp_path)
    rec = remember(db, "s", "kLive", "live note", source_refs=SRC)
    new = correct(db, "s", rec["id"], rec["revision"], "new note",
                  source_refs=SRC)
    assert new["key"] == "kLive" and new["state"] == "active"
    ret = retract(db, "s", new["id"], new["revision"], "withdrawn")
    assert ret["key"] == "kLive" and ret["state"] == "retracted"
    run = begin(db, "s", "work")
    receipt = finish(db, "s", run["run_id"], {"key": "out", "value": "done"})
    assert "key" not in receipt
    assert receipt["record_id"] is not None
    db.close()
