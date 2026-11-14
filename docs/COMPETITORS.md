# Competitors / nearest neighbours (T002)

Date checked: 2026-11-08. Sources: kit research ledger (98 records, 2026-11-08) +
supplementary web check by Muse on 2026-11-08 (search results only; no full-text
re-read of every neighbour — see scorecard for reading status).
Sources checked 2026-11-08: https://pypi.org/project/turnbook/0.9.46/ ,
https://pypi.org/project/truenex-memory/0.2.1/ , https://github.com/haraldalder-vibemogger/project-memory ,
https://github.com/localrivet/projectmemory , https://turnkeep.app/ (see docs/NAME_CHECK.md).

Claim discipline: "documented" = in kit ledger with URL+date; "code-verified" =
starter code/tests re-run on this VM; "run" = actually executed here;
"unknown" = not checked. No neighbour is claimed to be fully reproduced.

## Kit ledger neighbours (documented, 2026-11-08)
- PlanFence (arXiv 2609.03340v2, full read) — fresh-before-generation, rebase,
  mis-block measurement. Closest on change-recovery semantics.
- Engram, Hy-Memory, agent-coherence, Handover — long-term / handover memory
  mechanisms. See research/github_landscape.md (34 repos, 21 full READMEs) and
  research/papers.md (P01–P23: full-text 5, extracted 10, targeted 3, abstract 5).
- project-memory (haraldalder-vibemogger, GitHub) — file-based `.memory/` Markdown
  for Claude Code / Cowork, hooks guarantee read/write. Verified live in Cowork
  2026-09-18/19 per its README; release 0.2.1 itself not run live there.
- localrivet/projectmemory (Go) — MCP server, SQLite, save_context /
  retrieve_context, semantic search. 2 tools only.

## Supplementary neighbours found 2026-11-08 (search-result level)
- Turnbook (PyPI turnbook 0.9.46, 2026-10-27) — "One shared brain for every
  agent": Claude Code + Codex sharing one memory, LongMemEval 88.8% (all 500,
  self-reported), 0 model tokens to save, local PC. This is the strongest
  cross-tool sharing claim found; Turnkeep must NOT claim to be first at
  cross-tool shared memory. Turnbook's differentiator is verbatim recall +
  sharing; Turnkeep's is change-recovery (taint, atomic finish, rebase, impact
  explanation). LongMemEval figure is Turnbook's self-report, NOT independently
  reproduced here.
- Truenex Memory (PyPI truenex-memory 0.2.1, public alpha) — local-first memory
  layer for Codex / Claude Code / Cursor / other MCP-aware agents, `truenex-mem`
  CLI, open-source core. Overlaps Turnkeep's MCP + local-first surface.
- Octopus Project Memory — organisational causal memory ("why, not what"),
  signed events/0 bundles, Ed25519. Different trust model (signed facts).

## What is NOT first / NOT claimed
- Versioned memory, MVCC, CAS, evidence chains, undo: prior art exists.
- Cross-tool shared memory: Turnbook / Truenex / project-memory already do it.
- Turnkeep's retained, still-to-prove bet (per 08_RESEARCH_SYNTHESIS.md): daily
  handover + change-impact explanation + recovery operations + replayable
  evaluation as one low-threshold local experience. That combination is the
  claim to test in T040–T042 — it is a hypothesis, not a proven lead.

## Shrink conditions (from kit)
If a fair trial (T042, frozen protocol) shows no benefit vs plain Markdown +
original logs on task success / stale-fact use / rework cost, shrink to an
adapter layer / workbench over the better neighbour instead of copying its core.
