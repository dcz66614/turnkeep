"""Scripted J01 demo on the PRODUCT core (T019). Clearly labelled:
SCRIPTED_SIMULATION_NO_MODEL_CALLS — no model is involved.

Journey J01: agent A reads a snapshot and produces a result; agent B
corrects the requirement; A's stale finish is rejected; A resumes, reads
the diff, recomputes and finishes; an identical replay returns the receipt.
"""
from __future__ import annotations
import tempfile
from pathlib import Path

from .errors import TurnkeepError
from .memory import remember
from .corrections import correct, retract
from .runs import begin
from .commits import finish
from .recovery import resume
from .scopes import register_scope
from .storage import open_db

SRC = [{"id": "demo:user:1", "type": "user_statement"}]


FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "demo"


def _load_csv(path):
    import csv
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _load_jsonl(path):
    import json
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def _scenario_export_format(db, scope):
    """A reads CSV requirements; B changes the format via JSON Lines;
    A's old finish is rejected; A recomputes and commits."""
    events = []
    recs = {}
    for row in _load_csv(FIXTURES / "export-format" / "requirements.csv"):
        r = remember(db, scope, row["key"], row["value"], kind=row["kind"],
                     source_refs=[{"id": row["source_id"], "type": row["source_type"]}])
        recs[row["key"]] = r
        events.append({"step": "remember", "key": row["key"], "value": row["value"],
                       "state": r["state"]})
    run_a = begin(db, scope, "produce export manifest", actor_binding="demo-a")
    manifest = remember(db, scope, "export.manifest.v1", "manifest for zip",
                        kind="decision", source_refs=SRC,
                        parent_ids=[recs["export.format"]["id"]], actor_binding="demo-a")
    events.append({"step": "begin", "task": "produce export manifest", "actor": "demo-a"})
    for ch in _load_jsonl(FIXTURES / "export-format" / "changes.jsonl"):
        if ch["action"] == "correct":
            old = recs[ch["key"]]
            new = correct(db, scope, old["id"], old["revision"], ch["new_value"],
                          source_refs=[ch["source"]], actor_binding="demo-b")
            recs[ch["key"]] = new
            events.append({"step": "correct", "key": ch["key"], "value": ch["new_value"],
                           "state": new["state"], "actor": "demo-b", "note": ch.get("note", "")})
    try:
        finish(db, scope, run_a["run_id"], {"key": "export.manifest", "value": "manifest for zip"},
               actor_binding="demo-a")
        raise AssertionError("The stale result should have been rejected")
    except TurnkeepError as error:
        if error.code != "STALE_SNAPSHOT":
            raise
        rejection = error.as_dict()
        events.append({"step": "stale_finish_rejected", "code": "STALE_SNAPSHOT",
                       "changed_keys": sorted(rejection["details"]["changed_keys"])})
    inspection = resume(db, scope, run_a["run_id"])
    events.append({"step": "resume", "changed_keys": sorted(inspection["changed_keys"])})
    run_b = begin(db, scope, "recompute export manifest", actor_binding="demo-a")
    receipt = finish(db, scope, run_b["run_id"],
                     {"key": "export.manifest", "value": "manifest for tar"},
                     parent_ids=[recs["export.format"]["id"]], actor_binding="demo-a")
    replay = finish(db, scope, run_b["run_id"],
                    {"key": "export.manifest", "value": "manifest for tar"},
                    parent_ids=[recs["export.format"]["id"]], actor_binding="demo-a")
    events.append({"step": "finish", "key": "export.manifest", "value": "manifest for tar",
                   "state": "active"})
    return events, {
        "demonstration": "SCRIPTED_SIMULATION_NO_MODEL_CALLS",
        "scenario": "export-format",
        "steps": [
            "A reads CSV requirements (export.format=zip).",
            "B changes the format to tar via JSON Lines.",
            "A's old manifest finish is rejected (STALE_SNAPSHOT).",
            "A resumes, reads the diff, recomputes for tar and commits.",
            "An identical replay returns the original receipt.",
        ],
        "rejection": rejection,
        "changed_keys": sorted(inspection["changed_keys"]),
        "receipt": receipt,
        "idempotent_replay_identical": replay == receipt,
        "boundary": "Local SQLite correctness only — not MCP integration, model quality, or external-action safety.",
    }


def _scenario_research_correction(db, scope):
    """A research conclusion is corrected after its source is retracted:
    the retraction taints the conclusion; the conclusion is corrected to
    'unknown' with the retraction notice as its new source."""
    events = []
    recs = {}
    for row in _load_csv(FIXTURES / "research-correction" / "notes.csv"):
        r = remember(db, scope, row["key"], row["value"], kind=row["kind"],
                     source_refs=[{"id": row["source_id"], "type": row["source_type"]}])
        recs[row["key"]] = r
        events.append({"step": "remember", "key": row["key"], "value": row["value"],
                       "state": r["state"]})
    conclusion = remember(db, scope, "study.conclusion.v1", "caffeine improves recall",
                          kind="decision", source_refs=[{"id": "paper:1", "type": "imported_text"}],
                          parent_ids=[recs["study.evidence"]["id"]])
    events.append({"step": "remember", "key": "study.conclusion.v1",
                   "value": "caffeine improves recall", "state": conclusion["state"]})
    for ch in _load_jsonl(FIXTURES / "research-correction" / "retraction.jsonl"):
        if ch["action"] == "retract":
            old = recs[ch["key"]]
            out = retract(db, scope, old["id"], old["revision"], ch["reason"])
            events.append({"step": "retract", "key": ch["key"], "state": out["state"],
                           "reason": ch["reason"]})
        elif ch["action"] == "replace":
            from .lineage import check_usable
            usable_before = check_usable(db, scope, conclusion["id"])
            # A tainted record cannot be corrected in place; it is replaced
            # explicitly (remember + replaces_ids), keeping the history.
            new = remember(db, scope, ch["key"], ch["new_value"], kind="decision",
                           source_refs=[ch["source"]],
                           replaces_ids=[conclusion["id"]])
            events.append({"step": "replace", "key": ch["key"], "value": ch["new_value"],
                           "state": new["state"],
                           "conclusion_usable_before": usable_before})
    from .retrieval import search_by_key
    current = search_by_key(db, scope, "study.conclusion.v1")
    final = current[0] if current else {"value": "unknown", "state": "unknown"}
    events.append({"step": "final", "key": "study.conclusion.v1",
                   "value": final["value"], "state": final["state"]})
    return events, {
        "demonstration": "SCRIPTED_SIMULATION_NO_MODEL_CALLS",
        "scenario": "research-correction",
        "steps": [
            "Notes record a study conclusion and its evidence.",
            "The evidence paper is retracted; the conclusion becomes tainted.",
            "The conclusion is corrected to 'unknown' with the retraction notice as source.",
            "History is kept: the retraction is visible, nothing is silently erased.",
        ],
        "final_conclusion": final["value"],
        "final_state": final["state"],
        "boundary": "Local SQLite correctness only — not model quality or external fact-checking.",
    }


def run_demo(db_path=None, scenario="j01"):
    tmp = None
    if db_path is None:
        tmp = tempfile.TemporaryDirectory(prefix="turnkeep-demo-")
        db_path = Path(tmp.name) / "memory.sqlite3"
    db = open_db(db_path)
    register_scope(db, "demo", "Demo project")
    scope = "demo"
    if scenario in ("export-format", "research-correction"):
        fn = _scenario_export_format if scenario == "export-format" else _scenario_research_correction
        events, out = fn(db, scope)
        out["events"] = events
        db.close()
        if tmp is not None:
            tmp.cleanup()
        return out
    if scenario != "j01":
        raise TurnkeepError("INVALID_INPUT", f"unknown demo scenario {scenario!r}")
    requirement = remember(db, scope, "build.python_version", "3.11",
                           kind="requirement", source_refs=SRC)
    plan = remember(db, scope, "build.plan", "Build for Python 3.11",
                    kind="decision", source_refs=[{"id": "demo:plan", "type": "agent_inference"}],
                    parent_ids=[requirement["id"]])
    run_a = begin(db, scope, "Prepare a release from the current requirements")
    corrected = correct(db, scope, requirement["id"], 1, "3.12", source_refs=SRC)
    try:
        finish(db, scope, run_a["run_id"], {"key": "release", "value": "Python 3.11"})
        raise AssertionError("The stale result should have been rejected")
    except TurnkeepError as error:
        if error.code != "STALE_SNAPSHOT":
            raise
        rejection = error.as_dict()
    inspection = resume(db, scope, run_a["run_id"])
    run_b = begin(db, scope, "Recompute the release for the changed requirement")
    result = {"key": "release", "value": "Python 3.12"}
    receipt = finish(db, scope, run_b["run_id"], result)
    replay = finish(db, scope, run_b["run_id"], result)
    from .storage import row_or_404, dump_record
    out = {
        "demonstration": "SCRIPTED_SIMULATION_NO_MODEL_CALLS",
        "scenario": "j01",
        "events": [
            {"step": "remember", "key": "build.python_version", "value": "3.11", "state": "active"},
            {"step": "correct", "key": "build.python_version", "value": "3.12", "state": "active"},
            {"step": "stale_finish_rejected", "code": "STALE_SNAPSHOT",
             "changed_keys": sorted(rejection["details"]["changed_keys"])},
            {"step": "resume", "changed_keys": sorted(inspection["changed_keys"])},
            {"step": "finish", "key": "release", "value": "Python 3.12", "state": "active"},
        ],
        "steps": [
            "A reads a consistent project snapshot.",
            "B corrects the requirement; the old plan becomes tainted.",
            "A's stale result is rejected atomically.",
            "The changes are inspected and a new run computes a replacement result.",
            "The replacement commits once; an identical retry returns the original receipt.",
        ],
        "old_requirement": requirement["value"],
        "new_requirement": corrected["value"],
        "old_plan_state": dump_record(db, row_or_404(db, scope, plan["id"]))["state"],
        "impact": inspection["changed_keys"],
        "rejection": rejection,
        "changed_keys": inspection["changed_keys"],
        "receipt": receipt,
        "idempotent_replay_identical": replay == receipt,
        "boundary": "This demonstrates local SQLite correctness, not MCP integration, model quality, or external-action safety.",
    }
    db.close()
    if tmp is not None:
        tmp.cleanup()
    return out
