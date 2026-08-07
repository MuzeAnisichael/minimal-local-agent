from minimal_local_agent.evals import EvaluationCase, case_passes


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
