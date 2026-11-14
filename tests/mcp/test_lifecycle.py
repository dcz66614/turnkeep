"""T021 MCP lifecycle tests: initialize / list / call / shutdown, clean exit."""
import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[2]
VENV_PY = str(ROOT / ".venv" / "bin" / "python")
ENV = {"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin:/usr/local/bin",
       "HOME": "/tmp", "TMPDIR": "/tmp"}


def test_lifecycle_clean_shutdown(tmp_path):
    dbpath = tmp_path / "m.sqlite3"

    async def go():
        params = StdioServerParameters(
            command=VENV_PY,
            args=["-m", "turnkeep", "mcp", "--scope", "lc", "--db", str(dbpath)],
            env=ENV)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                info = await session.initialize()
                assert info.server_info.name == "turnkeep"
                tools = await session.list_tools()
                assert len(tools.tools) == 9
                r = await session.call_tool("brief", {"goal": "test"})
                assert json.loads(r.content[0].text)["scope"] == "lc"
        return True

    assert asyncio.run(go()) is True
    # server process exited with the client; DB is intact and openable
    from turnkeep.storage import open_db
    db = open_db(dbpath)
    assert db.execute("SELECT COUNT(*) FROM scopes").fetchone()[0] == 1
    db.close()


def test_unknown_tool_is_protocol_error(tmp_path):
    dbpath = tmp_path / "m.sqlite3"

    async def go():
        params = StdioServerParameters(
            command=VENV_PY,
            args=["-m", "turnkeep", "mcp", "--scope", "lc", "--db", str(dbpath)],
            env=ENV)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                res = await session.call_tool("no_such_tool", {})
                # MCP 2.x returns an error result for unknown tools, it does not raise.
                assert res.is_error is True
    asyncio.run(go())
