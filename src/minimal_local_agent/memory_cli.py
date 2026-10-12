"""Explicit human memory management, independent of model construction."""

from __future__ import annotations

import argparse
import json

from minimal_local_agent.config import Settings
from minimal_local_agent.extension_context import memory_scope
from minimal_local_agent.memory import MemoryRecord, SQLiteMemoryStore


def add_memory_parser(commands: argparse._SubParsersAction) -> None:
    parser = commands.add_parser("memory", help="Review scoped memory without a model")
    actions = parser.add_subparsers(dest="memory_action", required=True)
    for name in ("list", "show", "propose", "update", "approve", "reject", "forget"):
        action = actions.add_parser(name)
        action.add_argument(
            "--json", action="store_true", help="Print full JSON records"
        )
        if name == "list":
            action.add_argument("--limit", type=int, default=50)
            action.add_argument(
                "--status",
                choices=(
                    "candidate",
                    "approved",
                    "rejected",
                    "forgotten",
                    "superseded",
                ),
            )
        if name in {"show", "update", "approve", "reject", "forget"}:
            action.add_argument("record", help="Record ID in this workspace scope")
        if name in {"update", "approve", "reject", "forget"}:
            action.add_argument(
                "--revision",
                type=int,
                required=True,
                help="Revision you reviewed; stale modifications are rejected",
            )
        if name in {"propose", "update"}:
            action.add_argument(
                "content", help="Literal reference text, not instructions"
            )
            action.add_argument("--source", default="human:cli")
            action.add_argument(
                "--attribute",
                action="append",
                metavar="KEY=VALUE",
                help="Entity attributes; supplied values replace the attribute set",
            )
        if name == "propose":
            action.add_argument("--entity", help="Exact scope-local entity key")


def _attributes(values: list[str] | None) -> dict[str, str] | None:
    if values is None:
        return None
    result: dict[str, str] = {}
    for value in values:
        key, separator, content = value.partition("=")
        if not separator or not key.strip() or key in result:
            raise ValueError("Each --attribute must be a unique KEY=VALUE")
        result[key] = content
    return result


def manage_memory(settings: Settings, args: argparse.Namespace) -> int:
    # Invoking this explicit command opts into memory management, even if the
    # agent's memory tools/injection remain disabled. No model is instantiated.
    store = SQLiteMemoryStore(
        settings.memory_database or settings.database.with_name("memory.db")
    )
    scope = memory_scope(settings)
    action = args.memory_action
    if action == "list":
        records = store.list_records(scope, status=args.status, limit=args.limit)
    elif action == "show":
        record = store.get(scope, args.record)
        if record is None:
            raise ValueError("Memory record not found in this scope")
        records = [record]
    elif action == "propose":
        records = [
            store.propose(
                scope,
                args.content,
                source=args.source,
                entity_key=args.entity,
                attributes=_attributes(args.attribute),
            )
        ]
    elif action == "update":
        records = [
            store.propose_update(
                scope,
                args.record,
                content=args.content,
                source=args.source,
                attributes=_attributes(args.attribute),
                expected_revision=args.revision,
            )
        ]
    else:
        operation = {
            "approve": store.approve,
            "reject": store.reject,
            "forget": store.forget,
        }[action]
        records = [operation(scope, args.record, expected_revision=args.revision)]
    _print(records, scope, args.json)
    return 0


def _print(records: list[MemoryRecord], scope: str, json_output: bool) -> None:
    if json_output:
        print(
            json.dumps(
                {"scope": scope, "records": [record.to_dict() for record in records]},
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    print(f"Scope: {scope}")
    if not records:
        print("No matching memory records.")
    for record in records:
        print(
            f"{record.id}  {record.status}  revision={record.revision}  {record.kind}"
        )
        print(f"  {record.content}")
        if record.entity_key:
            print(f"  entity={record.entity_key} attributes={record.attributes}")
        print(f"  source={record.source}")


__all__ = ["add_memory_parser", "manage_memory"]
