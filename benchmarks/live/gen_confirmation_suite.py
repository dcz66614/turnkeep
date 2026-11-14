#!/usr/bin/env python3
"""Generate the frozen live confirmation suite (T042).

12 families x 3 variants (stable / changed / changed-during-recovery) = 36 tasks.
Each task uses the same shape as the deterministic smoke suite so the live
scorer can reuse the same rules. The brief carries the ORIGINAL (possibly
stale) facts; the memory artifact (per arm) carries the CURRENT facts.

Variant semantics:
- stable:   no change anywhere; the brief is already current.
- changed:  one relevant change is applied to the memory before the run.
- recovery: change #1 is applied before phase A; change #2 is applied
  between phase A and a resumed phase B. Only the final artifact is scored.

This generator and the runner are written by the same agent (Muse), so the
suite is a process measure against accidental leakage, NOT a true independent
blind test. That limitation is recorded in the campaign manifest.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

FAMILIES = [
    {
        "family": "F01", "goal": "Produce the release manifest using the current Python version.",
        "brief": "You are preparing the release manifest for the Acme web service. The release checklist says the manifest must state the Python version the release was built with. The project brief you were given says the build uses Python 3.11. Write the manifest to output/manifest.txt as exactly one line: 'Python <version>'.",
        "initial": [{"key": "build.python_version", "kind": "requirement", "value": "3.11"}],
        "change": {"key": "build.python_version", "relevant": True, "type": "correct", "value": "3.12"},
        "change2": {"key": "build.python_version", "relevant": True, "type": "correct", "value": "3.13"},
        "deliverable": {"file": "output/manifest.txt", "key": "release.manifest"},
        "expected": {"stable": "Python 3.11", "changed": "Python 3.12", "recovery": "Python 3.13"},
        "must_not_use": {"stable": [], "changed": ["3.11"], "recovery": ["3.11", "3.12"]},
        "context_files": {"pyproject.toml": "[project]\nname = \"acme-web\"\nversion = \"0.3.0\"\n", "release-checklist.md": "# Release checklist\n\n1. Run the test suite.\n2. Write the release manifest to output/manifest.txt.\n3. The manifest states the Python version the release was built with.\n"},
    },
    {
        "family": "F02", "goal": "Migrate the client to the current user field.",
        "brief": "You are updating the Acme client to use the current user display field. The integration brief you were given says the field is user.name. Write the field name the client must use to output/field.txt, exactly one line, nothing else.",
        "initial": [{"key": "api.user_field", "kind": "requirement", "value": "user.name"}],
        "change": {"key": "api.user_field", "relevant": True, "type": "correct", "value": "user.display_name"},
        "change2": {"key": "api.user_field", "relevant": True, "type": "correct", "value": "user.legal_name"},
        "deliverable": {"file": "output/field.txt", "key": "api.user_field"},
        "expected": {"stable": "user.name", "changed": "user.display_name", "recovery": "user.legal_name"},
        "must_not_use": {"stable": [], "changed": ["user.name"], "recovery": ["user.name", "user.display_name"]},
        "context_files": {"client-notes.md": "# Client notes\n\nThe client reads the user display field from the API and shows it in the header.\n"},
    },
    {
        "family": "F03", "goal": "Record the current build command.",
        "brief": "You are maintaining the Acme runbook. The runbook must record the build command that currently works. The handover note you were given says the build command is 'npm run build-legacy'. Write the working build command to output/build.txt, exactly one line, nothing else.",
        "initial": [{"key": "build.command", "kind": "fact", "value": "npm run build-legacy"}],
        "change": {"key": "build.command", "relevant": True, "type": "correct", "value": "npm run build"},
        "change2": {"key": "build.command", "relevant": True, "type": "correct", "value": "npm run build:prod"},
        "deliverable": {"file": "output/build.txt", "key": "build.command"},
        "expected": {"stable": "npm run build-legacy", "changed": "npm run build", "recovery": "npm run build:prod"},
        # recovery must_not_use intentionally omits the intermediate "npm run build":
        # it is a substring of the expected "npm run build:prod", so substring
        # matching would false-positive on the correct answer.
        "must_not_use": {"stable": [], "changed": ["npm run build-legacy"], "recovery": ["npm run build-legacy"]},
        "context_files": {"runbook.md": "# Runbook\n\nBuild: see the recorded build command.\nDeploy: after a green build.\n"},
    },
    {
        "family": "F04", "goal": "Record the API URL the Acme-A service must call.",
        "brief": "You are configuring the Acme-A service. Two services run in parallel (Acme-A and Acme-B) and they have different API URLs. The brief you were given says Acme-A must call https://api-a.example.com/v1. Write the URL Acme-A must call to output/api-url.txt, exactly one line, nothing else. Do not mix in Acme-B's URL.",
        "initial": [{"key": "projA.api_url", "kind": "fact", "value": "https://api-a.example.com/v1"}, {"key": "projB.api_url", "kind": "fact", "value": "https://api-b.example.com/v1"}],
        "change": {"key": "projA.api_url", "relevant": True, "type": "correct", "value": "https://api-a.example.com/v2"},
        "change2": {"key": "projA.api_url", "relevant": True, "type": "correct", "value": "https://api-a.example.com/v3"},
        "deliverable": {"file": "output/api-url.txt", "key": "projA.api_url"},
        "expected": {"stable": "https://api-a.example.com/v1", "changed": "https://api-a.example.com/v2", "recovery": "https://api-a.example.com/v3"},
        "must_not_use": {"stable": [], "changed": ["https://api-a.example.com/v1"], "recovery": ["https://api-a.example.com/v1", "https://api-a.example.com/v2"]},
        "context_files": {"services.md": "# Services\n\nAcme-A and Acme-B run in parallel. Each has its own API URL; never copy one service's URL into the other's config.\n"},
    },
    {
        "family": "F05", "goal": "Write the summary citing only live evidence.",
        "brief": "You are writing the weekly report summary for the Acme web service. The report draft you were given says the deadline is Friday. The summary must state the deadline, or state that it is unknown if the source was retracted. Write the summary to output/summary.txt as exactly one line: 'deadline <value>' or 'deadline unknown — source retracted'.",
        "initial": [{"key": "report.deadline", "kind": "fact", "value": "Friday"}],
        "change": {"key": "report.deadline", "relevant": True, "type": "retract"},
        "change2": {"key": "report.deadline", "relevant": True, "type": "correct", "value": "Monday"},
        "deliverable": {"file": "output/summary.txt", "key": "report.summary"},
        "expected": {"stable": "deadline Friday", "changed": "deadline unknown — source retracted", "recovery": "deadline Monday"},
        "must_not_use": {"stable": [], "changed": ["Friday"], "recovery": ["Friday"]},
        "context_files": {"report-draft.md": "# Weekly report (draft)\n\nStatus: on track.\n"},
    },
    {
        "family": "F06", "goal": "Record the delivery format the client asked for.",
        "brief": "You are preparing the Acme delivery. The client brief you were given says the delivery format is PDF. The content is ready; only the format matters. Write the delivery format to output/format.txt, exactly one line, nothing else.",
        "initial": [{"key": "delivery.format", "kind": "requirement", "value": "PDF"}],
        "change": {"key": "delivery.format", "relevant": True, "type": "correct", "value": "Markdown"},
        "change2": {"key": "delivery.format", "relevant": True, "type": "correct", "value": "HTML"},
        "deliverable": {"file": "output/format.txt", "key": "delivery.format"},
        "expected": {"stable": "PDF", "changed": "Markdown", "recovery": "HTML"},
        "must_not_use": {"stable": [], "changed": ["PDF"], "recovery": ["PDF", "Markdown"]},
        "context_files": {"delivery-notes.md": "# Delivery notes\n\nThe deliverable content is final. The format is whatever the client currently asks for.\n"},
    },
    {
        "family": "F07", "goal": "Resume the interrupted release task and produce the manifest.",
        "brief": "A previous session started the Acme release task and stopped after recording the facts. Resume it. The brief you were given says the release targets Python 3.11. Produce output/manifest.txt as exactly one line 'Python <version>' using the current recorded facts. Do not re-ask for facts that are already recorded.",
        "initial": [{"key": "build.python_version", "kind": "requirement", "value": "3.11"}],
        "change": {"key": "build.python_version", "relevant": True, "type": "correct", "value": "3.12"},
        "change2": {"key": "build.python_version", "relevant": True, "type": "correct", "value": "3.13"},
        "deliverable": {"file": "output/manifest.txt", "key": "release.manifest"},
        "expected": {"stable": "Python 3.11", "changed": "Python 3.12", "recovery": "Python 3.13"},
        "must_not_use": {"stable": [], "changed": ["3.11"], "recovery": ["3.11", "3.12"]},
        "context_files": {"handoff-note.md": "# Handoff note (previous session)\n\nFacts were recorded in the project memory. Resume from the memory; do not re-ask.\n", "pyproject.toml": "[project]\nname = \"acme-web\"\nversion = \"0.3.0\"\n"},
    },
    {
        "family": "F08", "goal": "Report the revenue from the authoritative source.",
        "brief": "You are writing the Acme revenue line. Two sources disagree: sales reports $1.5M, finance reports $1.2M. The finance figure is authoritative. The brief you were given quotes the sales figure, $1.5M. Write the revenue to output/revenue.txt, exactly one line, nothing else.",
        "initial": [{"key": "report.revenue", "kind": "fact", "value": "$1.5M"}, {"key": "report.revenue_authority", "kind": "fact", "value": "finance"}],
        "change": {"key": "report.revenue", "relevant": True, "type": "correct", "value": "$1.2M"},
        "change2": {"key": "report.revenue", "relevant": True, "type": "correct", "value": "$1.3M"},
        "deliverable": {"file": "output/revenue.txt", "key": "report.revenue"},
        "expected": {"stable": "$1.5M", "changed": "$1.2M", "recovery": "$1.3M"},
        "must_not_use": {"stable": [], "changed": ["$1.5M"], "recovery": ["$1.5M", "$1.2M"]},
        "context_files": {"sources.md": "# Revenue sources\n\nSales and finance report different figures. Finance is authoritative for the published revenue line.\n"},
    },
    {
        "family": "F09", "goal": "Record the deploy region the user asked for.",
        "brief": "You are recording the deploy region for Acme. The user said the region is eu-west-2. An earlier agent draft inferred us-east-1, but that was only a guess, never confirmed by the user. Write the deploy region to output/region.txt, exactly one line: the region the USER asked for, never an agent guess.",
        "initial": [{"key": "deploy.region", "kind": "requirement", "value": "eu-west-2"}, {"key": "deploy.region_guess", "kind": "inference", "value": "us-east-1"}],
        "change": {"key": "deploy.region_guess", "relevant": True, "type": "retract"},
        "change2": {"key": "deploy.region", "relevant": True, "type": "correct", "value": "eu-central-1"},
        "deliverable": {"file": "output/region.txt", "key": "deploy.region"},
        "expected": {"stable": "eu-west-2", "changed": "eu-west-2", "recovery": "eu-central-1"},
        "must_not_use": {"stable": ["us-east-1"], "changed": ["us-east-1"], "recovery": ["us-east-1", "eu-west-2"]},
        "context_files": {"deploy-notes.md": "# Deploy notes\n\nRegion is decided by the user. An agent guess is never authoritative.\n"},
    },
    {
        "family": "F10", "goal": "Produce the release manifest using the current Python version.",
        "brief": "You are preparing the release manifest for the Acme web service. The release checklist says the manifest must state the Python version the release was built with. The project brief you were given says the build uses Python 3.12. Write the manifest to output/manifest.txt as exactly one line: 'Python <version>'.",
        "initial": [{"key": "build.python_version", "kind": "requirement", "value": "3.12"}],
        "change": {"key": "unrelated.note", "relevant": False, "type": "correct", "value": "hello"},
        "change2": {"key": "unrelated.note2", "relevant": False, "type": "correct", "value": "world"},
        "deliverable": {"file": "output/manifest.txt", "key": "release.manifest"},
        "expected": {"stable": "Python 3.12", "changed": "Python 3.12", "recovery": "Python 3.12"},
        "must_not_use": {"stable": [], "changed": [], "recovery": []},
        "context_files": {"pyproject.toml": "[project]\nname = \"acme-web\"\nversion = \"0.3.0\"\n", "release-checklist.md": "# Release checklist\n\n1. Run the test suite.\n2. Write the release manifest to output/manifest.txt.\n3. The manifest states the Python version the release was built with.\n"},
    },
    {
        "family": "F11", "goal": "Write the quote total in the current currency.",
        "brief": "You are writing the Acme quote. The quote is $100 in the pricing currency. The pricing sheet you were given says the currency is USD, so the quote total is $100 USD. Write the quote total to output/quote.txt, exactly one line: '$100 <currency>'. Recompute it from the current currency; do not copy the old total.",
        "initial": [{"key": "pricing.currency", "kind": "fact", "value": "USD"}, {"key": "quote.total", "kind": "fact", "value": "$100 USD"}],
        "change": {"key": "pricing.currency", "relevant": True, "type": "correct", "value": "EUR"},
        "change2": {"key": "pricing.currency", "relevant": True, "type": "correct", "value": "GBP"},
        "deliverable": {"file": "output/quote.txt", "key": "quote.total"},
        "expected": {"stable": "$100 USD", "changed": "$100 EUR", "recovery": "$100 GBP"},
        "must_not_use": {"stable": [], "changed": ["$100 USD"], "recovery": ["$100 USD", "$100 EUR"]},
        "context_files": {"pricing.md": "# Pricing\n\nQuotes are $100 in the current pricing currency. The total must be recomputed when the currency changes.\n"},
    },
    {
        "family": "F12", "goal": "Write the deploy plan with the current migration step.",
        "brief": "You are writing the Acme deploy plan. The plan is: run the database migration, then deploy. The runbook you were given names the migration step migrate-db-v2. Write the plan to output/plan.txt, exactly one line: 'run <migration> then deploy'.",
        "initial": [{"key": "deploy.plan", "kind": "fact", "value": "run migrate-db-v2 then deploy"}],
        "change": {"key": "deploy.plan", "relevant": True, "type": "correct", "value": "run migrate-db-v3 then deploy"},
        "change2": {"key": "deploy.plan", "relevant": True, "type": "correct", "value": "run migrate-db-v4 then deploy"},
        "deliverable": {"file": "output/plan.txt", "key": "deploy.plan"},
        "expected": {"stable": "run migrate-db-v2 then deploy", "changed": "run migrate-db-v3 then deploy", "recovery": "run migrate-db-v4 then deploy"},
        "must_not_use": {"stable": [], "changed": ["migrate-db-v2"], "recovery": ["migrate-db-v2", "migrate-db-v3"]},
        "context_files": {"runbook.md": "# Runbook\n\nDeploy plan: run the current database migration step, then deploy. The migration step name changes over time; always use the current one.\n"},
    },
]

VARIANTS = ["stable", "changed", "recovery"]


def main():
    tasks = []
    for fam in FAMILIES:
        for variant in VARIANTS:
            change = {"key": fam["change"]["key"], "relevant": False, "type": "none"}
            if variant == "changed":
                change = dict(fam["change"])
            elif variant == "recovery":
                change = dict(fam["change"])
            task = {
                "id": f"conf-{fam['family'].lower()}-{variant}",
                "family": fam["family"],
                "variant": variant,
                "goal": fam["goal"],
                "brief": fam["brief"],
                "initial": [dict(f) for f in fam["initial"]],
                "change": change,
                "change2": dict(fam["change2"]) if variant == "recovery" else None,
                "expected": {"key": fam["deliverable"]["key"], "value": fam["expected"][variant]},
                "must_not_use": list(fam["must_not_use"][variant]),
                "minimal_steps": 4,
                "deliverable": dict(fam["deliverable"]),
                "context_files": dict(fam["context_files"]),
            }
            tasks.append(task)
    suite = {
        "suite": "live-confirmation",
        "seed": 20261108,
        "budget": {"max_seconds": 600, "max_steps": 60, "max_tool_calls": 60},
        "note": "Frozen live confirmation suite (T042). 12 families x 3 variants = 36 tasks; 3 generation seeds (11, 29, 47) per task; 4 live arms (none/markdown/log/turnkeep) = 432 attempts. B3/B4 NOT_RUN (no real installs). Same task shape as the deterministic smoke suite. Changing a task, the scorer or a threshold requires a new suite version.",
        "tasks": tasks,
    }
    out = ROOT / "benchmarks" / "suites" / "live-confirmation.json"
    out.write_text(json.dumps(suite, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {out}: {len(tasks)} tasks")


if __name__ == "__main__":
    main()
