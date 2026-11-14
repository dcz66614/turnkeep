# Environment (T004)

- Product root: `~/workspace/turnkeep-work/`; reference kit (read-only):
  `~/workspace/turnkeep/turnkeep-muse-kit/`
- Python >= 3.11 required; this VM: Python 3.12.3, SQLite 3.45.1, Node v24.22.1.
  Kit report claimed Python 3.12.14 / SQLite 3.53.1 — env difference, tests pass.
- Core: Python stdlib + SQLite only. No model API key, no Docker, no cloud DB.
- Dev venv: `.venv/` in product root (created with `python3 -m venv .venv`,
  ` .venv/bin/pip install -e ".[dev]"` installs pytest 9.2.0 and the build
  toolchain (`build` + `setuptools` + `wheel`, needed by the release-guard
  tests that rebuild via `python -m build --no-isolation`).
  System Python is
  externally managed (PEP 668) — do NOT `pip install --break-system-packages`.
- Offline core runs without the venv: `python3 -m turnkeep demo` from `src/`
  on PYTHONPATH, or after `pip install -e .` in the venv.
- Supported matrix (honest): Linux x86_64 verified here. Windows/macOS: NOT_RUN
  (T028/T043). Do not claim cross-platform from a single-platform run.
- Bootstrap: `python3 scripts/bootstrap_product.py` — verifies import + demo in
  a temp dir; if deps are missing it still runs pure core and reports the gap.
