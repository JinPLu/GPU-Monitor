from __future__ import annotations

import json
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from serverpilot.database import Database


def test_migration_upgrade_and_downgrade(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    database = Database(f"sqlite:///{tmp_path / 'migration.sqlite3'}", root)
    database.migrate()
    assert {
        "endpoints",
        "endpoint_telemetry_current",
        "endpoint_telemetry_snapshots",
        "gpu_devices",
        "telemetry_current",
        "leases",
        "lease_resources",
        "workload_profiles",
        "workload_profile_grants",
        "scheduler_targets",
        "scheduler_jobs",
        "scheduler_job_events",
        "scheduler_transfers",
        "lease_endpoint_commitments",
        "audit_events",
    }.issubset(
        inspect(database.engine).get_table_names()
    )
    gpu_columns = {column["name"] for column in inspect(database.engine).get_columns("gpu_devices")}
    assert {"present", "absent_at", "cuda_ordinal"}.issubset(gpu_columns)
    endpoint_columns = {
        column["name"] for column in inspect(database.engine).get_columns("endpoints")
    }
    lease_columns = {column["name"] for column in inspect(database.engine).get_columns("leases")}
    assert "keepalive_adapter_id" in endpoint_columns
    assert "workspace_path" in endpoint_columns
    assert "kind" in lease_columns
    expires_at = next(
        column for column in inspect(database.engine).get_columns("leases")
        if column["name"] == "expires_at"
    )
    assert expires_at["nullable"] is True
    endpoint_telemetry_columns = {
        column["name"]
        for column in inspect(database.engine).get_columns("endpoint_telemetry_current")
    }
    assert {
        "cpu_total_ticks",
        "cpu_idle_ticks",
        "cpu_utilization_pct",
        "cpu_usage_usec",
        "cpu_quota_usec",
        "cpu_period_usec",
    }.issubset(endpoint_telemetry_columns)
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "src" / "serverpilot" / "migrations"))
    config.set_main_option("sqlalchemy.url", database.url)
    command.downgrade(config, "base")
    assert "gpu_devices" not in inspect(database.engine).get_table_names()


def test_backup_and_safe_restore_target(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    database = Database(f"sqlite:///{tmp_path / 'source.sqlite3'}", root)
    database.migrate()
    backup = database.backup(tmp_path / "backups" / "snapshot.sqlite3")
    restored = Database.restore_to(backup, tmp_path / "restored.sqlite3")
    assert restored.is_file()
    assert "endpoints" in inspect(Database(f"sqlite:///{restored}", root).engine).get_table_names()


def test_backup_atomically_replaces_prior_output_and_refuses_live_database(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[1]
    source = tmp_path / "source.sqlite3"
    database = Database(f"sqlite:///{source}", root)
    database.migrate()
    destination = tmp_path / "backups" / "snapshot.sqlite3"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"incomplete prior backup")

    backup = database.backup(destination)

    assert backup == destination.resolve()
    assert "endpoints" in inspect(Database(f"sqlite:///{backup}", root).engine).get_table_names()
    assert list(destination.parent.glob(f".{destination.name}.*.tmp")) == []
    with pytest.raises(ValueError, match="must differ"):
        database.backup(source)


def test_migration_upgrades_existing_schema_to_endpoint_telemetry(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    database = Database(f"sqlite:///{tmp_path / 'upgrade.sqlite3'}", root)
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "src" / "serverpilot" / "migrations"))
    config.set_main_option("sqlalchemy.url", database.url)

    command.upgrade(config, "20260719_0002")
    assert "telemetry_current" in inspect(database.engine).get_table_names()
    assert "endpoint_telemetry_current" not in inspect(database.engine).get_table_names()

    command.upgrade(config, "head")
    assert "endpoint_telemetry_current" in inspect(database.engine).get_table_names()
    assert "endpoint_telemetry_snapshots" in inspect(database.engine).get_table_names()
    assert "workload_profiles" in inspect(database.engine).get_table_names()
    assert "scheduler_targets" in inspect(database.engine).get_table_names()
    endpoint_columns = {
        column["name"] for column in inspect(database.engine).get_columns("endpoints")
    }
    assert {"owner_project_id", "lifecycle_state"}.issubset(endpoint_columns)
    assert "workspace_path" in endpoint_columns
    gpu_columns = {column["name"] for column in inspect(database.engine).get_columns("gpu_devices")}
    assert {"present", "absent_at", "cuda_ordinal"}.issubset(gpu_columns)
    assert "lease_endpoint_commitments" in inspect(database.engine).get_table_names()
    with database.engine.connect() as connection:
        assert connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one() == ScriptDirectory.from_config(config).get_current_head()


def test_workspace_migration_preserves_legacy_endpoints_without_inventing_paths(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[1]
    database = Database(f"sqlite:///{tmp_path / 'workspace-upgrade.sqlite3'}", root)
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "src" / "serverpilot" / "migrations"))
    config.set_main_option("sqlalchemy.url", database.url)

    command.upgrade(config, "20260812_0021")
    with database.engine.begin() as connection:
        columns = {
            column["name"] for column in inspect(database.engine).get_columns("endpoints")
        }
        if "workspace_path" in columns:
            connection.execute(text("ALTER TABLE endpoints DROP COLUMN workspace_path"))
        connection.execute(
            text(
                """
                INSERT INTO endpoints (
                    id, host, port, ssh_user, observation_profile,
                    keepalive_policy, labels_json, lifecycle_state, enabled,
                    created_at, updated_at
                ) VALUES (
                    'legacy-endpoint', '127.0.0.1', 2222, 'gpu', 'linux-nvidia',
                    'disabled', '[]', 'active', 1,
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            )
        )

    command.upgrade(config, "head")
    with database.engine.connect() as connection:
        row = connection.execute(
            text("SELECT id, workspace_path FROM endpoints WHERE id = 'legacy-endpoint'")
        ).one()
        count = connection.execute(text("SELECT COUNT(*) FROM endpoints")).scalar_one()

    assert row.id == "legacy-endpoint"
    assert row.workspace_path is None
    assert count == 1


def test_keepalive_persistence_migration_changes_only_active_ownership(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[1]
    database = Database(f"sqlite:///{tmp_path / 'keepalive-upgrade.sqlite3'}", root)
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "src" / "serverpilot" / "migrations"))
    config.set_main_option("sqlalchemy.url", database.url)
    command.upgrade(config, "20260813_0022")

    with database.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO projects ("
                "id, display_name, weight, concurrency_limit, enabled, created_at, updated_at"
                ") VALUES "
                "('project-a', 'Project A', 1, 1, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP), "
                "('__serverpilot_keepalive__', 'Keepalive', 1, 1, 1, "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO actors (id, display_name, role, enabled, created_at, updated_at) "
                "VALUES ('__serverpilot_keepalive__', 'Keepalive', 'admin', 1, "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO endpoints ("
                "id, host, port, ssh_user, observation_profile, keepalive_policy, "
                "labels_json, lifecycle_state, enabled, created_at, updated_at"
                ") VALUES ('endpoint-a', '127.0.0.1', 22, 'gpu', 'linux-nvidia', "
                "'idle_keepalive', '[]', 'active', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO gpu_devices ("
                "id, endpoint_id, gpu_uuid, gpu_index, name, total_vram_mib, labels_json, "
                "health, enabled, present, first_seen_at, last_seen_at"
                ") VALUES ('gpu-a', 'endpoint-a', 'GPU-a', 0, 'GPU', 80000, '[]', "
                "'OK', 1, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        for request_id, actor_id, project_id, priority, state in (
            ("active-request", "__serverpilot_keepalive__", "__serverpilot_keepalive__", "keepalive", "ACTIVE"),
            ("released-request", "__serverpilot_keepalive__", "__serverpilot_keepalive__", "keepalive", "RELEASED"),
            ("workload-request", "__serverpilot_keepalive__", "project-a", "normal", "ACTIVE"),
        ):
            connection.execute(
                text(
                    "INSERT INTO allocation_requests ("
                    "id, actor_id, project_id, auto_activate, task_ref, purpose, "
                    "constraints_json, duration_seconds, state, priority_class, created_at, updated_at"
                    ") VALUES (:id, :actor, :project, 0, :id, :id, '{}', 600, :state, "
                    ":priority, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {
                    "id": request_id,
                    "actor": actor_id,
                    "project": project_id,
                    "state": state,
                    "priority": priority,
                },
            )
        for lease_id, request_id, kind, state, expiry in (
            ("active-keeper", "active-request", "keepalive", "ACTIVE", "2026-08-13 12:00:00"),
            ("released-keeper", "released-request", "keepalive", "RELEASED", "2026-08-13 13:00:00"),
            ("active-workload", "workload-request", "workload", "ACTIVE", "2026-08-13 14:00:00"),
        ):
            connection.execute(
                text(
                    "INSERT INTO leases ("
                    "id, request_id, actor_id, project_id, kind, state, issued_at, expires_at, "
                    "last_heartbeat_at, issued_revision"
                    ") VALUES (:id, :request, '__serverpilot_keepalive__', "
                    ":project, :kind, :state, CURRENT_TIMESTAMP, :expiry, CURRENT_TIMESTAMP, 1)"
                ),
                {
                    "id": lease_id,
                    "request": request_id,
                    "project": "project-a" if kind == "workload" else "__serverpilot_keepalive__",
                    "kind": kind,
                    "state": state,
                    "expiry": expiry,
                },
            )
        connection.execute(
            text(
                "INSERT INTO lease_resources (lease_id, gpu_id, active) "
                "VALUES ('active-keeper', 'gpu-a', 1)"
            )
        )

    command.upgrade(config, "head")
    with database.engine.connect() as connection:
        expiries = dict(
            connection.execute(text("SELECT id, expires_at FROM leases")).all()
        )
        current_columns = {
            column["name"] for column in inspect(database.engine).get_columns("keepalive_current")
        }
        migrated_cuda_ordinal = connection.execute(
            text("SELECT cuda_ordinal FROM gpu_devices WHERE id = 'gpu-a'")
        ).scalar_one()

    assert expiries["active-keeper"] is None
    assert expiries["released-keeper"] is not None
    assert expiries["active-workload"] is not None
    assert {"expected_pid", "expected_boot_id", "expected_process_started_at"}.issubset(
        current_columns
    )
    assert migrated_cuda_ordinal is None


def test_scheduler_transport_migration_scrubs_legacy_argv_and_disables_target(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[1]
    database = Database(f"sqlite:///{tmp_path / 'scheduler-upgrade.sqlite3'}", root)
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "src" / "serverpilot" / "migrations"))
    config.set_main_option("sqlalchemy.url", database.url)

    command.upgrade(config, "20260809_0012")
    with database.engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO scheduler_targets (
                    id, display_name, adapter, connection_json, credential_refs_json,
                    capabilities_json, access_hint, enabled, created_at, updated_at
                ) VALUES (
                    :id, :display_name, :adapter, :connection_json, :credential_refs_json,
                    :capabilities_json, :access_hint, :enabled, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            ),
            {
                "id": "legacy-target",
                "display_name": "Legacy target",
                "adapter": "slurm-command",
                "connection_json": json.dumps(
                    {
                        "command_prefix": ["/tmp/legacy-helper", "--site"],
                        "upload": {"ssh_host": "example.test"},
                    }
                ),
                "credential_refs_json": "{}",
                "capabilities_json": "[\"access-status\"]",
                "access_hint": "reconfigure transport",
                "enabled": True,
            },
        )

    command.upgrade(config, "head")
    with database.engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT connection_json, enabled, access_status, access_message "
                "FROM scheduler_targets WHERE id = 'legacy-target'"
            )
        ).one()

    migrated = json.loads(row.connection_json)
    assert "command_prefix" not in migrated
    assert migrated["transport_profile"] == "unconfigured"
    assert migrated["inspection_profile"] == "slurm-basic"
    assert migrated["upload"] == {"ssh_host": "example.test"}
    assert not row.enabled
    assert row.access_status == "unconfigured"
    assert "administrator" in row.access_message


def test_migration_uses_packaged_scripts_without_project_tree(tmp_path: Path) -> None:
    database = Database(
        f"sqlite:///{tmp_path / 'packaged.sqlite3'}",
        tmp_path / "no-source-release",
    )

    database.migrate()

    inspector = inspect(database.engine)
    assert "endpoint_telemetry_current" in inspector.get_table_names()
    assert "endpoint_telemetry_snapshots" in inspector.get_table_names()
    gpu_columns = {column["name"] for column in inspector.get_columns("gpu_devices")}
    assert {"present", "absent_at"}.issubset(gpu_columns)
