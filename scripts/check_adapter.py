#!/usr/bin/env python3
"""Adapter doctor (T024/T025). Validates the adapter config template and
reports live status. --mode config only: live client runs are NOT_RUN here
(claude CLI absent; codex login state unknown and never attempted).

  python scripts/check_adapter.py --client claude-code --mode config
  python scripts/check_adapter.py --client codex --mode config

Exit 0: config PASS. Exit 2: config FAIL. Live status is informational.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ADAPTERS = {
    "claude-code": ROOT / "adapters" / "claude-code" / ".mcp.json",
    "codex": ROOT / "adapters" / "codex" / "config.toml",
}


def check_claude():
    path = ADAPTERS["claude-code"]
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return {"config": "FAIL", "config_error": str(e)}
    srv = cfg.get("mcpServers", {}).get("turnkeep")
    if not srv or "command" not in srv or "args" not in srv:
        return {"config": "FAIL", "config_error": "mcpServers.turnkeep needs command+args"}
    cli = shutil.which("claude")
    return {"config": "PASS", "config_path": str(path), "server": srv,
            "live": "NOT_RUN",
            "live_reason": "claude CLI not installed on this machine" if not cli
                           else "claude CLI present; live session not attempted here"}


def check_codex():
    path = ADAPTERS["codex"]
    try:
        raw = path.read_text(encoding="utf-8")
        # Strip the leading comment block; TOML parser handles comments, so parse whole file.
        cfg = tomllib.loads(raw)
    except Exception as e:
        return {"config": "FAIL", "config_error": str(e)}
    srv = cfg.get("mcp_servers", {}).get("turnkeep")
    if not srv or "command" not in srv or "args" not in srv:
        return {"config": "FAIL", "config_error": "[mcp_servers.turnkeep] needs command+args"}
    cli = shutil.which("codex")
    installed = False
    listed = None
    if cli:
        try:
            out = subprocess.run([cli, "mcp", "list"], capture_output=True, text=True, timeout=30)
            listed = (out.stdout + out.stderr).strip()
            installed = "turnkeep" in listed
        except Exception as e:
            listed = f"codex mcp list error: {e}"
    return {"config": "PASS", "config_path": str(path), "server": srv,
            "live": "NOT_RUN",
            "live_reason": ("codex CLI installed but turnkeep not installed in ~/.codex/config.toml; "
                            "no login attempted" if not installed else "turnkeep installed; live session not run"),
            "codex_mcp_list": listed}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--client", required=True, choices=["claude-code", "codex"])
    ap.add_argument("--mode", default="config", choices=["config"])
    args = ap.parse_args()
    result = {"client": args.client, "mode": args.mode}
    result.update(check_claude() if args.client == "claude-code" else check_codex())
    print(json.dumps(result, indent=2))
    return 0 if result["config"] == "PASS" else 2


if __name__ == "__main__":
    sys.exit(main())
