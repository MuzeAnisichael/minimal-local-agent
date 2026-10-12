"""Bounded, in-memory observation for a local, read-only inspector.

The hub is not an execution scheduler or a durable audit log. Publishing does no
I/O; only readers may wait. Raw message/context contents disappear on eviction or
process restart and must not be confused with archived conversation history.
"""

from __future__ import annotations

import json
import math
from collections import OrderedDict, deque
from copy import deepcopy
from datetime import UTC, datetime
from threading import Condition
from typing import Any
from uuid import uuid4

from minimal_local_agent.events import RuntimeEvent

_RUN_STATES = {
    "run.started": "running",
    "run.completed": "completed",
    "run.failed": "failed",
    "run.cancelled": "cancelled",
    "run.interrupted": "interrupted",
}
_TERMINAL_STATES = frozenset({"completed", "failed", "cancelled", "interrupted"})
_PRIVATE_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "access_token",
        "refresh_token",
        "password",
        "secret",
        "credentials",
        "headers",
    }
)
_REASONING_KEYS = frozenset({"thinking", "reasoning", "chain_of_thought"})


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _bounded(value: Any, budget: list[int], clipped: list[bool], depth: int = 0) -> Any:
    """Copy JSON-like data without retaining arbitrary objects or unbounded text."""

    if budget[0] <= 0 or depth > 12:
        clipped[0] = True
        return "[observation limit]"
    if value is None or isinstance(value, bool | int):
        budget[0] -= 8
        return value
    if isinstance(value, float):
        budget[0] -= 16
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        # Slice before encoding so a huge input does not create a huge byte copy.
        raw = value[: budget[0]].encode("utf-8")
        text = raw[: budget[0]].decode("utf-8", errors="ignore")
        budget[0] -= len(text.encode("utf-8"))
        if len(text) < len(value):
            clipped[0] = True
            return text + " [truncated]"
        return text
    if isinstance(value, dict):
        if value.get("part_kind") == "thinking" or (
            isinstance(value.get("type"), str)
            and value["type"] in {"thinking", "reasoning"}
        ):
            return {"notice": "Hidden reasoning is not observed"}
        result = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 64 or budget[0] <= 0:
                clipped[0] = True
                break
            if not isinstance(key, str):
                clipped[0] = True
                continue
            name = key[:128]
            budget[0] -= len(name.encode("utf-8")) + 8
            normalized_key = key.casefold().replace("-", "_")
            if normalized_key in _PRIVATE_KEYS:
                result[name] = "[redacted]"
            elif normalized_key in _REASONING_KEYS:
                result[name] = "[not observed]"
            else:
                result[name] = _bounded(item, budget, clipped, depth + 1)
        return result
    if isinstance(value, list | tuple):
        result = []
        for index, item in enumerate(value):
            if index >= 64 or budget[0] <= 0:
                clipped[0] = True
                break
            result.append(_bounded(item, budget, clipped, depth + 1))
            budget[0] -= 4
        return result
    clipped[0] = True
    return "[unsupported observation value]"


class ObservationHub:
    """Thread-safe event ring and recent run metadata, independent of execution.

    ``execution_id`` is assigned by the caller before a run starts and should be
    supplied in every event's data. An uncorrelated event remains visible, but
    does not invent a run or assert that a task is running. Structured credential
    fields are redacted; callers must still avoid putting secrets in free text.
    """

    def __init__(
        self,
        *,
        max_events: int = 256,
        max_runs: int = 32,
        max_event_bytes: int = 64_000,
    ) -> None:
        if max_events < 1 or max_runs < 1 or max_event_bytes < 512:
            raise ValueError("Observation limits must be positive; event bytes >= 512")
        self.max_events = max_events
        self.max_runs = max_runs
        self.max_event_bytes = max_event_bytes
        self._condition = Condition()
        self._events: deque[dict[str, Any]] = deque(maxlen=max_events)
        self._runs: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._cursor = 0
        self._observer_errors = 0
        self._evicted_runs = 0
        self._hub_id = str(uuid4())
        self._created_at = _now()

    def _event(self, event: RuntimeEvent) -> dict[str, Any]:
        data = event.data
        execution_id = getattr(event, "execution_id", None) or data.get("execution_id")
        clipped = [False]
        # Reserve header/JSON overhead and check the actual serialized size below.
        safe_data = _bounded(data, [self.max_event_bytes // 2], clipped)
        header_limit = min(120, self.max_event_bytes // 10)

        def text(value: Any) -> str:
            printable = "".join(
                character if ord(character) >= 32 and ord(character) != 127 else "?"
                for character in str(value)[:header_limit]
            )
            return printable.encode("utf-8")[:header_limit].decode(
                "utf-8", errors="ignore"
            )

        result = {
            "sequence": event.sequence
            if isinstance(event.sequence, int) and 0 <= event.sequence < 2**63
            else None,
            "created_at": text(event.created_at),
            "session_id": text(event.session_id),
            "execution_id": text(execution_id)
            if isinstance(execution_id, str)
            else None,
            "type": text(event.type),
            "data": safe_data,
            "observation_truncated": clipped[0],
        }
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False).encode(
            "utf-8"
        )
        if len(encoded) + 32 > self.max_event_bytes:
            # Escaped control characters can exceed the UTF-8 text allowance.
            result["data"] = {"notice": "Content omitted: observation byte limit"}
            result["observation_truncated"] = True
        return result

    def publish(self, event: RuntimeEvent) -> None:
        """Never wait on a consumer; isolate malformed observations."""

        try:
            value = self._event(event)
            with self._condition:
                self._cursor += 1
                value["cursor"] = self._cursor
                self._update_run(value)
                self._events.append(value)
                self._condition.notify_all()
        except Exception:
            # Observation failure must not turn a successful task into a failure.
            with self._condition:
                self._observer_errors += 1

    def _update_run(self, event: dict[str, Any]) -> None:
        execution_id = event["execution_id"]
        if not execution_id:
            return
        data = event["data"]
        run = self._runs.get(execution_id)
        if run is None:
            run = {
                "execution_id": execution_id,
                "session_id": event["session_id"],
                "state": "observed",
                "started_at": None,
                "updated_at": event["created_at"],
                "agent_id": "local",
                "parent_execution_id": None,
                "parent_task_id": None,
                "events_seen": 0,
                "usage": None,
            }
            self._runs[execution_id] = run
        state = _RUN_STATES.get(event["type"])
        if state is not None and run["state"] not in _TERMINAL_STATES:
            run["state"] = state
        if event["type"] == "run.started" and run["started_at"] is None:
            run["started_at"] = event["created_at"]
        for field in ("agent_id", "parent_execution_id", "parent_task_id"):
            if isinstance(data.get(field), str):
                run[field] = data[field][:120]
        if event["type"] in {"run.completed", "run.failed"} and isinstance(
            data.get("usage"), dict
        ):
            run["usage"] = data["usage"]
        run["events_seen"] += 1
        run["updated_at"] = event["created_at"]
        run["last_event"] = event["type"]
        self._runs.move_to_end(execution_id)
        while len(self._runs) > self.max_runs:
            self._runs.popitem(last=False)
            self._evicted_runs += 1

    def _retention(self) -> dict[str, Any]:
        return {
            "max_events": self.max_events,
            "max_runs": self.max_runs,
            "max_event_bytes": self.max_event_bytes,
            "first_cursor": self._events[0]["cursor"] if self._events else None,
            "retained_events": len(self._events),
            "retained_runs": len(self._runs),
            "discarded_events": max(0, self._cursor - len(self._events)),
            "evicted_runs": self._evicted_runs,
        }

    def snapshot(self) -> dict[str, Any]:
        """Return detached JSON-compatible metadata and the retained event window."""

        with self._condition:
            payload = {
                "runs": [dict(run) for run in reversed(self._runs.values())],
                "events": list(self._events),
                "cursor": self._cursor,
                "gap": False,
                "retention": self._retention(),
                "meta": {
                    "hub_id": self._hub_id,
                    "created_at": self._created_at,
                    "storage": "memory",
                    "archived": False,
                    "observer_errors": self._observer_errors,
                },
            }
        # Event bodies are immutable after insertion. Copy their potentially large
        # content outside the publisher lock; only run metadata needs a locked copy.
        return deepcopy(payload)

    def read_after(self, cursor: int, *, timeout: float = 15.0) -> dict[str, Any]:
        """Wait only in the reader; report evicted history and server-reset cursors.

        ``gap`` means not all requested events are retained. ``reset`` additionally
        means the cursor is ahead of this hub, e.g. after a process restart. The
        consumer should fetch a new snapshot rather than silently merge epochs.
        """

        if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
            raise ValueError("Observation cursor must be a non-negative integer")
        if not math.isfinite(timeout) or not 0 <= timeout <= 30:
            raise ValueError("Observation wait must be between 0 and 30 seconds")
        with self._condition:
            if cursor == self._cursor:
                self._condition.wait_for(
                    lambda: self._cursor != cursor, timeout=timeout
                )
            reset = cursor > self._cursor
            first = self._events[0]["cursor"] if self._events else self._cursor + 1
            gap = reset or cursor < first - 1
            payload = {
                "events": [
                    event for event in self._events if reset or event["cursor"] > cursor
                ],
                "cursor": self._cursor,
                "gap": gap,
                "reset": reset,
                "hub_id": self._hub_id,
            }
        return deepcopy(payload)


__all__ = ["ObservationHub"]
