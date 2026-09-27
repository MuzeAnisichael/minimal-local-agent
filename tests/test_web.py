import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from pydantic_ai.models.test import TestModel

from minimal_local_agent.config import Settings
from minimal_local_agent.runtime import AgentRuntime
from minimal_local_agent.store import AuditStore
from minimal_local_agent.web import create_web_server


class _TestModelFactory:
    def create(self, _settings: Settings) -> TestModel:
        return TestModel(call_tools=[], custom_output_text="done")


def _runtime(settings: Settings) -> AgentRuntime:
    return AgentRuntime(settings, model_factory=_TestModelFactory())


@contextmanager
def _live_server(tmp_path: Path, runtime_factory=_runtime) -> Iterator[str]:
    settings = Settings(
        base_url="http://127.0.0.1:9/v1",
        workspace=tmp_path / "workspace",
        database=tmp_path / "state.db",
        write_policy="confirm",
    )
    server = create_web_server(settings, port=0, runtime_factory=runtime_factory)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _json_request(url: str, payload: dict[str, object]) -> dict[str, object]:
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=5) as response:
        return json.load(response)


def test_web_console_serves_packaged_frontend(tmp_path: Path) -> None:
    with _live_server(tmp_path) as base_url:
        with urlopen(base_url, timeout=5) as response:
            html = response.read().decode("utf-8")
            csp = response.headers["Content-Security-Policy"]
        with urlopen(f"{base_url}/assets/app.js", timeout=5) as response:
            javascript = response.read().decode("utf-8")

    assert "本地工作台" in html
    assert "default-src 'self'" in csp
    assert 'request("/api/run"' in javascript


def test_web_run_is_forced_to_safe_modes_and_saved(tmp_path: Path) -> None:
    with _live_server(tmp_path) as base_url:
        result = _json_request(
            f"{base_url}/api/run",
            {"prompt": "say done", "mode": "read"},
        )
        session_id = str(result["session_id"])
        with urlopen(f"{base_url}/api/sessions/{session_id}", timeout=5) as response:
            session = json.load(response)

    assert result["response"] == "done"
    assert result["mode"] == "read"
    assert len(str(result["receipt_hash"])) == 64
    assert [event["type"] for event in result["events"]] == [
        "run.started",
        "run.completed",
    ]
    assert session["runs"][0]["prompt"] == "say done"
    assert session["receipt_chain"]["verified"] is True


def test_web_error_keeps_failed_session_accessible(tmp_path: Path) -> None:
    class FailingRuntime:
        def __init__(self, settings: Settings) -> None:
            self.store = AuditStore(settings.database)

        def run(self, prompt: str, *, session_id: str, **_kwargs: object) -> None:
            self.store.record_run(
                session_id,
                prompt,
                response=None,
                success=False,
                error="model unavailable",
            )
            raise RuntimeError("model unavailable")

    with _live_server(tmp_path, runtime_factory=FailingRuntime) as base_url:
        with pytest.raises(HTTPError) as failed:
            _json_request(f"{base_url}/api/run", {"prompt": "hello"})
        error = json.load(failed.value)
        with urlopen(
            f"{base_url}/api/sessions/{error['session_id']}", timeout=5
        ) as response:
            session = json.load(response)

    assert failed.value.code == 500
    assert session["runs"][0]["error"] == "model unavailable"


def test_web_rejects_unsafe_mode_and_cross_origin_post(tmp_path: Path) -> None:
    with _live_server(tmp_path) as base_url:
        with pytest.raises(HTTPError) as mode_error:
            _json_request(
                f"{base_url}/api/run",
                {"prompt": "change a file", "mode": "confirm"},
            )

        request = Request(
            f"{base_url}/api/run",
            data=b'{"prompt":"hello","mode":"read"}',
            headers={
                "Content-Type": "application/json",
                "Origin": "https://example.com",
            },
            method="POST",
        )
        with pytest.raises(HTTPError) as origin_error:
            urlopen(request, timeout=5)

    assert mode_error.value.code == 400
    assert origin_error.value.code == 403


def test_web_rejects_forged_host_and_alias_origin(tmp_path: Path) -> None:
    with _live_server(tmp_path) as base_url:
        forged_host = Request(
            f"{base_url}/api/status",
            headers={"Host": "attacker.example"},
        )
        with pytest.raises(HTTPError) as host_error:
            urlopen(forged_host, timeout=5)

        alias_origin = Request(
            f"{base_url}/api/run",
            data=b'{"prompt":"hello"}',
            headers={
                "Content-Type": "application/json",
                "Origin": base_url.replace("127.0.0.1", "localhost"),
            },
            method="POST",
        )
        with pytest.raises(HTTPError) as origin_error:
            urlopen(alias_origin, timeout=5)

    assert host_error.value.code == 403
    assert origin_error.value.code == 403


def test_web_server_rejects_non_loopback_host(tmp_path: Path) -> None:
    settings = Settings(
        workspace=tmp_path / "workspace",
        database=tmp_path / "state.db",
    )

    with pytest.raises(ValueError, match="loopback-only"):
        create_web_server(settings, host="0.0.0.0")
