# DRAFT — DO NOT SEND. Local reply templates only (T044).

These are draft replies for GitHub issues / discussions. They have
not been posted, and nothing in this repository posts or messages on
your behalf. Adapt each reply to the actual report; never paste real
user data, tokens, or internal logs into a reply.

---

## 1. "Does it work with Claude Code / Codex?"

> Adapter templates for Claude Code and Codex are in the repo
> (`adapters/claude-code/.mcp.json`, `adapters/codex/config.toml`;
> setup in `docs/integrations/`), and they pass the config check
> (`python scripts/check_adapter.py --mode config`). But a live
> handover between two real clients has **not run yet** (tracked as
> T026) — the `claude` CLI is not installed on our verification
> machine and no Codex entry is installed. Two SDK test processes
> passing is protocol evidence, not a live handover. If you try it,
> please share your client versions + tool-call logs; the manual
> recipe is in `docs/COMPATIBILITY.md`.

## 2. "Is it faster / better than <neighbour>?"

> We do not have a live comparison. The deterministic smoke harness
> (12 scripted attempts, no model) is not a model result, and the live
> Gate C evaluation has not run (T042: no model selected, no budget,
> no endpoint). The honest comparison page is
> `docs/comparison.md` — it lists what each neighbour is strong at
> from their own docs. If you run Turnkeep and a neighbour on the
> same task, please share the frozen protocol inputs
> (`benchmarks/PROTOCOL.md`) so the result is reproducible.

## 3. "It is slow on my project"

> Known limitation, and we measure it rather than hide it: finish
> p95 is 7,760 ms at 10k records and 12,288 ms at 100k; recall p95 is
> 3,183 ms at 100k (thresholds: finish 500 ms, recall 1,000 ms).
> Causes: `begin()` snapshots all active records and `recall()`
> scans the whole table in Python. The fix (read-set snapshots +
> indexed recall) is not done yet. Workaround for now: keep scopes
> small or split a project into several scopes. Full numbers:
> `evidence/platforms/perf.json`, summary in
> `docs/KNOWN_LIMITATIONS.md` §4.

## 4. "Does it run on Windows / macOS?"

> Not verified yet. Installers exist (`scripts/install.sh`,
> `scripts/install.ps1`) and there is a CI workflow, but no run has
> executed on Windows or macOS (T043). Only Linux is verified. If you
> try the installer there, please report the `doctor --json` output
> and the exact failing command.

## 5. "My finish was rejected with STALE_SNAPSHOT"

> That is the product working as designed: the scope changed after
> your run read its snapshot, so the old result is not silently
> applied. Run `resume` on the run id to see `changed_keys` and the
> diff, re-read the affected work, start a new run with `begin`, and
> finish from there. Do not retry the same finish — it will fail the
> same way. Details: `docs/quickstart.md` §3 and
> `docs/troubleshooting.md`.

## 6. "I erased a record — is it really gone?"

> From the live database: yes — the payload is replaced with a
> tombstone, derived receipts are scrubbed, and the live file is
> purged (WAL checkpoint + VACUUM), verified by scanning every text
> column. What erasure cannot do: delete backups, exports, old DB
> copies, or another client's copy of a record it already read. Run
> `turnkeep residuals` to list them, and delete those copies yourself.
> Restoring a pre-erasure backup resurrects the payload — that is
> expected point-in-time behaviour. Details: `docs/retention.md`.
> If erasure returned `PARTIAL`, the payload is also in the
> append-only event history and was left untouched on purpose; export
> the scope to a fresh database and delete the old one.

## 7. "Can I use it for multiple users / as a service?"

> No — this release is for a single trusted OS user on one machine.
> Scope is a correctness boundary, not a security boundary against
> another local user who can read the database file, and there is no
> multi-tenancy or user authentication beyond the loopback workbench
> token. Do not expose it as a shared service.
