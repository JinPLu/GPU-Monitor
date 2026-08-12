from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import yaml
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text

from serverpilot import mcp_server
from serverpilot.api import create_app
from serverpilot.client import BrokerClient, BrokerClientError, codex_coordination_identity
from serverpilot.config import Settings
from serverpilot.database import Database
from serverpilot.schemas import RequestCreate
from serverpilot.service import BrokerError
from tests.helpers import observation


THREAD_ID = "019febd4-c455-7693-bb58-91ca9af7718e"
COORDINATION_URI = f"codex://threads/{THREAD_ID}"


def _request(task_ref: str) -> RequestCreate:
    return RequestCreate.model_validate(
        {
            "project_id": "project-a",
            "task_ref": task_ref,
            "purpose": "coordination test",
            "duration_seconds": 3600,
            "constraints": {"gpu_count": 1, "placement": "pack"},
        }
    )


def test_codex_task_identity_is_inherited_by_a_child_process(tmp_path: Path) -> None:
    environment = dict(os.environ)
    environment["CODEX_THREAD_ID"] = THREAD_ID
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json; "
                "from serverpilot.client import codex_coordination_identity; "
                "print(json.dumps(codex_coordination_identity()))"
            ),
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == [f"codex-{THREAD_ID}", COORDINATION_URI]


def test_client_derives_unique_actor_and_sends_internal_coordination_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEX_THREAD_ID", THREAD_ID)
    monkeypatch.delenv("SERVERPILOT_ACTOR", raising=False)
    calls: list[dict[str, object]] = []

    def request(_method: str, _url: str, **kwargs: object) -> httpx.Response:
        calls.append(kwargs)
        return httpx.Response(200, json={"schema_version": "v1", "data": {}})

    monkeypatch.setattr("serverpilot.client.httpx.request", request)
    client = BrokerClient.from_env()
    client.get("/api/v1/snapshot")

    assert client.actor == f"codex-{THREAD_ID}"
    assert calls[0]["headers"] == {
        "X-ServerPilot-Actor": f"codex-{THREAD_ID}",
        "X-ServerPilot-Coordination-URI": COORDINATION_URI,
    }


def test_routine_mutations_require_the_host_task_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.setattr(
        mcp_server,
        "ensure_broker_ready_for_mcp",
        lambda: pytest.fail("missing task identity must stop before daemon access"),
    )

    with pytest.raises(ValueError, match="CODEX_THREAD_ID"):
        mcp_server.gpu_apply()
    with pytest.raises(ValueError, match="CODEX_THREAD_ID"):
        mcp_server.gpu_release("lease-a")


def test_routine_client_sends_task_identity_without_idempotency_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEX_THREAD_ID", THREAD_ID)
    monkeypatch.setattr(mcp_server, "ensure_broker_ready_for_mcp", lambda: None)
    calls: list[dict[str, object]] = []

    def request(_method: str, _url: str, **kwargs: object) -> httpx.Response:
        calls.append(kwargs)
        return httpx.Response(200, json={"lease": {"id": "lease-a", "resources": []}})

    monkeypatch.setattr("serverpilot.client.httpx.request", request)
    mcp_server._routine_client(require_identity=True).post(
        "/api/v1/routine/claims",
        {"project_id": "codex", "task_ref": "task", "purpose": "task", "constraints": {"gpu_count": 1}},
    )

    assert calls[0]["headers"] == {
        "X-ServerPilot-Actor": f"codex-{THREAD_ID}",
        "X-ServerPilot-Coordination-URI": COORDINATION_URI,
    }


def test_invalid_ambient_thread_id_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_THREAD_ID", "NOT-A-CANONICAL-UUID")
    with pytest.raises(BrokerClientError, match="canonical UUID"):
        codex_coordination_identity()


def test_mcp_uses_task_scoped_actor_instead_of_tool_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEX_THREAD_ID", THREAD_ID)
    monkeypatch.setattr(mcp_server, "ensure_broker_ready_for_mcp", lambda: None)

    client = mcp_server._client("agent")

    assert client.actor == f"codex-{THREAD_ID}"
    assert client.coordination_uri == COORDINATION_URI


def test_mcp_coordination_projection_preserves_contact_uri(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = BrokerClient("http://127.0.0.1:8787", actor="coord-agent")
    paths: list[str] = []

    def get(path: str, *, params: dict[str, object] | None = None) -> dict[str, object]:
        assert params is None
        paths.append(path)
        return {
            "schema_version": "v1",
            "snapshot_revision": 1,
            "server_time": "2026-08-12T00:00:00+00:00",
            "data": {
                "agents": [{"coordination_uri": COORDINATION_URI}],
                "leases": [{"coordination_uri": COORDINATION_URI}],
                "servers": [{"consumers": [{"coordination_uri": COORDINATION_URI}]}],
            },
        }

    monkeypatch.setattr(client, "get", get)

    board = client.coordination()["data"]

    assert paths == ["/api/v1/coordination"]
    assert board["agents"][0]["coordination_uri"] == COORDINATION_URI
    assert board["leases"][0]["coordination_uri"] == COORDINATION_URI
    assert board["servers"][0]["consumers"][0]["coordination_uri"] == COORDINATION_URI


def test_registration_is_idempotent_strict_and_never_overwrites(service, admin) -> None:
    first = service.local_actor("coord-agent", coordination_uri=COORDINATION_URI)
    second = service.local_actor("coord-agent", coordination_uri=COORDINATION_URI)
    assert first == second

    with pytest.raises(BrokerError) as invalid:
        service.local_actor(
            "invalid-uri-agent",
            coordination_uri="codex://threads/019FEBD4-C455-7693-BB58-91CA9AF7718E",
        )
    assert invalid.value.code == "invalid_coordination_uri"

    with pytest.raises(BrokerError) as conflict:
        service.local_actor(
            "coord-agent",
            coordination_uri="codex://threads/019ff1a1-2961-7f63-a7a4-c9ac2013f1ae",
        )
    assert conflict.value.code == "actor_coordination_conflict"

    actor_row = next(item for item in service.list_actors(admin)["data"] if item["id"] == "coord-agent")
    assert actor_row["coordination_uri"] == COORDINATION_URI


def test_coordination_uri_is_returned_for_agents_leases_and_consumers(service, admin) -> None:
    service.ingest_observation(observation(count=1))
    actor = service.local_actor("coord-agent", coordination_uri=COORDINATION_URI)
    allocated = service.create_request(actor, _request("shared-task"), idempotency_key="coord-claim")
    assert allocated["lease"] is not None

    board = service.coordination(admin)["data"]
    assert board["agents"][0]["coordination_uri"] == COORDINATION_URI
    assert board["leases"][0]["coordination_uri"] == COORDINATION_URI
    assert board["servers"][0]["consumers"][0]["coordination_uri"] == COORDINATION_URI


def test_coordination_uri_never_grants_lease_authority(service) -> None:
    service.ingest_observation(observation(count=1))
    owner = service.local_actor("owner-agent", coordination_uri=COORDINATION_URI)
    other = service.local_actor(
        "other-agent",
        coordination_uri="codex://threads/019ff1a1-2961-7f63-a7a4-c9ac2013f1ae",
    )
    lease = service.create_request(
        owner, _request("owned-task"), idempotency_key="owned-claim"
    )["lease"]
    assert lease is not None

    with pytest.raises(BrokerError) as forbidden:
        service.release_lease(
            other,
            lease["id"],
            reason="must remain owner-scoped",
            idempotency_key="foreign-release",
        )
    assert forbidden.value.code == "lease_forbidden"


def test_api_header_registers_actor_without_request_body(tmp_path: Path, inventory) -> None:
    inventory_path = tmp_path / "inventory.yaml"
    inventory_path.write_text(
        yaml.safe_dump(inventory.model_dump(mode="json")), encoding="utf-8"
    )
    app = create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'api.sqlite3'}",
            inventory_path=inventory_path,
            session_secret="s" * 32,
        )
    )
    response = TestClient(app).get(
        "/api/v1/snapshot",
        headers={
            "X-ServerPilot-Actor": f"codex-{THREAD_ID}",
            "X-ServerPilot-Coordination-URI": COORDINATION_URI,
        },
    )

    assert response.status_code == 200
    stored_uri = app.state.service._read(
        lambda session: session.execute(
            text("SELECT coordination_uri FROM actors WHERE id = :actor_id"),
            {"actor_id": f"codex-{THREAD_ID}"},
        ).scalar_one()
    )
    assert stored_uri == COORDINATION_URI


def test_routine_routes_keep_the_task_lease_until_explicit_release(
    tmp_path: Path,
    inventory,
) -> None:  # type: ignore[no-untyped-def]
    inventory_path = tmp_path / "inventory.yaml"
    inventory_path.write_text(
        yaml.safe_dump(inventory.model_dump(mode="json")), encoding="utf-8"
    )
    app = create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'routine.sqlite3'}",
            inventory_path=inventory_path,
            session_secret="s" * 32,
        )
    )
    app.state.service.ingest_observation(observation(count=1))
    client = TestClient(app)
    headers = {
        "X-ServerPilot-Actor": f"codex-{THREAD_ID}",
        "X-ServerPilot-Coordination-URI": COORDINATION_URI,
    }

    claimed = client.post(
        "/api/v1/routine/claims",
        json={
            "project_id": "codex",
            "task_ref": "训练任务",
            "purpose": "Codex GPU task",
            "constraints": {"gpu_count": 1, "placement": "pack"},
        },
        headers=headers,
    )

    assert claimed.status_code == 200
    lease = claimed.json()["lease"]
    assert lease["actor_id"] == f"codex-{THREAD_ID}"
    assert lease["coordination_uri"] == COORDINATION_URI
    assert lease["expires_at"] is None
    assert lease["state"] == "HELD"
    assert claimed.json()["request"]["task_ref"] == "训练任务"
    with app.state.service.database.session() as session:
        app.state.service._reconcile_leases(
            session,
            datetime.now(UTC) + timedelta(days=2),
            actor_id="serverpilot-system",
        )
        session.commit()
        assert session.execute(
            text("SELECT state FROM leases WHERE id = :lease_id"),
            {"lease_id": lease["id"]},
        ).scalar_one() == "HELD"
    with app.state.service.database.engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM idempotency_records")).scalar_one() == 0

    released = client.post(
        f"/api/v1/routine/leases/{lease['id']}/release",
        headers=headers,
    )

    assert released.status_code == 200
    assert released.json()["lease"]["state"] == "RELEASED"
    with app.state.service.database.engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM idempotency_records")).scalar_one() == 0


def test_busy_status_uses_an_empty_agent_url_when_the_owner_has_no_task_uri() -> None:
    status = mcp_server._routine_gpu_status(
        {
            "data": {
                "endpoints": [{"id": "server-a", "workspace_path": "/srv/server-a"}],
                "gpus": [
                    {
                        "endpoint_id": "server-a",
                        "gpu_uuid": "GPU-a",
                        "gpu_index": 0,
                        "name": "A",
                        "total_vram_mib": 80_000,
                        "state": "HELD",
                        "publicly_available": False,
                        "public_status": "任务使用中",
                        "keepalive": {"state": "OFF", "reason": None},
                        "lease": {"task_ref": "没有 URL 的任务", "coordination_uri": None},
                    }
                ]
            }
        },
        include_busy=True,
    )

    assert status["gpus"][0]["agent_url"] == ""
    assert status["gpus"][0]["workspace_path"] == "/srv/server-a"


def test_routine_status_reports_no_gpu_from_the_canonical_summary() -> None:
    status = mcp_server._routine_gpu_status(
        {"data": {"summary": {"total_gpus": 0}, "gpus": []}},
        include_busy=False,
    )

    assert status == {"gpus": [], "message": "无 GPU"}


def test_actor_coordination_uri_migration_is_additive(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    database = Database(f"sqlite:///{tmp_path / 'migration.sqlite3'}", root)
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "src" / "serverpilot" / "migrations"))
    config.set_main_option("sqlalchemy.url", database.url)
    command.upgrade(config, "20260811_0016")
    with database.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO actors "
                "(id, display_name, role, enabled, created_at, updated_at) "
                "VALUES ('legacy-agent', 'Legacy', 'allocator', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )

    command.upgrade(config, "20260812_0017")

    assert "coordination_uri" in {
        column["name"] for column in inspect(database.engine).get_columns("actors")
    }
    with database.engine.connect() as connection:
        assert connection.execute(
            text("SELECT coordination_uri FROM actors WHERE id = 'legacy-agent'")
        ).scalar_one() is None
