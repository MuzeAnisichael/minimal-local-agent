"""Configuration loading with TOML defaults and environment overrides."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

DEFAULT_MODEL = "qwen3.5:9b"
DEFAULT_BASE_URL = "http://localhost:11434/v1"
WRITE_POLICIES = frozenset({"confirm", "deny"})


def _path(value: str | Path, base_dir: Path) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = base_dir / candidate
    return candidate.resolve()


def _env(name: str, fallback: Any, cast: type) -> Any:
    value = os.getenv(name)
    if value is None:
        return fallback
    try:
        return cast(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid value for {name}: {value!r}") from exc


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime settings for one local agent process."""

    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_BASE_URL
    workspace: Path = field(default_factory=lambda: Path("workspace").resolve())
    database: Path = field(
        default_factory=lambda: Path(".minimal-local-agent/state.db").resolve()
    )
    request_limit: int = 6
    tool_calls_limit: int = 8
    max_output_tokens: int = 2048
    temperature: float = 0.1
    write_policy: str = "confirm"
    max_file_bytes: int = 200_000
    max_list_results: int = 200
    max_search_results: int = 100
    max_search_files: int = 500

    def __post_init__(self) -> None:
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be a valid http(s) URL")
        for name in (
            "request_limit",
            "tool_calls_limit",
            "max_output_tokens",
            "max_file_bytes",
            "max_list_results",
            "max_search_results",
            "max_search_files",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be at least 1")
        if not 0 <= self.temperature <= 2:
            raise ValueError("temperature must be between 0 and 2")
        if self.write_policy not in WRITE_POLICIES:
            allowed = ", ".join(sorted(WRITE_POLICIES))
            raise ValueError(f"write_policy must be one of: {allowed}")

    @classmethod
    def load(cls, config_path: str | Path | None = None) -> Settings:
        """Load ``agent.toml`` when present, then apply ``MLA_*`` overrides."""

        explicit_path = config_path or os.getenv("MLA_CONFIG")
        candidate = Path(explicit_path or "agent.toml").expanduser()
        if explicit_path and not candidate.exists():
            raise FileNotFoundError(f"Configuration file does not exist: {candidate}")

        raw: dict[str, Any] = {}
        if candidate.exists():
            with candidate.open("rb") as handle:
                raw = tomllib.load(handle)
            base_dir = candidate.resolve().parent
        else:
            base_dir = Path.cwd().resolve()

        agent = raw.get("agent", {})
        paths = raw.get("paths", {})
        tools = raw.get("tools", {})
        if not all(isinstance(section, dict) for section in (agent, paths, tools)):
            raise ValueError("agent, paths, and tools must be TOML tables")

        model = str(_env("MLA_MODEL", agent.get("model", DEFAULT_MODEL), str)).strip()
        base_url = str(
            _env("MLA_BASE_URL", agent.get("base_url", DEFAULT_BASE_URL), str)
        ).rstrip("/")
        if not model:
            raise ValueError("model cannot be empty")

        return cls(
            model=model,
            base_url=base_url,
            workspace=_path(
                _env("MLA_WORKSPACE", paths.get("workspace", "workspace"), str),
                base_dir,
            ),
            database=_path(
                _env(
                    "MLA_DATABASE",
                    paths.get("database", ".minimal-local-agent/state.db"),
                    str,
                ),
                base_dir,
            ),
            request_limit=_env("MLA_REQUEST_LIMIT", agent.get("request_limit", 6), int),
            tool_calls_limit=_env(
                "MLA_TOOL_CALLS_LIMIT", agent.get("tool_calls_limit", 8), int
            ),
            max_output_tokens=_env(
                "MLA_MAX_OUTPUT_TOKENS", agent.get("max_output_tokens", 2048), int
            ),
            temperature=_env("MLA_TEMPERATURE", agent.get("temperature", 0.1), float),
            write_policy=str(
                _env(
                    "MLA_WRITE_POLICY",
                    agent.get("write_policy", "confirm"),
                    str,
                )
            )
            .strip()
            .casefold(),
            max_file_bytes=_env(
                "MLA_MAX_FILE_BYTES", tools.get("max_file_bytes", 200_000), int
            ),
            max_list_results=_env(
                "MLA_MAX_LIST_RESULTS", tools.get("max_list_results", 200), int
            ),
            max_search_results=_env(
                "MLA_MAX_SEARCH_RESULTS", tools.get("max_search_results", 100), int
            ),
            max_search_files=_env(
                "MLA_MAX_SEARCH_FILES", tools.get("max_search_files", 500), int
            ),
        )
