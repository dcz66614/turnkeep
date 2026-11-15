"""T020 SDK contract tests: SDK and CLI see the same view/errors on one fixture."""
import json
import subprocess
import sys
from pathlib import Path

import pytest
from turnkeep.client import TurnkeepClient
from turnkeep.errors import TurnkeepError

SRC = [{"id": "u:1", "type": "user_statement"}]
ROOT = Path(__file__).resolve().parents[2]


def test_sdk_handoff_flow(tmp_path):
    db = tmp_path / "sdk.sqlite3"
    with TurnkeepClient(db, "s1", actor="a") as a, TurnkeepClient(db, "s1", actor="b") as b:
        req = a.remember("k", "v1", source_refs=SRC)
        run = a.begin("work")
        b.correct(req["id"], 1, "v2", source_refs=SRC)
        with pytest.raises(TurnkeepError) as ei:
            a.finish(run["run_id"], "out", "x")
        assert ei.value.code == "STALE_SNAPSHOT"
        run2 = a.begin("work2")
        receipt = a.finish(run2["run_id"], "out", "x")
        assert receipt["status_at_commit"] == "active"


def test_sdk_and_cli_same_view(tmp_path):
    db = tmp_path / "sdk.sqlite3"
    with TurnkeepClient(db, "s1") as c:
        c.remember("k", "v1", source_refs=SRC)
    env = {"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": "/tmp"}
    r = subprocess.run([sys.executable, "-m", "turnkeep", "--db", str(db), "--json",
                        "recall", "--scope", "s1", "--query", "v1"],
                       capture_output=True, text=True, timeout=60, env=env, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["records"][0]["value"] == "v1"


def test_sdk_close_releases_connection(tmp_path):
    db = tmp_path / "sdk.sqlite3"
    c = TurnkeepClient(db, "s1")
    c.__enter__()
    c.close()
    assert c._db is None
    # reopening works after close
    with TurnkeepClient(db, "s1") as c2:
        assert c2.epoch == 0
