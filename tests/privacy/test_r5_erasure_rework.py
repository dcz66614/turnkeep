"""R5 rework regression tests: P0-R5-01 (repair erase accepts only
controlled reason labels), P2-R5-02 (payload scan scope-filtered).

Written BEFORE the fix where feasible: the glued-reason cases and the
cross-scope cases fail on the pre-fix code (verified against a HEAD
checkout of erasure.py) and must pass after it.
"""
import json

import pytest

from turnkeep.storage import open_db, publish_event
from turnkeep.scopes import register_scope
from turnkeep.memory import remember
from turnkeep.errors import TurnkeepError
from turnkeep.erasure import erase_record, residual_report
from turnkeep.portability import export_scope

SRC = [{"id": "u:1", "type": "user_statement"}]
SECRET = "sk-LONG_SYNTHETIC_SECRET_247890"
TOMBSTONE = {"_erased": True}


def _db(tmp_path, scopes=("s",)):
    db = open_db(tmp_path / "t.sqlite3")
    for scope in scopes:
        register_scope(db, scope)
    return db


def _erase_events(db, scope=None):
    if scope is None:
        rows = db.execute(
            "SELECT metadata_json FROM events WHERE event_type='erase'").fetchall()
    else:
        rows = db.execute(
            "SELECT metadata_json FROM events WHERE event_type='erase'"
            " AND scope_id=?", (scope,)).fetchall()
    return [json.loads(r[0]) for r in rows]


def _first_erase(db, scope, rec):
    out = erase_record(db, scope, rec["id"], rec["revision"], "rotation")
    assert out["status"] == "COMPLETE"
    return out


# ---------------------------------------------------------------- P0-R5-01

@pytest.mark.parametrize("bad_reason", [
    f"rotation:{SECRET}",
    f"rotated={SECRET}",
    f"?api_key={SECRET}",
    f"x{SECRET}y",
    f"ab:{SECRET}-cd",
])
def test_glued_secret_in_repair_reason_rejected(tmp_path, bad_reason):
    # GPT's P0-R5-01 repros: a repair-erase reason carrying the secret
    # glued into surrounding text must be rejected. The last two cases
    # are the ones the old token matching could NOT catch (no token
    # boundary around the secret); the first three were caught by token
    # matching before and must stay rejected under the allowlist.
    db = _db(tmp_path)
    rec = remember(db, "s", "k", {"api_key": SECRET}, kind="fact",
                   source_refs=SRC, idempotency_key="r5-glued-key-1")
    out1 = _first_erase(db, "s", rec)
    assert len(_erase_events(db)) == 1
    with pytest.raises(TurnkeepError) as ei:
        erase_record(db, "s", rec["id"], out1["revision"], bad_reason)
    assert ei.value.code == "INVALID_INPUT"
    assert "repair" in str(ei.value) and "rotation" in str(ei.value)
    # Nothing new was published: still exactly one erase event, and no
    # event metadata holds the secret.
    assert len(_erase_events(db)) == 1
    assert SECRET not in json.dumps(_erase_events(db))
    assert SECRET not in db.execute(
        "SELECT group_concat(metadata_json) FROM events").fetchone()[0]
    db.close()


@pytest.mark.parametrize("value,first_reason,repair_reason", [
    ("repair", "rotation", "repair"),
    ("rotation", "repair", "rotation"),
])
def test_repair_label_equal_to_payload_rejected(tmp_path, value,
                                               first_reason, repair_reason):
    # A label could itself be the needle: a payload whose value is
    # literally "repair", repaired with reason "repair", must be
    # rejected — the label is hash-checked against the stored needle
    # hashes, not just allowlisted.
    db = _db(tmp_path)
    rec = remember(db, "s", "k", value, kind="fact", source_refs=SRC)
    out1 = erase_record(db, "s", rec["id"], rec["revision"], first_reason)
    assert out1["status"] == "COMPLETE"
    assert len(_erase_events(db)) == 1
    with pytest.raises(TurnkeepError) as ei:
        erase_record(db, "s", rec["id"], out1["revision"], repair_reason)
    assert ei.value.code == "INVALID_INPUT"
    assert len(_erase_events(db)) == 1
    # The payload value itself must not have leaked anywhere either:
    # the repair event was never published.
    assert value not in json.dumps(_erase_events(db))
    db.close()


@pytest.mark.parametrize("repair_reason", ["repair", "rotation"])
def test_clean_repair_erase_still_succeeds(tmp_path, repair_reason):
    # The allowlist must not block a clean repair erase with either
    # controlled label.
    db = _db(tmp_path)
    rec = remember(db, "s", "k", {"api_key": SECRET}, kind="fact",
                   source_refs=SRC, idempotency_key="r5-clean-key-1")
    out1 = _first_erase(db, "s", rec)
    out2 = erase_record(db, "s", rec["id"], out1["revision"], repair_reason)
    assert out2["status"] == "COMPLETE" and out2["erased"] is True
    assert out2.get("repair") is True
    assert out2["revision"] == out1["revision"]  # no second bump
    assert out2["remaining"] == {}
    # Only the label is persisted in the repair event, never free text.
    metas = _erase_events(db)
    assert len(metas) == 2
    assert metas[1]["reason"] == repair_reason
    assert metas[1]["repair"] is True
    assert residual_report(db, "s")["controlled"]["live_tables_clean"] is True
    db.close()


def test_repair_erase_arbitrary_reason_rejected(tmp_path):
    # Any non-allowlisted reason — even one with no secret in it — is
    # INVALID_INPUT on the repair path. Free text is never persisted
    # by a repair erase, so there is no safe free-text reason.
    # (R6 note: "just cleaning up" stays the rejection pin — it is a
    # non-label, so it is still rejected; replacing it with the label
    # "repair" would make this call succeed and invert the test.)
    db = _db(tmp_path)
    rec = remember(db, "s", "k", "v", kind="fact", source_refs=SRC)
    out1 = _first_erase(db, "s", rec)
    with pytest.raises(TurnkeepError) as ei:
        erase_record(db, "s", rec["id"], out1["revision"], "just cleaning up")
    assert ei.value.code == "INVALID_INPUT"
    assert len(_erase_events(db)) == 1
    db.close()


def test_first_erase_reason_is_controlled_label(tmp_path):
    # R6 revision (P0-R6-01) of the old test_first_erase_free_text_
    # reason_unchanged, which pinned free-text persistence
    # ("rotating the key after the incident"). That behaviour is the
    # vulnerability: a free-text reason can carry a secret glued into
    # a longer token or reversibly encoded, with no verbatim needle
    # for any check to catch, and the text then lands in the
    # append-only event log. A first erase now accepts only the
    # controlled labels ERASE_REASONS, and only the label is persisted
    # in the erase event's metadata. The old free-text reason is now
    # INVALID_INPUT and publishes nothing.
    db = _db(tmp_path)
    rec = remember(db, "s", "k", {"api_key": SECRET}, kind="fact",
                   source_refs=SRC)
    out = erase_record(db, "s", rec["id"], rec["revision"], "rotation")
    assert out["status"] == "COMPLETE" and out["erased"] is True
    assert _erase_events(db)[0]["reason"] == "rotation"
    # Free text is rejected on the first-erase path too, and publishes
    # nothing — including the exact sentence the old test pinned.
    # (A fresh key: the first DB still holds erased key "k", and
    # re-remembering it would need explicit replacements.)
    rec = remember(db, "s", "k2", {"api_key": SECRET}, kind="fact",
                   source_refs=SRC)
    with pytest.raises(TurnkeepError) as ei:
        erase_record(db, "s", rec["id"], rec["revision"],
                     "rotating the key after the incident")
    assert ei.value.code == "INVALID_INPUT"
    assert "cleanup" in str(ei.value) and "repair" in str(ei.value) \
        and "rotation" in str(ei.value)
    assert len(_erase_events(db)) == 1
    db.close()


# ---------------------------------------------------------------- P2-R5-02

def test_same_secret_in_other_scope_is_not_a_residual(tmp_path):
    # GPT's P2-R5-02 repro: two scopes hold the same secret. Erasing
    # in s1 must return COMPLETE with empty remaining, and the erase
    # event must name no record from s2 (pre-fix the unscoped scan
    # reported s2's live record as a PARTIAL residual, disclosing its
    # id and key in the persisted `remaining` entry).
    db = _db(tmp_path, scopes=("s1", "s2"))
    r1 = remember(db, "s1", "k", {"api_key": SECRET}, kind="fact",
                  source_refs=SRC)
    r2 = remember(db, "s2", "k", {"api_key": SECRET}, kind="fact",
                  source_refs=SRC)
    out = erase_record(db, "s1", r1["id"], r1["revision"], "rotation")
    assert out["status"] == "COMPLETE" and out["erased"] is True
    assert out["remaining"] == {}
    metas = _erase_events(db, "s1")
    assert len(metas) == 1
    assert metas[0]["remaining"] == []
    assert r2["id"] not in json.dumps(metas)
    # s2's record is untouched: still active, still holding the secret.
    row = db.execute("SELECT value_json, state FROM memories WHERE id=?",
                     (r2["id"],)).fetchone()
    assert SECRET in row[0] and row[1] == "active"
    assert residual_report(db, "s1")["controlled"]["live_tables_clean"] is True
    db.close()


def test_append_only_hit_in_other_scope_does_not_refuse(tmp_path):
    # The step-1 append-only check is scope-filtered too: a payload
    # present only in another scope's events must not refuse this
    # scope's erase (pre-fix the unscoped scan refused it PARTIAL).
    db = _db(tmp_path, scopes=("s1", "s2"))
    r1 = remember(db, "s1", "k", {"api_key": SECRET}, kind="fact",
                  source_refs=SRC)
    publish_event(db, "s2", "note", "local", {"someone-else": None},
                  metadata={"leaked": SECRET})
    out = erase_record(db, "s1", r1["id"], r1["revision"], "rotation")
    assert out["status"] == "COMPLETE" and out["erased"] is True
    assert out["remaining"] == {}
    db.close()


def test_same_scope_second_copy_still_reported(tmp_path):
    # The scope filter must not blind the scan within a scope: a second
    # live record sharing the payload IN THE SAME scope is still a
    # PARTIAL residual, named by its own record id (not the other
    # scope's).
    db = _db(tmp_path, scopes=("s1", "s2"))
    r1 = remember(db, "s1", "k1", {"api_key": SECRET}, kind="fact",
                  source_refs=SRC)
    r2 = remember(db, "s1", "k2", {"api_key": SECRET}, kind="fact",
                  source_refs=SRC)
    remember(db, "s2", "k", {"api_key": SECRET}, kind="fact",
             source_refs=SRC)
    out = erase_record(db, "s1", r1["id"], r1["revision"], "rotation")
    assert out["status"] == "PARTIAL" and out["erased"] is True
    assert "memories" in out["remaining"]
    metas = _erase_events(db, "s1")
    remaining = metas[0]["remaining"]
    assert any(e.get("record_id") == r2["id"] for e in remaining)
    assert residual_report(db, "s1")["controlled"]["live_tables_clean"] is False
    db.close()


# ------------------------------------------------- end-to-end privacy chain

def test_end_to_end_privacy_chain_after_repair(tmp_path):
    """First erase → repair erase → residual report → history export:
    the exported events.jsonl contains no plaintext secret, and
    residual_report() says live_tables_clean=true."""
    db = _db(tmp_path)
    rec = remember(db, "s", "k", {"api_key": SECRET}, kind="fact",
                   source_refs=SRC, idempotency_key="r5-chain-key-1")
    out1 = _first_erase(db, "s", rec)
    out2 = erase_record(db, "s", rec["id"], out1["revision"], "repair")
    assert out2["status"] == "COMPLETE" and out2.get("repair") is True
    rep = residual_report(db, "s")
    assert rep["controlled"]["live_tables_clean"] is True
    assert rep["controlled"]["controlled_residuals"] == []
    out_dir = tmp_path / "export"
    export_scope(db, "s", out_dir)
    events_text = (out_dir / "events.jsonl").read_text()
    assert SECRET not in events_text
    assert SECRET not in (out_dir / "records.jsonl").read_text()
    # Every erase event in the export carries only a controlled label
    # as its reason — no free text that could hold the secret.
    for line in events_text.splitlines():
        event = json.loads(line)
        if event.get("event_type") == "erase":
            assert event["metadata"]["reason"] in ("repair", "rotation")
    db.close()
