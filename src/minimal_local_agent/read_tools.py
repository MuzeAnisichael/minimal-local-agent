"""Explicit declarations for trusted host-owned read tools."""

from __future__ import annotations

import inspect
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from pydantic_ai import FunctionToolset, RunContext, ToolsetTool, WrapperToolset

from minimal_local_agent.config import Settings
from minimal_local_agent.policy import BUILTIN_EFFECTS, PolicyEngine

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


class _AuditedReadToolset(WrapperToolset[Any]):
    def __init__(self, wrapped: FunctionToolset[Any], max_result_chars: int) -> None:
        super().__init__(wrapped)
        self.max_result_chars = max_result_chars

    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, Any],
        ctx: RunContext[Any],
        tool: ToolsetTool[Any],
    ) -> Any:
        encoded_args = json.dumps(
            tool_args, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        details = {
            "argument_names": sorted(tool_args),
            "arguments_sha256": sha256(encoded_args.encode("utf-8")).hexdigest(),
        }
        started = time.perf_counter()
        try:
            result = await super().call_tool(name, tool_args, ctx, tool)
            rendered = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
            if len(rendered) > self.max_result_chars:
                raise ValueError(
                    f"Read tool result exceeds {self.max_result_chars} characters"
                )
        except Exception as exc:
            ctx.deps.record_tool_event(
                name,
                "error",
                {
                    **details,
                    "latency_ms": round((time.perf_counter() - started) * 1000),
                    "error_type": type(exc).__name__,
                },
            )
            raise
        ctx.deps.record_tool_event(
            name,
            "ok",
            {
                **details,
                "latency_ms": round((time.perf_counter() - started) * 1000),
                "result_characters": len(rendered),
                "result_sha256": sha256(rendered.encode("utf-8")).hexdigest(),
            },
        )
        return result


def build_read_toolset(
    tools: tuple[ReadTool, ...],
    settings: Settings,
    policy: PolicyEngine,
) -> tuple[_AuditedReadToolset | None, tuple[str, ...]]:
    """Compile only allowed host tools into one audited function toolset."""

    if not all(isinstance(tool, ReadTool) for tool in tools):
        raise TypeError("read_tools accepts only ReadTool declarations")
    names = tuple(tool.name for tool in tools)
    if len(names) != len(set(names)):
        raise ValueError("Read tool names must be unique")
    allowed = [tool for tool in tools if policy.exposes(tool.name, "external-read")]
    if not allowed:
        return None, names

    toolset: FunctionToolset[Any] = FunctionToolset(id="python-read-tools")
    for tool in allowed:
        toolset.tool(tool.function, name=tool.name, description=tool.description)
    # The existing external-result bound also limits host-owned read tools.
    return _AuditedReadToolset(toolset, settings.max_mcp_result_chars), names


__all__ = ["ReadTool", "build_read_toolset"]
