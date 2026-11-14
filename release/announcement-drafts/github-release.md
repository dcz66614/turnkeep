# DRAFT — DO NOT PUBLISH. Local draft only (T044).

This file is a draft for a future GitHub release. It has not been
posted, and nothing in this repository posts on your behalf.
Publishing requires explicit authorisation, a re-check of the name
("Turnkeep" is a working name only — see below), and the release
checks (T045: licence / SBOM / secrets) to be complete.

---

# Turnkeep 0.1.0 — OFFLINE_RC (Linux offline release candidate)

**What it is:** local-first multi-agent project working memory +
change recovery. One agent leaves a task, decisions and evidence; the
next agent picks it up; when a requirement changes mid-flight,
Turnkeep says which finished work is now stale and organises a
controlled redo — instead of silently overwriting or silently reusing
an old result.

**Try it in 60 seconds (no key, no network, no model):**

```sh
bash scripts/install.sh
.venv/bin/python -m turnkeep demo --scenario export-format --no-model
```

**What is verified in this release:**

- No silent overwrite: same key + different value becomes an explicit
  conflict; CAS correction, retract, full-candidate resolve.
- Change recovery: AND-parent taint, `STALE_SNAPSHOT` rejection of
  old finishes, resume diff, rebase (max 2 auto attempts, then
  quarantine).
- Atomic finish + idempotent receipt (receipt = local SQLite commit
  only). 225/225 pytest at the R4 close (2026-11-08).
- Export / import v1, Markdown import (structured items only),
  expiry, erasure with residual report, backup / restore / diagnose.
- CLI, Python SDK, 9 MCP tools, loopback workbench + browser UI.
- Zero-key scripted demo with chained-digest replay files.

**What is NOT in this release (please read before evaluating):**

- **Real two-client handover: NOT_RUN.** No live Claude Code ↔ Codex
  handover has run. Adapter templates are config-only.
- **Live-model evaluation: NOT_RUN.** No model selected, no budget,
  no endpoint. The demo and smoke numbers are scripted simulations.
- **Windows / macOS: NOT_RUN.** Installers exist; no run on those
  platforms.
- **Performance gate: FAIL.** Finish p95 7,760 ms at 10k records and
  12,288 ms at 100k; recall p95 3,183 ms at 100k (thresholds 500 ms /
  1,000 ms). Cold start 159 ms passes. See `docs/KNOWN_LIMITATIONS.md`.
- **External-action atomicity: NOT_SUPPORTED.** A receipt covers the
  local SQLite commit only.
- **Name: working name only.** `turnkeep.app` is a live, same-spelled
  rental product in a different market. Trademark clearance has not run.

**Scope:** single trusted OS user, Linux offline. Python ≥ 3.11.
Licence: MIT.

Full honest list: `docs/KNOWN_LIMITATIONS.md`. Nearest neighbours and
what Turnkeep does not claim: `docs/comparison.md`.
