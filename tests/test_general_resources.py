from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, select

from serverpilot.models import ResourceAllocation, ResourceRunActual as ResourceRunActualModel
from serverpilot.service import ActorContext, BrokerError
from serverpilot.timeutil import json_dump
from serverpilot.schemas import (
    EndpointUpsert,
    RequestCreate,
    ResourceClaim,
    ResourcePlanEvaluationInput,
    ResourceRunActualInput,
)
from tests.helpers import observation


def host_claim(
    *,
    cpu_cores: float = 4.0,
    memory_mib: int = 8192,
    project_id: str = "project-a",
) -> ResourceClaim:
    return ResourceClaim.model_validate(
        {
            "project_id": project_id,
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

    snapshot = service.snapshot(admin)["data"]
    snapshot_claim = next(
        claim for claim in snapshot["resource_claims"] if claim["id"] == result["claim"]["id"]
    )
    assert snapshot_claim["state"] == "active"
    assert snapshot_claim["runtime_state"] == "ASSIGNED"
    assert snapshot_claim["quantities"]["cpu_cores"] == 8.0
    assert snapshot_claim["allocations"][0]["claim_id"] == result["claim"]["id"]
    assert snapshot["resource_providers"][0]["total"]["cpu_cores"] == 64.0
    assert snapshot["resource_providers"][0]["committed"]["cpu_cores"] == 8.0
    assert snapshot["allocatable_units"][0]["quantities"]["memory_mib"] == 262_144

    current = service.control_plane_state(admin)["data"]["current"]
    monitor = service.resource_monitor(admin)["data"]
    assert current["host_capacity"] == monitor["host_capacity"]
    projection = current["resource_projection"]
    assert projection["capacity"]["cpu_cores"] == 64.0
    assert projection["used"]["cpu_cores"] == 4.0
    assert projection["claimed"]["cpu_cores"] == 8.0
    assert projection["available"]["cpu_cores"] == 52.0
    assert projection["semantics"]["available_is_authoritative"] is True
    assert projection["semantics"]["used_and_claimed_may_overlap"] is True
    assert "BUSY_UNMANAGED" in projection["semantics"]["fail_closed_states"]
    assert [
        allocation
        for claim in current["resource_claims"]
        for allocation in claim["allocations"]
    ] == monitor["allocations"]

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
    assert result["claim"]["id"] not in {
        claim["id"] for claim in service.snapshot(admin)["data"]["resource_claims"]
    }


def test_restricted_state_capacity_accounts_other_projects_without_exposing_claims(
    service,
    admin,
) -> None:
    service.ingest_observation(observation(count=0))
    project_a = service.create_resource_claim(
        admin,
        host_claim(cpu_cores=5, memory_mib=5_000, project_id="project-a"),
        idempotency_key="restricted-capacity-project-a",
    )
    service.create_resource_claim(
        admin,
        host_claim(cpu_cores=11, memory_mib=11_000, project_id="project-b"),
        idempotency_key="restricted-capacity-project-b",
    )
    restricted = ActorContext(
        id="project-a-agent",
        role="agent",
        project_ids=frozenset({"project-a"}),
    )

    current = service.control_plane_state(restricted)["data"]["current"]
    restricted_card = next(
        card for card in current["host_capacity"] if card["endpoint"]["id"] == "endpoint-a"
    )
    admin_card = next(
        card
        for card in service.resource_monitor(admin)["data"]["host_capacity"]
        if card["endpoint"]["id"] == "endpoint-a"
    )

    assert restricted_card["capacity"] == admin_card["capacity"]
    assert restricted_card["capacity"]["generic_claim_cpu_cores"] == 16.0
    assert {claim["id"] for claim in current["resource_claims"]} == {
        project_a["claim"]["id"]
    }


def test_host_capacity_claim_fails_closed_on_stale_host_telemetry(service, admin) -> None:
    service.ingest_observation(
        observation(count=0, observed_at=datetime.now(UTC) - timedelta(hours=1))
    )

    with pytest.raises(BrokerError) as error:
        service.create_resource_claim(
            admin,
            host_claim(cpu_cores=1, memory_mib=1024),
            idempotency_key="stale-host-claim",
        )
    assert error.value.code == "no_capacity"
    assert service.list_resource_claims(admin)["data"] == []


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

    with pytest.raises(BrokerError) as error:
        service.create_resource_claim(
            admin,
            host_claim(cpu_cores=21, memory_mib=1),
            idempotency_key="over-direct-commitment",
        )
    assert error.value.code == "no_capacity"
    monitor = next(
        item
        for item in service.resource_monitor(admin)["data"]["host_capacity"]
        if item["endpoint"]["id"] == "endpoint-a"
    )
    assert monitor["capacity"]["available_cpu_cores"] == 20.0


def test_snapshot_correlates_generic_claim_with_native_lease_and_request(service, admin) -> None:
    service.ingest_observation(observation(count=1))
    lease_result = service.create_request(
        admin,
        RequestCreate.model_validate(
            {
                "project_id": "project-a",
                "task_ref": "linked-task",
                "purpose": "exercise snapshot record correlation",
                "constraints": {"gpu_count": 1},
            }
        ),
        idempotency_key="linked-native-lease",
    )
    claim_result = service.create_resource_claim(
        admin,
        host_claim(cpu_cores=2, memory_mib=2048),
        idempotency_key="linked-generic-claim",
    )
    lease_id = lease_result["lease"]["id"]
    request_id = lease_result["request"]["id"]
    with service.database.session() as session:
        allocation = session.scalar(
            select(ResourceAllocation).where(
                ResourceAllocation.claim_id == claim_result["claim"]["id"]
            )
        )
        assert allocation is not None
        allocation.native_lease_id = lease_id
        session.commit()

    snapshot = service.snapshot(admin)["data"]
    claim = next(
        item
        for item in snapshot["resource_claims"]
        if item["id"] == claim_result["claim"]["id"]
    )

    assert claim["native_lease_ids"] == [lease_id]
    assert claim["native_request_ids"] == [request_id]
    assert claim["runtime_state"] == "ASSIGNED"
    assert snapshot["leases"][0]["runtime_state"] == "ASSIGNED"


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
    snapshot = service.snapshot(admin)["data"]
    assert snapshot["resource_plan_evaluations"][0]["id"] == evaluation["evaluation"]["id"]
    assert snapshot["resource_run_actuals"][0]["id"] == actual["actual"]["id"]


def test_snapshot_bounds_resource_history_and_uses_plain_revision(service, admin) -> None:
    now = datetime.now(UTC)
    with service.database.session() as session:
        for index in range(RESOURCE_HISTORY_ROWS := 55):
            session.add(
                ResourceRunActualModel(
                    evaluation_id=None,
                    claim_id=None,
                    actor_id=admin.id,
                    project_id="project-a",
                    task_ref=f"history-{index}",
                    started_at=now - timedelta(seconds=index + 1),
                    completed_at=now,
                    actual_duration_seconds=index + 1,
                    quantities_json=json_dump({"cpu_cores": 1}),
                    outcome="succeeded",
                    notes_json="{}",
                    created_at=now - timedelta(seconds=index),
                )
            )
        session.commit()

    first = service.snapshot(admin)["data"]
    service.ingest_observation(observation(endpoint_id="endpoint-a", count=0))
    second = service.snapshot(admin)["data"]
    service.create_resource_claim(
        admin,
        host_claim(cpu_cores=1, memory_mib=1024),
        idempotency_key="resource-revision-change",
    )
    third = service.snapshot(admin)["data"]

    assert RESOURCE_HISTORY_ROWS > 50
    assert len(first["resource_run_actuals"]) == 50
    assert first["resource_usage_revision"] != second["resource_usage_revision"]
    assert second["resource_usage_revision"] != third["resource_usage_revision"]


def test_snapshot_keeps_all_candidates_for_each_bounded_evaluation(service, admin) -> None:
    candidate_count = 256
    evaluation_ids: list[str] = []
    for evaluation_index in range(3):
        evaluation = service.evaluate_resource_plan(
            admin,
            ResourcePlanEvaluationInput.model_validate(
                {
                    "project_id": "project-a",
                    "task_ref": f"fanout-{evaluation_index}",
                    "baseline_runtime_seconds": 1000,
                    "marginal_min_saved_seconds": 0,
                    "marginal_min_saved_ratio": 0,
                    "candidates": [
                        {
                            "candidate_key": f"candidate-{candidate_index}",
                            "quantities": {"cpu_cores": candidate_index + 1},
                            "predicted_runtime_seconds": 1000 - candidate_index,
                            "predicted_saved_seconds": candidate_index,
                            "predicted_saved_ratio": candidate_index / 1000,
                            "satisfies_marginal_threshold": True,
                            "selected": candidate_index == candidate_count - 1,
                        }
                        for candidate_index in range(candidate_count)
                    ],
                    "selected_candidate_key": f"candidate-{candidate_count - 1}",
                }
            ),
            idempotency_key=f"fanout-evaluation-{evaluation_index}",
        )
        evaluation_ids.append(evaluation["evaluation"]["id"])

    snapshot = service.snapshot(admin)["data"]
    evaluations = {
        evaluation["id"]: evaluation for evaluation in snapshot["resource_plan_evaluations"]
    }

    assert sum(len(evaluations[item]["candidates"]) for item in evaluation_ids) > 600
    for evaluation_id in evaluation_ids:
        evaluation = evaluations[evaluation_id]
        assert len(evaluation["candidates"]) == candidate_count
        assert any(
            candidate["candidate_key"] == evaluation["selected_candidate_key"]
            for candidate in evaluation["candidates"]
        )


def test_snapshot_resource_projection_uses_bounded_query_count(service, admin) -> None:
    def snapshot_select_count() -> int:
        statements: list[str] = []

        def record_statement(
            _connection, _cursor, statement, _parameters, _context, _many
        ) -> None:
            if statement.lstrip().upper().startswith("SELECT"):
                statements.append(statement)

        event.listen(service.database.engine, "before_cursor_execute", record_statement)
        try:
            service.snapshot(admin)
        finally:
            event.remove(service.database.engine, "before_cursor_execute", record_statement)
        return len(statements)

    baseline_count = snapshot_select_count()
    for index in range(10):
        service.upsert_endpoint(
            admin,
            EndpointUpsert(
                id=f"scale-endpoint-{index}",
                host=f"192.0.2.{index + 1}",
                port=22,
                ssh_user="gpu",
                workspace_path=f"/srv/scale-{index}",
            ),
            idempotency_key=f"scale-endpoint-{index}",
        )
    scaled_count = snapshot_select_count()

    assert baseline_count <= 24
    assert scaled_count == baseline_count
