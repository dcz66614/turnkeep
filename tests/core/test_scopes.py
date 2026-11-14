"""T008 scope tests."""
import pytest
from turnkeep.storage import open_db
from turnkeep.scopes import register_scope, scope_id_for_path, list_scopes, canonical_path
from turnkeep.errors import TurnkeepError


def test_two_projects_same_key_no_conflict(tmp_path):
    db = open_db(tmp_path / "s.sqlite3")
    register_scope(db, "proj-a"); register_scope(db, "proj-b")
    scopes = {s["id"] for s in list_scopes(db)}
    assert {"proj-a", "proj-b"} <= scopes
    db.close()


def test_scope_id_for_path_stable_and_distinct(tmp_path):
    a = scope_id_for_path("/tmp/work/a")
    b = scope_id_for_path("/tmp/work/a")
    c = scope_id_for_path("/tmp/work/b")
    assert a == b and a != c and a.startswith("proj-")


def test_symlink_resolves_to_same_scope(tmp_path):
    real = tmp_path / "real"; real.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    assert scope_id_for_path(str(link)) == scope_id_for_path(str(real))


def test_unknown_scope_epoch_raises(tmp_path):
    from turnkeep.storage import scope_epoch
    db = open_db(tmp_path / "s.sqlite3")
    with pytest.raises(TurnkeepError) as ei:
        scope_epoch(db, "nope")
    assert ei.value.code == "NOT_FOUND"
    db.close()
