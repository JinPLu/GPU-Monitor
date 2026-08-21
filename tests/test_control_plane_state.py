from __future__ import annotations

from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from serverpilot import API_CAPABILITIES
from serverpilot.api import create_app
from serverpilot.config import Settings
from serverpilot.schemas import RequestCreate
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
    service.pause_endpoint(actor, "endpoint-b", idempotency_key="endpoint-b-pause")

    response = TestClient(app).get(
        "/api/v1/state",
        headers={"X-ServerPilot-Actor": "state-agent"},
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
    assert {endpoint["id"] for endpoint in current["endpoints"]} == {
        "endpoint-a",
        "endpoint-b",
    }
    assert "retired_endpoints" not in history
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


def test_control_plane_state_can_omit_advanced_projections(tmp_path: Path, inventory) -> None:
    """The App renders no generic-resource or scheduler projection.

    Asking for the state without them keeps the observe/allocate sections
    byte-identical, so narrowing the request cannot change what the desktop
    table shows.
    """

    inventory_path = tmp_path / "inventory.yaml"
    inventory_path.write_text(yaml.safe_dump(inventory.model_dump(mode="json")), encoding="utf-8")
    app = create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'state-narrow.sqlite3'}",
            inventory_path=inventory_path,
            session_secret="s" * 32,
        )
    )
    service = app.state.service
    actor = service.local_actor("state-agent")
    service.ingest_observation(observation(count=1))
    service.create_request(actor, _request("state-lease"), idempotency_key="state-lease")
    client = TestClient(app)
    headers = {"X-ServerPilot-Actor": "state-agent"}

    full = client.get("/api/v1/state", headers=headers).json()["data"]
    narrowed = client.get(
        "/api/v1/state",
        params={"include_advanced": "false"},
        headers=headers,
    ).json()["data"]

    advanced = {
        "resource_providers",
        "allocatable_units",
        "scheduler_targets",
        "scheduler_jobs",
        "scheduler_transfers",
        "workload_profiles",
    }
    assert advanced <= full["current"].keys()
    assert not advanced & narrowed["current"].keys()
    assert "resource_plan_evaluations" in full["history"]
    assert "resource_plan_evaluations" not in narrowed["history"]

    # Everything the App actually renders is untouched, including the resource
    # claim and run-actual history the usage page reads.
    assert narrowed["current"].keys() == full["current"].keys() - advanced
    for shared in narrowed["current"]:
        assert narrowed["current"][shared] == full["current"][shared], shared
    assert narrowed["history"] == {"resource_run_actuals": full["history"]["resource_run_actuals"]}

    # Omitting the projections is opt-in: the default response is unchanged.
    default = client.get("/api/v1/state", headers=headers).json()["data"]
    assert default["current"].keys() == full["current"].keys()
