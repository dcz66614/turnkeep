"""T037 markdown import: sections, keys, duplicates, conflicts, boundaries."""
import hashlib
from pathlib import Path

import pytest

from turnkeep.storage import open_db, get_provenance
from turnkeep.scopes import register_scope
from turnkeep.memory import remember
from turnkeep.errors import TurnkeepError
from turnkeep.importers.markdown import import_markdown, preview_markdown

FIX = Path(__file__).resolve().parents[2] / "fixtures" / "imports" / "markdown-sample"
SRC = [{"id": "u:1", "type": "user_statement"}]


def _db(tmp_path):
    db = open_db(tmp_path / "m.sqlite3")
    register_scope(db, "md")
    return db


def test_notes_fixture_parses_structured_items_only(tmp_path):
    db = _db(tmp_path)
    rep = import_markdown(db, FIX / "notes.md", "md", dry_run=True)
    # 2 decisions + 2 goals + 1 todo + 1 done attempt + 1 attempt + 1 procedure
    assert len(rep["planned"]) == 8, rep
    assert rep["skipped_unmapped"] >= 1  # the "Notes" prose section
    assert rep["written"] == []
    assert db.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
    db.close()


def test_real_import_writes_new_candidates_only(tmp_path):
    db = _db(tmp_path)
    rep = import_markdown(db, FIX / "notes.md", "md", dry_run=False)
    assert len(rep["written"]) == 8
    kinds = {r["key"]: r["kind"] for r in db.execute("SELECT key, kind FROM memories").fetchall()}
    assert any(k.startswith("md.notes.decisions.") and v == "decision" for k, v in kinds.items())
    assert any(k.startswith("md.notes.todo.") and v == "requirement" for k, v in kinds.items())
    assert any(k.startswith("md.notes.todo.") and v == "attempt" for k, v in kinds.items())
    # Sources are imported_text, never the original user_statement; sources
    # dedupe by id, so 8 records link to 5 distinct source rows (one per heading).
    assert db.execute("SELECT COUNT(*) FROM sources WHERE source_type='imported_text'").fetchone()[0] == 5
    assert db.execute("SELECT COUNT(*) FROM memory_sources").fetchone()[0] == 8
    db.close()


def test_stable_keys_and_duplicates(tmp_path):
    db = _db(tmp_path)
    first = import_markdown(db, FIX / "notes.md", "md", dry_run=False)
    second = import_markdown(db, FIX / "notes.md", "md", dry_run=False)
    assert second["written"] == []
    assert len(second["duplicates"]) == 8
    assert db.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 8
    db.close()


def test_conflict_stays_for_adjudication(tmp_path):
    db = _db(tmp_path)
    # Pre-seed one key with a different value: compute the key first.
    preview = import_markdown(db, FIX / "notes.md", "md", dry_run=True)
    key = next(k for k in preview["planned"] if ".decisions." in k)
    remember(db, "md", key, "a completely different decision", kind="decision", source_refs=SRC)
    rep = import_markdown(db, FIX / "notes.md", "md", dry_run=False)
    assert len(rep["conflicts"]) == 1 and rep["conflicts"][0]["key"] == key
    assert db.execute("SELECT COUNT(*) FROM memories WHERE key=?", (key,)).fetchone()[0] == 1
    db.close()


def test_code_fence_and_prose_never_become_facts(tmp_path):
    db = _db(tmp_path)
    import_markdown(db, FIX / "notes.md", "md", dry_run=False)
    values = [r[0] for r in db.execute("SELECT value_json FROM memories").fetchall()]
    assert not any("SQLite for everything" in v for v in values)
    assert not any("plain prose" in v for v in values)
    db.close()


def test_empty_directory_imports_nothing(tmp_path):
    db = _db(tmp_path)
    empty = tmp_path / "empty"
    empty.mkdir()
    rep = import_markdown(db, empty, "md", dry_run=True)
    assert rep["files"] == 0 and rep.get("planned", []) == []
    db.close()


def test_oversize_file_rejected(tmp_path):
    db = _db(tmp_path)
    big = tmp_path / "big.md"
    big.write_text("# Decisions\n" + "- x\n" * 300_000, encoding="utf-8")
    with pytest.raises(TurnkeepError) as ei:
        import_markdown(db, big, "md", dry_run=True)
    assert ei.value.code == "INVALID_INPUT"
    db.close()


def test_import_persists_provenance(tmp_path):
    # P1-R2-02 / P1-c: the computed {file, heading, content_hash} must
    # survive the import — get_provenance() returns the file + hash.
    # The source dict itself is untouched (its content_digest must not
    # change): provenance lives in the record_provenance row.
    db = _db(tmp_path)
    md = tmp_path / "notes.md"
    text = "# Decisions\n\n- Use SQLite for storage\n"
    md.write_text(text, encoding="utf-8")
    rep = import_markdown(db, md, "md", dry_run=False)
    assert len(rep["written"]) == 1
    rid = rep["written"][0]
    prov = get_provenance(db, "md", rid)
    assert prov is not None
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    assert "notes.md" in prov["origin_id"]
    assert digest in prov["origin_id"]
    assert "decisions" in prov["origin_id"]  # heading slug
    assert prov["origin_scope"] == "markdown"
    srcs = db.execute("SELECT source_id FROM memory_sources WHERE memory_id=?",
                      (rid,)).fetchall()
    assert [s[0] for s in srcs] == ["md:notes.md#decisions"]
    db.close()


def test_symlink_refused(tmp_path):
    db = _db(tmp_path)
    target = tmp_path / "real.md"
    target.write_text("# Decisions\n- real\n", encoding="utf-8")
    link = tmp_path / "link.md"
    link.symlink_to(target)
    with pytest.raises(TurnkeepError) as ei:
        import_markdown(db, link, "md", dry_run=True)
    assert ei.value.code == "INVALID_INPUT"
    db.close()
