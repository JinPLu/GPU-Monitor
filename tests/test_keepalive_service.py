from __future__ import annotations

from datetime import UTC, timedelta

import pytest
from sqlalchemy import select

from serverpilot.models import AllocationRequest, Endpoint, Lease, LeaseResource
from serverpilot.schemas import EndpointUpdate, LeaseBind, LeaseObservedBind
from serverpilot.service import SYSTEM_ACTOR_ID, ActorContext, BrokerError
from serverpilot.timeutil import utcnow
from tests.helpers import observation, process_for_gpu


def _opt_in(service) -> None:  # noqa: ANN001
    with service.database.session() as session:
        endpoint = session.get(Endpoint, "endpoint-a")
        assert endpoint is not None
        endpoint.keepalive_adapter_id = "server-script-v1"
        session.commit()


def _begin(service, admin):  # noqa: ANN001
    _opt_in(service)
    service.ingest_observation(observation(count=2))
    return service.begin_keepalive(admin, "endpoint-a", idempotency_key="begin-1")


def _confirm(service, admin, begun, *, extra_process: bool = False):  # noqa: ANN001
    lease_id = begun["keepalive"]["lease_id"]
    started = utcnow()
    processes = [
        process_for_gpu("GPU-endpoint-a-0", pid=4321),
        process_for_gpu("GPU-endpoint-a-1", pid=4321),
    ]
    if extra_process:
        processes.append(process_for_gpu("GPU-endpoint-a-0", pid=9999))
    service.ingest_observation(observation(count=2, processes=processes))
    return service.confirm_keepalive(
        admin,
        "endpoint-a",
        lease_id,
        attested_pid=4321,
        observation_not_before=started,
        idempotency_key="confirm-1",
    )


def test_keepalive_success_projects_state_and_hides_internal_records(service, admin) -> None:
    begun = _begin(service, admin)
    confirmed = _confirm(service, admin, begun)

    assert confirmed["keepalive"]["state"] == "ACTIVE"
    assert (
        service.begin_keepalive(admin, "endpoint-a", idempotency_key="begin-retry")["keepalive"][
            "lease_id"
        ]
        == begun["keepalive"]["lease_id"]
    )
    snapshot = service.snapshot(admin)["data"]
    endpoint = next(item for item in snapshot["endpoints"] if item["id"] == "endpoint-a")
    endpoint_gpus = [item for item in snapshot["gpus"] if item["endpoint_id"] == "endpoint-a"]
    assert endpoint["keepalive"] == {"configured": True, "state": "ACTIVE"}
    assert {item["state"] for item in endpoint_gpus} == {"KEEPALIVE"}
    assert all(item["lease"] is None for item in endpoint_gpus)
    assert all(item["kind"] != "keepalive" for item in snapshot["leases"])


def test_keepalive_confirmation_rejects_partial_stale_and_foreign_processes(service, admin) -> None:
    begun = _begin(service, admin)
    lease_id = begun["keepalive"]["lease_id"]
    barrier = utcnow()
    service.ingest_observation(
        observation(
            count=1,
            processes=[process_for_gpu("GPU-endpoint-a-0", pid=4321)],
            observation_complete=False,
        )
    )
    with pytest.raises(BrokerError) as partial:
        service.confirm_keepalive(
            admin,
            "endpoint-a",
            lease_id,
            attested_pid=4321,
            observation_not_before=barrier,
            idempotency_key="partial",
        )
    assert partial.value.code == "keepalive_observation_stale"

    with pytest.raises(BrokerError) as stale:
        service.confirm_keepalive(
            admin,
            "endpoint-a",
            lease_id,
            attested_pid=4321,
            observation_not_before=utcnow() + timedelta(minutes=1),
            idempotency_key="stale",
        )
    assert stale.value.code == "keepalive_observation_stale"

    with pytest.raises(BrokerError) as foreign:
        _confirm(service, admin, begun, extra_process=True)
    assert foreign.value.code == "keepalive_foreign_process"


def test_keepalive_confirmation_accepts_attested_host_pid_with_hidden_metadata(
    service, admin
) -> None:
    begun = _begin(service, admin)
    lease_id = begun["keepalive"]["lease_id"]
    barrier = utcnow()
    observed_host_pid = 3_331_894
    processes = [
        process_for_gpu("GPU-endpoint-a-0", pid=observed_host_pid),
        process_for_gpu("GPU-endpoint-a-1", pid=observed_host_pid),
    ]
    # The helper resolves the host PID through its nvidia-smi view. An empty
    # process-details probe is represented by missing identity metadata on
    # every otherwise identical GPU process observation.
    processes = [
        process.model_copy(update={"username": None, "executable": "[Not Found]"})
        for process in processes
    ]
    service.ingest_observation(observation(count=2, processes=processes))

    confirmed = service.confirm_keepalive(
        admin,
        "endpoint-a",
        lease_id,
        attested_pid=observed_host_pid,
        observation_not_before=barrier,
        idempotency_key="host-pid-hidden-metadata",
    )

    assert confirmed["keepalive"]["state"] == "ACTIVE"
    snapshot = service.snapshot(admin)["data"]
    endpoint_gpus = [item for item in snapshot["gpus"] if item["endpoint_id"] == "endpoint-a"]
    assert {item["state"] for item in endpoint_gpus} == {"KEEPALIVE"}


def test_keepalive_confirmation_rejects_additional_namespace_hidden_pid(
    service, admin
) -> None:
    begun = _begin(service, admin)
    lease_id = begun["keepalive"]["lease_id"]
    barrier = utcnow()
    processes = [
        process_for_gpu("GPU-endpoint-a-0", pid=3_331_894),
        process_for_gpu("GPU-endpoint-a-1", pid=3_331_894),
        process_for_gpu("GPU-endpoint-a-0", pid=8_888_888),
    ]
    processes = [
        process.model_copy(update={"username": None, "executable": "[Not Found]"})
        for process in processes
    ]
    service.ingest_observation(observation(count=2, processes=processes))

    with pytest.raises(BrokerError) as foreign:
        service.confirm_keepalive(
            admin,
            "endpoint-a",
            lease_id,
            attested_pid=3_331_894,
            observation_not_before=barrier,
            idempotency_key="namespace-translated-foreign",
        )

    assert foreign.value.code == "keepalive_foreign_process"


def test_keepalive_stop_requires_fresh_empty_observation(service, admin) -> None:
    begun = _begin(service, admin)
    _confirm(service, admin, begun)
    lease_id = begun["keepalive"]["lease_id"]

    barrier = utcnow()
    service.ingest_observation(
        observation(
            count=2,
            processes=[
                process_for_gpu("GPU-endpoint-a-0", pid=4321),
                process_for_gpu("GPU-endpoint-a-1", pid=4321),
            ],
        )
    )
    with pytest.raises(BrokerError) as running:
        service.finalize_keepalive_stop(
            admin,
            "endpoint-a",
            lease_id,
            observation_not_before=barrier,
            idempotency_key="stop-running",
        )
    assert running.value.code == "keepalive_process_still_running"

    barrier = utcnow()
    service.ingest_observation(observation(count=2))
    endpoint = next(
        item for item in service.snapshot(admin)["data"]["endpoints"] if item["id"] == "endpoint-a"
    )
    assert endpoint["keepalive"]["state"] == "ERROR"
    stopped = service.finalize_keepalive_stop(
        admin,
        "endpoint-a",
        lease_id,
        observation_not_before=barrier,
        idempotency_key="stop-empty",
    )
    assert stopped["keepalive"]["enabled"] is False
    assert service.prepare_keepalive_stop(admin, "endpoint-a")["keepalive"]["state"] == "OFF"
    with service.database.session() as session:
        lease = session.get(Lease, lease_id)
        request = session.get(AllocationRequest, lease.request_id) if lease else None
        assert lease is not None and lease.state == "RELEASED"
        assert request is not None and request.state == "RELEASED"
        assert not any(
            session.scalars(
                select(LeaseResource).where(
                    LeaseResource.lease_id == lease_id,
                    LeaseResource.active.is_(True),
                )
            ).all()
        )


def test_active_keepalive_adapter_cannot_be_removed(service, admin) -> None:
    _begin(service, admin)

    with pytest.raises(BrokerError) as blocked:
        service.update_endpoint(
            admin,
            "endpoint-a",
            EndpointUpdate.model_validate({"keepalive_adapter_id": None}),
            idempotency_key="remove-active-keepalive-adapter",
        )

    assert blocked.value.code == "keepalive_endpoint_connection_in_use"
    assert service.collector_endpoint("endpoint-a").keepalive_adapter_id == "server-script-v1"


def test_complete_exact_observation_renews_only_matching_keepalive(service, admin) -> None:
    begun = _begin(service, admin)
    _confirm(service, admin, begun)
    lease_id = begun["keepalive"]["lease_id"]
    with service.database.session() as session:
        lease = session.get(Lease, lease_id)
        assert lease is not None
        lease.expires_at = utcnow() + timedelta(seconds=5)
        session.commit()

    processes = [
        process_for_gpu("GPU-endpoint-a-0", pid=4321),
        process_for_gpu("GPU-endpoint-a-1", pid=4321),
    ]
    service.ingest_observation(observation(count=2, processes=processes))
    with service.database.session() as session:
        renewed = session.get(Lease, lease_id)
        assert renewed is not None
        assert renewed.expires_at.replace(tzinfo=UTC) > utcnow() + timedelta(seconds=30)

        prior_expiry = renewed.expires_at.replace(tzinfo=UTC)
    service.ingest_observation(
        observation(count=2, processes=processes, observation_complete=False)
    )
    with service.database.session() as session:
        not_renewed = session.get(Lease, lease_id)
        assert not_renewed is not None
        assert not_renewed.expires_at.replace(tzinfo=UTC) == prior_expiry


def test_confirm_absent_rejects_complete_observation_that_drops_a_known_gpu(
    service, admin
) -> None:
    _opt_in(service)
    service.ingest_observation(observation(count=2))
    barrier = utcnow()
    service.ingest_observation(observation(count=1))

    with pytest.raises(BrokerError) as incomplete:
        service.confirm_keepalive_absent(
            admin,
            "endpoint-a",
            observation_not_before=barrier,
            idempotency_key="absent-missing-known-gpu",
        )

    assert incomplete.value.code == "keepalive_observation_incomplete"


def test_expired_keepalive_is_not_revived_by_matching_observation(service, admin) -> None:
    begun = _begin(service, admin)
    _confirm(service, admin, begun)
    lease_id = begun["keepalive"]["lease_id"]
    expired_at = utcnow() - timedelta(seconds=1)
    with service.database.session() as session:
        lease = session.get(Lease, lease_id)
        assert lease is not None
        lease.expires_at = expired_at
        session.commit()

    service.ingest_observation(
        observation(
            count=2,
            processes=[
                process_for_gpu("GPU-endpoint-a-0", pid=4321),
                process_for_gpu("GPU-endpoint-a-1", pid=4321),
            ],
        )
    )
    with service.database.session() as session:
        lease = session.get(Lease, lease_id)
        assert lease is not None
        assert lease.state == "ORPHANED_BUSY"
        assert lease.expires_at.replace(tzinfo=UTC) == expired_at


def test_public_lease_mutations_reject_internal_keepalive_even_for_forged_actor(
    service, admin
) -> None:
    begun = _begin(service, admin)
    lease_id = begun["keepalive"]["lease_id"]
    forged = ActorContext(
        id=SYSTEM_ACTOR_ID,
        role="admin",
        project_ids=frozenset({"serverpilot-system"}),
    )
    mutations = [
        lambda: service.activate_lease(forged, lease_id, idempotency_key="generic-activate"),
        lambda: service.renew_lease(forged, lease_id, idempotency_key="generic-renew"),
        lambda: service.release_lease(
            forged, lease_id, reason="bypass", idempotency_key="generic-release"
        ),
        lambda: service.bind_workload(
            forged,
            lease_id,
            LeaseBind(run_id="forged", process_keys=[]),
            idempotency_key="generic-bind",
        ),
        lambda: service.bind_observed_workload(
            forged,
            lease_id,
            LeaseObservedBind(run_id="forged"),
            idempotency_key="generic-observed-bind",
        ),
    ]
    for mutation in mutations:
        with pytest.raises(BrokerError) as blocked:
            mutation()
        assert blocked.value.code == "keepalive_requires_dedicated_operation"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("keepalive_adapter_id", None),
        ("ssh_user", "another"),
        ("ssh_alias", "another-host"),
        ("observation_profile", "server-script-v1"),
    ],
)
def test_active_keepalive_freezes_connection_and_verification_fields(
    service, admin, field: str, value: object
) -> None:
    _begin(service, admin)
    with pytest.raises(BrokerError) as blocked:
        service.update_endpoint(
            admin,
            "endpoint-a",
            EndpointUpdate.model_validate({field: value}),
            idempotency_key=f"change-{field}",
        )
    assert blocked.value.code == "keepalive_endpoint_connection_in_use"
    assert blocked.value.details == {"fields": [field]}


def test_confirm_keepalive_absent_requires_fresh_empty_whole_endpoint(service, admin) -> None:
    _opt_in(service)
    service.ingest_observation(observation(count=2))
    barrier = utcnow()
    service.ingest_observation(observation(count=2))
    result = service.confirm_keepalive_absent(
        admin,
        "endpoint-a",
        observation_not_before=barrier,
        idempotency_key="confirm-off-without-lease",
    )
    assert result["keepalive"] == {
        "endpoint_id": "endpoint-a",
        "enabled": False,
        "lease_id": None,
        "state": "OFF",
    }
