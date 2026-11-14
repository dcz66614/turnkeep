#!/usr/bin/env python3
"""T026 two-client handover runner — substitute pair (Sam's decision 2026-11-09).

Real Claude Code is NOT tested (not installed; Sam said skip it). Per the
T026 spec substitution clause ("as specified client unavailable, choose
another real MCP client per official docs; at least two different host
products"), the pair is:

  A-side: Codex CLI 0.164.0-alpha.9 via the Copilot bridge (model GPT-5.6 Sol)
  B-side: agy CLI (model gemini-3.8-flash-low)

Both are real host products with the Turnkeep MCP server installed in
their own configs (codex: ~/.codex/config.toml [mcp_servers.turnkeep];
agy: `agy mcp add turnkeep ...`). Each session is a fresh, separate
client process; no context is hand-copied between them. All memory
operations go through the turnkeep MCP tools (begin/recall/remember/
correct/resume/finish/brief) — never the turnkeep CLI.

Phases (all against one shared DB + shared scope "export-format"):
  smoke  : each client calls turnkeep recall via MCP (proves MCP wiring)
  S1     : Codex remembers the export.format requirement, begins a run,
           writes export-manifest.json from frozen-tree/, finishes, and
           writes a handoff record with its run_id.
  S2     : agy (fresh session) recalls + briefs, resumes A's run, verifies
           the manifest, finishes the candidate run.
  S3     : agy remembers a decision, begins a run, writes
           export-checklist.md, finishes, writes handoff 2 (reverse leg).
  S3b    : Codex (fresh session) recalls + briefs, resumes, verifies the
           checklist, finishes the candidate run.
  S4a    : Codex begins a run "verify export manifest v2" and writes a
           draft verify-v2.md WITHOUT finishing.
  change : harness corrects export.format -> tar.gz ... (product API).
  S4b    : Codex (same conversation) finishes the old run -> must be
           rejected STALE_SNAPSHOT -> resumes, re-reads, fixes the
           manifest + verify-v2.md, finishes the candidate run.

Usage:
  python scripts/run_t026_handover.py --smoke-only
  python scripts/run_t026_handover.py            # full run (skips smoke if done)
  python scripts/run_t026_handover.py --from S2  # resume from a phase

Outputs (never inside a T042 campaign dir):
  evidence/live-clients/run-20261109-01/{handover.sqlite3,work/,meta/,SUMMARY.json}
"""
import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmarks" / "live"))

from codex_driver import _run_codex, parse_rollout  # noqa: E402
from agy_driver import _run_agy, parse_stream  # noqa: E402

RUN_DIR = ROOT / "evidence" / "live-clients" / "run-20261109-01"
WORKDIR = RUN_DIR / "work"
META = RUN_DIR / "meta"
DB = RUN_DIR / "handover.sqlite3"
SCOPE = "export-format"
TIMEOUT = 600

MCP_NOTE = (
    "You have a Turnkeep MCP server named \"turnkeep\" with tools: begin, recall, "
    "remember, correct, retract, resolve_conflict, resume, finish, brief. "
    "Use ONLY these MCP tools for ALL memory operations — never the turnkeep CLI "
    "and never shell commands for memory. Use shell tools only to read/write "
    "ordinary files and to compute sha256sum. You MUST do the work with tool "
    "calls. Do not just reply in text.\n\n"
)

# Codex leg: this Codex build (0.164.0-alpha.9, exec mode) does NOT surface
# MCP server tools to the model — proven with the turnkeep server AND a
# trivial one-tool dummy MCP server (both listed enabled; the model only
# ever sees the built-in tools + MCP resource listing). So the Codex leg
# uses the Turnkeep CLI via the ./turnkeep wrapper in the workdir — the
# same live pattern the T042 drivers used for 432 real attempts. The agy
# leg uses real MCP tool calls. Both legs are real host products, real
# tool calls, separate fresh sessions, one shared DB.
CLI_NOTE = (
    "A Turnkeep CLI wrapper ./turnkeep is in the workdir (it runs the real "
    "Turnkeep product against the shared handover database). Use ONLY ./turnkeep "
    "for ALL memory operations, always with --scope export-format, e.g.:\n"
    "  ./turnkeep remember --scope export-format --key <key> --value \"<value>\" "
    "--kind <kind> --source <id:type>\n"
    "  ./turnkeep begin --scope export-format --task \"<task>\"\n"
    "  ./turnkeep recall --scope export-format --query \"<query>\"\n"
    "  ./turnkeep brief --scope export-format --goal \"<goal>\"\n"
    "  ./turnkeep resume --scope export-format --run <run-id>\n"
    "  ./turnkeep finish --scope export-format --run <run-id> --key <key> "
    "--value \"<value>\" --idempotency-key <key>\n"
    "Use shell tools only to read/write ordinary files and to compute sha256sum. "
    "You MUST do the work with tool calls. Do not just reply in text.\n\n"
)

PROMPTS = {
    "smoke-codex": CLI_NOTE + (
        "Smoke test: run ./turnkeep recall --scope export-format --query \"\" and "
        "report how many active records exist in the scope. Then end your turn with "
        "the single word OK on its own line.\n"
    ),
    "smoke-agy": MCP_NOTE + (
        "Smoke test: call the turnkeep MCP tool recall with query \"\" (empty) and "
        "report how many active records exist in the scope. Then end your turn with "
        "the single word OK on its own line.\n"
    ),
    "S1": CLI_NOTE + (
        "# Handover task (client A — Codex)\n\n"
        "You are client A in a two-client handover test for Turnkeep, a local-first "
        "working-memory system. A second real client will resume your work from "
        "Turnkeep alone — it sees nothing of this conversation.\n\n"
        "Steps, in order, all with tool calls:\n"
        "1. Remember the requirement: ./turnkeep remember --scope export-format "
        "--key export.format --value \"zip with manifest.json + SHA256SUMS\" "
        "--kind requirement --source u:1:user_statement\n"
        "2. Begin a run: ./turnkeep begin --scope export-format --task \"produce export "
        "manifest\". Note the run_id from the JSON output.\n"
        "3. In the frozen tree at frozen-tree/ (README.txt, data.csv, config.json), "
        "compute each file's SHA-256 with sha256sum and write a manifest file "
        "export-manifest.json in the workdir: {\"format\": \"zip with manifest.json + "
        "SHA256SUMS\", \"files\": [{\"path\": ..., \"sha256\": ..., \"bytes\": ...}, ...]}. "
        "Verify it with cat.\n"
        "4. Finish the run: ./turnkeep finish --scope export-format --run <your run_id> "
        "--key export.manifest --value '<manifest JSON text>' --idempotency-key "
        "export-a-1. Note the receipt.\n"
        "5. Remember a handoff record: ./turnkeep remember --scope export-format "
        "--key export.handoff --kind handoff --value '{\"run_id\": \"<your run_id>\", "
        "\"next_step\": \"verify the manifest against the frozen source tree\", "
        "\"evidence\": [\"export.format\", \"export.manifest\"]}' --source agent:codex:agent_inference\n"
        "6. End with a one-line summary including your run_id.\n"
    ),
    "S2": MCP_NOTE + (
        "# Handover task (client B — agy)\n\n"
        "You are client B in a two-client handover test for Turnkeep. You have NO "
        "context from client A except what Turnkeep holds — do not assume anything, "
        "read it from Turnkeep.\n\n"
        "Steps, in order, all with tool calls:\n"
        "1. Recall: turnkeep recall query \"export format\". Also read the brief: "
        "turnkeep brief goal \"export format handover\".\n"
        "2. Find the handoff record (key \"export.handoff\") for the run_id and next "
        "step. Resume that run with turnkeep resume and note the CANDIDATE run_id "
        "from the resume output.\n"
        "3. Verify export-manifest.json against frozen-tree/ by recomputing sha256sum "
        "for each file and comparing hashes and byte sizes.\n"
        "4. Finish the CANDIDATE run (from resume — NOT the original run) with key "
        "\"export.manifest.verified\", value \"manifest verified: all 3 files match "
        "frozen-tree\", idempotency_key \"export-b-1\".\n"
        "5. End with a one-line summary including the candidate run_id and the "
        "verification result.\n"
    ),
    "S3": MCP_NOTE + (
        "# Handover task, reverse direction (client A — agy)\n\n"
        "You are client A of the reverse leg of a two-client handover test for "
        "Turnkeep. A second real client will resume your work from Turnkeep alone.\n\n"
        "Steps, in order, all with tool calls:\n"
        "1. Remember the decision: turnkeep remember key \"export.checklist.style\", "
        "value \"one line per check, PASS/FAIL plus first 12 hex chars of sha256\", "
        "kind \"decision\", source_id \"user_statement\".\n"
        "2. Begin a run: turnkeep begin task \"produce export checklist\". Note the run_id.\n"
        "3. Write export-checklist.md in the workdir: one line per frozen-tree file "
        "(README.txt, data.csv, config.json) with its recomputed sha256 (first 12 hex "
        "chars) and PASS. Verify it with cat.\n"
        "4. Finish the run: turnkeep finish run_id <your run_id>, key \"export.checklist\", "
        "value = the checklist text, idempotency_key \"export-a2-1\". Note the receipt.\n"
        "5. Remember a handoff record: turnkeep remember key \"export.handoff.2\", kind "
        "\"handoff\", value = JSON {\"run_id\": \"<your run_id>\", \"next_step\": \"verify the "
        "checklist against the frozen tree\", \"evidence\": [\"export.checklist.style\", "
        "\"export.checklist\"]}, source_id \"agent:agy\".\n"
        "6. End with a one-line summary including your run_id.\n"
    ),
    "S3c": MCP_NOTE + (
        "# Handover task, reverse direction — COMPLETION (client A — agy)\n\n"
        "An earlier agy session already remembered the decision "
        "(key \"export.checklist.style\"), began a run and wrote "
        "export-checklist.md, but was interrupted before finishing. Verify "
        "everything from Turnkeep — do not trust this prompt alone.\n\n"
        "Steps, in order, all with tool calls:\n"
        "1. Recall: turnkeep recall query \"export checklist\". Also read the brief: "
        "turnkeep brief goal \"export checklist handover\".\n"
        "2. Find the open run for task \"produce export checklist\" and resume it with "
        "turnkeep resume; note the CANDIDATE run_id from the resume output.\n"
        "3. Verify export-checklist.md against frozen-tree/ by recomputing sha256sum "
        "for each file and comparing the 12-hex prefixes.\n"
        "4. Finish the CANDIDATE run (from resume — NOT the original run) with key "
        "\"export.checklist\", value = the checklist text, idempotency_key "
        "\"export-a2-1\". Note the receipt.\n"
        "5. Remember a handoff record: turnkeep remember key \"export.handoff.2\", kind "
        "\"handoff\", value = JSON {\"run_id\": \"<candidate run_id>\", \"next_step\": "
        "\"verify the checklist against the frozen tree\", \"evidence\": "
        "[\"export.checklist.style\", \"export.checklist\"]}, source_id \"agent:agy\".\n"
        "6. End with a one-line summary including the candidate run_id.\n"
    ),
    "S3b": CLI_NOTE + (
        "# Handover task, reverse direction (client B — Codex)\n\n"
        "You are client B of the reverse leg of a two-client handover test for "
        "Turnkeep. You have NO context from the other client except what Turnkeep "
        "holds.\n\n"
        "Steps, in order, all with tool calls:\n"
        "1. Recall: ./turnkeep recall --scope export-format --query \"export checklist\". "
        "Also read the brief: ./turnkeep brief --scope export-format --goal \"export "
        "checklist handover\".\n"
        "2. Find the handoff record (key \"export.handoff.2\") for the run_id and next "
        "step. Resume that run: ./turnkeep resume --scope export-format --run <run_id> "
        "and note the CANDIDATE run_id from the resume output.\n"
        "3. Verify export-checklist.md against frozen-tree/ by recomputing sha256sum "
        "for each file and comparing the 12-hex prefixes.\n"
        "4. Finish the CANDIDATE run (from resume — NOT the original run): "
        "./turnkeep finish --scope export-format --run <candidate run_id> --key "
        "export.checklist.verified --value \"checklist verified: all 3 files match "
        "frozen-tree\" --idempotency-key export-b2-1.\n"
        "5. End with a one-line summary including the candidate run_id and the "
        "verification result.\n"
    ),
    "S4a": CLI_NOTE + (
        "# Stale-finish round, part 1 (Codex)\n\n"
        "Steps, in order, all with tool calls:\n"
        "1. Recall: ./turnkeep recall --scope export-format --query \"export format\" — "
        "note the CURRENT format value.\n"
        "2. Begin a run: ./turnkeep begin --scope export-format --task \"verify export "
        "manifest v2\". Note the run_id from the JSON output.\n"
        "3. Write verify-v2.md in the workdir stating the manifest format is the value "
        "from step 1, listing the three frozen-tree files with their sha256 hashes "
        "recomputed just now, and that the manifest matches.\n"
        "4. Do NOT finish the run yet — the requirement may change while you wait. "
        "End your turn with a one-line summary including the run_id.\n"
    ),
    "S4b": CLI_NOTE + (
        "# Stale-finish round, part 2 (Codex, same run as part 1)\n\n"
        "A requirement change arrived after you began your run. Now:\n"
        "1. Finish your run from part 1: ./turnkeep finish --scope export-format "
        "--run <your run_id> --key export.manifest.v2 --value \"manifest v2 verified "
        "against frozen-tree\" --idempotency-key export-v2-1.\n"
        "2. If that finish is rejected with STALE_SNAPSHOT, do NOT just retry it: run "
        "./turnkeep resume --scope export-format --run <your run_id>, read the diff, "
        "re-read the CURRENT export.format requirement with ./turnkeep recall, update "
        "verify-v2.md AND export-manifest.json to the NEW format (same files, same "
        "hashes, format field changed), then finish the CANDIDATE run from resume: "
        "./turnkeep finish --scope export-format --run <candidate run_id> --key "
        "export.manifest.v2 --value \"manifest v2 verified against frozen-tree, format "
        "tar.gz\" --idempotency-key export-v2-2.\n"
        "3. End with a one-line summary stating whether the first finish was rejected "
        "and what the current format is.\n"
    ),
}

ORDER = ["smoke-codex", "smoke-agy", "S1", "S2", "S3", "S3b", "S4a", "change", "S4b"]


def mcp_calls(trace):
    """Tool calls that went through the turnkeep MCP server.

    Codex MCP tools surface as function_call names containing 'turnkeep';
    agy stream-json reports them as tool_name "call_mcp_tool" with the
    server name in arguments ({"ServerName": "turnkeep", "ToolName": ...}).
    CLI invocations of `turnkeep ...` are NOT counted here (the prompt
    forbids them).
    """
    out = []
    for tc in trace.get("tool_calls", []):
        name = (tc.get("name") or "").lower()
        if "turnkeep" in name:
            out.append(tc)
            continue
        if name == "call_mcp_tool":
            try:
                args = json.loads(tc.get("arguments") or "{}")
            except Exception:
                args = {}
            if isinstance(args, dict) and args.get("ServerName") == "turnkeep":
                out.append(tc)
    return out


def cli_calls(trace):
    """Tool calls that invoked the ./turnkeep CLI wrapper (Codex leg)."""
    out = []
    for tc in trace.get("tool_calls", []):
        args = tc.get("arguments") or ""
        if "./turnkeep " in args or "./turnkeep\"" in args:
            out.append(tc)
    return out


def ensure_wrapper():
    """./turnkeep in the workdir: real CLI against the shared handover DB."""
    w = WORKDIR / "turnkeep"
    w.write_text(
        "#!/bin/sh\n# Local wrapper so the agent can run the Turnkeep CLI here.\n"
        f"exec {ROOT}/.venv/bin/python -m turnkeep --db {DB} --json \"$@\"\n",
        encoding="utf-8")
    w.chmod(0o755)
    return w


def run_codex_turn(tag, prompt, session_id=None):
    (META / f"prompt-{tag}.txt").write_text(prompt, encoding="utf-8")
    r = _run_codex(WORKDIR, prompt, session_id, TIMEOUT, META)
    (META / f"codex-{tag}.stdout.txt").write_text(r["stdout"], encoding="utf-8")
    (META / f"codex-{tag}.stderr.txt").write_text(r["stderr"], encoding="utf-8")
    parsed = parse_rollout(r["session_id"])
    (META / f"rollout-{tag}.json").write_text(
        json.dumps(parsed, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"client": "codex", "tag": tag, "session_id": r["session_id"],
            "returncode": r["returncode"], "duration_ms": r["duration_ms"],
            "tokens_used": r["tokens_used"], "timed_out": r["timed_out"],
            "rollout": parsed, "stderr_tail": r["stderr"][-500:]}


def run_agy_turn(tag, prompt, conversation_id=None):
    (META / f"prompt-{tag}.txt").write_text(prompt, encoding="utf-8")
    r = _run_agy(WORKDIR, prompt, conversation_id, TIMEOUT, META)
    (META / f"agy-{tag}.stdout.txt").write_text(r["stdout"], encoding="utf-8")
    (META / f"agy-{tag}.stderr.txt").write_text(r["stderr"], encoding="utf-8")
    parsed = r["parsed"]
    (META / f"rollout-{tag}.json").write_text(
        json.dumps(parsed, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"client": "agy", "tag": tag, "session_id": r["session_id"],
            "returncode": r["returncode"], "duration_ms": r["duration_ms"],
            "tokens_used": r["tokens_used"], "timed_out": r["timed_out"],
            "quota_error": r.get("quota_error", False),
            "rollout": parsed, "stderr_tail": r["stderr"][-500:]}


def db_state():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    recs = [dict(r) for r in con.execute(
        "SELECT id, key, kind, state, revision, value_json FROM memories "
        "WHERE scope_id=? ORDER BY id", (SCOPE,))]
    runs = [dict(r) for r in con.execute(
        "SELECT id AS run_id, task, status, snapshot_epoch FROM runs WHERE scope_id=? "
        "ORDER BY rowid", (SCOPE,))]
    events = [dict(r) for r in con.execute(
        "SELECT scope_epoch, event_type, affected_ids_json FROM events "
        "WHERE scope_id=? ORDER BY scope_epoch", (SCOPE,))]
    con.close()
    return {"records": recs, "runs": runs, "events": events}


def apply_requirement_change():
    """Harness-side requirement change between S4a and S4b (product API,
    same as the T042 drivers' apply_change)."""
    sys.path.insert(0, str(ROOT / "src"))
    from turnkeep.storage import connect, transaction
    from turnkeep.corrections import correct
    db = connect(str(DB))
    with transaction(db, write=True):
        row = db.execute(
            "SELECT id, revision FROM memories WHERE scope_id=? AND key=? "
            "AND state='active'", (SCOPE, "export.format")).fetchone()
        if row is None:
            raise RuntimeError("export.format active record not found")
        rec_id, rev = row["id"], row["revision"]
    out = correct(db, SCOPE, rec_id, rev, "tar.gz with manifest.json + SHA256SUMS",
                  source_refs=[{"id": "u:change-1", "type": "user_statement"}])
    db.close()
    return {"changed_record": rec_id, "old_revision": rev,
            "new_value": "tar.gz with manifest.json + SHA256SUMS",
            "new_record_id": out["id"]}


def manifest_ok():
    """Harness-side check: export-manifest.json hashes match frozen-tree/."""
    p = WORKDIR / "export-manifest.json"
    if not p.exists():
        return {"exists": False}
    try:
        m = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        return {"exists": True, "parse_error": str(e)}
    ok = True
    for f in m.get("files", []):
        fp = WORKDIR / "frozen-tree" / f["path"]
        data = fp.read_bytes()
        if hashlib.sha256(data).hexdigest() != f["sha256"] or len(data) != f["bytes"]:
            ok = False
    return {"exists": True, "format": m.get("format"),
            "files": len(m.get("files", [])), "hashes_match": ok}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke-only", action="store_true")
    ap.add_argument("--from", dest="from_phase", default=None)
    ap.add_argument("--only", dest="only_phase", default=None)
    args = ap.parse_args()
    WORKDIR.mkdir(parents=True, exist_ok=True)
    META.mkdir(parents=True, exist_ok=True)
    ensure_wrapper()
    summary_path = RUN_DIR / "SUMMARY.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else {
        "task": "T026", "pair": ["codex", "agy"],
        "substitution": "Claude Code substituted by agy (real MCP client) per Sam's "
                        "2026-11-09 decision + T026 spec substitution clause; "
                        "Codex via Copilot bridge (GPT-5.6 Sol).",
        "codex_mcp_finding": "Codex CLI 0.164.0-alpha.9 exec mode does NOT surface "
            "MCP server tools to the model: proven with the turnkeep MCP server AND "
            "a trivial one-tool dummy MCP server (both listed enabled in `codex mcp "
            "list`; the model only ever sees built-in tools + MCP resource listing). "
            "Root cause #1 was the adapter template's `python3 -m turnkeep mcp` — "
            "system python3 has no `mcp` package (only the repo .venv does); fixed "
            "by pointing both clients at .venv/bin/python (mcp_smoke PASS, 9 tools). "
            "Even after that fix, Codex exposes resources only, no tools — so the "
            "Codex leg uses the Turnkeep CLI via the ./turnkeep wrapper (the same "
            "live pattern the T042 drivers used); the agy leg uses real MCP tool "
            "calls. Two real host products, real tool calls, separate fresh "
            "sessions, one shared DB.",
        "agy_mcp": "agy 1.5.3 exposes the turnkeep MCP server (call_mcp_tool, "
                   "ServerName turnkeep); smoke recall returned the live scope.",
        "clients": {"codex": "codex-cli 0.164.0-alpha.9 via Copilot bridge, model gpt5.6sol",
                    "agy": "agy 1.5.3, model gemini-3.8-flash-low"},
        "phases": {}, "change": None,
    }
    phases = summary["phases"]
    start = ORDER.index(args.from_phase) if args.from_phase else 0
    if args.only_phase:
        todo = [args.only_phase]
    elif args.smoke_only:
        todo = ["smoke-codex", "smoke-agy"]
    else:
        todo = ORDER[start:]
    sessions = {}
    for tag in todo:
        if tag == "change":
            print("== change: correcting export.format -> tar.gz ==", flush=True)
            try:
                summary["change"] = apply_requirement_change()
                print("   ", summary["change"], flush=True)
            except Exception as e:
                print("    CHANGE FAILED:", e, flush=True)
                summary["phases"]["change"] = {"status": "FAIL", "error": str(e)}
                break
            summary["phases"]["change"] = {"status": "OK", **summary["change"]}
            summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
            continue
        client = "codex" if tag in ("smoke-codex", "S1", "S3b", "S4a", "S4b") else "agy"
        sess = None
        if tag == "S4b":
            sess = sessions.get("S4a")
        print(f"== {tag} ({client}) session={sess} ==", flush=True)
        if client == "codex":
            res = run_codex_turn(tag, PROMPTS[tag], sess)
        else:
            res = run_agy_turn(tag, PROMPTS[tag], sess)
        sessions[tag] = res["session_id"]
        mc = mcp_calls(res["rollout"])
        cc = cli_calls(res["rollout"])
        stale = res["rollout"].get("stale_rejections", 0)
        res_light = {k: v for k, v in res.items() if k != "rollout"}
        res_light["mcp_tool_calls"] = [
            {"name": tc.get("name"), "arguments": (tc.get("arguments") or "")[:400]}
            for tc in mc]
        res_light["mcp_call_count"] = len(mc)
        res_light["cli_tool_calls"] = [
            {"name": tc.get("name"), "arguments": (tc.get("arguments") or "")[:400]}
            for tc in cc]
        res_light["cli_call_count"] = len(cc)
        res_light["stale_rejections"] = stale
        res_light["last_message"] = res["rollout"].get("last_message")
        phases[tag] = res_light
        print(f"    rc={res['returncode']} dur={res['duration_ms']}ms "
              f"mcp_calls={len(mc)} cli_calls={len(cc)} stale={stale} "
              f"completed={res['rollout'].get('task_completed')}", flush=True)
        summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        if res["timed_out"] or res["returncode"] not in (0, None):
            print(f"    WARNING: {tag} rc={res['returncode']} timed_out={res['timed_out']}; "
                  "continuing so the failure is on record", flush=True)
    summary["db_state"] = db_state()
    summary["manifest_check"] = manifest_ok()
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"manifest": summary["manifest_check"],
                      "records": len(summary["db_state"]["records"]),
                      "runs": len(summary["db_state"]["runs"]),
                      "events": len(summary["db_state"]["events"])}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
