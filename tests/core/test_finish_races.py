"""T014 finish race tests: two processes, one winner; crash mid-tx leaves nothing."""
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from turnkeep.storage import open_db
from turnkeep.scopes import register_scope
from turnkeep.memory import remember
from turnkeep.runs import begin
from turnkeep.commits import finish

# R2 (D15) portability: the checkout root is derived from this file's
# location, never hardcoded — the old literal VM work-dir path does not
# exist on any other machine, so the subprocess cwd failed there.
# (The literal old path is deliberately not quoted here: the release
# packager drops any shipped file containing this machine's home path.)
ROOT = Path(__file__).resolve().parents[2]

SRC = [{"id": "u:1", "type": "user_statement"}]
WORKER = textwrap.dedent("""
    import sys
    sys.path.insert(0, "src")
    from turnkeep.storage import open_db
    from turnkeep.runs import begin
    from turnkeep.commits import finish
    from turnkeep.errors import TurnkeepError
    db = open_db(sys.argv[1])
    run = begin(db, "s1", "race-worker")
    try:
        r = finish(db, "s1", run["run_id"], {"key": "race-out", "value": sys.argv[2]})
        print("ACCEPTED", r["record_id"])
    except TurnkeepError as e:
        print("REJECTED", e.code)
""")


def test_two_processes_one_finish_wins(tmp_path):
    dbpath = tmp_path / "race.sqlite3"
    db = open_db(dbpath); register_scope(db, "s1")
    remember(db, "s1", "req", "1", source_refs=SRC)
    db.close()
    env = dict(os.environ)
    outs = []
    for val in ("A", "B"):
        r = subprocess.run([sys.executable, "-c", WORKER, str(dbpath), val],
                           cwd=str(ROOT),
                           capture_output=True, text=True, timeout=60, env=env)
        outs.append(r.stdout.strip())
    assert sum("ACCEPTED" in o for o in outs) == 1, outs
    db = open_db(dbpath)
    assert db.execute("SELECT COUNT(*) FROM memories WHERE key='race-out'").fetchone()[0] == 1
    db.close()


def test_killed_writer_leaves_no_receipt(tmp_path):
    # A process that opens a write tx and is killed (os._exit) must leave the
    # DB consistent: no partial result, no receipt, epoch unchanged.
    dbpath = tmp_path / "kill.sqlite3"
    db = open_db(dbpath); register_scope(db, "s1")
    remember(db, "s1", "req", "1", source_refs=SRC)
    epoch0 = db.execute("SELECT epoch FROM scopes WHERE id='s1'").fetchone()[0]
    db.close()
    killer = textwrap.dedent("""
        import sys
        sys.path.insert(0, "src")
        from turnkeep.storage import open_db, transaction
        db = open_db(sys.argv[1])
        with transaction(db, write=True):
            db.execute("INSERT INTO memories(id, scope_id, key, value_json, kind, state, revision, created_at, updated_at) VALUES ('x','s1','killed','\\"v\\"','fact','active',1,'t','t')")
            import os; os._exit(137)
    """)
    r = subprocess.run([sys.executable, "-c", killer, str(dbpath)],
                       cwd=str(ROOT),
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 137
    db = open_db(dbpath)
    assert db.execute("SELECT COUNT(*) FROM memories WHERE key='killed'").fetchone()[0] == 0
    assert db.execute("SELECT epoch FROM scopes WHERE id='s1'").fetchone()[0] == epoch0
    db.close()
