# Turnkeep (working name)

**Local-first multi-agent project working memory + change recovery.**
Pick up the work. See what changed.

> **Status: OFFLINE_RC — Linux offline release candidate, version 0.1.0
> (contract v0.1).** This is a local-first SQLite + Python product for one
> trusted OS user. It is **not** a cross-platform finished product: see
> [Known limitations](#known-limitations) and
> [docs/KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md) before relying on
> it for anything else.

Turnkeep is the shared memory for agents working on the same project in
turns: one agent leaves a task, decisions and evidence; the next agent
picks it up; when a requirement changes mid-flight, Turnkeep says which
finished work is now stale and organises a controlled redo — instead of
silently overwriting, silently reusing an old result, or losing the
change entirely.

- **[verified]** No silent overwrite: same key + different value becomes
  an explicit conflict; both sides leave the default view until resolved.
- **[verified]** Change recovery: a requirement change taints everything
  that depended on it (AND parents); a finish based on an old snapshot is
  rejected with `STALE_SNAPSHOT`; `resume` shows the diff and `rebase`
  recomputes (max 2 automatic attempts, then the candidate is quarantined).
- **[verified]** Atomic finish + idempotent receipt: a run commits once;
  a retry with the same idempotency key returns the original receipt.
  The receipt proves only that the local SQLite database accepted the
  commit (`guarantee: "local_sqlite_commit_only"`).
- **[verified]** Handover briefs with record IDs, sources and budget
  limits; secrets are redacted from briefs.
- **[verified]** Export / import (v1 JSONL + manifest), Markdown import
  (structured items only), expiry, erasure with residual report, and
  online backup / restore / diagnosis.
- **[experimental]** Local HTTP workbench (`turnkeep serve`, loopback only,
  per-process session token) and a browser UI (see `docs/UX_SPEC.md`).
- **[not run]** Real two-client handover (T026) and live-model evaluation
  (T042) have not run. Windows / macOS install has not run.

## 60-second demo — no key, no network, no model

```sh
python -m turnkeep demo --scenario export-format --no-model
python -m turnkeep demo --scenario research-correction --no-model
```

Both demos run against a temporary local SQLite database with fixed
fixtures. Every demo output is labelled
`SCRIPTED_SIMULATION_NO_MODEL_CALLS`: it shows local SQLite correctness
only — not MCP integration, not model quality, not external-action safety.
See [docs/demo.md](docs/demo.md). A longer, two-agent SDK handover:

```sh
python examples/python/handoff.py --temp-db
```

## Install (Linux verified; macOS / Windows not run)

Requires Python ≥ 3.11. The core is stdlib-only (SQLite); no model API
key, no database service, no Docker.

```sh
bash scripts/install.sh
.venv/bin/python -m turnkeep doctor --json   # diagnostics
.venv/bin/python -m turnkeep demo --no-model
```

`install.sh` creates a `.venv` in the checkout (set
`TURNKEEP_WITH_MCP=1` to also install the MCP extra). It needs no admin
rights and changes no system Python. Data lives in
`~/.local/share/turnkeep` on Linux (override with `TURNKEEP_DATA_DIR`).
Full install, start/stop and uninstall instructions:
[docs/install.md](docs/install.md). Quickstart:
[docs/quickstart.md](docs/quickstart.md).

Windows (PowerShell) and macOS installers exist
(`scripts/install.ps1`, `bash scripts/install.sh`) but are **not run**:
no Windows/macOS runner has executed them. Do not treat them as
supported until a real run is recorded in `docs/COMPATIBILITY.md`.

## The handover loop (CLI)

```sh
# One project = one scope. Create it, then remember a requirement.
python -m turnkeep --db /tmp/demo.sqlite3 init --scope demo
python -m turnkeep --db /tmp/demo.sqlite3 --json remember --scope demo \
  --key build.python_version --value 3.11 --kind requirement

# Agent A begins a run (server-side snapshot of the scope at this epoch).
python -m turnkeep --db /tmp/demo.sqlite3 --json begin --scope demo \
  --task "prepare release"
# → {"run_id": "…", "read_epoch": 1, "records": […], "status": "open"}

# Agent B corrects the requirement (CAS: needs the current revision).
python -m turnkeep --db /tmp/demo.sqlite3 --json correct --scope demo \
  --id <record-id> --expected-revision 1 --value 3.12

# Agent A's finish on the old snapshot is rejected, not silently applied.
python -m turnkeep --db /tmp/demo.sqlite3 --json finish --scope demo \
  --run <run-id> --key release --value "Python 3.11"
# → exit 2, {"error": {"code": "STALE_SNAPSHOT",
#      "details": {"changed_keys": ["build.python_version"], …}}}

# Agent A resumes, reads the diff, recomputes, and finishes.
python -m turnkeep --db /tmp/demo.sqlite3 --json resume --scope demo --run <run-id>
python -m turnkeep --db /tmp/demo.sqlite3 --json begin --scope demo \
  --task "recompute release"
python -m turnkeep --db /tmp/demo.sqlite3 --json finish --scope demo \
  --run <new-run-id> --key release --value "Python 3.12" \
  --idempotency-key demo-1
# → receipt: {"record_id": "…", "commit_epoch": 3,
#             "guarantee": "local_sqlite_commit_only"}
```

Every command above is executed for real by
`scripts/test_documented_commands.py --offline` on a clean temporary
fixture — a failing documented command fails the test:

```sh
.venv/bin/python scripts/test_documented_commands.py --offline
.venv/bin/python scripts/check_docs.py --profile release
```

More journeys (J01–J05), recall/brief, conflicts, impact and recovery:
[docs/quickstart.md](docs/quickstart.md),
[docs/UX_SPEC.md](docs/UX_SPEC.md).

## MCP clients and the workbench

Turnkeep exposes 9 MCP tools (`begin`, `recall`, `remember`, `correct`,
`retract`, `resolve_conflict`, `resume`, `finish`, `brief`) over stdio:

```sh
python -m turnkeep mcp --scope demo --db /path/to/turnkeep.sqlite3
```

Adapter templates: [docs/integrations/claude-code.md](docs/integrations/claude-code.md),
[docs/integrations/codex.md](docs/integrations/codex.md).
**Config only, live NOT_RUN**: the templates parse and are checked by
`scripts/check_adapter.py --mode config`, but no real Claude Code /
Codex session has run a handover through them (T026). Two SDK test
processes are protocol evidence, never a substitute for two host
products.

Local workbench API (loopback only, per-process session token):

```sh
python -m turnkeep serve --scope demo   # prints {"url": "http://127.0.0.1:…", "token": "…"}
```

Every request needs the token (`Authorization: Bearer …` or
`X-Turnkeep-Token`). `GET /api/v1/health` returns
`{"status": "ok", "scope": …, "epoch": …}`. OpenAPI snapshot:
`specs/local-app-api.json`.

## Data: export, import, erase, back up

```sh
# Export a scope (JSONL + manifest with sha256 per file).
python -m turnkeep --db /tmp/demo.sqlite3 export --scope demo --out /tmp/demo-export

# Dry-run first, then import into a fresh database.
python -m turnkeep --db /tmp/fresh.sqlite3 --json import --format turnkeep --dry-run /tmp/demo-export
python -m turnkeep --db /tmp/fresh.sqlite3 --json import --format turnkeep /tmp/demo-export

# Markdown import: structured items only, plain prose is never upgraded.
python -m turnkeep import --format markdown --dry-run notes.md
```

Details: [docs/migration.md](docs/migration.md) (Markdown / legacy
imports), [docs/recovery.md](docs/recovery.md) (backup / restore /
crash diagnosis), [docs/retention.md](docs/retention.md) (retract vs
expire vs erase — erase replaces the payload with a tombstone and
reports uncontrolled residuals such as backups and exports).

Rollback: there is no installer rollback — nothing is installed
system-wide. To roll back data, restore a backup taken before the
change (`docs/recovery.md`); restoring a pre-erasure backup resurrects
the erased payload, which is expected point-in-time behaviour. To
uninstall, delete the checkout and, if you want the data gone too,
delete the data directory (`~/.local/share/turnkeep` on Linux).

## What each capability is

| Capability | Status | Evidence |
|---|---|---|
| Core: remember / conflicts / CAS correct / retract / resolve | [verified] | 110 core tests at R1 close; `run_gate --profile core` PASS |
| Snapshot begin / atomic finish / resume / rebase | [verified] | incl. 2-process race: 1 winner; stale finish rejected |
| Lexical recall, budgets, briefs | [verified] | scope-first filtering; chars, not tokens, are counted |
| CLI, Python SDK, MCP (9 tools, official SDK) | [verified] | protocol only for MCP: 12/12 SDK-client tests + `mcp_smoke.py` PASS |
| Local HTTP workbench + browser UI | [verified] | 11/11 e2e on dev and production builds |
| Zero-key scripted demo + replay | [verified] | 8 + 7 chained-digest events; tamper breaks the chain |
| Export / import v1, Markdown import | [verified] | deterministic re-import is a no-op; tampered manifest rejected |
| Expiry / erasure / backup / restore / diagnose | [verified] | erasure verified by scanning every text column |
| Deterministic eval harness (Gate C smoke) | [verified] | 12 scripted attempts: none 1/3, markdown 3/3, log 3/3, turnkeep 3/3 |
| Change replay, 25 frozen scenarios | [verified] | real core 25/25; mutations no-taint 21/25, silent-overwrite 22/25 |
| Real two-client handover (Claude Code ↔ Codex) | [not run] | T026 BLOCKED_EXTERNAL — see below |
| Live-model evaluation (Gate C) | [not run] | T042 BLOCKED_EXTERNAL — see below |
| Windows / macOS install + journeys | [not run] | T043 — see below |
| Performance at 10k / 100k records | [verified — FAIL] | Gate FAIL, 3 thresholds — see below |

## Known limitations

Read the full list in [docs/KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md).
The ones that change what you should expect:

- **Real two-client handover — NOT_RUN (T026).** The `claude` CLI is
  not installed here and no Codex turnkeep entry is installed; no live
  handover between two real host products has run. Do not claim
  cross-tool handover is proven.
- **Live-model evaluation — NOT_RUN (T042).** No model selected, no
  authorised budget ($0), no endpoint configured. The deterministic
  smoke numbers above are scripted simulations, not model results.
- **Windows / macOS — NOT_RUN (T043).** Installers and CI workflow
  exist; no run has executed on those platforms.
- **Performance gate — FAIL (T043).** Measured on Linux
  7.0.0-43-generic, 4 CPUs, 7.9 GB RAM, Python 3.12.3 / SQLite 3.45.1:
  finish p95 **7,760 ms at 10k records** and **12,288 ms at 100k**,
  recall p95 **3,183 ms at 100k** (thresholds: finish 500 ms, recall
  1,000 ms). Cold start 159 ms passes; finish p95 442 ms and recall
  p95 76 ms at 1k records pass. Causes are architectural
  (`begin()` snapshots all active records; `recall()` scans the whole
  table in Python). Thresholds were not lowered and datasets were not
  shrunk to pass. Do not use this release for large scopes expecting
  interactive latency.
- **External-action atomicity — NOT_SUPPORTED.** A receipt covers the
  local SQLite commit only. It does not make an email, a deployment or
  a transaction atomic.
- **Name / trademark — NOT_RUN.** "Turnkeep" is a working name only.
  PyPI / npm / GitHub-user checks returned 404 on 2026-11-08, but
  [turnkeep.app](https://turnkeep.app/) is a live, same-spelled rental
  turnover product in a different market. No trademark clearance has
  run; do not buy domains or file trademarks on this name without a
  re-check and a decision.
- **Single trusted OS user.** Scope is a correctness boundary, not a
  security boundary against another local user who can read the
  database file. No multi-tenancy, no auth beyond the loopback
  workbench token.

## Nearest neighbours

Turnkeep is not the first shared memory for agents, and does not claim
to be. What it adds is the combination — explicit-key conflicts,
AND-parent taint, server-side snapshot + atomic finish + idempotent
receipt, impact explanation and replayable evaluation — as one
local-first product, and that combination is a hypothesis still to be
proven by a live trial (T042), not a proven lead. Details, strengths
and the differences still to verify:
[docs/comparison.md](docs/comparison.md),
[docs/COMPETITORS.md](docs/COMPETITORS.md).

## Docs

- [docs/quickstart.md](docs/quickstart.md) — install → first handover
- [docs/demo.md](docs/demo.md) — scripted demos and replay files
- [docs/install.md](docs/install.md) — install / doctor / uninstall
- [docs/migration.md](docs/migration.md) — Markdown and export imports
- [docs/recovery.md](docs/recovery.md) — backup / restore / diagnosis
- [docs/retention.md](docs/retention.md) — retract / expire / erase
- [docs/troubleshooting.md](docs/troubleshooting.md) — doctor output,
  common failures, diagnostics export
- [docs/KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md) — the honest list
- [docs/comparison.md](docs/comparison.md) — nearest neighbours
- [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md) — client / platform /
  performance matrix with evidence
- [docs/EVALUATION_RESULTS.md](docs/EVALUATION_RESULTS.md) — Gate C status
- [specs/CORE_CONTRACT.md](specs/CORE_CONTRACT.md) — the frozen contract (v0.1)

## Contributing and support

- License: MIT — see [LICENSE](LICENSE). (Dependency / SBOM / security
  boundary checks are tracked separately; see `SECURITY.md` when it
  lands in the release checks.)
- Issues: when reporting, include `turnkeep doctor --json` output and
  the exact command that failed. Do not paste real user data, tokens or
  internal logs. Draft replies to common questions live in
  `release/announcement-drafts/issue-replies.md` — drafts only, not
  posted anywhere.
- Release drafts (GitHub release, Hacker News, forum post) are local
  files under `release/announcement-drafts/` and are **not published**
  by this repository. Nothing here posts, messages or uploads on your
  behalf.

## Version

0.1.0 — contract v0.1, OFFLINE_RC scope (Linux offline). Python ≥ 3.11,
SQLite 3.45.1 on the verification machine (kit report said 3.12.14 /
3.53.1; this VM is 3.12.3 / 3.45.1 — tests still pass). Test suite:
225/225 pytest at the R4 close (2026-11-08).
