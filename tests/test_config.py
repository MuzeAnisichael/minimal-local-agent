from pathlib import Path

import pytest

from minimal_local_agent.config import Settings


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
""".strip(),
        encoding="utf-8",
    )

    settings = Settings.load(config)

    assert settings.model == "qwen3.5:4b"
    assert settings.request_limit == 3
    assert settings.write_policy == "deny"
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
