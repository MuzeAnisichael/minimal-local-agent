"""SDK resource ownership under synthetic models and mocked HTTP only."""

import asyncio
import gc
import warnings
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic_ai.exceptions import ModelHTTPError, UnexpectedModelBehavior
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models import Model
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RequestUsage

from minimal_local_agent.agent import AgentRuntime
from minimal_local_agent.config import Settings
from minimal_local_agent.models import OpenAICompatibleModelFactory
from minimal_local_agent.peers import LocalPeer
from minimal_local_agent.planning import ModelPlanner


class _Tracking:
    events: list[str]
    active = False

    async def __aenter__(self) -> Any:
        assert not self.active
        self.events.append("enter")
        self.active = True
        return await super().__aenter__()

    async def __aexit__(self, *args: Any) -> Any:
        try:
            return await super().__aexit__(*args)
        finally:
            self.events.append("exit")
            self.active = False

    async def request(self, *args: Any, **kwargs: Any) -> Any:
        assert self.active, "A model request requires an entered lifecycle"
        self.events.append("request")
        return await super().request(*args, **kwargs)


class _FunctionSpy(_Tracking, FunctionModel):
    def __init__(self, *, failing: bool = False) -> None:
        self.events = []

        def respond(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
            if failing:
                raise ValueError("synthetic model failure")
            return ModelResponse(
                [TextPart("done")], usage=RequestUsage(input_tokens=5, output_tokens=2)
            )

        super().__init__(respond)


class _PlanSpy(_Tracking, TestModel):
    def __init__(self, *, invalid: bool = False) -> None:
        self.events = []
        steps = (
            []
            if invalid
            else [
                {
                    "title": "Inspect",
                    "completion_condition": "A read tool returns evidence",
                }
            ]
        )
        super().__init__(call_tools=[], custom_output_args={"steps": steps})


class _Factory:
    def __init__(self, model: Model) -> None:
        self.model = model

    def create(self, _settings: Settings) -> Model:
        return self.model


def _settings(tmp_path: Path, **kwargs: Any) -> Settings:
    return Settings(
        workspace=tmp_path / "synthetic-workspace",
        database=tmp_path / "synthetic-state.db",
        write_policy="deny",
        **kwargs,
    )


@pytest.mark.parametrize("failing", [False, True])
def test_runtime_enters_and_exits_model_on_success_or_failure(
    tmp_path: Path,
    failing: bool,
) -> None:
    model = _FunctionSpy(failing=failing)
    runtime = AgentRuntime(_settings(tmp_path), model_factory=_Factory(model))
    if failing:
        with pytest.raises(ValueError, match="synthetic model failure"):
            runtime.run("synthetic task")
    else:
        assert runtime.run("synthetic task").response == "done"
    assert model.events == ["enter", "request", "exit"]
    assert model.active is False


def test_same_runtime_reenters_model_for_a_continued_session(tmp_path: Path) -> None:
    model = _FunctionSpy()
    runtime = AgentRuntime(_settings(tmp_path), model_factory=_Factory(model))
    first = runtime.run("first synthetic task")
    second = runtime.run("second synthetic task", session_id=first.session_id)
    assert first.session_id == second.session_id
    assert model.events == ["enter", "request", "exit"] * 2
    assert model.active is False
    saved = runtime.store.load_history(second.session_id)
    assert "first synthetic task" in saved and "second synthetic task" in saved


@pytest.mark.parametrize("invalid", [False, True])
def test_planner_model_lifecycle_is_closed_even_when_plan_validation_fails(
    tmp_path: Path,
    invalid: bool,
) -> None:
    model = _PlanSpy(invalid=invalid)
    planner = ModelPlanner(_settings(tmp_path), model_factory=_Factory(model))
    if invalid:
        with pytest.raises(UnexpectedModelBehavior):
            planner.plan("synthetic planning task")
    else:
        assert planner.plan("synthetic planning task").usage["requests"] == 1
        assert planner.plan("second synthetic planning task").usage["requests"] == 1
    assert model.events == ["enter", "request", "exit"] * (1 if invalid else 2)
    assert model.active is False


@pytest.mark.parametrize("failing", [False, True])
def test_local_peer_worker_releases_its_model_before_returning(
    tmp_path: Path,
    failing: bool,
) -> None:
    model = _FunctionSpy(failing=failing)
    peer = LocalPeer(_settings(tmp_path), model_factory=_Factory(model))
    result = peer.call(
        "synthetic child task",
        timeout=5,
        request_limit=1,
        tool_calls_limit=1,
        max_output_tokens=128,
    )
    assert result.state == ("failed" if failing else "completed")
    assert model.events == ["enter", "request", "exit"]
    assert model.active is False
    assert peer.get_task(result.task_id) == result


def test_active_async_loop_rejects_sync_runtime_and_planner_without_coroutine_leak(
    tmp_path: Path,
) -> None:
    runtime_model, planner_model = _FunctionSpy(), _PlanSpy()
    runtime = AgentRuntime(_settings(tmp_path), model_factory=_Factory(runtime_model))
    planner = ModelPlanner(_settings(tmp_path), model_factory=_Factory(planner_model))

    async def misuse() -> None:
        with pytest.raises(RuntimeError, match="asyncio.to_thread"):
            runtime.run("synthetic task")
        with pytest.raises(RuntimeError, match="asyncio.to_thread"):
            planner.plan("synthetic plan")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        asyncio.run(misuse())
        gc.collect()
    assert not any("was never awaited" in str(warning.message) for warning in caught)
    assert runtime_model.events == planner_model.events == []


@pytest.mark.parametrize("first_status", [200, 400])
def test_real_compatible_factory_closes_and_rebuilds_owned_http_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    first_status: int,
) -> None:
    clients: list[httpx.AsyncClient] = []
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.host == "127.0.0.1"
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer local-only"
        if len(requests) == 1 and first_status == 400:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": "synthetic bad request",
                        "type": "invalid_request_error",
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "synthetic-completion",
                "object": "chat.completion",
                "created": 0,
                "model": "synthetic-model",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "done"},
                    }
                ],
                "usage": {
                    "prompt_tokens": 5,
                    "completion_tokens": 2,
                    "total_tokens": 7,
                },
            },
        )

    def new_client() -> httpx.AsyncClient:
        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        clients.append(client)
        return client

    monkeypatch.setattr(
        "pydantic_ai.providers.openai.create_async_http_client", new_client
    )
    runtime = AgentRuntime(
        _settings(
            tmp_path,
            provider="openai-compatible",
            base_url="http://127.0.0.1:9/v1",
            model="synthetic-model",
        ),
        model_factory=OpenAICompatibleModelFactory(),
    )
    try:
        assert len(clients) == 1 and not clients[0].is_closed
        if first_status == 400:
            with pytest.raises(ModelHTTPError) as failed:
                runtime.run("first synthetic request")
            assert failed.value.status_code == 400
        else:
            assert runtime.run("first synthetic request").response == "done"
        assert clients[0].is_closed
        assert runtime.run("second synthetic request").response == "done"
        assert len(requests) == len(clients) == 2
        assert clients[0] is not clients[1]
        assert all(client.is_closed for client in clients)
    finally:
        for client in clients:
            if not client.is_closed:
                asyncio.run(client.aclose())
