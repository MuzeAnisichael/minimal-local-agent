"""Local HTTP inspector contracts, using only synthetic data and fake models."""

import json
import sqlite3
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RequestUsage

from minimal_local_agent.agent import AgentRuntime, RunOutcome
from minimal_local_agent.config import Settings
from minimal_local_agent.events import EventBus, RuntimeEvent
from minimal_local_agent.models import ConfiguredModelFactory
from minimal_local_agent.observation import ObservationHub
from minimal_local_agent.store import AuditStore
from minimal_local_agent.web import WebServer, create_web_server


class _ModelFactory:
    def create(self, _settings: Settings) -> TestModel:
        return TestModel(call_tools=[], custom_output_text="done")


def _runtime(settings: Settings) -> AgentRuntime:
    return AgentRuntime(settings, model_factory=_ModelFactory())


@contextmanager
def _live_server(
    tmp_path: Path,
    *,
    memory: bool = False,
    runtime_factory: Any = _runtime,
    hub: ObservationHub | None = None,
) -> Iterator[tuple[WebServer, str]]:
    configured = Settings(
        base_url="http://127.0.0.1:9/v1",
        workspace=tmp_path / "synthetic-workspace",
        database=tmp_path / "synthetic-state.db",
        memory_enabled=memory,
        memory_database=tmp_path / "synthetic-memory.db",
        memory_scope="inspector-fixture",
    )
    server = create_web_server(configured, port=0, runtime_factory=runtime_factory)
    if hub is not None:
        server.app.observation = hub
    thread = Thread(
        target=lambda: server.serve_forever(poll_interval=0.05),
        daemon=True,
    )
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.app.inspector_closed.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


def _json(
    base: str, path: str, payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    request = Request(
        base + path,
        data=None if payload is None else json.dumps(payload).encode("utf-8"),
        headers={} if payload is None else {"Content-Type": "application/json"},
    )
    with urlopen(request, timeout=5) as response:
        return json.load(response)


def _frame(response: Any) -> tuple[int, dict[str, Any]]:
    cursor = None
    data = None
    for _line in range(8):
        line = response.readline().decode("utf-8").rstrip("\r\n")
        if line.startswith("id: "):
            cursor = int(line.removeprefix("id: "))
        elif line.startswith("data: "):
            data = json.loads(line.removeprefix("data: "))
        elif not line and data is not None:
            assert cursor is not None
            return cursor, data
    raise AssertionError("No bounded SSE frame received")


def _publish(server: WebServer, event_type: str, sequence: int) -> None:
    server.app.observation.publish(
        RuntimeEvent(
            sequence=sequence,
            created_at=datetime.now(UTC).isoformat(),
            session_id="synthetic-session",
            type=event_type,
            data={"execution_id": "synthetic-execution"},
        )
    )


def test_inspector_assets_are_packaged_read_only_and_same_origin(
    tmp_path: Path,
) -> None:
    with _live_server(tmp_path) as (_server, base):
        with urlopen(base + "/inspector", timeout=3) as response:
            html = response.read().decode("utf-8")
            assert response.headers["Content-Type"].startswith("text/html")
            assert response.headers["X-Frame-Options"] == "DENY"
            assert "connect-src 'self'" in response.headers["Content-Security-Policy"]
        with urlopen(base + "/assets/inspector.js", timeout=3) as response:
            script = response.read().decode("utf-8")
            assert response.headers["Content-Type"].startswith("text/javascript")
        with urlopen(base + "/assets/inspector.css", timeout=3) as response:
            assert response.read()
            assert response.headers["Content-Type"].startswith("text/css")
    assert "/assets/inspector.js" in html and "/assets/inspector.css" in html
    assert "此面板不能批准、编辑或删除记忆" in html
    assert "new EventSource" in script and "/api/inspector/snapshot" in script


@pytest.mark.parametrize("path", ["/api/inspector/snapshot", "/api/inspector/events"])
@pytest.mark.parametrize("kind", ["origin", "fetch-site", "credential-origin"])
def test_inspector_rejects_non_same_origin_reads(
    tmp_path: Path,
    path: str,
    kind: str,
) -> None:
    with _live_server(tmp_path) as (_server, base):
        headers = {
            "origin": {"Origin": "https://attacker.example"},
            "fetch-site": {"Sec-Fetch-Site": "cross-site"},
            "credential-origin": {
                "Origin": base.replace("http://", "http://user:pass@")
            },
        }[kind]
        request = Request(base + path, method="HEAD", headers=headers)
        with pytest.raises(HTTPError) as refused:
            urlopen(request, timeout=3)
        assert refused.value.code == 403


def test_inspector_accepts_exact_origin_but_rejects_forged_host(tmp_path: Path) -> None:
    with _live_server(tmp_path) as (_server, base):
        with urlopen(
            Request(
                base + "/api/inspector/snapshot",
                headers={"Origin": base},
            ),
            timeout=3,
        ) as response:
            assert json.load(response)["cursor"] == 0
        with pytest.raises(HTTPError) as refused:
            urlopen(
                Request(
                    base + "/api/inspector/snapshot",
                    headers={"Host": "attacker.example"},
                ),
                timeout=3,
            )
        assert refused.value.code == 403


def test_memory_enabled_snapshot_does_not_construct_or_probe_any_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("Inspector must not contact a model")

    monkeypatch.setattr(ConfiguredModelFactory, "create", forbidden)
    monkeypatch.setattr("minimal_local_agent.web.probe_model", forbidden)
    with _live_server(tmp_path, memory=True, runtime_factory=None) as (_server, base):
        snapshot = _json(base, "/api/inspector/snapshot")
    assert snapshot["memory"]["enabled"] is True
    assert snapshot["memory"]["scope"] == "inspector-fixture"
    assert snapshot["memory"]["records"] == []
    assert snapshot["runs"] == [] and snapshot["events"] == []


def test_sse_delivers_model_context_and_run_events_before_model_finishes(
    tmp_path: Path,
) -> None:
    entered, release = Event(), Event()

    class BlockingFactory:
        def create(self, _settings: Settings) -> FunctionModel:
            def respond(
                _messages: list[ModelMessage], _info: AgentInfo
            ) -> ModelResponse:
                entered.set()
                assert release.wait(timeout=5), "Synthetic model was not released"
                return ModelResponse(
                    [TextPart("done")],
                    usage=RequestUsage(input_tokens=5, output_tokens=2),
                )

            return FunctionModel(respond)

    def runtime(settings: Settings) -> AgentRuntime:
        return AgentRuntime(settings, model_factory=BlockingFactory())

    with (
        _live_server(tmp_path, runtime_factory=runtime) as (_server, base),
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        running = pool.submit(
            _json, base, "/api/run", {"prompt": "held inspector task"}
        )
        try:
            assert entered.wait(timeout=3)
            with urlopen(base + "/api/inspector/events?cursor=0", timeout=3) as stream:
                cursor, payload = _frame(stream)
                assert stream.headers["Content-Type"].startswith("text/event-stream")
            # Closing an observation connection must not cancel the held task.
            assert not running.done() and not release.is_set()
            types = {event["type"] for event in payload["events"]}
            assert {"run.started", "context.prepared", "model.started"} <= types
            assert "run.completed" not in types
            execution_id = payload["events"][0]["execution_id"]
            assert all(
                event["execution_id"] == execution_id for event in payload["events"]
            )
            snapshot = _json(base, "/api/inspector/snapshot")
            assert snapshot["runs"][0]["state"] == "running"
            release.set()
            result = running.result(timeout=3)
            assert result["response"] == "done"
            assert result["execution_id"] == execution_id
            assert [event["type"] for event in result["events"]] == [
                "run.started",
                "run.completed",
            ]
            with urlopen(
                Request(
                    base + "/api/inspector/events?cursor=0",
                    headers={"Last-Event-ID": str(cursor)},
                ),
                timeout=3,
            ) as reconnected:
                _cursor, terminal = _frame(reconnected)
            assert "run.completed" in {event["type"] for event in terminal["events"]}
            assert (
                _json(base, "/api/inspector/snapshot")["runs"][0]["state"]
                == "completed"
            )
        finally:
            release.set()


def test_last_event_id_overrides_stale_or_invalid_query_cursor(tmp_path: Path) -> None:
    with _live_server(tmp_path) as (server, base):
        for sequence, event_type in enumerate(
            ("run.started", "model.started", "run.completed"),
            start=1,
        ):
            _publish(server, event_type, sequence)
        with urlopen(
            Request(
                base + "/api/inspector/events?cursor=not-an-integer",
                headers={"Last-Event-ID": "2"},
            ),
            timeout=3,
        ) as response:
            cursor, payload = _frame(response)
    assert cursor == payload["cursor"] == 3
    assert [event["cursor"] for event in payload["events"]] == [3]
    assert payload["gap"] is False and payload["reset"] is False


@pytest.mark.parametrize(
    "query,header",
    [
        ("-1", None),
        ("not-an-integer", None),
        (str(2**63), None),
        ("0", "invalid"),
    ],
)
def test_invalid_stream_cursor_is_rejected_before_streaming(
    tmp_path: Path,
    query: str,
    header: str | None,
) -> None:
    with _live_server(tmp_path) as (_server, base):
        with pytest.raises(HTTPError) as refused:
            urlopen(
                Request(
                    base + "/api/inspector/events?cursor=" + query,
                    method="HEAD",
                    headers={} if header is None else {"Last-Event-ID": header},
                ),
                timeout=3,
            )
        assert refused.value.code == 400


def test_sse_retention_gap_and_future_epoch_are_explicit(tmp_path: Path) -> None:
    with _live_server(tmp_path, hub=ObservationHub(max_events=2)) as (server, base):
        for index in range(1, 5):
            _publish(server, "tool.completed", index)
        with urlopen(base + "/api/inspector/events?cursor=0", timeout=3) as response:
            _cursor, evicted = _frame(response)
        with urlopen(base + "/api/inspector/events?cursor=100", timeout=3) as response:
            _cursor, restarted = _frame(response)
    assert [event["cursor"] for event in evicted["events"]] == [3, 4]
    assert evicted["gap"] is True and evicted["reset"] is False
    assert restarted["gap"] is True and restarted["reset"] is True
    assert restarted["hub_id"] == evicted["hub_id"]


def test_memory_snapshot_distinguishes_candidates_entities_and_read_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _live_server(tmp_path, memory=True) as (server, base):
        memory = server.app.memory_store
        candidate = memory.propose("inspector-fixture", "unconfirmed fact")
        entity = memory.propose(
            "inspector-fixture",
            "confirmed component",
            entity_key="mla",
            attributes={"language": "Python"},
        )
        approved = memory.approve("inspector-fixture", entity.id, expected_revision=1)
        memory.propose("other-scope", "FOREIGN_MEMORY_MARKER")
        snapshot = _json(base, "/api/inspector/snapshot")["memory"]
        records = {record["id"]: record for record in snapshot["records"]}
        assert records[candidate.id]["status"] == "candidate"
        assert records[approved.id]["status"] == "approved"
        assert records[approved.id]["attributes"] == {"language": "Python"}
        assert "FOREIGN_MEMORY_MARKER" not in json.dumps(snapshot)

        def failure(*_args: Any, **_kwargs: Any) -> None:
            raise sqlite3.OperationalError("SYNTHETIC_PRIVATE_DIAGNOSTIC")

        with monkeypatch.context() as patch:
            patch.setattr(memory, "list_records", failure)
            failed = _json(base, "/api/inspector/snapshot")["memory"]
        assert failed["enabled"] is True and failed["error"] == "OperationalError"
        assert "records" not in failed  # A failure is not a successful empty read.
        assert "SYNTHETIC_PRIVATE_DIAGNOSTIC" not in json.dumps(failed)
        recovered = _json(base, "/api/inspector/snapshot")["memory"]
        assert len(recovered["records"]) == 2 and "error" not in recovered


def test_memory_snapshot_obeys_record_and_serialized_byte_limits(
    tmp_path: Path,
) -> None:
    with _live_server(tmp_path, memory=True) as (server, base):
        memory = server.app.memory_store
        for index in range(60):
            memory.propose("inspector-fixture", f"short candidate {index}")
        assert len(_json(base, "/api/inspector/snapshot")["memory"]["records"]) == 50
        for index in range(10):
            memory.propose("inspector-fixture", str(index) + "x" * 7_999)
        snapshot = _json(base, "/api/inspector/snapshot")["memory"]
    assert 0 < len(snapshot["records"]) <= 50
    assert (
        sum(
            len(json.dumps(record, ensure_ascii=False).encode("utf-8"))
            for record in snapshot["records"]
        )
        <= 64_000
    )
    assert snapshot["truncated"] is True and snapshot["notice"]


def test_unread_sse_client_cannot_block_runtime_or_grow_the_event_ring(
    tmp_path: Path,
) -> None:
    class BurstRuntime:
        def __init__(self, settings: Settings) -> None:
            self.store = AuditStore(settings.database)

        def run(
            self,
            prompt: str,
            *,
            session_id: str,
            execution_id: str,
            event_handler: Any,
            **_kwargs: Any,
        ) -> RunOutcome:
            bus = EventBus(session_id, event_handler, execution_id=execution_id)
            bus.emit("run.started")
            for _index in range(1_024):
                bus.emit("message.observed", {"content": "x" * 8_000})
            usage = {"requests": 0, "tool_calls": 0}
            run_id = self.store.record_run(
                session_id,
                prompt,
                response="done",
                success=True,
                usage=usage,
            )
            receipt = self.store.record_receipt(session_id, run_id, {"success": True})
            bus.emit("run.completed", {"usage": usage})
            return RunOutcome(session_id, "done", usage, receipt)

    hub = ObservationHub(max_events=8, max_event_bytes=2_048)
    with _live_server(tmp_path, hub=hub, runtime_factory=BurstRuntime) as (
        server,
        base,
    ):
        _publish(server, "observer.ready", 1)
        # The response is deliberately never consumed while the producer runs.
        with urlopen(base + "/api/inspector/events?cursor=0", timeout=3):
            with ThreadPoolExecutor(max_workers=1) as pool:
                completed = pool.submit(
                    _json, base, "/api/run", {"prompt": "synthetic burst"}
                )
                result = completed.result(timeout=3)
            snapshot = _json(base, "/api/inspector/snapshot")
    assert result["response"] == "done" and result["receipt_chain_verified"] is True
    assert [event["type"] for event in result["events"]] == [
        "run.started",
        "run.completed",
    ]
    assert len(snapshot["events"]) == 8
    assert snapshot["retention"]["discarded_events"] >= 1_000
    assert snapshot["runs"][0]["state"] == "completed"
    assert any(event["observation_truncated"] for event in snapshot["events"])
    assert all(
        len(json.dumps(event, ensure_ascii=False).encode("utf-8")) <= 2_048
        for event in snapshot["events"]
    )


def test_stream_connection_limit_does_not_block_snapshot_or_head(
    tmp_path: Path,
) -> None:
    with _live_server(tmp_path) as (server, base):
        acquired = 0
        try:
            for _index in range(8):
                assert server.app.inspector_clients.acquire(blocking=False)
                acquired += 1
            with pytest.raises(HTTPError) as refused:
                urlopen(base + "/api/inspector/events", timeout=3)
            assert refused.value.code == 503
            assert _json(base, "/api/inspector/snapshot")["runs"] == []
            with urlopen(
                Request(
                    base + "/api/inspector/events",
                    method="HEAD",
                ),
                timeout=3,
            ) as response:
                assert response.status == 200
                assert response.read() == b""
        finally:
            for _index in range(acquired):
                server.app.inspector_clients.release()
