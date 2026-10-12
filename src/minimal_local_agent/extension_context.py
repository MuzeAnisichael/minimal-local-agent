"""Approved reference assembly, distinct from archived messages and instructions."""

from __future__ import annotations

import hashlib
import json
import os
import re

from minimal_local_agent.config import Settings
from minimal_local_agent.events import EventBus
from minimal_local_agent.memory import MemoryRecord, MemoryStore


def memory_scope(settings: Settings) -> str:
    if settings.memory_scope is not None:
        return settings.memory_scope
    identity = os.path.normcase(str(settings.workspace.resolve()))
    return "workspace:" + hashlib.sha256(identity.encode()).hexdigest()[:32]


def bounded_records(
    records: list[MemoryRecord], scope: str, max_bytes: int
) -> list[MemoryRecord]:
    selected: list[MemoryRecord] = []
    size = 0
    for record in records[:6]:
        if record.scope != scope or record.status != "approved":
            raise ValueError("Memory adapter returned an unapproved or foreign record")
        encoded = json.dumps(record.to_dict(), ensure_ascii=False).encode("utf-8")
        if size + len(encoded) <= max_bytes:
            selected.append(record)
            size += len(encoded)
    return selected


def retrieve_memory(
    store: MemoryStore | None,
    scope: str,
    prompt: str,
    max_bytes: int,
    events: EventBus,
) -> list[MemoryRecord]:
    if store is None:
        return []
    # A small literal-query adapter, not a hidden embedding/model request.
    query = " ".join(dict.fromkeys(re.findall(r"[\w-]+", prompt[:1000])[:16]))[:1000]
    if not query:
        return []
    records = bounded_records(store.search(scope, query, limit=6), scope, max_bytes)
    events.emit(
        "memory.retrieved",
        {
            "query": query,
            "records": [r.to_dict() for r in records],
            "injection_bytes_limit": max_bytes,
        },
    )
    return records
