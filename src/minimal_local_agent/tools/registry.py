"""统一工具协议。

所有工具（内置文件工具、MCP 工具、子任务、控制工具）都用同一个 `Tool` 描述，
经同一个执行入口运行，所以幂等、超时、取消、补偿和事件记录只需实现一次。

- 只读工具实现 `run`，可以安全重放。
- 有副作用的工具实现 `prepare`，返回可序列化的 `Prepared`。执行入口先把它写进账本，
  再交给对应的 `EffectHandler` 落地；恢复时 handler 凭账本数据对账或补偿。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol

from pydantic_ai.tools import ToolDefinition

if TYPE_CHECKING:
    from minimal_local_agent.runtime.context import ToolContext

Effect = Literal["read", "write", "subtask", "control"]
EffectState = Literal["before", "after", "partial", "conflict", "unknown"]


@dataclass(frozen=True, slots=True)
class Prepared:
    """一次待落地的副作用。data 必须能 JSON 序列化，恢复时只凭它工作。"""

    kind: str
    data: dict[str, Any]
    preview: str
    summary: dict[str, Any] = field(default_factory=dict)


class EffectHandler(Protocol):
    async def apply(self, ctx: ToolContext, data: dict[str, Any]) -> Any: ...

    def probe(self, ctx: ToolContext, data: dict[str, Any]) -> EffectState: ...

    def compensate(self, ctx: ToolContext, data: dict[str, Any]) -> None: ...


RunFn = Callable[["ToolContext", dict[str, Any]], Awaitable[Any]]
PrepareFn = Callable[["ToolContext", dict[str, Any]], Awaitable[Prepared]]


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    effect: Effect
    run: RunFn | None = None
    prepare: PrepareFn | None = None
    timeout_s: float | None = None
    # 写工具：结果不确定时是否允许直接重放（例如声明幂等的外部接口）。
    replay_safe: bool = False

    def __post_init__(self) -> None:
        if (self.effect == "write") != (self.prepare is not None):
            raise ValueError(f"Tool {self.name}: write tools need prepare()")
        if self.effect != "write" and self.run is None:
            raise ValueError(f"Tool {self.name}: non-write tools need run()")

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters_json_schema=self.parameters,
        )


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._effects: dict[str, EffectHandler] = {}

    def add(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Duplicate tool name: {tool.name}")
        self._tools[tool.name] = tool

    def extend(self, tools: Iterable[Tool]) -> None:
        for tool in tools:
            self.add(tool)

    def add_effect(self, kind: str, handler: EffectHandler) -> None:
        self._effects[kind] = handler

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def effect(self, kind: str) -> EffectHandler:
        try:
            return self._effects[kind]
        except KeyError as exc:
            raise KeyError(f"No effect handler for {kind}") from exc

    def __iter__(self):
        return iter(self._tools.values())

    def names(self) -> list[str]:
        return sorted(self._tools)


def schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


__all__ = [
    "Effect",
    "EffectHandler",
    "EffectState",
    "Prepared",
    "Tool",
    "ToolRegistry",
    "schema",
]
