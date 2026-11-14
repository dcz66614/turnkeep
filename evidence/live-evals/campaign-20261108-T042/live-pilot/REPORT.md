# Live evaluation report — live-pilot (T042)
Real model: Codex CLI 0.164.0 via copilot-codex-bridge, model gpt5.6sol (GPT-5.6 Sol through Sam's existing M365 Copilot subscription, $0 new spend).
Scored deterministically from raw traces + deliverable artifacts; the agent's own claims are never trusted.
## Overall

- Attempts: 27
- Completed: 17 (success rate 0.630)
- Stale-fact rate: 0.074
- Contract violations: 0
- Irrelevant refusals: 0
- Mean redo cost (steps): 2.74
## Per arm
- none: 2/7 (rate 0.286), stale 0.286, violations 0, refusals 0- markdown: 5/7 (rate 0.714), stale 0.000, violations 0, refusals 0- log: 4/7 (rate 0.571), stale 0.000, violations 0, refusals 0- turnkeep: 6/6 (rate 1.000), stale 0.000, violations 0, refusals 0
## Failures (not completed)
- pilot-f01-changed / none / seed 20261108: stale=False violations=0 refusal=False timeout=False error=None
- pilot-f01-changed / log / seed 20261108: stale=False violations=0 refusal=False timeout=False error=None
- pilot-f01-changed / markdown / seed 20261108: stale=False violations=0 refusal=False timeout=False error=None
- pilot-f05-stable / none / seed 20261108: stale=False violations=0 refusal=False timeout=False error=None
- pilot-f05-stable / log / seed 20261108: stale=False violations=0 refusal=False timeout=False error=None
- pilot-f05-changed / none / seed 20261108: stale=True violations=0 refusal=False timeout=False error=None
- pilot-f02-changed / none / seed 20261108: stale=True violations=0 refusal=False timeout=False error=None
- pilot-f02-changed / log / seed 20261108: stale=False violations=0 refusal=False timeout=False error=None
- pilot-f03-stable / none / seed 20261108: stale=False violations=0 refusal=False timeout=False error=None
- pilot-f03-stable / markdown / seed 20261108: stale=False violations=0 refusal=False timeout=False error=None

## Notes

- B3/B4 arms NOT_RUN (no real installs); a missing arm is never scored as a competitor failure.
- Suite author and runner are the same agent; this is a process measure against accidental leakage, not a true independent blind test.
- Codex resends ~21k fixed context tokens per turn; per-attempt token totals are dominated by that overhead and are reported, not hidden.

## Coverage warning (added 2026-11-09, re-run cleanup)

This summary covers 27 of the 48 planned pilot attempts. 21 attempts were
deleted as backend-outage artifacts (fast empty answers after
2026-11-09T18:31:57Z) — see ../GAP-OUTAGE-20261109-RERUN.md. The 17/48
aggregate the runner printed at the end of the re-run is VOID and must
not be quoted. The 27 retained attempts are genuine measurements:
16 from the first run (f01/f05) + 11 from the re-run (f02 x8, f03-stable
x3, one with a fast-empty nudge phase disclosed in the GAP note).
