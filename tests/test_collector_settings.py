from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from serverpilot.api import create_app
from serverpilot.config import Settings
from serverpilot.models import RuntimeSetting
from serverpilot.schemas import CollectorSettingsUpdate
from serverpilot.service import BrokerService


def test_collector_setting_is_persisted_and_restored(service, admin, inventory, tmp_path: Path) -> None:
    initial = service.collector_settings(admin)["data"]
    assert initial == {
        "interval_seconds": 10,
        "stale_after_seconds": 30,
        "allowed_intervals": [5, 10, 30],
    }

    changed = service.update_collector_settings(
        admin,
        CollectorSettingsUpdate(interval_seconds=5),
        idempotency_key="collector-interval-5",
    )
    assert changed["settings"]["interval_seconds"] == 5
    assert changed["settings"]["stale_after_seconds"] == 15
    assert service.collector_interval_seconds() == 5

    replay = service.update_collector_settings(
        admin,
        CollectorSettingsUpdate(interval_seconds=5),
        idempotency_key="collector-interval-5",
    )
    assert replay["event_id"] == changed["event_id"]

    with service.database.session() as session:
        setting = session.scalar(select(RuntimeSetting))
        assert setting is not None
        assert setting.value == "5"

    restarted_inventory = inventory.model_copy(deep=True)
    restarted_inventory.collector.interval_seconds = 10
    restarted_inventory.collector.stale_after_seconds = 30
    restarted = BrokerService(service.database, restarted_inventory)
    restarted.initialize()
    assert restarted.collector_interval_seconds() == 5
    assert restarted.inventory.collector.stale_after_seconds == 15


def test_collector_setting_commit_failure_does_not_mutate_runtime(
    service, admin, monkeypatch
) -> None:
    session_type = service.database.Session.class_

    def fail_commit(_session) -> None:  # type: ignore[no-untyped-def]
        raise RuntimeError("forced commit failure")

    with monkeypatch.context() as patch:
        patch.setattr(session_type, "commit", fail_commit)
        with pytest.raises(RuntimeError, match="forced commit failure"):
            service.update_collector_settings(
                admin,
                CollectorSettingsUpdate(interval_seconds=5),
                idempotency_key="collector-interval-commit-failure",
            )

    assert service.collector_interval_seconds() == 10
    assert service.inventory.collector.stale_after_seconds == 30
    with service.database.session() as session:
        assert session.get(RuntimeSetting, "collector_interval_seconds") is None


def test_collector_settings_api_requires_supported_value_and_idempotency(
    tmp_path: Path, inventory
) -> None:
    project_root = Path(__file__).resolve().parents[1]
    inventory_path = tmp_path / "inventory.yaml"
    inventory_path.write_text(inventory.model_dump_json(), encoding="utf-8")
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'api.sqlite3'}",
        inventory_path=inventory_path,
        project_root=project_root,
        bootstrap_token="a" * 32,
        session_secret="test-secret",
    )
    app = create_app(settings)
    headers = {"X-ServerPilot-Actor": "bootstrap-admin"}
    with TestClient(app) as client:
        response = client.get("/api/v1/settings/collector", headers=headers)
        assert response.status_code == 200
        assert response.json()["data"]["interval_seconds"] == 10

        response = client.patch(
            "/api/v1/settings/collector",
            headers=headers,
            json={"interval_seconds": 5},
        )
        assert response.status_code == 422

        response = client.patch(
            "/api/v1/settings/collector",
            headers={**headers, "Idempotency-Key": "settings-5"},
            json={"interval_seconds": 7},
        )
        assert response.status_code == 422

        response = client.patch(
            "/api/v1/settings/collector",
            headers={**headers, "Idempotency-Key": "settings-5"},
            json={"interval_seconds": 5},
        )
        assert response.status_code == 200
        assert response.json()["settings"]["stale_after_seconds"] == 15
