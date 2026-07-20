from __future__ import annotations

import asyncio
import re
from pathlib import Path

import yaml
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from gpu_broker import API_CAPABILITIES, mcp_server
from gpu_broker.api import create_app
from gpu_broker.cli import app as cli_app
from gpu_broker.config import EndpointConfig, InventoryConfig, ProjectConfig, Settings
from gpu_broker.mcp_server import mcp
from gpu_broker.schemas import EndpointUpsert
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
    assert "GPU 资源空间" in home.text
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
    requests = client.get("/ui/requests")
    assert requests.status_code == 200
    assert "认领 GPU" in requests.text
    assert "可用 CPU 核数" in requests.text
    assert "可用内存 GiB" in requests.text
    assert "单卡可用显存 GiB" in requests.text
    assert "CPU 可用 16 核" in requests.text


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
    assert "GPU 已认领并登记为使用中" in claimed.text
    request = service.list_requests(service.local_actor("human"))["data"][0]
    assert request["profile_id"] == "api-eval-1gpu"
    assert request["purpose"] == "approved API evaluation"
    assert request["state"] == "ACTIVE"


def test_api_claim_auto_activates_without_a_duration_estimate(tmp_path: Path, inventory) -> None:
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
    assert claimed.json()["request"]["state"] == "ACTIVE"
    assert claimed.json()["lease"]["state"] == "ACTIVE"
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
    assert [endpoint["id"] for endpoint in listed.json()["data"]] == ["endpoint-a"]


def test_endpoint_delete_rest_route_preserves_nested_error_envelope(tmp_path: Path, inventory) -> None:
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
    headers = {"X-GPU-Broker-Actor": "endpoint-admin", "Idempotency-Key": "endpoint-maintenance"}
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

    blocked = client.delete(
        "/api/v1/endpoints/endpoint-b",
        headers={"X-GPU-Broker-Actor": "endpoint-admin", "Idempotency-Key": "delete-maintained"},
    )

    assert blocked.status_code == 409
    assert blocked.json()["error"] == {
        "code": "endpoint_referenced_by_maintenance",
        "message": "endpoint has maintenance history; cancel or retain the server disabled instead of deleting",
        "details": {"maintenance_ids": [created.json()["maintenance"]["id"]]},
    }


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
    assert "GPU 已认领并登记为使用中" in submitted.text

    lease = service.list_leases(service.local_actor("human"))["data"][0]
    assert lease["state"] == "ACTIVE"
    request = service.list_requests(service.local_actor("human"))["data"][0]
    assert request["state"] == "ACTIVE"
    assert request["purpose"] == "click-first-request"

    home_page = client.get("/")
    added_server = client.post(
        "/ui/action/endpoint",
        data={
            "id": "click-server",
            "host": "127.0.0.2",
            "port": "2203",
            "ssh_user": "gpu",
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
    assert "click-server" not in {endpoint["id"] for endpoint in service.list_endpoints(service.local_actor("human"))["data"]}

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
        "gpu_status",
        "gpu_coordination",
        "gpu_list",
        "gpu_who",
        "gpu_list_profiles",
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
        "gpu_delete_server",
    }.issubset(names)
    assert "gpu_grant_server_project" not in names
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


def test_mcp_common_tools_do_not_preflight_health(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls = []

    class FakeClient:
        def get(self, path, *, params=None):  # type: ignore[no-untyped-def]
            calls.append(("GET", path, params))
            return {"schema_version": "v1", "data": {}}

        def post(self, path, body=None, *, idempotency_key):  # type: ignore[no-untyped-def]
            calls.append(("POST", path, body, idempotency_key))
            return {"schema_version": "v1", "request": {}, "lease": None}

    monkeypatch.setattr(mcp_server, "_client", lambda actor_name=None: FakeClient())

    assert mcp_server.gpu_coordination() == {"schema_version": "v1", "data": {}}
    assert calls == [("GET", "/api/v1/coordination", None)]

    calls.clear()
    assert mcp_server.gpu_claim("agent", "project", "task", 1)["request"] == {}
    assert [call[:2] for call in calls] == [("POST", "/api/v1/claims")]


def test_ssh_preview_commit_is_bound_non_mutating_and_has_no_project_scope_by_default(tmp_path: Path, inventory) -> None:
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
        json={"command": "  ssh GPU_User@New-Host  ", "csrf": csrf},
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
        "project_ids": [],
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

    preview = client.post("/ui/endpoints/ssh/batch/preview", json={"commands": commands, "csrf": csrf})
    assert preview.status_code == 200
    preview_data = preview.json()["data"]
    assert preview_data["valid_count"] == 2
    assert [entry["status"] for entry in preview_data["entries"]] == ["new", "invalid", "new", "duplicate"]

    committed = client.post(
        "/ui/endpoints/ssh/batch/commit",
        json={"commands": commands, "preview_token": preview_data["preview_token"], "csrf": csrf},
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
