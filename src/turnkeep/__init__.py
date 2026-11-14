"""Turnkeep product package. Local-first project memory; no network,
model calls, or authentication in the core."""

from .errors import TurnkeepError
from .store import Store  # reference core, kept for parity (T004)

__all__ = ["Store", "TurnkeepError"]
__version__ = "0.1.0"
