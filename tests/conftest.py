from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from minimal_local_agent.config import ModelConfig, Settings, WorkspaceConfig
from minimal_local_agent.model.scripted import scripted_model
from minimal_local_agent.runtime.runtime import AgentRuntime


def make_settings(tmp_path: Path, **sections: Any) -> Settings:
    data: dict[str, Any] = {
        "workspace": WorkspaceConfig(
            root=tmp_path / "ws", state_dir=tmp_path / "state"
        ),
        "models": {
            "default": ModelConfig(
                provider="test", input_price_per_mtok=1.0, output_price_per_mtok=2.0
            )
        },
    }
    base = Settings(**data).model_dump()
    for name, values in sections.items():
        base[name] = (
            {**base.get(name, {}), **values} if isinstance(values, dict) else values
        )
    return Settings.model_validate(base)


@pytest.fixture
def runtime_factory(tmp_path: Path):
    def make(
        script: dict[str, Any],
        approver=None,
        models=None,
        extra_tools=(),
        **sections: Any,
    ) -> AgentRuntime:
        settings = make_settings(tmp_path, **sections)
        return AgentRuntime(
            settings,
            models=models or {"default": scripted_model(script)},
            approver=approver or (lambda _tool, _preview: True),
            tools=extra_tools,
        )

    return make
