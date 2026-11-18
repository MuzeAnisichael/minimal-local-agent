"""配置：TOML 文件 + pydantic 校验。各模块只读取属于自己的那一节。"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Decision = Literal["allow", "ask", "preview", "deny"]
Role = Literal["plan", "act", "summarize"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModelConfig(_Strict):
    provider: Literal["ollama", "openai-compatible", "test"] = "ollama"
    model: str = "qwen3.5:9b"
    base_url: str = "http://localhost:11434/v1"
    api_key_env: str | None = None
    temperature: float = Field(default=0.1, ge=0, le=2)
    # 价格用于计算“每成功任务成本”；未配置时成本记为未知（None）。
    input_price_per_mtok: float | None = Field(default=None, ge=0)
    output_price_per_mtok: float | None = Field(default=None, ge=0)


class RoutingConfig(_Strict):
    """按角色选模型；列表中靠后的模型是失败时的回退。"""

    plan: list[str] = Field(default_factory=lambda: ["default"])
    act: list[str] = Field(default_factory=lambda: ["default"])
    summarize: list[str] = Field(default_factory=lambda: ["default"])


class BudgetConfig(_Strict):
    requests: int = Field(default=16, ge=1)
    tool_calls: int = Field(default=32, ge=0)
    output_tokens: int = Field(default=16_000, ge=1)
    cost_usd: float | None = Field(default=None, ge=0)
    max_output_tokens_per_request: int = Field(default=2048, ge=1)


class ContextConfig(_Strict):
    max_tokens: int = Field(default=24_000, ge=256)
    compaction: Literal["truncate", "summarize"] = "truncate"
    keep_recent_steps: int = Field(default=4, ge=1)


class ToolsConfig(_Strict):
    timeout_s: float = Field(default=30.0, gt=0)
    max_file_bytes: int = Field(default=200_000, ge=1)
    max_list_results: int = Field(default=200, ge=1)
    max_search_results: int = Field(default=100, ge=1)
    max_search_files: int = Field(default=500, ge=1)
    max_change_files: int = Field(default=8, ge=1)
    max_result_chars: int = Field(default=20_000, ge=100)


class PolicyConfig(_Strict):
    write: Decision = "ask"
    tools: dict[str, Decision] = Field(default_factory=dict)


class PlanningConfig(_Strict):
    enabled: bool = True
    max_replans: int = Field(default=2, ge=0)


class RecoveryConfig(_Strict):
    """恢复策略开关；评测里“基础循环”就是全部关闭。"""

    enabled: bool = True
    tool_retries: int = Field(default=1, ge=0)
    compensate_on_failure: bool = True


class MemoryConfig(_Strict):
    strategy: Literal["none", "episodic"] = "none"
    max_notes: int = Field(default=5, ge=1)


class MCPServerConfig(_Strict):
    name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
    command: list[str] | None = None
    url: str | None = None
    # 只暴露这里列出的工具，值为副作用类型。
    tools: dict[str, Literal["read", "write"]]
    # 声明为幂等的写工具，恢复时允许安全重放。
    idempotent: list[str] = Field(default_factory=list)
    timeout_s: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _transport(self) -> MCPServerConfig:
        if (self.command is None) == (self.url is None):
            raise ValueError("MCP server needs exactly one of command or url")
        return self


class MCPConfig(_Strict):
    servers: list[MCPServerConfig] = Field(default_factory=list)


class WorkspaceConfig(_Strict):
    root: Path = Path("workspace")
    state_dir: Path = Path(".mla")


class Settings(_Strict):
    workspace: WorkspaceConfig = Field(default_factory=WorkspaceConfig)
    models: dict[str, ModelConfig] = Field(
        default_factory=lambda: {"default": ModelConfig()}
    )
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    planning: PlanningConfig = Field(default_factory=PlanningConfig)
    recovery: RecoveryConfig = Field(default_factory=RecoveryConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    mcp: MCPConfig = Field(default_factory=MCPConfig)

    @model_validator(mode="after")
    def _routes_exist(self) -> Settings:
        for role in ("plan", "act", "summarize"):
            names = getattr(self.routing, role)
            if not names:
                raise ValueError(f"routing.{role} needs at least one model")
            missing = [name for name in names if name not in self.models]
            if missing:
                raise ValueError(f"routing.{role} names unknown models: {missing}")
        return self

    @property
    def database(self) -> Path:
        return self.workspace.state_dir / "state.db"

    def resolved(self, base: Path) -> Settings:
        """把相对路径解析到配置文件所在目录。"""

        def fix(path: Path) -> Path:
            return path if path.is_absolute() else (base / path).resolve()

        return self.model_copy(
            update={
                "workspace": WorkspaceConfig(
                    root=fix(self.workspace.root),
                    state_dir=fix(self.workspace.state_dir),
                )
            }
        )

    @classmethod
    def load(cls, path: str | Path | None = None) -> Settings:
        source = Path(path) if path else Path("agent.toml")
        if not source.exists():
            if path:
                raise FileNotFoundError(f"Config not found: {source}")
            return cls().resolved(Path.cwd())
        with source.open("rb") as handle:
            data = tomllib.load(handle)
        return cls.model_validate(data).resolved(source.resolve().parent)


__all__ = [
    "BudgetConfig",
    "ContextConfig",
    "Decision",
    "MCPServerConfig",
    "MemoryConfig",
    "ModelConfig",
    "PlanningConfig",
    "PolicyConfig",
    "RecoveryConfig",
    "Role",
    "RoutingConfig",
    "Settings",
    "ToolsConfig",
]
