from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from serverpilot.models import (
    AllocationRequest,
    Endpoint,
    Lease,
    LeaseResource,
    TelemetryCurrent,
    TelemetrySnapshot,
)
from serverpilot.schemas import EndpointUpdate, RequestCreate
from serverpilot.service import SYSTEM_ACTOR_ID, SYSTEM_PROJECT_ID, BrokerError
from serverpilot.timeutil import json_dump, utcnow
from tests.helpers import observation, process_for_gpu


def _configure_idle_policy(service, admin, *, count: int = 2) -> None:  # noqa: ANN001
    with service.database.session() as session:
        endpoint = session.get(Endpoint, "endpoint-a")
        assert endpoint is not None
        endpoint.keepalive_adapter_id = "server-script-v1"
        session.commit()
    service.ingest_observation(observation(count=count))
    service.configure_keepalive_policy(
        admin,
        "endpoint-a",
        "idle_keepalive",
        idempotency_key="policy-idle",
    )


def _begin(service, admin, index: int = 0) -> dict[str, object]:  # noqa: ANN001
    return service.begin_keepalive(
        admin,
        "endpoint-a",
        f"endpoint-a:GPU-endpoint-a-{index}",
        idempotency_key=f"begin-{index}",
    )


def _confirm(service, admin, begun: dict[str, object], index: int = 0) -> dict[str, object]:  # noqa: ANN001
    keepalive = begun["keepalive"]
    assert isinstance(keepalive, dict)
    started = utcnow()
    service.ingest_observation(
        observation(
            count=2,
            processes=[process_for_gpu(f"GPU-endpoint-a-{index}", pid=4321 + index)],
        )
    )
    return service.confirm_keepalive(
        admin,
        "endpoint-a",
        str(keepalive["lease_id"]),
        attested_pid=4321 + index,
        observation_not_before=started,
        idempotency_key=f"confirm-{index}",
    )


def _endpoint(snapshot: dict[str, object]) -> dict[str, object]:
    endpoints = snapshot["data"]
    assert isinstance(endpoints, dict)
    value = next(item for item in endpoints["endpoints"] if item["id"] == "endpoint-a")
    assert isinstance(value, dict)
    return value


def _gpus(snapshot: dict[str, object]) -> dict[str, dict[str, object]]:
    data = snapshot["data"]
    assert isinstance(data, dict)
    return {item["id"]: item for item in data["gpus"] if item["endpoint_id"] == "endpoint-a"}


def test_policy_is_persisted_and_candidates_are_independent_per_gpu(service, admin) -> None:
    _configure_idle_policy(service, admin)

    initial = service.desired_keepalive_candidates("endpoint-a")
    assert {item["gpu_id"] for item in initial["candidates"]} == {
        "endpoint-a:GPU-endpoint-a-0",
        "endpoint-a:GPU-endpoint-a-1",
    }

    begun = _begin(service, admin, 0)
    keepalive = begun["keepalive"]
    assert isinstance(keepalive, dict)
    assert keepalive["scope"] == "gpu"
    with service.database.session() as session:
        lease = session.get(Lease, keepalive["lease_id"])
        assert lease is not None and lease.keepalive_scope == "gpu"
        assert [resource.gpu_id for resource in session.scalars(
            select(LeaseResource).where(LeaseResource.lease_id == lease.id)
        )] == ["endpoint-a:GPU-endpoint-a-0"]

    after = service.desired_keepalive_candidates("endpoint-a")
    assert [item["gpu_id"] for item in after["candidates"]] == ["endpoint-a:GPU-endpoint-a-1"]
    summary = service.get_endpoint_keepalive_summary("endpoint-a")["keepalive"]
    assert summary["policy"] == "idle_keepalive"
    assert summary["starting_gpu_count"] == 1
    assert summary["eligible_idle_gpu_count"] == 1


def test_one_gpu_keepalive_does_not_block_sibling_gpu_or_hide_public_state(service, admin) -> None:
    _configure_idle_policy(service, admin)
    _confirm(service, admin, _begin(service, admin, 0), 0)

    snapshot = service.snapshot(admin)
    endpoint = _endpoint(snapshot)
    gpus = _gpus(snapshot)
    first = gpus["endpoint-a:GPU-endpoint-a-0"]
    second = gpus["endpoint-a:GPU-endpoint-a-1"]
    assert first["state"] == "KEEPALIVE"
    assert first["keepalive"]["state"] == "ACTIVE"
    assert second["state"] == "AVAILABLE"
    assert second["keepalive"]["state"] == "OFF"
    assert endpoint["keepalive"] == {
        "configured": True,
        "policy": "idle_keepalive",
        "state": "ACTIVE",
        "active_gpu_count": 1,
        "starting_gpu_count": 0,
        "error_gpu_count": 0,
        "degraded_gpu_count": 0,
        "legacy_gpu_count": 0,
        "eligible_idle_gpu_count": 1,
        "reasons": [
            {
                "gpu_id": "endpoint-a:GPU-endpoint-a-0",
                "reason": "keepalive startup grace has not completed",
            }
        ],
    }
    assert all(item["kind"] != "keepalive" for item in snapshot["data"]["leases"])
    assert [item["gpu_id"] for item in service.desired_keepalive_candidates("endpoint-a")["candidates"]] == [
        "endpoint-a:GPU-endpoint-a-1"
    ]


def test_confirm_requires_exact_attested_process_on_its_one_gpu(service, admin) -> None:
    _configure_idle_policy(service, admin)
    begun = _begin(service, admin)
    keepalive = begun["keepalive"]
    assert isinstance(keepalive, dict)
    barrier = utcnow()
    service.ingest_observation(
        observation(
            count=2,
            processes=[
                process_for_gpu("GPU-endpoint-a-0", pid=4321),
                process_for_gpu("GPU-endpoint-a-0", pid=9999),
            ],
        )
    )
    with pytest.raises(BrokerError) as foreign:
        service.confirm_keepalive(
            admin,
            "endpoint-a",
            str(keepalive["lease_id"]),
            attested_pid=4321,
            observation_not_before=barrier,
            idempotency_key="confirm-foreign",
        )
    assert foreign.value.code == "keepalive_foreign_process"


def test_keepalive_effectiveness_has_grace_then_enforces_memory_and_rolling_utilization(
    service, admin
) -> None:
    _configure_idle_policy(service, admin)
    begun = _begin(service, admin)
    _confirm(service, admin, begun)
    gpu_id = "endpoint-a:GPU-endpoint-a-0"
    snapshot = _gpus(service.snapshot(admin))
    assert snapshot[gpu_id]["keepalive"]["health"]["state"] == "STARTING"
    assert snapshot[gpu_id]["keepalive"]["health"]["last_verified_at"] is not None

    with service.database.session() as session:
        lease = session.get(Lease, begun["keepalive"]["lease_id"])
        current = session.get(TelemetryCurrent, gpu_id)
        samples = session.scalars(select(TelemetrySnapshot).where(TelemetrySnapshot.gpu_id == gpu_id)).all()
        assert lease is not None and current is not None
        lease.activated_at = utcnow() - timedelta(minutes=6)
        current.memory_used_mib = 31_000
        current.gpu_utilization_pct = 35
        for index, sample in enumerate(samples):
            sample.memory_used_mib = 31_000
            sample.gpu_utilization_pct = 35
            if index == 0:
                sample.observed_at = utcnow() - timedelta(minutes=4, seconds=55)
        session.commit()

    healthy = _gpus(service.snapshot(admin))[gpu_id]["keepalive"]["health"]
    assert healthy["state"] == "HEALTHY"
    assert healthy["memory_fraction"] == 0.31
    assert healthy["rolling_utilization_pct"] == 35.0

    with service.database.session() as session:
        current = session.get(TelemetryCurrent, gpu_id)
        assert current is not None
        current.memory_used_mib = 1_000
        session.commit()
    degraded = _gpus(service.snapshot(admin))[gpu_id]["keepalive"]["health"]
    assert degraded["state"] == "DEGRADED"
    assert degraded["reason"] == "keepalive memory fraction is below 30%"


def test_keepalive_effectiveness_rejects_a_partial_rolling_utilization_window(service, admin) -> None:
    _configure_idle_policy(service, admin)
    begun = _begin(service, admin)
    _confirm(service, admin, begun)
    gpu_id = "endpoint-a:GPU-endpoint-a-0"
    with service.database.session() as session:
        lease = session.get(Lease, begun["keepalive"]["lease_id"])
        current = session.get(TelemetryCurrent, gpu_id)
        assert lease is not None and current is not None
        lease.activated_at = utcnow() - timedelta(minutes=6)
        current.memory_used_mib = 31_000
        current.gpu_utilization_pct = 35
        session.commit()

    health = _gpus(service.snapshot(admin))[gpu_id]["keepalive"]["health"]
    assert health["state"] == "ERROR"
    assert health["reason"] == "keepalive rolling 5m utilization window is incomplete"


def test_reclaim_plan_selects_only_complete_verified_per_gpu_keepalive_set(service, admin) -> None:
    _configure_idle_policy(service, admin)
    begun = _begin(service, admin, 0)
    _confirm(service, admin, begun, 0)
    keepalive = begun["keepalive"]
    assert isinstance(keepalive, dict)
    # The target keeper's current free VRAM is deliberately below the request
    # floor. It becomes eligible only because the planner can prove that this
    # exact worker will be stopped and fresh telemetry will be checked again.
    with service.database.session() as session:
        current = session.get(TelemetryCurrent, "endpoint-a:GPU-endpoint-a-0")
        assert current is not None
        current.memory_free_mib = 50_000
        session.commit()
    request = RequestCreate.model_validate(
        {
            "project_id": "project-a",
            "task_ref": "two-gpu-workload",
            "purpose": "needs both test GPUs",
            "duration_seconds": 600,
            "constraints": {"gpu_count": 2, "min_free_vram_mib": 70_000},
        }
    )
    plan = service.plan_keepalive_reclaim(request)
    assert plan["complete"] is True
    assert plan["transitions"] == [
        {
            "action": "reclaim",
            "endpoint_id": "endpoint-a",
            "gpu_id": "endpoint-a:GPU-endpoint-a-0",
            "gpu_uuid": "GPU-endpoint-a-0",
            "lease_id": keepalive["lease_id"],
        }
    ]


def test_stop_is_per_gpu_and_requires_fresh_empty_target_observation(service, admin) -> None:
    _configure_idle_policy(service, admin)
    begun = _begin(service, admin)
    _confirm(service, admin, begun)
    service.configure_keepalive_policy(
        admin,
        "endpoint-a",
        "disabled",
        idempotency_key="policy-disabled",
    )
    plan = service.list_keepalive_transitions("endpoint-a")
    assert [item["action"] for item in plan["transitions"]] == ["stop"]
    keepalive = begun["keepalive"]
    assert isinstance(keepalive, dict)

    barrier = utcnow()
    service.ingest_observation(
        observation(count=2, processes=[process_for_gpu("GPU-endpoint-a-0", pid=4321)])
    )
    with pytest.raises(BrokerError) as running:
        service.finalize_keepalive_stop(
            admin,
            "endpoint-a",
            str(keepalive["lease_id"]),
            observation_not_before=barrier,
            idempotency_key="stop-running",
        )
    assert running.value.code == "keepalive_process_still_running"

    barrier = utcnow()
    service.ingest_observation(observation(count=2))
    stopped = service.finalize_keepalive_stop(
        admin,
        "endpoint-a",
        str(keepalive["lease_id"]),
        observation_not_before=barrier,
        idempotency_key="stop-empty",
    )
    assert stopped["keepalive"]["state"] == "RELEASED"
    gpus = _gpus(service.snapshot(admin))
    assert gpus["endpoint-a:GPU-endpoint-a-0"]["state"] == "AVAILABLE"
    assert gpus["endpoint-a:GPU-endpoint-a-1"]["state"] == "AVAILABLE"


def test_legacy_whole_endpoint_keepalive_is_fail_closed_until_explicit_stop(service, admin) -> None:
    _configure_idle_policy(service, admin)
    now = utcnow()
    with service.database.session() as session:
        request = AllocationRequest(
            id="legacy-request",
            actor_id=SYSTEM_ACTOR_ID,
            project_id=SYSTEM_PROJECT_ID,
            profile_id=None,
            auto_activate=False,
            task_ref="legacy-endpoint-keepalive",
            purpose="historic endpoint keepalive",
            constraints_json=json_dump({"gpu_count": 2}),
            duration_seconds=60,
            expected_duration_seconds=None,
            start_after=None,
            deadline=None,
            approval_ref=None,
            state="ACTIVE",
            priority_class="keepalive",
            blocked_reason=None,
            created_at=now,
            updated_at=now,
        )
        lease = Lease(
            id="legacy-lease",
            request_id=request.id,
            actor_id=SYSTEM_ACTOR_ID,
            project_id=SYSTEM_PROJECT_ID,
            kind="keepalive",
            keepalive_scope="legacy_endpoint",
            state="ACTIVE",
            issued_at=now,
            expires_at=now - timedelta(seconds=1),
            last_heartbeat_at=now,
            activated_at=now,
            released_at=None,
            release_reason=None,
            issued_revision=1,
        )
        session.add_all([request, lease])
        session.add_all(
            [
                LeaseResource(lease_id=lease.id, gpu_id="endpoint-a:GPU-endpoint-a-0", active=True),
                LeaseResource(lease_id=lease.id, gpu_id="endpoint-a:GPU-endpoint-a-1", active=True),
            ]
        )
        session.commit()

    service.reconcile(admin)
    with service.database.session() as session:
        assert session.get(Lease, "legacy-lease").state == "ACTIVE"
    plan = service.list_keepalive_transitions("endpoint-a")
    assert plan["transitions"] == [
        {
            "action": "stop_legacy_endpoint",
            "endpoint_id": "endpoint-a",
            "lease_id": "legacy-lease",
            "state": "ACTIVE",
            "gpu_ids": ["endpoint-a:GPU-endpoint-a-0", "endpoint-a:GPU-endpoint-a-1"],
            "gpu_uuids": ["GPU-endpoint-a-0", "GPU-endpoint-a-1"],
            "reason": "legacy endpoint keepalive requires explicit operator stop",
        }
    ]
    gpus = _gpus(service.snapshot(admin))
    assert {item["state"] for item in gpus.values()} == {"CONFLICT"}
    assert _endpoint(service.snapshot(admin))["keepalive"]["legacy_gpu_count"] == 2


def test_active_keepalive_adapter_cannot_be_removed(service, admin) -> None:
    _configure_idle_policy(service, admin)
    _begin(service, admin)

    with pytest.raises(BrokerError) as blocked:
        service.update_endpoint(
            admin,
            "endpoint-a",
            EndpointUpdate.model_validate({"keepalive_adapter_id": None}),
            idempotency_key="remove-active-keepalive-adapter",
        )
    assert blocked.value.code == "keepalive_endpoint_connection_in_use"
