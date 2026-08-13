from __future__ import annotations

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
from serverpilot.client import BrokerClient, BrokerClientError
from serverpilot.config import Settings
from serverpilot.database import Database
from serverpilot.schemas import RequestCreate
from tests.helpers import observation


def _request(task_ref: str) -> RequestCreate:
    return RequestCreate.model_validate(
        {
            "project_id": "project-a",
            "task_ref": task_ref,
            "purpose": "harness-neutral test",
            "duration_seconds": 3600,
            "constraints": {"gpu_count": 1, "placement": "pack"},
        }
    )


def _keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value).union(*(map(_keys, value.values())), set())
    if isinstance(value, list):
        return set().union(*(map(_keys, value)), set())
    return set()


def test_client_emits_only_the_actor_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SERVERPILOT_ACTOR", raising=False)
    calls: list[dict[str, object]] = []

    def request(_method: str, _url: str, **kwargs: object) -> httpx.Response:
        calls.append(kwargs)
        return httpx.Response(200, json={"schema_version": "v1", "data": {}})

    monkeypatch.setattr("serverpilot.client.httpx.request", request)
    client = BrokerClient.from_env()
    client.get("/api/v1/snapshot")

    assert client.actor == "agent"
    assert calls[0]["headers"] == {"X-ServerPilot-Actor": "agent"}


def test_routine_mutations_use_a_harness_neutral_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_server, "ensure_broker_ready_for_mcp", lambda: None)
    calls: list[dict[str, object]] = []

    def request(_method: str, _url: str, **kwargs: object) -> httpx.Response:
        calls.append(kwargs)
        return httpx.Response(200, json={"lease": {"id": "lease-a", "resources": []}})

    monkeypatch.setattr("serverpilot.client.httpx.request", request)
    mcp_server.gpu_apply(task="训练任务")
    mcp_server.gpu_release("lease-a")

    assert [call["headers"] for call in calls] == [
        {"X-ServerPilot-Actor": "agent"},
        {"X-ServerPilot-Actor": "agent"},
    ]


def test_historical_contact_column_is_inert_and_not_projected(service, admin) -> None:
    actor = service.local_actor("legacy-contact-agent")
    with service.database.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE actors SET coordination_uri = 'legacy-contact' "
                "WHERE id = 'legacy-contact-agent'"
            )
        )

    assert service.local_actor("legacy-contact-agent") == actor
    with service.database.engine.connect() as connection:
        assert connection.execute(
            text("SELECT coordination_uri FROM actors WHERE id = 'legacy-contact-agent'")
        ).scalar_one() == "legacy-contact"

    service.ingest_observation(observation(count=1))
    allocated = service.create_request(
        actor,
        _request("shared-task"),
        idempotency_key="legacy-contact-claim",
    )
    assert allocated["lease"] is not None
    assert "coordination_uri" not in _keys(service.list_actors(admin))
    assert "coordination_uri" not in _keys(allocated)
    assert "coordination_uri" not in _keys(service.coordination(admin))


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
    headers = {"X-ServerPilot-Actor": "agent"}

    claimed = client.post(
        "/api/v1/routine/claims",
        json={
            "project_id": "agent",
            "task_ref": "训练任务",
            "purpose": "训练任务",
            "constraints": {"gpu_count": 1, "placement": "pack"},
        },
        headers=headers,
    )

    assert claimed.status_code == 200
    lease = claimed.json()["lease"]
    assert lease["actor_id"] == "agent"
    assert "coordination_uri" not in lease
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
        assert connection.execute(
            text("SELECT COUNT(*) FROM idempotency_records")
        ).scalar_one() == 0

    released = client.post(
        f"/api/v1/routine/leases/{lease['id']}/release",
        headers=headers,
    )

    assert released.status_code == 200
    assert released.json()["lease"]["state"] == "RELEASED"
    with app.state.service.database.engine.connect() as connection:
        assert connection.execute(
            text("SELECT COUNT(*) FROM idempotency_records")
        ).scalar_one() == 0


def test_routine_agent_can_retry_no_capacity_then_claim_two_gpus_on_one_server(
    tmp_path: Path,
    inventory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # type: ignore[no-untyped-def]
    inventory_path = tmp_path / "inventory.yaml"
    inventory_path.write_text(
        yaml.safe_dump(inventory.model_dump(mode="json")), encoding="utf-8"
    )
    app = create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'routine-retry.sqlite3'}",
            inventory_path=inventory_path,
            session_secret="s" * 32,
        )
    )
    rest = TestClient(app)
    headers = {"X-ServerPilot-Actor": "agent"}

    class RoutineClient:
        def post(self, path, body=None):  # type: ignore[no-untyped-def]
            response = rest.post(path, json=body, headers=headers)
            if response.is_error:
                error = response.json()["error"]
                raise BrokerClientError(
                    f"broker HTTP {response.status_code}: "
                    f"{error['code']}: {error['message']}"
                )
            return response.json()

    monkeypatch.setattr(mcp_server, "_routine_client", RoutineClient)

    with pytest.raises(BrokerClientError, match=r"no_capacity"):
        mcp_server.gpu_apply(server_id="endpoint-a", gpu_count=2, task="双卡训练")
    assert app.state.service.list_requests(
        app.state.service.local_actor("agent")
    )["data"] == []

    app.state.service.ingest_observation(observation(count=2))
    claimed = mcp_server.gpu_apply(
        server_id="endpoint-a", gpu_count=2, task="双卡训练"
    )

    assert claimed["lease_id"]
    assert len(claimed["gpus"]) == 2
    assert claimed["server_id"] == "endpoint-a"
    assert claimed["workspace_path"] == inventory.endpoints[0].workspace_path
    assert {gpu["server_id"] for gpu in claimed["gpus"]} == {"endpoint-a"}
    assert {gpu["workspace_path"] for gpu in claimed["gpus"]} == {
        inventory.endpoints[0].workspace_path
    }
    assert len({gpu["gpu_id"] for gpu in claimed["gpus"]}) == 2
    expected_visible_devices = ",".join(gpu["gpu_id"] for gpu in claimed["gpus"])
    assert claimed["cuda_visible_devices"] == expected_visible_devices
    assert {gpu["cuda_visible_devices"] for gpu in claimed["gpus"]} == {
        expected_visible_devices
    }
    assert {gpu["gpu_cuda_visible_devices"] for gpu in claimed["gpus"]} == {
        gpu["gpu_id"] for gpu in claimed["gpus"]
    }

    assert mcp_server.gpu_release(claimed["lease_id"]) == {"released": True}
    leases = app.state.service.list_leases(
        app.state.service.local_actor("agent")
    )["data"]
    assert len(leases) == 1
    assert leases[0]["state"] == "RELEASED"


@pytest.mark.parametrize(
    ("server_id", "gpu_count", "message"),
    [
        (None, True, "gpu_count 必须是正整数"),
        (None, 0, "gpu_count 必须是正整数"),
        (None, -1, "gpu_count 必须是正整数"),
        ("   ", 1, "提供 server_id 时不能为空"),
    ],
)
def test_routine_apply_rejects_invalid_daily_inputs_before_contacting_broker(
    server_id: str | None,
    gpu_count: int,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_client() -> object:
        raise AssertionError("invalid routine input must not contact the broker")

    monkeypatch.setattr(mcp_server, "_routine_client", unexpected_client)

    with pytest.raises(ValueError, match=message):
        mcp_server.gpu_apply(server_id=server_id, gpu_count=gpu_count, task="训练")


@pytest.mark.parametrize("lease_id", ["", "   "])
def test_routine_release_rejects_blank_lease_before_contacting_broker(
    lease_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected_client() -> object:
        raise AssertionError("blank lease id must not contact the broker")

    monkeypatch.setattr(mcp_server, "_routine_client", unexpected_client)

    with pytest.raises(ValueError, match="lease_id 不能为空"):
        mcp_server.gpu_release(lease_id)


def test_busy_status_returns_task_without_a_contact_field() -> None:
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
                        "lease": {"task_ref": "训练任务"},
                    }
                ],
            }
        },
        include_busy=True,
    )

    assert status["gpus"][0]["task"] == "训练任务"
    assert set(status["gpus"][0]) == {
        "server_id",
        "gpu_id",
        "index",
        "name",
        "vram_mib",
        "status",
        "workspace_path",
        "available",
        "task",
        "keepalive",
    }


def test_routine_status_reports_no_gpu_from_the_canonical_summary() -> None:
    status = mcp_server._routine_gpu_status(
        {"data": {"summary": {"total_gpus": 0}, "gpus": []}},
        include_busy=False,
    )

    assert status == {"gpus": [], "message": "无 GPU"}


def test_routine_status_explains_when_all_gpus_are_unavailable() -> None:
    status = mcp_server._routine_gpu_status(
        {"data": {"summary": {"total_gpus": 4, "available_gpus": 0}, "gpus": []}},
        include_busy=False,
    )

    assert status == {
        "gpus": [],
        "no_capacity": {
            "reason": "all_gpus_busy_or_unavailable",
            "message": (
                "当前没有可申请 GPU；可调用 gpu_status(include_busy=true) "
                "查看占用任务或异常状态。"
            ),
            "total_gpus": 4,
        },
    }


def test_historical_actor_contact_migration_is_additive(tmp_path: Path) -> None:
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
