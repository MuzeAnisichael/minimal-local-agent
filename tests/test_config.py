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

[paths]
workspace = "files"
database = "state/agent.db"
""".strip(),
        encoding="utf-8",
    )

    settings = Settings.load(config)

    assert settings.model == "qwen3.5:4b"
    assert settings.request_limit == 3
    assert settings.workspace == (tmp_path / "files").resolve()
    assert settings.database == (tmp_path / "state/agent.db").resolve()


def test_environment_overrides_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = tmp_path / "agent.toml"
    config.write_text("[agent]\nmodel = 'qwen3.5:4b'", encoding="utf-8")
    monkeypatch.setenv("MLA_MODEL", "qwen3.5:9b")
    monkeypatch.setenv("MLA_REQUEST_LIMIT", "4")

    settings = Settings.load(config)

    assert settings.model == "qwen3.5:9b"
    assert settings.request_limit == 4


def test_rejects_invalid_limits(tmp_path: Path) -> None:
    config = tmp_path / "agent.toml"
    config.write_text("[agent]\nrequest_limit = 0", encoding="utf-8")

    with pytest.raises(ValueError, match="request_limit"):
        Settings.load(config)
