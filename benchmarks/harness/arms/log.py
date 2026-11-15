"""B2 raw log (T040). Policy: an append-only events.log, one line per fact;
the agent greps the whole log and uses the LATEST entry for the key. Same
lexical retrieval budget as the Markdown arm; no hidden oracle."""
from pathlib import Path

from . import Trace


def _latest(lines, key):
    hits = [l for l in lines if f" {key} " in l or l.startswith(f"RETRACTED {key} ")]
    return hits[-1] if hits else None


def run(task, workdir, budget):
    t = Trace(task["id"], "log", task.get("seed", 0))
    workdir = Path(workdir)
    log = workdir / "events.log"
    lines = [f"2026-11-08 {f['key']} = {f['value']}" for f in task["initial"]]
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    t.step("write_initial", f"{len(lines)} facts to events.log")
    change = task["change"]
    if change["type"] == "correct":
        lines.append(f"2026-11-08 {change['key']} = {change['value']}")
    elif change["type"] == "retract":
        lines.append(f"2026-11-08 RETRACTED {change['key']} (was "
                     f"{next(f['value'] for f in task['initial'] if f['key'] == change['key'])})")
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    t.step("change_applied", f"{change['type']} {change['key']}")
    t.step("grep", "grep the whole log, latest entry wins")
    if "release manifest" in task["goal"]:
        line = _latest(log.read_text(encoding="utf-8").splitlines(), "build.python_version")
        value = line.split(" = ", 1)[1] if line and " = " in line else "unknown"
        t.event("use_fact", key="build.python_version", value=value)
        t.event("produce", key="release.manifest", value=f"Python {value}")
    else:
        line = _latest(log.read_text(encoding="utf-8").splitlines(), "report.deadline")
        if line and "RETRACTED" in line:
            t.event("produce", key="report.summary", value="deadline unknown — source retracted")
        else:
            value = line.split(" = ", 1)[1] if line and " = " in line else "unknown"
            t.event("use_fact", key="report.deadline", value=value)
            t.event("produce", key="report.summary", value=f"deadline {value}")
    t.data["claimed_completed"] = True
    return t.finish()
