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
    family: str = "general"
    expected_status: str = "ok"
    forbidden_tools: tuple[str, ...] = ()
    files: tuple[tuple[str, str], ...] = ()
    expected_files: tuple[tuple[str, str], ...] = ()
    absent_files: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EvaluationDataset:
    name: str
    cases: tuple[EvaluationCase, ...]


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    model: str
    case: str
    family: str
    passed: bool
    latency_ms: int
    expected_tool: str | None
    observed_tools: tuple[str, ...]
    tool_events: tuple[dict[str, Any], ...]
    response: str | None
    usage: dict[str, Any] | None
    error: str | None
    artifact_failures: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    model: str
    dataset: str
    results: tuple[EvaluationResult, ...]
    max_context_bytes: int = 64_000

    @property
    def passed(self) -> int:
        return sum(result.passed for result in self.results)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def summary(self) -> dict[str, Any]:
        return _summarize(self.results)

    @property
    def families(self) -> dict[str, dict[str, Any]]:
        names = dict.fromkeys(result.family for result in self.results)
        return {
            name: _summarize(tuple(r for r in self.results if r.family == name))
            for name in names
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "dataset": self.dataset,
            "passed": self.passed,
            "total": self.total,
            "summary": self.summary,
            "families": self.families,
            "parameters": {
                "temperature": 0.0,
                "request_limit": EVALUATION_REQUEST_LIMIT,
                "tool_calls_limit": EVALUATION_TOOL_CALLS_LIMIT,
                "max_output_tokens": EVALUATION_MAX_OUTPUT_TOKENS,
                "max_context_bytes": self.max_context_bytes,
                "write_policy": "deny",
            },
            "results": [asdict(result) for result in self.results],
        }


def _total_tokens(usage: dict[str, Any] | None) -> int | None:
    if usage is None:
        return None
    total = usage.get("total_tokens")
    if isinstance(total, int) and total >= 0:
        return total
    inputs = usage.get("input_tokens")
    outputs = usage.get("output_tokens")
    if all(isinstance(value, int) and value >= 0 for value in (inputs, outputs)):
        return inputs + outputs
    return None


def _summarize(results: tuple[EvaluationResult, ...]) -> dict[str, Any]:
    passed = sum(result.passed for result in results)
    total = len(results)
    latency = sum(result.latency_ms for result in results)
    token_values = [_total_tokens(result.usage) for result in results]
    unmeasured = sum(value is None for value in token_values)
    tokens = sum(value for value in token_values if value is not None)
    return {
        "passed": passed,
        "total": total,
        "success_rate": round(passed / total, 3) if total else 0.0,
        "latency_ms_per_success": round(latency / passed) if passed else None,
        "tokens_per_success": (
            round(tokens / passed, 1) if passed and not unmeasured else None
        ),
        "unmeasured_token_cases": unmeasured,
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
                family="workspace-read",
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
                family="workspace-read",
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
                family="workspace-read",
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
        expected_files = value.get("expected_files", {})
        absent_files = value.get("absent_files", [])
        forbidden = value.get("forbidden_tools", [])
        if not isinstance(files, dict) or not all(
            isinstance(key, str) and isinstance(item, str)
            for key, item in files.items()
        ):
            raise ValueError(f"Dataset case {index} files must map strings to strings")
        if not isinstance(expected_files, dict) or not all(
            isinstance(key, str) and key.strip() and isinstance(item, str)
            for key, item in expected_files.items()
        ):
            raise ValueError(
                f"Dataset case {index} expected_files must map paths to strings"
            )
        if not isinstance(absent_files, list) or not all(
            isinstance(item, str) and item.strip() for item in absent_files
        ):
            raise ValueError(f"Dataset case {index} absent_files must be paths")
        if len(absent_files) != len(set(absent_files)) or set(expected_files) & set(
            absent_files
        ):
            raise ValueError(f"Dataset case {index} has conflicting artifact paths")
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
        family = value.get("family", "general")
        if not isinstance(family, str) or not family.strip():
            raise ValueError(f"Dataset case {index} family cannot be empty")
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
                family=family.strip(),
                expected_status=expected_status,
                forbidden_tools=tuple(forbidden),
                files=tuple((key, item) for key, item in files.items()),
                expected_files=tuple(expected_files.items()),
                absent_files=tuple(absent_files),
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
            target.write_bytes(content.encode("utf-8"))


def case_passes(
    case: EvaluationCase,
    response: str,
    events: list[dict[str, Any]],
    workspace: Path | None = None,
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
        and not artifact_failures(case, workspace)
    )


def artifact_failures(case: EvaluationCase, workspace: Path | None) -> tuple[str, ...]:
    """Check exact file bytes and absence without trusting the model's report."""

    if not case.expected_files and not case.absent_files:
        return ()
    if workspace is None:
        raise ValueError("Artifact checks require an evaluation workspace")
    guard = WorkspaceGuard(workspace)
    failures: list[str] = []
    for path, expected in case.expected_files:
        try:
            target = guard.resolve(path)
            expected_bytes = expected.encode("utf-8")
            if (
                not target.is_file()
                or target.stat().st_size != len(expected_bytes)
                or target.read_bytes() != expected_bytes
            ):
                failures.append(f"content mismatch: {path}")
        except (OSError, ValueError):
            failures.append(f"unreadable: {path}")
    for path in case.absent_files:
        try:
            if guard.resolve(path).exists():
                failures.append(f"unexpected file: {path}")
        except (OSError, ValueError):
            failures.append(f"unsafe path: {path}")
    return tuple(failures)


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
            failed_artifacts: tuple[str, ...] = ()
            try:
                outcome = runtime.run(case.prompt, session_id=session_id)
                response = outcome.response
                usage = outcome.usage
                events = runtime.store.get_tool_events(session_id)
                passed = case_passes(case, response, events, workspace)
                if not passed:
                    failed_artifacts = artifact_failures(case, workspace)
            except Exception as exc:  # Keep comparisons running after one failure.
                error = str(exc)
                events = runtime.store.get_tool_events(session_id)
                failed_artifacts = artifact_failures(case, workspace)

            results.append(
                EvaluationResult(
                    model=model.strip(),
                    case=case.name,
                    family=case.family,
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
                    artifact_failures=failed_artifacts,
                )
            )

    return EvaluationReport(
        model=model.strip(),
        dataset=dataset.name,
        results=tuple(results),
        max_context_bytes=evaluation_settings.max_context_bytes,
    )


__all__ = [
    "DATASET_SCHEMA",
    "EvaluationCase",
    "EvaluationDataset",
    "EvaluationReport",
    "EvaluationResult",
    "case_passes",
    "artifact_failures",
    "evaluate_model",
    "load_evaluation_dataset",
]
