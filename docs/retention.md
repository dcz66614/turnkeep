# Retention and erasure (T038)

Three different operations, three different promises — do not confuse them.

| Operation | What happens to the payload | What happens to the record |
| --- | --- | --- |
| **Retract** (`turnkeep retract`) | Kept. The statement is marked untrue; the text stays in history. | state `retracted` |
| **Expire** (`turnkeep expire` + `sweep-expiry`) | Kept. The record leaves the active view at its explicit `valid_until`; children are tainted. No time-based probability decay is ever applied — a record with no `valid_until` never expires. | state `expired` |
| **Erase** (`turnkeep erase`) | Removed from the live DB (tombstone `{"_erased": true}`), from derived receipts, and purged from the live file with WAL checkpoint + VACUUM. | state `erased`, tombstone kept so lineage still resolves |

## Erasure scope

Controlled and scrubbed: `memories.value_json`, `runs.receipt_json` and
`idempotency.response_json` for receipts referencing the record; the
record's own source links whose id IS the payload are deleted, and a
receipt dump's key / source-ref id that IS the payload is replaced by
its HMAC. Verified afterwards by scanning every payload-bearing text
column for the payload — including four identifier columns scanned on
whole-value equality only (`events.affected_ids_json`,
`events.actor_binding`, `idempotency.idempotency_key`, `sources.id`):
an id that IS the payload is a copy; an id that merely contains it as
a substring is not. Any controlled table still containing it fails the
gate (`PARTIAL`).

Request digests are neutralized at erase time: a stored
`idempotency.request_digest` is a plain SHA-256 of the request, which
contains the raw value, so a low-entropy value would be recoverable
offline by enumerating candidates. Every idempotency row belonging to
the erased record has its digest replaced by the non-hex marker
`"erased"` (and a finish receipt's digest is removed from its runs
row too); `sources.content_digest` — a plain SHA-256 of the source
dict — is set NULL for every sources row whose id is the payload. A
row whose own `idempotency_key` IS the payload is deleted outright
(its key is part of the row's primary key and cannot be scrubbed).
HMAC is deliberately NOT used for this: the per-scope HMAC key lives
in the same database, so a database holder enumerates an HMAC just as
easily.

A replay of an erased operation raises `IDEMPOTENCY_MISMATCH` instead
of returning the stored response — the replay promise ("a replay
returns the original record even after it was corrected or retracted")
ends at erasure. The erase return dict never re-materialises the
payload either: its `"key"` is omitted and flagged `"key_redacted"`
whenever the key is payload material, by the same rule as the erase
event's metadata key.

Refused, never silently rewritten: if the payload is also in the
append-only `events` table, erasure returns `PARTIAL` and leaves the record
untouched — history is never rewritten. The honest path then is to export
the scope to a fresh DB and delete the old one.

## Residuals — what erasure cannot do

`turnkeep residuals` lists them; it never claims they are deleted:

- **Backups / exports** (`.bak`, export packages, old DB copies in the data
  dir): may still contain the payload. Turnkeep never deletes files outside
  the live DB — delete them yourself.
- **External client history**: an MCP/HTTP client that read the record
  before erasure may still hold it. Turnkeep cannot erase another client's
  memory.
- **Physical recovery**: WAL checkpoint + VACUUM purge the live file, but no
  tool can prove a deleted page is unrecoverable from SSD or filesystem
  backups.
- **Cross-record identifier residuals**: another record's idempotency key,
  or a live record's link to a source, whose id IS the erased payload —
  erasing that other record does not remove the key/link (it is the
  OTHER payload, not its own). The residual stays listed until the scope
  is exported to a fresh DB and the old one deleted.

The erasure receipt never copies the deleted content and never contains
anything that can directly restore it (no value, no value digest).
