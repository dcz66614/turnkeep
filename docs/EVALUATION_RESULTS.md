# Evaluation results (T042) — Gate C

## Status: T042 VERIFIED 2026-11-10 ~09:08 GMT (Sam's decision) — two independent clean live runs

Three separate campaigns, never mixed. The **codex-api campaign** (official
OpenAI API, GPT-6 Luna low effort) is complete and clean: pilot 43/48,
confirmation 354/432, total spend $1.90 — this is the run Sam accepted as
meeting T042's own bar. The **agy campaign** (below) is a second complete,
clean live evaluation, $0 new spend. The **Copilot-bridge Codex + GPT-5.6
Sol campaign** (further below) is still blocked: pilot contaminated twice
by M365 outages, confirmation never started. The finding is MIXED (see the
codex-api section): the turnkeep arm used 0% stale facts on both clean
runs, but its completion rate is below the file arms — no blanket win claim.

## Codex API route: COMPLETE and CLEAN (quotable) — the T042 evidence

Route (per Sam, 2026-11-09 ~19:54 GMT: "全部用luna low"): **Codex CLI
0.164.0 via the official OpenAI API (api.openai.com, Responses API),
model gpt-6-luna, reasoning effort low**, pay-as-you-go on Sam's OpenAI
account. **Total spend $1.90 for all 480 attempts** (frozen $25 budget in
`state/eval-budget-codex-api.json` + campaign `manifest.json`). Driver
`benchmarks/live/codex_driver.py` (`codex-api` mode; the Code Mode
`custom_tool_call` rollout format is parsed to real `exec_command` steps).
Campaign `evidence/live-evals/campaign-20261109-T042-codex-api/`
(commits 298ef6e/b27968c/3722e93). Full account:
`evidence/live-evals/campaign-20261109-T042-codex-api/NOTES.md`,
spend breakdown in `SPEND.md`.

### Smoke: PASS (1/1)

Trivial call returned `OK`, 12,657 tokens, ~17 s; rollout verified
`model_provider: openai`, zero bridge references.

### Pilot: CLEAN 43/48 (89.6%)

turnkeep 12/12, markdown 12/12, log 12/12, none 7/12; stale facts 10.4%,
all in the none arm; violations 0, refusals 0; mean redo cost 0.19 steps.

### Confirmation: CLEAN 354/432 (81.9%)

12 families × 3 variants × 3 seeds × 4 arms, `--parallel 2`. Overall:
completed 354/432 (81.9%), stale-fact rate 13.4%, contract violations 11,
irrelevant refusals 0, mean redo cost 0.90 steps. Per arm (n=108 each):
**none 46 (42.6%, stale 53.7%)**, markdown 105 (97.2%), log 105 (97.2%),
**turnkeep 98 (90.7%, stale 0.0%, violations 11, mean redo 2.43 steps)**.
Final trace check: 0 empty answers, 0 quota errors, 0 errors, 0 timeouts,
0 zero-step traces.

Spend: pilot $0.2225 + confirmation $1.6773 = **$1.8998 total** (~$0.0040
per attempt), from the rollout usage splits (input / cached input /
cache-write / output / reasoning) at Luna Standard rates ($0.10 input,
$0.01 cached, $0.125 cache write, $0.50 output per 1M tokens). Prompt
caching is why it is this cheap: only 7,974 of 37,956,617 input tokens
(0.02%) were fresh. Two blockers were found and fixed en route: the Code
Mode companion binary (`codex-code-mode-host`, installed beside a copy of
the codex binary in `~/workspace/codex-bin/`, ~400 MB, outside the repo)
and the Code Mode rollout parser (see NOTES.md).

Honest reading: the two clean runs agree closely (agy 352/432, 81.5%;
codex-api 354/432, 81.9%), and the pattern is the same on both — the
turnkeep arm never used a stale fact (0.0% stale), but plain files
completed more (97.2% vs 90.7% here; 99.1%/97.2% vs 88.0% on agy).
Turnkeep's edge is the receipt discipline and the 0% stale rate, not
completion. The none arm's 53.7% stale rate is the baseline pain the
product addresses. `run_gate --task T042` PASS (2026-11-10T09:12:09Z).

## agy route: COMPLETE and CLEAN (quotable)

Route (per Sam, 2026-11-09 ~01:12 GMT: "你可以先测其他agent", while the
Copilot bridge was rate-limited): **agy CLI 1.4.12 + gemini-3.8-flash-low**,
Sam's existing agy access. **$0 new spend** (frozen in
`state/eval-budget-agy.json` + campaign `manifest.json`). Driver
`benchmarks/live/agy_driver.py` (mirrors `codex_driver.py`; parses agy
`--output-format stream-json` events; `--conversation` resume; never
changes the scoring logic). Campaign
`evidence/live-evals/campaign-20261109-T042-agy/`.

### Smoke: PASS (1/1)

`live-pilot/` — pilot-f01-stable/none, deliverable "Python 3.11", 16.8 s.

### Pilot: CLEAN 43/48 (89.6%)

turnkeep 12/12, markdown 12/12, log 12/12, none 7/12; stale facts 10.4%,
all in the none arm; violations 0, refusals 0; mean redo cost 3.08 steps.

### Confirmation: CLEAN 352/432 (81.5%)

12 families × 3 variants × 3 seeds × 4 arms, `--parallel 3`. Overall:
completed 352/432 (81.5%), stale-fact rate 13.9%, contract violations 18,
irrelevant refusals 0, mean redo cost 6.41 steps. Per arm (n=108 each):
**none 45 (41.7%, stale 55.6%)**, markdown 107 (99.1%), log 105 (97.2%),
**turnkeep 95 (88.0%, stale 0.0%, violations 18, mean redo 21.69 steps)**.

Contamination found and fixed mid-run (full account:
`evidence/live-evals/campaign-20261109-T042-agy/GAP-AGY-20261109.md`):
the first confirmation pass recorded 304/432 (70.4%) but was VOID — 39
turnkeep-arm attempts crashed in driver seeding (a `codex_driver.py` bug:
`correct()`'s new record id was discarded, so the second change of a
recovery task failed; F09's "inference" kind is not a product kind) and
24 attempts were hit by agy quota 429s. 63 contaminated attempts were
deleted and re-run after the fixes (plus a 429 stop-guard in the runner);
a VM reboot at 04:39 killed the re-run at 410/432 and the last 22 were
resumed. Final verification of all 432 traces: 0 zero-step, 0 errors,
0 quota errors, 0 timeouts. The 304/432 aggregate must never be quoted;
the clean number is 352/432.

Honest reading: the Turnkeep arm never used a stale fact (0.0% stale), but
the agent paid for the contract — 18 unrecovered stale-finish rejections
across 13 failed turnkeep attempts, and 21.69 mean redo steps vs 0.63–0.69
for the file arms. For this agent+model, plain files completed more
(99.1%/97.2% vs 88.0%); Turnkeep's edge here is the receipt discipline
and the 0% stale rate, not completion. The none arm's 55.6% stale rate is
the baseline pain the product addresses.

## Codex route: BLOCKED_EXTERNAL / NOT_RUN (pilot contaminated)

Route (per Sam, 2026-11-08 ~23:00 GMT): **Codex + GPT-5.6 Sol** — Codex CLI
0.164.0 is the driver; the model is GPT-5.6 Sol (deep reasoning) served by
the local `copilot-codex-bridge` (`http://127.0.0.1:8322/v1`, Responses API)
on Sam's existing M365 Copilot subscription. **$0 new spend** (frozen in
`state/eval-budget.json` + campaign `manifest.json`).

## Smoke test: PASS (1/1)

`evidence/live-evals/smoke-20261108-T042/` — pilot-f01-stable/none,
completed=True, deliverable `output/manifest.txt` = "Python 3.11",
660,245 tokens, 438 s, 23 steps, real tool calls in the rollout.
End-to-end route verified before the pilot.

## Pilot: CONTAMINATED — not a result

Campaign `evidence/live-evals/campaign-20261108-T042/`, 48/48 attempts
recorded with raw traces. The M365 Copilot backend stopped serving at
**2026-11-09T00:30:57Z** (empty answers, 0 output tokens, both tones,
trivial prompts; still empty after a 10-minute wait and a full bridge
restart; token refresh and `/health` fine throughout). The recorded
aggregate (10/48 = 20.8%) **must not be quoted as a pilot result**.

- Valid (real model engagement, all turns before 00:30:57Z): **16 attempts,
  10 completed** — f01-stable 4/4; f01-changed turnkeep; f05-stable turnkeep
  + markdown; f05-changed markdown, log, turnkeep. 1 stale-fact use
  (f05-changed/none, 20 steps — a genuine model error). Valid-subset rates
  (description only, n=16, time-biased): overall 10/16; none 1/4,
  markdown 3/4, log 2/4, turnkeep 4/4.
- Contaminated (0 steps, 2 empty turns, ~48–50k tokens each): **32 attempts**
  (f02, f03, f07, f10 — all variants × all arms). Infrastructure artifacts,
  not arm measurements; to be deleted and re-run.
- Full account + resume plan:
  `evidence/live-evals/campaign-20261108-T042/GAP-OUTAGE-20261109.md`
  (marker file `CONTAMINATED` in the same dir; `benchmarks/live/run.py`
  refuses LIVE_COMPLETE while it exists).

## Confirmation set (Codex route): NOT_RUN

Never started (432 attempts planned: 12 families × 3 variants × 3 seeds).
Blocked on a clean pilot + a working backend. A re-run pilot on 2026-11-09
~18:16–18:37 GMT was contaminated **again** by the same outage (empty
answers from 18:31:57Z; 21 artifacts deleted, valid subset 17/27;
commit 7d7210c, `GAP-OUTAGE-20261109-RERUN.md`). The agy route's and
codex-api route's confirmations above are complete and separate; neither
substitutes for a Codex-route confirmation if the gate requires the
GPT-5.6 Sol numbers specifically.

## Still NOT_RUN

- Two-client comparison (depends on T026, BLOCKED_EXTERNAL) — never
  substitute two SDK processes for two host products.
- Competitor arms B3/B4 (Mem0/Hindsight, Tigerless/Engram) — no real
  installs configured; never scored as competitor failures.

## Parameter-freeze note (from the valid subset + smoke)

Per-attempt token use is far above EVALUATION_PLAN §7 (60k input cap):
codex resends ~21k fixed context tokens per turn; observed totals
48k–660k per attempt. The §7 caps must be re-frozen after a clean pilot
(the plan allows parameter freezing after the pilot).

## What exists (runnable now)

- `benchmarks/live/run.py` — precondition checker + `--live` campaign
  runner; writes a NOT_RUN gap record and exits 2 without `--live`, and
  refuses LIVE_COMPLETE for contaminated campaigns.
- `benchmarks/live/run_live.py` — campaign runner (pilot/confirmation,
  `--resume`, `--parallel`, frozen manifest, per-attempt raw traces).
- `benchmarks/live/codex_driver.py` — Codex-via-bridge driver (stdin-fed
  prompts, `--sandbox workspace-write`; **never pass `cwd=workdir`** —
  codex fails at startup with "No such file or directory (os error 2)"
  when process cwd == the `-C` workspace).
- `benchmarks/live/score_live.py` — deterministic scorer (same rules as
  the scripted harness); report only from traces.
- `benchmarks/live/report.py` — reads `evidence/live-evals/` incl.
  campaign dirs; flags contaminated campaigns.
- `benchmarks/harness/run.py --suite smoke --mode deterministic` — the
  scripted harness (12 attempts, labelled
  `SCRIPTED_SIMULATION_NO_MODEL_CALLS`); see `benchmarks/results/`.

## When a live campaign can (re)run

1. Re-test the backend first with a trivial `/v1/responses` call — it must
   return real text (see the GAP record for the exact command). If still
   empty, do not start; the block is upstream of this repo.
2. Delete the 32 contaminated attempt records, keep the 16 valid ones,
   re-run the pilot with `--resume`, then the confirmation set.
3. Results go to `evidence/live-evals/` as raw traces + usage; the report
   is generated from those traces only. Pilot tuning results are never
   presented as the confirmation set.
