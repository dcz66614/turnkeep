# Live evaluation report — live-pilot (T042)
Real model: agy CLI 1.4.12, model gemini-3.8-flash-low (Sam's existing agy access, $0 new spend).
Scored deterministically from raw traces + deliverable artifacts; the agent's own claims are never trusted.
## Overall

- Attempts: 48
- Completed: 43 (success rate 0.896)
- Stale-fact rate: 0.104
- Contract violations: 0
- Irrelevant refusals: 0
- Mean redo cost (steps): 3.08
## Per arm
- none: 7/12 (rate 0.583), stale 0.417, violations 0, refusals 0- markdown: 12/12 (rate 1.000), stale 0.000, violations 0, refusals 0- log: 12/12 (rate 1.000), stale 0.000, violations 0, refusals 0- turnkeep: 12/12 (rate 1.000), stale 0.000, violations 0, refusals 0
## Failures (not completed)
- pilot-f01-changed / none / seed 20261108: stale=True violations=0 refusal=False timeout=False error=None
- pilot-f05-changed / none / seed 20261108: stale=True violations=0 refusal=False timeout=False error=None
- pilot-f02-changed / none / seed 20261108: stale=True violations=0 refusal=False timeout=False error=None
- pilot-f03-changed / none / seed 20261108: stale=True violations=0 refusal=False timeout=False error=None
- pilot-f07-changed / none / seed 20261108: stale=True violations=0 refusal=False timeout=False error=None

## Notes

- B3/B4 arms NOT_RUN (no real installs); a missing arm is never scored as a competitor failure.
- Suite author and runner are the same agent; this is a process measure against accidental leakage, not a true independent blind test.
- This campaign uses the agy driver (separate from the Codex + GPT-5.6 Sol campaign); the two campaigns' numbers are never mixed.
