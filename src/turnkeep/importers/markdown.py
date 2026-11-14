"""Conservative Markdown importer (T037).

Reads .md files (AGENTS.md / CLAUDE.md style) and parses *structured* items
only — list items under a recognised section heading, plus task checkboxes.
Plain prose is never auto-upgraded to a verified fact: unmapped sections are
skipped and counted, never imported.

Every candidate carries its provenance: file path, heading, content hash and
read time. The imported text is data, never instructions: code fences are
skipped, links are kept as literal text, and nothing in the file is executed
or used to rebind identity. Imported sources are always `imported_text`.

Write path (dry_run=False): only candidates whose key is absent are written,
through the normal `memory.remember()` (all core validation applies). Same
key + same value is a duplicate (skipped); same key + different value is a
conflict (reported, left for adjudication, never auto-merged). A dry run
writes nothing.

Boundaries: max 1 MiB per file, max 200 files per directory, no symlinks, no
paths escaping the given root, at most 500 candidates. Larger / escaping /
symlinked inputs are rejected with a clear error, not silently skipped.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from ..errors import TurnkeepError
from ..memory import remember
from ..storage import record_provenance

MAX_FILE_BYTES = 1 * 1024 * 1024
MAX_FILES = 200
MAX_CANDIDATES = 500

# heading (lowercased, EN + ZH) -> kind. Unlisted headings are not imported.
SECTION_KINDS = {
    "decision": "decision", "decisions": "decision", "决定": "decision",
    "goal": "requirement", "goals": "requirement", "目标": "requirement",
    "requirement": "requirement", "requirements": "requirement",
    "需求": "requirement", "要求": "requirement",
    "attempt": "attempt", "attempts": "attempt", "尝试": "attempt",
    "experiment": "attempt", "experiments": "attempt", "实验": "attempt",
    "todo": "requirement", "todos": "requirement", "待办": "requirement",
    "task": "requirement", "tasks": "requirement", "任务": "requirement",
    "procedure": "procedure", "procedures": "procedure", "流程": "procedure",
    "步骤": "procedure", "handoff": "handoff", "交接": "handoff",
}
_SECRET_LIKE = ("api_key", "apikey", "password", "passwd", "token", "secret")


def _slug(text):
    text = re.sub(r"[^\w\u4e00-\u9fff]+", "-", text.lower()).strip("-")
    return text[:40] or "item"


def _kind_for_heading(heading):
    h = heading.strip().lower()
    for word, kind in SECTION_KINDS.items():
        if word in h:
            return kind
    return None


def _parse_file(path, rel):
    """Return (candidates, skipped_unmapped) for one .md file."""
    text = path.read_text(encoding="utf-8", errors="strict")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    candidates, skipped = [], 0
    kind, heading = None, ""
    in_fence = False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = re.match(r"^#{1,4}\s+(.+?)\s*$", line)
        if m:
            heading = m.group(1)
            kind = _kind_for_heading(heading)
            continue
        m = re.match(r"^\s*[-*]\s+(?:\[( |x|X)\]\s+)?(.+?)\s*$", line)
        if not m:
            continue
        if kind is None:
            skipped += 1
            continue
        checked, item = m.group(1), m.group(2)
        if item.startswith("!") or item.startswith("["):
            # Images / bare links are not memory items.
            skipped += 1
            continue
        item_kind = "attempt" if checked and checked.lower() == "x" else kind
        key = f"md.{_slug(Path(rel).stem)}.{_slug(heading)}.{hashlib.sha256(item.encode()).hexdigest()[:8]}"
        candidates.append({
            "key": key, "value": item, "kind": item_kind,
            "source": {"id": f"md:{rel}#{_slug(heading)}", "type": "imported_text"},
            "provenance": {"file": rel, "heading": heading, "content_hash": digest},
        })
    return candidates, skipped


def _collect_files(root):
    if root.is_symlink():
        raise TurnkeepError("INVALID_INPUT", f"refusing to follow symlink: {root}")
    files = []
    if root.is_file():
        files = [root]
    elif root.is_dir():
        for p in sorted(root.rglob("*.md")):
            if any(part.startswith(".") for part in p.relative_to(root).parts):
                continue
            if p.is_symlink():
                raise TurnkeepError("INVALID_INPUT", f"refusing to follow symlink: {p}")
            files.append(p)
            if len(files) > MAX_FILES:
                raise TurnkeepError("INVALID_INPUT", f"too many .md files (max {MAX_FILES})")
    else:
        raise TurnkeepError("NOT_FOUND", f"no such file or directory: {root}")
    for p in files:
        if p.stat().st_size > MAX_FILE_BYTES:
            raise TurnkeepError("INVALID_INPUT", f"file too large (max 1 MiB): {p}")
    return files


def _resolve_root(path):
    raw = Path(path)
    if raw.is_symlink():
        raise TurnkeepError("INVALID_INPUT", f"refusing to follow symlink: {raw}")
    return raw.resolve()


def preview_markdown(path, scope_id):
    """Parse only; never writes. Returns the preview dict."""
    root = _resolve_root(path)
    files = _collect_files(root)
    candidates, skipped_unmapped = [], 0
    for p in files:
        rel = str(p.relative_to(root)) if p != root else p.name
        try:
            c, s = _parse_file(p, rel)
        except UnicodeDecodeError:
            raise TurnkeepError("INVALID_INPUT", f"file is not valid UTF-8: {p}")
        candidates.extend(c)
        skipped_unmapped += s
    if len(candidates) > MAX_CANDIDATES:
        raise TurnkeepError("INVALID_INPUT", f"too many candidates (max {MAX_CANDIDATES})")
    secret_like = [c for c in candidates
                   if any(k in c["value"].lower() for k in _SECRET_LIKE)]
    return {"format": "markdown", "scope": scope_id, "files": len(files),
            "candidates": len(candidates), "skipped_unmapped": skipped_unmapped,
            "secret_like_excluded": [c["key"] for c in secret_like],
            "note": "Candidates are untrusted imported_text. Plain prose is never "
                    "auto-upgraded to a verified fact; conflicts stay for adjudication."}


def import_markdown(db, path, scope_id, *, dry_run=True):
    """Dry-run: preview only. Real import writes new candidates only."""
    root = _resolve_root(path)
    files = _collect_files(root)
    all_candidates, skipped_unmapped = [], 0
    for p in files:
        rel = str(p.relative_to(root)) if p != root else p.name
        try:
            c, s = _parse_file(p, rel)
        except UnicodeDecodeError:
            raise TurnkeepError("INVALID_INPUT", f"file is not valid UTF-8: {p}")
        all_candidates.extend(c)
        skipped_unmapped += s
    if len(all_candidates) > MAX_CANDIDATES:
        raise TurnkeepError("INVALID_INPUT", f"too many candidates (max {MAX_CANDIDATES})")
    # All states, not just active: a key already in conflict must stay for
    # adjudication and must not gain a third candidate by auto-write.
    existing = {}
    for row in db.execute("SELECT key, value_json, state FROM memories WHERE scope_id=?", (scope_id,)):
        import json as _json
        existing.setdefault(row[0], []).append({"value": _json.loads(row[1]), "state": row[2]})
    report = {"format": "markdown", "scope": scope_id, "dry_run": dry_run,
              "files": len(files), "skipped_unmapped": skipped_unmapped,
              "written": [], "duplicates": [], "conflicts": [], "excluded_secrets": []}
    for c in all_candidates:
        if any(k in c["value"].lower() for k in _SECRET_LIKE):
            report["excluded_secrets"].append(c["key"])
            continue
        if c["key"] in existing:
            values = {e["value"] for e in existing[c["key"]]}
            if values == {c["value"]}:
                report["duplicates"].append(c["key"])
            else:
                report["conflicts"].append({"key": c["key"],
                                           "existing": sorted(values, key=str),
                                           "incoming": c["value"]})
            continue
        if dry_run:
            report.setdefault("planned", []).append(c["key"])
            continue
        rec = remember(db, scope_id, c["key"], c["value"], kind=c["kind"],
                       source_refs=[c["source"]])
        # P1-R2-02: persist the computed provenance. record_provenance
        # has only (origin_id, origin_scope) columns, so the file,
        # heading and content hash are packed into origin_id and the
        # importer is named in origin_scope. The source dict is NOT
        # changed — that would change its content_digest.
        record_provenance(
            db, scope_id, rec["id"],
            f"md:{c['provenance']['file']}#{_slug(c['provenance']['heading'])}:{c['provenance']['content_hash']}",
            "markdown")
        report["written"].append(rec["id"])
        existing.setdefault(c["key"], []).append({"value": c["value"], "state": "active"})
    return report
