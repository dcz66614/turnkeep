# Live evaluation report — live-pilot (T042)
Real model: Codex CLI 0.164.0 via copilot-codex-bridge, model gpt5.6sol (GPT-5.6 Sol through Sam's existing M365 Copilot subscription, $0 new spend).
Scored deterministically from raw traces + deliverable artifacts; the agent's own claims are never trusted.
## Overall

- Attempts: 1
- Completed: 1 (success rate 1.000)
- Stale-fact rate: 0.000
- Contract violations: 0
- Irrelevant refusals: 0
- Mean redo cost (steps): 19.00
## Per arm
- none: 1/1 (rate 1.000), stale 0.000, violations 0, refusals 0- markdown: 0/0 (rate 0.000), stale 0.000, violations 0, refusals 0- log: 0/0 (rate 0.000), stale 0.000, violations 0, refusals 0- turnkeep: 0/0 (rate 0.000), stale 0.000, violations 0, refusals 0
## Failures (not completed)
None — every attempt completed.

## Notes

- B3/B4 arms NOT_RUN (no real installs); a missing arm is never scored as a competitor failure.
- Suite author and runner are the same agent; this is a process measure against accidental leakage, not a true independent blind test.
- Codex resends ~21k fixed context tokens per turn; per-attempt token totals are dominated by that overhead and are reported, not hidden.
