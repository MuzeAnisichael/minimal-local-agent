"""工具权限：被拒绝的工具不会出现在模型可见的 schema 里；写操作按策略审批。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from minimal_local_agent.config import Decision, PolicyConfig
from minimal_local_agent.tools.registry import Tool

# 审批回调：(工具名, 变更预览) -> 是否批准
Approver = Callable[[str, str], bool]


def deny_all(_tool: str, _preview: str) -> bool:
    return False


@dataclass(frozen=True, slots=True)
class Policy:
    config: PolicyConfig

    def decision(self, tool: Tool) -> Decision:
        if tool.effect == "control":
            return "allow"
        explicit = self.config.tools.get(tool.name)
        if tool.effect == "write":
            ceiling = self.config.write
            # 全局 deny / preview 是硬上限，单个工具的覆盖不能放宽它。
            if ceiling in {"deny", "preview"}:
                return "deny" if explicit == "deny" else ceiling
            return explicit or ceiling
        return "deny" if explicit == "deny" else "allow"

    def visible(self, tools: list[Tool]) -> list[Tool]:
        return [tool for tool in tools if self.decision(tool) != "deny"]


__all__ = ["Approver", "Policy", "deny_all"]
