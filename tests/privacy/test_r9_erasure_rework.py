"""R9 rework regression tests: P0-R9-01 (encoded key bypasses the event
scan), P0-R9-02 (broken urlsafe_b64decode(validate=True) call),
P0-R9-03 (encoded copy in another record not scanned), P1-R9-04
(export / import report surfaces re-leak the key).

The scan rule behind items 1-3 (report §3): a whole value that DECODES
(known set: Base64 / URL-safe Base64 / percent / reversed / hex /
rot13) to the payload IS a copy — for identifiers in audit structure
and for whole user-data values alike — while a decoded fragment merely
containing a short needle substring is never a hit (the P2-R8-03
"202" class stays fixed). The honest outcome on detection is PARTIAL:
append-only events cannot be rewritten, and a second live record
holding the copy must be erased first.

Written BEFORE the fix where feasible: on the pre-fix code the
six-encoding remember() cases erase COMPLETE while events.jsonl still
holds the encoded key; _decoded_forms("YWE-") misses "aa>" and the
import accepts it; the A/B cross-record cases erase COMPLETE while B
still holds the encoding; and the export / import reports carry the
raw payload key or its reversible encoding (the R7 test even asserted
the raw key).
"""
import base64
import codecs
import hashlib
import json
import subprocess
import sys
import urllib.parse
from pathlib import Path

import pytest

from turnkeep.storage import open_db, _json, _now
from turnkeep.scopes import register_scope
from turnkeep.memory import remember
from turnkeep.retention import set_expiry
from turnkeep.errors import TurnkeepError
from turnkeep.erasure import (
    erase_record, residual_report, _cell_holds_needle,
    _decoded_forms, _decoded_equals_needle, _encoded_forms,
    _value_contains_needle,
)
from turnkeep.portability import export_scope, import_package
from turnkeep.runs import begin

SRC = [{"id": "u:1", "type": "user_statement"}]
PAYLOAD = "SYNTHETIC-PAYLOAD-9f3a7c2e"
SHORT = "aa>"  # GPT's P0-R9-02 repro value; urlsafe Base64 is "YWE-"
LEGAL_TS = "2026-11-12T22:00:00+00:00"
LEGAL_CREATED = "2026-11-11T00:00:00+00:00"
ROOT = Path(__file__).resolve().parents[2]
ENV = {"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin:/usr/local/bin",
       "HOME": "/tmp", "TMPDIR": "/tmp"}


def _db(tmp_path, name="t", scope="s"):
    db = open_db(tmp_path / f"{name}.sqlite3")
    register_scope(db, scope)
    return db


def _encodings(s):
    return {
        "b64": base64.b64encode(s.encode()).decode(),
        "urlsafe": base64.urlsafe_b64encode(s.encode()).decode(),
        "percent": urllib.parse.quote(s),
        "reversed": s[::-1],
        "hex": s.encode().hex(),
        "rot13": codecs.encode(s, "rot_13"),
    }


def _mk_package(tmp_path, records, scope="s", name="pkg"):
    """Hand-build an export package dir with correct manifest digests."""
    d = tmp_path / name
    d.mkdir()
    rec_text = "".join(_json(r) + "\n" for r in records)
    # write_bytes, not write_text: text mode would translate "\n" to
    # "\r\n" on Windows while the manifest digest below is over the
    # in-memory (LF) text, so import would refuse the package as
    # tampered. Writing bytes keeps the file identical to the digest
    # input on every platform.
    (d / "records.jsonl").write_bytes(rec_text.encode("utf-8"))
    (d / "events.jsonl").write_bytes(b"")
    files = {
        "records.jsonl": hashlib.sha256(rec_text.encode()).hexdigest(),
        "events.jsonl": hashlib.sha256(b"").hexdigest(),
    }
    manifest = {"format": "turnkeep-export", "schema_version": 1,
                "scope_id": scope, "exported_at": _now(), "tool": "test",
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


# ---------------------------------------------------------------- P0-R9-02

def test_decoded_forms_includes_urlsafe():
    # The pre-fix call base64.urlsafe_b64decode(padded, validate=True)
    # raised TypeError on every call (urlsafe_b64decode takes no
    # validate argument), swallowed by the except — so "aa>" never
    # entered the decode set for "YWE-".
    assert SHORT in _decoded_forms("YWE-")
    assert SHORT in _decoded_forms("YWE+")  # std Base64 control
    assert "YWE-" not in _decoded_forms("YWE+")
    # The encode direction is the same six, urlsafe included.
    assert _encoded_forms(SHORT) == {
        SHORT, "YWE+", "YWE-", "aa%3E", ">aa", "61613e", "nn>",
    }


def test_import_rejects_urlsafe_encoded_key(tmp_path):
    # End-to-end P0-R9-02: the import gate already rejects the std
    # Base64 form; after the fix it rejects the URL-safe form too.
    db = _db(tmp_path)
    for key in ("YWE-", "YWE+"):
        pkg = _mk_package(tmp_path, [_rec("r1", key, SHORT)], name=f"pkg-{key}")
        with pytest.raises(TurnkeepError) as ei:
            import_package(db, pkg, target_scope="s")
        assert ei.value.code == "INVALID_INPUT"
    db.close()


def test_erase_urlsafe_encoded_key_is_partial(tmp_path):
    # The remember() path (no import gate): an encoded key in the
    # remember event's metadata must refuse the erase, honestly.
    db = _db(tmp_path)
    rec = remember(db, "s", "YWE-", SHORT, source_refs=SRC, idempotency_key="u1")
    out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
    assert out["status"] == "PARTIAL"
    assert "append-only" in out["reason"]
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is False
    # The PARTIAL event itself must not re-leak the encoded key.
    meta = _latest_erase_meta(db)
    assert "key" not in meta and meta.get("key_redacted") is True
    assert "YWE-" not in _json(meta)
    # Export excludes the record (hash check now decodes urlsafe), so
    # records.jsonl is clean; events.jsonl still holds the remember
    # event — append-only history, by design, and the refusal names it.
    _, recs, evs = _export_rep(db, tmp_path)
    assert "YWE-" not in recs
    assert "YWE-" in evs
    db.close()


# ---------------------------------------------------------------- P0-R9-01

def test_erase_encoded_key_is_partial_all_encodings(tmp_path):
    # GPT's six-encoding remember() repro: every encoding refuses.
    # Percent is the identity for this PAYLOAD and urlsafe == std
    # Base64 here, so those two cases are degenerate — the SHORT
    # variants below cover the distinct forms.
    for name, enc in _encodings(PAYLOAD).items():
        db = _db(tmp_path, name=f"a1-{name}")
        rec = remember(db, "s", enc, PAYLOAD, source_refs=SRC,
                       idempotency_key=f"e-{name}")
        out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
        assert out["status"] == "PARTIAL", name
        assert "append-only" in out["reason"]
        rep = residual_report(db, "s")
        assert rep["controlled"]["live_tables_clean"] is False
        db.close()


def test_erase_encoded_key_is_partial_short_distinct_forms(tmp_path):
    # SHORT ("aa>") makes urlsafe ("YWE-") and percent ("aa%3E")
    # genuinely distinct from std Base64 ("YWE+").
    for name, enc in _encodings(SHORT).items():
        db = _db(tmp_path, name=f"a1s-{name}")
        rec = remember(db, "s", enc, SHORT, source_refs=SRC,
                       idempotency_key=f"s-{name}")
        out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
        assert out["status"] == "PARTIAL", (name, enc)
        rep = residual_report(db, "s")
        assert rep["controlled"]["live_tables_clean"] is False
        db.close()


def test_erase_encoded_key_partial_names_the_event(tmp_path):
    # The refusal names the record and the append-only table, and the
    # persisted outcome lets residual_report() see the residual.
    db = _db(tmp_path)
    rec = remember(db, "s", _encodings(PAYLOAD)["b64"], PAYLOAD,
                   source_refs=SRC, idempotency_key="named")
    out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
    assert out["status"] == "PARTIAL"
    meta = _latest_erase_meta(db)
    assert meta["erase_status"] == "PARTIAL"
    assert meta["append_only_table"] == "events"
    # P0-R10-02a/b: the refusal names the event holding the payload
    # (its id) — here the remember event whose metadata.key is the
    # encoded payload.
    remember_ev = db.execute(
        "SELECT id FROM events WHERE scope_id='s' AND event_type='remember'"
        ).fetchone()[0]
    assert meta["remaining"] == [
        {"table": "events", "record_id": rec["id"],
         "event_id": remember_ev, "key_redacted": True}]
    assert meta["erased"] is False
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is False
    assert any(r["record_id"] == rec["id"]
               for r in rep["controlled"]["controlled_residuals"])
    db.close()


# ---------------------------------------------------------------- P0-R9-03

def test_erase_cross_record_encoded_copy_is_partial_all_encodings(tmp_path):
    # GPT's A/B repro: B holds the whole value encoded; erasing A
    # must refuse and name B, for all six encodings.
    for name, enc in _encodings(PAYLOAD).items():
        db = _db(tmp_path, name=f"a3-{name}")
        ra = remember(db, "s", "kA", PAYLOAD, source_refs=SRC,
                      idempotency_key=f"a-{name}")
        rb = remember(db, "s", "kB", enc, source_refs=SRC,
                      idempotency_key=f"b-{name}")
        out = erase_record(db, "s", ra["id"], ra["revision"], "rotation")
        assert out["status"] == "PARTIAL", name
        rep = residual_report(db, "s")
        assert rep["controlled"]["live_tables_clean"] is False
        assert any(r["record_id"] == rb["id"]
                   for r in rep["controlled"]["controlled_residuals"])
        brow = db.execute("SELECT value_json, state FROM memories WHERE id=?",
                          (rb["id"],)).fetchone()
        assert brow[1] == "active" and enc in brow[0]
        db.close()


def test_cross_record_encoded_copy_short_distinct_forms(tmp_path):
    for name, enc in _encodings(SHORT).items():
        db = _db(tmp_path, name=f"a3s-{name}")
        ra = remember(db, "s", "kA", SHORT, source_refs=SRC,
                      idempotency_key=f"a-{name}")
        rb = remember(db, "s", "kB", enc, source_refs=SRC,
                      idempotency_key=f"b-{name}")
        out = erase_record(db, "s", ra["id"], ra["revision"], "rotation")
        assert out["status"] == "PARTIAL", (name, enc)
        rep = residual_report(db, "s")
        assert rep["controlled"]["live_tables_clean"] is False
        db.close()


def test_cross_record_encoded_copy_resolves_after_erasing_b(tmp_path):
    # The honest path out of the PARTIAL: erase the copy first, then
    # repair-erase A; the residual resolves and the export is clean.
    for name, enc in (("b64", _encodings(PAYLOAD)["b64"]),
                      ("urlsafe", _encodings(SHORT)["urlsafe"])):
        payload = PAYLOAD if name == "b64" else SHORT
        db = _db(tmp_path, name=f"resolve-{name}")
        ra = remember(db, "s", "kA", payload, source_refs=SRC,
                      idempotency_key=f"a-{name}")
        rb = remember(db, "s", "kB", enc, source_refs=SRC,
                      idempotency_key=f"b-{name}")
        out = erase_record(db, "s", ra["id"], ra["revision"], "rotation")
        assert out["status"] == "PARTIAL"
        outb = erase_record(db, "s", rb["id"], rb["revision"], "rotation")
        assert outb["status"] == "COMPLETE"
        outa = erase_record(db, "s", ra["id"], _rev(db, ra["id"]), "repair")
        assert outa["status"] == "COMPLETE"
        rep = residual_report(db, "s")
        assert rep["controlled"]["live_tables_clean"] is True
        assert rep["controlled"]["controlled_residuals"] == []
        _, recs, evs = _export_rep(db, tmp_path)
        assert enc not in recs and enc not in evs
        assert payload not in recs and payload not in evs
        db.close()


def test_cross_record_encoded_copy_export_excluded(tmp_path):
    # After A erases PARTIAL, the export already excludes B — the R8
    # hash check decodes B's value — for all six encodings, urlsafe
    # included (the one form that slipped through before the fix).
    for name, enc in _encodings(SHORT).items():
        db = _db(tmp_path, name=f"excl-{name}")
        ra = remember(db, "s", "kA", SHORT, source_refs=SRC,
                      idempotency_key=f"a-{name}")
        remember(db, "s", "kB", enc, source_refs=SRC,
                 idempotency_key=f"b-{name}")
        out = erase_record(db, "s", ra["id"], ra["revision"], "rotation")
        assert out["status"] == "PARTIAL"
        _, recs, _ = _export_rep(db, tmp_path)
        assert enc not in recs, name
        db.close()


def test_receipt_dump_encoded_value_is_detected(tmp_path):
    # A receipt dump whose "value" subtree holds the encoded payload
    # is a copy too (user-data subtrees are never exempt, R8) — unit
    # level on the structural check, plus the live A/B flow above
    # already exercises it through the remember receipts.
    cell = _json({"id": "run1", "records": [
        {"id": "rb", "key": "kB", "value": _encodings(PAYLOAD)["b64"],
         "created_at": LEGAL_CREATED}]})
    assert _cell_holds_needle(cell, PAYLOAD, "runs", "receipt_json") is True
    assert _value_contains_needle(_encodings(PAYLOAD)["hex"], {PAYLOAD}) is True
    plain = _json({"id": "run1", "records": [
        {"id": "rb", "key": "kB", "value": "plain note",
         "created_at": LEGAL_CREATED}]})
    assert _cell_holds_needle(plain, PAYLOAD, "runs", "receipt_json") is False
    assert _value_contains_needle("plain note", {PAYLOAD}) is False


# ------------------------------------------------------- "202" must stay COMPLETE

def test_decoded_match_length_floor():
    # The floor is part of the rule: a 1-char key "k" rot13-decodes
    # to a 1-char payload "x" by coincidence — that must NOT count,
    # or no 1-char payload could ever erase (R4 guard). "aa>" (3
    # chars) still matches its urlsafe form.
    assert _decoded_equals_needle("k", "x") is False
    assert _decoded_equals_needle("YWE-", "aa>") is True
    assert _decoded_equals_needle("YWE+", "aa>") is True
    assert _decoded_equals_needle("k202", "202") is False


def test_erase_short_needles_still_complete(tmp_path):
    # Whole-value decoded equality must not bring back the P2-R8-03
    # class: "202" / "2026" erase COMPLETE, pinned ids and a random
    # loop (the R8 file keeps the 200x loops; 25x here is the smoke).
    for val, expiry in (("202", LEGAL_TS), ("2026", "2027-01-01T00:00:00+00:00")):
        # "2026" does NOT use LEGAL_TS: set_expiry itself refuses a
        # >= 4-char payload that is a substring of the expiry timestamp
        # (pre-existing P0-R4-01 reason rule, unrelated to the scan),
        # so the "2026" case uses a 2027 expiry instead.
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


# ---------------------------------------------------------------- P1-R9-04

def test_export_report_redacts_raw_payload_key(tmp_path):
    db = _db(tmp_path)
    remember(db, "s", PAYLOAD, PAYLOAD, source_refs=SRC, idempotency_key="r1")
    rep, _, _ = _export_rep(db, tmp_path)
    assert len(rep["records_excluded_secrets"]) == 1
    entry = rep["records_excluded_secrets"][0]
    assert "key" not in entry and entry.get("key_redacted") is True
    assert PAYLOAD not in _json(rep)
    db.close()


def test_export_report_redacts_encoded_key(tmp_path):
    db = _db(tmp_path)
    enc = _encodings(PAYLOAD)["b64"]
    rec = remember(db, "s", enc, PAYLOAD, source_refs=SRC, idempotency_key="r2")
    out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
    assert out["status"] == "PARTIAL"
    rep, recs, _ = _export_rep(db, tmp_path)
    assert len(rep["records_excluded_secrets"]) == 1
    entry = rep["records_excluded_secrets"][0]
    assert "key" not in entry and entry.get("key_redacted") is True
    assert enc not in _json(rep) and PAYLOAD not in _json(rep)
    assert enc not in recs
    db.close()


def test_export_report_redacts_stripped_encoded_key(tmp_path):
    # The stripped surface: the key decodes to material glued into
    # the illegal timestamp — redaction cannot rely on the record's
    # own value needles alone (its value is a plain note).
    db = _db(tmp_path)
    enc = _encodings(PAYLOAD)["b64"]
    rec = remember(db, "s", enc, "plain note", source_refs=SRC,
                   idempotency_key="r3")
    db.execute("UPDATE memories SET valid_until=? WHERE id=?",
               ("x" + PAYLOAD + "y", rec["id"]))
    db.commit()
    rep, _, _ = _export_rep(db, tmp_path)
    assert len(rep["stripped_illegal_timestamps"]) == 1
    entry = rep["stripped_illegal_timestamps"][0]
    assert entry["fields"] == ["valid_until"]
    assert "key" not in entry and entry.get("key_redacted") is True
    assert enc not in _json(rep) and PAYLOAD not in _json(rep)
    db.close()


def test_export_report_keeps_benign_keys(tmp_path):
    # The rule is conditional: benign keys stay visible, so the
    # report still names its record (roundtrip:51,80 and R7:432,456,489).
    db = _db(tmp_path)
    remember(db, "s", "ci.api_key", "api_key=<redacted>", source_refs=SRC,
             idempotency_key="b1")
    remember(db, "s", "k1", "plain note", source_refs=SRC, idempotency_key="b2")
    db.execute("UPDATE memories SET valid_until=? WHERE key='k1'",
               ("not-a-timestamp",))
    db.commit()
    rep, _, _ = _export_rep(db, tmp_path)
    assert [e["key"] for e in rep["records_excluded_secrets"]] == ["ci.api_key"]
    assert {"key": "k1", "fields": ["valid_until"]} in rep["stripped_illegal_timestamps"]
    db.close()


def test_import_plan_redacts_quarantined_encoded_key(tmp_path):
    # The quarantine paths continue before the key validation, so the
    # report surface must redact itself.
    db = _db(tmp_path)
    pkg = _mk_package(tmp_path, [_rec("r1", _encodings(PAYLOAD)["b64"], PAYLOAD,
                                      parent_ids=["missing"])])
    plan = import_package(db, pkg, target_scope="s")
    assert len(plan["quarantined"]) == 1
    entry = plan["quarantined"][0]
    assert "key" not in entry and entry.get("key_redacted") is True
    assert entry["origin_id"] == "r1"
    assert _encodings(PAYLOAD)["b64"] not in _json(plan)
    assert PAYLOAD not in _json(plan)
    db.close()


def test_import_plan_redacts_skipped_encoded_key(tmp_path):
    # Skipped-existing: the key encodes a payload already erased in
    # the target scope, so the erased-hash direction must redact it.
    # P0-R11-02 conforming: the hash direction only fires while the
    # erase persisted hashes — a PARTIAL erase (a second record still
    # holds the payload). The COMPLETE-erasure case is covered by
    # test_import_plan_shows_key_after_complete_erase below.
    db = _db(tmp_path)
    rec = remember(db, "s", "kX", PAYLOAD, source_refs=SRC, idempotency_key="s1")
    remember(db, "s", "kY", PAYLOAD, source_refs=SRC, idempotency_key="s2")
    out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
    assert out["status"] == "PARTIAL"
    pkg = _mk_package(tmp_path, [_rec("rY", _encodings(PAYLOAD)["b64"], "other note")])
    first = import_package(db, pkg, target_scope="s")
    assert first.get("imported") == 1
    second = import_package(db, pkg, target_scope="s")
    assert len(second["skipped_existing"]) == 1
    entry = second["skipped_existing"][0]
    assert "key" not in entry and entry.get("key_redacted") is True
    assert entry["origin_id"] == "rY"
    assert _encodings(PAYLOAD)["b64"] not in _json(second)
    db.close()


def test_import_plan_shows_key_after_complete_erase(tmp_path):
    # P0-R11-02 trade-off, documented in KNOWN_LIMITATIONS §24: after
    # a COMPLETE erase there is no stored needle hash and no live
    # payload left, so an import-plan entry whose key encodes the
    # erased payload can no longer be recognised as payload material
    # — the key is shown, not redacted. The import itself still works
    # (the key is a new record's key, and the value "other note" is
    # not the payload). This is the price of making the payload
    # non-enumerable: nothing derived from it may persist, so nothing
    # can recognise a future copy of it.
    db = _db(tmp_path)
    rec = remember(db, "s", "kX", PAYLOAD, source_refs=SRC, idempotency_key="s1")
    out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
    assert out["status"] == "COMPLETE"
    pkg = _mk_package(tmp_path, [_rec("rY", _encodings(PAYLOAD)["b64"], "other note")])
    first = import_package(db, pkg, target_scope="s")
    assert first.get("imported") == 1
    second = import_package(db, pkg, target_scope="s")
    assert len(second["skipped_existing"]) == 1
    entry = second["skipped_existing"][0]
    assert entry.get("key") == _encodings(PAYLOAD)["b64"]
    assert entry.get("key_redacted") is not True
    db.close()


def test_cli_export_stdout_has_no_payload(tmp_path):
    # The CLI prints the export return object directly — fixing the
    # return fixes the CLI (verification A4-cli, inverted).
    dbpath = tmp_path / "cli.sqlite3"
    db = open_db(dbpath)
    register_scope(db, "s")
    remember(db, "s", PAYLOAD, PAYLOAD, source_refs=SRC, idempotency_key="cli")
    db.close()
    proc = subprocess.run(
        [sys.executable, "-m", "turnkeep", "--db", str(dbpath),
         "export", "--scope", "s", "--out", str(tmp_path / "exp-cli")],
        capture_output=True, text=True, timeout=60, env=ENV, encoding="utf-8")
    assert proc.returncode == 0
    assert PAYLOAD not in proc.stdout
    assert _encodings(PAYLOAD)["b64"] not in proc.stdout


def test_export_report_has_no_reversible_encoding(tmp_path):
    # One sweep: no encoding of the payload anywhere in the report.
    db = _db(tmp_path)
    enc = _encodings(PAYLOAD)["urlsafe"]
    rec = remember(db, "s", enc, PAYLOAD, source_refs=SRC, idempotency_key="r4")
    erase_record(db, "s", rec["id"], rec["revision"], "rotation")
    rep, _, _ = _export_rep(db, tmp_path)
    blob = _json(rep)
    for form in _encoded_forms(PAYLOAD):
        assert form not in blob, form
    db.close()


# ------------------------------------------------------- retained: runs.task

def test_runs_task_encoding_still_retained(tmp_path):
    # No code action (KNOWN_LIMITATIONS §8): an encoding in free task
    # text has no value boundary, so whole-value decoded equality
    # cannot apply there. The encoding stays in the live DB's
    # runs.task, the residual still says clean, and it is NOT in the
    # export (runs are not exported).
    db = _db(tmp_path)
    rec = remember(db, "s", "k1", PAYLOAD, source_refs=SRC, idempotency_key="d1")
    begin(db, "s", f"revoke {_encodings(PAYLOAD)['b64']} now",
          idempotency_key="d-run")
    out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
    assert out["status"] == "COMPLETE"
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is True
    task = db.execute("SELECT task FROM runs WHERE scope_id='s'").fetchone()[0]
    assert _encodings(PAYLOAD)["b64"] in task
    _, recs, evs = _export_rep(db, tmp_path)
    assert _encodings(PAYLOAD)["b64"] not in recs
    assert _encodings(PAYLOAD)["b64"] not in evs
    db.close()
