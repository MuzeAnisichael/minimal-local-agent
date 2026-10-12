from pathlib import Path
from threading import Event
from time import monotonic, sleep

import pytest
from pydantic_ai.models.test import TestModel

from minimal_local_agent.config import MCPServerSettings, Settings
from minimal_local_agent.peers import (
    LocalPeer,
    PeerResult,
    loopback_endpoint,
    validate_call,
)
from minimal_local_agent.store import AuditStore


class TestFactory:
    __test__ = False

    def __init__(self) -> None:
        self.settings: list[Settings] = []

    def create(self, settings: Settings) -> TestModel:
        self.settings.append(settings)
        return TestModel(call_tools=[], custom_output_text="read-only answer")


def settings(tmp_path: Path) -> Settings:
    return Settings(
        workspace=tmp_path / "child-workspace", database=tmp_path / "child.db"
    )


def call(peer: LocalPeer, prompt: str = "isolated task") -> PeerResult:
    return peer.call(
        prompt,
        timeout=10.0,
        request_limit=2,
        tool_calls_limit=2,
        max_output_tokens=1024,
    )


def test_local_peer_caps_budget_and_never_exposes_write_or_mcp(tmp_path: Path) -> None:
    factory = TestFactory()
    configured = Settings(
        workspace=tmp_path / "workspace",
        database=tmp_path / "state.db",
        request_limit=1,
        tool_calls_limit=1,
        max_output_tokens=512,
        write_policy="confirm",
        mcp_servers=(MCPServerSettings("hidden", "http://127.0.0.1:12345", ("read",)),),
    )
    peer = LocalPeer(configured, model_factory=factory)
    result = call(peer)

    assert result.state == "completed"
    assert result.response == "read-only answer"
    assert result.usage["requests"] == 1
    actual = factory.settings[0]
    assert actual.request_limit == actual.tool_calls_limit == 1
    assert actual.max_output_tokens == 512
    assert actual.write_policy == "deny"
    assert actual.mcp_servers == ()
    assert configured.write_policy == "confirm"
    assert peer.get_task(result.task_id) == result


def test_local_peer_never_reuses_other_task_history(tmp_path: Path) -> None:
    peer = LocalPeer(settings(tmp_path), model_factory=TestFactory())
    first = call(peer, "private first task")
    second = call(peer, "second task")

    assert first.task_id != second.task_id
    history = AuditStore(peer.settings.database).load_history(second.task_id)
    assert "second task" in history
    assert "private first task" not in history


def test_local_peer_cannot_inherit_parent_memory_or_planner(tmp_path: Path) -> None:
    configured = Settings(
        workspace=tmp_path / "workspace",
        database=tmp_path / "state.db",
        memory_enabled=True,
        memory_database=tmp_path / "parent-knowledge.db",
        planning_enabled=True,
    )
    factory = TestFactory()
    result = call(LocalPeer(configured, model_factory=factory))

    assert result.state == "completed"
    assert factory.settings[0].memory_enabled is False
    assert factory.settings[0].planning_enabled is False
    assert not configured.memory_database.exists()


def test_local_peer_timeout_keeps_task_working_and_blocks_new_tasks(
    tmp_path: Path,
) -> None:
    entered, release = Event(), Event()

    class BlockingFactory(TestFactory):
        def create(self, settings: Settings) -> TestModel:
            entered.set()
            assert release.wait(10)
            return super().create(settings)

    peer = LocalPeer(settings(tmp_path), model_factory=BlockingFactory())
    try:
        result = peer.call(
            "held task",
            timeout=0.05,
            request_limit=1,
            tool_calls_limit=1,
            max_output_tokens=512,
        )
        assert entered.wait(2)
        assert result.state == "working"
        assert result.usage == {}
        with pytest.raises(RuntimeError, match="busy"):
            call(peer)
    finally:
        release.set()
    deadline = monotonic() + 10
    while peer.get_task(result.task_id).state == "working" and monotonic() < deadline:
        sleep(0.01)
    assert peer.get_task(result.task_id).state == "completed"


def test_local_peer_sanitizes_provider_failure(tmp_path: Path) -> None:
    class FailingFactory:
        def create(self, _settings: Settings) -> TestModel:
            raise ValueError("private credential")

    result = call(LocalPeer(settings(tmp_path), model_factory=FailingFactory()))

    assert result.state == "failed"
    assert result.usage == {}
    assert "private credential" not in result.response


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "http://127.0.0.2:9000",
        "http://localhost.evil:9000",
        "http://user@localhost:9000",
        "http://localhost:9000?a=1",
        "http://localhost:9000#fragment",
        "http://localhost:9000/other",
        "http://localhost:0",
        "http://localhost:invalid",
        "http://localhost\n:9000",
        "file://localhost",
    ],
)
def test_peer_rejects_non_explicit_origins(url: str) -> None:
    with pytest.raises(ValueError):
        loopback_endpoint(url)


def test_peer_normalizes_only_trailing_origin_slash() -> None:
    assert loopback_endpoint("http://127.0.0.1:9000/") == "http://127.0.0.1:9000"


@pytest.mark.parametrize("timeout", [0, -1, True, float("nan"), float("inf")])
def test_peer_rejects_invalid_timeouts(timeout: float) -> None:
    with pytest.raises(ValueError, match="timeout"):
        validate_call("task", timeout, 1, 1, 1)


@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_peer_rejects_invalid_budgets(value: int) -> None:
    with pytest.raises(ValueError, match="request_limit"):
        validate_call("task", 1.0, value, 1, 1)


@pytest.mark.parametrize("prompt", ["", " ", "x" * 32_001])
def test_peer_rejects_empty_or_excessive_tasks(prompt: str) -> None:
    with pytest.raises(ValueError):
        validate_call(prompt, 1.0, 1, 1, 1)


def test_unknown_usage_is_not_zero_usage() -> None:
    assert PeerResult("accepted", "task", "submitted").usage == {}
