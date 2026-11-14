# Information architecture (T029)

## Pages
- `/` Overview — projects, current decisions, rework queue, recent changes.
- `/current` Current view — active records with sources + revision.
- `/history` History — event timeline, ISO 8601, local timezone labelled.
- `/impact` Impact & Recovery — impact paths, conflict quarantine, resume/rebase.
- `/handoff` Handoff — brief for the next client/session (J02); shows NOT_RUN
  while no real client is connected.

## State machine (per view)
empty -> loading -> ready | error
ready -> conflict (when a record is conflicted)
ready -> stale (when a finish is rejected with STALE_SNAPSHOT)
stale -> recovery (resume) -> ready (recomputed finish committed)
Any view can show `scripted` when the data is a labelled simulation.

## What the UI never does
- Never shows a technical field (epoch, revision, hash) as the primary label.
- Never claims a record is verified when it is only `active` (available now).
- Never auto-closes a modal; every dialog has an explicit close/cancel.
- Never guesses a relationship the API did not return (schema change request
  goes to the coordinator instead).
