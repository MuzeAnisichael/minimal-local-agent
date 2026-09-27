import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import patch

import pytest
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel

from minimal_local_agent.config import Settings
from minimal_local_agent.models import (
    ConfiguredModelFactory,
    model_api_key,
    probe_model,
)


def test_compatible_endpoint_uses_key_from_named_environment_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_MODEL_KEY", "test-secret")
    settings = Settings(
        provider="openai-compatible",
        model="example/model",
        base_url="https://example.test/v1",
        api_key_env="TEST_MODEL_KEY",
    )

    model = ConfiguredModelFactory().create(settings)

    assert isinstance(model, OpenAIChatModel)
    assert model_api_key(settings) == "test-secret"
    assert "test-secret" not in repr(settings)


def test_environment_can_switch_the_default_model_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = tmp_path / "agent.toml"
    config.write_text("[agent]\nprovider = 'ollama'\n", encoding="utf-8")
    monkeypatch.setenv("MLA_PROVIDER", "openai-compatible")
    monkeypatch.setenv("MLA_MODEL", "example/model")
    monkeypatch.setenv("MLA_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("MLA_API_KEY_ENV", "TEST_MODEL_KEY")
    monkeypatch.setenv("TEST_MODEL_KEY", "test-secret")

    settings = Settings.load(config)

    assert settings.provider == "openai-compatible"
    assert settings.model == "example/model"
    assert isinstance(ConfiguredModelFactory().create(settings), OpenAIChatModel)


def test_compatible_endpoint_requires_key_for_remote_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TEST_MODEL_KEY", raising=False)
    remote = Settings(
        provider="openai-compatible",
        base_url="https://example.test/v1",
        api_key_env="TEST_MODEL_KEY",
    )
    local = Settings(
        provider="openai-compatible",
        base_url="http://127.0.0.1:1234/v1",
    )

    with pytest.raises(ValueError, match="TEST_MODEL_KEY"):
        ConfiguredModelFactory().create(remote)
    assert model_api_key(local) == "local-only"


def test_probe_uses_auth_without_returning_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_MODEL_KEY", "test-secret")
    settings = Settings(
        provider="openai-compatible",
        model="example/model",
        base_url="https://example.test/v1",
        api_key_env="TEST_MODEL_KEY",
    )

    def urlopen(request: object, timeout: int) -> io.BytesIO:
        assert timeout == 3
        assert request.get_header("Authorization") == "Bearer test-secret"
        return io.BytesIO(b'{"data":[{"id":"example/model"}]}')

    with patch("minimal_local_agent.models.urllib.request.urlopen", urlopen):
        result = probe_model(settings)

    assert result.state == "ready"
    assert "test-secret" not in str(result.to_dict())


def test_probe_handles_non_list_model_data() -> None:
    settings = Settings(base_url="http://127.0.0.1:1234/v1")
    with patch(
        "minimal_local_agent.models.urllib.request.urlopen",
        return_value=io.BytesIO(b'{"data":null}'),
    ):
        result = probe_model(settings)
    assert result.state == "unverified"


def test_rejects_key_in_url_and_insecure_keyed_remote() -> None:
    with pytest.raises(ValueError, match="credentials"):
        Settings(base_url="https://user:secret@example.test/v1")
    with pytest.raises(ValueError, match="HTTPS"):
        Settings(
            provider="openai-compatible",
            base_url="http://example.test/v1",
            api_key_env="TEST_MODEL_KEY",
        )


def test_compatible_endpoint_can_call_a_tool_over_local_chat_completions() -> None:
    requests: list[dict[str, object]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            assert self.path == "/v1/chat/completions"
            assert self.headers["Authorization"] == "Bearer local-only"
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            if len(requests) == 1:
                message = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "echo",
                                "arguments": '{"value":"ping"}',
                            },
                        }
                    ],
                }
                finish_reason = "tool_calls"
            else:
                message = {"role": "assistant", "content": "done"}
                finish_reason = "stop"
            payload = json.dumps(
                {
                    "id": f"completion-{len(requests)}",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "test-model",
                    "choices": [
                        {
                            "index": 0,
                            "message": message,
                            "finish_reason": finish_reason,
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                    },
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        settings = Settings(
            provider="openai-compatible",
            model="test-model",
            base_url=f"http://127.0.0.1:{server.server_address[1]}/v1",
        )
        agent = Agent(ConfiguredModelFactory().create(settings))

        @agent.tool_plain
        def echo(value: str) -> str:
            return value

        result = agent.run_sync("Call echo with ping")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert result.output == "done"
    assert len(requests) == 2
    assert requests[0]["tools"][0]["function"]["name"] == "echo"
