"""Root conftest: make the repo root importable so tests can import the
benchmarks harness (benchmarks/harness/...) alongside the installed package.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
