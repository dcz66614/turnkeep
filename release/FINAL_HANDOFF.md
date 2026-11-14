# Turnkeep — Final Handoff (R5 complete, local release candidate)

Version 0.1.0, contract v0.1. Head at packaging:
`HEAD@PACKAGING` (stamped at packaging time from the delivery manifest;
the repo copy of this file carries the token, the shipped copy the real
head). Date: 2026-11-10.
This is the last task of the project (T048). Drive + GitHub upload of
this release candidate was authorised by Sam 2026-11-10 ~09:08 GMT
(private repo turnkeep; no public release). Nothing was
posted, messaged, or logged in to; the only spend is the $1.90 T042
live-model evaluation Sam authorised.

## The grade (T047 independent final review, evidence/FINAL_REVIEW.md)

| Grade | Verdict |
|---|---|
| REFERENCE_CORE_READY | **READY** |
| OFFLINE_RC_READY (Linux offline only) | **READY** |
| INTEGRATED_RC_READY (two real clients) | **READY** (T026, hybrid accepted — see below) |
| VALUE_EVALUATED (real model) | **READY** (T042, finding MIXED — see below) |
| Public-release conditions | **NOT MET** |

Statuses are not interchangeable: a PASS in one gate never stands in
for another gate's PASS. Full gate-by-gate evidence (21 gates) is in
`evidence-summary.json` inside the zip and in `evidence/GATE_MATRIX.json`
in the source checkout.

## What is genuinely done (47 of 48 tasks VERIFIED)

- Core (T007–T018): explicit-key remember with conflicts (no silent
  overwrite), CAS correct/retract, full-candidate resolve, AND-parent
  lineage with taint propagation, server-side snapshot begin, atomic
  finish with idempotent receipt (local SQLite commit only), resume/diff
  with rebase max 2 then quarantine, scope-first lexical recall with
  budgets, handover briefs with secrets redacted. Full suite 234/234
  pytest at the T047 re-run.
- Protocol (T019–T023): CLI, Python SDK, MCP server with 9 tools via
  the official MCP Python SDK 2.3.0, conformance + parallel-client
  checks; `run_gate --profile core` PASS, `--profile protocol` PASS.
- Workbench + UI (T027–T035): loopback-only local HTTP API (15 paths,
  per-process session token), browser UI, journeys J01/J03/J04/J05
  PASS, UI e2e 11/11 on dev and production builds.
- Data (T036–T039): export/import v1 (JSONL + manifest, deterministic
  re-import is a no-op), structured-only Markdown import, expiry,
  erasure with residual report, online backup/restore/diagnose.
- Deterministic evaluation (T040–T041): Gate C smoke, 12 scripted
  attempts; 25 frozen change-replay scenarios, real core 25/25 PASS
  (mutations no-taint 21/25, silent-overwrite 22/25;
  false_refusals=2, reported separately).
- Docs / licence / build (T044–T046): honest README (English +
  Chinese), KNOWN_LIMITATIONS, MIT licence, SBOM (103 components, all
  permissive), release_checks PASS, reproducible build (two builds
  bit-for-bit), wheel + sdist + UI tarball + locks + SHA256SUMS +
  manifest in `release/artifacts/`. The artifact manifest commit
  matches the packaged HEAD (rebuilt at packaging time, D-R5-2);
  `verify_release.py --manifest` exit 0, 13/13 checks.

## What is NOT done — the one blocked task (T043); T026/T042 verified with disclosed limits

- **T026 live two-client handover — VERIFIED (hybrid substitution
  accepted by Sam 2026-11-10 ~09:08 GMT).** Pair: Codex CLI
  0.164.0-alpha.9 (bridge, GPT-5.6 Sol) + agy 1.5.3
  (gemini-3.8-flash-low); the agy reverse leg is VERIFIED (smoke PASS,
  S3+S3c complete, 6 real MCP calls, finish receipt; commit f9e2786).
  Disclosed: the Codex forward leg (S1/S2/S3b/S4) was NOT run (M365
  empty-answer outage from ~18:31Z); Codex exec does not surface MCP
  tools, so its leg must use the CLI wrapper. The strict
  live-clients gate remains NOT_RUN.
- **T042 live-model evaluation — VERIFIED (Sam 2026-11-10 ~09:08 GMT).**
  Two independent clean live runs: codex-api (GPT-6 Luna low, official
  OpenAI API) pilot 43/48, confirmation 354/432 (81.9%); agy pilot
  43/48, confirmation 352/432 (81.5%). Spend $1.90 for all 480
  attempts. Finding **MIXED**: the turnkeep arm had 0% stale facts on
  both clean runs, but completion below the file arms (codex-api 90.7%
  vs 97.2%; agy 88.0% vs 99.1%/97.2%) — graded on that basis, no
  blanket win claim. The contaminated bridge-Codex route is never
  quoted.
- **T043 platforms + perf — BLOCKED_EXTERNAL, perf gate FAIL.**
  Windows/macOS install NOT_RUN (no runners; CI workflow exists but
  never ran). Performance gate FAIL on 3 pre-registered thresholds
  (frozen evidence of record): finish p95 **7,760 ms at 10k records**,
  recall p95 **3,183 ms** + finish p95 **12,288 ms at 100k records**
  (thresholds: finish 500 ms, recall 1,000 ms). Cold start 159 ms
  PASS; 1k records PASS (finish p95 442 ms, recall p95 76 ms).
  Causes: `begin()` full-snapshot O(n), `recall()` full-table scan
  O(n). Thresholds were not lowered, datasets were not shrunk.
  A coordinator re-measurement failed the same 3 thresholds with
  different absolute numbers (finish p95 5,116 ms at 10k; recall p95
  2,875 ms + finish p95 12,631 ms at 100k) — the FAIL verdict is
  robust, not a one-off slow run. The frozen numbers above remain the
  evidence of record.

## Other hard limits (full list: release/KNOWN_LIMITATIONS.md, §1–§17)

- External-action atomicity NOT_SUPPORTED: a receipt proves only the
  local SQLite commit. `retract` is logical, not erasure; restoring a
  pre-erasure backup resurrects the payload (expected point-in-time
  behaviour).
- Trademark clearance NOT_RUN: "Turnkeep" is a working name only;
  turnkeep.app is a live same-spelled rental product in a different
  market. No dependency vulnerability scan ran (pip-audit / npm audit
  NOT_RUN); release_checks PASS is a pattern scan, not production
  safety.
- Installed-wheel `--scenario export-format` / `research-correction`
  demos fail (fixture path resolution, known gap); the default J01
  scenario needs no fixtures and passes from the installed wheel.
- Single trusted OS user only; scope is a correctness boundary, not a
  security boundary. No multi-tenancy, no auth beyond the loopback
  workbench token.

## How to run from this package

Requires Python ≥ 3.11 (Linux verified; macOS/Windows not run).
No model API key, no database service, no Docker.

```sh
# From the unpacked zip (source checkout):
unzip TURNKEEP_RC.zip && cd turnkeep-0.1.0
bash scripts/install.sh
.venv/bin/python -m turnkeep doctor --json
.venv/bin/python -m turnkeep demo --no-model   # J01, zero-key, scripted
```

Or install the shipped wheel from `release/artifacts/` into a clean
venv (`pip install turnkeep-0.1.0-py3-none-any.whl`), then
`turnkeep --version` (0.1.0) and the same demo/CLI commands. The
60-second demo, the handover loop, MCP, workbench and data commands
are all documented in README.md (verified by
`scripts/test_documented_commands.py --offline`).

Verify this package before trusting it:

```sh
.venv/bin/python scripts/verify_handoff.py --archive release/TURNKEEP_RC.zip
```

It extracts to a clean temp dir, checks every listed file's sha256,
scans for machine paths/secrets/DB files, runs the J01 demo and a
CLI remember/begin/finish round trip from the extracted copy. Exit 0
only if everything passes.

If you have only the zip (no source checkout), verify it from the
extracted copy instead. The manifest shipped inside the zip has
`archive_sha256` null by design — a file cannot hash the archive
that contains it, so no sidecar manifest inside the zip could carry
the zip's own hash either. Compute the zip's sha256 yourself and
pass it with `--expected-sha256`:

```sh
unzip TURNKEEP_RC.zip && cd turnkeep-0.1.0
sha256sum ../TURNKEEP_RC.zip
python3 scripts/verify_handoff.py --archive ../TURNKEEP_RC.zip --expected-sha256 <sha256 from sha256sum>
```

Without `--expected-sha256` the extracted verifier fails check 2 and
its failure message names the flag. The flag only replaces the
manifest's `archive_sha256` for that one check; every other check
runs unchanged, and the in-zip null stays as designed.

## What is in the zip (and what is not)

In: source (`src/`), tests, scripts (incl. `package_handoff.py` /
`verify_handoff.py`), specs, docs, examples, fixtures, adapters,
benchmarks (protocol/scenarios; run outputs excluded), requirements
locks, UI source, `.github/workflows`, `release/artifacts/` (wheel,
sdist, UI tarball, locks, SBOM, SHA256SUMS, manifest),
`release/KNOWN_LIMITATIONS.md`, `release/FINAL_HANDOFF.md` (this
file), `release/DELIVERY_MANIFEST.json` (file list + sha256 + grading
+ gaps + next actions), `evidence-summary.json` (desensitised gate
summary), `tasks/backlog.json`, release announcement drafts
(draft-only, never posted).

Evidence qualification: the zip's `VALUE_EVALUATED` grade is **summary
only — raw traces in the source checkout**. The zip ships no
`live-evals/` or `live-clients/` raw files, so per-run T042 results
cannot be independently confirmed from the zip alone; the full
evidence lives in the source checkout (`evidence/live-evals/`,
`evidence/live-clients/`, `evidence/GATE_MATRIX.json`). Never claim
the strict T026 gate ran, or that the zip alone proves per-run T042
results.

Not in: `.venv`, `.git`, `__pycache__`, `node_modules`, `ui/dist`,
`*.db`/`*.sqlite*`, `state/` and `evidence/` raw files (they contain
machine paths under this machine's home directory — 289 INFO hits in the T045/T047
release_checks scans were all of this kind), `requirements/ui.lock`
(empty R0 placeholder, not a lock), `benchmarks/results/`. Two test
files are also excluded because they hardcoded this machine's absolute
work-dir path (`tests/core/test_finish_races.py`,
`tests/integration/test_concurrency.py`); they run only in the source
checkout, and the exclusion is recorded in
`release/DELIVERY_MANIFEST.json`. (R2 rework fixed the hardcoding —
the checkout root is now derived from the test file path — so the
reason for the exclusion no longer applies; whether they ship is a
packaging decision for the next rebuild.)

## Next actions (no credentials exist for any of these)

1. **T026 follow-up (optional, disclosed)** — re-run the Codex forward
   leg (S1/S2/S3b/S4) once the M365 backend is healthy; save tool-call
   logs + client versions to `evidence/live-clients/`.
2. **T042** — complete as run (two clean live runs, finding MIXED); no
   further runs unless Sam asks.
3. **T043** — real Windows/macOS install + doctor + J01/J05 runs;
   perf fix (read-set snapshots + indexed recall), then re-measure
   against the same thresholds.
4. Trademark clearance + dependency vulnerability scan (pip-audit /
   npm audit) before any public release.
5. Durable packaging fixes (not blocking OFFLINE_RC): demo.py fixtures
   via `importlib.resources`; pyproject.toml package-data for
   `turnkeep/migrations/*.sql`; regenerate or remove
   `requirements/ui.lock`.
6. **Drive + GitHub upload of this RC was authorised by Sam 2026-11-10
   ~09:08 GMT** (private repo turnkeep; T043 set aside for
   now). No public release, announcement post or message may happen
   without Sam's explicit authorisation for that specific scope.
   Release drafts are local files under
   `release/announcement-drafts/` only.

## Recovery (new session, no other context)

Read this file + `HANDOFF.md` + `state/` + `specs/` in the source
checkout. The zip alone is enough to view, build and run the offline
journeys and to locate every gap (see `evidence-summary.json` and
`release/DELIVERY_MANIFEST.json`). The reference kit is read-only at
`~/workspace/turnkeep/turnkeep-muse-kit/` (not shipped). Work dir:
`~/workspace/turnkeep-work/`, git HEAD HEAD@PACKAGING. No background tasks were
left running by T048; nothing is scheduled.

## Update 2026-11-10 — GPT final-review R1 rework DONE (R2 prepared, not sent)

- Sam authorised the rework 2026-11-10 ~17:29 GMT ("按照他的意见改").
  All 12 verified items (5 P0 + 7 P1) fixed, each with regression tests.
  Full pytest **276/276** (234 baseline + 42 new); the verification
  report's P0-1 / P0-2 / P0-4 reproductions were re-run against the
  fixed code and are closed. Commits 74006fc, 43c962e, 6f04a26, 6cdb0e5,
  ce9c2e1, bafb7b2, fc68961 (+ docs 7cc341a).
- Schema v2 (migration 002_record_provenance). v1 databases still
  upgrade on open; v1 *backups* no longer restore under v2 (by design —
  version-mismatch refusal). First-open race fixed (journal_mode=WAL
  retry on the busy-handler blind spot): test_two_clients_see_same_records
  20/20 isolated; full suite 276/276 in 6 of 7 runs (one run 275/276,
  single unidentified transient, no recurrence in the following 3 runs).
- Grades unchanged (re-grading is R4's job): INTEGRATED_RC_READY READY,
  VALUE_EVALUATED READY on the disclosed T026/T042 basis;
  PUBLIC_CANDIDATE NOT MET. Final review R2 pack prepared (per-item
  changes + test evidence + fresh RC Drive link + clarification of GPT's
  two non-code errors) — NOT sent; the parent sends it via browser in
  the same ChatGPT conversation.
- Fresh RC: `release/TURNKEEP_RC.zip`, **719,495 bytes, sha256
  `7d825983daf287c6e568eb35afa5aaae547fbde96498d71040b9b7687db35332`**,
  269 files, head 7cc341a. `verify_release.py --manifest` PASS
  (failures [], not_run []), `verify_handoff.py` PASS.
  Drive upload: pending (see state/CURRENT_STATUS.md).

## Update 2026-11-10 — GPT final-review R2 returned FAIL, rework DONE

- Final review R2 (same ChatGPT conversation, GPT-6 + Extra High,
  sent ~18:24 GMT): FINAL_VERDICT: FAIL (~20:47 GMT). 2 new P0s
  (structured secret >5,000 chars exported in plaintext; residual
  report false-clean on a shared-secret residual), 5 new P1s, P2s
  (incl. brief budget ignoring structured sections, offline install
  failing with no network, 9 environment-coupled tests). 4 of the 12
  R1 items were judged partially closed. R2 rework complete (11 items,
  pytest 304/304); R3 sent ~21:10 GMT and returned FAIL.
- Final review R3 (GPT-6 + Extra High): FINAL_VERDICT: FAIL. 3 new P0s
  (idempotent Remember receipts keep the raw secret after erasure;
  erasure reason can write the deleted secret back into the event log;
  wheel/sdist in the ZIP are NOT built from R3 source). R3 rework
  complete 2026-11-11 ~10:17 GMT (pytest 339/339, committed first as
  422984d, rebuilt, wheel verified byte-identical to src/); R4 sent
  ~10:18 GMT and returned FAIL ~10:31 GMT.
- Final review R4 (GPT-6 + Extra High, 12m39s thinking):
  FINAL_VERDICT: FAIL. 3 new P0s (short-value reason leak; second-erase
  re-leak; unsalted digest enumerates a low-entropy PIN), 3 new P1s
  (follow-up run born stale; concurrent quarantine duplicates; SQL LIKE
  blocks erasure of "%"), P2s (guards compare .py not .sql; stale
  HEAD/status lines in these handoff docs; build not reproducible from
  the sdist). All verified true against the code (report:
  `~/workspace/turnkeep-gpt-R4-verification-2026-11-11.md`).

## Update 2026-11-11 — GPT final-review R4 rework DONE (R5 prepared, not sent)

- All 10 verified items fixed, each with regression tests. P0-R4-01:
  reason check now rejects a payload needle as a standalone token of
  any length. P0-R4-02: repair-erase reasons are vetted against the
  needle hashes the first erase persisted. P0-R4-04: needle hashes are
  HMAC-SHA256 under a per-scope secret (`scope_secrets`, migration 003,
  schema v3) that never leaves the live DB. P1-R4-03: the follow-up run
  is created after the `rebase_exhausted` event, so it is born at the
  current epoch and finishes. P1-R4-05: the quarantine lookup is inside
  the write transaction and the follow-up run is created under an
  idempotency key. P1-R4-06: `_scan_payload` escapes LIKE wildcards and
  scans payload-bearing columns only; the tombstone row is excluded
  from the post-scrub scan. P2s: guards compare all files under
  `src/turnkeep/` (.py + .sql); `pyproject.toml` declares the
  package-data so a plain rebuild from the sdist is equivalent;
  `verify_release` records the commit check as `not_run` without `.git`;
  these handoff docs' HEAD lines are stamped at packaging time from
  the manifest head, so the shipped copies name it exactly.
- Full pytest **362/362** (339 + 23 new), run twice, no transient
  failure. GPT's R4 repros re-run (all P0s + P1s closed) plus the
  end-to-end privacy chain on one chain. Fresh RC: rebuilt at the
  packaging head — bytes, sha256 and verify results are in
  `release/DELIVERY_MANIFEST.json` and `state/CURRENT_STATUS.md`.
  R5 is the LAST round in this conversation — if it fails, the next
  round opens a new conversation per factory rules. R5 NOT sent by
  this job; the parent sends it via browser immediately after the
  rebuild + Drive upload. No GitHub upload until PASS — Sam
  2026-11-10 ~20:46: on PASS upload to GitHub, note Windows untested.
