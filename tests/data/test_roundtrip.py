"""T036 roundtrip: export -> fresh DB -> export, normalised equivalence."""
import hashlib
import json
from pathlib import Path

from turnkeep.storage import open_db, usable, get_provenance
from turnkeep.scopes import register_scope
from turnkeep.memory import remember
from turnkeep.corrections import correct, retract
from turnkeep.portability import export_scope, import_package, _remap_id

SRC = [{"id": "u:1", "type": "user_statement"}]
FIX = Path(__file__).resolve().parents[2] / "fixtures" / "imports"


def _norm(pkgdir):
    recs = [json.loads(l) for l in (Path(pkgdir) / "records.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    by_id = {r["id"]: r for r in recs}
    out = {}
    for r in recs:
        parents = sorted(by_id[pid]["key"] for pid in r["parent_ids"])
        out[r["key"] + "|" + r["state"]] = (r["value"], r["kind"], r["revision"],
                                            parents, sorted(s["type"] for s in r["source_refs"]),
                                            sorted(by_id[s]["key"] for s in r["supersedes_ids"]))
    return out


def _seed(db, scope="rt"):
    register_scope(db, scope)
    register_scope(db, "other")
    req = remember(db, scope, "export.format", "zip", kind="requirement", source_refs=SRC)
    remember(db, scope, "release.manifest", "name=export", kind="decision",
             source_refs=SRC, parent_ids=[req["id"]])
    correct(db, scope, req["id"], 1, "tar", source_refs=SRC)
    remember(db, scope, "note.deadline", "Friday", kind="fact", source_refs=SRC)
    dead = remember(db, scope, "note.old", "draft", kind="fact", source_refs=SRC)
    retract(db, scope, dead["id"], 1, "superseded")
    remember(db, scope, "conflict.k", "v1", kind="fact", source_refs=SRC)
    remember(db, scope, "conflict.k", "v2", kind="fact", source_refs=SRC)
    remember(db, "other", "other.secret", "not yours", kind="fact", source_refs=SRC)
    remember(db, scope, "ci.api_key", "api_key=sk-test-123", kind="fact", source_refs=SRC)
    return db


def test_roundtrip_equivalent(tmp_path):
    db1 = open_db(tmp_path / "a.sqlite3")
    _seed(db1)
    pkg = tmp_path / "pkg"
    exp = export_scope(db1, "rt", pkg)
    assert exp["records_exported"] == 7  # 8 records minus the secret-like one
    assert [e["key"] for e in exp["records_excluded_secrets"]] == ["ci.api_key"]
    db1.close()

    db2 = open_db(tmp_path / "b.sqlite3")
    register_scope(db2, "rt")
    imp = import_package(db2, pkg)
    assert imp["imported"] == 7 and imp["quarantined"] == []
    pkg2 = tmp_path / "pkg2"
    export_scope(db2, "rt", pkg2)
    assert _norm(pkg) == _norm(pkg2)
    # The other scope was never exported: nothing from it leaked in.
    keys = {r["key"] for r in db2.execute("SELECT key FROM memories").fetchall()}
    assert "other.secret" not in keys and "ci.api_key" not in keys
    db2.close()


def test_export_excludes_long_structured_secret(tmp_path):
    # P0-R2-02 (GPT's exact repro): a structured api_key value of 5,100+
    # filler chars must be excluded, not exported in plaintext — the old
    # `len(text) < 5000` gate in _looks_secret() let it through. A long
    # value with no secret-like keyword still exports.
    db1 = open_db(tmp_path / "a.sqlite3")
    register_scope(db1, "rt")
    secret = "SHOULD_NOT_EXPORT_R2" + "x" * 5100
    remember(db1, "rt", "cfg.api", {"api_key": secret}, kind="fact", source_refs=SRC)
    remember(db1, "rt", "long.note", {"note": "x" * 6000}, kind="fact", source_refs=SRC)
    pkg = tmp_path / "pkg"
    exp = export_scope(db1, "rt", pkg)
    assert exp["records_exported"] == 1
    assert [e["key"] for e in exp["records_excluded_secrets"]] == ["cfg.api"]
    blob = (pkg / "records.jsonl").read_text(encoding="utf-8")
    assert secret not in blob and "SHOULD_NOT_EXPORT_R2" not in blob
    assert "long.note" in blob
    db1.close()


def test_imported_records_cite_import_event(tmp_path):
    # P1-R2-03a (GPT's repro): imported records' cause_event_id must be
    # the import event id, which exists in the target DB — not the source
    # DB's event id, which does not.
    db1 = open_db(tmp_path / "a.sqlite3")
    register_scope(db1, "rt")
    remember(db1, "rt", "imp.a", "alpha", kind="fact", source_refs=SRC)
    remember(db1, "rt", "imp.b", "beta", kind="fact", source_refs=SRC)
    pkg = tmp_path / "pkg"
    export_scope(db1, "rt", pkg)
    db1.close()
    db2 = open_db(tmp_path / "b.sqlite3")
    register_scope(db2, "rt")
    imp = import_package(db2, pkg)
    assert imp["imported"] == 2
    import_ev = db2.execute(
        "SELECT id FROM events WHERE event_type='import'").fetchone()[0]
    rows = db2.execute("SELECT key, cause_event_id FROM memories").fetchall()
    assert len(rows) == 2
    for key, cev in rows:
        assert cev == import_ev, key
        assert db2.execute("SELECT 1 FROM events WHERE id=?", (cev,)).fetchone() is not None
    db2.close()


def test_second_import_is_noop(tmp_path):
    db1 = open_db(tmp_path / "a.sqlite3")
    _seed(db1)
    pkg = tmp_path / "pkg"
    export_scope(db1, "rt", pkg)
    db1.close()
    db2 = open_db(tmp_path / "b.sqlite3")
    register_scope(db2, "rt")
    first = import_package(db2, pkg)
    assert first["imported"] == 7
    second = import_package(db2, pkg)
    assert second["already_imported"] and second["imported"] == 0
    assert db2.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 7
    db2.close()


def test_dry_run_writes_nothing(tmp_path):
    db = open_db(tmp_path / "a.sqlite3")
    register_scope(db, "rt")
    plan = import_package(db, FIX / "example-project", dry_run=True)
    assert plan["dry_run"] and len(plan["to_import"]) == 2
    assert db.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
    db.close()


def test_fixture_example_project_imports(tmp_path):
    db = open_db(tmp_path / "a.sqlite3")
    register_scope(db, "example")
    imp = import_package(db, FIX / "example-project")
    assert imp["imported"] == 2 and imp["quarantined"] == []
    db.close()


def test_import_child_before_parent_keeps_edge(tmp_path):
    # P0-2 repro: export, reorder records.jsonl child-before-parent (fix
    # the manifest digest), import into a fresh DB -> the edge survives,
    # the child stays active and usable. The dry-run plan already shows
    # the write order: parent before child.
    db1 = open_db(tmp_path / "a.sqlite3")
    register_scope(db1, "rt")
    parent = remember(db1, "rt", "parent.k", "p", kind="fact", source_refs=SRC)
    child = remember(db1, "rt", "child.k", "c", kind="fact", source_refs=SRC,
                     parent_ids=[parent["id"]])
    pkg = tmp_path / "pkg"
    export_scope(db1, "rt", pkg)
    db1.close()
    recs = [json.loads(l) for l in (pkg / "records.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(recs) == 2
    recs.sort(key=lambda r: 0 if r["key"] == "child.k" else 1)  # child first
    assert recs[0]["key"] == "child.k"
    (pkg / "records.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
    manifest = json.loads((pkg / "manifest.json").read_text(encoding="utf-8"))
    manifest["files"]["records.jsonl"] = hashlib.sha256(
        (pkg / "records.jsonl").read_bytes()).hexdigest()
    (pkg / "manifest.json").write_text(json.dumps(manifest) + "\n", encoding="utf-8")

    db2 = open_db(tmp_path / "b.sqlite3")
    register_scope(db2, "rt")
    plan = import_package(db2, pkg, dry_run=True)
    assert [i["record"]["key"] for i in plan["to_import"]] == ["parent.k", "child.k"]
    imp = import_package(db2, pkg)
    assert imp["imported"] == 2 and imp["edges_dropped"] == [] and imp["warnings"] == []
    assert db2.execute("SELECT COUNT(*) FROM dependencies").fetchone()[0] == 1
    child_local = _remap_id("rt", child["id"])
    row = db2.execute("SELECT state FROM memories WHERE id=?", (child_local,)).fetchone()
    assert row[0] == "active"
    assert usable(db2, "rt", child_local) is True
    db2.close()


def test_import_records_provenance_and_reimport_keeps_it(tmp_path):
    # P1-c: after a successful import, get_provenance() returns the origin
    # id + origin scope for every imported record; re-importing the same
    # package stays a no-op and does not duplicate provenance rows.
    db1 = open_db(tmp_path / "a.sqlite3")
    _seed(db1)
    pkg = tmp_path / "pkg"
    export_scope(db1, "rt", pkg)
    db1.close()
    db2 = open_db(tmp_path / "b.sqlite3")
    register_scope(db2, "rt")
    imp = import_package(db2, pkg)
    assert imp["imported"] == 7
    rows = db2.execute("SELECT id FROM memories").fetchall()
    assert len(rows) == 7
    for (local_id,) in rows:
        prov = get_provenance(db2, "rt", local_id)
        assert prov is not None
        assert prov["origin_scope"] == "rt"
        assert prov["origin_id"] and prov["origin_id"] != local_id
    assert db2.execute("SELECT COUNT(*) FROM record_provenance").fetchone()[0] == 7
    second = import_package(db2, pkg)
    assert second["already_imported"] and second["imported"] == 0
    assert db2.execute("SELECT COUNT(*) FROM record_provenance").fetchone()[0] == 7
    db2.close()
