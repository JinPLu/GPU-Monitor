from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

from serverpilot.database import Database
from serverpilot.models import (
    AllocationRequest,
    Alert,
    AuditEvent,
    EndpointTelemetryCurrent,
    EndpointTelemetrySnapshot,
    GPUDevice,
    Lease,
    LeaseResource,
    TelemetryCurrent,
    TelemetrySnapshot,
)
from serverpilot.schemas import (
    ActorCreate,
    EndpointCreate,
    EndpointObservation,
    EndpointUpdate,
    LeaseObservedBind,
    ReservationCreate,
    RequestCreate,
    RetentionPrune,
    WorkloadProfileClaim,
    WorkloadProfileUpsert,
)
from serverpilot.service import ACTIVE_LEASE_STATES, ActorContext, BrokerError, BrokerService
from serverpilot.timeutil import utcnow
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
    with pytest.raises(BrokerError) as error:
        service.create_request(admin, request_data("unknown"), idempotency_key="unknown-1")
    assert error.value.code == "no_capacity"
    assert service.list_requests(admin)["data"] == []


def test_initialize_has_no_bootstrap_login_token(tmp_path: Path, inventory) -> None:
    broker = BrokerService(
        Database(
            f"sqlite:///{tmp_path / 'bootstrap.sqlite3'}", Path(__file__).resolve().parents[1]
        ),
        inventory,
    )
    broker.initialize()
    broker.initialize()
    assert broker.local_actor("local-agent").role == "allocator"


def test_write_propagates_first_database_busy_error_without_retry(service, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class LockedSession:
        execute_calls = 0
        rollback_calls = 0

        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, *_args):  # type: ignore[no-untyped-def]
            return None

        def execute(self, _statement):  # type: ignore[no-untyped-def]
            self.execute_calls += 1
            raise OperationalError("BEGIN IMMEDIATE", {}, Exception("database is locked"))

        def rollback(self) -> None:
            self.rollback_calls += 1

    locked = LockedSession()
    monkeypatch.setattr(service.database, "session", lambda: locked)

    with pytest.raises(OperationalError, match="database is locked"):
        service._write(lambda _session: None)

    assert locked.execute_calls == 1
    assert locked.rollback_calls == 1


def test_idempotent_request_and_stable_uuid_identity(service, admin) -> None:
    service.ingest_observation(observation())
    first = service.create_request(admin, request_data("idempotent"), idempotency_key="key-1")
    second = service.create_request(admin, request_data("idempotent"), idempotency_key="key-1")
    assert first == second
    assert first["lease"] is not None
    gpu = service.list_gpus(admin)["data"][0]
    assert gpu["id"] == f"endpoint-a:{gpu['gpu_uuid']}"
    assert gpu["state"] == "HELD"


def test_complete_observation_reconciles_active_gpu_set(service, admin) -> None:
    service.ingest_observation(observation(gpu_uuids=["GPU-old-0", "GPU-old-1", "GPU-stays"]))
    result = service.ingest_observation(observation(gpu_uuids=["GPU-new-0", "GPU-stays"]))

    snapshot = service.snapshot(admin)["data"]

    assert result["absent_gpu_count"] == 2
    assert [gpu["id"] for gpu in snapshot["gpus"]] == [
        "endpoint-a:GPU-new-0",
        "endpoint-a:GPU-stays",
    ]
    assert snapshot["summary"]["total_gpus"] == 2
    assert snapshot["endpoints"][0]["monitor"]["gpu_count"] == 2
    assert snapshot["endpoints"][0]["monitor"]["absent_gpu_count"] == 2
    assert snapshot["absent_gpu_ids"] == ["endpoint-a:GPU-old-0", "endpoint-a:GPU-old-1"]


def test_incomplete_observation_does_not_mark_prior_gpus_absent(service, admin) -> None:
    service.ingest_observation(observation(gpu_uuids=["GPU-old-0", "GPU-stays"]))
    result = service.ingest_observation(
        observation(gpu_uuids=["GPU-stays"], observation_complete=False)
    )

    snapshot = service.snapshot(admin)["data"]

    assert result["absent_gpu_count"] == 0
    assert result["observation_complete"] is False
    assert [gpu["id"] for gpu in snapshot["gpus"]] == [
        "endpoint-a:GPU-old-0",
        "endpoint-a:GPU-stays",
    ]


def test_observation_gpu_uuid_index_and_cuda_ordinal_must_be_unique() -> None:
    with pytest.raises(ValidationError, match="unique gpu_uuid"):
        observation(gpu_uuids=["GPU-dup", "GPU-dup"])

    base = observation(gpu_uuids=["GPU-a", "GPU-b"]).model_dump()
    base["gpus"][1]["gpu_index"] = base["gpus"][0]["gpu_index"]
    with pytest.raises(ValidationError, match="unique gpu_index"):
        EndpointObservation.model_validate(base)

    base = observation(gpu_uuids=["GPU-a", "GPU-b"]).model_dump()
    base["gpus"][1]["cuda_ordinal"] = base["gpus"][0]["cuda_ordinal"]
    with pytest.raises(ValidationError, match="unique cuda_ordinal"):
        EndpointObservation.model_validate(base)


def test_absent_gpu_keeps_lease_but_is_not_eligible(service, admin) -> None:
    service.ingest_observation(observation(gpu_uuids=["GPU-old", "GPU-new"]))
    exact_old = RequestCreate.model_validate(
        {
            "project_id": "project-a",
            "task_ref": "old-gpu",
            "purpose": "hold old GPU",
            "duration_seconds": 3600,
            "constraints": {
                "gpu_count": 1,
                "placement": "exact",
                "gpu_ids": ["endpoint-a:GPU-old"],
            },
        }
    )
    claimed_old = service.create_request(admin, exact_old, idempotency_key="old-gpu")
    assert claimed_old["lease"] is not None

    service.ingest_observation(observation(gpu_uuids=["GPU-new"]))
    claim_new = service.create_request(admin, request_data("new-gpu"), idempotency_key="new-gpu")
    with pytest.raises(BrokerError) as error:
        service.create_request(admin, request_data("no-more-gpus"), idempotency_key="no-more-gpus")
    snapshot = service.snapshot(admin)["data"]

    assert claim_new["lease"]["gpu_ids"] == ["endpoint-a:GPU-new"]
    assert error.value.code == "no_capacity"
    assert snapshot["gpus"][0]["id"] == "endpoint-a:GPU-new"
    assert any("endpoint-a:GPU-old" in lease["gpu_ids"] for lease in snapshot["leases"])


def test_absent_gpu_in_multi_gpu_lease_suppresses_all_executable_resources(service, admin) -> None:
    service.ingest_observation(observation(gpu_uuids=["GPU-old", "GPU-new"]))
    claimed = service.create_request(
        admin,
        request_data("two-gpu-lease", count=2),
        idempotency_key="two-gpu-lease",
    )
    assert claimed["lease"] is not None
    assert len(claimed["lease"]["resources"]) == 1

    service.ingest_observation(observation(gpu_uuids=["GPU-new"]))
    lease = service.list_leases(admin)["data"][0]
    snapshot_lease = service.snapshot(admin)["data"]["leases"][0]

    assert set(lease["gpu_ids"]) == {"endpoint-a:GPU-old", "endpoint-a:GPU-new"}
    assert lease["absent_gpu_ids"] == ["endpoint-a:GPU-old"]
    assert lease["resources"] == []
    assert snapshot_lease["resources"] == []


def test_incomplete_observation_preserves_unobserved_process_and_blocks_exact_claim(
    service, admin
) -> None:
    service.ingest_observation(
        observation(
            gpu_uuids=["GPU-old", "GPU-new"],
            processes=[process_for_gpu("GPU-old")],
        )
    )
    result = service.ingest_observation(
        observation(gpu_uuids=["GPU-new"], observation_complete=False)
    )

    gpus = {gpu["id"]: gpu for gpu in service.list_gpus(admin)["data"]}
    exact_old = RequestCreate.model_validate(
        {
            "project_id": "project-a",
            "task_ref": "old-incomplete",
            "purpose": "should remain blocked by preserved process",
            "duration_seconds": 3600,
            "constraints": {
                "gpu_count": 1,
                "placement": "exact",
                "gpu_ids": ["endpoint-a:GPU-old"],
            },
        }
    )
    with pytest.raises(BrokerError) as error:
        service.create_request(admin, exact_old, idempotency_key="old-incomplete")

    assert result["observation_complete"] is False
    assert gpus["endpoint-a:GPU-old"]["state"] == "BUSY_UNMANAGED"
    assert len(gpus["endpoint-a:GPU-old"]["processes"]) == 1
    assert error.value.code == "no_capacity"


def test_absent_gpu_reappearance_restores_presence(service, admin) -> None:
    service.ingest_observation(observation(gpu_uuids=["GPU-old", "GPU-stays"]))
    service.ingest_observation(observation(gpu_uuids=["GPU-stays"]))
    service.ingest_observation(observation(gpu_uuids=["GPU-old", "GPU-stays"]))

    snapshot = service.snapshot(admin)["data"]

    assert [gpu["id"] for gpu in snapshot["gpus"]] == [
        "endpoint-a:GPU-old",
        "endpoint-a:GPU-stays",
    ]
    assert snapshot["absent_gpu_ids"] == []
    assert snapshot["summary"]["total_gpus"] == 2


def test_stale_observation_is_ignored_without_mutating_latest_presence(service, admin) -> None:
    first_seen = utcnow()
    second_seen = first_seen + timedelta(seconds=10)
    service.ingest_observation(observation(gpu_uuids=["GPU-new"], observed_at=second_seen))
    before = service.snapshot(admin)

    result = service.ingest_observation(observation(gpu_uuids=["GPU-old"], observed_at=first_seen))
    after = service.snapshot(admin)

    assert result["ignored"] is True
    assert result["ignore_reason"] == "stale_observation"
    assert result["snapshot_revision"] == before["snapshot_revision"]
    assert after["snapshot_revision"] == before["snapshot_revision"]
    assert [gpu["id"] for gpu in after["data"]["gpus"]] == ["endpoint-a:GPU-new"]
    assert after["data"]["absent_gpu_ids"] == []


def test_explicit_reservation_rejects_absent_gpu_until_reappears(service, admin) -> None:
    service.ingest_observation(observation(gpu_uuids=["GPU-old", "GPU-new"]))
    service.ingest_observation(observation(gpu_uuids=["GPU-new"]))
    start_at = utcnow() + timedelta(hours=1)
    reservation = ReservationCreate(
        project_id="project-a",
        gpu_ids=["endpoint-a:GPU-old"],
        start_at=start_at,
        end_at=start_at + timedelta(hours=1),
        reason="future use of returning GPU",
    )

    with pytest.raises(BrokerError) as error:
        service.create_reservation(admin, reservation, idempotency_key="absent-reservation")
    assert error.value.code == "gpu_absent"

    service.ingest_observation(observation(gpu_uuids=["GPU-old", "GPU-new"]))
    created = service.create_reservation(
        admin,
        reservation,
        idempotency_key="present-reservation",
    )

    assert created["reservation"]["gpu_ids"] == ["endpoint-a:GPU-old"]


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
    assert first["lease"]["state"] == "HELD"
    request = first["request"]
    assert request["state"] == "LEASED"
    assert request["profile_id"] == "benchmark-2gpu"
    assert request["purpose"] == "approved benchmark evaluation"
    assert request["duration_seconds"] == 7200
    assert request["constraints"]["gpu_count"] == 2
    assert request["constraints"]["endpoint_ids"] == []
    events = service.list_events(admin)["data"]
    request_event = next(event for event in events if event["action"] == "request.created")
    assert request_event["summary"]["profile_id"] == "benchmark-2gpu"


def test_routine_claim_fails_immediately_and_can_be_retried_when_capacity_arrives(
    service, admin
) -> None:
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
    with pytest.raises(BrokerError) as error:
        service.claim_workload_profile(
            admin,
            "queued-eval",
            WorkloadProfileClaim(task_ref="queued-run"),
            idempotency_key="queued-claim",
        )
    assert error.value.code == "no_capacity"
    assert service.list_requests(admin)["data"] == []

    service.ingest_observation(observation(count=1))
    claimed = service.claim_workload_profile(
        admin,
        "queued-eval",
        WorkloadProfileClaim(task_ref="retried-run"),
        idempotency_key="retried-claim",
    )
    assert claimed["request"]["state"] == "LEASED"
    assert claimed["lease"]["state"] == "HELD"


def test_unstarted_held_claim_is_retained_after_deprecated_startup_grace(
    tmp_path: Path, inventory
) -> None:
    broker = BrokerService(
        Database(
            f"sqlite:///{tmp_path / 'startup-grace.sqlite3'}", Path(__file__).resolve().parents[1]
        ),
        inventory.model_copy(update={"held_lease_startup_grace_seconds": 1}),
    )
    broker.initialize()
    admin = broker.local_actor("grace-agent")
    broker.ingest_observation(observation(count=1))

    first = broker.create_request(
        admin,
        request_data("grace-first"),
        idempotency_key="grace-first",
        activate_if_allocated=True,
    )
    assert first["lease"] is not None
    assert first["lease"]["state"] == "HELD"
    with pytest.raises(BrokerError) as error:
        broker.create_request(
            admin,
            request_data("grace-second"),
            idempotency_key="grace-second",
            activate_if_allocated=True,
        )
    assert error.value.code == "no_capacity"

    first_lease_id = first["lease"]["id"]

    def age_held_lease(session) -> None:  # type: ignore[no-untyped-def]
        lease = session.get(Lease, first_lease_id)
        assert lease is not None
        issued_at = utcnow() - timedelta(seconds=2)
        lease.issued_at = issued_at
        lease.last_heartbeat_at = issued_at

    broker._write(age_held_lease)
    broker.reconcile(admin)

    leases = {lease["id"]: lease for lease in broker.list_leases(admin)["data"]}
    assert leases[first_lease_id]["state"] == "HELD"
    assert len(leases) == 1
    requests = {request["task_ref"]: request for request in broker.list_requests(admin)["data"]}
    assert requests["grace-first"]["state"] == "LEASED"
    assert "grace-second" not in requests


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
    claimed = service.create_request(
        admin, request_data("renewal-window"), idempotency_key="renewal-window"
    )
    assert claimed["lease"] is not None

    with pytest.raises(BrokerError) as error:
        service.renew_lease(admin, claimed["lease"]["id"], idempotency_key="renewal-conflict")
    assert error.value.code == "lease_renewal_conflicts_with_reservation"


def test_reservation_cancellation_is_limited_to_creating_actor(service) -> None:
    service.ingest_observation(observation(count=1))
    owner = service.local_actor("reservation-owner")
    other = service.local_actor("reservation-other")
    gpu_id = service.list_gpus(owner)["data"][0]["id"]
    start_at = utcnow() + timedelta(minutes=5)
    created = service.create_reservation(
        owner,
        ReservationCreate(
            project_id="project-a",
            gpu_ids=[gpu_id],
            start_at=start_at,
            end_at=start_at + timedelta(hours=1),
            reason="owner-only future reservation",
        ),
        idempotency_key="owner-reservation",
    )
    with pytest.raises(BrokerError) as error:
        service.cancel_reservation(
            other,
            created["reservation"]["id"],
            idempotency_key="cross-actor-cancel",
        )
    assert error.value.code == "reservation_forbidden"
    cancelled = service.cancel_reservation(
        owner,
        created["reservation"]["id"],
        idempotency_key="owner-cancel",
    )
    assert cancelled["reservation"]["state"] == "CANCELLED"


def test_gang_all_or_nothing_and_no_partial_write(service, admin) -> None:
    service.ingest_observation(observation(count=3))
    first = service.create_request(admin, request_data("gang-a", count=2), idempotency_key="gang-a")
    with pytest.raises(BrokerError) as error:
        service.create_request(admin, request_data("gang-b", count=2), idempotency_key="gang-b")
    assert first["lease"] is not None
    assert len(first["lease"]["gpu_ids"]) == 2
    assert error.value.code == "no_capacity"
    leases = service.list_leases(admin)["data"]
    assert (
        sum(len(lease["gpu_ids"]) for lease in leases if lease["state"] in ACTIVE_LEASE_STATES) == 2
    )


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
    with pytest.raises(BrokerError) as cpu_error:
        service.create_request(admin, too_much_cpu, idempotency_key="absolute-cpu")
    assert cpu_error.value.code == "no_capacity"

    too_much_memory = RequestCreate.model_validate(
        {
            "project_id": "project-a",
            "task_ref": "needs-absolute-memory",
            "purpose": "request more free system memory than the endpoint currently has",
            "constraints": {"gpu_count": 1, "min_available_memory_mib": 200 * 1024},
        }
    )
    with pytest.raises(BrokerError) as memory_error:
        service.create_request(admin, too_much_memory, idempotency_key="absolute-memory")
    assert memory_error.value.code == "no_capacity"

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


def test_failed_claims_are_not_queued_when_capacity_arrives(service, admin) -> None:
    for task_ref, project_id in (
        ("story-a", "project-a"),
        ("story-b", "project-a"),
        ("project-b-task", "project-b"),
    ):
        with pytest.raises(BrokerError) as error:
            service.create_request(
                admin,
                request_data(task_ref, project_id=project_id),
                idempotency_key=task_ref,
            )
        assert error.value.code == "no_capacity"
    service.ingest_observation(observation(count=3))
    assert service.list_requests(admin)["data"] == []
    assert service.list_leases(admin)["data"] == []


def test_endpoint_identity_is_enforced(service, admin) -> None:
    service.ingest_observation(observation(count=4))
    created = service.create_endpoint(
        admin,
        EndpointCreate(
            id="endpoint-new",
            host="127.0.0.1",
            port=2203,
            ssh_user="gpu",
            workspace_path="/srv/project-new",
            project_ids=["project-a"],
        ),
        idempotency_key="endpoint-new",
    )
    assert created["endpoint"]["id"] == "endpoint-new"
    assert created["endpoint"]["workspace_path"] == "/srv/project-new"
    updated = service.update_endpoint(
        admin,
        "endpoint-new",
        EndpointUpdate(
            ssh_user="gpu-updated",
            workspace_path="/srv/project-new-updated",
            observation_profile="server-script-v1",
        ),
        idempotency_key="endpoint-update",
    )
    assert updated["endpoint"]["ssh_user"] == "gpu-updated"
    assert updated["endpoint"]["observation_profile"] == "server-script-v1"
    assert updated["endpoint"]["workspace_path"] == "/srv/project-new-updated"
    paused = service.pause_endpoint(admin, "endpoint-new", idempotency_key="endpoint-pause")
    assert paused["endpoint"]["lifecycle_state"] == "draining"
    assert "endpoint-new" in {endpoint.id for endpoint in service.collector_endpoints()}
    resumed = service.resume_endpoint(admin, "endpoint-new", idempotency_key="endpoint-resume")
    assert resumed["endpoint"]["lifecycle_state"] == "active"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        EndpointUpdate.model_validate({"host": "127.0.0.1"})
    with pytest.raises(BrokerError) as error:
        service.create_endpoint(
            admin,
            EndpointCreate(
                id="endpoint-new",
                host="127.0.0.1",
                port=2299,
                ssh_user="gpu",
                workspace_path="/srv/project-new",
                project_ids=["project-a"],
            ),
            idempotency_key="endpoint-move",
        )
    assert error.value.code == "endpoint_exists"


def test_endpoint_inventory_mutations_do_not_require_project_membership(service, admin) -> None:
    desktop_actor = ActorContext(
        id=admin.id,
        role="allocator",
        project_ids=frozenset(),
    )
    created = service.create_endpoint(
        desktop_actor,
        EndpointCreate(
            id="desktop-ownerless",
            host="127.0.0.1",
            port=2298,
            ssh_user="gpu",
            workspace_path="/srv/desktop-ownerless",
        ),
        idempotency_key="desktop-ownerless-create",
    )

    assert created["endpoint"]["owner_project_id"] is None
    updated = service.update_endpoint(
        desktop_actor,
        "endpoint-a",
        EndpointUpdate(labels=["desktop-updated"]),
        idempotency_key="desktop-update-without-project",
    )
    assert updated["endpoint"]["labels"] == ["desktop-updated"]


def test_claim_auto_creates_project_without_extra_endpoint_scope(service, admin) -> None:
    service.ingest_observation(observation(count=1))
    claimed = service.create_request(
        admin,
        request_data("cross-project-claim", project_id="storyboard"),
        idempotency_key="storyboard-claim",
        activate_if_allocated=True,
    )
    assert claimed["lease"] is not None
    assert claimed["request"]["state"] == "LEASED"
    assert claimed["lease"]["state"] == "HELD"
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
    assert bound["lease"]["state"] == "ACTIVE"
    assert bound["lease"]["workloads"][0]["run_id"] == (f"explicit:lease:{claimed['lease']['id']}")
    assert len(bound["lease"]["workloads"][0]["process_keys"]) == 1
    assert service.list_requests(admin)["data"][0]["state"] == "ACTIVE"

    gpu = service.list_gpus(admin)["data"][0]
    assert gpu["state"] == "RUNNING_MANAGED"
    assert gpu["processes"][0]["process_key"]
    board = service.coordination(admin)["data"]
    assert board["summary"]["active_agents"] == 1
    assert board["summary"]["managed_running_gpus"] == 1
    assert board["servers"][0]["consumers"][0]["agent_name"] == admin.id
    assert board["leases"][0]["activity"] == "running"
    assert board["agents"][0]["managed_running_gpus"] == 1


def test_collector_auto_binds_new_process_to_its_exact_workload_lease(service, admin) -> None:
    service.ingest_observation(observation(count=2))
    claimed = service.create_request(
        admin,
        request_data("auto-observed-run"),
        idempotency_key="auto-observed-claim",
        activate_if_allocated=True,
    )
    lease_id = claimed["lease"]["id"]
    claimed_gpu_id = claimed["lease"]["gpu_ids"][0]
    gpus = service.list_gpus(admin)["data"]
    claimed_gpu_uuid = next(gpu["gpu_uuid"] for gpu in gpus if gpu["id"] == claimed_gpu_id)
    other_gpu_uuid = next(gpu["gpu_uuid"] for gpu in gpus if gpu["id"] != claimed_gpu_id)

    service.ingest_observation(
        observation(
            count=2,
            processes=[
                process_for_gpu(claimed_gpu_uuid, pid=4101),
                process_for_gpu(other_gpu_uuid, pid=4102),
            ],
        )
    )

    lease = next(item for item in service.list_leases(admin)["data"] if item["id"] == lease_id)
    assert lease["state"] == "ACTIVE"
    snapshot_lease = next(
        item for item in service.snapshot(admin)["data"]["leases"] if item["id"] == lease_id
    )
    assert snapshot_lease["runtime_state"] == "RUNNING"
    assert lease["workloads"][0]["run_id"] == f"collector:lease:{lease_id}"
    assert lease["workloads"][0]["process_keys"] == [
        next(
            gpu["processes"][0]["process_key"]
            for gpu in service.list_gpus(admin)["data"]
            if gpu["id"] == claimed_gpu_id
        )
    ]
    states = {gpu["gpu_uuid"]: gpu["state"] for gpu in service.list_gpus(admin)["data"]}
    assert states[claimed_gpu_uuid] == "RUNNING_MANAGED"
    assert states[other_gpu_uuid] == "BUSY_UNMANAGED"
    assert service.list_requests(admin)["data"][0]["state"] == "ACTIVE"


def test_bound_workload_clears_historical_keepalive_error_without_mutating_lease(
    service, admin
) -> None:
    service.ingest_observation(observation(count=1))
    gpu_uuid = service.list_gpus(admin)["data"][0]["gpu_uuid"]
    service.set_keepalive_error("endpoint-a", ["endpoint-a:" + gpu_uuid], "start failed")
    claimed = service.create_request(
        admin,
        request_data("workload-after-keepalive-error"),
        idempotency_key="workload-after-keepalive-error-claim",
        activate_if_allocated=True,
    )
    lease_id = claimed["lease"]["id"]
    process = process_for_gpu(gpu_uuid, pid=4401)

    service.ingest_observation(observation(count=1, processes=[process]))

    gpu = service.list_gpus(admin)["data"][0]
    assert gpu["keepalive"]["actual"] == "OFF"
    assert gpu["keepalive"]["reason"] is None
    assert gpu["state"] == "RUNNING_MANAGED"
    lease = next(item for item in service.list_leases(admin)["data"] if item["id"] == lease_id)
    assert lease["state"] == "ACTIVE"
    assert lease["gpu_ids"] == [gpu["id"]]
    assert lease["workloads"][0]["process_keys"] == [gpu["processes"][0]["process_key"]]


def test_unbound_or_unknown_process_does_not_clear_keepalive_error(service, admin) -> None:
    service.ingest_observation(observation(count=1))
    gpu_uuid = service.list_gpus(admin)["data"][0]["gpu_uuid"]
    gpu_id = "endpoint-a:" + gpu_uuid
    service.set_keepalive_error("endpoint-a", [gpu_id], "ordinary keepalive failure")

    service.ingest_observation(
        observation(count=1, processes=[process_for_gpu(gpu_uuid, pid=4402)])
    )

    gpu = service.list_gpus(admin)["data"][0]
    assert gpu["state"] == "BUSY_UNMANAGED"
    assert gpu["keepalive"]["actual"] == "ERROR"
    assert gpu["keepalive"]["reason"] == "ordinary keepalive failure"


def test_assigned_workload_clears_error_after_process_turnover(service, admin) -> None:
    service.ingest_observation(observation(count=1))
    claimed = service.create_request(
        admin,
        request_data("unknown-after-bound"),
        idempotency_key="unknown-after-bound-claim",
        activate_if_allocated=True,
    )
    gpu_uuid = service.list_gpus(admin)["data"][0]["gpu_uuid"]
    initial = process_for_gpu(gpu_uuid, pid=4403)
    service.ingest_observation(observation(count=1, processes=[initial]))
    service.bind_observed_workload(
        admin,
        claimed["lease"]["id"],
        LeaseObservedBind(run_id="known-workload"),
        idempotency_key="known-workload-bind",
    )
    service.set_keepalive_error(
        "endpoint-a",
        ["endpoint-a:" + gpu_uuid],
        "ordinary keepalive failure",
    )

    replacement = process_for_gpu(gpu_uuid, pid=4404)
    service.ingest_observation(observation(count=1, processes=[replacement]))

    gpu = service.list_gpus(admin)["data"][0]
    assert gpu["state"] == "RUNNING_MANAGED"
    assert gpu["keepalive"]["actual"] == "OFF"
    assert gpu["keepalive"]["reason"] is None


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


def test_observed_workload_binding_survives_continuous_invisible_pid_metadata(
    service, admin
) -> None:
    """A namespace-hidden PID must not acquire a new identity every collection."""

    service.ingest_observation(observation(count=1))
    claimed = service.create_request(
        admin,
        request_data("namespace-hidden-run"),
        idempotency_key="namespace-hidden-claim",
        activate_if_allocated=True,
    )
    assert claimed["lease"] is not None
    gpu_uuid = service.list_gpus(admin)["data"][0]["gpu_uuid"]
    first_seen = utcnow() - timedelta(minutes=3)
    initial_process = process_for_gpu(gpu_uuid).model_copy(
        update={"username": None, "process_started_at": first_seen}
    )
    service.ingest_observation(observation(count=1, processes=[initial_process]))
    service.bind_observed_workload(
        admin,
        claimed["lease"]["id"],
        LeaseObservedBind(run_id="namespace-hidden-run-1"),
        idempotency_key="namespace-hidden-bind",
    )
    original_key = service.list_gpus(admin)["data"][0]["processes"][0]["process_key"]

    # Missing `ps` metadata makes the collector use each observation time as
    # its fallback start time. Continuous GPU/PID/boot observations still
    # identify the already-active process.
    later_sample = initial_process.model_copy(
        update={"process_started_at": first_seen + timedelta(seconds=10)}
    )
    service.ingest_observation(observation(count=1, processes=[later_sample]))

    gpu = service.list_gpus(admin)["data"][0]
    assert gpu["state"] == "RUNNING_MANAGED"
    assert gpu["processes"][0]["observations"] == 2
    assert gpu["processes"][0]["process_key"] == original_key
    assert gpu["lease"]["workloads"][0]["process_keys"] == [original_key]


def test_multi_gpu_task_stays_running_during_mixed_worker_turnover(
    service, admin
) -> None:
    service.ingest_observation(observation(count=4))
    claimed = service.create_request(
        admin,
        request_data("bridge-to-lpt", count=4, project_id="agent"),
        idempotency_key="bridge-to-lpt-claim",
        activate_if_allocated=True,
    )
    lease_id = claimed["lease"]["id"]
    gpu_uuids = [
        gpu["gpu_uuid"]
        for gpu in service.list_gpus(admin)["data"]
        if gpu["id"] in claimed["lease"]["gpu_ids"]
    ]
    first_cohort = [
        process_for_gpu(gpu_uuid, pid=4_100 + index) for index, gpu_uuid in enumerate(gpu_uuids)
    ]
    service.ingest_observation(observation(count=4, processes=first_cohort))

    replacement_cohort = [
        process_for_gpu(gpu_uuid, pid=5_100 + index) for index, gpu_uuid in enumerate(gpu_uuids)
    ]
    # A bridge-to-LPT hand-off can have old and new workers overlap on some
    # lanes while other lanes have already switched or are still on bridge.
    mixed_cohort = [
        first_cohort[0],
        replacement_cohort[0],
        replacement_cohort[1],
        first_cohort[2],
        first_cohort[3],
        replacement_cohort[3],
    ]
    service.ingest_observation(observation(count=4, processes=mixed_cohort))
    service.ingest_observation(observation(count=4, processes=mixed_cohort))

    lease = next(lease for lease in service.list_leases(admin)["data"] if lease["id"] == lease_id)
    assert lease["state"] == "ACTIVE"
    lease_gpus = [
        gpu
        for gpu in service.list_gpus(admin)["data"]
        if gpu["id"] in claimed["lease"]["gpu_ids"]
    ]
    assert {gpu["state"] for gpu in lease_gpus} == {"RUNNING_MANAGED"}
    assert {
        gpu["gpu_uuid"]: {process["pid"] for process in gpu["processes"]}
        for gpu in lease_gpus
    } == {
        gpu_uuids[0]: {4_100, 5_100},
        gpu_uuids[1]: {5_101},
        gpu_uuids[2]: {4_102},
        gpu_uuids[3]: {4_103, 5_103},
    }
    assert not any(
        alert["active"] and alert["resource_id"] == lease_id
        for alert in service.list_alerts(admin)["data"]
    )


def test_task_stays_running_during_overlapping_worker_turnover(
    service, admin
) -> None:
    service.ingest_observation(observation(count=1))
    claimed = service.create_request(
        admin,
        request_data("overlapping-routine-retry", project_id="agent"),
        idempotency_key="overlapping-routine-retry-claim",
        activate_if_allocated=True,
    )
    gpu_uuid = service.list_gpus(admin)["data"][0]["gpu_uuid"]
    bound_process = process_for_gpu(gpu_uuid, pid=4_150)
    service.ingest_observation(observation(count=1, processes=[bound_process]))
    replacement = process_for_gpu(gpu_uuid, pid=5_150)

    # The old and new workers overlap while a task transitions between
    # execution stages. Process identities are observations, not ownership.
    service.ingest_observation(observation(count=1, processes=[bound_process, replacement]))
    assert service.list_gpus(admin)["data"][0]["state"] == "RUNNING_MANAGED"

    service.ingest_observation(observation(count=1, processes=[bound_process, replacement]))
    assert service.list_gpus(admin)["data"][0]["state"] == "RUNNING_MANAGED"
    assert (
        next(
            lease
            for lease in service.list_leases(admin)["data"]
            if lease["id"] == claimed["lease"]["id"]
        )["state"]
        == "ACTIVE"
    )


def test_worker_turnover_then_empty_gpu_keeps_task_lease_active(
    service, admin
) -> None:
    service.ingest_observation(observation(count=1))
    claimed = service.create_request(
        admin,
        request_data("empty-routine-retry-window", project_id="agent"),
        idempotency_key="empty-routine-retry-window-claim",
        activate_if_allocated=True,
    )
    lease_id = claimed["lease"]["id"]
    gpu_uuid = service.list_gpus(admin)["data"][0]["gpu_uuid"]
    initial = process_for_gpu(gpu_uuid, pid=4_175)
    service.ingest_observation(observation(count=1, processes=[initial]))
    replacement = process_for_gpu(gpu_uuid, pid=5_175)
    service.ingest_observation(observation(count=1, processes=[initial, replacement]))
    service.ingest_observation(observation(count=1, processes=[initial, replacement]))
    assert service.list_gpus(admin)["data"][0]["state"] == "RUNNING_MANAGED"

    service.ingest_observation(observation(count=1, processes=[]))

    gpu = service.list_gpus(admin)["data"][0]
    assert gpu["state"] == "LEASED_IDLE"
    assert gpu["lease"]["id"] == lease_id
    assert gpu["lease"]["state"] == "ACTIVE"
    assert not any(
        alert["active"] and alert["resource_id"] == lease_id
        for alert in service.list_alerts(admin)["data"]
    )


def test_workload_lease_process_restart_preserves_task_assignment(service, admin) -> None:
    service.ingest_observation(observation(count=1))
    claimed = service.create_request(
        admin,
        request_data("strict-retry"),
        idempotency_key="strict-retry-claim",
        activate_if_allocated=True,
    )
    gpu_uuid = service.list_gpus(admin)["data"][0]["gpu_uuid"]
    service.ingest_observation(
        observation(count=1, processes=[process_for_gpu(gpu_uuid, pid=4_201)])
    )
    replacement = process_for_gpu(gpu_uuid, pid=5_201)
    service.ingest_observation(observation(count=1, processes=[replacement]))
    service.ingest_observation(observation(count=1, processes=[replacement]))

    assert service.list_gpus(admin)["data"][0]["state"] == "RUNNING_MANAGED"
    assert (
        next(
            lease
            for lease in service.list_leases(admin)["data"]
            if lease["id"] == claimed["lease"]["id"]
        )["state"]
        == "ACTIVE"
    )


def test_explicit_workload_binding_process_restart_preserves_task_assignment(service, admin) -> None:
    service.ingest_observation(observation(count=1))
    claimed = service.create_request(
        admin,
        request_data("explicit-agent-run", project_id="agent"),
        idempotency_key="explicit-agent-run-claim",
        activate_if_allocated=True,
    )
    gpu_uuid = service.list_gpus(admin)["data"][0]["gpu_uuid"]
    service.ingest_observation(
        observation(count=1, processes=[process_for_gpu(gpu_uuid, pid=4_251)])
    )
    service.bind_observed_workload(
        admin,
        claimed["lease"]["id"],
        LeaseObservedBind(run_id="strict-run-id"),
        idempotency_key="explicit-agent-run-bind",
    )
    replacement = process_for_gpu(gpu_uuid, pid=5_251)
    service.ingest_observation(observation(count=1, processes=[replacement]))
    service.ingest_observation(observation(count=1, processes=[replacement]))

    assert service.list_gpus(admin)["data"][0]["state"] == "RUNNING_MANAGED"


def test_default_explicit_binding_does_not_block_process_turnover(service, admin) -> None:
    service.ingest_observation(observation(count=1))
    claimed = service.create_request(
        admin,
        request_data("default-explicit-agent-run", project_id="agent"),
        idempotency_key="default-explicit-agent-run-claim",
        activate_if_allocated=True,
    )
    gpu_uuid = service.list_gpus(admin)["data"][0]["gpu_uuid"]
    service.ingest_observation(
        observation(count=1, processes=[process_for_gpu(gpu_uuid, pid=4_261)])
    )
    service.bind_observed_workload(
        admin,
        claimed["lease"]["id"],
        LeaseObservedBind(),
        idempotency_key="default-explicit-agent-run-bind",
    )
    replacement = process_for_gpu(gpu_uuid, pid=5_261)
    service.ingest_observation(observation(count=1, processes=[replacement]))
    service.ingest_observation(observation(count=1, processes=[replacement]))

    lease = next(
        lease
        for lease in service.list_leases(admin)["data"]
        if lease["id"] == claimed["lease"]["id"]
    )
    assert lease["workloads"][0]["run_id"] == f"explicit:lease:{claimed['lease']['id']}"
    assert lease["state"] == "ACTIVE"


def test_incomplete_process_observation_does_not_change_task_assignment(service, admin) -> None:
    service.ingest_observation(observation(count=1))
    claimed = service.create_request(
        admin,
        request_data("incomplete-routine-retry", project_id="agent"),
        idempotency_key="incomplete-routine-retry-claim",
        activate_if_allocated=True,
    )
    gpu_uuid = service.list_gpus(admin)["data"][0]["gpu_uuid"]
    service.ingest_observation(
        observation(count=1, processes=[process_for_gpu(gpu_uuid, pid=4_301)])
    )
    replacement = process_for_gpu(gpu_uuid, pid=5_301)
    service.ingest_observation(
        observation(
            count=1,
            processes=[replacement],
            observation_complete=False,
        )
    )
    service.ingest_observation(
        observation(
            count=1,
            processes=[replacement],
            observation_complete=False,
        )
    )

    assert service.list_gpus(admin)["data"][0]["state"] == "RUNNING_MANAGED"
    assert (
        next(
            lease
            for lease in service.list_leases(admin)["data"]
            if lease["id"] == claimed["lease"]["id"]
        )["state"]
        == "ACTIVE"
    )


def test_namespace_hidden_pid_reuse_remains_an_observed_task_process(service, admin) -> None:
    """A reused PID changes process telemetry without changing the task assignment."""

    service.ingest_observation(observation(count=1))
    claimed = service.create_request(
        admin,
        request_data("namespace-gap-run"),
        idempotency_key="namespace-gap-claim",
        activate_if_allocated=True,
    )
    assert claimed["lease"] is not None
    gpu_uuid = service.list_gpus(admin)["data"][0]["gpu_uuid"]
    first_seen = utcnow() - timedelta(minutes=3)
    initial_process = process_for_gpu(gpu_uuid).model_copy(
        update={"username": None, "process_started_at": first_seen}
    )
    service.ingest_observation(observation(count=1, processes=[initial_process]))
    service.bind_observed_workload(
        admin,
        claimed["lease"]["id"],
        LeaseObservedBind(run_id="namespace-gap-run-1"),
        idempotency_key="namespace-gap-bind",
    )

    # One complete observation without the process closes the continuity
    # window. The same PID after that gap must receive a new identity.
    service.ingest_observation(observation(count=1, processes=[]))
    replacement = initial_process.model_copy(
        update={"process_started_at": first_seen + timedelta(seconds=30)}
    )
    service.ingest_observation(observation(count=1, processes=[replacement]))
    assert service.list_gpus(admin)["data"][0]["state"] == "RUNNING_MANAGED"

    repeated_replacement = replacement.model_copy(
        update={"process_started_at": first_seen + timedelta(seconds=40)}
    )
    service.ingest_observation(observation(count=1, processes=[repeated_replacement]))
    assert service.list_gpus(admin)["data"][0]["state"] == "RUNNING_MANAGED"


def test_initialize_normalizes_legacy_process_attribution_conflict(
    service, admin
) -> None:
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

    lease_id = claimed["lease"]["id"]

    def seed_legacy_conflict(session) -> None:  # type: ignore[no-untyped-def]
        now = utcnow()
        lease = session.get(Lease, lease_id)
        assert lease is not None
        lease.state = "CONFLICT"
        session.add(
            Alert(
                id="legacy-process-attribution-conflict",
                alert_type="lease_process_conflict",
                severity="critical",
                resource_type="lease",
                resource_id=lease_id,
                message="legacy PID attribution conflict",
                active=True,
                first_seen_at=now,
                last_seen_at=now,
                acknowledged_at=None,
                acknowledged_by=None,
            )
        )

    service._write(seed_legacy_conflict)
    service.initialize()

    gpu = service.list_gpus(admin)["data"][0]
    assert gpu["state"] == "RUNNING_MANAGED"
    assert gpu["lease"]["state"] == "ACTIVE"
    assert not any(
        alert["active"] and alert["resource_id"] == lease_id
        for alert in service.list_alerts(admin)["data"]
    )
    assert any(
        event["action"] == "lease.conflict_resolved"
        and event["resource_id"] == lease_id
        and event["summary"].get("source") == "retired_process_attribution_policy"
        for event in service.list_events(admin, limit=1000)["data"]
    )


def test_endpoint_operator_can_release_empty_conflicted_lease_after_fresh_observation(
    service, admin
) -> None:
    service.ingest_observation(observation(count=1))
    claimed = service.create_request(
        admin,
        request_data("clear-empty-conflict"),
        idempotency_key="clear-empty-conflict-claim",
        activate_if_allocated=True,
    )
    lease_id = claimed["lease"]["id"]
    gpu_uuid = service.list_gpus(admin)["data"][0]["gpu_uuid"]
    service.ingest_observation(observation(count=1, processes=[process_for_gpu(gpu_uuid)]))

    def seed_legacy_conflict(session) -> None:  # type: ignore[no-untyped-def]
        now = utcnow()
        lease = session.get(Lease, lease_id)
        assert lease is not None
        lease.state = "CONFLICT"
        session.add(
            Alert(
                id="legacy-empty-release-conflict",
                alert_type="lease_process_conflict",
                severity="critical",
                resource_type="lease",
                resource_id=lease_id,
                message="legacy PID attribution conflict",
                active=True,
                first_seen_at=now,
                last_seen_at=now,
                acknowledged_at=None,
                acknowledged_by=None,
            )
        )

    service._write(seed_legacy_conflict)

    barrier = utcnow()
    service.ingest_observation(observation(count=1, processes=[]))
    released = service.release_empty_conflicted_lease(
        admin,
        "endpoint-a",
        lease_id,
        observation_not_before=barrier,
        idempotency_key="clear-empty-conflict-release",
    )

    assert released["released"] is True
    assert released["lease"]["state"] == "RELEASED"
    assert service.list_gpus(admin)["data"][0]["state"] == "AVAILABLE"
    assert not any(
        alert["active"] and alert["resource_id"] == lease_id
        for alert in service.list_alerts(admin)["data"]
    )


def test_owner_can_release_legacy_conflict_while_process_is_observed(service, admin) -> None:
    service.ingest_observation(observation(count=1))
    claimed = service.create_request(
        admin,
        request_data("keep-conflict"),
        idempotency_key="keep-conflict-claim",
        activate_if_allocated=True,
    )
    lease_id = claimed["lease"]["id"]
    gpu_uuid = service.list_gpus(admin)["data"][0]["gpu_uuid"]
    service.ingest_observation(observation(count=1, processes=[process_for_gpu(gpu_uuid)]))

    def seed_legacy_conflict(session) -> None:  # type: ignore[no-untyped-def]
        now = utcnow()
        lease = session.get(Lease, lease_id)
        assert lease is not None
        lease.state = "CONFLICT"
        session.add(
            Alert(
                id="legacy-owner-release-conflict",
                alert_type="lease_process_conflict",
                severity="critical",
                resource_type="lease",
                resource_id=lease_id,
                message="legacy PID attribution conflict",
                active=True,
                first_seen_at=now,
                last_seen_at=now,
                acknowledged_at=None,
                acknowledged_by=None,
            )
        )

    service._write(seed_legacy_conflict)
    barrier = utcnow() - timedelta(seconds=1)

    with pytest.raises(BrokerError) as error:
        service.release_empty_conflicted_lease(
            admin,
            "endpoint-a",
            lease_id,
            observation_not_before=barrier,
            idempotency_key="keep-conflict-release",
    )

    assert error.value.code == "conflict_process_present"
    gpu = service.list_gpus(admin)["data"][0]
    assert gpu["state"] == "RUNNING_MANAGED"
    assert gpu["lease"]["state"] == "CONFLICT"

    released = service.release_lease(
        admin,
        lease_id,
        reason="workload completed",
        idempotency_key="release-legacy-conflict-with-process",
    )
    assert released["lease"]["state"] == "RELEASED"
    assert not any(
        alert["active"] and alert["resource_id"] == lease_id
        for alert in service.list_alerts(admin)["data"]
    )
    assert service.list_gpus(admin)["data"][0]["state"] == "BUSY_UNMANAGED"


def test_initialize_resolves_stale_alerts_for_terminal_lease(service, admin) -> None:
    service.ingest_observation(observation(count=1))
    claimed = service.create_request(
        admin,
        request_data("startup-alert-repair"),
        idempotency_key="startup-alert-repair-claim",
        activate_if_allocated=True,
    )
    lease_id = claimed["lease"]["id"]
    service.release_lease(
        admin,
        lease_id,
        reason="workload completed",
        idempotency_key="startup-alert-repair-release",
    )

    def seed_stale_alerts(session) -> None:  # type: ignore[no-untyped-def]
        now = utcnow()
        for alert_type in ("lease_process_conflict", "orphaned_busy"):
            session.add(
                Alert(
                    id=f"startup-{alert_type}",
                    alert_type=alert_type,
                    severity="critical",
                    resource_type="lease",
                    resource_id=lease_id,
                    message="stale test alert",
                    active=True,
                    first_seen_at=now,
                    last_seen_at=now,
                    acknowledged_at=None,
                    acknowledged_by=None,
                )
            )

    service._write(seed_stale_alerts)
    service.initialize()

    repaired = [
        alert for alert in service.list_alerts(admin)["data"] if alert["resource_id"] == lease_id
    ]
    assert {alert["type"] for alert in repaired} == {
        "lease_process_conflict",
        "orphaned_busy",
    }
    assert all(alert["active"] is False for alert in repaired)


def test_reconcile_resolves_alerts_for_lease_without_active_resources(service, admin) -> None:
    service.ingest_observation(observation(count=1))
    claimed = service.create_request(
        admin,
        request_data("resource-alert-repair"),
        idempotency_key="resource-alert-repair-claim",
        activate_if_allocated=True,
    )
    lease_id = claimed["lease"]["id"]

    def orphan_alerts_from_resources(session) -> None:  # type: ignore[no-untyped-def]
        now = utcnow()
        resources = session.scalars(
            select(LeaseResource).where(LeaseResource.lease_id == lease_id)
        ).all()
        assert resources
        for resource in resources:
            resource.active = False
            resource.released_at = now
        for alert_type in ("lease_process_conflict", "orphaned_busy"):
            session.add(
                Alert(
                    id=f"reconcile-{alert_type}",
                    alert_type=alert_type,
                    severity="critical",
                    resource_type="lease",
                    resource_id=lease_id,
                    message="stale test alert",
                    active=True,
                    first_seen_at=now,
                    last_seen_at=now,
                    acknowledged_at=None,
                    acknowledged_by=None,
                )
            )

    service._write(orphan_alerts_from_resources)
    service.reconcile(admin)

    repaired = [
        alert for alert in service.list_alerts(admin)["data"] if alert["resource_id"] == lease_id
    ]
    assert {alert["type"] for alert in repaired} == {
        "lease_process_conflict",
        "orphaned_busy",
    }
    assert all(alert["active"] is False for alert in repaired)


def test_endpoint_operator_can_release_empty_idle_workload_lease(service, admin) -> None:
    service.ingest_observation(observation(count=1))
    claimed = service.create_request(
        admin,
        request_data("clear-idle-lease"),
        idempotency_key="clear-idle-lease-claim",
        activate_if_allocated=True,
    )
    lease_id = claimed["lease"]["id"]
    assert service.list_gpus(admin)["data"][0]["state"] == "HELD"

    barrier = utcnow()
    service.ingest_observation(observation(count=1, processes=[]))
    released = service.release_empty_conflicted_lease(
        admin,
        "endpoint-a",
        lease_id,
        observation_not_before=barrier,
        idempotency_key="clear-idle-lease-release",
    )

    assert released["released"] is True
    assert released["lease"]["state"] == "RELEASED"
    assert service.list_gpus(admin)["data"][0]["state"] == "AVAILABLE"


def test_process_and_stale_telemetry_block_admission(service, admin) -> None:
    service.ingest_observation(
        observation(count=1, processes=[process_for_gpu("GPU-endpoint-a-0")])
    )
    # A compute process blocks immediately; a second sample is only needed to label a lease conflict.
    with pytest.raises(BrokerError) as error:
        service.create_request(admin, request_data("process-busy"), idempotency_key="proc-busy")
    assert error.value.code == "no_capacity"
    assert service.list_gpus(admin)["data"][0]["state"] == "BUSY_UNMANAGED"

    def age_telemetry(session) -> None:  # type: ignore[no-untyped-def]
        snapshot = session.scalar(select(TelemetryCurrent))
        assert snapshot is not None
        snapshot.observed_at = utcnow() - timedelta(seconds=1000)

    service._write(age_telemetry)
    assert service.list_gpus(admin)["data"][0]["state"] == "UNKNOWN_STALE"


def test_attention_summary_separates_endpoint_and_gpu_units(service, admin) -> None:
    service.ingest_observation(
        observation(count=2, processes=[process_for_gpu("GPU-endpoint-a-0")])
    )
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


def test_snapshot_includes_per_gpu_recent_telemetry_average(service, admin) -> None:
    start = utcnow() - timedelta(minutes=9)

    def sample(
        observed_at,
        *,
        memory_used_mib: int,
        gpu_utilization_pct: int,
        memory_utilization_pct: int,
        temperature_c: int,
    ) -> EndpointObservation:
        value = observation(
            count=1,
            observed_at=observed_at,
            host={
                "cpu_count": 64,
                "load_1m": {
                    10_000: 6.4,
                    20_000: 12.8,
                    30_000: 32.0,
                }[memory_used_mib],
                "memory_total_mib": 100_000,
                "memory_available_mib": {
                    10_000: 90_000,
                    20_000: 80_000,
                    30_000: 60_000,
                }[memory_used_mib],
            },
        )
        value.gpus[0] = value.gpus[0].model_copy(
            update={
                "memory_used_mib": memory_used_mib,
                "memory_free_mib": 100_000 - memory_used_mib,
                "gpu_utilization_pct": gpu_utilization_pct,
                "memory_utilization_pct": memory_utilization_pct,
                "temperature_c": temperature_c,
            }
        )
        return value

    service.ingest_observation(
        sample(
            start,
            memory_used_mib=10_000,
            gpu_utilization_pct=10,
            memory_utilization_pct=20,
            temperature_c=40,
        )
    )
    service.ingest_observation(
        sample(
            start + timedelta(seconds=61),
            memory_used_mib=20_000,
            gpu_utilization_pct=40,
            memory_utilization_pct=50,
            temperature_c=50,
        )
    )
    # This newest current sample is not yet another persisted history point,
    # but must still contribute to the average exactly once.
    service.ingest_observation(
        sample(
            start + timedelta(seconds=90),
            memory_used_mib=30_000,
            gpu_utilization_pct=80,
            memory_utilization_pct=90,
            temperature_c=60,
        )
    )

    recent_average = service.snapshot(admin)["data"]["gpus"][0]["telemetry"]["recent_average"]
    assert recent_average == {
        "window_seconds": 600,
        "sample_count": 3,
        "first_observed_at": start.isoformat(),
        "last_observed_at": (start + timedelta(seconds=90)).isoformat(),
        "memory_used_mib": 20_000.0,
        "memory_free_mib": 80_000.0,
        "memory_used_pct": 20.0,
        "gpu_utilization_pct": 43.33,
        "memory_utilization_pct": 53.33,
        "temperature_c": 50.0,
    }
    recent_host_average = service.snapshot(admin)["data"]["endpoints"][0]["host_telemetry"]["recent_average"]
    assert recent_host_average == {
        "window_seconds": 600,
        "sample_count": 3,
        "first_observed_at": start.isoformat(),
        "last_observed_at": (start + timedelta(seconds=90)).isoformat(),
        "cpu_load_fraction": 0.27,
        "memory_used_pct": 23.33,
    }


def test_cpu_only_observation_is_persisted_as_endpoint_resource_kind(service, admin) -> None:
    service.ingest_observation(
        observation(
            count=0,
            observation_complete=True,
        ).model_copy(update={"gpu_probe_status": "cpu_only"})
    )

    endpoint = service.snapshot(admin)["data"]["endpoints"][0]

    assert endpoint["resource_kind"] == "cpu_only"
    assert endpoint["monitor"] == {
        **endpoint["monitor"],
        "status": "ONLINE",
        "gpu_count": 0,
        "last_error": None,
    }


def test_endpoint_cpu_and_memory_telemetry_is_exposed_in_snapshot(service, admin) -> None:
    service.ingest_observation(observation(count=1))
    endpoint = service.snapshot(admin)["data"]["endpoints"][0]
    assert endpoint["host_telemetry"] == {
        "observed_at": endpoint["host_telemetry"]["observed_at"],
        "collected_at": endpoint["host_telemetry"]["collected_at"],
        "cpu_count": 64,
        "load_1m": 4.0,
        "cpu_utilization_pct": None,
        "cpu_total_ticks": None,
        "cpu_idle_ticks": None,
        "memory_total_mib": 262_144,
        "memory_available_mib": 196_608,
        "provider": "raw-ssh",
        "recent_average": {
            "window_seconds": 600,
            "sample_count": 1,
            "first_observed_at": endpoint["host_telemetry"]["observed_at"],
            "last_observed_at": endpoint["host_telemetry"]["observed_at"],
            "cpu_load_fraction": 0.06,
            "memory_used_pct": 25.0,
        },
    }


def test_endpoint_history_is_throttled_and_calculates_cpu_utilization(service, admin) -> None:
    start = utcnow() - timedelta(minutes=10)
    first = service.ingest_observation(
        observation(
            count=1,
            observed_at=start,
            host={"cpu_total_ticks": 1_000, "cpu_idle_ticks": 700},
        )
    )
    second = service.ingest_observation(
        observation(
            count=1,
            observed_at=start + timedelta(seconds=30),
            host={"cpu_total_ticks": 1_100, "cpu_idle_ticks": 750},
        )
    )
    third = service.ingest_observation(
        observation(
            count=1,
            observed_at=start + timedelta(seconds=61),
            host={"cpu_total_ticks": 1_200, "cpu_idle_ticks": 810},
        )
    )

    def endpoint_history_count(session):  # type: ignore[no-untyped-def]
        return len(session.scalars(select(EndpointTelemetrySnapshot)).all())

    assert first["endpoint_history_points_written"] == 1
    assert second["endpoint_history_points_written"] == 0
    assert third["endpoint_history_points_written"] == 1
    assert service._read(endpoint_history_count) == 2

    current_host = service.snapshot(admin)["data"]["endpoints"][0]["host_telemetry"]
    assert current_host["cpu_utilization_pct"] == 40.0
    history = service.endpoint_history(admin, "endpoint-a", window_seconds=3600, max_points=120)
    assert history["data"]["point_count"] == 2
    assert history["data"]["points"][0]["cpu_utilization_pct"] is None
    assert history["data"]["points"][1]["cpu_utilization_pct"] == 40.0
    assert history["data"]["points"][1]["memory_used_pct"] == 25.0
    gpu_series = history["data"]["gpu_series"]
    assert len(gpu_series) == 1
    assert gpu_series[0]["gpu_id"] == "endpoint-a:GPU-endpoint-a-0"
    assert gpu_series[0]["gpu_uuid"] == "GPU-endpoint-a-0"
    assert gpu_series[0]["gpu_index"] == 0
    assert len(gpu_series[0]["points"]) == 2
    assert gpu_series[0]["points"][1]["gpu_utilization_pct"] == 0.0
    assert gpu_series[0]["points"][1]["memory_used_pct"] == 0.0


def test_endpoint_history_validates_window_points_and_identity(service, admin) -> None:
    service.ingest_observation(observation(count=1))

    assert (
        service.endpoint_history(admin, "endpoint-a", window_seconds=21_600)["data"][
            "window_seconds"
        ]
        == 21_600
    )
    with pytest.raises(BrokerError) as bad_window:
        service.endpoint_history(admin, "endpoint-a", window_seconds=300)
    with pytest.raises(BrokerError) as bad_points:
        service.endpoint_history(admin, "endpoint-a", max_points=121)
    with pytest.raises(BrokerError) as missing_endpoint:
        service.endpoint_history(admin, "missing", window_seconds=3600)

    assert bad_window.value.code == "invalid_history_window"
    assert bad_points.value.code == "invalid_history_points"
    assert missing_endpoint.value.code == "endpoint_not_found"


def test_endpoint_history_excludes_current_outside_requested_window(service, admin) -> None:
    service.ingest_observation(observation(count=1, observed_at=utcnow() - timedelta(hours=2)))

    history = service.endpoint_history(admin, "endpoint-a", window_seconds=3600, max_points=120)

    assert history["data"]["point_count"] == 0


def test_telemetry_prune_deletes_gpu_and_endpoint_history_but_not_current(
    service,
    admin,
) -> None:
    service.ingest_observation(observation(count=1, observed_at=utcnow() - timedelta(minutes=10)))
    service.ingest_observation(observation(count=1))

    result = service.prune_telemetry(
        admin,
        RetentionPrune(older_than_seconds=60),
        idempotency_key="prune-endpoint-history",
    )

    def counts(session):  # type: ignore[no-untyped-def]
        return (
            len(session.scalars(select(TelemetryCurrent)).all()),
            len(session.scalars(select(EndpointTelemetryCurrent)).all()),
            len(session.scalars(select(TelemetrySnapshot)).all()),
            len(session.scalars(select(EndpointTelemetrySnapshot)).all()),
        )

    assert result["deleted_count"] == 2
    assert result["gpu_deleted_count"] == 1
    assert result["endpoint_deleted_count"] == 1
    assert service._read(counts) == (1, 1, 1, 1)


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
        return [
            event.action for event in session.scalars(select(AuditEvent).order_by(AuditEvent.id))
        ]

    assert service._read(actions) == ["telemetry.failed", "telemetry.recovered"]


def test_human_monitoring_sees_endpoint_failures_and_can_read_latest_events(service) -> None:
    service.record_provider_failure("endpoint-a", "timeout")
    service.record_provider_failure("endpoint-b", "connection refused")

    human_events = service.list_events(service.local_actor("human"))["data"]
    assert [event["action"] for event in human_events] == [
        "telemetry.failed",
        "telemetry.failed",
    ]
    latest = service.list_events(service.local_actor("human"), latest_first=True, limit=1)["data"]
    assert latest[0]["resource_id"] == "endpoint-b"


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
    service.ingest_observation(
        observation(count=1, processes=[process_for_gpu("GPU-endpoint-a-0")])
    )
    lease = next(item for item in service.list_leases(admin)["data"] if item["id"] == lease_id)
    assert lease["state"] == "ORPHANED_BUSY"
    with pytest.raises(BrokerError) as error:
        service.create_request(
            admin, request_data("must-not-reuse"), idempotency_key="blocked-orphan"
        )
    assert error.value.code == "no_capacity"


def test_allocator_can_claim_an_unregistered_project_without_login_token(service, admin) -> None:
    created = service.create_actor(
        admin,
        ActorCreate(
            id="story-agent",
            display_name="Project A agent",
            role="allocator",
            project_ids=["project-a"],
        ),
        idempotency_key="new-agent",
    )
    assert "token" not in created
    agent = service.local_actor("story-agent")
    service.ingest_observation(observation(count=1))
    claimed = service.create_request(
        agent,
        request_data("unregistered-project", project_id="storyboard"),
        idempotency_key="unregistered-project",
    )
    assert claimed["request"]["project_id"] == "storyboard"
    assert any(
        item["id"] == claimed["request"]["id"] for item in service.list_requests(agent)["data"]
    )
    actors = service.list_actors(admin)["data"]
    assert "tokens" not in str(actors)


def test_one_hundred_concurrent_requests_never_double_lease(service, admin) -> None:
    service.ingest_observation(observation(count=4))

    def submit(index: int):  # type: ignore[no-untyped-def]
        try:
            return service.create_request(
                admin,
                request_data(f"concurrent-{index}"),
                idempotency_key=f"concurrent-{index}",
            )
        except BrokerError as error:
            assert error.code == "no_capacity"
            return None

    results = []
    with ThreadPoolExecutor(max_workers=32) as pool:
        futures = [pool.submit(submit, index) for index in range(100)]
        for future in as_completed(futures):
            results.append(future.result())
    leases = [result["lease"] for result in results if result is not None]
    gpu_ids = [gpu_id for lease in leases for gpu_id in lease["gpu_ids"]]
    assert len(gpu_ids) == len(set(gpu_ids)) == 4
    assert all(result["request"]["state"] == "LEASED" for result in results if result is not None)


def test_database_unique_index_rejects_duplicate_active_gpu(service, admin) -> None:
    service.ingest_observation(observation(count=1))
    first = service.create_request(admin, request_data("first"), idempotency_key="first")
    assert first["lease"] is not None
    gpu_id = first["lease"]["gpu_ids"][0]
    with pytest.raises(BrokerError) as error:
        service.create_request(admin, request_data("second"), idempotency_key="second")
    assert error.value.code == "no_capacity"

    def illegal_duplicate(session) -> None:  # type: ignore[no-untyped-def]
        now = utcnow()
        request = AllocationRequest(
            id="illegal-request",
            actor_id=admin.id,
            project_id="project-a",
            profile_id=None,
            auto_activate=False,
            task_ref="illegal-duplicate",
            purpose="verify active GPU uniqueness",
            constraints_json="{}",
            duration_seconds=3600,
            expected_duration_seconds=None,
            start_after=None,
            deadline=None,
            approval_ref=None,
            state="LEASED",
            priority_class="normal",
            blocked_reason=None,
            created_at=now,
            updated_at=now,
        )
        session.add(request)
        session.flush()
        lease = Lease(
            id="illegal",
            request_id=request.id,
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


def test_cooperative_actor_labels_are_not_admin_and_lease_ownership_is_exact(service) -> None:
    service.ingest_observation(observation(count=1))
    owner = service.local_actor("lease-owner")
    other = service.local_actor("lease-other")
    assert owner.role == "allocator"
    claimed = service.create_request(
        owner, request_data("owner-only"), idempotency_key="owner-only"
    )
    assert claimed["lease"] is not None
    with pytest.raises(BrokerError) as forbidden:
        service.release_lease(
            other,
            claimed["lease"]["id"],
            reason="not the owner",
            idempotency_key="other-release",
        )
    assert forbidden.value.code == "lease_forbidden"

    with pytest.raises(BrokerError) as override_forbidden:
        service.release_lease(
            other,
            claimed["lease"]["id"],
            reason="not a human operator",
            idempotency_key="other-override-release",
            operator_override=True,
        )
    assert override_forbidden.value.code == "operator_role_required"
    assert service.list_leases(other)["data"] == []
    assert service.list_requests(other)["data"] == []


def test_paused_endpoint_blocks_claims_and_can_resume(service, admin) -> None:
    service.ingest_observation(observation(endpoint_id="endpoint-a", count=1))
    drained = service.pause_endpoint(admin, "endpoint-a", idempotency_key="drain-a")
    assert drained["endpoint"]["lifecycle_state"] == "draining"
    assert {endpoint.id for endpoint in service.collector_endpoints()} == {
        "endpoint-a",
        "endpoint-b",
    }
    with pytest.raises(BrokerError) as error:
        service.create_request(
            admin,
            RequestCreate.model_validate(
                {
                    "project_id": "project-a",
                    "task_ref": "must-not-use-draining",
                    "purpose": "lifecycle admission test",
                    "constraints": {"gpu_count": 1, "endpoint_ids": ["endpoint-a"]},
                }
            ),
            idempotency_key="draining-claim",
        )
    assert error.value.code == "no_capacity"
    resumed = service.resume_endpoint(admin, "endpoint-a", idempotency_key="resume-a")
    assert resumed["endpoint"]["lifecycle_state"] == "active"
    assert {endpoint.id for endpoint in service.collector_endpoints()} == {
        "endpoint-a",
        "endpoint-b",
    }


def test_direct_lease_returns_executable_resources_and_accounts_endpoint_commitments(
    service, admin
) -> None:
    service.ingest_observation(observation(count=2))
    first = service.create_request(
        admin,
        RequestCreate.model_validate(
            {
                "project_id": "project-a",
                "task_ref": "commitment-one",
                "purpose": "per endpoint commitment",
                "constraints": {"gpu_count": 1, "cpu_cores": 40, "memory_mib": 200_000},
            }
        ),
        idempotency_key="commitment-one",
    )
    assert first["lease"] is not None
    resource = first["lease"]["resources"][0]
    assert resource["endpoint"] == {
        "id": "endpoint-a",
        "host": "127.0.0.1",
        "port": 2201,
        "ssh_user": "gpu",
        "workspace_path": "/srv/project-a",
    }
    assert resource["gpus"][0]["gpu_uuid"].startswith("GPU-")
    assert resource["cuda_visible_devices"] == str(resource["gpus"][0]["cuda_ordinal"])
    assert resource["cuda_device_order"] == "PCI_BUS_ID"
    assert resource["commitment"] == {"cpu_cores": 40.0, "memory_mib": 200_000}
    with pytest.raises(BrokerError) as error:
        service.create_request(
            admin,
            RequestCreate.model_validate(
                {
                    "project_id": "project-a",
                    "task_ref": "commitment-two",
                    "purpose": "must not overcommit endpoint",
                    "constraints": {"gpu_count": 1, "cpu_cores": 40, "memory_mib": 200_000},
                }
            ),
            idempotency_key="commitment-two",
        )
    assert error.value.code == "no_capacity"


def test_gpu_without_current_cuda_ordinal_is_not_allocated(service, admin) -> None:
    service.ingest_observation(observation(count=1))
    with service.database.session() as session:
        gpu = session.get(GPUDevice, "endpoint-a:GPU-endpoint-a-0")
        assert gpu is not None
        gpu.cuda_ordinal = None
        session.commit()

    with pytest.raises(BrokerError) as error:
        service.create_request(
            admin,
            request_data("missing-cuda-selector"),
            idempotency_key="missing-cuda-selector",
        )

    assert error.value.code == "no_capacity"


def test_human_can_reassign_a_task_to_an_exact_gpu(service, admin) -> None:
    service.ingest_observation(observation(count=2))
    claimed = service.create_request(
        admin,
        request_data("manual-gpu-move"),
        idempotency_key="manual-gpu-move-claim",
    )
    lease = claimed["lease"]
    assert lease is not None
    assert lease["gpu_ids"] == ["endpoint-a:GPU-endpoint-a-0"]

    moved = service.reassign_lease_gpus(
        admin,
        lease["id"],
        ["endpoint-a:GPU-endpoint-a-1"],
        idempotency_key="manual-gpu-move-apply",
    )

    assert moved["restart_required"] is True
    assert moved["lease"]["gpu_ids"] == ["endpoint-a:GPU-endpoint-a-1"]
    assert moved["lease"]["resources"][0]["cuda_visible_devices"] == "1"
    assert moved["lease"]["resources"][0]["cuda_device_order"] == "PCI_BUS_ID"
    gpus = {gpu["id"]: gpu for gpu in service.list_gpus(admin)["data"]}
    assert gpus["endpoint-a:GPU-endpoint-a-0"]["state"] == "AVAILABLE"
    assert gpus["endpoint-a:GPU-endpoint-a-1"]["lease"]["id"] == lease["id"]


def test_lease_gpu_reassignment_requires_the_lease_owner(service) -> None:
    service.ingest_observation(observation(count=2))
    owner = service.local_actor("reassignment-owner")
    other = service.local_actor("reassignment-other")
    claimed = service.create_request(
        owner,
        request_data("owner-reassignment-only"),
        idempotency_key="owner-reassignment-only-claim",
    )
    lease = claimed["lease"]
    assert lease is not None
    target_gpu_id = "endpoint-a:GPU-endpoint-a-1"

    with pytest.raises(BrokerError) as planning_error:
        service.keepalive_reclaim_request_for_reassignment(
            other,
            lease["id"],
            [target_gpu_id],
        )
    assert planning_error.value.code == "lease_forbidden"

    with pytest.raises(BrokerError) as mutation_error:
        service.reassign_lease_gpus(
            other,
            lease["id"],
            [target_gpu_id],
            idempotency_key="foreign-reassignment",
        )
    assert mutation_error.value.code == "lease_forbidden"
    current = service.list_leases(owner)["data"][0]
    assert current["gpu_ids"] == ["endpoint-a:GPU-endpoint-a-0"]


def test_operator_override_can_plan_and_reassign_a_foreign_lease(service, admin) -> None:
    service.ingest_observation(observation(count=2))
    owner = service.local_actor("operator-correction-owner")
    allocator = service.local_actor("operator-correction-allocator")
    claimed = service.create_request(
        owner,
        request_data("operator-correction"),
        idempotency_key="operator-correction-claim",
    )
    lease = claimed["lease"]
    assert lease is not None
    target_gpu_id = "endpoint-a:GPU-endpoint-a-1"

    with pytest.raises(BrokerError) as role_error:
        service.reassign_lease_gpus(
            allocator,
            lease["id"],
            [target_gpu_id],
            idempotency_key="allocator-override-rejected",
            operator_override=True,
        )
    assert role_error.value.code == "operator_role_required"

    reclaim_request = service.keepalive_reclaim_request_for_reassignment(
        admin,
        lease["id"],
        [target_gpu_id],
        operator_override=True,
    )
    assert reclaim_request is None

    moved = service.reassign_lease_gpus(
        admin,
        lease["id"],
        [target_gpu_id],
        idempotency_key="operator-override-reassignment",
        operator_override=True,
    )
    assert moved["lease"]["gpu_ids"] == [target_gpu_id]


def test_reassigned_active_task_auto_binds_process_on_its_new_gpu(service, admin) -> None:
    service.ingest_observation(observation(count=2))
    claimed = service.create_request(
        admin,
        request_data("running-gpu-move"),
        idempotency_key="running-gpu-move-claim",
        activate_if_allocated=True,
    )
    lease_id = claimed["lease"]["id"]
    service.ingest_observation(
        observation(count=2, processes=[process_for_gpu("GPU-endpoint-a-0", pid=4201)])
    )
    assert service.list_leases(admin)["data"][0]["state"] == "ACTIVE"

    moved = service.reassign_lease_gpus(
        admin,
        lease_id,
        ["endpoint-a:GPU-endpoint-a-1"],
        idempotency_key="running-gpu-move-apply",
    )
    assert moved["lease"]["state"] == "ACTIVE"
    assert moved["lease"]["workloads"] == []

    service.ingest_observation(
        observation(count=2, processes=[process_for_gpu("GPU-endpoint-a-1", pid=4202)])
    )

    lease = service.list_leases(admin)["data"][0]
    assert lease["state"] == "ACTIVE"
    assert lease["workloads"][0]["run_id"] == f"collector:lease:{lease_id}"
    snapshot_lease = service.snapshot(admin)["data"]["leases"][0]
    assert snapshot_lease["runtime_state"] == "RUNNING"
    gpus = {gpu["id"]: gpu for gpu in service.list_gpus(admin)["data"]}
    assert gpus["endpoint-a:GPU-endpoint-a-0"]["state"] == "AVAILABLE"
    assert gpus["endpoint-a:GPU-endpoint-a-1"]["state"] == "RUNNING_MANAGED"
