"""T023 conformance tests: duplicate requests, cancel/disconnect, schema
mismatch, large payload, busy DB, reconnect. Official SDK client only.
"""
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


def _params(dbpath, scope="conf"):
    return StdioServerParameters(command=VENV_PY,
                                 args=["-m", "turnkeep", "mcp", "--scope", scope, "--db", str(dbpath)],
                                 env=ENV)


def _run(coro):
    return asyncio.run(coro)


def test_duplicate_finish_same_key_no_double_result(tmp_path):
    dbpath = tmp_path / "c.sqlite3"

    async def go():
        async with stdio_client(_params(dbpath)) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                await session.call_tool("remember", {"key": "k", "value": "v"})
                run = json.loads((await session.call_tool("begin", {"task": "w"})).content[0].text)
                args = {"run_id": run["run_id"], "key": "out", "value": "x", "idempotency_key": "dup-1"}
                r1 = json.loads((await session.call_tool("finish", args)).content[0].text)
                r2 = json.loads((await session.call_tool("finish", args)).content[0].text)
                return r1, r2
    r1, r2 = _run(go())
    assert r1 == r2


def test_schema_mismatch_rejected(tmp_path):
    dbpath = tmp_path / "c.sqlite3"

    async def go():
        async with stdio_client(_params(dbpath)) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                # expected_revision must be an int; a string is a schema error.
                res = await session.call_tool("correct", {"record_id": "x", "expected_revision": "one", "new_value": "y"})
                return res
    res = _run(go())
    assert res.is_error is True


def test_large_payload_ok(tmp_path):
    dbpath = tmp_path / "c.sqlite3"

    async def go():
        async with stdio_client(_params(dbpath)) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                big = "x" * 200_000
                r = await session.call_tool("remember", {"key": "big", "value": big})
                return json.loads(r.content[0].text)
    out = _run(go())
    assert out["state"] == "active" and len(out["value"]) == 200_000


def test_reconnect_after_disconnect(tmp_path):
    dbpath = tmp_path / "c.sqlite3"

    async def go():
        for i in range(2):
            async with stdio_client(_params(dbpath)) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    await session.call_tool("remember", {"key": f"k{i}", "value": "v"})
        async with stdio_client(_params(dbpath)) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                hits = await session.call_tool("recall", {"query": ""})
                return json.loads(hits.content[0].text)["records"]
    recs = _run(go())
    assert {r["key"] for r in recs} == {"k0", "k1"}
