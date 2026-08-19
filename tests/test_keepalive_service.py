from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from serverpilot.models import (
    Endpoint,
    KeepaliveCurrent,
    Lease,
    LeaseResource,
    ProcessObservation,
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
    assert summary["desired"] == "ON"
    assert summary["actual"] == "ON"
    assert summary["error_gpu_count"] == 0
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
    assert first["keepalive"]["desired"] == "ON"
    assert first["keepalive"]["actual"] == "ON"
    assert second["state"] == "AVAILABLE"
    assert second["keepalive"]["desired"] == "ON"
    assert second["keepalive"]["actual"] == "OFF"
    assert second["keepalive"]["reason"] is None
    assert second["publicly_available"] is True
    assert second["public_status"] == "可用 · 占卡未运行"
    assert endpoint["keepalive"] == {
        "configured": True,
        "policy": "idle_keepalive",
        "desired": "ON",
        "actual": "ON",
        "state": "ON",
        "active_gpu_count": 1,
        "error_gpu_count": 0,
        "eligible_idle_gpu_count": 1,
        "reasons": [],
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


def test_workload_turnover_on_one_gpu_does_not_block_sibling_keepalive_candidate(
    service, admin
) -> None:
    """A normal workload worker replacement leaves sibling capacity unchanged."""

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
    assert gpus["endpoint-a:GPU-endpoint-a-0"]["state"] == "RUNNING_MANAGED"
    assert gpus["endpoint-a:GPU-endpoint-a-1"]["state"] == "AVAILABLE"
    assert [item["gpu_id"] for item in service.desired_keepalive_candidates("endpoint-a")["candidates"]] == [
        "endpoint-a:GPU-endpoint-a-1"
    ]


def test_confirm_rejects_additional_process_on_keepalive_gpu(service, admin) -> None:
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
    with pytest.raises(BrokerError) as conflicted:
        service.activate_keepalive(
            admin,
            "endpoint-a",
            "endpoint-a:GPU-endpoint-a-0",
            observation_not_before=barrier,
            idempotency_key="activate-current-process",
        )
    assert conflicted.value.code == "keepalive_process_conflict"


def test_foreign_replacement_never_becomes_public_keepalive_capacity(service, admin) -> None:
    _configure_idle_policy(service, admin)
    _begin(service, admin, 0)
    service.ingest_observation(
        observation(
            count=2,
            processes=[process_for_gpu("GPU-endpoint-a-0", pid=8801)],
        )
    )

    gpu = _gpus(service.snapshot(admin))["endpoint-a:GPU-endpoint-a-0"]
    assert gpu["state"] == "CONFLICT"
    assert gpu["keepalive"]["desired"] == "ON"
    assert gpu["keepalive"]["actual"] == "ERROR"
    assert gpu["keepalive"]["reason"] == "检测到不属于占卡程序的进程"
    assert gpu["publicly_available"] is False
    assert gpu["public_status"] == "占卡校验失败，暂不可申请"


def test_live_identity_mismatch_plans_attested_recovery_without_admission(service, admin) -> None:
    _configure_idle_policy(service, admin)
    _begin(service, admin, 0)
    service.ingest_observation(
        observation(
            count=2,
            processes=[process_for_gpu("GPU-endpoint-a-0", pid=8801)],
        )
    )

    transitions = service.list_keepalive_transitions("endpoint-a")["transitions"]

    assert transitions == [
        {
            "action": "recover",
            "endpoint_id": "endpoint-a",
            "gpu_id": "endpoint-a:GPU-endpoint-a-0",
            "gpu_uuid": "GPU-endpoint-a-0",
            "reason": "requires sealed helper attestation",
        },
        {
            "action": "start",
            "endpoint_id": "endpoint-a",
            "gpu_id": "endpoint-a:GPU-endpoint-a-1",
            "gpu_uuid": "GPU-endpoint-a-1",
        },
    ]
    gpu = _gpus(service.snapshot(admin))["endpoint-a:GPU-endpoint-a-0"]
    assert gpu["state"] == "CONFLICT"
    assert gpu["publicly_available"] is False


def test_attested_confirmation_rebinds_replaced_keepalive_worker(service, admin) -> None:
    _configure_idle_policy(service, admin)
    _begin(service, admin, 0)
    barrier = utcnow()
    service.ingest_observation(
        observation(
            count=2,
            processes=[process_for_gpu("GPU-endpoint-a-0", pid=8801)],
        )
    )
    gpu_id = "endpoint-a:GPU-endpoint-a-0"

    confirmed = service.confirm_keepalive_workers(
        admin,
        "endpoint-a",
        [gpu_id],
        confirmed_worker_identities={gpu_id: (8801, "boot-endpoint-a")},
        observation_not_before=barrier,
        idempotency_key="confirm-replaced-keeper",
    )

    assert confirmed["keepalives"][0]["state"] == "ACTIVE"
    with service.database.session() as session:
        current = session.get(KeepaliveCurrent, gpu_id)
        observed = session.scalar(
            select(ProcessObservation).where(
                ProcessObservation.gpu_id == gpu_id,
                ProcessObservation.pid == 8801,
                ProcessObservation.active.is_(True),
            )
        )
        assert current is not None
        assert observed is not None
        assert current.actual == "ON"
        assert current.error_reason is None
        assert current.expected_pid == observed.pid
        assert current.expected_boot_id == observed.boot_id
        assert current.expected_process_started_at == observed.process_started_at
    gpu = _gpus(service.snapshot(admin))[gpu_id]
    assert gpu["state"] == "KEEPALIVE"
    assert gpu["publicly_available"] is True


def test_attested_confirmation_starts_and_binds_new_keepalive_worker(service, admin) -> None:
    _configure_idle_policy(service, admin)
    barrier = utcnow()
    service.ingest_observation(
        observation(
            count=2,
            processes=[process_for_gpu("GPU-endpoint-a-0", pid=7722)],
        )
    )
    gpu_id = "endpoint-a:GPU-endpoint-a-0"

    confirmed = service.confirm_keepalive_workers(
        admin,
        "endpoint-a",
        [gpu_id],
        confirmed_worker_identities={gpu_id: (7722, "boot-endpoint-a")},
        observation_not_before=barrier,
        idempotency_key="confirm-new-keeper",
    )

    assert confirmed["keepalives"][0]["state"] == "ACTIVE"
    gpu = _gpus(service.snapshot(admin))[gpu_id]
    assert gpu["state"] == "KEEPALIVE"
    assert gpu["keepalive"]["actual"] == "ON"


def test_attested_confirmation_rejects_unmatched_identity_and_keeps_conflict(service, admin) -> None:
    _configure_idle_policy(service, admin)
    _begin(service, admin, 0)
    barrier = utcnow()
    service.ingest_observation(
        observation(
            count=2,
            processes=[process_for_gpu("GPU-endpoint-a-0", pid=8801)],
        )
    )
    gpu_id = "endpoint-a:GPU-endpoint-a-0"

    with pytest.raises(BrokerError) as rejected:
        service.confirm_keepalive_workers(
            admin,
            "endpoint-a",
            [gpu_id],
            confirmed_worker_identities={gpu_id: (4321, "boot-endpoint-a")},
            observation_not_before=barrier,
            idempotency_key="reject-foreign-replacement",
        )

    assert rejected.value.code == "keepalive_confirmation_mismatch"
    gpu = _gpus(service.snapshot(admin))[gpu_id]
    assert gpu["state"] == "CONFLICT"
    assert gpu["keepalive"]["actual"] == "ERROR"


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


def test_validated_keepalive_stop_clears_old_expected_process_identity(service, admin) -> None:
    _configure_idle_policy(service, admin)
    begun = _begin(service, admin)
    keepalive = begun["keepalive"]
    assert isinstance(keepalive, dict)
    service.configure_keepalive_policy(
        admin,
        "endpoint-a",
        "disabled",
        idempotency_key="disable-before-identity-clear",
    )
    barrier = utcnow()
    service.ingest_observation(observation(count=2))

    service.finalize_keepalive_stop(
        admin,
        "endpoint-a",
        str(keepalive["lease_id"]),
        observation_not_before=barrier,
        idempotency_key="validated-stop-clears-identity",
    )

    with service.database.session() as session:
        current = session.get(KeepaliveCurrent, "endpoint-a:GPU-endpoint-a-0")
        assert current is not None
        assert current.actual == "OFF"
        assert current.expected_pid is None
        assert current.expected_boot_id is None
        assert current.expected_process_started_at is None


def test_complete_observation_releases_keepalive_lease_on_absent_gpu(service, admin) -> None:
    _configure_idle_policy(service, admin)
    begun = _begin(service, admin)
    keepalive = begun["keepalive"]
    assert isinstance(keepalive, dict)
    lease_id = str(keepalive["lease_id"])

    service.ingest_observation(observation(gpu_uuids=["GPU-new-0", "GPU-new-1"]))

    with service.database.session() as session:
        lease = session.get(Lease, lease_id)
        assert lease is not None
        assert lease.state == "RELEASED"
        assert lease.release_reason == "gpu absent from endpoint inventory"
        resources = session.scalars(
            select(LeaseResource).where(LeaseResource.lease_id == lease_id)
        ).all()
        assert resources
        assert all(not resource.active for resource in resources)
        assert session.get(KeepaliveCurrent, "endpoint-a:GPU-endpoint-a-0") is None

    claimed = service.create_request(
        admin,
        RequestCreate.model_validate(
            {
                "project_id": "project-a",
                "task_ref": "after-absent-keepalive",
                "purpose": "new GPU should be allocatable",
                "duration_seconds": 3600,
                "constraints": {"gpu_count": 1},
            }
        ),
        idempotency_key="after-absent-keepalive",
    )
    assert claimed["lease"] is not None
    assert claimed["lease"]["gpu_ids"] == ["endpoint-a:GPU-new-0"]


def test_absent_workload_lease_is_not_auto_released_but_can_be_cleared(
    service, admin
) -> None:
    service.ingest_observation(observation(gpu_uuids=["GPU-old", "GPU-new"]))
    claimed = service.create_request(
        admin,
        RequestCreate.model_validate(
            {
                "project_id": "project-a",
                "task_ref": "absent-workload",
                "purpose": "hold old GPU",
                "duration_seconds": 3600,
                "constraints": {
                    "gpu_count": 1,
                    "placement": "exact",
                    "gpu_ids": ["endpoint-a:GPU-old"],
                },
            }
        ),
        idempotency_key="absent-workload",
        activate_if_allocated=True,
    )
    lease_id = claimed["lease"]["id"]

    service.ingest_observation(observation(gpu_uuids=["GPU-new"]))
    with service.database.session() as session:
        lease = session.get(Lease, lease_id)
        assert lease is not None
        assert lease.state in {"HELD", "ACTIVE"}
        assert lease.kind == "workload"

    barrier = utcnow()
    service.ingest_observation(observation(gpu_uuids=["GPU-new"]))
    released = service.release_empty_conflicted_lease(
        admin,
        "endpoint-a",
        lease_id,
        observation_not_before=barrier,
        idempotency_key="absent-workload-release-empty",
    )
    assert released["released"] is True
    assert released["lease"]["state"] == "RELEASED"


def test_incomplete_observation_does_not_release_absent_keepalive_lease(service, admin) -> None:
    _configure_idle_policy(service, admin)
    begun = _begin(service, admin)
    keepalive = begun["keepalive"]
    assert isinstance(keepalive, dict)
    lease_id = str(keepalive["lease_id"])

    service.ingest_observation(
        observation(gpu_uuids=["GPU-new-0"], observation_complete=False)
    )

    with service.database.session() as session:
        lease = session.get(Lease, lease_id)
        assert lease is not None
        assert lease.state == "ACTIVE"
        resources = session.scalars(
            select(LeaseResource).where(
                LeaseResource.lease_id == lease_id, LeaseResource.active.is_(True)
            )
        ).all()
        assert [resource.gpu_id for resource in resources] == ["endpoint-a:GPU-endpoint-a-0"]


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
