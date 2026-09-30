"""Configuration loading with TOML defaults and environment overrides."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

DEFAULT_MODEL = "qwen3.5:9b"
DEFAULT_BASE_URL = "http://localhost:11434/v1"
MODEL_PROVIDERS = frozenset({"ollama", "openai-compatible"})
WRITE_POLICIES = frozenset({"confirm", "deny", "preview"})
POLICY_DECISIONS = frozenset({"allow", "ask", "deny", "preview"})
READ_TOOLS = frozenset({"list_files", "read_file", "search_text"})
WRITE_TOOLS = frozenset({"write_file", "edit_files"})
_MCP_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
_MCP_TOOL_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TOML_FIELDS = {
    "agent": {
        "model": str,
        "base_url": str,
        "provider": str,
        "api_key_env": str,
        "request_limit": int,
        "tool_calls_limit": int,
        "max_output_tokens": int,
        "max_context_bytes": int,
        "temperature": (int, float),
        "write_policy": str,
    },
    "paths": {"workspace": str, "database": str},
    "tools": {
        "max_file_bytes": int,
        "max_list_results": int,
        "max_search_results": int,
        "max_search_files": int,
        "max_transaction_files": int,
        "max_diff_chars": int,
        "max_mcp_result_chars": int,
    },
    "policy": {"tools": dict},
    "mcp": {"servers": list},
}


def _validate_fields(table: dict[str, Any], name: str, fields: dict[str, Any]) -> None:
    unknown = sorted(table.keys() - fields.keys())
    if unknown:
        raise ValueError(f"Unknown {name} field(s): {', '.join(unknown)}")
    for key, value in table.items():
        if isinstance(value, bool) or not isinstance(value, fields[key]):
            raise ValueError(f"Invalid type for {name}.{key}")


@dataclass(frozen=True, slots=True)
class MCPServerSettings:
    """One loopback MCP endpoint with an explicit read-only tool allowlist."""

    name: str
    url: str
    allow_tools: tuple[str, ...]

    def __post_init__(self) -> None:
        if not _MCP_NAME.fullmatch(self.name):
            raise ValueError(
                "MCP server name must start with a letter and contain only "
                "letters, numbers, underscores, or hyphens"
            )
        parsed = urlparse(self.url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"MCP server {self.name} must use an http(s) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError(
                f"MCP server {self.name} URL cannot contain credentials, query, "
                "or fragment"
            )
        if parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError(f"MCP server {self.name} must be loopback-only")
        if not self.allow_tools:
            raise ValueError(f"MCP server {self.name} requires allow_tools")
        if len(self.allow_tools) != len(set(self.allow_tools)):
            raise ValueError(f"MCP server {self.name} has duplicate allow_tools")
        if any(not tool.strip() for tool in self.allow_tools):
            raise ValueError(f"MCP server {self.name} has an empty tool name")
        for tool in self.allow_tools:
            if not _MCP_TOOL_NAME.fullmatch(tool):
                raise ValueError(
                    f"MCP tool {tool!r} must be an OpenAI-compatible function name"
                )
            if len(f"{self.prefix}{tool}") > 64:
                raise ValueError(
                    f"Prefixed MCP tool name is longer than 64 characters: {tool}"
                )

    @property
    def prefix(self) -> str:
        return f"mcp_{self.name.replace('-', '_')}_"

    @property
    def visible_tools(self) -> tuple[str, ...]:
        return tuple(f"{self.prefix}{tool}" for tool in self.allow_tools)


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
    provider: str = "ollama"
    api_key_env: str | None = None
    workspace: Path = field(default_factory=lambda: Path("workspace").resolve())
    database: Path = field(
        default_factory=lambda: Path(".minimal-local-agent/state.db").resolve()
    )
    request_limit: int = 6
    tool_calls_limit: int = 8
    max_output_tokens: int = 2048
    max_context_bytes: int = 64_000
    temperature: float = 0.1
    write_policy: str = "confirm"
    max_file_bytes: int = 200_000
    max_list_results: int = 200
    max_search_results: int = 100
    max_search_files: int = 500
    max_transaction_files: int = 8
    max_diff_chars: int = 40_000
    max_mcp_result_chars: int = 100_000
    tool_policies: tuple[tuple[str, str], ...] = ()
    mcp_servers: tuple[MCPServerSettings, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("model cannot be empty")
        if not isinstance(self.base_url, str):
            raise ValueError("base_url must be a valid http(s) URL")
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be a valid http(s) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("base_url cannot contain credentials, query, or fragment")
        if self.provider not in MODEL_PROVIDERS:
            raise ValueError(
                f"provider must be one of: {', '.join(sorted(MODEL_PROVIDERS))}"
            )
        if self.api_key_env is not None and not _ENV_NAME.fullmatch(self.api_key_env):
            raise ValueError("api_key_env must be an environment variable name")
        if (
            self.api_key_env
            and parsed.scheme != "https"
            and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        ):
            raise ValueError("API keys require HTTPS outside loopback endpoints")
        for name in (
            "request_limit",
            "tool_calls_limit",
            "max_output_tokens",
            "max_context_bytes",
            "max_file_bytes",
            "max_list_results",
            "max_search_results",
            "max_search_files",
            "max_transaction_files",
            "max_diff_chars",
            "max_mcp_result_chars",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if (
            isinstance(self.temperature, bool)
            or not isinstance(self.temperature, (int, float))
            or not 0 <= self.temperature <= 2
        ):
            raise ValueError("temperature must be between 0 and 2")
        if self.write_policy not in WRITE_POLICIES:
            allowed = ", ".join(sorted(WRITE_POLICIES))
            raise ValueError(f"write_policy must be one of: {allowed}")
        policy_names = [name for name, _decision in self.tool_policies]
        if len(policy_names) != len(set(policy_names)):
            raise ValueError("Tool policy names must be unique")
        for name, decision in self.tool_policies:
            if not name.strip() or decision not in POLICY_DECISIONS:
                raise ValueError(f"Invalid tool policy: {name!r}={decision!r}")
            if name in WRITE_TOOLS and decision == "allow":
                raise ValueError(f"Write tool {name} cannot bypass approval")
            if name not in WRITE_TOOLS and decision not in {"allow", "deny"}:
                raise ValueError(f"Read tool {name} only supports allow or deny")
        server_names = [server.name for server in self.mcp_servers]
        if len(server_names) != len(set(server_names)):
            raise ValueError("MCP server names must be unique")
        visible_tools = [
            tool for server in self.mcp_servers for tool in server.visible_tools
        ]
        if len(visible_tools) != len(set(visible_tools)):
            raise ValueError("MCP tool names collide after prefixing")

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

        _validate_fields(raw, "configuration", dict.fromkeys(_TOML_FIELDS, dict))
        for name, fields in _TOML_FIELDS.items():
            _validate_fields(raw.get(name, {}), name, fields)

        agent = raw.get("agent", {})
        paths = raw.get("paths", {})
        tools = raw.get("tools", {})
        policy = raw.get("policy", {})
        mcp = raw.get("mcp", {})
        policy_tools = policy.get("tools", {})
        if not all(isinstance(decision, str) for decision in policy_tools.values()):
            raise ValueError("policy.tools must map tool names to string decisions")

        tool_policies = {
            str(name).strip(): str(decision).strip().casefold()
            for name, decision in policy_tools.items()
        }
        disabled = os.getenv("MLA_DISABLED_TOOLS", "")
        for name in disabled.split(","):
            if name.strip():
                tool_policies[name.strip()] = "deny"

        raw_servers = mcp.get("servers", [])
        if not isinstance(raw_servers, list) or not all(
            isinstance(server, dict) for server in raw_servers
        ):
            raise ValueError("mcp.servers must be an array of TOML tables")
        mcp_servers: list[MCPServerSettings] = []
        for server in raw_servers:
            _validate_fields(
                server, "mcp.servers", {"name": str, "url": str, "allow_tools": list}
            )
            allow_tools = server.get("allow_tools", [])
            if not isinstance(allow_tools, list) or not all(
                isinstance(tool, str) for tool in allow_tools
            ):
                raise ValueError("MCP allow_tools must be an array of strings")
            mcp_servers.append(
                MCPServerSettings(
                    name=str(server.get("name", "")).strip(),
                    url=str(server.get("url", "")).strip().rstrip("/"),
                    allow_tools=tuple(tool.strip() for tool in allow_tools),
                )
            )

        model = str(_env("MLA_MODEL", agent.get("model", DEFAULT_MODEL), str)).strip()
        base_url = str(
            _env("MLA_BASE_URL", agent.get("base_url", DEFAULT_BASE_URL), str)
        ).rstrip("/")
        if not model:
            raise ValueError("model cannot be empty")

        return cls(
            model=model,
            base_url=base_url,
            provider=str(_env("MLA_PROVIDER", agent.get("provider", "ollama"), str))
            .strip()
            .casefold(),
            api_key_env=(
                str(
                    _env(
                        "MLA_API_KEY_ENV",
                        agent.get("api_key_env", ""),
                        str,
                    )
                ).strip()
                or None
            ),
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
            max_context_bytes=_env(
                "MLA_MAX_CONTEXT_BYTES", agent.get("max_context_bytes", 64_000), int
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
            max_transaction_files=_env(
                "MLA_MAX_TRANSACTION_FILES",
                tools.get("max_transaction_files", 8),
                int,
            ),
            max_diff_chars=_env(
                "MLA_MAX_DIFF_CHARS", tools.get("max_diff_chars", 40_000), int
            ),
            max_mcp_result_chars=_env(
                "MLA_MAX_MCP_RESULT_CHARS",
                tools.get("max_mcp_result_chars", 100_000),
                int,
            ),
            tool_policies=tuple(sorted(tool_policies.items())),
            mcp_servers=tuple(mcp_servers),
        )


__all__ = ["MCPServerSettings", "MODEL_PROVIDERS", "Settings"]
