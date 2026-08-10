from dataclasses import replace
from pathlib import Path

from pydantic_ai.models.test import TestModel

from minimal_local_agent.agent import build_agent
from minimal_local_agent.config import Settings
from minimal_local_agent.policy import PolicyEngine
from minimal_local_agent.runtime import AgentRuntime


class _TestModelFactory:
    def create(self, _settings: Settings) -> TestModel:
        return TestModel(call_tools=[], custom_output_text="done")


def _tool_names(settings: Settings) -> set[str]:
    agent = build_agent(settings)
    return set(agent.toolsets[0].tools)


def test_read_only_policy_removes_write_tool() -> None:
    settings = Settings()

    assert _tool_names(settings) == {
        "edit_files",
        "list_files",
        "read_file",
        "search_text",
        "write_file",
    }
    assert _tool_names(replace(settings, write_policy="deny")) == {
        "list_files",
        "read_file",
        "search_text",
    }
    assert _tool_names(replace(settings, write_policy="preview")) == {
        "edit_files",
        "list_files",
        "read_file",
        "search_text",
        "write_file",
    }


def test_model_facing_read_tools_do_not_expose_glob_patterns() -> None:
    agent = build_agent(Settings(write_policy="deny"))
    tools = agent.toolsets[0].tools

    assert set(tools["list_files"].function_schema.json_schema["properties"]) == {
        "path"
    }
    assert set(tools["search_text"].function_schema.json_schema["properties"]) == {
        "query",
        "path",
        "case_sensitive",
    }


def test_explicit_policy_is_compiled_into_tool_surface() -> None:
    settings = Settings(tool_policies=(("search_text", "deny"),))

    assert "search_text" not in _tool_names(settings)


def test_global_write_mode_is_a_hard_policy_ceiling() -> None:
    override = (("write_file", "ask"), ("edit_files", "ask"))

    assert _tool_names(Settings(write_policy="deny", tool_policies=override)) == {
        "list_files",
        "read_file",
        "search_text",
    }
    preview = Settings(write_policy="preview", tool_policies=override)
    assert set(_tool_names(preview)) >= {"write_file", "edit_files"}
    policy = PolicyEngine(preview)
    assert policy.decision("write_file", "write") == "preview"
    assert policy.decision("edit_files", "write") == "preview"


def test_runtime_emits_events_and_records_receipt(tmp_path: Path) -> None:
    settings = Settings(
        workspace=tmp_path / "workspace",
        database=tmp_path / "state.db",
        write_policy="deny",
    )
    runtime = AgentRuntime(settings, model_factory=_TestModelFactory())
    events = []

    outcome = runtime.run("say done", event_handler=events.append)

    assert outcome.response == "done"
    assert len(outcome.receipt_hash) == 64
    assert [event.type for event in events] == ["run.started", "run.completed"]
    assert runtime.store.verify_receipt_chain(outcome.session_id) == (True, None)
    receipt = runtime.store.get_receipts(outcome.session_id)[0]["receipt"]
    assert receipt["prompt_sha256"]
    assert receipt["response_sha256"]
    assert receipt["policy_sha256"]


def test_event_handler_failure_does_not_change_run_semantics(tmp_path: Path) -> None:
    settings = Settings(
        workspace=tmp_path / "workspace",
        database=tmp_path / "state.db",
        write_policy="deny",
    )
    runtime = AgentRuntime(settings, model_factory=_TestModelFactory())

    def broken_handler(_event: object) -> None:
        raise RuntimeError("observer failed")

    outcome = runtime.run("say done", event_handler=broken_handler)

    assert outcome.response == "done"
    assert outcome.event_handler_errors == (
        "observer failed",
        "observer failed",
    )
    assert runtime.store.get_runs(outcome.session_id)[0]["success"] == 1
