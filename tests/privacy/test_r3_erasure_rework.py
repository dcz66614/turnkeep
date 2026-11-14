"""R3 rework regression tests: P0-R3-01 (receipt scrub shapes + completable
PARTIAL erase) and P0-R3-02 (erasure/retract reason must never leak the
payload into the append-only event log).

Written BEFORE the fix: every test here fails on the pre-fix code
(verified) and must pass after it.
"""
import hashlib
import json

import pytest

from turnkeep.storage import open_db, publish_event
from turnkeep.scopes import register_scope
from turnkeep.memory import remember
from turnkeep.corrections import correct, retract, resolve_conflict
from turnkeep.runs import begin
from turnkeep.errors import TurnkeepError
from turnkeep.erasure import (
    erase_record, residual_report, _describe_remaining,
    _tombstone_structure_clean,
)
from turnkeep.portability import export_scope

SRC = [{"id": "u:1", "type": "user_statement"}]
TOMBSTONE = {"_erased": True}


def _db(tmp_path, scope="s"):
    db = open_db(tmp_path / "t.sqlite3")
    register_scope(db, scope)
    return db


def _receipt(db, scope="s"):
    row = db.execute(
        "SELECT response_json FROM idempotency WHERE scope_id=?", (scope,)).fetchone()
    assert row is not None, "expected an idempotency receipt"
    return json.loads(row[0])


def _no_secret_anywhere(db, scope, secret):
    """The secret must not survive in any live table's text."""
    tables = [r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
        " AND name NOT LIKE 'sqlite_%'").fetchall()]
    for table in tables:
        cols = [r[1] for r in db.execute(f"PRAGMA table_info({table})").fetchall()]
        for col in cols:
            if col == "id":
                continue
            hits = db.execute(
                f"SELECT COUNT(*) FROM {table} WHERE CAST({col} AS TEXT) LIKE ?",
                (f"%{secret}%",)).fetchone()[0]
            assert hits == 0, f"secret survives in {table}.{col}"


# ---------------------------------------------------------------- P0-R3-01

def test_remember_idempotency_receipt_scrubbed_on_erase(tmp_path):
    # GPT's exact P0-R3-01 repro: Remember with value {"api_key": "sk-..."}
    # + idempotency key -> erase -> receipt must not hold the secret,
    # status COMPLETE.
    secret = "sk-SYNTH-R3-01-abcdef"
    db = _db(tmp_path)
    rec = remember(db, "s", "k.api", {"api_key": secret}, kind="fact",
                   source_refs=SRC, idempotency_key="r3-01-key-1234")
    assert secret in json.dumps(_receipt(db))
    out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
    assert out["status"] == "COMPLETE" and out["erased"] is True
    receipt = _receipt(db)
    assert receipt["value"] == TOMBSTONE
    assert receipt["id"] == rec["id"]  # pointer kept, payload gone
    assert secret not in json.dumps(receipt)
    _no_secret_anywhere(db, "s", secret)
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is True
    assert rep["controlled"]["controlled_residuals"] == []
    db.close()


def test_correct_idempotency_receipt_scrubbed_on_erase(tmp_path):
    # correct() receipts store dump_record() output ({"id", "value"}),
    # not {"record_id"} — same blind spot as remember.
    secret = "sk-SYNTH-R3-01-correct"
    db = _db(tmp_path)
    old = remember(db, "s", "k", "v1", kind="fact", source_refs=SRC)
    new = correct(db, "s", old["id"], old["revision"],
                  {"api_key": secret}, source_refs=SRC,
                  idempotency_key="correct-key-1")
    assert secret in json.dumps(_receipt(db))
    out = erase_record(db, "s", new["id"], new["revision"], "rotation")
    assert out["status"] == "COMPLETE" and out["erased"] is True
    assert _receipt(db)["value"] == TOMBSTONE
    _no_secret_anywhere(db, "s", secret)
    assert residual_report(db, "s")["controlled"]["live_tables_clean"] is True
    db.close()


def test_retract_idempotency_receipt_scrubbed_on_erase(tmp_path):
    secret = "sk-SYNTH-R3-01-retract"
    db = _db(tmp_path)
    rec = remember(db, "s", "k", {"api_key": secret}, kind="fact", source_refs=SRC)
    retracted = retract(db, "s", rec["id"], rec["revision"], "correction",
                        idempotency_key="retract-key-1")
    assert secret in json.dumps(_receipt(db))
    out = erase_record(db, "s", retracted["id"], retracted["revision"], "rotation")
    assert out["status"] == "COMPLETE" and out["erased"] is True
    assert _receipt(db)["value"] == TOMBSTONE
    _no_secret_anywhere(db, "s", secret)
    assert residual_report(db, "s")["controlled"]["live_tables_clean"] is True
    db.close()


def test_resolve_conflict_idempotency_receipt_scrubbed_on_erase(tmp_path):
    secret = "sk-SYNTH-R3-01-resolve"
    db = _db(tmp_path)
    remember(db, "s", "k", "v1", kind="fact", source_refs=SRC)
    remember(db, "s", "k", "v2", kind="fact", source_refs=SRC)
    cands = db.execute(
        "SELECT id, revision FROM memories WHERE scope_id='s' AND key='k'"
        " AND state='conflicted' ORDER BY id").fetchall()
    resolved = resolve_conflict(
        db, "s", "k", [{"id": r[0], "revision": r[1]} for r in cands],
        {"api_key": secret}, source_refs=SRC, idempotency_key="resolve-key-1")
    assert secret in json.dumps(_receipt(db))
    out = erase_record(db, "s", resolved["id"], resolved["revision"], "rotation")
    assert out["status"] == "COMPLETE" and out["erased"] is True
    assert _receipt(db)["value"] == TOMBSTONE
    _no_secret_anywhere(db, "s", secret)
    assert residual_report(db, "s")["controlled"]["live_tables_clean"] is True
    db.close()


def test_begin_idempotency_receipt_nested_records_scrubbed(tmp_path):
    # begin() receipts nest full record dumps inside "records" lists.
    # Only the erased record's dump is tombstoned; the other record's
    # dump in the same receipt must stay intact.
    secret = "sk-SYNTH-R3-01-begin"
    db = _db(tmp_path)
    doomed = remember(db, "s", "k.secret", {"api_key": secret}, kind="fact",
                      source_refs=SRC)
    kept = remember(db, "s", "k.plain", "ordinary value", kind="fact",
                    source_refs=SRC)
    begin(db, "s", "read both", idempotency_key="begin-key-1")
    receipt = _receipt(db)
    assert secret in json.dumps(receipt)
    out = erase_record(db, "s", doomed["id"], doomed["revision"], "rotation")
    assert out["status"] == "COMPLETE" and out["erased"] is True
    receipt = _receipt(db)
    by_id = {r["id"]: r for r in receipt["records"]}
    assert by_id[doomed["id"]]["value"] == TOMBSTONE
    assert by_id[kept["id"]]["value"] == "ordinary value"
    _no_secret_anywhere(db, "s", secret)
    assert residual_report(db, "s")["controlled"]["live_tables_clean"] is True
    db.close()


def test_tombstone_structure_clean_sees_dump_shaped_receipts(tmp_path):
    # _tombstone_structure_clean had the same {"record_id"}-only blind
    # spot: a dump-shaped receipt still holding the payload must fail it.
    secret = "sk-SYNTH-R3-01-structure"
    db = _db(tmp_path)
    rec = remember(db, "s", "k", {"api_key": secret}, kind="fact",
                   source_refs=SRC, idempotency_key="struct-key-1")
    erase_record(db, "s", rec["id"], rec["revision"], "rotation")
    assert _tombstone_structure_clean(db, "s", {rec["id"]}) is True
    db.execute("UPDATE idempotency SET response_json=?",
               (json.dumps({"id": rec["id"], "value": {"api_key": secret}}),))
    db.commit()
    assert _tombstone_structure_clean(db, "s", {rec["id"]}) is False
    db.close()


def test_second_erase_repairs_receipt_left_by_old_code(tmp_path):
    # A PARTIAL erase from the old code left the receipt holding the
    # secret (simulated here by putting the payload back into the
    # receipt after a COMPLETE erase). A second erase_record() on the
    # already-erased record must complete the erase: scrub the receipt,
    # NOT resurrect the record, NOT bump the revision again.
    secret = "sk-SYNTH-R3-01-repair"
    db = _db(tmp_path)
    rec = remember(db, "s", "k.api", {"api_key": secret}, kind="fact",
                   source_refs=SRC, idempotency_key="repair-key-1")
    out1 = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
    assert out1["status"] == "COMPLETE"
    revision_after_first = out1["revision"]
    # Simulate the old-code residual: the receipt still holds the secret.
    db.execute("UPDATE idempotency SET response_json=?",
               (json.dumps({"id": rec["id"], "key": "k.api",
                            "value": {"api_key": secret}}),))
    db.commit()
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is False
    assert rep["status"] == "CONTROLLED_NOT_CLEAN"
    out2 = erase_record(db, "s", rec["id"], revision_after_first, "repair")
    assert out2["status"] == "COMPLETE" and out2["erased"] is True
    assert out2["revision"] == revision_after_first  # no second bump
    assert out2.get("repair") is True
    row = db.execute("SELECT value_json, state, revision FROM memories WHERE id=?",
                     (rec["id"],)).fetchone()
    assert json.loads(row[0]) == TOMBSTONE and row[1] == "erased"
    assert row[2] == revision_after_first
    assert _receipt(db)["value"] == TOMBSTONE
    _no_secret_anywhere(db, "s", secret)
    rep2 = residual_report(db, "s")
    assert rep2["controlled"]["live_tables_clean"] is True
    assert rep2["controlled"]["controlled_residuals"] == []
    db.close()


def test_second_erase_when_nothing_remaining_is_a_clean_noop(tmp_path):
    db = _db(tmp_path)
    rec = remember(db, "s", "k", "v", kind="fact", source_refs=SRC)
    out1 = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
    # R5 expectation change (P0-R5-01): a repair erase accepts only the
    # controlled labels {"repair", "rotation"} — the old "again" reason
    # is now INVALID_INPUT, so the clean-noop repair uses "repair".
    out2 = erase_record(db, "s", rec["id"], out1["revision"], "repair")
    assert out2["status"] == "COMPLETE" and out2["erased"] is True
    assert out2["revision"] == out1["revision"]
    assert out2["scrubbed_receipts"] == []
    db.close()


def test_second_erase_wrong_revision_still_conflicts(tmp_path):
    db = _db(tmp_path)
    rec = remember(db, "s", "k", "v", kind="fact", source_refs=SRC)
    out1 = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
    with pytest.raises(TurnkeepError) as ei:
        # R5 (P0-R5-01): "repair" is a controlled label; the revision
        # check runs before the allowlist gate, so a stale revision
        # still reports VERSION_CONFLICT, not INVALID_INPUT.
        erase_record(db, "s", rec["id"], out1["revision"] + 1, "repair")
    assert ei.value.code == "VERSION_CONFLICT"
    db.close()


def test_describe_remaining_attributes_idempotency_residual(tmp_path):
    # _describe_remaining used to report an idempotency residual as
    # record_id None / key None (it only read data["record_id"]).
    secret = "sk-SYNTH-R3-01-describe"
    db = _db(tmp_path)
    rec = remember(db, "s", "k.api", {"api_key": secret}, kind="fact",
                   source_refs=SRC, idempotency_key="describe-key-1")
    rowid = db.execute(
        "SELECT rowid FROM idempotency WHERE scope_id='s'").fetchone()[0]
    payload = json.dumps({"api_key": secret}, sort_keys=True,
                         ensure_ascii=False, separators=(",", ":"))
    described = _describe_remaining(db, "s", {"idempotency": [rowid]},
                                    needles={payload, secret})
    assert len(described) == 1
    assert described[0]["record_id"] == rec["id"]
    assert described[0]["key"] == "k.api"
    db.close()


def test_old_style_unattributed_residual_is_attributed_and_repaired(tmp_path):
    # Residuals persisted by the old code carry no record_id/key in the
    # remaining entry. They must still be attributable (via the erase
    # event's affected ids) and clearable by a repair erase.
    secret = "sk-SYNTH-R3-01-oldstyle"
    db = _db(tmp_path)
    rec = remember(db, "s", "k.api", {"api_key": secret}, kind="fact",
                   source_refs=SRC, idempotency_key="oldstyle-key-1")
    out1 = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
    assert out1["status"] == "COMPLETE"
    # Simulate the old-code state: receipt holds the secret again, and
    # the erase event's remaining entry has no record id (old shape).
    db.execute("UPDATE idempotency SET response_json=?",
               (json.dumps({"id": rec["id"], "key": "k.api",
                            "value": {"api_key": secret}}),))
    db.commit()
    publish_event(db, "s", "erase", "local", {rec["id"]: None},
                  metadata={"key": "k.api", "reason": "rotation",
                            "erase_status": "PARTIAL", "erased": True,
                            "remaining": [{"table": "idempotency", "rowid": 1}],
                            "needle_hashes": [
                                hashlib.sha256(secret.encode()).hexdigest()]})
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is False
    residuals = rep["controlled"]["controlled_residuals"]
    assert any(x["record_id"] == rec["id"] and x["key"] == "k.api"
               for x in residuals), residuals
    out2 = erase_record(db, "s", rec["id"], out1["revision"], "repair")
    assert out2["status"] == "COMPLETE"
    rep2 = residual_report(db, "s")
    assert rep2["controlled"]["live_tables_clean"] is True
    assert rep2["controlled"]["controlled_residuals"] == []
    db.close()


# ---------------------------------------------------------------- P0-R3-02

def test_erase_reason_equal_to_secret_rejected(tmp_path):
    # GPT's exact P0-R3-02 repro: erase with reason == secret must not
    # write the secret into the events table or events.jsonl, and the
    # residual report must stay honest (no erase happened at all).
    secret = "sk-SYNTHETIC-REASON-R3-9448"
    db = _db(tmp_path)
    rec = remember(db, "s", "k.tok", secret, kind="fact", source_refs=SRC)
    with pytest.raises(TurnkeepError) as ei:
        erase_record(db, "s", rec["id"], rec["revision"], secret)
    assert ei.value.code == "INVALID_INPUT"
    # Nothing was published: no erase event, record untouched.
    assert db.execute(
        "SELECT COUNT(*) FROM events WHERE event_type='erase'").fetchone()[0] == 0
    row = db.execute("SELECT value_json, state FROM memories WHERE id=?",
                     (rec["id"],)).fetchone()
    assert secret in row[0] and row[1] == "active"
    assert db.execute(
        "SELECT COUNT(*) FROM events WHERE CAST(metadata_json AS TEXT)"
        " LIKE ?", (f"%{secret}%",)).fetchone()[0] == 0
    out_dir = tmp_path / "export"
    export_scope(db, "s", out_dir)
    assert secret not in (out_dir / "events.jsonl").read_text()
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is True
    assert rep["controlled"]["controlled_residuals"] == []
    db.close()


def test_erase_reason_containing_secret_rejected(tmp_path):
    secret = "sk-SYNTH-R3-02-substring"
    db = _db(tmp_path)
    rec = remember(db, "s", "k", {"api_key": secret}, kind="fact", source_refs=SRC)
    with pytest.raises(TurnkeepError) as ei:
        erase_record(db, "s", rec["id"], rec["revision"],
                     f"rotating {secret} after leak")
    assert ei.value.code == "INVALID_INPUT"
    assert db.execute(
        "SELECT COUNT(*) FROM events WHERE event_type='erase'").fetchone()[0] == 0
    db.close()


def test_erase_reason_checked_before_append_only_refusal(tmp_path):
    # The reason check must run BEFORE the step-1 PARTIAL refusal
    # publishes its erase event — otherwise the refusal itself leaks.
    secret = "sk-SYNTH-R3-02-step1"
    db = _db(tmp_path)
    rec = remember(db, "s", "k", {"api_key": secret}, kind="fact", source_refs=SRC)
    publish_event(db, "s", "note", "local", {rec["id"]: None},
                  metadata={"leaked": secret})
    with pytest.raises(TurnkeepError) as ei:
        erase_record(db, "s", rec["id"], rec["revision"], secret)
    assert ei.value.code == "INVALID_INPUT"
    assert db.execute(
        "SELECT COUNT(*) FROM events WHERE event_type='erase'").fetchone()[0] == 0
    # The record is untouched (the refusal path never ran either).
    row = db.execute("SELECT value_json, state FROM memories WHERE id=?",
                     (rec["id"],)).fetchone()
    assert secret in row[0] and row[1] == "active"
    db.close()


def test_retract_reason_equal_to_secret_rejected(tmp_path):
    # Same verbatim-reason pattern in retract (corrections.py): a
    # retract reason equal to the payload must not land in the event log
    # — it would also make a later erase refuse (append-only PARTIAL).
    secret = "sk-SYNTH-R3-02-retract"
    db = _db(tmp_path)
    rec = remember(db, "s", "k", secret, kind="fact", source_refs=SRC)
    with pytest.raises(TurnkeepError) as ei:
        retract(db, "s", rec["id"], rec["revision"], secret)
    assert ei.value.code == "INVALID_INPUT"
    assert db.execute(
        "SELECT COUNT(*) FROM events WHERE event_type='retract'").fetchone()[0] == 0
    row = db.execute("SELECT value_json, state FROM memories WHERE id=?",
                     (rec["id"],)).fetchone()
    assert secret in row[0] and row[1] == "active"
    # And a clean reason still retracts (the check must not over-block).
    out = retract(db, "s", rec["id"], rec["revision"], "correction")
    assert out["state"] == "retracted"
    db.close()


def test_retract_reason_containing_secret_rejected(tmp_path):
    # R6 (P0-R6-01): "see {secret} for why" stays rejected — now as a
    # non-label by the retract allowlist, before the needle check.
    secret = "sk-SYNTH-R3-02-retract-sub"
    db = _db(tmp_path)
    rec = remember(db, "s", "k", {"api_key": secret}, kind="fact", source_refs=SRC)
    with pytest.raises(TurnkeepError) as ei:
        retract(db, "s", rec["id"], rec["revision"], f"see {secret} for why")
    assert ei.value.code == "INVALID_INPUT"
    assert db.execute(
        "SELECT COUNT(*) FROM events WHERE event_type='retract'").fetchone()[0] == 0
    db.close()


def test_clean_erase_reason_still_works(tmp_path):
    # The reason guard must not reject ordinary reasons.
    db = _db(tmp_path)
    rec = remember(db, "s", "k", {"api_key": "sk-SYNTH-R3-02-clean"},
                   kind="fact", source_refs=SRC)
    out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
    assert out["status"] == "COMPLETE"
    ev = db.execute(
        "SELECT metadata_json FROM events WHERE event_type='erase'").fetchone()
    assert json.loads(ev[0])["reason"] == "rotation"
    db.close()
