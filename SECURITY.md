# SECURITY.md — Turnkeep (working name), v0.1.0 / OFFLINE_RC

This is the security boundary statement for the offline release
candidate. It states what Turnkeep does and does **not** promise.
Passing the release checks (`scripts/release_checks.py --profile
license-and-secrets`) means "no real secret and no known severe
boundary hole was found by that scan" — it is **not** a claim that
the software is production-safe, penetration-tested, or suitable
for multi-user deployment.

## 1. Trust model — single trusted OS user, local only

- Turnkeep is a **local-first, single-user** tool. The threat model
  assumes one trusted OS user on one machine. It is **not**
  multi-tenant: there is no authentication of distinct users, no
  tenant isolation, and no per-user authorisation.
- Scope isolation (T008) and actor binding (T022) separate
  *projects* and *local sessions* for one trusted user. They are
  **not** a security boundary against the same OS user reading the
  SQLite database file directly. Anyone who can read the database
  file can read its contents.
- The local workbench HTTP API (T027) binds **127.0.0.1 only**
  (stdlib `http.server`, per-process session token, Origin/Host
  checks, 1 MiB body cap, no wildcard CORS, GET never mutates).
  It never falls back to `0.0.0.0`. It must not be exposed to a
  LAN or the internet, and there is no TLS: loopback is the only
  transport protection. The session token is printed on stdout by
  `turnkeep serve`; anyone on the same machine can see it — that
  is acceptable only under the single-trusted-user model.
- The MCP server (T021) runs over stdio with the official MCP
  Python SDK; one server instance is bound to one scope from local
  configuration. Request bodies cannot override the bound actor or
  scope. Tool results and imported text are **untrusted data**:
  they are stored as data, never spliced into system/developer
  prompts, and imported records keep the `imported_text` source
  kind (T036/T037). Turnkeep cannot stop an agent from bypassing
  it and writing files or calling other tools directly — the
  promise covers only the controlled API.
- There is **no external transaction guarantee**: a receipt
  (`guarantee: "local_sqlite_commit_only"`) proves only a local
  SQLite commit. It does not make an email, a deployment, a
  payment or any other external action atomic.

## 2. What the release scan does and does not do

`scripts/release_checks.py --profile license-and-secrets`
(release_checks 1.0.0, stdlib only) really scans the working tree
(excluding `.venv/`, `.git/`, `__pycache__/`, `.tmp/`,
`ui/node_modules/`, `ui/dist/`, `.pytest_cache/`) for API-key /
token / private-key / password patterns, personal paths and real
user data; verifies the SBOM against the lock files; and checks the
loopback binding, brief redaction and untrusted-import handling.
Known test fixtures containing **fake** secrets
(`sk-live-123456`, `sk-live-999`, `SYNTHETIC-SECRET-*`,
`api_key=<redacted>`, the `hunter2` line in the malicious-import
fixture) are allowlisted by exact file + value in the script and
reported as whitelisted, not silently ignored.

Limitations (read before relying on a PASS):

- Pattern scanning cannot find secrets in a form it does not
  recognise, in binary files, or in data the user later writes
  into a Turnkeep database. A PASS is about this repository at
  this commit, not about your data.
- **No dependency vulnerability scan was performed in T045**
  (no `pip-audit` / `npm audit` run is recorded). Pinned
  versions are listed in `release/SBOM.json`; known CVEs in those
  versions were not assessed. Run a vulnerability scan before
  any public release (next action for T046/T047).
- No dynamic penetration test, fuzzing, or external audit has
  been done. The checks are static scan + boundary unit tests
  (`tests/security/test_release_boundary.py`).

## 3. Secrets and logging

- API keys must never be stored as memory values, log fragments
  or export fields. Briefs and handoffs redact secret-like values
  (`src/turnkeep/briefs.py: redact()`), exports exclude
  secret-like keys (`api_key` / `password` / `token` / `secret`,
  `src/turnkeep/portability.py`), and the Markdown importer
  applies the same exclusion. Diagnostic exports are redacted by
  default.
- If you paste a real secret into Turnkeep by mistake, treat it
  as compromised: rotate it at the issuer. Erasing it from
  Turnkeep does not un-leak it.

## 4. Data erasure — what `retract` and `erase` do and do not do

- `retract` is a **logical** state change: the record stops being
  returned by default recall, but its payload remains in history.
  `retract` is **not** erasure.
- Payload erasure (T038) covers the controlled live database,
  full-text index, summaries and caches for the selected records,
  and leaves a tombstone that contains no original text. The
  erasure receipt never copies the deleted content.
- **Residuals that erasure cannot remove** (see
  `docs/retention.md` and T038 evidence):
  - backups you made before erasure (including `turnkeep backup`
    copies and any filesystem snapshot) still contain the data —
    erasing the live database does **not** erase your backups,
    and restoring a pre-erasure backup **revives** the erased
    payload. Delete or re-create backups after an erasure if
    that matters.
  - exports, diagnostic bundles, or copies you sent elsewhere;
    history held by an MCP client or another agent; logs you
    copied out of the machine.
  - SQLite WAL / freelist pages: maintenance (checkpoint /
    VACUUM) is best-effort only. Turnkeep cannot prove that no
    trace remains recoverable from SSD / filesystem slack.
  - Databases erased by a pre-fix release on a default-OFF SQLite
    build may still hold a deleted verification key in the live
    file: run `turnkeep purge-residue` once on such a database
    (ON + VACUUM + checkpoint + probe verification; see
    docs/KNOWN_LIMITATIONS.md §28). Current releases enforce
    `PRAGMA secure_delete=ON` on every open and refuse an erase
    when it is not enabled (P0-R15-01).
- Expiry (`expires_at`) removes records from the current view
  and propagates taint; it is a validity mechanism, not a
  certified deletion.

## 5. Release boundaries that are NOT security vulnerabilities

These are real, recorded limits of the OFFLINE_RC. They affect
what you may claim about the release; they are not holes an
attacker can exploit, and fixing them is out of scope for T045:

- **Performance gate FAIL (T043, measured, thresholds not
  lowered):** finish p95 7,760 ms at 10k records (threshold
  500 ms); recall p95 3,183 ms and finish p95 12,288 ms at 100k
  records (thresholds 1,000 ms / 500 ms). Causes are
  architectural and correctness-first by design: `begin()`
  snapshots all active records, `recall()` loads the whole table
  and filters in Python. Cold start 159 ms PASS. A slow local
  operation is a denial-of-service consideration only under a
  multi-user model this release does not claim.
- **Windows / macOS NOT_RUN (T043):** only Linux was really
  installed and journey-tested on this VM. The CI workflow file
  exists but no Windows/macOS run has executed. Do not claim
  cross-platform support.
- **Real-model evaluation VERIFIED (T042) and two-client live
  handoff VERIFIED (T026):** T042 completed two independent clean
  live runs (Codex API 354/432, agy 352/432), finding MIXED
  (turnkeep arm 0% stale, completion below the file arms; Sam
  2026-11-10 ~09:08 GMT decision; the contaminated pilot is never
  quoted). T026 is VERIFIED on the hybrid substitution accepted
  by Sam (Codex CLI + agy MCP; agy reverse leg verified, commit
  f9e2786); the strict live-clients gate itself is still NOT_RUN.
  See DELIVERY_MANIFEST.json / FINAL_HANDOFF.md for the full
  disclosed limits.

## 6. Known risks (severity + exploit precondition)

| Severity | Risk | Precondition to matter |
|---|---|---|
| Medium | Lock hygiene gap: `requirements/ui.lock` is empty/stale and `pyproject.toml` optional deps are empty while real deps are pinned elsewhere (see NOTICE §4). | Rebuilding from `ui.lock` / `pyproject.toml` alone yields different or missing dependencies. Not a vulnerability; SBOM itself is accurate. |
| Low | Machine home-directory paths appear as `cwd` records in `evidence/` and `state/` working files, and as a hardcoded subprocess `cwd` in two pre-existing tests (`tests/core/test_finish_races.py`, `tests/integration/test_concurrency.py`); a synthetic Windows fixture path uses a placeholder username in `tests/platform/test_paths.py`. | Only if T046 packaging ships `evidence/` or `state/`, or if the test paths are read as real user data. Follow-up (not T045-owned paths): make the test `cwd` repo-root-relative. |
| Low | `turnkeep serve` prints the session token on stdout. | Same-machine observer only; acceptable under the single-trusted-user model, unacceptable if the model ever changes. |
| Low | No dependency vulnerability scan performed (see §2). | A pinned dependency has a known CVE; unassessed until a scan runs. |
| Info | Perf FAIL + Windows/macOS NOT_RUN (see §5). | Claiming performance or cross-platform support the evidence does not support. |

No severe (High/Critical) known boundary vulnerability was found
unhandled in T045. No real secret was found in the repository.
If a severe issue is found later, it blocks promotion of the
release candidate until fixed or recorded as a blocker — it must
not be hidden by rewording.

## 7. Reporting a vulnerability

There is no dedicated `security@` address yet. Until the
repository is published, report a suspected vulnerability to the
project maintainer (Sam / Muse) through the project channel, or —
once the repository is public — by opening a GitHub issue in the
project repository marked as a security report. Include the
version/commit, the exact command or API call, and what you
observed. **Do not include real user data, tokens, private keys,
or database contents** in the report; use synthetic values.
Do not publish exploit details publicly before the maintainer has
had a chance to fix the issue.

## 8. Licence status

Own code: MIT (`LICENSE`). Dependency licences: see
`release/SBOM.json` and `NOTICE`. This repository is
source-available as an offline release candidate; it is not yet a
published open-source project, and no "open source" claim should
be made on the strength of the MIT file alone until the repository
is actually published (see NOTICE §3).
