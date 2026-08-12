from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from serverpilot.models import (
    Endpoint,
    Lease,
    LeaseResource,
    TelemetryCurrent,
)
from serverpilot.schemas import EndpointUpdate, LeaseObservedBind, RequestCreate
from serverpilot.service import BrokerError, BrokerService
from serverpilot.timeutil import utcnow
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
    started = utcnow()
    service.ingest_observation(
        observation(
            count=2,
            processes=[process_for_gpu(f"GPU-endpoint-a-{index}", pid=4321 + index)],
        )
    )
    return service.activate_keepalive(
        admin,
        "endpoint-a",
        f"endpoint-a:GPU-endpoint-a-{index}",
        observation_not_before=started,
        idempotency_key=f"activate-{index}",
    )


def _confirm(service, admin, begun: dict[str, object], index: int = 0) -> dict[str, object]:  # noqa: ANN001
    return begun


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
    with service.database.session() as session:
        lease = session.get(Lease, keepalive["lease_id"])
        assert lease is not None and lease.kind == "keepalive"
        assert [resource.gpu_id for resource in session.scalars(
            select(LeaseResource).where(LeaseResource.lease_id == lease.id)
        )] == ["endpoint-a:GPU-endpoint-a-0"]

    after = service.desired_keepalive_candidates("endpoint-a")
    assert [item["gpu_id"] for item in after["candidates"]] == [
        "endpoint-a:GPU-endpoint-a-1",
    ]
    summary = service.get_endpoint_keepalive_summary("endpoint-a")["keepalive"]
    assert summary["policy"] == "idle_keepalive"
    assert summary["state"] == "ERROR"
    assert summary["error_gpu_count"] == 1
    assert {item["reason"] for item in summary["reasons"]} == {"未检测到占卡程序"}
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
    assert second["keepalive"]["state"] == "ERROR"
    assert second["keepalive"]["reason"] == "未检测到占卡程序"
    assert second["publicly_available"] is True
    assert second["public_status"] == "可用 · 占卡异常：未检测到占卡程序"
    assert endpoint["keepalive"] == {
        "configured": True,
        "policy": "idle_keepalive",
        "state": "ERROR",
        "active_gpu_count": 1,
        "error_gpu_count": 1,
        "eligible_idle_gpu_count": 1,
        "reasons": [
            {
                "gpu_id": "endpoint-a:GPU-endpoint-a-1",
                "reason": "未检测到占卡程序",
            }
        ],
    }
    assert all(item["kind"] != "keepalive" for item in snapshot["data"]["leases"])
    assert [item["gpu_id"] for item in service.desired_keepalive_candidates("endpoint-a")["candidates"]] == [
        "endpoint-a:GPU-endpoint-a-1"
    ]


def test_public_gpu_status_reports_connection_failure_from_canonical_monitor_state() -> None:
    projection = BrokerService._gpu_public_projection(
        {
            "state": "UNKNOWN_STALE",
            "lease": None,
            "keepalive": {"state": "OFF", "reason": None},
        },
        monitor_status="ERROR",
    )

    assert projection == {
        "publicly_available": False,
        "public_status": "连接失败",
    }


def test_workload_conflict_on_one_gpu_does_not_block_sibling_keepalive_candidate(service, admin) -> None:
    """A stale workload ownership record must be isolated to its own GPU."""

    _configure_idle_policy(service, admin)
    claimed = service.create_request(
        admin,
        RequestCreate.model_validate(
            {
                "project_id": "project-a",
                "task_ref": "conflict-on-one-gpu",
                "purpose": "test independent keepalive placement",
                "duration_seconds": 600,
                "constraints": {"gpu_count": 1, "placement": "pack"},
            }
        ),
        idempotency_key="conflict-on-one-gpu-claim",
        activate_if_allocated=True,
    )
    lease_id = claimed["lease"]["id"]
    gpu_uuid = service.list_gpus(admin)["data"][0]["gpu_uuid"]
    started_at = utcnow() - timedelta(minutes=3)
    initial = process_for_gpu(gpu_uuid).model_copy(update={"process_started_at": started_at})
    service.ingest_observation(observation(count=2, processes=[initial]))
    service.bind_observed_workload(
        admin,
        lease_id,
        LeaseObservedBind(run_id="conflict-on-one-gpu-run"),
        idempotency_key="conflict-on-one-gpu-bind",
    )
    replacement = initial.model_copy(update={"process_started_at": started_at + timedelta(seconds=10)})
    service.ingest_observation(observation(count=2, processes=[replacement]))
    service.ingest_observation(observation(count=2, processes=[replacement]))

    gpus = _gpus(service.snapshot(admin))
    assert gpus["endpoint-a:GPU-endpoint-a-0"]["state"] == "CONFLICT"
    assert gpus["endpoint-a:GPU-endpoint-a-1"]["state"] == "AVAILABLE"
    assert [item["gpu_id"] for item in service.desired_keepalive_candidates("endpoint-a")["candidates"]] == [
        "endpoint-a:GPU-endpoint-a-1"
    ]


def test_confirm_uses_the_current_gpu_process_without_extra_binding_state(service, admin) -> None:
    _configure_idle_policy(service, admin)
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
    confirmed = service.activate_keepalive(
        admin,
        "endpoint-a",
        "endpoint-a:GPU-endpoint-a-0",
        observation_not_before=barrier,
        idempotency_key="activate-current-process",
    )
    assert confirmed["keepalive"]["state"] == "ACTIVE"


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


def test_endpoint_operator_can_clear_stale_per_gpu_keepalive_lease(service, admin) -> None:
    """A failed stop leaves a recoverable internal lease, not a permanent wedge."""

    _configure_idle_policy(service, admin)
    begun = _begin(service, admin)
    keepalive = begun["keepalive"]
    assert isinstance(keepalive, dict)
    lease_id = str(keepalive["lease_id"])

    service.configure_keepalive_policy(
        admin,
        "endpoint-a",
        "disabled",
        idempotency_key="stale-keepalive-policy-disabled",
    )
    barrier = utcnow()
    service.ingest_observation(observation(count=2, processes=[]))

    released = service.release_empty_conflicted_lease(
        admin,
        "endpoint-a",
        lease_id,
        observation_not_before=barrier,
        idempotency_key="stale-keepalive-release-empty",
    )

    assert released["released"] is True
    assert released["lease"]["state"] == "RELEASED"
    gpus = _gpus(service.snapshot(admin))
    assert gpus["endpoint-a:GPU-endpoint-a-0"]["state"] == "AVAILABLE"


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


def test_active_keepalive_workspace_cannot_change(service, admin) -> None:
    _configure_idle_policy(service, admin)
    _begin(service, admin)

    with pytest.raises(BrokerError) as blocked:
        service.update_endpoint(
            admin,
            "endpoint-a",
            EndpointUpdate.model_validate({"workspace_path": "/srv/project-a-next"}),
            idempotency_key="change-active-keepalive-workspace",
        )
    assert blocked.value.code == "keepalive_endpoint_connection_in_use"
    assert blocked.value.details == {"fields": ["workspace_path"]}
