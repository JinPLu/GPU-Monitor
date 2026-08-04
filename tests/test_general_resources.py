from __future__ import annotations

from datetime import UTC, datetime, timedelta

from gpu_broker.schemas import (
    RequestCreate,
    ResourceClaim,
    ResourcePlanEvaluationInput,
    ResourceRunActualInput,
)
from tests.helpers import observation


def host_claim(*, cpu_cores: float = 4.0, memory_mib: int = 8192) -> ResourceClaim:
    return ResourceClaim.model_validate(
        {
            "project_id": "project-a",
            "task_ref": "cpu-only-task",
            "purpose": "coordinate host CPU and memory for an agent task",
            "provider_type": "host-capacity",
            "quantities": {"cpu_cores": cpu_cores, "memory_mib": memory_mib},
        }
    )


def test_host_capacity_claim_allocates_without_gpu_and_releases(service, admin) -> None:
    service.ingest_observation(observation(count=0))

    result = service.create_resource_claim(
        admin,
        host_claim(cpu_cores=8, memory_mib=16_384),
        idempotency_key="host-claim-one",
    )

    assert result["claim"]["state"] == "active"
    assert result["allocation"]["native_lease_id"] is None
    assert result["allocation"]["quantities"]["gpu_count"] == 0
    assert result["allocation"]["quantities"]["cpu_cores"] == 8.0

    board = service.list_resources(admin)["data"]
    endpoint_a = next(
        card for card in board["host_capacity"] if card["endpoint"]["id"] == "endpoint-a"
    )
    assert endpoint_a["capacity"]["available_cpu_cores"] == 52.0
    assert endpoint_a["capacity"]["available_memory_mib"] == 180_224
    assert board["summary"]["active_resource_claims"] == 1

    released = service.release_resource_claim(
        admin,
        result["claim"]["id"],
        reason="test complete",
        idempotency_key="host-claim-one-release",
    )

    assert released["claim"]["state"] == "released"
    assert released["allocations"][0]["state"] == "released"
    board_after_release = service.list_resources(admin)["data"]
    endpoint_after_release = next(
        card for card in board_after_release["host_capacity"] if card["endpoint"]["id"] == "endpoint-a"
    )
    assert endpoint_after_release["capacity"]["available_cpu_cores"] == 60.0


def test_host_capacity_claim_fails_closed_on_stale_host_telemetry(service, admin) -> None:
    service.ingest_observation(
        observation(count=0, observed_at=datetime.now(UTC) - timedelta(hours=1))
    )

    result = service.create_resource_claim(
        admin,
        host_claim(cpu_cores=1, memory_mib=1024),
        idempotency_key="stale-host-claim",
    )

    assert result["claim"]["state"] == "blocked"
    assert result["allocation"] is None
    candidate = next(
        item for item in result["candidates"] if item["endpoint"]["id"] == "endpoint-a"
    )
    assert candidate["excluded_reason"] == "host telemetry is stale"


def test_host_capacity_accounts_existing_direct_lease_commitments(service, admin) -> None:
    service.ingest_observation(observation(count=1))
    lease_result = service.create_request(
        admin,
        RequestCreate.model_validate(
            {
                "project_id": "project-a",
                "task_ref": "gpu-with-host-commitment",
                "purpose": "reserve host capacity alongside GPU",
                "constraints": {"gpu_count": 1, "cpu_cores": 40, "memory_mib": 100_000},
            }
        ),
        idempotency_key="gpu-host-commitment",
    )
    assert lease_result["lease"] is not None

    result = service.create_resource_claim(
        admin,
        host_claim(cpu_cores=21, memory_mib=1),
        idempotency_key="over-direct-commitment",
    )

    assert result["claim"]["state"] == "blocked"
    candidate = next(
        item for item in result["candidates"] if item["endpoint"]["id"] == "endpoint-a"
    )
    assert candidate["excluded_reason"] == "insufficient_cpu"
    assert candidate["capacity"]["available_cpu_cores"] == 20.0


def test_resource_plan_evaluation_uses_marginal_threshold_and_actuals(service, admin) -> None:
    evaluation = service.evaluate_resource_plan(
        admin,
        ResourcePlanEvaluationInput.model_validate(
            {
                "project_id": "project-a",
                "task_ref": "frontier-task",
                "baseline_runtime_seconds": 2000,
                "candidates": [
                    {
                        "candidate_key": "small",
                        "provider_type": "host-capacity",
                        "quantities": {"cpu_cores": 2},
                        "predicted_runtime_seconds": 1000,
                        "predicted_saved_seconds": 1000,
                        "predicted_saved_ratio": 0.5,
                        "satisfies_marginal_threshold": True,
                    },
                    {
                        "candidate_key": "medium",
                        "provider_type": "host-capacity",
                        "quantities": {"cpu_cores": 4},
                        "predicted_runtime_seconds": 881,
                        "predicted_saved_seconds": 1119,
                        "predicted_saved_ratio": 0.5595,
                        "satisfies_marginal_threshold": False,
                    },
                    {
                        "candidate_key": "large",
                        "provider_type": "host-capacity",
                        "quantities": {"cpu_cores": 8},
                        "predicted_runtime_seconds": 600,
                        "predicted_saved_seconds": 1400,
                        "predicted_saved_ratio": 0.7,
                        "satisfies_marginal_threshold": True,
                    },
                ],
            }
        ),
        idempotency_key="evaluate-frontier",
    )

    assert evaluation["evaluation"]["selected_candidate_key"] == "small"
    assert [decision["candidate_key"] for decision in evaluation["decisions"]] == [
        "small",
        "medium",
    ]
    assert evaluation["decisions"][-1]["reason"] == "marginal-benefit-below-threshold"

    actual = service.record_resource_run_actual(
        admin,
        ResourceRunActualInput.model_validate(
            {
                "project_id": "project-a",
                "task_ref": "frontier-task",
                "quantities": {"cpu_cores": 2},
                "started_at": datetime.now(UTC) - timedelta(seconds=180),
                "completed_at": datetime.now(UTC),
                "actual_duration_seconds": 180,
                "outcome": "succeeded",
            }
        ),
        idempotency_key="actual-frontier",
        evaluation_id=evaluation["evaluation"]["id"],
    )

    assert actual["actual"]["actual_duration_seconds"] == 180
    board = service.list_resources(admin)["data"]
    assert board["plan_evaluations"][0]["selected_candidate_key"] == "small"
