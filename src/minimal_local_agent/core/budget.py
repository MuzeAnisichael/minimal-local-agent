"""统一预算账本：模型请求、工具调用、输出 token 和成本都记在同一本账上。

子任务从父账本切出额度（预留），结束时按实际用量结算，多余部分退回。
未报告的 token 用量按本次请求的上限计入，并标记为“用量不完整”，不当作免费。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from minimal_local_agent.config import BudgetConfig


class BudgetExceeded(RuntimeError):
    pass


@dataclass(slots=True)
class Usage:
    requests: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    tokens_complete: bool = True
    cost_complete: bool = True

    def add(self, other: Usage) -> None:
        self.requests += other.requests
        self.tool_calls += other.tool_calls
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cost_usd += other.cost_usd
        self.tokens_complete = self.tokens_complete and other.tokens_complete
        self.cost_complete = self.cost_complete and other.cost_complete

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["cost_usd"] = round(self.cost_usd, 8) if self.cost_complete else None
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Usage:
        values = dict(data)
        if values.get("cost_usd") is None:
            values["cost_usd"] = 0.0
            values["cost_complete"] = False
        return cls(**values)


@dataclass(slots=True)
class Limits:
    requests: int
    tool_calls: int
    output_tokens: int
    cost_usd: float | None = None

    @classmethod
    def from_config(cls, config: BudgetConfig) -> Limits:
        return cls(
            config.requests, config.tool_calls, config.output_tokens, config.cost_usd
        )


@dataclass(slots=True)
class Budget:
    limits: Limits
    max_tokens_per_request: int = 2048
    used: Usage = field(default_factory=Usage)
    reserved: dict[str, Limits] = field(default_factory=dict)

    def _reserved(self, name: str) -> float:
        return sum(getattr(r, name) or 0 for r in self.reserved.values())

    def remaining(self, name: str) -> float:
        limit = getattr(self.limits, name)
        return limit - getattr(self.used, name) - self._reserved(name)

    def before_model(self) -> int:
        """返回本次请求允许的最大输出 token；额度不足时抛出 BudgetExceeded。"""

        if self.remaining("requests") < 1:
            raise BudgetExceeded("Model request budget exhausted")
        remaining_output = int(self.remaining("output_tokens"))
        if remaining_output < 1:
            raise BudgetExceeded("Output token budget exhausted")
        if self.limits.cost_usd is not None and self.remaining("cost_usd") <= 0:
            raise BudgetExceeded("Cost budget exhausted")
        return min(self.max_tokens_per_request, remaining_output)

    def charge_model(
        self,
        input_tokens: int | None,
        output_tokens: int | None,
        cost_usd: float | None,
        requested_max: int,
    ) -> None:
        self.used.requests += 1
        self.used.input_tokens += input_tokens or 0
        if output_tokens is None:
            self.used.output_tokens += requested_max
            self.used.tokens_complete = False
        else:
            self.used.output_tokens += output_tokens
        if input_tokens is None:
            self.used.tokens_complete = False
        if cost_usd is None:
            self.used.cost_complete = False
        else:
            self.used.cost_usd += cost_usd

    def before_tool(self) -> None:
        if self.remaining("tool_calls") < 1:
            raise BudgetExceeded("Tool call budget exhausted")
        self.used.tool_calls += 1

    def allocate(
        self, child_id: str, requests: int, tool_calls: int, output_tokens: int
    ) -> Limits:
        """为子任务预留额度；上限是父账本剩余量的一半，至少留 1 次请求给父任务收尾。"""

        def share(name: str, wanted: int, keep: int) -> int:
            available = int(self.remaining(name)) - keep
            return max(0, min(wanted, available))

        limits = Limits(
            requests=share("requests", requests, 1),
            tool_calls=share("tool_calls", tool_calls, 0),
            output_tokens=share("output_tokens", output_tokens, 1),
            cost_usd=None
            if self.limits.cost_usd is None
            else self.remaining("cost_usd") / 2,
        )
        if limits.requests < 1 or limits.output_tokens < 1:
            raise BudgetExceeded("Not enough budget left for a subtask")
        self.reserved[child_id] = limits
        return limits

    def settle(self, child_id: str, child_used: Usage) -> None:
        self.reserved.pop(child_id, None)
        self.used.add(child_used)

    def to_dict(self) -> dict[str, Any]:
        return {
            "limits": asdict(self.limits),
            "max_tokens_per_request": self.max_tokens_per_request,
            "used": asdict(self.used),
            "reserved": {k: asdict(v) for k, v in self.reserved.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Budget:
        return cls(
            limits=Limits(**data["limits"]),
            max_tokens_per_request=data["max_tokens_per_request"],
            used=Usage(**data["used"]),
            reserved={k: Limits(**v) for k, v in data.get("reserved", {}).items()},
        )


__all__ = ["Budget", "BudgetExceeded", "Limits", "Usage"]
