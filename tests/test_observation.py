from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Event

import pytest

from minimal_local_agent.events import EventBus, RuntimeEvent
from minimal_local_agent.observation import ObservationHub


def _event(
    event_type: str = "run.started",
    *,
    execution_id: str | None = "run-one",
    sequence: int = 1,
    **data: object,
) -> RuntimeEvent:
    return RuntimeEvent(
        sequence=sequence,
        created_at="2026-10-12T08:00:00.000+00:00",
        session_id="session-one",
        type=event_type,
        data={"execution_id": execution_id, **data},
    )


def test_empty_hub_is_not_a_completed_or_running_task() -> None:
    hub = ObservationHub()
    snapshot = hub.snapshot()
    assert snapshot["runs"] == []
    assert snapshot["events"] == []
    assert snapshot["cursor"] == 0
    assert snapshot["meta"]["storage"] == "memory"
    assert snapshot["meta"]["archived"] is False
    assert hub.read_after(0, timeout=0)["gap"] is False


def test_ring_cursor_is_global_and_eviction_reports_a_gap() -> None:
    hub = ObservationHub(max_events=2)
    hub.publish(_event())
    hub.publish(_event("tool.completed", sequence=2))
    hub.publish(_event(execution_id="run-two", sequence=1))
    snapshot = hub.snapshot()
    assert [event["cursor"] for event in snapshot["events"]] == [2, 3]
    assert [event["sequence"] for event in snapshot["events"]] == [2, 1]
    assert snapshot["retention"]["discarded_events"] == 1
    assert hub.read_after(0, timeout=0)["gap"] is True
    assert hub.read_after(1, timeout=0)["gap"] is False
    assert hub.read_after(3, timeout=0)["events"] == []


def test_future_cursor_is_an_explicit_reset_not_silent_replay() -> None:
    hub = ObservationHub()
    hub.publish(_event())
    result = hub.read_after(100, timeout=0)
    assert result["reset"] is True
    assert result["gap"] is True
    assert result["cursor"] == 1
    assert result["hub_id"] == hub.snapshot()["meta"]["hub_id"]


def test_only_explicit_run_events_change_run_state() -> None:
    hub = ObservationHub()
    hub.publish(_event("tool.completed"))
    assert hub.snapshot()["runs"][0]["state"] == "observed"
    hub.publish(_event())
    for event_type in (
        "peer.completed",
        "peer.failed",
        "memory.committed",
        "plan.generated",
        "context.prepared",
    ):
        hub.publish(_event(event_type))
        assert hub.snapshot()["runs"][0]["state"] == "running"
    hub.publish(_event("run.failed"))
    hub.publish(_event("tool.completed"))
    hub.publish(_event("run.started"))
    assert hub.snapshot()["runs"][0]["state"] == "failed"


def test_uncorrelated_event_does_not_invent_a_run() -> None:
    hub = ObservationHub()
    hub.publish(_event(execution_id=None))
    assert len(hub.snapshot()["events"]) == 1
    assert hub.snapshot()["runs"] == []


def test_recent_run_metadata_is_bounded_and_keeps_parent_identity() -> None:
    hub = ObservationHub(max_runs=2)
    hub.publish(_event(execution_id="parent"))
    hub.publish(
        _event(execution_id="child", agent_id="worker", parent_execution_id="parent")
    )
    hub.publish(_event(execution_id="third"))
    snapshot = hub.snapshot()
    assert [run["execution_id"] for run in snapshot["runs"]] == ["third", "child"]
    assert snapshot["runs"][1]["parent_execution_id"] == "parent"
    assert snapshot["runs"][1]["agent_id"] == "worker"
    assert snapshot["retention"]["evicted_runs"] == 1


def test_snapshot_and_stream_values_are_detached_from_publisher() -> None:
    hub = ObservationHub()
    usage = {"input_tokens": 4, "output_tokens": 2}
    event = _event("run.completed", usage=usage)
    hub.publish(event)
    usage["input_tokens"] = 99
    snapshot = hub.snapshot()
    snapshot["events"][0]["data"]["usage"]["input_tokens"] = 88
    snapshot["runs"][0]["usage"]["input_tokens"] = 77
    stream = hub.read_after(0, timeout=0)
    stream["events"][0]["data"]["usage"]["input_tokens"] = 66
    assert hub.snapshot()["runs"][0]["usage"]["input_tokens"] == 4


@pytest.mark.parametrize(
    "content", ["文本" * 40_000, "\x00" * 40_000], ids=["multibyte", "escaped-control"]
)
def test_event_content_has_a_real_serialized_byte_bound(content: str) -> None:
    hub = ObservationHub(max_event_bytes=512)
    hub.publish(_event("message.observed", content=content))
    event = hub.snapshot()["events"][0]
    assert len(json.dumps(event, ensure_ascii=False).encode("utf-8")) <= 512
    assert event["observation_truncated"] is True


def test_control_characters_in_headers_cannot_exceed_the_event_bound() -> None:
    hub = ObservationHub(max_event_bytes=512)
    hub.publish(
        RuntimeEvent(
            sequence=10**1000,
            created_at="\x00" * 500,
            session_id="\x00" * 500,
            type="\x00" * 500,
            data={"execution_id": "\x00" * 500},
        )
    )
    event = hub.snapshot()["events"][0]
    assert len(json.dumps(event, ensure_ascii=False).encode("utf-8")) <= 512
    assert event["sequence"] is None


def test_credentials_and_hidden_reasoning_are_not_observed() -> None:
    hub = ObservationHub()
    hub.publish(
        _event(
            "message.observed",
            api_key="synthetic-private-key",
            headers={"Authorization": "Bearer synthetic-token"},
            message={
                "parts": [
                    {"part_kind": "thinking", "content": "private-thought"},
                    {"part_kind": "text", "content": "visible-answer"},
                ]
            },
        )
    )
    encoded = json.dumps(hub.snapshot())
    assert "synthetic-private-key" not in encoded
    assert "synthetic-token" not in encoded
    assert "private-thought" not in encoded
    assert "visible-answer" in encoded


def test_observer_error_does_not_escape_or_break_later_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub = ObservationHub()
    bus = EventBus("session-one", handler=hub.publish)

    def broken(_event: RuntimeEvent) -> dict[str, object]:
        raise RuntimeError("synthetic observer failure")

    with monkeypatch.context() as patch:
        patch.setattr(hub, "_event", broken)
        bus.emit("run.started", {"execution_id": "one"})
    bus.emit("run.completed", {"execution_id": "one"})
    assert bus.handler_errors == ()
    assert hub.snapshot()["meta"]["observer_errors"] == 1
    assert hub.snapshot()["runs"][0]["state"] == "completed"


def test_waiting_reader_wakes_without_blocking_publish() -> None:
    hub = ObservationHub()
    reading = Event()

    def read() -> dict[str, object]:
        reading.set()
        return hub.read_after(0, timeout=2)

    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(read)
        assert reading.wait(timeout=1)
        hub.publish(_event())
        assert result.result(timeout=2)["cursor"] == 1


def test_slow_snapshot_copy_does_not_hold_the_publish_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub = ObservationHub()
    copying = Event()
    release = Event()

    def slow_copy(value: object) -> object:
        copying.set()
        assert release.wait(timeout=2)
        return deepcopy(value)

    monkeypatch.setattr("minimal_local_agent.observation.deepcopy", slow_copy)
    with ThreadPoolExecutor(max_workers=2) as pool:
        snapshot = pool.submit(hub.snapshot)
        assert copying.wait(timeout=1)
        try:
            pool.submit(hub.publish, _event()).result(timeout=1)
        finally:
            release.set()
        assert snapshot.result(timeout=1)["cursor"] == 0
    assert hub.read_after(0, timeout=0)["cursor"] == 1


def test_concurrent_publishers_have_unique_monotonic_cursors() -> None:
    hub = ObservationHub(max_events=100)

    def publish(worker: int) -> None:
        for sequence in range(1, 21):
            hub.publish(
                _event(
                    "tool.completed",
                    execution_id=f"worker-{worker}",
                    sequence=sequence,
                )
            )

    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(publish, range(5)))
    assert [event["cursor"] for event in hub.snapshot()["events"]] == list(
        range(1, 101)
    )
    assert all(run["events_seen"] == 20 for run in hub.snapshot()["runs"])


@pytest.mark.parametrize("cursor", [-1, True, 1.5, "0"])
def test_invalid_cursor_is_rejected(cursor: object) -> None:
    with pytest.raises(ValueError, match="cursor"):
        ObservationHub().read_after(cursor, timeout=0)  # type: ignore[arg-type]


@pytest.mark.parametrize("timeout", [-1, 31, float("nan"), float("inf")])
def test_wait_is_finite_and_bounded(timeout: float) -> None:
    with pytest.raises(ValueError, match="wait"):
        ObservationHub().read_after(0, timeout=timeout)
