from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from gpu_broker.database import Database


def test_migration_upgrade_and_downgrade(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    database = Database(f"sqlite:///{tmp_path / 'migration.sqlite3'}", root)
    database.migrate()
    assert {
        "endpoints",
        "gpu_devices",
        "telemetry_current",
        "leases",
        "lease_resources",
        "audit_events",
    }.issubset(
        inspect(database.engine).get_table_names()
    )
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
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
