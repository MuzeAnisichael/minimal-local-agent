"""模型适配层：PydanticAI 只在这里出现，负责“发一次请求、拿回一次响应”。

循环、工具执行、重试与状态都由运行时自己管理；以后要换掉 PydanticAI，只改这个文件。
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from ipaddress import ip_address
from typing import Any
from urllib.parse import urlparse

from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import ToolDefinition

from minimal_local_agent.config import ModelConfig, Role, RoutingConfig


def api_key(config: ModelConfig) -> str | None:
    """只在需要时读取密钥，从不写入配置或存储。"""

    if config.provider != "openai-compatible":
        return None
    if config.api_key_env:
        value = os.environ.get(config.api_key_env)
        if not value:
            raise ValueError(f"Set the {config.api_key_env} environment variable")
        return value
    host = urlparse(config.base_url).hostname or ""
    try:
        local = ip_address(host).is_private or ip_address(host).is_loopback
    except ValueError:
        local = host == "localhost"
    if local:
        return "local-only"
    raise ValueError("Remote model endpoints require api_key_env")


def build_model(config: ModelConfig) -> Model:
    if config.provider == "ollama":
        from pydantic_ai.models.ollama import OllamaModel
        from pydantic_ai.providers.ollama import OllamaProvider

        return OllamaModel(
            config.model, provider=OllamaProvider(base_url=config.base_url)
        )
    if config.provider == "openai-compatible":
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.profiles.openai import OpenAIModelProfile
        from pydantic_ai.providers.openai import OpenAIProvider

        return OpenAIChatModel(
            config.model,
            provider=OpenAIProvider(base_url=config.base_url, api_key=api_key(config)),
            # 通用兼容接口不一定支持 OpenAI 的严格 schema。
            profile=OpenAIModelProfile(openai_supports_strict_tool_definition=False),
        )
    raise ValueError(
        "provider 'test' needs an explicit model object (pass models= to the runtime)"
    )


@dataclass(frozen=True, slots=True)
class ModelCall:
    model: str
    response: ModelResponse
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    latency_ms: int


def _tokens(value: int) -> int | None:
    # 部分兼容接口不返回用量时会报 0；有提示词的请求不可能消耗 0 token。
    return value if value > 0 else None


class ModelAdapter:
    def __init__(self, name: str, config: ModelConfig, model: Model) -> None:
        self.name = name
        self.config = config
        self.model = model

    def cost(self, input_tokens: int | None, output_tokens: int | None) -> float | None:
        prices = (self.config.input_price_per_mtok, self.config.output_price_per_mtok)
        if None in prices or input_tokens is None or output_tokens is None:
            return None
        return (input_tokens * prices[0] + output_tokens * prices[1]) / 1_000_000

    async def request(
        self,
        messages: list[ModelMessage],
        tools: list[ToolDefinition],
        *,
        max_tokens: int,
    ) -> ModelCall:
        started = time.monotonic()
        response = await self.model.request(
            messages,
            ModelSettings(temperature=self.config.temperature, max_tokens=max_tokens),
            ModelRequestParameters(function_tools=tools, allow_text_output=True),
        )
        input_tokens = _tokens(response.usage.input_tokens)
        output_tokens = _tokens(response.usage.output_tokens)
        return ModelCall(
            model=self.name,
            response=response,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=self.cost(input_tokens, output_tokens),
            latency_ms=round((time.monotonic() - started) * 1000),
        )


class AllModelsFailed(RuntimeError):
    def __init__(self, role: str, errors: list[str]) -> None:
        self.errors = errors
        super().__init__(f"All models for role {role} failed: {'; '.join(errors)}")


class ModelRouter:
    """按角色路由，并在调用失败时按顺序回退到下一个模型。"""

    def __init__(self, adapters: Mapping[str, ModelAdapter], routing: RoutingConfig):
        self.adapters = dict(adapters)
        self.routing = routing

    @classmethod
    def from_config(
        cls,
        models: Mapping[str, ModelConfig],
        routing: RoutingConfig,
        overrides: Mapping[str, Model] | None = None,
    ) -> ModelRouter:
        overrides = overrides or {}
        used = {n for r in ("plan", "act", "summarize") for n in getattr(routing, r)}
        adapters = {
            name: ModelAdapter(
                name, models[name], overrides.get(name) or build_model(models[name])
            )
            for name in sorted(used)
        }
        return cls(adapters, routing)

    def chain(self, role: Role) -> list[ModelAdapter]:
        return [self.adapters[name] for name in getattr(self.routing, role)]

    async def request(
        self,
        role: Role,
        messages: list[ModelMessage],
        tools: list[ToolDefinition],
        *,
        max_tokens: int,
        on_failure: Callable[[str, Exception], Any] | None = None,
    ) -> ModelCall:
        errors: list[str] = []
        for adapter in self.chain(role):
            try:
                return await adapter.request(messages, tools, max_tokens=max_tokens)
            except Exception as exc:  # 回退到下一个模型
                errors.append(f"{adapter.name}: {type(exc).__name__}: {exc}")
                if on_failure is not None:
                    on_failure(adapter.name, exc)
        raise AllModelsFailed(role, errors)


__all__ = [
    "AllModelsFailed",
    "ModelAdapter",
    "ModelCall",
    "ModelRouter",
    "api_key",
    "build_model",
]
