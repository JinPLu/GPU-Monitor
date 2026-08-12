from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import inspect, text

from serverpilot.config import EndpointConfig, InventoryConfig
from serverpilot.database import Database
from serverpilot.models import Actor, AllocationRequest, Lease, Project
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
            workspace_path="/srv/invalid-adapter",
            keepalive_adapter_id="arbitrary-shell",  # type: ignore[arg-type]
        )

    created = service.create_endpoint(
        admin,
        EndpointCreate(
            id="keepalive-endpoint",
            host="127.0.0.1",
            port=2299,
            ssh_user="gpu",
            workspace_path="/srv/keepalive",
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
            workspace_path="/srv/invalid-policy",
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
            workspace_path="/srv/invalid-idle-policy",
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
    first.initialize()
    admin = first.local_actor("policy-agent")
    first.configure_keepalive_policy(
        admin,
        "endpoint-a",
        "idle_keepalive",
        idempotency_key="runtime-policy",
    )

    restarted = BrokerService(database, configured)
    restarted.initialize(sync_inventory=True)
    assert restarted.get_endpoint_keepalive_summary("endpoint-a")["keepalive"]["policy"] == "idle_keepalive"


def test_enabling_keepalive_attaches_the_fixed_helper_to_an_endpoint(service, admin) -> None:
    service.ingest_observation(observation(count=2))

    configured = service.configure_keepalive_policy(
        admin,
        "endpoint-a",
        "idle_keepalive",
        idempotency_key="endpoint-enable",
    )

    assert configured["keepalive"]["configured"] is True
    assert configured["keepalive"]["policy"] == "idle_keepalive"
    endpoint = service.list_endpoints(admin)["data"][0]
    assert endpoint["keepalive_adapter_id"] == "server-script-v1"


def test_system_identity_is_tokenless_hidden_and_reserved(service, admin) -> None:
    with service.database.session() as session:
        assert session.get(Actor, SYSTEM_ACTOR_ID) is not None
        assert session.get(Project, SYSTEM_PROJECT_ID) is not None
        assert "api_tokens" not in inspect(session.bind).get_table_names()

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


def test_initialize_ignores_legacy_tokens_because_login_is_removed(
    tmp_path: Path, inventory
) -> None:  # noqa: ANN001
    root = Path(__file__).resolve().parents[1]
    broker = BrokerService(Database(f"sqlite:///{tmp_path / 'identity-token.sqlite3'}", root), inventory)
    broker.initialize()
    now = utcnow()
    with broker.database.engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE api_tokens ("
                "id VARCHAR(64) PRIMARY KEY, actor_id VARCHAR(128) NOT NULL, "
                "label VARCHAR(120) NOT NULL, legacy_value VARCHAR(255) NOT NULL, "
                "token_prefix VARCHAR(32) NOT NULL, created_at DATETIME NOT NULL)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO api_tokens "
                "(id, actor_id, label, legacy_value, token_prefix, created_at) "
                "VALUES (:id, :actor_id, :label, :legacy_value, :token_prefix, :created_at)"
            ),
            {
                "id": "legacy-system-token",
                "actor_id": SYSTEM_ACTOR_ID,
                "label": "legacy",
                "legacy_value": "legacy-unused-value",
                "token_prefix": "legacy",
                "created_at": now,
            },
        )

    broker.initialize()


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


def test_0020_preserves_legacy_tokens_retired_endpoints_and_keepalive_rows(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[1]
    database = Database(f"sqlite:///{tmp_path / 'legacy-0020.sqlite3'}", root)
    with database.engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE api_tokens (id VARCHAR(64) PRIMARY KEY, legacy_value TEXT)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE endpoints (id VARCHAR(128) PRIMARY KEY, lifecycle_state VARCHAR(32))"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE allocation_requests "
                "(id VARCHAR(64) PRIMARY KEY, priority_class VARCHAR(32))"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE leases ("
                "id VARCHAR(64) PRIMARY KEY, kind VARCHAR(16) NOT NULL, "
                "keepalive_scope VARCHAR(24), "
                "CONSTRAINT ck_lease_keepalive_scope CHECK ("
                "keepalive_scope IS NULL OR keepalive_scope IN ('gpu', 'legacy_endpoint')))"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE lease_resources "
                "(lease_id VARCHAR(64), gpu_id VARCHAR(256), active BOOLEAN)"
            )
        )
        connection.execute(text("INSERT INTO api_tokens VALUES ('old-token', 'preserve-me')"))
        connection.execute(
            text("INSERT INTO endpoints VALUES ('retired-server', 'retired')")
        )
        connection.execute(
            text("INSERT INTO allocation_requests VALUES ('keeper-request', 'keepalive')")
        )
        connection.execute(
            text("INSERT INTO leases VALUES ('keeper-lease', 'keepalive', 'legacy_endpoint')")
        )
        connection.execute(
            text("INSERT INTO lease_resources VALUES ('keeper-lease', 'gpu-0', 1)")
        )
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(
            text("INSERT INTO alembic_version VALUES ('20260812_0019')")
        )

    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "src" / "serverpilot" / "migrations"))
    config.set_main_option("sqlalchemy.url", database.url)
    command.upgrade(config, "20260812_0020")

    with database.engine.begin() as connection:
        assert connection.execute(text("SELECT * FROM api_tokens")).all() == [
            ("old-token", "preserve-me")
        ]
        assert connection.execute(text("SELECT * FROM endpoints")).all() == [
            ("retired-server", "retired")
        ]
        assert connection.execute(text("SELECT * FROM allocation_requests")).all() == [
            ("keeper-request", "keepalive")
        ]
        assert connection.execute(text("SELECT id, kind FROM leases")).all() == [
            ("keeper-lease", "keepalive")
        ]
        assert connection.execute(text("SELECT * FROM lease_resources")).all() == [
            ("keeper-lease", "gpu-0", 1)
        ]
