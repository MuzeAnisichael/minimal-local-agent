from pathlib import Path

import pytest
from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.models.test import TestModel

from minimal_local_agent.config import Settings
from minimal_local_agent.planning import ModelPlanner, Plan, PlanStep


class _Factory:
    def __init__(self, output: dict) -> None:
        self.model = TestModel(call_tools=[], custom_output_args=output)

    def create(self, _settings: Settings) -> TestModel:
        return self.model


def _settings(tmp_path: Path) -> Settings:
    return Settings(workspace=tmp_path / "workspace", database=tmp_path / "state.db")


def _valid_output() -> dict:
    return {
        "steps": [
            {
                "title": "Read notes",
                "completion_condition": "Notes returned by read_file",
            },
            {
                "title": "Summarize",
                "completion_condition": "Summary references the notes",
            },
        ]
    }


def test_model_planner_returns_advice_and_actual_usage(tmp_path: Path) -> None:
    planner = ModelPlanner(_settings(tmp_path), model_factory=_Factory(_valid_output()))
    plan = planner.plan("Summarize the project", context=("Approved: Python project",))
    assert plan.id.startswith("plan-")
    assert plan.steps[0] == PlanStep("Read notes", "Notes returned by read_file")
    assert plan.usage["requests"] == 1
    assert plan.usage["input_tokens"] > 0
    assert plan.usage["output_tokens"] > 0
    assert len(plan.to_dict()["steps"]) == 2
    assert "status" not in plan.to_dict()["steps"][0]
    assert not (tmp_path / "state.db").exists()


def test_planning_always_uses_one_request_without_tools(tmp_path: Path) -> None:
    factory = _Factory(_valid_output())
    planner = ModelPlanner(_settings(tmp_path), model_factory=factory)
    result = planner.plan("Task", request_limit=9)
    assert result.usage["requests"] == 1
    assert result.usage["tool_calls"] == 0
    assert factory.model.last_model_request_parameters.function_tools == []


@pytest.mark.parametrize(
    "output",
    [
        {"steps": []},
        {"steps": [{"title": " ", "completion_condition": "checked"}]},
        {"steps": [{"title": "step", "completion_condition": ""}]},
        {"steps": [{"title": "step", "completion_condition": "checked", "done": True}]},
        {"steps": [{"title": "step", "completion_condition": "checked"}] * 9},
    ],
)
def test_invalid_plan_fails_without_retry(tmp_path: Path, output: dict) -> None:
    planner = ModelPlanner(_settings(tmp_path), model_factory=_Factory(output))
    with pytest.raises(UnexpectedModelBehavior):
        planner.plan("Task")


@pytest.mark.parametrize("name", ["request_limit", "output_tokens_limit"])
@pytest.mark.parametrize("value", [0, -1, True, "1"])
def test_planning_budget_parameters(tmp_path: Path, name: str, value: object) -> None:
    planner = ModelPlanner(_settings(tmp_path), model_factory=_Factory(_valid_output()))
    with pytest.raises(ValueError, match=name):
        planner.plan("Task", **{name: value})


def test_output_budget_is_enforced(tmp_path: Path) -> None:
    planner = ModelPlanner(_settings(tmp_path), model_factory=_Factory(_valid_output()))
    with pytest.raises(UsageLimitExceeded, match="output_tokens"):
        planner.plan("Task", output_tokens_limit=1)


@pytest.mark.parametrize(
    "arguments",
    [
        {"prompt": " "},
        {"prompt": "X" * 64_001},
        {"context": ["not a tuple"]},
        {"context": ("x",) * 33},
        {"context": ("x" * 8_001,)},
        {"context": (False,)},
    ],
)
def test_planning_input_validation(tmp_path: Path, arguments: dict) -> None:
    planner = ModelPlanner(_settings(tmp_path), model_factory=_Factory(_valid_output()))
    with pytest.raises(ValueError):
        planner.plan(**{"prompt": "Task", **arguments})


def test_planning_input_obeys_shared_context_size(tmp_path: Path) -> None:
    settings = Settings(
        workspace=tmp_path / "workspace",
        database=tmp_path / "state.db",
        max_context_bytes=100,
    )
    planner = ModelPlanner(settings, model_factory=_Factory(_valid_output()))
    with pytest.raises(ValueError, match="max_context_bytes"):
        planner.plan("任务" * 100)


def test_plan_objects_validate_and_serialize() -> None:
    step = PlanStep("Inspect", "Evidence returned")
    plan = Plan("plan-test", (step,), {"requests": 0})
    assert plan.to_dict()["steps"] == [step.to_dict()]
    for title, condition in (("", "done"), ("step", " "), ("x" * 241, "done")):
        with pytest.raises(ValueError):
            PlanStep(title, condition)
    for steps in ((), (step,) * 9, ("not a step",)):
        with pytest.raises(ValueError):
            Plan("plan-test", steps)
    with pytest.raises(ValueError):
        Plan("plan-test", (step,), {"requests": True})
