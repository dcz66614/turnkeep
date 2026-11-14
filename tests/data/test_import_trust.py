"""T036 import trust: tampering, future formats, missing parents, and
untrusted incoming claims are rejected or quarantined — never silently fixed."""
import hashlib
import json
from pathlib import Path

import pytest

from turnkeep.storage import open_db, usable
from turnkeep.scopes import register_scope
from turnkeep.errors import TurnkeepError
from turnkeep.portability import export_scope, import_package, _remap_id

FIX = Path(__file__).resolve().parents[2] / "fixtures" / "imports"


def _make_pkg(pkg, recs, scope="t"):
    """Write a minimal hand-built export package from record dicts."""
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "records.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs))
    (pkg / "events.jsonl").write_text("")
    files = {n: hashlib.sha256((pkg / n).read_bytes()).hexdigest()
             for n in ("records.jsonl", "events.jsonl")}
    (pkg / "manifest.json").write_text(json.dumps(
        {"format": "turnkeep-export", "schema_version": 1, "scope_id": scope,
         "exported_at": "2026-11-08T00:00:00Z", "tool": "test", "record_count": len(recs),
         "event_count": 0, "files": files}) + "\n")


def _db(tmp_path, scope="t"):
    db = open_db(tmp_path / "t.sqlite3")
    register_scope(db, scope)
    return db


def test_tampered_manifest_rejected(tmp_path):
    db = _db(tmp_path)
    with pytest.raises(TurnkeepError) as ei:
        import_package(db, FIX / "tampered")
    assert ei.value.code == "INVALID_INPUT"
    assert db.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
    db.close()


def test_future_schema_version_rejected(tmp_path):
    db = _db(tmp_path)
    with pytest.raises(TurnkeepError) as ei:
        import_package(db, FIX / "future-version")
    assert ei.value.code == "UNSUPPORTED"
    db.close()


def test_missing_parent_quarantined_not_repaired(tmp_path):
    # A record whose parent is not in the package is skipped, never given a
    # synthetic parent; the rest of the package still imports.
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    recs = [
        {"id": "o1", "key": "a", "value": "1", "kind": "fact", "state": "active",
         "revision": 1, "source_refs": [{"id": "u:1", "type": "user_statement"}],
         "parent_ids": [], "supersedes_ids": []},
        {"id": "o2", "key": "b", "value": "2", "kind": "decision", "state": "active",
         "revision": 1, "source_refs": [{"id": "u:1", "type": "user_statement"}],
         "parent_ids": ["o-missing"], "supersedes_ids": []},
    ]
    (pkg / "records.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs))
    (pkg / "events.jsonl").write_text("")
    import hashlib
    files = {n: hashlib.sha256((pkg / n).read_bytes()).hexdigest() for n in ("records.jsonl", "events.jsonl")}
    (pkg / "manifest.json").write_text(json.dumps(
        {"format": "turnkeep-export", "schema_version": 1, "scope_id": "t",
         "exported_at": "2026-11-08T00:00:00Z", "tool": "test", "record_count": 2,
         "event_count": 0, "files": files}) + "\n")
    db = _db(tmp_path)
    imp = import_package(db, pkg)
    assert imp["imported"] == 1
    assert len(imp["quarantined"]) == 1 and imp["quarantined"][0]["key"] == "b"
    assert db.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
    db.close()


def test_active_claim_with_quarantined_parent_becomes_tainted(tmp_path):
    # The parent is in the package but is itself quarantined (unknown source
    # type); the child's incoming "active" claim is not trusted.
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    recs = [
        {"id": "o1", "key": "parent", "value": "p", "kind": "fact", "state": "active",
         "revision": 1, "source_refs": [{"id": "u:1", "type": "mystery_source"}],
         "parent_ids": [], "supersedes_ids": []},
        {"id": "o2", "key": "child", "value": "c", "kind": "decision", "state": "active",
         "revision": 1, "source_refs": [{"id": "u:1", "type": "user_statement"}],
         "parent_ids": ["o1"], "supersedes_ids": []},
    ]
    (pkg / "records.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs))
    (pkg / "events.jsonl").write_text("")
    import hashlib
    files = {n: hashlib.sha256((pkg / n).read_bytes()).hexdigest() for n in ("records.jsonl", "events.jsonl")}
    (pkg / "manifest.json").write_text(json.dumps(
        {"format": "turnkeep-export", "schema_version": 1, "scope_id": "t",
         "exported_at": "2026-11-08T00:00:00Z", "tool": "test", "record_count": 2,
         "event_count": 0, "files": files}) + "\n")
    db = _db(tmp_path)
    imp = import_package(db, pkg)
    assert imp["imported"] == 1
    row = db.execute("SELECT key, state FROM memories").fetchone()
    assert tuple(row) == ("child", "tainted")
    db.close()


def test_incoming_actor_claim_is_not_authority(tmp_path):
    # Extra fields in a record (e.g. a forged actor_id) are dropped on import;
    # nothing in the package can rebind identity or grant authority.
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    recs = [{"id": "o1", "key": "k", "value": "v", "kind": "fact", "state": "active",
             "revision": 1, "actor_id": "admin", "authority": "root",
             "source_refs": [{"id": "u:1", "type": "user_statement"}],
             "parent_ids": [], "supersedes_ids": []}]
    (pkg / "records.jsonl").write_text(json.dumps(recs[0]) + "\n")
    (pkg / "events.jsonl").write_text("")
    import hashlib
    files = {n: hashlib.sha256((pkg / n).read_bytes()).hexdigest() for n in ("records.jsonl", "events.jsonl")}
    (pkg / "manifest.json").write_text(json.dumps(
        {"format": "turnkeep-export", "schema_version": 1, "scope_id": "t",
         "exported_at": "2026-11-08T00:00:00Z", "tool": "test", "record_count": 1,
         "event_count": 0, "files": files}) + "\n")
    db = _db(tmp_path)
    imp = import_package(db, pkg)
    assert imp["imported"] == 1
    cols = {r[1] for r in db.execute("PRAGMA table_info(memories)").fetchall()}
    assert "actor_id" not in cols and "authority" not in cols
    db.close()


def test_import_source_identity_conflict_errors(tmp_path):
    # P1-c: a package source sharing an id with a local source but a
    # different type is a hard error, not a silent first-wins keep; the
    # local row is unchanged and nothing is imported (tx rolls back).
    # Same id + same type stays an idempotent no-op.
    from turnkeep.memory import remember
    db = _db(tmp_path)
    remember(db, "t", "k", "v", source_refs=[{"id": "shared-src", "type": "user_statement"}])
    recs = [{"id": "o1", "key": "k2", "value": "v2", "kind": "fact", "state": "active",
             "revision": 1, "source_refs": [{"id": "shared-src", "type": "tool_observation"}],
             "parent_ids": [], "supersedes_ids": []}]
    _make_pkg(tmp_path / "pkg", recs)
    with pytest.raises(TurnkeepError) as ei:
        import_package(db, tmp_path / "pkg")
    assert ei.value.code == "INVALID_SOURCE"
    row = db.execute("SELECT source_type FROM sources WHERE id='shared-src'").fetchone()
    assert row[0] == "user_statement"
    assert db.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM import_batches").fetchone()[0] == 0
    recs[0]["source_refs"] = [{"id": "shared-src", "type": "user_statement"}]
    _make_pkg(tmp_path / "pkg2", recs)
    imp = import_package(db, tmp_path / "pkg2")
    assert imp["imported"] == 1
    db.close()


def test_import_quarantined_parent_child_tainted_and_reported(tmp_path):
    # P0-5: the parent is quarantined (unknown source type), so the child
    # is imported tainted, its edge is dropped as a prominent warning
    # (message + child + parent), and the integrity section records the
    # drop as expected — never silently as a plan footnote.
    recs = [
        {"id": "o1", "key": "parent", "value": "p", "kind": "fact", "state": "active",
         "revision": 1, "source_refs": [{"id": "u:1", "type": "mystery_source"}],
         "parent_ids": [], "supersedes_ids": []},
        {"id": "o2", "key": "child", "value": "c", "kind": "decision", "state": "active",
         "revision": 1, "source_refs": [{"id": "u:1", "type": "user_statement"}],
         "parent_ids": ["o1"], "supersedes_ids": []},
    ]
    _make_pkg(tmp_path / "pkg", recs)
    db = _db(tmp_path)
    imp = import_package(db, tmp_path / "pkg")
    assert imp["imported"] == 1
    assert len(imp["edges_dropped"]) == 1
    assert len(imp["warnings"]) == 1
    assert imp["warnings"][0]["child"] == "child"
    assert imp["warnings"][0]["parent"] == _remap_id("t", "o1")
    assert "message" in imp["warnings"][0]
    assert imp["integrity_repairs"] == []
    assert len(imp["integrity_expected_drops"]) == 1
    assert imp["integrity_expected_drops"][0]["child"] == "child"
    row = db.execute("SELECT key, state FROM memories").fetchone()
    assert tuple(row) == ("child", "tainted")
    assert db.execute("SELECT COUNT(*) FROM dependencies").fetchone()[0] == 0
    db.close()


def test_import_retracted_parent_taints_active_child(tmp_path):
    # P0-5 repair, end to end: the parent IS in the package (edge kept)
    # but retracted, so the child's incoming "active" claim cannot stand:
    # the post-import check taints it in place, records the repair, and
    # cites the import event as the cause.
    recs = [
        {"id": "o1", "key": "parent", "value": "p", "kind": "fact", "state": "retracted",
         "revision": 2, "source_refs": [{"id": "u:1", "type": "user_statement"}],
         "parent_ids": [], "supersedes_ids": []},
        {"id": "o2", "key": "child", "value": "c", "kind": "decision", "state": "active",
         "revision": 1, "source_refs": [{"id": "u:1", "type": "user_statement"}],
         "parent_ids": ["o1"], "supersedes_ids": []},
    ]
    _make_pkg(tmp_path / "pkg", recs)
    db = _db(tmp_path)
    imp = import_package(db, tmp_path / "pkg")
    assert imp["imported"] == 2 and imp["edges_dropped"] == []
    assert [r["action"] for r in imp["integrity_repairs"]] == ["tainted_child_with_unusable_parent"]
    assert imp["integrity_repairs"][0]["child"] == "child"
    assert db.execute("SELECT COUNT(*) FROM dependencies").fetchone()[0] == 1
    child_local = _remap_id("t", "o2")
    row = db.execute("SELECT state, cause_event_id FROM memories WHERE id=?",
                     (child_local,)).fetchone()
    assert row[0] == "tainted"
    ev = db.execute("SELECT id FROM events WHERE event_type='import'").fetchone()[0]
    assert row[1] == ev
    assert usable(db, "t", child_local) is False
    db.close()


def test_import_dependency_cycle_fails_instead_of_dropping_edges(tmp_path):
    # P0-2 escalation: a parent cycle can never be topo-sorted, so the
    # write loop must fail the import rather than silently drop edges.
    recs = [
        {"id": "o1", "key": "a", "value": "1", "kind": "fact", "state": "active",
         "revision": 1, "source_refs": [{"id": "u:1", "type": "user_statement"}],
         "parent_ids": ["o2"], "supersedes_ids": []},
        {"id": "o2", "key": "b", "value": "2", "kind": "fact", "state": "active",
         "revision": 1, "source_refs": [{"id": "u:1", "type": "user_statement"}],
         "parent_ids": ["o1"], "supersedes_ids": []},
    ]
    _make_pkg(tmp_path / "pkg", recs)
    db = _db(tmp_path)
    with pytest.raises(TurnkeepError) as ei:
        import_package(db, tmp_path / "pkg")
    assert ei.value.code == "INVALID_PARENT"
    assert db.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM import_batches").fetchone()[0] == 0
    db.close()


def test_verify_import_integrity_reinserts_missing_edge(tmp_path):
    # P0-5 repair, helper level: a parent exists but the edge row is
    # missing (dropped by an older writer) -> re-inserted + reported,
    # and the child stays active because its parent is usable.
    from turnkeep.memory import remember
    from turnkeep.portability import _verify_import_integrity
    from turnkeep.storage import _id
    db = _db(tmp_path)
    parent = remember(db, "t", "p", "pv", kind="fact",
                      source_refs=[{"id": "u:1", "type": "user_statement"}])
    child = remember(db, "t", "c", "cv", kind="fact",
                     source_refs=[{"id": "u:1", "type": "user_statement"}],
                     parent_ids=[parent["id"]])
    db.execute("DELETE FROM dependencies WHERE child_id=?", (child["id"],))
    assert db.execute("SELECT COUNT(*) FROM dependencies").fetchone()[0] == 0
    plan, changes = {}, {child["id"]: None}
    repairs, _ = _verify_import_integrity(
        db, "t", [{"local_id": child["id"], "record": {"key": "c"},
                   "parent_ids": [parent["id"]], "state": "active"}],
        _id(), plan, changes)
    assert [r["action"] for r in repairs] == ["inserted_missing_edge"]
    assert db.execute("SELECT COUNT(*) FROM dependencies").fetchone()[0] == 1
    assert db.execute("SELECT state FROM memories WHERE id=?",
                      (child["id"],)).fetchone()[0] == "active"
    db.close()
