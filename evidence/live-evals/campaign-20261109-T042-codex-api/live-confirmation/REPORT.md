# Live evaluation report — live-confirmation (T042)
Real model: Codex CLI 0.164.0 via official OpenAI API, model gpt-6-luna, reasoning effort low (Sam's OpenAI account, pay-as-you-go).
Scored deterministically from raw traces + deliverable artifacts; the agent's own claims are never trusted.
## Overall

- Attempts: 432
- Completed: 354 (success rate 0.819)
- Stale-fact rate: 0.134
- Contract violations: 11
- Irrelevant refusals: 0
- Mean redo cost (steps): 0.90
## Per arm
- none: 46/108 (rate 0.426), stale 0.537, violations 0, refusals 0- markdown: 105/108 (rate 0.972), stale 0.000, violations 0, refusals 0- log: 105/108 (rate 0.972), stale 0.000, violations 0, refusals 0- turnkeep: 98/108 (rate 0.907), stale 0.000, violations 11, refusals 0
## Failures (not completed)
- conf-f09-recovery / none / seed 29: stale=True violations=0 refusal=False timeout=False error=None
- conf-f02-changed / none / seed 47: stale=True violations=0 refusal=False timeout=False error=None
- conf-f07-changed / none / seed 11: stale=True violations=0 refusal=False timeout=False error=None
- conf-f02-changed / none / seed 11: stale=True violations=0 refusal=False timeout=False error=None
- conf-f03-changed / none / seed 11: stale=True violations=0 refusal=False timeout=False error=None
- conf-f06-changed / none / seed 29: stale=True violations=0 refusal=False timeout=False error=None
- conf-f02-recovery / turnkeep / seed 29: stale=False violations=1 refusal=False timeout=False error=None
- conf-f04-recovery / turnkeep / seed 11: stale=False violations=1 refusal=False timeout=False error=None
- conf-f12-recovery / none / seed 11: stale=True violations=0 refusal=False timeout=False error=None
- conf-f03-recovery / none / seed 29: stale=True violations=0 refusal=False timeout=False error=None
- conf-f11-changed / none / seed 29: stale=True violations=0 refusal=False timeout=False error=None
- conf-f12-recovery / none / seed 29: stale=True violations=0 refusal=False timeout=False error=None
- conf-f08-stable / markdown / seed 11: stale=False violations=0 refusal=False timeout=False error=None
- conf-f08-stable / turnkeep / seed 29: stale=False violations=0 refusal=False timeout=False error=None
- conf-f04-recovery / turnkeep / seed 47: stale=False violations=1 refusal=False timeout=False error=None
- conf-f11-changed / none / seed 47: stale=True violations=0 refusal=False timeout=False error=None
- conf-f02-recovery / none / seed 47: stale=True violations=0 refusal=False timeout=False error=None
- conf-f02-recovery / none / seed 29: stale=True violations=0 refusal=False timeout=False error=None
- conf-f05-recovery / none / seed 29: stale=False violations=0 refusal=False timeout=False error=None
- conf-f08-recovery / none / seed 11: stale=True violations=0 refusal=False timeout=False error=None
- conf-f08-stable / none / seed 47: stale=False violations=0 refusal=False timeout=False error=None
- conf-f07-recovery / none / seed 47: stale=True violations=0 refusal=False timeout=False error=None
- conf-f08-stable / none / seed 29: stale=False violations=0 refusal=False timeout=False error=None
- conf-f08-stable / markdown / seed 47: stale=False violations=0 refusal=False timeout=False error=None
- conf-f08-recovery / none / seed 47: stale=True violations=0 refusal=False timeout=False error=None
- conf-f05-changed / none / seed 11: stale=True violations=0 refusal=False timeout=False error=None
- conf-f04-changed / none / seed 29: stale=True violations=0 refusal=False timeout=False error=None
- conf-f01-recovery / none / seed 29: stale=True violations=0 refusal=False timeout=False error=None
- conf-f04-recovery / none / seed 29: stale=True violations=0 refusal=False timeout=False error=None
- conf-f07-recovery / none / seed 11: stale=True violations=0 refusal=False timeout=False error=None
- conf-f09-recovery / none / seed 47: stale=True violations=0 refusal=False timeout=False error=None
- conf-f01-changed / none / seed 29: stale=True violations=0 refusal=False timeout=False error=None
- conf-f06-recovery / turnkeep / seed 47: stale=False violations=2 refusal=False timeout=False error=None
- conf-f12-changed / none / seed 29: stale=True violations=0 refusal=False timeout=False error=None
- conf-f05-recovery / none / seed 11: stale=True violations=0 refusal=False timeout=False error=None
- conf-f03-recovery / none / seed 11: stale=True violations=0 refusal=False timeout=False error=None
- conf-f12-changed / none / seed 11: stale=True violations=0 refusal=False timeout=False error=None
- conf-f01-changed / none / seed 47: stale=True violations=0 refusal=False timeout=False error=None
- conf-f06-changed / none / seed 11: stale=True violations=0 refusal=False timeout=False error=None
- conf-f08-stable / markdown / seed 29: stale=False violations=0 refusal=False timeout=False error=None
- conf-f01-recovery / none / seed 47: stale=True violations=0 refusal=False timeout=False error=None
- conf-f05-changed / none / seed 47: stale=True violations=0 refusal=False timeout=False error=None
- conf-f06-recovery / turnkeep / seed 11: stale=False violations=1 refusal=False timeout=False error=None
- conf-f03-changed / none / seed 29: stale=True violations=0 refusal=False timeout=False error=None
- conf-f08-stable / log / seed 29: stale=False violations=0 refusal=False timeout=False error=None
- conf-f03-changed / none / seed 47: stale=True violations=0 refusal=False timeout=False error=None
- conf-f11-changed / none / seed 11: stale=True violations=0 refusal=False timeout=False error=None
- conf-f11-recovery / none / seed 47: stale=True violations=0 refusal=False timeout=False error=None
- conf-f04-recovery / none / seed 11: stale=True violations=0 refusal=False timeout=False error=None
- conf-f09-recovery / none / seed 11: stale=True violations=0 refusal=False timeout=False error=None
- conf-f02-recovery / none / seed 11: stale=True violations=0 refusal=False timeout=False error=None
- conf-f05-recovery / none / seed 47: stale=True violations=0 refusal=False timeout=False error=None
- conf-f01-recovery / none / seed 11: stale=True violations=0 refusal=False timeout=False error=None
- conf-f04-recovery / turnkeep / seed 29: stale=False violations=2 refusal=False timeout=False error=None
- conf-f06-recovery / none / seed 47: stale=True violations=0 refusal=False timeout=False error=None
- conf-f04-changed / none / seed 11: stale=True violations=0 refusal=False timeout=False error=None
- conf-f07-changed / none / seed 29: stale=True violations=0 refusal=False timeout=False error=None
- conf-f01-changed / none / seed 11: stale=True violations=0 refusal=False timeout=False error=None
- conf-f06-recovery / none / seed 29: stale=True violations=0 refusal=False timeout=False error=None
- conf-f03-recovery / none / seed 47: stale=True violations=0 refusal=False timeout=False error=None
- conf-f07-changed / none / seed 47: stale=True violations=0 refusal=False timeout=False error=None
- conf-f07-recovery / none / seed 29: stale=True violations=0 refusal=False timeout=False error=None
- conf-f08-recovery / turnkeep / seed 11: stale=False violations=2 refusal=False timeout=False error=None
- conf-f08-stable / none / seed 11: stale=False violations=0 refusal=False timeout=False error=None
- conf-f11-recovery / none / seed 29: stale=True violations=0 refusal=False timeout=False error=None
- conf-f08-stable / log / seed 47: stale=False violations=0 refusal=False timeout=False error=None
- conf-f02-changed / none / seed 29: stale=True violations=0 refusal=False timeout=False error=None
- conf-f04-changed / none / seed 47: stale=True violations=0 refusal=False timeout=False error=None
- conf-f04-recovery / none / seed 47: stale=True violations=0 refusal=False timeout=False error=None
- conf-f11-recovery / none / seed 11: stale=True violations=0 refusal=False timeout=False error=None
- conf-f08-stable / turnkeep / seed 47: stale=False violations=0 refusal=False timeout=False error=None
- conf-f08-stable / log / seed 11: stale=False violations=0 refusal=False timeout=False error=None
- conf-f08-stable / turnkeep / seed 11: stale=False violations=1 refusal=False timeout=False error=None
- conf-f06-recovery / none / seed 11: stale=True violations=0 refusal=False timeout=False error=None
- conf-f06-changed / none / seed 47: stale=True violations=0 refusal=False timeout=False error=None
- conf-f12-changed / none / seed 47: stale=True violations=0 refusal=False timeout=False error=None
- conf-f12-recovery / none / seed 47: stale=True violations=0 refusal=False timeout=False error=None
- conf-f08-recovery / none / seed 29: stale=True violations=0 refusal=False timeout=False error=None

## Notes

- B3/B4 arms NOT_RUN (no real installs); a missing arm is never scored as a competitor failure.
- Suite author and runner are the same agent; this is a process measure against accidental leakage, not a true independent blind test.
- Official OpenAI API (gpt-6-luna, low effort); per-attempt token totals (input / cached / cache-write / output / reasoning split) are recorded in the traces and costed in the campaign's SPEND.md. This campaign is separate from the bridge (Codex + GPT-5.6 Sol) and agy campaigns; the three campaigns' numbers are never mixed.
