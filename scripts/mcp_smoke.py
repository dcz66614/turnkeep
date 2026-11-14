#!/usr/bin/env python3
"""MCP smoke (T023). Starts the real server via the official SDK client,
lists tools, does one remember/recall roundtrip, shuts down cleanly.

  python scripts/mcp_smoke.py --server "python -m turnkeep mcp"

The --server argument is a command string split with shlex (no shell).
Exit 0 PASS, 1 FAIL, 2 NOT_RUN (server cannot start / mcp SDK missing).
"""
import argparse
import asyncio
import json
import os
import shlex
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


async def _smoke(server_cmd, dbpath, scope):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    parts = shlex.split(server_cmd)
    if parts and parts[0] in ("python", "python3"):
        # "python" is not on PATH on this VM (only python3 / venv python);
        # resolve it to the interpreter running this script.
        parts[0] = sys.executable
    env = {"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin:/usr/local/bin",
           "HOME": "/tmp", "TMPDIR": "/tmp"}
    params = StdioServerParameters(command=parts[0],
                                   args=parts[1:] + ["--scope", scope, "--db", str(dbpath)],
                                   env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            info = await session.initialize()
            tools = await session.list_tools()
            names = [t.name for t in tools.tools]
            assert len(names) == 9, names
            r = await session.call_tool("remember", {"key": "smoke", "value": "ok"})
            rec = json.loads(r.content[0].text)
            assert rec["state"] == "active", rec
            hits = await session.call_tool("recall", {"query": "ok"})
            assert len(json.loads(hits.content[0].text)["records"]) == 1
            return {"server": info.server_info.name, "tools": names, "status": "PASS"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", required=True)
    ap.add_argument("--db")
    ap.add_argument("--scope", default="smoke")
    args = ap.parse_args()
    import tempfile
    tmp = args.db or None
    try:
        with tempfile.TemporaryDirectory(prefix="turnkeep-smoke-") as td:
            dbpath = Path(args.db) if args.db else Path(td) / "smoke.sqlite3"
            result = asyncio.run(_smoke(args.server, dbpath, args.scope))
        print(json.dumps(result, indent=2))
        return 0
    except Exception as e:
        print(f"FAIL mcp_smoke: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
