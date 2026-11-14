# Quickstart — install to first handover (Linux)

Time: about 5 minutes. No API key, no network, no model. All commands
below are executed for real by
`scripts/test_documented_commands.py --offline` on a clean temporary
fixture.

## 1. Install

```sh
bash scripts/install.sh
.venv/bin/python -m turnkeep --version        # turnkeep 0.1.0
.venv/bin/python -m turnkeep doctor --json    # diagnostics
```

`doctor` reports Python / SQLite versions, data-dir writability, DB
schema version + scopes + last event, available commands, client
config status, and the capabilities that are still unverified
(macOS / Windows / live clients). If `doctor` says `NOT_RUN` for the
database, that just means no scope has been created yet — run `init`.

## 2. First scope and first fact

```sh
python -m turnkeep --db /tmp/demo.sqlite3 init --scope demo
python -m turnkeep --db /tmp/demo.sqlite3 --json remember --scope demo \
  --key build.python_version --value 3.11 --kind requirement
```

`--db` points at a SQLite file; `--json` (before the subcommand) asks
for stable JSON output. Scopes isolate projects: two projects can use
the same key with different values and never see each other's records.

## 3. The handover loop

Agent A begins a run — the server takes a consistent snapshot of the
scope at the current epoch and stores the run; the client never invents
its own run:

```sh
python -m turnkeep --db /tmp/demo.sqlite3 --json begin --scope demo \
  --task "prepare release"
```

Agent B corrects the requirement. `correct` is compare-and-swap: it
needs the record id and the current revision, so two concurrent
corrections cannot both win:

```sh
python -m turnkeep --db /tmp/demo.sqlite3 --json correct --scope demo \
  --id <record-id> --expected-revision 1 --value 3.12
```

Agent A now tries to finish on the old snapshot. Turnkeep rejects it
instead of silently writing stale work — exit code 2:

```sh
python -m turnkeep --db /tmp/demo.sqlite3 --json finish --scope demo \
  --run <run-id> --key release --value "Python 3.11"
# {"error": {"code": "STALE_SNAPSHOT",
#   "details": {"changed_keys": ["build.python_version"], …}}}
```

Agent A resumes (diff only — the old result is never silently
"fixed"), starts a new run, and finishes against the new snapshot:

```sh
python -m turnkeep --db /tmp/demo.sqlite3 --json resume --scope demo --run <run-id>
python -m turnkeep --db /tmp/demo.sqlite3 --json begin --scope demo \
  --task "recompute release"
python -m turnkeep --db /tmp/demo.sqlite3 --json finish --scope demo \
  --run <new-run-id> --key release --value "Python 3.12" \
  --idempotency-key demo-1
```

The receipt is the proof of the local commit only:

```json
{"record_id": "…", "commit_epoch": 3,
 "guarantee": "local_sqlite_commit_only"}
```

Repeating the same finish (same run, key, value, idempotency key)
returns the identical receipt — it does not write a second result.

## 4. Recall and brief

```sh
python -m turnkeep --db /tmp/demo.sqlite3 --json recall --scope demo --query python
python -m turnkeep --db /tmp/demo.sqlite3 --json brief --scope demo --goal "prepare release"
```

`recall` returns active records only by default (`--history` adds
labelled history). `brief` returns a fixed-field handoff: current
status, valid decisions, open questions, attempts, recent changes,
and the records with their ids — within a character budget that keeps
conflict labels even when trimming.

## 5. Conflicts

```sh
python -m turnkeep --db /tmp/demo.sqlite3 --json remember --scope demo \
  --key build.python_version --value 3.13 --kind requirement
```

Same key, different value, no correction relationship: both records
leave the active view as a conflict. Resolve it only with a new
sourced value that lists **all** current candidates, as `id:revision`
pairs — re-read the revisions immediately before resolving, because a
new conflicting write bumps them (here the 3.12 record is at revision
2, not 1):

```sh
python -m turnkeep --db /tmp/demo.sqlite3 --json recall --scope demo --query python --history
python -m turnkeep --db /tmp/demo.sqlite3 --json resolve --scope demo \
  --key build.python_version --value 3.12 \
  --candidate <id-of-3.12>:2 --candidate <id-of-3.13>:1 \
  --source cli-1:user_statement
```

If the candidate set changed since you read it, resolve fails with
`CANDIDATE_SET_CHANGED` — re-read and retry. Never resolve with a
partial candidate list.

## 6. Workbench (optional)

```sh
python -m turnkeep serve --scope demo
# {"url": "http://127.0.0.1:PORT", "token": "…", "scope": "demo"}
curl -H "X-Turnkeep-Token: <token>" http://127.0.0.1:PORT/api/v1/health
# {"status": "ok", "scope": "demo", "epoch": …}
```

Loopback only; every request needs the per-process token; Ctrl-C stops
the server (no daemon is installed). The browser UI is served from
`ui/dist` — see `docs/UX_SPEC.md` for the five journeys (J01–J05).

## 7. Scripted demo and SDK handover (no key)

```sh
python -m turnkeep demo --scenario export-format --no-model
python -m turnkeep demo --scenario research-correction --no-model
python examples/python/handoff.py --temp-db
```

Both are labelled `SCRIPTED_SIMULATION_NO_MODEL_CALLS`. See
`docs/demo.md` and `examples/README.md`.

## 8. Export your data

```sh
python -m turnkeep --db /tmp/demo.sqlite3 export --scope demo --out /tmp/demo-export
python -m turnkeep --db /tmp/fresh.sqlite3 --json import --format turnkeep --dry-run /tmp/demo-export
python -m turnkeep --db /tmp/fresh.sqlite3 --json import --format turnkeep /tmp/demo-export
```

Dry-run first, always: it lists what would be imported, skipped, or
quarantined, and writes nothing. Details: `docs/migration.md`,
`docs/recovery.md`, `docs/retention.md`.

## 9. When something goes wrong

See [docs/troubleshooting.md](troubleshooting.md): `doctor` output,
`STALE_SNAPSHOT`, `VERSION_CONFLICT`, `IDEMPOTENCY_MISMATCH`,
corrupt-database diagnosis, and how to export diagnostics without
leaking real data.
