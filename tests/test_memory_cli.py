import json
import os
from dataclasses import replace

import pytest

from minimal_local_agent import cli
from minimal_local_agent.config import Settings
from minimal_local_agent.extension_context import memory_scope
from minimal_local_agent.memory import SQLiteMemoryStore


@pytest.fixture
def config(tmp_path, monkeypatch):
    for key in tuple(os.environ):
        if key.startswith("MLA_"):
            monkeypatch.delenv(key)
    path = tmp_path / "agent.toml"
    path.write_text(
        '[agent]\nprovider="openai-compatible"\n'
        'base_url="https://example.test/v1"\napi_key_env="MISSING_CLI_TEST_KEY"\n'
        '[paths]\nworkspace="files"\ndatabase="state/audit.db"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("MISSING_CLI_TEST_KEY", raising=False)
    monkeypatch.setattr(
        cli,
        "AgentRuntime",
        lambda *_a, **_k: pytest.fail("Memory management must not build a model"),
    )
    return path


def invoke(config, capsys, *args):
    assert cli.main(["--config", str(config), "memory", *args, "--json"]) == 0
    return json.loads(capsys.readouterr().out)["records"]


def test_human_proposes_reviews_approves_then_forgets(config, capsys):
    candidate = invoke(config, capsys, "propose", "Prefer concise Chinese answers")[0]
    assert candidate["status"] == "candidate" and candidate["source"] == "human:cli"
    assert invoke(config, capsys, "list", "--status", "approved") == []
    reviewed = invoke(config, capsys, "show", candidate["id"])[0]
    assert reviewed == candidate
    approved = invoke(config, capsys, "approve", candidate["id"], "--revision", "1")[0]
    assert approved["status"] == "approved" and approved["revision"] == 2
    forgotten = invoke(config, capsys, "forget", candidate["id"], "--revision", "2")[0]
    assert forgotten["status"] == "forgotten"
    assert invoke(config, capsys, "list", "--status", "approved") == []
    settings = Settings.load(config)
    assert not settings.memory_enabled
    assert not settings.database.exists()  # No audit DB/model side effect.


def test_entity_updates_are_candidates_until_approved(config, capsys):
    base = invoke(
        config,
        capsys,
        "propose",
        "Project uses Python",
        "--entity",
        "project",
        "--attribute",
        "language=Python",
        "--attribute",
        "version=3.11",
    )[0]
    invoke(config, capsys, "approve", base["id"], "--revision", "1")
    update = invoke(
        config,
        capsys,
        "update",
        base["id"],
        "Project uses Python 3.12",
        "--revision",
        "2",
        "--attribute",
        "version=3.12",
    )[0]
    assert update["status"] == "candidate" and update["replaces_id"] == base["id"]
    assert update["attributes"] == {"version": "3.12"}
    assert invoke(config, capsys, "show", base["id"])[0]["status"] == "approved"
    invoke(config, capsys, "approve", update["id"], "--revision", "1")
    assert invoke(config, capsys, "show", base["id"])[0]["status"] == "superseded"


def test_reject_and_stale_revision_do_not_approve(config, capsys):
    candidate = invoke(config, capsys, "propose", "Wrong candidate")[0]
    assert (
        cli.main(
            [
                "--config",
                str(config),
                "memory",
                "approve",
                candidate["id"],
                "--revision",
                "2",
            ]
        )
        == 2
    )
    assert "revision changed" in capsys.readouterr().err
    rejected = invoke(config, capsys, "reject", candidate["id"], "--revision", "1")[0]
    assert rejected["status"] == "rejected"


@pytest.mark.parametrize("attribute", ["missing-value", "=value", "duplicate=value"])
def test_bad_attributes_do_not_create_records(config, capsys, attribute):
    args = ["propose", "entity", "--entity", "item", "--attribute", attribute]
    if attribute.startswith("duplicate"):
        args += ["--attribute", attribute]
    assert cli.main(["--config", str(config), "memory", *args]) == 2
    capsys.readouterr()
    assert invoke(config, capsys, "list") == []


def test_scopes_and_limits_are_enforced(config, capsys):
    candidate = invoke(config, capsys, "propose", "private record")[0]
    configured = Settings.load(config)
    foreign = SQLiteMemoryStore(configured.database.with_name("memory.db"))
    assert foreign.get("another-project", candidate["id"]) is None
    assert cli.main(["--config", str(config), "memory", "list", "--limit", "101"]) == 2
    assert "limit" in capsys.readouterr().err


def test_memory_capabilities_are_optional_and_honest(config, capsys):
    assert (
        cli.main(["--config", str(config), "capabilities", "--memory", "--json"]) == 0
    )
    manifest = json.loads(capsys.readouterr().out)["capabilities"]
    memory = {item["name"]: item for item in manifest if item["source"] == "memory"}
    assert memory["memory_propose"]["effect"] == "memory-candidate"
    assert "memory_approve" not in memory
    assert not Settings.load(config).database.exists()


def test_runtime_flags_are_additive():
    parser = cli._build_parser()
    for command in ("run", "chat", "web"):
        args = parser.parse_args(
            [command, *(["task"] if command == "run" else []), "--memory", "--planner"]
        )
        assert args.memory and args.planner


def test_toml_optional_modules_and_explicit_scope(tmp_path):
    config = tmp_path / "agent.toml"
    config.write_text(
        '[memory]\nenabled=true\ndatabase="notes.db"\nscope="chosen-project"\n'
        "[planner]\nenabled=true\n",
        encoding="utf-8",
    )
    settings = Settings.load(config)
    assert settings.memory_enabled and settings.planning_enabled
    assert settings.memory_database == tmp_path / "notes.db"
    assert memory_scope(settings) == "chosen-project"


@pytest.mark.parametrize(
    "text",
    [
        "[memory]\nenabled=1",
        "[planner]\nenabled='true'",
        "[memory]\nscope=''",
        "[memory]\nenabeld=true",
    ],
)
def test_invalid_optional_configuration_is_not_silently_ignored(tmp_path, text):
    config = tmp_path / "agent.toml"
    config.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        Settings.load(config)


@pytest.mark.skipif(os.name == "nt", reason="Windows workspace names ignore case")
def test_default_scope_keeps_case_sensitive_workspaces_separate(tmp_path):
    settings = Settings(workspace=tmp_path / "Project")
    assert memory_scope(settings) != memory_scope(
        replace(settings, workspace=tmp_path / "project")
    )
