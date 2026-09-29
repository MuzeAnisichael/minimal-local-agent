import json
from pathlib import Path

import pytest

from minimal_local_agent.evals import (
    DATASET_SCHEMA,
    EvaluationCase,
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
