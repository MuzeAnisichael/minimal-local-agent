"""Dependency-light command line interface."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import replace

from minimal_local_agent import __version__
from minimal_local_agent.agent import LocalAgent
from minimal_local_agent.config import Settings
from minimal_local_agent.evals import evaluate_model
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
        description="A minimal, auditable local agent powered by Ollama.",
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
        "--read-only",
        action="store_true",
        help="Remove the write tool for this run",
    )

    chat = commands.add_parser("chat", help="Start an interactive session")
    chat.add_argument("--session", help="Continue an existing session")
    chat.add_argument(
        "--read-only",
        action="store_true",
        help="Remove the write tool for this chat",
    )

    commands.add_parser("doctor", help="Check Ollama and model availability")

    sessions = commands.add_parser("sessions", help="List saved sessions")
    sessions.add_argument("--limit", type=int, default=20)

    history = commands.add_parser("history", help="Show completed runs in a session")
    history.add_argument("session")

    audit = commands.add_parser("audit", help="Show tool events in a session")
    audit.add_argument("session")
    audit.add_argument("--json", action="store_true", help="Print JSON output")

    evaluation = commands.add_parser(
        "eval", help="Evaluate installed Ollama models on three tool-use checks"
    )
    evaluation.add_argument(
        "--model",
        action="append",
        help="Model to evaluate; repeat to compare models (default: configured model)",
    )
    evaluation.add_argument("--json", action="store_true", help="Print JSON output")
    return parser


def _doctor(settings: Settings) -> int:
    url = f"{settings.base_url.rstrip('/')}/models"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.load(response)
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        print(f"[FAIL] Ollama API: {exc}")
        print(f"       Expected endpoint: {url}")
        return 2

    model_ids = {
        str(item.get("id"))
        for item in payload.get("data", [])
        if isinstance(item, dict)
    }
    print(f"[OK] Ollama API: {url}")
    if settings.model in model_ids:
        print(f"[OK] Model installed: {settings.model}")
        return 0
    print(f"[WARN] Model not found: {settings.model}")
    print(f"       Run: ollama pull {settings.model}")
    return 1


def _run_once(settings: Settings, args: argparse.Namespace) -> int:
    local_agent = LocalAgent(settings)
    outcome = local_agent.run(
        " ".join(args.prompt), session_id=args.session, confirm=_confirm
    )
    if args.json:
        print(
            json.dumps(
                {
                    "session_id": outcome.session_id,
                    "response": outcome.response,
                    "usage": outcome.usage,
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
    else:
        print(outcome.response)
        print(f"\nSession: {outcome.session_id}", file=sys.stderr)
    return 0


def _chat(settings: Settings, session_id: str | None) -> int:
    local_agent = LocalAgent(settings)
    session_id = session_id or local_agent.store.new_session()
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
            outcome = local_agent.run(prompt, session_id=session_id, confirm=_confirm)
            print(f"\nagent> {outcome.response}")
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


def _evaluate(settings: Settings, models: list[str] | None, json_output: bool) -> int:
    selected = models or [settings.model]
    reports = [evaluate_model(settings, model) for model in selected]
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
            print(f"\n{report.model}: {report.passed}/{report.total} passed")
            for result in report.results:
                state = "PASS" if result.passed else "FAIL"
                tools = ", ".join(result.observed_tools) or "none"
                print(
                    f"  [{state}] {result.case:<6} {result.latency_ms:>6} ms  "
                    f"tools={tools}"
                )
                if result.error:
                    print(f"         error={result.error}")
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
        if args.command == "doctor":
            return _doctor(settings)
        if args.command == "run":
            return _run_once(settings, args)
        if args.command == "chat":
            return _chat(settings, args.session)
        if args.command == "sessions":
            return _sessions(settings, args.limit)
        if args.command == "history":
            return _history(settings, args.session)
        if args.command == "audit":
            return _audit(settings, args.session, args.json)
        if args.command == "eval":
            return _evaluate(settings, args.model, args.json)
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
