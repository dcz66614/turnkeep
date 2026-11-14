#!/usr/bin/env python3
"""Doc profile checks (T029, T044).

--profile ux verifies docs/UX_SPEC.md covers all journeys J01–J05, the
four launch areas, every UI state, and the non-negotiables (no fake
recovery, scripted labelling, closable modals).

--profile release (T044) verifies the release documentation set: both
READMEs exist and carry the required honest disclosures (NOT_RUN gates,
perf FAIL numbers, OFFLINE_RC scope, name collision, receipt scope,
external atomicity), no un-evidenced claim phrases appear, no personal
paths / tokens leak into docs or drafts, announcement drafts are
marked draft-only, and scripts/test_documented_commands.py exists and
really executes commands (subprocess) instead of string-matching.

Exit 0 PASS, 1 FAIL, 2 NOT_RUN (spec file missing).
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

UX_REQUIREMENTS = {
    "journeys": ["J01", "J02", "J03", "J04", "J05"],
    "areas": ["项目总览", "当前视图", "历史", "影响与恢复"],
    "states": ["empty", "loading", "error", "conflict", "stale", "recovery", "scripted"],
    "phrases": ["SCRIPTED_SIMULATION_NO_MODEL_CALLS", "未验证", "可操作的下一步",
                "没有无法关闭的 modal", "active", "当前可用"],
}


def check_ux():
    spec = ROOT / "docs" / "UX_SPEC.md"
    if not spec.exists():
        print("NOT_RUN: docs/UX_SPEC.md missing", file=sys.stderr)
        return 2
    text = spec.read_text(encoding="utf-8")
    missing = []
    for group, items in UX_REQUIREMENTS.items():
        for item in items:
            if item not in text:
                missing.append(f"{group}:{item}")
    # fixtures must exist for every state
    fixtures = ROOT / "tests" / "ui" / "fixtures"
    for name in ("overview.json", "states.json"):
        if not (fixtures / name).exists():
            missing.append(f"fixture:{name}")
    # types must define the view models
    types = ROOT / "ui" / "src" / "types.ts"
    if not types.exists():
        missing.append("types:ui/src/types.ts")
    else:
        t = types.read_text(encoding="utf-8")
        for token in ("ViewState", "MemoryRecord", "StaleRejection", "HandoffBrief"):
            if token not in t:
                missing.append(f"types:{token}")
    if missing:
        print("FAIL: UX spec missing: " + ", ".join(missing), file=sys.stderr)
        return 1
    print("PASS: UX spec covers J01–J05, 4 areas, 7 states, fixtures + types")
    return 0


def check_release():
    missing = []
    required = [
        ROOT / "README.md",
        ROOT / "README.zh-CN.md",
        ROOT / "docs" / "KNOWN_LIMITATIONS.md",
        ROOT / "docs" / "quickstart.md",
        ROOT / "docs" / "troubleshooting.md",
        ROOT / "docs" / "comparison.md",
        ROOT / "examples" / "README.md",
        ROOT / "scripts" / "test_documented_commands.py",
        ROOT / "release" / "announcement-drafts" / "github-release.md",
        ROOT / "release" / "announcement-drafts" / "hacker-news.md",
        ROOT / "release" / "announcement-drafts" / "forum-post.md",
        ROOT / "release" / "announcement-drafts" / "issue-replies.md",
    ]
    for path in required:
        if not path.exists():
            missing.append(f"missing:{path.relative_to(ROOT)}")
    # Honest disclosures that must appear in BOTH readmes (T044 invariants).
    required_markers = [
        "T026", "T042", "NOT_RUN", "7,760", "3,183", "12,288",
        "OFFLINE_RC", "turnkeep.app", "NOT_SUPPORTED",
        "local_sqlite_commit_only", "Windows", "macOS",
    ]
    for name in ("README.md", "README.zh-CN.md"):
        path = ROOT / name
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for marker in required_markers:
            if marker not in text:
                missing.append(f"{name}:missing-marker:{marker}")
    # Un-evidenced claims that must NOT appear in readmes, the
    # limitations doc, or any announcement draft.
    banned = ["永不遗忘", "保证高星", "已实测省", "never forget",
              "guaranteed stars"]
    claim_files = [ROOT / "README.md", ROOT / "README.zh-CN.md",
                   ROOT / "docs" / "KNOWN_LIMITATIONS.md"]
    claim_files += sorted((ROOT / "release" / "announcement-drafts").glob("*.md"))
    for path in claim_files:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for phrase in banned:
            if phrase in text:
                missing.append(f"{path.relative_to(ROOT)}:banned:{phrase}")
    # No personal paths, account handles, or token-shaped material in
    # docs, examples readme, readmes, or drafts.
    leak_patterns = ["/home/", "dcz66614", "ghp_", "AKIA"]
    leak_files = [ROOT / "README.md", ROOT / "README.zh-CN.md",
                  ROOT / "examples" / "README.md"]
    leak_files += sorted((ROOT / "docs").rglob("*.md"))
    leak_files += sorted((ROOT / "release" / "announcement-drafts").glob("*.md"))
    for path in leak_files:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for pat in leak_patterns:
            if pat in text:
                missing.append(f"{path.relative_to(ROOT)}:leak:{pat}")
    # Drafts must be labelled draft-only and must not claim publication.
    drafts = sorted((ROOT / "release" / "announcement-drafts").glob("*.md"))
    for path in drafts:
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        if "DRAFT" not in text or "DO NOT" not in text:
            missing.append(f"{path.relative_to(ROOT)}:not-marked-draft")
    # Quickstart must reference the demo and doctor commands.
    qs = ROOT / "docs" / "quickstart.md"
    if qs.exists():
        text = qs.read_text(encoding="utf-8")
        for token in ("demo --scenario", "doctor"):
            if token not in text:
                missing.append(f"quickstart:missing:{token}")
    # The documented-commands test must really execute commands.
    tdc = ROOT / "scripts" / "test_documented_commands.py"
    if tdc.exists():
        text = tdc.read_text(encoding="utf-8")
        for token in ("--offline", "subprocess.run", "argparse"):
            if token not in text:
                missing.append(f"test_documented_commands:missing:{token}")
    if missing:
        print("FAIL: release docs missing: " + ", ".join(missing),
              file=sys.stderr)
        return 1
    print("PASS: release docs carry honest disclosures, no banned claims, "
          "no leaks, drafts marked draft-only")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True, choices=["ux", "release"])
    args = ap.parse_args()
    if args.profile == "release":
        return check_release()
    return check_ux()


if __name__ == "__main__":
    sys.exit(main())
