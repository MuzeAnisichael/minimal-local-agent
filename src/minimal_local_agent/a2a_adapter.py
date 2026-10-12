"""Opt-in A2A 1.0 HTTP+JSON adapters using the official Python SDK.

Only explicit loopback origins are supported. Budgets in metadata are advisory
for other implementations, not a protocol-level cost or cancellation guarantee.
No SDK dependency is imported until a client call or server build is requested.
"""

from __future__ import annotations

import asyncio
import math
import re
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlsplit

from minimal_local_agent.peers import (
    LocalPeer,
    PeerResult,
    loopback_endpoint,
    validate_call,
)


def _sdk() -> dict[str, Any]:
    try:
        import httpx
        from a2a.client.card_resolver import A2ACardResolver
        from a2a.client.errors import A2AClientTimeoutError
        from a2a.client.transports.rest import RestTransport
        from a2a.helpers.proto_helpers import (
            get_message_text,
            get_text_parts,
            new_text_message,
        )
        from a2a.types import a2a_pb2
        from a2a.utils.constants import PROTOCOL_VERSION_CURRENT, VERSION_HEADER
        from google.protobuf.json_format import MessageToDict
    except ImportError as exc:
        raise RuntimeError('Install A2A support with: pip install ".[a2a]"') from exc
    return {
        "httpx": httpx,
        "resolver": A2ACardResolver,
        "transport": RestTransport,
        "text_message": new_text_message,
        "message_text": get_message_text,
        "text_parts": get_text_parts,
        "types": a2a_pb2,
        "version": PROTOCOL_VERSION_CURRENT,
        "version_header": VERSION_HEADER,
        "to_dict": MessageToDict,
        "timeout_error": A2AClientTimeoutError,
    }


def _task_id(value: str) -> str:
    # The SDK uses task IDs as URL segments; no query/path injection.
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise ValueError("Unsupported A2A task identifier")
    return value


def _response_text(value: str) -> str:
    if len(value.encode("utf-8")) > 100_000:
        raise ValueError("A2A response exceeds 100,000 bytes")
    return value


def _ensure_sync_caller() -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise RuntimeError("Use asyncio.to_thread for the synchronous A2A peer API")


def _normalize_usage(usage: dict[str, Any]) -> dict[str, Any]:
    # Protobuf Struct encodes every JSON number as a double. Convert only known
    # counters; preserve absent fields as unknown and other metadata unchanged.
    counts = {
        "requests",
        "tool_calls",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cache_write_tokens",
        "cache_read_tokens",
        "input_audio_tokens",
        "cache_audio_read_tokens",
        "output_audio_tokens",
    }
    normalized = dict(usage)
    for key in counts & usage.keys():
        value = usage[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"Invalid A2A usage counter: {key}")
        if value < 0 or (
            isinstance(value, float)
            and (not math.isfinite(value) or not value.is_integer())
        ):
            raise ValueError(f"Invalid A2A usage counter: {key}")
        normalized[key] = int(value)
    return normalized


def _task_result(task: Any, sdk: dict[str, Any]) -> PeerResult:
    state = sdk["types"].TaskState.Name(task.status.state)
    state = state.removeprefix("TASK_STATE_").lower()
    parts = [part for artifact in task.artifacts for part in artifact.parts]
    response = "\n".join(sdk["text_parts"](parts))
    if not response and task.status.HasField("message"):
        response = sdk["message_text"](task.status.message)
    response = _response_text(response)
    usage: dict[str, Any] = {}
    for artifact in task.artifacts:
        metadata = sdk["to_dict"](artifact.metadata)
        envelope = metadata.get("minimal_local_agent", {})
        reported = envelope.get("usage") if isinstance(envelope, dict) else None
        if isinstance(reported, dict):
            usage = _normalize_usage(reported)
    return PeerResult(response, _task_id(task.id), state, usage)


class A2APeer:
    """One trusted local A2A endpoint; only the provided task is sent.

    Uses the official SDK's HTTP+JSON transport and Agent Card parser. The card
    must advertise the exact configured origin and protocol 1.0. No redirects,
    environment proxies, automatic discovery, credentials, or remote files.
    """

    def __init__(self, endpoint: str) -> None:
        self.endpoint = loopback_endpoint(endpoint)

    async def _transport(self, client: Any, sdk: dict[str, Any]) -> Any:
        card = await sdk["resolver"](client, self.endpoint).get_agent_card()
        interfaces = [
            interface
            for interface in card.supported_interfaces
            if interface.protocol_binding == "HTTP+JSON"
            and interface.protocol_version in {"1.0", "1.0.0"}
        ]
        if not interfaces or any(
            loopback_endpoint(interface.url) != self.endpoint or interface.tenant
            for interface in interfaces
        ):
            raise ValueError("A2A card must advertise the configured HTTP+JSON origin")
        if card.security_requirements:
            raise ValueError(
                "Authenticated A2A endpoints are not supported in this MVP"
            )
        return sdk["transport"](client, card, self.endpoint)

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
        _ensure_sync_caller()
        return asyncio.run(
            self._call(
                prompt,
                timeout,
                request_limit,
                tool_calls_limit,
                max_output_tokens,
            )
        )

    async def _call(
        self,
        prompt: str,
        timeout: float,
        request_limit: int,
        tool_calls_limit: int,
        max_output_tokens: int,
    ) -> PeerResult:
        sdk = _sdk()
        types = sdk["types"]
        task_id: str | None = None
        state = "unknown"
        try:
            async with (
                asyncio.timeout(timeout),
                sdk["httpx"].AsyncClient(
                    timeout=timeout,
                    trust_env=False,
                    follow_redirects=False,
                    headers={sdk["version_header"]: sdk["version"]},
                ) as client,
            ):
                transport = await self._transport(client, sdk)
                sent = await transport.send_message(
                    types.SendMessageRequest(
                        message=sdk["text_message"](prompt, role=types.Role.ROLE_USER),
                        configuration=types.SendMessageConfiguration(
                            return_immediately=True,
                            history_length=0,
                            accepted_output_modes=["text/plain"],
                        ),
                        metadata={
                            "minimal_local_agent": {
                                "request_limit": request_limit,
                                "tool_calls_limit": tool_calls_limit,
                                "max_output_tokens": max_output_tokens,
                            }
                        },
                    )
                )
                if sent.WhichOneof("payload") == "message":
                    return PeerResult(
                        _response_text(sdk["message_text"](sent.message)),
                        None,
                        "completed",
                    )
                if sent.WhichOneof("payload") != "task":
                    raise ValueError("A2A response has no task or message")
                task = sent.task
                while True:
                    result = _task_result(task, sdk)
                    task_id, state = result.task_id, result.state
                    if state not in {"submitted", "working"}:
                        return result
                    await asyncio.sleep(0.1)
                    task = await transport.get_task(
                        types.GetTaskRequest(id=task_id, history_length=0)
                    )
        except (TimeoutError, sdk["httpx"].TimeoutException, sdk["timeout_error"]):
            return PeerResult(
                "Wait timed out; remote execution may continue and usage is unknown.",
                task_id,
                state,
            )

    def get_task(self, task_id: str, *, timeout: float = 5.0) -> PeerResult:
        task_id = _task_id(task_id)
        validate_call("query task", timeout, 1, 1, 1)
        _ensure_sync_caller()

        async def query() -> PeerResult:
            sdk = _sdk()
            async with (
                asyncio.timeout(timeout),
                sdk["httpx"].AsyncClient(
                    timeout=timeout,
                    trust_env=False,
                    follow_redirects=False,
                    headers={sdk["version_header"]: sdk["version"]},
                ) as client,
            ):
                transport = await self._transport(client, sdk)
                task = await transport.get_task(
                    sdk["types"].GetTaskRequest(id=task_id, history_length=0)
                )
                return _task_result(task, sdk)

        return asyncio.run(query())


def build_a2a_app(
    peer: LocalPeer,
    endpoint: str,
    *,
    name: str = "Minimal Local Agent (read-only peer)",
    version: str = "1.1.0",
) -> Any:
    """Build an opt-in loopback-only ASGI app with SDK tasks and Agent Card.

    Bind the ASGI server to 127.0.0.1. The extra boundary also rejects non-loopback
    clients, unexpected Host headers, and browser Origins. No authentication is
    provided: other local processes are trusted. The task index is in-memory;
    the child runtime's own receipts persist independently. Cancellation is
    explicitly unsupported because Python cannot safely terminate model threads.
    """

    if not isinstance(peer, LocalPeer):
        raise TypeError("A2A server requires an independently configured LocalPeer")
    endpoint = loopback_endpoint(endpoint)
    sdk = _sdk()
    try:
        from a2a.helpers.proto_helpers import new_task, new_text_part
        from a2a.server.agent_execution import AgentExecutor
        from a2a.server.request_handlers import DefaultRequestHandler
        from a2a.server.routes.agent_card_routes import create_agent_card_routes
        from a2a.server.routes.rest_routes import create_rest_routes
        from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
        from a2a.utils.errors import InvalidParamsError, TaskNotCancelableError
        from starlette.applications import Starlette
        from starlette.responses import PlainTextResponse
    except ImportError as exc:
        raise RuntimeError('Install A2A support with: pip install ".[a2a]"') from exc
    types = sdk["types"]

    class Executor(AgentExecutor):
        async def execute(self, context: Any, event_queue: Any) -> None:
            message = context.message
            if (
                message is None
                or message.role != types.Role.ROLE_USER
                or not message.parts
                or any(not part.HasField("text") for part in message.parts)
            ):
                raise InvalidParamsError("Only user text tasks are supported")
            limits = context.metadata.get("minimal_local_agent", {})
            if not isinstance(limits, dict):
                raise InvalidParamsError("Invalid local peer budget")
            values: dict[str, int] = {}
            for key in ("request_limit", "tool_calls_limit", "max_output_tokens"):
                value = limits.get(key, getattr(peer.settings, key))
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    or value < 1
                    or value != int(value)
                ):
                    raise InvalidParamsError(
                        "Peer budget must contain positive integers"
                    )
                values[key] = min(int(value), getattr(peer.settings, key))
            prompt = context.get_user_input()
            try:
                validate_call(prompt, 60.0, **values)
            except ValueError as exc:
                raise InvalidParamsError(str(exc)) from exc
            task = new_task(
                context.task_id,
                context.context_id,
                types.TaskState.TASK_STATE_SUBMITTED,
                history=[message],
            )
            await event_queue.enqueue_event(task)
            updater = TaskUpdater(event_queue, task.id, task.context_id)
            await updater.start_work()
            try:
                result = await asyncio.to_thread(
                    peer.call, prompt, timeout=60.0, **values
                )
                # A LocalPeer timeout only stops waiting. Keep the protocol task
                # working and let status queries observe the eventual outcome.
                while result.state == "working":
                    await asyncio.sleep(0.1)
                    result = peer.get_task(result.task_id)
            except RuntimeError:
                await updater.reject(
                    updater.new_agent_message([new_text_part("Peer is already busy.")])
                )
                return
            await updater.add_artifact(
                [new_text_part(result.response)],
                name="response",
                metadata={"minimal_local_agent": {"usage": result.usage}},
            )
            state = (
                types.TaskState.TASK_STATE_COMPLETED
                if result.state == "completed"
                else types.TaskState.TASK_STATE_FAILED
            )
            await updater.update_status(state)

        async def cancel(self, _context: Any, _event_queue: Any) -> None:
            raise TaskNotCancelableError(
                "Read-only model execution cannot be terminated"
            )

    class Handler(DefaultRequestHandler):
        async def on_cancel_task(self, request: Any, context: Any = None) -> Any:
            # Reject before the SDK cancels its asyncio wrapper around the model
            # thread, which would incorrectly suggest the thread had stopped.
            raise TaskNotCancelableError(
                "Read-only model execution cannot be terminated"
            )

    card = types.AgentCard(
        name=name,
        description="Independent read-only task delegation; no shared memory.",
        version=version,
        supported_interfaces=[
            types.AgentInterface(
                url=endpoint, protocol_binding="HTTP+JSON", protocol_version="1.0"
            )
        ],
        capabilities=types.AgentCapabilities(streaming=False, push_notifications=False),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        skills=[
            types.AgentSkill(
                id="read-only-workspace-task",
                name="Read-only workspace task",
                description="Inspect the separately configured local workspace.",
                tags=["local", "read-only"],
            )
        ],
    )
    handler = Handler(Executor(), InMemoryTaskStore(), card, validate_input_modes=True)

    @asynccontextmanager
    async def lifespan(_app: Any) -> Any:
        try:
            yield
        finally:
            await handler.aclose()

    app = Starlette(
        routes=[*create_agent_card_routes(card), *create_rest_routes(handler)],
        lifespan=lifespan,
    )
    expected_host = urlsplit(endpoint).netloc.lower()

    class LoopbackBoundary:
        def __init__(self, wrapped: Any) -> None:
            self.app = wrapped

        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            if scope["type"] == "http":
                headers = {key.lower(): value for key, value in scope["headers"]}
                client = scope.get("client") or ("", 0)
                host = headers.get(b"host", b"").decode("ascii", errors="replace")
                if (
                    client[0] not in {"127.0.0.1", "::1"}
                    or host.lower() != expected_host
                    or b"origin" in headers
                ):
                    await PlainTextResponse(
                        "Local agent clients only", status_code=403
                    )(scope, receive, send)
                    return
            await self.app(scope, receive, send)

    return LoopbackBoundary(app)


__all__ = ["A2APeer", "build_a2a_app"]
