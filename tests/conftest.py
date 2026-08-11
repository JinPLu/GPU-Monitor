from __future__ import annotations

from pathlib import Path

import pytest

from serverpilot.config import EndpointConfig, InventoryConfig, ProjectConfig
from serverpilot.database import Database
from serverpilot.service import ActorContext, BrokerService


@pytest.fixture
def inventory() -> InventoryConfig:
    return InventoryConfig(
        schema_version=1,
        projects=[
            ProjectConfig(id="project-a", display_name="Project A", weight=1),
            ProjectConfig(id="project-b", display_name="Project B", weight=1),
        ],
        endpoints=[
            EndpointConfig(
                id="endpoint-a",
                host="127.0.0.1",
                port=2201,
                ssh_user="gpu",
                labels=["direct-ssh", "test"],
                storage_group="test-storage",
                project_ids=["project-a", "project-b"],
            ),
            EndpointConfig(
                id="endpoint-b",
                host="127.0.0.1",
                port=2202,
                ssh_user="gpu",
                labels=["direct-ssh", "test"],
                storage_group="test-storage",
                project_ids=["project-a", "project-b"],
            ),
        ],
    )


@pytest.fixture
def service(tmp_path: Path, inventory: InventoryConfig) -> BrokerService:
    project_root = Path(__file__).resolve().parents[1]
    broker = BrokerService(Database(f"sqlite:///{tmp_path / 'broker.sqlite3'}", project_root), inventory)
    broker.initialize("a" * 32)
    return broker


@pytest.fixture
def admin(service: BrokerService) -> ActorContext:
    return service.authenticate("a" * 32)
