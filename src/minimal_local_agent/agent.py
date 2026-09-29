"""The single-agent runtime and its small, policy-compiled tool registry."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any

from pydantic_ai import Agent, ModelSettings, RunContext, UsageLimits
from pydantic_ai.capabilities import ProcessHistory
from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter

from minimal_local_agent.config import Settings
from minimal_local_agent.context import (
    ContextBudgetExceeded,
    ContextReducer,
    select_context,
)
from minimal_local_agent.events import EventBus, EventHandler
from minimal_local_agent.mcp import build_mcp_bundle
from minimal_local_agent.models import ConfiguredModelFactory, ModelFactory
from minimal_local_agent.mutations import EditRequest, MutationEngine, sha256_text
from minimal_local_agent.policy import PolicyEngine
from minimal_local_agent.read_tools import ReadTool, build_read_toolset
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
- Deletion and shell execution are intentionally unavailable. Do not simulate them.
- If a tool returns an error, correct the request when safe; do not repeat a denied
  write request.
- Stop when the task is complete or the available evidence is insufficient.
- Reply in the user's language and keep the final response concise.
""".strip()


def _system_prompt(policy: PolicyEngine) -> str:
    mutations = {
        name: policy.decision(name, "write") for name in ("write_file", "edit_files")
    }
    visible = {
        name: decision for name, decision in mutations.items() if decision != "deny"
    }
    if not visible:
        capability = """
Runtime capability:
- This run is read-only. No write tool is registered.
- You may explain or propose changes, but never claim they were applied.
"""
    else:
        policy_lines = "\n".join(
            f"- {name}: {decision}" for name, decision in visible.items()
        )
        capability = f"""
Runtime capability:
- write_file creates or replaces a full file. edit_files performs exact replacements
  across one or more existing files and should be preferred for modifications.
- Effective mutation policies:
{policy_lines}
- A preview tool validates and shows a diff but never modifies files. Never claim a
  previewed change was applied. An ask tool requires one-time approval.
"""
    return f"{SYSTEM_PROMPT}\n\n{capability.strip()}"


Confirm = Callable[[str, str], bool]


@dataclass(slots=True)
class AgentDependencies:
    session_id: str
    workspace: WorkspaceTools
    mutations: MutationEngine
    store: AuditStore
    confirm: Confirm
    events: EventBus
    max_context_bytes: int
    context_reducer: ContextReducer | None
    context_checks: list[dict[str, Any]]

    def record_tool_event(
        self,
        tool_name: str,
        status: str,
        details: dict[str, Any],
    ) -> None:
        self.store.record_tool_event(self.session_id, tool_name, status, details)
        self.events.emit(
            "tool.completed",
            {"tool_name": tool_name, "status": status, "details": details},
        )


@dataclass(frozen=True, slots=True)
class RunOutcome:
    session_id: str
    response: str
    usage: dict[str, Any]
    receipt_hash: str
    event_handler_errors: tuple[str, ...] = ()


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


def _prepare_history(
    ctx: RunContext[AgentDependencies], messages: list[ModelMessage]
) -> list[ModelMessage]:
    try:
        selected = select_context(
            messages, ctx.deps.max_context_bytes, ctx.deps.context_reducer
        )
    except ContextBudgetExceeded as exc:
        details = {**exc.details, "status": "rejected"}
        ctx.deps.context_checks.append(details)
        ctx.deps.events.emit("context.rejected", details)
        raise
    details = {
        **selected.details,
        "status": "reduced" if selected.details["reduced"] else "ok",
    }
    ctx.deps.context_checks.append(details)
    if selected.details["reduced"]:
        ctx.deps.events.emit("context.reduced", details)
    return selected.messages


def build_agent(
    settings: Settings,
    *,
    policy: PolicyEngine | None = None,
    model_factory: ModelFactory | None = None,
    toolsets: Sequence[Any] = (),
) -> Agent[AgentDependencies, str]:
    """Compile settings and policy into the model-visible tool surface."""

    policy = policy or PolicyEngine(settings)
    model = (model_factory or ConfiguredModelFactory()).create(settings)
    agent: Agent[AgentDependencies, str] = Agent(
        model,
        deps_type=AgentDependencies,
        instructions=_system_prompt(policy),
        toolsets=list(toolsets),
        model_settings=ModelSettings(
            temperature=settings.temperature,
            max_tokens=settings.max_output_tokens,
        ),
        capabilities=[ProcessHistory(_prepare_history)],
    )

    def list_files(
        ctx: RunContext[AgentDependencies],
        path: str = ".",
    ) -> dict[str, Any]:
        """Recursively list paths under a relative workspace directory."""

        try:
            items = ctx.deps.workspace.list_files(path)
            ctx.deps.record_tool_event(
                "list_files", "ok", {"path": path, "count": len(items)}
            )
            return {"ok": True, "items": items, "count": len(items)}
        except Exception as exc:  # The error is intentionally returned to the model.
            ctx.deps.record_tool_event(
                "list_files", "error", {"path": path, "error": str(exc)}
            )
            return {"ok": False, "error": str(exc)}

    def read_file(ctx: RunContext[AgentDependencies], path: str) -> dict[str, Any]:
        """Read one UTF-8 text file inside the workspace."""

        try:
            content = ctx.deps.workspace.read_file(path)
            digest = sha256_text(content)
            ctx.deps.record_tool_event(
                "read_file",
                "ok",
                {"path": path, "characters": len(content), "sha256": digest},
            )
            return {
                "ok": True,
                "path": path,
                "content": content,
                "sha256": digest,
            }
        except Exception as exc:
            ctx.deps.record_tool_event(
                "read_file", "error", {"path": path, "error": str(exc)}
            )
            return {"ok": False, "error": str(exc)}

    def search_text(
        ctx: RunContext[AgentDependencies],
        query: str,
        path: str = ".",
        case_sensitive: bool = False,
    ) -> dict[str, Any]:
        """Recursively search text under a relative workspace directory."""

        try:
            matches = ctx.deps.workspace.search_text(
                query, path, case_sensitive=case_sensitive
            )
            ctx.deps.record_tool_event(
                "search_text",
                "ok",
                {"query": query, "path": path, "count": len(matches)},
            )
            return {"ok": True, "matches": matches, "count": len(matches)}
        except Exception as exc:
            ctx.deps.record_tool_event(
                "search_text",
                "error",
                {"query": query, "path": path, "error": str(exc)},
            )
            return {"ok": False, "error": str(exc)}

    def handle_mutation(
        ctx: RunContext[AgentDependencies],
        tool_name: str,
        transaction: Any,
    ) -> dict[str, Any]:
        audit_details = transaction.audit_details()
        decision = policy.decision(tool_name, "write")
        ctx.deps.events.emit(
            "mutation.preview",
            {
                "tool_name": tool_name,
                "decision": decision,
                "paths": audit_details["paths"],
                "diff": transaction.approval_detail(),
            },
        )
        if decision == "preview":
            ctx.deps.record_tool_event(tool_name, "preview", audit_details)
            return {
                "ok": True,
                "applied": False,
                "dry_run": True,
                "paths": audit_details["paths"],
                "message": "Validated preview only; no files were changed.",
            }
        if not ctx.deps.confirm(tool_name, transaction.approval_detail()):
            ctx.deps.record_tool_event(tool_name, "denied", audit_details)
            return {
                "ok": False,
                "code": "approval_denied",
                "error": "The user did not approve this change. Do not retry it.",
            }
        try:
            change_set_id = ctx.deps.mutations.commit(
                transaction,
                session_id=ctx.deps.session_id,
                kind=tool_name,
            )
            details = {**audit_details, "change_set_id": change_set_id}
            ctx.deps.record_tool_event(tool_name, "ok", details)
            ctx.deps.events.emit("mutation.applied", details)
            return {
                "ok": True,
                "applied": True,
                "change_set_id": change_set_id,
                "paths": audit_details["paths"],
            }
        except Exception as exc:
            ctx.deps.record_tool_event(
                tool_name, "error", {**audit_details, "error": str(exc)}
            )
            return {"ok": False, "error": str(exc)}

    def write_file(
        ctx: RunContext[AgentDependencies],
        path: str,
        content: str,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """Validate, preview, and write UTF-8 text after explicit approval."""

        try:
            transaction = ctx.deps.mutations.prepare_write(
                path, content, overwrite=overwrite
            )
        except Exception as exc:
            ctx.deps.record_tool_event(
                "write_file",
                "error",
                {"path": path, "overwrite": overwrite, "error": str(exc)},
            )
            return {"ok": False, "error": str(exc)}
        return handle_mutation(ctx, "write_file", transaction)

    def edit_files(
        ctx: RunContext[AgentDependencies],
        edits: list[EditRequest],
    ) -> dict[str, Any]:
        """Apply exact, unique text replacements as one approved transaction."""

        try:
            transaction = ctx.deps.mutations.prepare_edits(edits)
        except Exception as exc:
            ctx.deps.record_tool_event(
                "edit_files",
                "error",
                {"paths": [edit.path for edit in edits], "error": str(exc)},
            )
            return {"ok": False, "error": str(exc)}
        return handle_mutation(ctx, "edit_files", transaction)

    builtin_tools = (
        ("list_files", "read", list_files),
        ("read_file", "read", read_file),
        ("search_text", "read", search_text),
        ("write_file", "write", write_file),
        ("edit_files", "write", edit_files),
    )
    for name, effect, function in builtin_tools:
        if policy.exposes(name, effect):
            agent.tool(function, name=name)

    return agent


class AgentRuntime:
    """Stable embedding API for one model, policy, workspace, and audit store."""

    def __init__(
        self,
        settings: Settings,
        *,
        model_factory: ModelFactory | None = None,
        read_tools: Sequence[ReadTool] = (),
        context_reducer: ContextReducer | None = None,
    ) -> None:
        self.settings = settings
        self.context_reducer = context_reducer
        self.policy = PolicyEngine(settings)
        read_toolset, self.python_tools = build_read_toolset(
            tuple(read_tools), settings, self.policy
        )
        mcp_bundle = build_mcp_bundle(settings, self.policy)
        self.mcp_tools = mcp_bundle.tool_names
        self.store = AuditStore(settings.database)
        self.workspace = WorkspaceTools(
            WorkspaceGuard(settings.workspace),
            max_file_bytes=settings.max_file_bytes,
            max_list_results=settings.max_list_results,
            max_search_results=settings.max_search_results,
            max_search_files=settings.max_search_files,
        )
        self.mutations = MutationEngine(
            self.workspace,
            self.store,
            max_files=settings.max_transaction_files,
            max_diff_chars=settings.max_diff_chars,
        )
        self.agent = build_agent(
            settings,
            policy=self.policy,
            model_factory=model_factory,
            toolsets=(
                *((read_toolset,) if read_toolset is not None else ()),
                *mcp_bundle.toolsets,
            ),
        )

    def _policy_manifest(self) -> list[dict[str, str]]:
        return self.policy.manifest(self.mcp_tools, python_tools=self.python_tools)

    def _policy_fingerprint(self) -> str:
        return self.policy.fingerprint(self.mcp_tools, python_tools=self.python_tools)

    def _receipt(
        self,
        *,
        session_id: str,
        run_id: int,
        prompt: str,
        response: str | None,
        success: bool,
        usage: dict[str, Any] | None,
        error: str | None,
        tool_events: list[dict[str, Any]],
        context_checks: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "schema": "minimal-local-agent.execution-receipt.v1",
            "session_id": session_id,
            "run_id": run_id,
            "model": self.settings.model,
            "provider": self.settings.provider,
            "endpoint_sha256": sha256_text(self.settings.base_url),
            "prompt_sha256": sha256_text(prompt),
            "response_sha256": None if response is None else sha256_text(response),
            "success": success,
            "error": error,
            "usage": usage,
            "policy": self._policy_manifest(),
            "policy_sha256": self._policy_fingerprint(),
            "tool_events": [
                {
                    "tool_name": event["tool_name"],
                    "status": event["status"],
                    "details": event["details"],
                }
                for event in tool_events
            ],
            "context_checks": context_checks,
        }

    def run(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        confirm: Confirm | None = None,
        event_handler: EventHandler | None = None,
    ) -> RunOutcome:
        if not prompt.strip():
            raise ValueError("Prompt cannot be empty")
        session_id = session_id or self.store.new_session()
        self.store.ensure_session(session_id)
        existing_events = self.store.get_tool_events(session_id)
        previous_event_id = existing_events[-1]["id"] if existing_events else 0
        events = EventBus(session_id, event_handler)
        events.emit(
            "run.started",
            {
                "model": self.settings.model,
                "policy_sha256": self._policy_fingerprint(),
            },
        )
        history_json = self.store.load_history(session_id)
        history = (
            []
            if history_json is None
            else ModelMessagesTypeAdapter.validate_json(history_json)
        )
        context_checks: list[dict[str, Any]] = []
        dependencies = AgentDependencies(
            session_id=session_id,
            workspace=self.workspace,
            mutations=self.mutations,
            store=self.store,
            confirm=confirm or (lambda _action, _detail: False),
            events=events,
            max_context_bytes=self.settings.max_context_bytes,
            context_reducer=self.context_reducer,
            context_checks=context_checks,
        )

        try:
            result = self.agent.run_sync(
                prompt,
                deps=dependencies,
                message_history=list(history),
                usage_limits=UsageLimits(
                    request_limit=self.settings.request_limit,
                    tool_calls_limit=self.settings.tool_calls_limit,
                    output_tokens_limit=self.settings.max_output_tokens,
                ),
            )
            response = str(result.output)
            usage = _usage_dict(result.usage)
            serialized = ModelMessagesTypeAdapter.dump_json(
                [*history, *result.new_messages()]
            ).decode("utf-8")
            self.store.save_history(session_id, serialized)
        except Exception as exc:
            run_id = self.store.record_run(
                session_id,
                prompt,
                response=None,
                success=False,
                error=str(exc),
            )
            tool_events = [
                event
                for event in self.store.get_tool_events(session_id)
                if event["id"] > previous_event_id
            ]
            receipt_hash = self.store.record_receipt(
                session_id,
                run_id,
                self._receipt(
                    session_id=session_id,
                    run_id=run_id,
                    prompt=prompt,
                    response=None,
                    success=False,
                    usage=None,
                    error=str(exc),
                    tool_events=tool_events,
                    context_checks=context_checks,
                ),
            )
            events.emit(
                "run.failed",
                {"run_id": run_id, "receipt_hash": receipt_hash, "error": str(exc)},
            )
            raise

        run_id = self.store.record_run(
            session_id,
            prompt,
            response=response,
            success=True,
            usage=usage,
        )
        tool_events = [
            event
            for event in self.store.get_tool_events(session_id)
            if event["id"] > previous_event_id
        ]
        receipt_hash = self.store.record_receipt(
            session_id,
            run_id,
            self._receipt(
                session_id=session_id,
                run_id=run_id,
                prompt=prompt,
                response=response,
                success=True,
                usage=usage,
                error=None,
                tool_events=tool_events,
                context_checks=context_checks,
            ),
        )
        events.emit(
            "run.completed",
            {"run_id": run_id, "receipt_hash": receipt_hash, "usage": usage},
        )
        return RunOutcome(
            session_id=session_id,
            response=response,
            usage=usage,
            receipt_hash=receipt_hash,
            event_handler_errors=events.handler_errors,
        )


# Kept for source compatibility with v0.1-v0.3 integrations.
LocalAgent = AgentRuntime


__all__ = [
    "AgentDependencies",
    "AgentRuntime",
    "Confirm",
    "LocalAgent",
    "RunOutcome",
    "build_agent",
]
