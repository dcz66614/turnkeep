#!/usr/bin/env python3
"""Live-model attempt driver (T042).

Runs ONE attempt of a live evaluation task with the real agent:
Codex CLI 0.164.0 via the local copilot-codex-bridge (model gpt5.6sol,
GPT-5.6 Sol through Sam's existing M365 Copilot subscription, $0 new spend).

Design (mirrors the deterministic harness in benchmarks/harness/):
- The brief carries the ORIGINAL (possibly stale) facts.
- The memory artifact (per arm) carries the CURRENT facts. The runner seeds
  it and applies the change BEFORE the agent runs; the agent must consult
  it before producing the deliverable.
- Arms: none (no memory), markdown (decisions.md), log (events.log),
  turnkeep (tk.sqlite3 via the real SDK for seeding, CLI for the agent).
- Variant "recovery": change #1 before phase A, change #2 between phase A
  and a resumed phase B (codex exec resume). Only the final artifact scores.
- The agent runs with --sandbox workspace-write (the default read-only
  sandbox blocks ALL writes, which made the first probes fail with
  "Read-only file system" even though the model called the right tool).
- The prompt is fed via stdin ("-") because codex exec waits on an open
  stdin pipe when a positional prompt is given; the first probe hung 300 s
  on exactly that.

The driver never trusts the agent's claim: the trace records the raw
rollout (tool calls, outputs, token usage, latency) and the deliverable
artifact; scoring is done separately by score_live.py.

Two modes (env TURNKEEP_CODEX_MODE, read at call time; default "bridge"):
- "bridge": the original route — Codex CLI via the local
  copilot-codex-bridge (model gpt5.6sol, GPT-5.6 Sol through Sam's M365
  Copilot subscription, $0 new spend).
- "official": the official OpenAI API — Codex CLI with its own auth
  (~/.codex/auth.json, never read or copied by this code), model
  gpt-6-luna, reasoning effort low. No bridge base URL, no bridge key.
  Pay-as-you-go on Sam's OpenAI account; Sam authorised this route
  2026-11-09 ~19:54 GMT ("全部用luna low"). Used by the
  campaign-20261109-T042-codex-api campaign; the bridge and agy
  campaigns' numbers are never mixed with it.
  Code-mode-host fix (2026-11-09 ~22:59 GMT): GPT-6 Luna engages Codex
  "Code Mode" for tool execution, which needs the companion binary
  codex-code-mode-host as a sibling of the invoked codex binary. The
  image's /opt/hatch-image/bin is read-only and ships no host binary,
  so every workspace-write tool call failed ("failed to spawn code-mode
  host ... No such file or directory"); disabling the host
  (features.code_mode_host=false) also fails closed. Fix: the official
  host binary for release rust-v0.164.0-alpha.9 (matching the CLI
  0.164.0-alpha.9, downloaded from github.com/openai/codex releases,
  tarball size verified 28,128,048 bytes) lives in
  ~/workspace/codex-bin/ next to a COPY of the codex binary (306 MB,
  same version) plus codex-resources (bwrap). Official mode invokes
  that copy (TURNKEEP_CODEX_BIN overrides the path). A symlink does NOT
  work: codex resolves it to /opt/hatch-image/bin and looks there.
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

BRIDGE_BASE_URL = "http://127.0.0.1:8322/v1"
MODEL = "gpt5.6sol"
# Official OpenAI API route (Sam authorised 2026-11-09 ~19:54 GMT).
OFFICIAL_MODEL = "gpt-6-luna"
OFFICIAL_REASONING_EFFORT = "low"
CODEX_BIN = "/opt/hatch-image/bin/codex"
# Official mode must NOT use the image binary: GPT-6 Luna needs the
# code-mode-host companion, which only exists next to the COPY in
# ~/workspace/codex-bin/ (see the module docstring). Env-overridable.
OFFICIAL_CODEX_BIN = os.environ.get(
    "TURNKEEP_CODEX_BIN", str(Path.home() / "workspace" / "codex-bin" / "codex"))


def _mode():
    """'bridge' (default) or 'official' (official OpenAI API, gpt-6-luna)."""
    return os.environ.get("TURNKEEP_CODEX_MODE", "bridge").strip().lower() or "bridge"
SRC_REF = [{"id": "u:1", "type": "user_statement"}]

ARM_MEMORY = {
    "none": None,
    "markdown": "decisions.md",
    "log": "events.log",
    "turnkeep": "tk.sqlite3",
}

# Turnkeep product kinds (src/turnkeep/store.py KINDS). Suite facts may use
# other labels (e.g. F09 "inference"); the markdown/log arms record the
# label verbatim, but the turnkeep arm must use a product kind to seed the
# real client. Unknown labels map to "fact" here only — the kind label is
# metadata, never scored.
TK_KINDS = {"requirement", "fact", "decision", "attempt", "procedure", "handoff"}


def _tk_kind(kind):
    return kind if kind in TK_KINDS else "fact"


# Code Mode (GPT-6 Luna, official API): inner tool invocations inside the
# custom `exec` JavaScript, e.g. tools.exec_command({cmd:"cat decisions.md"}).
# Groups: (1) tool name, (2) double-quoted cmd, (3) single-quoted cmd.
_INNER_TOOL_RE = re.compile(
    r"""tools\.([A-Za-z_][A-Za-z0-9_]*)\(\{cmd:(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)')"""
)

ARM_INSTRUCTIONS = {
    "none": "You have no persistent memory. Only the brief above and the project files are available. If a fact changed after the brief was written, you have no way to know — do the best you can with what you have.",
    "markdown": "Your project memory is decisions.md in this directory. It records the project's facts; the LATEST entry for a key wins. Re-read it before producing the deliverable — it may have been updated since the brief was written.",
    "log": "Your project memory is events.log in this directory, an append-only log. The LATEST entry for a key wins. Search it (for example with grep) for the keys you need before producing the deliverable — it may have been updated since the brief was written.",
    "turnkeep": (
        "Your project memory is the Turnkeep database tk.sqlite3 in this directory. "
        "Use the ./turnkeep command to consult it BEFORE producing the deliverable — it may have been updated since the brief was written.\n"
        "Commands (run them in this directory, each needs --scope bench):\n"
        "  ./turnkeep recall --scope bench --query <key>   — show the current value of a fact\n"
        "  ./turnkeep brief --scope bench --goal \"<goal>\"  — show the current brief\n"
        "  ./turnkeep begin --scope bench --task \"<goal>\"  — start a run (prints a run id)\n"
        "  ./turnkeep finish --scope bench --run <run-id> --key <deliverable-key> --value \"<value>\" --idempotency-key <task-id>-1  — record the deliverable with a receipt\n"
        "  If finish is rejected with STALE_SNAPSHOT, run ./turnkeep resume --scope bench --run <run-id>, then re-check with recall and finish again.\n"
        "  Always record the deliverable with finish (with a receipt) after writing the deliverable file."
    ),
}


def _bridge_api_key():
    key_file = Path.home() / "workspace" / "copilot-codex-bridge" / ".api-key"
    return key_file.read_text(encoding="utf-8").strip()


def seed_memory(task, arm, workdir):
    """Write context files + the arm's memory with the INITIAL facts."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    for name, content in task.get("context_files", {}).items():
        p = workdir / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    (workdir / "output").mkdir(exist_ok=True)
    if arm == "none":
        return {}
    if arm == "markdown":
        lines = ["# Project decisions (memory)\n", "\nThe LATEST entry for a key wins.\n"]
        for f in task["initial"]:
            lines.append(f"- {f['key']} = {f['value']} ({f['kind']}) — recorded 2026-11-08, source: user\n")
        (workdir / "decisions.md").write_text("".join(lines), encoding="utf-8")
        return {}
    if arm == "log":
        lines = []
        for f in task["initial"]:
            lines.append(f"2026-11-08T10:00:00Z remember {f['key']} = {f['value']} ({f['kind']})\n")
        (workdir / "events.log").write_text("".join(lines), encoding="utf-8")
        return {}
    if arm == "turnkeep":
        from turnkeep.client import TurnkeepClient
        dbpath = workdir / "tk.sqlite3"
        ids = {}
        with TurnkeepClient(dbpath, "bench") as c:
            for f in task["initial"]:
                rec = c.remember(f["key"], f["value"], kind=_tk_kind(f["kind"]), source_refs=SRC_REF)
                ids[f["key"]] = {"id": rec["id"], "revision": rec["revision"]}
        wrapper = workdir / "turnkeep"
        wrapper.write_text(
            "#!/bin/sh\n# Local wrapper so the agent can run the Turnkeep CLI in this directory.\n"
            f"exec {ROOT}/.venv/bin/python -m turnkeep --db tk.sqlite3 --json \"$@\"\n",
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
        return {"turnkeep_ids": ids}
    raise ValueError(f"unknown arm {arm!r}")


def apply_change(task, arm, workdir, change, meta):
    """Apply one change to the arm's memory. No-op for type 'none'."""
    if not change or change.get("type") in (None, "none"):
        return
    workdir = Path(workdir)
    key, ctype = change["key"], change["type"]
    if arm == "none":
        return
    if arm == "markdown":
        p = workdir / "decisions.md"
        if ctype == "correct":
            kind = next((f["kind"] for f in task["initial"] if f["key"] == key), "fact")
            with p.open("a", encoding="utf-8") as fh:
                fh.write(f"- {key} = {change['value']} ({kind}) — corrected 2026-11-08, supersedes the earlier line\n")
        elif ctype == "retract":
            old = next((f["value"] for f in task["initial"] if f["key"] == key), "?")
            with p.open("a", encoding="utf-8") as fh:
                fh.write(f"- RETRACTED: {key} (was {old}) — source retracted 2026-11-08\n")
        else:
            raise ValueError(f"unsupported change type {ctype!r}")
        return
    if arm == "log":
        p = workdir / "events.log"
        if ctype == "correct":
            with p.open("a", encoding="utf-8") as fh:
                fh.write(f"2026-11-08T11:00:00Z correct {key} = {change['value']}\n")
        elif ctype == "retract":
            old = next((f["value"] for f in task["initial"] if f["key"] == key), "?")
            with p.open("a", encoding="utf-8") as fh:
                fh.write(f"2026-11-08T11:00:00Z retract {key} (was {old})\n")
        else:
            raise ValueError(f"unsupported change type {ctype!r}")
        return
    if arm == "turnkeep":
        # The product's record lifecycle matters across two changes
        # (recovery tasks apply change, run phase A, then apply change2):
        # correct() creates a NEW record id and supersedes the old one, and
        # the phase-A agent may itself have modified the key. The old code
        # kept the stale id from seeding (and discarded correct()'s return
        # value), so change2 crashed with "Only active records can be
        # corrected (state=superseded/retracted)" on every recovery task.
        # Fix: re-read the key's CURRENT state from the DB before applying
        # each change, use the operation's returned record, and translate
        # each state faithfully (retracted -> remember anew with replaces;
        # conflicted -> resolve_conflict to the new value).
        from turnkeep.client import TurnkeepClient
        import sqlite3
        ids = meta.get("turnkeep_ids", {})
        dbpath = workdir / "tk.sqlite3"

        def _current(key):
            con = sqlite3.connect(f"file:{dbpath}?mode=ro", uri=True)
            try:
                rows = con.execute(
                    "SELECT id, revision, state, kind FROM memories "
                    "WHERE scope_id=? AND key=?", ("bench", key)).fetchall()
            finally:
                con.close()
            active = [r for r in rows if r[2] == "active"]
            conflicted = [r for r in rows if r[2] == "conflicted"]
            retracted = [r for r in rows if r[2] == "retracted"]
            return active, conflicted, retracted

        def _track(rec):
            ids[key] = {"id": rec["id"], "revision": rec["revision"]}

        with TurnkeepClient(dbpath, "bench") as c:
            if ctype == "correct":
                active, conflicted, retracted = _current(key)
                if conflicted:
                    cands = [{"id": r[0], "revision": r[1]} for r in conflicted]
                    _track(c.resolve(key, cands, change["value"], source_refs=SRC_REF))
                elif active:
                    rec = active[0]
                    _track(c.correct(rec[0], rec[1], change["value"], source_refs=SRC_REF))
                elif retracted:
                    old = retracted[0]
                    _track(c.remember(key, change["value"], kind=old[3], source_refs=SRC_REF,
                                      replaces_ids=[old[0]]))
                else:
                    _track(c.remember(key, change["value"], kind="fact", source_refs=SRC_REF))
            elif ctype == "retract":
                active, conflicted, retracted = _current(key)
                if active:
                    rec = active[0]
                    _track(c.retract(rec[0], rec[1], "correction"))
                elif conflicted:
                    # Retracting the fact retracts every live candidate for it.
                    for r in conflicted:
                        c.retract(r[0], r[1], "correction")
                    ids[key] = {"id": conflicted[0][0], "revision": conflicted[0][1]}
                # Already retracted, or never recorded: no-op.
            else:
                raise ValueError(f"unsupported change type {ctype!r}")
        meta["turnkeep_ids"] = ids
        return
    raise ValueError(f"unknown arm {arm!r}")


def build_prompt(task, arm, phase="A"):
    mem = ARM_INSTRUCTIONS[arm]
    d = task["deliverable"]
    if arm == "turnkeep":
        mem += (
            "\nExact finish command for this task (copy it, filling in <run-id> and <value>):\n"
            f"  ./turnkeep finish --scope bench --run <run-id> --key {d['key']} "
            f"--value \"<value>\" --idempotency-key {task['id']}-1\n"
        )
    if phase == "A":
        return (
            f"# Task\n\n{task['brief']}\n\n"
            f"# Memory\n\n{mem}\n\n"
            "# How to work\n\n"
            "Follow these steps in order. You MUST do the work with tool calls (exec_command). Do not just reply in text.\n"
            "1. Read the task files and your memory FIRST.\n"
            f"2. Write the deliverable to {d['file']} with exactly the format the task asks for.\n"
            "3. Verify the deliverable with `cat`.\n"
            "4. End your turn with a one-line summary of what you wrote and where.\n"
            "Only the deliverable file is scored; your chat reply is not scored. "
            "The task is NOT finished until the deliverable file exists and you have verified it with `cat`.\n"
            "- Never invent facts. Only use the brief, the project files, and your memory.\n"
            "- Do not use the network.\n"
        )
    return (
        "A further change arrived while you were finishing. Re-check your memory for the current facts "
        "(do not trust the values from your first attempt), update the deliverable file if needed, "
        "verify with `cat`, then end your turn with a one-line summary.\n"
        "If you use Turnkeep and your previous finish is now stale, resume the run and finish again with the new value.\n"
        "You MUST do the work with tool calls (exec_command). Do not just reply in text.\n"
        "Only the deliverable file is scored; your chat reply is not scored. "
        "The task is NOT finished until the deliverable file exists and you have verified it with `cat`.\n"
    )


def _codex_base_argv(workdir):
    if _mode() == "official":
        # Official OpenAI API: Codex CLI's own auth (~/.codex/auth.json).
        # No provider override, no bridge base URL — the CLI talks to
        # api.openai.com directly. Flags match the parent-verified smoke
        # invocation (2026-11-09 ~22:51 GMT: OK, 12,656 tokens, ~17 s).
        return [
            OFFICIAL_CODEX_BIN, "exec", "--skip-git-repo-check", "--sandbox", "workspace-write",
            "--model", OFFICIAL_MODEL,
            "-c", f'model_reasoning_effort="{OFFICIAL_REASONING_EFFORT}"',
            "-C", str(workdir),
        ]
    return [
        CODEX_BIN, "exec", "--skip-git-repo-check", "--sandbox", "workspace-write",
        "-c", f"model={MODEL}",
        "-c", "model_provider=copilot",
        "-c", 'model_providers.copilot.name="Copilot (M365)"',
        "-c", f'model_providers.copilot.base_url="{BRIDGE_BASE_URL}"',
        "-c", 'model_providers.copilot.env_key="COPILOT2API_KEY"',
        "-c", 'model_providers.copilot.wire_api="responses"',
        "-C", str(workdir),
    ]


def _run_codex(workdir, prompt, session_id, timeout_s, meta_dir):
    """Run one codex turn. Returns a dict with returncode/stdout/session/tokens.

    In official mode the bridge key is never loaded and any bridge
    base-URL env is stripped, so a stray env var cannot reroute the
    official-API run through the bridge. API errors are flagged
    (quota_error on 429/rate-limit, auth_error on 401/403) so the
    runner can stop instead of burning attempts.
    """
    env = dict(os.environ)
    env["PATH"] = "/opt/hatch-image/bin:" + env.get("PATH", "")
    if _mode() == "official":
        env.pop("OPENAI_BASE_URL", None)
        env.pop("COPILOT2API_KEY", None)
        # The copied binary's dir also holds codex-code-mode-host.
        env["PATH"] = str(Path(OFFICIAL_CODEX_BIN).parent) + ":" + env.get("PATH", "")
    else:
        env["COPILOT2API_KEY"] = _bridge_api_key()
    if session_id:
        argv = _codex_base_argv(workdir) + ["resume", session_id, "-"]
    else:
        argv = _codex_base_argv(workdir) + ["-"]
    t0 = time.monotonic()
    try:
        # NOTE: do NOT pass cwd=workdir here. Codex receives the workspace
        # via -C; setting the process cwd to the workdir as well makes
        # codex fail at startup with "No such file or directory (os error 2)"
        # (reproduced: cwd set -> instant fail; cwd unset + -C -> works).
        # The agent's commands still run with cwd=workdir (codex sets it).
        proc = subprocess.run(argv, input=prompt, capture_output=True, text=True,
                              timeout=timeout_s, env=env, encoding="utf-8")
        timed_out = False
        stdout, stderr = proc.stdout, proc.stderr
        rc = proc.returncode
    except subprocess.TimeoutExpired as e:
        timed_out = True
        stdout = (e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        stderr = (e.stderr or b"").decode("utf-8", "replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
        rc = None
    dur_ms = int((time.monotonic() - t0) * 1000)
    # Codex prints its progress (session id, tokens used) to STDERR and
    # only the last message to STDOUT; parse the combined streams.
    combined = (stdout or "") + "\n" + (stderr or "")
    m = re.search(r"session id:\s*([0-9a-f-]{36})", combined)
    sid = m.group(1) if m else session_id
    m2 = re.search(r"tokens used\s*\n([\d,]+)", combined)
    tokens = int(m2.group(1).replace(",", "")) if m2 else None
    # API-error detection uses full phrases, never bare status numbers:
    # bare substrings false-positived on stderr timestamps ("...22.429665")
    # and hex digests ("...bef401980..."), and even word boundaries
    # false-positive on the thousands-separated "tokens used" line
    # ("17,401" matches \b401\b) — one confirmation row was flagged that
    # way while completing normally. The "tokens used" line is stripped
    # before scanning; only error phrases count.
    scan = re.sub(r"tokens used\s*\n[\d,]+", "", combined, flags=re.I)
    low = scan.lower()
    quota_error = ("rate limit" in low or "rate_limit" in low
                   or "insufficient_quota" in low or "quota exceeded" in low
                   or "quota_exceeded" in low or "too many requests" in low
                   or "status: 429" in low or "error 429" in low)
    auth_error = ("unauthorized" in low or "invalid api key" in low
                  or "incorrect api key" in low
                  or "status: 401" in low or "error 401" in low
                  or "401 unauthorized" in low
                  or "status: 403" in low or "error 403" in low
                  or "403 forbidden" in low)
    return {"returncode": rc, "stdout": stdout, "stderr": stderr,
            "session_id": sid, "tokens_used": tokens,
            "duration_ms": dur_ms, "timed_out": timed_out,
            "quota_error": quota_error, "auth_error": auth_error}


def _find_rollout(session_id):
    """Locate the codex rollout JSONL for a session id."""
    base = Path.home() / ".codex" / "sessions"
    if not session_id:
        return None
    for p in base.rglob(f"rollout-*-{session_id}.jsonl"):
        return p
    return None


def parse_rollout(session_id):
    """Extract tool calls, messages and usage from the rollout file.

    Note: STALE_SNAPSHOT is counted ONLY in tool OUTPUTS (results), never
    in arguments — the agent's own commands may legitimately contain the
    string (e.g. a fallback script that greps for it). Likewise the finish
    receipt is detected in outputs only.
    """
    out = {"tool_calls": [], "agent_messages": [], "max_total_tokens": None,
           "task_duration_ms": None, "last_message": None, "found": False,
           "task_completed": False, "stale_rejections": 0, "finish_receipt": False,
           "usage_split": None}
    p = _find_rollout(session_id)
    if not p:
        return out
    out["found"] = True
    out["rollout_path"] = str(p)
    max_tokens = None
    # Usage records are cumulative per session: keep the max of each
    # component across records (input / cached / cache-write / output /
    # reasoning). Needed for real spend accounting on the official API.
    split_keys = ("input_tokens", "cached_input_tokens",
                  "cache_write_input_tokens", "output_tokens",
                  "reasoning_output_tokens")
    split = {k: 0 for k in split_keys}

    def _absorb_usage(u):
        nonlocal max_tokens
        if not isinstance(u, dict):
            return
        tot = u.get("total_tokens")
        if isinstance(tot, int):
            max_tokens = tot if max_tokens is None else max(max_tokens, tot)
        for k in split_keys:
            v = u.get(k)
            if isinstance(v, int):
                split[k] = max(split[k], v)

    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        t, payload = d.get("type"), d.get("payload", {})
        if t == "response_item" and payload.get("type") == "function_call":
            out["tool_calls"].append({
                "name": payload.get("name"),
                "arguments": payload.get("arguments", ""),
                "timestamp": d.get("timestamp"),
            })
        elif t == "response_item" and payload.get("type") == "function_call_output":
            output = str(payload.get("output", ""))
            if "STALE_SNAPSHOT" in output:
                out["stale_rejections"] += 1
            if '"receipt"' in output:
                out["finish_receipt"] = True
            out["tool_calls"].append({
                "name": "__output__",
                "arguments": "",
                "output": output[:2000],
                "timestamp": d.get("timestamp"),
            })
        elif t == "response_item" and payload.get("type") == "custom_tool_call":
            # Code Mode (GPT-6 Luna on the official API): the model calls
            # one custom `exec` tool whose input is JavaScript that
            # orchestrates inner tools, e.g.
            #   const r = await tools.exec_command({cmd:"cat decisions.md",...});
            # Parse each inner tools.<name>({cmd:"..."}) as one step so
            # step counts and memory-consulted detection match the classic
            # function_call traces from the bridge route.
            js = str(payload.get("input", ""))
            found = list(_INNER_TOOL_RE.finditer(js))
            if found:
                for m in found:
                    cmd = m.group(2) if m.group(2) is not None else m.group(3)
                    out["tool_calls"].append({
                        "name": m.group(1),
                        "arguments": cmd or "",
                        "timestamp": d.get("timestamp"),
                    })
            else:
                out["tool_calls"].append({
                    "name": payload.get("name") or "exec",
                    "arguments": js[:2000],
                    "timestamp": d.get("timestamp"),
                })
        elif t == "response_item" and payload.get("type") == "custom_tool_call_output":
            raw = payload.get("output") or []
            parts = []
            if isinstance(raw, str):
                parts.append(raw)
            else:
                for item in raw:
                    if isinstance(item, dict):
                        parts.append(str(item.get("text", "")))
                    else:
                        parts.append(str(item))
            output = "".join(parts)
            if "STALE_SNAPSHOT" in output:
                out["stale_rejections"] += 1
            if '"receipt"' in output:
                out["finish_receipt"] = True
            out["tool_calls"].append({
                "name": "__output__",
                "arguments": "",
                "output": output[:2000],
                "timestamp": d.get("timestamp"),
            })
        elif t == "response_item" and payload.get("type") == "message" and payload.get("role") == "assistant":
            for c in payload.get("content", []):
                if c.get("type") == "output_text":
                    out["agent_messages"].append(c.get("text", ""))
        elif t == "token_usage_record":
            _absorb_usage(payload.get("usage") or {})
        elif t == "event_msg" and payload.get("type") == "token_count":
            _absorb_usage((payload.get("info") or {}).get("total_token_usage") or {})
        elif t == "event_msg" and payload.get("type") == "task_complete":
            out["task_completed"] = True
            out["task_duration_ms"] = payload.get("duration_ms")
            out["last_message"] = payload.get("last_agent_message")
    out["max_total_tokens"] = max_tokens
    if max_tokens is not None:
        out["usage_split"] = dict(split)
    return out


def read_deliverable(workdir, task):
    p = Path(workdir) / task["deliverable"]["file"]
    if not p.exists():
        return {"exists": False, "content": None}
    return {"exists": True, "content": p.read_text(encoding="utf-8").strip()}


def turnkeep_receipt(workdir):
    """Durable finish evidence: an accepted run with a receipt in the DB.

    The agent's shell often captures the finish JSON into a variable and
    never prints it, so the rollout text cannot prove a receipt. The DB
    row (runs.status='accepted' AND receipt_json NOT NULL) can.
    """
    db = Path(workdir) / "tk.sqlite3"
    if not db.exists():
        return False
    try:
        import sqlite3
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        row = con.execute(
            "SELECT COUNT(*) FROM runs WHERE status='accepted' AND receipt_json IS NOT NULL"
        ).fetchone()
        con.close()
        return bool(row and row[0] > 0)
    except Exception:
        return False


def run_attempt(task, arm, seed, attempt_dir, timeout_s=600):
    """Run one full attempt (phase A, plus phase B for recovery variant).

    timeout_s is per phase (600 s = the EVALUATION_PLAN §7 10-minute cap
    per task-seed-arm for a single phase; a recovery attempt can use two
    phases and is recorded as such)."""
    attempt_dir = Path(attempt_dir)
    workdir = attempt_dir / "work"
    meta_dir = attempt_dir / "meta"
    workdir.mkdir(parents=True, exist_ok=True)
    meta_dir.mkdir(parents=True, exist_ok=True)
    meta = seed_memory(task, arm, workdir)
    apply_change(task, arm, workdir, task.get("change"), meta)
    (meta_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    phases = []
    session_id = None
    timed_out = False
    nudges = 0

    def _nudge_phase(tag):
        """One resume turn when the deliverable is still missing. The nudge
        states only that the file is missing (verifiable by the agent
        itself); it never contains the expected value. Identical for all
        arms, so comparisons stay fair."""
        nonlocal session_id, timed_out, nudges
        prompt_n = (
            f"The deliverable file {task['deliverable']['file']} is still missing. "
            "You have not finished. Write it now with a tool call, verify it with `cat`, "
            "then end your turn with a one-line summary.\n"
            "You MUST do the work with tool calls (exec_command). Do not just reply in text.\n"
        )
        (meta_dir / f"prompt-{tag}.txt").write_text(prompt_n, encoding="utf-8")
        rn = _run_codex(workdir, prompt_n, session_id, timeout_s, meta_dir)
        (meta_dir / f"codex-{tag}.stdout.txt").write_text(rn["stdout"], encoding="utf-8")
        (meta_dir / f"codex-{tag}.stderr.txt").write_text(rn["stderr"], encoding="utf-8")
        timed_out = timed_out or rn["timed_out"]
        rollout_n = parse_rollout(rn["session_id"] or session_id)
        (meta_dir / f"rollout-{tag}.json").write_text(json.dumps(rollout_n, indent=2, ensure_ascii=False), encoding="utf-8")
        phases.append({"phase": tag, "session_id": rn["session_id"], "returncode": rn["returncode"],
                       "duration_ms": rn["duration_ms"], "tokens_used": rn["tokens_used"],
                       "timed_out": rn["timed_out"], "rollout": rollout_n,
                       "quota_error": rn.get("quota_error", False),
                       "auth_error": rn.get("auth_error", False),
                       "stderr_tail": rn["stderr"][-500:]})
        nudges += 1
        return read_deliverable(workdir, task)

    # Phase A
    prompt_a = build_prompt(task, arm, phase="A")
    (meta_dir / "prompt-A.txt").write_text(prompt_a, encoding="utf-8")
    r = _run_codex(workdir, prompt_a, None, timeout_s, meta_dir)
    (meta_dir / "codex-A.stdout.txt").write_text(r["stdout"], encoding="utf-8")
    (meta_dir / "codex-A.stderr.txt").write_text(r["stderr"], encoding="utf-8")
    session_id = r["session_id"]
    timed_out = timed_out or r["timed_out"]
    rollout_a = parse_rollout(session_id)
    (meta_dir / "rollout-A.json").write_text(json.dumps(rollout_a, indent=2, ensure_ascii=False), encoding="utf-8")
    phases.append({"phase": "A", "session_id": session_id, "returncode": r["returncode"],
                   "duration_ms": r["duration_ms"], "tokens_used": r["tokens_used"],
                   "timed_out": r["timed_out"], "rollout": rollout_a,
                   "quota_error": r.get("quota_error", False),
                   "auth_error": r.get("auth_error", False),
                   "stderr_tail": r["stderr"][-500:]})
    artifact_a = read_deliverable(workdir, task)
    if not artifact_a["exists"] and not timed_out:
        artifact_a = _nudge_phase("N1")

    # Phase B (recovery variant only)
    artifact_b = None
    if task.get("variant") == "recovery" and task.get("change2") and not timed_out:
        apply_change(task, arm, workdir, task["change2"], meta)
        (meta_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        prompt_b = build_prompt(task, arm, phase="B")
        (meta_dir / "prompt-B.txt").write_text(prompt_b, encoding="utf-8")
        r2 = _run_codex(workdir, prompt_b, session_id, timeout_s, meta_dir)
        (meta_dir / "codex-B.stdout.txt").write_text(r2["stdout"], encoding="utf-8")
        (meta_dir / "codex-B.stderr.txt").write_text(r2["stderr"], encoding="utf-8")
        timed_out = timed_out or r2["timed_out"]
        rollout_b = parse_rollout(r2["session_id"] or session_id)
        (meta_dir / "rollout-B.json").write_text(json.dumps(rollout_b, indent=2, ensure_ascii=False), encoding="utf-8")
        phases.append({"phase": "B", "session_id": r2["session_id"], "returncode": r2["returncode"],
                       "duration_ms": r2["duration_ms"], "tokens_used": r2["tokens_used"],
                       "timed_out": r2["timed_out"], "rollout": rollout_b,
                       "quota_error": r2.get("quota_error", False),
                       "auth_error": r2.get("auth_error", False),
                       "stderr_tail": r2["stderr"][-500:]})
        artifact_b = read_deliverable(workdir, task)
        if not artifact_b["exists"] and not timed_out:
            artifact_b = _nudge_phase("N2")

    final_artifact = artifact_b if artifact_b is not None else artifact_a
    # Memory-consulted + stale-finish signals from the raw rollouts.
    # Phase B resumes the SAME session, so its rollout file repeats phase A
    # items: dedupe tool calls by (name, arguments, timestamp).
    seen, tool_calls = set(), []
    for ph in phases:
        for tc in ph["rollout"].get("tool_calls", []):
            k = (tc.get("name"), tc.get("arguments", ""), tc.get("timestamp"))
            if k not in seen:
                seen.add(k)
                tool_calls.append(tc)
    args_text = "\n".join((tc.get("arguments", "") or "") for tc in tool_calls
                          if tc.get("name") != "__output__")
    # Memory access is judged from what the agent INVOKED (arguments only):
    # markdown -> read decisions.md; log -> read events.log;
    # turnkeep -> ran the ./turnkeep CLI. Outputs are not counted: the
    # agent's own file listing already contains these names.
    if arm == "markdown":
        memory_consulted = "decisions.md" in args_text
    elif arm == "log":
        memory_consulted = "events.log" in args_text
    elif arm == "turnkeep":
        memory_consulted = "turnkeep" in args_text
    else:
        memory_consulted = False
    stale_rejections = sum(ph["rollout"].get("stale_rejections", 0) for ph in phases)
    finish_receipt = any(ph["rollout"].get("finish_receipt") for ph in phases)
    if arm == "turnkeep":
        # The rollout text often misses the receipt (the agent captures the
        # finish JSON in a shell variable); the DB row is authoritative.
        finish_receipt = finish_receipt or turnkeep_receipt(workdir)
    total_tokens = max([ph["tokens_used"] for ph in phases if ph["tokens_used"]] or [None])
    total_ms = sum(ph["duration_ms"] for ph in phases)
    # Usage split merged across phases (max per component; the rollout
    # records are cumulative per session, so the max is the session total).
    _split_keys = ("input_tokens", "cached_input_tokens",
                   "cache_write_input_tokens", "output_tokens",
                   "reasoning_output_tokens")
    split_total = {k: 0 for k in _split_keys}
    for ph in phases:
        s = (ph["rollout"] or {}).get("usage_split") or {}
        for k in _split_keys:
            split_total[k] = max(split_total[k], s.get(k, 0))
    # Empty-answer artifact: a rollout exists but the agent made no tool
    # calls and sent no messages in ANY phase (the M365-outage signature
    # on the bridge route; on the official API it would mean an empty
    # completion). Flagged so contaminated attempts can be deleted, never
    # quoted as measurements.
    _real_calls = [tc for tc in tool_calls if tc.get("name") != "__output__"]
    empty_answer = (not _real_calls
                    and not any(ph["rollout"].get("agent_messages") for ph in phases)
                    and any(ph["rollout"].get("found") for ph in phases))
    last_msgs = [ph["rollout"].get("last_message") for ph in phases if ph["rollout"].get("last_message")]
    last_message = last_msgs[-1] if last_msgs else None
    ended = any(ph["rollout"].get("task_completed") for ph in phases)
    # If codex never started (no rollout at all), record the failure mode
    # explicitly instead of a silent zero-step trace.
    start_error = None
    if not any(ph["rollout"].get("found") for ph in phases):
        start_error = "codex did not start: no rollout file; stderr: " + \
            (phases[0].get("stderr_tail", "") if phases else "")
    trace = {
        "task_id": task["id"], "arm": arm, "seed": seed, "mode": "live",
        "steps": [{"tool": tc.get("name"), "arguments": (tc.get("arguments") or "")[:500]}
                  for tc in tool_calls
                  if tc.get("name") != "__output__"],
        "events": [],
        "claimed_completed": ended,
        "last_message": last_message,
        "nudges": nudges,
        "usage": ({"tokens_total": total_tokens, "turns": len(phases), **split_total}
                  if total_tokens else None),
        "latency_ms": total_ms,
        "timeout": timed_out,
        "error": start_error,
        "quota_error": any(ph.get("quota_error") for ph in phases),
        "auth_error": any(ph.get("auth_error") for ph in phases),
        "empty_answer": empty_answer,
        "session_id": session_id,
        "phases": [{"phase": ph["phase"], "duration_ms": ph["duration_ms"],
                    "tokens_used": ph["tokens_used"], "timed_out": ph["timed_out"],
                    "returncode": ph["returncode"]} for ph in phases],
        "memory_consulted": memory_consulted,
        "stale_finish_rejections": stale_rejections,
        "finish_receipt": finish_receipt,
        "deliverable": {"file": task["deliverable"]["file"], **final_artifact},
        "artifact_after_phase_A": artifact_a,
    }
    return trace
