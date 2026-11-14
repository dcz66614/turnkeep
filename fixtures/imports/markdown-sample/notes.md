# Project notes (SYNTHETIC fixture — structure modelled on the public
# CLAUDE.md / AGENTS.md convention; not an export from any real system)

## Decisions
- Use PostgreSQL for the shared store, not SQLite, for the multi-user service.
- Keep the public API versioned at /api/v1.

## Goals
- Ship the export format before the end of the quarter.
- Reduce p95 latency below 200ms.

## TODO
- [ ] Write the migration guide for existing users.
- [x] Add the health endpoint.

## Attempts
- Tried running the worker on a single thread; throughput was too low.

## Notes
This is plain prose about the project and must never become a verified fact
on its own. It mentions the word decision in passing but lives under an
unmapped heading, so the importer must skip it.

- this bullet lives under an unmapped heading and must be skipped too

```python
# code fences are never parsed as memory items
- Use SQLite for everything.
```

## Procedures
- Deploy by tagging main and letting CI publish the release.
