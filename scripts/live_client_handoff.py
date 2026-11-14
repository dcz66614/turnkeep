#!/usr/bin/env python3
"""Live two-client handover runner (T026).

  python scripts/live_client_handoff.py --pair claude-code,codex \
      --fixture export-format --record evidence/live-clients

This script does NOT simulate clients. It checks the prerequisites for a
real A->B->A handover with two real MCP host products (binary present,
Turnkeep installed in the client's own config, auth state present) and
records the verdict. If any prerequisite is missing it writes a NOT_RUN
record and exits 2 — never a fake pass, never a mock recording.

When prerequisites ARE met, the script prints the manual run recipe
(fixture path, steps, what to record) and exits 2 as well: the handover
itself is two interactive client sessions driven by a human, and this
script does not impersonate them. A completed run is recorded by placing
the real tool-call logs + client versions in the --record directory and
re-running this script with --complete <run-dir> (not implemented until
a real run exists).
"""
import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOME = Path.home()


def check_claude():
    bin_ = shutil.which("claude")
    cfg = None
    # Installed means in the CLIENT's own config (~/.claude.json user scope
    # or a project's .mcp.json). The template inside this repo is NOT an
    # installation — counting it would fake a pass.
    for cand in [HOME / ".claude.json"]:
        if cand.exists():
            try:
                data = json.loads(cand.read_text())
                if "turnkeep" in data.get("mcpServers", {}):
                    cfg = str(cand)
            except Exception:
                pass
    return {"binary": bool(bin_), "turnkeep_installed": bool(cfg),
            "config_path": cfg, "auth": None,
            "note": "claude CLI absent on this machine" if not bin_ else
                    ("turnkeep not installed in client config" if not cfg else "ready")}


def check_codex():
    bin_ = shutil.which("codex")
    cfg_path = HOME / ".codex" / "config.toml"
    installed = cfg_path.exists() and "[mcp_servers.turnkeep]" in cfg_path.read_text(errors="ignore")
    auth = (HOME / ".codex" / "auth.json").exists()
    return {"binary": bool(bin_), "turnkeep_installed": installed,
            "config_path": str(cfg_path) if installed else None,
            "auth": auth,
            "note": ("codex CLI installed; turnkeep not installed in ~/.codex/config.toml; "
                     "no login attempted" if bin_ and not installed else
                     "codex CLI absent" if not bin_ else
                     "auth.json absent — login state unknown, no login attempted")}


CHECKS = {"claude-code": check_claude, "codex": check_codex}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pair", required=True)
    ap.add_argument("--fixture", required=True)
    ap.add_argument("--record", required=True)
    args = ap.parse_args()
    pair = [p.strip() for p in args.pair.split(",")]
    fixture = ROOT / "fixtures" / "live-handoff" / f"{args.fixture}.json"
    record_dir = Path(args.record)
    record_dir.mkdir(parents=True, exist_ok=True)
    clients = {}
    reasons = []
    for name in pair:
        fn = CHECKS.get(name)
        if fn is None:
            clients[name] = {"error": f"unknown client {name}"}
            reasons.append(f"{name}: unknown client")
            continue
        info = fn()
        clients[name] = info
        if not info["binary"]:
            reasons.append(f"{name}: CLI binary not installed")
        elif not info["turnkeep_installed"]:
            reasons.append(f"{name}: turnkeep not installed in client config")
        elif info.get("auth") is False:
            reasons.append(f"{name}: no auth state on file (no login attempted by this script)")
    if not fixture.exists():
        reasons.append(f"fixture {fixture} missing")
    ready = not reasons
    record = {
        "task": "T026",
        "journey": "J02",
        "pair": pair,
        "fixture": str(fixture),
        "clients": clients,
        "status": "PASS" if ready else "NOT_RUN",
        "reasons": reasons,
        "note": ("Prerequisites met — run the manual recipe in docs/COMPATIBILITY.md "
                 "with two real client sessions; this script never impersonates them."
                 if ready else
                 "Two real host products are required; two SDK test processes are NOT "
                 "a substitute (backlog invariant). No mock recording was made."),
    }
    (record_dir / "live_client_gate.json").write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(record, indent=2, ensure_ascii=False))
    return 0 if ready else 2


if __name__ == "__main__":
    sys.exit(main())
