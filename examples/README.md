# Examples

All examples are synthetic fixtures — no real user data, no keys, no
third-party text. Everything runs offline against a temporary SQLite
database.

## Scripted demos (`examples/demo/`)

Replay files for the two zero-key demo scenarios (see `docs/demo.md`):

- `export-format.replay.jsonl` — file-export project (J01/J03): A
  reads `export.format=zip` from CSV; B changes it to `tar`; A's old
  finish is rejected (`STALE_SNAPSHOT`); A resumes, recomputes for
  `tar`, commits; an identical replay returns the original receipt.
- `research-correction.replay.jsonl` — retracted source (J03/J04):
  a study conclusion is tainted when its evidence is retracted, then
  replaced explicitly with the retraction notice as its new source.

Run them:

```sh
python -m turnkeep demo --scenario export-format --no-model
python -m turnkeep demo --scenario research-correction --no-model
python scripts/record_demo.py --scenario export-format --verify
```

`--verify` checks the chained sha256 digest (a tampered line breaks
the chain, exit 1) and that a fresh re-run reproduces the same event
sequence. Every demo output is labelled
`SCRIPTED_SIMULATION_NO_MODEL_CALLS` — local SQLite correctness only,
not MCP integration, not model quality, not a real client recording.

## Python SDK handover (`examples/python/`)

`handoff.py` — two `TurnkeepClient` instances on the same database
standing in for two agents/sessions (scripted simulation, no model
calls): A begins, B corrects the requirement, A's stale finish is
rejected, A resumes, reads the diff, recomputes and finishes.

```sh
python examples/python/handoff.py --temp-db
# PASS: SDK two-agent handover
```

Use `--db PATH --scope ID` to run it against a real database instead
of a throwaway one.

## Fixtures used by the demos

- `fixtures/demo/export-format/requirements.csv` + `changes.jsonl`
- `fixtures/demo/research-correction/notes.csv` + `retraction.jsonl`
- `fixtures/live-handoff/export-format.json` — the frozen fixture for
  the real two-client handover (T026, NOT_RUN — no live run yet)
