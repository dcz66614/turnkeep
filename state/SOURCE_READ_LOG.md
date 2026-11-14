# Turnkeep — SOURCE_READ_LOG

Date: 2026-11-08 ~18:47 GMT. Kit: ~/workspace/turnkeep/turnkeep-muse-kit/ (66 files, 1.8M, zip 1.1M).
Method: 3 parallel read-only subagents, each group read in full in segments; no code written, no starter run by readers. Controller then re-verified hashes and re-ran starter itself (see CURRENT_STATUS.md).

## Integrity (controller re-run 18:47 GMT)
- `sha256sum -c SHA256SUMS`: 65/65 OK
- `python3 tools/verify_package.py`: PASS (required_files 17, json_parse 17, task_plan 48 DAG acyclic, target 12 tables / API 8 paths, recorded_core 25 tests, docx valid, traceability 10 req / 98 sources, package_hashes 65 matched)
- MANIFEST.json: 64 files listed + MANIFEST + SHA256SUMS = 66 total. All read-group hashes match MANIFEST.

## Group A — core docs / plan / evidence (20 files, all read, no truncation)
- 00_START_HERE.md — 2db0abe6… — 3347 B, 58 lines. Entry: execute 10_MUSE_CONTROL_PROMPT.md, no re-brainstorm; only design/research/plan + reference core in kit; MCP/workbench/real clients still to build.
- 01_PROJECT_BRIEF.md — f94f04fe… — 20298 B, 212 lines. Turnkeep: local-first multi-agent project working memory + change recovery. R0 reference core, R1 local memory, R2 two-client handover, R3 workbench, R4 public candidate (brief numbering; see discrepancy 1).
- 02_EXECUTION_PLAN.md — 65fcbfa2… — 122450 B, 2147 lines, 6 segments. 48 tasks, J01–J05 journeys, F01–F24 fault scenarios, §17 run_gate leaf/aggregate contract.
- 03_ARCHITECTURE.md — 5d21c695… — 10059 B, 120 lines. Single-service SQLite, modes A/B/C, modules M01–M08.
- 08_RESEARCH_SYNTHESIS.md — bcc60028… — 7386 B, 88 lines. Long-term memory not one capability; change-recovery handover is the retained bet.
- 10_MUSE_CONTROL_PROMPT.md — 3de43b11… — 7475 B, 66 lines. Controller instructions: Phase 1 read+verify, semantics per CORE_CONTRACT, max 3 workers, task_result.schema.json, max 2 repairs + 1 alternative, budget additional spend 0, completion R1–R5, NOT_RUN not PASS.
- MANIFEST.json — 635a167a… — 10704 B, 338 lines. task_count 48, source_records 98, reference_tests 25.
- SHA256SUMS — bc1504a3… — 5955 B, 65 lines.
- docs/ADVERSARIAL_REVIEW.md — d99a9d25… — 9159 B, 84 lines. C01–C10 fixed in spec, P01/P02 need code+regression, R01–R04 unproven by test.
- docs/COMPETITION_AND_DISTRIBUTION.md — 84c71bc2… — 11143 B, 92 lines. 12 nearest neighbours; shrink conditions if fair trial shows no benefit.
- docs/DECISIONS.md — 8bf2d6a0… — 2634 B, 31 lines. D01–D20.
- docs/EVALUATION_PLAN.md — 65fd6143… — 18680 B, 175 lines. Gates A/B/C/D pre-registered, NOT results. Gate C: 6 arms, 36 tasks x 3 seeds, 648 runs.
- docs/RELEASE_PLAYBOOK.md — 364c1670… — 3139 B, 43 lines. Local candidate first; public upload only with explicit authorisation.
- docs/THREAT_MODEL.md — d39dbad3… — 4296 B, 49 lines. Same trusted OS user boundary; memory text is not authority.
- evidence/IMPLEMENTATION_STATUS.md — 30fe27e5… — 4076 B, 61 lines. Reference core claimed 25/25; MCP/HTTP/workbench/migration/erase NOT_IMPLEMENTED, real clients/model eval/Win+mac NOT_RUN.
- evidence/REVIEW_CLOSEOUT.md — d43c2659… — 2657 B, 30 lines. 6 consistency fixes; future run_gate/MCP still to be built and run.
- evidence/document_qa.json — 7cbd0aa0… — 9251 B, 210 lines. PASS, brief 11pp / plan 70pp, 133 + 1242 text units, 0 missing (author-side record, not independently rechecked).
- evidence/package_validation.json — 72ace3d3… — 1974 B, 85 lines. PASS, 7 checks.
- evidence/starter_test_report.json — bba9c529… — 7781 B, 55 lines. PASS claimed, Linux Python 3.12.14 / SQLite 3.53.1, 25 tests, demo SCRIPTED_SIMULATION_NO_MODEL_CALLS; real MCP/model/Win/mac NOT_RUN.
- evidence/task_plan_validation.json — 0aa2adfb… — 760 B, 29 lines. PASS, R0 6 / R1 12 / R2 10 / R3 7 / R4 8 / R5 5 = 48, DAG acyclic.

## Group B — specs / backlog / templates / tools (all read, no NOT_READ)
- specs/ADAPTER_MAPPING.md — 2222ade5… — 1806 B, 19 lines. Starter != PUBLIC_API; adapter + contract tests needed; error mapping CYCLIC_DEPENDENCY→CYCLE_DETECTED, ACTOR_MISMATCH→FORBIDDEN.
- specs/CORE_CONTRACT.md — fc1909bd… — 13546 B, 124 lines. Authoritative semantics v0.1: revision from 1, no silent overwrite, CAS, AND parents, taint propagation, begin snapshot, atomic finish, idempotent replay by digest, receipt = commit-only proof, coarse scope epoch mis-blocks, actor = label not auth.
- specs/PUBLIC_API.json — 9d73fad5… — 26552 B, 1027 lines, 3 segments. OpenAPI 3.1, 8 paths, 10 schemas, 15 error codes, Idempotency-Key 8–128 chars, localToken 127.0.0.1:7841.
- specs/TARGET_SCHEMA.sql — e1068298… — 5070 B, 135 lines. 12 tables, draft only, never overwrite reference DB.
- tasks/backlog.json — aff718ee… — 156822 B, 4484 lines, 11 segments. 48 tasks T001–T048, all NOT_STARTED, all release_blocker=true, P0 44 / P1 4 (T002,T037,T042,T044), max 3 workers, max 2 repairs + 1 alternative, initial ready only T001.
- templates/CHECKPOINT.md — 9177a914… — 530 B, 15 lines. 12-field checkpoint template.
- templates/CONTROL_STATE.json — 81c6de9e… — 556 B, 23 lines. Initial phase R0, environment_verified false, live_client NOT_RUN, budget additional 0 USD.
- templates/task_result.schema.json — 1dd13f5e… — 2979 B, 148 lines. Worker result, status 5 enums, no VERIFIED (controller only).
- prompts/WORKER_PROMPTS.md — 5e7232ad… — 3579 B, 42 lines. Generic + 5 worker types (core/adapter/UI/eval/adversarial).
- requirements_traceability.csv — 272b1709… — 953 B, 11 lines. R01–R10, statuses CORE_ONLY / CORE_TESTED / NOT_RUN / RETRACT_ONLY etc.
- tools/README.md — 9bb7c1cc… — 1908 B, 29 lines. verify_package read-only; --run-starter runs core; do not overwrite input package evidence.
- tools/build_docs.py — 975090a5… — 11828 B, 237 lines (read only). MD→docx.
- tools/build_visuals.py — 024d3e66… — 3936 B, 83 lines (read only). Pillow+SVG diagrams.
- tools/verify_package.py — e3359b92… — 9593 B, 200 lines (read only). 7 checks + optional rerun_offline_core.
- starter/README.md — d154a3c7… — 12873 B, 154 lines. Reference core, Python >=3.11 stdlib only, demo scripted, 25 tests, MCP/HTTP/workbench/erase/auth NOT in scope.

## Group C — research / demo / starter (all text read in segments; binaries metadata only)
- research/SOURCE_INDEX.csv — 028fa158… — 20545 B, 99 lines. 98 records: P 23, G 38, GI 16, U 20, PFV2 1.
- research/SOURCE_INDEX.json — 2d648c57… — 100106 B, 2040 lines. Full ledger; reading_status: extracted_page_read 33, full_text_read 5, abstract_only 5, README complete 21, README fetched-not-reviewed 13, issue complete 14, etc.
- research/github_landscape.md — 81ab1a15… — 21418 B, 170 lines. 34 repos, 21 full READMEs; no "first" claim; opportunity = cross-tool handover + change recovery, measured.
- research/github_snapshot.json — d617930c… — 22515 B, 557 lines. 34 repos API snapshot 2026-11-08; license_file_reviewed all false.
- research/github_sources.json — 33e7fbc8… — 51762 B, 1213 lines. 54 entries G01–G38 + GI01–GI16.
- research/github_trending_snapshot.json — 332bfd08… — 2030 B, 82 lines. Trending monthly 12 items, period_window_verified false.
- research/market_research_stats.json — 8c835cf2… — 461 B, 12 lines. Exa 6 searches / 64 results / 63 URLs / 20 pages read.
- research/market_sources.json — 2a81150f… — 17754 B, 367 lines. U01–U20, incl. 5 acquisitions (official announcements only).
- research/papers.md — 5996ffb6… — 31299 B, 376 lines. P01–P23: full 5 / extracted 10 / targeted 3 / abstract 5.
- research/papers_sources.json — 8d7201ef… — 25266 B, 348 lines. Same 23, experimental claims not independently reproduced.
- research/planfence_v2_source.json — 836be201… — 1170 B, 20 lines. PFV2, arXiv 2609.03340v2, extracted HTML 113,199 chars, full read.
- research/planfence_v2_supplement.md — e68df689… — 4608 B, 43 lines. Fresh-before-generation, rebase, mis-block measurement.
- research/user_market.md — 8254a6c9… — 17479 B, 139 lines. H1–H5 falsifiable hypotheses; no market-size / star / acquisition proof.
- demo/SCENARIOS.md — 3c0d26e3… — 2243 B, 32 lines. Scenarios A–D (CSV→JSONL, date correction, conflict, mis-block).
- demo/02_execution_plan_flow.dot — 90365dd8… — 944 B, 32 lines.
- starter/turnkeep/__init__.py — f62986f3… — 213 B, 7 lines. v0.1.0-reference, offline.
- starter/turnkeep/__main__.py — d152b44f… — 4752 B, 96 lines. demo/inspect/history CLI, SCRIPTED_SIMULATION_NO_MODEL_CALLS.
- starter/turnkeep/errors.py — 609b354b… — 360 B, 11 lines. TurnkeepError only.
- starter/turnkeep/store.py — 8287b6d4… — 29212 B, 567 lines, 2 segments. SQLite WAL, SCHEMA_VERSION=1, 6 kinds / 4 source types / 5 states, remember/correct/resolve/retract/begin/finish/resume/history/impact/backup.
- starter/tests/test_core.py — d5eb395f… — 18794 B, 363 lines. 25 tests test_01–test_25, NOT_RUN by readers.
- starter/verify.py — f6d85a7b… — 2453 B, 52 lines. Runs unittest + demo, writes evidence/starter_test_report.json.
- starter/LICENSE — 73dec276… — 1079 B, 22 lines. MIT.
- Binaries (metadata only): demo/*.png (171750 / 47109 / 121848 B), demo/*.svg (10817 / 2337 / 4663 B), 01_PROJECT_BRIEF.docx 209545 B c852411b…, 02_EXECUTION_PLAN.docx 262899 B 2fe2cf4f…. Docx content not parsed; md versions read by Group A and claimed identical in document_qa.json (not independently rechecked).

## Discrepancies found (not blockers, record for execution)
1. Version numbering drift: Brief §11 R4 = public candidate; Execution Plan / Control Prompt R4 = migration+evaluation, R5 = release candidate. Plan §5 declares this explicitly — use execution-phase numbering internally, do not mix externally.
2. PUBLIC_API has no independent recall/resume/brief paths, while backlog T021 MCP surface lists 9 tools incl. recall/resume/brief — mapping gap to close in T021/T003.
3. T042 blocks_profiles includes `comparative_claims`, not defined in release_profiles (only offline_rc / integrated_rc / value_evaluated / public_candidate).
4. R06 traceability lists only T038 but status RETRACT_ONLY — consistent with starter state, but T038 itself is still NOT_STARTED; do not read as done.
5. "Closed" claims (C06/P01/P02) in Adversarial Review §7 are scoped to reference core + listed regression tests only, not full product/platform proof.
6. Source counts overlap: 98 records across ledgers, not 98 independent full-text reads (full-text papers 5 + PlanFence 1; rest extracted/README/metadata/issue).
