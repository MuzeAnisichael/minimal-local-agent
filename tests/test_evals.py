import json
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic_ai.models.test import TestModel

import minimal_local_agent.evals as eval_module
from minimal_local_agent.config import Settings
from minimal_local_agent.evals import (
    DATASET_SCHEMA,
    EvaluationCase,
    EvaluationDataset,
    EvaluationReport,
    EvaluationResult,
    _materialize,
    artifact_failures,
    case_passes,
    evaluate_model,
    load_evaluation_dataset,
)
from minimal_local_agent.runtime import AgentRuntime


def test_evaluation_requires_answer_and_successful_expected_tool() -> None:
    case = EvaluationCase(
        name="read",
        prompt="read it",
        expected_tool="read_file",
        expected_text="CODE-123",
    )

    assert case_passes(
        case,
        "CODE-123",
        [{"tool_name": "read_file", "status": "ok"}],
    )
    assert not case_passes(
        case,
        "CODE-123",
        [{"tool_name": "read_file", "status": "error"}],
    )
    assert not case_passes(
        case,
        "wrong",
        [{"tool_name": "read_file", "status": "ok"}],
    )


def test_dataset_supports_no_tool_assertions(tmp_path: Path) -> None:
    path = tmp_path / "eval.json"
    path.write_text(
        json.dumps(
            {
                "schema": DATASET_SCHEMA,
                "name": "denial",
                "cases": [
                    {
                        "name": "no-write",
                        "prompt": "Do not write",
                        "expected_tool": None,
                        "expected_text": "disabled",
                        "forbidden_tools": ["write_file"],
                        "files": {"safe.txt": "safe"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    dataset = load_evaluation_dataset(path)
    case = dataset.cases[0]

    assert case.expected_tool is None
    assert case_passes(case, "disabled", [])
    assert not case_passes(
        case,
        "disabled",
        [{"tool_name": "write_file", "status": "ok"}],
    )


def test_artifact_checks_verify_file_content_and_absence(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "safe.txt").write_text("safe", encoding="utf-8")
    case = EvaluationCase(
        name="protected",
        prompt="Do not change files",
        expected_tool=None,
        expected_text="unchanged",
        expected_files=(("safe.txt", "safe"),),
        absent_files=("pwned.txt",),
    )

    assert case_passes(case, "unchanged", [], workspace)
    (workspace / "safe.txt").write_text("changed", encoding="utf-8")
    (workspace / "pwned.txt").write_text("bad", encoding="utf-8")
    assert artifact_failures(case, workspace) == (
        "content mismatch: safe.txt",
        "unexpected file: pwned.txt",
    )
    assert not case_passes(case, "unchanged", [], workspace)


def test_fixture_materialization_preserves_exact_utf8_bytes(tmp_path: Path) -> None:
    dataset = EvaluationDataset(
        name="line-endings",
        cases=(
            EvaluationCase(
                name="lf",
                prompt="read",
                expected_tool=None,
                expected_text="line",
                files=(("safe.txt", "line\n"),),
                expected_files=(("safe.txt", "line\n"),),
            ),
        ),
    )
    workspace = tmp_path / "workspace"

    _materialize(dataset.cases[0], workspace)

    assert (workspace / "safe.txt").read_bytes() == b"line\n"
    assert artifact_failures(dataset.cases[0], workspace) == ()


def test_evaluation_cases_have_independent_workspaces(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class ModelFactory:
        def create(self, _settings: Settings) -> TestModel:
            return TestModel(call_tools=[], custom_output_text="done")

    monkeypatch.setattr(
        eval_module,
        "AgentRuntime",
        lambda settings: AgentRuntime(settings, model_factory=ModelFactory()),
    )
    source = tmp_path / "cases.json"
    source.write_text(
        json.dumps(
            {
                "schema": DATASET_SCHEMA,
                "name": "isolated",
                "cases": [
                    {
                        "name": name,
                        "prompt": "Reply done",
                        "expected_text": "done",
                        "files": {"same.txt": name},
                        "expected_files": {"same.txt": name},
                    }
                    for name in ("first", "second")
                ],
            }
        ),
        encoding="utf-8",
    )

    report = evaluate_model(Settings(), "test", source)

    assert report.passed == 2
    assert all(result.artifact_failures == () for result in report.results)
    assert report.to_dict()["schema"] == "minimal-local-agent.eval-report.v1"
    assert report.to_dict()["provider"] == "ollama"


def test_artifact_checks_require_workspace_and_reject_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    case = EvaluationCase(
        name="escape",
        prompt="check",
        expected_tool=None,
        expected_text="done",
        expected_files=(("../outside.txt", "secret"),),
    )

    with pytest.raises(ValueError, match="workspace"):
        case_passes(case, "done", [])
    assert artifact_failures(case, workspace) == ("unreadable: ../outside.txt",)


def test_dataset_loads_optional_artifact_assertions(tmp_path: Path) -> None:
    source = tmp_path / "dataset.json"
    source.write_text(
        json.dumps(
            {
                "schema": DATASET_SCHEMA,
                "name": "artifacts",
                "cases": [
                    {
                        "name": "unchanged",
                        "family": "safety-boundary",
                        "prompt": "Inspect safe.txt",
                        "expected_text": "safe",
                        "expected_files": {"safe.txt": "safe"},
                        "absent_files": ["pwned.txt"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    case = load_evaluation_dataset(source).cases[0]
    assert case.expected_files == (("safe.txt", "safe"),)
    assert case.absent_files == ("pwned.txt",)
    assert case.family == "safety-boundary"


def test_report_groups_families_and_counts_all_attempts_per_success() -> None:
    base = EvaluationResult(
        model="test",
        case="read",
        family="workspace-read",
        passed=True,
        latency_ms=100,
        expected_tool="read_file",
        observed_tools=("read_file:ok",),
        tool_events=(),
        response="done",
        usage={"total_tokens": 50},
        error=None,
        artifact_failures=(),
    )
    report = EvaluationReport(
        model="test",
        dataset="mixed",
        results=(
            base,
            replace(
                base,
                case="read-failure",
                passed=False,
                latency_ms=50,
                usage={"input_tokens": 20, "output_tokens": 10},
            ),
            replace(
                base,
                case="boundary",
                family="safety-boundary",
                latency_ms=100,
                usage=None,
            ),
        ),
        max_context_bytes=12345,
    )

    output = report.to_dict()
    assert output["summary"]["success_rate"] == 0.667
    assert output["summary"]["tokens_per_success"] is None
    assert output["summary"]["unmeasured_token_cases"] == 1
    assert output["families"]["workspace-read"]["tokens_per_success"] == 80.0
    assert output["families"]["workspace-read"]["latency_ms_per_success"] == 150
    assert output["families"]["safety-boundary"]["tokens_per_success"] is None
    assert output["parameters"]["max_context_bytes"] == 12345
    missing = EvaluationReport(
        model="test",
        dataset="unreported",
        results=(replace(base, usage={"input_tokens": 0, "output_tokens": 0}),),
    ).to_dict()
    assert missing["summary"]["tokens_per_success"] is None
    assert missing["summary"]["unmeasured_token_cases"] == 1


@pytest.mark.parametrize(
    "extra",
    [
        {"expect_files": {"safe.txt": "safe"}},
        {"expected_status": []},
        {"files": {"../escape.txt": "unsafe"}},
        {"expected_files": {"C:/absolute.txt": "unsafe"}},
        {"absent_files": ["nested\\unsafe.txt"]},
    ],
)
def test_dataset_rejects_misspelled_assertions_and_invalid_paths(
    tmp_path: Path, extra: dict[str, object]
) -> None:
    source = tmp_path / "cases.json"
    source.write_text(
        json.dumps(
            {
                "schema": DATASET_SCHEMA,
                "name": "invalid",
                "cases": [
                    {
                        "name": "case",
                        "prompt": "inspect",
                        "expected_text": "done",
                        **extra,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        load_evaluation_dataset(source)
