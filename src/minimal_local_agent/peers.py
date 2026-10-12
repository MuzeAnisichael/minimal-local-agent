"""Small, explicit task delegation boundary; no shared conversation or memory."""

from __future__ import annotations

import math
from concurrent.futures import Future, TimeoutError
from dataclasses import dataclass, field, replace
from threading import Lock, Thread
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import uuid4

from minimal_local_agent.config import Settings
from minimal_local_agent.models import ModelFactory


@dataclass(frozen=True, slots=True)
class PeerResult:
    response: str
    task_id: str | None
    state: str
    # Empty means unknown, not zero tokens/cost. A2A usage is peer-reported.
    usage: dict[str, Any] = field(default_factory=dict)


class Peer(Protocol):
    def call(
        self,
        prompt: str,
        *,
        timeout: float,
        request_limit: int,
        tool_calls_limit: int,
        max_output_tokens: int,
    ) -> PeerResult:
        """Delegate only this task under the supplied budget and wait bound."""


def validate_call(
    prompt: str,
    timeout: float,
    request_limit: int,
    tool_calls_limit: int,
    max_output_tokens: int,
) -> None:
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("Peer task cannot be empty")
    if len(prompt.encode("utf-8")) > 32_000:
        raise ValueError("Peer task exceeds 32,000 bytes")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise ValueError("Peer timeout must be a positive finite number")
    for name, value in (
        ("request_limit", request_limit),
        ("tool_calls_limit", tool_calls_limit),
        ("max_output_tokens", max_output_tokens),
    ):
        if type(value) is not int or value < 1:
            raise ValueError(f"Peer {name} must be a positive integer")


def loopback_endpoint(url: str) -> str:
    """Validate an explicitly configured origin, never a discovery redirect."""

    if not isinstance(url, str) or any(ord(char) < 32 for char in url):
        raise ValueError("Peer endpoint must be a loopback HTTP(S) origin")
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Peer endpoint has an invalid port") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in {"localhost", "127.0.0.1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or port == 0
    ):
        raise ValueError("Peer endpoint must be a loopback HTTP(S) origin")
    return url.rstrip("/")


class LocalPeer:
    """One independently configured, read-only runtime with bounded delegation.

    Each task gets a new session; no caller history is copied. The host must give
    this peer its own workspace and database (distinct from the parent runtime).
    Timeout stops waiting, not execution. A timed-out peer remains busy until its
    worker exits; ``get_task`` reports the eventual result. Only the last 32 tasks
    are retained in this in-process index; the runtime keeps its ordinary audit.
    """

    def __init__(
        self, settings: Settings, *, model_factory: ModelFactory | None = None
    ) -> None:
        self.settings = replace(
            settings,
            write_policy="deny",
            mcp_servers=(),
            memory_enabled=False,
            planning_enabled=False,
        )
        self.model_factory = model_factory
        self._lock = Lock()
        self._active: Future[PeerResult] | None = None
        self._tasks: dict[str, Future[PeerResult]] = {}

    def call(
        self,
        prompt: str,
        *,
        timeout: float,
        request_limit: int,
        tool_calls_limit: int,
        max_output_tokens: int,
    ) -> PeerResult:
        validate_call(
            prompt, timeout, request_limit, tool_calls_limit, max_output_tokens
        )
        settings = replace(
            self.settings,
            request_limit=min(request_limit, self.settings.request_limit),
            tool_calls_limit=min(tool_calls_limit, self.settings.tool_calls_limit),
            max_output_tokens=min(max_output_tokens, self.settings.max_output_tokens),
        )
        task_id = str(uuid4())
        future: Future[PeerResult] = Future()
        with self._lock:
            if self._active is not None and not self._active.done():
                raise RuntimeError("Local peer is still busy with its previous task")
            self._active = future
            while len(self._tasks) >= 32:
                self._tasks.pop(next(iter(self._tasks)))
            self._tasks[task_id] = future

        def execute() -> None:
            try:
                # Lazy to keep the interface independent of runtime construction.
                from minimal_local_agent.agent import AgentRuntime

                runtime = AgentRuntime(settings, model_factory=self.model_factory)
                outcome = runtime.run(prompt, session_id=task_id)
                result = PeerResult(
                    outcome.response, task_id, "completed", outcome.usage
                )
            except Exception as exc:
                # Runtime audit retains diagnostics; do not forward secret-bearing
                # provider exceptions or the child's private conversation.
                result = PeerResult(
                    f"Local peer failed ({type(exc).__name__})", task_id, "failed"
                )
            future.set_result(result)

        Thread(target=execute, name="local-agent-peer", daemon=True).start()
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            return PeerResult(
                "Wait timed out; the read-only peer may still be running.",
                task_id,
                "working",
            )

    def get_task(self, task_id: str) -> PeerResult:
        with self._lock:
            future = self._tasks.get(task_id)
        if future is None:
            raise KeyError("Unknown or expired local peer task")
        if not future.done():
            return PeerResult("Read-only peer is still running.", task_id, "working")
        return future.result()


__all__ = ["LocalPeer", "Peer", "PeerResult", "loopback_endpoint", "validate_call"]
