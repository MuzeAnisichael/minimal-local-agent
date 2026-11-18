"""故障注入：在不改动内核代码的前提下，模拟崩溃、工具超时、文件被外部改动和非法工具调用。

每种故障只触发有限次数（默认一次），用来测量“故障恢复率”。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Any

from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models import Model
from pydantic_ai.models.wrapper import WrapperModel

from minimal_local_agent.runtime.context import ToolContext


class SimulatedCrash(BaseException):
    """模拟进程被杀：继承 BaseException，不会被循环当作普通失败处理。"""


class _Counter:
    def __init__(self, times: int) -> None:
        self.left = times

    def fire(self) -> bool:
        if self.left > 0:
            self.left -= 1
            return True
        return False


def crash_during_write(runtime: Any, kind: str = "files", times: int = 1) -> None:
    """写盘完成后、账本标记完成前“崩溃”：恢复时必须靠对账判断已写入，而不是重写或丢失。"""

    inner = runtime.registry.effect(kind)
    counter = _Counter(times)

    class Crashing:
        async def apply(self, ctx: ToolContext, data: dict[str, Any]) -> Any:
            result = await inner.apply(ctx, data)
            if counter.fire():
                raise SimulatedCrash("crash after write")
            return result

        def probe(self, ctx: ToolContext, data: dict[str, Any]) -> Any:
            return inner.probe(ctx, data)

        def compensate(self, ctx: ToolContext, data: dict[str, Any]) -> None:
            inner.compensate(ctx, data)

    runtime.registry.add_effect(kind, Crashing())


def crash_before_tool(runtime: Any, tool_name: str, times: int = 1) -> None:
    """模型已经要求调用工具、工具还没执行时“崩溃”。"""

    tool = runtime.registry.get(tool_name)
    counter = _Counter(times)
    if tool.effect == "write":
        prepare = tool.prepare

        async def crashing_prepare(ctx: ToolContext, args: dict[str, Any]) -> Any:
            if counter.fire():
                raise SimulatedCrash("crash before tool")
            return await prepare(ctx, args)

        runtime.registry.replace(replace(tool, prepare=crashing_prepare))
    else:
        run = tool.run

        async def crashing_run(ctx: ToolContext, args: dict[str, Any]) -> Any:
            if counter.fire():
                raise SimulatedCrash("crash before tool")
            return await run(ctx, args)

        runtime.registry.replace(replace(tool, run=crashing_run))


def slow_tool(runtime: Any, tool_name: str, delay_s: float, times: int = 1) -> None:
    """让工具的前几次调用超过超时时间。"""

    tool = runtime.registry.get(tool_name)
    counter = _Counter(times)
    field = "prepare" if tool.effect == "write" else "run"
    original = getattr(tool, field)

    async def slow(ctx: ToolContext, args: dict[str, Any]) -> Any:
        if counter.fire():
            await asyncio.sleep(delay_s)
        return await original(ctx, args)

    runtime.registry.replace(replace(tool, **{field: slow}))


def external_edit(
    runtime: Any, path: str, content: str, kind: str = "files", times: int = 1
) -> None:
    """在写操作通过审批、落盘之前，模拟另一个进程改了同一个文件。"""

    inner = runtime.registry.effect(kind)
    counter = _Counter(times)

    class Interfering:
        async def apply(self, ctx: ToolContext, data: dict[str, Any]) -> Any:
            if counter.fire():
                ctx.workspace.write_file(path, content)
            return await inner.apply(ctx, data)

        def probe(self, ctx: ToolContext, data: dict[str, Any]) -> Any:
            return inner.probe(ctx, data)

        def compensate(self, ctx: ToolContext, data: dict[str, Any]) -> None:
            inner.compensate(ctx, data)

    runtime.registry.add_effect(kind, Interfering())


class BadCallModel(WrapperModel):
    """第一次带工具的请求返回一个参数不是合法 JSON 的工具调用。"""

    def __init__(self, wrapped: Model, times: int = 1) -> None:
        super().__init__(wrapped)
        self._counter = _Counter(times)

    async def request(self, messages, model_settings, model_request_parameters):  # type: ignore[override]
        response = await super().request(
            messages, model_settings, model_request_parameters
        )
        if model_request_parameters.function_tools and self._counter.fire():
            name = model_request_parameters.function_tools[0].name
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name=name, args="{not json", tool_call_id="bad-call"
                    )
                ],
                usage=response.usage,
                model_name=response.model_name,
                # 保留脚本位置，让脚本模型下一回合重放同一步
                provider_response_id=_previous_position(messages),
            )
        return response


def _previous_position(messages: list[Any]) -> str | None:
    for message in reversed(messages):
        if isinstance(message, ModelResponse) and message.provider_response_id:
            return message.provider_response_id
    return None


FAULTS = {
    "crash_during_write": crash_during_write,
    "crash_before_tool": crash_before_tool,
    "slow_tool": slow_tool,
    "external_edit": external_edit,
}


def describe(fault: dict[str, Any]) -> str:
    return json.dumps(fault, ensure_ascii=False, sort_keys=True)


__all__ = [
    "FAULTS",
    "BadCallModel",
    "SimulatedCrash",
    "crash_before_tool",
    "crash_during_write",
    "describe",
    "external_edit",
    "slow_tool",
]
