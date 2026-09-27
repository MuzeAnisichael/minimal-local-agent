"""Explicit declarations for trusted host-owned read tools."""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from minimal_local_agent.policy import BUILTIN_EFFECTS

_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


@dataclass(frozen=True, slots=True)
class ReadTool:
    """A host-owned Python function declared to be read-only.

    This is not a sandbox: only register code you trust. The runtime applies
    capability filtering, result limits, and audit recording around the call.
    """

    name: str
    function: Callable[..., Any]
    description: str | None = None

    def __post_init__(self) -> None:
        if not _NAME.fullmatch(self.name):
            raise ValueError("Read tool name must be an OpenAI-compatible identifier")
        if self.name in BUILTIN_EFFECTS or self.name.startswith("mcp_"):
            raise ValueError(f"Read tool name is reserved: {self.name}")
        if not callable(self.function):
            raise ValueError("Read tool function must be callable")
        if not (self.description or inspect.getdoc(self.function)):
            raise ValueError("Read tool needs a description or function docstring")


__all__ = ["ReadTool"]
