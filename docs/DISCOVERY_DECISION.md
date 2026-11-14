# Discovery decision (T002) — GO_WITH_BOUNDS

Date: 2026-11-08. Inputs: kit research (98 records), starter re-run (T001),
supplementary web check 2026-11-08 (see docs/COMPETITORS.md, docs/NAME_CHECK.md).

## Decision
GO_WITH_BOUNDS. Build Turnkeep as specified in CORE_CONTRACT v0.1 — do NOT
re-scope to copy a neighbour's core, do NOT claim first/best/comprehensive.

## Why (evidence, not hype)
- The retained bet is narrow and testable: change-recovery handover
  (conflict → taint → stale-snapshot rejection → rebase → new receipt) +
  low-threshold local experience, in one product. PlanFence covers the
  semantics in research; Turnbook/Truenex cover cross-tool sharing; none in
  the ledger combines all of: explicit-key conflicts, AND-parent taint,
  server-side snapshot + atomic finish + idempotent receipt, impact
  explanation, and a replayable fair evaluation — as one local-first product.
  That combination is a hypothesis to be tested (T040–T042), not a fact.
- Starter already proves the core semantics locally (25/25 on this VM).
- Name: PyPI/npm free, GitHub user free, BUT turnkeep.app is a live rental
  product with the same name. Working name only; revisit before publication.

## Bounds
1. No "first / best / only" claims anywhere (README, release notes, UI).
2. No GitHub stars / retention / acquisition claims — NOT_ESTABLISHED.
3. If T042 fair trial shows no benefit vs Markdown+logs baseline, shrink to
   adapter/workbench over the better neighbour (see COMPETITORS.md).
4. Name: re-check before any public candidate; Sam decides on any rename.
5. Research budget for this task: web check only, no further ideation rounds.
