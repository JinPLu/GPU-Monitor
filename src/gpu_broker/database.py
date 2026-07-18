"""SQLite WAL setup, Alembic migration, readiness and recoverable backup helpers."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker


class Database:
    def __init__(self, url: str, project_root: Path) -> None:
        self.url = url
        self.project_root = project_root
        parsed = make_url(url)
        if parsed.get_backend_name() != "sqlite":
            raise ValueError("pilot only supports SQLite; migrate to PostgreSQL before multi-writer deployment")
        database = parsed.database
        if database and database != ":memory:":
            Path(database).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self.engine: Engine = create_engine(
            url,
            connect_args={"check_same_thread": False, "timeout": 30},
            future=True,
        )
        event.listen(self.engine, "connect", self._configure_sqlite)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)

    @staticmethod
    def _configure_sqlite(dbapi_connection, _connection_record) -> None:  # type: ignore[no-untyped-def]
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

    def migrate(self) -> None:
        config = Config(str(self.project_root / "alembic.ini"))
        config.set_main_option("script_location", str(self.project_root / "migrations"))
        config.set_main_option("sqlalchemy.url", self.url)
        command.upgrade(config, "head")

    def session(self) -> Session:
        return self.Session()

    def ready(self) -> bool:
        try:
            with self.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            return True
        except Exception:  # readiness must never leak a DB URL/credential
            return False

    def backup(self, destination: Path) -> Path:
        """Create a recoverable SQLite copy after checkpointing the WAL."""

        parsed = make_url(self.url)
        if not parsed.database or parsed.database == ":memory:":
            raise ValueError("cannot back up an in-memory database")
        source = Path(parsed.database).expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self.engine.connect() as connection:
            connection.execute(text("PRAGMA wal_checkpoint(FULL)"))
        shutil.copy2(source, destination)
        return destination

    @staticmethod
    def restore_to(source: Path, destination: Path) -> Path:
        """Validate a SQLite backup and restore only to a new explicit target path.

        The method intentionally refuses overwrite; changing a live control-plane
        database is a deployment action, not a routine CLI side effect.
        """

        source = source.expanduser().resolve()
        destination = destination.expanduser().resolve()
        if not source.is_file():
            raise ValueError(f"backup does not exist: {source}")
        if destination.exists():
            raise ValueError(f"refusing to overwrite restore target: {destination}")
        with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            raise ValueError("backup integrity check failed")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        return destination
