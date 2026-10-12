"""Workspace tool registration; independent of the runtime host."""

from __future__ import annotations

from typing import Any

from pydantic_ai import Agent, RunContext

from minimal_local_agent.mutations import EditRequest, sha256_text
from minimal_local_agent.policy import PolicyEngine


def register_workspace_tools(agent: Agent[Any, str], policy: PolicyEngine) -> None:
    """Register the workspace tools exposed by the effective policy."""

    def list_files(
        ctx: RunContext[Any],
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

    def read_file(ctx: RunContext[Any], path: str) -> dict[str, Any]:
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
        ctx: RunContext[Any],
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
        ctx: RunContext[Any],
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
        ctx: RunContext[Any],
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
        ctx: RunContext[Any],
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
