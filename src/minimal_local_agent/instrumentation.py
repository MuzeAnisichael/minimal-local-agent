"""SDK lifecycle adaptation; observers never own execution or transport."""

from __future__ import annotations

from dataclasses import asdict
from time import monotonic
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelMessagesTypeAdapter


class RuntimeHooks(AbstractCapability[Any]):
    async def wrap_model_request(
        self, ctx: RunContext[Any], *, request_context: Any, handler: Any
    ) -> Any:
        remaining = ctx.deps.budget.before_model(ctx.usage)
        configured = request_context.model_settings or {}
        request_context.model_settings = {
            **configured,
            "max_tokens": min(configured.get("max_tokens", remaining), remaining),
        }
        if ctx.deps.observe:
            ctx.deps.events.emit(
                "model.started",
                {
                    "request_index": ctx.usage.requests + 1,
                    "max_output_tokens": request_context.model_settings["max_tokens"],
                    "messages": ModelMessagesTypeAdapter.dump_python(
                        request_context.messages, mode="json"
                    ),
                },
            )
        started = monotonic()
        try:
            response = await handler(request_context)
            ctx.deps.budget.after_model(ctx.usage, response.usage)
        except Exception as exc:
            if ctx.deps.observe:
                ctx.deps.events.emit("model.failed", {"error_type": type(exc).__name__})
            raise
        if ctx.deps.observe:
            ctx.deps.events.emit(
                "model.completed",
                {
                    "latency_ms": round((monotonic() - started) * 1000),
                    "usage": asdict(response.usage),
                },
            )
        return response

    async def wrap_tool_execute(
        self, ctx: RunContext[Any], *, call: Any, tool_def: Any, args: Any, handler: Any
    ) -> Any:
        ctx.deps.budget.tool_started(ctx.usage)
        try:
            if ctx.deps.observe:
                ctx.deps.events.emit("tool.started", {"tool_name": tool_def.name})
            return await handler(args)
        finally:
            ctx.deps.budget.tool_finished()
