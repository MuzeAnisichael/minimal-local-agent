"""Structured runtime events for embedding, streaming, and local observability."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    sequence: int
    created_at: str
    session_id: str
    type: str
    data: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


EventHandler = Callable[[RuntimeEvent], None]


class EventBus:
    """Synchronous, dependency-free event dispatcher scoped to one agent run."""

    def __init__(
        self,
        session_id: str,
        handler: EventHandler | None = None,
    ) -> None:
        self.session_id = session_id
        self.handler = handler
        self._sequence = 0
        self._handler_errors: list[str] = []

    @property
    def handler_errors(self) -> tuple[str, ...]:
        return tuple(self._handler_errors)

    def emit(self, event_type: str, data: dict[str, Any] | None = None) -> RuntimeEvent:
        self._sequence += 1
        event = RuntimeEvent(
            sequence=self._sequence,
            created_at=datetime.now(UTC).isoformat(timespec="milliseconds"),
            session_id=self.session_id,
            type=event_type,
            data=data or {},
        )
        if self.handler is not None:
            try:
                self.handler(event)
            except Exception as exc:
                # Observability callbacks must not change agent execution semantics.
                self._handler_errors.append(str(exc))
        return event


__all__ = ["EventBus", "EventHandler", "RuntimeEvent"]
