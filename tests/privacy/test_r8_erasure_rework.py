"""R8 rework regression tests: P0-R8-01 (complete key / source-ID
persistence policy), P0-R8-02 (structural-path timestamp exemption),
P1 (receipt source_refs residual tracking), P2-R8-03 (short-needle
identifier false positives, fixed at the product level).

The unified rule behind items 1 and 4 (report §2 A1d): a whole value
EQUAL to the payload (or its encoding) is a copy and must be handled;
a random id that merely CONTAINS a short needle substring is not a
copy and must be allowed through. The erase scan, the import gate,
the receipt/snapshot scrub, and the export exclusion all apply that
one rule — no gate uses substring matching while another uses
equality.

Written BEFORE the fix where feasible: the import-gate cases fail on
the pre-fix code (no validation on key / source ids at all); the
legacy key/source-id chain cases erase COMPLETE while the secret stays
in the DB, the residual report, records.jsonl and events.jsonl; the
timestamp-exemption A/B case erases COMPLETE while record B still
holds the needle; the receipt source_refs case reports clean while the
receipt still holds the payload id; the key-contains-"202" case erases
PARTIAL on a random identifier substring.
"""
import base64
import codecs
import hashlib
import json

import pytest

from turnkeep.storage import open_db, _json, _now, insert_record
from turnkeep.scopes import register_scope
from turnkeep.memory import remember
from turnkeep.retention import set_expiry
from turnkeep.errors import TurnkeepError
from turnkeep.erasure import (
    erase_record, residual_report, _cell_holds_needle,
    _receipt_holds_value, _stored_needle_hashes,
    _identifier_matches_hashes, _record_verification_keys,
)
from turnkeep.portability import export_scope, import_package
from turnkeep.runs import begin

SRC = [{"id": "u:1", "type": "user_statement"}]
SECRET = "sk-TESTSECRET-abcdef"
TOMBSTONE = {"_erased": True}
LEGAL_TS = "2026-11-12T22:00:00+00:00"
LEGAL_CREATED = "2026-11-11T00:00:00+00:00"


def _db(tmp_path, scope="s"):
    db = open_db(tmp_path / "t.sqlite3")
    register_scope(db, scope)
    return db


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


def _export_text(db, tmp_path, scope="s", name="exp"):
    out = tmp_path / name
    export_scope(db, scope, out)
    return ((out / "records.jsonl").read_text(encoding="utf-8"),
            (out / "events.jsonl").read_text(encoding="utf-8"))


def _legacy_record(db, rid, key, value, sources=(), scope="s"):
    """A record as a pre-fix import left it: present in memories +
    sources, with NO remember event (so no pre-erase history carries
    the key — the import event itself never recorded keys). Built with
    insert_record directly, so no event row is published."""
    insert_record(db, scope, key, _json(value), "fact", list(sources), [],
                  {}, state="active", cause_event_id="legacy-import",
                  record_id=rid)
    return rid


def _b64(value):
    return base64.b64encode(value.encode()).decode()


def _assert_chain_clean(db, tmp_path, scope="s"):
    """The full post-erase assertion: DB, residual report, records.jsonl
    and events.jsonl must ALL be free of the payload (P0-R8-01 1e)."""
    rep = residual_report(db, scope)
    assert rep["controlled"]["live_tables_clean"] is True
    assert rep["controlled"]["controlled_residuals"] == []
    assert SECRET not in _json(rep)
    recs, evs = _export_text(db, tmp_path, scope)
    assert SECRET not in recs, "export records.jsonl still holds the payload"
    assert SECRET not in evs, "export events.jsonl still holds the payload"
    assert _b64(SECRET) not in recs and _b64(SECRET) not in evs
    # Live values must be clean everywhere. Live KEYS must be clean too —
    # except a tombstone row's key: the tombstoned row keeps its key by
    # design (a stable identifier; the record must stay addressable),
    # and the key never leaves the live DB — the erase event redacts it
    # and the export excludes the row. That is the documented rule, not
    # a gap: see erasure._PAYLOAD_COLUMNS and KNOWN_LIMITATIONS §21.
    live = db.execute(
        "SELECT value_json, key, state FROM memories WHERE scope_id=?", (scope,)).fetchall()
    for value_json, key, state in live:
        assert SECRET not in value_json
        if state != "erased":
            assert SECRET not in key
    # Sources / links: an ERASED record's own links must be gone, but a
    # LIVE record's link to the same source id survives by design (its
    # record is excluded from export instead — the cross-record rule).
    # So a sources row with id == payload is allowed only while a live
    # record still cites it.
    erased_ids = {r[0] for r in db.execute(
        "SELECT id FROM memories WHERE scope_id=? AND state='erased'", (scope,)).fetchall()}
    links = db.execute(
        "SELECT memory_id, source_id FROM memory_sources WHERE scope_id=?", (scope,)).fetchall()
    for memory_id, source_id in links:
        if memory_id in erased_ids:
            assert source_id != SECRET
    live_citers = {m for m, s in links if m not in erased_ids and s == SECRET}
    srcs = db.execute(
        "SELECT id FROM sources WHERE scope_id=?", (scope,)).fetchall()
    for (sid,) in srcs:
        if sid == SECRET:
            assert live_citers, "orphaned source id == payload must be deleted"
    return recs, evs


# ---------------------------------------------------------------- P0-R8-01d: import gate

@pytest.mark.parametrize("field", ["key", "source_id", "source_id_b64", "key_b64"])
def test_import_identifier_equals_payload_rejected(tmp_path, field):
    # P0-R8-01d: import validated key / source ids not at all — a
    # package could plant the secret as an identifier and a later
    # erase still reported COMPLETE. Pre-fix: imported=1.
    db = _db(tmp_path)
    if field == "key":
        rec = _rec("o1", SECRET, {"api_key": SECRET})
    elif field == "key_b64":
        rec = _rec("o1", _b64(SECRET), {"api_key": SECRET})
    elif field == "source_id":
        rec = _rec("o1", "k1", {"api_key": SECRET},
                   source_refs=[{"id": SECRET, "type": "user_statement"}])
    else:
        rec = _rec("o1", "k1", {"api_key": SECRET},
                   source_refs=[{"id": _b64(SECRET), "type": "user_statement"}])
    pkg = _mk_package(tmp_path, [rec])
    with pytest.raises(TurnkeepError) as ei:
        import_package(db, pkg, target_scope="s")
    assert ei.value.code == "INVALID_INPUT"
    assert db.execute("SELECT COUNT(*) FROM memories WHERE scope_id='s'").fetchone()[0] == 0
    db.close()


def test_import_identifier_containing_payload_substring_allowed(tmp_path):
    # The unified rule, other side: a key that merely CONTAINS the
    # payload as a substring is NOT rejected at import (whole-value
    # equality only) — the documented residual in KNOWN_LIMITATIONS
    # §21, applied consistently, not a gate that pretends otherwise.
    db = _db(tmp_path)
    pkg = _mk_package(tmp_path, [_rec("o1", "prefix-" + SECRET + "-suffix",
                                      {"note": "plain"})])
    plan = import_package(db, pkg, target_scope="s")
    assert plan["imported"] == 1
    db.close()


# ---------------------------------------------------------------- P0-R8-01a/b/c/e: key == payload chain

def test_erase_key_equals_payload_chain_clean(tmp_path):
    # P0-R8-01(a): the erase event itself wrote the raw secret into
    # metadata.key (the scan runs before the event is published, so it
    # can never see this event's own key). Pre-fix: erase COMPLETE,
    # residual clean — but events.jsonl held the secret in the erase
    # event's metadata.key, and records.jsonl held it in the tombstone
    # row's key. Post-fix the key is omitted (key_redacted) and the
    # tombstone row is excluded from export.
    db = _db(tmp_path)
    _legacy_record(db, "r1", SECRET, {"api_key": SECRET}, sources=SRC)
    begin(db, "s", task="check key record", idempotency_key="run1")
    rev = db.execute("SELECT revision FROM memories WHERE scope_id='s' AND id='r1'").fetchone()[0]
    out = erase_record(db, "s", "r1", rev, "rotation")
    assert out["status"] == "COMPLETE" and out["erased"] is True
    assert out["remaining"] == {}
    # The erase event must not carry the raw key.
    ev_meta = db.execute(
        "SELECT metadata_json FROM events WHERE scope_id='s' AND event_type='erase'"
        " ORDER BY scope_epoch DESC LIMIT 1").fetchone()[0]
    meta = json.loads(ev_meta)
    assert "key" not in meta and meta.get("key_redacted") is True
    assert SECRET not in ev_meta
    # The run snapshot that listed the record must have the key replaced.
    snap = db.execute("SELECT snapshot_json FROM runs WHERE scope_id='s'").fetchone()[0]
    assert SECRET not in snap
    _assert_chain_clean(db, tmp_path)
    db.close()


def test_erase_source_id_equals_payload_chain_clean(tmp_path):
    # P0-R8-01(b): a source id equal to the payload survived erasure in
    # sources / memory_sources and in the receipt dump, and was exported
    # verbatim. Pre-fix: erase COMPLETE, residual clean, export leaked.
    db = _db(tmp_path)
    rec = remember(db, "s", "k1", {"api_key": SECRET},
                   source_refs=[{"id": SECRET, "type": "user_statement"}],
                   idempotency_key="k1")
    rev = db.execute("SELECT revision FROM memories WHERE scope_id='s' AND id=?",
                     (rec["id"],)).fetchone()[0]
    out = erase_record(db, "s", rec["id"], rev, "rotation")
    assert out["status"] == "COMPLETE" and out["erased"] is True
    assert out["remaining"] == {}
    assert out["cleared_source_ids"] >= 1
    assert out["scrubbed_identifiers"] >= 1
    # The receipt dump's source-ref id is replaced, not the raw secret.
    for (raw,) in db.execute(
            "SELECT response_json FROM idempotency WHERE scope_id='s'").fetchall():
        assert SECRET not in raw
    _assert_chain_clean(db, tmp_path)
    db.close()


def test_erase_source_id_base64_payload_chain_clean(tmp_path):
    # P0-R8-01(c): exact-equality checks cannot stop an encoding — the
    # source id was the Base64 of the payload. Pre-fix: erase COMPLETE,
    # residual clean, export held the decodable secret.
    db = _db(tmp_path)
    rec = remember(db, "s", "k1", {"api_key": SECRET},
                   source_refs=[{"id": _b64(SECRET), "type": "user_statement"}],
                   idempotency_key="k1")
    rev = db.execute("SELECT revision FROM memories WHERE scope_id='s' AND id=?",
                     (rec["id"],)).fetchone()[0]
    out = erase_record(db, "s", rec["id"], rev, "rotation")
    assert out["status"] == "COMPLETE" and out["erased"] is True
    assert out["remaining"] == {}
    assert out["cleared_source_ids"] >= 1
    _assert_chain_clean(db, tmp_path)
    db.close()


def test_erase_cross_record_source_id_excluded_from_export(tmp_path):
    # P0-R8-01(c) cross-record (same construction as the R8 verification
    # repro: import, so no receipt holds either record): X's value is
    # the payload, Y's source id is the payload. Erasing X is PARTIAL
    # since the R10 rework (P0-R10-02, item 2d): the sources table is
    # now scanned, so Y's live link — a controlled copy of the
    # payload — is honestly reported as a residual naming Y. Y's live
    # link survives (only the erased record's own links are cleared)
    # but Y itself is excluded from the export, so the payload cannot
    # leave through Y.
    db = _db(tmp_path)
    pkg = _mk_package(tmp_path, [
        _rec("rx", "kx", {"api_key": SECRET}),
        _rec("ry", "ky", {"note": "other"},
             source_refs=[{"id": SECRET, "type": "user_statement"}]),
    ])
    plan = import_package(db, pkg, target_scope="s")
    assert plan["imported"] == 2
    xrid = db.execute("SELECT id FROM memories WHERE scope_id='s' AND key='kx'").fetchone()[0]
    yrid = db.execute("SELECT id FROM memories WHERE scope_id='s' AND key='ky'").fetchone()[0]
    rev = db.execute("SELECT revision FROM memories WHERE scope_id='s' AND id=?",
                     (xrid,)).fetchone()[0]
    out = erase_record(db, "s", xrid, rev, "rotation")
    assert out["status"] == "PARTIAL" and out["erased"] is True
    # The terminal return's remaining maps table -> rowids; the
    # sources rowid is the row whose id IS the payload.
    assert "sources" in out["remaining"]
    src_rowid = db.execute(
        "SELECT rowid FROM sources WHERE scope_id='s' AND id=?",
        (SECRET,)).fetchone()[0]
    assert src_rowid in out["remaining"]["sources"]
    # Y's live link survives (only the erased record's own links are
    # cleared); Y itself is excluded from the export.
    assert db.execute(
        "SELECT 1 FROM memory_sources WHERE scope_id='s' AND memory_id=? AND source_id=?",
        (yrid, SECRET)).fetchone() is not None
    # The residual report honestly lists the sources residual now, so
    # the full chain-clean helper (which requires a clean report) does
    # not apply here; check the report and the export directly.
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is False
    assert any(r.get("record_id") == yrid
               for r in rep["controlled"]["controlled_residuals"])
    recs, evs = _export_text(db, tmp_path)
    assert SECRET not in recs and SECRET not in evs
    assert _b64(SECRET) not in recs and _b64(SECRET) not in evs
    assert "ky" not in recs, "Y (source id == erased payload) must be excluded from export"
    db.close()


def test_erase_cross_record_receipt_holds_payload_is_partial(tmp_path):
    # The harder cross-record variant: Y was remembered with an
    # idempotency receipt, so Y's receipt dump holds the source id ==
    # payload as a live derived copy. Erasing X is then PARTIAL — the
    # honest refusal, same as a second live record holding the payload
    # as its value (P0-R2-01 / P0-3): the receipt is a copy the product
    # controls and has not cleaned, so COMPLETE would be a lie. The
    # export still excludes Y (the PARTIAL erase event persists the
    # needle hashes), so no channel leaks.
    db = _db(tmp_path)
    x = remember(db, "s", "kx", {"api_key": SECRET}, source_refs=SRC,
                 idempotency_key="kx")
    y = remember(db, "s", "ky", {"note": "other"},
                 source_refs=[{"id": SECRET, "type": "user_statement"}],
                 idempotency_key="ky")
    rev = db.execute("SELECT revision FROM memories WHERE scope_id='s' AND id=?",
                     (x["id"],)).fetchone()[0]
    out = erase_record(db, "s", x["id"], rev, "rotation")
    assert out["status"] == "PARTIAL"
    assert "idempotency" in out["remaining"]
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is False
    recs, evs = _export_text(db, tmp_path)
    assert SECRET not in recs and SECRET not in evs
    assert "ky" not in recs
    db.close()


def test_erase_tombstone_row_export_excluded(tmp_path):
    # P0-R8-01(c) tombstone-blindness: the R7 exclusion check used the
    # tombstone row's own (empty) needles, so its key/source_refs were
    # exported verbatim. Post-fix the erased-payload hashes exclude it.
    db = _db(tmp_path)
    _legacy_record(db, "r1", SECRET, {"api_key": SECRET},
                   sources=[{"id": SECRET, "type": "user_statement"}])
    rev = db.execute("SELECT revision FROM memories WHERE scope_id='s' AND id='r1'").fetchone()[0]
    out = erase_record(db, "s", "r1", rev, "rotation")
    assert out["status"] == "COMPLETE"
    recs, evs = _assert_chain_clean(db, tmp_path)
    assert "r1" not in recs, "tombstone row must not be exported"
    db.close()


# ---------------------------------------------------------------- P1: receipt source_refs tracking

def test_receipt_holds_value_sees_source_refs(tmp_path):
    # P1: _receipt_holds_value only checked the receipt's "value",
    # ignoring source_refs — so an erase that was PARTIAL over a
    # source-ref id still reported clean. The tracking function itself
    # must see the identifier: build the dirty state directly (a
    # receipt dump owned by the erased record, value already tombstoned
    # but source-ref id still the payload) and assert it reports held.
    # P0-R11-02 conforming: the identifier extension matches against
    # the stored needle hashes, which a COMPLETE erase no longer
    # persists — so the extension is exercised on a PARTIAL erase (a
    # second record still holds the payload, so the hashes are
    # persisted). The COMPLETE-erasure blind spot (a post-erase
    # re-introduction into a receipt is undetectable once the payload
    # and its hashes are gone) is covered by
    # test_receipt_holds_value_blind_after_complete_erase below, and
    # documented in KNOWN_LIMITATIONS §24.
    db = _db(tmp_path)
    rec = remember(db, "s", "k1", {"api_key": SECRET}, source_refs=SRC,
                   idempotency_key="k1")
    remember(db, "s", "k2", {"api_key": SECRET}, source_refs=SRC,
             idempotency_key="k2")
    rev = db.execute("SELECT revision FROM memories WHERE scope_id='s' AND id=?",
                     (rec["id"],)).fetchone()[0]
    out = erase_record(db, "s", rec["id"], rev, "rotation")
    assert out["status"] == "PARTIAL"
    assert _receipt_holds_value(db, "s", rec["id"]) is False
    # Re-dirty one receipt dump by hand: value tombstoned, source id raw.
    # Pick the idempotency row whose receipt is owned by this record.
    row = None
    for (rowid, raw) in db.execute(
            "SELECT rowid, response_json FROM idempotency WHERE scope_id='s'").fetchall():
        if f'"{rec["id"]}"' in raw:
            row = (rowid, raw)
            break
    assert row is not None
    data = json.loads(row[1])
    data["source_refs"] = [{"id": SECRET, "type": "user_statement"}]
    db.execute("UPDATE idempotency SET response_json=? WHERE rowid=?",
               (_json(data), row[0]))
    db.commit()
    assert _receipt_holds_value(db, "s", rec["id"]) is True
    # And the repair erase cleans it: the repair runs on the stored
    # hashes (PARTIAL, so they exist). The repair itself reports
    # COMPLETE — its needles are reconstructed from this record's own
    # receipts (_needles_from_receipts), which are already tombstoned,
    # so the scan is about this record's own residuals, not the second
    # record's live copy: that copy is not this record's residual to
    # clean, and it stays tracked by the FIRST erase's PARTIAL outcome
    # (residual_report still lists it — the repair's COMPLETE does
    # not hide it).
    out2 = erase_record(db, "s", rec["id"], out["revision"], "repair")
    assert out2["status"] == "COMPLETE"
    assert _receipt_holds_value(db, "s", rec["id"]) is False
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is False
    assert rep["status"] == "CONTROLLED_NOT_CLEAN"
    rev2 = db.execute("SELECT revision FROM memories WHERE scope_id='s' AND key='k2'").fetchone()[0]
    rid2 = db.execute("SELECT id FROM memories WHERE scope_id='s' AND key='k2'").fetchone()[0]
    out3 = erase_record(db, "s", rid2, rev2, "rotation")
    assert out3["status"] == "COMPLETE"
    _assert_chain_clean(db, tmp_path)


def test_receipt_holds_value_blind_after_complete_erase(tmp_path):
    # P0-R11-02 trade-off, documented in KNOWN_LIMITATIONS §24: after
    # a COMPLETE erase there is no stored needle hash and no live
    # payload left, so a copy re-introduced into a receipt AFTER the
    # erase (here planted by hand, as an out-of-band DB write) cannot
    # be detected — the receipt's value is tombstoned (so the value
    # check is clean) and the identifier extension has no hashes to
    # match the re-introduced id against. The repair erase likewise
    # cannot see or clean it, and still reports COMPLETE. This is the
    # price of making the payload non-enumerable: nothing derived from
    # it may persist, so nothing can recognise a future copy of it.
    # In normal operation nothing re-dirties a receipt after a
    # COMPLETE erase (the repair path has no needles to re-introduce
    # with); the scenario needs a direct DB write by the DB holder,
    # who is inside the trust boundary.
    db = _db(tmp_path)
    rec = remember(db, "s", "k1", {"api_key": SECRET}, source_refs=SRC,
                   idempotency_key="k1")
    rev = db.execute("SELECT revision FROM memories WHERE scope_id='s' AND id=?",
                     (rec["id"],)).fetchone()[0]
    out = erase_record(db, "s", rec["id"], rev, "rotation")
    assert out["status"] == "COMPLETE"
    assert _receipt_holds_value(db, "s", rec["id"]) is False
    row = db.execute(
        "SELECT rowid, response_json FROM idempotency WHERE scope_id='s'").fetchone()
    data = json.loads(row[1])
    data["source_refs"] = [{"id": SECRET, "type": "user_statement"}]
    db.execute("UPDATE idempotency SET response_json=? WHERE rowid=?",
               (_json(data), row[0]))
    db.commit()
    assert _receipt_holds_value(db, "s", rec["id"]) is False
    out2 = erase_record(db, "s", rec["id"], out["revision"], "repair")
    assert out2["status"] == "COMPLETE"
    # The re-introduced copy survives: the repair had nothing to
    # match it against. Documented, not hidden.
    row = db.execute(
        "SELECT response_json FROM idempotency WHERE scope_id='s'").fetchone()
    assert SECRET in row[0]
    db.close()
    db.close()


def test_repair_erase_cleans_key_equal_payload(tmp_path):
    # Repair path with stored hashes: a legacy record whose key equals
    # the payload, erased once (COMPLETE), then repaired — the repair
    # event must not re-publish the raw key either.
    db = _db(tmp_path)
    _legacy_record(db, "r1", SECRET, {"api_key": SECRET}, sources=SRC)
    rev = db.execute("SELECT revision FROM memories WHERE scope_id='s' AND id='r1'").fetchone()[0]
    out = erase_record(db, "s", "r1", rev, "rotation")
    assert out["status"] == "COMPLETE"
    out2 = erase_record(db, "s", "r1", out["revision"], "repair")
    assert out2["status"] == "COMPLETE"
    ev_meta = db.execute(
        "SELECT metadata_json FROM events WHERE scope_id='s' AND event_type='erase'"
        " ORDER BY scope_epoch DESC LIMIT 1").fetchone()[0]
    assert SECRET not in ev_meta
    assert json.loads(ev_meta).get("key_redacted") is True
    _assert_chain_clean(db, tmp_path)
    db.close()


# ---------------------------------------------------------------- P0-R8-02: structural timestamp exemption

def test_timestamp_exemption_misfire_on_user_data(tmp_path):
    # GPT's A/B repro: record A value "2026-11-12"; record B value_json
    # {"created_at": "2026-11-12T22:00:00+00:00"}. B's created_at is
    # ordinary user-stored JSON content, not a DB audit timestamp — but
    # the R7 scan exempted it by field NAME alone, so A's erase returned
    # COMPLETE while B still held the full needle. Post-fix the
    # exemption is structural: memories.value_json is never exempt, so
    # the erase is PARTIAL and names B.
    db = _db(tmp_path)
    a = remember(db, "s", "ka", "2026-11-12", source_refs=SRC, idempotency_key="ka")
    b = remember(db, "s", "kb", {"created_at": "2026-11-12T22:00:00+00:00"},
                 source_refs=SRC, idempotency_key="kb")
    rev = db.execute("SELECT revision FROM memories WHERE scope_id='s' AND id=?",
                     (a["id"],)).fetchone()[0]
    out = erase_record(db, "s", a["id"], rev, "rotation")
    # PARTIAL at step 3b (copies remain in another live record): the
    # record itself IS tombstoned (erased=True), but the erase is
    # refused as incomplete because B still holds the needle. Only the
    # step-1 append-only PARTIAL leaves erased=False.
    assert out["status"] == "PARTIAL" and out["erased"] is True
    remaining = out["remaining"]
    assert "memories" in remaining
    b_rowid = db.execute(
        "SELECT rowid FROM memories WHERE scope_id='s' AND id=?",
        (b["id"],)).fetchone()[0]
    assert b_rowid in remaining["memories"], "B (user-data created_at) must be named"
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is False
    db.close()


def test_timestamp_exemption_nested_and_control(tmp_path):
    # Nested created_at inside user data is still user data (PARTIAL);
    # the same timestamp under a non-timestamp key is a hit too
    # (control — it never had an exemption to lose).
    for key, value in (
            ("nested", {"outer": {"created_at": "2026-11-12T22:00:00+00:00"}}),
            ("control", {"note": "2026-11-12T22:00:00+00:00"})):
        db = _db(tmp_path / key)
        a = remember(db, "s", "ka", "2026-11-12", source_refs=SRC, idempotency_key="ka")
        remember(db, "s", "kb", value, source_refs=SRC, idempotency_key="kb")
        rev = db.execute("SELECT revision FROM memories WHERE scope_id='s' AND id=?",
                         (a["id"],)).fetchone()[0]
        out = erase_record(db, "s", a["id"], rev, "rotation")
        assert out["status"] == "PARTIAL", key
        assert out["remaining"].get("memories"), key
        db.close()


def test_timestamp_exemption_receipt_value_subtree(tmp_path):
    # A receipt dump's top-level created_at/updated_at/valid_until are
    # real audit timestamps (exempt); the dump's "value" subtree is
    # user data and is NEVER exempt — even when it carries a created_at
    # field holding the needle.
    db = _db(tmp_path)
    a = remember(db, "s", "ka", "2026-11-12", source_refs=SRC, idempotency_key="ka")
    b = remember(db, "s", "kb", {"created_at": "2026-11-12T22:00:00+00:00"},
                 source_refs=SRC, idempotency_key="kb")
    begin(db, "s", task="check kb", idempotency_key="run1")
    rev = db.execute("SELECT revision FROM memories WHERE scope_id='s' AND id=?",
                     (a["id"],)).fetchone()[0]
    out = erase_record(db, "s", a["id"], rev, "rotation")
    # Step-3b PARTIAL (see the A/B test): A is tombstoned, but B's
    # receipt dump still holds the needle in its "value" subtree.
    assert out["status"] == "PARTIAL" and out["erased"] is True
    assert out["remaining"], "the receipt copy must be named"
    db.close()


def test_timestamp_exemption_audit_positions_still_exempt(tmp_path):
    # The other direction, kept from R7: a needle that is a substring
    # of the record's OWN legal expiry timestamp (events metadata
    # valid_until) is still not a copy — erasing "202" COMPLETEs.
    db = _db(tmp_path)
    rec = remember(db, "s", "k1", "202", source_refs=SRC, idempotency_key="k1")
    set_expiry(db, "s", rec["id"], LEGAL_TS, rec["revision"])
    rev = db.execute("SELECT revision FROM memories WHERE scope_id='s' AND id=?",
                     (rec["id"],)).fetchone()[0]
    out = erase_record(db, "s", rec["id"], rev, "rotation")
    assert out["status"] == "COMPLETE" and out["erased"] is True
    assert out["remaining"] == {}
    db.close()


# ---------------------------------------------------------------- P2-R8-03: identifier rule

def test_identifier_whole_value_rule_unit(tmp_path):
    # The scan distinguishes identifier fields from payload fields by
    # structural path. A receipt dump whose cause_event_id merely
    # CONTAINS "202" is not a hit; one whose cause_event_id IS "202" is.
    cell_with_substring = _json({
        "id": "abc", "key": "k1", "cause_event_id": "88e72202af49f077f",
        "value": {"note": "plain"}, "created_at": LEGAL_CREATED})
    cell_with_equality = _json({
        "id": "abc", "key": "k1", "cause_event_id": "202",
        "value": {"note": "plain"}, "created_at": LEGAL_CREATED})
    assert _cell_holds_needle(cell_with_substring, "202", "idempotency", "response_json") is False
    assert _cell_holds_needle(cell_with_equality, "202", "idempotency", "response_json") is True
    # And a key that merely contains "202" as a substring is not a hit.
    cell_key_substring = _json({"key": "k202x", "reason": "rotation"})
    assert _cell_holds_needle(cell_key_substring, "202", "events", "metadata_json") is False


def test_erase_key_containing_short_needle_completes(tmp_path):
    # End-to-end version of the unit rule: a record whose KEY contains
    # "202" as a substring (but is not the payload) erases COMPLETE.
    # Pre-fix the R7 text scan matched the remember event's metadata
    # key and refused PARTIAL.
    db = _db(tmp_path)
    rec = remember(db, "s", "k202x", "202", source_refs=SRC, idempotency_key="k1")
    set_expiry(db, "s", rec["id"], LEGAL_TS, rec["revision"])
    # NOTE: the run task must not itself contain the needle — runs.task
    # is free text, scanned raw (the documented runs.task channel).
    begin(db, "s", task="check record", idempotency_key="run1")
    rev = db.execute("SELECT revision FROM memories WHERE scope_id='s' AND id=?",
                     (rec["id"],)).fetchone()[0]
    out = erase_record(db, "s", rec["id"], rev, "rotation")
    assert out["status"] == "COMPLETE" and out["erased"] is True
    assert out["remaining"] == {}
    db.close()


def test_erase_value_202_receipt_random_ids_loop(tmp_path):
    # P2-R8-03 strongest evidence: 200 fresh runs with RANDOM record
    # ids (no pinning) through the receipt-holding scenario — the loop
    # exercises 200 fresh random cause_event_ids, which is where the
    # "202" substring actually came from (verified: a "202" substring
    # at an arbitrary position in a random uuid in the receipt, NOT a
    # record id starting "2020" as GPT claimed).
    for i in range(200):
        db = _db(tmp_path / f"run{i}")
        rec = remember(db, "s", "k1", "202", source_refs=SRC, idempotency_key="k1")
        set_expiry(db, "s", rec["id"], LEGAL_TS, rec["revision"])
        begin(db, "s", task="check k1", idempotency_key="run1")
        rev = db.execute("SELECT revision FROM memories WHERE scope_id='s' AND id=?",
                         (rec["id"],)).fetchone()[0]
        out = erase_record(db, "s", rec["id"], rev, "rotation")
        assert out["status"] == "COMPLETE" and out["erased"] is True
        assert out["remaining"] == {}
        db.close()


# ---------------------------------------------------------------- identifiers: hashes + decoders

def test_identifier_matches_hashes_uses_decoded_forms(tmp_path):
    # The export/scrub hash check matches an identifier in its decoded
    # forms too (Base64 of the payload hashes to the erased hash), and
    # never matches a random id that merely contains a short needle.
    # P0-R11-02 conforming: a COMPLETE erase persists no needle
    # hashes, so the hash check is exercised on a PARTIAL erase (a
    # second record still holds the payload, so the hashes are
    # persisted — and the repair / residual / export paths still need
    # them). The decoded-forms property itself is unchanged.
    db = _db(tmp_path)
    rec = remember(db, "s", "k1", {"api_key": SECRET}, source_refs=SRC,
                   idempotency_key="k1")
    remember(db, "s", "k2", {"api_key": SECRET}, source_refs=SRC,
             idempotency_key="k2")
    rev = db.execute("SELECT revision FROM memories WHERE scope_id='s' AND id=?",
                     (rec["id"],)).fetchone()[0]
    out = erase_record(db, "s", rec["id"], rev, "rotation")
    assert out["status"] == "PARTIAL"
    stored = _stored_needle_hashes(db, "s", rec["id"])
    assert stored, "a PARTIAL erase must persist needle hashes"
    # P0-R12-01: the hashes are keyed by the record's own
    # verification key (per-record key, created at its first erase),
    # not a scope key — a fresh DB has no scope key at all.
    keys = _record_verification_keys(db, "s", rec["id"])
    assert _identifier_matches_hashes(_b64(SECRET), stored, keys) is True
    assert _identifier_matches_hashes(SECRET, stored, keys) is True
    assert _identifier_matches_hashes("88e72202af49f077f", stored, keys) is False
    db.close()
