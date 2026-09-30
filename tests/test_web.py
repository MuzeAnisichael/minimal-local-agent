import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Event, Thread
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from pydantic_ai import RunContext
from pydantic_ai.models.test import TestModel

from minimal_local_agent.agent import AgentDependencies
from minimal_local_agent.config import Settings
from minimal_local_agent.models import ConfiguredModelFactory
from minimal_local_agent.read_tools import ReadTool
from minimal_local_agent.runtime import AgentRuntime
from minimal_local_agent.store import AuditStore
from minimal_local_agent.web import create_web_server


class _TestModelFactory:
    def create(self, _settings: Settings) -> TestModel:
        return TestModel(call_tools=[], custom_output_text="done")


def _runtime(settings: Settings) -> AgentRuntime:
    return AgentRuntime(settings, model_factory=_TestModelFactory())


@contextmanager
def _live_server(
    tmp_path: Path,
    runtime_factory=_runtime,
    read_tools: tuple[ReadTool, ...] = (),
) -> Iterator[str]:
    settings = Settings(
        base_url="http://127.0.0.1:9/v1",
        workspace=tmp_path / "workspace",
        database=tmp_path / "state.db",
        write_policy="confirm",
    )
    server = create_web_server(
        settings, port=0, runtime_factory=runtime_factory, read_tools=read_tools
    )
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
        with pytest.raises(HTTPError) as again:
            _json_request(f"{base_url}/api/run", {"prompt": "retry"})

    assert failed.value.code == 500
    assert again.value.code == 500
    assert session["runs"][0]["error"] == "model unavailable"


def test_web_rejects_overlapping_tasks_without_creating_a_session(
    tmp_path: Path,
) -> None:
    started = Event()
    release = Event()
    results: list[dict[str, object]] = []

    class BlockingRuntime(AgentRuntime):
        def __init__(self, settings: Settings) -> None:
            super().__init__(settings, model_factory=_TestModelFactory())

        def run(self, *args: Any, **kwargs: Any) -> Any:
            started.set()
            if not release.wait(timeout=5):
                raise TimeoutError("test did not release the first task")
            return super().run(*args, **kwargs)

    with _live_server(tmp_path, runtime_factory=BlockingRuntime) as base_url:
        first = Thread(
            target=lambda: results.append(
                _json_request(f"{base_url}/api/run", {"prompt": "first"})
            )
        )
        first.start()
        try:
            assert started.wait(timeout=3)
            with pytest.raises(HTTPError) as busy:
                _json_request(f"{base_url}/api/run", {"prompt": "overlap"})
            assert busy.value.code == 409
        finally:
            release.set()
            first.join(timeout=5)
        assert not first.is_alive()
        assert results[0]["response"] == "done"
        assert (
            _json_request(f"{base_url}/api/run", {"prompt": "next"})["response"]
            == "done"
        )
        with urlopen(f"{base_url}/api/sessions", timeout=5) as response:
            sessions = json.load(response)["sessions"]

    assert len(sessions) == 2
    assert all(session["last_prompt"] != "overlap" for session in sessions)


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


def test_web_can_expose_an_explicit_python_read_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "a").write_text("one\ntwo\n", encoding="utf-8")

    def line_count(ctx: RunContext[AgentDependencies], path: str) -> int:
        """Count lines in a workspace file."""
        return len(ctx.deps.workspace.read_file(path).splitlines())

    def model(_self: ConfiguredModelFactory, _settings: Settings) -> TestModel:
        return TestModel(call_tools=["line_count"], custom_output_text="done")

    monkeypatch.setattr(ConfiguredModelFactory, "create", model)
    with _live_server(
        tmp_path,
        runtime_factory=None,
        read_tools=(ReadTool("line_count", line_count),),
    ) as base_url:
        with urlopen(f"{base_url}/api/status", timeout=5) as response:
            status: dict[str, Any] = json.load(response)
        result = _json_request(
            f"{base_url}/api/run", {"prompt": "Count lines", "mode": "read"}
        )
        with urlopen(
            f"{base_url}/api/sessions/{result['session_id']}", timeout=5
        ) as response:
            session: dict[str, Any] = json.load(response)

    capabilities = status["modes"]["read"]["capabilities"]
    assert any(
        item["name"] == "line_count" and item["source"] == "python"
        for item in capabilities
    )
    assert result["response"] == "done"
    assert session["tool_events"][0]["tool_name"] == "line_count"
    assert session["receipt_chain"]["verified"] is True
