from __future__ import annotations

import asyncio
import re
from pathlib import Path

import yaml
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from gpu_broker.api import create_app
from gpu_broker.cli import app as cli_app
from gpu_broker.config import InventoryConfig, ProjectConfig, Settings
from gpu_broker.mcp_server import mcp
from gpu_broker.schemas import EndpointUpsert
from tests.helpers import observation


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
    headers = {"X-GPU-Broker-Actor": "test-agent", "Idempotency-Key": "api-key"}
    payload = {
        "project_id": "project-a",
        "task_ref": "api-request",
        "purpose": "API test",
        "duration_seconds": 3600,
        "constraints": {"gpu_count": 1},
    }
    first = client.post("/api/v1/requests", json=payload, headers=headers)
    second = client.post("/api/v1/requests", json=payload, headers=headers)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    snapshot = client.get("/api/v1/snapshot", headers={"X-GPU-Broker-Actor": "test-agent"})
    assert snapshot.status_code == 200
    assert snapshot.json()["data"]["gpus"][0]["state"] == "HELD"
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
    assert "JSON payload" not in request_page.text
    submitted = client.post(
        "/ui/action/request",
        data={
            "project_id": "project-a",
            "task_ref": "click-first-request",
            "purpose": "browser form test",
            "gpu_count": "1",
            "duration_hours": "1",
            "min_free_vram_gib": "",
            "placement": "pack",
            "endpoint_id": "",
            "csrf": _csrf(request_page.text),
            "confirmed": "yes",
        },
        follow_redirects=True,
    )
    assert submitted.status_code == 200
    assert "资源请求已获分配" in submitted.text

    lease = service.list_leases(service.local_actor("human"))["data"][0]
    lease_page = client.get("/ui/leases")
    assert "激活租约" in lease_page.text
    activated = client.post(
        "/ui/action/activate-lease",
        data={"lease_id": lease["id"], "csrf": _csrf(lease_page.text), "confirmed": "yes"},
        follow_redirects=True,
    )
    assert activated.status_code == 200
    assert "操作完成" in activated.text

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

    switched = client.post("/ui/actor", data={"actor_id": "click-agent"}, follow_redirects=True)
    assert switched.status_code == 200
    assert 'value="click-agent"' in switched.text

    for page in ["/", "/ui/gpus", "/ui/requests", "/ui/leases", "/ui/reservations", "/ui/projects", "/ui/maintenance", "/ui/alerts", "/ui/audit", "/ui/doctor"]:
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
        "gpu_list",
        "gpu_who",
        "gpu_request",
        "gpu_request_status",
        "gpu_cancel_request",
        "gpu_activate_lease",
        "gpu_renew_lease",
        "gpu_release_lease",
        "gpu_bind_workload",
        "gpu_list_reservations",
        "gpu_history",
        "gpu_claim",
        "gpu_release",
        "gpu_schedule",
        "gpu_add_server",
    }.issubset(names)
    for name in ("gpu_claim", "gpu_schedule", "gpu_add_server"):
        assert "project_id" in by_name[name].inputSchema["required"]
    assert {"purpose", "gpu_count", "hours"}.issubset(by_name["gpu_claim"].inputSchema["required"])
    assert "reason" in by_name["gpu_release"].inputSchema["required"]


def test_ssh_preview_commit_is_bound_non_mutating_and_uses_defaults(tmp_path: Path, inventory) -> None:
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
        "project_ids": ["project-a", "project-b"],
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
