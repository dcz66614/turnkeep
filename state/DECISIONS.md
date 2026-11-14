# Turnkeep — DECISIONS (R0)

- D-R0-1: Execution-phase numbering (R0–R5 per 02_EXECUTION_PLAN.md) is used internally; the brief's R4=public-candidate numbering differs — do not mix externally.
- D-R0-2: R0 executed serially by the coordinator itself (no worker subagents): the R0 chain T001→T006 is strictly linear, and local doc/script work needed no model calls. Worker protocol (ops/worker-contract.json, max 3) applies from R1 where tasks can run in parallel.
- D-R0-3: pytest lives in .venv only (system Python PEP-668). Acceptance commands in backlog that say `python` are run as `.venv/bin/python` / `python3` as appropriate; recorded in each task result.
- D-R0-4: tasks/backlog.json is a symlink to state/backlog.json — one source of truth, satisfying T006's literal --backlog tasks/backlog.json argv.
- D-R0-5: Name GO_WITH_BOUNDS (T002): PyPI JSON 404 / npm 404 / GitHub user 404, but turnkeep.app collision recorded; trademark clearance NOT_RUN.
- D-R0-6: Full legal/illegal payload schema validation deferred from T003 to T005 as the backlog itself orders; done in tests/contract/test_schemas.py (14/14 with harness).
- D-R1-1: run_gate treats aggregate acceptance specs as DEFERRED_AGGREGATE (top-level Muse invocation only, never dispatched by the runner, never counted toward PASS). A task with no leaf specs cannot PASS. The top-level invocation's own gate record is the aggregate evidence. Fixed during T007 (2 repairs used there: WAL-header byte-identity + aggregate handling).
- D-R1-2: Product migration 001 changes sources PK from the TARGET_SCHEMA.sql draft's global `id PRIMARY KEY` to composite `(scope_id, id)`. The draft is inconsistent: memory_sources FK references sources(scope_id, id) (per-scope), but a global PK makes the same source ref (e.g. 'u:1') unusable in a second scope (FK failure). The draft file in specs/ is untouched; only the product migration deviates, and the deviation is tested (cross-scope recall test).
- D-R1-3: run_gate aggregate-spec handling (D-R1-1) applies to all later tasks: aggregate specs are top-level-only evidence.

- D-R2-1: run_gate exit-code contract fixed — leaf exit 2 = NOT_RUN (missing credential/platform/client), not FAIL. Runner doc already said so; run_leaf mapped all non-zero to FAIL. Fixed during T026; self-test 6/6 still PASS.
- D-R2-2: T026 live handover is BLOCKED_EXTERNAL (NOT_RUN), not VERIFIED. Implementation (runner, fixture, recipe, COMPATIBILITY.md) is complete; execution needs two real host products. Two SDK test processes are never a substitute for two host products.
- D-R2-3: A repo template is not an installation — live_client_handoff.py counts only the client's own config (~/.claude.json / ~/.codex/config.toml) as 'installed'.

## D-R4-1 (2026-11-08): T042/T043 are NOT marked READY while T026 is BLOCKED_EXTERNAL
- `tests/harness/test_state_recovery.py::test_no_task_ready_before_deps` requires every READY task's dependencies to be VERIFIED/IMPLEMENTED. T042 and T043 both depend on T026, which is BLOCKED_EXTERNAL (live two-client handover, no client available) and will not become VERIFIED in this session.
- The backlog already declares `dependency_exceptions: {T026: implementation_or_documented_external_block}` for T042/T043, and `offline_completion` says implementation/fixtures/recipes may complete while external execution stays NOT_RUN.
- Decision: keep T042/T043 NOT_STARTED (work them as the active task without the READY flag) and record the external gate as NOT_RUN in their evidence. Do NOT weaken the test and do NOT mark the external gate PASS.

## D-R4-2 (2026-11-08): T043 recorded BLOCKED_EXTERNAL with a perf FAIL inside
- T043 is an external_gate + release_blocker task. Its platform part is
  genuinely external: Linux verified, Windows/macOS NOT_RUN (no runners).
- Its perf part is NOT external: perf.py ran and FAILED 3 pre-registered
  thresholds (finish p95 7.8s at 10k, recall p95 3.2s + finish p95 12.3s at
  100k). Root causes are the O(n) begin() full-snapshot and O(n) recall()
  full-table scan — correctness-first design, recorded as a known
  limitation with a scoped follow-up, never hidden by lowering thresholds
  or shrinking datasets (per the task's own acceptance_invariants).
- Status choice: BLOCKED_EXTERNAL (same precedent as T042) because the
  backlog vocabulary has no GATE_FAIL; the evidence file (T043.json)
  separates the two sub-gates explicitly. T043 is NOT VERIFIED. Per the
  kit rule, this record satisfies downstream preparation only; the product
  is OFFLINE_RC scope, not cross-platform complete.

## D-R5-1 (2026-11-08): T044 extends scripts/check_docs.py with a release profile
- scripts/check_docs.py was not in T044's owned_paths, but the T044 acceptance command `check_docs.py --profile release` could not pass as built: argparse only accepted `--profile ux` (exit 2 on `release`).
- Decision: accept the worker's minimal extension — a new check_release() profile; the ux profile is untouched and re-verified PASS by the coordinator. Recorded as a deviation, not a silent ownership change.

## D-R5-2 (2026-11-08): artifacts manifest commit lags HEAD by one commit
- build_release.py records the HEAD it was built from. Any commit of the
  artifacts themselves moves HEAD, so the committed manifest always
  records its parent commit. This is not a defect and must not be
  "fixed" by weakening verify_release.py's commit check.
- Rule: T048 rebuilds artifacts at packaging time so the shipped
  manifest matches the shipped source snapshot; the packaging commit
  itself is recorded separately in release/DELIVERY_MANIFEST.json.

## D-R5-3 (2026-11-08): `--strict-status` semantics for run_gate.py (F1, T047)
- The T047 acceptance command and the execution plan both cite
  `--profile release --strict-status`, but the flag did not exist
  (argparse exit 2). The plan says the command is "to be created or
  established by the task", so implementing it is the correct follow-up,
  not weakening the spec.
- Defined semantics: strict overall is FAIL unless every task in the
  selection PASSes. Per-task NOT_RUN is preserved (never softened to
  PASS); it fails the strict gate and blocks its badge, per the plan's
  pass condition ("unexecuted external gates are explicitly NOT_RUN and
  block the corresponding badge"). Default (non-strict) behaviour is
  unchanged. Self-test now has 7 checks.
- Coordinator re-ran the verbatim command: exit 1, overall FAIL
  (strict), 40 PASS / 3 NOT_RUN (T001 layout artifact, T026, T042) /
  3 FAIL (T006 state artifact — clears once T047 is marked VERIFIED;
  T043 genuine perf FAIL, third independent re-measurement
  5,116 / 2,875 / 12,631 ms, same 3 thresholds; T046 build refused on
  a dirty tree with artifacts preserved — see D-R5-4).

## D-R5-4 (2026-11-08): build_release.py --clean checks the dirty tree BEFORE deleting (F2, T047)
- `--clean` deleted release/artifacts/ and only then refused on a dirty
  tree; it destroyed the candidate during the T047 review (restored
  from git, hashes verified). The dirty check now runs before any
  deletion. Verified by the coordinator: dirty-tree build exits 1 with
  all 8 artifact hashes still matching the manifest.
