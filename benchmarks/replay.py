#!/usr/bin/env python3
"""Change replay (T041).

  python benchmarks/replay.py --suite core-change --output evidence/replay [--mutation no-taint|silent-overwrite]

Replays a frozen scenario suite against the REAL core (fresh temp DB per
scenario), records the raw event trace, and checks every observation.
A scenario passes only when all its observations hold and no forbidden
behaviour occurred. Failures are reported per scenario with an explanation
and a recovery count (resumes/rebases used) — never silently dropped.

--mutation runs the same scenarios against a deliberately broken engine
(see benchmarks/harness/mutations.py) to prove the replay distinguishes a
working implementation from a broken one. Mutation runs are labelled as
such and never count as product evidence.

Replay fixes external events and tool results; it does NOT claim replaying
LLM sampling is byte-identical — model inputs/outputs are records, not
regenerated. Coarse-epoch false refusals (an irrelevant update rejecting a
stale finish) are reported separately as `false_refusals`, not as model
failures and not hidden.
Exit 0 = the replay ran (scenarios may fail — that is a result); 1 = the
harness itself failed.
"""
import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "benchmarks" / "harness"))

from turnkeep.storage import open_db  # noqa: E402
from turnkeep.scopes import register_scope  # noqa: E402
from turnkeep.memory import remember  # noqa: E402
from turnkeep.corrections import correct, retract, resolve_conflict  # noqa: E402
from turnkeep.runs import begin  # noqa: E402
from turnkeep.commits import finish  # noqa: E402
from turnkeep.recovery import resume, rebase  # noqa: E402
from turnkeep.retrieval import recall, search_by_key  # noqa: E402
from turnkeep.storage import usable, dump_record  # noqa: E402
from turnkeep.retention import set_expiry, sweep_expired  # noqa: E402
from turnkeep.erasure import erase_record  # noqa: E402
from turnkeep.errors import TurnkeepError  # noqa: E402

SRC = [{"id": "u:1", "type": "user_statement"}]


class Engine:
    """Real core, wrapped so mutations can substitute one rule."""

    def __init__(self, db, scope):
        self.db, self.scope = db, scope
        self.runs = {}
        self.recoveries = 0

    def remember(self, key, value, kind=None, parent_keys=(), scope=None):
        scope = scope or self.scope
        parents = [self._id(k, scope) for k in parent_keys]
        if kind is None:
            # One key keeps one declared kind: inherit it when the key exists.
            existing = search_by_key(self.db, scope, key, include_history=True)
            kind = existing[-1]["kind"] if existing else "fact"
        rec = remember(self.db, scope, key, value, kind=kind,
                       source_refs=SRC, parent_ids=parents)
        return {"ok": True, "id": rec["id"], "revision": rec["revision"], "state": rec["state"]}

    def correct(self, key, value, expected_revision=None, scope=None, which="last"):
        scope = scope or self.scope
        recs = search_by_key(self.db, scope, key, include_history=True)
        rec = recs[0] if which == "first" else recs[-1]
        rev = expected_revision if expected_revision is not None else rec["revision"]
        out = correct(self.db, scope, rec["id"], rev, value, source_refs=SRC)
        return {"ok": True, "id": out["id"], "revision": out["revision"]}

    def retract(self, key, reason="correction", scope=None):
        scope = scope or self.scope
        rec = search_by_key(self.db, scope, key, include_history=True)[-1]
        out = retract(self.db, scope, rec["id"], rec["revision"], reason)
        return {"ok": True, "id": out["id"]}

    def resolve(self, key, value, partial=False, scope=None):
        from turnkeep.memory import list_conflicts
        scope = scope or self.scope
        cands = [{"id": c["id"], "revision": c["revision"]}
                 for c in list_conflicts(self.db, scope, key)]
        if partial:
            cands = cands[:1]
        out = resolve_conflict(self.db, scope, key, cands, value, source_refs=SRC)
        return {"ok": True, "id": out["id"]}

    def begin(self, task, name):
        run = begin(self.db, self.scope, task)
        self.runs[name] = run["run_id"]
        return {"ok": True, "run_id": run["run_id"]}

    def finish(self, name, key, value, idempotency_key=""):
        return {"ok": True, "receipt": finish(
            self.db, self.scope, self.runs[name], {"key": key, "value": value},
            idempotency_key=idempotency_key)}

    def resume(self, name):
        self.recoveries += 1
        return {"ok": True, "diff": resume(self.db, self.scope, self.runs[name])}

    def rebase(self, name):
        self.recoveries += 1
        return {"ok": True, "diff": rebase(self.db, self.scope, self.runs[name])}

    def erase(self, key, reason="cleanup", scope=None):
        scope = scope or self.scope
        rec = search_by_key(self.db, scope, key, include_history=True)[-1]
        return erase_record(self.db, scope, rec["id"], rec["revision"], reason)

    def expire_set(self, key, valid_until, scope=None):
        scope = scope or self.scope
        rec = search_by_key(self.db, scope, key, include_history=True)[-1]
        out = set_expiry(self.db, scope, rec["id"], valid_until, rec["revision"])
        return {"ok": True, "id": out["id"]}

    def sweep(self, now):
        return sweep_expired(self.db, self.scope, now=now)

    def _id(self, key, scope=None):
        scope = scope or self.scope
        recs = search_by_key(self.db, scope, key, include_history=True)
        if not recs:
            raise TurnkeepError("NOT_FOUND", f"no record for key {key}")
        return recs[-1]["id"]


def _run_event(engine, ev):
    op = ev["op"]
    try:
        if op == "remember":
            # kind=None lets the engine inherit the key's declared kind.
            return engine.remember(ev["key"], ev["value"], ev.get("kind"),
                                   ev.get("parent_keys", ()), scope=ev.get("scope"))
        if op == "correct":
            return engine.correct(ev["key"], ev["value"], ev.get("expected_revision"),
                                  scope=ev.get("scope"), which=ev.get("which", "last"))
        if op == "retract":
            return engine.retract(ev["key"], ev.get("reason", "correction"),
                                  scope=ev.get("scope"))
        if op == "resolve":
            return engine.resolve(ev["key"], ev["value"], scope=ev.get("scope"))
        if op == "resolve_partial":
            return engine.resolve(ev["key"], ev["value"], partial=True, scope=ev.get("scope"))
        if op == "begin":
            return engine.begin(ev["task"], ev["as"])
        if op == "finish":
            return engine.finish(ev["run"], ev["key"], ev["value"], ev.get("idempotency_key", ""))
        if op == "resume":
            return engine.resume(ev["run"])
        if op == "rebase":
            return engine.rebase(ev["run"])
        if op == "erase":
            return engine.erase(ev["key"], ev.get("reason", "cleanup"),
                                scope=ev.get("scope"))
        if op == "expire_set":
            return engine.expire_set(ev["key"], ev["valid_until"], scope=ev.get("scope"))
        if op == "sweep":
            return engine.sweep(ev["now"])
        raise ValueError(f"unknown op {op!r}")
    except TurnkeepError as e:
        return {"ok": False, "error_code": e.code, "error": str(e)}
    except Exception as e:  # noqa: BLE001 - a crashing implementation is a failed event, never a harness crash
        return {"ok": False, "error_code": type(e).__name__, "error": str(e)}


def _check(engine, obs, event_outcomes):
    check = obs["check"]
    db, scope = engine.db, obs.get("scope") or engine.scope
    if check == "recall_contains":
        rows = recall(db, scope, obs.get("query", obs["key"]))
        return any(r["key"] == obs["key"] and r["value"] == obs["value"] for r in rows)
    if check == "recall_excludes_key":
        rows = recall(db, scope, obs.get("query", obs["key"]))
        return not any(r["key"] == obs["key"] for r in rows)
    if check == "recall_excludes_value":
        rows = recall(db, scope, obs.get("query", ""), include_history=True)
        return not any(obs["value"] in str(r.get("value", "")) for r in rows)
    if check == "state_is":
        recs = search_by_key(db, scope, obs["key"], include_history=True)
        return bool(recs) and recs[-1]["state"] == obs["state"]
    if check == "usable":
        recs = search_by_key(db, scope, obs["key"], include_history=True)
        return bool(recs) and usable(db, scope, recs[-1]["id"]) == obs["usable"]
    if check == "event_ok":
        return bool(event_outcomes[obs["index"]].get("ok"))
    if check == "event_not_ok":
        return not event_outcomes[obs["index"]].get("ok")
    if check == "event_error":
        return event_outcomes[obs["index"]].get("error_code") == obs["code"]
    if check == "event_error_not":
        return event_outcomes[obs["index"]].get("error_code") != obs.get("code")
    raise ValueError(f"unknown check {check!r}")


def _export_import(engine, tmp, *, tamper=False):
    """Export the scope, then import into a fresh DB and swap the engine onto
    it (export_import) — or, when tampering, flip a byte first and expect the
    import to be refused (export_tamper_import)."""
    from turnkeep.portability import export_scope, import_package
    pkg = Path(tmp) / "pkg"
    export_scope(engine.db, engine.scope, pkg)
    if tamper:
        p = pkg / "records.jsonl"
        p.write_text(p.read_text().replace('"', "'", 1))
    fresh = open_db(Path(tmp) / "fresh.sqlite3")
    register_scope(fresh, engine.scope)
    try:
        result = import_package(fresh, pkg)
    except Exception as e:  # noqa: BLE001 - any import failure is a refused import
        fresh.close()
        code = e.code if isinstance(e, TurnkeepError) else type(e).__name__
        return {"ok": False, "error_code": code, "error": str(e)}
    if tamper:
        fresh.close()
        return {"ok": False, "error_code": "EXPECTED_REFUSAL_MISSING",
                "error": "tampered package was imported without refusal"}
    engine.db.close()
    engine.db = fresh
    return {"ok": True, "imported": result.get("imported")}


def replay_scenario(scenario, mutation=None):
    with tempfile.TemporaryDirectory(prefix="tk-replay-") as tmp:
        db = open_db(Path(tmp) / "r.sqlite3")
        register_scope(db, "replay")
        register_scope(db, "other")
        engine = Engine(db, "replay")
        if mutation:
            from mutations import apply_mutation
            engine = apply_mutation(mutation, engine)
        outcomes, event_outcomes, failures = [], [], []
        # Initial state.
        for item in scenario.get("initial", []):
            outcomes.append(_run_event(engine, {"op": "remember", **item}))
        for i, ev in enumerate(scenario.get("events", [])):
            if ev["op"] == "restart":
                db.close()
                db = open_db(Path(tmp) / "r.sqlite3")
                engine.db = db
                event_outcomes.append({"ok": True, "restarted": True})
                outcomes.append(event_outcomes[-1])
                continue
            if ev["op"] in ("export_import", "export_tamper_import"):
                event_outcomes.append(_export_import(engine, tmp, tamper=(ev["op"] == "export_tamper_import")))
                outcomes.append(event_outcomes[-1])
                # export_import swaps the engine onto the fresh DB.
                continue
            event_outcomes.append(_run_event(engine, ev))
            outcomes.append(event_outcomes[-1])
            for obs in scenario.get("observations", []):
                if obs.get("after") == i:
                    try:
                        ok = _check(engine, obs, event_outcomes)
                    except Exception as e:  # noqa: BLE001 - a broken check is a scenario failure
                        ok, err = False, f"{type(e).__name__}: {e}"
                    else:
                        err = ""
                    if not ok:
                        failures.append({"observation": obs, "error": err,
                                         "explanation": obs.get("explanation", "")})
        # Observations after the last event.
        n = len(scenario.get("events", []))
        for obs in scenario.get("observations", []):
            if obs.get("after", n) >= n:
                try:
                    ok = _check(engine, obs, event_outcomes)
                except Exception as e:  # noqa: BLE001
                    ok, err = False, f"{type(e).__name__}: {e}"
                else:
                    err = ""
                if not ok:
                    failures.append({"observation": obs, "error": err,
                                     "explanation": obs.get("explanation", "")})
        db.close()
        return {"id": scenario["id"], "title": scenario.get("title", ""),
                "family": scenario.get("family", ""),
                "status": "PASS" if not failures else "FAIL",
                "failures": failures, "recoveries": engine.recoveries,
                "events": len(scenario.get("events", [])),
                "false_refusal": scenario.get("false_refusal", False)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--suite", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--mutation", default=None, choices=["no-taint", "silent-overwrite"])
    args = ap.parse_args()
    suite_path = ROOT / "benchmarks" / "scenarios" / f"{args.suite}.json"
    if not suite_path.exists():
        print(f"NOT_RUN: no suite {args.suite!r}", file=sys.stderr)
        return 2
    suite = json.loads(suite_path.read_text(encoding="utf-8"))
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    results = [replay_scenario(s, mutation=args.mutation) for s in suite["scenarios"]]
    passed = sum(1 for r in results if r["status"] == "PASS")
    manifest = {"suite": args.suite, "version": suite.get("version", 1),
                "mutation": args.mutation or "none",
                "label": ("MUTATION RUN — broken-by-design engine, not product evidence"
                          if args.mutation else "SCRIPTED_REPLAY_NO_MODEL_CALLS"),
                "scenarios": len(results), "passed": passed, "failed": len(results) - passed,
                "false_refusals": sum(1 for s in suite["scenarios"] if s.get("false_refusal")),
                "false_refusal_note": "Scenarios flagged false_refusal demonstrate the conservative "
                                      "contract rejecting a finish on an unrelated change; they PASS "
                                      "when the rejection + recompute behave as specified, and are "
                                      "reported separately from model failures.",
                "results": results}
    (out / ("manifest-mutation.json" if args.mutation else "manifest.json")).write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in manifest.items() if k != "results"}, indent=2))
    for r in results:
        if r["status"] == "FAIL":
            print(f"FAIL {r['id']}: {r['failures']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
