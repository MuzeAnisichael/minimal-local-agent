from dataclasses import replace
from pathlib import Path

import pytest
from pydantic_ai import (
    ModelRequest,
    ModelResponse,
    RunContext,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.messages import ModelMessagesTypeAdapter
from pydantic_ai.models.test import TestModel

from minimal_local_agent.agent import AgentDependencies
from minimal_local_agent.config import Settings
from minimal_local_agent.context import ContextBudgetExceeded, select_context
from minimal_local_agent.read_tools import ReadTool
from minimal_local_agent.runtime import AgentRuntime


class _ModelFactory:
    def __init__(self, tools: list[str] | None = None) -> None:
        self.tools = tools or []

    def create(self, _settings: Settings) -> TestModel:
        return TestModel(call_tools=self.tools, custom_output_text="done")


def _settings(tmp_path: Path, *, max_context_bytes: int = 64_000) -> Settings:
    return Settings(
        workspace=tmp_path / "workspace",
        database=tmp_path / "state.db",
        write_policy="deny",
        max_context_bytes=max_context_bytes,
    )


def test_context_rejects_by_default_and_accepts_explicit_reducer() -> None:
    messages = [
        ModelRequest(parts=[UserPromptPart(content="old" * 200)]),
        ModelResponse(parts=[TextPart(content="done")]),
        ModelRequest(parts=[UserPromptPart(content="latest")]),
    ]
    limit = len(ModelMessagesTypeAdapter.dump_json(messages[-1:])) + 10

    with pytest.raises(ContextBudgetExceeded) as failure:
        select_context(messages, limit)
    assert failure.value.details["original_bytes"] > limit

    selected = select_context(messages, limit, lambda history, _limit: history[-1:])
    assert selected.messages == messages[-1:]
    assert selected.details["reduced"] is True
    assert selected.details["submitted_bytes"] <= limit
    with pytest.raises(ValueError, match="latest message"):
        select_context(messages, limit, lambda history, _limit: history[:-1])


def test_overflow_fails_before_model_call_and_leaves_history_intact(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, max_context_bytes=1)
    runtime = AgentRuntime(settings, model_factory=_ModelFactory())
    session_id = runtime.store.new_session()
    events = []

    with pytest.raises(ContextBudgetExceeded, match="start a new session"):
        runtime.run("say done", session_id=session_id, event_handler=events.append)

    assert runtime.store.load_history(session_id) is None
    receipt = runtime.store.get_receipts(session_id)[0]["receipt"]
    assert receipt["success"] is False
    assert receipt["context_checks"][0]["status"] == "rejected"
    assert [event.type for event in events] == [
        "run.started",
        "context.rejected",
        "run.failed",
    ]


def test_reducer_changes_model_view_but_not_persisted_history(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    runtime = AgentRuntime(settings, model_factory=_ModelFactory())
    first = runtime.run("A" * 1000)
    original = runtime.store.load_history(first.session_id)
    assert original is not None

    reduced_runtime = AgentRuntime(
        replace(settings, max_context_bytes=1500),
        model_factory=_ModelFactory(),
        context_reducer=lambda messages, _limit: messages[-1:],
    )
    second = reduced_runtime.run("second", session_id=first.session_id)

    saved = reduced_runtime.store.load_history(first.session_id)
    assert saved is not None
    assert "A" * 1000 in saved
    assert "second" in saved
    assert len(ModelMessagesTypeAdapter.validate_json(saved)) == 4
    receipt = reduced_runtime.store.get_receipts(second.session_id)[-1]["receipt"]
    assert receipt["context_checks"][0]["status"] == "reduced"
    assert receipt["context_checks"][0]["submitted_bytes"] <= 1500


def test_budget_is_checked_again_after_a_tool_result(tmp_path: Path) -> None:
    def payload(_ctx: RunContext[AgentDependencies]) -> str:
        """Return a large read-only result."""
        return "X" * 1000

    runtime = AgentRuntime(
        _settings(tmp_path, max_context_bytes=1500),
        model_factory=_ModelFactory(["payload"]),
        read_tools=(ReadTool("payload", payload),),
    )
    session_id = runtime.store.new_session()

    with pytest.raises(ContextBudgetExceeded):
        runtime.run("Read the payload", session_id=session_id)

    assert runtime.store.get_tool_events(session_id)[0]["tool_name"] == "payload"
    receipt = runtime.store.get_receipts(session_id)[0]["receipt"]
    assert [check["status"] for check in receipt["context_checks"]] == [
        "ok",
        "rejected",
    ]
