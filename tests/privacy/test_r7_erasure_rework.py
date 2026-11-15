"""R7 rework regression tests: P0-R7-01 (import timestamp-field
validation + legacy illegal-timestamp erasure/export), P1-R7-02
(timezone-aware sweep_expired), P2-R7-03 (_check_valid_until type
check).

Written BEFORE the fix where feasible: the import-encoding cases fail
on the pre-fix code (created_at/updated_at accepted with no validation
at all); the legacy cases erase COMPLETE while the secret stays in the
tombstoned row and the export; the "202"/"2026" cases erase PARTIAL
(the erase scan matches a needle inside a legal timestamp held in
expire_set event metadata / receipt dumps); the sweep cases come out
exactly reversed; _check_valid_until(123) raises a raw TypeError.

The fix is value-aware, not substring-based: a timestamp field is a
residual only when it is NOT a legal timestamp (ISO-8601 with a
timezone) or when it EQUALS a payload needle. A legal timestamp that
merely CONTAINS a short needle as a substring ("2026-…" contains
"202") is legitimate expiry text, not a copy of the payload.
"""
import base64
import codecs
import hashlib
import json

import pytest

from turnkeep.storage import open_db, _json, _now
from turnkeep.scopes import register_scope
from turnkeep.memory import remember
from turnkeep.retention import set_expiry, sweep_expired, _check_valid_until
from turnkeep.errors import TurnkeepError
from turnkeep.erasure import erase_record, residual_report
from turnkeep.portability import export_scope, import_package
from turnkeep.runs import begin

SRC = [{"id": "u:1", "type": "user_statement"}]
SECRET = "SYNTHETIC-SECRET-9d4c1a7e-r7-do-not-use"
TOMBSTONE = {"_erased": True}
LEGAL_TS = "2026-11-12T22:00:00+00:00"
LEGAL_CREATED = "2026-11-11T00:00:00+00:00"


def _db(tmp_path, scope="s"):
    db = open_db(tmp_path / "t.sqlite3")
    register_scope(db, scope)
    return db


def _encodings():
    return [
        ("base64", base64.b64encode(SECRET.encode()).decode()),
        ("percent-char-by-char",
         "".join(f"%{b:02x}" for b in SECRET.encode())),
        ("reversed", SECRET[::-1]),
        ("hex", SECRET.encode().hex()),
        ("rot13", codecs.encode(SECRET, "rot_13")),
    ]


def _mk_package(tmp_path, records, scope="s"):
    """Hand-build an export package dir with correct manifest digests."""
    d = tmp_path / "pkg"
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


def _export_text(db, tmp_path, scope="s"):
    out = tmp_path / "exp"
    export_scope(db, scope, out)
    return ((out / "records.jsonl").read_text(encoding="utf-8"),
            (out / "events.jsonl").read_text(encoding="utf-8"))


def _legacy(db, field, value, scope="s", record_id=None):
    """Write an illegal timestamp field directly (pre-R6 import or a
    direct DB write): the record the R7 fix must handle at erase time."""
    rid = record_id or db.execute(
        "SELECT id FROM memories WHERE scope_id=?", (scope,)).fetchone()[0]
    db.execute(f"UPDATE memories SET {field}=? WHERE scope_id=? AND id=?",
               (value, scope, rid))
    db.commit()
    return rid


# ---------------------------------------------------------------- import (1a/1e)

@pytest.mark.parametrize("field", ["created_at", "updated_at", "valid_until"])
def test_import_timestamp_field_with_verbatim_secret_rejected(tmp_path, field):
    # P0-R7-01(b): created_at/updated_at had NO validation at all — a
    # package could plant the secret verbatim there and a later erase
    # still reported COMPLETE. Pre-fix: imported=1 for created_at and
    # updated_at (valid_until was already rejected by the R6 fix).
    db = _db(tmp_path)
    pkg = _mk_package(tmp_path, [_rec("o1", "k1", {"api_key": SECRET},
                                     **{field: "x" + SECRET + "y"})])
    with pytest.raises(TurnkeepError) as ei:
        import_package(db, pkg, target_scope="s")
    assert ei.value.code == "INVALID_INPUT"
    assert db.execute("SELECT COUNT(*) FROM memories WHERE scope_id='s'"
                      ).fetchone()[0] == 0
    db.close()


@pytest.mark.parametrize("field", ["created_at", "updated_at", "valid_until"])
@pytest.mark.parametrize("label,enc", _encodings())
def test_import_timestamp_field_with_encoded_secret_rejected(tmp_path, field, label, enc):
    # Same validation must catch every reversible encoding: none of
    # them is a timestamp, so the ISO-8601 check rejects them all for
    # the same reason, with no decoder set (R6 pattern).
    db = _db(tmp_path)
    pkg = _mk_package(tmp_path, [_rec("o1", "k1", {"api_key": SECRET},
                                     **{field: enc})])
    with pytest.raises(TurnkeepError) as ei:
        import_package(db, pkg, target_scope="s")
    assert ei.value.code == "INVALID_INPUT"
    assert db.execute("SELECT COUNT(*) FROM memories WHERE scope_id='s'"
                      ).fetchone()[0] == 0
    db.close()


@pytest.mark.parametrize("field", ["created_at", "updated_at", "valid_until"])
def test_import_timestamp_field_with_legal_timestamp_accepted(tmp_path, field):
    # Control: a legal timestamp in any of the three fields imports.
    db = _db(tmp_path)
    pkg = _mk_package(tmp_path, [_rec("o1", "k1", "v", **{field: LEGAL_TS})])
    plan = import_package(db, pkg, target_scope="s")
    assert plan["imported"] == 1
    col = {"created_at": 0, "updated_at": 1, "valid_until": 2}[field]
    row = db.execute("SELECT created_at, updated_at, valid_until FROM memories "
                     "WHERE scope_id='s'").fetchone()
    assert row[col] == LEGAL_TS
    db.close()


def test_import_timestamp_equal_to_payload_rejected(tmp_path):
    # Second layer (needle check behind the ISO check): a timestamp
    # field that IS the record's own payload is still refused, even
    # though it parses as a legal timestamp.
    db = _db(tmp_path)
    pkg = _mk_package(tmp_path, [_rec("o1", "k1", LEGAL_TS,
                                     created_at=LEGAL_TS)])
    with pytest.raises(TurnkeepError) as ei:
        import_package(db, pkg, target_scope="s")
    assert ei.value.code == "INVALID_INPUT"
    db.close()


# ---------------------------------------------------------------- import kind/state (1b)

@pytest.mark.parametrize("kind", ["note", "NOTE", "", "fact ", 123])
def test_import_unknown_kind_rejected(tmp_path, kind):
    db = _db(tmp_path)
    pkg = _mk_package(tmp_path, [_rec("o1", "k1", "v", kind=kind)])
    with pytest.raises(TurnkeepError) as ei:
        import_package(db, pkg, target_scope="s")
    assert ei.value.code == "INVALID_INPUT"
    db.close()


@pytest.mark.parametrize("state", ["deleted", "ACTIVE", "", "pending", 1])
def test_import_unknown_state_rejected(tmp_path, state):
    db = _db(tmp_path)
    pkg = _mk_package(tmp_path, [_rec("o1", "k1", "v", state=state)])
    with pytest.raises(TurnkeepError) as ei:
        import_package(db, pkg, target_scope="s")
    assert ei.value.code == "INVALID_INPUT"
    db.close()


@pytest.mark.parametrize("kind,state", [
    ("requirement", "active"), ("fact", "conflicted"),
    ("decision", "superseded"), ("attempt", "retracted"),
    ("procedure", "tainted"), ("handoff", "expired"),
    ("fact", "erased"),
])
def test_import_known_kind_state_accepted(tmp_path, kind, state):
    db = _db(tmp_path)
    pkg = _mk_package(tmp_path, [_rec("o1", "k1", "v", kind=kind, state=state)])
    plan = import_package(db, pkg, target_scope="s")
    assert plan["imported"] == 1
    db.close()


# ---------------------------------------------------------------- legacy erasure (1c)

@pytest.mark.parametrize("field", ["valid_until", "created_at", "updated_at"])
@pytest.mark.parametrize("label,enc", [("verbatim", "x" + SECRET + "y")] + _encodings())
def test_legacy_illegal_timestamp_erased_clean(tmp_path, field, label, enc):
    # P0-R7-01(a/c): a record already holding an illegal timestamp
    # field (pre-R6 import / direct write) must erase COMPLETE with
    # the field cleared — in the DB and in the export — not COMPLETE
    # while the recoverable text stays in the tombstoned row.
    db = _db(tmp_path)
    rec = remember(db, "s", "k1", {"api_key": SECRET}, source_refs=SRC,
                   idempotency_key="k1")
    _legacy(db, field, enc)
    out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
    assert out["status"] == "COMPLETE" and out["erased"] is True
    row = db.execute("SELECT valid_until, created_at, updated_at, state "
                     "FROM memories WHERE scope_id='s' AND id=?",
                     (rec["id"],)).fetchone()
    assert row[3] == "erased"
    # valid_until is nulled; created_at/updated_at are NOT NULL, so an
    # illegal value there is replaced by the erase time — in every case
    # a fresh legal timestamp, never the planted text.
    col = {"valid_until": 0, "created_at": 1, "updated_at": 2}[field]
    if field == "valid_until":
        assert row[col] is None
    else:
        assert row[col] is not None and row[col] != enc
        _check_valid_until(row[col])
    assert SECRET not in (row[0] or "") and SECRET not in (row[1] or "") \
        and SECRET not in (row[2] or "")
    assert field in (out.get("cleared_illegal_timestamps") or [])
    recs, evs = _export_text(db, tmp_path)
    assert SECRET not in recs and SECRET not in evs
    assert enc not in recs and enc not in evs
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is True
    db.close()


def test_legacy_illegal_timestamp_repair_erase_clears_it(tmp_path):
    # The same clearing must happen on the repair path: a record
    # erased cleanly, whose tombstoned row is later found to hold an
    # illegal timestamp field (direct write after the first erase),
    # is cleared by a second erase with no revision bump.
    db = _db(tmp_path)
    rec = remember(db, "s", "k1", {"api_key": SECRET}, source_refs=SRC,
                   idempotency_key="k1")
    out1 = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
    assert out1["status"] == "COMPLETE"
    _legacy(db, "valid_until", "x" + SECRET + "y")
    out2 = erase_record(db, "s", rec["id"], out1["revision"], "repair")
    assert out2["status"] == "COMPLETE" and out2.get("repair") is True
    assert out2["revision"] == out1["revision"]
    row = db.execute("SELECT valid_until FROM memories WHERE scope_id='s' AND id=?",
                     (rec["id"],)).fetchone()
    assert row[0] is None
    recs, evs = _export_text(db, tmp_path)
    assert SECRET not in recs and SECRET not in evs
    db.close()


def test_erase_keeps_legal_timestamp_fields(tmp_path):
    # Guard against over-clearing: a legal timestamp is legitimate
    # expiry/audit text and must survive erasure untouched. (The value
    # is "hello", not a 1-char string: a 1-char needle such as "v"
    # matches the metadata KEY "valid_until" itself, which is the
    # pre-existing documented structural-key limit, not a timestamp
    # false positive.)
    db = _db(tmp_path)
    rec = remember(db, "s", "k1", "hello", source_refs=SRC, idempotency_key="k1")
    set_expiry(db, "s", rec["id"], LEGAL_TS, rec["revision"])
    rev = db.execute("SELECT revision FROM memories WHERE scope_id='s' AND id=?",
                     (rec["id"],)).fetchone()[0]
    out = erase_record(db, "s", rec["id"], rev, "rotation")
    assert out["status"] == "COMPLETE"
    row = db.execute("SELECT valid_until, created_at FROM memories "
                     "WHERE scope_id='s' AND id=?", (rec["id"],)).fetchone()
    assert row[0] == LEGAL_TS and row[1] is not None
    assert not out.get("cleared_illegal_timestamps")
    db.close()


def test_legacy_timestamp_equal_to_payload_cleared(tmp_path):
    # Corner: the payload itself IS a legal timestamp, and the legacy
    # valid_until equals it. Legality alone would keep it — but the
    # field IS the payload, so erasure must clear it.
    db = _db(tmp_path)
    rec = remember(db, "s", "k1", LEGAL_TS, source_refs=SRC,
                   idempotency_key="k1")
    _legacy(db, "valid_until", LEGAL_TS)
    out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
    assert out["status"] == "COMPLETE" and out["erased"] is True
    row = db.execute("SELECT valid_until FROM memories WHERE scope_id='s' AND id=?",
                     (rec["id"],)).fetchone()
    assert row[0] is None
    recs, evs = _export_text(db, tmp_path)
    assert LEGAL_TS not in recs and LEGAL_TS not in evs
    db.close()


# ---------------------------------------------------------------- "202" / "2026" (1c-ii)

# P2-R8-03: the three tests below are looped 200x each with a PINNED
# record id (a fixed hex string containing no "202"). Pinning alone
# does NOT prove the fix — the receipt's cause_event_id stays random
# every iteration, and that random uuid is where the "202" substring
# actually came from (verified: the flake is a "202" substring at an
# arbitrary position in a random uuid in the receipt, NOT a record id
# starting "2020" as GPT claimed). The loop is the evidence: 200 fresh
# runs exercise 200 fresh random cause_event_ids. The product-level fix
# is in erasure._cell_holds_needle: an identifier field counts only on
# whole-value equality, so a random id merely containing "202" is not
# a hit. The R6 chain flake (a 4-digit PIN appearing in a _now()
# microsecond timestamp) is a different instance — timestamp text,
# covered by the timestamp exemption — and is not conflated here.

PINNED_ID_202 = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
PINNED_ID_202_RECEIPT = "b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6"
PINNED_ID_2026 = "c1d2e3f4a5b6c7d8e9f0a1b2c3d4e5f6"


def test_erase_value_202_with_expiry_completes(tmp_path):
    # P0-R7-01(c): the 3-char needle "202" matched the legal timestamp
    # in the expire_set event metadata and wrongly refused the erase.
    # A needle occurrence inside a legal timestamp value is not a copy.
    for i in range(200):
        db = _db(tmp_path / f"run{i}")
        rec = remember(db, "s", "k1", "202", source_refs=SRC, idempotency_key="k1",
                       record_id=PINNED_ID_202)
        set_expiry(db, "s", rec["id"], LEGAL_TS, rec["revision"])
        rev = db.execute("SELECT revision FROM memories WHERE scope_id='s' AND id=?",
                         (rec["id"],)).fetchone()[0]
        out = erase_record(db, "s", rec["id"], rev, "rotation")
        assert out["status"] == "COMPLETE" and out["erased"] is True
        assert out["remaining"] == {}
        db.close()


def test_erase_value_202_with_receipt_holding_expiry_completes(tmp_path):
    # Same false positive through a run receipt: the begin receipt's
    # dump carries the same legal valid_until. Pre-fix: PARTIAL.
    for i in range(200):
        db = _db(tmp_path / f"run{i}")
        rec = remember(db, "s", "k1", "202", source_refs=SRC, idempotency_key="k1",
                       record_id=PINNED_ID_202_RECEIPT)
        set_expiry(db, "s", rec["id"], LEGAL_TS, rec["revision"])
        begin(db, "s", task="check k1", idempotency_key="run1")
        rev = db.execute("SELECT revision FROM memories WHERE scope_id='s' AND id=?",
                         (rec["id"],)).fetchone()[0]
        out = erase_record(db, "s", rec["id"], rev, "rotation")
        assert out["status"] == "COMPLETE" and out["erased"] is True
        assert out["remaining"] == {}
        db.close()


def test_erase_value_2026_with_legacy_expiry_completes(tmp_path):
    # Value "2026": set_expiry refuses the timestamp at write time
    # (guard, tested below), so the expiry here is a legacy direct
    # write. Pre-fix the erase was PARTIAL: the idempotency receipt's
    # created_at/updated_at timestamps contain "2026" as a substring.
    for i in range(200):
        db = _db(tmp_path / f"run{i}")
        rec = remember(db, "s", "k1", "2026", source_refs=SRC, idempotency_key="k1",
                       record_id=PINNED_ID_2026)
        _legacy(db, "valid_until", LEGAL_TS)
        out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
        assert out["status"] == "COMPLETE" and out["erased"] is True
        assert out["remaining"] == {}
        db.close()


def test_set_expiry_still_refuses_timestamp_containing_payload(tmp_path):
    # Write-time guard, unchanged by this rework: setting an expiry
    # whose text contains the payload is refused, because the
    # timestamp is copied verbatim into append-only event metadata.
    db = _db(tmp_path)
    rec = remember(db, "s", "k1", "2026", source_refs=SRC, idempotency_key="k1")
    with pytest.raises(TurnkeepError) as ei:
        set_expiry(db, "s", rec["id"], LEGAL_TS, rec["revision"])
    assert ei.value.code == "INVALID_INPUT"
    db.close()


def test_real_append_only_leak_still_refused(tmp_path):
    # Guard the other direction: an illegal timestamp that DID leak
    # into append-only event metadata (pre-R6 set_expiry) still makes
    # the erase PARTIAL — the value-aware scan must not hide a real
    # copy. Here the leak is simulated by writing the secret into the
    # expire_set event metadata directly.
    db = _db(tmp_path)
    rec = remember(db, "s", "k1", {"api_key": SECRET}, source_refs=SRC,
                   idempotency_key="k1")
    set_expiry(db, "s", rec["id"], LEGAL_TS, rec["revision"])
    db.execute("UPDATE events SET metadata_json=? WHERE scope_id='s' AND event_type='expire_set'",
               (_json({"key": "k1", "valid_until": "x" + SECRET + "y"}),))
    db.commit()
    rev = db.execute("SELECT revision FROM memories WHERE scope_id='s' AND id=?",
                     (rec["id"],)).fetchone()[0]
    out = erase_record(db, "s", rec["id"], rev, "rotation")
    assert out["status"] == "PARTIAL" and out["erased"] is False
    db.close()


# ---------------------------------------------------------------- export (1d)

@pytest.mark.parametrize("field", ["created_at", "updated_at", "valid_until"])
@pytest.mark.parametrize("label,enc", [("verbatim", "x" + SECRET + "y")] + _encodings())
def test_export_strips_illegal_timestamp_field(tmp_path, field, label, enc):
    # A live record with an illegal timestamp field must export with
    # that field stripped (null), never carrying the secret out — and
    # the stripping must be reported, not silent. The value is
    # deliberately NOT secret-like: a secret-like value would exclude
    # the whole record before stripping is ever reached, so stripping
    # is what protects a record whose timestamp field holds secret
    # material its value does not.
    db = _db(tmp_path)
    rec = remember(db, "s", "k1", "plain note", source_refs=SRC,
                   idempotency_key="k1")
    _legacy(db, field, enc)
    out_dir = tmp_path / "exp"
    rep = export_scope(db, "s", out_dir)
    recs = (out_dir / "records.jsonl").read_text(encoding="utf-8")
    evs = (out_dir / "events.jsonl").read_text(encoding="utf-8")
    assert SECRET not in recs and SECRET not in evs
    assert enc not in recs and enc not in evs
    exported = [json.loads(l) for l in recs.splitlines()]
    assert len(exported) == 1 and exported[0][field] is None
    assert {"key": "k1", "fields": [field]} in rep["stripped_illegal_timestamps"]
    db.close()


@pytest.mark.parametrize("field", ["created_at", "updated_at", "valid_until"])
def test_export_strips_illegal_timestamp_on_erased_legacy_row(tmp_path, field):
    # Defense in depth for DBs erased BEFORE this fix: a tombstoned
    # row whose timestamp field was written directly after the first
    # erase (the pre-fix erase left it) must still export clean — the
    # field is stripped, the tombstone itself still exports.
    db = _db(tmp_path)
    rec = remember(db, "s", "k1", "plain note", source_refs=SRC,
                   idempotency_key="k1")
    out1 = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
    assert out1["status"] == "COMPLETE"
    _legacy(db, field, "x" + SECRET + "y")
    out_dir = tmp_path / "exp"
    rep = export_scope(db, "s", out_dir)
    recs = (out_dir / "records.jsonl").read_text(encoding="utf-8")
    evs = (out_dir / "events.jsonl").read_text(encoding="utf-8")
    assert SECRET not in recs and SECRET not in evs
    exported = [json.loads(l) for l in recs.splitlines()]
    assert len(exported) == 1 and exported[0]["value"] == TOMBSTONE
    assert exported[0][field] is None
    assert {"key": "k1", "fields": [field]} in rep["stripped_illegal_timestamps"]
    db.close()


def test_export_excludes_record_whose_key_is_the_payload(tmp_path):
    # Identifiers cannot be stripped, so a record whose key IS its own
    # payload is excluded from the export (same contract as a
    # secret-like value). Whole-value equality only: a key that merely
    # contains a short needle as a substring is not excluded. The
    # payload here is deliberately NOT secret-like, so only the new
    # key rule can exclude the record.
    db = _db(tmp_path)
    payload = "plain-note-7f3a9c2e"
    remember(db, "s", payload, payload, source_refs=SRC, idempotency_key="k1")
    remember(db, "s", "k202", "202", source_refs=SRC, idempotency_key="k2")
    out_dir = tmp_path / "exp"
    rep = export_scope(db, "s", out_dir)
    recs = (out_dir / "records.jsonl").read_text(encoding="utf-8")
    assert payload not in recs
    # P1-R9-04: a key that IS the payload is omitted from the report
    # and flagged "key_redacted" — the old assertion (the raw payload
    # key is returned) was the defect, not the contract.
    assert len(rep["records_excluded_secrets"]) == 1
    entry = rep["records_excluded_secrets"][0]
    assert "key" not in entry and entry.get("key_redacted") is True
    assert payload not in _json(rep)
    assert '"k202"' in recs  # substring-only key still exports
    db.close()


def test_export_excludes_record_whose_source_id_is_the_payload(tmp_path):
    db = _db(tmp_path)
    payload = "plain-note-7f3a9c2e"
    remember(db, "s", "k1", payload, source_refs=[{"id": payload, "type": "user_statement"}],
             idempotency_key="k1")
    out_dir = tmp_path / "exp"
    rep = export_scope(db, "s", out_dir)
    recs = (out_dir / "records.jsonl").read_text(encoding="utf-8")
    assert payload not in recs
    assert [e["key"] for e in rep["records_excluded_secrets"]] == ["k1"]
    db.close()


def test_export_keeps_legal_timestamp_containing_short_needle(tmp_path):
    # The "202" rule on the export side: a legal timestamp stays in
    # the export even though it contains the short payload as a
    # substring — it is legitimate expiry text, not a copy.
    db = _db(tmp_path)
    rec = remember(db, "s", "k1", "202", source_refs=SRC, idempotency_key="k1")
    set_expiry(db, "s", rec["id"], LEGAL_TS, rec["revision"])
    out_dir = tmp_path / "exp"
    rep = export_scope(db, "s", out_dir)
    assert rep["stripped_illegal_timestamps"] == []
    exported = [json.loads(l) for l in (out_dir / "records.jsonl").read_text(encoding="utf-8").splitlines()]
    assert exported[0]["valid_until"] == LEGAL_TS
    db.close()


def test_export_strips_timestamp_equal_to_payload(tmp_path):
    # A live record whose payload IS a timestamp and whose valid_until
    # equals it: the export must strip that field (it is the payload).
    db = _db(tmp_path)
    rec = remember(db, "s", "k1", LEGAL_TS, source_refs=SRC, idempotency_key="k1")
    _legacy(db, "valid_until", LEGAL_TS)
    out_dir = tmp_path / "exp"
    export_scope(db, "s", out_dir)
    exported = [json.loads(l) for l in (out_dir / "records.jsonl").read_text(encoding="utf-8").splitlines()]
    assert exported[0]["valid_until"] is None
    db.close()


def test_export_stripped_timestamps_reimport_cleanly(tmp_path):
    # Round-trip: a stripped (null) created_at re-imports as the
    # import time instead of failing the NOT NULL constraint.
    db = _db(tmp_path)
    rec = remember(db, "s", "k1", "plain note", source_refs=SRC,
                   idempotency_key="k1")
    _legacy(db, "created_at", "x" + SECRET + "y")
    out_dir = tmp_path / "exp"
    export_scope(db, "s", out_dir)
    db2 = open_db(tmp_path / "t2.sqlite3")
    register_scope(db2, "s2")
    plan = import_package(db2, out_dir, target_scope="s2")
    assert plan["imported"] == 1
    row = db2.execute("SELECT created_at FROM memories WHERE scope_id='s2'").fetchone()
    assert row[0] is not None
    db.close()
    db2.close()


# ---------------------------------------------------------------- sweep (P1-R7-02)

def test_sweep_timezone_aware(tmp_path):
    # P1-R7-02: GPT's two cases, exactly reversed pre-fix (string
    # comparison on the raw ISO text). now = 2026-11-12T20:30:00Z.
    db = _db(tmp_path)
    r1 = remember(db, "s", "exp", "v1", source_refs=SRC, idempotency_key="e1")
    r2 = remember(db, "s", "notexp", "v2", source_refs=SRC, idempotency_key="e2")
    r3 = remember(db, "s", "expz", "v3", source_refs=SRC, idempotency_key="e3")
    r4 = remember(db, "s", "notexpz", "v4", source_refs=SRC, idempotency_key="e4")
    set_expiry(db, "s", r1["id"], "2026-11-12T22:00:00+02:00", r1["revision"])  # UTC 20:00 -> expired
    set_expiry(db, "s", r2["id"], "2026-11-12T19:00:00-02:00", r2["revision"])  # UTC 21:00 -> not expired
    set_expiry(db, "s", r3["id"], "2026-11-12T20:00:00+00:00", r3["revision"])  # UTC 20:00 -> expired (+00:00 control)
    set_expiry(db, "s", r4["id"], "2026-11-12T21:00:00+00:00", r4["revision"])  # UTC 21:00 -> not expired (control)
    res = sweep_expired(db, "s", now="2026-11-12T20:30:00+00:00")
    assert set(res["expired"]) == {r1["id"], r3["id"]}
    assert db.execute("SELECT state FROM memories WHERE id=?", (r1["id"],)).fetchone()[0] == "expired"
    assert db.execute("SELECT state FROM memories WHERE id=?", (r2["id"],)).fetchone()[0] == "active"
    assert db.execute("SELECT state FROM memories WHERE id=?", (r4["id"],)).fetchone()[0] == "active"
    # Idempotent: a second sweep finds nothing to do.
    assert sweep_expired(db, "s", now="2026-11-12T20:30:00+00:00")["expired"] == []
    db.close()


def test_sweep_naive_legacy_timestamp_read_as_utc(tmp_path):
    # Documented rule: a naive legacy valid_until (no timezone) is
    # read as UTC. It is never silently reinterpreted in local time.
    db = _db(tmp_path)
    r1 = remember(db, "s", "exp", "v1", source_refs=SRC, idempotency_key="e1")
    r2 = remember(db, "s", "notexp", "v2", source_refs=SRC, idempotency_key="e2")
    _legacy(db, "valid_until", "2026-11-12T20:00:00", record_id=r1["id"])
    _legacy(db, "valid_until", "2026-11-12T21:00:00", record_id=r2["id"])
    res = sweep_expired(db, "s", now="2026-11-12T20:30:00+00:00")
    assert res["expired"] == [r1["id"]]
    db.close()


def test_sweep_never_sweeps_unparseable_valid_until(tmp_path):
    # An unparseable valid_until has no defined expiry: it is skipped
    # (and reported), never swept on a guess.
    db = _db(tmp_path)
    r1 = remember(db, "s", "bad", "v1", source_refs=SRC, idempotency_key="e1")
    _legacy(db, "valid_until", "not-a-timestamp", record_id=r1["id"])
    res = sweep_expired(db, "s", now="2026-11-12T20:30:00+00:00")
    assert res["expired"] == []
    assert r1["id"] in res["skipped_unparseable"]
    assert db.execute("SELECT state FROM memories WHERE id=?", (r1["id"],)).fetchone()[0] == "active"
    db.close()


# ---------------------------------------------------------------- type check (P2-R7-03)

@pytest.mark.parametrize("bad", [123, 12.5, None, True, b"2026-01-01T00:00:00+00:00", ["2026-01-01T00:00:00+00:00"]])
def test_check_valid_until_rejects_non_string(bad):
    # P2-R7-03: datetime.fromisoformat only raises ValueError, not
    # TypeError — a non-str reached it unguarded from import (a JSON
    # number). The entry check makes it INVALID_INPUT.
    with pytest.raises(TurnkeepError) as ei:
        _check_valid_until(bad)
    assert ei.value.code == "INVALID_INPUT"


def test_import_valid_until_number_rejected(tmp_path):
    # The reachable path GPT got wrong: import (JSON number), not
    # set_expiry. Pre-fix: raw TypeError from fromisoformat.
    db = _db(tmp_path)
    pkg = _mk_package(tmp_path, [_rec("o1", "k1", "v", valid_until=123)])
    with pytest.raises(TurnkeepError) as ei:
        import_package(db, pkg, target_scope="s")
    assert ei.value.code == "INVALID_INPUT"
    db.close()


@pytest.mark.parametrize("field", ["created_at", "updated_at"])
def test_import_timestamp_number_rejected(tmp_path, field):
    db = _db(tmp_path)
    pkg = _mk_package(tmp_path, [_rec("o1", "k1", "v", **{field: 123})])
    with pytest.raises(TurnkeepError) as ei:
        import_package(db, pkg, target_scope="s")
    assert ei.value.code == "INVALID_INPUT"
    db.close()


def test_set_expiry_number_still_invalid_input(tmp_path):
    # Guard test: set_expiry(123) was already INVALID_INPUT via _text
    # (GPT's stated path was wrong); it must stay INVALID_INPUT.
    db = _db(tmp_path)
    rec = remember(db, "s", "k1", "v", source_refs=SRC, idempotency_key="k1")
    with pytest.raises(TurnkeepError) as ei:
        set_expiry(db, "s", rec["id"], 123, rec["revision"])
    assert ei.value.code == "INVALID_INPUT"
    db.close()


# ---------------------------------------------------------------- end-to-end chain

def test_end_to_end_r7_privacy_chain(tmp_path):
    # One chain: legacy bad fields (verbatim + Base64) -> first erase
    # -> repair erase -> residual report -> export. Nothing recoverable
    # from the DB text or the export afterwards. Erasure ends the
    # replay promise (P0-R10-01, item 1c): replaying the erased
    # remember raises IDEMPOTENCY_MISMATCH instead of returning the
    # tombstoned pointer — the stored request digest was neutralized
    # at erase time, and a still-valid digest of a request holding a
    # low-entropy value IS the enumerable leak.
    db = _db(tmp_path)
    enc = base64.b64encode(SECRET.encode()).decode()
    rec = remember(db, "s", "k1", {"api_key": SECRET}, source_refs=SRC,
                   idempotency_key="r7-chain-key-1")
    _legacy(db, "created_at", enc)
    _legacy(db, "valid_until", "x" + SECRET + "y")
    out1 = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
    assert out1["status"] == "COMPLETE" and out1["erased"] is True
    assert set(out1.get("cleared_illegal_timestamps") or []) == {"created_at", "valid_until"}
    out2 = erase_record(db, "s", rec["id"], out1["revision"], "repair")
    assert out2["status"] == "COMPLETE" and out2.get("repair") is True
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is True
    assert rep["controlled"]["controlled_residuals"] == []
    recs, evs = _export_text(db, tmp_path)
    assert SECRET not in recs and SECRET not in evs
    assert enc not in recs and enc not in evs
    # No recoverable copy left in any live-table text either.
    live_text = db.execute(
        "SELECT group_concat(value_json) || group_concat(valid_until) || "
        "group_concat(created_at) FROM memories WHERE scope_id='s'").fetchone()[0]
    assert SECRET not in (live_text or "") and enc not in (live_text or "")
    # R10 rework (P0-R10-01, item 1c): erasure ends the replay
    # promise — see the comment at the top of this test. No
    # resurrection: the record stays erased.
    with pytest.raises(TurnkeepError) as ei:
        remember(db, "s", "k1", {"api_key": SECRET}, source_refs=SRC,
                 idempotency_key="r7-chain-key-1")
    assert ei.value.code == "IDEMPOTENCY_MISMATCH"
    row = db.execute("SELECT value_json, state FROM memories WHERE id=?",
                     (rec["id"],)).fetchone()
    assert json.loads(row[0]) == TOMBSTONE and row[1] == "erased"
    db.close()
