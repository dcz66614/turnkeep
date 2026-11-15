"""R6 rework regression tests: P0-R6-01 (first erase + retract +
set_expiry + import must stop persisting free text that can carry a
recoverable secret).

Written BEFORE the fix where feasible: the glued/encoded-reason cases
fail on the pre-fix code (each returns COMPLETE and writes the
recoverable text into the append-only event log) and must be rejected
after it. No decode-and-compare anywhere: the fix is the allowlist
(first erase / retract) and ISO-8601 validation (set_expiry / import),
so an encoding the test never thought of is rejected for the same
reason — it is not a label / not a timestamp.
"""
import base64
import codecs
import hashlib
import json

import pytest

from turnkeep.storage import open_db
from turnkeep.scopes import register_scope
from turnkeep.memory import remember
from turnkeep.corrections import retract
from turnkeep.retention import set_expiry
from turnkeep.errors import TurnkeepError
from turnkeep.erasure import erase_record, residual_report
from turnkeep.portability import export_scope, import_package

SRC = [{"id": "u:1", "type": "user_statement"}]
KEY = "sk-LONG_SYNTHETIC_SECRET_247890"
TOMBSTONE = {"_erased": True}


def _db(tmp_path, scope="s"):
    db = open_db(tmp_path / "t.sqlite3")
    register_scope(db, scope)
    return db


def _erase_events(db):
    rows = db.execute(
        "SELECT metadata_json FROM events WHERE event_type='erase'").fetchall()
    return [json.loads(r[0]) for r in rows]


def _encodings():
    return [
        ("base64", base64.b64encode(KEY.encode()).decode()),
        ("percent-char-by-char",
         "".join(f"%{b:02x}" for b in KEY.encode())),
        ("reversed", KEY[::-1]),
        ("hex", KEY.encode().hex()),
        ("rot13", codecs.encode(KEY, "rot_13")),
    ]


# ---------------------------------------------------------------- first erase

def test_first_erase_glued_short_secret_rejected(tmp_path):
    # GPT's P0-R6-01 repro: value "abc", reason "rotatedabcagain" —
    # the 3-char needle glued into a longer token, no token boundary,
    # too short for the old substring check. Pre-fix: COMPLETE, and
    # the exported events.jsonl contained "rotatedabcagain".
    db = _db(tmp_path)
    rec = remember(db, "s", "k", "abc", kind="fact", source_refs=SRC)
    with pytest.raises(TurnkeepError) as ei:
        erase_record(db, "s", rec["id"], rec["revision"], "rotatedabcagain")
    assert ei.value.code == "INVALID_INPUT"
    assert "cleanup" in str(ei.value) and "repair" in str(ei.value) \
        and "rotation" in str(ei.value)
    # Nothing published: no erase event, record untouched.
    assert len(_erase_events(db)) == 0
    row = db.execute("SELECT value_json, state FROM memories WHERE id=?",
                     (rec["id"],)).fetchone()
    assert "abc" in row[0] and row[1] == "active"
    db.close()


@pytest.mark.parametrize("label,enc", _encodings())
def test_first_erase_encoded_reason_rejected(tmp_path, label, enc):
    # GPT's four first-erase forms (Base64, percent-encoding, reversed)
    # plus hex and rot13: each carries a reversible encoding of the key
    # in the reason. Pre-fix: COMPLETE in every case, the encoding
    # stayed in the exported events.jsonl, the key was recoverable
    # from the export alone. The allowlist rejects them all for the
    # same reason — none of them is a label — with no decoder set.
    db = _db(tmp_path)
    rec = remember(db, "s", "k", KEY, kind="fact", source_refs=SRC)
    with pytest.raises(TurnkeepError) as ei:
        erase_record(db, "s", rec["id"], rec["revision"],
                     f"rotated {enc} for audit")
    assert ei.value.code == "INVALID_INPUT"
    assert len(_erase_events(db)) == 0
    assert enc not in db.execute(
        "SELECT group_concat(metadata_json) FROM events").fetchone()[0]
    row = db.execute("SELECT value_json, state FROM memories WHERE id=?",
                     (rec["id"],)).fetchone()
    assert KEY in row[0] and row[1] == "active"
    db.close()


@pytest.mark.parametrize("value,reason", [
    ("repair", "repair"),
    ("rotation", "rotation"),
])
def test_first_erase_label_equal_to_payload_rejected(tmp_path, value, reason):
    # The allowlist alone is not enough: a label that IS the erased
    # value must still be caught — by the needle check as a second
    # layer behind the allowlist (same shape as the R5 repair test).
    db = _db(tmp_path)
    rec = remember(db, "s", "k", value, kind="fact", source_refs=SRC)
    with pytest.raises(TurnkeepError) as ei:
        erase_record(db, "s", rec["id"], rec["revision"], reason)
    assert ei.value.code == "INVALID_INPUT"
    assert len(_erase_events(db)) == 0
    db.close()


@pytest.mark.parametrize("value,reason", [
    ("repair", "rotation"),
    ("rotation", "cleanup"),
    ("cleanup", "repair"),
])
def test_first_erase_other_label_succeeds_when_payload_is_a_label(tmp_path, value, reason):
    # The second layer must not over-block: a label that is NOT the
    # payload erases COMPLETE, even when the payload itself is one of
    # the labels.
    db = _db(tmp_path)
    rec = remember(db, "s", "k", value, kind="fact", source_refs=SRC)
    out = erase_record(db, "s", rec["id"], rec["revision"], reason)
    assert out["status"] == "COMPLETE" and out["erased"] is True
    assert _erase_events(db)[0]["reason"] == reason
    assert residual_report(db, "s")["controlled"]["live_tables_clean"] is True
    db.close()


def test_clean_first_erase_with_label_succeeds(tmp_path):
    # The allowlist must not block a clean first erase with a label:
    # COMPLETE, clean residual report, and the export carries no
    # plaintext secret and only the label as the erase reason.
    db = _db(tmp_path)
    rec = remember(db, "s", "k", {"api_key": KEY}, kind="fact",
                   source_refs=SRC, idempotency_key="r6-clean-key-1")
    out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
    assert out["status"] == "COMPLETE" and out["erased"] is True
    assert residual_report(db, "s")["controlled"]["live_tables_clean"] is True
    out_dir = tmp_path / "export"
    export_scope(db, "s", out_dir)
    events_text = (out_dir / "events.jsonl").read_text(encoding="utf-8")
    assert KEY not in events_text
    assert KEY not in (out_dir / "records.jsonl").read_text(encoding="utf-8")
    for line in events_text.splitlines():
        event = json.loads(line)
        if event.get("event_type") == "erase":
            assert event["metadata"]["reason"] in ("cleanup", "repair", "rotation")
    db.close()


# ---------------------------------------------------------------- retract

@pytest.mark.parametrize("label,enc", _encodings())
def test_retract_encoded_reason_rejected(tmp_path, label, enc):
    # GPT's A2 repro (Base64) plus the other encodings: a retract
    # reason carrying an encoding of the payload must be rejected —
    # pre-fix the retract was accepted, and a later clean erase still
    # returned COMPLETE while the exported events held the encoding.
    db = _db(tmp_path)
    rec = remember(db, "s", "k", KEY, kind="fact", source_refs=SRC)
    with pytest.raises(TurnkeepError) as ei:
        retract(db, "s", rec["id"], rec["revision"],
                f"retracting {enc} per policy")
    assert ei.value.code == "INVALID_INPUT"
    assert db.execute(
        "SELECT COUNT(*) FROM events WHERE event_type='retract'").fetchone()[0] == 0
    assert enc not in db.execute(
        "SELECT group_concat(metadata_json) FROM events").fetchone()[0]
    # A clean retract with a label still works, and a later erase is
    # COMPLETE with a clean export.
    out = retract(db, "s", rec["id"], rec["revision"], "correction")
    assert out["state"] == "retracted"
    out2 = erase_record(db, "s", rec["id"], out["revision"], "rotation")
    assert out2["status"] == "COMPLETE"
    out_dir = tmp_path / "export"
    export_scope(db, "s", out_dir)
    assert enc not in (out_dir / "events.jsonl").read_text(encoding="utf-8")
    assert KEY not in (out_dir / "events.jsonl").read_text(encoding="utf-8")
    db.close()


@pytest.mark.parametrize("value,reason", [
    ("correction", "correction"),
    ("superseded", "superseded"),
    ("withdrawn", "withdrawn"),
])
def test_retract_label_equal_to_payload_rejected(tmp_path, value, reason):
    # Same second layer as first erase: a retract label that IS the
    # payload is rejected by the needle check behind the allowlist.
    db = _db(tmp_path)
    rec = remember(db, "s", "k", value, kind="fact", source_refs=SRC)
    with pytest.raises(TurnkeepError) as ei:
        retract(db, "s", rec["id"], rec["revision"], reason)
    assert ei.value.code == "INVALID_INPUT"
    assert db.execute(
        "SELECT COUNT(*) FROM events WHERE event_type='retract'").fetchone()[0] == 0
    db.close()


# ---------------------------------------------------------------- set_expiry

@pytest.mark.parametrize("valid_until", [
    base64.b64encode(KEY.encode()).decode(),
    "".join(f"%{b:02x}" for b in KEY.encode()),
    KEY[::-1],
    KEY.encode().hex(),
    codecs.encode(KEY, "rot_13"),
    KEY,  # the secret verbatim is not a timestamp either
    "tomorrow",
    "2026-01-01",  # date only: parses, but naive (no timezone)
    "2026-01-01T00:00:00",  # naive timestamp: no timezone
    "2026-13-01T00:00:00+00:00",  # not a real date
])
def test_set_expiry_non_timestamp_valid_until_rejected(tmp_path, valid_until):
    # GPT's A2 repro (Base64) plus the other encodings, the verbatim
    # secret, and non-timestamp / naive values: valid_until must be an
    # ISO-8601 timestamp WITH a timezone. Pre-fix the Base64 value was
    # accepted, survived erasure in the tombstoned row, and stayed in
    # the exported records.jsonl.
    db = _db(tmp_path)
    rec = remember(db, "s", "k", KEY, kind="fact", source_refs=SRC)
    with pytest.raises(TurnkeepError) as ei:
        set_expiry(db, "s", rec["id"], valid_until, rec["revision"])
    assert ei.value.code == "INVALID_INPUT"
    assert db.execute(
        "SELECT COUNT(*) FROM events WHERE event_type='expire_set'").fetchone()[0] == 0
    row = db.execute("SELECT valid_until, revision FROM memories WHERE id=?",
                     (rec["id"],)).fetchone()
    assert row[0] is None and row[1] == rec["revision"]
    db.close()


def test_set_expiry_timestamp_equal_to_payload_rejected(tmp_path):
    # Second-layer corner: the payload itself IS a timestamp, so
    # valid_until equal to it passes the ISO check but must still be
    # caught by the needle check behind it.
    ts = "2027-06-01T00:00:00+00:00"
    db = _db(tmp_path)
    rec = remember(db, "s", "k", ts, kind="fact", source_refs=SRC)
    with pytest.raises(TurnkeepError) as ei:
        set_expiry(db, "s", rec["id"], ts, rec["revision"])
    assert ei.value.code == "INVALID_INPUT"
    db.close()


@pytest.mark.parametrize("valid_until", [
    "2026-01-01T00:00:00+00:00",
    "2030-01-01T00:00:00Z",
    "2031-06-01T12:30:00.123456+00:00",
])
def test_set_expiry_valid_timestamps_accepted(tmp_path, valid_until):
    # The ISO check must not over-block real timestamps (the formats
    # the existing expiry tests already use, plus _now() output shape).
    db = _db(tmp_path)
    rec = remember(db, "s", "k", "v", kind="fact", source_refs=SRC)
    out = set_expiry(db, "s", rec["id"], valid_until, rec["revision"])
    assert out["valid_until"] == valid_until
    db.close()


# ---------------------------------------------------------------- import

def _package_with_valid_until(tmp_path, valid_until):
    """Export one record, rewrite its valid_until in the package, and
    recompute the manifest digests so the tamper check passes (same
    construction as the R6 verification A4 repro)."""
    src = _db(tmp_path / "src")
    remember(src, "s", "k", "hello", kind="fact", source_refs=SRC)
    pkg = tmp_path / "pkg"
    export_scope(src, "s", pkg)
    src.close()
    recs_path = pkg / "records.jsonl"
    lines = [json.loads(l) for l in recs_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    lines[0]["valid_until"] = valid_until
    recs_path.write_text("".join(json.dumps(x, sort_keys=True, separators=(",", ":")) + "\n"
                                 for x in lines), encoding="utf-8")
    man_path = pkg / "manifest.json"
    man = json.loads(man_path.read_text(encoding="utf-8"))
    for name in ("records.jsonl", "events.jsonl"):
        man["files"][name] = hashlib.sha256((pkg / name).read_bytes()).hexdigest()
    man_path.write_text(json.dumps(man, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    return pkg


@pytest.mark.parametrize("valid_until", [
    KEY,
    base64.b64encode(KEY.encode()).decode(),
    "".join(f"%{b:02x}" for b in KEY.encode()),
    "tomorrow",
])
def test_import_non_timestamp_valid_until_rejected(tmp_path, valid_until):
    # GPT's A4 repro (verbatim secret) plus encodings: import_package
    # used to copy valid_until with NO check at all, so a package could
    # plant the secret there and a later erase still reported
    # COMPLETE while the exported records.jsonl held it.
    pkg = _package_with_valid_until(tmp_path, valid_until)
    db = _db(tmp_path / "dst")
    with pytest.raises(TurnkeepError) as ei:
        import_package(db, pkg, target_scope="s")
    assert ei.value.code == "INVALID_INPUT"
    assert db.execute("SELECT COUNT(*) FROM memories WHERE scope_id='s'").fetchone()[0] == 0
    db.close()


def test_import_valid_timestamp_valid_until_accepted(tmp_path):
    # The import check must not over-block a real timestamp: a package
    # whose valid_until is ISO-8601 with a timezone imports, and the
    # record keeps the expiry.
    pkg = _package_with_valid_until(tmp_path, "2027-01-01T00:00:00+00:00")
    db = _db(tmp_path / "dst")
    plan = import_package(db, pkg, target_scope="s")
    assert plan["imported"] == 1
    row = db.execute("SELECT valid_until FROM memories WHERE scope_id='s' AND key='k'").fetchone()
    assert row[0] == "2027-01-01T00:00:00+00:00"
    db.close()


# ------------------------------------------------- end-to-end privacy chain

def test_end_to_end_privacy_chain(tmp_path):
    """One chain, as GPT asked: first erase, second erase, residual
    report, history export, idempotent receipts, low-entropy recovery.
    A low-entropy secret (PIN 4826) is remembered with an idempotency
    key, retracted with a label, erased with a label, repair-erased
    with a label, reported clean, exported — and the PIN must not be
    recoverable from the export by enumerating 0000-9999. Erasure
    ends the replay promise (P0-R10-01, item 1c): replaying the
    erased remember raises IDEMPOTENCY_MISMATCH — the stored request
    digest was neutralized at erase time — without resurrecting the
    record. Every erase/retract event in the export carries only a
    controlled label as its reason."""
    db = _db(tmp_path)
    rec = remember(db, "s", "k.pin", {"pin": "4826"}, kind="fact",
                   source_refs=SRC, idempotency_key="r6-chain-key-1")
    retracted = retract(db, "s", rec["id"], rec["revision"], "correction")
    assert retracted["state"] == "retracted"
    out1 = erase_record(db, "s", rec["id"], retracted["revision"], "rotation")
    assert out1["status"] == "COMPLETE" and out1["erased"] is True
    out2 = erase_record(db, "s", rec["id"], out1["revision"], "repair")
    assert out2["status"] == "COMPLETE" and out2.get("repair") is True
    assert out2["revision"] == out1["revision"]  # no second bump
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is True
    assert rep["controlled"]["controlled_residuals"] == []
    assert rep["status"] == "COMPLETE_WITH_RESIDUALS"  # uncontrolled copies listed, controlled clean
    out_dir = tmp_path / "export"
    export_scope(db, "s", out_dir)
    events_text = (out_dir / "events.jsonl").read_text(encoding="utf-8")
    assert "4826" not in events_text
    assert "4826" not in (out_dir / "records.jsonl").read_text(encoding="utf-8")
    exported_hashes = set()
    for line in events_text.splitlines():
        event = json.loads(line)
        exported_hashes.update(
            event["metadata"].get("needle_hashes", []) or [])
        if event.get("event_type") in ("erase", "retract"):
            assert event["metadata"]["reason"] in (
                "cleanup", "repair", "rotation",
                "correction", "superseded", "withdrawn")
    # P0-R11-02 conforming: a COMPLETE erase persists NO needle
    # hashes — in the live DB or in the export — so there is nothing
    # to enumerate against, which is stronger than the R4 HMAC
    # guarantee (the hashes are gone, not merely keyed). The
    # enumeration loop below is therefore over an empty set.
    assert exported_hashes == set(), \
        "a COMPLETE erase persists no needle hashes in the export (P0-R11-02)"
    for n in range(10000):
        assert hashlib.sha256(f"{n:04d}".encode()).hexdigest() not in exported_hashes
    # R10 rework (P0-R10-01, item 1c): erasure ends the replay
    # promise. The stored request digest was neutralized at erase
    # time, so replaying the erased remember raises
    # IDEMPOTENCY_MISMATCH instead of returning the tombstoned
    # pointer. The old expectation (replay returns the stored
    # response) required the digest to stay valid after erasure —
    # and a still-valid plain SHA-256 digest of a request holding
    # a low-entropy value IS the enumerable leak. No resurrection,
    # no new row: the record stays erased.
    with pytest.raises(TurnkeepError) as ei:
        remember(db, "s", "k.pin", {"pin": "4826"}, kind="fact",
                 source_refs=SRC, idempotency_key="r6-chain-key-1")
    assert ei.value.code == "IDEMPOTENCY_MISMATCH"
    row = db.execute("SELECT value_json, state, revision FROM memories WHERE id=?",
                     (rec["id"],)).fetchone()
    assert json.loads(row[0]) == TOMBSTONE and row[1] == "erased"
    assert row[2] == out1["revision"]
    assert db.execute(
        "SELECT COUNT(*) FROM memories WHERE scope_id='s'").fetchone()[0] == 1
    db.close()
