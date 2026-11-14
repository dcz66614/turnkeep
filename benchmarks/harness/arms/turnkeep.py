"""T Turnkeep (T040). Policy: the real product via the SDK — remember the
initial facts, begin a run, apply the change mid-run (correct / retract),
finish (a stale finish is rejected with STALE_SNAPSHOT), resume, recompute
and finish. No oracle: parents come only from the observed facts."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from turnkeep.client import TurnkeepClient  # noqa: E402
from turnkeep.errors import TurnkeepError  # noqa: E402
from . import Trace  # noqa: E402

SRC = [{"id": "u:1", "type": "user_statement"}]


def run(task, workdir, budget):
    t = Trace(task["id"], "turnkeep", task.get("seed", 0))
    dbpath = Path(workdir) / "tk.sqlite3"
    with TurnkeepClient(dbpath, "bench") as c:
        ids = {}
        for f in task["initial"]:
            rec = c.remember(f["key"], f["value"], kind=f["kind"], source_refs=SRC)
            ids[f["key"]] = rec
        t.step("remember_initial", f"{len(ids)} facts remembered")
        run = c.begin(task["goal"])
        t.step("begin", run["run_id"])
        change = task["change"]
        rec = ids.get(change["key"])
        if change["type"] == "correct" and rec is not None:
            c.correct(rec["id"], rec["revision"], change["value"], source_refs=SRC)
            t.step("change_applied", f"correct {change['key']} -> {change['value']}")
        elif change["type"] == "retract" and rec is not None:
            c.retract(rec["id"], rec["revision"], "correction")
            t.step("change_applied", f"retract {change['key']}")
        else:
            # An unrelated key changes: the conservative contract still
            # rejects the first finish (A07 false block), so the arm must
            # resume and recompute — that redo cost is measured, not hidden.
            c.remember(change["key"], change["value"], kind="fact", source_refs=SRC)
            t.step("change_applied", f"irrelevant change to {change['key']}")
        # First finish attempt: stale when the change touched a read fact.
        goal = task["goal"]
        try:
            if "release manifest" in goal:
                found = c.recall("build.python_version")
                value = found[0]["value"] if found else "unknown"
                receipt = c.finish(run["run_id"], "release.manifest", f"Python {value}",
                                   idempotency_key=f"{task['id']}-1")
                t.event("use_fact", key="build.python_version", value=value)
                t.event("produce", key="release.manifest", value=f"Python {value}")
            else:
                found = c.recall("report.deadline")
                if found:
                    value = found[0]["value"]
                    t.event("use_fact", key="report.deadline", value=value)
                    c.finish(run["run_id"], "report.summary", f"deadline {value}",
                             idempotency_key=f"{task['id']}-1")
                    t.event("produce", key="report.summary", value=f"deadline {value}")
                else:
                    c.finish(run["run_id"], "report.summary",
                             "deadline unknown — source retracted",
                             idempotency_key=f"{task['id']}-1")
                    t.event("produce", key="report.summary",
                            value="deadline unknown — source retracted")
            t.step("finish", "first finish accepted")
        except TurnkeepError as e:
            if e.code == "STALE_SNAPSHOT":
                t.step("stale_finish_rejected", f"changed_keys={e.details.get('changed_keys')}")
                diff = c.resume(run["run_id"])
                t.step("resume", f"changed_keys={diff.get('changed_keys')}")
                run2 = c.begin("recompute " + task["goal"])
                if "release manifest" in goal:
                    found = c.recall("build.python_version")
                    value = found[0]["value"] if found else "unknown"
                    c.finish(run2["run_id"], "release.manifest", f"Python {value}",
                             idempotency_key=f"{task['id']}-2")
                    t.event("use_fact", key="build.python_version", value=value)
                    t.event("produce", key="release.manifest", value=f"Python {value}")
                else:
                    found = c.recall("report.deadline")
                    if found:
                        value = found[0]["value"]
                        t.event("use_fact", key="report.deadline", value=value)
                        c.finish(run2["run_id"], "report.summary", f"deadline {value}",
                                 idempotency_key=f"{task['id']}-2")
                        t.event("produce", key="report.summary", value=f"deadline {value}")
                    else:
                        c.finish(run2["run_id"], "report.summary",
                                 "deadline unknown — source retracted",
                                 idempotency_key=f"{task['id']}-2")
                        t.event("produce", key="report.summary",
                                value="deadline unknown — source retracted")
                t.step("finish", "recomputed finish accepted")
            else:
                t.event("contract_violation", code=e.code, detail=str(e))
                t.data["error"] = f"{e.code}: {e}"
    t.data["claimed_completed"] = True
    return t.finish()
