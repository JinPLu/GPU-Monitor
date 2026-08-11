from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import inspect, select, text

from serverpilot.config import EndpointConfig, InventoryConfig
from serverpilot.database import Database
from serverpilot.models import Actor, AllocationRequest, ApiToken, Lease, Project
from serverpilot.schemas import ActorCreate, EndpointCreate, EndpointUpdate, RequestCreate
from serverpilot.service import (
    SYSTEM_ACTOR_ID,
    SYSTEM_PROJECT_ID,
    BrokerError,
    BrokerService,
)
from serverpilot.timeutil import json_dump, utcnow
from tests.helpers import observation


def _request(project_id: str, task_ref: str) -> RequestCreate:
    return RequestCreate.model_validate(
        {
            "project_id": project_id,
            "task_ref": task_ref,
            "purpose": "keepalive persistence test",
            "duration_seconds": 3600,
            "constraints": {"gpu_count": 1},
        }
    )


def test_keepalive_adapter_is_sealed_and_round_trips_endpoint_surfaces(service, admin) -> None:
    with pytest.raises(ValidationError):
        EndpointConfig(
            id="invalid-adapter",
            host="127.0.0.1",
            port=2298,
            ssh_user="gpu",
            keepalive_adapter_id="arbitrary-shell",  # type: ignore[arg-type]
        )

    created = service.create_endpoint(
        admin,
        EndpointCreate(
            id="keepalive-endpoint",
            host="127.0.0.1",
            port=2299,
            ssh_user="gpu",
            keepalive_adapter_id="server-script-v1",
        ),
        idempotency_key="keepalive-endpoint-create",
    )
    assert created["endpoint"]["keepalive_adapter_id"] == "server-script-v1"
    collected = {endpoint.id: endpoint for endpoint in service.collector_endpoints()}
    assert collected["keepalive-endpoint"].keepalive_adapter_id == "server-script-v1"

    disabled = service.update_endpoint(
        admin,
        "keepalive-endpoint",
        EndpointUpdate(keepalive_adapter_id=None),
        idempotency_key="keepalive-endpoint-disable",
    )
    assert disabled["endpoint"]["keepalive_adapter_id"] is None

    with pytest.raises(ValidationError):
        EndpointConfig(
            id="invalid-policy",
            host="127.0.0.1",
            port=2300,
            ssh_user="gpu",
            keepalive_policy="always-on",  # type: ignore[arg-type]
        )

    with pytest.raises(ValidationError):
        EndpointUpdate(keepalive_policy="idle_keepalive")  # type: ignore[call-arg]

    with pytest.raises(ValidationError):
        EndpointCreate(
            id="invalid-idle-policy",
            host="127.0.0.1",
            port=2301,
            ssh_user="gpu",
            keepalive_policy="idle_keepalive",
        )


def test_runtime_keepalive_policy_survives_static_inventory_restart_when_not_explicit(
    tmp_path: Path, inventory: InventoryConfig
) -> None:
    configured = inventory.model_copy(deep=True)
    configured.endpoints[0].keepalive_adapter_id = "server-script-v1"
    assert "keepalive_policy" not in configured.endpoints[0].model_fields_set
    root = Path(__file__).resolve().parents[1]
    database = Database(f"sqlite:///{tmp_path / 'policy-restart.sqlite3'}", root)
    first = BrokerService(database, configured)
    first.initialize("a" * 32)
    admin = first.authenticate("a" * 32)
    first.configure_keepalive_policy(
        admin,
        "endpoint-a",
        "idle_keepalive",
        idempotency_key="runtime-policy",
    )

    restarted = BrokerService(database, configured)
    restarted.initialize("a" * 32, sync_inventory=True)
    assert restarted.get_endpoint_keepalive_summary("endpoint-a")["keepalive"]["policy"] == "idle_keepalive"


def test_enabling_keepalive_attaches_the_sealed_helper_to_legacy_endpoints(service, admin) -> None:
    service.ingest_observation(observation(count=2))

    configured = service.configure_keepalive_policy(
        admin,
        "endpoint-a",
        "idle_keepalive",
        idempotency_key="legacy-endpoint-enable",
    )

    assert configured["keepalive"]["configured"] is True
    assert configured["keepalive"]["policy"] == "idle_keepalive"
    endpoint = service.list_endpoints(admin)["data"][0]
    assert endpoint["keepalive_adapter_id"] == "server-script-v1"


def test_system_identity_is_tokenless_hidden_and_reserved(service, admin) -> None:
    with service.database.session() as session:
        assert session.get(Actor, SYSTEM_ACTOR_ID) is not None
        assert session.get(Project, SYSTEM_PROJECT_ID) is not None
        assert session.scalar(select(ApiToken).where(ApiToken.actor_id == SYSTEM_ACTOR_ID)) is None

    assert SYSTEM_ACTOR_ID not in {item["id"] for item in service.list_actors(admin)["data"]}
    assert SYSTEM_PROJECT_ID not in {item["id"] for item in service.list_projects(admin)["data"]}

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        RequestCreate.model_validate(
            {
                "project_id": "project-a",
                "task_ref": "forged-kind",
                "purpose": "ordinary API cannot select an internal lease kind",
                "duration_seconds": 3600,
                "constraints": {"gpu_count": 1},
                "kind": "keepalive",
            }
        )

    with pytest.raises(BrokerError, match="internal identity") as actor_error:
        service.create_actor(
            admin,
            ActorCreate(
                id=SYSTEM_ACTOR_ID,
                display_name="forged",
                role="admin",
                project_ids=[],
            ),
            idempotency_key="forge-system-actor",
        )
    assert actor_error.value.code == "reserved_system_identity"

    with pytest.raises(BrokerError, match="internal project") as project_error:
        service.create_request(
            admin,
            _request(SYSTEM_PROJECT_ID, "forge-system-project"),
            idempotency_key="forge-system-project",
        )
    assert project_error.value.code == "reserved_project_id"

    with pytest.raises(BrokerError) as local_error:
        service.local_actor(SYSTEM_ACTOR_ID)
    assert local_error.value.code == "reserved_system_identity"
    with pytest.raises(BrokerError) as context_error:
        service.context_for_actor(SYSTEM_ACTOR_ID)
    assert context_error.value.code == "reserved_system_identity"


def test_initialize_fails_closed_if_reserved_identity_has_a_token(
    tmp_path: Path, inventory
) -> None:  # noqa: ANN001
    root = Path(__file__).resolve().parents[1]
    broker = BrokerService(Database(f"sqlite:///{tmp_path / 'identity-token.sqlite3'}", root), inventory)
    broker.initialize()
    now = utcnow()
    with broker.database.session() as session:
        session.add(
            ApiToken(
                id="legacy-system-token",
                actor_id=SYSTEM_ACTOR_ID,
                label="legacy",
                token_hash="f" * 64,
                created_at=now,
                expires_at=None,
                revoked_at=None,
                last_used_at=None,
            )
        )
        session.commit()

    with pytest.raises(BrokerError) as conflict:
        broker.initialize()
    assert conflict.value.code == "reserved_system_identity_conflict"


def test_initialize_fails_closed_if_reserved_identity_attributes_were_repurposed(
    tmp_path: Path, inventory
) -> None:  # noqa: ANN001
    root = Path(__file__).resolve().parents[1]
    broker = BrokerService(Database(f"sqlite:///{tmp_path / 'identity-role.sqlite3'}", root), inventory)
    broker.initialize()
    with broker.database.session() as session:
        actor = session.get(Actor, SYSTEM_ACTOR_ID)
        assert actor is not None
        actor.role = "admin"
        session.commit()

    with pytest.raises(BrokerError) as conflict:
        broker.initialize()
    assert conflict.value.code == "reserved_system_identity_conflict"


def test_workload_kind_is_explicit_and_keepalive_is_outside_quota_and_fair_queue(
    service, admin
) -> None:
    service.ingest_observation(observation(count=1))
    workload = service.create_request(
        admin,
        _request("project-a", "ordinary-workload"),
        idempotency_key="ordinary-workload",
    )
    assert workload["lease"]["kind"] == "workload"

    now = utcnow()
    with service.database.session() as session:
        session.add(
            AllocationRequest(
                id="keepalive-queued-test",
                actor_id=SYSTEM_ACTOR_ID,
                project_id=SYSTEM_PROJECT_ID,
                profile_id=None,
                auto_activate=False,
                task_ref="keepalive",
                purpose="internal keepalive",
                constraints_json=json_dump({"gpu_count": 1}),
                duration_seconds=3600,
                expected_duration_seconds=None,
                start_after=None,
                deadline=None,
                approval_ref=None,
                state="QUEUED",
                priority_class="keepalive",
                blocked_reason=None,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            AllocationRequest(
                id="keepalive-leased-test",
                actor_id=SYSTEM_ACTOR_ID,
                project_id=SYSTEM_PROJECT_ID,
                profile_id=None,
                auto_activate=False,
                task_ref="keepalive-active",
                purpose="internal keepalive",
                constraints_json=json_dump({"gpu_count": 1}),
                duration_seconds=3600,
                expected_duration_seconds=None,
                start_after=None,
                deadline=None,
                approval_ref=None,
                state="LEASED",
                priority_class="keepalive",
                blocked_reason=None,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            Lease(
                id="keepalive-lease-test",
                request_id="keepalive-leased-test",
                actor_id=SYSTEM_ACTOR_ID,
                project_id=SYSTEM_PROJECT_ID,
                kind="keepalive",
                state="HELD",
                issued_at=now,
                expires_at=now + timedelta(hours=1),
                last_heartbeat_at=now,
                activated_at=None,
                released_at=None,
                release_reason=None,
                issued_revision=1,
            )
        )
        session.commit()

    with service.database.session() as session:
        gpu_usage, lease_usage = service._project_usage(session)
        assert SYSTEM_PROJECT_ID not in gpu_usage
        assert SYSTEM_PROJECT_ID not in lease_usage
        assert "keepalive-queued-test" not in {
            request.id for request in service._queue_candidates(session, now)
        }

    assert "keepalive-lease-test" not in {
        lease["id"] for lease in service.list_leases(admin)["data"]
    }


def test_0015_upgrades_a_legacy_0014_database(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    database = Database(f"sqlite:///{tmp_path / 'legacy-0014.sqlite3'}", root)
    with database.engine.begin() as connection:
        connection.execute(text("CREATE TABLE endpoints (id VARCHAR(128) PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE leases (id VARCHAR(64) PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(
            text("INSERT INTO alembic_version (version_num) VALUES ('20260810_0014')")
        )

    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "src" / "serverpilot" / "migrations"))
    config.set_main_option("sqlalchemy.url", database.url)
    command.upgrade(config, "20260811_0015")

    inspector = inspect(database.engine)
    assert "keepalive_adapter_id" in {
        column["name"] for column in inspector.get_columns("endpoints")
    }
    assert "kind" in {column["name"] for column in inspector.get_columns("leases")}
    with database.engine.begin() as connection:
        assert connection.execute(text("SELECT kind FROM leases")).all() == []
        with pytest.raises(Exception):
            connection.execute(text("INSERT INTO leases (id, kind) VALUES ('bad', 'arbitrary')"))


def test_0019_defaults_endpoint_policy_and_marks_old_keepalive_scope(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    database = Database(f"sqlite:///{tmp_path / 'legacy-0018.sqlite3'}", root)
    with database.engine.begin() as connection:
        connection.execute(text("CREATE TABLE endpoints (id VARCHAR(128) PRIMARY KEY)"))
        connection.execute(
            text("CREATE TABLE leases (id VARCHAR(64) PRIMARY KEY, kind VARCHAR(16) NOT NULL)")
        )
        connection.execute(text("INSERT INTO leases (id, kind) VALUES ('old-keepalive', 'keepalive')"))
        connection.execute(text("INSERT INTO leases (id, kind) VALUES ('ordinary', 'workload')"))
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version (version_num) VALUES ('20260812_0018')"))

    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "src" / "serverpilot" / "migrations"))
    config.set_main_option("sqlalchemy.url", database.url)
    command.upgrade(config, "20260812_0019")

    inspector = inspect(database.engine)
    assert "keepalive_policy" in {column["name"] for column in inspector.get_columns("endpoints")}
    assert "keepalive_scope" in {column["name"] for column in inspector.get_columns("leases")}
    with database.engine.begin() as connection:
        assert connection.execute(
            text("SELECT id, keepalive_scope FROM leases ORDER BY id")
        ).all() == [("old-keepalive", "legacy_endpoint"), ("ordinary", None)]
        with pytest.raises(Exception):
            connection.execute(
                text("INSERT INTO endpoints (id, keepalive_policy) VALUES ('bad', 'always-on')")
            )
