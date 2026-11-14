import sqlite3
import traceback
from turnkeep import commits, storage

db = sqlite3.connect("tk.sqlite3")
run = storage.get_run(db, "bench", "f5e155cb9477471b82ac588d07e0ca0f")
print("Run:", run)
try:
    commits.finish(db, "bench", "f5e155cb9477471b82ac588d07e0ca0f", {"key": "build.command", "value": "npm run build"}, idempotency_key="conf-f03-changed-1")
except Exception as e:
    traceback.print_exc()
