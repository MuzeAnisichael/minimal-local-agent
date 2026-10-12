import asyncio
import importlib
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Event, Thread
from time import monotonic, sleep
from types import SimpleNamespace

import httpx
import pytest
from pydantic_ai.models.test import TestModel

from minimal_local_agent.a2a_adapter import (
    A2APeer,
    _normalize_usage,
    _task_result,
    build_a2a_app,
)
from minimal_local_agent.config import Settings
from minimal_local_agent.execution_budget import ExecutionBudget
from minimal_local_agent.peers import LocalPeer

pytest.importorskip("a2a")
uvicorn = pytest.importorskip("uvicorn")

RestTransport = importlib.import_module("a2a.client.transports.rest").RestTransport
new_text_part = importlib.import_module("a2a.helpers.proto_helpers").new_text_part
agent_card_to_dict = importlib.import_module(
    "a2a.server.request_handlers.response_helpers"
).agent_card_to_dict
proto = importlib.import_module("a2a.types.a2a_pb2")
AgentCard, AgentInterface = proto.AgentCard, proto.AgentInterface
Artifact, CancelTaskRequest = proto.Artifact, proto.CancelTaskRequest
Task, TaskState, TaskStatus = proto.Task, proto.TaskState, proto.TaskStatus
TaskNotCancelableError = importlib.import_module(
    "a2a.utils.errors"
).TaskNotCancelableError


class TestFactory:
    __test__ = False

    def __init__(self, release: Event | None = None) -> None:
        self.settings: list[Settings] = []
        self.release = release
        self.entered = Event()

    def create(self, settings: Settings) -> TestModel:
        self.settings.append(settings)
        self.entered.set()
        if self.release is not None:
            assert self.release.wait(20)
        return TestModel(call_tools=[], custom_output_text="child answer")


@contextmanager
def server(peer: LocalPeer) -> Iterator[str]:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    endpoint = f"http://127.0.0.1:{listener.getsockname()[1]}"
    listener.listen()
    config = uvicorn.Config(
        build_a2a_app(peer, endpoint),
        host="127.0.0.1",
        log_level="error",
        timeout_graceful_shutdown=3,
    )
    running = uvicorn.Server(config)
    thread = Thread(target=running.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = monotonic() + 10
        while not running.started and thread.is_alive() and monotonic() < deadline:
            sleep(0.01)
        assert running.started
        yield endpoint
    finally:
        running.should_exit = True
        thread.join(5)
        listener.close()
        assert not thread.is_alive()


def child(tmp_path: Path, factory: TestFactory) -> LocalPeer:
    return LocalPeer(
        Settings(
            workspace=tmp_path / "child-workspace",
            database=tmp_path / "child.db",
            request_limit=2,
            tool_calls_limit=2,
            max_output_tokens=1024,
        ),
        model_factory=factory,
    )


def test_official_http_client_server_card_task_and_usage(tmp_path: Path) -> None:
    factory = TestFactory()
    local = child(tmp_path, factory)
    with server(local) as endpoint:
        remote = A2APeer(endpoint)
        budget = ExecutionBudget(4, 4, 2048)
        allocation = budget.reserve_peer(
            SimpleNamespace(requests=1, tool_calls=1, output_tokens=0)
        )
        result = remote.call(
            "one independent task",
            timeout=10,
            request_limit=allocation[0],
            tool_calls_limit=allocation[1],
            max_output_tokens=allocation[2],
        )
        assert result.state == "completed"
        assert result.response == "child answer"
        assert result.task_id
        assert result.usage["requests"] == 1
        for key in ("requests", "tool_calls", "input_tokens", "output_tokens"):
            assert type(result.usage[key]) is int
        budget.peer_completed(result.usage, allocation)
        assert budget.peer_usage == [result.usage]
        assert remote.get_task(result.task_id).response == result.response
        with httpx.Client(trust_env=False) as client:
            card = client.get(endpoint + "/.well-known/agent-card.json").json()
            assert card["supportedInterfaces"][0]["protocolBinding"] == "HTTP+JSON"
            assert card["supportedInterfaces"][0]["protocolVersion"] == "1.0"
            assert card["defaultInputModes"] == ["text/plain"]
        assert factory.settings[0].write_policy == "deny"
        assert factory.settings[0].request_limit == 1
        assert factory.settings[0].tool_calls_limit == 1
        assert factory.settings[0].max_output_tokens == 512
        assert list(local.settings.workspace.iterdir()) == []


def test_wait_timeout_does_not_forge_completion_or_cancel(tmp_path: Path) -> None:
    release = Event()
    factory = TestFactory(release)
    with server(child(tmp_path, factory)) as endpoint:
        remote = A2APeer(endpoint)
        try:
            result = remote.call(
                "held task",
                timeout=2.0,
                request_limit=1,
                tool_calls_limit=1,
                max_output_tokens=512,
            )
            assert factory.entered.wait(2)
            assert result.state in {"submitted", "working"}
            assert result.task_id
            assert result.usage == {}
            assert remote.get_task(result.task_id).state == "working"

            async def cancel() -> None:
                async with httpx.AsyncClient(
                    trust_env=False, headers={"A2A-Version": "1.0"}
                ) as client:
                    transport = RestTransport(client, AgentCard(), endpoint)
                    with pytest.raises(TaskNotCancelableError):
                        await transport.cancel_task(
                            CancelTaskRequest(id=result.task_id)
                        )

            asyncio.run(cancel())
            assert remote.get_task(result.task_id).state == "working"
        finally:
            release.set()
        deadline = monotonic() + 10
        while (
            remote.get_task(result.task_id).state == "working"
            and monotonic() < deadline
        ):
            sleep(0.02)
        assert remote.get_task(result.task_id).state == "completed"


def test_a2a_boundary_rejects_browser_origin_and_wrong_host(tmp_path: Path) -> None:
    with (
        server(child(tmp_path, TestFactory())) as endpoint,
        httpx.Client(trust_env=False) as client,
    ):
        assert (
            client.get(
                endpoint + "/.well-known/agent-card.json",
                headers={"Origin": "http://evil.example"},
            ).status_code
            == 403
        )
        assert (
            client.get(
                endpoint + "/.well-known/agent-card.json",
                headers={"Host": "evil.example"},
            ).status_code
            == 403
        )


def test_a2a_server_rejects_non_loopback_clients(tmp_path: Path) -> None:
    endpoint = "http://127.0.0.1:9000"
    app = build_a2a_app(child(tmp_path, TestFactory()), endpoint)

    async def request() -> None:
        transport = httpx.ASGITransport(app=app, client=("192.0.2.1", 50000))
        async with httpx.AsyncClient(transport=transport, base_url=endpoint) as client:
            assert (await client.get("/.well-known/agent-card.json")).status_code == 403

    asyncio.run(request())


@pytest.mark.parametrize("advertised", ["https://example.com", "http://127.0.0.1:9001"])
def test_card_cannot_redirect_delegation(
    advertised: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    card = AgentCard(
        supported_interfaces=[
            AgentInterface(
                url=advertised, protocol_binding="HTTP+JSON", protocol_version="1.0"
            )
        ]
    )

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json=agent_card_to_dict(card))

    original = httpx.AsyncClient

    def client(**kwargs: object) -> httpx.AsyncClient:
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        return original(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    with pytest.raises(ValueError):
        A2APeer("http://127.0.0.1:9000").call(
            "task",
            timeout=1,
            request_limit=1,
            tool_calls_limit=1,
            max_output_tokens=512,
        )
    assert calls == ["http://127.0.0.1:9000/.well-known/agent-card.json"]


def test_remote_omitted_usage_remains_unknown() -> None:
    from minimal_local_agent.a2a_adapter import _sdk

    task = Task(
        id="task-1",
        status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
        artifacts=[Artifact(parts=[new_text_part("answer")])],
    )
    result = _task_result(task, _sdk())
    assert result.response == "answer"
    assert result.usage == {}


@pytest.mark.parametrize("metadata", [{}, {"minimal_local_agent": {"note": "extra"}}])
def test_artifact_without_usage_cannot_erase_explicit_peer_counters(
    metadata: dict,
) -> None:
    from minimal_local_agent.a2a_adapter import _sdk

    reported = {"requests": 1, "tool_calls": 0, "input_tokens": 10, "output_tokens": 2}
    task = Task(
        id="task-1",
        status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
        artifacts=[
            Artifact(
                parts=[new_text_part("answer")],
                metadata={"minimal_local_agent": {"usage": reported}},
            ),
            Artifact(parts=[new_text_part("extra artifact")], metadata=metadata),
        ],
    )

    result = _task_result(task, _sdk())
    assert result.usage == reported
    assert all(type(result.usage[key]) is int for key in reported)


def test_explicit_empty_usage_is_unknown_not_inherited_or_zero_filled() -> None:
    from minimal_local_agent.a2a_adapter import _sdk

    task = Task(
        id="task-1",
        status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
        artifacts=[
            Artifact(metadata={"minimal_local_agent": {"usage": {"requests": 1}}}),
            Artifact(metadata={"minimal_local_agent": {"usage": {}}}),
        ],
    )

    assert _task_result(task, _sdk()).usage == {}


def test_usage_normalizes_struct_counters_without_inventing_missing_values() -> None:
    from minimal_local_agent.a2a_adapter import _sdk

    task = Task(
        id="task-1",
        status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
        artifacts=[
            Artifact(
                parts=[new_text_part("answer")],
                metadata={
                    "minimal_local_agent": {
                        "usage": {"requests": 1, "output_tokens": 12, "details": {}}
                    }
                },
            )
        ],
    )
    result = _task_result(task, _sdk())
    assert result.usage == {"requests": 1, "output_tokens": 12, "details": {}}
    assert type(result.usage["requests"]) is int
    assert type(result.usage["output_tokens"]) is int
    assert "tool_calls" not in result.usage


@pytest.mark.parametrize(
    "counter", ["requests", "tool_calls", "input_tokens", "output_tokens"]
)
@pytest.mark.parametrize("value", [True, False, 1.5, -1, "1", "invalid", None])
def test_task_rejects_malformed_reported_usage(counter: str, value: object) -> None:
    from minimal_local_agent.a2a_adapter import _sdk

    task = Task(
        id="task-1",
        status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
        artifacts=[
            Artifact(metadata={"minimal_local_agent": {"usage": {counter: value}}})
        ],
    )
    with pytest.raises(ValueError, match="usage counter"):
        _task_result(task, _sdk())


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_usage_is_rejected_before_integer_conversion(value: float) -> None:
    with pytest.raises(ValueError, match="usage counter"):
        _normalize_usage({"requests": value})


def test_normalization_preserves_non_counter_metadata_and_unknown_usage() -> None:
    assert _normalize_usage({}) == {}
    assert _normalize_usage({"details": {"latency_ms": 1.5}}) == {
        "details": {"latency_ms": 1.5}
    }
    assert _normalize_usage({"requests": 1.0, "tool_calls": 0.0}) == {
        "requests": 1,
        "tool_calls": 0,
    }


@pytest.mark.parametrize(
    "task_id", ["../other", "task?historyLength=100", "task/other"]
)
def test_task_query_rejects_path_injection(task_id: str) -> None:
    with pytest.raises(ValueError, match="identifier"):
        A2APeer("http://127.0.0.1:9000").get_task(task_id)


def test_remote_task_id_is_validated_before_automatic_polling() -> None:
    from minimal_local_agent.a2a_adapter import _sdk

    task = Task(id="../other", status=TaskStatus(state=TaskState.TASK_STATE_WORKING))
    with pytest.raises(ValueError, match="identifier"):
        _task_result(task, _sdk())


def test_sync_api_rejects_async_misuse_without_starting_a_coroutine() -> None:
    async def misuse() -> None:
        with pytest.raises(RuntimeError, match="to_thread"):
            A2APeer("http://127.0.0.1:9000").call(
                "task",
                timeout=1,
                request_limit=1,
                tool_calls_limit=1,
                max_output_tokens=512,
            )

    asyncio.run(misuse())
