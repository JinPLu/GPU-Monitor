from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from gpu_broker.database import Database
from gpu_broker.models import (
    Alert,
    AuditEvent,
    Endpoint,
    GPUDevice,
    Lease,
    LeaseResource,
    MaintenanceWindow,
    ProviderState,
    TelemetryCurrent,
    TelemetrySnapshot,
)
from gpu_broker.schemas import (
    ActorCreate,
    EndpointEnabled,
    EndpointUpsert,
    LeaseObservedBind,
    MaintenanceCreate,
    ReservationCreate,
    RequestCreate,
    WorkloadProfileClaim,
    WorkloadProfileUpsert,
)
from gpu_broker.service import ACTIVE_LEASE_STATES, BrokerError, BrokerService
from gpu_broker.timeutil import utcnow
from tests.helpers import observation, process_for_gpu


def request_data(task_ref: str, *, count: int = 1, project_id: str = "project-a") -> RequestCreate:
    return RequestCreate.model_validate(
        {
            "project_id": project_id,
            "task_ref": task_ref,
            "purpose": "unit-test cooperative request",
            "duration_seconds": 3600,
            "constraints": {"gpu_count": count, "placement": "pack"},
        }
    )


def test_inventory_unknown_is_fail_closed(service, admin) -> None:
    result = service.create_request(admin, request_data("unknown"), idempotency_key="unknown-1")
    assert result["lease"] is None
    assert result["request"]["state"] == "QUEUED"
    assert "eligible" in result["request"]["blocked_reason"]


def test_bootstrap_token_is_created_once_and_never_replaced(tmp_path: Path, inventory) -> None:
    broker = BrokerService(
        Database(f"sqlite:///{tmp_path / 'bootstrap.sqlite3'}", Path(__file__).resolve().parents[1]),
        inventory,
    )
    first = "a" * 32
    second = "b" * 32
    assert broker.initialize(first) is True
    assert broker.initialize(second) is False
    assert broker.authenticate(first).is_admin
    with pytest.raises(BrokerError) as error:
        broker.authenticate(second)
    assert error.value.code == "invalid_token"


def test_idempotent_request_and_stable_uuid_identity(service, admin) -> None:
    service.ingest_observation(observation())
    first = service.create_request(admin, request_data("idempotent"), idempotency_key="key-1")
    second = service.create_request(admin, request_data("idempotent"), idempotency_key="key-1")
    assert first == second
    assert first["lease"] is not None
    gpu = service.list_gpus(admin)["data"][0]
    assert gpu["id"] == f"endpoint-a:{gpu['gpu_uuid']}"
    assert gpu["state"] == "HELD"


def test_workload_profile_claim_uses_approved_contract_atomically(service, admin) -> None:
    service.ingest_observation(observation(count=2))
    created = service.upsert_workload_profile(
        admin,
        WorkloadProfileUpsert.model_validate(
            {
                "id": "benchmark-2gpu",
                "project_id": "project-a",
                "display_name": "Benchmark two GPU",
                "purpose": "approved benchmark evaluation",
                "duration_seconds": 7200,
                "constraints": {
                    "gpu_count": 2,
                    "placement": "pack",
                },
            }
        ),
        idempotency_key="profile-upsert",
    )
    assert created["workload_profile"]["constraints"]["endpoint_ids"] == []

    first = service.claim_workload_profile(
        admin,
        "benchmark-2gpu",
        WorkloadProfileClaim(task_ref="run-2026-07-19"),
        idempotency_key="profile-claim",
    )
    second = service.claim_workload_profile(
        admin,
        "benchmark-2gpu",
        WorkloadProfileClaim(task_ref="run-2026-07-19"),
        idempotency_key="profile-claim",
    )

    assert first == second
    assert first["lease"] is not None
    assert first["lease"]["state"] == "ACTIVE"
    request = first["request"]
    assert request["profile_id"] == "benchmark-2gpu"
    assert request["purpose"] == "approved benchmark evaluation"
    assert request["duration_seconds"] == 7200
    assert request["constraints"]["gpu_count"] == 2
    assert request["constraints"]["endpoint_ids"] == []
    events = service.list_events(admin)["data"]
    request_event = next(event for event in events if event["action"] == "request.created")
    assert request_event["summary"]["profile_id"] == "benchmark-2gpu"


def test_queued_routine_claim_auto_activates_when_capacity_arrives(service, admin) -> None:
    service.upsert_workload_profile(
        admin,
        WorkloadProfileUpsert.model_validate(
            {
                "id": "queued-eval",
                "project_id": "project-a",
                "display_name": "Queued evaluation",
                "purpose": "approved queued evaluation",
                "duration_seconds": 7200,
                "constraints": {"gpu_count": 1},
            }
        ),
        idempotency_key="queued-profile",
    )
    queued = service.claim_workload_profile(
        admin,
        "queued-eval",
        WorkloadProfileClaim(task_ref="queued-run"),
        idempotency_key="queued-claim",
    )
    assert queued["lease"] is None
    assert queued["request"]["state"] == "QUEUED"

    service.ingest_observation(observation(count=1))
    request = service.list_requests(admin)["data"][0]
    lease = service.list_leases(admin)["data"][0]
    assert request["state"] == lease["state"] == "ACTIVE"


def test_renewal_cannot_cross_a_future_reservation(service, admin) -> None:
    service.ingest_observation(observation(count=1))
    gpu_id = service.list_gpus(admin)["data"][0]["id"]
    start_at = utcnow() + timedelta(minutes=65)
    service.create_reservation(
        admin,
        ReservationCreate(
            project_id="project-a",
            gpu_ids=[gpu_id],
            start_at=start_at,
            end_at=start_at + timedelta(hours=1),
            reason="next approved workload",
        ),
        idempotency_key="future-reservation",
    )
    claimed = service.create_request(admin, request_data("renewal-window"), idempotency_key="renewal-window")
    assert claimed["lease"] is not None

    with pytest.raises(BrokerError) as error:
        service.renew_lease(admin, claimed["lease"]["id"], idempotency_key="renewal-conflict")
    assert error.value.code == "lease_renewal_conflicts_with_reservation"


def test_gang_all_or_nothing_and_no_partial_write(service, admin) -> None:
    service.ingest_observation(observation(count=3))
    first = service.create_request(admin, request_data("gang-a", count=2), idempotency_key="gang-a")
    second = service.create_request(admin, request_data("gang-b", count=2), idempotency_key="gang-b")
    assert first["lease"] is not None
    assert len(first["lease"]["gpu_ids"]) == 2
    assert second["lease"] is None
    assert second["request"]["state"] == "QUEUED"
    leases = service.list_leases(admin)["data"]
    assert sum(len(lease["gpu_ids"]) for lease in leases if lease["state"] in ACTIVE_LEASE_STATES) == 2


def test_host_resource_constraints_are_absolute_and_fail_closed(service, admin) -> None:
    service.ingest_observation(observation(count=2))

    too_much_cpu = RequestCreate.model_validate(
        {
            "project_id": "project-a",
            "task_ref": "needs-absolute-cpu",
            "purpose": "request more free CPU cores than the endpoint currently has",
            "constraints": {"gpu_count": 1, "min_available_cpu_cores": 61},
        }
    )
    cpu_blocked = service.create_request(admin, too_much_cpu, idempotency_key="absolute-cpu")
    assert cpu_blocked["lease"] is None
    assert cpu_blocked["request"]["state"] == "QUEUED"
    assert "available_cpu" in cpu_blocked["request"]["blocked_reason"]

    too_much_memory = RequestCreate.model_validate(
        {
            "project_id": "project-a",
            "task_ref": "needs-absolute-memory",
            "purpose": "request more free system memory than the endpoint currently has",
            "constraints": {"gpu_count": 1, "min_available_memory_mib": 200 * 1024},
        }
    )
    memory_blocked = service.create_request(admin, too_much_memory, idempotency_key="absolute-memory")
    assert memory_blocked["lease"] is None
    assert "available_memory" in memory_blocked["request"]["blocked_reason"]

    right_sized = RequestCreate.model_validate(
        {
            "project_id": "project-a",
            "task_ref": "right-sized-absolute-resources",
            "purpose": "request absolute resources within current telemetry",
            "constraints": {
                "gpu_count": 1,
                "min_available_cpu_cores": 16,
                "min_available_memory_mib": 64 * 1024,
                "min_free_vram_mib": 60 * 1024,
                "min_total_vram_mib": 80 * 1024,
            },
        }
    )
    allocated = service.create_request(admin, right_sized, idempotency_key="absolute-right-sized")
    assert allocated["lease"] is not None


def test_fair_queue_interleaves_projects_after_fresh_telemetry(service, admin) -> None:
    # All requests initially queue because no GPU UUID has been observed yet.
    service.create_request(admin, request_data("story-a"), idempotency_key="story-a")
    service.create_request(admin, request_data("story-b"), idempotency_key="story-b")
    service.create_request(
        admin,
        request_data("project-b-task", project_id="project-b"),
        idempotency_key="wr-a",
    )
    service.ingest_observation(observation(count=3))
    allocations = [
        event["summary"]["project_id"]
        for event in service.list_events(admin)["data"]
        if event["action"] == "lease.issued"
    ]
    assert set(allocations[:2]) == {"project-a", "project-b"}


def test_endpoint_identity_is_enforced(service, admin) -> None:
    service.ingest_observation(observation(count=4))
    created = service.upsert_endpoint(
        admin,
        EndpointUpsert(
            id="endpoint-new",
            host="127.0.0.1",
            port=2203,
            ssh_user="gpu",
            project_ids=["project-a"],
        ),
        idempotency_key="endpoint-new",
    )
    assert created["endpoint"]["id"] == "endpoint-new"
    disabled = service.set_endpoint_enabled(
        admin,
        "endpoint-new",
        EndpointEnabled(enabled=False),
        idempotency_key="endpoint-disable",
    )
    assert disabled["endpoint"]["enabled"] is False
    with pytest.raises(BrokerError) as error:
        service.upsert_endpoint(
            admin,
            EndpointUpsert(
                id="endpoint-new",
                host="127.0.0.1",
                port=2299,
                ssh_user="gpu",
                project_ids=["project-a"],
            ),
            idempotency_key="endpoint-move",
        )
    assert error.value.code == "endpoint_identity_immutable"


def test_endpoint_delete_removes_unleased_monitoring_state(service, admin) -> None:
    service.ingest_observation(observation(count=2))
    service.record_provider_failure("endpoint-a", "timeout")

    deleted = service.delete_endpoint(admin, "endpoint-a", idempotency_key="endpoint-delete")
    retried = service.delete_endpoint(admin, "endpoint-a", idempotency_key="endpoint-delete")

    assert retried == deleted
    assert deleted["endpoint_id"] == "endpoint-a"
    assert deleted["deleted_gpu_count"] == 2
    assert deleted["deleted_monitoring_records"] == {
        "alerts": 1,
        "provider_states": 1,
        "host_telemetry_current": 1,
        "gpu_telemetry_current": 2,
        "gpu_telemetry_history": 2,
        "process_observations": 0,
    }
    assert [endpoint["id"] for endpoint in service.list_endpoints(admin)["data"]] == ["endpoint-b"]
    assert all(gpu["endpoint_id"] != "endpoint-a" for gpu in service.list_gpus(admin)["data"])
    assert any(event["action"] == "endpoint.deleted" for event in service.list_events(admin)["data"])

    def deleted_rows(session):  # type: ignore[no-untyped-def]
        return (
            session.get(Endpoint, "endpoint-a"),
            session.scalars(select(GPUDevice).where(GPUDevice.endpoint_id == "endpoint-a")).all(),
            session.scalars(select(ProviderState).where(ProviderState.endpoint_id == "endpoint-a")).all(),
            session.scalars(
                select(Alert).where(
                    Alert.resource_type == "endpoint",
                    Alert.resource_id == "endpoint-a",
                )
            ).all(),
        )

    endpoint, gpus, provider_states, alerts = service._read(deleted_rows)
    assert endpoint is None
    assert gpus == []
    assert provider_states == []
    assert alerts == []


def test_endpoint_delete_refuses_active_and_historical_leases(service, admin) -> None:
    service.ingest_observation(observation(count=1))
    claimed = service.create_request(admin, request_data("delete-blocked"), idempotency_key="delete-blocked")
    assert claimed["lease"] is not None

    with pytest.raises(BrokerError) as active_error:
        service.delete_endpoint(admin, "endpoint-a", idempotency_key="delete-active")
    assert active_error.value.code == "endpoint_has_active_leases"

    service.release_lease(
        admin,
        claimed["lease"]["id"],
        reason="finished",
        idempotency_key="delete-blocked-release",
    )
    with pytest.raises(BrokerError) as history_error:
        service.delete_endpoint(admin, "endpoint-a", idempotency_key="delete-history")
    assert history_error.value.code == "endpoint_has_lease_history"


def test_endpoint_delete_refuses_maintenance_history(service, admin) -> None:
    service.ingest_observation(observation(count=1))
    created = service.create_maintenance(
        admin,
        MaintenanceCreate(
            endpoint_id="endpoint-a",
            start_at=utcnow() - timedelta(minutes=5),
            end_at=utcnow() + timedelta(minutes=55),
            reason="hardware inspection",
        ),
        idempotency_key="endpoint-maintenance",
    )

    with pytest.raises(BrokerError) as error:
        service.delete_endpoint(admin, "endpoint-a", idempotency_key="delete-maintenance")

    assert error.value.code == "endpoint_referenced_by_maintenance"
    assert error.value.details == {"maintenance_ids": [created["maintenance"]["id"]]}
    assert service._read(lambda session: session.get(MaintenanceWindow, created["maintenance"]["id"])) is not None


def test_endpoint_delete_refuses_enabled_workload_profile_references(service, admin) -> None:
    service.upsert_workload_profile(
        admin,
        WorkloadProfileUpsert.model_validate(
            {
                "id": "endpoint-bound-profile",
                "project_id": "project-a",
                "display_name": "Endpoint bound profile",
                "purpose": "keep endpoint reference valid",
                "duration_seconds": 3600,
                "constraints": {"gpu_count": 1, "endpoint_ids": ["endpoint-b"]},
            }
        ),
        idempotency_key="endpoint-bound-profile",
    )

    with pytest.raises(BrokerError) as error:
        service.delete_endpoint(admin, "endpoint-b", idempotency_key="delete-profile-ref")
    assert error.value.code == "endpoint_referenced_by_profiles"


def test_claim_auto_creates_project_and_ignores_legacy_endpoint_scope(service, admin) -> None:
    service.ingest_observation(observation(count=1))
    claimed = service.create_request(
        admin,
        request_data("cross-project-claim", project_id="storyboard"),
        idempotency_key="storyboard-claim",
        activate_if_allocated=True,
    )
    assert claimed["lease"] is not None
    assert claimed["request"]["state"] == "ACTIVE"
    assert claimed["lease"]["project_id"] == "storyboard"
    projects = {project["id"] for project in service.list_projects(admin)["data"]}
    assert "storyboard" in projects


def test_coordination_board_and_observed_binding_are_agent_self_service(service, admin) -> None:
    service.ingest_observation(observation(count=1))
    claimed = service.create_request(
        admin,
        request_data("coordination-run"),
        idempotency_key="coordination-claim",
        activate_if_allocated=True,
    )
    assert claimed["lease"] is not None
    gpu = service.list_gpus(admin)["data"][0]
    service.ingest_observation(observation(count=1, processes=[process_for_gpu(gpu["gpu_uuid"])]))

    bound = service.bind_observed_workload(
        admin,
        claimed["lease"]["id"],
        LeaseObservedBind(),
        idempotency_key="coordination-bind",
    )
    assert bound["lease"]["workloads"][0]["run_id"] == f"lease:{claimed['lease']['id']}"
    assert len(bound["lease"]["workloads"][0]["process_keys"]) == 1

    gpu = service.list_gpus(admin)["data"][0]
    assert gpu["state"] == "RUNNING_MANAGED"
    assert gpu["processes"][0]["process_key"]
    board = service.coordination(admin)["data"]
    assert board["summary"]["active_agents"] == 1
    assert board["summary"]["managed_running_gpus"] == 1
    assert board["servers"][0]["consumers"][0]["agent_name"] == admin.id
    assert board["leases"][0]["activity"] == "running"
    assert board["agents"][0]["managed_running_gpus"] == 1


def test_observed_workload_binding_survives_one_second_process_start_jitter(service, admin) -> None:
    service.ingest_observation(observation(count=1))
    claimed = service.create_request(
        admin,
        request_data("jitter-stable-run"),
        idempotency_key="jitter-stable-claim",
        activate_if_allocated=True,
    )
    assert claimed["lease"] is not None
    gpu_uuid = service.list_gpus(admin)["data"][0]["gpu_uuid"]
    started_at = utcnow() - timedelta(minutes=3)
    initial_process = process_for_gpu(gpu_uuid).model_copy(
        update={"process_started_at": started_at}
    )
    service.ingest_observation(observation(count=1, processes=[initial_process]))
    service.bind_observed_workload(
        admin,
        claimed["lease"]["id"],
        LeaseObservedBind(run_id="jitter-stable-run-1"),
        idempotency_key="jitter-stable-bind",
    )

    jittered_process = initial_process.model_copy(
        update={"process_started_at": started_at + timedelta(seconds=1)}
    )
    service.ingest_observation(observation(count=1, processes=[jittered_process]))

    gpu = service.list_gpus(admin)["data"][0]
    assert gpu["state"] == "RUNNING_MANAGED"
    assert gpu["processes"][0]["observations"] == 2
    assert gpu["lease"]["workloads"][0]["process_keys"] == [gpu["processes"][0]["process_key"]]


def test_observed_binding_recovers_an_attribution_conflict_without_remote_control(service, admin) -> None:
    service.ingest_observation(observation(count=1))
    claimed = service.create_request(
        admin,
        request_data("recover-attribution-run"),
        idempotency_key="recover-attribution-claim",
        activate_if_allocated=True,
    )
    assert claimed["lease"] is not None
    gpu_uuid = service.list_gpus(admin)["data"][0]["gpu_uuid"]
    started_at = utcnow() - timedelta(minutes=3)
    initial_process = process_for_gpu(gpu_uuid).model_copy(
        update={"process_started_at": started_at}
    )
    service.ingest_observation(observation(count=1, processes=[initial_process]))
    service.bind_observed_workload(
        admin,
        claimed["lease"]["id"],
        LeaseObservedBind(run_id="recover-attribution-run-1"),
        idempotency_key="recover-attribution-bind-initial",
    )

    replacement = initial_process.model_copy(
        update={"process_started_at": started_at + timedelta(seconds=10)}
    )
    service.ingest_observation(observation(count=1, processes=[replacement]))
    service.ingest_observation(observation(count=1, processes=[replacement]))
    assert service.list_gpus(admin)["data"][0]["state"] == "CONFLICT"

    recovered = service.bind_observed_workload(
        admin,
        claimed["lease"]["id"],
        LeaseObservedBind(run_id="recover-attribution-run-1"),
        idempotency_key="recover-attribution-bind-current",
    )
    assert recovered["conflict_resolved"] is True
    gpu = service.list_gpus(admin)["data"][0]
    assert gpu["state"] == "RUNNING_MANAGED"
    assert gpu["lease"]["state"] == "ACTIVE"


def test_process_and_stale_telemetry_block_admission(service, admin) -> None:
    service.ingest_observation(observation(count=1, processes=[process_for_gpu("GPU-endpoint-a-0")]))
    # A compute process blocks immediately; a second sample is only needed to label a lease conflict.
    blocked = service.create_request(admin, request_data("process-busy"), idempotency_key="proc-busy")
    assert blocked["lease"] is None
    assert service.list_gpus(admin)["data"][0]["state"] == "BUSY_UNMANAGED"

    def age_telemetry(session) -> None:  # type: ignore[no-untyped-def]
        snapshot = session.scalar(select(TelemetryCurrent))
        assert snapshot is not None
        snapshot.observed_at = utcnow() - timedelta(seconds=1000)

    service._write(age_telemetry)
    assert service.list_gpus(admin)["data"][0]["state"] == "UNKNOWN_STALE"


def test_attention_summary_separates_endpoint_and_gpu_units(service, admin) -> None:
    service.ingest_observation(observation(count=2, processes=[process_for_gpu("GPU-endpoint-a-0")]))
    service.record_provider_failure("endpoint-b", "timeout")

    snapshot = service.snapshot(admin)["data"]

    assert snapshot["summary"]["abnormal_gpus"] == 0
    assert snapshot["summary"]["attention"] == {
        "endpoint_count": 1,
        "endpoint_status_counts": {"ERROR": 1},
        "gpu_count": 1,
        "gpu_state_counts": {"BUSY_UNMANAGED": 1},
        "unmanaged_gpu_count": 1,
        "total_resource_count": 2,
    }


def test_current_telemetry_is_bounded_and_routine_samples_do_not_audit(service, admin) -> None:
    first = observation(count=3)
    service.ingest_observation(first)
    service.ingest_observation(observation(count=3))

    def counts(session):  # type: ignore[no-untyped-def]
        return (
            len(session.scalars(select(TelemetryCurrent)).all()),
            len(session.scalars(select(TelemetrySnapshot)).all()),
            len(session.scalars(select(AuditEvent)).all()),
        )

    current_count, history_count, audit_count = service._read(counts)
    assert current_count == 3
    assert history_count == 3
    assert audit_count == 0


def test_endpoint_cpu_and_memory_telemetry_is_exposed_in_snapshot(service, admin) -> None:
    service.ingest_observation(observation(count=1))
    endpoint = service.snapshot(admin)["data"]["endpoints"][0]
    assert endpoint["host_telemetry"] == {
        "observed_at": endpoint["host_telemetry"]["observed_at"],
        "collected_at": endpoint["host_telemetry"]["collected_at"],
        "cpu_count": 64,
        "load_1m": 4.0,
        "memory_total_mib": 262_144,
        "memory_available_mib": 196_608,
        "provider": "raw-ssh",
    }


def test_gpu_history_is_downsampled_to_requested_cap(service, admin) -> None:
    service.ingest_observation(observation(count=1))
    gpu_id = service.list_gpus(admin)["data"][0]["id"]

    def seed_history(session) -> None:  # type: ignore[no-untyped-def]
        start = utcnow() - timedelta(hours=3)
        for index in range(130):
            session.add(
                TelemetrySnapshot(
                    gpu_id=gpu_id,
                    observed_at=start + timedelta(minutes=index),
                    collected_at=start + timedelta(minutes=index),
                    memory_used_mib=index,
                    memory_free_mib=100_000 - index,
                    gpu_utilization_pct=index % 100,
                    memory_utilization_pct=index % 100,
                    temperature_c=35,
                    power_watts=100.0,
                    pstate="P0",
                    health="OK",
                    provider="test",
                )
            )

    service._write(seed_history)
    history = service.gpu_history(admin, gpu_id, window_seconds=21_600, max_points=120)
    assert history["data"]["point_count"] == 120


def test_provider_audit_is_written_only_on_failure_and_recovery_transitions(service) -> None:
    service.record_provider_failure("endpoint-a", "timeout")
    service.record_provider_failure("endpoint-a", "timeout")
    service.ingest_observation(observation(count=1))
    service.ingest_observation(observation(count=1))

    def actions(session):  # type: ignore[no-untyped-def]
        return [event.action for event in session.scalars(select(AuditEvent).order_by(AuditEvent.id))]

    assert service._read(actions) == ["telemetry.failed", "telemetry.recovered"]


def test_expired_lease_with_process_becomes_orphan_and_stays_blocked(service, admin) -> None:
    service.ingest_observation(observation(count=1))
    allocated = service.create_request(admin, request_data("will-orphan"), idempotency_key="orphan")
    assert allocated["lease"] is not None
    lease_id = allocated["lease"]["id"]

    def expire(session) -> None:  # type: ignore[no-untyped-def]
        lease = session.get(Lease, lease_id)
        assert lease is not None
        lease.expires_at = utcnow() - timedelta(seconds=1)

    service._write(expire)
    service.ingest_observation(observation(count=1, processes=[process_for_gpu("GPU-endpoint-a-0")]))
    lease = next(item for item in service.list_leases(admin)["data"] if item["id"] == lease_id)
    assert lease["state"] == "ORPHANED_BUSY"
    blocked = service.create_request(admin, request_data("must-not-reuse"), idempotency_key="blocked-orphan")
    assert blocked["lease"] is None


def test_allocator_can_claim_an_unregistered_project_and_token_hash_never_returned(service, admin) -> None:
    created = service.create_actor(
        admin,
        ActorCreate(
            id="story-agent",
            display_name="Project A agent",
            role="allocator",
            project_ids=["project-a"],
            token_label="test",
        ),
        idempotency_key="new-agent",
    )
    assert created["token"]
    agent = service.authenticate(created["token"])
    claimed = service.create_request(
        agent,
        request_data("unregistered-project", project_id="storyboard"),
        idempotency_key="unregistered-project",
    )
    assert claimed["request"]["project_id"] == "storyboard"
    assert any(item["id"] == claimed["request"]["id"] for item in service.list_requests(agent)["data"])
    actors = service.list_actors(admin)["data"]
    assert "token_hash" not in str(actors)


def test_one_hundred_concurrent_requests_never_double_lease(service, admin) -> None:
    service.ingest_observation(observation(count=4))

    def submit(index: int):  # type: ignore[no-untyped-def]
        return service.create_request(
            admin,
            request_data(f"concurrent-{index}"),
            idempotency_key=f"concurrent-{index}",
        )

    results = []
    with ThreadPoolExecutor(max_workers=32) as pool:
        futures = [pool.submit(submit, index) for index in range(100)]
        for future in as_completed(futures):
            results.append(future.result())
    leases = [result["lease"] for result in results if result["lease"] is not None]
    gpu_ids = [gpu_id for lease in leases for gpu_id in lease["gpu_ids"]]
    assert len(gpu_ids) == len(set(gpu_ids)) == 4
    assert all(result["request"]["state"] in {"LEASED", "QUEUED"} for result in results)


def test_database_unique_index_rejects_duplicate_active_gpu(service, admin) -> None:
    service.ingest_observation(observation(count=1))
    first = service.create_request(admin, request_data("first"), idempotency_key="first")
    assert first["lease"] is not None
    gpu_id = first["lease"]["gpu_ids"][0]
    queued = service.create_request(admin, request_data("second"), idempotency_key="second")
    assert queued["lease"] is None

    def illegal_duplicate(session) -> None:  # type: ignore[no-untyped-def]
        lease = Lease(
            id="illegal",
            request_id=queued["request"]["id"],
            actor_id=admin.id,
            project_id="project-a",
            state="HELD",
            issued_at=utcnow(),
            expires_at=utcnow() + timedelta(hours=1),
            last_heartbeat_at=utcnow(),
            issued_revision=1,
        )
        session.add(lease)
        session.flush()
        session.add(LeaseResource(lease_id=lease.id, gpu_id=gpu_id, active=True))
        session.flush()

    with pytest.raises(IntegrityError):
        service._write(illegal_duplicate)
