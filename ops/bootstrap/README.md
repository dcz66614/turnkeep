# Bootstrap (T001)

Reference kit is read-only at `~/workspace/turnkeep/turnkeep-muse-kit/` — never modify it.
Product work lives in this repo (`~/workspace/turnkeep-work/`).

Frozen baseline: see `state/BASELINE_STATUS.json` and `state/SOURCE_READ_LOG.md`.
Starter re-run on this VM: unittest 25/25 OK, demo SCRIPTED_SIMULATION_NO_MODEL_CALLS exit 0.
Python 3.12.3 / SQLite 3.45.1 (kit report said 3.12.14 / 3.53.1 — env difference, tests still pass).

Reproduce:
```
cd <kit>/starter && python3 -m unittest discover -s tests -v
cd <kit>/starter && python3 -m turnkeep demo
cd <kit> && sha256sum -c SHA256SUMS && python3 tools/verify_package.py
```
