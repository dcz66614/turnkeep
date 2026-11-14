"""T009 idempotency tests."""
import pytest
from turnkeep.storage import open_db, transaction
from turnkeep.idempotency import (request_digest, legacy_request_digest,
                                  check, check_any, store,
                                  DIGEST_VERSION, LEGACY_DIGEST_VERSION)
from turnkeep.errors import TurnkeepError


def _setup(tmp_path):
    db = open_db(tmp_path / "i.sqlite3")
    from turnkeep.scopes import register_scope
    register_scope(db, "s1"); register_scope(db, "s2")
    return db


def test_same_key_same_digest_returns_stored(tmp_path):
    db = _setup(tmp_path)
    d = request_digest("finish", "s1", "cli:local", {"run": "r1"})
    with transaction(db, write=True):
        store(db, "s1", "cli:local", "finish", "k1", d, {"receipt": "abc"})
    assert check(db, "s1", "cli:local", "finish", "k1", d) == {"receipt": "abc"}
    db.close()


def test_same_key_different_digest_mismatch(tmp_path):
    db = _setup(tmp_path)
    d1 = request_digest("finish", "s1", "cli:local", {"run": "r1"})
    d2 = request_digest("finish", "s1", "cli:local", {"run": "r2"})
    assert d1 != d2
    with transaction(db, write=True):
        store(db, "s1", "cli:local", "finish", "k1", d1, {"receipt": "abc"})
    with pytest.raises(TurnkeepError) as ei:
        check(db, "s1", "cli:local", "finish", "k1", d2)
    assert ei.value.code == "IDEMPOTENCY_MISMATCH"
    db.close()


def test_same_key_other_scope_does_not_leak(tmp_path):
    db = _setup(tmp_path)
    d = request_digest("finish", "s1", "cli:local", {"run": "r1"})
    with transaction(db, write=True):
        store(db, "s1", "cli:local", "finish", "k1", d, {"receipt": "abc"})
    assert check(db, "s2", "cli:local", "finish", "k1", d) is None
    db.close()


def test_digest_versioned(tmp_path):
    # P1-a: the finish digest changed (now covers parent_ids), so the
    # version bumped 1 -> 2; v1 stays available only as a legacy replay path.
    assert DIGEST_VERSION == 2
    assert LEGACY_DIGEST_VERSION == 1
    assert request_digest("finish", "s1", "cli:local", {"a": 1}) == request_digest("finish", "s1", "cli:local", {"a": 1})


def test_legacy_digest_differs_from_v2_and_is_deterministic(tmp_path):
    req = {"run": "r1"}
    v1 = legacy_request_digest("finish", "s1", "cli:local", req)
    v2 = request_digest("finish", "s1", "cli:local", req)
    assert v1 != v2
    assert v1 == legacy_request_digest("finish", "s1", "cli:local", req)
    assert v1 == request_digest("finish", "s1", "cli:local", req, version=1)


def test_check_any_accepts_legacy_digest(tmp_path):
    db = _setup(tmp_path)
    v1 = legacy_request_digest("finish", "s1", "cli:local", {"run": "r1"})
    v2 = request_digest("finish", "s1", "cli:local", {"run": "r1"})
    with transaction(db, write=True):
        store(db, "s1", "cli:local", "finish", "k1", v1, {"receipt": "abc"})
    # Stored v1 digest: accepted via the legacy slot, not the v2 slot.
    assert check_any(db, "s1", "cli:local", "finish", "k1", (v2, v1)) == {"receipt": "abc"}
    # The strict check() still rejects a v2-only digest against a v1 row.
    with pytest.raises(TurnkeepError) as ei:
        check(db, "s1", "cli:local", "finish", "k1", v2)
    assert ei.value.code == "IDEMPOTENCY_MISMATCH"
    # check_any with neither digest matching still raises.
    other = request_digest("finish", "s1", "cli:local", {"run": "r2"})
    with pytest.raises(TurnkeepError) as ei:
        check_any(db, "s1", "cli:local", "finish", "k1", (v2, other))
    assert ei.value.code == "IDEMPOTENCY_MISMATCH"
    # Unseen key still returns None.
    assert check_any(db, "s1", "cli:local", "finish", "nope", (v2, v1)) is None
    db.close()


# ---- P1-R2-04: idempotency for the five non-Finish ops -------------------
# Same mechanism as finish (same store/lookup, same replay behaviour):
# check first inside the write tx, store in the same tx as the write.

SRC = [{"id": "test:1", "type": "user_statement"}]


def _count(db, table, where=""):
    return db.execute(f"SELECT COUNT(*) FROM {table} {where}").fetchone()[0]


def test_remember_repeat_same_key_one_write(tmp_path):
    """Required regression: repeat a remember with the same key ->
    identical response, exactly one record written."""
    from turnkeep.memory import remember
    db = _setup(tmp_path)
    r1 = remember(db, "s1", "k", "v", source_refs=SRC, idempotency_key="remember-key-1")
    r2 = remember(db, "s1", "k", "v", source_refs=SRC, idempotency_key="remember-key-1")
    assert r1 == r2
    assert _count(db, "memories", "WHERE scope_id='s1'") == 1
    assert _count(db, "idempotency", "WHERE operation='remember'") == 1
    db.close()


def test_remember_same_key_different_value_mismatch(tmp_path):
    from turnkeep.memory import remember
    db = _setup(tmp_path)
    remember(db, "s1", "k", "v", source_refs=SRC, idempotency_key="remember-key-1")
    with pytest.raises(TurnkeepError) as ei:
        remember(db, "s1", "k", "different", source_refs=SRC, idempotency_key="remember-key-1")
    assert ei.value.code == "IDEMPOTENCY_MISMATCH"
    assert _count(db, "memories", "WHERE scope_id='s1'") == 1
    db.close()


def test_remember_replay_after_correction_returns_original(tmp_path):
    """A replay must return the stored response even though the record
    has since been corrected (a naive re-run would see a new revision)."""
    from turnkeep.memory import remember
    from turnkeep.corrections import correct
    db = _setup(tmp_path)
    r1 = remember(db, "s1", "k", "v", source_refs=SRC, idempotency_key="remember-key-1")
    correct(db, "s1", r1["id"], 1, "v2", source_refs=SRC)
    r2 = remember(db, "s1", "k", "v", source_refs=SRC, idempotency_key="remember-key-1")
    assert r2 == r1 and r2["value"] == "v"
    db.close()


def test_begin_repeat_same_key_one_run(tmp_path):
    from turnkeep.runs import begin
    db = _setup(tmp_path)
    b1 = begin(db, "s1", "work", idempotency_key="begin-key-123")
    b2 = begin(db, "s1", "work", idempotency_key="begin-key-123")
    assert b1 == b2
    assert b1["run_id"] == b2["run_id"]
    assert _count(db, "runs", "WHERE scope_id='s1'") == 1
    db.close()


def test_begin_same_key_different_task_mismatch(tmp_path):
    from turnkeep.runs import begin
    db = _setup(tmp_path)
    begin(db, "s1", "work", idempotency_key="begin-key-123")
    with pytest.raises(TurnkeepError) as ei:
        begin(db, "s1", "other work", idempotency_key="begin-key-123")
    assert ei.value.code == "IDEMPOTENCY_MISMATCH"
    assert _count(db, "runs", "WHERE scope_id='s1'") == 1
    db.close()


def test_begin_without_key_still_makes_distinct_runs(tmp_path):
    """No key -> no dedup: two begins are two runs (the key is opt-in
    in core, as for finish)."""
    from turnkeep.runs import begin
    db = _setup(tmp_path)
    assert begin(db, "s1", "work")["run_id"] != begin(db, "s1", "work")["run_id"]
    assert _count(db, "runs", "WHERE scope_id='s1'") == 2
    db.close()


def test_correct_repeat_same_key_one_write(tmp_path):
    from turnkeep.memory import remember
    from turnkeep.corrections import correct
    db = _setup(tmp_path)
    rec = remember(db, "s1", "k", "v1", source_refs=SRC)
    c1 = correct(db, "s1", rec["id"], 1, "v2", source_refs=SRC, idempotency_key="correct-key-1")
    # A naive second correct would raise VERSION_CONFLICT (old is now
    # superseded, revision moved); the replay returns the stored record.
    c2 = correct(db, "s1", rec["id"], 1, "v2", source_refs=SRC, idempotency_key="correct-key-1")
    assert c1 == c2
    assert _count(db, "memories", "WHERE scope_id='s1' AND state='active'") == 1
    assert _count(db, "memories", "WHERE scope_id='s1'") == 2  # old + one new
    db.close()


def test_retract_repeat_same_key_one_write(tmp_path):
    from turnkeep.memory import remember
    from turnkeep.corrections import retract
    db = _setup(tmp_path)
    rec = remember(db, "s1", "k", "v", source_refs=SRC)
    t1 = retract(db, "s1", rec["id"], 1, "correction", idempotency_key="retract-key-1")
    t2 = retract(db, "s1", rec["id"], 1, "correction", idempotency_key="retract-key-1")
    assert t1 == t2 and t1["state"] == "retracted"
    assert _count(db, "memories", "WHERE scope_id='s1'") == 1
    db.close()


def test_resolve_repeat_same_key_one_write(tmp_path):
    from turnkeep.memory import remember, list_conflicts
    from turnkeep.corrections import resolve_conflict
    db = _setup(tmp_path)
    remember(db, "s1", "k", "a", source_refs=SRC)
    remember(db, "s1", "k", "b", source_refs=SRC)
    cands = [{"id": r["id"], "revision": r["revision"]} for r in list_conflicts(db, "s1", "k")]
    assert len(cands) == 2
    s1 = resolve_conflict(db, "s1", "k", cands, "a", source_refs=SRC,
                          idempotency_key="resolve-key-1")
    # Candidates are superseded now; a naive re-run would raise
    # CANDIDATE_SET_CHANGED. The replay returns the stored record.
    s2 = resolve_conflict(db, "s1", "k", cands, "a", source_refs=SRC,
                          idempotency_key="resolve-key-1")
    assert s1 == s2
    assert _count(db, "memories", "WHERE scope_id='s1' AND state='active'") == 1
    assert _count(db, "memories", "WHERE scope_id='s1'") == 3  # 2 candidates + 1 new
    db.close()


def test_same_key_different_operation_does_not_leak(tmp_path):
    """Keys are scoped to (scope, principal, operation): the same key
    string on remember and on begin are independent."""
    from turnkeep.memory import remember
    from turnkeep.runs import begin
    db = _setup(tmp_path)
    remember(db, "s1", "k", "v", source_refs=SRC, idempotency_key="shared-key-1")
    run = begin(db, "s1", "work", idempotency_key="shared-key-1")
    assert run["status"] == "open"
    assert _count(db, "idempotency", "WHERE operation='remember'") == 1
    assert _count(db, "idempotency", "WHERE operation='begin'") == 1
    db.close()
