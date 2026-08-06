from __future__ import annotations

from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from gpu_broker import API_CAPABILITIES
from gpu_broker.api import create_app
from gpu_broker.config import Settings
from gpu_broker.schemas import EndpointUpsert, RequestCreate
from tests.helpers import observation


def _request(task_ref: str) -> RequestCreate:
    return RequestCreate.model_validate(
        {
            "project_id": "project-a",
            "task_ref": task_ref,
            "purpose": "state contract regression",
            "constraints": {"gpu_count": 1},
        }
    )


def test_control_plane_state_route_groups_current_and_history(tmp_path: Path, inventory) -> None:
    inventory_path = tmp_path / "inventory.yaml"
    inventory_path.write_text(yaml.safe_dump(inventory.model_dump(mode="json")), encoding="utf-8")
    app = create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'state.sqlite3'}",
            inventory_path=inventory_path,
            session_secret="s" * 32,
        )
    )
    service = app.state.service
    actor = service.local_actor("state-agent")
    service.ingest_observation(observation(count=1))
    service.create_request(actor, _request("state-lease"), idempotency_key="state-lease")
    service.upsert_endpoint(
        actor,
        EndpointUpsert(
            id="endpoint-b",
            host="127.0.0.1",
            port=2202,
            ssh_user="gpu",
            lifecycle_state="draining",
        ),
        idempotency_key="endpoint-b-draining",
    )
    service.upsert_endpoint(
        actor,
        EndpointUpsert(
            id="endpoint-b",
            host="127.0.0.1",
            port=2202,
            ssh_user="gpu",
            lifecycle_state="retired",
        ),
        idempotency_key="endpoint-b-retired",
    )

    response = TestClient(app).get(
        "/api/v1/state",
        headers={"X-GPU-Broker-Actor": "state-agent"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert "control_plane_state" in API_CAPABILITIES
    assert payload["schema_version"] == "v1"
    assert isinstance(payload["snapshot_revision"], int)
    assert payload["data"].keys() == {"current", "history"}
    current = payload["data"]["current"]
    history = payload["data"]["history"]
    assert {
        "summary",
        "data_age_seconds",
        "freshness_seconds",
        "endpoints",
        "gpus",
        "absent_gpu_ids",
        "leases",
        "requests",
        "reservations",
        "maintenance",
        "alerts",
        "resource_providers",
        "allocatable_units",
        "host_capacity",
        "resource_claims",
        "scheduler_targets",
        "scheduler_jobs",
        "scheduler_transfers",
        "workload_profiles",
        "admission_boundary",
    } <= set(current)
    assert current["host_capacity"]
    assert {endpoint["id"] for endpoint in current["endpoints"]} == {"endpoint-a"}
    assert {endpoint["id"] for endpoint in history["retired_endpoints"]} == {"endpoint-b"}
    assert "resource_plan_evaluations" in history
    assert "resource_run_actuals" in history
    assert "audit" not in current
    assert "telemetry" not in current


def test_idempotent_mutation_replay_retains_committed_revision(service, admin) -> None:
    service.ingest_observation(observation(count=1))

    first = service.create_request(admin, _request("committed-replay"), idempotency_key="commit-key")
    second = service.create_request(admin, _request("committed-replay"), idempotency_key="commit-key")

    assert first == second
    assert first["committed"] == {"snapshot_revision": first["snapshot_revision"]}
