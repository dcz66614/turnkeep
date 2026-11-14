"""B0 no-memory (T040). Policy: the agent only has the task prompt, which
carries the ORIGINAL facts. A change made after the prompt was written is
invisible — the agent produces from the stale prompt. That is the honest
baseline: it is not deprived of the current task source, it simply has no
persistent memory to consult."""
from . import Trace


def run(task, workdir, budget):
    t = Trace(task["id"], "none", task.get("seed", 0))
    t.step("read_prompt", "initial facts as given in the task prompt")
    initial = {f["key"]: f["value"] for f in task["initial"]}
    goal = task["goal"]
    if "release manifest" in goal:
        value = initial.get("build.python_version", "unknown")
        t.event("use_fact", key="build.python_version", value=value)
        t.event("produce", key="release.manifest", value=f"Python {value}")
    else:
        value = initial.get("report.deadline", "unknown")
        t.event("use_fact", key="report.deadline", value=value)
        t.event("produce", key="report.summary", value=f"deadline {value}")
    t.data["claimed_completed"] = True
    return t.finish()
