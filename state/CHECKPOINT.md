# Checkpoint — R4 complete

- Date: 2026-11-08 ~23:00 GMT
- Phase: R4 done; next phase R5, next task T044 (docs/licence/release)
- Completed VERIFIED: T001–T025, T027–T041 (40 tasks; evidence in evidence/tasks/)
- Blocked: T026 BLOCKED_EXTERNAL (live two-client handover NOT_RUN — release blocker), T042 BLOCKED_EXTERNAL (live-model eval Gate C NOT_RUN — no model/budget/endpoint), T043 BLOCKED_EXTERNAL (platform matrix Linux-only + perf gate FAIL, see below)
- T043 — implementation complete, gate NOT passed (NOT VERIFIED): platform_gate PASS on Linux (doctor, J01/J05, spaces+Chinese path, UI prod gate); Windows/macOS NOT_RUN (no runners; CI workflow file exists, no run executed). Perf gate FAIL, 3 thresholds: finish p95 7,760 ms at 10k records, recall p95 3,183 ms + finish p95 12,288 ms at 100k records. Causes: begin() snapshots ALL active records (O(n)/run) and recall() scans the whole table in Python (O(n)/query). Follow-up: read-set snapshots + indexed recall. Thresholds NOT lowered, datasets NOT shrunk. Full report: evidence/platforms/perf.json. D-R4-2.
- Contract revision: 0.1 (frozen); local app API 15 paths (OpenAPI snapshot in sync)
- Environment: Python 3.12.3, SQLite 3.45.1, Node v24.22.1, Linux 7.0.0-43-generic x86_64, 4 CPUs, 7.9 GB RAM; Playwright Chromium installed (with deps)
- Budget spent: $0 additional (no model calls used in R4 — coordinator executed directly)
- Last verified commands: pytest tests/ 225/225; run_gate --profile core PASS, --profile protocol PASS (re-run after one load-flaky parallel_clients failure in the full-suite run; isolated 6/6), --profile live-clients NOT_RUN; benchmarks/replay.py 25/25 (real core; mutations no-taint 21/25, silent-overwrite 22/25); benchmarks/harness/run.py smoke deterministic 12 attempts (none 1/3, markdown/log/turnkeep 3/3, SCRIPTED_SIMULATION_NO_MODEL_CALLS); run_journey cli J01/J05 PASS; ui_gate --production-build PASS
