"""Shared trace builder for scripted baseline arms (T040)."""
import time


class Trace:
    def __init__(self, task_id, arm, seed):
        self.data = {"task_id": task_id, "arm": arm, "seed": seed,
                     "mode": "deterministic", "steps": [], "events": [],
                     "claimed_completed": False, "usage": None,
                     "error": None, "timeout": False}
        self._t0 = time.monotonic()

    def step(self, action, detail=""):
        self.data["steps"].append({"action": action, "detail": detail})

    def event(self, type_, **fields):
        self.data["events"].append({"type": type_, **fields})

    def finish(self):
        self.data["latency_ms"] = round((time.monotonic() - self._t0) * 1000, 1)
        return self.data
