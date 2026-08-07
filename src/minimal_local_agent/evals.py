"""Small, deterministic tool-use checks for locally installed Ollama models."""

from __future__ import annotations

import tempfile
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from minimal_local_agent.agent import LocalAgent
from minimal_local_agent.config import Settings

EVALUATION_REQUEST_LIMIT = 4
EVALUATION_TOOL_CALLS_LIMIT = 4
EVALUATION_MAX_OUTPUT_TOKENS = 4096


@dataclass(frozen=True, slots=True)
class EvaluationCase:
    name: str
    prompt: str
    expected_tool: str
    expected_text: str


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    model: str
    case: str
    passed: bool
    latency_ms: int
    expected_tool: str
    observed_tools: tuple[str, ...]
    tool_events: tuple[dict[str, Any], ...]
    response: str | None
    usage: dict[str, Any] | None
    error: str | None


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    model: str
    results: tuple[EvaluationResult, ...]

    @property
    def passed(self) -> int:
        return sum(result.passed for result in self.results)

    @property
    def total(self) -> int:
        return len(self.results)

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "passed": self.passed,
            "total": self.total,
            "parameters": {
                "temperature": 0.0,
                "request_limit": EVALUATION_REQUEST_LIMIT,
                "tool_calls_limit": EVALUATION_TOOL_CALLS_LIMIT,
                "max_output_tokens": EVALUATION_MAX_OUTPUT_TOKENS,
                "write_policy": "deny",
            },
            "results": [asdict(result) for result in self.results],
        }


def _evaluation_cases(workspace: Path) -> tuple[EvaluationCase, ...]:
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "marker-7f3c1a.txt").write_text(
        "This file exists for the list-files evaluation.\n", encoding="utf-8"
    )
    (workspace / "fact.txt").write_text(
        "verification-code: CODE-91D2E7\n", encoding="utf-8"
    )
    nested = workspace / "nested"
    nested.mkdir()
    (nested / "search-result.md").write_text("needle: FIND-4A8C6E\n", encoding="utf-8")

    return (
        EvaluationCase(
            name="list",
            prompt=(
                "Use list_files on the workspace root. Reply with only the exact "
                "filename that begins with marker-. Do not guess."
            ),
            expected_tool="list_files",
            expected_text="marker-7f3c1a.txt",
        ),
        EvaluationCase(
            name="read",
            prompt=(
                "Use read_file to inspect fact.txt. Reply with only the exact "
                "verification code from that file."
            ),
            expected_tool="read_file",
            expected_text="CODE-91D2E7",
        ),
        EvaluationCase(
            name="search",
            prompt=(
                "Use search_text to find FIND-4A8C6E in the workspace. Reply with "
                "only the exact relative path containing it."
            ),
            expected_tool="search_text",
            expected_text="nested/search-result.md",
        ),
    )


def case_passes(
    case: EvaluationCase,
    response: str,
    events: list[dict[str, Any]],
) -> bool:
    """Require both an expected answer and a successful expected tool call."""

    successful_tools = {
        str(event.get("tool_name")) for event in events if event.get("status") == "ok"
    }
    return (
        case.expected_tool in successful_tools
        and case.expected_text.casefold() in response.casefold()
    )


def evaluate_model(settings: Settings, model: str) -> EvaluationReport:
    """Run three isolated read-only checks against one Ollama model."""

    if not model.strip():
        raise ValueError("Evaluation model cannot be empty")

    with tempfile.TemporaryDirectory(prefix="mla-eval-") as temporary:
        root = Path(temporary)
        workspace = root / "workspace"
        cases = _evaluation_cases(workspace)
        evaluation_settings = replace(
            settings,
            model=model.strip(),
            workspace=workspace,
            database=root / "state.db",
            write_policy="deny",
            temperature=0.0,
            request_limit=EVALUATION_REQUEST_LIMIT,
            tool_calls_limit=EVALUATION_TOOL_CALLS_LIMIT,
            max_output_tokens=EVALUATION_MAX_OUTPUT_TOKENS,
        )
        local_agent = LocalAgent(evaluation_settings)
        results: list[EvaluationResult] = []

        for case in cases:
            session_id = local_agent.store.new_session()
            started = time.perf_counter()
            response: str | None = None
            usage: dict[str, Any] | None = None
            error: str | None = None
            passed = False
            try:
                outcome = local_agent.run(case.prompt, session_id=session_id)
                response = outcome.response
                usage = outcome.usage
                events = local_agent.store.get_tool_events(session_id)
                passed = case_passes(case, response, events)
            except Exception as exc:  # Keep the comparison running after one failure.
                error = str(exc)
                events = local_agent.store.get_tool_events(session_id)

            results.append(
                EvaluationResult(
                    model=model.strip(),
                    case=case.name,
                    passed=passed,
                    latency_ms=round((time.perf_counter() - started) * 1000),
                    expected_tool=case.expected_tool,
                    observed_tools=tuple(
                        f"{event['tool_name']}:{event['status']}" for event in events
                    ),
                    tool_events=tuple(events),
                    response=response,
                    usage=usage,
                    error=error,
                )
            )

    return EvaluationReport(model=model.strip(), results=tuple(results))


__all__ = [
    "EvaluationCase",
    "EvaluationReport",
    "EvaluationResult",
    "EVALUATION_MAX_OUTPUT_TOKENS",
    "EVALUATION_REQUEST_LIMIT",
    "EVALUATION_TOOL_CALLS_LIMIT",
    "case_passes",
    "evaluate_model",
]
