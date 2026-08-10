"""Optional, allowlisted, loopback-only MCP client integration."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any

from minimal_local_agent.config import Settings
from minimal_local_agent.policy import PolicyEngine


@dataclass(frozen=True, slots=True)
class MCPBundle:
    toolsets: tuple[Any, ...]
    tool_names: tuple[str, ...]


def build_mcp_bundle(settings: Settings, policy: PolicyEngine) -> MCPBundle:
    """Build guarded MCP toolsets only when endpoints are explicitly configured."""

    if not settings.mcp_servers:
        return MCPBundle((), ())
    try:
        from pydantic_ai.mcp import MCPToolset
    except ImportError as exc:  # pragma: no cover - depends on optional installation.
        raise RuntimeError(
            'MCP is configured; install it with: pip install ".[mcp]"'
        ) from exc

    toolsets: list[Any] = []
    visible_names: list[str] = []
    for server in settings.mcp_servers:
        allowed = frozenset(server.allow_tools)
        prefix = server.prefix

        async def process_tool_call(
            ctx: Any,
            call_tool: Any,
            name: str,
            arguments: dict[str, Any],
            *,
            _prefix: str = prefix,
            _server: str = server.name,
        ) -> Any:
            visible_name = f"{_prefix}{name}"
            encoded_arguments = json.dumps(
                arguments,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
            details = {
                "server": _server,
                "argument_names": sorted(arguments),
                "arguments_sha256": hashlib.sha256(
                    encoded_arguments.encode("utf-8")
                ).hexdigest(),
            }
            started = time.perf_counter()
            try:
                result = await call_tool(name, arguments)
                rendered = json.dumps(result, ensure_ascii=False, default=str)
                if len(rendered) > settings.max_mcp_result_chars:
                    raise ValueError(
                        f"MCP result exceeds {settings.max_mcp_result_chars} characters"
                    )
            except Exception as exc:
                ctx.deps.record_tool_event(
                    visible_name,
                    "error",
                    {
                        **details,
                        "latency_ms": round((time.perf_counter() - started) * 1000),
                        "error": str(exc),
                    },
                )
                raise
            ctx.deps.record_tool_event(
                visible_name,
                "ok",
                {
                    **details,
                    "latency_ms": round((time.perf_counter() - started) * 1000),
                    "result_characters": len(rendered),
                },
            )
            return result

        try:
            base = MCPToolset(
                server.url,
                id=f"mcp:{server.name}",
                max_retries=0,
                tool_error_behavior="error",
                process_tool_call=process_tool_call,
                include_instructions=False,
                sampling_model=None,
                sampling_handler=None,
                elicitation_handler=None,
                roots=None,
                init_timeout=5,
                read_timeout=60,
            )
        except ImportError as exc:  # Optional client dependencies are absent.
            raise RuntimeError(
                'MCP is configured; install it with: pip install ".[mcp]"'
            ) from exc

        def is_allowed(
            _ctx: Any,
            tool_definition: Any,
            *,
            _allowed: frozenset[str] = allowed,
            _prefix: str = prefix,
        ) -> bool:
            visible_name = f"{_prefix}{tool_definition.name}"
            return tool_definition.name in _allowed and policy.exposes(
                visible_name, "external-read"
            )

        # PydanticAI inserts one separator underscore for prefixed toolsets.
        toolsets.append(base.filtered(is_allowed).prefixed(prefix.rstrip("_")))
        visible_names.extend(server.visible_tools)

    return MCPBundle(tuple(toolsets), tuple(visible_names))


__all__ = ["MCPBundle", "build_mcp_bundle"]
