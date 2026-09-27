from dataclasses import replace

import pytest

from minimal_local_agent.config import Settings
from minimal_local_agent.policy import PolicyEngine
from minimal_local_agent.read_tools import ReadTool


def _example(_ctx: object, path: str) -> int:
    """Count lines in a workspace file."""
    return len(path)


def test_read_tool_requires_a_distinct_documented_name() -> None:
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
