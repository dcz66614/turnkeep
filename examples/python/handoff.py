#!/usr/bin/env python3
"""Two-agent handoff via the Python SDK (T020). SCRIPTED SIMULATION — no
model calls: agent A and agent B are two TurnkeepClient instances on the
same DB, standing in for two agents/sessions.

  python examples/python/handoff.py --temp-db

A begins, B corrects the requirement, A's stale finish is rejected, A
resumes, reads the diff, recomputes and finishes. Run with --temp-db for a
throwaway database, or --db PATH --scope ID for a real one.
"""
import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from turnkeep.client import TurnkeepClient
from turnkeep.errors import TurnkeepError

SRC = [{"id": "demo:user:1", "type": "user_statement"}]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--temp-db", action="store_true")
    ap.add_argument("--db")
    ap.add_argument("--scope", default="demo")
    args = ap.parse_args()
    if args.temp_db:
        tmp = tempfile.TemporaryDirectory(prefix="turnkeep-sdk-")
        dbpath = Path(tmp.name) / "sdk.sqlite3"
    elif args.db:
        dbpath, tmp = Path(args.db), None
    else:
        print("use --temp-db or --db PATH")
        return 2
    print("SCRIPTED_SIMULATION_NO_MODEL_CALLS: two SDK clients, no model involved.")
    with TurnkeepClient(dbpath, args.scope, actor="agent-a") as a, \
            TurnkeepClient(dbpath, args.scope, actor="agent-b") as b:
        req = a.remember("build.python_version", "3.11", kind="requirement", source_refs=SRC)
        run = a.begin("prepare release")
        b.correct(req["id"], 1, "3.12", source_refs=SRC)
        try:
            a.finish(run["run_id"], "release", "Python 3.11")
            print("ERROR: stale finish was accepted")
            return 1
        except TurnkeepError as e:
            assert e.code == "STALE_SNAPSHOT"
            print(f"A's stale finish rejected: {e.code} changed_keys={e.details.get('changed_keys')}")
        diff = a.resume(run["run_id"])
        print(f"A resumed: changed_keys={diff['changed_keys']}")
        run2 = a.begin("recompute release")
        receipt = a.finish(run2["run_id"], "release", "Python 3.12")
        print(f"A finished: record {receipt['record_id']} at epoch {receipt['commit_epoch']}")
    print("PASS: SDK two-agent handoff")
    return 0


if __name__ == "__main__":
    sys.exit(main())
