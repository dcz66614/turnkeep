# Compatibility (T026)

Real-host evidence only. SDK test processes are protocol evidence, never a
substitute for a host product (backlog T026 invariant).

## Clients

| Client | Config | Live evidence | Status |
|---|---|---|---|
| Claude Code (`claude-code`) | `adapters/claude-code/.mcp.json` — config PASS (`check_adapter.py`) | none | **NOT_RUN** — `claude` CLI not installed on this VM; no Claude login available here |
| Codex (`codex`) | `adapters/codex/config.toml` — config PASS (`check_adapter.py`) | `codex mcp list` runs without auth: "No MCP servers configured yet" | **NOT_RUN** — codex 0.164.0-alpha.9 installed, login/auth state unknown, no login attempted; template not installed into `~/.codex/config.toml` |
| Turnkeep CLI | `src/turnkeep/cli.py` | J01 journey PASS (`run_journey.py --mode cli --journey J01`) | verified |
| Turnkeep Python SDK | `src/turnkeep/client.py` | `examples/python/handoff.py --temp-db` PASS; SDK/CLI same view | verified |
| MCP protocol (SDK client) | `src/turnkeep/mcp_server.py` | 12/12 pytest via official SDK client + `mcp_smoke.py` PASS | verified (protocol only) |

## Live handover (J02)

- Runner: `python scripts/live_client_handoff.py --pair claude-code,codex --fixture export-format --record evidence/live-clients`
- Fixture: `fixtures/live-handoff/export-format.json`
- Record: `evidence/live-clients/live_client_gate.json` — currently
  **NOT_RUN**: Claude Code CLI absent; Codex turnkeep entry not installed
  and no auth state on file.
- Manual recipe (when both prerequisites are met):
  1. Install both adapters (see `docs/integrations/claude-code.md` and
     `docs/integrations/codex.md`) pointing at the same `--db`.
  2. Client A: fresh session, remember the export-format requirement,
     begin+finish, write a brief with evidence record ids and the next step.
  3. Close A. Client B: fresh session, no context copied by hand — recall
     + brief + resume, verify the manifest, finish.
  4. Reverse the direction; then have one side change the requirement so
     the other side's old finish is rejected (STALE_SNAPSHOT), re-reads,
     and fixes.
  5. Save each client's real tool-call log + client version in
     `evidence/live-clients/` and re-run the gate.

## Platforms (T043)

Record: `evidence/platforms/current.json` (`scripts/platform_gate.py`).

| Platform | Status | Evidence |
|---|---|---|
| Linux (this VM: Linux 7.0.0-43-generic, 4 CPUs, Python 3.12.3 / SQLite 3.45.1) | verified | doctor PASS, J01 + J05 journeys PASS, spaces + Chinese path init/remember PASS, UI production gate PASS |
| macOS | **NOT_RUN** | No macOS runner on this VM. CI workflow exists (`.github/workflows/test.yml`) — a workflow file is not evidence of a run. |
| Windows | **NOT_RUN** | No Windows runner on this VM. Same CI note as macOS. |

Only platforms with a passing record are on the support list. Linux
containers + path mocks are never counted as Windows/macOS evidence.

## Performance (T043) — measured, not promised

Full report: `evidence/platforms/perf.json` (`benchmarks/harness/perf.py`,
dataset `fixtures/perf/manifest.json`, pre-registered thresholds).
Hardware: Linux 7.0.0-43-generic, 4 CPUs, 7.9 GB RAM, Python 3.12.3 /
SQLite 3.45.1. Numbers are experiment points on this machine.

| Dataset | Recall p50 / p95 | Finish p50 / p95 | DB size |
|---|---|---|---|
| 1k records | 25 ms / 76 ms | 218 ms / 442 ms | 4.2 MB |
| 10k records | 251 ms / 350 ms | 1,924 ms / **7,760 ms** | 41 MB |
| 100k records | 2,917 ms / **3,183 ms** | 12,275 ms / **12,288 ms** | 150 MB |

Cold start 159 ms; 8 concurrent writers → 1 winner, 7 VERSION_CONFLICT,
0 busy errors.

**Gate status: FAIL (3 thresholds).** Causes, both architectural and
correctness-first by design:
1. `begin()` snapshots **all** active records into `snapshot_json` (O(n)
   per run) — at 10k records that is already seconds per finish.
2. `recall()` loads the whole `memories` table and filters in Python
   (O(n) per query) — ~3 s per query at 100k records.
Fixing this needs a scoped follow-up (snapshot only the read set;
indexed recall). It is recorded as a known limitation, not hidden by
lowering thresholds or shrinking the dataset.
