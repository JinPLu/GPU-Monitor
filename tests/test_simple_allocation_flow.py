from __future__ import annotations

import pytest
from sqlalchemy import func, select

from serverpilot.models import AllocationRequest, Lease
from serverpilot.schemas import RequestCreate
from serverpilot.service import BrokerError
from tests.helpers import observation


def claim(task: str) -> RequestCreate:
    return RequestCreate.model_validate(
        {
            "project_id": "project-a",
            "task_ref": task,
            "purpose": task,
            "duration_seconds": 3600,
            "constraints": {"gpu_count": 1, "placement": "pack"},
        }
    )


def test_no_idle_gpu_fails_without_creating_a_waiting_record(service, admin) -> None:
    with pytest.raises(BrokerError) as error:
        service.create_request(admin, claim("no-gpu"), idempotency_key="no-gpu")

    assert error.value.code == "no_capacity"
    with service.database.session() as session:
        assert session.scalar(select(func.count()).select_from(AllocationRequest)) == 0
        assert session.scalar(select(func.count()).select_from(Lease)) == 0


def test_claim_is_immediate_and_a_second_claim_does_not_queue(service, admin) -> None:
    service.ingest_observation(observation(count=1))
    first = service.create_request(admin, claim("first"), idempotency_key="first")
    assert first["lease"] is not None

    with pytest.raises(BrokerError) as error:
        service.create_request(admin, claim("second"), idempotency_key="second")
    assert error.value.code == "no_capacity"

    with service.database.session() as session:
        assert session.scalar(select(func.count()).select_from(AllocationRequest)) == 1
        assert session.scalar(select(func.count()).select_from(Lease)) == 1

    current = service.control_plane_state(admin)["data"]["current"]
    assert current["requests"] == []
    assert current["reservations"] == []
    assert current["maintenance"] == []
