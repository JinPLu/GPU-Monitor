from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from gpu_broker import API_CAPABILITIES, cli as cli_module, mcp_server
from gpu_broker.api import create_app
from gpu_broker.cli import app as cli_app
from gpu_broker.config import EndpointConfig, InventoryConfig, ProjectConfig, Settings
from gpu_broker.mcp_server import mcp
from gpu_broker.models import Endpoint
from gpu_broker.schemas import EndpointUpsert, RequestCreate
from tests.helpers import observation, process_for_gpu


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', html)
    assert match is not None
    return match.group(1)


def test_api_gui_and_idempotency(tmp_path: Path, inventory) -> None:
    inventory_path = tmp_path / "inventory.yaml"
    inventory_path.write_text(yaml.safe_dump(inventory.model_dump(mode="json")), encoding="utf-8")
    app = create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'api.sqlite3'}",
            inventory_path=inventory_path,
            session_secret="s" * 32,
        )
    )
    service = app.state.service
    service.ingest_observation(observation(count=1))
    client = TestClient(app)
    home = client.get("/")
    assert home.status_code == 200
    assert "我的计算资源" in home.text
    assert "添加服务器" in home.text
    assert 'id="server-groups"' in home.text
    assert 'id="gpu-detail"' in home.text
    assert 'id="resource-search"' in home.text
    assert 'class="resource-list-head"' in home.text
    assert 'id="toggle-coordination"' in home.text
    assert 'id="coordination-reopen"' in home.text
    assert 'id="refresh-dashboard"' in home.text
    assert 'aria-label="刷新"' in home.text
    assert 'id="refresh-interval"' in home.text
    assert "从不自动刷新" in home.text
    assert 'data-resource-filter="attention"' in home.text
    assert "/static/assets/server-room-background.jpg" in home.text
    assert "展开全部" in home.text
    assert "/static/vendor/phosphor/style.css?v=2.1.2" in home.text
    assert "uPlot.iife.min.js" not in home.text
    assert "API token" not in home.text
    assert '/ui/action/quick-claim' in home.text
    assert '/ui/identities' in home.text
    assert '/ui/projects' not in home.text
    assert 'name="purpose"' not in home.text
    headers = {"X-GPU-Broker-Actor": "test-agent", "Idempotency-Key": "api-key"}
    payload = {
        "project_id": "project-a",
        "task_ref": "api-request",
        "purpose": "API test",
        "constraints": {
            "gpu_count": 1,
            "min_available_cpu_cores": 16,
            "min_available_memory_mib": 64 * 1024,
            "min_free_vram_mib": 60 * 1024,
            "min_total_vram_mib": 80 * 1024,
        },
    }
    first = client.post("/api/v1/requests", json=payload, headers=headers)
    second = client.post("/api/v1/requests", json=payload, headers=headers)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert first.json()["request"]["duration_seconds"] == 8 * 60 * 60
    snapshot = client.get("/api/v1/snapshot", headers={"X-GPU-Broker-Actor": "test-agent"})
    assert snapshot.status_code == 200
    assert snapshot.json()["data"]["gpus"][0]["state"] == "HELD"
    capabilities = client.get("/health/live").json()["capabilities"]
    assert capabilities[: len(API_CAPABILITIES)] == list(API_CAPABILITIES)
    assert "endpoint_deletion" in capabilities
    assert {"endpoint_update", "endpoint_pause_resume", "endpoint_retirement"}.issubset(
        capabilities
    )
    compact = client.get(
        "/api/v1/gpus?compact=true",
        headers={"X-GPU-Broker-Actor": "test-agent"},
    )
    assert compact.status_code == 200
    assert "processes" not in compact.json()["data"][0]
    assert compact.json()["data"][0]["owner"] == "test-agent"
    history = client.get(
        f"/api/v1/gpus/{compact.json()['data'][0]['id']}/history?window_seconds=3600&points=120",
        headers={"X-GPU-Broker-Actor": "test-agent"},
    )
    assert history.status_code == 200
    assert history.json()["data"]["point_count"] <= 120
    endpoint_history = client.get(
        "/api/v1/endpoints/endpoint-a/history?window_seconds=3600&points=120",
        headers={"X-GPU-Broker-Actor": "test-agent"},
    )
    assert endpoint_history.status_code == 200
    assert endpoint_history.json()["data"]["point_count"] <= 120
    invalid_endpoint_history = client.get(
        "/api/v1/endpoints/endpoint-a/history?window_seconds=300",
        headers={"X-GPU-Broker-Actor": "test-agent"},
    )
    assert invalid_endpoint_history.status_code == 422
    requests = client.get("/ui/requests")
    assert requests.status_code == 200
    assert "申请 GPU" in requests.text
    assert "可用 CPU 核数" in requests.text
    assert "可用内存 GiB" in requests.text
    assert "单卡可用显存 GiB" in requests.text


def test_snapshot_api_uses_latest_complete_gpu_set(tmp_path: Path, inventory) -> None:
    inventory_path = tmp_path / "inventory.yaml"
    inventory_path.write_text(yaml.safe_dump(inventory.model_dump(mode="json")), encoding="utf-8")
    app = create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'latest.sqlite3'}",
            inventory_path=inventory_path,
            session_secret="s" * 32,
        )
    )
    service = app.state.service
    service.ingest_observation(
        observation(gpu_uuids=["GPU-old-0", "GPU-old-1", "GPU-stays"])
    )
    service.ingest_observation(observation(gpu_uuids=["GPU-new-0", "GPU-stays"]))

    client = TestClient(app)
    snapshot = client.get("/api/v1/snapshot", headers={"X-GPU-Broker-Actor": "test-agent"})

    assert snapshot.status_code == 200
    data = snapshot.json()["data"]
    assert [gpu["id"] for gpu in data["gpus"]] == [
        "endpoint-a:GPU-new-0",
        "endpoint-a:GPU-stays",
    ]
    assert data["summary"]["total_gpus"] == 2
    assert data["endpoints"][0]["monitor"]["gpu_count"] == 2


def test_control_plane_state_api_exposes_current_and_history_contract(
    tmp_path: Path, inventory
) -> None:
    inventory_path = tmp_path / "state-contract.yaml"
    inventory_path.write_text(yaml.safe_dump(inventory.model_dump(mode="json")), encoding="utf-8")
    app = create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'state-contract.sqlite3'}",
            inventory_path=inventory_path,
            session_secret="s" * 32,
        )
    )
    app.state.service.ingest_observation(observation(count=1))
    client = TestClient(app)

    response = client.get("/api/v1/state", headers={"X-GPU-Broker-Actor": "test-agent"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] == "v1"
    assert isinstance(payload["snapshot_revision"], int)
    assert payload["server_time"]
    assert set(payload["data"]) == {"current", "history"}
    current = payload["data"]["current"]
    history = payload["data"]["history"]
    assert {
        "summary",
        "endpoints",
        "gpus",
        "leases",
        "requests",
        "reservations",
        "resource_providers",
        "scheduler_targets",
        "scheduler_jobs",
        "workload_profiles",
    }.issubset(current)
    assert {
        "retired_endpoints",
        "resource_plan_evaluations",
        "resource_run_actuals",
    }.issubset(history)
    assert current["gpus"][0]["state"] == "AVAILABLE"


def test_lease_api_suppresses_executable_resources_when_claimed_gpu_absent(
    tmp_path: Path, inventory
) -> None:
    inventory_path = tmp_path / "inventory.yaml"
    inventory_path.write_text(yaml.safe_dump(inventory.model_dump(mode="json")), encoding="utf-8")
    app = create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'lease-presence.sqlite3'}",
            inventory_path=inventory_path,
            session_secret="s" * 32,
        )
    )
    service = app.state.service
    actor = service.local_actor("test-agent")
    service.ingest_observation(observation(gpu_uuids=["GPU-old", "GPU-new"]))
    service.create_request(
        actor,
        RequestCreate.model_validate(
            {
                "project_id": "project-a",
                "task_ref": "api-two-gpu",
                "purpose": "API resource suppression test",
                "duration_seconds": 3600,
                "constraints": {"gpu_count": 2, "placement": "pack"},
            }
        ),
        idempotency_key="api-two-gpu",
    )
    service.ingest_observation(observation(gpu_uuids=["GPU-new"]))

    client = TestClient(app)
    leases = client.get("/api/v1/leases", headers={"X-GPU-Broker-Actor": "test-agent"})

    assert leases.status_code == 200
    lease = leases.json()["data"][0]
    assert set(lease["gpu_ids"]) == {"endpoint-a:GPU-old", "endpoint-a:GPU-new"}
    assert lease["absent_gpu_ids"] == ["endpoint-a:GPU-old"]
    assert lease["resources"] == []


def test_workload_profile_rest_and_gui_claim(tmp_path: Path, inventory) -> None:
    inventory_path = tmp_path / "inventory.yaml"
    inventory_path.write_text(yaml.safe_dump(inventory.model_dump(mode="json")), encoding="utf-8")
    app = create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'profiles.sqlite3'}",
            inventory_path=inventory_path,
            session_secret="s" * 32,
        )
    )
    service = app.state.service
    service.ingest_observation(observation(count=1))
    client = TestClient(app)
    headers = {"X-GPU-Broker-Actor": "profile-agent", "Idempotency-Key": "profile-upsert"}
    profile = {
        "id": "api-eval-1gpu",
        "project_id": "project-a",
        "display_name": "API evaluation",
        "purpose": "approved API evaluation",
        "duration_seconds": 3600,
        "constraints": {
            "gpu_count": 1,
            "placement": "pack",
            "endpoint_ids": ["endpoint-a"],
        },
        "enabled": True,
    }
    created = client.post("/api/v1/workload-profiles", json=profile, headers=headers)
    assert created.status_code == 200
    assert created.json()["workload_profile"]["id"] == "api-eval-1gpu"

    listed = client.get(
        "/api/v1/workload-profiles?project_id=project-a",
        headers={"X-GPU-Broker-Actor": "profile-agent"},
    )
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["data"]] == ["api-eval-1gpu"]

    page = client.get("/ui/requests")
    assert page.status_code == 200
    assert '/ui/action/profile-claim' in page.text
    assert 'value="api-eval-1gpu"' in page.text
    claimed = client.post(
        "/ui/action/profile-claim",
        data={
            "profile_id": "api-eval-1gpu",
            "task_ref": "profile-gui-task",
            "csrf": _csrf(page.text),
            "confirmed": "yes",
        },
        follow_redirects=True,
    )
    assert claimed.status_code == 200
    assert "GPU 已申领，待使用" in claimed.text
    request = service.list_requests(service.local_actor("human"))["data"][0]
    assert request["profile_id"] == "api-eval-1gpu"
    assert request["purpose"] == "approved API evaluation"
    assert request["state"] == "LEASED"


def test_api_claim_starts_held_without_a_duration_estimate(tmp_path: Path, inventory) -> None:
    inventory_path = tmp_path / "inventory.yaml"
    inventory_path.write_text(yaml.safe_dump(inventory.model_dump(mode="json")), encoding="utf-8")
    app = create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'claim.sqlite3'}",
            inventory_path=inventory_path,
            session_secret="s" * 32,
        )
    )
    app.state.service.ingest_observation(observation(count=1))
    client = TestClient(app)
    claimed = client.post(
        "/api/v1/claims",
        json={
            "project_id": "s",
            "task_ref": "api-claim",
            "purpose": "api-claim",
            "constraints": {"gpu_count": 1},
        },
        headers={"X-GPU-Broker-Actor": "claim-agent", "Idempotency-Key": "api-claim"},
    )
    assert claimed.status_code == 200
    assert claimed.json()["request"]["state"] == "LEASED"
    assert claimed.json()["lease"]["state"] == "HELD"
    assert claimed.json()["lease"]["project_id"] == "s"
    assert claimed.json()["request"]["duration_seconds"] == 8 * 60 * 60


def test_api_claim_bootstraps_an_empty_project_registry(tmp_path: Path) -> None:
    inventory = InventoryConfig(
        schema_version=1,
        endpoints=[
            EndpointConfig(
                id="endpoint-a",
                host="127.0.0.1",
                port=2201,
                ssh_user="gpu",
            )
        ],
    )
    inventory_path = tmp_path / "inventory.yaml"
    inventory_path.write_text(yaml.safe_dump(inventory.model_dump(mode="json")), encoding="utf-8")
    app = create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'empty-projects.sqlite3'}",
            inventory_path=inventory_path,
            session_secret="s" * 32,
        )
    )
    app.state.service.ingest_observation(observation(count=1))
    client = TestClient(app)

    claimed = client.post(
        "/api/v1/claims",
        json={
            "project_id": "x",
            "task_ref": "unregistered-project",
            "purpose": "unregistered-project",
            "constraints": {"gpu_count": 1},
        },
        headers={"X-GPU-Broker-Actor": "claim-agent", "Idempotency-Key": "claim-empty-projects"},
    )

    assert claimed.status_code == 200
    assert claimed.json()["lease"]["project_id"] == "x"


def test_general_resource_rest_contracts_delegate_and_fail_closed(tmp_path: Path, inventory) -> None:
    inventory_path = tmp_path / "inventory.yaml"
    inventory_path.write_text(yaml.safe_dump(inventory.model_dump(mode="json")), encoding="utf-8")
    app = create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'general-resources.sqlite3'}",
            inventory_path=inventory_path,
            session_secret="s" * 32,
        )
    )
    service = app.state.service
    calls = []

    service.list_resource_providers = lambda actor, provider_type=None, enabled=None: {  # type: ignore[attr-defined]
        "schema_version": "v1",
        "data": [{"actor": actor.id, "provider_type": provider_type, "enabled": enabled}],
    }
    service.resource_monitor = lambda actor, project_id=None: {  # type: ignore[attr-defined]
        "schema_version": "v1",
        "data": {"actor": actor.id, "project_id": project_id},
    }

    def evaluate_resource_plan(actor, evaluation, *, idempotency_key):  # type: ignore[no-untyped-def]
        calls.append(("evaluate", actor.id, evaluation.project_id, idempotency_key))
        return {"schema_version": "v1", "evaluation": {"project_id": evaluation.project_id}}

    def claim_resource(actor, claim, *, idempotency_key):  # type: ignore[no-untyped-def]
        calls.append(("claim", actor.id, claim.quantities.cpu_cores, idempotency_key))
        return {"schema_version": "v1", "claim": {"project_id": claim.project_id}}

    def release_resource_claim(actor, claim_id, *, reason, idempotency_key):  # type: ignore[no-untyped-def]
        calls.append(("release", actor.id, claim_id, reason, idempotency_key))
        return {"schema_version": "v1", "claim": {"id": claim_id, "state": "RELEASED"}}

    def record_resource_run_actual(actor, actual, *, claim_id=None, evaluation_id=None, idempotency_key):  # type: ignore[no-untyped-def]
        calls.append(("actual", actor.id, actual.outcome, claim_id, evaluation_id, idempotency_key))
        return {"schema_version": "v1", "actual": {"outcome": actual.outcome}}

    service.evaluate_resource_plan = evaluate_resource_plan  # type: ignore[attr-defined]
    service.claim_resource = claim_resource  # type: ignore[attr-defined]
    service.release_resource_claim = release_resource_claim  # type: ignore[attr-defined]
    service.record_resource_run_actual = record_resource_run_actual  # type: ignore[attr-defined]

    client = TestClient(app)
    headers = {"X-GPU-Broker-Actor": "resource-agent", "Idempotency-Key": "resource-key"}
    providers = client.get(
        "/api/v1/resource-providers?provider_type=host-capacity&enabled=true",
        headers={"X-GPU-Broker-Actor": "resource-agent"},
    )
    assert providers.status_code == 200
    assert providers.json()["data"][0] == {
        "actor": "resource-agent",
        "provider_type": "host-capacity",
        "enabled": True,
    }
    monitor = client.get(
        "/api/v1/resource-monitor?project_id=project-a",
        headers={"X-GPU-Broker-Actor": "resource-agent"},
    )
    assert monitor.status_code == 200
    assert monitor.json()["data"]["project_id"] == "project-a"
    missing = client.get(
        "/api/v1/resource-claims",
        headers={"X-GPU-Broker-Actor": "resource-agent"},
    )
    assert missing.status_code == 200
    assert missing.json()["data"] == []

    evaluation = {
        "project_id": "project-a",
        "task_ref": "train-1",
        "baseline_runtime_seconds": 1200,
        "marginal_min_saved_seconds": 120,
        "marginal_min_saved_ratio": 0.10,
        "selected_candidate_key": "cpu-8",
        "candidates": [
            {
                "candidate_key": "cpu-8",
                "provider_type": "host-capacity",
                "quantities": {"cpu_cores": 8, "memory_mib": 32768},
                "predicted_runtime_seconds": 600,
                "predicted_saved_seconds": 600,
                "predicted_saved_ratio": 0.5,
                "satisfies_marginal_threshold": True,
                "selected": True,
            }
        ],
    }
    assert client.post("/api/v1/resource-plan-evaluations", json=evaluation, headers=headers).status_code == 200
    claim = {
        "project_id": "project-a",
        "task_ref": "train-1",
        "purpose": "cpu-only training",
        "provider_type": "host-capacity",
        "quantities": {"cpu_cores": 8, "memory_mib": 32768},
        "forecast": {
            "quantities": {"cpu_cores": 8, "memory_mib": 32768},
            "predicted_runtime_seconds": 600,
            "predicted_saved_seconds": 600,
            "predicted_saved_ratio": 0.5,
        },
    }
    assert client.post("/api/v1/resource-claims", json=claim, headers=headers).status_code == 200
    assert (
        client.post(
            "/api/v1/resource-claims/claim-1/release",
            json={"reason": "done"},
            headers=headers,
        ).status_code
        == 200
    )
    actual = {
        "project_id": "project-a",
        "task_ref": "train-1",
        "quantities": {"cpu_cores": 8, "memory_mib": 32768},
        "started_at": datetime(2026, 8, 4, 1, 0, tzinfo=UTC).isoformat(),
        "completed_at": datetime(2026, 8, 4, 1, 10, tzinfo=UTC).isoformat(),
        "actual_duration_seconds": 600,
        "outcome": "succeeded",
    }
    assert (
        client.post(
            "/api/v1/resource-run-actuals?claim_id=claim-1&evaluation_id=eval-1",
            json=actual,
            headers=headers,
        ).status_code
        == 200
    )
    assert calls == [
        ("evaluate", "resource-agent", "project-a", "resource-key"),
        ("claim", "resource-agent", 8.0, "resource-key"),
        ("release", "resource-agent", "claim-1", "done", "resource-key"),
        ("actual", "resource-agent", "succeeded", "claim-1", "eval-1", "resource-key"),
    ]


def test_coordination_api_and_observed_binding(tmp_path: Path, inventory) -> None:
    inventory_path = tmp_path / "inventory.yaml"
    inventory_path.write_text(yaml.safe_dump(inventory.model_dump(mode="json")), encoding="utf-8")
    app = create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'coordination.sqlite3'}",
            inventory_path=inventory_path,
            session_secret="s" * 32,
        )
    )
    service = app.state.service
    service.ingest_observation(observation(count=1))
    client = TestClient(app)
    claim_headers = {"X-GPU-Broker-Actor": "coordination-agent", "Idempotency-Key": "coordination-claim"}
    claimed = client.post(
        "/api/v1/claims",
        json={
            "project_id": "project-a",
            "task_ref": "coordination-api-run",
            "purpose": "coordination-api-run",
            "constraints": {"gpu_count": 1},
        },
        headers=claim_headers,
    )
    assert claimed.status_code == 200
    lease_id = claimed.json()["lease"]["id"]
    gpu = service.list_gpus(service.local_actor("coordination-agent"))["data"][0]
    service.ingest_observation(observation(count=1, processes=[process_for_gpu(gpu["gpu_uuid"])]))

    bound = client.post(
        f"/api/v1/leases/{lease_id}/bind-observed-workload",
        json={},
        headers={"X-GPU-Broker-Actor": "coordination-agent", "Idempotency-Key": "coordination-bind"},
    )
    assert bound.status_code == 200
    assert bound.json()["lease"]["workloads"][0]["run_id"] == f"lease:{lease_id}"
    coordination = client.get(
        "/api/v1/coordination",
        headers={"X-GPU-Broker-Actor": "coordination-agent"},
    )
    assert coordination.status_code == 200
    capacity = coordination.json()["data"]["servers"][0]["capacity"]
    assert capacity["available_cpu_cores"] == 60.0
    assert capacity["available_memory_mib"] == 196_608
    assert capacity["total_vram_mib"] == 100_000
    board = client.get("/api/v1/coordination", headers={"X-GPU-Broker-Actor": "coordination-agent"})
    assert board.status_code == 200
    assert board.json()["data"]["servers"][0]["capacity"]["managed_running_gpus"] == 1
    assert board.json()["data"]["leases"][0]["activity"] == "running"


def test_endpoint_project_grant_route_is_not_exposed(tmp_path: Path, inventory) -> None:
    inventory_path = tmp_path / "inventory.yaml"
    inventory_path.write_text(yaml.safe_dump(inventory.model_dump(mode="json")), encoding="utf-8")
    app = create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'endpoint-project.sqlite3'}",
            inventory_path=inventory_path,
            session_secret="s" * 32,
        )
    )
    client = TestClient(app)
    response = client.post(
        "/api/v1/endpoints/endpoint-a/projects",
        json={"project_id": "storyboard"},
        headers={"X-GPU-Broker-Actor": "endpoint-admin", "Idempotency-Key": "unused"},
    )
    assert response.status_code == 404


def test_collector_observation_ingestion_is_not_a_public_actor_route(tmp_path: Path, inventory) -> None:
    inventory_path = tmp_path / "inventory.yaml"
    inventory_path.write_text(yaml.safe_dump(inventory.model_dump(mode="json")), encoding="utf-8")
    app = create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'collector-private.sqlite3'}",
            inventory_path=inventory_path,
            session_secret="s" * 32,
        )
    )
    client = TestClient(app)
    response = client.post(
        "/api/v1/internal/observations",
        json=observation(count=1).model_dump(mode="json"),
        headers={"X-GPU-Broker-Actor": "arbitrary-actor"},
    )
    assert response.status_code == 404


def test_endpoint_delete_rest_route_is_idempotent(tmp_path: Path, inventory) -> None:
    inventory_path = tmp_path / "inventory.yaml"
    inventory_path.write_text(yaml.safe_dump(inventory.model_dump(mode="json")), encoding="utf-8")
    app = create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'endpoint-delete.sqlite3'}",
            inventory_path=inventory_path,
            session_secret="s" * 32,
        )
    )
    client = TestClient(app)
    with app.state.service.database.session() as session:
        endpoint = session.get(Endpoint, "endpoint-b")
        assert endpoint is not None
        endpoint.owner_project_id = None
        session.commit()

    missing_key = client.delete(
        "/api/v1/endpoints/endpoint-b",
        headers={"X-GPU-Broker-Actor": "endpoint-admin"},
    )
    assert missing_key.status_code == 422
    assert missing_key.json()["error"]["code"] == "idempotency_key_required"

    headers = {"X-GPU-Broker-Actor": "endpoint-admin", "Idempotency-Key": "delete-endpoint-b"}
    deleted = client.delete("/api/v1/endpoints/endpoint-b", headers=headers)
    retried = client.delete("/api/v1/endpoints/endpoint-b", headers=headers)

    assert deleted.status_code == 200
    assert retried.json() == deleted.json()
    assert deleted.json()["endpoint_id"] == "endpoint-b"
    listed = client.get("/api/v1/endpoints", headers={"X-GPU-Broker-Actor": "endpoint-admin"})
    endpoints = {endpoint["id"]: endpoint for endpoint in listed.json()["data"]}
    assert endpoints["endpoint-b"]["lifecycle_state"] == "draining"


def test_endpoint_rest_lifecycle_uses_explicit_create_update_pause_resume_retire(
    tmp_path: Path, inventory
) -> None:
    inventory_path = tmp_path / "inventory.yaml"
    inventory_path.write_text(yaml.safe_dump(inventory.model_dump(mode="json")), encoding="utf-8")
    app = create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'endpoint-lifecycle.sqlite3'}",
            inventory_path=inventory_path,
            session_secret="s" * 32,
        )
    )
    client = TestClient(app)
    actor = {"X-GPU-Broker-Actor": "endpoint-admin"}
    endpoint = {
        "id": "endpoint-lifecycle",
        "host": "127.0.0.1",
        "port": 2399,
        "ssh_user": "gpu",
    }
    created = client.post(
        "/api/v1/endpoints", json=endpoint, headers={**actor, "Idempotency-Key": "endpoint-create"}
    )
    assert created.status_code == 200
    assert created.json()["endpoint"]["lifecycle_state"] == "active"
    assert created.json()["endpoint"]["observation_profile"] == "server-script-v1"
    duplicate = client.post(
        "/api/v1/endpoints", json=endpoint, headers={**actor, "Idempotency-Key": "endpoint-create-new"}
    )
    assert duplicate.status_code == 409
    identity_patch = client.patch(
        "/api/v1/endpoints/endpoint-lifecycle",
        json={"host": "127.0.0.2"},
        headers={**actor, "Idempotency-Key": "endpoint-host-change"},
    )
    assert identity_patch.status_code == 422
    updated = client.patch(
        "/api/v1/endpoints/endpoint-lifecycle",
        json={"ssh_alias": "lab-script", "labels": ["lab"]},
        headers={**actor, "Idempotency-Key": "endpoint-update"},
    )
    assert updated.status_code == 200
    assert updated.json()["endpoint"]["ssh_alias"] == "lab-script"
    paused = client.post(
        "/api/v1/endpoints/endpoint-lifecycle/pause",
        json={},
        headers={**actor, "Idempotency-Key": "endpoint-pause"},
    )
    assert paused.status_code == 200
    assert paused.json()["endpoint"]["lifecycle_state"] == "draining"
    paused_again = client.post(
        "/api/v1/endpoints/endpoint-lifecycle/pause",
        json={},
        headers={**actor, "Idempotency-Key": "endpoint-pause-again"},
    )
    assert paused_again.json()["changed"] is False
    resumed = client.post(
        "/api/v1/endpoints/endpoint-lifecycle/resume",
        json={},
        headers={**actor, "Idempotency-Key": "endpoint-resume"},
    )
    assert resumed.status_code == 200
    assert resumed.json()["endpoint"]["lifecycle_state"] == "active"
    active_retire = client.post(
        "/api/v1/endpoints/endpoint-lifecycle/retire",
        json={},
        headers={**actor, "Idempotency-Key": "endpoint-retire-active"},
    )
    assert active_retire.status_code == 409
    client.post(
        "/api/v1/endpoints/endpoint-lifecycle/pause",
        json={},
        headers={**actor, "Idempotency-Key": "endpoint-pause-before-retire"},
    )
    retired = client.post(
        "/api/v1/endpoints/endpoint-lifecycle/retire",
        json={},
        headers={**actor, "Idempotency-Key": "endpoint-retire"},
    )
    assert retired.status_code == 200
    assert retired.json()["endpoint"]["lifecycle_state"] == "retired"
    retired_update = client.patch(
        "/api/v1/endpoints/endpoint-lifecycle",
        json={"ssh_user": "other"},
        headers={**actor, "Idempotency-Key": "endpoint-update-retired"},
    )
    assert retired_update.status_code == 409


def test_endpoint_delete_rest_route_preserves_maintenance_history(tmp_path: Path, inventory) -> None:
    inventory_path = tmp_path / "inventory.yaml"
    inventory_path.write_text(yaml.safe_dump(inventory.model_dump(mode="json")), encoding="utf-8")
    app = create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'endpoint-delete-error.sqlite3'}",
            inventory_path=inventory_path,
            session_secret="s" * 32,
        )
    )
    client = TestClient(app)
    headers = {"X-GPU-Broker-Actor": "human", "Idempotency-Key": "endpoint-maintenance"}
    created = client.post(
        "/api/v1/maintenance",
        json={
            "endpoint_id": "endpoint-b",
            "start_at": "2026-07-20T00:00:00+00:00",
            "end_at": "2026-07-20T01:00:00+00:00",
            "reason": "hardware inspection",
        },
        headers=headers,
    )
    assert created.status_code == 200

    drained = client.delete(
        "/api/v1/endpoints/endpoint-b",
        headers={"X-GPU-Broker-Actor": "human", "Idempotency-Key": "delete-maintained"},
    )

    assert drained.status_code == 200
    assert drained.json()["history_retained"] is True
    assert drained.json()["endpoint"]["lifecycle_state"] == "draining"
    assert created.json()["maintenance"]["id"]


def test_project_creation_route_and_gui_are_not_exposed(tmp_path: Path, inventory) -> None:
    inventory_path = tmp_path / "inventory.yaml"
    inventory_path.write_text(yaml.safe_dump(inventory.model_dump(mode="json")), encoding="utf-8")
    app = create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'no-project-admin.sqlite3'}",
            inventory_path=inventory_path,
            session_secret="s" * 32,
        )
    )
    client = TestClient(app)

    response = client.post(
        "/api/v1/projects",
        json={"id": "storyboard", "display_name": "Storyboard"},
        headers={"X-GPU-Broker-Actor": "project-admin", "Idempotency-Key": "unused"},
    )
    assert response.status_code == 405
    assert client.get("/ui/projects").status_code == 404
    identities = client.get("/ui/identities")
    assert identities.status_code == 200
    assert "/ui/action/project" not in identities.text


def test_click_first_gui_forms_and_all_human_pages(tmp_path: Path, inventory) -> None:
    inventory_path = tmp_path / "inventory.yaml"
    inventory_path.write_text(yaml.safe_dump(inventory.model_dump(mode="json")), encoding="utf-8")
    app = create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'clicks.sqlite3'}",
            inventory_path=inventory_path,
            session_secret="s" * 32,
        )
    )
    service = app.state.service
    service.ingest_observation(observation(count=1))
    client = TestClient(app)

    request_page = client.get("/ui/requests")
    assert request_page.status_code == 200
    assert 'name="task_ref"' in request_page.text
    assert 'name="purpose"' not in request_page.text
    assert '/ui/action/quick-claim' in request_page.text
    assert "JSON payload" not in request_page.text
    submitted = client.post(
        "/ui/action/quick-claim",
        data={
            "project_id": "project-a",
            "task_ref": "click-first-request",
            "gpu_count": "1",
            "placement": "pack",
            "endpoint_id": "",
            "csrf": _csrf(request_page.text),
            "confirmed": "yes",
        },
        follow_redirects=True,
    )
    assert submitted.status_code == 200
    assert "GPU 已申领，待使用" in submitted.text

    lease = service.list_leases(service.local_actor("human"))["data"][0]
    assert lease["state"] == "HELD"
    request = service.list_requests(service.local_actor("human"))["data"][0]
    assert request["state"] == "LEASED"
    assert request["purpose"] == "click-first-request"

    home_page = client.get("/")
    added_server = client.post(
        "/ui/action/endpoint",
        data={
            "id": "click-server",
            "host": "127.0.0.2",
            "port": "2203",
            "ssh_user": "gpu",
            "owner_project_id": "project-a",
            "expected_gpu_count": "2",
            "enabled": "true",
            "csrf": _csrf(home_page.text),
            "confirmed": "yes",
        },
        follow_redirects=True,
    )
    assert added_server.status_code == 200
    assert "click-server" in added_server.text
    removed_server = client.post(
        "/ui/action/delete-endpoint",
        data={
            "endpoint_id": "click-server",
            "csrf": _csrf(added_server.text),
            "confirmed": "yes",
        },
        follow_redirects=True,
    )
    assert removed_server.status_code == 200
    endpoints = {endpoint["id"]: endpoint for endpoint in service.list_endpoints(service.local_actor("human"))["data"]}
    assert endpoints["click-server"]["lifecycle_state"] == "draining"

    switched = client.post("/ui/actor", data={"actor_id": "click-agent"}, follow_redirects=True)
    assert switched.status_code == 200
    assert 'value="click-agent"' in switched.text

    for page in ["/", "/ui/gpus", "/ui/requests", "/ui/leases", "/ui/reservations", "/ui/identities", "/ui/maintenance", "/ui/alerts", "/ui/audit", "/ui/doctor"]:
        response = client.get(page)
        assert response.status_code == 200, page
    gpu_id = service.list_gpus(service.local_actor("click-agent"))["data"][0]["id"]
    assert client.get(f"/ui/gpus/{gpu_id}").status_code == 200


def test_mcp_exposes_required_tools() -> None:
    tools = asyncio.run(mcp.list_tools())
    by_name = {tool.name: tool for tool in tools}
    names = set(by_name)
    assert {
        "control_plane_state",
        "gpu_status",
        "gpu_coordination",
        "gpu_list",
        "gpu_who",
        "gpu_list_profiles",
        "gpu_scheduler_targets",
        "gpu_scheduler_access_status",
        "gpu_scheduler_profiles",
        "gpu_scheduler_submit_profile",
        "gpu_scheduler_submit_once",
        "gpu_scheduler_job_status",
        "gpu_scheduler_cancel",
        "gpu_request",
        "gpu_request_status",
        "gpu_cancel_request",
        "gpu_activate_lease",
        "gpu_renew_lease",
        "gpu_release_lease",
        "gpu_bind_workload",
        "gpu_bind_observed_workload",
        "gpu_list_reservations",
        "gpu_history",
        "gpu_claim",
        "gpu_claim_profile",
        "gpu_release",
        "gpu_schedule",
        "gpu_add_server",
        "gpu_update_server",
        "gpu_pause_server",
        "gpu_resume_server",
        "gpu_retire_server",
        "gpu_delete_server",
        "resource_providers",
        "resource_monitor",
        "resource_claims",
        "resource_evaluate_plan",
        "resource_claim",
        "resource_release",
        "resource_record_actual",
    }.issubset(names)
    assert "gpu_grant_server_project" not in names
    assert "gpu_scheduler_upload" not in names
    assert "gpu_scheduler_transfer_status" not in names
    for name in ("gpu_claim", "gpu_schedule"):
        assert "project_id" in by_name[name].inputSchema["required"]
    assert {"agent_name", "project_id", "task", "gpu_count"}.issubset(
        by_name["gpu_claim"].inputSchema["required"]
    )
    assert "purpose" not in by_name["gpu_claim"].inputSchema["required"]
    assert "hours" not in by_name["gpu_claim"].inputSchema["properties"]
    assert {"agent_name", "profile_id", "task"}.issubset(
        by_name["gpu_claim_profile"].inputSchema["required"]
    )
    assert "reason" not in by_name["gpu_release"].inputSchema["required"]
    for name in (
        "gpu_add_server",
        "gpu_update_server",
        "gpu_pause_server",
        "gpu_resume_server",
        "gpu_retire_server",
        "gpu_delete_server",
    ):
        assert {"approval_ref", "idempotency_key"}.issubset(by_name[name].inputSchema["required"])


def test_mcp_endpoint_administration_requires_contract_and_uses_rest(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls = []

    class FakeClient:
        def post(self, path, body=None, *, idempotency_key):  # type: ignore[no-untyped-def]
            calls.append(("POST", path, body, idempotency_key))
            return {"endpoint": {"id": "server-a"}}

        def patch(self, path, body=None, *, idempotency_key):  # type: ignore[no-untyped-def]
            calls.append(("PATCH", path, body, idempotency_key))
            return {"endpoint": {"id": "server-a"}}

    monkeypatch.setattr(mcp_server, "_client", lambda actor_name=None: FakeClient())
    with pytest.raises(ValueError, match="approval_ref"):
        mcp_server.gpu_pause_server("agent", "server-a", "", "stable-key")
    with pytest.raises(ValueError, match="idempotency_key"):
        mcp_server.gpu_pause_server("agent", "server-a", "approved-task", "")

    created = mcp_server.gpu_add_server(
        "agent",
        "project-a",
        "10.0.0.8",
        "approved-task",
        "create-stable",
        server_id="server-a",
    )
    assert created["endpoint"]["id"] == "server-a"
    assert calls[-1] == (
        "POST",
        "/api/v1/endpoints",
        {
            "id": "server-a",
            "host": "10.0.0.8",
            "port": 22,
            "ssh_user": "root",
            "ssh_alias": None,
            "observation_profile": "server-script-v1",
            "labels": [],
            "storage_group": None,
            "expected_gpu_count": None,
            "expected_gpu_total_vram_mib": None,
            "owner_project_id": "project-a",
        },
        "create-stable",
    )
    mcp_server.gpu_update_server(
        "agent", "server-a", "approved-task", "update-stable", ssh_user="gpu"
    )
    mcp_server.gpu_pause_server("agent", "server-a", "approved-task", "pause-stable")
    mcp_server.gpu_resume_server("agent", "server-a", "approved-task", "resume-stable")
    mcp_server.gpu_retire_server("agent", "server-a", "approved-task", "retire-stable")
    mcp_server.gpu_delete_server("agent", "server-a", "approved-task", "delete-stable")
    assert calls[1:] == [
        ("PATCH", "/api/v1/endpoints/server-a", {"ssh_user": "gpu"}, "update-stable"),
        ("POST", "/api/v1/endpoints/server-a/pause", {}, "pause-stable"),
        ("POST", "/api/v1/endpoints/server-a/resume", {}, "resume-stable"),
        ("POST", "/api/v1/endpoints/server-a/retire", {}, "retire-stable"),
        ("POST", "/api/v1/endpoints/server-a/pause", {}, "delete-stable"),
    ]


def test_mcp_common_tools_do_not_preflight_health(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls = []

    class FakeClient:
        def control_plane_state(self):  # type: ignore[no-untyped-def]
            calls.append(("STATE", "/api/v1/state"))
            return {
                "schema_version": "v1",
                "snapshot_revision": 1,
                "server_time": "2026-08-06T00:00:00Z",
                "data": {
                    "current": {
                        "coordination": {},
                    },
                    "history": {},
                },
            }

        def coordination(self):  # type: ignore[no-untyped-def]
            state = self.control_plane_state()
            return {
                "schema_version": state["schema_version"],
                "snapshot_revision": state["snapshot_revision"],
                "server_time": state["server_time"],
                "data": state["data"]["current"]["coordination"],
            }

        def post(self, path, body=None, *, idempotency_key):  # type: ignore[no-untyped-def]
            calls.append(("POST", path, body, idempotency_key))
            return {"schema_version": "v1", "request": {}, "lease": None}

    monkeypatch.setattr(mcp_server, "_client", lambda actor_name=None: FakeClient())

    assert mcp_server.gpu_coordination() == {
        "schema_version": "v1",
        "snapshot_revision": 1,
        "server_time": "2026-08-06T00:00:00Z",
        "data": {},
    }
    assert calls == [("STATE", "/api/v1/state")]

    calls.clear()
    assert mcp_server.gpu_claim("agent", "project", "task", 1)["request"] == {}
    assert [call[:2] for call in calls] == [("POST", "/api/v1/claims")]


def test_mcp_general_resource_tools_delegate_and_enforce_marginal_policy(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls = []

    class FakeClient:
        def resource_providers(self, *, provider_type=None, enabled=None):  # type: ignore[no-untyped-def]
            calls.append(("providers", provider_type, enabled))
            return {"schema_version": "v1", "data": []}

        def resource_monitor(self, *, project_id=None):  # type: ignore[no-untyped-def]
            calls.append(("monitor", project_id))
            return {"schema_version": "v1", "data": {}}

        def resource_claims(self, *, project_id=None, state=None):  # type: ignore[no-untyped-def]
            calls.append(("claims", project_id, state))
            return {"schema_version": "v1", "data": []}

        def evaluate_resource_plan(self, evaluation, *, idempotency_key):  # type: ignore[no-untyped-def]
            calls.append(("evaluate", evaluation["selected_candidate_key"], idempotency_key))
            return {"schema_version": "v1", "evaluation": {}}

        def claim_resource(self, claim, *, idempotency_key):  # type: ignore[no-untyped-def]
            calls.append(("claim", claim["quantities"]["cpu_cores"], idempotency_key))
            return {"schema_version": "v1", "claim": {}}

        def release_resource_claim(self, claim_id, *, reason, idempotency_key):  # type: ignore[no-untyped-def]
            calls.append(("release", claim_id, reason, idempotency_key))
            return {"schema_version": "v1", "claim": {}}

        def record_resource_run_actual(self, actual, *, claim_id=None, evaluation_id=None, idempotency_key):  # type: ignore[no-untyped-def]
            calls.append(("actual", actual["outcome"], claim_id, evaluation_id, idempotency_key))
            return {"schema_version": "v1", "actual": {}}

    monkeypatch.setattr(mcp_server, "_client", lambda actor_name=None: FakeClient())

    evaluation = {
        "project_id": "project-a",
        "task_ref": "task",
        "baseline_runtime_seconds": 1200,
        "marginal_min_saved_seconds": 120,
        "marginal_min_saved_ratio": 0.10,
        "selected_candidate_key": "cpu-8",
        "candidates": [
            {
                "candidate_key": "cpu-8",
                "quantities": {"cpu_cores": 8, "memory_mib": 32768},
                "predicted_runtime_seconds": 600,
                "predicted_saved_seconds": 600,
                "predicted_saved_ratio": 0.5,
                "satisfies_marginal_threshold": True,
                "selected": True,
            }
        ],
    }
    assert mcp_server.resource_providers(provider_type="host-capacity")["data"] == []
    assert mcp_server.resource_monitor(project_id="project-a")["data"] == {}
    assert mcp_server.resource_claims(state="ACTIVE")["data"] == []
    assert mcp_server.resource_evaluate_plan("agent", evaluation, idempotency_key="eval-key")["evaluation"] == {}

    invalid_threshold = {**evaluation, "marginal_min_saved_seconds": 60}
    with pytest.raises(ValueError, match="marginal_min_saved_seconds must be 120"):
        mcp_server.resource_evaluate_plan("agent", invalid_threshold)

    claim = {
        "project_id": "project-a",
        "task_ref": "task",
        "purpose": "cpu-only",
        "quantities": {"cpu_cores": 8},
        "forecast": {
            "quantities": {"cpu_cores": 8},
            "predicted_runtime_seconds": 600,
        },
    }
    assert mcp_server.resource_claim("agent", claim, idempotency_key="claim-key")["claim"] == {}
    assert (
        mcp_server.resource_release("agent", "claim-1", reason="done", idempotency_key="release-key")["claim"]
        == {}
    )
    actual = {
        "project_id": "project-a",
        "task_ref": "task",
        "quantities": {"cpu_cores": 8},
        "outcome": "succeeded",
    }
    assert (
        mcp_server.resource_record_actual(
            "agent",
            actual,
            claim_id="claim-1",
            evaluation_id="eval-1",
            idempotency_key="actual-key",
        )["actual"]
        == {}
    )
    assert calls == [
        ("providers", "host-capacity", None),
        ("monitor", "project-a"),
        ("claims", None, "ACTIVE"),
        ("evaluate", "cpu-8", "eval-key"),
        ("claim", 8, "claim-key"),
        ("release", "claim-1", "done", "release-key"),
        ("actual", "succeeded", "claim-1", "eval-1", "actual-key"),
    ]


def test_ssh_preview_commit_is_bound_non_mutating_and_requires_project_scope(tmp_path: Path, inventory) -> None:
    inventory_path = tmp_path / "inventory.yaml"
    inventory_path.write_text(yaml.safe_dump(inventory.model_dump(mode="json")), encoding="utf-8")
    app = create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'ssh-preview.sqlite3'}",
            inventory_path=inventory_path,
            session_secret="s" * 32,
        )
    )
    client = TestClient(app)
    csrf = _csrf(client.get("/").text)
    service = app.state.service
    actor = service.local_actor("human")
    endpoints_before = service.list_endpoints(actor)["data"]
    events_before = service.list_events(actor)["data"]

    preview = client.post(
        "/ui/endpoints/ssh/preview",
        json={"command": "  ssh GPU_User@New-Host  ", "project_ids": ["project-a"], "csrf": csrf},
    )
    assert preview.status_code == 200
    data = preview.json()["data"]
    assert data["status"] == "new"
    assert data["normalized_command"] == "ssh GPU_User@new-host"
    assert data["endpoint"] == {
        "id": "server-new-host-p22",
        "host": "new-host",
        "port": 22,
        "ssh_user": "GPU_User",
        "ssh_alias": None,
        "labels": ["gpu", "direct-ssh"],
        "storage_group": None,
        "expected_gpu_count": None,
        "expected_gpu_total_vram_mib": None,
        "project_ids": ["project-a"],
        "enabled": True,
    }
    assert len(data["preview_token"]) == 64
    assert service.list_endpoints(actor)["data"] == endpoints_before
    assert service.list_events(actor)["data"] == events_before

    tampered_command = client.post(
        "/ui/endpoints/ssh/commit",
        json={
            "command": "ssh GPU_User@other-host",
            "preview_token": data["preview_token"],
            "project_ids": ["project-a"],
            "csrf": csrf,
        },
    )
    assert tampered_command.status_code == 409
    assert tampered_command.json()["error"]["code"] == "invalid_ssh_preview_token"
    tampered_token = client.post(
        "/ui/endpoints/ssh/commit",
        json={
            "command": "  ssh GPU_User@New-Host  ",
            "preview_token": "0" * 64,
            "project_ids": ["project-a"],
            "csrf": csrf,
        },
    )
    assert tampered_token.status_code == 409
    assert tampered_token.json()["error"]["code"] == "invalid_ssh_preview_token"
    assert service.list_endpoints(actor)["data"] == endpoints_before

    committed = client.post(
        "/ui/endpoints/ssh/commit",
        json={
            "command": "  ssh GPU_User@New-Host  ",
            "preview_token": data["preview_token"],
            "project_ids": ["project-a"],
            "csrf": csrf,
        },
    )
    assert committed.status_code == 200
    assert committed.json()["data"]["endpoint"]["id"] == "server-new-host-p22"


def test_ssh_preview_reports_existing_address_and_id_collision(tmp_path: Path, inventory) -> None:
    inventory_path = tmp_path / "inventory.yaml"
    inventory_path.write_text(yaml.safe_dump(inventory.model_dump(mode="json")), encoding="utf-8")
    app = create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'ssh-collisions.sqlite3'}",
            inventory_path=inventory_path,
            session_secret="s" * 32,
        )
    )
    client = TestClient(app)
    csrf = _csrf(client.get("/").text)
    service = app.state.service
    actor = service.local_actor("human")

    existing = client.post(
        "/ui/endpoints/ssh/preview",
        json={"command": "ssh -p 2201 gpu@127.0.0.1", "project_ids": ["project-a"], "csrf": csrf},
    )
    assert existing.status_code == 200
    assert existing.json()["data"]["status"] == "existing"
    assert existing.json()["data"]["endpoint"]["id"] == "endpoint-a"
    assert existing.json()["data"]["existing_endpoint"]["id"] == "endpoint-a"

    service.upsert_endpoint(
        actor,
        EndpointUpsert(
            id="server-collision-host-p22",
            host="other-host",
            port=22,
            ssh_user="gpu",
            project_ids=["project-a"],
        ),
        idempotency_key="collision-setup",
    )
    collision = client.post(
        "/ui/endpoints/ssh/preview",
        json={"command": "ssh gpu@collision-host", "project_ids": ["project-a"], "csrf": csrf},
    )
    assert collision.status_code == 200
    collision_data = collision.json()["data"]
    assert collision_data["status"] == "id_collision"
    assert collision_data["id_collision"]["host"] == "other-host"

    rejected = client.post(
        "/ui/endpoints/ssh/commit",
        json={
            "command": "ssh gpu@collision-host",
            "project_ids": ["project-a"],
            "preview_token": collision_data["preview_token"],
            "csrf": csrf,
        },
    )
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "endpoint_id_collision"
    resolved = client.post(
        "/ui/endpoints/ssh/commit",
        json={
            "command": "ssh gpu@collision-host",
            "endpoint_id": "collision-host-explicit",
            "project_ids": ["project-a"],
            "preview_token": collision_data["preview_token"],
            "csrf": csrf,
        },
    )
    assert resolved.status_code == 200
    assert resolved.json()["data"]["endpoint"]["id"] == "collision-host-explicit"


def test_ssh_batch_registers_valid_lines_and_skips_invalid_or_duplicate_lines(tmp_path: Path, inventory) -> None:
    inventory_path = tmp_path / "inventory.yaml"
    inventory_path.write_text(yaml.safe_dump(inventory.model_dump(mode="json")), encoding="utf-8")
    app = create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'ssh-batch.sqlite3'}",
            inventory_path=inventory_path,
            session_secret="s" * 32,
        )
    )
    client = TestClient(app)
    csrf = _csrf(client.get("/").text)
    commands = [
        "ssh -p 2201 gpu@batch-host",
        "not an ssh command",
        "ssh -p 2202 gpu@batch-host",
        "ssh -p 2201 root@batch-host",
    ]

    preview = client.post(
        "/ui/endpoints/ssh/batch/preview",
        json={"commands": commands, "project_ids": ["project-a"], "csrf": csrf},
    )
    assert preview.status_code == 200
    preview_data = preview.json()["data"]
    assert preview_data["valid_count"] == 2
    assert [entry["status"] for entry in preview_data["entries"]] == ["new", "invalid", "new", "duplicate"]

    committed = client.post(
        "/ui/endpoints/ssh/batch/commit",
        json={
            "commands": commands,
            "project_ids": ["project-a"],
            "preview_token": preview_data["preview_token"],
            "csrf": csrf,
        },
    )
    assert committed.status_code == 200
    result = committed.json()["data"]
    assert result["registered_count"] == 2
    assert result["updated_count"] == 0
    assert [entry["status"] for entry in result["entries"]] == ["registered", "invalid", "registered", "duplicate"]


def test_app_starts_with_projects_and_no_endpoints(tmp_path: Path) -> None:
    inventory = InventoryConfig(
        schema_version=1,
        projects=[ProjectConfig(id="project-a", display_name="Project A")],
        endpoints=[],
    )
    inventory_path = tmp_path / "empty-inventory.yaml"
    inventory_path.write_text(yaml.safe_dump(inventory.model_dump(mode="json")), encoding="utf-8")
    app = create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'empty.sqlite3'}",
            inventory_path=inventory_path,
            session_secret="s" * 32,
        )
    )
    client = TestClient(app)
    home = client.get("/")
    assert home.status_code == 200
    assert "添加第一台 GPU 服务器" in home.text
    assert "ssh -p 22 gpu@gpu-host.example.com" in home.text
    response = client.get("/api/v1/endpoints", headers={"X-GPU-Broker-Actor": "agent"})
    assert response.status_code == 200
    assert response.json()["data"] == []


def test_cli_help_is_available() -> None:
    result = CliRunner().invoke(cli_app, ["--help"])
    assert result.exit_code == 0
    assert "status" in result.stdout


def test_cli_state_uses_canonical_client_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    class FakeClient:
        def control_plane_state(
            self,
            *,
            minimum_snapshot_revision=None,
            timeout_seconds=0,
            poll_interval_seconds=0.25,
        ):  # type: ignore[no-untyped-def]
            calls.append((minimum_snapshot_revision, timeout_seconds, poll_interval_seconds))
            return {
                "schema_version": "v1",
                "snapshot_revision": 12,
                "server_time": "2026-08-06T00:00:00Z",
                "data": {"current": {"gpus": []}, "history": {}},
            }

    monkeypatch.setattr(cli_module, "_client", lambda url, actor: FakeClient())

    result = CliRunner().invoke(
        cli_app,
        [
            "state",
            "--minimum-snapshot-revision",
            "12",
            "--timeout-seconds",
            "2",
            "--poll-interval-seconds",
            "0.5",
        ],
    )

    assert result.exit_code == 0
    assert '"snapshot_revision": 12' in result.stdout
    assert calls == [(12, 2.0, 0.5)]


def test_cli_resource_evaluate_uses_client_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    class FakeClient:
        def evaluate_resource_plan(self, evaluation, *, idempotency_key):  # type: ignore[no-untyped-def]
            calls.append((evaluation["selected_candidate_key"], bool(idempotency_key)))
            return {"schema_version": "v1", "evaluation": {"id": "eval-1"}}

    monkeypatch.setattr(cli_module, "_client", lambda url, actor: FakeClient())
    payload = {
        "project_id": "project-a",
        "task_ref": "cpu-task",
        "baseline_runtime_seconds": 1200,
        "selected_candidate_key": "cpu-8",
        "candidates": [
            {
                "candidate_key": "cpu-8",
                "provider_type": "host-capacity",
                "quantities": {"cpu_cores": 8, "memory_mib": 32768},
                "predicted_runtime_seconds": 600,
                "predicted_saved_seconds": 600,
                "predicted_saved_ratio": 0.5,
                "satisfies_marginal_threshold": True,
                "selected": True,
            }
        ],
    }
    path = tmp_path / "evaluation.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")

    result = CliRunner().invoke(cli_app, ["resource", "evaluate", "--file", str(path), "--json"])

    assert result.exit_code == 0
    assert '"eval-1"' in result.stdout
    assert calls == [("cpu-8", True)]
