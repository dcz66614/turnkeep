# Turnkeep — BLOCKERS

Date: 2026-11-08. None open in R0.

Recorded (not blockers, kept for R1+):
- Name: turnkeep.app is a live rental product with the same name (different domain). Working name only; revisit before any public release (T002 / docs/NAME_CHECK.md).
- PUBLIC_API has no recall/resume/brief HTTP paths vs T021's 9 MCP tools — mapping gap to close in T021 (T003).
- Env diff: this VM Python 3.12.3 / SQLite 3.45.1 vs kit report 3.12.14 / 3.53.1; tests pass.
- System Python is PEP-668 externally managed; venv only for pip installs.
- Real clients / model eval / Windows+macOS: NOT_RUN (external conditions, T026/T042/T043).

- T026 (R2, release blocker): live two-client handover NOT_RUN — claude CLI not installed; codex turnkeep entry not installed, no auth state, no login attempted. Runner + fixture + recipe complete; needs Sam to install adapters and run two real client sessions. Blocks integrated_rc / public_candidate.
