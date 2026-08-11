from __future__ import annotations

import pytest

from serverpilot.planner import ResourcePlanCandidate, select_smallest_useful_plan


def candidate(identifier: str, seconds: int, *, cpu: float) -> ResourcePlanCandidate:
    return ResourcePlanCandidate(
        id=identifier,
        provider_kind="host-capacity",
        cpu_cores=cpu,
        predicted_remaining_seconds=seconds,
        forecast_basis="task supplied benchmark v1",
    )


@pytest.mark.parametrize(
    ("seconds", "selected"),
    [(1801, "small"), (1800, "large")],
)
def test_expansion_requires_at_least_ten_percent_savings(seconds: int, selected: str) -> None:
    result = select_smallest_useful_plan([candidate("small", 2000, cpu=1), candidate("large", seconds, cpu=2)])

    assert result.selected.id == selected


@pytest.mark.parametrize(
    ("seconds", "selected"),
    [(881, "small"), (880, "large")],
)
def test_expansion_requires_at_least_two_minutes_savings(seconds: int, selected: str) -> None:
    result = select_smallest_useful_plan([candidate("small", 1000, cpu=1), candidate("large", seconds, cpu=2)])

    assert result.selected.id == selected


def test_first_unhelpful_marginal_edge_stops_later_expansion() -> None:
    result = select_smallest_useful_plan(
        [candidate("small", 1000, cpu=1), candidate("medium", 950, cpu=2), candidate("large", 700, cpu=4)]
    )

    assert result.selected.id == "small"
    assert [decision.candidate_id for decision in result.decisions] == ["small", "medium"]
    assert result.decisions[-1].reason == "marginal-benefit-below-threshold"


def test_cpu_only_plan_is_valid() -> None:
    result = select_smallest_useful_plan([candidate("cpu-only", 240, cpu=4)])

    assert result.selected.gpu_count == 0


def test_rejects_zero_resources_and_non_monotonic_frontiers() -> None:
    with pytest.raises(ValueError, match="at least one resource"):
        select_smallest_useful_plan(
            [ResourcePlanCandidate("empty", "host-capacity", 60, "provided")]
        )
    with pytest.raises(ValueError, match="monotonically expanding"):
        select_smallest_useful_plan([candidate("larger", 1000, cpu=2), candidate("smaller", 800, cpu=1)])
