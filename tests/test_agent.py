from dataclasses import replace

from minimal_local_agent.agent import build_agent
from minimal_local_agent.config import Settings


def _tool_names(settings: Settings) -> set[str]:
    agent = build_agent(settings)
    return set(agent.toolsets[0].tools)


def test_read_only_policy_removes_write_tool() -> None:
    settings = Settings()

    assert _tool_names(settings) == {
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
