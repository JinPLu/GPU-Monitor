from __future__ import annotations

import json
import os
import subprocess
import sys
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
    current = {
        "summary": {},
        "gpus": [
            {
                "id": "gpu-a",
                "endpoint_id": "server-a",
                "state": "HELD",
                "telemetry": None,
                "processes": [],
                "total_vram_mib": 80_000,
                "lease": {"id": "lease-a"},
            }
        ],
        "endpoints": [
            {
                "id": "server-a",
                "monitor": {"status": "HEALTHY"},
                "host_telemetry": None,
            }
        ],
        "leases": [
            {
                "id": "lease-a",
                "actor_id": "coord-agent",
                "coordination_uri": COORDINATION_URI,
                "project_id": "project-a",
                "task_ref": "shared-task",
                "state": "HELD",
                "gpu_ids": ["gpu-a"],
                "workloads": [],
                "issued_at": "2026-08-12T00:00:00+00:00",
                "expires_at": "2026-08-12T01:00:00+00:00",
            }
        ],
        "requests": [],
        "scheduler_targets": [],
        "scheduler_jobs": [],
    }
    monkeypatch.setattr(
        client,
        "control_plane_state",
        lambda: {
            "schema_version": "v1",
            "snapshot_revision": 1,
            "server_time": "2026-08-12T00:00:00+00:00",
            "data": {"current": current, "history": {}},
        },
    )

    board = client.coordination()["data"]

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
