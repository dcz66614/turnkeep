"""T023 parallel clients: two independent stdio connections share one DB;
each keeps its own run/actor, and a stale finish from the first client
after the second client's correction is rejected."""
import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[2]
VENV_PY = sys.executable  # the test process runs under the venv interpreter in CI; a POSIX .venv/bin path does not exist on Windows
ENV = {"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin:/usr/local/bin",
       "HOME": "/tmp", "TMPDIR": "/tmp"}


def _params(dbpath, scope="par"):
    return StdioServerParameters(command=VENV_PY,
                                 args=["-m", "turnkeep", "mcp", "--scope", scope, "--db", str(dbpath)],
                                 env=ENV)


def test_two_clients_stale_finish_rejected(tmp_path):
    dbpath = tmp_path / "p.sqlite3"

    async def go():
        async with stdio_client(_params(dbpath)) as (r1, w1), \
                stdio_client(_params(dbpath)) as (r2, w2):
            async with ClientSession(r1, w1) as a, ClientSession(r2, w2) as b:
                await a.initialize(); await b.initialize()
                req = json.loads((await a.call_tool("remember", {"key": "k", "value": "v1"})).content[0].text)
                run_a = json.loads((await a.call_tool("begin", {"task": "A work"})).content[0].text)
                await b.call_tool("correct", {"record_id": req["id"], "expected_revision": 1, "new_value": "v2"})
                out = json.loads((await a.call_tool("finish", {"run_id": run_a["run_id"], "key": "out", "value": "x"})).content[0].text)
                return out
    out = asyncio.run(go())
    assert out["error"]["code"] == "STALE_SNAPSHOT"


def test_two_clients_see_same_records(tmp_path):
    dbpath = tmp_path / "p.sqlite3"

    async def go():
        async with stdio_client(_params(dbpath)) as (r1, w1), \
                stdio_client(_params(dbpath)) as (r2, w2):
            async with ClientSession(r1, w1) as a, ClientSession(r2, w2) as b:
                await a.initialize(); await b.initialize()
                await a.call_tool("remember", {"key": "shared", "value": "yes"})
                hits = json.loads((await b.call_tool("recall", {"query": "shared"})).content[0].text)
                return hits["records"]
    recs = asyncio.run(go())
    assert len(recs) == 1 and recs[0]["value"] == "yes"
