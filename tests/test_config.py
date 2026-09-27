from pathlib import Path

import pytest

from minimal_local_agent.config import MCPServerSettings, Settings


def test_loads_toml_and_resolves_relative_paths(tmp_path: Path) -> None:
    config = tmp_path / "agent.toml"
    config.write_text(
        """
[agent]
model = "qwen3.5:4b"
request_limit = 3
write_policy = "deny"

[paths]
workspace = "files"
database = "state/agent.db"

[tools]
max_transaction_files = 4
max_diff_chars = 12000
""".strip(),
        encoding="utf-8",
    )

    settings = Settings.load(config)

    assert settings.model == "qwen3.5:4b"
    assert settings.request_limit == 3
    assert settings.write_policy == "deny"
    assert settings.max_transaction_files == 4
    assert settings.max_diff_chars == 12_000
    assert settings.workspace == (tmp_path / "files").resolve()
    assert settings.database == (tmp_path / "state/agent.db").resolve()


def test_environment_overrides_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = tmp_path / "agent.toml"
    config.write_text("[agent]\nmodel = 'qwen3.5:4b'", encoding="utf-8")
    monkeypatch.setenv("MLA_MODEL", "qwen3.5:9b")
    monkeypatch.setenv("MLA_REQUEST_LIMIT", "4")
    monkeypatch.setenv("MLA_WRITE_POLICY", "deny")

    settings = Settings.load(config)

    assert settings.model == "qwen3.5:9b"
    assert settings.request_limit == 4
    assert settings.write_policy == "deny"


def test_loads_compatible_provider_without_storing_the_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = tmp_path / "agent.toml"
    config.write_text(
        '[agent]\nprovider = "openai-compatible"\n'
        'model = "example-model"\nbase_url = "https://example.test/v1"\n'
        'api_key_env = "TEST_MODEL_KEY"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("TEST_MODEL_KEY", "not-in-settings")

    settings = Settings.load(config)

    assert settings.provider == "openai-compatible"
    assert settings.api_key_env == "TEST_MODEL_KEY"
    assert "not-in-settings" not in repr(settings)


def test_rejects_invalid_limits(tmp_path: Path) -> None:
    config = tmp_path / "agent.toml"
    config.write_text("[agent]\nrequest_limit = 0", encoding="utf-8")

    with pytest.raises(ValueError, match="request_limit"):
        Settings.load(config)


def test_rejects_invalid_write_policy(tmp_path: Path) -> None:
    config = tmp_path / "agent.toml"
    config.write_text("[agent]\nwrite_policy = 'always'", encoding="utf-8")

    with pytest.raises(ValueError, match="write_policy"):
        Settings.load(config)


def test_accepts_preview_write_policy(tmp_path: Path) -> None:
    config = tmp_path / "agent.toml"
    config.write_text("[agent]\nwrite_policy = 'preview'", encoding="utf-8")

    assert Settings.load(config).write_policy == "preview"


def test_loads_loopback_mcp_with_explicit_allowlist(tmp_path: Path) -> None:
    config = tmp_path / "agent.toml"
    config.write_text(
        """
[[mcp.servers]]
name = "notes"
url = "http://127.0.0.1:8000/mcp"
allow_tools = ["search_notes", "read_note"]

[policy.tools]
mcp_notes_read_note = "deny"
""".strip(),
        encoding="utf-8",
    )

    settings = Settings.load(config)

    assert settings.mcp_servers[0].visible_tools == (
        "mcp_notes_search_notes",
        "mcp_notes_read_note",
    )
    assert dict(settings.tool_policies)["mcp_notes_read_note"] == "deny"


def test_mcp_rejects_remote_or_implicit_tool_access() -> None:
    with pytest.raises(ValueError, match="loopback-only"):
        MCPServerSettings(
            name="remote",
            url="https://example.com/mcp",
            allow_tools=("search",),
        )
    with pytest.raises(ValueError, match="requires allow_tools"):
        MCPServerSettings(
            name="local",
            url="http://localhost:8000/mcp",
            allow_tools=(),
        )
