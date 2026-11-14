# T026 substitute run — GAP: Codex backend returning empty answers (2026-11-09)

Date: 2026-11-09 ~18:33–18:41 GMT (ongoing at time of writing).
Run dir: `evidence/live-clients/run-20261109-01/` (shared DB `handover.sqlite3`,
shared scope `export-format`).

## What happened

- Bridge `/health`: ok:true, token valid (exp 19:07:35Z). The bridge is NOT down.
- The M365 backend behind the Copilot bridge returns **empty completions**
  (0 output tokens): `codex exec` turns end in ~5 s with rc=0, no tool calls,
  no assistant message. Three consecutive smoke turns for this run did this
  (18:33, 18:37, 18:38 GMT), plus the earlier probe turn at 18:24 DID get a
  real reply (tool list) — so the outage started between ~18:24 and ~18:33.
- The concurrent T042 Codex re-run campaign shows the same symptom: its most
  recent sessions (18:33:48–18:37:53 GMT) all have 0 tool calls / 0 messages,
  and it has launched no new Codex session since 18:37:53.
- Same signature as the 2026-11-09 ~00:30Z outage that contaminated the first
  T042 Codex pilot (empty answers from 00:30:57Z). Per Sam's guidance then,
  wait 30–60 min before retrying.

## What is NOT affected

- agy leg (gemini-3.8-flash-low) works normally throughout: smoke recall OK,
  S3 + S3c complete with 6 real MCP tool calls, finish receipt on record.
- The Codex leg needs no code change — only a live backend.

## Status of this run at the pause

- COMPLETE: agy smoke (MCP recall), S3 + S3c (reverse leg, agy half):
  decision `export.checklist.style` remembered, run begun, checklist written
  and hash-verified, candidate run finished with receipt, handoff record
  `export.handoff.2` written. All verified directly in the DB.
- NOT RUN: S1 (Codex first leg), S2 (agy resume of S1), S3b (Codex resume of
  the checklist), S4a/change/S4b (stale-finish round). All need a live Codex.
- No Codex attempt was recorded as a measurement: the empty turns wrote no
  Turnkeep records and are not counted anywhere.

## Findings banked while blocked

1. Adapter templates' `python3 -m turnkeep mcp` fails on this VM: system
   python3 has no `mcp` package (only the repo `.venv` does). Both clients
   now point at `.venv/bin/python`; `mcp_smoke.py` PASS (9 tools).
2. Codex CLI 0.164.0-alpha.9 `exec` mode does NOT surface MCP server tools
   to the model — proven with the turnkeep server AND a trivial one-tool
   dummy MCP server (both listed enabled; the model only ever sees built-in
   tools + MCP resource listing). So the Codex leg uses the Turnkeep CLI via
   the `./turnkeep` wrapper (same live pattern as the T042 drivers); the agy
   leg uses real MCP tool calls. Recorded in `scripts/run_t026_handover.py`.

## Resume precondition

A trivial `codex exec` call must return real text (a tool list, or a
one-line reply) before re-running S1. Then: `--from S1` runs
S1, S2, S3b, S4a, change, S4b in order.

## Retries

- ~18:52 GMT (4th empty Codex turn, ~6 s, 0 calls, 0 messages): still empty.
  Stopped retrying; resume after the backend recovers (Sam's earlier guidance
  for this pattern: wait 30–60 min).
