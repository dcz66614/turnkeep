# Evaluation protocol (T040) — pre-registered, not results

Source: `docs/EVALUATION_PLAN.md` (Gate C). This file freezes the harness
rules. Changing a task, the scorer, a budget or a threshold requires a new
suite version; old results are kept, never overwritten.

## Arms

| Arm | What it is | Fairness constraint |
| --- | --- | --- |
| B0 `none` | No persistent memory; only the task prompt | Still has the current task source; not deprived of normal tools |
| B1 `markdown` | Structured `decisions.md`, dated lines, grep latest | Keeps normal search AND update ability; never crippled |
| B2 `log` | Append-only `events.log`, grep latest | Same lexical retrieval budget as B1; no hidden oracle |
| T `turnkeep` | The real product via the SDK | Metadata only from observable inputs; no oracle parents |
| B3 mature-memory (Mem0/Hindsight) | **NOT_RUN** | No real install configured here; a missing arm is never scored as a competitor failure |
| B4 nearest-workflow (Tigerless/Engram) | **NOT_RUN** | Same as B3 |

## Fairness

- Every arm gets the same task, the same initial facts, the same budget
  (`max_steps` 20, `max_seconds` 60 in the smoke suite), the same seed.
- Failures and timeouts stay in the denominator. A crashing arm is a
  failed attempt, never a pass.
- The scorer reads raw traces. It computes `completed` itself; a trace
  that claims success while using a stale fact scores `completed=False`.
  A corrupt trace (missing field, wrong mode) fails scoring outright.
- Metrics (pre-registered): task success rate (primary), stale-fact use
  rate, contract violations, irrelevant-refusal rate, redo cost (steps
  beyond minimal), latency, tokens (or `"unknown"` when no usage exists).
- Two comparison lines stay separate: equal-budget and recommended-default.
  This runner only implements the equal-budget line.

## Modes

- `deterministic`: scripted policies only, **no model calls**. Results are
  labelled `SCRIPTED_SIMULATION_NO_MODEL_CALLS` and measure the harness
  and the policies — never real model performance.
- Live model mode does not exist in this runner. Gate C (real models,
  paired tasks, full cost) is **NOT_RUN** until credentials and a budget
  are provided and the manifest is frozen per EVALUATION_PLAN §2.
  No paid API campaign is started by this harness.

## Suites

- `smoke` (3 tasks): recall after correction, retracted source, irrelevant
  change (negative control). Seed 20261108. Only used to shake out the
  harness — never a superiority claim.
- A confirmation suite (12 families × 3 variants, 3 seeds) is not built
  yet; it needs the live-model budget first.
