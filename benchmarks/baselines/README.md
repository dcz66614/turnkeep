# Baseline arms (T040)

Implemented arms live in `benchmarks/harness/arms/` (scripted policies,
deterministic mode only). This directory holds their frozen descriptions.

| Arm | Module | Policy |
| --- | --- | --- |
| B0 none | `arms/none.py` | Prompt only; changes after the prompt are invisible |
| B1 markdown | `arms/markdown.py` | Dated lines in `decisions.md`; grep, latest entry wins; RETRACTED means gone |
| B2 log | `arms/log.py` | Append-only `events.log`; grep, latest entry wins |
| T turnkeep | `arms/turnkeep.py` | Real SDK: remember → begin → change → finish (stale rejected) → resume → recompute → finish |
| B3 mature-memory | — | NOT_RUN: no real Mem0/Hindsight install configured |
| B4 nearest-workflow | — | NOT_RUN: no real Tigerless/Engram install configured |

All arms share the same budget (`max_steps` 20, `max_seconds` 60 in smoke)
and the same seed. The Markdown and log arms keep their normal search and
update ability; they are not crippled to make Turnkeep look good.
