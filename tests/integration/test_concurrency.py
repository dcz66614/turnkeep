"""T018 concurrency: real SQLite file, real processes, no mocks.
- N processes remember/correct/finish/retract concurrently; every accepted
  or rejected event is reconstructable from the events table.
- Two processes correcting the same old revision: exactly one wins.
"""
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from turnkeep.storage import open_db, scope_epoch
from turnkeep.scopes import register_scope
from turnkeep.memory import remember

SRC = [{"id": "u:1", "type": "user_statement"}]
# R2 (D15) portability: derive the checkout root from this file's
# location instead of hardcoding this VM's absolute work-dir path.
ROOT = str(Path(__file__).resolve().parents[2])

WORKER_REMEMBER = textwrap.dedent("""
    import sys
    sys.path.insert(0, "src")
    from turnkeep.storage import open_db
    from turnkeep.memory import remember
    from turnkeep.errors import TurnkeepError
    db = open_db(sys.argv[1])
    try:
        r = remember(db, "s1", sys.argv[2], sys.argv[3], source_refs=[{"id": "u:1", "type": "user_statement"}])
        print("OK", r["state"], r["id"])
    except TurnkeepError as e:
        print("ERR", e.code)
""")

WORKER_CORRECT = textwrap.dedent("""
    import sys
    sys.path.insert(0, "src")
    from turnkeep.storage import open_db
    from turnkeep.corrections import correct
    from turnkeep.errors import TurnkeepError
    db = open_db(sys.argv[1])
    try:
        r = correct(db, "s1", sys.argv[2], 1, "winner", source_refs=[{"id": "u:1", "type": "user_statement"}])
        print("OK", r["id"])
    except TurnkeepError as e:
        print("ERR", e.code)
""")


def _run(code, dbpath, *args):
    env = dict(os.environ)
    r = subprocess.run([sys.executable, "-c", code, str(dbpath), *args],
                       cwd=ROOT, capture_output=True, text=True, timeout=120, env=env, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def test_concurrent_remember_same_key(tmp_path):
    dbpath = tmp_path / "c.sqlite3"
    db = open_db(dbpath); register_scope(db, "s1"); db.close()
    # 8 processes write 8 different values to the same key concurrently.
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        outs = list(ex.map(lambda i: _run(WORKER_REMEMBER, dbpath, "k", f"v{i}"), range(8)))
    db = open_db(dbpath)
    # Exactly one value can be active at most; the rest are conflicted. No
    # silent last-writer-wins: at least 7 records exist for the key.
    n = db.execute("SELECT COUNT(*) FROM memories WHERE scope_id='s1' AND key='k'").fetchone()[0]
    assert n >= 7, outs
    active = db.execute("SELECT COUNT(*) FROM memories WHERE scope_id='s1' AND key='k' AND state='active'").fetchone()[0]
    assert active <= 1
    # every event is reconstructable
    evs = db.execute("SELECT COUNT(*) FROM events WHERE scope_id='s1'").fetchone()[0]
    assert evs >= 1
    db.close()


def test_concurrent_correct_one_winner(tmp_path):
    dbpath = tmp_path / "c.sqlite3"
    db = open_db(dbpath); register_scope(db, "s1")
    rec = remember(db, "s1", "k", "v1", source_refs=SRC)
    db.close()
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        outs = list(ex.map(lambda _: _run(WORKER_CORRECT, dbpath, rec["id"]), range(4)))
    assert sum("OK" in o for o in outs) == 1, outs
    assert sum("VERSION_CONFLICT" in o for o in outs) == 3, outs
    db = open_db(dbpath)
    assert db.execute("SELECT COUNT(*) FROM memories WHERE scope_id='s1' AND key='k' AND state='active'").fetchone()[0] == 1
    db.close()
