import json
from pathlib import Path

from minimal_local_agent.evals import (
    DATASET_SCHEMA,
    EvaluationCase,
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
