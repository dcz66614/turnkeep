"""B1 Markdown (T040). Policy: a structured decisions.md, one dated line per
fact, appended (never silently overwritten); the agent greps the file and
uses the LATEST entry for the key. A RETRACTED line means the fact is gone.
The baseline keeps its normal search AND update ability — it is not
crippled to make Turnkeep look good."""
from pathlib import Path

from . import Trace

SRC = [{"id": "u:1", "type": "user_statement"}]


def _read_lines(path):
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def _latest(lines, key):
    hits = [l for l in lines if f" {key} " in l or l.startswith(f"RETRACTED {key} ")]
    return hits[-1] if hits else None


def run(task, workdir, budget):
    t = Trace(task["id"], "markdown", task.get("seed", 0))
    workdir = Path(workdir)
    md = workdir / "decisions.md"
    lines = [f"2026-11-08 {f['key']} = {f['value']}" for f in task["initial"]]
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    t.step("write_initial", f"{len(lines)} facts to decisions.md")
    # Environment change: appended as a new dated line (atomic rewrite).
    change = task["change"]
    if change["type"] == "correct":
        lines.append(f"2026-11-08 {change['key']} = {change['value']}")
    elif change["type"] == "retract":
        lines.append(f"2026-11-08 RETRACTED {change['key']} (was "
                     f"{next(f['value'] for f in task['initial'] if f['key'] == change['key'])})")
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    t.step("change_applied", f"{change['type']} {change['key']}")
    t.step("grep", f"grep {task['expected']['key']} in decisions.md")
    if "release manifest" in task["goal"]:
        line = _latest(_read_lines(md), "build.python_version")
        value = line.split(" = ", 1)[1] if line and " = " in line else "unknown"
        t.event("use_fact", key="build.python_version", value=value)
        t.event("produce", key="release.manifest", value=f"Python {value}")
    else:
        line = _latest(_read_lines(md), "report.deadline")
        if line and "RETRACTED" in line:
            t.event("produce", key="report.summary", value="deadline unknown — source retracted")
        else:
            value = line.split(" = ", 1)[1] if line and " = " in line else "unknown"
            t.event("use_fact", key="report.deadline", value=value)
            t.event("produce", key="report.summary", value=f"deadline {value}")
    t.data["claimed_completed"] = True
    return t.finish()
