"""Extensible, deterministic tool-use checks for locally installed models."""

from __future__ import annotations

import json
import tempfile
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from minimal_local_agent.config import Settings
from minimal_local_agent.runtime import AgentRuntime
from minimal_local_agent.security import WorkspaceGuard

EVALUATION_REQUEST_LIMIT = 4
EVALUATION_TOOL_CALLS_LIMIT = 4
EVALUATION_MAX_OUTPUT_TOKENS = 4096
DATASET_SCHEMA = "minimal-local-agent.eval-dataset.v1"


@dataclass(frozen=True, slots=True)
class EvaluationCase:
    name: str
    prompt: str
    expected_tool: str | None
    expected_text: str
    expected_status: str = "ok"
    forbidden_tools: tuple[str, ...] = ()
    files: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class EvaluationDataset:
    name: str
    cases: tuple[EvaluationCase, ...]


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    model: str
    case: str
    passed: bool
    latency_ms: int
    expected_tool: str | None
    observed_tools: tuple[str, ...]
    tool_events: tuple[dict[str, Any], ...]
    response: str | None
    usage: dict[str, Any] | None
    error: str | None


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    model: str
    dataset: str
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
            "dataset": self.dataset,
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


def builtin_dataset() -> EvaluationDataset:
    return EvaluationDataset(
        name="builtin-read-tools",
        cases=(
            EvaluationCase(
                name="list",
                prompt=(
                    "Use list_files on the workspace root. Reply with only the exact "
                    "filename that begins with marker-. Do not guess."
                ),
                expected_tool="list_files",
                expected_text="marker-7f3c1a.txt",
                files=(("marker-7f3c1a.txt", "list evaluation marker\n"),),
            ),
            EvaluationCase(
                name="read",
                prompt=(
                    "Use read_file to inspect fact.txt. Reply with only the exact "
                    "verification code from that file."
                ),
                expected_tool="read_file",
                expected_text="CODE-91D2E7",
                files=(("fact.txt", "verification-code: CODE-91D2E7\n"),),
            ),
            EvaluationCase(
                name="search",
                prompt=(
                    "Use search_text to find FIND-4A8C6E in the workspace. Reply with "
                    "only the exact relative path containing it."
                ),
                expected_tool="search_text",
                expected_text="nested/search-result.md",
                files=(("nested/search-result.md", "needle: FIND-4A8C6E\n"),),
            ),
        ),
    )


def load_evaluation_dataset(path: str | Path) -> EvaluationDataset:
    """Load the documented JSON dataset format without adding a YAML dependency."""

    source = Path(path)
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid evaluation dataset JSON: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("schema") != DATASET_SCHEMA:
        raise ValueError(f"Dataset schema must be {DATASET_SCHEMA!r}")
    name = raw.get("name")
    values = raw.get("cases")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Dataset name cannot be empty")
    if not isinstance(values, list) or not values:
        raise ValueError("Dataset cases must be a non-empty array")

    cases: list[EvaluationCase] = []
    for index, value in enumerate(values, start=1):
        if not isinstance(value, dict):
            raise ValueError(f"Dataset case {index} must be an object")
        files = value.get("files", {})
        forbidden = value.get("forbidden_tools", [])
        if not isinstance(files, dict) or not all(
            isinstance(key, str) and isinstance(item, str)
            for key, item in files.items()
        ):
            raise ValueError(f"Dataset case {index} files must map strings to strings")
        if not isinstance(forbidden, list) or not all(
            isinstance(item, str) for item in forbidden
        ):
            raise ValueError(f"Dataset case {index} forbidden_tools must be strings")
        required = ("name", "prompt", "expected_text")
        if any(
            not isinstance(value.get(key), str) or not value[key].strip()
            for key in required
        ):
            raise ValueError(f"Dataset case {index} has an empty required field")
        expected_status = value.get("expected_status", "ok")
        if expected_status not in {"ok", "error", "denied", "preview"}:
            raise ValueError(f"Dataset case {index} has an invalid expected_status")
        expected_tool = value.get("expected_tool")
        if expected_tool is not None and (
            not isinstance(expected_tool, str) or not expected_tool.strip()
        ):
            raise ValueError(
                f"Dataset case {index} expected_tool must be a string or null"
            )
        cases.append(
            EvaluationCase(
                name=value["name"].strip(),
                prompt=value["prompt"].strip(),
                expected_tool=(
                    None if expected_tool is None else expected_tool.strip()
                ),
                expected_text=value["expected_text"],
                expected_status=expected_status,
                forbidden_tools=tuple(forbidden),
                files=tuple((key, item) for key, item in files.items()),
            )
        )
    names = [case.name for case in cases]
    if len(names) != len(set(names)):
        raise ValueError("Dataset case names must be unique")
    return EvaluationDataset(name=name.strip(), cases=tuple(cases))


def _materialize(dataset: EvaluationDataset, workspace: Path) -> None:
    guard = WorkspaceGuard(workspace)
    for case in dataset.cases:
        for relative_path, content in case.files:
            target = guard.resolve(relative_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")


def case_passes(
    case: EvaluationCase,
    response: str,
    events: list[dict[str, Any]],
) -> bool:
    """Require expected evidence and reject any successful forbidden tool call."""

    expected_event = case.expected_tool is None or any(
        event.get("tool_name") == case.expected_tool
        and event.get("status") == case.expected_status
        for event in events
    )
    forbidden_called = any(
        event.get("tool_name") in case.forbidden_tools for event in events
    )
    return (
        expected_event
        and case.expected_text.casefold() in response.casefold()
        and not forbidden_called
    )


def evaluate_model(
    settings: Settings,
    model: str,
    dataset_path: str | Path | None = None,
) -> EvaluationReport:
    """Run an isolated read-only dataset against one Ollama model."""

    if not model.strip():
        raise ValueError("Evaluation model cannot be empty")
    dataset = (
        builtin_dataset()
        if dataset_path is None
        else load_evaluation_dataset(dataset_path)
    )

    with tempfile.TemporaryDirectory(prefix="mla-eval-") as temporary:
        root = Path(temporary)
        workspace = root / "workspace"
        _materialize(dataset, workspace)
        evaluation_settings = replace(
            settings,
            model=model.strip(),
            workspace=workspace,
            database=root / "state.db",
            write_policy="deny",
            tool_policies=(),
            mcp_servers=(),
            temperature=0.0,
            request_limit=EVALUATION_REQUEST_LIMIT,
            tool_calls_limit=EVALUATION_TOOL_CALLS_LIMIT,
            max_output_tokens=EVALUATION_MAX_OUTPUT_TOKENS,
        )
        runtime = AgentRuntime(evaluation_settings)
        results: list[EvaluationResult] = []

        for case in dataset.cases:
            session_id = runtime.store.new_session()
            started = time.perf_counter()
            response: str | None = None
            usage: dict[str, Any] | None = None
            error: str | None = None
            passed = False
            try:
                outcome = runtime.run(case.prompt, session_id=session_id)
                response = outcome.response
                usage = outcome.usage
                events = runtime.store.get_tool_events(session_id)
                passed = case_passes(case, response, events)
            except Exception as exc:  # Keep comparisons running after one failure.
                error = str(exc)
                events = runtime.store.get_tool_events(session_id)

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

    return EvaluationReport(
        model=model.strip(), dataset=dataset.name, results=tuple(results)
    )


__all__ = [
    "DATASET_SCHEMA",
    "EvaluationCase",
    "EvaluationDataset",
    "EvaluationReport",
    "EvaluationResult",
    "case_passes",
    "evaluate_model",
    "load_evaluation_dataset",
]
