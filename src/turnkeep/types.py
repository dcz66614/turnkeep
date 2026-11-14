"""Product types (T003). Frozen contract v0.1 — mirrors specs/public_api.schema.json.
Stdlib only. These are data shapes, not the store implementation (T007+)."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Optional

KINDS = ("requirement", "fact", "decision", "attempt", "procedure", "handoff")
SOURCE_TYPES = ("user_statement", "tool_observation", "agent_inference", "imported_text")
STATES = ("active", "conflicted", "superseded", "retracted", "tainted", "expired", "erased")
ERROR_CODES = ("INVALID_INPUT","VERSION_REQUIRED","VERSION_CONFLICT","CONFLICT_REQUIRES_RESOLVE",
    "CANDIDATE_SET_CHANGED","REPLACEMENT_REQUIRED","NOT_FOUND","SCOPE_MISMATCH","INVALID_PARENT",
    "CYCLE_DETECTED","STALE_SNAPSHOT","IDEMPOTENCY_MISMATCH","FORBIDDEN","STORE_BUSY","UNSUPPORTED")

@dataclass(frozen=True)
class Source:
    type: str
    ref: str
    def validate(self):
        if self.type not in SOURCE_TYPES: raise ValueError(f"INVALID_INPUT: source type {self.type!r}")
        if not self.ref: raise ValueError("INVALID_INPUT: source ref empty")

@dataclass
class RememberRequest:
    scope: str; key: str; value: str; kind: str
    source: Source; parent_ids: List[str] = field(default_factory=list)
    expected_revision: Optional[int] = None; replaces_ids: List[str] = field(default_factory=list)
    def validate(self):
        if not self.scope or not self.key: raise ValueError("INVALID_INPUT: scope/key required")
        if self.kind not in KINDS: raise ValueError(f"INVALID_INPUT: kind {self.kind!r}")
        self.source.validate()

@dataclass
class FinishRequest:
    run_id: str; result_key: str; result_value: str
    parent_ids: List[str] = field(default_factory=list)
    idempotency_key: str = ""
    def validate(self):
        if not self.run_id: raise ValueError("INVALID_INPUT: run_id required")
        if self.idempotency_key and not (8 <= len(self.idempotency_key) <= 128):
            raise ValueError("INVALID_INPUT: Idempotency-Key must be 8-128 chars")
