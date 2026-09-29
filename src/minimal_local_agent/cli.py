"""Dependency-light command line interface."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import replace

from minimal_local_agent import __version__
from minimal_local_agent.config import Settings
from minimal_local_agent.evals import evaluate_model
from minimal_local_agent.events import RuntimeEvent
from minimal_local_agent.models import probe_model
from minimal_local_agent.policy import PolicyEngine
from minimal_local_agent.runtime import AgentRuntime
from minimal_local_agent.store import AuditStore


def _confirm(action: str, detail: str) -> bool:
    print(f"\nApproval required: {action}\n{detail}", file=sys.stderr)
    if not sys.stdin.isatty():
        print("Denied because stdin is not interactive.", file=sys.stderr)
        return False
    answer = input("Approve once? [y/N] ").strip().casefold()
    return answer in {"y", "yes"}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="minimal-agent",
        description="A minimal, auditable local-first agent.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--config", help="Path to agent.toml (default: ./agent.toml when present)"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="Run one task")
    run.add_argument("prompt", nargs="+", help="Task for the agent")
    run.add_argument("--session", help="Continue an existing session")
    run.add_argument("--json", action="store_true", help="Print JSON output")
    run.add_argument(
        "--events",
        action="store_true",
        help="Stream structured runtime events as JSONL on stderr",
    )
    run_mode = run.add_mutually_exclusive_group()
    run_mode.add_argument(
        "--read-only",
        action="store_true",
        help="Remove the write tool for this run",
    )
    run_mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and preview mutations without changing files",
    )

    chat = commands.add_parser("chat", help="Start an interactive session")
    chat.add_argument("--session", help="Continue an existing session")
    chat.add_argument(
        "--events",
        action="store_true",
        help="Stream structured runtime events as JSONL on stderr",
    )
    chat_mode = chat.add_mutually_exclusive_group()
    chat_mode.add_argument(
        "--read-only",
        action="store_true",
        help="Remove the write tool for this chat",
    )
    chat_mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and preview mutations without changing files",
    )

    commands.add_parser("doctor", help="Check the configured model endpoint")

    sessions = commands.add_parser("sessions", help="List saved sessions")
    sessions.add_argument("--limit", type=int, default=20)

    history = commands.add_parser("history", help="Show completed runs in a session")
    history.add_argument("session")

    audit = commands.add_parser("audit", help="Show tool events in a session")
    audit.add_argument("session")
    audit.add_argument("--json", action="store_true", help="Print JSON output")

    capabilities = commands.add_parser(
        "capabilities", help="Show the effective model-visible capability policy"
    )
    capabilities.add_argument("--json", action="store_true", help="Print JSON output")

    receipts = commands.add_parser(
        "receipts", help="Show hash-chained execution receipts"
    )
    receipts.add_argument("session")
    receipts.add_argument("--json", action="store_true", help="Print JSON output")

    verify = commands.add_parser("verify", help="Verify a session receipt hash chain")
    verify.add_argument("session")
    verify.add_argument(
        "--head",
        help="Expected externally saved final receipt hash",
    )

    changes = commands.add_parser("changes", help="List reversible change sets")
    changes.add_argument("--limit", type=int, default=20)

    change = commands.add_parser("change", help="Inspect one change set")
    change.add_argument("change_set")
    change.add_argument("--json", action="store_true", help="Print JSON output")

    undo = commands.add_parser("undo", help="Undo one unchanged change set")
    undo.add_argument("change_set")
    undo.add_argument(
        "--yes",
        action="store_true",
        help="Apply undo without an interactive confirmation",
    )

    evaluation = commands.add_parser(
        "eval", help="Evaluate installed Ollama models on tool-use checks"
    )
    evaluation.add_argument(
        "--model",
        action="append",
        help="Model to evaluate; repeat to compare models (default: configured model)",
    )
    evaluation.add_argument(
        "--dataset",
        help="Path to a minimal-local-agent.eval-dataset.v1 JSON file",
    )
    evaluation.add_argument("--json", action="store_true", help="Print JSON output")

    web = commands.add_parser("web", help="Start the local web console")
    web.add_argument(
        "--host",
        default="127.0.0.1",
        help="Loopback host (default: 127.0.0.1)",
    )
    web.add_argument("--port", type=int, default=8765, help="Port (default: 8765)")
    return parser


def _doctor(settings: Settings) -> int:
    result = probe_model(settings)
    label = "OK" if result.state == "ready" else "WARN" if result.online else "FAIL"
    print(f"[{label}] {settings.provider} / {settings.model}: {result.detail}")
    if result.state == "ready":
        return 0
    if result.state == "missing" and settings.provider == "ollama":
        print(f"       Run: ollama pull {settings.model}")
    return 1 if result.online else 2


def _event_printer(event: RuntimeEvent) -> None:
    print(json.dumps(event.to_dict(), ensure_ascii=False, default=str), file=sys.stderr)


def _preview_printer(event: RuntimeEvent) -> None:
    if event.type == "mutation.preview":
        print(f"\n{event.data['diff']}", file=sys.stderr)


def _run_once(settings: Settings, args: argparse.Namespace) -> int:
    runtime = AgentRuntime(settings)
    event_handler = (
        _event_printer
        if args.events
        else _preview_printer
        if settings.write_policy == "preview"
        else None
    )
    outcome = runtime.run(
        " ".join(args.prompt),
        session_id=args.session,
        confirm=_confirm,
        event_handler=event_handler,
    )
    if args.json:
        print(
            json.dumps(
                {
                    "session_id": outcome.session_id,
                    "response": outcome.response,
                    "usage": outcome.usage,
                    "receipt_hash": outcome.receipt_hash,
                    "event_handler_errors": outcome.event_handler_errors,
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
    else:
        print(outcome.response)
        print(f"\nSession: {outcome.session_id}", file=sys.stderr)
        print(f"Receipt: {outcome.receipt_hash}", file=sys.stderr)
    for error in outcome.event_handler_errors:
        print(f"Event handler warning: {error}", file=sys.stderr)
    return 0


def _chat(settings: Settings, session_id: str | None, events: bool) -> int:
    runtime = AgentRuntime(settings)
    session_id = session_id or runtime.store.new_session()
    event_handler = (
        _event_printer
        if events
        else _preview_printer
        if settings.write_policy == "preview"
        else None
    )
    print(f"Session: {session_id}")
    print("Commands: /exit, /quit")
    while True:
        try:
            prompt = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if prompt in {"/exit", "/quit"}:
            return 0
        if not prompt:
            continue
        try:
            outcome = runtime.run(
                prompt,
                session_id=session_id,
                confirm=_confirm,
                event_handler=event_handler,
            )
            print(f"\nagent> {outcome.response}")
            for error in outcome.event_handler_errors:
                print(f"Event handler warning: {error}", file=sys.stderr)
        except Exception as exc:
            print(f"Error: {exc}", file=sys.stderr)


def _sessions(settings: Settings, limit: int) -> int:
    rows = AuditStore(settings.database).list_sessions(limit=max(1, limit))
    if not rows:
        print("No sessions yet.")
        return 0
    for row in rows:
        print(f"{row['id']}  runs={row['run_count']}  updated={row['updated_at']}")
    return 0


def _history(settings: Settings, session_id: str) -> int:
    rows = AuditStore(settings.database).get_runs(session_id)
    if not rows:
        print("No completed runs in this session.")
        return 0
    for row in rows:
        state = "ok" if row["success"] else "error"
        print(f"\n[{row['id']}] {row['created_at']} ({state})")
        print(f"you> {row['prompt']}")
        if row["response"]:
            print(f"agent> {row['response']}")
        if row["error"]:
            print(f"error> {row['error']}")
    return 0


def _audit(settings: Settings, session_id: str, json_output: bool) -> int:
    rows = AuditStore(settings.database).get_tool_events(session_id)
    if json_output:
        print(
            json.dumps(
                {"session_id": session_id, "events": rows},
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        return 0
    if not rows:
        print("No tool events in this session.")
        return 0
    for row in rows:
        details = json.dumps(row["details"], ensure_ascii=False, sort_keys=True)
        print(
            f"[{row['id']}] {row['created_at']} "
            f"{row['tool_name']} {row['status']} {details}"
        )
    return 0


def _capabilities(settings: Settings, json_output: bool) -> int:
    policy = PolicyEngine(settings)
    external_tools = tuple(
        tool for server in settings.mcp_servers for tool in server.visible_tools
    )
    manifest = policy.manifest(external_tools)
    fingerprint = policy.fingerprint(external_tools)
    if json_output:
        print(
            json.dumps(
                {"policy_sha256": fingerprint, "capabilities": manifest},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    print(f"Policy: {fingerprint}")
    for capability in manifest:
        print(
            f"{capability['name']:<14} {capability['effect']:<8} "
            f"{capability['decision']:<7} {capability['source']}"
        )
    return 0


def _receipts(settings: Settings, session_id: str, json_output: bool) -> int:
    rows = AuditStore(settings.database).get_receipts(session_id)
    if json_output:
        print(
            json.dumps(
                {"session_id": session_id, "receipts": rows},
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        return 0
    if not rows:
        print("No execution receipts in this session.")
        return 0
    for row in rows:
        state = "ok" if row["receipt"]["success"] else "error"
        print(f"[{row['id']}] run={row['run_id']} {state} hash={row['receipt_hash']}")
    return 0


def _verify(settings: Settings, session_id: str, expected_head: str | None) -> int:
    if expected_head is not None and (
        len(expected_head) != 64
        or any(character not in "0123456789abcdefABCDEF" for character in expected_head)
    ):
        print("[FAIL] --head must be a 64-character SHA-256 hash", file=sys.stderr)
        return 2
    ok, error = AuditStore(settings.database).verify_receipt_chain(
        session_id, expected_head=expected_head
    )
    if ok:
        print(f"[OK] Receipt chain verified: {session_id}")
        return 0
    print(f"[FAIL] {error}", file=sys.stderr)
    return 1


def _changes(settings: Settings, limit: int) -> int:
    rows = AuditStore(settings.database).list_change_sets(limit=max(1, limit))
    if not rows:
        print("No change sets yet.")
        return 0
    for row in rows:
        print(
            f"{row['id']}  {row['status']}  files={row['file_count']}  "
            f"kind={row['kind']}  created={row['created_at']}"
        )
    return 0


def _change(settings: Settings, change_set_id: str, json_output: bool) -> int:
    change_set = AuditStore(settings.database).get_change_set(change_set_id)
    if change_set is None:
        print(f"Unknown change set: {change_set_id}", file=sys.stderr)
        return 1
    safe = {
        **change_set,
        "changes": [
            {key: value for key, value in change.items() if key != "before_text"}
            for change in change_set["changes"]
        ],
    }
    if json_output:
        print(json.dumps(safe, ensure_ascii=False, indent=2, default=str))
    else:
        print(
            f"{safe['id']}  status={safe['status']}  kind={safe['kind']}  "
            f"session={safe['session_id']}"
        )
        for item in safe["changes"]:
            print(
                f"  {item['path']}  existed={bool(item['existed'])}  "
                f"after={item['after_sha256']}"
            )
    return 0


def _undo(settings: Settings, change_set_id: str, assume_yes: bool) -> int:
    runtime = AgentRuntime(settings)
    plan = runtime.mutations.prepare_undo(change_set_id)
    if not assume_yes and not _confirm("undo_change_set", plan.approval_detail()):
        print("Undo denied.", file=sys.stderr)
        return 1
    runtime.mutations.undo(plan)
    print(f"Undone: {change_set_id}")
    return 0


def _evaluate(
    settings: Settings,
    models: list[str] | None,
    dataset: str | None,
    json_output: bool,
) -> int:
    selected = models or [settings.model]
    reports = [evaluate_model(settings, model, dataset) for model in selected]
    if json_output:
        print(
            json.dumps(
                {"reports": [report.to_dict() for report in reports]},
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
    else:
        for report in reports:
            print(
                f"\n{report.model} [{report.dataset}]: "
                f"{report.passed}/{report.total} passed"
            )
            for family, summary in report.families.items():
                tokens = summary["tokens_per_success"]
                token_text = "n/a" if tokens is None else str(tokens)
                latency = summary["latency_ms_per_success"]
                latency_text = "n/a" if latency is None else str(latency)
                print(
                    f"  {family}: {summary['passed']}/{summary['total']} "
                    f"success ({summary['success_rate']:.1%}); "
                    f"{latency_text} ms/success; "
                    f"{token_text} tokens/success"
                )
            for result in report.results:
                state = "PASS" if result.passed else "FAIL"
                tools = ", ".join(result.observed_tools) or "none"
                print(
                    f"  [{state}] {result.case:<6} {result.latency_ms:>6} ms  "
                    f"tools={tools}"
                )
                if result.error:
                    print(f"         error={result.error}")
                if result.artifact_failures:
                    print(f"         artifacts={', '.join(result.artifact_failures)}")
                elif not result.passed and result.response:
                    response = " ".join(result.response.split())
                    print(f"         response={response[:300]!r}")
                if not result.passed and result.tool_events:
                    events = json.dumps(
                        result.tool_events,
                        ensure_ascii=False,
                        default=str,
                    )
                    print(f"         events={events[:1000]}")
    return 0 if all(report.passed == report.total for report in reports) else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        settings = Settings.load(args.config)
        if getattr(args, "read_only", False):
            settings = replace(settings, write_policy="deny")
        elif getattr(args, "dry_run", False):
            settings = replace(settings, write_policy="preview")
        if args.command == "doctor":
            return _doctor(settings)
        if args.command == "run":
            return _run_once(settings, args)
        if args.command == "chat":
            return _chat(settings, args.session, args.events)
        if args.command == "sessions":
            return _sessions(settings, args.limit)
        if args.command == "history":
            return _history(settings, args.session)
        if args.command == "audit":
            return _audit(settings, args.session, args.json)
        if args.command == "capabilities":
            return _capabilities(settings, args.json)
        if args.command == "receipts":
            return _receipts(settings, args.session, args.json)
        if args.command == "verify":
            return _verify(settings, args.session, args.head)
        if args.command == "changes":
            return _changes(settings, args.limit)
        if args.command == "change":
            return _change(settings, args.change_set, args.json)
        if args.command == "undo":
            return _undo(settings, args.change_set, args.yes)
        if args.command == "eval":
            return _evaluate(settings, args.model, args.dataset, args.json)
        if args.command == "web":
            from minimal_local_agent.web import serve_web

            return serve_web(settings, host=args.host, port=args.port)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Agent error: {exc}", file=sys.stderr)
        return 1
    parser.error("Unknown command")
    return 2
