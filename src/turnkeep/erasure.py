"""Payload erasure (T038). Explicit, per-record, and honest about its limits.

erase_record():
  1. Refuses when the payload also lives in an append-only table (events):
     history is never rewritten; the result is PARTIAL and the record is
     left untouched, with the reason recorded.
  2. Replaces the payload in memories.value_json with a tombstone
     ({"_erased": true}) and sets state='erased', revision+1 (CAS).
     Every descendant of the erased record is tainted in the same
     write transaction (CORE_CONTRACT §41/§47: an active record
     becoming unavailable propagates taint), and the erase event's
     affected ids include those tainted descendants.
  3. Scrubs the same payload out of derived copies the product controls:
     runs.receipt_json and idempotency.response_json for receipts that
     reference this record (value replaced by the tombstone marker; ids,
     keys and revisions are kept so the receipt stays a valid pointer).
     Receipts are recognised by owner id in EITHER shape — "record_id"
     (finish receipts) or "id" (dump_record() output: remember / correct
     / retract / resolve_conflict receipts) — including dumps nested in
     begin receipts' "records" / "conflicts" lists (P0-R3-01: the scrub
     used to recognise only {"record_id"} receipts, so every dump-shaped
     receipt survived erasure). A second erase_record() on an
     already-erased record re-runs the scrub + scan for that record
     (repair path: no resurrection, no second revision bump), so a
     PARTIAL erase left by the old scrub can still be completed.
     The repair path also clears illegal timestamp fields the first
     erase left behind (P0-R7-01c): a tombstoned row whose valid_until
     was written directly after the first erase is cleared the same
     way — valid_until to NULL, created_at/updated_at (NOT NULL) to
     the repair time — with no revision bump and no resurrection.
     The payload needles are the raw stored payload PLUS every string
     leaf obtained by recursively expanding the parsed JSON value
     (dicts and lists, all depths), so a secret nested inside a
     structured value is scrubbed and verified, not just the top level.
     The scrub also replaces a receipt dump's key / source-ref ids
     that ARE the payload (by hash — verbatim or in a known encoding),
     and deletes the erased record's own source links whose id IS the
     payload (P0-R8-01b / P1); a live record's link to the same source
     id is left alone — that record is excluded from export instead.
  4. Purges the live file with PRAGMA wal_checkpoint(TRUNCATE) — the
     checkpoint's (busy, log, checkpointed) return is READ (P0-R13-01):
     busy != 0 means the checkpoint did not complete (a concurrent read
     transaction holds a snapshot), so the deleted key's bytes may
     still be recoverable from the live DB file / WAL. Before the key
     is deleted, a maintenance_pending marker is written in the SAME
     transaction (P0-R14-01, crash-consistency): a crash after the
     erase transaction commits but before the checkpoint runs still
     leaves a persisted marker, so no recovery path can report the
     file cleanup as complete while it is unfinished. The marker is
     written UNCONDITIONALLY for every terminal COMPLETE erase — it
     covers the deleted key's bytes AND the old payload pages the
     checkpoint has not yet purged (on the first-erase path the
     payload bytes sit in the WAL from the remember frame even
     though its key never hit disk). On a busy checkpoint the key
     is RESTORED and the marker is refreshed with the checkpoint
     failure, the erase returns PARTIAL, and residual_report()
     reports not-clean until a later repair re-run retries the
     checkpoint successfully and clears the marker. A marker that
     survives a crash between checkpoint success and the clear is
     "purge unconfirmed" (fail-closed, possibly already clean) and
     is cleared only by a later repair re-run that re-runs the
     checkpoint. VACUUM runs after the checkpoint but is not the
     purge action: in this stack's WAL mode it compacts the b-trees
     (page_count drops, freelist -> 0) yet does not shrink the file
     and removes no deleted bytes beyond what the checkpoint already
     removed (under secure_delete=ON — set on every open_db() and
     gated at the erase entry, P0-R15-01 — a delete +
     checkpoint(busy=0) already removes the deleted key bytes from
     the live file; under OFF the deleted cell stays in the page
     freeblock and the checkpoint copies that page into the main
     file, and VACUUM's compact pages go to the WAL with no
     checkpoint after them, so the key stays recoverable — see
     purge_residue for the cleanup of such databases); its success
     says nothing about purge. After _run_maintenance the erase
     settlement runs a file-level purge ACCEPTANCE (_verify_purge)
     before the marker is cleared: a sliding-window probe over the
     main DB file + WAL must confirm the deleted key is not
     recoverable; on failure the erase is PARTIAL with the key
     restored and the marker kept, exactly like a busy checkpoint.
  5. Verifies by scanning every payload-bearing text column (see
     _PAYLOAD_COLUMNS: value_json, receipts, task labels, event
     metadata — plus four identifier columns scanned on whole-value
     equality only (P0-R10-02): events.affected_ids_json,
     events.actor_binding, idempotency.idempotency_key, sources.id,
     since an id that IS the payload is a copy; other enum /
     identifier columns are not scanned, since structural text such
     as "user_statement" or "remember" would otherwise match
     1-character payloads) for the needles; any
     controlled table still containing one fails the gate
     (PARTIAL). The scan is structural (P0-R8-02, P2-R8-03): it knows
     each cell's (table, column) and each string's position inside it,
     so a legal timestamp counts only at a real audit position (never
     inside user data, whatever its field name), and an identifier
     field counts only on whole-value equality (a random id that merely
     contains a short needle substring is not a copy). Whole-value
     equality is decode-aware (P0-R9-01/03): an identifier or a whole
     user-data value that decodes (Base64 / URL-safe Base64 / percent
     / reversed / hex / rot13) to a needle IS a copy — a decoded
     fragment merely containing a short needle substring is still not
     one. Decoded matching applies only to needles of at least 3
     characters (below that an encoding is indistinguishable from
     coincidence). An encoding inside unparseable free text (runs.task) is not
     scanned for (docs/KNOWN_LIMITATIONS.md §8). The scan
     outcome is persisted in the erase event's metadata (status +
     remaining table/record-key + needle HMACs only — never the
     payload value), so residual_report() can still see a controlled
     residual afterwards, when the payload itself is already tombstoned
     and its needles can no longer be reconstructed. An erase event
     never writes a raw key: a key that carries the payload is omitted
     and flagged "key_redacted" (P0-R8-01a) — the scan runs before the
     event is published, so it can never see the event's own key.
     residual_report() reports CONTROLLED_NOT_CLEAN while any persisted
     controlled residual is unresolved, and lists the affected record
     keys. Its `live_tables_clean` field means exactly: tombstone
     structure intact AND no persisted unresolved controlled residual
     AND no maintenance_pending marker AND no purge_pending marker
     (P0-R13-01 / P0-R14-01: a terminal COMPLETE erase whose WAL
     checkpoint was busy leaves the verification key recoverable
     from the live DB file / WAL, and a crash before the checkpoint
     leaves the same material unpurged with a marker standing — so
     the tables are not called clean while the purge is unconfirmed.
     P0-R16-03: a failed historical cleanup (purge_residue) leaves
     the same kind of standing marker, at scope level instead of
     per-record — the tables are not called clean until a successful
     purge_residue re-check clears it; a repair re-run never clears
     it. A marker is "purge not yet confirmed complete", never
     "file dirty": a stale marker after a crash between checkpoint
     success and the marker clear is a fail-closed false positive,
     cleared only by a later repair re-run that re-runs the
     checkpoint (erase markers) or a later purge_residue re-run
     (cleanup markers). The pending records are listed separately
     as `file_purge_pending`, so logical-table-clean and
     file-cleanup-incomplete stay distinguishable). It cannot
     detect a payload re-introduced into a live table after
     erasure (a COMPLETE erase persists no needle hashes at all — see
     _erase_event_hash_fields — and a PARTIAL erase persists them only
     as keyed hashes (HMACs), never as values), and it does not claim
     that — see residual_report().

The receipt never copies the deleted content and never contains anything
that can directly restore it (no value, no value digest).

No recoverable material in events either: a PARTIAL erase event carries
needle hashes as HMAC-SHA256 keyed by a per-scope secret stored in the
scope_secrets table (live DB only, never exported — see _needle_hashes).
A hash in an exported events.jsonl cannot be enumerated from the export
alone, even for a low-entropy needle such as a 4-digit PIN. Events
written before the HMAC fix carry raw SHA-256 hashes and stay enumerable
from exports already made; the fix protects new exports only. A COMPLETE
erase event carries NO needle hashes at all (P0-R11-02): the hashes
would be the only enumerable material for the payload in the live DB,
since the scope key sits in the same DB — so the event instead carries
a non-digest export-exclusion map (which records' identifiers were the
payload at erase time, as record ids and field names — never payload
material), and the blocked repair-reason labels go to the live-DB-only
erase_flags table (see _store_blocked_repair_reasons): a blocked label
is a fixed vocabulary word, and persisting it in an event would write
the payload itself into history when the payload is literally that label.
"Move the key out of the DB" was evaluated and rejected: this is a local-first
single-machine SQLite product with no external key management, and any
disk file under the same OS user is inside the same trust boundary, so
a sidecar key file would break portability for no real protection.

Request digests are neutralized at erase time (P0-R10-01): a stored
idempotency request_digest is a plain SHA-256 of the request, which
contains the raw value for remember / correct / finish /
resolve_conflict — a low-entropy value is recoverable offline by
enumerating candidates and re-computing the digest, while the erase
reports COMPLETE. At erase time every idempotency row whose stored
response belongs to the erased record has its digest replaced by the
non-hex marker "erased" (NEUTRALIZED_DIGEST), and a finish receipt's
digest is removed from the runs row too; sources.content_digest — a
plain SHA-256 of the source dict — is set NULL for every sources row
whose id is the erased payload. HMAC is NOT used for this: the
per-scope HMAC key lives in the same DB, so a DB holder enumerates an
HMAC just as easily (R10 verification repro A1b.2). A replay of the
erased operation after erasure therefore raises IDEMPOTENCY_MISMATCH
instead of returning the stored response — the replay promise ("a
replay returns the original record even after it was corrected or
retracted") ends at erasure. A row whose own idempotency_key IS the
erased payload is deleted outright (its key is part of the row's
primary key and cannot be scrubbed); another record's row whose key
is this payload is left alone and reported as a PARTIAL residual
instead (P0-R10-02c). The erase return dict never re-materialises
the payload either (P1-R10-03): its "key" is omitted and flagged
"key_redacted" whenever the key is payload material, by the same
rule as the erase event's metadata key — the CLI prints the return
dict to stdout.

The erase `reason` is one of a fixed allowlist of short labels
(P0-R6-01): free text is never persisted into the append-only event
log, because no verbatim check can be complete against it — a secret
glued into a longer token ("rotatedabcagain") or reversibly encoded
(Base64, percent-encoding, reversed, hex, rot13) contains no verbatim
needle, so the old substring/token check passed it and the recoverable
text landed in events.jsonl while residual_report() still said clean.
A reason outside the allowlist is rejected (INVALID_INPUT, naming the
allowed labels) and nothing is published. The same allowlist pattern
applies to retract's reason (RETRACT_REASONS). set_expiry's
valid_until is instead validated as an ISO-8601 timestamp with a
timezone — a non-timestamp is INVALID_INPUT, and an encoded secret
cannot be a timestamp. A reason leaked into an existing DB by the
pre-fix code cannot be scrubbed (append-only) — see
docs/KNOWN_LIMITATIONS.md §8.

A repair erase (second erase of an already-erased record, P0-R5-01)
accepts NO free text at all: its reason must be one of the controlled
labels {"repair", "rotation"} (REPAIR_REASONS), and the label is vetted
against what the first erase persisted — the needle hashes when the
first erase was PARTIAL, the blocked-repair-reasons flags when it was
COMPLETE (P0-R11-02: a COMPLETE erase persists no hashes) — so even a
label that is itself the erased value (a payload literally "repair") is
rejected. The label is what gets persisted in the repair
event's metadata ("reason"), never caller-supplied text — so no glued
secret can enter events through a repair erase, whatever its shape
("rotation:<secret>", "x<secret>y", ...). The first-erase path takes
the same shape now (P0-R6-01): its reason must be one of ERASE_REASONS
(the repair labels plus "cleanup"), hash/token-checked against the live
needles as a second layer, so a label that is itself the erased value
(a payload literally "repair", first-erased with reason "repair") is
rejected too. The token/substring needle check survives only as that
second layer behind the allowlist, everywhere — it is not a
completeness claim: the R5 docstring claim that the live-needle
substring check made the first-erase path "complete" was disproven
by P0-R6-01 (encodings and glued short secrets pass it).

What erasure CANNOT do (reported as residuals, never claimed as deleted):
  - copies the user already exported, backed up (.bak / export packages),
    or kept in another client (MCP/HTTP clients may have cached the record);
  - physical recoverability from SSD / filesystem backups — the WAL
    checkpoint only purges the live file; no tool can prove a deleted
    page is unrecoverable from storage media. That media-level boundary
    is NOT the same as a busy checkpoint (P0-R13-01): a checkpoint
    blocked by a concurrent read transaction leaves the key in the
    CURRENT live DB file / WAL, which is detectable and in scope — it
    is reported via the maintenance_pending marker, never silently
    accepted (see _run_maintenance).
"""
from __future__ import annotations

import base64
import codecs
import hashlib
import hmac
import json
import re
import secrets
import string
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

from .errors import TurnkeepError
from .idempotency import NEUTRALIZED_DIGEST
from .storage import (KINDS, SOURCE_TYPES, STATES, check_revision, dump_record,
                      ensure_scope, publish_event, row_or_404, taint_descendants,
                      transaction, _id, _json, _now, _text)

TOMBSTONE = {"_erased": True}
# Append-only tables: a payload found here can never be scrubbed by rewriting.
APPEND_ONLY = ("events",)
# Controlled derived copies that may hold the payload and can be scrubbed.
SCRUBBABLE = (("runs", "receipt_json"), ("idempotency", "response_json"))


def _string_leaves(value):
    """Yield every string leaf of a parsed JSON value, recursively.

    Dicts and lists are expanded to all depths; scalars other than str
    yield nothing. The empty string is not a usable needle (it matches
    every text cell) and is skipped."""
    if isinstance(value, str):
        if value:
            yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _string_leaves(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _string_leaves(v)


def _needles_for_payload(payload_json):
    """Needles for a stored payload: the raw JSON text plus every string
    leaf of the parsed value (same expansion erase_record() has always
    used, factored out so retract / set_expiry / repair can reuse it)."""
    needles = {payload_json}
    try:
        needles.update(_string_leaves(json.loads(payload_json)))
    except ValueError:
        pass
    return needles


# Timestamp fields of a memories row (P0-R7-01c). Every product write
# validates these as ISO-8601 timestamps with a timezone (retention /
# portability), so a field holding anything else can only be a pre-R6
# import or a direct write — legacy data, never a legitimate value.
_TIMESTAMP_FIELDS = ("valid_until", "created_at", "updated_at")

# Timestamp-valued fields inside audit JSON (P2-R11-03): the three
# memories-row timestamp columns, plus a finish receipt's
# "committed_at" (the commit time, written by commits.finish as an
# ISO-8601 timestamp with a timezone — the same format _now()
# produces). A legal timestamp under one of these names is legitimate
# audit text, not a copy of the payload — a short needle that is a
# substring of its own commit time ("202" in "2026-…") must not refuse
# the erase, any more than one inside its own expiry timestamp does
# (P0-R7-01c). Kept separate from _TIMESTAMP_FIELDS (memories-row
# columns, used by the legacy-timestamp clearing): "committed_at" is
# a receipt field, never a memories column.
_AUDIT_TIMESTAMP_FIELDS = _TIMESTAMP_FIELDS + ("committed_at",)


def _is_legal_timestamp(value):
    """True when value is a str that parses as an ISO-8601 timestamp
    WITH a timezone (P0-R7-01c).

    This is the value-aware test behind the legacy-timestamp handling:
    a legal timestamp is legitimate expiry/audit text — a needle that
    merely occurs inside it as a substring ("202" in "2026-…") is not
    a copy of the payload. Anything else (free text, an encoding, a
    naive timestamp, a non-string) is not a timestamp and is treated
    as suspect. The same predicate gates import (portability) and
    export stripping."""
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _illegal_timestamp_fields(row, needles=()):
    """Timestamp fields of a memories row that must not survive erasure
    (P0-R7-01c).

    A field qualifies when it is not a legal timestamp (free text, an
    encoding, a naive timestamp — only a pre-R6 import or a direct
    write can have put it there; every product write validates), OR
    when it equals a payload needle (the payload itself IS a legal
    timestamp stored as the expiry). A legal timestamp that merely
    contains a short needle as a substring is NOT listed — it is
    legitimate expiry text. Returns the field names, in
    _TIMESTAMP_FIELDS order."""
    bad = []
    for field in _TIMESTAMP_FIELDS:
        value = row[field]
        if value is None:
            continue
        if not _is_legal_timestamp(value) or value in needles:
            bad.append(field)
    return bad


# Opaque digest field names inside audit JSON (P2-R11-03). A string
# under one of these names is derived material, never raw payload: a
# needle_hash (HMAC hex), a request_digest (SHA-256 hex, or the
# "erased" marker), a content_digest (SHA-256 hex). Such a value must
# never be substring-matched — a short needle that happens to occur
# inside a hex digest ("202" inside a needle hash) is not a copy of
# the payload. Digests are matched only by hash equality, where a
# hash check exists at all (see _identifier_matches_hashes); the
# scan never treats one as raw text. Boundary, documented: a payload
# literally equal to a stored HMAC hex is a pathological corner the
# scan cannot distinguish from the digest itself — it is not a copy
# in any ordinary sense, and no product path writes such a value.
_DIGEST_FIELDS = frozenset({"needle_hashes", "request_digest", "content_digest"})


def _is_controlled_label(key, node):
    """True when node is a known controlled label under an audit field
    name (P2-R11-03).

    The field names below hold values from a fixed, closed set — a
    reason label, an erase status, a table name, a source type, a
    record kind / state, a run status, a receipt guarantee — so the
    value is audit text, not a payload copy: it counts only on
    whole-value equality (see _walk_holds_needle). A value OUTSIDE
    the set (a pre-fix free-text reason, an unknown kind) is NOT a
    controlled label and falls back to substring matching, fail-closed,
    so a leak already sitting in an existing DB is still detected by
    the scan. The sets are read at call time: ERASE_REASONS /
    RETRACT_REASONS / REPAIR_REASONS and _PAYLOAD_COLUMNS are defined
    below this function in the module.
    """
    if key == "reason":
        return node in ERASE_REASONS or node in RETRACT_REASONS
    if key == "erase_status":
        return node in ("COMPLETE", "PARTIAL")
    if key == "append_only_table":
        return node in APPEND_ONLY
    if key == "table":
        return node in _PAYLOAD_COLUMNS
    if key == "cleared_illegal_timestamps":
        return node in _TIMESTAMP_FIELDS
    if key == "export_excluded_identifiers":
        return node in ("id", "key", "source_id")
    if key == "type":
        return node in SOURCE_TYPES
    if key == "kind":
        return node in KINDS
    if key in ("state", "status_at_commit"):
        return node in STATES
    if key == "status":
        return node in ("open", "accepted", "succeeded")
    if key == "guarantee":
        return node == "local_sqlite_commit_only"
    return False


# Identifier field names inside a JSON cell (P0-R8-01, P2-R8-03). A
# string sitting under one of these names is an identifier, not
# payload text: it counts as a hit only when the WHOLE value equals
# the needle. A random id that merely contains a short needle as a
# substring ("202" inside a uuid) is not a copy and must not refuse
# the erase — that is the same whole-value rule the key / source-id
# policy uses everywhere (see _identifier_matches_hashes): the two
# sides of one rule, "equals the payload (or its encoding) = a copy;
# merely contains a short needle substring = not a copy".
_IDENTIFIER_KEYS = frozenset({"id", "key", "record_id", "cause_event_id", "run_id"})

# Columns whose cells ARE identifiers, not payload text (P0-R10-02).
# A hit in one of these counts only on whole-value equality (see
# _cell_holds_needle): events.affected_ids_json (a JSON array of
# record ids — one element equalling the payload means the event
# history names a record whose id IS the payload), events.actor_binding
# (the actor label of an event — a binding that IS the payload is a
# copy; one merely containing it as a substring is not),
# idempotency.idempotency_key, and sources.id. A future migration
# that adds a payload-bearing column must register it in
# _PAYLOAD_COLUMNS, and an identifier-bearing one here, or the scan
# will miss it — the same rule that left these four unregistered
# until R10. Note the "local" collision: the default actor_binding is
# the literal string "local", so a payload that IS "local" can never
# erase while any event carries that binding — whole-value equality
# is a copy even when coincidental (same stance as a payload
# literally "repair" erased with reason "repair", P0-R4-01); the
# remedy is export to a fresh DB (docs/KNOWN_LIMITATIONS.md §8/§23).
_IDENTIFIER_COLUMNS = frozenset({
    ("events", "affected_ids_json"),
    ("events", "actor_binding"),
    ("idempotency", "idempotency_key"),
    ("sources", "id"),
})


def _walk_holds_needle(node, needle, key, user_data):
    """Recursive structural needle check for one parsed JSON cell
    (P0-R8-02, P2-R8-03).

    The walk knows WHERE each string sits, which the R7 text-masking
    version did not: it tracked only the field NAME, so a `created_at`
    inside user data (a `memories.value_json` like
    `{"created_at": "2026-11-12T22:00:00+00:00"}`) was wrongly
    exempted as if it were audit text, and the payload it carried
    survived erasure. The rules, per string leaf:
      - user data (memories.value_json as a whole, or a receipt dump's
        "value" subtree — entered by descending into a "value" key):
        any substring occurrence is a hit. No exemption ever applies
        here, whatever the field name. A whole value that decodes
        (known set) to the needle is a hit too (P0-R9-03) — never a
        substring of a decoded value.
      - audit structure: a legal timestamp under a timestamp field
        name (valid_until / created_at / updated_at, plus a finish
        receipt's committed_at — P2-R11-03) is exempt UNLESS the
        needle IS that timestamp (the payload itself is that
        timestamp, stored verbatim);
      - audit structure: an identifier under an identifier field
        name counts only on whole-value equality (a random id that
        merely contains a short needle substring is not a copy) —
        where whole-value equality includes a KNOWN decoding of the
        identifier (P0-R9-01: Base64 / URL-safe Base64 / percent /
        reversed / hex / rot13), for needles of at least 3 characters
        (below that an encoding is indistinguishable from coincidence:
        a 1-char key "k" rot13-decodes to a 1-char payload "x");
      - audit structure: a controlled label under a controlled-label
        field name (a reason label, an erase status, a table name, a
        source type, a record kind / state, a run status — P2-R11-03)
        counts only on whole-value equality. The value is fixed audit
        text, not a payload copy: a short needle that is a substring
        of a label ("r" in "rotation", "r" in "user_statement") is
        not a copy. A value outside the label set (a pre-fix free-text
        reason) is not a controlled label and still matches on
        substring, fail-closed. P1-R12-02: whole-value equality
        includes the REVERSE decode direction — a label that the
        needle decodes to (the payload is an encoding of the label,
        e.g. Base64("COMPLETE")) IS a copy, because writing that
        label stores a value the payload decodes from. The >= 3-char
        floor of _decoded_equals_needle applies (see
        _label_is_payload_copy): a 1-char payload never matches a
        label by decoding;
      - audit structure: an opaque digest under a digest field name
        (needle_hashes, request_digest, content_digest — P2-R11-03)
        never matches on substring: a digest is derived material, not
        raw payload. A short needle occurring inside a hex digest
        ("202" inside a needle hash) is not a copy;
      - audit structure: a dict KEY name is not a payload carrier
        (P2-R11-03). Key names are fixed schema words — a payload
        cannot be written through a key name (user strings only go
        into values) — so in audit structure a key name is never
        substring-matched. Inside user data ("value" subtrees) key
        names ARE still matched: that is user data, and a 1-char
        payload colliding with a user-data key name ("e" in "key")
        still reports PARTIAL, fail-closed (P1-R4-06, honest limit —
        see docs/KNOWN_LIMITATIONS.md §8);
      - the tombstone marker {"_erased": true} is structural, not a
        copy: a needle that is a substring of the marker ("r" / "e" /
        "_" in "_erased", "true") never counts, wherever the marker
        sits — a receipt dump's "value" subtree holding the tombstone
        is the erased record, not a payload copy (P2-R11-03);
      - numbers / booleans / null in audit structure are structural
        (counts, flags) and never match; in user data a numeric leaf
        can BE the payload, so it still matches on substring;
      - anything else in audit structure (an unknown field's free
        text) counts on substring, as before — fail-closed.
    An unparseable cell is handled by the caller (raw text,
    fail-closed)."""
    if isinstance(node, str):
        if user_data:
            if needle in node:
                return True
            # P0-R9-03: a whole value that decodes to the needle IS a
            # copy, even when no verbatim needle occurs (record B holds
            # Base64(payload) as its whole value). Whole-value decoded
            # equality ONLY — never substring-match a decoded fragment,
            # or the "202" false-positive class returns (P2-R8-03: a
            # random id's hex decoding must not be flagged). The
            # length floor is part of the rule (see
            # _decoded_equals_needle): 1-2 char payloads match verbatim
            # only.
            return _decoded_equals_needle(node, needle)
        if key in _AUDIT_TIMESTAMP_FIELDS and _is_legal_timestamp(node):
            return node == needle
        if key in _IDENTIFIER_KEYS or key == "scrubbed":
            # P0-R9-01: an identifier that decodes (whole-value) to the
            # needle IS the payload — an encoded key in event metadata
            # (remember events carry metadata.key verbatim) or a receipt
            # dump's key / source-ref id. Whole-value equality only, as
            # before: a random id merely containing a short needle
            # substring is not a copy (P2-R8-03). The length floor is
            # part of the rule (see _decoded_equals_needle): a 1-char
            # key "k" rot13-decodes to a 1-char payload "x" by
            # coincidence, and must not refuse the erase.
            return node == needle or _decoded_equals_needle(node, needle)
        if key in _DIGEST_FIELDS:
            # P2-R11-03: an opaque digest is derived material, never
            # raw payload — it must never be substring-matched (see
            # _DIGEST_FIELDS). A payload literally equal to a stored
            # HMAC hex is the documented pathological corner, not a
            # copy in any ordinary sense.
            return False
        if _is_controlled_label(key, node):
            # P2-R11-03: a controlled label counts only on whole-value
            # equality (see _is_controlled_label). A short needle that
            # is a substring of a label ("r" in "rotation") is audit
            # text, not a copy. P1-R12-02: whole-value equality
            # includes the reverse decode direction — the label is a
            # decoded form of the needle (see _label_is_payload_copy):
            # a record whose value is Base64("COMPLETE") carries the
            # label "COMPLETE" in every erase event that stores it.
            return node == needle or _decoded_equals_needle(needle, node)
        return needle in node
    if isinstance(node, dict):
        # P2-R11-03: the tombstone marker is structural, never a copy
        # (see the docstring): a needle inside "_erased" / "true" is
        # the marker saying the value is gone, not the payload.
        if node == TOMBSTONE:
            return False
        for k, v in node.items():
            # P2-R11-03: in audit structure a dict KEY name is a fixed
            # schema word, not a payload carrier — it is never
            # substring-matched. Inside user data ("value" subtrees)
            # key names are still matched (P1-R4-06, honest limit).
            if user_data and _walk_holds_needle(k, needle, key, user_data):
                return True
            if _walk_holds_needle(v, needle, k, user_data or k == "value"):
                return True
        return False
    if isinstance(node, list):
        for v in node:
            if _walk_holds_needle(v, needle, key, user_data):
                return True
        return False
    # Numbers / booleans / null: in user data a numeric leaf can BE the
    # payload, so it still matches on substring; in audit structure a
    # count or flag is structural, never a payload copy (P2-R11-03).
    return needle in json.dumps(node) if user_data else False


def _cell_holds_needle(cell_text, needle, table, column):
    """True when a payload-bearing cell holds the needle (P0-R8-02, P2-R8-03).

    Structural, not textual: the cell is parsed and walked with its
    (table, column) as context, so the check knows whether each
    string is user payload, an audit timestamp, or an identifier
    (see _walk_holds_needle). Two fast paths first: a needle absent
    from the raw text cannot be present; a needle that IS itself a
    JSON object/array can only occur in the cell as that exact value
    (its braces, quotes and structure protect it from matching inside
    an identifier or a timestamp), so an occurrence is a hit without
    further analysis. A needle that is the raw JSON of a string
    payload needs no such path: its unquoted leaf is checked by the
    walk, and the leaf is what a copy would carry. P0-R9-01/03: a
    cell holding only an ENCODED copy contains no verbatim needle, so
    the fast path also admits rows holding any encoding of the needle
    — the structural walk below still decides, by whole-value decoded
    equality. An unparseable cell (runs.task free text) is a hit only
    on a VERBATIM needle: an encoded form in free text has no value
    boundary, so whole-value decoded equality cannot apply there —
    the retained runs.task limitation (docs/KNOWN_LIMITATIONS.md §8)."""
    # P0-R9-01/03: admit rows holding an encoding of the needle too;
    # the walk decides, so the wider filter cannot create hits.
    # P1-R12-02: admit rows holding a DECODING of the needle too — a
    # controlled label (erase_status "COMPLETE", a reason "rotation")
    # is a decoded form of the payload, so the row holding it contains
    # no verbatim needle and no encoding of it, only the label. The
    # walk still decides every hit (whole-value decode equality for
    # labels), so the wider filter cannot create hits by itself.
    if needle not in cell_text and not any(
            form in cell_text
            for form in _encoded_forms(needle) | _decoded_forms(needle)
            if form != needle):
        return False
    # P0-R10-02: identifier columns hold identifiers, not payload text
    # — a hit counts only on whole-value equality (verbatim or a known
    # decoding, >= _MIN_DECODED_MATCH_LEN), never on substring. A
    # random id that merely contains a short needle substring is not a
    # copy (P2-R8-03, same rule as identifier fields inside JSON
    # cells). events.affected_ids_json is a JSON array of ids: each
    # element is checked whole-value. The other identifier columns
    # are single strings: the whole cell is the identifier.
    if (table, column) in _IDENTIFIER_COLUMNS:
        if column == "affected_ids_json":
            try:
                ids = json.loads(cell_text)
            except ValueError:
                return False  # not a JSON array; cannot hold an id
            if not isinstance(ids, list):
                return False
            return any(isinstance(i, str)
                       and (i == needle or _decoded_equals_needle(i, needle))
                       for i in ids)
        return cell_text == needle or _decoded_equals_needle(cell_text, needle)
    if needle in cell_text and needle[:1] in ("{", "["):
        return True
    try:
        data = json.loads(cell_text)
    except ValueError:
        return needle in cell_text  # unparseable (runs.task free text): raw text matched, fail-closed
    # memories.value_json is user payload through and through: no audit
    # positions, no identifier positions, no exemption. Every other
    # scanned cell is audit structure whose "value" subtree is user data.
    return _walk_holds_needle(data, needle, None, table == "memories")


def _scan_payload_value_aware(db, needle, scope_id):
    """_scan_payload minus hits explained solely by an audit timestamp
    or by an identifier that merely contains the needle (P0-R7-01c,
    P0-R8-02, P2-R8-03).

    _scan_payload stays the coarse SQL LIKE filter, unchanged; this
    re-checks each hit row's payload cells in Python, per (table,
    column), and keeps only rows where the needle still occurs as a
    copy (see _cell_holds_needle). Raw _scan_payload is unchanged —
    callers that need the unfiltered hits (tests probing the scan
    itself) keep them. P0-R9-01/03: rows holding only an ENCODED copy
    are fetched too (every encoding of the needle is LIKE'd); the
    structural re-check still decides, by whole-value decoded
    equality, so the wider filter cannot create hits by itself.
    P1-R12-02: rows holding only a DECODED copy are fetched too (every
    decoding of the needle is LIKE'd) — a controlled label such as
    erase_status "COMPLETE" is a decoded form of the payload, so
    without this the row holding it would never reach the walk."""
    raw = _scan_payload(db, needle, scope_id)
    for form in _encoded_forms(needle) | _decoded_forms(needle):
        if form == needle:
            continue
        for table, rows in _scan_payload(db, form, scope_id).items():
            raw.setdefault(table, []).extend(rows)
    if not raw:
        return {}
    kept = {}
    for table, rowids in raw.items():
        existing = {r[1] for r in db.execute(f"PRAGMA table_info({table})").fetchall()}
        cols = sorted(_PAYLOAD_COLUMNS[table] & existing)
        if not cols:
            continue
        for rowid in sorted(set(rowids)):
            row = db.execute(
                f"SELECT {', '.join(cols)} FROM {table} WHERE rowid=?",
                (rowid,)).fetchone()
            if row is None:
                continue
            if any(cell is not None
                   and _cell_holds_needle(cell, needle, table, col)
                   for cell, col in zip(row, cols)):
                kept.setdefault(table, []).append(rowid)
    return kept


# A first erase accepts no free text either (P0-R6-01): its reason
# must be one of these short labels. Each label is legitimate audit
# text for why a record is being erased, and nothing else is ever
# persisted as the reason in an erase event:
#   "rotation" — credential/secret rotation (the privacy-driven erase:
#     the record held a secret that has been rotated, so the stored
#     copy must go);
#   "repair" — re-erase/repair of a prior erase (a first erase may
#     also be labelled "repair" when it is completing/cleaning up
#     after an earlier partial erase of the same material);
#   "cleanup" — data-hygiene deletion of records no longer needed.
# The labels are fixed strings, so no caller text — glued, encoded,
# or otherwise — can reach the append-only event log through the
# reason. The label actually used is still vetted against the live
# needles (see _check_reason_against_needles, second layer), so a
# label that IS the erased value (a payload literally "repair") is
# rejected too.
ERASE_REASONS = frozenset({"cleanup", "repair", "rotation"})

# A retract accepts no free text either (P0-R6-01): its reason must be
# one of these short labels. Each label is the correction semantics
# the product's own tests already use, and nothing else is ever
# persisted as the reason in a retract event:
#   "correction" — the recorded value is no longer true;
#   "superseded" — replaced by newer information;
#   "withdrawn" — the statement is withdrawn.
RETRACT_REASONS = frozenset({"correction", "superseded", "withdrawn"})


def _check_controlled_reason(reason, allowed, noun):
    """Gate a reason against a controlled-label allowlist (P0-R6-01).

    The allowlist runs FIRST, before any needle check and before
    anything is published: a reason outside it is INVALID_INPUT
    naming the allowed labels, whatever it contains. Only a label
    survives to be persisted in event metadata, so no free text —
    and therefore no glued or reversibly-encoded secret — can enter
    the append-only event log through a reason. Raises INVALID_INPUT;
    publishes nothing.
    """
    if reason not in allowed:
        labels = ", ".join(f"'{label}'" for label in sorted(allowed))
        raise TurnkeepError(
            "INVALID_INPUT",
            f"{noun} reason must be one of the controlled labels "
            f"{labels}: a reason is persisted verbatim into the "
            "append-only event log, so no free text is accepted here")


# A repair erase (second erase of an already-erased record) accepts
# no free text: its reason must be one of these short labels (P0-R5-01).
# They are the only labels existing tests use for repair erases, and a
# label is all a repair event needs to say — the repair re-runs the
# scrub + scan, it carries no new information. Anything else is
# INVALID_INPUT, and the label actually used is hash-checked against
# the stored needle hashes, so a label that IS the erased value (a
# payload literally "repair") is rejected too.
# REPAIR_REASONS stays the two-label subset {"repair", "rotation"} of
# ERASE_REASONS, not an alias of it: "cleanup" is a first-erase
# hygiene label, not a repair label, so the repair path's behaviour
# is identical to the R5 rework (same allowlist, same stored-hash
# check). A repair erase labelled "cleanup" is INVALID_INPUT.
REPAIR_REASONS = frozenset({"repair", "rotation"})


# Second layer behind the allowlist (P0-R6-01): a controlled label is
# rejected when it contains a payload needle of at least this length,
# or equals a needle of any length, or contains a needle as a
# standalone TOKEN of any length (P0-R4-01). The only input that
# reaches this check now is a label, so the 1-3 character false
# positives that used to constrain free-text reasons ("api" in
# "capital", "x" in "box") cannot occur here — but the length-4
# threshold and the token rule are kept unchanged, because they are
# what catch a label that IS the erased value (a payload literally
# "repair", erased with reason "repair").
_MIN_REASON_NEEDLE_LEN = 4


def _reason_tokens(reason):
    """Candidate tokens of a reason label, for needle matching.

    The whole reason, each whitespace-delimited token with surrounding
    punctuation stripped, and each maximal alphanumeric run inside those
    tokens. The union matters: a hyphenated secret such as
    "sk-LONG_SYNTHETIC_SECRET_247890" survives as one whitespace token
    (so it matches a stored needle hash in the repair path), while a
    short leaf such as "abc" survives as an alphanumeric run (so it
    matches in the live path). A needle glued inside a longer run with
    no boundary ("xabcx") produces no matching token — which is why the
    repair path no longer accepts free text at all (P0-R5-01: only the
    REPAIR_REASONS labels, hash-checked against the stored needles).
    Token matching is now only a second layer behind the allowlist
    everywhere (P0-R6-01: first erase uses ERASE_REASONS, retract uses
    RETRACT_REASONS, and the needle check then catches a label that is
    itself the erased value). It is NOT a completeness claim: a
    reversible encoding contains no verbatim needle at all, so no
    token/substring matching over the reason can catch it — only the
    allowlist closes that class, by never persisting free text."""
    candidates = {reason}
    for tok in reason.split():
        stripped = tok.strip(string.punctuation)
        if stripped:
            candidates.add(stripped)
        for sub in re.split(r"[^A-Za-z0-9]+", tok):
            if sub:
                candidates.add(sub)
    return candidates


def _check_repair_reason(db, scope_id, record_id, reason):
    """Gate a repair-erase reason (P0-R5-01): allowlist, then hash check.

    A repair erase persists its reason verbatim into the append-only
    event log, and the payload values are already gone — so token or
    substring matching against live needles cannot be complete (a secret
    glued into surrounding text with no token boundary, "x<secret>y",
    produces no matching token, which was the documented residual risk
    of the P0-R4-02 hash vetting). The fix is to accept no free text at
    all: the reason must be one of REPAIR_REASONS, and the label is
    then vetted against the needle hashes the first erase persisted
    (see _check_reason_against_stored_hashes) — a label that is itself
    the erased value (a payload literally "repair", repaired with
    reason "repair") is rejected. The label is what gets persisted in
    the repair event's metadata, never caller-supplied text. Raises
    INVALID_INPUT naming the allowed labels; publishes nothing.
    """
    if reason not in REPAIR_REASONS:
        raise TurnkeepError(
            "INVALID_INPUT",
            "repair-erase reason must be one of the controlled labels "
            "'repair', 'rotation': a repair erase persists its reason "
            "verbatim into the append-only event log, so no free text "
            "is accepted here; use 'repair' or 'rotation'")
    _check_reason_against_stored_hashes(db, scope_id, record_id, reason)


def _check_reason_against_needles(reason, needles):
    """Reject a controlled label that IS (or carries) erased-payload
    material (P0-R3-02, P0-R4-01).

    This is now the SECOND layer behind the allowlist (P0-R6-01): the
    caller gates the reason against ERASE_REASONS / RETRACT_REASONS /
    REPAIR_REASONS first, and this check then vets the surviving label
    against the payload needles — a label that is itself the erased
    value (a payload literally "repair", erased with reason "repair")
    is rejected here. On its own this check cannot be complete (a
    glued or reversibly-encoded secret contains no verbatim needle),
    which is why free text is no longer accepted at all; it survives
    only to catch the label-equals-needle corner the allowlist cannot
    see.

    Event metadata is copied verbatim into events and exported into
    events.jsonl, and the events table is never rewritten — so a reason
    containing the payload is an undeletable copy of the deleted
    content, and it would also make a later erase refuse (step-1
    append-only check). The check must run BEFORE any event is
    published. The error never quotes the needle itself.
    P1-R12-02: the reverse direction also rejects — a reason label
    that IS a decoded form of a needle (the payload is an encoding of
    the label, e.g. Base64("rotation")) would store a value the
    payload decodes from, so writing it leaks the payload exactly as
    writing the payload itself would. See _label_is_payload_copy.
    """
    tokens = _reason_tokens(reason)
    for needle in sorted(needles):
        if not needle:
            continue
        if needle == reason or needle in tokens or (
                len(needle) >= _MIN_REASON_NEEDLE_LEN and needle in reason):
            raise TurnkeepError(
                "INVALID_INPUT",
                "reason must not contain the record's payload: free text is "
                "copied verbatim into the append-only event log, where it "
                "can never be erased; use a short label instead "
                "(e.g. 'rotation')")
    if _label_is_payload_copy(reason, needles):
        raise TurnkeepError(
            "INVALID_INPUT",
            "reason must not contain the record's payload: free text is "
            "copied verbatim into the append-only event log, where it "
            "can never be erased; use a short label instead "
            "(e.g. 'rotation')")


# A decoded match is evidence only when the needle is at least this
# long (P0-R9-01/03): below it an encoding is indistinguishable from
# coincidence — every single letter is the rot13 of another single
# letter, so a key "k" would "decode" to a payload "x" and no 1-char
# payload could ever erase (R4 incidental-substring guard). Verbatim
# whole-value equality has NO floor (a key that IS the payload is a
# copy at any length); the floor applies only to the DECODED branch.
# 3 covers every required case ("aa>", "202", "2026") while keeping
# 1-2 char payloads verbatim-only.
_MIN_DECODED_MATCH_LEN = 3


def _decoded_forms(value):
    """The value plus every reversible decoding of it (P0-R8-01d).

    Known set, matching the encodings the final-review repros use:
    Base64, URL-safe Base64, percent-encoding, reversed, hex, rot13.
    Best-effort: a
    form that fails to decode yields nothing. The set is incomplete
    by construction (R6 fix-space probe: hex and rot13 pass even with
    b64/pct/rev in the set, and an attacker can always invent a new
    one) — so this is a second layer behind the allowlists and the
    export hash check, never a completeness claim. The residual
    incompleteness is documented in docs/KNOWN_LIMITATIONS.md §8/§21,
    not claimed as closed."""
    if not isinstance(value, str) or not value:
        return set()
    forms = {value}
    padded = value + "=" * (-len(value) % 4)
    # P0-R9-02: base64.urlsafe_b64decode takes NO validate argument
    # (its signature is urlsafe_b64decode(s)) — passing validate=True
    # raised TypeError on every call, swallowed by the except below,
    # so URL-safe forms NEVER entered the decode set and a payload
    # such as "aa>" ("YWE-") slipped through every gate that reads
    # this set (import, export hash check, receipt scrub).
    # b64decode takes validate=True; urlsafe_b64decode is called bare.
    for decoder, kwargs in ((base64.b64decode, {"validate": True}),
                            (base64.urlsafe_b64decode, {})):
        try:
            forms.add(decoder(padded, **kwargs).decode("utf-8"))
        except Exception:  # noqa: BLE001 - a failed decode yields nothing
            pass
    try:
        forms.add(urllib.parse.unquote(value))
    except Exception:  # noqa: BLE001 - a failed decode yields nothing
        pass
    forms.add(value[::-1])
    try:
        forms.add(bytes.fromhex(value).decode("utf-8"))
    except Exception:  # noqa: BLE001 - a failed decode yields nothing
        pass
    try:
        forms.add(codecs.encode(value, "rot_13"))
    except Exception:  # noqa: BLE001 - a failed decode yields nothing
        pass
    return {f for f in forms if isinstance(f, str) and f}


def _encoded_forms(value):
    """Every reversible encoding of a value (P0-R9-01, P0-R9-03).

    The inverse direction of _decoded_forms: the forms a copy of this
    payload can take. Known set, the same six: Base64, URL-safe
    Base64, percent-encoding, reversed, hex, rot13. Used ONLY to widen
    the SQL LIKE candidate filter in _scan_payload_value_aware: a copy
    stored in a known encoding contains no verbatim needle, so the
    LIKE filter would never surface its row for the structural
    re-check. The structural walk still decides every hit, by
    whole-value decoded equality — never by substring-matching a
    decoded fragment — so the wider filter cannot create hits by
    itself (see _walk_holds_needle)."""
    if not isinstance(value, str) or not value:
        return set()
    forms = {value}
    try:
        forms.add(base64.b64encode(value.encode("utf-8")).decode("ascii"))
    except Exception:  # noqa: BLE001 - a failed encode yields nothing
        pass
    try:
        forms.add(base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii"))
    except Exception:  # noqa: BLE001 - a failed encode yields nothing
        pass
    try:
        forms.add(urllib.parse.quote(value))
    except Exception:  # noqa: BLE001 - a failed encode yields nothing
        pass
    forms.add(value[::-1])
    try:
        forms.add(value.encode("utf-8").hex())
    except Exception:  # noqa: BLE001 - a failed encode yields nothing
        pass
    try:
        forms.add(codecs.encode(value, "rot_13"))
    except Exception:  # noqa: BLE001 - a failed encode yields nothing
        pass
    return {f for f in forms if isinstance(f, str) and f}


def _decoded_equals_needle(value, needle):
    """True when a value decodes (known set) to the needle, whole-value
    only (P0-R9-01/03). The length floor (_MIN_DECODED_MATCH_LEN) is
    part of the rule, not a tuning knob: below it an encoding is
    indistinguishable from coincidence (see the constant above), so
    1-2 char payloads match verbatim only. The decoded form equals
    the needle, so the floor is on the needle's length."""
    return (len(needle) >= _MIN_DECODED_MATCH_LEN
            and needle in _decoded_forms(value))


def _carries_needle(value, needles):
    """True when a value carries a payload needle (P0-R8-01a).

    Same rules as _check_reason_against_needles (the second layer
    behind the allowlists): the value equals a needle, contains a
    needle as a standalone token, or contains a needle of at least
    _MIN_REASON_NEEDLE_LEN characters as a substring — applied to
    the value and to each of its decoded forms. Used where a value
    is about to be persisted into append-only history (an erase
    event's metadata key): a match means the value must be redacted,
    not written raw. It is deliberately stricter than the erase
    scan's identifier rule (whole-value equality): at a write
    boundary into append-only history a false redaction costs a
    missing key label, while in the scan a false hit costs a refused
    erase, so the two gates use different thresholds on purpose. It
    is NOT a completeness claim: an unknown encoding contains no
    verbatim needle."""
    if not isinstance(value, str) or not value:
        return False
    for form in _decoded_forms(value):
        tokens = _reason_tokens(form)
        for needle in needles:
            if not needle:
                continue
            if needle == form or needle in tokens or (
                    len(needle) >= _MIN_REASON_NEEDLE_LEN and needle in form):
                return True
    return False


def _identifier_equals_needle(value, needles):
    """True when an identifier IS a payload needle (P0-R8-01d).

    Whole-value equality only, applied to the value and to each of
    its decoded forms: the identifier equals a needle verbatim, or
    decodes (Base64 / URL-safe Base64 / percent / reversed / hex /
    rot13) to one. This
    is the import boundary's rule for keys and source ids, and the
    same whole-value rule the erase scan uses for identifier fields
    (P2-R8-03): a random id that merely contains a short needle
    substring is not a copy, and is not rejected here either. A key
    that merely CONTAINS a longer needle as a substring is likewise
    not rejected — that is the documented residual in
    docs/KNOWN_LIMITATIONS.md §21, applied consistently at import,
    in the scan, and at export, rather than one gate using substring
    matching while the others use equality."""
    if not isinstance(value, str) or not value:
        return False
    for form in _decoded_forms(value):
        if form in needles:
            return True
    return False


def _label_is_payload_copy(label, needles):
    """True when a controlled label IS (or decodes from) a payload
    needle (P1-R12-02).

    The reverse direction of _identifier_equals_needle: the LABEL is a
    decoded form of the needle — the needle (payload) is an encoding
    of the label (e.g. Base64("COMPLETE") == "Q09NUExFVEU=", so a
    record whose value is "Q09NUExFVEU=" carries the label
    "COMPLETE" in every erase event that stores it). Whole-value
    only, with the >= 3-char floor of _decoded_equals_needle: a 1-2
    char label cannot be distinguished from coincidence, so it never
    matches by decoding. Every controlled label is >= 4 chars
    ("fact" / "open" are the shortest), so the floor never hides a
    real label hit — and a 1-char payload ("r", Base64("r") ==
    "cg==") still erases COMPLETE, since no label decodes from it.
    Used for the labels the erase itself writes or the record carries:
    the reason (see _check_reason_against_needles), the erase_status
    (see erase_record / _try_repair_erased), and the record's own
    kind (see _label_matches_stored_hashes for the repair path)."""
    if not isinstance(label, str) or not label:
        return False
    for needle in needles:
        if not needle:
            continue
        if label == needle or _decoded_equals_needle(needle, label):
            return True
    return False


def _label_matches_stored_hashes(db, scope_id, record_id, label):
    """True when a controlled label encodes to a payload whose hashes
    this record's erase events persisted (P1-R12-02, repair path).

    The hash form of _label_is_payload_copy: the repair's live needles
    are receipt-derived and empty once the receipts are scrubbed, so
    the label is checked against the stored hashes instead — hash the
    label's ENCODINGS (_encoded_forms) and compare them with the
    stored set under the record's verification keys. A deleted
    per-record key verifies nothing (the record is terminal COMPLETE
    then, so no label residual can exist); the blocked-reason flags
    cover the COMPLETE case for repair reasons. The raw SHA-256
    branch covers pre-fix events."""
    stored = _stored_needle_hashes(db, scope_id, record_id)
    if not stored:
        return False
    keys = _record_verification_keys(db, scope_id, record_id)
    for form in _encoded_forms(label):
        if not form:
            continue
        digests = {hashlib.sha256(form.encode("utf-8")).hexdigest()}
        for key in keys:
            digests.add(hmac.new(key, form.encode("utf-8"),
                                 hashlib.sha256).hexdigest())
        if digests & stored:
            return True
    return False


def _report_key_redacted(key, needles, *, db, scope_id, erased_by_record=None):
    """True when a key in an export / import REPORT entry must be
    redacted (P1-R9-04) — the one unified rule for every report
    surface: export_scope's records_excluded_secrets and
    stripped_illegal_timestamps, and _plan_import's skipped_existing
    and quarantined entries. A redacted entry omits "key" and carries
    "key_redacted": True instead, the same convention erase events
    and residual_report() already use (P0-R8-01a).

    A key is redacted when it IS payload material:
      - whole-value equality, verbatim or in a known decoding, against
        the record's own value needles (_identifier_equals_needle);
      - whole-value equality, verbatim or in a known decoding, against
        the needle hashes of payloads erased in this scope
        (_identifier_matches_any_erased) — a tombstone row's key, or a
        cross-record copy, whose own value no longer carries the
        payload. P0-R12-01: the check is per-record-keyed (the hashes
        are grouped by the erase event's first affected record, see
        _erased_payload_hashes_by_record), so a hash whose record's
        key was retired at terminal COMPLETE verifies nothing and
        never redacts a key;
      - a decoded form of the key (length >= 4) occurs INSIDE a
        needle — the key decodes to material glued into a needle, e.g.
        a Base64 key whose payload sits inside an illegal timestamp
        value ("x<payload>y"). The length floor is _carries_needle's
        (>= 4): a 1-3 char decoded fragment matching inside a longer
        needle is noise, not a copy.
    A false redaction costs only a missing key label, never a refused
    erase — so the substring direction, forbidden in the erase scan
    (P2-R8-03), is allowed here, where the scan's whole-value rule
    would leave the stripped / quarantined surfaces leaking. Benign
    keys ("ci.api_key", "cfg.api", "k1", "b") match none of the three
    and stay visible, so existing report assertions on benign keys
    are unaffected."""
    if not isinstance(key, str) or not key:
        return False
    if _identifier_equals_needle(key, needles):
        return True
    if erased_by_record and _identifier_matches_any_erased(
            db, scope_id, key, erased_by_record):
        return True
    for form in _decoded_forms(key):
        if len(form) < _MIN_REASON_NEEDLE_LEN:
            continue
        for needle in needles:
            if needle and form in needle:
                return True
    return False


def _iter_record_dumps(data):
    """Yield every record-dump dict inside a parsed receipt.

    A dump is a dict with a "value" field and an owning id in "id"
    (dump_record() output: remember / correct / retract /
    resolve_conflict receipts) or "record_id" (finish receipts). The
    receipt itself is yielded when it is a dump, plus every dump nested
    in lists/dicts — begin receipts nest full dumps inside "records"
    (and "conflicts") lists. A receipt with no "value" field holds
    nothing to leak and is never yielded (P0-R3-01: the scrub used to
    recognise only the top-level {"record_id"} shape, so every
    dump-shaped receipt survived erasure)."""
    if isinstance(data, dict):
        if "value" in data and ("id" in data or "record_id" in data):
            yield data
        for v in data.values():
            yield from _iter_record_dumps(v)
    elif isinstance(data, list):
        for v in data:
            yield from _iter_record_dumps(v)


def _dump_owner(dump):
    """The record id a receipt dump belongs to, from whichever field
    holds it ("record_id" for finish receipts, "id" for dump_record()
    output)."""
    return dump.get("record_id") or dump.get("id")


def _value_contains_needle(value, needles):
    """True when a dump's value carries any needle (canonical JSON text
    contains it, or one of its string leaves equals it). P0-R9-03: a
    leaf whose WHOLE value decodes (known set) to a needle is a copy
    too — a receipt dump holding Base64(payload) as its value. Never
    substring-match a decoded fragment (P2-R8-03)."""
    if not needles:
        return False
    try:
        text = _json(value)
    except TurnkeepError:
        text = None
    if text is not None and any(n in text for n in needles if n):
        return True
    leaves = list(_string_leaves(value))
    if any(leaf in needles for leaf in leaves):
        return True
    return any(_decoded_equals_needle(leaf, needle)
               for leaf in leaves for needle in needles if needle)


def _receipt_holds_value(db, scope_id, record_id):
    """True when any receipt in scope still holds a non-tombstone value
    for a dump owned by record_id (live check, used when deciding
    whether a persisted controlled residual is really resolved).

    Extended (P1, P0-R8-01b): also True when a dump owned by
    record_id still holds a key or source-ref id that IS the record's
    erased payload. The payload itself is gone (tombstoned), so the
    check is by hash: each identifier (verbatim, and in every known
    decoding) is hashed both ways and compared with the needle hashes
    the record's erase events persisted. A random id that merely
    contains a short needle substring never matches (whole-value
    rule, P2-R8-03). When the record has no persisted hashes yet
    (no erase event), the extension is a no-op and only the value
    check applies."""
    stored = _stored_needle_hashes(db, scope_id, record_id)
    keys = _record_verification_keys(db, scope_id, record_id) if stored else []
    for table, col in SCRUBBABLE:
        for (raw,) in db.execute(
                f"SELECT {col} FROM {table} WHERE scope_id=?", (scope_id,)).fetchall():
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            for dump in _iter_record_dumps(data):
                if _dump_owner(dump) != record_id:
                    continue
                if dump.get("value") != TOMBSTONE:
                    return True
                if isinstance(dump.get("key"), str) and _identifier_matches_hashes(
                        dump["key"], stored, keys):
                    return True
                for src in dump.get("source_refs", []) or []:
                    if (isinstance(src, dict) and isinstance(src.get("id"), str)
                            and _identifier_matches_hashes(src["id"], stored, keys)):
                        return True
    return False


def _tombstone_structure_clean(db, scope_id, erased_ids):
    """Tombstone-structure check behind residual_report().

    This verifies STRUCTURE only: every erased record's memories.value_json
    is exactly the tombstone, and every runs.receipt_json /
    idempotency.response_json row referencing an erased record id holds
    the tombstone as its value (a receipt with no value field holds
    nothing to leak and is clean). Receipts are recognised by owner id
    in EITHER shape — "record_id" (finish receipts) or "id"
    (dump_record() output: remember / correct / retract /
    resolve_conflict receipts) — including dumps nested in begin
    receipts' "records" / "conflicts" lists (P0-R3-01). Anything else
    — a payload still in place, an unparseable tombstone, a receipt
    value that is not the tombstone — is not clean. It does NOT by
    itself prove no live table still holds the erased payload: a second
    live record sharing the payload is invisible to this check, which is
    why residual_report() also consults the persisted erase outcomes
    (see _unresolved_controlled_residuals). Events are never scanned for
    values here: since the P0-R3-02 fix, no event metadata may carry
    payload material (erase/retract reasons are controlled labels,
    vetted against the payload needles before publishing — P0-R6-01);
    a reason leaked by the pre-fix code
    into an existing DB cannot be detected or scrubbed here — see
    docs/KNOWN_LIMITATIONS.md §8."""
    for (value_json,) in db.execute(
            "SELECT value_json FROM memories WHERE scope_id=? AND state='erased'",
            (scope_id,)).fetchall():
        try:
            if json.loads(value_json) != TOMBSTONE:
                return False
        except ValueError:
            return False
    for table, col in SCRUBBABLE:
        for (raw,) in db.execute(
                f"SELECT {col} FROM {table} WHERE scope_id=?", (scope_id,)).fetchall():
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            for dump in _iter_record_dumps(data):
                if _dump_owner(dump) in erased_ids and dump.get("value") != TOMBSTONE:
                    return False
    return True


def _scope_hmac_key(db, scope_id):
    """Per-scope HMAC key (32 random bytes), created lazily on first
    erase and stored in scope_secrets (live DB only — NEVER exported:
    export_scope copies events + records only, and this table must never
    be added to any export). Runs inside the caller's transaction, so a
    key created by a rolled-back erase never persists. INSERT OR IGNORE
    + re-read keeps concurrent first-erasers from duplicating the row
    (writers already serialise on BEGIN IMMEDIATE)."""
    row = db.execute("SELECT secret_hex FROM scope_secrets WHERE scope_id=?",
                     (scope_id,)).fetchone()
    if row is not None:
        return bytes.fromhex(row[0])
    key = secrets.token_bytes(32)
    db.execute("INSERT OR IGNORE INTO scope_secrets(scope_id, secret_hex, created_at) "
               "VALUES (?,?,?)", (scope_id, key.hex(), _now()))
    row = db.execute("SELECT secret_hex FROM scope_secrets WHERE scope_id=?",
                     (scope_id,)).fetchone()
    return bytes.fromhex(row[0])


def _record_erase_key(db, scope_id, record_id):
    """Per-record verification key (32 random bytes), created lazily on
    first erase of the record and stored in record_erase_keys (live DB
    only — NEVER exported, NEVER in the append-only event log; see
    migrations/005_record_erase_keys.sql, P0-R12-01).

    This is the key the record's needle hashes are HMAC'd under (see
    _needle_hashes). It is independent per record, so deleting it when
    the record reaches terminal COMPLETE retires that record's
    historical hashes without touching any other record's verification
    — the property a shared scope key cannot give (a scope key must
    stay while any other record in the scope is still PARTIAL, even
    for a different payload). Runs inside the caller's transaction;
    INSERT OR IGNORE + re-read keeps concurrent first-erasers from
    duplicating the row (writers already serialise on BEGIN
    IMMEDIATE)."""
    row = db.execute("SELECT secret_hex FROM record_erase_keys "
                     "WHERE scope_id=? AND record_id=?",
                     (scope_id, record_id)).fetchone()
    if row is not None:
        return bytes.fromhex(row[0])
    key = secrets.token_bytes(32)
    db.execute("INSERT OR IGNORE INTO record_erase_keys"
               "(scope_id, record_id, secret_hex, created_at) "
               "VALUES (?,?,?,?)", (scope_id, record_id, key.hex(), _now()))
    row = db.execute("SELECT secret_hex FROM record_erase_keys "
                     "WHERE scope_id=? AND record_id=?",
                     (scope_id, record_id)).fetchone()
    return bytes.fromhex(row[0])


def _read_record_erase_key(db, scope_id, record_id):
    """The record's verification key if one exists, else None.

    Read-only: unlike _record_erase_key it never creates the key, so
    export / reporting never writes a record_erase_keys row as a side
    effect of a read."""
    row = db.execute("SELECT secret_hex FROM record_erase_keys "
                     "WHERE scope_id=? AND record_id=?",
                     (scope_id, record_id)).fetchone()
    if row is None:
        return None
    try:
        return bytes.fromhex(row[0])
    except ValueError:
        return None


def _delete_record_erase_key(db, scope_id, record_id):
    """Delete the record's verification key (P0-R12-01).

    Called when the record reaches terminal COMPLETE (its erase event
    records erase_status COMPLETE, no remaining copies): the historical
    needle hashes in the append-only events become unverifiable, since
    no key left in the DB can recompute them. The deletion runs inside
    the caller's transaction, after the erase event is published and
    after every hash verification that erase needed — a later repair
    that goes PARTIAL again re-creates a fresh key lazily, and the old
    hashes stay retired (a new random key cannot verify them)."""
    db.execute("DELETE FROM record_erase_keys WHERE scope_id=? AND record_id=?",
               (scope_id, record_id))


def _read_record_erase_key_row(db, scope_id, record_id):
    """The record's verification-key row as (secret_hex, created_at),
    or None when the record has no key (P0-R13-01).

    The erase paths capture the row BEFORE deleting the key at
    terminal COMPLETE, so a busy checkpoint can restore the exact
    same key afterwards (see _restore_record_erase_key)."""
    row = db.execute("SELECT secret_hex, created_at FROM record_erase_keys "
                     "WHERE scope_id=? AND record_id=?",
                     (scope_id, record_id)).fetchone()
    if row is None:
        return None
    return (row[0], row[1])


def _restore_record_erase_key(db, scope_id, record_id, secret_hex, created_at):
    """Re-insert a verification key deleted at terminal COMPLETE
    (P0-R13-01). Runs when the purge checkpoint was busy: the key's
    bytes may still be recoverable from the live DB file / WAL, so
    deleting the row would retire the historical hashes while the
    material is still there — restoring the row keeps the old hashes
    verifiable, which is exactly what the repair / residual paths
    need while the purge is pending. Runs inside the caller's
    transaction; INSERT OR IGNORE keeps a concurrent restorer from
    duplicating the row."""
    db.execute("INSERT OR IGNORE INTO record_erase_keys"
               "(scope_id, record_id, secret_hex, created_at) "
               "VALUES (?,?,?,?)", (scope_id, record_id, secret_hex, created_at))


def _set_maintenance_pending(db, scope_id, record_id, detail):
    """Persist a file-purge-pending marker for a record (P0-R13-01,
    P0-R14-01).

    The marker means "purge NOT YET CONFIRMED COMPLETE" — never
    "the file is dirty": a marker that survives a crash between a
    successful checkpoint and the marker clear (P0-R14-01, point
    (iv)) is a fail-closed false positive — the file may already be
    clean. It is cleared only by a later repair re-run that re-runs
    the checkpoint and finds busy == 0 (see
    _clear_maintenance_pending); it is never silently cleared
    without re-running the checkpoint.

    The marker is written UNCONDITIONALLY for every terminal
    COMPLETE erase, in the erase transaction itself, BEFORE the
    record's verification key is deleted (P0-R14-01) — so a crash
    after the erase transaction commits but before the checkpoint
    runs still leaves a persisted marker, and there is no
    commit-to-marker window. The conditional alternative (write
    only when the key row already existed before this transaction)
    was evaluated and rejected: it would need the pre-existence
    capture before the scan phase lazily creates the key (fragile),
    and it would leave the first-erase path unprotected — its key
    never hits disk, but the payload bytes sit in the WAL from the
    remember frame and the checkpoint outcome is not known inside
    the erase transaction, so a post-commit marker write for that
    path would reintroduce exactly the crash window P0-R14-01 is
    about. The marker is therefore written for the key bytes AND
    the old payload pages the checkpoint has not yet purged. The
    same call also REFRESHES the marker in the post-checkpoint
    settlement when the checkpoint returns busy, recording the
    checkpoint failure as the detail.

    The marker names the record and the checkpoint failure only —
    never payload material — and lives in the live-DB-only
    maintenance_pending table (migration
    006_maintenance_pending.sql), NEVER in the append-only event log
    and NEVER exported. residual_report() consults it: a pending row
    makes live_tables_clean false, so the report never calls the
    tables clean while the purge is unconfirmed. The marker write
    is inside the erase transaction, so a write failure rolls the
    whole erase back (key not deleted, event not published) — the
    erase fails atomically. Runs inside the caller's transaction."""
    db.execute(
        "INSERT INTO maintenance_pending(scope_id, record_id, detail, created_at) "
        "VALUES (?,?,?,?) "
        "ON CONFLICT(scope_id, record_id) DO UPDATE SET "
        "detail=excluded.detail, created_at=excluded.created_at",
        (scope_id, record_id, detail, _now()))


def _clear_maintenance_pending(db, scope_id, record_id):
    """Clear a record's file-purge-pending marker (P0-R13-01,
    P0-R14-01): its terminal COMPLETE erase's checkpoint has now
    succeeded, so the deleted key's bytes are purged from the live
    file. Called only after a checkpoint with busy == 0 — never
    silently, and never without re-running the checkpoint: a marker
    left standing by a crash between checkpoint success and this
    clear (a stale marker, "purge unconfirmed") is cleared by a
    later repair re-run, which re-runs the checkpoint first. The
    clear is idempotent and retryable. A no-op when the record has
    no marker. Runs inside the caller's transaction."""
    db.execute("DELETE FROM maintenance_pending WHERE scope_id=? AND record_id=?",
               (scope_id, record_id))


def _maintenance_pending(db, scope_id):
    """Pending file purges in this scope (P0-R13-01): [{record_id,
    detail}] for every record whose terminal COMPLETE erase's WAL
    checkpoint returned busy. Empty when every terminal erase's purge
    completed. Read-only; never a side effect of a report."""
    rows = db.execute(
        "SELECT record_id, detail FROM maintenance_pending "
        "WHERE scope_id=? ORDER BY record_id", (scope_id,)).fetchall()
    return [{"record_id": r[0], "detail": r[1]} for r in rows]


def _set_purge_pending(db, scope_id, detail):
    """Persist a failed-historical-cleanup marker for a scope
    (P0-R16-03).

    The marker means "purge_residue failed for this scope and the
    residue is NOT YET CONFIRMED CLEAN" — never "the file is dirty":
    a marker that survives a crash between a successful re-check
    and the marker clear is a fail-closed false positive — the file
    may already be clean. It is cleared only by a later
    purge_residue run that succeeds (busy==0 checkpoint + VACUUM +
    file check; see _clear_purge_pending) — never silently, never by
    a repair re-run (repair cannot know whether the historical
    residue is gone), and never without re-running the cleanup.

    One row per scope (the cleanup is scope-level, not per-record):
    an upsert refreshes the detail on a repeated failure. The row
    names the scope and the checkpoint failure only — the detail
    holds the checkpoint triple and the boolean probe results,
    NEVER a recovered value (P1-R16-02) — and lives in the
    live-DB-only purge_pending table (migration
    007_purge_pending.sql), NEVER in the append-only event log and
    NEVER exported. residual_report() consults it: a pending row
    makes live_tables_clean false, so the report never calls the
    tables clean while a persisted marker stands. The marker
    counts only once it is committed: a failed cleanup whose
    settlement could not commit (retry budget spent, or had_txn
    with the caller owning the commit) leaves no marker yet, and
    the report reads clean until a retry persists one (see §32).
    The marker write is
    committed immediately after the failure is observed (see
    purge_residue): a crash between observing the failure and
    writing the row leaves no marker — that state is equivalent to
    "purge_residue never ran" and is a disclosed known window (the
    residue stays in the file, so the next purge_residue re-runs the
    full physical cleanup: residue within the probe's 4-digit PIN
    range can be re-discovered by it, anything else is cleared by
    VACUUM and a busy==0 checkpoint, which do not depend on the
    probe). Runs inside the caller's transaction; the caller commits."""
    db.execute(
        "INSERT INTO purge_pending(scope_id, detail, created_at) "
        "VALUES (?,?,?) "
        "ON CONFLICT(scope_id) DO UPDATE SET "
        "detail=excluded.detail, created_at=excluded.created_at",
        (scope_id, detail, _now()))


def _clear_purge_pending(db, scope_id):
    """Clear a scope's failed-historical-cleanup marker (P0-R16-03):
    a purge_residue re-run has now succeeded (busy==0 checkpoint +
    VACUUM + file check), so the historical residue is confirmed
    purged from the live file. Called only after such a successful
    re-check — never silently, and never by a repair re-run. The
    clear is idempotent and retryable. A no-op when the scope has no
    marker. Runs inside the caller's transaction; the caller
    commits."""
    db.execute("DELETE FROM purge_pending WHERE scope_id=?", (scope_id,))


def _purge_pending(db, scope_id):
    """Failed historical cleanups in this scope (P0-R16-03):
    [{scope_id, detail, created_at}] for every scope whose
    purge_residue run failed and has not yet been cleared by a
    successful re-check. Empty when every cleanup succeeded.
    Read-only; never a side effect of a report."""
    rows = db.execute(
        "SELECT scope_id, detail, created_at FROM purge_pending "
        "WHERE scope_id=?", (scope_id,)).fetchall()
    return [{"scope_id": r[0], "detail": r[1], "created_at": r[2]}
            for r in rows]


def _record_verification_keys(db, scope_id, record_id):
    """Keys that can verify this record's stored needle hashes, in
    order (P0-R12-01).

    The per-record key first (current shape: every hash this release
    persists is HMAC'd under it), then the scope key (legacy shape:
    databases created by an older release carry scope-keyed hashes,
    which still verify via scope_secrets — so repair / residual
    judgments on pre-fix data keep working). A deleted per-record key
    is simply absent from the list: "hashes exist but the key is
    gone" verifies as NO match, never as an error — the repair /
    residual paths then fall back to the R11 compensations (the
    blocked-reason flags, the export-exclusion map, key_redacted),
    exactly as a first COMPLETE erase already does. A fresh DB has no
    scope key at all (_needle_hashes no longer creates one), so the
    list is just [per-record key] there."""
    keys = []
    record_key = _read_record_erase_key(db, scope_id, record_id)
    if record_key is not None:
        keys.append(record_key)
    scope_key = _read_scope_hmac_key(db, scope_id)
    if scope_key is not None:
        keys.append(scope_key)
    return keys


def _needle_hashes(db, scope_id, needles, record_id=None):
    """HMAC-SHA256 hex digests of the payload needles, sorted, keyed by
    the record's verification key (P0-R12-01; P0-R4-04 for the HMAC
    shape).

    Hashes — never the values — are what gets persisted in the erase
    event metadata, so a later report can tell *that* a residual was
    recorded. Keyed hashes, unlike the raw SHA-256 this used to write,
    cannot be enumerated from an exported events.jsonl alone: without
    the record's key (which never leaves the live DB) there is nothing
    to brute-force a low-entropy needle against. The events guarantee
    is therefore the same as the receipts one: no recoverable material
    in events either. Old events written before this fix carry raw
    SHA-256 hashes; those stay enumerable from exports already made —
    the fix protects new exports, it cannot un-share old ones.
    record_id selects the per-record key (every production caller
    passes it); without one the scope key is used (legacy callers and
    the pre-fix shape). The key is created lazily here and deleted by
    the erase path when the record reaches terminal COMPLETE, which
    retires the hashes this call produces (see
    _delete_record_erase_key)."""
    key = (_record_erase_key(db, scope_id, record_id) if record_id is not None
           else _scope_hmac_key(db, scope_id))
    return sorted(hmac.new(key, n.encode("utf-8"), hashlib.sha256).hexdigest()
                  for n in needles)


def _stored_needle_hashes(db, scope_id, record_id):
    """Needle hashes persisted by EARLIER erase events covering this
    record (any event whose affected ids name it). Both HMACs (current
    shape) and raw SHA-256 digests (pre-fix events) are returned as
    stored — the repair path hashes reason tokens both ways, so a
    second erase is vetted against the first erase's stored material
    whatever shape it was written in (P0-R4-02)."""
    hashes = set()
    rows = db.execute(
        "SELECT affected_ids_json, metadata_json FROM events "
        "WHERE scope_id=? AND event_type='erase'", (scope_id,)).fetchall()
    for affected_json, meta_json in rows:
        try:
            affected = json.loads(affected_json) if affected_json else []
        except ValueError:
            continue
        if record_id not in affected:
            continue
        try:
            meta = json.loads(meta_json) if meta_json else {}
        except ValueError:
            continue
        for h in meta.get("needle_hashes", []) or []:
            if isinstance(h, str):
                hashes.add(h)
    return hashes


def _blocked_repair_labels(db, scope_id, record_id):
    """The REPAIR_REASONS labels blocked for this record's repair
    erases (P0-R11-02), from the live-DB-only erase_flags table.

    Returns a frozenset (empty when the record has no flags row, e.g.
    a pre-fix erase). The labels are stored here — not in the erase
    event's metadata — because a blocked label IS a fixed vocabulary
    word, and persisting it in an event would write the payload itself
    into append-only history when the payload is literally that label
    (a payload "repair", repaired with reason "repair"). The table is
    live-DB-only and never exported (see migrations/004_erase_flags.sql).
    """
    row = db.execute(
        "SELECT blocked_repair_reasons FROM erase_flags "
        "WHERE scope_id=? AND record_id=?", (scope_id, record_id)).fetchone()
    if row is None:
        return frozenset()
    try:
        labels = json.loads(row[0]) if row[0] else []
    except ValueError:
        return frozenset()
    return frozenset(l for l in labels if isinstance(l, str))


def _store_blocked_repair_reasons(db, scope_id, record_id, labels):
    """Persist (union) the blocked repair-reason labels for a record
    (P0-R11-02) into the live-DB-only erase_flags table.

    Called at erase time with the labels the live needles block (first
    erase) or the union of the already-stored labels and the ones the
    receipt-derived needles block now (repair path). Runs inside the
    caller's transaction. A row is written even when the union is
    empty, so a later repair can tell "no label blocked" apart from
    "no flags recorded" (a pre-fix erase) — the distinction matters
    only for diagnostics; the vetting treats both as unblocked.
    """
    prior = _blocked_repair_labels(db, scope_id, record_id)
    merged = sorted(set(prior) | {l for l in labels if isinstance(l, str)})
    db.execute(
        "INSERT INTO erase_flags(scope_id, record_id, blocked_repair_reasons) "
        "VALUES (?,?,?) "
        "ON CONFLICT(scope_id, record_id) DO UPDATE SET blocked_repair_reasons=excluded.blocked_repair_reasons",
        (scope_id, record_id, _json(merged)))


def _erased_record_flags(db, scope_id, record_id):
    """Compensating flags from the record's erase events (P0-R11-02).

    A COMPLETE erase persists no needle hashes (see
    _erase_event_hash_fields): the hashes are the only enumerable
    material for the payload once the live copies are gone, so
    persisting them would keep the payload recoverable by enumeration
    from the live DB. But several repair / reporting paths used those
    hashes as the only record of what was erased. The flags below are
    the non-digest replacement, computed from the live needles at
    erase time:
      - "key_redacted": True when the record's key was payload material
        at erase time (the erase event already redacted it — a
        boolean, never the payload itself, so it stays in the event
        metadata);
      - "blocked_repair_reasons": the REPAIR_REASONS labels the live
        reason check would have rejected against the erased payload
        (a label that IS the payload, or contains a >= 4-char needle
        as a substring) — for the repair path's reason vetting, which
        used to hash-check against the stored needle hashes. These
        live in the live-DB-only erase_flags table, NOT in the event
        metadata (see _blocked_repair_labels): a blocked label is a
        fixed vocabulary word, and persisting it in an event would
        write the payload itself into history when the payload is
        literally that label.
    Returns {"key_redacted": bool, "blocked_repair_reasons":
    frozenset}. The flags name labels and booleans only — no payload
    material, and nothing enumerable: a label is one of two fixed
    strings, so it cannot be enumerated back into an arbitrary
    low-entropy payload (a PIN blocks no label).
    """
    key_redacted = False
    rows = db.execute(
        "SELECT affected_ids_json, metadata_json FROM events "
        "WHERE scope_id=? AND event_type='erase'", (scope_id,)).fetchall()
    for affected_json, meta_json in rows:
        try:
            affected = json.loads(affected_json) if affected_json else []
        except ValueError:
            continue
        if record_id not in affected:
            continue
        try:
            meta = json.loads(meta_json) if meta_json else {}
        except ValueError:
            continue
        if meta.get("key_redacted"):
            key_redacted = True
    return {"key_redacted": key_redacted,
            "blocked_repair_reasons": _blocked_repair_labels(db, scope_id, record_id)}


def _export_excluded_identifiers(db, scope_id):
    """{record_id: frozenset(fields)} union over all erase events in
    the scope (P0-R11-02).

    Each erase event persists, under "export_excluded_identifiers",
    the records whose id, key, or source-ref ids WERE the erased
    payload at erase time (see
    _export_excluded_identifiers_at_erase). Export reads the union
    and excludes those records — the replacement for the hash check
    (_erased_payload_hashes) that a COMPLETE erase no longer feeds.
    Record ids and field names only: no payload material, and nothing
    enumerable.
    """
    merged = {}
    for (meta_json,) in db.execute(
            "SELECT metadata_json FROM events WHERE scope_id=? AND event_type='erase'",
            (scope_id,)).fetchall():
        try:
            meta = json.loads(meta_json) if meta_json else {}
        except ValueError:
            continue
        for rec_id, fields in (meta.get("export_excluded_identifiers") or {}).items():
            if not isinstance(rec_id, str) or not isinstance(fields, list):
                continue
            merged.setdefault(rec_id, set()).update(
                f for f in fields if isinstance(f, str))
    return {rid: frozenset(fs) for rid, fs in merged.items()}


def _export_excluded_identifiers_at_erase(db, scope_id, needles=(), needle_hashes=frozenset(),
                                          record_id=None):
    """Compute the export-exclusion map at erase time (P0-R11-02).

    Records in scope whose id, key, or source-ref ids ARE a payload
    needle (verbatim or in a known decoding — the same whole-value
    rule as the erase scan, P2-R8-03), keyed by record id with the
    matched fields ("id" / "key" / "source_id"). The map is computed
    from the live needles (first erase) or the receipt-derived needles
    plus the stored hashes (repair path), and persisted in the erase
    event's metadata, so export can keep excluding those records
    after a COMPLETE erase stops persisting needle hashes. A record
    created AFTER the erase is not in the map — its identifier copy
    is a post-erase re-introduction, which the residual report
    already cannot detect (see residual_report()); the payload is
    live in the DB again then, so excluding it from export would not
    make the DB clean. Values are deliberately NOT in the map: a live
    record whose VALUE is the payload makes the erase PARTIAL, so the
    hashes persist and the hash check covers it.
    """
    if not needles and not needle_hashes:
        return {}
    # P0-R12-01: the hashes belong to the record being erased, so they
    # verify under that record's verification keys (its per-record key,
    # then the legacy scope key). Without a record id the scope key is
    # the legacy fallback.
    keys = (_record_verification_keys(db, scope_id, record_id)
            if record_id is not None else _read_scope_hmac_key(db, scope_id))

    def _matches(identifier):
        if not isinstance(identifier, str) or not identifier:
            return False
        if _identifier_equals_needle(identifier, needles):
            return True
        return bool(needle_hashes) and _identifier_matches_hashes(
            identifier, needle_hashes, keys)

    excluded = {}
    for rec_id, key in db.execute(
            "SELECT id, key FROM memories WHERE scope_id=?", (scope_id,)).fetchall():
        fields = set()
        if _matches(rec_id):
            fields.add("id")
        if _matches(key):
            fields.add("key")
        if fields:
            excluded.setdefault(rec_id, set()).update(fields)
    for mem_id, sid in db.execute(
            "SELECT memory_id, source_id FROM memory_sources WHERE scope_id=?",
            (scope_id,)).fetchall():
        if _matches(sid):
            excluded.setdefault(mem_id, set()).add("source_id")
    return {rid: sorted(fs) for rid, fs in sorted(excluded.items())}


def _blocked_repair_reasons(needles):
    """REPAIR_REASONS labels the live reason check would reject
    against these needles (P0-R11-02).

    Computed at erase time, from the live needles, and persisted in
    the erase event's metadata (see _erased_record_flags): the repair
    path's reason vetting used to hash-check the label against the
    stored needle hashes, which a COMPLETE erase no longer persists.
    A label is blocked when _check_reason_against_needles rejects it
    — the label IS a needle, or contains a >= 4-char needle as a
    substring (the live check is stricter than the old hash check,
    which only caught a label equal to a needle, so the flags make
    repair vetting stricter, not weaker).
    """
    blocked = []
    for label in sorted(REPAIR_REASONS):
        try:
            _check_reason_against_needles(label, needles)
        except TurnkeepError:
            blocked.append(label)
    return blocked


def _erase_event_hash_fields(db, scope_id, needles, scan_status, *, hashes,
                             stored_hashes=frozenset(), record_id=None):
    """The needle-hash + export-exclusion fields for an erase event's
    metadata (P0-R11-02).

    Returns {"needle_hashes": [...], "export_excluded_identifiers":
    {...}}.
      - needle_hashes: the given hashes, persisted ONLY when the erase
        is PARTIAL. A PARTIAL erase leaves a live copy of the payload
        in the DB, so the hashes add no new leak surface — and the
        repair / residual / export paths need them to judge and clean
        that copy. A COMPLETE erase leaves no live copy, so the hashes
        would be the ONLY enumerable material for the payload in the
        live DB (a 4-digit PIN is recoverable by enumerating 0000–9999
        against them while the scope key sits in the same DB's
        scope_secrets table — the R10 principle, applied here to
        needle_hashes instead of request_digest); they are NOT
        persisted. The step-1 PARTIAL refusal always persists hashes
        (it is PARTIAL by construction).
      - export_excluded_identifiers: always persisted, computed from
        the live needles (and, in the repair path, the stored hashes)
        — the non-digest compensation for the hashes a COMPLETE erase
        omits (see _export_excluded_identifiers_at_erase). Record ids
        and field names only, never payload material, so it stays in
        the event metadata.
    The third compensation — the blocked repair-reason labels — is NOT
    returned here: a blocked label is a fixed vocabulary word, and
    persisting it in an event would write the payload itself into
    history when the payload is literally that label, so it goes to
    the live-DB-only erase_flags table instead (see
    _store_blocked_repair_reasons, called by the erase paths).
    hashes is the full hash list for these needles (live hashes in the
    first erase, receipt-derived hashes in the repair path);
    stored_hashes is the hash set the identifier map may also match
    against (the live hashes in the first erase, the first erase's
    stored hashes in the repair path).
    """
    return {
        "needle_hashes": list(hashes) if scan_status == "PARTIAL" else [],
        "export_excluded_identifiers": _export_excluded_identifiers_at_erase(
            db, scope_id, needles, stored_hashes or frozenset(hashes),
            record_id=record_id),
    }


def _check_reason_against_stored_hashes(db, scope_id, record_id, reason):
    """Vet a repair-erase reason against the first erase's stored
    material (P0-R4-02).

    After a first COMPLETE erase the receipts are scrubbed, so the
    repair path's receipt-derived needle set is empty and the plain
    reason check passes anything. The persisted needle hashes are the
    only remaining record of what was erased: split the reason into
    tokens (see _reason_tokens), hash each token — and the whole reason
    — with the record's verification keys (HMAC, current shape: its
    per-record key, then the legacy scope key) and with raw SHA-256
    (pre-fix events), and reject on any match. Computing a hash of a
    caller-supplied reason publishes nothing; the error never quotes
    the token.

    This is now the second layer behind the repair allowlist (P0-R5-01):
    _check_repair_reason admits only the REPAIR_REASONS labels and then
    calls this, so a glued secret can no longer reach this check at all
    — the allowlist rejects it first. Kept because a label that IS the
    erased value (a payload literally "repair") still has to be caught
    here, by hash, since the live needles are gone.

    P0-R11-02: a first COMPLETE erase persists no needle hashes (see
    _erase_event_hash_fields), so the hash check alone would pass
    anything after a COMPLETE erase. The persisted compensating flags
    (see _erased_record_flags) cover it: a label in the record's
    blocked_repair_reasons is rejected here too, before the hash
    check. The flags are computed from the live needles at erase time,
    so they catch the label-equals-needle corner exactly as the hash
    check did — and the >= 4-char substring case the hash check
    missed. The error is the same, and never quotes the token.
    """
    flags = _erased_record_flags(db, scope_id, record_id)
    if reason in flags["blocked_repair_reasons"]:
        raise TurnkeepError(
            "INVALID_INPUT",
            "reason must not contain the record's payload: free text is "
            "copied verbatim into the append-only event log, where it "
            "can never be erased; use a short label instead "
            "(e.g. 'repair')")
    stored = _stored_needle_hashes(db, scope_id, record_id)
    if not stored:
        return
    # P0-R12-01: the stored hashes are keyed by this record's
    # verification keys (its per-record key, then the legacy scope
    # key) — a deleted per-record key simply verifies nothing, so
    # "hashes exist but the key is gone" is a non-match here, and the
    # flags above are what still vet the reason then.
    keys = _record_verification_keys(db, scope_id, record_id)
    for token in _reason_tokens(reason):
        if not token:
            continue
        digests = {hashlib.sha256(token.encode("utf-8")).hexdigest()}
        for key in keys:
            digests.add(hmac.new(key, token.encode("utf-8"),
                                 hashlib.sha256).hexdigest())
        if digests & stored:
            raise TurnkeepError(
                "INVALID_INPUT",
                "reason must not contain the record's payload: free text is "
                "copied verbatim into the append-only event log, where it "
                "can never be erased; use a short label instead "
                "(e.g. 'repair')")
    # P1-R12-02: the reverse direction — the reason label itself is a
    # decoded form of the erased payload (the payload is an encoding
    # of the label, e.g. Base64("rotation")). Hash the label's
    # ENCODINGS and compare them with the stored hashes: a match
    # means writing this label would store a value the payload
    # decodes to. The flags above already cover this corner for a
    # first COMPLETE erase (they are computed from the live needles,
    # which see the reverse direction too — see
    # _check_reason_against_needles); the hash check here covers the
    # PARTIAL first erase, whose flags are the same set.
    for form in _encoded_forms(reason):
        if not form:
            continue
        digests = {hashlib.sha256(form.encode("utf-8")).hexdigest()}
        for key in keys:
            digests.add(hmac.new(key, form.encode("utf-8"),
                                 hashlib.sha256).hexdigest())
        if digests & stored:
            raise TurnkeepError(
                "INVALID_INPUT",
                "reason must not contain the record's payload: free text is "
                "copied verbatim into the append-only event log, where it "
                "can never be erased; use a short label instead "
                "(e.g. 'repair')")


def _read_scope_hmac_key(db, scope_id):
    """The scope's HMAC key if one exists, else None (P0-R8-01c).

    Read-only: unlike _scope_hmac_key it never creates the key, so
    export / reporting never writes a scope_secrets row as a side
    effect of a read."""
    row = db.execute("SELECT secret_hex FROM scope_secrets WHERE scope_id=?",
                     (scope_id,)).fetchone()
    if row is None:
        return None
    try:
        return bytes.fromhex(row[0])
    except ValueError:
        return None


def _erased_payload_hashes(db, scope_id):
    """Needle hashes from every erase event in the scope (P0-R8-01c).

    The union over all erase events (COMPLETE and PARTIAL alike): a
    payload that was ever erased — even one whose erase was refused
    — must not be re-exported through another record's id, key,
    source id, or value. Both hash shapes are collected: HMAC
    (current) and raw SHA-256 (pre-fix events)."""
    hashes = set()
    for (meta_json,) in db.execute(
            "SELECT metadata_json FROM events WHERE scope_id=? AND event_type='erase'",
            (scope_id,)).fetchall():
        try:
            meta = json.loads(meta_json) if meta_json else {}
        except ValueError:
            continue
        for h in meta.get("needle_hashes", []) or []:
            if isinstance(h, str):
                hashes.add(h)
    return hashes


def _identifier_matches_hashes(identifier, needle_hashes, scope_key):
    """True when an identifier equals an erased payload (P0-R8-01c).

    The identifier (verbatim, and in every known decoding) is hashed
    both ways the erase events persist needle hashes — HMAC-SHA256
    keyed by a verification key, and raw SHA-256 (pre-fix events) —
    and compared with the stored set. A match means the identifier IS
    the erased payload (or its encoding), not merely that it contains
    a short needle as a substring: random ids are never matched this
    way, which is the same whole-value rule the erase scan uses for
    identifier fields (P2-R8-03).
    scope_key is one key (bytes), None (raw SHA-256 only), or a list
    of keys to try in order (P0-R12-01: the record's verification keys
    — its per-record key, then the legacy scope key). Trying several
    keys is safe: a hash verifies under exactly one key, so a
    different key only fails to match."""
    if not isinstance(identifier, str) or not identifier or not needle_hashes:
        return False
    needle_hashes = set(needle_hashes)  # callers pass lists or sets
    if scope_key is None:
        keys = []
    elif isinstance(scope_key, (list, tuple)):
        keys = [k for k in scope_key if k is not None]
    else:
        keys = [scope_key]
    for form in _decoded_forms(identifier):
        digests = {hashlib.sha256(form.encode("utf-8")).hexdigest()}
        for key in keys:
            digests.add(hmac.new(key, form.encode("utf-8"),
                                 hashlib.sha256).hexdigest())
        if digests & needle_hashes:
            return True
    return False


def _erased_payload_hashes_by_record(db, scope_id):
    """[(record_id, frozenset(hashes))] — needle hashes per erase
    event's record (P0-R12-01).

    The grouped form of _erased_payload_hashes: each event's hashes
    belong to the record its affected ids name first (the erased
    record; tainted descendants listed after it are not the payload
    owners). The scope-wide checks (export, import-plan redaction)
    verify each group under that record's own verification keys, so a
    retired record's hashes (key deleted at terminal COMPLETE) never
    match, while a still-PARTIAL record's hashes still do."""
    grouped = {}
    for affected_json, meta_json in db.execute(
            "SELECT affected_ids_json, metadata_json FROM events "
            "WHERE scope_id=? AND event_type='erase'", (scope_id,)).fetchall():
        try:
            affected = json.loads(affected_json) if affected_json else []
            meta = json.loads(meta_json) if meta_json else {}
        except ValueError:
            continue
        if not affected or not isinstance(affected[0], str):
            continue
        hashes = {h for h in meta.get("needle_hashes", []) or []
                  if isinstance(h, str)}
        if hashes:
            grouped.setdefault(affected[0], set()).update(hashes)
    return [(rid, frozenset(hs)) for rid, hs in sorted(grouped.items())]


def _identifier_matches_any_erased(db, scope_id, identifier, erased_by_record=None):
    """True when an identifier equals any payload erased in this scope
    (P0-R12-01) — the scope-wide form of _identifier_matches_hashes.

    Each erase event's hashes are verified under that event's
    record's verification keys (its per-record key, then the legacy
    scope key). A record whose key was deleted at terminal COMPLETE
    contributes hashes no key can verify, so they never match here;
    the export-exclusion map (not the hashes) is what still excludes
    such a record (see _export_excluded_identifiers). erased_by_record
    is the precomputed _erased_payload_hashes_by_record map (pass it
    when the caller already read it, so a loop over many candidates
    does not re-read every erase event's metadata per candidate)."""
    if not isinstance(identifier, str) or not identifier:
        return False
    if erased_by_record is None:
        erased_by_record = _erased_payload_hashes_by_record(db, scope_id)
    for record_id, hashes in erased_by_record:
        keys = _record_verification_keys(db, scope_id, record_id)
        if _identifier_matches_hashes(identifier, hashes, keys):
            return True
    return False


def _label_encodes_erased(db, scope_id, label):
    """True when a controlled label encodes to a payload erased in
    this scope (P1-R12-02, kind disposition).

    The reverse direction of _identifier_matches_any_erased: the label
    itself is a decoded form of the payload (the payload is an
    encoding of the label — e.g. Base64("fact") == "ZmFjdA=="), so the
    check hashes the label's ENCODINGS (_encoded_forms) and compares
    them with each erased record's hashes under that record's
    verification keys. Used by the export to exclude a tombstone row
    whose kind is such a label: the kind is a structural attribute
    that cannot be rewritten at erase time, so the fresh-DB remedy
    must not carry it."""
    if not isinstance(label, str) or not label:
        return False
    forms = _encoded_forms(label)
    if not forms:
        return False
    for record_id, hashes in _erased_payload_hashes_by_record(db, scope_id):
        keys = _record_verification_keys(db, scope_id, record_id)
        for form in forms:
            digests = {hashlib.sha256(form.encode("utf-8")).hexdigest()}
            for key in keys:
                digests.add(hmac.new(key, form.encode("utf-8"),
                                     hashlib.sha256).hexdigest())
            if digests & hashes:
                return True
    return False


def _identifier_replacement(identifier, scope_key):
    """Non-recoverable replacement for an identifier that IS an erased
    payload (P0-R8-01b): its HMAC hex (scope-keyed), or its raw
    SHA-256 hex when no scope key exists. The dump / receipt keeps a
    stable, unique pointer, but the payload text is gone — and the
    replacement is the same value the erase events already persist as
    a needle hash, so no new material is introduced."""
    if scope_key is not None:
        return hmac.new(scope_key, identifier.encode("utf-8"),
                        hashlib.sha256).hexdigest()
    return hashlib.sha256(identifier.encode("utf-8")).hexdigest()


def _safe_erase_event_key(db, scope_id, record_id, key, needles):
    """The key value safe to persist in an erase event (P0-R8-01a).

    Returns (key, redacted): the raw key when it carries no payload
    needle, else (None, True) — the caller omits "key" and records
    "key_redacted": True. A key that carries the payload must never
    be written raw into append-only history: the erase event is
    published AFTER the step-1 scan, so the scan can never see this
    event's own key (that is how the R8 leak happened — the erase
    event itself wrote the secret into metadata.key). The record id
    in affected_ids still names the record; only the human-readable
    key label is redacted. The live-needle check runs first; the
    stored-hash check covers the repair path, where the live needles
    are gone because the receipts are already scrubbed."""
    if _carries_needle(key, needles):
        return None, True
    if not needles and record_id:
        stored = _stored_needle_hashes(db, scope_id, record_id)
        if stored and _identifier_matches_hashes(
                key, stored, _record_verification_keys(db, scope_id, record_id)):
            return None, True
        # P0-R11-02: a first COMPLETE erase persists no needle hashes,
        # so the hash check above is empty — but the first erase's
        # key_redacted flag (see _erased_record_flags) still says the
        # record's key was payload material at erase time, and the key
        # cannot have changed since (the record is tombstoned). A
        # repair event must not write that key raw into history.
        if _erased_record_flags(db, scope_id, record_id)["key_redacted"]:
            return None, True
    return key, False


def _redact_entry_key(entry, needles):
    """Drop a remaining-entry's key when it carries a needle (P0-R8-01a).

    The entry is persisted in the erase event's metadata, so a key
    that IS the payload would leak it into events.jsonl. The record
    id still names the record; the redaction is flagged, not silent.
    """
    if entry.get("key") is not None and _carries_needle(entry["key"], needles):
        del entry["key"]
        entry["key_redacted"] = True


def _erase_return_key(event_key, key_redacted):
    """The "key" part of an erase_record() return dict (P1-R10-03).

    The raw key when it carries no payload material, else omitted
    with "key_redacted": True — the same rule as the erase event's
    metadata key (_safe_erase_event_key) and the report surfaces.
    The erase response must not re-materialise the payload it
    certifies as erased: the CLI prints this dict to stdout, and a
    caller logging it would keep another raw copy. (event_key is
    None exactly when redacted, so the two cases are exhaustive.)"""
    if key_redacted:
        return {"key_redacted": True}
    return {"key": event_key}


def _task_text_locations(db, table, rowid, needles):
    """Task-text locations in a runs / idempotency row that still hold
    a payload needle verbatim (P0-R11-01).

    A run's task is free text, not a receipt dump: the receipt walk in
    _describe_remaining cannot see it, so a row whose task still holds
    the needle was described as a receipt residual and then wrongly
    judged resolved (the receipt dump is tombstoned / clean, while the
    task text still holds the payload). The three locations, verbatim
    substring only (raw text; an encoding in free text has no value
    boundary, so it is not scanned — the retained runs.task limitation,
    docs/KNOWN_LIMITATIONS.md §8):
      - "runs.task": the runs.task column itself;
      - "runs.receipt_json.task": the top-level "task" field of a
        finish receipt (a begin row's receipt_json is NULL; a finish
        receipt carries no "task" field in the current code, so this
        location is checked defensively and is normally absent);
      - "idempotency.response_json.task": the top-level "task" field
        of a stored begin response.
    Returns the location names that hold a needle (empty when none).
    Location names only, never the text: the entry is persisted in the
    erase event's metadata, and the task text may itself be the
    payload.
    """
    if not needles:
        return []
    locations = []
    if table == "runs":
        row = db.execute(
            "SELECT task, receipt_json FROM runs WHERE rowid=?", (rowid,)).fetchone()
        if row is None:
            return []
        task, receipt = row
        if isinstance(task, str) and any(n in task for n in needles if n):
            locations.append("runs.task")
        if receipt:
            try:
                data = json.loads(receipt)
            except ValueError:
                data = None
            if (isinstance(data, dict) and isinstance(data.get("task"), str)
                    and any(n in data["task"] for n in needles if n)):
                locations.append("runs.receipt_json.task")
        return locations
    if table == "idempotency":
        row = db.execute(
            "SELECT response_json FROM idempotency WHERE rowid=?", (rowid,)).fetchone()
        if row is None or not row[0]:
            return []
        try:
            data = json.loads(row[0])
        except ValueError:
            return []
        if (isinstance(data, dict) and isinstance(data.get("task"), str)
                and any(n in data["task"] for n in needles if n)):
            locations.append("idempotency.response_json.task")
        return locations
    return []


def _describe_remaining(db, scope_id, remaining, needles=(), needle_hashes=(),
                        record_id=None):
    """Resolve ``{table: [rowids]}`` from a payload scan to stable refs.

    Rowids are not stable across VACUUM, so each hit is resolved here —
    before any maintenance — to the record id / key it belongs to (for
    memories), or the referenced record id (for receipts), so the
    persisted residual names a record, not a row number. Entries that
    cannot be resolved keep their table + rowid and no record id.

    needle_hashes (the erase's needle hashes, live in the first erase,
    stored in the repair path) backs the hash form of the
    whole-value checks below, for the repair path where the live
    needles are gone because the receipts are already scrubbed.
    record_id names the record being erased, so the hash checks verify
    under its verification keys (P0-R12-01: its per-record key, then
    the legacy scope key); without one the scope key is the legacy
    fallback (direct callers such as the R3 test, which pass no
    hashes anyway).

    Task text (P0-R11-01): a runs / idempotency row whose TASK still
    holds the needle verbatim is flagged on the entry
    ("task_text": True, "task_locations": [...]) — the receipt dump
    walk cannot see free task text, so without the flag the entry
    would be attributed to the receipt alone and then judged resolved
    while the task still holds the payload. The flag is persisted in
    the erase event's metadata (location names only, never the text),
    so _unresolved_controlled_residuals can judge it by its actual
    location. An entry flagged this way never resolves in the live DB
    (task text is never scrubbed — see _unresolved_controlled_residuals).
    Entries written by the pre-fix code carry no flag and keep the old
    misjudgement; the repair path cannot backfill them (its needles
    are receipt-derived, and the task text is not a receipt), so a
    pre-fix DB's task-text residual stays misreported until the scope
    is exported to a fresh DB (docs/KNOWN_LIMITATIONS.md §24).

    Receipt hits are attributed by NEEDLE (P0-R3-01): the receipt JSON
    is walked for record dumps whose value carries a needle, and one
    entry is emitted per owning record id (a begin receipt can hold
    dumps of several records, and only the ones holding this payload
    are named). The key is read from memories for each owner. A receipt
    row whose payload cannot be attributed to a record keeps table +
    rowid only — it is never attributed to the receipt row's own id
    (a run id / idempotency-row id is not a record id, and attributing
    it would make _unresolved_controlled_residuals wrongly treat the
    residual as resolved).

    A resolved key that carries a needle is redacted from the entry
    (P0-R8-01a): the entry is persisted in the erase event's metadata,
    and a key that IS the payload would leak it into events.jsonl.
    The record id still names the record; _unresolved_controlled_
    residuals falls back to reading the key from memories (in-memory
    only) when it needs one for the report.

    Scope-filtered (P2-R5-02): a rowid from another scope is never
    resolved to that scope's record — the receipt row's scope_id is
    checked first, and the memories lookup carries scope_id, so a
    cross-scope hit keeps table + rowid only and names no record from
    the other scope."""
    # P0-R12-01: the needle_hashes belong to the record being erased,
    # so the hash-form checks below verify under its verification keys.
    keys = (_record_verification_keys(db, scope_id, record_id)
            if record_id is not None else _read_scope_hmac_key(db, scope_id))
    described = []
    for table in sorted(remaining):
        for rowid in sorted(set(remaining[table])):
            if table in ("runs", "idempotency"):
                json_col = "receipt_json" if table == "runs" else "response_json"
                owners = []
                raw = db.execute(
                    f"SELECT scope_id, {json_col} FROM {table} WHERE rowid=?",
                    (rowid,)).fetchone()
                if raw and raw[0] == scope_id and raw[1]:
                    try:
                        data = json.loads(raw[1])
                    except ValueError:
                        data = None
                    if data is not None:
                        for dump in _iter_record_dumps(data):
                            owner = _dump_owner(dump)
                            if not owner:
                                continue
                            # Attribute by value (payload text, substring
                            # rule) OR by identifier (key / source-ref
                            # ids, whole-value rule, P0-R8-01b): a dump
                            # whose source-ref id IS the payload holds a
                            # copy even when its value is clean — without
                            # the attribution the remaining entry names no
                            # record, _unresolved_controlled_residuals
                            # falls back to the erased record's affected
                            # ids, and the receipt check (which only
                            # looks at dumps owned by that record) calls
                            # the residual resolved while the other
                            # record's receipt still holds the payload.
                            holds = _value_contains_needle(dump.get("value"), needles)
                            if not holds and isinstance(dump.get("key"), str):
                                holds = _identifier_equals_needle(dump["key"], needles)
                            if not holds:
                                for src in dump.get("source_refs", []) or []:
                                    if (isinstance(src, dict)
                                            and _identifier_equals_needle(
                                                src.get("id"), needles)):
                                        holds = True
                                        break
                            if holds:
                                owners.append(owner)
                    # P0-R10-02c: an idempotency row's own KEY can be
                    # the payload — the key lives in its own column,
                    # not in the response_json, so the dump walk above
                    # cannot see it. When it is (whole-value, verbatim
                    # or in a known decoding, live needles or stored
                    # hashes), the residual belongs to the record(s)
                    # this row's response names, so attribute it to
                    # them (finish receipts name their record in a
                    # top-level "record_id" — see _response_owners).
                    if table == "idempotency":
                        key_row = db.execute(
                            "SELECT idempotency_key FROM idempotency WHERE rowid=?",
                            (rowid,)).fetchone()
                        if key_row is not None and (
                                _identifier_equals_needle(key_row[0], needles)
                                or (needle_hashes
                                    and _identifier_matches_hashes(
                                        key_row[0], needle_hashes, keys))):
                            owners.extend(_response_owners(data))
                # P0-R11-01: task text is free text, not a receipt
                # dump — the walk above cannot see it. Flag the entry
                # when the row's task still holds the needle verbatim
                # (see _task_text_locations): without the flag the
                # entry would be judged by the receipt dump alone and
                # wrongly resolved while the task still holds the
                # payload.
                task_locations = _task_text_locations(db, table, rowid, needles)
                if owners:
                    for owner in sorted(set(owners)):
                        entry = {"table": table, "rowid": rowid, "record_id": owner}
                        key_row = db.execute(
                            "SELECT key FROM memories WHERE scope_id=? AND id=?",
                            (scope_id, owner)).fetchone()
                        if key_row is not None:
                            entry["key"] = key_row[0]
                        _redact_entry_key(entry, needles)
                        if task_locations:
                            entry["task_text"] = True
                            entry["task_locations"] = task_locations
                        described.append(entry)
                    continue
                if task_locations:
                    described.append({"table": table, "rowid": rowid,
                                      "task_text": True,
                                      "task_locations": task_locations})
                    continue
                described.append({"table": table, "rowid": rowid})
                continue
            if table == "sources":
                # P0-R10-02d: a sources hit names the live record(s)
                # citing that source id (via memory_sources), one
                # entry each — the sources row itself names no record.
                # No citing record (orphan row): the entry names no
                # record and can never resolve by erasing one, so it
                # is flagged source_orphan and stays unresolved (see
                # _unresolved_controlled_residuals).
                src_row = db.execute(
                    "SELECT id FROM sources WHERE rowid=? AND scope_id=?",
                    (rowid, scope_id)).fetchone()
                if src_row is None:
                    described.append({"table": table, "rowid": rowid})
                    continue
                citing = db.execute(
                    "SELECT memory_id FROM memory_sources WHERE scope_id=? AND source_id=? "
                    "ORDER BY memory_id", (scope_id, src_row[0])).fetchall()
                if not citing:
                    described.append({"table": table, "rowid": rowid, "source_orphan": True})
                    continue
                for (mid,) in citing:
                    entry = {"table": table, "rowid": rowid, "record_id": mid}
                    key_row = db.execute(
                        "SELECT key FROM memories WHERE scope_id=? AND id=?",
                        (scope_id, mid)).fetchone()
                    if key_row is not None:
                        entry["key"] = key_row[0]
                    _redact_entry_key(entry, needles)
                    if (entry.get("key") is not None and needle_hashes
                            and _identifier_matches_hashes(
                                entry["key"], needle_hashes, keys)):
                        del entry["key"]
                        entry["key_redacted"] = True
                    described.append(entry)
                continue
            entry = {"table": table, "rowid": rowid}
            try:
                cols = {r[1] for r in db.execute(f"PRAGMA table_info({table})").fetchall()}
                if table == "memories" and "id" in cols:
                    row = db.execute(
                        "SELECT id, key FROM memories WHERE rowid=? AND scope_id=?",
                        (rowid, scope_id)).fetchone()
                    if row is not None:
                        entry["record_id"], entry["key"] = row[0], row[1]
                        _redact_entry_key(entry, needles)
                elif table == "events":
                    # P0-R10-02a/b: name the event itself (its id).
                    # The entry names no record — the payload sits in
                    # the event's affected ids or actor binding, and
                    # the record it belongs to is not necessarily the
                    # erased one. _unresolved_controlled_residuals
                    # falls back to the erase event's affected ids
                    # for the record, and an events entry never
                    # resolves (append-only).
                    ev_row = db.execute(
                        "SELECT id FROM events WHERE rowid=? AND scope_id=?",
                        (rowid, scope_id)).fetchone()
                    if ev_row is not None:
                        entry["event_id"] = ev_row[0]
                # Any other table: no record id. Its own row id is not a
                # record id, and attributing it would wrongly resolve
                # the residual (see _unresolved_controlled_residuals).
            except Exception:  # noqa: BLE001 - a failed lookup must not fail erasure
                pass
            described.append(entry)
    return described


def _idempotency_key_matches_erased(db, scope_id, erase_meta, record_id=None):
    """True when some idempotency row's key still IS an erased payload
    (P0-R10-02c).

    The key is checked against the needle hashes the erase event
    persisted (verbatim and in every known decoding, by hash): a row
    whose key is the erased payload — belonging to another record's
    operation — survives that record's erasure, because the key is
    the OTHER payload, not its own. The residual naming it stays
    unresolved until the key is gone (the remedy is export to a
    fresh DB). record_id names the record the erase event erased, so
    the hashes verify under its verification keys (P0-R12-01); without
    one the scope key is the legacy fallback."""
    hashes = erase_meta.get("needle_hashes") or []
    if not hashes:
        return False
    keys = (_record_verification_keys(db, scope_id, record_id)
            if record_id is not None else _read_scope_hmac_key(db, scope_id))
    return any(_identifier_matches_hashes(k, hashes, keys)
               for (k,) in db.execute(
                   "SELECT idempotency_key FROM idempotency WHERE scope_id=?",
                   (scope_id,)).fetchall())


def _live_source_matches_erased(db, scope_id, record_id, erase_meta,
                                key_record_id=None):
    """True when a record still cites a source whose id IS an erased
    payload (P0-R10-02d).

    Same hash check as above, over the record's live source links:
    erasing the citing record does not remove the link (its id is the
    erased payload, not that record's own), so the residual stays
    until no such link remains. record_id is the CITING record (the
    residual's record); key_record_id is the record the erase event
    erased, whose verification keys the hashes need (P0-R12-01) —
    the two are different records when the link belongs to another
    record's operation, and the keys must be the erased record's
    (the hashes are its payload's), not the citing record's."""
    hashes = erase_meta.get("needle_hashes") or []
    if not hashes:
        return False
    keys = _record_verification_keys(
        db, scope_id, key_record_id if key_record_id is not None else record_id)
    return any(_identifier_matches_hashes(sid, hashes, keys)
               for (sid,) in db.execute(
                   "SELECT source_id FROM memory_sources WHERE scope_id=? AND memory_id=?",
                   (scope_id, record_id)).fetchall())


def _unresolved_controlled_residuals(db, scope_id):
    """Controlled residuals recorded by erase events and still unresolved.

    Reads every erase event's persisted outcome (erase_status + remaining
    table/record-key, needle hashes only). A remaining entry is unresolved
    while the record it names still lives in the DB in a non-erased state
    and no LATER erase event with a COMPLETE outcome covers that record
    (erasing the second copy afterwards resolves the residual). Erase
    events written before this persistence existed carry no outcome
    and contribute nothing here — for those, only the tombstone-structure
    check applies. Returns a list of {erase_event_id, table, record_id,
    key} dicts, deduplicated.

    Attribution (P0-R3-01): an entry with no record id is NOT left
    unattributed when the erase event's affected ids name the record
    the erase covered — pre-fix entries recorded no record id at all
    (idempotency receipts were never attributed), and a record id that
    names no memories row in this scope is a misattribution (pre-fix
    runs receipts named the run id), so both fall back to the event's
    affected ids. An entry that still names no record stays unresolved:
    it cannot be re-checked, so it must not be called clean.

    Erased-record entries: a record being erased does NOT by itself
    resolve a receipt-table entry — the receipt is a derived copy, and
    the residual is unresolved while any receipt still holds a
    non-tombstone value for that record (checked live). An events-table
    entry never resolves (append-only: a payload in events can never
    be scrubbed). An idempotency-table entry stays unresolved while
    any idempotency key in scope still IS this payload (P0-R10-02c),
    and a sources-table entry while the record still cites such a
    source (P0-R10-02d) — erasing the citing record removes neither,
    because the key / link id is the OTHER payload, not its own.
    An orphan sources entry (no citing record at all) never resolves.
    A task-text entry (P0-R11-01: a runs / idempotency row whose task
    still holds the payload verbatim) never resolves either — task
    text is live free text that is never scrubbed, so the entry stays
    unresolved whatever the receipt check says; the remedy is export
    to a fresh DB (runs are not exported)."""
    rows = db.execute(
        "SELECT id, scope_epoch, affected_ids_json, metadata_json FROM events "
        "WHERE scope_id=? AND event_type='erase' ORDER BY scope_epoch",
        (scope_id,)).fetchall()
    # Record ids covered by a later COMPLETE erase, per epoch: erasing a
    # record completely removes its payload, resolving residuals naming it.
    complete_by_epoch = []
    for _ev_id, epoch, affected_json, meta_json in rows:
        try:
            meta = json.loads(meta_json) if meta_json else {}
        except ValueError:
            meta = {}
        if meta.get("erase_status") == "COMPLETE":
            try:
                affected = json.loads(affected_json) if affected_json else []
            except ValueError:
                affected = []
            complete_by_epoch.append((epoch, set(affected)))
    unresolved, seen = [], set()
    for ev_id, epoch, affected_json, meta_json in rows:
        try:
            meta = json.loads(meta_json) if meta_json else {}
        except ValueError:
            continue
        if meta.get("erase_status") != "PARTIAL":
            continue
        try:
            affected = json.loads(affected_json) if affected_json else []
        except ValueError:
            affected = []
        affected = [a for a in affected if isinstance(a, str)]
        for entry in meta.get("remaining", []) or []:
            if not isinstance(entry, dict):
                continue
            if entry.get("source_orphan"):
                # P0-R10-02d: an orphan sources row (no live record
                # cites it) still holds the payload id, names no
                # record, and can never resolve by erasing one — it
                # stays unresolved. The remedy is export to a fresh
                # DB (the row itself carries no record to erase).
                dedup = ("sources", None, entry.get("rowid"), None)
                if dedup not in seen:
                    seen.add(dedup)
                    unresolved.append({"erase_event_id": ev_id, "table": "sources",
                                       "record_id": None, "key": None})
                continue
            record_id = entry.get("record_id")
            key = entry.get("key")
            # P0-R8-01a: an entry whose key was redacted at publish time
            # ("key_redacted": True) stays redacted here. The fallback
            # below reads the key back from memories — which would
            # re-materialise the payload into the report for a record
            # whose key IS the payload. The record id still names the
            # record; the report carries the flag instead of the key.
            redacted = bool(entry.get("key_redacted"))
            flag = {"key_redacted": True} if redacted else {}
            if redacted:
                key = None
            # Fall back to the erase event's affected ids when the entry
            # names no record, or names one that does not exist in this
            # scope (a pre-fix misattribution, e.g. a run id).
            if record_id is not None:
                row = db.execute(
                    "SELECT state FROM memories WHERE scope_id=? AND id=?",
                    (scope_id, record_id)).fetchone()
                if row is None:
                    record_id = affected[0] if affected else None
                    key = None
            elif affected:
                record_id = affected[0]
            if record_id is not None and key is None and not redacted:
                key_row = db.execute(
                    "SELECT key FROM memories WHERE scope_id=? AND id=?",
                    (scope_id, record_id)).fetchone()
                if key_row is not None:
                    key = key_row[0]
            if entry.get("task_text") or entry.get("kind_label") or entry.get("status_label"):
                # P0-R11-01: task text is live free text — it is never
                # scrubbed and never resolves in the live DB (the run's
                # operation history, not the erased record's own data;
                # the disposition is keep + honest PARTIAL, see
                # docs/KNOWN_LIMITATIONS.md §24), so an entry flagged
                # for it stays unresolved whatever the receipt check
                # says. The receipt check below would wrongly resolve
                # it: the receipt dump is tombstoned / clean while the
                # task text still holds the payload. The location names
                # are carried so the report names the column without
                # leaking content. The fallback attribution above still
                # applies (an unattributed entry names the erased
                # record), but it must not lead to a resolved verdict.
                # P1-R12-02: the same never-resolves rule covers the
                # kind and status-label residuals — kind is a structural
                # attribute that is never rewritten, and an erase_status
                # label sits in append-only history, so neither can
                # resolve in the live DB; the remedy for both is export
                # to a fresh DB (see docs/KNOWN_LIMITATIONS.md §25).
                dedup = (entry.get("table"), record_id, entry.get("rowid"), key)
                if dedup not in seen:
                    seen.add(dedup)
                    item = {"erase_event_id": ev_id, "table": entry.get("table"),
                            "record_id": record_id, "key": key, **flag}
                    if entry.get("task_text"):
                        item["task_text"] = True
                        item["task_locations"] = entry.get("task_locations", [])
                    if entry.get("kind_label"):
                        item["kind_label"] = True
                        item["kind_locations"] = entry.get("kind_locations", [])
                    if entry.get("status_label"):
                        item["status_label"] = True
                        item["status_locations"] = entry.get("status_locations", [])
                    unresolved.append(item)
                continue
            dedup = (entry.get("table"), record_id, entry.get("rowid"), key)
            if dedup in seen:
                continue
            seen.add(dedup)
            if record_id is not None:
                row = db.execute(
                    "SELECT state FROM memories WHERE scope_id=? AND id=?",
                    (scope_id, record_id)).fetchone()
                if row is None:
                    # Still unattributable: cannot be re-checked, stays.
                    unresolved.append({"erase_event_id": ev_id, "table": entry.get("table"),
                                       "record_id": record_id, "key": key, **flag})
                    continue
                if row[0] == "erased":
                    if entry.get("table") in ("runs", "idempotency"):
                        # The record is gone, but its receipts may still
                        # hold the payload (P0-R3-01): unresolved while
                        # any receipt does, resolved once they are clean
                        # (a repair erase clears exactly this).
                        if _receipt_holds_value(db, scope_id, record_id):
                            unresolved.append({"erase_event_id": ev_id, "table": entry.get("table"),
                                               "record_id": record_id, "key": key, **flag})
                        elif (entry.get("table") == "idempotency"
                                and _idempotency_key_matches_erased(
                                    db, scope_id, meta,
                                    affected[0] if affected else None)):
                            # P0-R10-02c: an idempotency row whose KEY
                            # is this payload survives the record's
                            # erasure when the row belongs to another
                            # operation (its key is the erased payload,
                            # not its own record's). Unresolved while
                            # any such key remains in scope. The keys
                            # are the ERASED record's (affected[0]),
                            # not the entry's record — the hashes are
                            # that record's payload's (P0-R12-01).
                            unresolved.append({"erase_event_id": ev_id, "table": entry.get("table"),
                                               "record_id": record_id, "key": key, **flag})
                        continue
                    if entry.get("table") == "events":
                        unresolved.append({"erase_event_id": ev_id, "table": entry.get("table"),
                                           "record_id": record_id, "key": key, **flag})
                        continue
                    if entry.get("table") == "sources":
                        # P0-R10-02d: erasing the citing record does
                        # NOT remove its link (the link's id is this
                        # payload, not that record's own) — unresolved
                        # while the record still cites a source whose
                        # id matches this erase's needle hashes.
                        if _live_source_matches_erased(
                                db, scope_id, record_id, meta,
                                key_record_id=affected[0] if affected else None):
                            unresolved.append({"erase_event_id": ev_id, "table": entry.get("table"),
                                               "record_id": record_id, "key": key, **flag})
                        continue
                    continue  # record gone / erased since: residual resolved
                if any(later_epoch > epoch and record_id in covered
                       for later_epoch, covered in complete_by_epoch):
                    continue  # a later COMPLETE erase of this record resolved it
            unresolved.append({"erase_event_id": ev_id, "table": entry.get("table"),
                               "record_id": record_id, "key": key, **flag})
    return unresolved


# Columns that can hold payload-derived text, per table. ONLY these
# are scanned for payload needles (P1-R4-06).
#
# The scan used to cover every text column of every table, but enum /
# identifier / timestamp columns can never hold a payload copy — and
# scanning them turns structural text into phantom residuals: every
# source_type ("user_statement", "tool_observation", ...) contains '_',
# every event_type ("remember", "retract", ...) contains 'e', record
# ids are hex (a short hex needle matches random ids), so a 1-char
# payload of "_" or "e" could never erase at all. The "id" exclusion
# the scan already had is the same principle, extended to the other
# non-payload columns. Keys, ids, states, kinds, digests and timestamps
# are identifiers by design and persist after erasure (the tombstoned
# row keeps its key; erase events record a vetted form of it — see
# _safe_erase_event_key) — they are not payload copies and are not
# scanned as columns. Three refinements: (P0-R7-01c) a LEGAL timestamp
# is what persists — an illegal timestamp field on the row being erased
# is cleared by the tombstone UPDATE itself, so no smuggled text
# survives in the tombstoned row; (P0-R8-02) inside the scanned cells
# the check is structural, by (table, column) and by path — a needle
# occurrence inside a legal timestamp counts only at a real audit
# position, never inside user data (see _cell_holds_needle), so a
# short needle that is a substring of its own expiry timestamp no
# longer refuses the erase, while the same text planted in a
# value_json "created_at" still does; (P2-R8-03) an identifier field
# inside a scanned cell counts only on whole-value equality, so a
# random id that merely contains a short needle substring is not a
# hit. Free-text columns that CAN hold a copy
# stay in: receipts, task labels, event metadata (reasons / leaked
# values). A future migration that adds a payload-bearing table or
# column must register it here, or the scan will miss it.
# (P0-R10-02) Four more columns registered: events.affected_ids_json
# (an event naming a record whose id IS the payload), events.actor_binding
# (an actor label that IS the payload), idempotency.idempotency_key, and
# sources.id (a live record citing a source whose id IS the payload).
# All four are identifier columns (see _IDENTIFIER_COLUMNS): whole-value
# equality only, never substring — the same rule as identifier fields
# inside JSON cells. memory_sources.source_id is NOT registered
# separately: its values are exactly the sources ids, and the scan
# resolves a sources hit to the citing records via memory_sources
# (see _describe_remaining), so one entry per citing record, no
# duplicates. Digests are NOT scanned: a digest never contains its
# input as a substring — an enumerable digest is neutralized at erase
# time instead (see _neutralize_idempotency_digests /
# _neutralize_payload_source_digests, P0-R10-01).
_PAYLOAD_COLUMNS = {
    "memories": {"value_json"},
    "runs": {"receipt_json", "task"},
    "idempotency": {"response_json", "idempotency_key"},
    "events": {"metadata_json", "affected_ids_json", "actor_binding"},
    "sources": {"id"},
}


def _scan_payload(db, payload, scope_id):
    """Return {table: [rowids]} for every payload-bearing text cell
    containing the payload (see _PAYLOAD_COLUMNS), in scope_id only.

    Scope-filtered (P2-R5-02): every payload table carries scope_id,
    and the scan restricts to it — one scope's erase must never see
    another scope's rows. Pre-fix the scan was unscoped, so erasing a
    record in s1 reported a second live record holding the same secret
    in s2 as a PARTIAL residual, and the persisted `remaining` entry
    named s2's record id and key (cross-scope disclosure).

    LIKE wildcards in the payload are escaped (P1-R4-06): ``%`` and
    ``_`` are wildcards in SQLite LIKE and ``\\`` is the escape
    character only when an explicit ESCAPE clause names it, so the
    pattern is built from the escaped payload and every query carries
    ``ESCAPE '\\'``. Without this a payload of "%" matches every
    non-null text cell and erasure is wrongly refused (or wrongly
    reported as a residual)."""
    found = {}
    for table, cols in sorted(_PAYLOAD_COLUMNS.items()):
        existing = {r[1] for r in db.execute(f"PRAGMA table_info({table})").fetchall()}
        text_cols = sorted(cols & existing)
        if not text_cols:
            continue
        escaped = (payload.replace("\\", "\\\\")
                          .replace("%", "\\%")
                          .replace("_", "\\_"))
        cond = " OR ".join(f"CAST({c} AS TEXT) LIKE ? ESCAPE '\\'" for c in text_cols)
        rows = db.execute(f"SELECT rowid FROM {table} WHERE scope_id=? AND ({cond})",
                          [scope_id] + [f"%{escaped}%"] * len(text_cols)).fetchall()
        if rows:
            found[table] = [r[0] for r in rows]
    return found


def _scan_for_remaining(db, scope_id, record_id, needles):
    """Post-scrub scan for the erase flow: _scan_payload_value_aware
    over every needle, minus the erased record's own tombstone row
    (P1-R4-06).

    Structural (P0-R7-01c, P0-R8-02, P2-R8-03): a needle occurrence
    inside a legal timestamp at a real audit position (an expire_set
    event's valid_until, a receipt dump's top-level
    created_at/updated_at/valid_until) is not a copy of the payload
    and is not reported — the raw scan would otherwise refuse every
    erase of a short value such as "202" whose needle is a substring
    of its own expiry timestamp. A needle that equals such a
    timestamp, or occurs anywhere else in the cell — including inside
    a timestamp field name in user data, which is never exempt — is
    still reported (see _cell_holds_needle). An identifier that
    merely contains a short needle as a substring is likewise not
    reported: only a whole-value match counts there. Whole-value
    matching is decode-aware (P0-R9-01/03): an identifier or a whole
    user-data value that decodes (known set) to a needle IS reported;
    a decoded fragment merely containing a short needle substring is
    not.

    The tombstone marker {"_erased": true} itself contains '_' (and
    'e', 'r', 'a', 's', 'd', 't', 'u', ...), so a payload needle that
    is a substring of the marker — e.g. "_" or "e" — would otherwise
    match the tombstone and report a phantom residual on every erase
    of such a value. Pre-fix this was masked: "_" never reached the
    scan (step 1 refused it first as a wildcard match). Only the
    erased record's own row is excluded, and only while it holds
    exactly the tombstone; any other row in the same scope matching a
    needle — including a second live record sharing the payload — is
    still reported. Rows in other scopes are never seen (P2-R5-02:
    _scan_payload is scope-filtered), so a same-secret record in
    another scope is neither a residual nor named in the outcome."""
    remaining = {}
    for needle in needles:
        for table, rows in _scan_payload_value_aware(db, needle, scope_id).items():
            remaining.setdefault(table, []).extend(rows)
    for table in remaining:
        remaining[table] = sorted(set(remaining[table]))
    row = db.execute("SELECT rowid, value_json FROM memories "
                     "WHERE scope_id=? AND id=?", (scope_id, record_id)).fetchone()
    if row is not None and "memories" in remaining:
        try:
            is_tombstone = json.loads(row[1]) == TOMBSTONE
        except ValueError:
            is_tombstone = False
        if is_tombstone:
            remaining["memories"] = [r for r in remaining["memories"]
                                     if r != row[0]]
            if not remaining["memories"]:
                del remaining["memories"]
    return remaining


def _needles_from_receipts(db, scope_id, record_id):
    """Reconstruct payload needles for an already-erased record from the
    receipt copies still holding its value.

    The record itself is tombstoned, so its payload cannot be recovered
    from memories — but any receipt dump owned by the record still
    carries a copy, and that copy yields exactly the needles the repair
    erase needs (raw JSON text + string leaves). Only dumps owned by
    this record contribute: a second live record sharing the payload
    keeps its own needles out of this set. Returns an empty set when no
    receipt still holds the record's value (nothing left to scrub)."""
    needles = set()
    for table, col in SCRUBBABLE:
        for (raw,) in db.execute(
                f"SELECT {col} FROM {table} WHERE scope_id=?", (scope_id,)).fetchall():
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            for dump in _iter_record_dumps(data):
                if _dump_owner(dump) == record_id and "value" in dump:
                    # A dump already holding the tombstone carries no
                    # payload — and its marker text must NOT become a
                    # needle, or the repair scan would flag the tombstone
                    # itself as a remaining copy.
                    if dump["value"] == TOMBSTONE:
                        continue
                    try:
                        needles.add(_json(dump["value"]))
                    except TurnkeepError:
                        pass
                    needles.update(_string_leaves(dump["value"]))
    return needles


def _scrub_receipts(db, scope_id, record_id, payload):
    """Replace the payload inside receipts that reference this record.

    Scrub by NEEDLE and OWNER (P0-R3-01): only rows whose raw text
    contains the needle are touched, and inside each row only the dumps
    owned by this record (owner id in "record_id" for finish receipts
    or "id" for dump_record() output, including dumps nested in begin
    receipts' "records" / "conflicts" lists) have their "value"
    replaced by the tombstone. Dumps owned by other records in the same
    receipt are left alone — their payload is theirs, and the post-scrub
    scan reports them as a residual instead of silently corrupting a
    live record's receipt. Ids, keys and revisions are kept, so the
    receipt stays a valid pointer."""
    scrubbed = []
    for table, col in SCRUBBABLE:
        for row in db.execute(f"SELECT rowid, {col} FROM {table} WHERE scope_id=?", (scope_id,)).fetchall():
            raw = row[1]
            if not raw or payload not in raw:
                continue
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            changed = False
            for dump in _iter_record_dumps(data):
                if _dump_owner(dump) == record_id and dump.get("value") != TOMBSTONE:
                    dump["value"] = TOMBSTONE
                    changed = True
            if changed:
                db.execute(f"UPDATE {table} SET {col}=? WHERE rowid=?", (_json(data), row[0]))
                scrubbed.append(f"{table}:{row[0]}")
    return scrubbed


def _scrub_receipt_identifiers(db, scope_id, record_id, needle_hashes):
    """Replace a receipt dump's key / source-ref ids that ARE an erased
    payload (P0-R8-01b, P1).

    The value scrub (_scrub_receipts) only replaces "value": a dump
    whose key or source-ref id equals the payload kept the recoverable
    text while the erase reported COMPLETE (import path) or PARTIAL-
    but-clean (idempotency receipts). Identification is by hash, not
    by substring: each identifier (verbatim, and in every known
    decoding) is hashed both ways and compared with the needle hashes
    given (live hashes in the first erase, stored hashes in the repair
    path, where the live needles are gone because the receipts are
    already scrubbed). A random id that merely contains a short
    needle substring is never touched (whole-value rule, P2-R8-03).
    A matching identifier is replaced by its HMAC hex, keyed by the
    record's first verification key (its per-record key when one
    exists — P0-R12-01): the dump keeps a stable, unique pointer, but
    the payload text is gone, and the replacement is retired together
    with that key when the record reaches terminal COMPLETE (a
    scope-keyed replacement would stay enumerable, since the scope key
    is never deleted). Returns the number of identifiers replaced.
    Runs over every receipt row, not just rows containing the verbatim
    needle: an encoded identifier (Base64 of the payload) does not
    contain the needle as text, so the per-needle row filter would
    skip it."""
    if not needle_hashes:
        return 0
    keys = _record_verification_keys(db, scope_id, record_id)
    replacement_key = keys[0] if keys else None
    replaced = 0
    # A run snapshot (runs.snapshot_json) is a live derived copy too:
    # it lists every record the run read as {id, key, revision, state}.
    # A snapshot entry for the erased record whose key IS the payload
    # holds the recoverable text exactly like a receipt dump does, so
    # the same hash rule replaces it. Only the erased record's own
    # entries are touched (entry "id" == record_id): another record's
    # snapshot entry is that record's own copy, and erasing this record
    # must not rewrite it — the scan flags it instead, honestly.
    for (rowid, raw) in db.execute(
            "SELECT rowid, snapshot_json FROM runs WHERE scope_id=?", (scope_id,)).fetchall():
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        changed = False
        for group in ("records", "conflicts"):
            for entry in data.get(group, []) or []:
                if (isinstance(entry, dict) and entry.get("id") == record_id
                        and isinstance(entry.get("key"), str)
                        and _identifier_matches_hashes(entry["key"], needle_hashes, keys)):
                    entry["key"] = _identifier_replacement(entry["key"], replacement_key)
                    changed = True
                    replaced += 1
        if changed:
            db.execute("UPDATE runs SET snapshot_json=? WHERE rowid=?", (_json(data), rowid))
    for table, col in SCRUBBABLE:
        for row in db.execute(f"SELECT rowid, {col} FROM {table} WHERE scope_id=?", (scope_id,)).fetchall():
            raw = row[1]
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            changed = False
            for dump in _iter_record_dumps(data):
                if _dump_owner(dump) != record_id:
                    continue
                if isinstance(dump.get("key"), str) and _identifier_matches_hashes(
                        dump["key"], needle_hashes, keys):
                    dump["key"] = _identifier_replacement(dump["key"], replacement_key)
                    changed = True
                    replaced += 1
                for src in dump.get("source_refs", []) or []:
                    if (isinstance(src, dict) and isinstance(src.get("id"), str)
                            and _identifier_matches_hashes(src["id"], needle_hashes, keys)):
                        src["id"] = _identifier_replacement(src["id"], replacement_key)
                        changed = True
                        replaced += 1
            if changed:
                db.execute(f"UPDATE {table} SET {col}=? WHERE rowid=?", (_json(data), row[0]))
    return replaced


def _clear_identifier_sources(db, scope_id, record_id, needle_hashes):
    """Delete the erased record's source links whose id IS an erased
    payload (P0-R8-01b), and orphaned sources rows.

    Only the erased record's own links are removed: a source id is
    provenance shared by every record that cites it, so a link
    belonging to a still-live record is left alone (that record is
    excluded from export instead — see portability.export_scope).
    Identification is by hash, as in _scrub_receipt_identifiers. A
    sources row is deleted only when no memory_sources row anywhere
    in the scope still references it (the FK would refuse otherwise).
    Returns the number of source ids cleared."""
    if not needle_hashes:
        return 0
    keys = _record_verification_keys(db, scope_id, record_id)
    cleared = []
    for (sid,) in db.execute(
            "SELECT source_id FROM memory_sources WHERE scope_id=? AND memory_id=?",
            (scope_id, record_id)).fetchall():
        if _identifier_matches_hashes(sid, needle_hashes, keys):
            db.execute("DELETE FROM memory_sources WHERE scope_id=? AND memory_id=? AND source_id=?",
                       (scope_id, record_id, sid))
            cleared.append(sid)
    for sid in cleared:
        if db.execute("SELECT 1 FROM memory_sources WHERE scope_id=? AND source_id=?",
                      (scope_id, sid)).fetchone() is None:
            db.execute("DELETE FROM sources WHERE scope_id=? AND id=?", (scope_id, sid))
    return len(cleared)


def _response_owners(data):
    """Record ids a stored idempotency response belongs to (P0-R10-01).

    Dump owners inside the response (remember / correct / retract /
    resolve_conflict receipts) plus a top-level "record_id" (finish
    receipts, which carry no "value" and so are not dumps). A begin
    receipt's "records" list holds dumps of every record active at
    begin time, so a begin row is located when ANY of them is the
    erased record. The response is what ties an idempotency row (and
    a finish receipt) to the record its operation produced — the
    request itself is not stored, only its digest."""
    owners = set()
    for dump in _iter_record_dumps(data):
        owner = _dump_owner(dump)
        if owner:
            owners.add(owner)
    if isinstance(data, dict) and isinstance(data.get("record_id"), str):
        owners.add(data["record_id"])
    return owners


def _neutralize_idempotency_digests(db, scope_id, record_id, needle_hashes):
    """Neutralize the request digests of the erased record's own
    idempotency rows, and drop a finish receipt's digest (P0-R10-01).

    A stored request_digest is a plain SHA-256 of the request, which
    contains the raw value for remember / correct / finish /
    resolve_conflict (the task for begin, the record id + reason for
    retract) — a low-entropy value is recoverable offline by
    enumerating candidates and re-computing the digest, while the
    erase reports COMPLETE. The response_json is already scrubbed to
    a tombstone by _scrub_receipts; the digest is the only remaining
    recoverable material on the row, so it is neutralized here.
    The rule is uniform over all six operations using request_digest
    (remember / finish / begin / correct / retract /
    resolve_conflict), located by the stored response: a row whose
    response belongs to the erased record (see _response_owners) has
    its digest replaced by NEUTRALIZED_DIGEST ("erased" — never a
    computed digest, so a replay raises IDEMPOTENCY_MISMATCH instead
    of returning the stored response). HMAC is NOT used: the
    per-scope HMAC key lives in the same DB (scope_secrets), so a DB
    holder enumerates an HMAC just as easily (R10 verification repro
    A1b.2). The digest computation is unchanged (no version bump, no
    dual-accept path): only stored digests of erased records are
    neutralized; rows belonging to live records are untouched, so
    their replays keep working. A finish receipt carries the digest
    twice more — the runs row's request_digest column and the
    receipt_json's "request_digest" field — both are neutralized for
    a receipt of the erased record (begin receipts carry no digest).
    One exception, by deletion not neutralization (P0-R10-02c): a row
    whose own idempotency_key IS the erased payload is deleted
    outright — the row is the erased operation's own derived copy,
    and its key (part of the row's primary key) cannot be scrubbed
    the way a receipt dump's key can. Any OTHER record's row whose
    key is this payload is left alone and flagged by the scan
    instead (see _describe_remaining). Returns
    (neutralized, deleted) counts — digests replaced, rows deleted.
    Re-running is a no-op (a neutralized digest already equals the
    marker; a deleted row is gone), so the repair path can call this
    again safely."""
    keys = _record_verification_keys(db, scope_id, record_id)
    neutralized = deleted = 0
    for rowid, key, digest, raw in db.execute(
            "SELECT rowid, idempotency_key, request_digest, response_json "
            "FROM idempotency WHERE scope_id=?", (scope_id,)).fetchall():
        try:
            data = json.loads(raw) if raw else None
        except ValueError:
            data = None
        if data is None or record_id not in _response_owners(data):
            continue
        if needle_hashes and _identifier_matches_hashes(
                key, needle_hashes, keys):
            db.execute("DELETE FROM idempotency WHERE rowid=?", (rowid,))
            deleted += 1
            continue
        if digest != NEUTRALIZED_DIGEST:
            db.execute("UPDATE idempotency SET request_digest=? WHERE rowid=?",
                       (NEUTRALIZED_DIGEST, rowid))
            neutralized += 1
    for rowid, raw in db.execute(
            "SELECT rowid, receipt_json FROM runs WHERE scope_id=? AND receipt_json IS NOT NULL",
            (scope_id,)).fetchall():
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(data, dict) or data.get("record_id") != record_id:
            continue
        touched = False
        if "request_digest" in data:
            del data["request_digest"]
            db.execute("UPDATE runs SET receipt_json=? WHERE rowid=?", (_json(data), rowid))
            touched = True
        cur = db.execute("SELECT request_digest FROM runs WHERE rowid=?", (rowid,)).fetchone()
        if cur is not None and cur[0] != NEUTRALIZED_DIGEST:
            db.execute("UPDATE runs SET request_digest=? WHERE rowid=?", (NEUTRALIZED_DIGEST, rowid))
            touched = True
        if touched:
            neutralized += 1
    return neutralized, deleted


def _neutralize_payload_source_digests(db, scope_id, needle_hashes, record_id=None):
    """Neutralize content_digest values that encode an erased payload
    (P0-R10-01b).

    sources.content_digest is a plain SHA-256 of the source dict
    (type + id), so a source id that IS the payload is enumerable
    from it exactly like a request_digest. The digest is neutralized
    (set NULL) for every sources row whose id matches the erased
    payload hashes — whether the row survives because a live record
    still cites it (the id itself stays as a tracked P0-R10-02d
    residual) or because it is orphaned. A row deleted as orphaned
    by _clear_identifier_sources takes its digest with it and needs
    nothing here. upsert_sources tolerates a NULL stored digest (the
    type is still checked; the digest is not re-verified and not
    restored — once neutralized it stays neutralized). Returns the
    number of digests neutralized."""
    if not needle_hashes:
        return 0
    # P0-R12-01: the hashes belong to the record being erased, so they
    # verify under its verification keys; without a record id the scope
    # key is the legacy fallback.
    keys = (_record_verification_keys(db, scope_id, record_id)
            if record_id is not None else _read_scope_hmac_key(db, scope_id))
    neutralized = 0
    for sid, digest in db.execute(
            "SELECT id, content_digest FROM sources WHERE scope_id=?",
            (scope_id,)).fetchall():
        if digest is None:
            continue
        if _identifier_matches_hashes(sid, needle_hashes, keys):
            db.execute("UPDATE sources SET content_digest=NULL WHERE scope_id=? AND id=?",
                       (scope_id, sid))
            neutralized += 1
    return neutralized


# P0-R13-01: the busy_timeout the maintenance checkpoint waits for a
# concurrent read transaction to release its snapshot. The connection's
# own timeout is 10 s (storage.open_db) — under a held reader every
# erase / repair would stall the full 10 s and STILL report busy, so
# the maintenance checkpoint waits only this long: a transient reader
# (a normal recall holds a snapshot for milliseconds) clears inside
# it, a long-lived reader is reported fast and honestly instead, and
# convergence is via a later repair re-run (see the
# maintenance_pending marker). The previous timeout is restored
# afterwards (see _run_maintenance).
_MAINTENANCE_CHECKPOINT_TIMEOUT_MS = 1000


def _run_maintenance(db):
    """WAL checkpoint (TRUNCATE) + VACUUM so old pages leave the live DB
    file (best effort; a lock is reported, not fatal). Returns failure
    notes, empty on success.

    P0-R13-01: the checkpoint's (busy, log, checkpointed) return triple
    is READ — busy != 0 means the checkpoint did not complete and is a
    maintenance FAILURE, whatever checkpointed says: the WAL could not
    be reset (a concurrent read transaction holds a snapshot), so a
    deleted key's bytes may still be recoverable from the live DB file
    / WAL. (1, 83, 83) — checkpointed == log — is still a failure:
    the frames were copied but the WAL cannot be truncated while a
    read mark is held. The old code discarded the return value and
    only caught exceptions, so a busy checkpoint was reported as a
    success and the erase paths' PARTIAL downgrade could never
    trigger. VACUUM runs after the checkpoint but is NOT the purge
    action: in this stack's WAL mode it compacts the b-trees
    (page_count drops, freelist -> 0) yet does not shrink the file
    and removes no deleted bytes beyond what the checkpoint already
    removed (under secure_delete=ON — set on every open_db() and
    gated at the erase entry, P0-R15-01 — a delete +
    checkpoint(busy=0) already leaves the deleted key bytes out of
    the live file; under OFF the deleted cell stays in the page
    freeblock and the checkpoint copies that page into the main
    file, so the checkpoint alone cannot purge it). VACUUM's
    success says nothing about purge, and a VACUUM failure alone
    never fails the purge verdict — see _purge_failed). This
    function runs NO checkpoint after the VACUUM: VACUUM's compact
    pages go to the WAL, so the main file is only as clean as the
    pre-VACUUM checkpoint left it — the gap that let an OFF erasure
    report clean while the key stayed recoverable (P0-R15-01). The
    file-level purge acceptance (_verify_purge, run by the erase
    settlement after this function) is what catches that gap; it
    does not reorder this function, so the R13 busy-checkpoint
    behaviour is unchanged."""
    maintenance = []
    try:
        prev_timeout = db.execute("PRAGMA busy_timeout").fetchone()[0]
    except Exception:  # noqa: BLE001 - a pragma read must not fail maintenance
        prev_timeout = None
    try:
        db.execute(f"PRAGMA busy_timeout={_MAINTENANCE_CHECKPOINT_TIMEOUT_MS}")
        try:
            row = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        except Exception as e:  # noqa: BLE001 - a lock must be reported, not fatal
            maintenance.append(f"PRAGMA wal_checkpoint(TRUNCATE) failed: {e}")
            row = None
        if row is not None:
            busy, log, checkpointed = int(row[0]), int(row[1]), int(row[2])
            if busy != 0:
                maintenance.append(
                    "PRAGMA wal_checkpoint(TRUNCATE) busy: "
                    f"({busy}, {log}, {checkpointed})")
    finally:
        if prev_timeout is not None:
            try:
                db.execute(f"PRAGMA busy_timeout={prev_timeout}")
            except Exception:  # noqa: BLE001 - restoring must not raise
                pass
    # VACUUM reclaims freelist space; it is not part of the purge
    # guarantee. Its failure is a diagnostic note only.
    try:
        db.execute("VACUUM")
    except Exception as e:  # noqa: BLE001 - reported, not fatal
        maintenance.append(f"VACUUM failed: {e}")
    return maintenance


def _purge_failed(maintenance):
    """True when the maintenance notes record a WAL checkpoint failure
    (P0-R13-01) — i.e. the live file was NOT purged. Only the
    checkpoint decides the purge verdict: a VACUUM failure alone
    (space reclamation) never fails it."""
    return any("wal_checkpoint" in note for note in maintenance)


def _require_secure_delete(db):
    """P0-R15-01: fail-closed gate at the erase entry.

    secure_delete is per-connection (set by storage.open_db() on
    every open) and NOT persisted in the file, so an erase must
    confirm the live connection actually has it ON before doing
    anything: under OFF a deleted verification key's cell bytes
    stay in the main DB file and the erase would report COMPLETE
    while the PIN stays recoverable from an ordinary file read
    (P0-R15-01). Read-back != 1 refuses the erase outright
    (TurnkeepError, nothing written, the record keeps its pre-erase
    state and the call is retryable) — a silent downgrade to PARTIAL
    would be read as "a convergence-pending residual", but here the
    erase itself is untrustworthy. The check runs BEFORE the erase
    transaction in both erase paths (erase_record and
    _try_repair_erased), so a failing check can never delete a key
    first and then report the failure."""
    try:
        row = db.execute("PRAGMA secure_delete").fetchone()
    except Exception as e:  # noqa: BLE001 - a pragma read failure is a fail-closed erase refusal
        raise TurnkeepError(
            "SECURE_DELETE_UNAVAILABLE",
            f"cannot confirm secure_delete on this connection: {e}")
    if row is None or int(row[0]) != 1:
        raise TurnkeepError(
            "SECURE_DELETE_OFF",
            "secure_delete is not enabled on this connection; refusing "
            "to erase — a deleted key's bytes would stay recoverable "
            "from the live DB file")


def _db_file_path(db):
    """Filesystem path of the connection's main database file.

    Recovered via PRAGMA database_list (sqlite3.Connection has no
    public path attribute — see cli.db_path). Returns None for a
    :memory: database, which has no file to probe."""
    for row in db.execute("PRAGMA database_list").fetchall():
        if row[1] == "main":
            return row[2] or None
    return None


def _hex_run_candidates(blob):
    """Candidate 32-byte keys from >=64-char hex runs in file bytes.

    Sliding window (R14 verification): a fixed 64-char regex window
    is insufficient — the key hex nests inside longer hex strings
    (adjacent cell payloads are hex too)."""
    candidates = set()
    for match in re.finditer(rb"[0-9a-f]{64,}", blob):
        run = match.group(0).decode("ascii")
        for off in range(len(run) - 63):
            candidates.add(run[off:off + 64])
    return candidates


def _candidate_recovers_pin(key, needle_hashes):
    """The low-entropy enumeration probe (P0-R15-01): True when this
    candidate 32-byte key verifies any historical needle hash for a
    4-digit PIN, else False. Manual HMAC-SHA256 with the inner/outer
    states computed once per key and copied per PIN (identical output
    to hmac.new(key, msg, hashlib.sha256) — see the equivalence test)
    because the enumeration runs 10,000 PINs per candidate. The
    threat model is low-entropy payloads — a high-entropy payload
    cannot be enumerated this way, and the check does not claim
    otherwise.

    P1-R16-02: the recovered candidate PIN is NEVER returned — the
    function answers only "a low-entropy value is recoverable", so
    no erased value can leak into a return dict, an exception, a log
    or a test diagnostic through this probe. The PIN string exists
    only inside the enumeration loop below."""
    block = 64
    k = key
    if len(k) > block:
        k = hashlib.sha256(k).digest()
    k = k + b"\x00" * (block - len(k))
    ipad = bytes(b ^ 0x36 for b in k)
    opad = bytes(b ^ 0x5C for b in k)
    inner_base = hashlib.sha256(ipad)
    outer_base = hashlib.sha256(opad)
    # Compare raw digests (not hex) and reuse the precomputed PIN
    # bytes: the loop runs 10,000 times per candidate, so per-PIN
    # string formatting and hex conversion dominate.
    hash_bytes = set()
    for h in needle_hashes:
        try:
            hash_bytes.add(bytes.fromhex(h))
        except ValueError:
            continue
    for i, pin in enumerate(_PIN_BYTES):
        inner = inner_base.copy()
        inner.update(pin)
        outer = outer_base.copy()
        outer.update(inner.digest())
        if outer.digest() in hash_bytes:
            return True
    return False


# The 10,000 4-digit PINs as bytes, precomputed once: the enumeration
# loop runs per candidate, so formatting them per PIN would dominate.
_PIN_BYTES = tuple(f"{i:04d}".encode("ascii") for i in range(10000))


def _live_key_hexes(db):
    """Hex of every verification key still live in this DB (the
    per-record keys in record_erase_keys + the legacy scope key in
    scope_secrets). A live key is NOT residue — its row still exists
    and the repair / residual paths still need it — so the file
    probes skip these hexes: without the skip, the legacy scope key
    (which still verifies pre-R12 scope-keyed hashes) would fail
    every purge check on a migrated DB."""
    hexes = set()
    for (h,) in db.execute("SELECT secret_hex FROM record_erase_keys").fetchall():
        if isinstance(h, str):
            hexes.add(h.lower())
    for (h,) in db.execute("SELECT secret_hex FROM scope_secrets").fetchall():
        if isinstance(h, str):
            hexes.add(h.lower())
    return hexes


def _read_file_bytes(path):
    """Main DB file bytes + WAL bytes, read separately (never merged
    into one blob for the probe — the two files are different
    evidence: a key in the WAL is not a key in the main file)."""
    main = Path(path).read_bytes() if Path(path).exists() else b""
    wal_path = Path(str(path) + "-wal")
    wal = wal_path.read_bytes() if wal_path.exists() else b""
    return main, wal


def _probe_files(db, needle_hashes):
    """Blind residue probe over the live DB file + WAL: True when
    some key in the files verifies a historical needle hash for a
    low-entropy value, else False. Live keys are skipped (they are
    not residue). A :memory: database has no file: vacuously False.
    Reads file bytes only — opens no new connection.

    P1-R16-02: the probe answers a boolean only — the recovered
    value is never returned, so purge_residue's return dict (and the
    CLI that prints it) cannot re-disclose an erased PIN. Coverage
    is 4-digit PINs only (see _candidate_recovers_pin): a 6-digit or
    opaque value is outside the probe's range, which is a disclosed
    blind spot (docs/KNOWN_LIMITATIONS.md §29) — the probe is one
    acceptance signal, never the whole purge verdict: a busy
    checkpoint fails the purge even when the probe finds nothing
    (P0-R16-01)."""
    path = _db_file_path(db)
    if path is None:
        return False
    if not needle_hashes:
        return False
    main, wal = _read_file_bytes(path)
    live = _live_key_hexes(db)
    candidates = set()
    for blob in (main, wal):
        candidates |= _hex_run_candidates(blob)
    candidates -= live
    for cand in sorted(candidates):
        try:
            key = bytes.fromhex(cand)
        except ValueError:
            continue
        if _candidate_recovers_pin(key, needle_hashes):
            return True
    return False


def _verify_purge(db, key_hex, needle_hashes):
    """P0-R15-01: file-level purge ACCEPTANCE for a terminal COMPLETE
    erase. True when the deleted verification key is NOT recoverable
    from the live DB file / WAL. Two stages, both required:

    (1) the deleted key's hex ASCII must not occur in either file
        (the form the key is stored in — the record_erase_keys
        .secret_hex TEXT value; the file holds hex, not raw bytes);
    (2) a sliding-window enumeration probe over both files must not
        recover any low-entropy value from the historical needle
        hashes (a key in any other recoverable form).

    Live keys are skipped in stage (2) — a live key is not residue
    (see _live_key_hexes). The key assertion is mandatory; the
    payload-byte assertion is belt-and-braces (the busy-WAL case:
    busy checkpoint leaves remember frames in the WAL — see §27)
    and is NOT asserted here as "payload absent from the main file
    after an OFF chain", because in this layout the payload occurs
    0 times there (R15 verification §1.4: the recoverable material
    is the deleted KEY, not the raw payload). A :memory: database
    has no file: vacuously True. Reads file bytes only — opens no
    new connection, and never runs inside the settlement
    transaction."""
    path = _db_file_path(db)
    if path is None:
        return True
    main, wal = _read_file_bytes(path)
    key_ascii = key_hex.encode("ascii") if isinstance(key_hex, str) else bytes(key_hex)
    if key_ascii in main or key_ascii in wal:
        return False
    return not _probe_files(db, needle_hashes)


def _all_stored_needle_hashes(db, scope_id=None):
    """All needle hashes persisted by erase events (every scope, or
    one scope when scope_id is given) — the historical hash set the
    residue probe needs."""
    if scope_id is None:
        rows = db.execute(
            "SELECT metadata_json FROM events WHERE event_type='erase'").fetchall()
    else:
        rows = db.execute(
            "SELECT metadata_json FROM events WHERE scope_id=? AND event_type='erase'",
            (scope_id,)).fetchall()
    hashes = set()
    for (meta_json,) in rows:
        try:
            meta = json.loads(meta_json) if meta_json else {}
        except ValueError:
            continue
        for h in meta.get("needle_hashes", []) or []:
            if isinstance(h, str):
                hashes.add(h)
    return hashes


def _purge_generation(db, scope_id):
    """Snapshot the generation a purge_residue run covers (P0-R18-01).

    Returns (erase_event_ids, marker_rows): the ids of the erase
    events and the (scope_id, detail, created_at) rows of
    purge_pending visible right now. A scope_id of None covers
    every scope; an explicit scope covers only itself. Content
    sets, not rowids: VACUUM rebuilds the file, so a rowid snapshot
    would be fragile — event ids (TEXT PRIMARY KEY) and marker rows
    are stable across a rebuild. The purge_pending read is guarded
    on the table's existence so a pre-v7 DB opened without
    migration degrades to the event-only set instead of failing (as
    in _purge_settlement_scopes)."""
    if scope_id is None:
        event_ids = {r[0] for r in db.execute(
            "SELECT id FROM events WHERE event_type='erase'").fetchall()}
        if db.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                "AND name='purge_pending'").fetchone()[0]:
            markers = {tuple(r) for r in db.execute(
                "SELECT scope_id, detail, created_at FROM purge_pending"
                ).fetchall()}
        else:
            markers = set()
    else:
        event_ids = {r[0] for r in db.execute(
            "SELECT id FROM events WHERE scope_id=? AND event_type='erase'",
            (scope_id,)).fetchall()}
        if db.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                "AND name='purge_pending'").fetchone()[0]:
            markers = {tuple(r) for r in db.execute(
                "SELECT scope_id, detail, created_at FROM purge_pending "
                "WHERE scope_id=?", (scope_id,)).fetchall()}
        else:
            markers = set()
    return event_ids, markers


def _resurrected_key_rows(db, scope_id):
    """Live record_erase_keys rows that must be absent (P0-R18-01).

    A row is resurrected when its record is erased, its latest
    erase event says COMPLETE (the scan outcome), and no
    maintenance_pending marker stands for it. The marker is the
    discriminator: a key restored after a failed purge keeps its
    marker (the restore and the marker write commit together), so
    that row is legitimate — the repair still needs it. A PARTIAL
    latest event keeps the key for the same reason, and a live
    record's key row is never evidence of resurrection (no erase
    event at all, or the record is not tombstoned). Returns
    [(scope_id, record_id, secret_hex)]. "Latest" is by
    scope_epoch DESC — the per-scope event sequence, not rowid."""
    resurrected = []
    for (sid, rid, hexv) in db.execute(
            "SELECT scope_id, record_id, secret_hex FROM record_erase_keys"
            ).fetchall():
        if scope_id is not None and sid != scope_id:
            continue
        state = db.execute(
            "SELECT state FROM memories WHERE scope_id=? AND id=?",
            (sid, rid)).fetchone()
        if state is None or state[0] != "erased":
            continue
        latest = None
        for (meta_json, aff_json) in db.execute(
                "SELECT metadata_json, affected_ids_json FROM events "
                "WHERE scope_id=? AND event_type='erase' "
                "ORDER BY scope_epoch DESC", (sid,)).fetchall():
            try:
                affected = json.loads(aff_json) if aff_json else {}
            except ValueError:
                continue
            if rid not in affected:
                continue
            try:
                meta = json.loads(meta_json) if meta_json else {}
            except ValueError:
                meta = {}
            latest = meta.get("erase_status")
            break
        if latest != "COMPLETE":
            continue
        marker = db.execute(
            "SELECT COUNT(*) FROM maintenance_pending "
            "WHERE scope_id=? AND record_id=?", (sid, rid)).fetchone()[0]
        if marker:
            continue
        resurrected.append((sid, rid, hexv))
    return resurrected


def _purge_settlement_scopes(db, scope_id):
    """Scopes a purge_residue settlement covers (P2-R17-01 union).

    A global call (scope_id=None) collects the UNION of
    erase-event scopes and purge_pending scopes; an explicit scope
    call settles only that scope. The purge_pending read is guarded
    on the table's existence so a pre-v7 DB opened without
    migration degrades to the event-only set instead of failing."""
    if scope_id is not None:
        return [scope_id]
    scopes = {r[0] for r in db.execute(
        "SELECT DISTINCT scope_id FROM events "
        "WHERE event_type='erase'").fetchall()}
    if db.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
            "AND name='purge_pending'").fetchone()[0]:
        scopes |= {r[0] for r in db.execute(
            "SELECT DISTINCT scope_id FROM purge_pending").fetchall()}
    return sorted(scopes)


def _purge_failure_detail(checkpoint, residue_before, residue_after, error):
    """The purge_pending marker detail for a failed purge_residue
    (P0-R16-03): checkpoint triple and boolean probe results only —
    NEVER a recovered value (P1-R16-02)."""
    return (
        "purge_residue failed: "
        f"checkpoint={checkpoint}; "
        f"residue_before={residue_before}; "
        f"residue_after={residue_after}"
        + (f"; error={error}" if error else ""))


def _purge_settlement_recheck(db, scope_id, gen_events, gen_markers):
    """Post-settlement recheck for purge_residue (P0-R18-01).

    Runs INSIDE the settlement write transaction (BEGIN IMMEDIATE),
    after the physical cleanup, before any marker is cleared — so
    no other writer can commit between this final check and the
    marker clear. Returns (notes, resurrected): notes is a list of
    failure notes (empty = the settlement may clear the markers);
    resurrected is the list of resurrected key rows found (see
    _resurrected_key_rows). The notes name counts and check names
    only — never a record id, scope value, or key (P1-R16-02: the
    notes land in the marker detail, which residual_report()
    surfaces verbatim).

    Checks, in order:
      (a) generation: no erase event and no purge_pending row
          appeared after the pre-cleanup snapshot. A new erase
          event means new hashes/keys the probe never saw (the GPT
          R18 main variant: a legacy OFF erase chain landing after
          the checkpoint); a new marker means a scope's failure
          state appeared after the check (the GPT R18 second
          interleaving variant: a concurrent explicit purge writing
          a marker the global call then cleared on the stale
          verdict). Either fails the purge.
      (b) no resurrected verification-key row for a terminal-COMPLETE
          record (a concurrent old-version write re-inserting a
          deleted key — the verification's A1/A2/A4 constructions).
      (c) the probe over the current hashes finds no recoverable
          low-entropy value (4-digit PIN range — the disclosed
          blind spot for six-digit/opaque values is unchanged).
      (d) each resurrected key's hex is absent from the main DB
          file and WAL (the file-level confirmation of (b): a live
          row's hex is necessarily in the file, so (d) fires
          exactly when (b) does — belt and braces, and it names
          the file evidence the P0 is about).
    Any failure — or any error running the recheck — fails closed:
    the caller keeps/refreshes the markers and reports
    cleaned=False. A write landing after this transaction commits
    is a NEW generation: the verdict is correct as of the commit,
    and the next purge_residue re-check covers it (see §31).

    The resurrected rows are returned (not deleted here) so the
    caller can delete them in the same settlement transaction:
    a resurrected row is a row that by design must be absent (its
    record is terminal-COMPLETE), so deleting it restores the
    invariant — and it is what makes a bare purge_residue retry
    converge (the row is committed data; the writer exiting does
    not remove it, so flagging it without deleting it would fail
    every later retry too). The deletion removes the row only; its
    bytes leave the file on the next VACUUM + busy==0 checkpoint,
    so this call still reports cleaned=False and the retry is the
    one that reports clean (see §31)."""
    notes = []
    resurrected = []
    try:
        cur_events, cur_markers = _purge_generation(db, scope_id)
        new_events = cur_events - gen_events
        if new_events:
            notes.append(
                "purge recheck failed: generation changed: "
                f"{len(new_events)} new erase event(s) after the "
                "physical check")
        if cur_markers != gen_markers:
            notes.append(
                "purge recheck failed: generation changed: "
                "purge_pending markers changed after the physical check")
        resurrected = _resurrected_key_rows(db, scope_id)
        if resurrected:
            notes.append(
                "purge recheck failed: "
                f"{len(resurrected)} resurrected verification-key row(s) "
                "for terminal-COMPLETE record(s)")
        hashes = _all_stored_needle_hashes(db, scope_id)
        if _probe_files(db, hashes):
            notes.append(
                "purge recheck failed: probe found recoverable residue")
        if resurrected:
            path = _db_file_path(db)
            main, wal = _read_file_bytes(path) if path else (b"", b"")
            for (_sid, _rid, hexv) in resurrected:
                key_ascii = (hexv.encode("ascii")
                             if isinstance(hexv, str) else bytes(hexv))
                if key_ascii in main or key_ascii in wal:
                    notes.append(
                        "purge recheck failed: resurrected key bytes "
                        "present in the live DB file / WAL")
                    break
    except Exception as e:  # noqa: BLE001 - a recheck that cannot run fails closed
        notes.append(f"purge recheck could not run: {e}")
    return notes, resurrected


# P0-R19-01: bounds for the failure-branch marker settlement retry.
# The budget matches the connection's default busy_timeout (10 s):
# a writer that commits inside it is transient contention (the GPT
# R19 scenario-B writer commits in 0.748 s); a writer holding the
# write lock past it is a stuck transaction, and the settlement then
# reports the unpersisted marker explicitly instead of pretending
# it was written (see _settle_failure_markers). Each attempt uses a
# short busy_timeout so one attempt cannot eat the whole budget
# when the caller set a long one.
_FAILURE_MARKER_RETRY_BUDGET_S = 10.0
_FAILURE_MARKER_RETRY_SLEEP_S = 0.05
_FAILURE_MARKER_ATTEMPT_TIMEOUT_MS = 100


def _settle_failure_markers(db, scope_id, detail, gen_events, gen_markers):
    """Persist failure markers for the re-collected scope union (P0-R19-01).

    The failure branch of purge_residue (cleaned=False) settles here.
    The scope collection + marker write run inside ONE write
    transaction (BEGIN IMMEDIATE), and the scope union + generation
    are re-collected INSIDE that transaction — never the stale
    pre-cleanup set. A writer that committed after the physical
    check (a legacy OFF erase chain landing new erase events, a
    concurrent explicit purge writing a new marker) is therefore
    included: its scopes get markers, so the later
    residual_report() stays NOT_CLEAN for them.

    Generation semantics on the FAILURE path differ from the
    success path: the physical cleanup already failed, so a
    generation change is not a reason to refuse — it means MORE
    scopes to mark. The generation is re-read and compared only to
    note it (counts only, never ids or keys — the note lands in the
    marker detail, which residual_report() surfaces verbatim) and
    to make the re-collection observable: a changed generation
    appends "; generation changed after physical check: N new erase
    event(s)" (and/or "; purge_pending markers changed after
    physical check") to the detail written with the markers.

    Lock contention is retried, bounded: a "database is locked"
    failure rolls the attempt back and retries after a short sleep,
    until the budget is spent. Each attempt re-collects inside its
    own transaction, so a writer that commits between attempts is
    included by the next one. The per-attempt busy_timeout is
    shortened for the loop (saved and restored afterwards) so one
    attempt cannot consume the whole budget when the caller set a
    long busy_timeout.

    Returns (persisted, attempts): persisted is True iff the marker
    transaction committed; attempts is how many BEGIN IMMEDIATE
    attempts were made. When the budget is spent without a commit
    (a writer holding the write lock past it — a stuck transaction),
    persisted is False and the caller must say so in "error"
    explicitly ("marker could not be persisted ..."): returning
    cleaned=False with no marker and no such note would be exactly
    the P0 again (a later residual_report() would read clean). The
    caller then retries the purge after the writer releases; the
    next purge persists the marker (or cleans up). Non-lock errors
    propagate (a marker write failing for a non-transient reason
    means the DB is unusable for reporting anyway).

    Precondition: no transaction is open on this connection (the
    caller guarantees it — the had_txn path never calls this).
    A BEGIN IMMEDIATE failing for a non-lock reason propagates the
    same way."""
    try:
        orig_timeout = db.execute("PRAGMA busy_timeout").fetchone()[0]
    except Exception:  # noqa: BLE001 - best effort; a missing value just skips the restore
        orig_timeout = None
    deadline = time.monotonic() + _FAILURE_MARKER_RETRY_BUDGET_S
    attempts = 0
    last_error = None
    try:
        db.execute(
            f"PRAGMA busy_timeout={_FAILURE_MARKER_ATTEMPT_TIMEOUT_MS}")
        while True:
            attempts += 1
            try:
                with transaction(db, write=True):
                    cur_events, cur_markers = _purge_generation(
                        db, scope_id)
                    scopes = _purge_settlement_scopes(db, scope_id)
                    final_detail = detail
                    new_events = cur_events - gen_events
                    if new_events:
                        final_detail += (
                            "; generation changed after physical check: "
                            f"{len(new_events)} new erase event(s)")
                    if cur_markers != gen_markers:
                        final_detail += (
                            "; purge_pending markers changed after "
                            "physical check")
                    for s in scopes:
                        _set_purge_pending(db, s, final_detail)
                return True, attempts
            except Exception as e:  # noqa: BLE001 - lock errors retry; anything else propagates
                last_error = e
                if "locked" not in str(e).lower():
                    raise
                if time.monotonic() >= deadline:
                    break
                time.sleep(_FAILURE_MARKER_RETRY_SLEEP_S)
        return False, attempts
    finally:
        if orig_timeout is not None:
            try:
                db.execute(f"PRAGMA busy_timeout={int(orig_timeout)}")
            except Exception:  # noqa: BLE001 - best effort restore
                pass


def _erase_settlement_recheck(db, scope_id, record_id, key_row, needle_hashes):
    """Re-verify the file purge INSIDE an erase settlement
    transaction (P0-R18-01). Returns True iff the settlement may
    clear the marker.

    The pre-settlement _verify_purge runs before the settlement
    transaction starts, so a concurrent old-version write landing
    between the check and the settlement would otherwise defeat
    the verdict: the erase would report COMPLETE with the deleted
    verification key live again (reproduced in
    ~/workspace/r19-work/erase-settle-race.py, 8/8, on the repair
    re-run path). Running this check after BEGIN IMMEDIATE — which
    blocks other writers from committing — closes that window to
    the commit-to-return gap (the same residual as purge_residue;
    see §31). The check is the pre-settlement acceptance plus the
    row check: _verify_purge (key hex absent from the files +
    blind probe) when a key was captured, the probe alone when the
    key was already deleted, and in both branches the direct SQL
    signal — the record's key row must still be absent at
    settlement time (the probe is blind to six-digit/opaque values,
    so the row check is the backstop there). Any error running the
    recheck fails closed (False)."""
    try:
        if key_row is not None:
            if not _verify_purge(db, key_row[0], needle_hashes):
                return False
        elif _probe_files(db, needle_hashes):
            return False
        if _read_record_erase_key_row(db, scope_id, record_id) is not None:
            return False
        return True
    except Exception:  # noqa: BLE001 - a recheck that cannot run fails closed
        return False


def purge_residue(db, scope_id=None):
    """P0-R15-01: clean historical secure_delete=OFF residue from a
    live DB (the product cleanup entry; also exposed as
    `turnkeep purge-residue`).

    For databases whose erasures ran under a secure_delete=OFF
    connection (every RC before this fix, on a default-OFF SQLite
    build): a deleted verification key's bytes can still sit in the
    main DB file even though the key row is gone and the report says
    clean — opening the DB with secure_delete=ON does NOT by itself
    clear it (verified §1.5: ON + checkpoint-only clears it only
    when the WAL still holds VACUUM's compact pages; the no-VACUUM
    residue case needs ON + VACUUM + checkpoint). This entry runs
    exactly that remedy — ON + VACUUM + wal_checkpoint(TRUNCATE) —
    then re-probes the files. Idempotent and safe to run on a clean
    DB (VACUUM only rebuilds live data; no payload is lost) — run
    it once on any DB whose erasures may predate the fix. Scope is
    by provenance: the DB file itself records no OFF marker, since
    secure_delete is per-connection and not persisted, so this entry
    cannot detect which DBs were OFF-erased.

    P0-R16-01: physical cleanup succeeds ONLY when the checkpoint
    reports busy == 0 — a busy checkpoint (a concurrent read
    transaction holds a snapshot) is a FAILURE, whatever the probe
    says: the WAL could not be reset, so a deleted key's bytes may
    still be recoverable from the live DB file / WAL. The probe and
    the busy check are two independent requirements: the probe
    covers 4-digit PINs only (a 6-digit or opaque value is outside
    its range — a disclosed blind spot, see §29), so "the probe
    found nothing" can never compensate for a busy checkpoint.
    P0-R16-03: a failed cleanup PERSISTS a purge_pending marker
    (one row per scope, live-DB-only table) when the failure
    settlement commits: residual_report() reads it, so a later
    report returns CONTROLLED_NOT_CLEAN until a successful
    purge_residue re-check clears it. When the settlement cannot
    commit — the retry budget is spent with the write lock still
    held, or the caller already has a transaction open (had_txn) —
    no marker is persisted yet: the error names it ("marker could
    not be persisted ...") and the caller retries the purge after
    the writer releases (see §32). A repair re-run
    does NOT clear it. The marker is written by the failure
    settlement (see P0-R19-01: scope collection + marker write
    inside ONE write transaction, with the scope union +
    generation re-collected inside it and lock contention
    retried); a crash between observing the failure and the
    settlement transaction committing leaves no marker — that state
    is equivalent to "purge_residue never ran" and is a disclosed
    known window (the residue stays in the file, so the next run
    re-runs the full physical cleanup: residue within the probe's
    4-digit PIN range can be re-discovered by it, anything else is
    cleared by VACUUM and a busy==0 checkpoint, which do not depend
    on the probe).
    P2-R17-01: a global call (scope_id=None) collects the scopes to
    settle as the UNION of erase-event scopes and purge_pending
    scopes — a scope that only has a marker (no erase event, e.g.
    an unregistered --scope) is still covered, so a global success
    clears its marker; collecting from erase events alone stranded
    it forever. VACUUM + checkpoint + the file probe are file-wide,
    so "covered" means every scope in the union. An explicit scope
    call still settles only that scope.
    P0-R18-01: the check-to-settle race is closed by a settlement
    recheck (see _purge_settlement_recheck): the generation (erase
    events + purge_pending rows) is captured before VACUUM, and
    the settlement — final recheck + marker clear — runs inside
    ONE write transaction (BEGIN IMMEDIATE), so no other writer can
    commit between the final check and the marker clear. The
    recheck fails the purge (cleaned=False, markers kept or
    refreshed, report NOT_CLEAN) when the generation changed after
    the physical check, when a deleted verification key's row is
    live again for a terminal-COMPLETE record, when the probe
    finds recoverable residue, or when a resurrected key's bytes
    are in the live file / WAL. A resurrected row found by the
    recheck is deleted in the same settlement transaction: it is
    a row that by design must be absent (its record is
    terminal-COMPLETE), so deleting it restores the invariant —
    and it is what makes a bare purge_residue retry converge
    (the row is committed data; the writer exiting does not
    remove it, so flagging it without deleting it would fail
    every later retry too). Its bytes leave the file on the next
    VACUUM + busy==0 checkpoint, so this call still reports
    cleaned=False and the retry is the one that reports clean
    (see §31). The verdict is correct as of the settlement
    transaction's commit; a write landing after that commit is a
    NEW generation, covered by the next purge_residue re-check
    (see §31). The recheck does NOT use PRAGMA data_version:
    the purge's own commits (VACUUM + the marker settlement)
    also advance it, so the expected delta depends on path
    details and cannot distinguish our commits from concurrent
    ones without fragile exact accounting — the content +
    generation recheck verifies the property directly instead
    (see §31).
    P0-R19-01: the FAILURE branch settles the same way. The
    success-path recheck above does not run when the initial
    verdict is already cleaned=False — and the old failure branch
    collected the scope union and wrote the markers with no
    transaction and no generation re-check, so a scope whose erase
    events landed after the scope collection got no marker and a
    later residual_report() read clean (false-clean). The fix
    (see _settle_failure_markers): scope collection + marker
    write run inside ONE write transaction (BEGIN IMMEDIATE), and
    the scope union + generation are re-collected INSIDE that
    transaction — a writer that committed after the physical
    check is included, so its scopes get markers. Generation
    semantics on the failure path differ from the success path:
    the physical cleanup already failed, so a generation change
    means MORE scopes to mark, not a reason to refuse; the
    re-read generation is compared only to note it (counts only)
    in the marker detail. Lock contention is retried, bounded
    (10 s, matching busy_timeout): a "database is locked" failure
    rolls the attempt back and retries, each attempt re-collecting
    inside its own transaction. If the budget is spent without a
    commit — a writer holding the write lock past it, a stuck
    transaction — the purge reports the unpersisted marker
    explicitly in "error" ("marker could not be persisted ...")
    and returns cleaned=False; it never returns as if the marker
    had been written. The caller then retries the purge after the
    writer releases. If the caller already had a transaction open
    (had_txn), the marker joins the caller's transaction instead
    (see §29 / C-5): the scopes are re-collected at write time,
    best effort within the caller's snapshot, and the caller owns
    the commit. See §32.
    P1-R16-02: the probe answers booleans only — the return dict
    carries "residue_before" / "residue_after" (True = a recoverable
    low-entropy value was found), never a recovered value, so no
    erased PIN can leak into stdout, a log or an API response
    through this entry. The old "probe_before" / "probe_after"
    keys (which held the recovered PIN) are gone — see §29 for the
    contract change.

    Returns a dict with "cleaned" (bool), "secure_delete" (the
    read-back, 1), "checkpoint" (the (busy, log, checkpointed)
    triple or None), "residue_before" / "residue_after" (bools),
    and "error" (a VACUUM/checkpoint failure note, or None). On
    failure "cleaned" is False AND, when the failure settlement
    commits, a purge_pending marker is persisted for every
    affected scope, so the residual report stays NOT_CLEAN. If the
    settlement cannot commit (retry budget spent, or had_txn with
    the caller owning the commit), no marker is persisted yet and
    the error says so explicitly — the report stays NOT_CLEAN only
    after a retry persists the marker (see §32). The R15 claim
    that a failed cleanup keeps the residual report NOT_CLEAN was
    FALSE for this entry (the failure wrote no marker and the
    report still read clean — P0-R16-03); the marker is exactly
    what closes that gap."""
    db.execute("PRAGMA secure_delete=ON")
    row = db.execute("PRAGMA secure_delete").fetchone()
    if row is None or int(row[0]) != 1:
        raise TurnkeepError(
            "SECURE_DELETE_UNAVAILABLE",
            "secure_delete could not be enabled on this connection; "
            "refusing to clean residue")
    hashes = _all_stored_needle_hashes(db, scope_id)
    # P0-R18-01: capture the generation BEFORE the physical cleanup
    # — the erase-event ids and purge_pending rows this run covers.
    # Anything appearing after this point (a legacy OFF erase chain
    # landing new events, a concurrent explicit purge writing a new
    # marker, an old-version write re-inserting a deleted key row)
    # is NOT covered by the VACUUM + checkpoint + probe below and
    # must fail the purge (fail-closed) instead of being silently
    # settled on the stale cleaned=True. The generation comparison
    # happens inside the settlement transaction (see below).
    gen_events, gen_markers = _purge_generation(db, scope_id)
    residue_before = _probe_files(db, hashes)
    error = None
    checkpoint = None
    try:
        db.execute("VACUUM")
        crow = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        checkpoint = tuple(int(x) for x in crow) if crow is not None else None
    except Exception as e:  # noqa: BLE001 - a failed cleanup is reported, not fatal
        error = str(e)
    residue_after = _probe_files(db, hashes)
    # P0-R16-01: busy != 0 is a failure, whatever the probe says —
    # the checkpoint did not complete, so the WAL was not reset.
    # The note uses _run_maintenance's format, and is never silent.
    busy = checkpoint is not None and checkpoint[0] != 0
    if busy:
        busy_note = ("PRAGMA wal_checkpoint(TRUNCATE) busy: "
                     f"{checkpoint}")
        error = f"{error}; {busy_note}" if error else busy_note
    cleaned = (not residue_after and error is None
               and checkpoint is not None and checkpoint[0] == 0)
    # P0-R16-03: persist the failure (or clear on success) for
    # every affected scope, and commit immediately — the marker is
    # what makes a later residual_report() see the failure. A
    # scope_id of None covers every scope with erase hashes in the
    # file; an explicit scope covers only itself. The marker write
    # must never mask the cleanup outcome: if it fails, the entry
    # still reports cleaned=False (fail-closed) with the failure
    # noted in "error".
    # P2-R17-01: the global (scope_id=None) collection is the UNION
    # of erase-event scopes and purge_pending scopes (see
    # _purge_settlement_scopes). Collecting from erase events alone
    # strands the marker of a scope that has no erase event (e.g.
    # an unregistered --scope whose explicit cleanup failed): the
    # global run would report cleaned=True while
    # residual_report() for that scope still says
    # CONTROLLED_NOT_CLEAN. VACUUM + checkpoint + the file probe
    # are file-wide, so a global success covers every scope in the
    # union and may clear all their markers; an explicit scope call
    # still handles only that scope.
    # P0-R18-01: the settlement — final recheck + marker clear —
    # runs inside ONE write transaction (BEGIN IMMEDIATE), so no
    # other writer can commit between the final check and the
    # marker clear. The recheck (see _purge_settlement_recheck)
    # compares the generation captured before VACUUM with the
    # generation inside this transaction, checks for resurrected
    # key rows, and re-probes the files; any failure keeps or
    # refreshes the markers and reports cleaned=False.
    # P0-R19-01: the FAILURE branch settles the same way — scope
    # collection + marker write inside ONE write transaction
    # (BEGIN IMMEDIATE), with the scope union + generation
    # re-collected inside it and lock contention retried (see
    # _settle_failure_markers). A failure verdict is fail-closed
    # as a RETURN value (cleaned=False) but that says nothing about
    # the PERSISTED state: without the transaction + re-collection
    # a concurrent scope gets no marker and a later
    # residual_report() reads clean — which is the P0. The marker
    # write below commits immediately — unless the connection
    # already had a transaction open when VACUUM failed (VACUUM
    # cannot run inside one): then the marker joins the caller's
    # transaction and the caller owns the commit; committing here
    # would commit the caller's uncommitted work.
    had_txn = db.in_transaction
    try:
        if cleaned and not had_txn:
            with transaction(db, write=True):
                settle_scopes = _purge_settlement_scopes(db, scope_id)
                notes, resurrected = _purge_settlement_recheck(
                    db, scope_id, gen_events, gen_markers)
                # P0-R18-01: delete resurrected rows in this same
                # transaction (they are rows that by design must be
                # absent — their record is terminal-COMPLETE — so
                # deleting them restores the invariant and is what
                # makes a bare purge_residue retry converge; their
                # bytes leave the file on the next VACUUM + busy==0
                # checkpoint, so this call still reports
                # cleaned=False — see §31).
                for (_sid, _rid, _hexv) in resurrected:
                    db.execute(
                        "DELETE FROM record_erase_keys WHERE "
                        "scope_id = ? AND record_id = ?", (_sid, _rid))
                if notes:
                    cleaned = False
                    error = "; ".join([error, *notes]) if error else "; ".join(notes)
                    detail = _purge_failure_detail(
                        checkpoint, residue_before, residue_after, error)
                    for s in settle_scopes:
                        _set_purge_pending(db, s, detail)
                else:
                    for s in settle_scopes:
                        _clear_purge_pending(db, s)
        else:
            if cleaned:
                # Defensive: VACUUM cannot have succeeded inside a
                # caller transaction, so cleaned True here is
                # untrustworthy — fail closed rather than settle on
                # it. (Unreachable in practice: VACUUM fails inside
                # a transaction, which already makes cleaned False.)
                # C-2: this falls into the SAME failure settlement
                # below (P0-R19-01), so the defensive path gets the
                # transaction + re-collection + retry too.
                cleaned = False
                error = ((f"{error}; ") if error else "") + (
                    "purge_residue called inside a caller transaction; "
                    "refusing to report success")
            detail = _purge_failure_detail(
                checkpoint, residue_before, residue_after, error)
            if had_txn:
                # C-5: the marker joins the caller's transaction
                # (the caller owns the commit); the scopes are
                # re-collected at write time, best effort within the
                # caller's snapshot. A write failure here cannot be
                # retried without touching the caller's transaction,
                # so it falls through to the outer handler (error
                # string, cleaned=False) — the disclosed C-5 window.
                scopes = _purge_settlement_scopes(db, scope_id)
                for s in scopes:
                    _set_purge_pending(db, s, detail)
            else:
                # P0-R19-01: the failure settlement owns its
                # transaction — scope collection + marker write inside
                # ONE BEGIN IMMEDIATE, with bounded retry on lock
                # contention (see _settle_failure_markers). The
                # collection call itself throwing (C-3) is inside the
                # same protected path: a lock error retries, any other
                # error propagates to the outer handler.
                persisted, attempts = _settle_failure_markers(
                    db, scope_id, detail, gen_events, gen_markers)
                if attempts > 1:
                    error = ((f"{error}; ") if error else "") + (
                        f"purge marker write retried ({attempts} attempts)")
                if not persisted:
                    error = ((f"{error}; ") if error else "") + (
                        "purge marker write failed: database is locked "
                        f"(marker could not be persisted after {attempts} "
                        "attempts)")
    except Exception as e:  # noqa: BLE001 - a marker failure must not lie about the cleanup
        error = f"{error}; purge marker write failed: {e}" if error else (
            f"purge marker write failed: {e}")
        cleaned = False
    return {
        "cleaned": cleaned,
        "secure_delete": 1,
        "checkpoint": checkpoint,
        "residue_before": residue_before,
        "residue_after": residue_after,
        "error": error,
    }


def _carried_task_text_entries(db, scope_id, record_id):
    """Task-text residual entries a repair must carry forward (P0-R11-01).

    The repair's needles are receipt-derived, and the receipts are
    already tombstoned, so the repair's own scan cannot see a task-text
    copy — but task text is never scrubbed (keep + honest PARTIAL),
    so a task-text residual flagged by an earlier erase still holds
    unless the row itself is gone. Returns the stored remaining-entry
    dicts (table, rowid, task_text, task_locations, record attribution
    as published) from every earlier erase event covering this record
    whose location still exists in the live DB, deduplicated by
    (table, rowid). Fail-closed: a row that still exists is assumed
    to still hold the payload (the repair has no needles to re-verify
    with, and it cannot clear the text); a row that is gone takes its
    task text with it and is dropped. The entries are location names
    only, never the task text, so carrying them publishes no payload
    material."""
    carried, seen = [], set()
    rows = db.execute(
        "SELECT affected_ids_json, metadata_json FROM events "
        "WHERE scope_id=? AND event_type='erase' ORDER BY scope_epoch",
        (scope_id,)).fetchall()
    for affected_json, meta_json in rows:
        try:
            affected = json.loads(affected_json) if affected_json else []
            meta = json.loads(meta_json) if meta_json else {}
        except ValueError:
            continue
        if record_id not in affected:
            continue
        for entry in meta.get("remaining", []) or []:
            if not isinstance(entry, dict) or not entry.get("task_text"):
                continue
            table, rowid = entry.get("table"), entry.get("rowid")
            if table not in ("runs", "idempotency") or rowid is None:
                continue
            if (table, rowid) in seen:
                continue
            row = db.execute(
                f"SELECT 1 FROM {table} WHERE rowid=? AND scope_id=?",
                (rowid, scope_id)).fetchone()
            if row is None:
                continue
            seen.add((table, rowid))
            carried.append(dict(entry))
    return carried


def _describe_remaining_with_carried(db, scope_id, remaining, needles,
                                     needle_hashes, carried, record_id=None):
    """_describe_remaining plus the carried task-text entries (P0-R11-01).

    The repair's needles are receipt-derived and empty, so
    _describe_remaining cannot re-derive a task_text flag from them —
    a carried row would come back as a bare {table, rowid} entry and
    the resolver would judge it by the (clean) receipt dump alone.
    Each carried entry is merged in: when _describe_remaining already
    produced an entry for the same (table, rowid) the flag and
    locations are added to it; otherwise the stored entry (location
    names + attribution as published by the earlier erase) is
    appended as-is. Entries are location names only, never the task
    text, so no payload material is published. record_id names the
    record being erased, for the hash-form checks (P0-R12-01)."""
    described = _describe_remaining(db, scope_id, remaining, needles, needle_hashes,
                                    record_id=record_id)
    by_location = {(e.get("table"), e.get("rowid")): e
                   for e in described if isinstance(e, dict)}
    for entry in carried:
        key = (entry.get("table"), entry.get("rowid"))
        existing = by_location.get(key)
        if existing is None:
            described.append(dict(entry))
            by_location[key] = described[-1]
        else:
            # The carried entry's attribution was computed by the
            # earlier erase with live needles; the described entry
            # (empty needles) has none — keep the carried one.
            existing["task_text"] = True
            existing["task_locations"] = entry.get("task_locations", [])
            for field in ("record_id", "key", "key_redacted"):
                if existing.get(field) is None and entry.get(field) is not None:
                    existing[field] = entry[field]
    return described


def _flag_label_residuals(described, own_rowid, record_id, kind_hit, status_hit):
    """Flag kind / status-label residuals on a described remaining list
    (P1-R12-02).

    described is the _describe_remaining output for this erase. When
    kind_hit, the entry for the record's own memories rowid gains
    kind_label + kind_locations (the record's own kind is a decoded
    form of the payload; kind is structural and never changes, so the
    entry never resolves in the live DB — see
    _unresolved_controlled_residuals — and the remedy is export, where
    the tombstone row is excluded). When status_hit, a synthetic
    events entry naming the record is appended (the erase_status label
    is a decoded form of the payload; events entries never resolve,
    since history is append-only). Location names only, never the
    label value: the label is a fixed vocabulary word, but naming
    which one matched would name the payload's decoded form. The
    list is mutated in place and returned."""
    if kind_hit:
        for entry in described:
            if (isinstance(entry, dict) and entry.get("table") == "memories"
                    and entry.get("rowid") == own_rowid):
                entry["kind_label"] = True
                entry["kind_locations"] = ["memories.kind"]
                break
        else:
            # The own row was not described (it was already clean of
            # every other needle) — the kind hit is the only reason it
            # is named, so append a bare entry for it.
            described.append({"table": "memories", "rowid": own_rowid,
                              "record_id": record_id,
                              "kind_label": True,
                              "kind_locations": ["memories.kind"]})
    if status_hit:
        described.append({"table": "events", "record_id": record_id,
                          "status_label": True,
                          "status_locations": ["events.erase_status"]})
    return described


def _try_repair_erased(db, scope_id, record_id, expected_revision, reason, *,
                       actor_binding="local"):
    """Repair path for an already-erased record (P0-R3-01).

    A PARTIAL erase used to be a dead end: a second erase_record()
    raised INVALID_INPUT ("record is already erased"), so a controlled
    residual (e.g. a receipt the old scrub could not recognise) could
    never be cleared. This re-runs the scrub + scan for that record
    WITHOUT resurrecting it and WITHOUT bumping the revision a second
    time: the memories row keeps its tombstone at its current revision
    (only its timestamp columns may change, when an illegal timestamp
    field is cleared — P0-R7-01c), only receipts owned by the record
    are scrubbed, and a fresh erase event records the repair outcome
    (metadata "repair": True) so residual_report() can see the
    residual resolve.
    Needles are reconstructed from the receipt copies themselves (see
    _needles_from_receipts) because the payload is already tombstoned;
    the reason is gated BEFORE anything is published: a repair erase
    accepts no free text (P0-R5-01, see _check_repair_reason) — only
    the REPAIR_REASONS labels, hash-checked against the needle hashes
    the FIRST erase persisted, so a label that is itself the erased
    value is rejected and no glued secret ("rotation:<secret>",
    "x<secret>y") can enter events. The live-needle check
    (_check_reason_against_needles, P0-R3-02) still runs as a second
    layer for the receipt-derived needles. Returns the erase result
    dict, or None when the record is not already erased (the caller
    then runs the live path).
    A record erased by a concurrent writer between this check and the
    caller's live path still raises INVALID_INPUT there; retrying then
    takes this path.
    """
    # P0-R15-01: the secure_delete gate runs BEFORE the transaction —
    # a repair must not delete the key and then discover the
    # connection cannot purge it.
    _require_secure_delete(db)
    with transaction(db, write=True):
        ensure_scope(db, scope_id)
        row = row_or_404(db, scope_id, record_id)
        check_revision(row, expected_revision)
        if row["state"] != "erased":
            return None
        # P0-R5-01: the allowlist gate runs before any needle check and
        # before anything is published — but after check_revision, so a
        # stale revision still reports VERSION_CONFLICT first.
        _check_repair_reason(db, scope_id, record_id, reason)
        needles = _needles_from_receipts(db, scope_id, record_id)
        _check_reason_against_needles(reason, needles)
        # P0-R7-01c: clear illegal timestamp fields on the tombstoned
        # row (a direct write after the first erase can have planted
        # one). No revision bump, no resurrection — the row stays
        # tombstoned; only the timestamp columns change. valid_until
        # is nulled; created_at/updated_at are NOT NULL, so an illegal
        # value there is replaced by the repair time. The cleared
        # fields are recorded in the repair event's metadata.
        cleared = _illegal_timestamp_fields(row, needles)
        if cleared:
            now = _now()
            db.execute(
                "UPDATE memories SET valid_until=?, created_at=?, updated_at=? "
                "WHERE scope_id=? AND id=?",
                (None if "valid_until" in cleared else row["valid_until"],
                 now if "created_at" in cleared else row["created_at"],
                 now if "updated_at" in cleared else row["updated_at"],
                 scope_id, record_id))
        scrubbed = []
        for needle in needles:
            scrubbed.extend(_scrub_receipts(db, scope_id, record_id, needle))
        # P0-R8-01b / P1: the live needles are gone when the receipts
        # are already scrubbed, so the identifier cleanup runs on the
        # needle hashes the FIRST erase persisted, not on live needles.
        # A receipt dump's key / source-ref id that IS the payload is
        # replaced by its HMAC; the record's own source links whose id
        # IS the payload are deleted (orphaned sources rows go too).
        stored_hashes = _stored_needle_hashes(db, scope_id, record_id)
        scrubbed_ids = _scrub_receipt_identifiers(db, scope_id, record_id, stored_hashes)
        cleared_sources = _clear_identifier_sources(db, scope_id, record_id, stored_hashes)
        # P0-R10-01 (repair path): same digest neutralization as the
        # first erase, on the stored hashes — a second erase of an
        # already-erased record must not leave a recoverable digest
        # behind either. Counts go into the event metadata only
        # (the repair return keeps its existing shape).
        neutralized_idem, deleted_idem = _neutralize_idempotency_digests(
            db, scope_id, record_id, stored_hashes)
        neutralized_src = _neutralize_payload_source_digests(
            db, scope_id, stored_hashes, record_id)
        remaining = _scan_for_remaining(db, scope_id, record_id, needles)
        # P0-R11-01: carry forward task-text residuals from earlier
        # erases of this record. The repair's needles are receipt-derived
        # and the receipts are already tombstoned, so the scan above
        # cannot see a task-text copy — but task text is never scrubbed
        # (keep + honest PARTIAL), so a flagged entry still holds unless
        # its row is gone. Without the carry-forward the repair would
        # report COMPLETE and its event would hide the residual the
        # first erase correctly reported PARTIAL. The carried rowids
        # join the raw remaining (so the return and the status see
        # them) and their stored entries join the described remaining
        # below (so the flag + locations survive _describe_remaining,
        # which cannot re-derive them from empty needles).
        carried = _carried_task_text_entries(db, scope_id, record_id)
        for entry in carried:
            remaining.setdefault(entry["table"], []).append(entry["rowid"])
        for table in remaining:
            remaining[table] = sorted(set(remaining[table]))
        # P1-R12-02 (kind, repair path): the record's own kind can be
        # a decoded form of the payload. The live needles are
        # receipt-derived and empty once the receipts are scrubbed, so
        # the stored hashes are checked too (see
        # _label_matches_stored_hashes). A hit names the record's own
        # tombstone row in remaining (so the return and the status see
        # it) and is flagged on the described entry below — kind is
        # structural and never resolves in the live DB.
        own_rowid = db.execute(
            "SELECT rowid FROM memories WHERE scope_id=? AND id=?",
            (scope_id, record_id)).fetchone()[0]
        kind_hit = (_label_is_payload_copy(row["kind"], needles)
                    or _label_matches_stored_hashes(db, scope_id, record_id, row["kind"]))
        if kind_hit:
            remaining.setdefault("memories", []).append(own_rowid)
            remaining["memories"] = sorted(set(remaining["memories"]))
        scan_status = "COMPLETE" if not remaining else "PARTIAL"
        # P1-R12-02 (erase_status, repair path): the status label the
        # repair is about to write is itself a decoded form of the
        # payload, so the repair cannot honestly report COMPLETE —
        # downgrade and record the label as a residual (it never
        # resolves: events are append-only).
        status_hit = (_label_is_payload_copy(scan_status, needles)
                      or _label_matches_stored_hashes(db, scope_id, record_id, scan_status))
        if status_hit:
            scan_status = "PARTIAL"
        ev = _id()
        event_key, key_redacted = _safe_erase_event_key(
            db, scope_id, record_id, row["key"], needles)
        # P0-R11-02: the repair event's hashes + export map (see
        # _erase_event_hash_fields). The blocked repair-reason labels
        # go to the live-DB-only erase_flags table, NOT into the event
        # (see _store_blocked_repair_reasons): a blocked label is a
        # fixed vocabulary word, and persisting it in an event would
        # write the payload itself into history when the payload is
        # literally that label. _store_blocked_repair_reasons unions
        # with the first erase's already-stored labels, so no label
        # the first erase blocked is ever unblocked by a repair.
        _store_blocked_repair_reasons(
            db, scope_id, record_id, _blocked_repair_reasons(needles))
        described = _describe_remaining_with_carried(
            db, scope_id, remaining, needles, stored_hashes, carried,
            record_id=record_id)
        _flag_label_residuals(described, own_rowid, record_id, kind_hit, status_hit)
        metadata = {"reason": reason, "scrubbed": scrubbed,
                    "erase_status": scan_status, "erased": True,
                    "remaining": described,
                    **_erase_event_hash_fields(
                        db, scope_id, needles, scan_status,
                        hashes=_needle_hashes(db, scope_id, needles, record_id),
                        stored_hashes=stored_hashes, record_id=record_id),
                    "cleared_illegal_timestamps": cleared,
                    "scrubbed_identifiers": scrubbed_ids,
                    "cleared_source_ids": cleared_sources,
                    "neutralized_idempotency": neutralized_idem,
                    "deleted_idempotency": deleted_idem,
                    "neutralized_source_digests": neutralized_src,
                    "repair": True}
        if event_key is not None:
            metadata["key"] = event_key
        if key_redacted:
            metadata["key_redacted"] = True
        publish_event(db, scope_id, "erase", actor_binding, {record_id: None},
                      metadata=metadata, event_id=ev)
        # P0-R12-01 / P0-R13-01 / P0-R14-01: terminal COMPLETE retires
        # the record's verification key (see _delete_record_erase_key)
        # — after the event is published and every hash verification
        # this repair needed, inside the same transaction. The key row
        # is captured first: if the purge checkpoint below returns
        # busy, the key is restored (its bytes may still be in the
        # live file / WAL) and a maintenance_pending marker is
        # persisted, so the erase cannot terminate COMPLETE while the
        # key is recoverable. The marker is written FIRST, in this
        # same transaction, BEFORE the key is deleted (P0-R14-01): a
        # crash after this transaction commits but before the
        # checkpoint runs must still leave a persisted marker — the
        # old order (commit, then checkpoint, then a second
        # transaction writing the marker) had a window with no
        # persisted protection at all. The write is UNCONDITIONAL
        # for every terminal COMPLETE erase (see
        # _set_maintenance_pending): it also covers the old payload
        # pages the checkpoint has not yet purged, and the first-erase
        # path's payload bytes sit in the WAL from the remember frame
        # even though its key never hit disk. The marker is cleared
        # only after the checkpoint succeeds (settlement below); a
        # marker that survives a crash between checkpoint success and
        # the clear means "purge unconfirmed", never "file dirty" —
        # a later repair re-run re-runs the checkpoint and clears it.
        key_row = (_read_record_erase_key_row(db, scope_id, record_id)
                   if scan_status == "COMPLETE" else None)
        if scan_status == "COMPLETE":
            _set_maintenance_pending(
                db, scope_id, record_id,
                "purge pending: WAL checkpoint not yet run (P0-R14-01)")
            _delete_record_erase_key(db, scope_id, record_id)
        remaining_out, scrubbed_out = dict(remaining), list(scrubbed)
        revision, key = row["revision"], row["key"]
    maintenance = _run_maintenance(db)
    purge_failed = _purge_failed(maintenance)
    # P0-R15-01: file-level purge ACCEPTANCE, after _run_maintenance
    # and before the settlement transaction clears the marker (see
    # _verify_purge). Only when the checkpoint claims success: a busy
    # checkpoint already fails the purge, and a PARTIAL scan keeps the
    # key anyway. When this erase deleted a key, the check is the
    # direct key-hex test plus the blind probe; when the key was
    # already deleted by an earlier (crashed) erase, there is no key
    # hex to check directly — but the blind probe over the historical
    # hashes still catches OFF residue from that earlier erase (the
    # upgrade path for a pre-fix database whose OFF erase crashed
    # after the key-deletion commit). Without the probe-only branch
    # the marker would clear on the repair re-run while the old
    # key's bytes linger. On failure the note joins the maintenance
    # list and purge_failed folds it into the existing settlement
    # below — the key is restored when one was captured, the marker
    # is refreshed, the erase returns PARTIAL.
    if scan_status == "COMPLETE" and not purge_failed:
        if key_row is not None:
            purge_ok = _verify_purge(db, key_row[0], stored_hashes)
        else:
            purge_ok = not _probe_files(db, stored_hashes)
        if not purge_ok:
            maintenance.append(
                "file purge verification failed: the deleted "
                "verification key is still recoverable from the live "
                "DB file / WAL (P0-R15-01)")
            purge_failed = True
    maintenance_pending = False
    if scan_status == "COMPLETE":
        # P0-R13-01 / P0-R14-01: the purge verdict is settled here,
        # after the checkpoint. The marker was already written in the
        # erase transaction (P0-R14-01), so a crash before this point
        # still leaves it standing. On a busy checkpoint the key is
        # restored and the marker is REFRESHED with the checkpoint
        # failure (PARTIAL — retry via a later repair re-run); on
        # success the marker is cleared. A PARTIAL scan keeps the key
        # anyway (the repair path still needs it), so no marker is
        # written for it.
        with transaction(db, write=True):
            # P0-R18-01: re-verify INSIDE the settlement transaction
            # (BEGIN IMMEDIATE blocks other writers from committing
            # between this check and the marker decision): a
            # concurrent old-version write that re-inserted the
            # deleted key row after the pre-settlement _verify_purge
            # must turn this repair PARTIAL instead of reporting
            # COMPLETE with the key live (reproduced in
            # ~/workspace/r19-work/erase-settle-race.py, 8/8).
            if not purge_failed and not _erase_settlement_recheck(
                    db, scope_id, record_id, key_row, stored_hashes):
                maintenance.append(
                    "file purge re-verification failed: the deleted "
                    "verification key is still recoverable from the "
                    "live DB file / WAL, or its key row is live again "
                    "(P0-R18-01)")
                purge_failed = True
            if purge_failed:
                if key_row is not None:
                    _restore_record_erase_key(
                        db, scope_id, record_id, key_row[0], key_row[1])
                _set_maintenance_pending(
                    db, scope_id, record_id, "; ".join(maintenance))
                maintenance_pending = True
            else:
                _clear_maintenance_pending(db, scope_id, record_id)
    # The return status follows scan_status (not just the raw
    # remaining): a status-label downgrade (P1-R12-02) leaves
    # remaining empty but the repair is still PARTIAL, since the label
    # it just wrote is a copy. (The kind hit does land in remaining,
    # so scan_status already covers it.) A purge failure also makes
    # it PARTIAL: the key may still be recoverable from the live file.
    status = "COMPLETE" if scan_status == "COMPLETE" and not purge_failed else "PARTIAL"
    # P1-R10-03: the return dict must not re-materialise the payload
    # (see _erase_return_key) — the CLI prints it to stdout.
    result = {"status": status, "record_id": record_id,
              "state": "erased", "revision": revision, "erased_at": _now(),
              "scrubbed_receipts": scrubbed_out, "remaining": remaining_out,
              "maintenance": maintenance, "maintenance_pending": maintenance_pending,
              "erased": True, "repair": True,
              "cleared_illegal_timestamps": cleared}
    result.update(_erase_return_key(event_key, key_redacted))
    return result


def erase_record(db, scope_id, record_id, expected_revision, reason, *, actor_binding="local"):
    scope_id = _text(scope_id, "scope")
    reason = _text(reason, "reason")
    # P0-R15-01: the secure_delete gate runs BEFORE the repair check
    # and before the transaction — an erase must not delete the key
    # and then discover the connection cannot purge it. (The repair
    # path checks again at its own entry; the double check is cheap
    # defense-in-depth for direct _try_repair_erased callers.)
    _require_secure_delete(db)
    # P0-R3-01 repair: a second erase of an already-erased record
    # re-runs the scrub + scan for that record instead of raising
    # (see _try_repair_erased). It returns None for a live record.
    repaired = _try_repair_erased(db, scope_id, record_id, expected_revision,
                                  reason, actor_binding=actor_binding)
    if repaired is not None:
        return repaired
    with transaction(db, write=True):
        ensure_scope(db, scope_id)
        row = row_or_404(db, scope_id, record_id)
        check_revision(row, expected_revision)
        if row["state"] == "erased":
            # Only reachable when a concurrent writer erased the record
            # between the repair check above and this transaction; the
            # repair path owns already-erased records, so retrying this
            # call takes it.
            raise TurnkeepError("INVALID_INPUT", "record is already erased")
        payload = row["value_json"]
        # The payload as stored (JSON text) plus every string leaf of the
        # parsed value, recursively (dicts/lists, all depths): a secret
        # nested inside a structured value must be found even when a
        # derived copy stores it in a different serialisation.
        needles = _needles_for_payload(payload)
        # P0-R6-01: the allowlist gate runs FIRST, before the needle
        # check and before anything is published — both the step-1
        # PARTIAL refusal below and the step-3b COMPLETE event copy the
        # reason verbatim into the append-only event log, so only a
        # controlled label may ever reach them. The needle check then
        # runs as a second layer for the surviving label (it catches a
        # label that is itself the payload).
        _check_controlled_reason(reason, ERASE_REASONS, "first-erase")
        _check_reason_against_needles(reason, needles)
        # 1. Append-only history must not contain the payload. The
        # scan is value-aware (P0-R7-01c): a needle occurrence inside
        # a legal timestamp held under a timestamp field name in event
        # metadata is not a copy and does not refuse the erase.
        for table in APPEND_ONLY:
            for needle in needles:
                hits = _scan_payload_value_aware(db, needle, scope_id)
                if table in hits:
                    # Persist the refused outcome as an erase event (hashes
                    # only, never the payload): without it,
                    # residual_report() could never see that a live,
                    # controlled table still holds this payload.
                    # P0-R8-01a: the key is vetted before publishing —
                    # a key that IS the payload must not be written raw
                    # into the append-only event log.
                    ev = _id()
                    event_key, key_redacted = _safe_erase_event_key(
                        db, scope_id, record_id, row["key"], needles)
                    remaining_entry = {"table": table, "record_id": record_id}
                    # P0-R10-02a/b: name the event holding the
                    # payload (its id) — the entry names the erased
                    # record, but the payload sits in another event's
                    # affected ids / actor binding, and the event it
                    # belongs to is not necessarily the erased
                    # record's own.
                    if table == "events" and hits[table]:
                        ev_row = db.execute(
                            "SELECT id FROM events WHERE rowid=? AND scope_id=?",
                            (hits[table][0], scope_id)).fetchone()
                        if ev_row is not None:
                            remaining_entry["event_id"] = ev_row[0]
                    if event_key is not None:
                        remaining_entry["key"] = event_key
                    if key_redacted:
                        remaining_entry["key_redacted"] = True
                    metadata = {"reason": reason,
                                "erase_status": "PARTIAL", "erased": False,
                                "append_only_table": table,
                                "remaining": [remaining_entry],
                                "needle_hashes": _needle_hashes(db, scope_id, needles, record_id)}
                    if event_key is not None:
                        metadata["key"] = event_key
                    if key_redacted:
                        metadata["key_redacted"] = True
                    publish_event(db, scope_id, "erase", actor_binding,
                                  {record_id: None},
                                  metadata=metadata,
                                  event_id=ev)
                    # P1-R10-03: the return dict must not
                    # re-materialise the payload — the CLI prints it
                    # to stdout. The key is omitted (and flagged)
                    # exactly when it is payload material, by the
                    # same rule as the event metadata above.
                    result = {"status": "PARTIAL", "record_id": record_id,
                              "reason": f"payload also present in append-only table {table}; "
                                        "history is never rewritten — export to a fresh DB "
                                        "and delete this one instead",
                              "erased": False}
                    result.update(_erase_return_key(event_key, key_redacted))
                    return result
        # 2. Tombstone the record itself. P1-g: ONE event id for the whole
        # erase; the tombstone UPDATE and the descendant taint cite it.
        # P0-R7-01c: the tombstone UPDATE also clears the record's
        # illegal timestamp fields — a pre-R6 import or direct write
        # can have planted free text (or an encoding) in valid_until /
        # created_at / updated_at, and the tombstoned row would
        # otherwise keep the recoverable text (and export it) while
        # the erase reported COMPLETE. valid_until is nulled;
        # created_at/updated_at are NOT NULL, so an illegal value
        # there is replaced by the erase time. A legal timestamp is
        # never touched — it is legitimate expiry/audit text — and a
        # field that merely contains a short needle as a substring is
        # kept for the same reason (see _illegal_timestamp_fields).
        # The cleared fields are recorded in the erase event's
        # metadata (field names only, never the values).
        cleared = _illegal_timestamp_fields(row, needles)
        ev = _id()
        now = _now()
        db.execute("UPDATE memories SET value_json=?, state='erased', revision=revision+1, "
                   "updated_at=?, cause_event_id=?, valid_until=?, created_at=? "
                   "WHERE scope_id=? AND id=?",
                   (_json(TOMBSTONE), now, ev,
                    None if "valid_until" in cleared else row["valid_until"],
                    now if "created_at" in cleared else row["created_at"],
                    scope_id, record_id))
        # 2b. Taint descendants in the same transaction (CORE_CONTRACT
        # §41/§47): a child of an erased record must not stay usable.
        # changes carries the erased record plus every tainted descendant
        # so the erase event's affected ids include them all.
        changes = {record_id: None}
        taint_descendants(db, scope_id, [record_id], changes, cause_event_id=ev)
        # 3. Scrub derived receipts.
        scrubbed = []
        for needle in needles:
            scrubbed.extend(_scrub_receipts(db, scope_id, record_id, needle))
        # P0-R8-01b / P1: a receipt dump's key / source-ref id that IS
        # the payload is replaced by its HMAC, and the record's own
        # source links whose id IS the payload are deleted (orphaned
        # sources rows go too). Identification is by hash, on the live
        # needles — a random id that merely contains a short needle
        # substring is never touched (whole-value rule, P2-R8-03).
        live_hashes = _needle_hashes(db, scope_id, needles, record_id)
        scrubbed_ids = _scrub_receipt_identifiers(db, scope_id, record_id, live_hashes)
        cleared_sources = _clear_identifier_sources(db, scope_id, record_id, live_hashes)
        # P0-R10-01: neutralize the request digests of every
        # idempotency row this record's operations left behind
        # (marker, not delete — see _neutralize_idempotency_digests),
        # and the content digests of sources rows whose id IS the
        # payload. Both run BEFORE the 3b scan, so a digest that
        # could still recover the payload is gone by the time the
        # scan certifies the tables clean — and digests are not
        # scanned (they never contain their input as a substring).
        neutralized_idem, deleted_idem = _neutralize_idempotency_digests(
            db, scope_id, record_id, live_hashes)
        neutralized_src = _neutralize_payload_source_digests(
            db, scope_id, live_hashes, record_id)
        # 3b. Verify NOW, inside the transaction and before VACUUM (which
        # may reassign rowids): scan every text column for the needles and
        # resolve hits to stable record ids/keys while rowids are still
        # meaningful. The outcome is persisted in the erase event's
        # metadata (status + remaining + needle HASHES only) so
        # residual_report() can see a controlled residual after the
        # payload itself is tombstoned.
        remaining = _scan_for_remaining(db, scope_id, record_id, needles)
        # P1-R12-02 (kind): the record's own kind is a controlled label
        # that can BE the payload's decoded form (the payload is an
        # encoding of the kind, e.g. Base64("fact")). _scan_for_remaining
        # excludes the record's own tombstone rowid from remaining, so
        # the own kind is checked here directly: kind is a structural
        # attribute that cannot be rewritten at erase time, so a hit
        # names the record's own tombstone row in remaining (so the
        # return and the status see it) and is flagged on the described
        # entry below — it never resolves in the live DB, like
        # runs.task; the remedy is export, where the tombstone row is
        # excluded. The other records' kinds are NOT scanned (they are
        # those records' own structural labels; flagging all of them
        # would make any adversarial payload un-erasable while live
        # "fact" records exist, and export cannot remove a live
        # record's kind — see docs/KNOWN_LIMITATIONS.md §25).
        own_rowid = db.execute(
            "SELECT rowid FROM memories WHERE scope_id=? AND id=?",
            (scope_id, record_id)).fetchone()[0]
        kind_hit = _label_is_payload_copy(row["kind"], needles)
        if kind_hit:
            remaining.setdefault("memories", []).append(own_rowid)
            remaining["memories"] = sorted(set(remaining["memories"]))
        scan_status = "COMPLETE" if not remaining else "PARTIAL"
        # P1-R12-02 (erase_status): the status label the erase is about
        # to write is itself a decoded form of the payload, so the
        # erase cannot honestly report COMPLETE — downgrade and record
        # the label as a residual (it never resolves: events are
        # append-only). The fresh-DB scan alone cannot catch it: the
        # event holding the label is published by this erase, so the
        # label is checked here, before the event is written.
        status_hit = _label_is_payload_copy(scan_status, needles)
        if status_hit:
            scan_status = "PARTIAL"
        event_key, key_redacted = _safe_erase_event_key(
            db, scope_id, record_id, row["key"], needles)
        # P0-R11-02: the blocked repair-reason labels go to the
        # live-DB-only erase_flags table, not into the event (see
        # _store_blocked_repair_reasons) — a blocked label is a fixed
        # vocabulary word, and persisting it in an event would write
        # the payload itself into history when the payload is literally
        # that label.
        _store_blocked_repair_reasons(
            db, scope_id, record_id, _blocked_repair_reasons(needles))
        described = _describe_remaining(
            db, scope_id, remaining, needles, live_hashes, record_id=record_id)
        _flag_label_residuals(described, own_rowid, record_id, kind_hit, status_hit)
        metadata = {"reason": reason, "scrubbed": scrubbed,
                    "erase_status": scan_status, "erased": True,
                    "remaining": described,
                    **_erase_event_hash_fields(
                        db, scope_id, needles, scan_status, hashes=live_hashes,
                        record_id=record_id),
                    "cleared_illegal_timestamps": cleared,
                    "scrubbed_identifiers": scrubbed_ids,
                    "cleared_source_ids": cleared_sources,
                    "neutralized_idempotency": neutralized_idem,
                    "deleted_idempotency": deleted_idem,
                    "neutralized_source_digests": neutralized_src}
        if event_key is not None:
            metadata["key"] = event_key
        if key_redacted:
            metadata["key_redacted"] = True
        publish_event(db, scope_id, "erase", actor_binding, changes,
                      metadata=metadata,
                      event_id=ev)
        # P0-R12-01 / P0-R13-01 / P0-R14-01: terminal COMPLETE retires
        # the record's verification key (see _delete_record_erase_key)
        # — after the event is published and every hash verification
        # this erase needed, inside the same transaction. The key row
        # is captured first: if the purge checkpoint below returns
        # busy, the key is restored (its bytes may still be in the
        # live file / WAL) and a maintenance_pending marker is
        # persisted, so the erase cannot terminate COMPLETE while the
        # key is recoverable. The marker is written FIRST, in this
        # same transaction, BEFORE the key is deleted (P0-R14-01): a
        # crash after this transaction commits but before the
        # checkpoint runs must still leave a persisted marker — the
        # old order (commit, then checkpoint, then a second
        # transaction writing the marker) had a window with no
        # persisted protection at all. The write is UNCONDITIONAL
        # for every terminal COMPLETE erase (see
        # _set_maintenance_pending): it also covers the old payload
        # pages the checkpoint has not yet purged — on this path the
        # payload bytes sit in the WAL from the remember frame even
        # though the key itself was created and deleted inside this
        # transaction and never hit disk. The marker is cleared only
        # after the checkpoint succeeds (settlement below); a marker
        # that survives a crash between checkpoint success and the
        # clear means "purge unconfirmed", never "file dirty" — a
        # later repair re-run re-runs the checkpoint and clears it.
        key_row = (_read_record_erase_key_row(db, scope_id, record_id)
                   if scan_status == "COMPLETE" else None)
        if scan_status == "COMPLETE":
            _set_maintenance_pending(
                db, scope_id, record_id,
                "purge pending: WAL checkpoint not yet run (P0-R14-01)")
            _delete_record_erase_key(db, scope_id, record_id)
        new_row = row_or_404(db, scope_id, record_id)
    # 4. Purge old pages from the live file (best effort; see module docstring).
    maintenance = _run_maintenance(db)
    purge_failed = _purge_failed(maintenance)
    # P0-R15-01: file-level purge ACCEPTANCE, after _run_maintenance
    # and before the settlement transaction clears the marker (see
    # _verify_purge). Only when the checkpoint claims success: a busy
    # checkpoint already fails the purge, and a PARTIAL scan keeps the
    # key anyway. When this erase deleted a key, the check is the
    # direct key-hex test plus the blind probe; when the key was
    # already deleted by an earlier (crashed) erase, there is no key
    # hex to check directly — but the blind probe over the historical
    # hashes still catches OFF residue from that earlier erase (the
    # upgrade path for a pre-fix database whose OFF erase crashed
    # after the key-deletion commit). Without the probe-only branch
    # the marker would clear on the repair re-run while the old
    # key's bytes linger. On failure the note joins the maintenance
    # list and purge_failed folds it into the existing settlement
    # below — the key is restored when one was captured, the marker
    # is refreshed, the erase returns PARTIAL.
    if scan_status == "COMPLETE" and not purge_failed:
        if key_row is not None:
            purge_ok = _verify_purge(db, key_row[0], live_hashes)
        else:
            purge_ok = not _probe_files(db, live_hashes)
        if not purge_ok:
            maintenance.append(
                "file purge verification failed: the deleted "
                "verification key is still recoverable from the live "
                "DB file / WAL (P0-R15-01)")
            purge_failed = True
    maintenance_pending = False
    if scan_status == "COMPLETE":
        # P0-R13-01 / P0-R14-01: the purge verdict is settled here,
        # after the checkpoint. The marker was already written in the
        # erase transaction (P0-R14-01), so a crash before this point
        # still leaves it standing. On a busy checkpoint the key is
        # restored and the marker is REFRESHED with the checkpoint
        # failure (PARTIAL — retry via a later repair re-run); on
        # success the marker is cleared. A PARTIAL scan keeps the key
        # anyway (the first erase still needs it), so no marker is
        # written for it.
        with transaction(db, write=True):
            # P0-R18-01: re-verify INSIDE the settlement transaction
            # (BEGIN IMMEDIATE blocks other writers from committing
            # between this check and the marker decision): a
            # concurrent old-version write that re-inserted the
            # deleted key row after the pre-settlement _verify_purge
            # must turn this erase PARTIAL instead of reporting
            # COMPLETE with the key live (the repair re-run path
            # reproduces it in
            # ~/workspace/r19-work/erase-settle-race.py, 8/8; the
            # first-erase path cannot be hit the same way — its key
            # is created and deleted inside one transaction, so no
            # writer can learn the hex before the commit — but the
            # same in-transaction recheck protects it at no cost).
            if not purge_failed and not _erase_settlement_recheck(
                    db, scope_id, record_id, key_row, live_hashes):
                maintenance.append(
                    "file purge re-verification failed: the deleted "
                    "verification key is still recoverable from the "
                    "live DB file / WAL, or its key row is live again "
                    "(P0-R18-01)")
                purge_failed = True
            if purge_failed:
                if key_row is not None:
                    _restore_record_erase_key(
                        db, scope_id, record_id, key_row[0], key_row[1])
                _set_maintenance_pending(
                    db, scope_id, record_id, "; ".join(maintenance))
                maintenance_pending = True
            else:
                _clear_maintenance_pending(db, scope_id, record_id)
    # A purge failure also makes the returned status PARTIAL (the key
    # may still be recoverable from the live file), and it IS recorded
    # as a persistent marker — the scan above established no live
    # table holds the payload, but the purge is a separate outcome.
    # Authority is split and documented: the persisted erase_status
    # records the SCAN outcome, the maintenance_pending marker records
    # the PURGE outcome, and residual_report() reads both — neither
    # alone claims the file is purged. The return status follows
    # scan_status (not just the raw remaining): a status-label
    # downgrade (P1-R12-02) leaves remaining empty but the erase is
    # still PARTIAL, since the label it just wrote is a copy.
    status = "COMPLETE" if scan_status == "COMPLETE" and not purge_failed else "PARTIAL"
    # P1-R10-03: the return dict must not re-materialise the payload
    # (see _erase_return_key) — the CLI prints it to stdout.
    result = {"status": status, "record_id": record_id,
              "state": "erased", "revision": new_row["revision"], "erased_at": _now(),
              "scrubbed_receipts": scrubbed, "remaining": remaining,
              "maintenance": maintenance, "maintenance_pending": maintenance_pending,
              "erased": True,
              "cleared_illegal_timestamps": cleared,
              "scrubbed_identifiers": scrubbed_ids,
              "cleared_source_ids": cleared_sources,
              "neutralized_idempotency": neutralized_idem,
              "deleted_idempotency": deleted_idem,
              "neutralized_source_digests": neutralized_src}
    result.update(_erase_return_key(event_key, key_redacted))
    return result


def residual_report(db, scope_id, *, data_dir=None):
    """Controlled tables verified clean + uncontrolled copies enumerated.
    Never claims a copy is deleted when it is only listed.

    `controlled["live_tables_clean"]` is True only when THREE hold: the
    tombstone structure is intact (every erased record holds the
    tombstone; every receipt referencing one holds the tombstone or no
    value) AND no persisted erase outcome records an unresolved
    controlled residual (e.g. a second live record still holding the
    erased payload — see _unresolved_controlled_residuals) AND no
    file-purge-pending marker stands (P0-R13-01 / P0-R14-01: a
    terminal COMPLETE erase whose WAL checkpoint returned busy leaves
    the record's verification key recoverable from the live DB file /
    WAL, and a crash before the checkpoint leaves the same material
    unpurged with a marker standing — the tables are not called clean
    while the purge is unconfirmed. The same marker stands when the
    post-maintenance file check finds the deleted key still
    recoverable (P0-R15-01: _verify_purge) — the purge is
    unconfirmed for the same reason. A marker means "purge not yet
    confirmed complete", never "file dirty": a stale marker after a
    crash between checkpoint success and the marker clear is a
    fail-closed false positive, cleared only by a later repair
    re-run that re-runs the checkpoint. The pending records are
    listed separately in `controlled["file_purge_pending"]`, so
    logical-table-clean and file-cleanup-incomplete stay
    distinguishable). It does NOT claim no payload could ever be
    re-introduced after erasure: needles are persisted only as keyed
    hashes (HMACs), never as values, so a copy created after the
    erase cannot be detected by this report. Unresolved residuals
    are listed in `controlled["controlled_residuals"]` with their
    record keys — except a key that IS an erased payload, which is
    listed as "key_redacted" instead (P0-R8-01a): the report must
    not re-materialise the payload it certifies as erased. The same
    redaction applies to `controlled["erased_records"]`."""
    scope_id = _text(scope_id, "scope")
    report = {"scope": scope_id, "controlled": {}, "uncontrolled": [], "status": "COMPLETE"}
    erased = db.execute("SELECT id, key FROM memories WHERE scope_id=? AND state='erased'",
                        (scope_id,)).fetchall()
    # P0-R8-01a: the report lists every erased record — including one
    # whose key IS the payload. Such a key is redacted here (the record
    # id still names it): the check is by hash against the needle hashes
    # that record's own erase events persisted, verbatim and in every
    # known decoding, so a random key merely containing a short needle
    # substring is never redacted (whole-value rule, P2-R8-03).
    # P0-R11-02: a COMPLETE erase persists no needle hashes, so the
    # hash check is empty for it — the key_redacted flag the first
    # erase persisted (see _erased_record_flags) is the fallback: the
    # key cannot have changed since (the record is tombstoned).
    # P0-R12-01: the check runs on the record's own verification keys
    # (per-record key first, then the legacy scope key) — a deleted
    # per-record key verifies nothing, which is exactly right: a
    # terminal COMPLETE erase retired the key, so its historical
    # hashes are unverifiable and the key_redacted flag below is the
    # only signal left.
    erased_list = []
    for r in erased:
        stored = _stored_needle_hashes(db, scope_id, r[0])
        keys = _record_verification_keys(db, scope_id, r[0])
        if stored and _identifier_matches_hashes(r[1], stored, keys):
            erased_list.append({"id": r[0], "key_redacted": True})
        elif _erased_record_flags(db, scope_id, r[0])["key_redacted"]:
            erased_list.append({"id": r[0], "key_redacted": True})
        else:
            erased_list.append({"id": r[0], "key": r[1]})
    report["controlled"]["erased_records"] = erased_list
    # Tombstone-structure check, plus the persisted erase outcomes: a
    # second live record sharing the erased payload is invisible to the
    # structure check, so a report that ignored the persisted outcomes
    # would call the tables clean while a controlled table still holds
    # the payload (P0-R2-01 / P0-3).
    report["controlled"]["tombstone_structure_clean"] = _tombstone_structure_clean(
        db, scope_id, {r[0] for r in erased})
    report["controlled"]["controlled_residuals"] = _unresolved_controlled_residuals(
        db, scope_id)
    # P0-R13-01 / P0-R14-01: a terminal COMPLETE erase whose WAL
    # checkpoint was busy leaves the record's verification key
    # recoverable from the live DB file / WAL, and a crash before the
    # checkpoint leaves the same material unpurged with a marker
    # standing — the tables are not called clean while the purge is
    # unconfirmed. A marker means "purge not yet confirmed
    # complete", never "file dirty" (see _set_maintenance_pending):
    # a stale marker is a fail-closed false positive. The pending
    # records are listed separately, so logical-table-clean and
    # file-cleanup-incomplete stay distinguishable: a pending row is
    # NOT a payload copy in a live table (the scan already
    # established that), it is a purge that has not completed. The
    # remedy is a repair re-run after the reader releases (see
    # _run_maintenance).
    # P0-R16-03: a failed historical cleanup (purge_residue) leaves
    # the same kind of standing marker, at scope level instead of
    # per-record (see _set_purge_pending): a purge_pending row is
    # listed here with record_id "purge_residue:<scope_id>" so it
    # is distinguishable from a per-record erase marker, and it
    # makes live_tables_clean false until a successful
    # purge_residue re-check clears it. A repair re-run never
    # clears it (repair cannot know whether the historical residue
    # is gone). The two marker tables are two separate states; the
    # report consults both.
    report["controlled"]["file_purge_pending"] = (
        _maintenance_pending(db, scope_id)
        + [{"record_id": f"purge_residue:{r['scope_id']}",
            "detail": r["detail"]}
           for r in _purge_pending(db, scope_id)])
    report["controlled"]["live_tables_clean"] = (
        report["controlled"]["tombstone_structure_clean"]
        and not report["controlled"]["controlled_residuals"]
        and not report["controlled"]["file_purge_pending"])
    report["controlled"]["live_tables_clean_scope"] = (
        "tombstone structure intact, no persisted unresolved controlled "
        "residual, and no file-purge-pending marker (a terminal COMPLETE "
        "erase whose WAL checkpoint returned busy, or a crash before the "
        "checkpoint, or a failed historical cleanup — see "
        "file_purge_pending; a marker means 'purge not yet confirmed "
        "complete', never 'file dirty'); cannot detect a payload "
        "re-introduced after erasure (needles are stored only as "
        "keyed hashes (HMACs), never as values); does not cover the "
        "disclosed 2-bit finite-vocabulary residual in the live-DB-only "
        "erase_flags table (blocked repair-reason labels — see "
        "docs/KNOWN_LIMITATIONS.md §25)")
    if not report["controlled"]["live_tables_clean"]:
        report["status"] = "CONTROLLED_NOT_CLEAN"
    if data_dir is None:
        row = db.execute("PRAGMA database_list").fetchone()
        base = Path(row[2]).parent if row else Path(".")
    else:
        base = Path(data_dir)
    live = Path(db.execute("PRAGMA database_list").fetchone()[2]).resolve() if data_dir is None else None
    if base.exists():
        for p in sorted(base.rglob("*")):
            if not p.is_file():
                continue
            if live is not None and p.resolve() in (live, Path(str(live) + "-wal"), Path(str(live) + "-shm")):
                continue  # the live DB (and its WAL) is the controlled file, not a residual copy
            name = p.name
            if name.endswith((".bak", ".sqlite3", ".sqlite3-wal", ".sqlite3-shm")) or "export" in name:
                report["uncontrolled"].append({
                    "path": str(p), "kind": "backup_or_export_copy",
                    "note": "may still contain erased payloads; delete it yourself — "
                            "Turnkeep never deletes files outside the live DB"})
    report["uncontrolled"].append({
        "path": "(external clients)", "kind": "client_history",
        "note": "MCP/HTTP clients that read the record before erasure may still "
                "hold it in their own history; Turnkeep cannot erase another "
                "client's memory"})
    report["uncontrolled"].append({
        "path": "(storage media)", "kind": "physical_recovery",
        "note": "WAL checkpoint purges the live file (VACUUM reclaims "
                "freelist space only); SSD / filesystem backups may still "
                "hold old pages — not provable as unrecoverable. A busy "
                "checkpoint is NOT this boundary: it leaves the key in "
                "the current live DB file / WAL, is detectable, and is "
                "reported via file_purge_pending instead"})
    # A dirty controlled table is worse than uncontrolled residuals:
    # it must not be overwritten by the residuals status below.
    if report["uncontrolled"] and report["controlled"]["live_tables_clean"]:
        report["status"] = "COMPLETE_WITH_RESIDUALS"
    return report
