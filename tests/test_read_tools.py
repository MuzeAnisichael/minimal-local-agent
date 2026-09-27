from dataclasses import replace
from pathlib import Path

import pytest
from pydantic_ai import RunContext
from pydantic_ai.models.test import TestModel

from minimal_local_agent import ReadTool as PublicReadTool
from minimal_local_agent.agent import AgentDependencies, AgentRuntime
from minimal_local_agent.config import Settings
from minimal_local_agent.policy import PolicyEngine
from minimal_local_agent.read_tools import ReadTool


def _example(_ctx: object, path: str) -> int:
    """Count lines in a workspace file."""
    return len(path)


def test_read_tool_requires_a_distinct_documented_name() -> None:
    assert PublicReadTool is ReadTool
    assert ReadTool("line_count", _example).name == "line_count"
    for name in ("write_file", "mcp_notes_search", "bad-name", ""):
        with pytest.raises(ValueError):
            ReadTool(name, _example)
    with pytest.raises(ValueError, match="description"):
        ReadTool("undocumented", lambda _ctx: None)


def test_python_read_tool_appears_in_policy_and_can_be_denied() -> None:
    settings = Settings()
    allowed = PolicyEngine(settings)
    denied = PolicyEngine(replace(settings, tool_policies=(("line_count", "deny"),)))

    capability = allowed.manifest(python_tools=("line_count",))[-1]
    assert capability == {
        "name": "line_count",
        "effect": "external-read",
        "decision": "allow",
        "source": "python",
    }
    assert denied.manifest(python_tools=("line_count",))[-1]["decision"] == "deny"
    assert allowed.fingerprint(python_tools=("line_count",)) != denied.fingerprint(
        python_tools=("line_count",)
    )


class _ModelFactory:
    def __init__(self, call_tools: list[str]) -> None:
        self.model = TestModel(call_tools=call_tools, custom_output_text="done")

    def create(self, _settings: Settings) -> TestModel:
        return self.model


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    return Settings(
        workspace=tmp_path / "workspace",
        database=tmp_path / "state.db",
        write_policy="deny",
        **overrides,
    )


def test_python_read_tool_is_called_audited_and_receipted(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "a").write_text("one\ntwo\n", encoding="utf-8")

    def line_count(ctx: RunContext[AgentDependencies], path: str) -> int:
        """Count lines in a workspace file."""
        return len(ctx.deps.workspace.read_file(path).splitlines())

    model_factory = _ModelFactory(["line_count"])
    runtime = AgentRuntime(
        _settings(tmp_path),
        model_factory=model_factory,
        read_tools=(ReadTool("line_count", line_count),),
    )

    outcome = runtime.run("Count the lines in a")

    assert outcome.response == "done"
    events = runtime.store.get_tool_events(outcome.session_id)
    assert len(events) == 1
    assert events[0]["tool_name"] == "line_count"
    assert events[0]["status"] == "ok"
    details = events[0]["details"]
    assert details["argument_names"] == ["path"]
    assert len(details["arguments_sha256"]) == 64
    assert len(details["result_sha256"]) == 64
    assert "path" not in details
    receipt = runtime.store.get_receipts(outcome.session_id)[0]["receipt"]
    assert receipt["tool_events"][0]["tool_name"] == "line_count"
    assert receipt["policy"][-1] == {
        "name": "line_count",
        "effect": "external-read",
        "decision": "allow",
        "source": "python",
    }
    assert runtime.store.verify_receipt_chain(outcome.session_id) == (True, None)


def test_denied_python_read_tool_is_absent_from_model_surface(tmp_path: Path) -> None:
    def line_count(_ctx: RunContext[AgentDependencies]) -> int:
        """Count lines in a workspace file."""
        raise AssertionError("denied tool must not run")

    model_factory = _ModelFactory([])
    runtime = AgentRuntime(
        _settings(tmp_path, tool_policies=(("line_count", "deny"),)),
        model_factory=model_factory,
        read_tools=(ReadTool("line_count", line_count),),
    )

    outcome = runtime.run("Say done")

    parameters = model_factory.model.last_model_request_parameters
    assert parameters is not None
    assert "line_count" not in {tool.name for tool in parameters.function_tools}
    receipt = runtime.store.get_receipts(outcome.session_id)[0]["receipt"]
    assert receipt["policy"][-1]["decision"] == "deny"
    assert runtime.store.get_tool_events(outcome.session_id) == []


def test_read_tool_registry_rejects_duplicates_and_raw_toolsets(tmp_path: Path) -> None:
    declaration = ReadTool("line_count", _example)
    with pytest.raises(ValueError, match="unique"):
        AgentRuntime(_settings(tmp_path), read_tools=(declaration, declaration))
    with pytest.raises(TypeError, match="read_tools"):
        AgentRuntime(_settings(tmp_path), read_tools=(_example,))
    with pytest.raises(TypeError, match="toolsets"):
        AgentRuntime(_settings(tmp_path), toolsets=())


def test_python_read_tool_result_limit_records_an_error(tmp_path: Path) -> None:
    def oversized(_ctx: RunContext[AgentDependencies]) -> str:
        """Return a deliberately oversized value."""
        return "too-large"

    runtime = AgentRuntime(
        _settings(tmp_path, max_mcp_result_chars=4),
        model_factory=_ModelFactory(["oversized"]),
        read_tools=(ReadTool("oversized", oversized),),
    )

    with pytest.raises(ValueError):
        runtime.run("Call oversized")

    session_id = runtime.store.list_sessions()[0]["id"]
    events = runtime.store.get_tool_events(session_id)
    assert events
    assert all(event["status"] == "error" for event in events)
    assert all(event["details"]["error_type"] == "ValueError" for event in events)
    assert runtime.store.verify_receipt_chain(session_id) == (True, None)
