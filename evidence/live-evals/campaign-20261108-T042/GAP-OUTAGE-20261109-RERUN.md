# T042 Codex re-run pilot — CONTAMINATED AGAIN by backend outage (GAP record)

Date: 2026-11-09 ~18:15–18:40 GMT. Campaign: `evidence/live-evals/campaign-20261108-T042/`.
Route: **Codex + GPT-5.6 Sol** — Codex CLI 0.164.0 driver, model GPT-5.6 Sol
via the local `copilot-codex-bridge` (`http://127.0.0.1:8322/v1`, Responses API)
on Sam's existing M365 Copilot subscription. **$0 new spend.** This re-run was
dispatched after Sam said the Copilot-connected Codex was restored (~18:14 GMT).

## Resume precondition: PASSED before the run

- `/health`: `ok:true`, token valid (exp 2026-11-09T19:07:35Z), bridge pid 2007.
- Trivial model call (own construction, `POST /v1/responses`,
  `{"model":"GPT-5.6 Sol","input":[{"role":"user","content":"Reply with exactly the word OK and nothing else."}],"stream":false}`):
  **HTTP 200, status completed, text "OK", output_tokens 2, no incomplete_details,
  5.1 s.** The backend was genuinely serving when the run started.
  (Two earlier attempts failed on my side, not the backend: first without the
  bridge `Authorization: Bearer` key → 401; then with a bare-string `input`,
  which the bridge drops → 400 "input 为空". Both are request-shape issues.)

## What was done before the re-run

- Deleted the 32 contaminated records from the first pilot (see
  `GAP-OUTAGE-20261109.md`) from `live-pilot/attempts.jsonl` + their
  `attempts/<id>/` dirs; kept the 16 valid first-run attempts (f01/f05).
  First-run `manifest.json` backed up as `manifest-20261109-first-run.json`
  (the runner's `freeze_manifest` overwrites it on re-run).
- Re-ran with `--resume` (skips recorded attempts):
  `python3 benchmarks/live/run_live.py --suite pilot --driver codex --resume
  --output evidence/live-evals/campaign-20261108-T042 --parallel 2`,
  log `codex-pilot-rerun.log`. Runner: "48 planned, 32 to run, 16 already recorded".

## What happened: the backend died again mid-run, same signature

- **18:16–~18:31Z: backend serving.** First 11 re-run attempts are real
  measurements (f02-stable 4/4 completed, f02-changed 3/4 with 1 genuine
  stale-fact failure, f03-stable/log + f03-stable/none genuine).
- **First fast-empty answer: 2026-11-09T18:31:57Z.** Bridge log
  (`/tmp/bridge-restart.log`, since lost in the 18:39 VM restart):
  `responses engine=ws tone=Gpt_5_6_Reasoning tools=10 tool_call=- chars=0
  total=2385ms err=空回答（可能被 Disengaged 或额度限制）` — identical to the
  00:30Z outage signature (fast ~2.5 s, empty text, 0 output tokens).
  In the 18:30–18:39 window, **48 of 61 bridge responses calls were chars=0**.
- Every attempt finishing after ~18:31:53Z ([28/48]–[48/48]) took ~10 s each,
  0 steps, 2 turns, ~48.8–50.8k tokens — the outage signature, not measurements.
- **Runner-printed aggregate 17/48 is VOID and must not be quoted** — it
  includes 21 outage artifacts.

## Classification of the 32 re-run attempts (verified per attempt from rollout JSONs)

**Deleted as outage artifacts — 21 attempts** (both phases ~3–6 s, empty agent
messages, 0 tool calls):
- pilot-f03-changed × {none, markdown, log, turnkeep} (4)
- pilot-f03-stable / turnkeep (1)
- pilot-f07-stable × 4, pilot-f07-changed × 4, pilot-f10-changed × 4,
  pilot-f10-stable × 4 (16)

**Kept as genuine measurements — 11 attempts:**
- pilot-f02-stable × 4: real tool use (6–30 calls), all completed.
- pilot-f02-changed / none: real (26 tool calls), failed with a genuine stale
  fact — a model error, not infrastructure.
- pilot-f02-changed / markdown: real (18 tool calls), completed.
- pilot-f02-changed / turnkeep: phase A hit the 180 s WS timeout (empty),
  nudge phase ran 395 s with 8 tool calls and completed — genuine recovery.
- pilot-f02-changed / log: BOTH phases hit the 180 s WS timeout
  (`WS 超时 180000ms，已收 0 字`, bridge log 18:23/18:24/18:26Z), 0 steps,
  failed. This is the known WS-timeout flake class (same as 3 kept 0-step
  attempts from the first run), NOT the fast-empty outage — kept as a failure.
- pilot-f03-stable / none: both phases returned real text but made 0 tool
  calls — genuine model failure, kept.
- pilot-f03-stable / log: completed (3 steps, 6 tool calls); its final agent
  message was empty but the work was done via tools — kept.
- pilot-f03-stable / markdown: phase A ran 148 s with 2 tool calls and failed
  (genuine); its N1 nudge phase was a 4 s fast-empty — that phase ran during
  the outage, disclosed here; the scored outcome (failed, 1 step) comes from
  phase A. Kept with this disclosure.

Note the WS timeout in this bridge instance was 180 s, not the 600 s raised
for the first campaign — the raise did not survive the bridge restarts.
That is a separate, known flake source; it did not cause this outage.

## Valid-subset result after cleanup (re-scored with the module's own scorer)

`live-pilot/attempts.jsonl` now holds **27 valid attempts** (16 first-run +
11 re-run); `summary.json` + `REPORT.md` were regenerated from them.
**This is a partial pilot (27/48), NOT a full pilot result — 21 attempts are
still unmeasured.**

- Overall: **17/27 completed (63.0%)**, stale-fact rate 7.4% (2/27),
  contract violations 0, irrelevant refusals 0, mean redo 2.74 steps.
- Per arm: none 2/7 (stale 28.6%), markdown 5/7 (stale 0.0%),
  log 4/7 (stale 0.0%), **turnkeep 6/6 (stale 0.0%)**.
- The 2 stale facts: pilot-f02-changed/none (re-run) and pilot-f05-changed/none
  (first run) — both genuine model errors.

## STOP decision

Per the task's stop rule (429 / empty answers recur → stop, do not burn
attempts on a dead backend): **the 432-attempt confirmation set was NOT run.**
The M365 backend has now died twice in one day on this route (00:30Z and
18:31Z) with the identical fast-empty signature, after a clean trivial-call
check both times. The daily-quota question (GPT61SolQueryDaily 40 in the
static quota snapshot) is still unresolved.

## Environment notes

- VM restarted again at **18:39 UTC** (uptime 1 min at 18:41Z); the bridge
  died with it and `/tmp/bridge-restart.log` was lost. The bridge must be
  restarted and re-verified with a live call before any future Codex work.
- Before any next re-run: raise the bridge WS timeout back to 600 s, wait for
  the backend to recover, re-verify with a trivial call returning real text,
  then re-run the 21 missing pilot attempts with `--resume`.
