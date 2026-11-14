# Turnkeep — Independent Final Review (T047)

**Reviewer:** REVIEW (isolated from the T044/T045/T046 builders and from the
R0–R4 builders). Review-only: no product code, test, build script, README or
docs file was modified by this review. Only the three owned paths were
written: `evidence/FINAL_REVIEW.md` (this file),
`evidence/GATE_MATRIX.json`, `release/KNOWN_LIMITATIONS.md`.

**Head reviewed:** `24111b9a95471eddb0c8814e351b5f495d629c76`
**Date:** 2026-11-08 ~21:15–21:45 GMT
**Product:** Turnkeep (working name) 0.1.0, contract v0.1
**Scope of the review:** every release claim checked against its own gate's
evidence — core (T007–T018), protocol (T021–T023), real clients (T026),
platforms (T043), real model (T042), docs (T044), licence (T045), build
(T046). Statuses are NOT interchangeable: a PASS in one gate never stands
in for another gate's PASS.

## 1. Verdict in one paragraph

The Linux offline release candidate is what it says it is, and nothing it
says it is not. All offline gates genuinely pass on re-run (full pytest
234/234, protocol PASS, UI e2e 11/11 on re-run, clean-venv install + J01 +
CLI round trip PASS). The three external gates are honestly NOT_RUN/FAIL and
are disclosed everywhere they matter. **OFFLINE_RC_READY: READY.
REFERENCE_CORE_READY: READY. INTEGRATED_RC_READY: NOT READY (T026).
VALUE_EVALUATED: NOT READY (T042). Public-release conditions: NOT MET.**
Two new reviewer findings need a follow-up before T048 packaging
(§4, F1–F2); neither is a product-correctness defect and neither blocks the
OFFLINE_RC grade, but F2 destroyed the release artifacts on disk during this
review's re-run and had to be recovered from git (§4).

## 2. What the reviewer re-ran (all exit codes recorded)

System Python is PEP-668 managed, so every `python` in the backlog was run
as `.venv/bin/python`, per D-R0-3. Commands run from `~/workspace/turnkeep-work/`.

| # | Command (as run) | Exit | Result |
|---|---|---|---|
| 1 | `.venv/bin/python scripts/run_gate.py --profile release --strict-status` | **2** | argparse error: `--strict-status` is not a flag of `run_gate.py`. See F1 (§4). This is the backlog's acceptance command verbatim; it cannot run as written. |
| 2 | `.venv/bin/python scripts/run_gate.py --profile release` (the one permitted alternative: same command minus the unsupported flag) | **1** | Profile FAIL. Per-task detail in §3. Expected in part (NOT_RUN/FAIL gates are real), but see F2–F4 for the three sub-results that are runner/state artifacts, not product failures. Full record: `evidence/gate/profile_release.json` (generated 2026-11-08T21:20:30Z). |
| 3 | `.venv/bin/python scripts/verify_release.py --claims README.md --gates evidence/GATE_MATRIX.json` | **1** | Claims check itself: **7 claims parsed, 21 gate entries, 0 unmatched, no claim failures.** The only failure in the whole run is the manifest commit lag (see §5). |
| 4 | `.venv/bin/python scripts/verify_release.py --manifest release/artifacts/manifest.json` | **1** | 10/11 checks PASS (incl. clean-venv install, J01 demo, CLI round trip with receipt). Only failure: commit lag (§5). |
| 5 | `.venv/bin/python -m pytest -q` (full suite) | **0** | **234 passed, 0 failed** in 42.31 s. The known flaky `test_two_clients_see_same_records` did NOT fail in this run, so no isolated re-run was needed. (Suite grew 225 → 234 since the R4 close: +9 T045 boundary tests.) |
| 6 | `.venv/bin/python scripts/release_checks.py --profile license-and-secrets` | **0** | PASS: 374 files scanned, 0 FAIL, 303 INFO, 14 whitelisted fakes. (T045 recorded 363 files / 289 INFO; the tree grew since then. Verdict unchanged.) |
| 7 | `.venv/bin/python scripts/check_docs.py --profile release` | **0** | PASS: release docs carry honest disclosures, no banned claims, no leaks, drafts marked draft-only. |

## 3. Release-profile re-run, task by task

`run_gate --profile release` (command 2) returned exit 1 with this
per-task picture (`evidence/gate/profile_release.json`):

- **PASS (40 tasks):** T002–T005, T007–T025, T027–T041, T044, T045.
  Every offline gate re-passed on this machine, including the perf-adjacent
  Linux platform checks, the UI production gate (e2e 11/11 re-run inside
  T035's leaf), and the deterministic smoke replay (T040).
- **T026 NOT_RUN (exit 2, genuine):** `live_client_handoff.py` re-ran and
  reproduced the gap exactly — `claude` CLI absent; Codex installed but no
  turnkeep entry in `~/.codex/config.toml`, no auth state. No mock was
  recorded. This is the honest external block, not a failure to try.
- **T042 NOT_RUN (exit 2, genuine):** `benchmarks/live/run.py` re-ran and
  reproduced the gap exactly — no model selected, authorised spend $0, no
  endpoint. The runner writes a NOT_RUN gap record and never simulates a
  run. `report.py` confirms 0 attempts.
- **T043 FAIL (genuine perf gate, re-measured):** platform sub-gate PASS
  (Linux doctor/J01/J05/spaces+Chinese path/UI prod gate all re-passed);
  perf sub-gate FAIL on the same 3 pre-registered thresholds. The reviewer's
  re-measurement differs in absolute numbers from the frozen T043 record —
  finish p95 **3,470.2 ms** (frozen: 7,759.9 ms) at 10k, recall p95
  **2,895.8 ms** (frozen: 3,182.9 ms) and finish p95 **15,230.7 ms**
  (frozen: 12,288.3 ms) at 100k — but the verdict is identical (FAIL, same
  3 thresholds). Absolute timings are load-dependent; the FAIL verdict is
  robust. The frozen `evidence/platforms/perf.json` was **not** overwritten
  by this re-run (verified: `git diff` empty); the frozen numbers
  7,760 / 3,183 / 12,288 ms remain the evidence of record and are the
  numbers cited in README, `docs/KNOWN_LIMITATIONS.md` and §6 below.
  Thresholds were not lowered and datasets were not shrunk.
- **T001 NOT_RUN (runner/layout artifact, F4):** both leaf commands need
  cwd `INPUT_KIT/starter`, which does not exist in this worktree — the kit
  lives at `~/workspace/turnkeep/turnkeep-muse-kit/`. This is a pre-existing
  gap between the T001 acceptance spec (written for the kit layout) and the
  product worktree, not a product failure. T001's own evidence
  (`state/BASELINE_STATUS.json`: starter 25/25 + demo OK on this VM) stands
  from R0 and was not contradicted by anything in this review.
- **T006 FAIL (state artifact, F3):** `check_state.py` exit 1 because the
  backlog still lists active task T047 as `NOT_STARTED` and because T047
  depends on T026/T042/T043 (BLOCKED_EXTERNAL). That is a true statement
  about the backlog state mid-review, not a product defect. The second T006
  leaf (`test_state_recovery.py`) passed 5/5.
- **T046 FAIL (cascade from F2, not a build defect):** see §4 F2. The
  build step refused on a dirty tree; the verify step then failed only
  because the build step had already deleted the artifacts. After the
  reviewer restored the artifacts from git (hashes re-verified against the
  manifest), the standalone `verify_release.py --manifest` run (command 4)
  passed 10/11 checks.

## 4. Reviewer findings (new; refer back, do not fix here)

### F1 — Acceptance command cannot run as written: `run_gate.py` has no `--strict-status` flag (refer to T005 / coordinator)

The T047 backlog acceptance command is
`python scripts/run_gate.py --profile release --strict-status`, and the
spec argv lists `--strict-status` too — but `run_gate.py`'s argparse only
accepts `--task/--profile/--self-test/--root/--out`. Verbatim execution
exits 2 (argparse usage error). No other task's spec uses this flag; it
appears only in T047. The reviewer used the single permitted alternative
(command 2: the same invocation without the flag) and records the verbatim
failure honestly. **Do not "fix" this by weakening the acceptance command
or by silently dropping the flag from the record.** Either implement the
flag in the runner (T005-owned) with defined strict semantics, or correct
the T047 spec. The review's conclusions do not depend on the flag: the
profile run without it already surfaces every NOT_RUN/FAIL gate.

### F2 — `build_release.py --clean` deletes the release artifacts BEFORE checking the dirty tree (refer to T046)

During the reviewer's `run_gate --profile release` re-run, the T046 leaf
`build_release.py --clean --output release/artifacts` deleted all 9 files
under `release/artifacts/` (wheel, sdist, UI tarball, SBOM, SHA256SUMS,
locks, manifest) and *then* refused with "tracked files are modified;
commit or revert before building". The tree was dirty partly because of
the review itself (untracked `evidence/GATE_MATRIX.json`, plus evidence
files the gate's own leaf commands had just rewritten). The follow-on
`verify_release.py --manifest` leaf then failed with "manifest not found".
The reviewer restored the artifacts with `git checkout -- release/artifacts/`
and re-verified every file's sha256 against the restored manifest — all 8
manifest files match, and the standalone verify (command 4) then passed
10/11. **This is a real hazard, not a theoretical one: a `--clean` build
on a dirty tree destroys the previous candidate before refusing.**
Recommended follow-up (T046-owned, outside this review): run the dirty-tree
check *before* deleting anything, or make `--clean` refuse without touching
the output directory. The T046 build itself is not downgraded — the
reproducible build evidence (two bit-for-bit builds, T046) still stands —
but the release-profile re-run can never pass T046 while the tree is dirty,
which is by construction during any review that writes evidence.

### F3 — T006 FAIL in the release profile is a backlog-state artifact (no action; coordinator to note)

See §3. `check_state` correctly reports that T047 is active-but-
`NOT_STARTED` and depends on three BLOCKED_EXTERNAL tasks. Once the
coordinator updates T047's status, a re-run of the T006 leaf should pass.
Not a product defect.

### F4 — T001 NOT_RUN in the release profile is a layout gap (refer to T005 / coordinator)

See §3. The T001 leaf commands assume `INPUT_KIT/starter` under the
product root. Either the runner should resolve the kit path, or the
release profile should treat T001 as already-evidenced (R0) rather than
re-dispatching it. Not a product defect.

### F5 — Perf absolute numbers vary run to run; the FAIL verdict does not (no action)

See §3, T043. The reviewer's re-measurement (3,470 / 2,896 / 15,231 ms)
differs from the frozen record (7,760 / 3,183 / 12,288 ms) but fails the
same 3 thresholds. This strengthens, not weakens, the FAIL: it is not a
one-off slow run. Do not quote either set as "the" performance of the
product; both are single-machine measurements on this VM.

## 5. Manifest commit lag — by design, not a defect (D-R5-2)

Both `verify_release.py` runs (commands 3 and 4) fail **only** on:
`manifest commit 7391e83b76aa50f658d1ee98862b87ccbf1c8096 != current HEAD
24111b9a95471eddb0c8814e351b5f495d629c76 (rebuild before verifying)`.

This is the documented by-design lag (state/DECISIONS.md D-R5-2):
`build_release.py` records the HEAD it was built from, and committing the
artifacts themselves moves HEAD, so the committed manifest always records
its parent commit. **Conclusion: T048 rebuilds the artifacts at packaging
time so the shipped manifest matches the shipped tree. This must NOT be
"fixed" by weakening `verify_release.py`'s commit check, and it does not
downgrade T046.** Everything else in the manifest verification passed:
manifest fields + known gaps present, version matches pyproject.toml
(0.1.0), 8 files present with matching sha256, SHA256SUMS agrees,
platforms honest (Linux verified, Windows/macOS NOT_RUN), wheel installs
into a clean venv, `--version` reports 0.1.0, J01 zero-key demo PASS from
the installed artifact, CLI remember/begin/finish round trip PASS with a
real receipt (record_id `70e239c5…` / `839d0277…`, commit_epoch 2).

## 6. Required facts — verified against evidence (all present in `release/KNOWN_LIMITATIONS.md` too)

- **T026 real two-client handover NOT_RUN (BLOCKED_EXTERNAL):**
  `claude` CLI missing, Codex has no Turnkeep entry installed, no auth
  state on file; `run_gate --profile live-clients` NOT_RUN (exit 2).
  Evidence: `evidence/live-clients/live_client_gate.json`,
  `evidence/tasks/T026.json`, `evidence/gate/profile_live-clients.json`.
- **T042 real-model evaluation NOT_RUN (BLOCKED_EXTERNAL):** no model
  selected, no budget authorised, no endpoint configured; **$0 spent**.
  The offline harness only writes a NOT_RUN gap record and never simulates
  a run. Evidence: `evidence/live-evals/gap.json`,
  `evidence/tasks/T042.json`.
- **T043 platforms + perf:** Windows/macOS install NOT_RUN (no runners;
  CI workflow `.github/workflows/test.yml` exists but has never run).
  Performance gate **FAIL** — finish p95 **7,760 ms @ 10k**, recall p95
  **3,183 ms** + finish p95 **12,288 ms @ 100k** (thresholds: finish
  500 ms, recall 1,000 ms); cold start **159 ms PASS**. Causes:
  `begin()` full snapshot O(n) + `recall()` full-table scan O(n).
  Thresholds not lowered, datasets not shrunk. Evidence:
  `evidence/platforms/perf.json`, `evidence/platforms/current.json`,
  `evidence/tasks/T043.json`.
- **Trademark clearance NOT_RUN:** `turnkeep.app` is a live, same-spelled
  rental turnover product in a different market; "Turnkeep" is a working
  name only. Evidence: `docs/NAME_CHECK.md`, `docs/KNOWN_LIMITATIONS.md §6`.
- **Installed-wheel demo fixtures FAIL:** the `--scenario export-format` /
  `research-correction` demos fail from the installed wheel
  (`demo.py` resolves fixtures via `parents[2]`, which is the repo root
  in source but `lib/python3.12` when installed). The reviewer confirmed
  this live: `verify_release.py`'s installed-wheel probe records it as a
  note in both runs (commands 3–4). The default J01 scenario needs no
  fixtures and **passes** from the installed wheel. Durable fix is
  `demo.py` via `importlib.resources` (core-owned, outside T046).
  Evidence: `release/artifacts/manifest.json`
  `known_gaps.demo_fixtures_installed`.
- **No dependency vulnerability scan:** pip-audit / npm audit NOT_RUN.
  `release_checks` PASS is a pattern scan, not production safety.
  Evidence: `SECURITY.md §2`, `evidence/tasks/T045.json`.
- **`requirements/ui.lock` intentionally empty** (R0 design); the UI lock
  source is `ui/package-lock.json`. Evidence: `release/artifacts/manifest.json`
  `locks.ui`, `release/SBOM.json` notes, `SECURITY.md §6` (Medium lock-hygiene
  risk — the SBOM itself is accurate).
- **Receipt scope:** a receipt proves only that the local SQLite database
  accepted the commit at that time (`guarantee: local_sqlite_commit_only`).
  External-action atomicity is **NOT_SUPPORTED**. `retract` ≠ `erase`:
  retract is logical (payload stays in history); erase leaves a tombstone
  but uncontrolled copies remain, and restoring a pre-erasure backup
  resurrects the erased payload (expected point-in-time behaviour).
  Evidence: `specs/CORE_CONTRACT.md §3.4`, `docs/KNOWN_LIMITATIONS.md §5/§8`,
  `SECURITY.md §1/§4`.
- **Current grade:** the product is **OFFLINE_RC (Linux offline) only** —
  never a cross-platform finished product, an integrated finished product,
  or a value-verified product. See §7.

## 7. Grading (per 02_EXECUTION_PLAN.md §9 — only with complete evidence)

| Grade | Verdict | Evidence |
|---|---|---|
| REFERENCE_CORE_READY ("reference mechanism runs") | **READY** | Starter re-run on this VM: unittest 25/25 + scripted demo OK (T001, `state/BASELINE_STATUS.json`); product core gate `evidence/gate/profile_core.json` PASS; reviewer's full pytest 234/234. |
| OFFLINE_RC_READY ("runnable local candidate; real clients / missing platforms listed separately") | **READY** | All gates for the advertised Linux offline scope PASS on reviewer re-run: core (T007–T018), protocol (T021–T023, profile PROTOCOL PASS), UI/workbench (T027–T035, e2e 11/11), data (T036–T039), deterministic eval harness (T040–T041), docs/licence (T044–T045), build (T046, modulo the by-design commit lag in §5). The FAIL/NOT_RUN gates are disclosed limits of the RC, not hidden defects; they block higher grades, not this one. |
| INTEGRATED_RC_READY ("verified for the listed client versions") | **NOT READY** | Blocked by T026: live-two-client handover NOT_RUN (BLOCKED_EXTERNAL). MCP protocol evidence from the official SDK test client (12/12) cannot substitute for two real host products (plan §9: "SDK测试client通过不能代替实际host"). |
| VALUE_EVALUATED ("results measured under these conditions") | **NOT READY** | Blocked by T042: live-model evaluation NOT_RUN (BLOCKED_EXTERNAL). Deterministic smoke (12 scripted attempts) and replay (25/25) PASS is not a model result. |
| Public-release conditions (PUBLIC_CANDIDATE) | **NOT MET** | Missing all of: real clients (T026 NOT_RUN) + real model (T042 NOT_RUN) + cross-platform (T043 Windows/macOS NOT_RUN and perf gate FAIL) + trademark clearance NOT_RUN + dependency vulnerability scan NOT_RUN. |

## 8. Journey check (J01–J05, from evidence, not from summaries)

- **J01 zero-key demo — PASS (verified live by the reviewer).** Default
  scenario runs from source and from the installed wheel (commands 3–4);
  output labelled `SCRIPTED_SIMULATION_NO_MODEL_CALLS`. The two
  `--scenario` variants fail only from the installed wheel (§6); from a
  source checkout they run (T034 chained-digest replay 8 + 7 events,
  `--verify` PASS, tamper breaks the chain).
- **J02 two real clients — NOT_RUN.** The T033 "J02 ui-api journey PASS"
  is two principals sharing one DB through the HTTP API — useful UI
  evidence, but it is **not** two real MCP host products and must never be
  cited as T026. T026 remains BLOCKED_EXTERNAL.
- **J03 requirement change + recovery — PASS.** UI-API journey PASS
  (T031), impact paths with old/new/source/affected/reason, resume diff,
  rebase ×2 then quarantine; replay scenarios S01–S03 PASS against the
  real core (T041).
- **J04 restart + recovery — PASS.** UI e2e J04 (restart recovers runs
  and records from the DB, not localStorage), `restore_check.py --fixture
  restart-handoff` PASS (T039), killed-writer leaves no receipt (T014/T018).
- **J05 selective migration — PASS.** J05 CLI journey PASS (T036);
  deterministic re-import is a no-op; tampered manifest rejected; incoming
  active claims not trusted (missing/quarantined parents → tainted).

## 9. Claims audit (README.md vs gate matrix)

`verify_release.py --claims README.md --gates evidence/GATE_MATRIX.json`
parsed 7 claims and matched all 7 to gate entries (0 unmatched, 0 claim
failures). Spot-checks by the reviewer, claim by claim:

- "[verified] No silent overwrite" → PASS gate (T010/T011; 9/9 + 9/9).
- "[verified] Change recovery / STALE_SNAPSHOT / rebase max 2" → PASS gate
  (T012–T015; incl. 2-process race 1 winner).
- "[verified] Atomic finish + idempotent receipt" → PASS gate (T009/T014);
  the `local_sqlite_commit_only` qualifier is present in the claim itself.
- "[verified] Handover briefs … secrets redacted" → PASS gate (T016/T017).
  > **SUPERSEDED — WRONG (P0-4 rework, 2026-11-10).** This assertion was
  > false: structured secret values (e.g. a decision record with value
  > `{"api_key": "sk-…"}`) were NOT redacted in briefs/handoffs/UI — the
  > flat-text regex could not match inside JSON, and `valid_decisions` /
  > `open_questions` / `attempts` embedded raw values. Verified in
  > `~/workspace/turnkeep-gpt-review-verification-2026-11-09.md` §1 P0-4
  > and §7 item 4. Fixed by recursive redaction (`redact_value`) in
  > `src/turnkeep/briefs.py`, `src/turnkeep/handoff.py` and
  > `ui/src/views/Handoff.ts`; regression tests in
  > `tests/core/test_briefs.py` / `tests/core/test_handoff.py`. The
  > original line above is kept for history; do not quote it as evidence.
- "[verified] Export/import / Markdown / expiry / erasure / backup" →
  PASS gate (T036–T039).
- "[experimental] Local HTTP workbench + browser UI" → PASS gate
  (T027–T035); the experimental marker is honest — no stronger claim made.
- "[not run] Real two-client handover (T026) and live-model evaluation
  (T042)… Windows/macOS install has not run" → NOT_RUN gate. Correct.
- The README capability table's "Performance … [verified — FAIL]" row is
  not parsed as a claim by the checker (table row, not a bullet), but the
  gate matrix carries the perf gate as FAIL with the exact numbers, and the
  README text itself states the FAIL with the same numbers.

No banned claims were found by `check_docs.py --profile release` (command
7, exit 0), and the reviewer found no claim in README.md /
README.zh-CN.md that exceeds its evidence. The Chinese README carries the
same disclosures with the same numbers (7,760 / 3,183 / 12,288 ms, 159 ms
cold start, $0).

## 10. Core-author vs reviewer conflicts

**None.** The reviewer found no disagreement with any prior task's evidence
that needed a fixture/contract arbitration. Spot-checks reproduced the
prior records exactly where they are deterministic: replay manifest
(25/25, false_refusals=2), perf frozen numbers (7,760 / 3,183 / 12,288),
live-client gap record, live-eval gap record, SBOM counts (103 = 33 + 70).
The only numeric divergence is the perf re-measurement (§3, T043 / F5),
which is a timing-variance observation, not a conflict: both runs fail
the same 3 thresholds, and the frozen file was not overwritten.

Two honest-statement checks the reviewer explicitly confirmed rather than
taking on trust:
- The SBOM's lock-gap note is accurate: 21 Python transitive packages are
  listed as installed-but-unpinned, and `requirements/ui.lock` is indeed a
  one-comment placeholder while `ui/package-lock.json` pins the real Node
  deps. The SBOM itself (built from the real locks + installed metadata)
  is accurate.
- `docs/COMPETITORS.md` / `docs/comparison.md` do not claim first-mover
  status; the "first cross-tool memory" claim is explicitly refuted (T002).

## 11. What is NOT in this review

- No product code, test, build script, README or docs file was changed.
  The only files written are the three owned paths. Files the review's own
  re-runs rewrote (`evidence/platforms/current.json`,
  `evidence/ui/ui-gate.json`,
  `benchmarks/results/smoke-deterministic/attempts.jsonl`,
  `src/turnkeep.egg-info/*` — refreshed run-ids/timings only, same
  outcomes) were restored to HEAD with `git checkout`, and the deleted
  `release/artifacts/*` were restored from git and hash-verified (§4 F2).
  The working tree at the end of this review contains only two untracked
  files: `evidence/GATE_MATRIX.json` (owned) and
  `evidence/gate/profile_release.json` (the run_gate output of command 2,
  kept as its record). Nothing was committed (coordinator commits).
- No external publish, upload, login, or purchase. No GitHub/Drive action.
- No live-model call, no client login, no Windows/macOS run was attempted
  (no credentials, runners, or authorisation exist for them).

## 12. Next actions (for the coordinator / T048)

1. **T048:** rebuild artifacts at packaging time so the shipped manifest
   matches the shipped tree (D-R5-2); re-run `verify_release.py --manifest`
   after the rebuild and expect exit 0.
2. **T046 follow-up:** make `build_release.py --clean` check the dirty
   tree *before* deleting the output directory (F2). Until then, never run
   a `--clean` build on a dirty tree.
3. **T005/coordinator:** resolve the `--strict-status` flag gap (F1) —
   implement it in the runner or correct the T047 spec; do not silently
   drop it.
4. **T044/T004 follow-up (not blocking OFFLINE_RC):** demo fixtures via
   `importlib.resources` so the installed wheel runs the `--scenario`
   demos; regenerate or remove the empty `requirements/ui.lock` and fix
   the stale `pyproject.toml` optional deps (`mcp=[]` / `ui=[]`).
5. **Before any public release:** real two-client handover (T026), live
   model evaluation with an authorised budget (T042), Windows/macOS runs
   (T043), perf fix + re-measurement against the same thresholds (T043),
   trademark clearance, and a dependency vulnerability scan (pip-audit /
   npm audit). None of these is needed for the OFFLINE_RC grade.
6. **No re-review of already-passing gates is needed** on the basis of
   this review. A re-review is needed only if a product file changes
   after this HEAD.
