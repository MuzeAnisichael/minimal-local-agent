from types import SimpleNamespace

import pytest

from minimal_local_agent.execution_budget import ExecutionBudget


def usage(requests=0, tool_calls=0, output_tokens=0):
    return SimpleNamespace(
        requests=requests, tool_calls=tool_calls, output_tokens=output_tokens
    )


def test_planning_and_execution_share_request_and_output_limits():
    budget = ExecutionBudget(2, 3, 100)
    budget.planning({"requests": 1, "output_tokens": 20}, 30)
    assert budget.before_model(usage()) == 80
    with pytest.raises(ValueError, match="request budget"):
        budget.before_model(usage(requests=1))


def test_unreported_planning_tokens_reserve_the_allocation():
    budget = ExecutionBudget(3, 3, 100)
    budget.planning({"requests": 1, "output_tokens": 0}, 30)
    assert budget.before_model(usage()) == 70


@pytest.mark.parametrize("extension", ["planning", "peer"])
@pytest.mark.parametrize("reported", [{}, {"requests": None, "output_tokens": None}])
def test_missing_or_null_extension_usage_is_unknown(extension, reported):
    budget = ExecutionBudget(5, 5, 100)
    if extension == "planning":
        budget.planning(reported, 30)
    else:
        allocation = budget.reserve_peer(usage(requests=1))
        budget.peer_completed(reported, allocation)
    result = budget.summary({"requests": 1, "output_tokens": 10})
    assert result["requests"] is None
    assert result["output_tokens"] is None
    assert result["reserved_extension_requests"] == 1
    if extension == "peer":
        assert result["peer_usage_complete"] is False


def test_unknown_peer_usage_is_not_reported_as_zero_cost():
    budget = ExecutionBudget(4, 3, 100)
    allocated = budget.reserve_peer(usage(requests=1))
    budget.peer_completed({}, allocated)
    result = budget.summary(
        {"requests": 2, "tool_calls": 1, "input_tokens": 10, "output_tokens": 12}
    )
    assert result["requests"] is None
    assert result["output_tokens"] is None
    assert result["peer_usage_complete"] is False
    assert result["reserved_extension_requests"] == 1


def test_a_failed_peer_reservation_remains_unknown():
    budget = ExecutionBudget(4, 3, 100)
    budget.reserve_peer(usage(requests=1))
    assert budget.summary({"requests": 1})["peer_usage_complete"] is False


def test_peer_allocation_keeps_request_budget_for_consuming_result():
    with pytest.raises(ValueError, match="delegation"):
        ExecutionBudget(2, 3, 100).reserve_peer(usage(requests=1))


@pytest.mark.parametrize(
    "reported",
    [
        {"requests": 2},
        {"requests": True},
        {"requests": -1},
        {"requests": 1.0},
        {"output_tokens": 101},
        {"tool_calls": 2},
    ],
)
def test_peer_usage_cannot_inflate_its_allocation(reported):
    budget = ExecutionBudget(5, 5, 100)
    allocation = budget.reserve_peer(usage())
    with pytest.raises(ValueError):
        budget.peer_completed(reported, allocation)


def test_simultaneous_tools_cannot_claim_the_same_remaining_slot():
    budget = ExecutionBudget(5, 1, 100)
    budget.tool_started(usage())
    with pytest.raises(ValueError, match="tool-call"):
        budget.tool_started(usage())
    budget.tool_finished()
    with pytest.raises(ValueError, match="tool-call"):
        budget.tool_started(usage(tool_calls=1))


def test_recorded_peer_usage_releases_only_measured_allocation():
    budget = ExecutionBudget(5, 5, 100)
    allocation = budget.reserve_peer(usage(requests=1))
    budget.peer_completed(
        {"requests": 1, "tool_calls": 0, "input_tokens": 8, "output_tokens": 10},
        allocation,
    )
    assert budget.extra_tools == 0
    assert budget.extra_output == 10
    summary = budget.summary(
        {"requests": 2, "tool_calls": 1, "input_tokens": 20, "output_tokens": 12}
    )
    assert summary["requests"] == 3
    assert summary["output_tokens"] == 22


def test_reported_model_overflow_is_rejected_even_if_provider_ignored_max_tokens():
    budget = ExecutionBudget(3, 3, 100)
    budget.planning({"requests": 1, "output_tokens": 30}, 30)
    assert budget.before_model(usage()) == 70
    with pytest.raises(ValueError, match="Shared output token budget"):
        budget.after_model(usage(), usage(output_tokens=80))
    with pytest.raises(ValueError, match="Shared output token budget"):
        budget.summary({"requests": 1, "output_tokens": 80})
