import json
from dataclasses import replace
from pathlib import Path

import pytest

from minimal_local_agent.evals import (
    DATASET_SCHEMA,
    EvaluationCase,
    EvaluationDataset,
    EvaluationReport,
    EvaluationResult,
    _materialize,
    artifact_failures,
    case_passes,
    load_evaluation_dataset,
)


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

    _materialize(dataset, workspace)

    assert (workspace / "safe.txt").read_bytes() == b"line\n"
    assert artifact_failures(dataset.cases[0], workspace) == ()


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
