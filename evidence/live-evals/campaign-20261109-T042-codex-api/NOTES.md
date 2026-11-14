# T042 live campaign — official Codex API, GPT-6 Luna (low effort)

Campaign dir: `evidence/live-evals/campaign-20261109-T042-codex-api/`
Started: 2026-11-09 ~22:52 GMT. Route authorised by Sam 2026-11-09 ~19:54 GMT
("全部用luna low"). This REPLACES the Copilot-bridge route for T042; the
bridge is not used for any attempt in this campaign.

## Route

- Driver: Codex CLI 0.164.0 (`/opt/hatch-image/bin/codex`), official OpenAI
  API (`api.openai.com`, Responses API). Driver mode
  `TURNKEEP_CODEX_MODE=official` in `benchmarks/live/codex_driver.py`;
  no bridge base URL, no bridge key in this mode.
- Model: `gpt-6-luna`, reasoning effort `low` (frozen for the campaign).
- Auth: Codex CLI's own `~/.codex/auth.json` (mode 0600). The key value is
  NEVER read, printed, copied, pasted, or stored in this repo by any
  tooling. On 401/403 the run stops and reports; the key is never inspected.
- Smoke test 2026-11-09 ~22:52 GMT: trivial call returned `OK`, 12,657
  tokens, ~17 s. Rollout verified: model_provider `openai`, zero bridge
  references (no `8322`, no `127.0.0.1`). WebSocket transport fails in this
  sandbox (no `Connection: upgrade` header) and codex falls back to HTTPS;
  each call pays ~10 s of reconnect retries — accepted overhead.
- Suites: same frozen `live-pilot` (48 attempts) and `live-confirmation`
  (432 attempts) as the other campaigns, same scorer. Only the driver/model
  differ.

## Code-mode-host fix (2026-11-09 ~22:59 GMT, before the pilot)

- GPT-6 Luna engages Codex "Code Mode" for tool execution, which needs
  the companion binary `codex-code-mode-host` as a sibling of the invoked
  codex binary. The image's `/opt/hatch-image/bin` is read-only and ships
  no host binary, so the first validation attempt failed on EVERY tool
  call ("failed to spawn code-mode host ... No such file or directory");
  disabling the host (`features.code_mode_host=false`) also fails closed.
  A bare text reply (the smoke test) does not need tools, so it passed
  and masked the break.
- Fix (proven with a real write+verify probe): the official host binary
  for release `rust-v0.164.0-alpha.9` (matching CLI 0.164.0-alpha.9,
  downloaded from github.com/openai/codex releases, tarball size verified
  28,128,048 bytes; Sigstore bundle saved alongside) in
  `~/workspace/codex-bin/`, next to a COPY of the codex binary (306 MB,
  same version) plus `codex-resources` (bwrap). Official mode invokes
  that copy (`TURNKEEP_CODEX_BIN` overrides). A symlink does NOT work:
  codex resolves it to `/opt/hatch-image/bin` and looks there.

## Spend (Luna Standard rates, per 1M tokens)

- Input $0.10, cached input $0.01, cache write $0.125, output $0.50.
  Reasoning tokens bill as output. Over 272K input tokens per request the
  whole request reprices (2x input/cache, 1.5x output) — watch for it in
  long attempts.
- Budget: `state/eval-budget-codex-api.json`, authorised cap $25. Expected
  ~$10–16 for all 480 attempts (measured from the bridge pilot rerun:
  ~315k tokens/attempt, 99% input). Actual spend is tracked per attempt
  from the rollout usage records (input / cached / cache-write / output /
  reasoning split) and totalled in `SPEND.md`. If projected spend
  approaches the $25 cap, the run stops and reports.

## Separation from the other campaigns

- `campaign-20261108-T042` (bridge, Codex + GPT-5.6 Sol): pilot
  CONTAMINATED twice (M365 empty-answer outages 00:30Z and 18:31Z); its
  10/48 and partial numbers are VOID and are never quoted or reused here.
- `campaign-20261109-T042-agy` (agy + gemini-3.8-flash-low): CLEAN
  confirmation 352/432 (81.5%). Separate numbers, separate campaign.
- The three campaigns' numbers are never mixed. This campaign stands or
  falls on its own pilot + confirmation.

## Stop rules

- 3 consecutive quota/auth errors stop the run (runner-enforced).
- Empty-answer artifacts (rollout exists, 0 tool calls, 0 messages) are
  flagged `empty_answer` in the trace; if any appear they are deleted,
  recorded in a GAP file, and the aggregate is never quoted contaminated.

## Pilot result (2026-11-09 ~23:14 GMT, verified from summary.json)

- 48/48 attempts, **43 completed (89.6%)**, stale 10.4%, violations 0,
  refusals 0. Per arm: none 7/12 (stale 41.7%), markdown 12/12,
  log 12/12, turnkeep 12/12 (stale 0.0%). 0 empty answers, 0 errors,
  0 timeouts, 0 zero-step traces. CLEAN.
- Pilot spend: **$0.2225** (usage sums: input 2,877,122 / cached
  1,271,986 / cache-write 1,604,512 / output 18,398 / reasoning 3,844;
  fresh input only 624 tokens — the fixed prompt is cached after turn 1).
  Projected 480-attempt total at the pilot rate: **~$2.23**, far under the
  $10–16 estimate (which assumed uncached input) and the $25 cap.
- Correction: the first driver version flagged 6 quota/auth errors across
  5 pilot rows — all FALSE POSITIVES from bare-substring matching on
  stderr timestamps (`...22.429665`) and hex request digests
  (`...bef401980...`). Detection was changed to word boundaries
  (`\b429\b`, `\b401\b`, `\b403\b`); the 5 rows were re-scanned from
  their saved stderr and corrected to quota=0/auth=0. The driver fix is
  committed before the confirmation run, so it uses the corrected
  detection.

## Confirmation result (2026-11-10 ~00:19 GMT, verified from summary.json)

- 432/432 attempts, **354 completed (81.9%)**, stale 13.4%, violations 11,
  refusals 0, mean redo 0.90 steps. Per arm (each 108): none 46 (42.6%,
  stale 53.7%), markdown 105 (97.2%, stale 0.0%), log 105 (97.2%,
  stale 0.0%), **turnkeep 98 (90.7%, stale 0.0%, violations 11,
  mean redo 2.43 steps)**. 0 empty answers, 0 quota errors, 0 errors,
  0 timeouts, 0 zero-step traces. CLEAN.
- Confirmation spend: **$1.6773**; campaign total (pilot + confirmation,
  480 attempts): **$1.90** — see `SPEND.md` for the full split.
- Correction 2: one confirmation row (conf-f04-stable/turnkeep/seed47)
  was flagged `auth_error` by the word-boundary detection — another FALSE
  POSITIVE: the cause was the thousands-separated `tokens used` line
  (`17,401` matches `\b401\b`). Detection now uses full error phrases
  only (and strips the `tokens used` line before scanning); the row was
  re-scanned from its saved stderr and corrected to auth=0. The row
  completed normally (5 steps, deliverable exists). The confirmation run
  itself executed under commit b27968c (word-boundary detection); the
  phrase-based fix is committed with these results and documented here,
  so the manifest hash still matches the code that ran.
- Caveat (spend accounting): the trace `tokens_total` field (from codex's
  stderr "tokens used" line) is the LAST TURN's count only, not the
  session total — cost from the usage split in `SPEND.md`, never from
  `tokens_total`.
