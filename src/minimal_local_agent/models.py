"""Replaceable model construction boundary."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from ipaddress import ip_address
from typing import Protocol
from urllib.parse import urlparse

from pydantic_ai.models import Model
from pydantic_ai.models.ollama import OllamaModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.providers.ollama import OllamaProvider
from pydantic_ai.providers.openai import OpenAIProvider

from minimal_local_agent.config import Settings


class ModelFactory(Protocol):
    def create(self, settings: Settings) -> Model:
        """Create one PydanticAI model from runtime settings."""


class OllamaModelFactory:
    def create(self, settings: Settings) -> Model:
        return OllamaModel(
            settings.model,
            provider=OllamaProvider(base_url=settings.base_url),
        )


def model_api_key(settings: Settings) -> str | None:
    """Resolve a secret only when needed; never persist it in Settings."""

    if settings.provider == "ollama":
        return None
    if settings.api_key_env:
        value = os.environ.get(settings.api_key_env)
        if not value:
            raise ValueError(f"Set the {settings.api_key_env} environment variable")
        return value
    hostname = urlparse(settings.base_url).hostname or ""
    try:
        local_address = ip_address(hostname).is_private
    except ValueError:
        local_address = hostname == "localhost"
    if local_address:
        return "local-only"
    raise ValueError("Remote model endpoints require agent.api_key_env")


class OpenAICompatibleModelFactory:
    """Use Chat Completions for explicitly configured compatible endpoints."""

    def create(self, settings: Settings) -> Model:
        return OpenAIChatModel(
            settings.model,
            provider=OpenAIProvider(
                base_url=settings.base_url,
                api_key=model_api_key(settings),
            ),
            # A generic endpoint may not support OpenAI-specific strict schemas.
            profile=OpenAIModelProfile(openai_supports_strict_tool_definition=False),
        )


class ConfiguredModelFactory:
    """Keep Ollama as the public default and select other providers explicitly."""

    def create(self, settings: Settings) -> Model:
        if settings.provider == "ollama":
            return OllamaModelFactory().create(settings)
        return OpenAICompatibleModelFactory().create(settings)


@dataclass(frozen=True, slots=True)
class ModelProbe:
    state: str
    detail: str

    @property
    def online(self) -> bool:
        return self.state in {"ready", "unverified", "missing"}

    @property
    def model_available(self) -> bool | None:
        if self.state == "ready":
            return True
        if self.state == "missing":
            return False
        return None

    def to_dict(self) -> dict[str, str | bool | None]:
        return {
            "state": self.state,
            "detail": self.detail,
            "online": self.online,
            "model_available": self.model_available,
        }


def probe_model(settings: Settings) -> ModelProbe:
    """Check reachability without making a billable generation request."""

    try:
        key = model_api_key(settings)
    except ValueError as exc:
        return ModelProbe("configuration", str(exc))
    headers = {"Accept": "application/json"}
    if settings.provider != "ollama" and key is not None:
        headers["Authorization"] = f"Bearer {key}"
    request = urllib.request.Request(
        f"{settings.base_url.rstrip('/')}/models",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            return ModelProbe("unauthorized", "模型服务拒绝了当前凭证")
        if settings.provider != "ollama" and exc.code in {404, 405}:
            return ModelProbe(
                "unverified", "此接口未提供模型列表，请通过一次任务验证模型"
            )
        return ModelProbe("unreachable", f"模型服务返回 HTTP {exc.code}")
    except (OSError, urllib.error.URLError):
        return ModelProbe("unreachable", "无法连接已配置的模型服务")
    except (ValueError, TypeError):
        return ModelProbe("unverified", "模型服务在线，但模型列表格式无法识别")

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return ModelProbe("unverified", "模型服务在线，但模型列表格式无法识别")
    model_ids = {str(item.get("id")) for item in data if isinstance(item, dict)}
    if settings.model in model_ids:
        return ModelProbe("ready", "模型已就绪")
    if settings.provider == "ollama":
        return ModelProbe("missing", "模型服务在线，但未找到当前模型")
    return ModelProbe(
        "unverified", "接口已连接，但模型列表未确认当前模型；请运行任务验证"
    )


__all__ = [
    "ConfiguredModelFactory",
    "ModelFactory",
    "ModelProbe",
    "OllamaModelFactory",
    "OpenAICompatibleModelFactory",
    "model_api_key",
    "probe_model",
]
