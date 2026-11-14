# Known limitations — release candidate (T047 review copy)

Version 0.1.0, contract v0.1, OFFLINE_RC scope (Linux offline).
Last updated: 2026-11-10 (T026 + T042 VERIFIED per Sam; GPT R1 FAIL + 12-item rework DONE, R2 not yet sent).

This file ships with the release candidate. It reproduces the full honest
list from `docs/KNOWN_LIMITATIONS.md` (§1–§12, nothing removed) and adds
the limitations the T047 independent review confirmed or newly found
(§13–§17). Each item states what is NOT true, what the evidence is, and
what would change the status. Nothing here is a roadmap promise —
"planned" means "not done", not "coming soon".

## 1. Real two-client handover — VERIFIED via accepted hybrid substitution (T026, Sam 2026-11-10 ~09:08 GMT)

- **Claim made (narrowly):** that two real host products — Codex CLI
  0.164.0-alpha.9 (via Copilot bridge, GPT-5.6 Sol) and agy 1.5.3
  (gemini-3.8-flash-low) — handed work over through Turnkeep in the
  reverse direction, per the T026 spec substitution clause (Sam
  2026-11-09 ~18:15 GMT: skip real Claude Code).
- **Fact:** agy reverse leg VERIFIED in `evidence/live-clients/
  run-20261109-01/` (commit f9e2786): smoke PASS, S3 + S3c complete,
  6 real MCP tool calls, finish receipt, hashes recomputed.
- **NOT claimed:** the Codex forward leg (S1/S2/S3b/S4) was NOT run —
  the M365 backend returned empty answers from ~18:31Z. Codex exec does
  not surface MCP tools at all (proven), so its leg must use the CLI
  wrapper; adapter templates are broken on this VM (system python3 has
  no `turnkeep`/`mcp`, only the repo `.venv` does). The strict
  `run_gate --profile live-clients` remains NOT_RUN (claude CLI absent);
  the VERIFIED marking rests on Sam's decision, not on the strict gate.
- **What exists:** adapter templates (config PASS), a frozen fixture
  (`fixtures/live-handoff/export-format.json`), a manual recipe in
  `docs/COMPATIBILITY.md`, and MCP protocol evidence from the official
  SDK test client (12/12) plus `mcp_smoke.py`. Two SDK test processes
  are never a substitute for two host products. The T033 "J02 ui-api
  journey PASS" is two principals sharing one database through the HTTP
  API — useful UI evidence, not two real MCP host products.
- **What changes it:** a real Codex-forward-leg run (resume:
  `scripts/run_t026_handover.py --from S1` after a trivial `codex exec`
  returns real text), plus a strict two-MCP-client run if Codex ever
  exposes MCP tools.

## 2. Live-model evaluation — VERIFIED (T042, Sam 2026-11-10 ~09:08 GMT)

- **Claim made:** two independent clean live evaluations, same pattern.
  Codex API route (GPT-6 Luna, low effort, official OpenAI API): pilot
  CLEAN 43/48 (89.6%), confirmation CLEAN 354/432 (81.9%) — none
  46/108 (stale 53.7%), markdown 105/108, log 105/108, turnkeep 98/108
  (stale 0.0%, violations 11, redo 2.43); total spend $1.90 for all 480
  attempts. agy route (gemini-3.8-flash-low): pilot CLEAN 43/48,
  confirmation CLEAN 352/432 (81.5%) — turnkeep 95/108 (stale 0.0%,
  violations 18, redo 21.69); $0 new spend. `run_gate --task T042`
  PASS (09:12). Full account: `docs/EVALUATION_RESULTS.md`.
- **FINDING IS MIXED (no blanket win claim):** the turnkeep arm used
  0% stale facts on both runs, but its completion rate is below the
  file arms (codex-api 90.7% vs 97.2%; agy 88.0% vs 99.1%/97.2%).
- **NOT claimed:** the Copilot-bridge Codex + GPT-5.6 Sol route —
  pilot contaminated twice by M365 outages (re-run 17/27 valid),
  confirmation NOT run, never quoted as a result. Two-client comparison
  and competitor arms B3/B4 remain NOT_RUN.
- **What exists (offline only):** the frozen Gate C protocol
  (`benchmarks/PROTOCOL.md`), a deterministic smoke harness (12
  scripted attempts, labelled `SCRIPTED_SIMULATION_NO_MODEL_CALLS`:
  none 1/3, markdown 3/3, log 3/3, turnkeep 3/3), and 25 frozen
  change-replay scenarios against the real core (25/25 PASS;
  mutations no-taint 21/25, silent-overwrite 22/25; false_refusals=2,
  reported separately, not hidden).

## 3. Windows / macOS — NOT_RUN (T043)

- **Claim NOT made:** that Turnkeep installs or runs on Windows or
  macOS.
- **Fact:** no Windows or macOS runner exists on this VM. The
  installers (`scripts/install.sh`, `scripts/install.ps1`) and the CI
  workflow (`.github/workflows/test.yml`) exist, but a file is not
  evidence of a run. Platform path rules are unit-tested with a
  monkeypatched `platform.system()` — that is not platform evidence.
- **Verified:** Linux only (this VM: Linux 7.0.0-43-generic, 4 CPUs,
  Python 3.12.3 / SQLite 3.45.1): doctor PASS, J01 + J05 journeys
  PASS, paths with spaces and Chinese characters PASS, UI production
  gate PASS. Re-confirmed by the T047 reviewer re-run (platform
  sub-gate PASS).
- **What changes it:** a real install + doctor + J01/J05 run on each
  platform, recorded in `evidence/platforms/`.
- **Installers (2026-11-10 rework, P1-f):** install.sh now installs the
  wheel (`pip install -e .`) and runs doctor via the installed `turnkeep`
  binary — verified on Linux in a temp checkout copy (exit 0, `turnkeep
  --help`, `doctor --json` PASS, `remember` round-trip). install.ps1 has
  the same change but was NOT executed (no PowerShell on this VM) — a
  real Windows install run is still part of T043.

## 4. Performance gate — FAIL (T043)

Measured, not promised. Hardware: Linux 7.0.0-43-generic, 4 CPUs,
7.9 GB RAM, Python 3.12.3 / SQLite 3.45.1. Datasets 1k / 10k / 100k
(seeds 42 / 1729 / 20261108), pre-registered thresholds (finish p95
≤ 500 ms, recall p95 ≤ 1,000 ms). Full numbers:
`evidence/platforms/perf.json`.

| Dataset | Recall p50 / p95 | Finish p50 / p95 | Verdict |
|---|---|---|---|
| 1k records | 25 ms / 76 ms | 218 ms / 442 ms | PASS |
| 10k records | 251 ms / 350 ms | 1,924 ms / **7,760 ms** | **FAIL** (finish p95) |
| 100k records | 2,917 ms / **3,183 ms** | 12,275 ms / **12,288 ms** | **FAIL** (recall + finish p95) |

Cold start 159 ms (PASS). 8 concurrent writers → 1 winner,
7 `VERSION_CONFLICT`, 0 busy errors.

- **Causes (architectural, correctness-first by design):**
  1. `begin()` snapshots **all** active records into `snapshot_json`
     (O(n) per run).
  2. `recall()` loads the whole `memories` table and filters in Python
     (O(n) per query).
- **Follow-up (not done):** snapshot only the read set; indexed recall.
- **What was NOT done to pass:** thresholds were not lowered and
  datasets were not shrunk. The gate stays FAIL until a re-run on the
  same manifest passes.
- **Reviewer re-measurement (T047, 2026-11-08):** an independent re-run
  failed the same 3 thresholds with different absolute numbers
  (finish p95 3,470 ms at 10k; recall p95 2,896 ms and finish p95
  15,231 ms at 100k). Absolute timings are load-dependent; the FAIL
  verdict is robust, not a one-off slow run. The frozen numbers above
  remain the evidence of record.
- **Practical consequence:** at 1k records the product is interactive;
  at 10k a finish takes seconds; at 100k a recall takes ~3 s. Do not
  use this release for large scopes expecting interactive latency.

## 5. External-action atomicity — NOT_SUPPORTED

- A finish receipt proves only that the local SQLite database accepted
  the commit (`guarantee: "local_sqlite_commit_only"`). It does not
  make an email, a deployment, a payment or any other external action
  atomic, and it does not prove the external world changed.
- The product never claims otherwise; any UI/docs wording that implies
  external atomicity is a bug, not a feature.

## 6. Name and trademark — NOT_RUN

- "Turnkeep" is a working name only. Checked 2026-11-08: PyPI JSON API
  404, npm registry 404, GitHub user 404 — but
  [turnkeep.app](https://turnkeep.app/) is a live, same-spelled
  short-term-rental turnover product in a different market. Search
  confusion is real; the domain is taken.
- Trademark clearance has **not** run (needs a legal check, out of
  scope). Do not buy domains, file trademarks, or claim name ownership
  on this name. Re-check PyPI / npm / GitHub + the turnkeep.app
  collision before any public release; a rename (e.g. package prefix
  `turnkeep-memory`) is still on the table. Full note:
  `docs/NAME_CHECK.md`.

## 7. Single trusted OS user — no multi-tenancy, no auth boundary

- Scope is a correctness boundary (which project's records a run may
  see), not a security boundary against another local user who can
  read the database file. The workbench token protects a loopback HTTP
  server on one machine; it is not user authentication.
- Actor is a label bound at server/CLI start; a request body cannot
  rebind it, but anyone who can run the CLI against the DB file can
  act as any actor.
- Do not run Turnkeep as a shared service for untrusted users.

## 8. Erasure has residuals — by design, and reported

- `erase` scrubs the live DB (tombstone, WAL checkpoint + VACUUM,
  verified by scanning every text column) but cannot delete backups,
  exports, old DB copies, or another client's copy of a record it
  already read. `turnkeep residuals` lists them; it never claims they
  are deleted.
- Restoring a backup taken **before** an erasure resurrects the erased
  payload — expected point-in-time behaviour, not a bug.
- If the payload is also in the append-only `events` table, erasure
  returns `PARTIAL` and leaves the record untouched — history is never
  rewritten. See `docs/retention.md`.
- `retract` is a **logical** state change only: the record stops being
  returned by default recall, but its payload remains in history.
  `retract` is **not** erasure.
- **Residual reporting (2026-11-10 rework, P0-3):** `residual_report()`
  now reports `CONTROLLED_NOT_CLEAN` when a controlled table still holds
  a payload (previously it hardcoded `live_tables_clean=true` without
  scanning). The scan checks every erased record holds the tombstone
  and every receipt referencing one holds the tombstone (or no value).

## 9. Retrieval is lexical, not semantic

- Recall is scope-first filtering + FTS/lexical ranking (with a
  Chinese token fallback). There is no embedding, no vector search,
  no semantic similarity merge. Same-key conflicts are explicit;
  same-meaning-different-key conflicts are **not** detected. Do not
  describe recall as understanding meaning.
- Budgets count **characters** (and an estimated token count labelled
  as an estimate), never `len()` passed off as exact tokens.

## 10. What the demo does NOT show

- The zero-key demo is a scripted simulation
  (`SCRIPTED_SIMULATION_NO_MODEL_CALLS`). It proves local SQLite
  correctness only — not MCP integration, not model quality, not
  external-action safety, not a real client recording.

## 11. Neighbour and evaluation honesty

- Turnkeep is not the first cross-tool shared memory (Turnbook,
  Truenex Memory, project-memory already do it) and versioned memory,
  MVCC, CAS, evidence chains and undo all have prior art. The retained
  bet — the combination of change-recovery handover + low-threshold
  local experience + replayable evaluation in one product — is a
  hypothesis to be tested (T042), not a proven lead. If a fair trial
  shows no benefit vs plain Markdown + original logs, the honest move
  recorded in `docs/COMPETITORS.md` is to shrink to an adapter layer
  or workbench over the better neighbour.
- No retention, GitHub-star, or acquisition claims exist or are made:
  all are NOT_ESTABLISHED. No growth is manufactured (no bought stars,
  bots, or fake users).

## 12. License / SBOM / security boundary — checked (T045 VERIFIED)

- Code is MIT (see `LICENSE` and `NOTICE`). Dependency list is in
  `release/SBOM.json` (103 components: 33 Python + 70 Node, all
  permissive licences, no GPL/AGPL). Secret/boundary scan is
  `scripts/release_checks.py --profile license-and-secrets` (verdict
  PASS at T045: 363 files, 0 FAIL; re-run by the T047 reviewer:
  PASS, 374 files, 0 FAIL, 303 INFO, 14 whitelisted fakes — see
  `SECURITY.md`).
- Limits that remain: no dependency vulnerability scan has run
  (pip-audit / npm audit NOT_RUN — see `SECURITY.md` §2); a PASS from
  the pattern scan is not a claim of production safety; `requirements/ui.lock`
  is intentionally empty (R0 design — UI deps are pinned in
  `ui/package-lock.json`, which is what the SBOM uses).

## 13. Installed-wheel demo scenarios fail (T046 known gap, confirmed by T047)

- **Fact:** the README 60-second demo commands
  `python -m turnkeep demo --scenario export-format --no-model` and
  `--scenario research-correction` **fail from the installed wheel**
  (`FileNotFoundError`): `demo.py` resolves fixtures via
  `Path(__file__).parents[2]`, which is the repo root in a source
  checkout but `lib/python3.12` when installed — a location no wheel
  layout can populate. The T047 reviewer confirmed this live:
  `verify_release.py`'s installed-wheel probe records it as a note
  in both the `--manifest` and the `--claims` runs.
- **Not affected:** the default J01 scenario (the actual J01 journey)
  needs no fixtures and **passes** from the installed wheel
  (confirmed by the same runs, including a CLI remember/begin/finish
  round trip ending in a real receipt). A source checkout — including
  one unpacked from the shipped sdist, which contains the fixtures —
  runs both `--scenario` demos.
- **What changes it:** load the fixtures via `importlib.resources`
  in `demo.py` (core-owned; outside T046's paths). Until then, do not
  present the `--scenario` demos as working from a wheel install.
  Evidence: `release/artifacts/manifest.json`
  `known_gaps.demo_fixtures_installed`.

## 14. Shipped manifest records the previous commit, by design (D-R5-2)

- **Fact:** `release/artifacts/manifest.json` records commit
  `7391e83`, while the tree it was committed in is at HEAD `24111b9`.
  `verify_release.py --manifest` therefore reports exactly one failure
  (commit mismatch) even though all 10 other checks pass. This is the
  documented by-design lag: the manifest records the HEAD it was built
  from, and committing the artifacts moves HEAD. It is **not** a defect
  and must not be "fixed" by weakening the commit check.
- **What changes it:** T048 rebuilds the artifacts at packaging time
  so the shipped manifest matches the shipped tree; the packaging
  commit itself is recorded separately in
  `release/DELIVERY_MANIFEST.json`.

## 15. `build_release.py --clean` deletes the old artifacts before checking the tree (T047 finding F2)

- **Fact:** during the T047 review re-run, `build_release.py --clean`
  deleted all files under `release/artifacts/` and *then* refused
  ("tracked files are modified; commit or revert before building").
  The reviewer restored the artifacts from git and re-verified every
  sha256 against the manifest (all match). No data was lost, but a
  `--clean` build on a dirty tree destroys the previous candidate
  before refusing.
- **Consequence for you:** never run a `--clean` build on a dirty
  tree. The follow-up (T046-owned) is to check the tree *before*
  deleting the output directory.

## 16. Dependency vulnerability scan — NOT_RUN

- **Fact:** no `pip-audit` / `npm audit` run is recorded. Pinned
  versions are listed in `release/SBOM.json`; known CVEs in those
  versions were not assessed.
- **What changes it:** run a vulnerability scan before any public
  release. A PASS from `release_checks.py` (a pattern scan for
  secrets and boundary violations) is not a claim of production
  safety and does not cover known CVEs.

## 17. What this release candidate IS — the grade

- **OFFLINE_RC (Linux offline) only.** Reference core: ready. Offline
  release candidate: ready. Integrated release candidate (two real
  clients): READY — T026 VERIFIED via the hybrid substitution Sam
  accepted (Codex-via-CLI + agy-via-MCP; Codex forward leg not run,
  disclosed; strict live-clients gate remains NOT_RUN). Value
  evaluated (real model): READY — T042 VERIFIED via two clean live
  runs (finding MIXED: 0% stale, completion below files).
  Public-release conditions: NOT MET — missing cross-platform
  (T043 Windows/macOS NOT_RUN, perf gate FAIL) + trademark clearance
  NOT_RUN + vulnerability scan NOT_RUN.
- Full grading with evidence citations: `evidence/FINAL_REVIEW.md` §7
  and `evidence/GATE_MATRIX.json`.

## 18. GPT final-review R1 rework — DONE 2026-11-10 (R2 not yet sent)

- **What:** ChatGPT final review R1 (GPT-6 + Extra High, 2026-11-09
  ~01:15 GMT) returned FINAL_VERDICT: FAIL (5 P0 / 8 P1 / 6 P2). Muse
  independently verified all 5 P0 + 7 P1 as genuine
  (`~/workspace/turnkeep-gpt-review-verification-2026-11-09.md` §7);
  Sam authorised the fix 2026-11-10 ~17:29 GMT. All 12 items are fixed
  in this checkout, each with regression tests; full pytest **276/276**
  (234 baseline + 42 new). The report's P0-1 / P0-2 / P0-4 reproductions
  were re-run against the fixed code and are closed.
- **Fixed:** P0-1 erase taints descendants + recall applies usable();
  P0-2 import writes topologically sorted, planned-parent edge drops fail
  the import, quarantined-parent drops are plan warnings; P0-3 needles
  expand recursively + residual_report() scans for real (new status
  CONTROLLED_NOT_CLEAN); P0-4 recursive redaction in briefs/handoff/UI
  (the wrong PASS assertion at evidence/FINAL_REVIEW.md:274 is annotated
  SUPERSEDED — WRONG); P0-5 post-import integrity check (repairs recorded
  in the plan); P1-a finish digest covers sorted parent_ids (v2; v1
  legacy digests still replay, so pre-fix runs are unaffected);
  P1-b rebase exhaustion persists a tainted handoff record +
  rebase_exhausted event; P1-c source-id conflicts error (INVALID_SOURCE)
  + per-record provenance (schema 002); P1-d transport policy wired into
  MCP/HTTP/CLI (forged actor → 403 FORBIDDEN); P1-e atomic first
  migration (no executescript()) + journal_mode=WAL retry on the
  first-open lock race; P1-f installers install the wheel (Linux
  verified; install.ps1 still unexecuted — see §3); P1-g every state
  change cites its triggering event id (cause_event_id).
- **Schema v2 consequence:** SCHEMA_VERSION is now 2 (migration
  002_record_provenance.sql adds record_provenance). v1 databases still
  upgrade on open (002 applies, data preserved). v1 *backups* do NOT
  restore under v2: backup.py refuses a version mismatch by design —
  a v1 backup must be restored with the v1 code, or opened (upgraded)
  rather than restored.
- **First-open race, verified fixed:** the P1-e flake
  (test_two_clients_see_same_records) failed 3/5 isolated runs before
  the fix. After the journal_mode retry fix it passed 20/20 isolated
  (together with the new concurrent-first-open test), and the full suite
  ran 276/276 in 6 of 7 runs — one run was 275/276 with a single
  unidentified transient failure (failing test not captured; no
  recurrence in the following 3 runs).
- **Grades unchanged by this rework:** INTEGRATED_RC_READY READY and
  VALUE_EVALUATED READY still rest on Sam's T026/T042 decisions on the
  disclosed basis; PUBLIC_CANDIDATE stays NOT MET. Re-grading is the
  R2 reviewer's job — final review R2 has been prepared but NOT sent.
- **NOT "fixed" (verified wrong, clarified only in the R2 message):**
  GPT's claim the RC backlog "belongs to Agent Project Factory
  (P001–P048)" (files are Turnkeep T001–T048); DELIVERY_MANIFEST
  archive_sha256 null inside the zip (self-referential by design);
  GPT's "pytest 213" (its environment; local suite is 276/276).

## 19. GPT final-review R2 rework — DONE 2026-11-10 (R3 not yet sent)

- **Verdict:** R2 FINAL_VERDICT: FAIL (GPT-6 + Extra High, 18m57s thinking). 2 new P0s + 4 partially-closed items + 5 new P1s + 2 P2s, all verified true against the code; 11 rework items, all fixed.
- **P0-R2-02:** `_looks_secret()` no longer has the `len < 5000` gate — a structured secret of any length is now excluded from exports.
- **P0-R2-01 / P0-3:** `residual_report()` persists each erase outcome (status + remaining table/record-key + needle hash, never the value) and reports `CONTROLLED_NOT_CLEAN` while any unresolved controlled residual exists. `live_tables_clean` now means "tombstone structure clean AND no unresolved controlled residual". Honest limit: a residual can only be detected for needles recorded at erase time. (Since the R4 rework the persisted hashes are HMAC-SHA256 under a per-scope key, not raw SHA-256 — see §21.)
- **P1-R2-03:** imported records cite the import event id; `set_expiry()` bumps revision and cites its `expire_set` event.
- **P1-R2-01 / P1-b:** quarantine record is truthful (explicit "no candidate result was produced or saved") and singular (one row per run). The epoch bump on rebase is BY DESIGN (attempts are counted via rebase events).
- **P1-R2-02 / P1-c:** Markdown importer persists provenance (`md:<file>#<heading>:<content_hash>`).
- **P1-R2-04:** `Idempotency-Key` header (8–128 chars, required) enforced on all six POST routes; core + MCP + CLI thread the key through. Spec paths aligned to the implemented routes (`/api/v1/remember`, …).
- **P1-R2-05:** README and HANDOFF.md state T026/T042 as VERIFIED with disclosed limits; the zip's `VALUE_EVALUATED` grade is summary-only — raw traces live in the source checkout.
- **P1-f:** `install.sh` works offline (installs the shipped wheel); `install.ps1` still NOT_RUN on real Windows (T043).
- **P2s:** brief budget counts the structured sections; the 9 environment-coupled tests are now portable.
- **Tests:** full pytest **304/304** (276 + 28 new), run twice with no transient failure.

## 20. GPT final-review R3 rework — DONE 2026-11-11 (R4 not yet sent)

- **Verdict:** R3 FINAL_VERDICT: FAIL (GPT-6 + Extra High, 12m57s thinking). 3 new P0s + 1 P1 (T015 candidate persistence) + 1 P2 (BrokenPipeError on Python 3.13) + wording leftovers, all verified true against the code; rework items below, all fixed.
- **P0-R3-01:** erasure used to scrub only receipts shaped `{"record_id": ...}` (finish receipts); remember / correct / retract / resolve_conflict receipts store `dump_record()` output (`{"id": ..., "value": ...}`) and begin receipts nest record dumps inside `"records"` lists, so those copies survived erasure and the erase came back PARTIAL with no way to complete it. The scrub now recognises dumps by owner id in either shape, including nested dumps; a second `erase_record()` on an already-erased record re-runs the scrub + scan for that record (repair: no resurrection, no second revision bump). Receipt residuals are now attributed to the owning record id / key (previously `None` / `None`).
- **P0-R3-02:** erase / retract reasons (and `set_expiry`'s `valid_until`) are checked against the record's payload needles BEFORE any event is published; a reason containing the payload is rejected (`INVALID_INPUT`) and nothing is published. Already-leaked reasons in existing DBs cannot be scrubbed (append-only) — recovery is export → import into a fresh DB → delete the old DB AND the export package (its `events.jsonl` still holds the leaked reason).
- **P0-R3-03:** the wheel/sdist in the R3 ZIP were NOT built from the R3 source — 15 files differed; the wheel was built 2026-11-10 18:21, before the R2 rework even started. Root cause: `build_release.py` refuses on a dirty tree, and the R2/R3 "do not commit after the rebuild" discipline left the rework uncommitted, so the build exited 1 and packaging proceeded anyway with the stale 18:21 artifacts (the zip shipped new `src/` + old wheel + an 18:21 manifest claiming `tracked_tree_clean: True`). Fixed by committing the rework FIRST, then rebuilding; plus durable guards — `verify_release.py` and `package_handoff.py` now unpack the wheel (and sdist) and diff every `.py` against `src/turnkeep/`, failing/refusing on any difference, and `package_handoff.py` re-evaluates tree cleanliness at packaging time instead of trusting the manifest's stale `tracked_tree_clean`.
- **P1 (T015):** candidate persistence implemented — `rebase(..., candidate_result=...)` (threaded through SDK, CLI, web), persisted only on exhaustion as not-default-recallable state, with a follow-up run (`quarantine-followup-<run_id>`) as the independent task and a `get_quarantine()` read path (candidate + next action checkable after exhaustion). No candidate supplied keeps the truthful "no candidate result was produced or saved" marker.
- **P2:** `_read_json` now drains the request body before rejecting oversized requests (Python 3.13 `BrokenPipeError`); verified on Python 3.12 only on this VM (no 3.13 here).
- **Extra fix found during verification (concurrent first-open race):** `_precheck` read `user_version` and the table list as two separate statements, so a migrator committing between them made a fresh DB look like an "Unrecognised existing database" (spurious refusal, ~1 run in 7 of `test_concurrent_first_open_all_succeed`). Both reads now run inside one deferred read transaction (single snapshot). Post-fix: the test passes 50/50.
- **Wording:** `HANDOFF.md:22` no longer cites the stale R5 commit `73b0f93`; `release/FINAL_HANDOFF.md:223` updated to the current round; `HANDOFF.md:21` clarifies the strict run_gate NOT_RUN refers to the strict gates, not the VERIFIED T026/T042 tasks.
- **Tests:** full pytest **339/339** (305 + 34 new), run twice with no transient failure.

## 21. GPT final-review R4 rework — DONE 2026-11-11 (R5 not yet sent)

- **Verdict:** R4 FINAL_VERDICT: FAIL (GPT-6 + Extra High, 12m39s thinking). 3 new P0s + 3 new P1s + P2s, all verified true against the code (report: `~/workspace/turnkeep-gpt-R4-verification-2026-11-11.md`); 10 rework items, all fixed.
- **P0-R4-01:** the reason check rejected a needle only on exact equality or a ≥ 4-char substring, so 1–3 char secrets leaked into append-only events via the reason. The check now also rejects a needle appearing as a standalone token in the reason, whatever its length ("abc" in "token abc rotated" refused; "x" inside "box" still accepted). Shared by erase / retract / `set_expiry`. Prevention only — a leak already in an existing DB cannot be scrubbed (append-only; §8 remedy).
- **P0-R4-02:** a second erase of an already-erased record checked its reason against an empty needle set (receipts already scrubbed) and re-leaked the secret. The repair path now also vets the reason token-by-token against the needle hashes the first erase persisted in its event (HMAC for new erasures, raw SHA-256 for pre-fix ones). Residual risk (documented in §8): a secret glued into surrounding text with no token boundary still slips through.
- **P0-R4-04:** erase events carried raw SHA-256 needle hashes, so a 4-digit PIN was recoverable by enumerating 0000–9999 against an exported `events.jsonl` alone. Hashes are now HMAC-SHA256 keyed by a per-scope secret in the new `scope_secrets` table (migration 003, schema v3; the key never leaves the live DB and is never exported). Events carry no recoverable material, same as receipts. Pre-fix events already exported still carry raw hashes and stay enumerable — the fix protects new exports only. The HMAC approach was chosen over dropping the hashes because the P0-R4-02 repair check needs them.
- **P1-R4-03:** the follow-up run was created by `begin()` BEFORE the `rebase_exhausted` event bumped the epoch, so it was born stale and `finish()` on it raised STALE_SNAPSHOT. The follow-up id is now pre-generated, the quarantine record + event are written in one transaction naming it, and `begin(run_id=...)` runs after the transaction — the run is born at the post-event epoch and finishes with no other writes. Window (documented in `recovery.py`): if `begin()` fails after the transaction commits, the quarantine names a not-yet-existing run; one retry is attempted, and a later exhausted call retries the begin from the stored id.
- **P1-R4-05:** the quarantine lookup ran outside the write transaction and `begin()` was called with no idempotency key, so two concurrent exhaustions created 2 quarantine records + 2 follow-up runs. The lookup now lives inside the write transaction (the second writer blocks, then reuses the first writer's record) and the follow-up run is created with idempotency key `quarantine-followup-<run_id>`. A partial unique index was NOT added: the same key can legitimately recur among tainted rows, and widening `one_active_value_per_slot` to tainted rows would break that.
- **P1-R4-06:** `_scan_payload` never escaped LIKE wildcards, so a payload of "%" or "_" matched every text cell and erasure was wrongly refused. It now escapes `\`, `%`, `_` with an explicit `ESCAPE '\'` clause. Two adjacent defects surfaced while fixing it and are fixed in the same rework: the scan covers payload-bearing columns only (structural enum text such as `source_type` "user_statement" contains '_'), and the erased record's own tombstone row is excluded from the post-scrub scan (the marker `{"_erased": true}` contains '_' and "true"). Honest limit (see §8): a 1-char payload colliding with a structural JSON key name (e.g. "e" in "key") still reports PARTIAL, fail-closed.
- **P2s:** wheel/sdist-vs-source guards now compare every file under `src/turnkeep/` (37 .py + 3 .sql), not `.py` only, in both `verify_release.py` and `package_handoff.py`; `pyproject.toml` declares `[tool.setuptools.package-data] turnkeep = ["migrations/*.sql"]` so a plain `python -m build` from the sdist produces an equivalent wheel (the build script's injection stays as a no-op safety net; touching the T004-owned file is justified in the commit message because build reproducibility is a release property); `verify_release` records the commit check as `not_run` instead of silently skipping it when there is no `.git` (extracted package: 12 checks + 1 not_run); the handoff docs' HEAD lines are stamped at packaging time from the manifest head (`HEAD@PACKAGING` token in the repo copies), so the shipped docs can no longer go stale.
- **Tests:** full pytest **362/362** (339 + 23 new), run twice with no transient failure. GPT's R4 repros re-run as `/tmp/repro_r4_fixed.py` (all 14 checks PASS) and as regression tests: short-needle tokens, second-erase re-leak, PIN enumeration from the export, follow-up-run finish, concurrent exhaustion (1 quarantine + 1 follow-up), wildcard values, mutated-.sql guard, sdist rebuild, commit-check not_run, HEAD stamping — plus the end-to-end privacy chain (first erase → second erase → residual report → history export → idempotent receipts → low-entropy recovery) on one chain.
- **NOT "fixed" (verified overstated, clarified only in the R5 message):** P0-R4-04's cited conflict (`erasure.py:47-48`) is about receipts, not events — the real conflict is with `_needle_hashes`' own docstring; the build-reproducibility caveat (C9) was understated, not overstated — a plain rebuild from the sdist provably dropped the migration SQL, it was not merely unproven; the docs staleness (C8) was understated — four more stale lines than GPT named. The 1-char structural-key limit above is new, found during this rework, not claimed by GPT.
