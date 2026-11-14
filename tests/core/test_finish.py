"""T014 finish tests."""
import pytest
from turnkeep.storage import open_db, scope_epoch, row_or_404, dump_record
from turnkeep.scopes import register_scope
from turnkeep.memory import remember
from turnkeep.runs import begin
from turnkeep.commits import finish
from turnkeep.corrections import correct
from turnkeep.errors import TurnkeepError
from turnkeep.idempotency import legacy_request_digest

SRC = [{"id": "u:1", "type": "user_statement"}]


def _db(tmp_path):
    db = open_db(tmp_path / "f.sqlite3"); register_scope(db, "s1"); return db


def test_finish_commits_once_and_replay_returns_receipt(tmp_path):
    db = _db(tmp_path)
    req = remember(db, "s1", "req", "3.11", kind="requirement", source_refs=SRC)
    run = begin(db, "s1", "work")
    receipt = finish(db, "s1", run["run_id"], {"key": "out", "value": "done"},
                     idempotency_key="key-1234")
    assert receipt["status_at_commit"] == "active"
    assert receipt["guarantee"] == "local_sqlite_commit_only"
    replay = finish(db, "s1", run["run_id"], {"key": "out", "value": "done"},
                    idempotency_key="key-1234")
    assert replay == receipt
    assert db.execute("SELECT COUNT(*) FROM memories WHERE key='out'").fetchone()[0] == 1
    db.close()


def test_same_key_different_request_mismatch(tmp_path):
    db = _db(tmp_path)
    remember(db, "s1", "req", "3.11", kind="requirement", source_refs=SRC)
    run = begin(db, "s1", "work")
    finish(db, "s1", run["run_id"], {"key": "out", "value": "done"}, idempotency_key="key-1234")
    run2 = begin(db, "s1", "work2")
    # same key, different result payload on a NEW run: the key is already used
    with pytest.raises(TurnkeepError) as ei:
        finish(db, "s1", run2["run_id"], {"key": "out", "value": "different"}, idempotency_key="key-1234")
    assert ei.value.code == "IDEMPOTENCY_MISMATCH"
    db.close()


def test_stale_snapshot_rejected_with_details(tmp_path):
    db = _db(tmp_path)
    req = remember(db, "s1", "req", "3.11", kind="requirement", source_refs=SRC)
    run = begin(db, "s1", "work")
    correct(db, "s1", req["id"], 1, "3.12", source_refs=SRC)
    with pytest.raises(TurnkeepError) as ei:
        finish(db, "s1", run["run_id"], {"key": "out", "value": "x"})
    assert ei.value.code == "STALE_SNAPSHOT"
    assert "req" in ei.value.details.get("changed_keys", [])
    assert db.execute("SELECT COUNT(*) FROM memories WHERE key='out'").fetchone()[0] == 0
    db.close()


def test_empty_parent_ids_still_inherit_snapshot_parents(tmp_path):
    db = _db(tmp_path)
    req = remember(db, "s1", "req", "3.11", kind="requirement", source_refs=SRC)
    run = begin(db, "s1", "work")
    receipt = finish(db, "s1", run["run_id"], {"key": "out", "value": "done"}, parent_ids=[])
    assert req["id"] in receipt["parent_ids"]
    # ...so a later change to req taints the result
    correct(db, "s1", req["id"], 1, "3.12", source_refs=SRC)
    assert dump_record(db, row_or_404(db, "s1", receipt["record_id"]))["state"] == "tainted"
    db.close()


def test_replay_same_parents_returns_receipt(tmp_path):
    # P1-a: same run + same result + same parents -> original receipt, even
    # when the parents are declared explicitly on the replay.
    db = _db(tmp_path)
    req = remember(db, "s1", "req", "3.11", kind="requirement", source_refs=SRC)
    run = begin(db, "s1", "work")
    result = {"key": "out", "value": "done"}
    receipt = finish(db, "s1", run["run_id"], result, parent_ids=[req["id"]],
                     idempotency_key="key-1234")
    assert finish(db, "s1", run["run_id"], result, parent_ids=[req["id"]],
                  idempotency_key="key-1234") == receipt
    # Declared parents already in the snapshot merge to the same set.
    assert finish(db, "s1", run["run_id"], result, parent_ids=[],
                  idempotency_key="key-1234") == receipt
    assert db.execute("SELECT COUNT(*) FROM memories WHERE key='out'").fetchone()[0] == 1
    db.close()


def test_replay_different_parents_mismatch(tmp_path):
    # P1-a: same run + same result + DIFFERENT parent_ids on replay must
    # raise IDEMPOTENCY_MISMATCH, never return the old receipt. The extra
    # parent is remembered after begin (so it is outside the snapshot and
    # changes the merged parent set); replay precedence means the digest
    # check fires before the epoch check.
    db = _db(tmp_path)
    remember(db, "s1", "req", "3.11", kind="requirement", source_refs=SRC)
    run = begin(db, "s1", "work")
    result = {"key": "out", "value": "done"}
    receipt = finish(db, "s1", run["run_id"], result, idempotency_key="key-1234")
    extra = remember(db, "s1", "extra", "9.99", kind="requirement", source_refs=SRC)
    with pytest.raises(TurnkeepError) as ei:
        finish(db, "s1", run["run_id"], result, parent_ids=[extra["id"]],
               idempotency_key="key-1234")
    assert ei.value.code == "IDEMPOTENCY_MISMATCH"
    # Same-parents replay still returns the original receipt.
    assert finish(db, "s1", run["run_id"], result,
                  idempotency_key="key-1234") == receipt
    assert db.execute("SELECT COUNT(*) FROM memories WHERE key='out'").fetchone()[0] == 1
    db.close()


def test_replay_different_parents_mismatch_no_key(tmp_path):
    # P1-a, run-bound fallback path (no idempotency key): different parents
    # must still raise IDEMPOTENCY_MISMATCH rather than returning the receipt.
    db = _db(tmp_path)
    remember(db, "s1", "req", "3.11", kind="requirement", source_refs=SRC)
    run = begin(db, "s1", "work")
    result = {"key": "out", "value": "done"}
    receipt = finish(db, "s1", run["run_id"], result)
    extra = remember(db, "s1", "extra", "9.99", kind="requirement", source_refs=SRC)
    with pytest.raises(TurnkeepError) as ei:
        finish(db, "s1", run["run_id"], result, parent_ids=[extra["id"]])
    assert ei.value.code == "IDEMPOTENCY_MISMATCH"
    assert finish(db, "s1", run["run_id"], result) == receipt
    db.close()


def test_legacy_v1_digest_still_replays_run_bound(tmp_path):
    # P1-a compat: a run accepted under the v1 digest (run_id + result only,
    # no parent_ids) still replays to its original receipt. Simulated by
    # overwriting the stored run digest with a manually computed v1 digest.
    db = _db(tmp_path)
    remember(db, "s1", "req", "3.11", kind="requirement", source_refs=SRC)
    run = begin(db, "s1", "work")
    result = {"key": "out", "value": "done"}
    receipt = finish(db, "s1", run["run_id"], result)
    v1 = legacy_request_digest("finish", "s1", "local",
                               {"run_id": run["run_id"], "result": result})
    assert v1 != receipt["request_digest"]
    db.execute("UPDATE runs SET request_digest=? WHERE scope_id='s1' AND id=?",
               (v1, run["run_id"]))
    db.commit()
    assert finish(db, "s1", run["run_id"], result) == receipt
    db.close()


def test_legacy_v1_digest_still_replays_with_key(tmp_path):
    # P1-a compat, idempotency-key path: stored v1 digests in BOTH the runs
    # row and the idempotency row still replay to the original receipt.
    db = _db(tmp_path)
    remember(db, "s1", "req", "3.11", kind="requirement", source_refs=SRC)
    run = begin(db, "s1", "work")
    result = {"key": "out", "value": "done"}
    receipt = finish(db, "s1", run["run_id"], result, idempotency_key="key-1234")
    v1 = legacy_request_digest("finish", "s1", "local",
                               {"run_id": run["run_id"], "result": result})
    db.execute("UPDATE runs SET request_digest=? WHERE scope_id='s1' AND id=?",
               (v1, run["run_id"]))
    db.execute("UPDATE idempotency SET request_digest=? WHERE idempotency_key='key-1234'",
               (v1,))
    db.commit()
    assert finish(db, "s1", run["run_id"], result,
                  idempotency_key="key-1234") == receipt
    db.close()
