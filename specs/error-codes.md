# Error codes (frozen, T003 — contract v0.1)

| Code | Meaning | When |
|---|---|---|
| INVALID_INPUT | malformed / missing required field, bad kind/source, invalid parent shape | any op |
| VERSION_REQUIRED | mutation of an existing record without expected_revision | remember (append source/parent), correct, retract, resolve |
| VERSION_CONFLICT | expected_revision does not match current revision | correct, retract, resolve |
| CONFLICT_REQUIRES_RESOLVE | correct() called on a conflicted record | correct |
| CANDIDATE_SET_CHANGED | resolve candidate id/revision set differs from stored set | resolve_conflict |
| REPLACEMENT_REQUIRED | re-remember after only-invalid history without replaces_ids | remember |
| NOT_FOUND | id / run_id unknown | any by-id op |
| SCOPE_MISMATCH | parent / record in a different scope | remember, finish |
| INVALID_PARENT | missing / unavailable / conflicted parent, self-loop | remember, finish |
| CYCLE_DETECTED | parent graph would cycle (starter internal name CYCLIC_DEPENDENCY maps here) | remember |
| STALE_SNAPSHOT | run epoch != current scope epoch at finish | finish |
| IDEMPOTENCY_MISMATCH | same Idempotency-Key, different request digest | finish / any idempotent op |
| FORBIDDEN | actor mismatch / transport principal mismatch (starter internal ACTOR_MISMATCH maps here) | any op |
| STORE_BUSY | SQLite busy / lock timeout | any op |
| UNSUPPORTED | operation not in v0.1 scope (e.g. remote multi-tenant) | any op |

Notes: `active` ≠ truth, `retract` ≠ `erase`, receipt ≠ external atomicity.
Starter internal names map per specs/ADAPTER_MAPPING.md; external clients see
only the codes above. Full payload fixtures: tests/contract/fixtures/.
