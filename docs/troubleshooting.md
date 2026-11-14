# Troubleshooting

## First step: doctor

```sh
python -m turnkeep doctor --json
```

`doctor` reports, with a `PASS` / `NOT_RUN` status each:

- Python version (needs ≥ 3.11) and SQLite version.
- Data directory path and whether it is writable.
- Database: schema version, scopes, last event — or `NOT_RUN` with
  "no turnkeep database found yet — run `turnkeep init --scope <id>`".
- Whether the `mcp` and `serve` commands are implemented.
- Client config status (Claude Code / Codex adapters) and the
  capabilities that are still unverified (macOS / Windows / live
  clients).

If `doctor` itself fails, check that you are running the project's
Python (`.venv/bin/python -m turnkeep doctor --json` from the
checkout) and not the system Python — system Python on PEP-668
systems cannot see the installed package.

## Common errors

### `STALE_SNAPSHOT` (finish, exit 2)

The scope changed after this run read its snapshot. This is the
product working, not a bug. Do not retry the same finish — it will
fail the same way.

```sh
python -m turnkeep --db <db> --json resume --scope <scope> --run <run-id>
```

`resume` shows `changed_keys`, added / changed records, and the new
candidate snapshot. Re-read, recompute the affected work, start a new
run with `begin`, and finish from that run. After 2 automatic
`rebase` attempts the candidate is quarantined for manual review
(see `docs/UX_SPEC.md` J03 / `docs/recovery.md`).

### `VERSION_CONFLICT` (correct / retract / erase, exit 2)

The record changed since you read its revision. Re-read the record
(`recall --history` or `inspect`) and retry with the current
`expected_revision`. Two concurrent corrections: exactly one wins.

### `CANDIDATE_SET_CHANGED` (resolve, exit 2)

The conflict set changed since you read it. Re-read the candidates
(`recall --query <key> --history` shows ids, states and revisions)
and retry `resolve` with the complete, current set as
`--candidate <id>:<revision>` (repeatable) plus a new sourced value
(`--source <id>:<type>`, e.g. `cli-1:user_statement`). A partial
candidate list is rejected as a whole — never silently applied.

### `IDEMPOTENCY_MISMATCH` (finish / remember retry, exit 2)

The same idempotency key was reused with a different request payload.
Repeating the *identical* request returns the original receipt; a
different payload under the same key is refused. Use a new
idempotency key for a genuinely new request.

### `INVALID_SOURCE` / `INVALID_INPUT`

- Sources must be `id:type` (e.g. `cli-1:user_statement`,
  `demo:user:1`). The type must describe how the information was
  obtained (`user_statement`, `imported_text`, `agent_inference` …).
  A bare word is rejected.
- Candidates must be `id:revision`.
- A body that tries to rebind `actor` / `tenant` is rejected
  (`FORBIDDEN`): identity comes from the CLI / MCP / HTTP binding,
  never from request content.

### `PARTIAL` (erase)

The payload is also in the append-only `events` table, so erasure
refuses to rewrite history and leaves the record untouched. The
honest path is to export the scope to a fresh database and delete the
old one (see `docs/retention.md`). Erasure residuals (backups,
exports, other clients' copies) are listed by `turnkeep residuals`
and are never claimed deleted.

### 401 `UNAUTHORIZED` (workbench)

The per-process session token is missing or wrong. `serve` prints
the token once at startup (`{"url": …, "token": …}`); send it as
`Authorization: Bearer <token>` or `X-Turnkeep-Token: <token>` on
every request. Restarting `serve` issues a new token — old tokens
stop working. 403 `FORBIDDEN` means a non-loopback `Host` / `Origin`.

### `database or disk is full` / slow finish on large scopes

Known performance limitation, not a configuration error: `begin()`
snapshots all active records and `recall()` scans the whole table
(see `docs/KNOWN_LIMITATIONS.md` §4). At 10k records a finish takes
seconds; at 100k a recall takes ~3 s. Workaround for now: keep
scopes small, or split a project into several scopes. Do not raise
timeouts and call it fixed.

## Database diagnosis

```sh
python -m turnkeep doctor --json            # quick status
python scripts/restore_check.py --fixture restart-handoff   # backup/restore round-trip
```

From Python, `turnkeep.backup.diagnose(path)` is read-only and never
repairs or deletes: it reports `OK` / `DEGRADED` (e.g. WAL frames not
yet checkpointed) / `CORRUPT` (missing file, bad header, failed
`integrity_check`) with a recommended action. See
`docs/recovery.md`.

## Exporting diagnostics for a bug report

Include:

1. `python -m turnkeep doctor --json` output.
2. The exact failing command (with `--json`) and its full stderr.
3. Turnkeep version (`python -m turnkeep --version`) and platform
   (`uname -a` / Python version).
4. Whether the database is a fresh fixture or a real project DB.

Do **not** include: the database file itself if it holds real project
content, any API key or token, real user data, or internal logs. If a
reproduction needs data, build it from the scripted demo fixtures
(`fixtures/demo/…`) or a synthetic scope and confirm the failure
reproduces there first.
