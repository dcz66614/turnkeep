"""R4 rework regression tests: P0-R4-01 (short needles as tokens),
P0-R4-02 (second erase vetted against stored hashes), P0-R4-04 (HMAC
needle hashes), P1-R4-06 (LIKE wildcard escaping).

Written BEFORE the fix where feasible: every test here fails on the
pre-fix code (verified against the R4 verification repros) and must
pass after it.
"""
import hashlib
import json
import re

import pytest

from turnkeep.storage import open_db, publish_event
from turnkeep.scopes import register_scope
from turnkeep.memory import remember
from turnkeep.corrections import retract
from turnkeep.errors import TurnkeepError
from turnkeep.erasure import erase_record, residual_report
from turnkeep.portability import export_scope

SRC = [{"id": "u:1", "type": "user_statement"}]
TOMBSTONE = {"_erased": True}


def _db(tmp_path, scope="s"):
    db = open_db(tmp_path / "t.sqlite3")
    register_scope(db, scope)
    return db


def _erase_events(db):
    rows = db.execute(
        "SELECT metadata_json FROM events WHERE event_type='erase'").fetchall()
    return [json.loads(r[0]) for r in rows]


_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?([+-]\d{2}:\d{2}|Z)?$")


def _holds_pin(obj, pin):
    """True when `pin` occurs in any string value of a parsed export
    line that is NOT an ISO-8601 timestamp. A raw-text `pin not in
    events.jsonl` check is flaky: a microsecond timestamp can contain
    a 4-digit PIN by coincidence (the known R7 flake, ~0.26% per
    timestamp), so the check is structural — the PIN must not appear
    as payload material, while timestamps are exempt. Same helper as
    repro_r12.py's B1.4. Not a weakening: the assertion is still
    "the export contains no payload material"."""
    if isinstance(obj, str):
        return pin in obj and not _TS_RE.match(obj)
    if isinstance(obj, dict):
        return any(_holds_pin(v, pin) for v in obj.values())
    if isinstance(obj, list):
        return any(_holds_pin(v, pin) for v in obj)
    return False


def _export_holds_pin(text, pin):
    return any(_holds_pin(json.loads(l), pin)
               for l in text.splitlines() if l.strip())


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


# ---------------------------------------------------------------- P0-R4-01

@pytest.mark.parametrize("value,reason", [
    ("abc", "token abc rotated"),
    ("123", "secret 123 rotated"),
    ("p4ss", "token p4ss rotated"),
    ("sk-FOO-312", "token sk-FOO-312 rotated"),
])
def test_short_needle_as_token_in_reason_rejected(tmp_path, value, reason):
    # GPT's exact P0-R4-01 cases: 1-3 char needles embedded in a longer
    # reason must be rejected (the first two passed pre-fix); the 4+
    # char cases were already rejected and must stay rejected.
    # R6 (P0-R6-01): these are now rejected as non-labels by the
    # first-erase allowlist, before the needle check — the
    # INVALID_INPUT assertions are unchanged, and the check no longer
    # depends on token matching at all.
    db = _db(tmp_path)
    rec = remember(db, "s", "k", value, kind="fact", source_refs=SRC)
    with pytest.raises(TurnkeepError) as ei:
        erase_record(db, "s", rec["id"], rec["revision"], reason)
    assert ei.value.code == "INVALID_INPUT"
    assert db.execute(
        "SELECT COUNT(*) FROM events WHERE event_type='erase'").fetchone()[0] == 0
    row = db.execute("SELECT value_json, state FROM memories WHERE id=?",
                     (rec["id"],)).fetchone()
    assert value in row[0] and row[1] == "active"
    db.close()


def test_short_needle_in_dict_value_as_token_rejected(tmp_path):
    db = _db(tmp_path)
    rec = remember(db, "s", "k", {"code": "abc"}, kind="fact", source_refs=SRC)
    with pytest.raises(TurnkeepError) as ei:
        erase_record(db, "s", rec["id"], rec["revision"], "token abc rotated")
    assert ei.value.code == "INVALID_INPUT"
    db.close()


def test_incidental_substring_still_accepted(tmp_path):
    # False-positive guard, now label-based (R6, P0-R6-01): needle "x"
    # inside "box" is NOT a token, so a reason mentioning "box" must
    # still erase — but reasons are controlled labels now, so the guard
    # is that a clean label erase still succeeds for a 1-char payload
    # whose needle occurs inside ordinary words. The old free-text
    # reason "fix box" is INVALID_INPUT as a non-label; the label
    # "repair" must erase COMPLETE.
    db = _db(tmp_path)
    rec = remember(db, "s", "k", "x", kind="fact", source_refs=SRC)
    with pytest.raises(TurnkeepError) as ei:
        erase_record(db, "s", rec["id"], rec["revision"], "fix box")
    assert ei.value.code == "INVALID_INPUT"
    out = erase_record(db, "s", rec["id"], rec["revision"], "repair")
    assert out["status"] == "COMPLETE" and out["erased"] is True
    assert residual_report(db, "s")["controlled"]["live_tables_clean"] is True
    db.close()


def test_retract_reason_short_needle_token_rejected(tmp_path):
    # The check is shared: retract's reason leaks the same way.
    # R6 (P0-R6-01): now rejected as a non-label by the retract
    # allowlist (RETRACT_REASONS) before the needle check; the clean
    # retract below uses the label "correction".
    db = _db(tmp_path)
    rec = remember(db, "s", "k", "abc", kind="fact", source_refs=SRC)
    with pytest.raises(TurnkeepError) as ei:
        retract(db, "s", rec["id"], rec["revision"], "token abc rotated")
    assert ei.value.code == "INVALID_INPUT"
    assert db.execute(
        "SELECT COUNT(*) FROM events WHERE event_type='retract'").fetchone()[0] == 0
    out = retract(db, "s", rec["id"], rec["revision"], "correction")
    assert out["state"] == "retracted"
    db.close()


# ---------------------------------------------------------------- P0-R4-02

def test_second_erase_with_secret_in_reason_rejected(tmp_path):
    # GPT's exact P0-R4-02 repro: first erase COMPLETE, then a second
    # erase whose reason carries the full secret must be rejected —
    # the repair path used to check the reason against an empty needle
    # set (receipts already scrubbed) and published the secret.
    secret = "sk-LONG_SYNTHETIC_SECRET_247890"
    db = _db(tmp_path)
    rec = remember(db, "s", "k", {"api_key": secret}, kind="fact",
                   source_refs=SRC, idempotency_key="r4-02-key-1")
    out1 = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
    assert out1["status"] == "COMPLETE"
    assert len(_erase_events(db)) == 1
    with pytest.raises(TurnkeepError) as ei:
        erase_record(db, "s", rec["id"], out1["revision"],
                     f"rotation of {secret} again")
    assert ei.value.code == "INVALID_INPUT"
    # Nothing new was published: still exactly one erase event, and no
    # event metadata holds the secret.
    assert len(_erase_events(db)) == 1
    assert secret not in json.dumps(_erase_events(db))
    assert secret not in db.execute(
        "SELECT group_concat(metadata_json) FROM events").fetchone()[0]
    _no_secret_anywhere(db, "s", secret)
    db.close()


def test_clean_repair_erase_still_succeeds(tmp_path):
    # The stored-hash vetting must not block a clean repair erase.
    secret = "sk-LONG_SYNTHETIC_SECRET_247890"
    db = _db(tmp_path)
    rec = remember(db, "s", "k", {"api_key": secret}, kind="fact",
                   source_refs=SRC, idempotency_key="r4-02-key-2")
    out1 = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
    assert out1["status"] == "COMPLETE"
    out2 = erase_record(db, "s", rec["id"], out1["revision"], "repair")
    assert out2["status"] == "COMPLETE" and out2["erased"] is True
    assert out2.get("repair") is True
    assert out2["revision"] == out1["revision"]
    db.close()


# ---------------------------------------------------------------- P0-R4-04

def test_pin_not_brute_forceable_from_export(tmp_path):
    # GPT's exact P0-R4-04 repro: PIN 4826, erase COMPLETE, export.
    # The export must carry no raw SHA-256 of the PIN, and enumerating
    # 0000-9999 against the exported hashes must find nothing.
    # P0-R11-02 conforming: a COMPLETE erase now persists NO needle
    # hashes at all — in the live DB or in the export — so there is
    # nothing to enumerate against, which is stronger than the R4
    # HMAC guarantee (the hashes are gone, not merely keyed). The
    # enumeration loop below is therefore over an empty set; the
    # property it guards is now asserted directly (exported_hashes
    # == set()) and by the live-DB enumeration in
    # test_r11_erasure_rework (GPT A2 chain).
    db = _db(tmp_path)
    rec = remember(db, "s", "k", {"pin": "4826"}, kind="fact", source_refs=SRC)
    out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
    assert out["status"] == "COMPLETE"
    out_dir = tmp_path / "export"
    export_scope(db, "s", out_dir)
    events_text = (out_dir / "events.jsonl").read_text(encoding="utf-8")
    records_text = (out_dir / "records.jsonl").read_text(encoding="utf-8")
    # Structured check (see _holds_pin): a raw-text "4826" not in
    # events_text is flaky — a microsecond timestamp can contain the
    # PIN by coincidence (the known R7 flake). The assertion is
    # unchanged in substance: no payload material in the export.
    assert not _export_holds_pin(events_text, "4826")
    assert not _export_holds_pin(records_text, "4826")
    exported_hashes = set()
    for line in events_text.splitlines():
        meta = json.loads(line)["metadata"]
        exported_hashes.update(meta.get("needle_hashes", []) or [])
    assert exported_hashes == set(), \
        "a COMPLETE erase persists no needle hashes in the export (P0-R11-02)"
    raw_pin = hashlib.sha256(b"4826").hexdigest()
    assert raw_pin not in exported_hashes
    for n in range(10000):
        cand = hashlib.sha256(f"{n:04d}".encode()).hexdigest()
        assert cand not in exported_hashes, f"PIN recovered: {n:04d}"
    # P0-R12-01: after a terminal COMPLETE erase the record's
    # verification key is deleted, and a fresh DB never creates a
    # scope key — so no key material remains in the DB at all, and
    # none of it can appear in the export.
    assert db.execute(
        "SELECT COUNT(*) FROM record_erase_keys WHERE scope_id='s'").fetchone()[0] == 0
    assert db.execute(
        "SELECT COUNT(*) FROM scope_secrets WHERE scope_id='s'").fetchone()[0] == 0
    db.close()


def test_needle_hash_differs_from_raw_sha256(tmp_path):
    # P0-R11-02 conforming: a COMPLETE erase persists no needle
    # hashes, so the "hashes are HMAC, not raw SHA-256" property is
    # now checked on a PARTIAL erase (a second record still holds the
    # PIN, so the hashes are persisted — and the repair / residual /
    # export paths still need them). The property itself is unchanged:
    # every persisted needle hash is a scope-keyed HMAC, never a raw
    # SHA-256 of a needle.
    db = _db(tmp_path)
    rec = remember(db, "s", "k", {"pin": "4826"}, kind="fact", source_refs=SRC)
    remember(db, "s", "k2", {"pin": "4826"}, kind="fact", source_refs=SRC)
    out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
    assert out["status"] == "PARTIAL"
    meta = _erase_events(db)[0]
    assert meta["needle_hashes"]
    for needle in ('{"pin":"4826"}', "4826"):
        assert hashlib.sha256(needle.encode()).hexdigest() not in meta["needle_hashes"]
    db.close()


# ---------------------------------------------------------------- P1-R4-06

@pytest.mark.parametrize("value", ["%", "_"])
def test_erase_wildcard_value_completes(tmp_path, value):
    # GPT's P1-R4-06 repros: a payload of "%" or "_" must erase
    # COMPLETE — pre-fix, _scan_payload treated them as LIKE wildcards
    # and the step-1 append-only refusal fired on every text cell.
    db = _db(tmp_path)
    rec = remember(db, "s", "k", value, kind="fact", source_refs=SRC)
    out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
    assert out["status"] == "COMPLETE" and out["erased"] is True
    row = db.execute("SELECT value_json, state FROM memories WHERE id=?",
                     (rec["id"],)).fetchone()
    assert json.loads(row[0]) == TOMBSTONE and row[1] == "erased"
    assert residual_report(db, "s")["controlled"]["live_tables_clean"] is True
    db.close()


def test_erase_needle_inside_tombstone_marker_completes(tmp_path):
    # Found while fixing P1-R4-06: the tombstone marker
    # {"_erased": true} itself contains "true" (and '_', 'e', 'r', ...),
    # so the post-scrub scan used to match the erased record's own row
    # and report a phantom residual for any needle that is a substring
    # of the marker. The erased row is excluded while it holds exactly
    # the tombstone; any other row still counts. ("e" is NOT used here:
    # before the R11 rework the letter 'e' also appeared in structural
    # JSON key names such as "key" in event metadata, which no
    # text-based scan could tell apart from a payload occurrence — a
    # 1-char payload colliding with a structural key name still
    # reported PARTIAL, fail-closed. The R11 rework (P2-R11-03) fixed
    # that for AUDIT-structure key names — see
    # tests/privacy/test_r11_erasure_rework.py::test_one_char_payload_e_after_prior_erase
    # — and the tombstone marker itself is now structural too, so "e"
    # erases COMPLETE; only a collision with a USER-DATA key name
    # still reports PARTIAL, fail-closed — see KNOWN_LIMITATIONS §8/§24.)
    db = _db(tmp_path)
    rec = remember(db, "s", "k", "true", kind="fact", source_refs=SRC)
    out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
    assert out["status"] == "COMPLETE" and out["erased"] is True
    assert out["remaining"] == {}
    assert residual_report(db, "s")["controlled"]["live_tables_clean"] is True
    db.close()


def test_real_append_only_hit_still_refused(tmp_path):
    # The escaping must not blind the scan: a payload genuinely present
    # in the append-only events table is still refused PARTIAL.
    secret = "sk-SYNTH-R4-06-appendonly"
    db = _db(tmp_path)
    rec = remember(db, "s", "k", {"api_key": secret}, kind="fact", source_refs=SRC)
    publish_event(db, "s", "note", "local", {rec["id"]: None},
                  metadata={"leaked": secret})
    out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
    assert out["status"] == "PARTIAL" and out["erased"] is False
    row = db.execute("SELECT value_json, state FROM memories WHERE id=?",
                     (rec["id"],)).fetchone()
    assert secret in row[0] and row[1] == "active"
    db.close()


# ------------------------------------------------- end-to-end privacy chain

def test_end_to_end_privacy_chain(tmp_path):
    """One chain, as GPT asked: first erase, second erase, residual
    report, history export, idempotent receipts, low-entropy recovery.
    A low-entropy secret (PIN 4826) is remembered with an idempotency
    key, erased, repair-erased with a clean reason, reported clean,
    exported — and the PIN must not be recoverable from the export by
    enumerating 0000-9999. Erasure ends the replay promise (P0-R10-01,
    item 1c): replaying the erased remember raises IDEMPOTENCY_MISMATCH
    — the stored request digest was neutralized at erase time — without
    resurrecting the record."""
    db = _db(tmp_path)
    rec = remember(db, "s", "k.pin", {"pin": "4826"}, kind="fact",
                   source_refs=SRC, idempotency_key="r4-chain-key-1")
    out1 = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
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
    exported_hashes = set()
    for line in events_text.splitlines():
        exported_hashes.update(
            json.loads(line)["metadata"].get("needle_hashes", []) or [])
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
                 source_refs=SRC, idempotency_key="r4-chain-key-1")
    assert ei.value.code == "IDEMPOTENCY_MISMATCH"
    row = db.execute("SELECT value_json, state, revision FROM memories WHERE id=?",
                     (rec["id"],)).fetchone()
    assert json.loads(row[0]) == TOMBSTONE and row[1] == "erased"
    assert row[2] == out1["revision"]
    assert db.execute(
        "SELECT COUNT(*) FROM memories WHERE scope_id='s'").fetchone()[0] == 1
    db.close()
