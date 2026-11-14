# Migration — Markdown and legacy memory (T037)

Turnkeep imports Markdown conservatively: it parses **structured items
only** (list items under a recognised section heading, plus task
checkboxes). Plain prose is never auto-upgraded to a verified fact.

## What is imported

| Source | Kind | Rule |
| --- | --- | --- |
| `## Decisions` / `## 决定` list items | `decision` | one candidate per item |
| `## Goals` / `## 目标`, `## Requirements` / `## 需求` | `requirement` | one candidate per item |
| `## Attempts` / `## 尝试`, `## Experiments` / `## 实验` | `attempt` | one candidate per item |
| `## TODO` / `## 待办`, `## Tasks` / `## 任务` | `requirement` | `- [ ]` items; `- [x]` items become `attempt` |
| `## Procedures` / `## 流程`, `## Handoff` / `## 交接` | `procedure` / `handoff` | one candidate per item |
| Any other heading | — | skipped, counted as `skipped_unmapped` |
| Code fences, images, bare links | — | skipped, never parsed as items |

Every candidate carries its provenance: file path, heading, content hash and
read time. Sources are always `imported_text`. Keys are stable suggestions:
`md.<file>.<heading>.<sha256(item)[:8]>`.

## Write rules

- Dry-run first: `turnkeep import --format markdown --dry-run <path>` writes
  nothing and lists planned / duplicate / conflict / excluded keys.
- Only candidates whose key is absent are written, through the normal
  `remember()` path (all core validation applies).
- Same key + same value is a duplicate (skipped). Same key + different
  value is a conflict — reported, left for adjudication, never auto-merged.
- Secret-like values (api_key / password / token / secret) are excluded and
  listed by key only.
- Imported text is data, not instructions or authority: nothing in a file is
  executed, and no field in a file can rebind identity or grant authority.

## Boundaries

Max 1 MiB per file, max 200 `.md` files per directory, max 500 candidates.
Symlinks are refused. Hidden files/dirs are skipped. Non-UTF-8 files are
rejected, not guessed. A directory with no `.md` files imports zero
candidates (exit 0, no DB change).

## Adapter capability table

| System / format | Status | Evidence |
| --- | --- | --- |
| Markdown (AGENTS.md / CLAUDE.md style, sections above) | Implemented | `tests/data/test_markdown_import.py`, fixtures in `fixtures/imports/markdown-sample/` (SYNTHETIC — structure modelled on the public convention, not an export from any real system) |
| Turnkeep v1 export package (JSONL + manifest) | Implemented | T036, `tests/data/test_roundtrip.py` |
| Obsidian vault markdown, Notion export, other memory projects | **UNVERIFIED** | Not parsed, not claimed. Add an adapter only after parsing a real, lawfully obtained export and recording its differences here. |

If an upstream format changes, its adapter is marked experimental; the
generic JSONL export (T036) remains the stable path. No screen-scraping is
used as a substitute for a stable import.
