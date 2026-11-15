"""T021 MCP tool tests via the OFFICIAL SDK client (stdio, real server process).
No hand-rolled JSON-RPC: ClientSession + stdio_client from mcp 2.3.0.
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


def _params(dbpath, scope="mcp-test"):
    return StdioServerParameters(
        command=VENV_PY,
        args=["-m", "turnkeep", "mcp", "--scope", scope, "--db", str(dbpath)],
        env=ENV)


async def _call(dbpath, tool, args):
    async with stdio_client(_params(dbpath)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool, args)
            texts = [c.text for c in result.content if c.type == "text"]
            return json.loads(texts[0]) if texts else None


def _run(coro):
    return asyncio.run(coro)


def test_list_tools_has_nine(tmp_path):
    dbpath = tmp_path / "m.sqlite3"

    async def go():
        async with stdio_client(_params(dbpath)) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                return [t.name for t in tools.tools]
    names = _run(go())
    for expected in ("begin", "recall", "remember", "correct", "retract",
                     "resolve_conflict", "resume", "finish", "brief"):
        assert expected in names, names
    assert len(names) == 9


def test_remember_begin_finish_roundtrip(tmp_path):
    dbpath = tmp_path / "m.sqlite3"
    req = _run(_call(dbpath, "remember", {"key": "k", "value": "v1"}))
    assert req["state"] == "active"
    run = _run(_call(dbpath, "begin", {"task": "work"}))
    receipt = _run(_call(dbpath, "finish", {"run_id": run["run_id"], "key": "out", "value": "done"}))
    assert receipt["status_at_commit"] == "active"


def test_stale_finish_returns_domain_error_not_crash(tmp_path):
    dbpath = tmp_path / "m.sqlite3"
    req = _run(_call(dbpath, "remember", {"key": "k", "value": "v1"}))
    run = _run(_call(dbpath, "begin", {"task": "work"}))
    _run(_call(dbpath, "correct", {"record_id": req["id"], "expected_revision": 1, "new_value": "v2"}))
    out = _run(_call(dbpath, "finish", {"run_id": run["run_id"], "key": "out", "value": "x"}))
    assert out["error"]["code"] == "STALE_SNAPSHOT"
    assert "k" in out["error"]["details"]["changed_keys"]


def test_invalid_payload_does_not_touch_db(tmp_path):
    dbpath = tmp_path / "m.sqlite3"
    out = _run(_call(dbpath, "remember", {"key": "k", "value": "v", "kind": "bogus"}))
    assert out["error"]["code"] == "INVALID_INPUT"
    hits = _run(_call(dbpath, "recall", {"query": "v"}))
    assert hits["records"] == []


def test_remember_path_like_source_id_rejected(tmp_path):
    # P1-d: a tool-supplied source id that looks like a filesystem path must
    # be rejected by the transport policy before anything reaches storage.
    dbpath = tmp_path / "m.sqlite3"
    out = _run(_call(dbpath, "remember", {"key": "k", "value": "v",
                                          "source_id": "/home/alice/notes.md"}))
    assert out["error"]["code"] == "INVALID_INPUT"
    out = _run(_call(dbpath, "remember", {"key": "k2", "value": "v",
                                          "source_id": "C:\\Users\\alice\\notes.md"}))
    assert out["error"]["code"] == "INVALID_INPUT"
    hits = _run(_call(dbpath, "recall", {"query": "v"}))
    assert hits["records"] == []
