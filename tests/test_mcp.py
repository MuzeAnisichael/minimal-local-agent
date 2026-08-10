import asyncio
import socket
from contextlib import suppress
from pathlib import Path

import pytest
from pydantic_ai.models.test import TestModel

from minimal_local_agent.config import MCPServerSettings, Settings
from minimal_local_agent.runtime import AgentRuntime

pytest.importorskip("fastmcp.client")
FastMCP = pytest.importorskip("mcp.server.fastmcp").FastMCP
pytestmark = pytest.mark.filterwarnings(
    "ignore::pydantic_settings.exceptions.IncompleteFieldDefinitionWarning"
)


class _MCPTestModelFactory:
    def create(self, _settings: Settings) -> TestModel:
        return TestModel(call_tools=["mcp_demo_allowed_echo"])


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


async def _wait_until_listening(port: int) -> None:
    for _ in range(100):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                return
        except OSError:
            await asyncio.sleep(0.05)
    raise TimeoutError("MCP test server did not start")


async def _exercise_mcp(tmp_path: Path) -> None:
    port = _free_port()
    server = FastMCP(
        "test",
        host="127.0.0.1",
        port=port,
        log_level="ERROR",
        stateless_http=True,
        json_response=True,
    )

    @server.tool()
    def allowed_echo(value: str) -> str:
        return f"echo:{value}"

    @server.tool()
    def hidden_write(value: str) -> str:
        return f"should-not-run:{value}"

    server_task = asyncio.create_task(server.run_streamable_http_async())
    try:
        await _wait_until_listening(port)
        settings = Settings(
            workspace=tmp_path / "workspace",
            database=tmp_path / "state.db",
            write_policy="deny",
            mcp_servers=(
                MCPServerSettings(
                    name="demo",
                    url=f"http://127.0.0.1:{port}/mcp",
                    allow_tools=("allowed_echo",),
                ),
            ),
        )
        runtime = AgentRuntime(settings, model_factory=_MCPTestModelFactory())

        outcome = await asyncio.to_thread(runtime.run, "Call the allowed echo tool")

        events = runtime.store.get_tool_events(outcome.session_id)
        assert [event["tool_name"] for event in events] == ["mcp_demo_allowed_echo"]
        assert events[0]["status"] == "ok"
        assert events[0]["details"]["argument_names"] == ["value"]
        assert "hidden_write" not in outcome.response
        assert runtime.store.verify_receipt_chain(outcome.session_id) == (True, None)
    finally:
        server_task.cancel()
        with suppress(asyncio.CancelledError):
            await server_task


def test_allowlisted_mcp_tool_executes_and_is_audited(tmp_path: Path) -> None:
    asyncio.run(_exercise_mcp(tmp_path))
