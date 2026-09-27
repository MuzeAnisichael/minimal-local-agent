"""Dependency-free local web console for Minimal Local Agent."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from minimal_local_agent import __version__
from minimal_local_agent.config import Settings
from minimal_local_agent.models import probe_model
from minimal_local_agent.policy import PolicyEngine
from minimal_local_agent.runtime import AgentRuntime
from minimal_local_agent.store import AuditStore

_ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost"})
_MAX_REQUEST_BYTES = 64 * 1024
_MAX_PROMPT_CHARS = 20_000
_ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/assets/app.css": ("app.css", "text/css; charset=utf-8"),
    "/assets/app.js": ("app.js", "text/javascript; charset=utf-8"),
}


class ApiError(ValueError):
    """An expected request error with an HTTP status."""

    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status


class AgentRunError(RuntimeError):
    """A failed run whose audit session can still be opened in the UI."""

    def __init__(self, session_id: str, message: str) -> None:
        super().__init__(message)
        self.session_id = session_id


RuntimeFactory = Callable[[Settings], AgentRuntime]


class WebApp:
    """Small same-origin JSON API over the existing runtime and audit store."""

    def __init__(
        self,
        settings: Settings,
        *,
        runtime_factory: RuntimeFactory = AgentRuntime,
    ) -> None:
        self.settings = settings
        self.runtime_factory = runtime_factory
        self.store = AuditStore(settings.database)

    @property
    def external_tools(self) -> tuple[str, ...]:
        return tuple(
            tool
            for server in self.settings.mcp_servers
            for tool in server.visible_tools
        )

    def _model_status(self) -> dict[str, Any]:
        return probe_model(self.settings).to_dict()

    def status(self) -> dict[str, Any]:
        read_settings = replace(self.settings, write_policy="deny")
        preview_settings = replace(self.settings, write_policy="preview")
        return {
            "name": "Minimal Local Agent",
            "version": __version__,
            "model": self.settings.model,
            "provider": self.settings.provider,
            "workspace": str(self.settings.workspace),
            "model_status": self._model_status(),
            "modes": {
                "read": {
                    "label": "只读",
                    "capabilities": PolicyEngine(read_settings).manifest(
                        self.external_tools
                    ),
                },
                "preview": {
                    "label": "变更预览",
                    "capabilities": PolicyEngine(preview_settings).manifest(
                        self.external_tools
                    ),
                },
            },
        }

    def list_sessions(self, limit: int) -> dict[str, Any]:
        safe_limit = min(max(limit, 1), 50)
        return {"sessions": self.store.list_sessions(limit=safe_limit)}

    def session(self, session_id: str) -> dict[str, Any]:
        runs: list[dict[str, Any]] = []
        for row in self.store.get_runs(session_id):
            item = dict(row)
            usage_json = item.pop("usage_json", None)
            item["usage"] = json.loads(usage_json) if usage_json else None
            item["success"] = bool(item["success"])
            runs.append(item)

        receipts = [
            {
                "id": row["id"],
                "run_id": row["run_id"],
                "created_at": row["created_at"],
                "previous_hash": row["previous_hash"],
                "receipt_hash": row["receipt_hash"],
                "success": bool(row["receipt"].get("success")),
            }
            for row in self.store.get_receipts(session_id)
        ]
        verified, verification_error = self.store.verify_receipt_chain(session_id)
        return {
            "session_id": session_id,
            "runs": runs,
            "tool_events": self.store.get_tool_events(session_id),
            "receipts": receipts,
            "receipt_chain": {
                "verified": verified,
                "error": verification_error,
            },
        }

    def run(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "请求内容必须是 JSON 对象")

        prompt = payload.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ApiError(HTTPStatus.BAD_REQUEST, "请输入任务内容")
        if len(prompt) > _MAX_PROMPT_CHARS:
            raise ApiError(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                f"任务内容不能超过 {_MAX_PROMPT_CHARS} 个字符",
            )

        mode = payload.get("mode", "read")
        if not isinstance(mode, str) or mode not in {"read", "preview"}:
            raise ApiError(HTTPStatus.BAD_REQUEST, "模式必须是 read 或 preview")

        session_id = payload.get("session_id")
        if session_id is not None and not isinstance(session_id, str):
            raise ApiError(HTTPStatus.BAD_REQUEST, "session_id 必须是字符串")
        if session_id is not None and (not session_id.strip() or len(session_id) > 120):
            raise ApiError(
                HTTPStatus.BAD_REQUEST,
                "session_id 长度必须在 1 到 120 之间",
            )

        settings = replace(
            self.settings,
            write_policy="deny" if mode == "read" else "preview",
        )
        runtime = self.runtime_factory(settings)
        run_session_id = session_id or runtime.store.new_session()
        events: list[dict[str, Any]] = []
        try:
            outcome = runtime.run(
                prompt.strip(),
                session_id=run_session_id,
                confirm=lambda _action, _detail: False,
                event_handler=lambda event: events.append(event.to_dict()),
            )
        except Exception as exc:
            raise AgentRunError(run_session_id, str(exc)) from exc
        chain_verified, _ = runtime.store.verify_receipt_chain(outcome.session_id)
        return {
            "session_id": outcome.session_id,
            "response": outcome.response,
            "usage": outcome.usage,
            "receipt_hash": outcome.receipt_hash,
            "receipt_chain_verified": chain_verified,
            "events": events,
            "event_handler_errors": list(outcome.event_handler_errors),
            "mode": mode,
        }


class WebServer(ThreadingHTTPServer):
    """HTTP server carrying the application object for request handlers."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], app: WebApp) -> None:
        self.app = app
        super().__init__(address, WebRequestHandler)


class WebRequestHandler(BaseHTTPRequestHandler):
    """Serve package assets and a deliberately small same-origin API."""

    server: WebServer
    server_version = "MinimalLocalAgentWeb"
    sys_version = ""

    def log_message(self, _format: str, *_args: Any) -> None:
        # The CLI prints one clean startup message; requests stay quiet.
        return

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'",
        )

    def _send_bytes(
        self,
        status: HTTPStatus,
        content: bytes,
        content_type: str,
        *,
        cache: str = "no-store",
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", cache)
        self._security_headers()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(content)

    def _send_json(self, status: HTTPStatus, payload: Any) -> None:
        content = json.dumps(
            payload,
            ensure_ascii=False,
            default=str,
            separators=(",", ":"),
        ).encode("utf-8")
        self._send_bytes(status, content, "application/json; charset=utf-8")

    def _send_api_error(self, error: ApiError) -> None:
        self._send_json(error.status, {"error": str(error)})

    def _host_allowed(self) -> bool:
        host = self.headers.get("Host")
        if not host:
            return False
        try:
            parsed = urlparse(f"http://{host}")
            return (
                parsed.hostname in _ALLOWED_HOSTS
                and parsed.port == self.server.server_address[1]
                and parsed.username is None
                and parsed.password is None
                and not parsed.path
                and not parsed.query
                and not parsed.fragment
            )
        except ValueError:
            return False

    def _origin_allowed(self) -> bool:
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        try:
            parsed = urlparse(origin)
            host = urlparse(f"http://{self.headers['Host']}")
        except ValueError:
            return False
        return (
            parsed.scheme == "http"
            and parsed.hostname == host.hostname
            and parsed.port == host.port
            and not parsed.path
            and not parsed.query
            and not parsed.fragment
        )

    def _read_json(self) -> Any:
        if not self._origin_allowed():
            raise ApiError(HTTPStatus.FORBIDDEN, "拒绝非同源请求")
        content_type = self.headers.get("Content-Type", "")
        if not content_type.casefold().startswith("application/json"):
            raise ApiError(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                "Content-Type 必须是 application/json",
            )
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise ApiError(HTTPStatus.LENGTH_REQUIRED, "缺少 Content-Length")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "Content-Length 无效") from exc
        if length < 0 or length > _MAX_REQUEST_BYTES:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "请求内容过大")
        try:
            return json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "JSON 内容无效") from exc

    def _serve_asset(self, path: str) -> bool:
        asset = _ASSETS.get(path)
        if asset is None:
            return False
        name, content_type = asset
        content = files("minimal_local_agent").joinpath("web_assets", name).read_bytes()
        cache = "public, max-age=300" if path.startswith("/assets/") else "no-store"
        self._send_bytes(HTTPStatus.OK, content, content_type, cache=cache)
        return True

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if not self._host_allowed():
                raise ApiError(HTTPStatus.FORBIDDEN, "拒绝无效的 Host")
            if self._serve_asset(path):
                return
            if path == "/favicon.ico":
                self._send_bytes(HTTPStatus.NO_CONTENT, b"", "image/x-icon")
                return
            if path == "/api/status":
                self._send_json(HTTPStatus.OK, self.server.app.status())
                return
            if path == "/api/sessions":
                query = parse_qs(parsed.query)
                try:
                    limit = int(query.get("limit", ["20"])[0])
                except ValueError as exc:
                    raise ApiError(HTTPStatus.BAD_REQUEST, "limit 必须是整数") from exc
                self._send_json(
                    HTTPStatus.OK,
                    self.server.app.list_sessions(limit),
                )
                return
            if path.startswith("/api/sessions/"):
                session_id = unquote(path.removeprefix("/api/sessions/"))
                if not session_id:
                    raise ApiError(HTTPStatus.BAD_REQUEST, "缺少 session_id")
                self._send_json(
                    HTTPStatus.OK,
                    self.server.app.session(session_id),
                )
                return
            raise ApiError(HTTPStatus.NOT_FOUND, "未找到该页面")
        except ApiError as exc:
            self._send_api_error(exc)
        except ValueError as exc:
            self._send_api_error(ApiError(HTTPStatus.BAD_REQUEST, str(exc)))
        except Exception as exc:
            self._send_api_error(
                ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, f"服务错误：{exc}")
            )

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            if not self._host_allowed():
                raise ApiError(HTTPStatus.FORBIDDEN, "拒绝无效的 Host")
            if parsed.path != "/api/run":
                raise ApiError(HTTPStatus.NOT_FOUND, "未找到该接口")
            payload = self._read_json()
            self._send_json(HTTPStatus.OK, self.server.app.run(payload))
        except ApiError as exc:
            self._send_api_error(exc)
        except AgentRunError as exc:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": str(exc), "session_id": exc.session_id},
            )
        except ValueError as exc:
            self._send_api_error(ApiError(HTTPStatus.BAD_REQUEST, str(exc)))
        except Exception as exc:
            self._send_api_error(
                ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, f"Agent 运行失败：{exc}")
            )


def create_web_server(
    settings: Settings,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    runtime_factory: RuntimeFactory = AgentRuntime,
) -> WebServer:
    """Create, but do not start, the local-only web server."""

    if host not in _ALLOWED_HOSTS:
        allowed = ", ".join(sorted(_ALLOWED_HOSTS))
        raise ValueError(f"Web host must be loopback-only: {allowed}")
    if not 0 <= port <= 65_535:
        raise ValueError("Web port must be between 0 and 65535")
    return WebServer((host, port), WebApp(settings, runtime_factory=runtime_factory))


def serve_web(
    settings: Settings,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> int:
    """Run the web console until interrupted."""

    server = create_web_server(settings, host=host, port=port)
    actual_port = server.server_address[1]
    print(f"Minimal Local Agent Web: http://{host}:{actual_port}", flush=True)
    print("Press Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
    return 0


__all__ = ["WebApp", "WebServer", "create_web_server", "serve_web"]
