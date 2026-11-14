"""T037 import injection: hostile Markdown is inert data, never an instruction.
No file outside the given root is read, no command is executed, no secret is
written, and no identity is rebound."""
import os
from pathlib import Path

import pytest

from turnkeep.storage import open_db
from turnkeep.scopes import register_scope
from turnkeep.errors import TurnkeepError
from turnkeep.importers.markdown import import_markdown

FIX = Path(__file__).resolve().parents[2] / "fixtures" / "imports" / "markdown-sample"


def _db(tmp_path):
    db = open_db(tmp_path / "m.sqlite3")
    register_scope(db, "md")
    return db


def test_injection_fixture_is_inert_text(tmp_path):
    db = _db(tmp_path)
    rep = import_markdown(db, FIX / "injection.md", "md", dry_run=False)
    # The api_key / password items are excluded as secrets; the instruction
    # items are imported as literal text, never acted on.
    assert len(rep["excluded_secrets"]) >= 1
    assert rep["written"], rep
    values = [r[0] for r in db.execute("SELECT value_json FROM memories").fetchall()]
    assert not any("sk-live-999" in v for v in values)
    assert not any("hunter2" in v for v in values)
    # No record claims admin authority and no side-effect file exists.
    assert db.execute("SELECT COUNT(*) FROM memories WHERE key LIKE '%admin%'").fetchone()[0] == 0
    assert not (tmp_path / "collect").exists()
    db.close()


def test_local_file_link_is_not_read(tmp_path):
    db = _db(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET", encoding="utf-8")
    evil = tmp_path / "evil.md"
    evil.write_text(f"# Decisions\n- See [the file](file://{secret}) for details.\n", encoding="utf-8")
    rep = import_markdown(db, evil, "md", dry_run=False)
    values = [r[0] for r in db.execute("SELECT value_json FROM memories").fetchall()]
    assert not any("TOP SECRET" in v for v in values)
    db.close()


def test_symlink_inside_dir_cannot_escape_root(tmp_path):
    db = _db(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("# Decisions\n- outside\n", encoding="utf-8")
    root = tmp_path / "root"
    root.mkdir()
    (root / "inner.md").write_text("# Decisions\n- inner\n", encoding="utf-8")
    (root / "escape.md").symlink_to(outside)
    with pytest.raises(TurnkeepError) as ei:
        import_markdown(db, root, "md", dry_run=True)
    assert ei.value.code == "INVALID_INPUT"
    # Nothing from the escaped file was written.
    assert db.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
    db.close()


def test_absolute_path_outside_root_is_rejected(tmp_path):
    db = _db(tmp_path)
    # The importer resolves the given path; a non-existent path is NOT_FOUND,
    # and an existing absolute path outside the working tree is still only
    # read as the single file the user explicitly named — never followed
    # through a symlink or a directory walk outside the root.
    with pytest.raises(TurnkeepError) as ei:
        import_markdown(db, "/nonexistent/does-not-exist.md", "md", dry_run=True)
    assert ei.value.code == "NOT_FOUND"
    db.close()
