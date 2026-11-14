"""Versioned export / import (T036, J05). JSONL + manifest; the SQLite file
is never the only export format.

Format v1 (a directory):
  manifest.json  {format: "turnkeep-export", schema_version: 1,
                  scope_id, exported_at, tool, record_count,
                  files: {records.jsonl: sha256, events.jsonl: sha256}}
  records.jsonl  one record per line (dump_record shape minus scope):
                 id (origin id), key, value, kind, state, revision,
                 created_at, updated_at, valid_until, cause_event_id,
                 source_refs, parent_ids, supersedes_ids
  events.jsonl   audit history for the scope (informational; import records
                 ONE import event instead of replaying raw events, so epoch
                 accounting stays local).

Trust rules:
- Export excludes secret-like values (same pattern as briefs.redact)
  and lists them in the report — by key only when the key is benign;
  a key that IS payload material (verbatim or in a known encoding)
  is omitted from the report entry and flagged "key_redacted"
  (P1-R9-04: the report must not re-leak the secret it certifies as
  excluded). The same rule covers stripped_illegal_timestamps and
  the import plan's skipped_existing / quarantined entries.
- Import is dry-run first (conflicts + id remap listed, nothing written).
- Imported records keep their origin ids only as provenance; local ids are
  deterministic remaps (sha256 of scope + origin id), so importing the same
  package twice is a no-op (import_batches records the package digest).
- Incoming state/actor/authority claims are NOT trusted: a record claiming
  active whose remapped parents are missing or unusable is imported as
  tainted; cross-scope edges and missing parents are quarantined, never
  silently repaired. Unknown future schema_version is rejected outright.
- Write order is topologically sorted (parents before children), so an
  edge is never dropped merely because the child precedes the parent in
  the file. An edge dropped at write time for a parent that WAS planned
  is a topo-sort failure and fails the import; a drop for a quarantined /
  skipped parent is a prominent plan warning, never a silent footnote.
- After the write loop, still inside the same transaction, every imported
  record is verified: each parent edge exists and the parent exists and is
  usable; an active child of an unusable parent is tainted in place (its
  cause is the import event) and a missing edge for an existing parent is
  re-inserted. Every outcome is recorded in the plan.
- Each imported record's origin id / origin scope is written to
  record_provenance, so an imported record traces back to its source
  package (P1-c).
- A tampered manifest (digest mismatch) rejects the whole package.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .erasure import (_check_reason_against_needles, _decoded_forms,
                      _erased_payload_hashes_by_record,
                      _export_excluded_identifiers,
                      _identifier_matches_any_erased, _label_encodes_erased,
                      _is_legal_timestamp, _needles_for_payload,
                      _report_key_redacted)
from .errors import TurnkeepError
from .retention import _check_valid_until
from .scopes import ensure_scope, register_scope
from .storage import (KINDS, STATES, dump_record, publish_event,
                      record_provenance, set_state, transaction,
                      upsert_sources, usable, _id, _now, _json)

FORMAT = "turnkeep-export"
SCHEMA_VERSION = 1
_SECRET_LIKE = ("api_key", "apikey", "api-key", "password", "passwd", "token", "secret", "bearer")


def _sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _looks_secret(value):
    # No length gate: a secret-like value is excluded whatever its size.
    # (P0-R2-02: the old `len(text) < 5000` gate let a 5,100-char structured
    # api_key export in plaintext.) Keyword list mirrors
    # briefs._SECRET_KEY (api[_-]?key, password, passwd, secret, token,
    # bearer); keep the two in sync.
    text = value if isinstance(value, str) else _json(value)
    lowered = text.lower()
    return any(k in lowered for k in _SECRET_LIKE)


def _remap_id(scope_id, origin_id):
    return hashlib.sha256(f"turnkeep-import-v1:{scope_id}:{origin_id}".encode()).hexdigest()[:32]


def _report_entry(base, key, needles, erased_by_record, db, scope_id, *,
                  force_redact_key=False):
    """A report entry carrying a key (P1-R9-04): the key is omitted and
    flagged "key_redacted" when it is payload material (see
    erasure._report_key_redacted); a benign key stays visible, so the
    entry still names its record. One helper for every report surface
    (export excluded / stripped, import skipped / quarantined), so no
    surface can drift back to echoing a raw key. force_redact_key is
    for a caller that already knows the key is payload material from
    a source the three _report_key_redacted rules cannot see — the
    export-excluded-identifiers map (P0-R11-02), which records that a
    record's key WAS an erased payload at erase time, after a COMPLETE
    erase stopped persisting the hashes that rule checks against.
    erased_by_record is the per-record-keyed hash map (see
    erasure._erased_payload_hashes_by_record): the redaction check
    verifies each hash under its own record's key (P0-R12-01), so a
    hash whose key was retired at terminal COMPLETE never redacts."""
    entry = dict(base)
    if force_redact_key or _report_key_redacted(
            key, needles, db=db, scope_id=scope_id,
            erased_by_record=erased_by_record):
        entry["key_redacted"] = True
    else:
        entry["key"] = key
    return entry


def _import_report_needles(rec):
    """Needle set for redacting an import-plan entry's key (P1-R9-04):
    the record's own value needles plus its raw timestamp field values
    (a key decoding to material glued into a timestamp is payload
    material, as in the export stripped surface). The record's own
    value is the only payload this plan entry can be checked against —
    a key encoding some OTHER record's payload is the import gate's
    known residual, not something the report can detect."""
    needles = _needles_for_payload(_json(rec.get("value")))
    for field in ("created_at", "updated_at", "valid_until"):
        value = rec.get(field)
        if isinstance(value, str) and value:
            needles.add(value)
    return needles


def export_scope(db, scope_id, out_dir, *, include_history=True):
    """Export one scope to out_dir. Returns the report dict."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    rows = db.execute(
        "SELECT * FROM memories WHERE scope_id=? ORDER BY key, created_at", (scope_id,)).fetchall()
    records, excluded, stripped = [], [], []
    # P0-R8-01c: needle hashes from EVERY erase event in the scope
    # (COMPLETE and PARTIAL alike), read once up front, grouped by the
    # erase event's first affected record. A payload that was ever
    # erased must not be re-exported through another record's id, key,
    # source id, or value — the R7 check below only covers a record's
    # OWN live payload, and is vacuous for a tombstone row (its value
    # is the tombstone, so its needles are empty) and blind to a
    # cross-record source id (Y's source id == X's erased payload is
    # not among Y's own needles). That tombstone-blindness is what
    # leaked erased payloads through exports. P0-R12-01: the map is
    # grouped by record so each hash is verified under its own
    # record's key (see _identifier_matches_any_erased) — a hash
    # whose record reached terminal COMPLETE had its key retired, so
    # it verifies nothing and never excludes a record.
    erased_by_record = _erased_payload_hashes_by_record(db, scope_id)
    # P0-R11-02: records whose id, key, or source-ref ids WERE an
    # erased payload at erase time, read once up front (the union over
    # all erase events — see erasure._export_excluded_identifiers). A
    # COMPLETE erase persists no needle hashes, so the hash check
    # below cannot see its payload; the map is the replacement for
    # those records. A record created AFTER the erase is not in the
    # map (a post-erase re-introduction — see residual_report()).
    excluded_identifiers = _export_excluded_identifiers(db, scope_id)
    for row in rows:
        rec = dump_record(db, row)
        needles = _needles_for_payload(_json(rec["value"]))
        if _looks_secret(rec["value"]):
            excluded.append(_report_entry(
                {"reason": "secret-like value excluded"}, rec["key"],
                needles, erased_by_record, db, scope_id))
            continue
        # P0-R7-01d: an identifier that IS the payload cannot be
        # stripped — the whole record is excluded instead (same
        # contract as a secret-like value). Whole-value equality only:
        # a key / source id that merely contains a short needle as a
        # substring is not a copy (identifiers are identifiers by
        # design; see erasure._PAYLOAD_COLUMNS).
        if rec["key"] in needles:
            excluded.append(_report_entry(
                {"reason": "key equals the record's payload; "
                           "an identifier cannot be stripped"},
                rec["key"], needles, erased_by_record, db, scope_id))
            continue
        if any(src.get("id") in needles for src in rec.get("source_refs", [])):
            excluded.append(_report_entry(
                {"reason": "source id equals the record's payload; "
                           "an identifier cannot be stripped"},
                rec["key"], needles, erased_by_record, db, scope_id))
            continue
        # P0-R8-01c: the same whole-record exclusion, for an erased
        # payload. Candidates are the record's id, key, source ids and
        # value needles (raw JSON + leaves), each checked verbatim and
        # in every known decoding against the erased-payload hashes —
        # so a tombstone row's key/source_refs, a cross-record source
        # id, and an encoded identifier are all excluded, while a
        # random id that merely contains a short needle substring is
        # never matched (whole-value rule, P2-R8-03). Kept alongside
        # the R7 checks above: those cover a record's own live payload,
        # for which no erase event exists yet. P0-R12-01: each hash is
        # verified under its own record's key (see
        # _identifier_matches_any_erased) — a retired key verifies
        # nothing, so a terminal-COMPLETE record's hashes never
        # exclude anything here.
        candidates = [rec["id"], rec["key"]]
        candidates.extend(src.get("id") for src in rec.get("source_refs", [])
                          if isinstance(src.get("id"), str))
        candidates.extend(needles)
        if any(_identifier_matches_any_erased(db, scope_id, c, erased_by_record)
               for c in candidates if isinstance(c, str)):
            excluded.append(_report_entry(
                {"reason": "record carries a previously erased payload "
                           "in its id, key, source id or value"},
                rec["key"], needles, erased_by_record, db, scope_id))
            continue
        # P0-R11-02: the same whole-record exclusion, for a COMPLETE
        # erase — whose event persists no needle hashes, so the check
        # above cannot see it. The map records, per erase event, which
        # records' identifiers were the payload at erase time; a match
        # here excludes the record. The key is redacted only when the
        # map says the KEY was the payload (a record excluded because
        # only its source id matched keeps its benign key visible).
        matched_fields = excluded_identifiers.get(rec["id"], frozenset())
        if matched_fields:
            excluded.append(_report_entry(
                {"reason": "record carries a previously erased payload "
                           "in its id, key or source id"},
                rec["key"], needles, erased_by_record, db, scope_id,
                force_redact_key=("key" in matched_fields)))
            continue
        # P1-R12-02 (kind): a tombstone row whose kind encodes to a
        # previously erased payload is excluded whole-record (see
        # erasure._label_encodes_erased) — the erase reported PARTIAL
        # for that kind, so the export is the remedy that removes it.
        # The check is scope-wide (fail-closed): a tombstone row
        # carries no live data, so excluding one whose kind happens to
        # match another record's erased payload costs only audit
        # traceability. Live records' kinds are NOT excluded — they
        # are those records' own structural labels, and export cannot
        # remove a live record's kind (see erasure.erase_record and
        # docs/KNOWN_LIMITATIONS.md §25).
        if rec["state"] == "erased" and _label_encodes_erased(db, scope_id, rec["kind"]):
            excluded.append(_report_entry(
                {"reason": "tombstone record's kind encodes a previously "
                           "erased payload"},
                rec["key"], needles, erased_by_record, db, scope_id))
            continue
        # P0-R7-01d: a timestamp field that is not a legal timestamp
        # (legacy smuggled text, an encoding) — or that equals the
        # payload itself — is stripped (null) from the exported record,
        # never carried out. A legal timestamp stays, even when it
        # contains a short needle as a substring ("2026-…" contains
        # "202"): it is legitimate expiry text, not a copy. Stripping
        # is reported, not silent. events.jsonl is NOT filtered the
        # same way: it is the append-only audit history, and refusing
        # an export over a pre-fix leaked reason would break the
        # documented remedy (export to a fresh DB and delete this one)
        # that erase_record itself offers for PARTIAL erasures.
        stripped_fields = []
        stripped_values = []
        for field in ("created_at", "updated_at", "valid_until"):
            value = rec.get(field)
            if value is not None and (not _is_legal_timestamp(value)
                                      or value in needles):
                stripped_values.append(value)
                rec[field] = None
                stripped_fields.append(field)
        if stripped_fields:
            # P1-R9-04: the stripped (illegal) timestamp values are
            # part of the needle set — a key decoding to material
            # glued into one of them is payload material, even when
            # the record's own value is a plain note.
            stripped.append(_report_entry(
                {"fields": stripped_fields}, rec["key"],
                needles | set(stripped_values), erased_by_record, db, scope_id))
        rec.pop("scope", None)
        records.append(rec)
    rec_path = out / "records.jsonl"
    rec_path.write_text("".join(_json(r) + "\n" for r in records), encoding="utf-8")
    events = []
    if include_history:
        for row in db.execute(
                "SELECT scope_epoch, event_type, affected_ids_json, metadata_json, created_at "
                "FROM events WHERE scope_id=? ORDER BY scope_epoch", (scope_id,)).fetchall():
            events.append({"scope_epoch": row[0], "event_type": row[1],
                           "affected_ids": json.loads(row[2]),
                           "metadata": json.loads(row[3]) if row[3] else {},
                           "created_at": row[4]})
    ev_path = out / "events.jsonl"
    ev_path.write_text("".join(_json(e) + "\n" for e in events), encoding="utf-8")
    manifest = {"format": FORMAT, "schema_version": SCHEMA_VERSION, "scope_id": scope_id,
                "exported_at": _now(), "tool": "turnkeep 0.1.0",
                "record_count": len(records), "event_count": len(events),
                "files": {"records.jsonl": _sha256_file(rec_path),
                          "events.jsonl": _sha256_file(ev_path)}}
    (out / "manifest.json").write_text(_json(manifest) + "\n", encoding="utf-8")
    return {"scope": scope_id, "records_exported": len(records),
            "records_excluded_secrets": excluded,
            "stripped_illegal_timestamps": stripped,
            "events_exported": len(events),
            "files": manifest["files"], "out_dir": str(out)}


def _read_package(package_dir):
    pkg = Path(package_dir)
    manifest_path = pkg / "manifest.json"
    if not manifest_path.exists():
        raise TurnkeepError("INVALID_INPUT", f"not a turnkeep export package: {pkg} (no manifest.json)")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("format") != FORMAT:
        raise TurnkeepError("INVALID_INPUT", f"unknown export format {manifest.get('format')!r}")
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise TurnkeepError("UNSUPPORTED",
                            f"export schema_version {manifest.get('schema_version')!r} is not supported (this tool reads v1)")
    for name, digest in manifest.get("files", {}).items():
        path = pkg / name
        if not path.exists():
            raise TurnkeepError("INVALID_INPUT", f"export package is missing {name}")
        if _sha256_file(path) != digest:
            raise TurnkeepError("INVALID_INPUT",
                                f"export package tampered: {name} digest mismatch (manifest does not match file)")
    records = [json.loads(l) for l in (pkg / "records.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    return manifest, records


def _plan_import(db, manifest, records, target_scope):
    """Validate + plan without writing. Returns (plan, package_digest)."""
    package_digest = _sha256_text(_json(manifest) + _json(manifest["files"]))
    origin_scope = manifest["scope_id"]
    plan = {"target_scope": target_scope, "origin_scope": origin_scope,
            "records": len(records), "to_import": [], "skipped_existing": [],
            "quarantined": [], "edges_dropped": [], "warnings": []}
    # P1-R9-04: needle hashes of payloads erased in the target scope,
    # for redacting report keys that encode one (same rule as export),
    # grouped by the erase event's first affected record so each hash
    # is verified under its own record's key (P0-R12-01).
    # P0-R11-02: a COMPLETE erase persists no needle hashes, so a
    # skipped / quarantined record whose key equals a COMPLETE-erased
    # payload is NOT redacted here (the record's own value needles
    # still are). The import report cannot know a payload it never
    # saw: the key is the package's own, supplied by the caller, and
    # the erased payload's hashes are gone by design. Documented in
    # docs/KNOWN_LIMITATIONS.md §24.
    erased_by_record = _erased_payload_hashes_by_record(db, target_scope)
    ids_in_package = {r["id"] for r in records}
    for rec in records:
        local_id = _remap_id(target_scope, rec["id"])
        existing = db.execute("SELECT state FROM memories WHERE id=?", (local_id,)).fetchone()
        if existing is not None:
            plan["skipped_existing"].append(_report_entry(
                {"origin_id": rec["id"]}, rec["key"],
                _import_report_needles(rec), erased_by_record, db, target_scope))
            continue
        # P0-R7-01 (1b): kind/state are enum-validated — a package must
        # not smuggle free text in through them (the DB CHECK would
        # otherwise fail the write with a raw IntegrityError).
        if rec.get("kind") not in KINDS:
            raise TurnkeepError(
                "INVALID_INPUT",
                f"unknown memory kind {rec.get('kind')!r}: kind must be one of "
                f"{', '.join(sorted(KINDS))}")
        if rec.get("state") not in STATES:
            raise TurnkeepError(
                "INVALID_INPUT",
                f"unknown memory state {rec.get('state')!r}: state must be one of "
                f"{', '.join(sorted(STATES))}")
        parents = []
        for pid in rec.get("parent_ids", []):
            if pid not in ids_in_package:
                plan["quarantined"].append(_report_entry(
                    {"origin_id": rec["id"],
                     "reason": f"parent {pid} not in package"},
                    rec["key"], _import_report_needles(rec),
                    erased_by_record, db, target_scope))
                parents = None
                break
            parents.append(_remap_id(target_scope, pid))
        if parents is None:
            continue
        state = rec["state"]
        if state == "active":
            # Do not trust the incoming active claim: parents must resolve.
            missing = [p for p in parents
                       if db.execute("SELECT 1 FROM memories WHERE id=?", (p,)).fetchone() is None
                       and p not in {_remap_id(target_scope, r["id"]) for r in records}]
            if missing:
                state = "tainted"
                plan["quarantined"].append(_report_entry(
                    {"origin_id": rec["id"],
                     "reason": "active claim not trusted: parent missing; imported as tainted"},
                    rec["key"], _import_report_needles(rec),
                    erased_by_record, db, target_scope))
        for src in rec.get("source_refs", []):
            if src.get("type") not in ("user_statement", "tool_observation", "agent_inference", "imported_text"):
                plan["quarantined"].append(_report_entry(
                    {"origin_id": rec["id"],
                     "reason": f"unknown source type {src.get('type')!r}"},
                    rec["key"], _import_report_needles(rec),
                    erased_by_record, db, target_scope))
                parents = None
                break
        if parents is None:
            continue
        # P0-R7-01 (1a): created_at / updated_at get the SAME validation
        # as valid_until — ISO-8601 with a timezone first, then the
        # needle check against this record's own value as a second
        # layer (a payload that itself IS a timestamp). Before this fix
        # neither field was checked at all, so a package could plant
        # the secret (verbatim or encoded) there and a later erase
        # still reported COMPLETE. Only records actually being imported
        # are validated: a quarantined/skipped record's timestamps are
        # never written. The needle set is shared with the valid_until
        # check below.
        needles = _needles_for_payload(_json(rec["value"]))
        for field in ("created_at", "updated_at"):
            ts = rec.get(field)
            if ts is not None:
                _check_valid_until(ts)
                _check_reason_against_needles(ts, needles)
        # P0-R6-01 (A4): valid_until is copied verbatim from the package
        # into memories.valid_until (exported in records.jsonl, never
        # cleared by erase) with NO check at all before this fix — a
        # package could plant the secret verbatim (or encoded) there and
        # a later erase still reported COMPLETE. Apply the same
        # validation set_expiry uses: ISO-8601 timestamp with a timezone
        # first, then the needle check against this record's own value
        # as a second layer (a payload that itself IS a timestamp).
        # Only records actually being imported are validated: a
        # quarantined/skipped record's valid_until is never written.
        valid_until = rec.get("valid_until")
        if valid_until is not None:
            _check_valid_until(valid_until)
            _check_reason_against_needles(valid_until, needles)
        # P0-R8-01d: the key and source ids get the same boundary —
        # an identifier that IS the record's payload (verbatim, or in
        # a known encoding: Base64 / URL-safe Base64 / percent /
        # reversed / hex / rot13)
        # is rejected, not imported. Whole-value equality only: a
        # random id that merely contains a short needle substring is
        # not a copy, and is not rejected (the same unified rule the
        # erase scan uses for identifier fields, P2-R8-03). The
        # decoded set is incomplete by construction (R6 fix-space
        # probe) — a key carrying an unknown encoding is the documented
        # residual in docs/KNOWN_LIMITATIONS.md §21, not a gap this
        # gate claims to close; the export hash check (P0-R8-01c) is
        # the second layer for identifiers that slip through.
        for identifier, where in ((rec.get("key"), "key"),) + tuple(
                (src.get("id"), "source id") for src in rec.get("source_refs", [])):
            if not isinstance(identifier, str) or not identifier:
                continue
            for form in _decoded_forms(identifier):
                if form in needles:
                    raise TurnkeepError(
                        "INVALID_INPUT",
                        f"imported {where} equals the record's payload "
                        f"(verbatim or in a known encoding): an identifier "
                        f"that IS the payload cannot be imported")
        plan["to_import"].append({"record": rec, "local_id": local_id,
                                  "parent_ids": parents, "state": state})
    # Second pass: an active claim is only trusted when every parent will
    # actually exist (imported now or already in the DB). A parent that was
    # quarantined or skipped taints the child instead of being silently fixed.
    will_exist = {i["local_id"] for i in plan["to_import"]}
    for item in plan["to_import"]:
        if item["state"] != "active":
            continue
        for parent in item["parent_ids"]:
            if parent in will_exist:
                continue
            if db.execute("SELECT 1 FROM memories WHERE id=?", (parent,)).fetchone() is None:
                item["state"] = "tainted"
                plan["quarantined"].append(_report_entry(
                    {"origin_id": item["record"]["id"],
                     "reason": "active claim not trusted: parent quarantined; imported as tainted"},
                    item["record"]["key"],
                    _import_report_needles(item["record"]),
                    erased_by_record, db, target_scope))
                break
    # P0-2: write (and dry-run) in dependency order — parents before
    # children — so the write loop never drops an edge merely because the
    # child precedes the parent in the file. Edges to parents already in
    # the DB or quarantined do not constrain the order.
    plan["to_import"] = _topo_sort_import(plan["to_import"])
    return plan, package_digest


def _topo_sort_import(items):
    """Stable Kahn sort of planned imports by parent_ids (parents first).

    Only parents that are themselves being imported constrain the order.
    Ties keep file order (stable). A cycle can never be ordered: its
    members are appended in file order at the end, and the write loop's
    escalation then fails the import instead of silently dropping edges."""
    by_id = {i["local_id"]: i for i in items}
    order = {i["local_id"]: n for n, i in enumerate(items)}
    parents_in_plan = {i["local_id"]: {p for p in i["parent_ids"] if p in by_id}
                       for i in items}
    children = {i["local_id"]: set() for i in items}
    for child, parents in parents_in_plan.items():
        for p in parents:
            children[p].add(child)
    indegree = {lid: len(parents) for lid, parents in parents_in_plan.items()}
    ready = sorted((lid for lid, deg in indegree.items() if deg == 0),
                   key=lambda lid: order[lid])
    sorted_items, done = [], set()
    while ready:
        lid = ready.pop(0)
        done.add(lid)
        sorted_items.append(by_id[lid])
        for child in children[lid]:
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
        ready.sort(key=lambda lid: order[lid])
    for item in items:  # leftovers: a dependency cycle, unordered
        if item["local_id"] not in done:
            sorted_items.append(item)
    return sorted_items


def _verify_import_integrity(db, target, items, event_id, plan, changes):
    """Post-write dependency-graph check, inside the import transaction.

    For every imported record, in topo order (parents verified before
    children): each parent edge must exist in dependencies and the parent
    must exist and be usable; an active child of an unusable parent must
    be tainted. Repairs happen in place and every outcome is recorded in
    the plan: plan["integrity_repairs"] for repairs, and
    plan["integrity_expected_drops"] for edges dropped for quarantined /
    skipped parents whose child is already tainted (the only legitimate
    missing-edge case)."""
    repairs = plan.setdefault("integrity_repairs", [])
    expected_drops = plan.setdefault("integrity_expected_drops", [])
    for item in items:
        child_id, child_key = item["local_id"], item["record"]["key"]
        row = db.execute("SELECT state FROM memories WHERE scope_id=? AND id=?",
                         (target, child_id)).fetchone()
        if row is None:
            continue  # unreachable: just inserted by the write loop
        state = row[0]
        for parent in item["parent_ids"]:
            parent_row = db.execute(
                "SELECT state, revision FROM memories WHERE scope_id=? AND id=?",
                (target, parent)).fetchone()
            if parent_row is None:
                if state == "tainted":
                    expected_drops.append({
                        "child": child_key, "child_id": child_id, "parent": parent,
                        "reason": "parent quarantined/skipped; child already tainted"})
                else:
                    set_state(db, target, child_id, "tainted", changes,
                              cause_event_id=event_id)
                    state = "tainted"
                    repairs.append({
                        "action": "tainted_child_with_missing_parent",
                        "child": child_key, "child_id": child_id, "parent": parent})
                continue
            edge = db.execute(
                "SELECT 1 FROM dependencies WHERE scope_id=? AND child_id=? AND parent_id=?",
                (target, child_id, parent)).fetchone()
            if edge is None:
                db.execute("INSERT OR IGNORE INTO dependencies(scope_id, child_id, parent_id, "
                           "parent_revision_at_use) VALUES (?,?,?,?)",
                           (target, child_id, parent, parent_row[1]))
                repairs.append({
                    "action": "inserted_missing_edge",
                    "child": child_key, "child_id": child_id, "parent": parent})
            if not usable(db, target, parent):
                if state == "active":
                    set_state(db, target, child_id, "tainted", changes,
                              cause_event_id=event_id)
                    state = "tainted"
                    repairs.append({
                        "action": "tainted_child_with_unusable_parent",
                        "child": child_key, "child_id": child_id, "parent": parent})
    return repairs, expected_drops


def import_package(db, package_dir, *, target_scope=None, dry_run=False):
    """Import a package. Dry-run returns the plan without writing.
    Real import writes in one transaction + an import_batches row."""
    manifest, records = _read_package(package_dir)
    target = target_scope or manifest["scope_id"]
    plan, package_digest = _plan_import(db, manifest, records, target)
    plan["dry_run"] = dry_run
    plan["package_digest"] = package_digest
    already = db.execute(
        "SELECT status FROM import_batches WHERE scope_id=? AND source_digest=?",
        (target, package_digest)).fetchone()
    if already is not None and already[0] == "published":
        plan["already_imported"] = True
        plan["to_import"] = []
        plan["imported"] = 0
        return plan
    plan["already_imported"] = False
    if dry_run:
        return plan
    with transaction(db, write=True):
        ensure_scope(db, target)
        # ONE event id for the whole import (P1-g): integrity repairs cite
        # it as their cause, and the import event row itself carries it.
        ev = _id()
        changes = {i["local_id"]: None for i in plan["to_import"]}
        planned = {i["local_id"] for i in plan["to_import"]}
        for item in plan["to_import"]:
            rec = item["record"]
            upsert_sources(db, target, rec.get("source_refs", []))
            db.execute(
                "INSERT INTO memories(id, scope_id, key, value_json, kind, state, revision, "
                "created_at, updated_at, valid_until, cause_event_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (item["local_id"], target, rec["key"], _json(rec["value"]), rec["kind"],
                 item["state"], rec["revision"],
                 # P0-R7-01d: a null created_at/updated_at in the package
                 # means "stripped at export (illegal timestamp) or
                 # unknown" — it re-imports as the import time, never as
                 # NULL (the columns are NOT NULL). valid_until stays
                 # nullable, so a null there stays null.
                 rec.get("created_at") or _now(),
                 rec.get("updated_at") or _now(), rec.get("valid_until"),
                 # P1-R2-03a: cite the import event (ev), never the source
                 # DB's cause_event_id — that id does not exist in the
                 # target DB's events table.
                 ev))
            # P1-c: per-record provenance — origin id + origin scope.
            record_provenance(db, target, item["local_id"], rec["id"],
                              manifest["scope_id"])
            for src in rec.get("source_refs", []):
                db.execute("INSERT OR IGNORE INTO memory_sources(scope_id, memory_id, source_id) "
                           "VALUES (?,?,?)", (target, item["local_id"], src["id"]))
            for parent in item["parent_ids"]:
                # A quarantined parent was never written: the edge is dropped
                # (the record is already tainted by the second pass), never
                # faked with a synthetic parent. That drop is a prominent
                # plan warning, not a silent footnote.
                if db.execute("SELECT 1 FROM memories WHERE id=?", (parent,)).fetchone() is None:
                    if parent in planned:
                        # Topo-sort failure: the parent was planned but was
                        # not written before this child. Must not happen
                        # after the sort — fail the import, never drop it.
                        raise TurnkeepError(
                            "INVALID_PARENT",
                            "Import topo-sort failure: planned parent was not written "
                            "before its child; refusing to drop the edge",
                            child=rec["key"], parent=parent)
                    plan["edges_dropped"].append({"child": rec["key"], "parent": parent})
                    plan["warnings"].append({
                        "message": f"edge dropped: parent {parent} was quarantined or "
                                   f"skipped, so no dependency edge was written for "
                                   f"child {rec['key']}",
                        "child": rec["key"], "parent": parent})
                    continue
                db.execute("INSERT OR IGNORE INTO dependencies(scope_id, child_id, parent_id, "
                           "parent_revision_at_use) VALUES (?,?,?,?)",
                           (target, item["local_id"], parent, 1))
            for old_id in rec.get("supersedes_ids", []):
                if old_id in {r["id"] for r in records}:
                    db.execute("INSERT OR IGNORE INTO replacements(scope_id, old_id, new_id, reason) "
                               "VALUES (?,?,?,?)", (target, _remap_id(target, old_id), item["local_id"], "import"))
        # P0-5: verify the dependency graph in this same transaction,
        # before the import_batches row; repairs are recorded in the plan.
        _verify_import_integrity(db, target, plan["to_import"], ev, plan, changes)
        db.execute("INSERT INTO import_batches(id, scope_id, status, source_digest, schema_version, created_at) "
                   "VALUES (?,?,?,?,?,?)",
                   (hashlib.sha256(f"{target}:{package_digest}".encode()).hexdigest()[:32], target, "published", package_digest,
                    str(SCHEMA_VERSION), _now()))
        publish_event(db, target, "import", "import", changes,
                      metadata={"package_digest": package_digest,
                                "records": len(plan["to_import"]),
                                "quarantined": len(plan["quarantined"])},
                      event_id=ev)
    plan["imported"] = len(plan["to_import"])
    return plan
