"""Explicit message-budget boundary with an opt-in future reduction seam."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter

ContextReducer = Callable[[tuple[ModelMessage, ...], int], Sequence[ModelMessage]]


class ContextBudgetExceeded(ValueError):
    """The model-bound message payload cannot fit the configured byte budget."""

    def __init__(self, details: dict[str, Any]) -> None:
        self.details = details
        super().__init__(
            "Context message budget exceeded "
            f"({details['submitted_bytes']} > {details['max_bytes']} bytes); "
            "start a new session or increase max_context_bytes"
        )


@dataclass(slots=True)
class ContextSelection:
    messages: list[ModelMessage]
    details: dict[str, Any]


def _digest(messages: Sequence[ModelMessage]) -> tuple[int, str]:
    payload = ModelMessagesTypeAdapter.dump_json(list(messages))
    return len(payload), hashlib.sha256(payload).hexdigest()


def select_context(
    messages: list[ModelMessage],
    max_bytes: int,
    reducer: ContextReducer | None = None,
) -> ContextSelection:
    """Reject overflow by default; an explicit reducer may prepare a smaller view.

    This only selects model-bound messages. The caller remains responsible for
    persisting the original conversation independently of the selected view.
    """

    original_bytes, original_hash = _digest(messages)
    details: dict[str, Any] = {
        "max_bytes": max_bytes,
        "original_bytes": original_bytes,
        "original_sha256": original_hash,
        "submitted_bytes": original_bytes,
        "submitted_sha256": original_hash,
        "reduced": False,
    }
    if original_bytes <= max_bytes:
        return ContextSelection(messages, details)
    if reducer is None:
        raise ContextBudgetExceeded(details)

    selected = list(reducer(tuple(messages), max_bytes))
    if not selected or selected[-1] is not messages[-1]:
        raise ValueError("Context reducer must preserve the latest message unchanged")
    submitted_bytes, submitted_hash = _digest(selected)
    details.update(
        submitted_bytes=submitted_bytes,
        submitted_sha256=submitted_hash,
        reduced=True,
    )
    if submitted_bytes > max_bytes:
        raise ContextBudgetExceeded(details)
    return ContextSelection(selected, details)


__all__ = ["ContextBudgetExceeded", "ContextReducer", "select_context"]
