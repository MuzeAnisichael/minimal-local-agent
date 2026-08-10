"""Replaceable model construction boundary."""

from __future__ import annotations

from typing import Protocol

from pydantic_ai.models import Model
from pydantic_ai.models.ollama import OllamaModel
from pydantic_ai.providers.ollama import OllamaProvider

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


__all__ = ["ModelFactory", "OllamaModelFactory"]
