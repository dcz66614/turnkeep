# Live evaluation report — live-confirmation (T042)
Real model: agy CLI 1.4.12, model gemini-3.8-flash-low (Sam's existing agy access, $0 new spend).
Scored deterministically from raw traces + deliverable artifacts; the agent's own claims are never trusted.
## Overall

- Attempts: 432
- Completed: 352 (success rate 0.815)
- Stale-fact rate: 0.139
- Contract violations: 18
- Irrelevant refusals: 0
- Mean redo cost (steps): 6.41
## Per arm
- none: 45/108 (rate 0.417), stale 0.556, violations 0, refusals 0- markdown: 107/108 (rate 0.991), stale 0.000, violations 0, refusals 0- log: 105/108 (rate 0.972), stale 0.000, violations 0, refusals 0- turnkeep: 95/108 (rate 0.880), stale 0.000, violations 18, refusals 0
## Failures (not completed)
- conf-f09-recovery / none / seed 29: stale=True violations=0 refusal=False timeout=False error=None
- conf-f02-changed / none / seed 47: stale=True violations=0 refusal=False timeout=False error=None
- conf-f07-changed / none / seed 11: stale=True violations=0 refusal=False timeout=False error=None
- conf-f02-changed / none / seed 11: stale=True violations=0 refusal=False timeout=False error=None
- conf-f03-changed / none / seed 11: stale=True violations=0 refusal=False timeout=False error=None
- conf-f06-changed / none / seed 29: stale=True violations=0 refusal=False timeout=False error=None
- conf-f05-changed / none / seed 29: stale=True violations=0 refusal=False timeout=False error=None
- conf-f12-recovery / none / seed 11: stale=True violations=0 refusal=False timeout=False error=None
- conf-f03-recovery / none / seed 29: stale=True violations=0 refusal=False timeout=False error=None
- conf-f11-changed / none / seed 29: stale=True violations=0 refusal=False timeout=False error=None
- conf-f12-recovery / none / seed 29: stale=True violations=0 refusal=False timeout=False error=None
- conf-f08-stable / turnkeep / seed 29: stale=False violations=0 refusal=False timeout=False error=None
- conf-f11-changed / none / seed 47: stale=True violations=0 refusal=False timeout=False error=None
- conf-f02-recovery / none / seed 29: stale=True violations=0 refusal=False timeout=False error=None
- conf-f02-recovery / none / seed 47: stale=True violations=0 refusal=False timeout=False error=None
- conf-f05-recovery / none / seed 29: stale=True violations=0 refusal=False timeout=False error=None
- conf-f08-recovery / none / seed 11: stale=True violations=0 refusal=False timeout=False error=None
- conf-f07-recovery / none / seed 47: stale=True violations=0 refusal=False timeout=False error=None
- conf-f08-stable / none / seed 47: stale=False violations=0 refusal=False timeout=False error=None
- conf-f08-stable / none / seed 29: stale=False violations=0 refusal=False timeout=False error=None
- conf-f08-recovery / none / seed 47: stale=True violations=0 refusal=False timeout=False error=None
- conf-f05-changed / none / seed 11: stale=True violations=0 refusal=False timeout=False error=None
- conf-f04-changed / none / seed 29: stale=True violations=0 refusal=False timeout=False error=None
- conf-f01-recovery / none / seed 29: stale=True violations=0 refusal=False timeout=False error=None
- conf-f04-recovery / none / seed 29: stale=True violations=0 refusal=False timeout=False error=None
- conf-f07-recovery / none / seed 11: stale=True violations=0 refusal=False timeout=False error=None
- conf-f09-recovery / none / seed 47: stale=True violations=0 refusal=False timeout=False error=None
- conf-f01-changed / none / seed 29: stale=True violations=0 refusal=False timeout=False error=None
- conf-f12-changed / none / seed 29: stale=True violations=0 refusal=False timeout=False error=None
- conf-f05-recovery / none / seed 11: stale=True violations=0 refusal=False timeout=False error=None
- conf-f03-recovery / none / seed 11: stale=True violations=0 refusal=False timeout=False error=None
- conf-f12-changed / none / seed 11: stale=True violations=0 refusal=False timeout=False error=None
- conf-f04-changed / turnkeep / seed 47: stale=False violations=2 refusal=False timeout=False error=None
- conf-f01-changed / none / seed 47: stale=True violations=0 refusal=False timeout=False error=None
- conf-f06-changed / none / seed 11: stale=True violations=0 refusal=False timeout=False error=None
- conf-f08-stable / markdown / seed 29: stale=False violations=0 refusal=False timeout=False error=None
- conf-f01-recovery / none / seed 47: stale=True violations=0 refusal=False timeout=False error=None
- conf-f05-changed / none / seed 47: stale=True violations=0 refusal=False timeout=False error=None
- conf-f04-stable / turnkeep / seed 47: stale=False violations=2 refusal=False timeout=False error=None
- conf-f03-changed / none / seed 29: stale=True violations=0 refusal=False timeout=False error=None
- conf-f12-stable / turnkeep / seed 47: stale=False violations=1 refusal=False timeout=False error=None
- conf-f08-stable / log / seed 29: stale=False violations=0 refusal=False timeout=False error=None
- conf-f03-changed / none / seed 47: stale=True violations=0 refusal=False timeout=False error=None
- conf-f11-changed / none / seed 11: stale=True violations=0 refusal=False timeout=False error=None
- conf-f11-recovery / none / seed 47: stale=True violations=0 refusal=False timeout=False error=None
- conf-f04-recovery / none / seed 11: stale=True violations=0 refusal=False timeout=False error=None
- conf-f09-recovery / none / seed 11: stale=True violations=0 refusal=False timeout=False error=None
- conf-f02-recovery / none / seed 11: stale=True violations=0 refusal=False timeout=False error=None
- conf-f05-recovery / none / seed 47: stale=True violations=0 refusal=False timeout=False error=None
- conf-f01-recovery / none / seed 11: stale=True violations=0 refusal=False timeout=False error=None
- conf-f06-recovery / none / seed 47: stale=True violations=0 refusal=False timeout=False error=None
- conf-f04-changed / none / seed 11: stale=True violations=0 refusal=False timeout=False error=None
- conf-f07-changed / none / seed 29: stale=True violations=0 refusal=False timeout=False error=None
- conf-f01-changed / none / seed 11: stale=True violations=0 refusal=False timeout=False error=None
- conf-f12-stable / turnkeep / seed 11: stale=False violations=2 refusal=False timeout=False error=None
- conf-f06-recovery / none / seed 29: stale=True violations=0 refusal=False timeout=False error=None
- conf-f03-recovery / none / seed 47: stale=True violations=0 refusal=False timeout=False error=None
- conf-f07-changed / none / seed 47: stale=True violations=0 refusal=False timeout=False error=None
- conf-f07-recovery / none / seed 29: stale=True violations=0 refusal=False timeout=False error=None
- conf-f08-stable / none / seed 11: stale=False violations=0 refusal=False timeout=False error=None
- conf-f11-recovery / none / seed 29: stale=True violations=0 refusal=False timeout=False error=None
- conf-f08-stable / log / seed 47: stale=False violations=0 refusal=False timeout=False error=None
- conf-f04-changed / none / seed 47: stale=True violations=0 refusal=False timeout=False error=None
- conf-f02-changed / none / seed 29: stale=True violations=0 refusal=False timeout=False error=None
- conf-f11-recovery / none / seed 11: stale=True violations=0 refusal=False timeout=False error=None
- conf-f04-recovery / none / seed 47: stale=True violations=0 refusal=False timeout=False error=None
- conf-f06-stable / turnkeep / seed 11: stale=False violations=2 refusal=False timeout=False error=None
- conf-f08-stable / log / seed 11: stale=False violations=0 refusal=False timeout=False error=None
- conf-f06-changed / none / seed 47: stale=True violations=0 refusal=False timeout=False error=None
- conf-f12-changed / none / seed 47: stale=True violations=0 refusal=False timeout=False error=None
- conf-f09-recovery / turnkeep / seed 11: stale=False violations=3 refusal=False timeout=False error=None
- conf-f02-recovery / turnkeep / seed 29: stale=False violations=1 refusal=False timeout=False error=None
- conf-f04-recovery / turnkeep / seed 47: stale=False violations=1 refusal=False timeout=False error=None
- conf-f09-changed / turnkeep / seed 29: stale=False violations=1 refusal=False timeout=False error=None
- conf-f09-stable / turnkeep / seed 11: stale=False violations=1 refusal=False timeout=False error=None
- conf-f02-recovery / turnkeep / seed 47: stale=False violations=1 refusal=False timeout=False error=None
- conf-f06-recovery / none / seed 11: stale=True violations=0 refusal=False timeout=False error=None
- conf-f03-recovery / turnkeep / seed 29: stale=False violations=1 refusal=False timeout=False error=None
- conf-f12-recovery / none / seed 47: stale=True violations=0 refusal=False timeout=False error=None
- conf-f08-recovery / none / seed 29: stale=True violations=0 refusal=False timeout=False error=None

## Notes

- B3/B4 arms NOT_RUN (no real installs); a missing arm is never scored as a competitor failure.
- Suite author and runner are the same agent; this is a process measure against accidental leakage, not a true independent blind test.
- This campaign uses the agy driver (separate from the Codex + GPT-5.6 Sol campaign); the two campaigns' numbers are never mixed.
