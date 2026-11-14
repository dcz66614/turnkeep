# Demo (T034) — zero-key, replayable, labelled scripted

No API key, no database service, no internet needed (dependencies already
installed). Everything runs against a temporary local SQLite DB with fixed
fixtures and a fixed seed. Every demo output is labelled
`SCRIPTED_SIMULATION_NO_MODEL_CALLS` — never a real model call, never a
real client recording (those live in `evidence/live-clients/`, separately).

## Run

```
python -m turnkeep demo --scenario export-format --no-model
python -m turnkeep demo --scenario research-correction --no-model
python -m turnkeep demo --no-model        # J01 (default scenario)
```

## Scenarios

### export-format — file export project (J01/J03)

Fixture: `fixtures/demo/export-format/requirements.csv` (A reads CSV
requirements) + `changes.jsonl` (B changes the format via JSON Lines).

1. A reads `export.format=zip` from the CSV.
2. B changes the format to `tar` via JSON Lines.
3. A's old manifest finish is rejected with `STALE_SNAPSHOT`.
4. A resumes, reads the diff, recomputes for `tar` and commits.
5. An identical replay returns the original receipt.

### research-correction — retracted source (J03/J04)

Fixture: `fixtures/demo/research-correction/notes.csv` + `retraction.jsonl`.

1. Notes record a study conclusion and its evidence.
2. The evidence paper is retracted; the conclusion becomes tainted
   (AND-parent semantics — no silent reliance on bad evidence).
3. The conclusion is replaced explicitly (`remember` + `replaces_ids`)
   with the retraction notice as its new source; history is kept.

## Replay files

```
python scripts/record_demo.py --scenario export-format
python scripts/record_demo.py --scenario export-format --verify
```

`examples/demo/<scenario>.replay.jsonl` records one deterministic event
per step plus a chained sha256 digest per line. `--verify` checks both
the digest chain (a tampered line breaks it — try editing a value and
re-running `--verify`) and that a fresh re-run reproduces the same event
sequence. Record ids and timestamps are excluded from events, so the
comparison is stable across runs.

## What the demo does NOT show

Local SQLite correctness only. It is not MCP integration, not model
quality, not external-action safety, and not a real client recording.
