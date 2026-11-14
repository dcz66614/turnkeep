"""Turnkeep product CLI (T019). Task-oriented commands, stable --json output.

Commands (all take --db or use the default data path; --scope is always
explicit — the CLI never guesses a project):
  init --scope ID [--display-name NAME] [--db PATH]
  remember --scope S --key K --value V [--kind K] [--source id:type]...
           [--parent ID]... [--expected-revision N] [--replaces ID]...
           [--idempotency-key K]
  begin --scope S --task TEXT [--idempotency-key K]
  correct --scope S --id ID --expected-revision N --value V [--idempotency-key K]
  retract --scope S --id ID --expected-revision N --reason TEXT [--idempotency-key K]
  resolve --scope S --key K --value V --candidate ID:REV ... [--idempotency-key K]
  finish --scope S --run RUN_ID --key K --value V [--parent ID]... [--idempotency-key K]
  resume --scope S --run RUN_ID
  rebase --scope S --run RUN_ID
  recall --scope S [--query Q] [--history]
  brief --scope S [--goal TEXT]
  inspect --scope S [--history]
  history --scope S
  demo [--db PATH]   (scripted J01 demo, clearly labelled, no model calls)
  doctor [--data-dir DIR] [--json]  (diagnostics, T028; exit 0 always)
  purge-residue [--db PATH] [--scope S]  (clean historical
           secure_delete=OFF residue: ON + VACUUM + checkpoint + file
           probe, P0-R15-01; exit 0 when clean, 2 when residue remains)
  serve --scope S [--port N] [--db PATH]  (loopback-only workbench API, T027;
           prints URL + session token on stdout, serves until Ctrl-C)

Errors: non-zero exit + structured JSON diagnosis on stderr with --json
(or plain text otherwise). --non-interactive is the default behaviour:
the CLI never prompts; ambiguous scope is an error, never a guess.
Values with spaces / Chinese / quotes pass through argv unchanged (no
shell string building anywhere).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .context import bind_context
from .errors import TurnkeepError
from .storage import open_db, data_path, scope_epoch
from .transports.context import bind_for_server
from .transports.policy import validate_request_body


def _parse_source(spec):
    if ":" not in spec:
        raise TurnkeepError("INVALID_INPUT", f"source must be id:type, got {spec!r}")
    sid, stype = spec.rsplit(":", 1)
    return {"id": sid, "type": stype}


def _parse_candidate(spec):
    if ":" not in spec:
        raise TurnkeepError("INVALID_INPUT", f"candidate must be id:revision, got {spec!r}")
    rid, rev = spec.rsplit(":", 1)
    return {"id": rid, "revision": int(rev)}


def _validate_sources(tbinding, scope, refs):
    """P1-d: --source values go through the shared transport policy before
    they reach storage — path-like source ids are rejected."""
    validate_request_body(tbinding, {"source_refs": refs}, scope)


def _emit(payload, as_json):
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(payload, ensure_ascii=False))


def _fail(error, as_json):
    diag = {"error": {"code": error.code, "message": str(error),
                      "details": getattr(error, "details", {}) or {}}}
    if as_json:
        print(json.dumps(diag, ensure_ascii=False, indent=2), file=sys.stderr)
    else:
        print(f"ERROR {error.code}: {error}", file=sys.stderr)
        details = diag["error"]["details"]
        if details:
            print(f"details: {json.dumps(details, ensure_ascii=False)}", file=sys.stderr)
    return 2


def _db_for(args):
    path = Path(args.db) if getattr(args, "db", None) else data_path(args.scope)
    return open_db(path)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="turnkeep", description="Turnkeep local project memory (v0.1)")
    parser.add_argument("--version", action="version", version=f"turnkeep {__version__}")
    parser.add_argument("--db", help="SQLite path (default: per-scope data path)")
    parser.add_argument("--json", action="store_true", help="stable JSON output")
    parser.add_argument("--non-interactive", action="store_true", help="never prompt (default behaviour)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init"); p.add_argument("--scope", required=True); p.add_argument("--display-name")
    p = sub.add_parser("remember"); p.add_argument("--scope", required=True); p.add_argument("--key", required=True)
    p.add_argument("--value", required=True); p.add_argument("--kind", default="fact")
    p.add_argument("--source", action="append", default=[]); p.add_argument("--parent", action="append", default=[])
    p.add_argument("--expected-revision", type=int); p.add_argument("--replaces", action="append", default=[])
    p.add_argument("--idempotency-key", default="")
    p = sub.add_parser("begin"); p.add_argument("--scope", required=True); p.add_argument("--task", required=True)
    p.add_argument("--idempotency-key", default="")
    p = sub.add_parser("correct"); p.add_argument("--scope", required=True); p.add_argument("--id", required=True)
    p.add_argument("--expected-revision", type=int, required=True); p.add_argument("--value", required=True)
    p.add_argument("--source", action="append", default=[])
    p.add_argument("--idempotency-key", default="")
    p = sub.add_parser("retract"); p.add_argument("--scope", required=True); p.add_argument("--id", required=True)
    p.add_argument("--expected-revision", type=int, required=True); p.add_argument("--reason", required=True,
                   help="controlled label only: correction, superseded or withdrawn (free text is not accepted — the reason is persisted verbatim in the append-only event log)")
    p.add_argument("--idempotency-key", default="")
    p = sub.add_parser("resolve"); p.add_argument("--scope", required=True); p.add_argument("--key", required=True)
    p.add_argument("--value", required=True); p.add_argument("--candidate", action="append", default=[])
    p.add_argument("--source", action="append", default=[])
    p.add_argument("--idempotency-key", default="")
    p = sub.add_parser("finish"); p.add_argument("--scope", required=True); p.add_argument("--run", required=True)
    p.add_argument("--key", required=True); p.add_argument("--value", required=True)
    p.add_argument("--parent", action="append", default=[]); p.add_argument("--idempotency-key", default="")
    p = sub.add_parser("resume"); p.add_argument("--scope", required=True); p.add_argument("--run", required=True)
    p = sub.add_parser("rebase"); p.add_argument("--scope", required=True); p.add_argument("--run", required=True)
    p.add_argument("--candidate-key", default=None,
                   help="key of the candidate result recomputed in the latest rebased run (with --candidate-value)")
    p.add_argument("--candidate-value", default=None,
                   help="value of the candidate result; saved only when rebase is exhausted")
    p = sub.add_parser("quarantine"); p.add_argument("--scope", required=True); p.add_argument("--run", required=True)
    p = sub.add_parser("recall"); p.add_argument("--scope", required=True); p.add_argument("--query", default="")
    p.add_argument("--history", action="store_true")
    p = sub.add_parser("brief"); p.add_argument("--scope", required=True); p.add_argument("--goal", default="")
    p = sub.add_parser("inspect"); p.add_argument("--scope", required=True); p.add_argument("--history", action="store_true")
    p = sub.add_parser("history"); p.add_argument("--scope", required=True)
    p = sub.add_parser("expire"); p.add_argument("--scope", required=True); p.add_argument("--id", required=True)
    p.add_argument("--valid-until", required=True,
                   help="ISO-8601 timestamp with a timezone, e.g. 2026-01-01T00:00:00+00:00 (a non-timestamp is rejected)")
    p.add_argument("--expected-revision", type=int, required=True)
    p = sub.add_parser("sweep-expiry"); p.add_argument("--scope", required=True)
    p = sub.add_parser("erase"); p.add_argument("--scope", required=True); p.add_argument("--id", required=True)
    p.add_argument("--expected-revision", type=int, required=True); p.add_argument("--reason", required=True,
                   help="controlled label only: cleanup, repair or rotation (free text is not accepted — the reason is persisted verbatim in the append-only event log)")
    p = sub.add_parser("residuals"); p.add_argument("--scope", required=True)
    p = sub.add_parser("export"); p.add_argument("--scope", required=True)
    p.add_argument("--out", required=True, help="output directory (manifest.json + records.jsonl + events.jsonl)")
    p = sub.add_parser("import"); p.add_argument("path", nargs="?", help="package directory or .md file/dir")
    p.add_argument("--package", help="same as the positional path")
    p.add_argument("--format", default="turnkeep", choices=["turnkeep", "markdown"],
                   help="turnkeep: versioned export package (default); markdown: .md files, candidates only")
    p.add_argument("--scope", help="target scope (default: the package's origin scope, or the path name for markdown)")
    p.add_argument("--dry-run", action="store_true", help="validate and list conflicts/remap, write nothing")
    p = sub.add_parser("demo"); p.add_argument("--db", default=argparse.SUPPRESS)
    p.add_argument("--scenario", default="j01")
    p.add_argument("--no-model", action="store_true")
    p = sub.add_parser("doctor"); p.add_argument("--data-dir")
    p.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    p = sub.add_parser("purge-residue"); p.add_argument("--scope")
    p.add_argument("--db", default=argparse.SUPPRESS)
    p = sub.add_parser("mcp"); p.add_argument("--scope"); p.add_argument("--db", default=argparse.SUPPRESS)
    p = sub.add_parser("serve"); p.add_argument("--scope", required=True)
    p.add_argument("--port", type=int, default=0); p.add_argument("--db", default=argparse.SUPPRESS)
    p.add_argument("--principal", default="web:local", help="actor label bound to this server (one scope + one principal)")

    args = parser.parse_args(argv)
    as_json = getattr(args, "json", False)
    try:
        if args.command == "doctor":
            from .doctor import run_doctor
            _emit(run_doctor(args.data_dir), as_json)
            return 0
        if args.command == "demo":
            from .demo import run_demo
            if getattr(args, "scenario", "j01") != "j01" and not getattr(args, "no_model", False):
                print("NOT_RUN: scenario demos require --no-model (no model calls exist in this demo)",
                      file=sys.stderr)
                return 2
            _emit(run_demo(Path(args.db) if getattr(args, "db", None) else None,
                           scenario=getattr(args, "scenario", "j01")), as_json)
            return 0
        if args.command == "serve":
            from .web.app import serve as _serve
            import threading
            dbp = Path(args.db) if getattr(args, "db", None) else data_path(args.scope)
            server, token = _serve(dbp, args.scope, principal=args.principal, port=args.port)
            print(json.dumps({"url": f"http://127.0.0.1:{server.server_port}",
                              "token": token, "scope": args.scope}, ensure_ascii=False),
                  flush=True)
            print("serving loopback only; Ctrl-C to stop", file=sys.stderr)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            return 0
        if args.command == "mcp":
            from .mcp_server import run_stdio
            db = _db_for(args) if args.scope else None
            run_stdio(db_path=Path(args.db) if getattr(args, "db", None) else None, scope=args.scope)
            return 0
        if args.command == "purge-residue":
            # P0-R15-01: explicit cleanup entry for databases whose
            # erasures ran under a secure_delete=OFF connection (every
            # RC before this fix, on a default-OFF SQLite build).
            # Resolves the path like _db_for, but does not require a
            # registered scope: the DB may be any legacy file. --scope
            # only narrows the probe to that scope's erase hashes;
            # without it all scopes in the file are probed.
            from .erasure import purge_residue
            path = Path(args.db) if getattr(args, "db", None) else (
                data_path(args.scope) if getattr(args, "scope", None) else None)
            if path is None:
                raise TurnkeepError("INVALID_INPUT", "purge-residue needs --db or --scope")
            db = open_db(path)
            try:
                out = purge_residue(db, getattr(args, "scope", None))
            finally:
                db.close()
            _emit(out, as_json)
            return 0 if out.get("cleaned") else 2
        if args.command == "import":
            args.package = getattr(args, "package", None) or getattr(args, "path", None)
            if not args.package:
                raise SystemExit("import needs a package path (positional or --package)")
            if not getattr(args, "scope", None):
                if getattr(args, "format", "turnkeep") == "markdown":
                    # Markdown has no manifest: the path name is the scope.
                    args.scope = Path(args.package).name
                else:
                    # Target scope comes from the package manifest when --scope is omitted.
                    _manifest = json.loads(Path(args.package, "manifest.json").read_text(encoding="utf-8"))
                    args.scope = _manifest["scope_id"]
        db = _db_for(args)
        ctx = bind_context(scope_id=args.scope, principal_binding="cli:local")
        # Transport policy binding mirrors the CLI RequestContext exactly
        # (same scope, same principal, same actor): the CLI is bound at
        # startup, so there is no actor/scope input to rebind — the policy
        # check below only has to reject path-like --source values (P1-d).
        tbinding = bind_for_server(ctx.scope_id, ctx.principal_binding, actor=ctx.actor)
        from . import memory as _mem, corrections as _corr, runs as _runs
        from . import commits as _com, recovery as _rec, retrieval as _ret, briefs as _br
        from .scopes import register_scope
        from .storage import scope_epoch as _epoch
        cmd = args.command
        if cmd == "init":
            register_scope(db, args.scope, args.display_name)
            out = {"scope": args.scope, "epoch": _epoch(db, args.scope), "db": str(db_path(db))}
        elif cmd == "remember":
            refs = [_parse_source(s) for s in args.source] or [{"id": "cli:1", "type": "user_statement"}]
            _validate_sources(tbinding, ctx.scope_id, refs)
            out = _mem.remember(db, args.scope, args.key, args.value, kind=args.kind,
                                source_refs=refs,
                                parent_ids=args.parent, expected_revision=args.expected_revision,
                                replaces_ids=args.replaces, actor_binding=ctx.principal_binding,
                                idempotency_key=args.idempotency_key)
        elif cmd == "begin":
            out = _runs.begin(db, args.scope, args.task, actor_binding=ctx.principal_binding,
                              idempotency_key=args.idempotency_key)
        elif cmd == "correct":
            refs = [_parse_source(s) for s in args.source] or [{"id": "cli:1", "type": "user_statement"}]
            _validate_sources(tbinding, ctx.scope_id, refs)
            out = _corr.correct(db, args.scope, args.id, args.expected_revision, args.value,
                                source_refs=refs,
                                actor_binding=ctx.principal_binding,
                                idempotency_key=args.idempotency_key)
        elif cmd == "retract":
            out = _corr.retract(db, args.scope, args.id, args.expected_revision, args.reason,
                                actor_binding=ctx.principal_binding,
                                idempotency_key=args.idempotency_key)
        elif cmd == "resolve":
            refs = [_parse_source(s) for s in args.source] or [{"id": "cli:1", "type": "user_statement"}]
            _validate_sources(tbinding, ctx.scope_id, refs)
            out = _corr.resolve_conflict(db, args.scope, args.key,
                                         [_parse_candidate(c) for c in args.candidate],
                                         args.value,
                                         source_refs=refs,
                                         actor_binding=ctx.principal_binding,
                                         idempotency_key=args.idempotency_key)
        elif cmd == "finish":
            out = _com.finish(db, args.scope, args.run, {"key": args.key, "value": args.value},
                              parent_ids=args.parent, idempotency_key=args.idempotency_key,
                              actor_binding=ctx.principal_binding)
        elif cmd == "resume":
            out = _rec.resume(db, args.scope, args.run)
        elif cmd == "rebase":
            if bool(args.candidate_key) != bool(args.candidate_value):
                raise TurnkeepError("INVALID_INPUT",
                                    "--candidate-key and --candidate-value must be given together")
            candidate = ({"key": args.candidate_key, "value": args.candidate_value}
                         if args.candidate_key is not None else None)
            out = _rec.rebase(db, args.scope, args.run, actor_binding=ctx.principal_binding,
                              candidate_result=candidate)
        elif cmd == "quarantine":
            out = _rec.get_quarantine(db, args.scope, args.run)
            if out is None:
                raise TurnkeepError("NOT_FOUND", "No quarantine record for this run", run_id=args.run)
        elif cmd == "recall":
            out = {"scope": args.scope, "records": _ret.recall(db, args.scope, args.query, include_history=args.history)}
        elif cmd == "brief":
            out = _br.build_brief(db, args.scope, goal=args.goal)
        elif cmd == "expire":
            from .retention import set_expiry
            out = set_expiry(db, args.scope, args.id, args.valid_until, args.expected_revision)
        elif cmd == "sweep-expiry":
            from .retention import sweep_expired
            out = sweep_expired(db, args.scope)
        elif cmd == "erase":
            from .erasure import erase_record
            out = erase_record(db, args.scope, args.id, args.expected_revision, args.reason)
        elif cmd == "residuals":
            from .erasure import residual_report
            out = residual_report(db, args.scope)
        elif cmd == "export":
            from .portability import export_scope
            out = export_scope(db, args.scope, args.out)
        elif cmd == "import":
            if getattr(args, "format", "turnkeep") == "markdown":
                from .importers.markdown import import_markdown
                out = import_markdown(db, args.package, args.scope, dry_run=args.dry_run)
            else:
                from .portability import import_package
                out = import_package(db, args.package, target_scope=args.scope, dry_run=args.dry_run)
        elif cmd == "inspect":
            from .memory import active_records
            out = {"scope": args.scope, "records": active_records(db, args.scope, include_history=args.history)}
        elif cmd == "history":
            from .events import list_events
            out = {"scope": args.scope, "events": list_events(db, args.scope)}
        else:
            raise TurnkeepError("UNSUPPORTED", f"unknown command {cmd}")
        _emit(out, as_json)
        return 0
    except TurnkeepError as e:
        return _fail(e, as_json)
    except (ValueError, KeyError) as e:
        return _fail(TurnkeepError("INVALID_INPUT", str(e)), as_json)


def db_path(db):
    # sqlite3.Connection has no public path attr; recover via pragma.
    row = db.execute("PRAGMA database_list").fetchone()
    return row[2] if row else ""
