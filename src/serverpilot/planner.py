"""Deterministic marginal-utility selection for agent resource contracts.

The planner deliberately evaluates a supplied, bounded frontier.  It does not
invent forecasts, reach into a provider, or decide whether a workload may run;
those remain the service's capacity, ownership, and approval checks.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Sequence


ProviderKind = Literal["direct-gpu", "host-capacity", "scheduler"]

MIN_SAVED_FRACTION = 0.10
MIN_SAVED_SECONDS = 120


@dataclass(frozen=True, slots=True)
class ResourcePlanCandidate:
    """One explicit, non-executable resource contract and its task forecast."""

    id: str
    provider_kind: ProviderKind
    predicted_remaining_seconds: int
    forecast_basis: str
    cpu_cores: float = 0
    memory_mib: int = 0
    gpu_count: int = 0
    nodes: int = 0

    def validate(self) -> None:
        if not self.id:
            raise ValueError("candidate id is required")
        if self.predicted_remaining_seconds <= 0:
            raise ValueError("predicted_remaining_seconds must be positive")
        if not self.forecast_basis:
            raise ValueError("forecast_basis is required")
        if self.cpu_cores < 0 or self.memory_mib < 0 or self.gpu_count < 0 or self.nodes < 0:
            raise ValueError("resource quantities cannot be negative")
        if not any((self.cpu_cores, self.memory_mib, self.gpu_count, self.nodes)):
            raise ValueError("candidate must request at least one resource")

    @property
    def resource_size(self) -> tuple[float, int, int, int]:
        return (self.cpu_cores, self.memory_mib, self.gpu_count, self.nodes)


@dataclass(frozen=True, slots=True)
class MarginalDecision:
    candidate_id: str
    selected: bool
    reason: str
    saved_seconds: int | None = None
    saved_fraction: float | None = None


@dataclass(frozen=True, slots=True)
class ResourcePlanSelection:
    selected: ResourcePlanCandidate
    decisions: tuple[MarginalDecision, ...]


def select_smallest_useful_plan(
    candidates: Sequence[ResourcePlanCandidate],
    *,
    min_saved_fraction: float = MIN_SAVED_FRACTION,
    min_saved_seconds: int = MIN_SAVED_SECONDS,
) -> ResourcePlanSelection:
    """Choose the first plan then expand only across useful marginal edges.

    Candidate order is the task/profile's resource frontier from smallest to
    largest.  Once an edge misses either threshold we stop: later plans cannot
    be selected by skipping an unhelpful intermediate expansion.
    """

    if not candidates:
        raise ValueError("at least one candidate is required")
    if not 0 <= min_saved_fraction <= 1:
        raise ValueError("min_saved_fraction must be between 0 and 1")
    if min_saved_seconds < 0:
        raise ValueError("min_saved_seconds cannot be negative")

    seen_ids: set[str] = set()
    previous_size: tuple[float, int, int, int] | None = None
    for candidate in candidates:
        candidate.validate()
        if candidate.id in seen_ids:
            raise ValueError("candidate ids must be unique")
        seen_ids.add(candidate.id)
        if previous_size is not None:
            if any(next_value < previous_value for next_value, previous_value in zip(candidate.resource_size, previous_size)):
                raise ValueError("candidates must be monotonically expanding")
            if candidate.resource_size == previous_size:
                raise ValueError("adjacent candidates must expand resources")
        previous_size = candidate.resource_size

    selected = candidates[0]
    decisions: list[MarginalDecision] = [
        MarginalDecision(candidate_id=selected.id, selected=True, reason="smallest-feasible")
    ]
    for candidate in candidates[1:]:
        saved_seconds = selected.predicted_remaining_seconds - candidate.predicted_remaining_seconds
        saved_fraction = saved_seconds / selected.predicted_remaining_seconds
        if saved_seconds >= min_saved_seconds and saved_fraction >= min_saved_fraction:
            selected = candidate
            decisions.append(
                MarginalDecision(
                    candidate_id=candidate.id,
                    selected=True,
                    reason="marginal-benefit-qualified",
                    saved_seconds=saved_seconds,
                    saved_fraction=saved_fraction,
                )
            )
            continue
        decisions.append(
            MarginalDecision(
                candidate_id=candidate.id,
                selected=False,
                reason="marginal-benefit-below-threshold",
                saved_seconds=saved_seconds,
                saved_fraction=saved_fraction,
            )
        )
        break
    return ResourcePlanSelection(selected=selected, decisions=tuple(decisions))
