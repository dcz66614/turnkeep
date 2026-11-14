# T042 live pilot — CONTAMINATED by backend outage (GAP record)

> Update 2026-11-09 ~18:40 GMT: the re-run after Sam reported the Codex route
> restored hit the SAME outage again at 18:31:57Z — see
> `GAP-OUTAGE-20261109-RERUN.md`. The pilot is still incomplete (27/48 valid
> attempts); the 432-attempt confirmation was NOT run.

Date: 2026-11-09 ~01:00 GMT. Campaign: `evidence/live-evals/campaign-20261108-T042/`.
Route (per Sam, 2026-11-08 ~23:00 GMT): **Codex + GPT-5.6 Sol** — Codex CLI
0.164.0 is the driver; the model is GPT-5.6 Sol (deep reasoning) served by
the local `copilot-codex-bridge` (`http://127.0.0.1:8322/v1`, Responses API)
on Sam's existing M365 Copilot subscription. **$0 new spend.** No paid API,
no new purchase, no new login. Full route + budget frozen in
`manifest.json` and `state/eval-budget.json`.

## What happened

The pilot ran 48/48 attempts (raw traces kept), but the M365 Copilot
backend stopped serving mid-campaign. **The recorded pilot aggregate
(10/48 = 20.8%) is NOT a valid pilot result and must not be quoted as one.**

- Last successful model call: **2026-11-09T00:30:57.590Z** (bridge log,
  `responses engine=ws tone=Gpt_5_6_Reasoning tool_call=exec_command chars=116`).
- First empty answer: **2026-11-09T00:30:57.727Z** — 137 ms later.
- After 00:31:00Z: **0 successful calls in 72 bridge calls** (all
  `空回答（可能被 Disengaged 或额度限制）`, 0 output tokens, ~2.5 s each).
  Still empty at retest 00:44:55Z (after a 10-minute wait) and after a full
  bridge restart (~00:47Z, new bridge, same result).

## Symptoms (all verified, not assumed)

- Backend replies fast (~2.5 s) with a completed response containing
  **empty text, `output_tokens: 0`**, `incomplete_details: {reason: "error",
  message: "空回答（可能被 Disengaged 或额度限制）"}` — for BOTH tones
  (`gpt5.6sol` deep and `gpt5.6sol-quick`) and for a trivial prompt
  ("Reply with exactly the word OK").
- NOT the prompt filter: the bridge distinguishes a real `Disengaged`
  messageType (separate error) from empty text; these calls returned empty
  text with NO Disengaged flag, on fresh conversationIds every call
  (`ws.chat` generates a new uuid per call — no shared conversation to
  poison).
- NOT auth: token auto-refresh works (`refreshedAt 2026-11-09T00:20:01Z`,
  new access token captured); `/health` reports `ok:true, token valid:true`
  throughout; WS connects fine.
- NOT a timeout: empty answers arrive in ~2.5 s, not at the 600 s WS
  timeout (raised from 180 s for this campaign after 10 timeouts in the
  previous bridge instance).
- NOT the harness: the same empty-answer signature appears in direct
  `curl /v1/responses` calls with no Codex involved.
- Quota snapshot (`GET /v1/quota`) is static and uninformative — the same
  values (GPT61SolQueryDaily 40, GPT61SolQueryWeekly 75,
  ReasoningModelTurnUsage 10, TotalTurnUsageDaily 999, LLMOnly 100) across
  the whole night, before and after the outage. No limit can be confirmed
  or excluded from it. Hypotheses (unverified): daily/hourly M365 query
  cap exhausted, or an upstream M365 throttling/outage for this account.

## Which attempts are valid, which are contaminated

- **Valid (real model engagement, all model turns before 00:30:57Z):
  16 attempts** — pilot-f01-stable × {log, turnkeep, markdown, none},
  pilot-f01-changed × {none, turnkeep, log, markdown},
  pilot-f05-stable × {none, turnkeep, log, markdown},
  pilot-f05-changed × {markdown, log, none, turnkeep}.
  Of these 16: **10 completed** (f01-stable 4/4; f01-changed turnkeep;
  f05-stable turnkeep + markdown; f05-changed markdown, log, turnkeep),
  1 used a stale fact (f05-changed/none, 20 steps — a genuine model error),
  5 failed (3 with 0 steps from the pre-restart 180 s WS-timeout era,
  2 with 1 step). Valid-subset rates, for description only, n=16:
  overall 10/16; none 1/4, markdown 3/4, log 2/4, turnkeep 4/4.
  These are NOT the pilot result — n=16 is too small and the set is
  time-biased (only the first tasks in suite order).
- **Contaminated (backend-outage signature: 0 tool steps, 2 empty turns,
  ~48–50k context tokens each, all model calls after 00:30:57Z):
  32 attempts** — pilot-f02-stable × 4, pilot-f02-changed × 4,
  pilot-f03-stable × 4, pilot-f03-changed × 4, pilot-f07-stable × 4,
  pilot-f07-changed × 4, pilot-f10-changed × 4, pilot-f10-stable × 4.
  These are infrastructure artifacts, NOT arm measurements. They must be
  deleted and re-run, never scored.
- Smoke test (separate, before the pilot): **PASS 1/1** —
  `evidence/live-evals/smoke-20261108-T042/` (pilot-f01-stable/none,
  completed=True, deliverable `output/manifest.txt` = "Python 3.11",
  660,245 tokens, 438 s, 23 steps, real tool calls in the rollout).
  The harness fix that made this possible: do not pass `cwd=workdir` to
  the codex subprocess (codex fails at startup with
  "No such file or directory (os error 2)" when process cwd == the `-C`
  workspace under `evidence/`; `-C` alone works).

## What was tried

1. Harness bug (48/48 instant failures, os error 2): diagnosed by
   bisection to `cwd=workdir` in `subprocess.run`; fixed in
   `benchmarks/live/codex_driver.py` (use `-C` only); verified by an
   isolated `_run_codex` call (rc=0, session id, 22,917 tokens) and by
   the smoke test.
2. WS 180 s timeouts (10 in the first bridge instance): bridge restarted
   with `WS_TIMEOUT_MS=600000`; verified in the new process environ.
3. Outage: 10-minute wait + retest (still empty); full bridge restart +
   retest (still empty); both tones; trivial prompt; token refresh
   verified; `/health` verified. Stopped — further attempts would only
   burn context tokens on empty answers.

## T042 status

**BLOCKED_EXTERNAL / NOT_RUN.** The pilot did not complete cleanly, the
confirmation set (432 attempts) never started, the two-client comparison
(T026-dependent) is NOT_RUN, and competitor arms B3/B4 (Mem0/Hindsight,
Tigerless/Engram — no real installs) are NOT_RUN. `value_evaluated`
remains NOT READY. No scores are claimed.

## Resume plan (for whoever picks this up)

1. Re-test the backend first with a trivial call — it must return real
   text before any campaign runs:
   `curl -s http://127.0.0.1:8322/v1/responses -H "Authorization: Bearer $(cat
   ~/workspace/copilot-codex-bridge/.api-key | tr -d '\r\n')" -H
   "Content-Type: application/json" -d '{"model":"gpt5.6sol-quick","input":
   [{"role":"user","content":"Reply with exactly the word OK"}]}'`
   — look for `"text":"OK"` and `"output_tokens":1+`. If still empty,
   do not start the campaign; the block is upstream of this repo.
2. Delete the 32 contaminated attempt records from
   `live-pilot/attempts.jsonl` (keep the 16 valid ones) and their
   `live-pilot/attempts/<id>/` dirs, then re-run the pilot with
   `benchmarks/live/run_live.py --suite pilot --parallel 3 --resume
   --output evidence/live-evals/campaign-20261108-T042` (resume skips
   recorded attempts). Remove the `CONTAMINATED` marker only when the
   re-run pilot completes with no outage-signature attempts.
3. Then run the confirmation set the same way (`--suite confirmation`);
   432 attempts ≈ 6–8 h at parallel 3 — run it in the background with
   `--resume` support and monitor via `attempts.jsonl`.
4. Parameter-freeze note from the valid subset + smoke: per-attempt token
   use is far above EVALUATION_PLAN §7 (60k input cap) — codex resends
   ~21k fixed context tokens per turn; observed totals 48k–660k per
   attempt. The §7 caps must be re-frozen after a clean pilot (the plan
   allows parameter freezing after the pilot).
