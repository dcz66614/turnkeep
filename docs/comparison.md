# Comparison — nearest neighbours (T044)

Checked 2026-11-08. Sources: the kit research ledger (98 records,
2026-11-08) and a supplementary web check the same day; the kit's
competition note (`docs/COMPETITION_AND_DISTRIBUTION.md` in the
reference kit) and `docs/COMPETITORS.md` in this repo. Reading status
per neighbour is in `docs/discovery-scorecard.json`.

**How to read this page.** "Strong at" means the neighbour's own
documentation describes it — not that Turnkeep re-ran it. "Difference
still to verify" means the thing Turnkeep would have to *demonstrate*,
not assert. No neighbour is claimed to be fully reproduced here, and
no defect is claimed for any neighbour that its own docs do not
state. Star counts are not used to rank anything here.

## What Turnkeep does not claim

- **First cross-tool shared memory:** no. Turnbook, Truenex Memory
  and project-memory already share memory across tools.
- **First versioned memory / MVCC / CAS / evidence chains / undo:**
  no. Prior art exists (see below and `docs/COMPETITORS.md`).
- **Proven better than Markdown + logs:** not yet. That is the
  hypothesis the frozen Gate C evaluation (T040–T042) is supposed to
  test — and the live part of that test has **not run** (T042
  NOT_RUN). The deterministic smoke numbers (12 scripted attempts)
  are simulations, not model results.
- **High GitHub stars / retention / acquisition value:** nothing of
  the kind is established or claimed.

## Nearest neighbours

| Neighbour | Strong at (their docs) | Difference still to verify for Turnkeep |
|---|---|---|
| [Turnbook](https://pypi.org/project/turnbook/0.9.46/) | "One shared brain for every agent": Claude Code + Codex sharing one memory; LongMemEval 88.8% (all 500, self-reported, **not** reproduced here); verbatim recall; 0 model tokens to save (their claim) | Turnkeep's bet is change-recovery (taint, atomic finish, rebase, impact explanation), not verbatim recall. The LongMemEval figure is Turnbook's self-report; Turnkeep must not present its own unrun eval as comparable. |
| [Truenex Memory](https://pypi.org/project/truenex-memory/0.2.1/) | Local-first memory layer for Codex / Claude Code / Cursor / other MCP-aware agents; `truenex-mem` CLI; open-source core | Overlaps Turnkeep's MCP + local-first surface directly. Turnkeep must show a handover that Truenex's layer does not already handle, not re-describe the overlap as a difference. |
| [project-memory](https://github.com/haraldalder-vibemogger/project-memory) (haraldalder-vibemogger) | File-based `.memory/` Markdown for Claude Code / Cowork; hooks guarantee read/write; verified live in Cowork 2026-09-18/19 per its README | File-based handover already works there. Turnkeep's database-authoritative model must prove concrete concurrency / recovery gains over the file approach — it cannot disparage the file approach to win. |
| [localrivet/projectmemory](https://github.com/localrivet/projectmemory) (Go) | MCP server, SQLite, `save_context` / `retrieve_context`, semantic search; 2 tools only | Small, focused surface. Turnkeep's larger surface (9 tools, snapshots, taint) must justify its complexity against this minimal baseline. |
| [Hy-Memory / MemHub](https://github.com/Tencent-Hunyuan/Hy-Memory) | Three memory classes, work source chains, cross-coding-tool use, local service with permission scoping | MemHub's docs at check time did not support Windows; the comparison to run is a real cross-platform recovery journey, not another unified store. |
| [TencentDB-Agent-Memory](https://github.com/TencentCloud/TencentDB-Agent-Memory) | Team memory assets in four classes, ACL, versions, sources, agent access | Turnkeep's first release is deliberately narrower (single-machine task handover); it does not compete on team-platform feature count. |
| [Mem0](https://github.com/mem0ai/mem0) | SDK, user/session scoping, extraction, entities, hybrid retrieval | The measurement target is recovery after a requirement change — a hosted optimisation score does not stand in for the local open-source build. |
| [Graphiti](https://github.com/getzep/graphiti) | Bitemporal data, source episodes, incremental graph, invalidation relations | Graph capability is reusable; the fact that its reference MCP has no built-in ACL must not be stretched into "graph memory has no governance". |
| [Hindsight](https://github.com/vectorize-io/hindsight) | Facts / experience / observations, evidence, temporal retrieval, knowledge pages, many hosts | Strong composite baseline: any comparison must count curation and model-processing cost together, not compare against a default empty configuration. |
| [Engram](https://github.com/Gentleman-Programming/engram) | Single binary, FTS, multiple hosts, doctor, structured handoff | Local simplicity and handoff already exist here; compare first handover and change explanation, not feature lists. |
| [Basic Memory](https://github.com/basicmachines-co/basic-memory) | Editable Markdown, portability, MCP, relations, briefs | A database-authoritative source must earn its place with provable concurrency / recovery gains; it cannot win by disparaging files. |
| [Tigerless Agent Memory](https://github.com/tigerless-labs/agent-memory) | Muse Code connected, shared directory, raw traces, sources, expiry, background curation | Direct baseline for the Muse scenario. "No extra key" does not mean the host's inference is free. |
| [Jamjet Engram](https://github.com/jamjet-labs/engram) | revoke / supersede, `derived_from` transitive hiding, tombstone, erase | These mechanisms are **not** Turnkeep originals; any difference must show up in work-impact explanation and handover experience. |
| [agent-coherence](https://github.com/cohexa-ai/agent-coherence) | CAS, fencing, consistent-read cut, directed handover, offline replay | Scope-epoch rejection may be easier to use — or may over-refuse. False refusals and bypass coverage must be reported, not hidden (the replay harness reports `false_refusals` separately for this reason). |
| [Relay](https://relaymemory.com/) | Logs + current facts, supersede, MCP, shared handover | Highly overlapping. Its website is not an implementation audit, and its BSL self-hosting is not OSI open source — neither fact is a defect. |
| [Kalairos](https://github.com/LabsKrishna/kalairos) | Bitemporal data, source categories, contradiction isolation, audit | Source and dispute state alone are not a unique selling point; compare end-to-end reliability. |
| PlanFence (arXiv 2609.03340v2) | Fresh-before-generation, rebase, mis-block measurement — closest on change-recovery semantics | Research, not a shipped product: the comparison is semantic (what "rebase" and "mis-block" mean), not a feature race. |

## Shrink condition (recorded, not hypothetical)

If a fair trial under the frozen protocol shows no benefit vs plain
Markdown + original logs on task success / stale-fact use / rework
cost — or if a neighbour plus a thin adapter already completes the
same journey — Turnkeep shrinks to an adapter layer / workbench over
the better neighbour instead of copying its core. That outcome is a
deliverable, not a failure to hide. See `docs/COMPETITORS.md` and
`docs/DISCOVERY_DECISION.md`.
