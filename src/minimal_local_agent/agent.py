"""The single-agent loop and its small, explicit tool registry."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any

from pydantic_ai import Agent, ModelSettings, RunContext, UsageLimits
from pydantic_ai.messages import ModelMessagesTypeAdapter
from pydantic_ai.models.ollama import OllamaModel
from pydantic_ai.providers.ollama import OllamaProvider

from minimal_local_agent.config import Settings
from minimal_local_agent.security import WorkspaceGuard
from minimal_local_agent.store import AuditStore
from minimal_local_agent.workspace import WorkspaceTools

SYSTEM_PROMPT = """
You are a local-first workspace agent. Complete the user's task with the smallest
reasonable number of tool calls.

Rules:
- Work only through the tools you are given and only with relative workspace paths.
- Prefer read-only inspection. Never claim a file was read or changed unless a tool
  result confirms it.
- Before writing, explain the intended change briefly, then call write_file. The
  runtime will ask the user for approval.
- Deletion and shell execution are intentionally unavailable. Do not simulate them.
- If a tool returns an error, correct the request when safe; do not repeat a denied
  write request.
- Stop when the task is complete or the available evidence is insufficient.
- Reply in the user's language and keep the final response concise.
""".strip()


Confirm = Callable[[str, str], bool]


@dataclass(slots=True)
class AgentDependencies:
    session_id: str
    workspace: WorkspaceTools
    store: AuditStore
    confirm: Confirm


@dataclass(frozen=True, slots=True)
class RunOutcome:
    session_id: str
    response: str
    usage: dict[str, Any]


def _usage_dict(value: Any) -> dict[str, Any]:
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "__dict__"):
        return {
            key: item for key, item in vars(value).items() if not key.startswith("_")
        }
    return {"summary": str(value)}


def build_agent(settings: Settings) -> Agent[AgentDependencies, str]:
    model = OllamaModel(
        settings.model,
        provider=OllamaProvider(base_url=settings.base_url),
    )
    agent: Agent[AgentDependencies, str] = Agent(
        model,
        deps_type=AgentDependencies,
        instructions=SYSTEM_PROMPT,
        model_settings=ModelSettings(
            temperature=settings.temperature,
            max_tokens=settings.max_output_tokens,
        ),
    )

    @agent.tool
    def list_files(
        ctx: RunContext[AgentDependencies],
        path: str = ".",
        pattern: str = "**/*",
    ) -> dict[str, Any]:
        """List workspace paths. ``path`` and ``pattern`` must remain relative."""

        try:
            items = ctx.deps.workspace.list_files(path, pattern)
            ctx.deps.store.record_tool_event(
                ctx.deps.session_id,
                "list_files",
                "ok",
                {"path": path, "pattern": pattern, "count": len(items)},
            )
            return {"ok": True, "items": items, "count": len(items)}
        except Exception as exc:  # The error is intentionally returned to the model.
            ctx.deps.store.record_tool_event(
                ctx.deps.session_id,
                "list_files",
                "error",
                {"path": path, "pattern": pattern, "error": str(exc)},
            )
            return {"ok": False, "error": str(exc)}

    @agent.tool
    def read_file(ctx: RunContext[AgentDependencies], path: str) -> dict[str, Any]:
        """Read one UTF-8 text file inside the workspace."""

        try:
            content = ctx.deps.workspace.read_file(path)
            ctx.deps.store.record_tool_event(
                ctx.deps.session_id,
                "read_file",
                "ok",
                {"path": path, "characters": len(content)},
            )
            return {"ok": True, "path": path, "content": content}
        except Exception as exc:
            ctx.deps.store.record_tool_event(
                ctx.deps.session_id,
                "read_file",
                "error",
                {"path": path, "error": str(exc)},
            )
            return {"ok": False, "error": str(exc)}

    @agent.tool
    def search_text(
        ctx: RunContext[AgentDependencies],
        query: str,
        path: str = ".",
        pattern: str = "**/*",
        case_sensitive: bool = False,
    ) -> dict[str, Any]:
        """Search plain text in workspace files and return matching lines."""

        try:
            matches = ctx.deps.workspace.search_text(
                query, path, pattern, case_sensitive=case_sensitive
            )
            ctx.deps.store.record_tool_event(
                ctx.deps.session_id,
                "search_text",
                "ok",
                {
                    "query": query,
                    "path": path,
                    "pattern": pattern,
                    "count": len(matches),
                },
            )
            return {"ok": True, "matches": matches, "count": len(matches)}
        except Exception as exc:
            ctx.deps.store.record_tool_event(
                ctx.deps.session_id,
                "search_text",
                "error",
                {"query": query, "path": path, "error": str(exc)},
            )
            return {"ok": False, "error": str(exc)}

    @agent.tool
    def write_file(
        ctx: RunContext[AgentDependencies],
        path: str,
        content: str,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """Write UTF-8 text after explicit user confirmation."""

        detail = (
            f"Write {len(content.encode('utf-8'))} bytes to {path!r} "
            f"(overwrite={overwrite})"
        )
        if not ctx.deps.confirm("write_file", detail):
            ctx.deps.store.record_tool_event(
                ctx.deps.session_id,
                "write_file",
                "denied",
                {"path": path, "overwrite": overwrite},
            )
            return {
                "ok": False,
                "code": "approval_denied",
                "error": "The user did not approve this write. Do not retry it.",
            }
        try:
            written = ctx.deps.workspace.write_file(path, content, overwrite=overwrite)
            ctx.deps.store.record_tool_event(
                ctx.deps.session_id,
                "write_file",
                "ok",
                {"path": path, "overwrite": overwrite, "bytes": written},
            )
            return {"ok": True, "path": path, "bytes": written}
        except Exception as exc:
            ctx.deps.store.record_tool_event(
                ctx.deps.session_id,
                "write_file",
                "error",
                {"path": path, "overwrite": overwrite, "error": str(exc)},
            )
            return {"ok": False, "error": str(exc)}

    return agent


class LocalAgent:
    """Coordinates the model, bounded tools, and persisted conversation history."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = AuditStore(settings.database)
        self.workspace = WorkspaceTools(
            WorkspaceGuard(settings.workspace),
            max_file_bytes=settings.max_file_bytes,
            max_list_results=settings.max_list_results,
            max_search_results=settings.max_search_results,
            max_search_files=settings.max_search_files,
        )
        self.agent = build_agent(settings)

    def run(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        confirm: Confirm | None = None,
    ) -> RunOutcome:
        if not prompt.strip():
            raise ValueError("Prompt cannot be empty")
        session_id = session_id or self.store.new_session()
        self.store.ensure_session(session_id)
        history_json = self.store.load_history(session_id)
        history = (
            []
            if history_json is None
            else ModelMessagesTypeAdapter.validate_json(history_json)
        )
        dependencies = AgentDependencies(
            session_id=session_id,
            workspace=self.workspace,
            store=self.store,
            confirm=confirm or (lambda _action, _detail: False),
        )

        try:
            result = self.agent.run_sync(
                prompt,
                deps=dependencies,
                message_history=history,
                usage_limits=UsageLimits(
                    request_limit=self.settings.request_limit,
                    tool_calls_limit=self.settings.tool_calls_limit,
                    output_tokens_limit=self.settings.max_output_tokens,
                ),
            )
            response = str(result.output)
            usage_value = result.usage
            if callable(usage_value):  # Compatibility with older PydanticAI releases.
                usage_value = usage_value()
            usage = _usage_dict(usage_value)
            serialized = ModelMessagesTypeAdapter.dump_json(
                result.all_messages()
            ).decode("utf-8")
            self.store.save_history(session_id, serialized)
            self.store.record_run(
                session_id,
                prompt,
                response=response,
                success=True,
                usage=usage,
            )
            return RunOutcome(session_id=session_id, response=response, usage=usage)
        except Exception as exc:
            self.store.record_run(
                session_id,
                prompt,
                response=None,
                success=False,
                error=str(exc),
            )
            raise
