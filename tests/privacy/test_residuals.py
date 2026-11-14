"""T038 residuals: what erasure cannot control is listed, never claimed deleted."""
import json

from turnkeep.storage import open_db
from turnkeep.scopes import register_scope
from turnkeep.memory import remember
from turnkeep.erasure import erase_record, residual_report

SRC = [{"id": "u:1", "type": "user_statement"}]


def _db(tmp_path):
    db = open_db(tmp_path / "r.sqlite3")
    register_scope(db, "r")
    return db


def test_residual_report_clean_after_real_scan(tmp_path):
    # P0-3: live_tables_clean is computed by a real scan, and a clean
    # erasure really scans clean.
    db = _db(tmp_path)
    rec = remember(db, "r", "k", {"api_key": "sk-nested-SECRET-999", "tags": ["tag-alpha", "tag-beta"]},
                   kind="fact", source_refs=SRC)
    erase_record(db, "r", rec["id"], rec["revision"], "cleanup")
    rep = residual_report(db, "r")
    assert rep["controlled"]["live_tables_clean"] is True
    db.close()


def test_residual_report_flags_receipt_still_holding_payload(tmp_path):
    # P0-3: a hand-crafted receipt that still holds the erased payload
    # must make live_tables_clean False (previously hardcoded True).
    from turnkeep.runs import begin
    from turnkeep.commits import finish
    from turnkeep.retrieval import search_by_key
    db = _db(tmp_path)
    run = begin(db, "r", "write the secret down")
    finish(db, "r", run["run_id"], {"key": "ci.api_key", "value": "SYNTHETIC-SECRET-residual-test"},
           idempotency_key="r-1")
    rec = search_by_key(db, "r", "ci.api_key")[0]
    erase_record(db, "r", rec["id"], rec["revision"], "rotation")
    assert residual_report(db, "r")["controlled"]["live_tables_clean"] is True
    # Tamper: put the payload back into the run receipt, as an unscrupulous
    # (or buggy) derived copy would.
    db.execute("UPDATE runs SET receipt_json=?",
               (json.dumps({"record_id": rec["id"],
                            "value": "SYNTHETIC-SECRET-residual-test"}),))
    db.commit()
    rep = residual_report(db, "r")
    assert rep["controlled"]["live_tables_clean"] is False
    assert rep["status"] == "CONTROLLED_NOT_CLEAN"
    db.close()


def test_residual_report_flags_shared_secret_residual(tmp_path):
    # P0-R2-01 / P0-3 (GPT's exact repro): two records share a secret;
    # erasing one is PARTIAL with the other in `remaining`, and the
    # residual report must NOT call the live tables clean — it must say
    # CONTROLLED_NOT_CLEAN and name the affected record. Erasing the
    # second copy afterwards resolves the residual and the report goes
    # clean again.
    import hashlib
    db = _db(tmp_path)
    shared = "SHARED_SYNTH_R2"
    r1 = remember(db, "r", "m.one", {"api_key": shared, "note": "first"},
                  kind="fact", source_refs=SRC)
    r2 = remember(db, "r", "m.two", {"api_key": shared, "note": "second"},
                  kind="fact", source_refs=SRC)
    out = erase_record(db, "r", r1["id"], r1["revision"], "cleanup")
    assert out["status"] == "PARTIAL" and out["remaining"] == {"memories": [2]}
    rep = residual_report(db, "r")
    assert rep["controlled"]["live_tables_clean"] is False
    assert rep["status"] == "CONTROLLED_NOT_CLEAN"
    residuals = rep["controlled"]["controlled_residuals"]
    assert any(x["record_id"] == r2["id"] and x["key"] == "m.two" for x in residuals)
    # The persisted outcome records needle HASHES only: the shared secret
    # itself must appear nowhere in the erase event metadata. Since the
    # R4 rework the hashes are HMAC-SHA256 keyed by the per-scope secret
    # (P0-R4-04), NOT raw SHA-256: the raw digest must NOT appear, and
    # the stored hash must equal the HMAC of the secret under the
    # scope's key (the old raw-SHA-256 assertion is the exact shape
    # P0-R4-04 removed).
    meta = db.execute(
        "SELECT metadata_json FROM events WHERE event_type='erase'").fetchone()[0]
    assert shared not in meta
    assert hashlib.sha256(shared.encode()).hexdigest() not in meta
    import hmac as _hmac
    # P0-R12-01: the hashes are HMAC-SHA256 keyed by the erased
    # record's own verification key (per-record key), not a scope
    # key — a fresh DB has no scope key at all.
    key_hex = db.execute(
        "SELECT secret_hex FROM record_erase_keys "
        "WHERE scope_id='r' AND record_id=?", (r1["id"],)).fetchone()[0]
    assert _hmac.new(bytes.fromhex(key_hex), shared.encode(),
                     hashlib.sha256).hexdigest() in meta
    # Erasing the second copy resolves the residual.
    out2 = erase_record(db, "r", r2["id"], r2["revision"], "cleanup")
    assert out2["status"] == "COMPLETE"
    rep2 = residual_report(db, "r")
    assert rep2["controlled"]["controlled_residuals"] == []
    assert rep2["controlled"]["live_tables_clean"] is True
    db.close()


def test_residual_report_flags_refused_erase_in_append_only_history(tmp_path):
    # A refused erase (payload also in append-only events) is persisted
    # as an erase outcome too, so the report flags it instead of
    # calling the tables clean while the live events table holds it.
    from turnkeep.storage import publish_event
    db = _db(tmp_path)
    rec = remember(db, "r", "k", {"api_key": "SYNTHETIC-SECRET-refused"},
                   kind="fact", source_refs=SRC)
    publish_event(db, "r", "note", "local", {rec["id"]: None},
                  metadata={"leaked": "SYNTHETIC-SECRET-refused"})
    out = erase_record(db, "r", rec["id"], rec["revision"], "rotation")
    assert out["status"] == "PARTIAL" and out["erased"] is False
    rep = residual_report(db, "r")
    assert rep["controlled"]["live_tables_clean"] is False
    assert rep["status"] == "CONTROLLED_NOT_CLEAN"
    assert any(x["record_id"] == rec["id"] and x["key"] == "k"
               for x in rep["controlled"]["controlled_residuals"])
    db.close()


def test_residual_report_lists_uncontrolled_copies(tmp_path):
    db = _db(tmp_path)
    rec = remember(db, "r", "k", "v", kind="fact", source_refs=SRC)
    # A backup copy the user made sits next to the live DB.
    (tmp_path / "r.sqlite3.bak").write_bytes(b"backup copy")
    erase_record(db, "r", rec["id"], rec["revision"], "cleanup")
    rep = residual_report(db, "r")
    assert rep["status"] == "COMPLETE_WITH_RESIDUALS"
    assert rep["controlled"]["erased_records"] == [{"id": rec["id"], "key": "k"}]
    kinds = {u["kind"] for u in rep["uncontrolled"]}
    assert {"backup_or_export_copy", "client_history", "physical_recovery"} <= kinds
    bak = next(u for u in rep["uncontrolled"] if u["kind"] == "backup_or_export_copy")
    assert "r.sqlite3.bak" in bak["path"]
    assert "delete it yourself" in bak["note"]
    db.close()


def test_retract_erase_external_are_not_confused(tmp_path):
    # Retract keeps the payload (state change only); erase removes it.
    # The two are different operations with different wording.
    db = _db(tmp_path)
    from turnkeep.corrections import retract
    rec = remember(db, "r", "k", "secret-value", kind="fact", source_refs=SRC)
    retract(db, "r", rec["id"], 1, "correction")
    row = db.execute("SELECT value_json, state FROM memories WHERE id=?", (rec["id"],)).fetchone()
    assert "secret-value" in row[0] and row[1] == "retracted"
    rec2 = remember(db, "r", "k2", "secret-value-2", kind="fact", source_refs=SRC)
    erase_record(db, "r", rec2["id"], 1, "cleanup")
    row2 = db.execute("SELECT value_json, state FROM memories WHERE id=?", (rec2["id"],)).fetchone()
    assert "secret-value-2" not in row2[0] and row2[1] == "erased"
    db.close()
