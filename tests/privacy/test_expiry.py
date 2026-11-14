"""T038 expiry: explicit valid_until only; expired leaves the view and taints children."""
import pytest

from turnkeep.errors import TurnkeepError
from turnkeep.storage import open_db
from turnkeep.scopes import register_scope
from turnkeep.memory import remember
from turnkeep.retrieval import recall, search_by_key
from turnkeep.retention import set_expiry, sweep_expired

SRC = [{"id": "u:1", "type": "user_statement"}]


def _db(tmp_path):
    db = open_db(tmp_path / "x.sqlite3")
    register_scope(db, "x")
    return db


def test_set_expiry_and_sweep(tmp_path):
    db = _db(tmp_path)
    req = remember(db, "x", "token.ttl", "short-lived", kind="fact", source_refs=SRC)
    child = remember(db, "x", "uses.token", "yes", kind="decision", source_refs=SRC,
                     parent_ids=[req["id"]])
    set_expiry(db, "x", req["id"], "2026-01-01T00:00:00+00:00", req["revision"])
    out = sweep_expired(db, "x", now="2026-06-01T00:00:00+00:00")
    assert out["expired"] == [req["id"]]
    assert child["id"] in out["tainted"]
    assert search_by_key(db, "x", "token.ttl") == []  # out of the active view
    assert search_by_key(db, "x", "token.ttl", include_history=True)[0]["state"] == "expired"
    assert recall(db, "x", "uses.token") == []  # tainted child is not usable
    db.close()


def test_sweep_is_idempotent_and_ignores_future_expiry(tmp_path):
    db = _db(tmp_path)
    a = remember(db, "x", "a", "1", kind="fact", source_refs=SRC)
    set_expiry(db, "x", a["id"], "2027-01-01T00:00:00+00:00", a["revision"])
    assert sweep_expired(db, "x", now="2026-06-01T00:00:00+00:00")["expired"] == []
    b = remember(db, "x", "b", "2", kind="fact", source_refs=SRC)
    set_expiry(db, "x", b["id"], "2026-01-01T00:00:00+00:00", b["revision"])
    assert sweep_expired(db, "x", now="2026-06-01T00:00:00+00:00")["expired"] == [b["id"]]
    assert sweep_expired(db, "x", now="2026-06-01T00:00:00+00:00")["expired"] == []
    db.close()


def test_set_expiry_bumps_revision_and_cites_its_event(tmp_path):
    # P1-R2-03b: set_expiry() is a CAS write — it must bump revision and
    # cite its own expire_set event, so a stale expected_revision fails.
    db = _db(tmp_path)
    r = remember(db, "x", "doc", "v1", kind="fact", source_refs=SRC)
    assert r["revision"] == 1
    out = set_expiry(db, "x", r["id"], "2030-01-01T00:00:00Z", 1)
    assert out["revision"] == 2
    assert out["valid_until"] == "2030-01-01T00:00:00Z"
    ev = db.execute(
        "SELECT id FROM events WHERE scope_id='x' AND event_type='expire_set' "
        "ORDER BY scope_epoch DESC LIMIT 1").fetchone()[0]
    assert out["cause_event_id"] == ev
    assert out["cause_event_id"] != r["cause_event_id"]  # no longer the remember event
    # Same stale expected_revision must now conflict (CAS hole closed).
    with pytest.raises(TurnkeepError) as ei:
        set_expiry(db, "x", r["id"], "2031-06-01T00:00:00Z", 1)
    assert ei.value.code == "VERSION_CONFLICT"
    # The current revision still works.
    out2 = set_expiry(db, "x", r["id"], "2031-06-01T00:00:00Z", 2)
    assert out2["revision"] == 3
    assert out2["valid_until"] == "2031-06-01T00:00:00Z"
    db.close()


def test_no_probabilistic_decay(tmp_path):
    # A record with no valid_until never expires, however old it is.
    db = _db(tmp_path)
    r = remember(db, "x", "old", "still valid", kind="fact", source_refs=SRC)
    assert sweep_expired(db, "x", now="2099-01-01T00:00:00+00:00")["expired"] == []
    assert search_by_key(db, "x", "old")[0]["id"] == r["id"]
    db.close()
